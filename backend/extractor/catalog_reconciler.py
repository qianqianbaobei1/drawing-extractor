# -*- coding: utf-8 -*-
"""图纸目录 vs 实际提取覆盖对账引擎 (Drawing Catalog Reconciliation).

根治"范围缺失"导致的漏柜（如 JX1~JX21 漏柜是因为配电箱系统图图幅根本未进流水线）。
通过识别图纸目录/图纸清单，将设计声明包含的配电箱与实际解析出的箱体进行原子级对账。
若发现整张图幅或箱体范围缺失，立即给出 ERROR 级范围告警，避免带病交付。
"""

from __future__ import annotations
import re
from typing import Any, Iterable, List, Optional, Set

from .schema import (
    Box,
    CatalogItem,
    CatalogReconciliation,
    Uncertainty,
    normalize_code,
)

# 常见低压配电箱/柜代号前缀
PANEL_PREFIX_PATTERN = r"(?:AL|AP|AT|AW|AA|AH|AM|APE|ALE|ATE|AWE|JX|KZ|XF|P|GGD|GCK|MNS|XL|AC|AK)"


def expand_panel_range(text: str) -> list[str]:
    """将图纸目录中的配电箱范围表达式展开为离散编号列表。

    示例：
    - "1AL1~1AL5" -> ["1AL1", "1AL2", "1AL3", "1AL4", "1AL5"]
    - "1AL1~5"    -> ["1AL1", "1AL2", "1AL3", "1AL4", "1AL5"]
    - "JX1~JX21"  -> ["JX1", "JX2", ..., "JX21"]
    - "JX1~21"    -> ["JX1", "JX2", ..., "JX21"]
    - "2AP1-4"    -> ["2AP1", "2AP2", "2AP3", "2AP4"]
    - "1AL1, 1AL2, 2AP1" -> ["1AL1", "1AL2", "2AP1"]
    """
    if not text:
        return []

    cleaned = text.strip()
    results: list[str] = []
    seen: set[str] = set()

    def add_code(c: str):
        nc = normalize_code(c)
        if nc and nc not in seen:
            seen.add(nc)
            results.append(nc)

    # 1. 匹配带波浪号或短横线的区间写法：如 1AL1~1AL5, JX1~21, 1AP1~5, 1AL1-1AL5
    range_regex = re.compile(
        r"([A-Za-z0-9_\-]+?)"                    # 前缀+前编号前段
        r"(\d+)"                                # 前编号尾部连续数字
        r"\s*(~|至|到|-|–|—)\s*"                # 范围连接符
        r"([A-Za-z0-9_\-]+?)?"                  # 后编号可选前缀
        r"(\d+)",                               # 后编号尾部数字
        re.IGNORECASE
    )

    remaining_text = cleaned
    for match in range_regex.finditer(cleaned):
        prefix1, num1_str, sep, prefix2, num2_str = match.groups()
        prefix2 = prefix2 or ""

        norm_p1 = prefix1.upper()
        norm_p2 = prefix2.upper()

        if not norm_p2 or norm_p1 == norm_p2:
            effective_prefix = prefix1
            try:
                start_n = int(num1_str)
                end_n = int(num2_str)
                # 范围需 end_n > start_n，且跨度合理 (1 ~ 150)
                if 0 < end_n - start_n <= 150 and start_n > 0:
                    for i in range(start_n, end_n + 1):
                        add_code(f"{effective_prefix}{i}")
                    remaining_text = remaining_text.replace(match.group(0), " ")
            except ValueError:
                pass

    # 2. 如果包含逗号、顿号、斜杠、空格或汉字包围的离散箱体
    # 如 "1AL1, 1AL2, 2AP1" 或 "AP1/AP2/AP3" 或 "01B-01 1AL1配电箱系统图"
    token_regex = re.compile(
        rf"(?<![A-Za-z0-9_\-])((?:[B\-\d]{{1,4}})?{PANEL_PREFIX_PATTERN}[A-Za-z0-9_\-]*\d+[A-Za-z0-9_\-]*)(?![A-Za-z0-9_\-])",
        re.IGNORECASE
    )
    for match in token_regex.finditer(remaining_text):
        code = match.group(1)
        add_code(code)

    return results


class DrawingCatalogReconciler:
    """图纸目录与实际提取覆盖核对器。"""

    def __init__(self, catalog_items: list[CatalogItem] | None = None):
        self.catalog_items = list(catalog_items or [])
        # 绑定实例上的 reconcile 方法，支持 reconciler.reconcile(boxes, source_name)
        self.reconcile = lambda extracted_boxes, source_name="CAD图纸目录/PDF清单": DrawingCatalogReconciler.reconcile(
            self.catalog_items, extracted_boxes, source_name
        )

    def __iter__(self):
        return iter(self.catalog_items)

    def __len__(self):
        return len(self.catalog_items)

    def __getitem__(self, idx):
        return self.catalog_items[idx]

    @classmethod
    def parse_catalog_line(cls, line: str) -> Optional[CatalogItem]:
        """从图纸目录单行文本中解析图号、图名与所辖配电箱。"""
        if not line or not line.strip():
            return None
        text = line.strip()

        # 过滤纯表头
        if any(h in text for h in ("图纸目录", "序号", "图名", "图幅", "版本", "设计阶段", "比例")):
            if not re.search(r"\d", text):
                return None

        # 尝试匹配图号与图名：
        # 常见形态："01B-03 动力配电箱系统图(三) JX1~JX21" 或 "D-02 低压配电系统图(一)"
        sheet_no = ""
        sheet_title = text

        m_no = re.search(r"([A-Za-z0-9_\-]{2,12})\s+(.+)", text)
        if m_no:
            sheet_no = m_no.group(1).strip()
            sheet_title = m_no.group(2).strip()

        # 提取所声明的配电箱范围
        declared = expand_panel_range(sheet_title)
        if not declared and not sheet_no:
            return None

        return CatalogItem(
            sheet_no=sheet_no,
            sheet_title=sheet_title,
            declared_panels=declared,
            matched_panels=[],
            missing_panels=list(declared),
            status="COVERED" if not declared else "MISSING",
        )

    @classmethod
    def from_records(cls, records: list[dict[str, Any]]) -> "DrawingCatalogReconciler":
        """从字典列表快捷构建 DrawingCatalogReconciler 实例。支持 sheet_no, sheet_title, declared_panels 等键。"""
        items: list[CatalogItem] = []
        for r in records:
            sheet_no = str(r.get("sheet_no", "")).strip()
            sheet_title = str(r.get("sheet_title", "")).strip()
            declared = r.get("declared_panels")
            if declared is None:
                declared = expand_panel_range(sheet_title)
            items.append(CatalogItem(
                sheet_no=sheet_no,
                sheet_title=sheet_title,
                declared_panels=list(declared),
                missing_panels=list(declared),
                status="COVERED" if not declared else "MISSING",
            ))
        return cls(items)

    @classmethod
    def reconcile(
        cls,
        catalog_items: list[CatalogItem] | DrawingCatalogReconciler,
        extracted_boxes: list[Box] | list[str],
        source_name: str = "CAD图纸目录/PDF清单",
    ) -> CatalogReconciliation:
        """执行图纸目录与提取结果的对账。支持 extracted_boxes 传入 Box 或 str 列表。"""
        if isinstance(catalog_items, DrawingCatalogReconciler):
            catalog_items = catalog_items.catalog_items

        extracted_codes: set[str] = set()
        for b in extracted_boxes:
            if isinstance(b, Box):
                if b.code and b.code.strip():
                    extracted_codes.add(normalize_code(b.code))
            elif isinstance(b, str) and b.strip():
                extracted_codes.add(normalize_code(b))

        total_declared_set: set[str] = set()
        covered_set: set[str] = set()
        missing_set: set[str] = set()

        processed_items: list[CatalogItem] = []

        for item in catalog_items:
            declared = item.declared_panels
            if not declared:
                processed_items.append(item)
                continue

            item_matched = []
            item_missing = []
            for code in declared:
                norm_c = normalize_code(code)
                total_declared_set.add(norm_c)
                if norm_c in extracted_codes:
                    item_matched.append(code)
                    covered_set.add(norm_c)
                else:
                    item_missing.append(code)
                    missing_set.add(norm_c)

            # 更新 item 状态
            if not item_missing:
                item_status = "COVERED"
            elif not item_matched:
                item_status = "MISSING"
            else:
                item_status = "PARTIAL"

            updated_item = CatalogItem(
                sheet_no=item.sheet_no,
                sheet_title=item.sheet_title,
                declared_panels=declared,
                matched_panels=item_matched,
                missing_panels=item_missing,
                status=item_status,
            )
            processed_items.append(updated_item)

        total_count = len(total_declared_set)
        covered_count = len(covered_set)
        missing_count = len(missing_set)
        coverage_rate = (covered_count / total_count) if total_count > 0 else 1.0

        return CatalogReconciliation(
            has_catalog=bool(catalog_items),
            catalog_source=source_name,
            total_declared_panels=total_count,
            covered_count=covered_count,
            missing_count=missing_count,
            coverage_rate=round(coverage_rate, 4),
            items=processed_items,
            missing_box_codes=sorted(list(missing_set)),
        )

    @classmethod
    def generate_reconciliation_issues(
        cls,
        reconciliation: CatalogReconciliation,
    ) -> list[Uncertainty]:
        """根据对账结果生成 ERROR 级或 WARNING 级待核对项。"""
        issues: list[Uncertainty] = []
        if not reconciliation.has_catalog or reconciliation.missing_count == 0:
            return issues

        for item in reconciliation.items:
            if item.status == "MISSING" and item.missing_panels:
                sample = ", ".join(item.missing_panels[:5])
                count = len(item.missing_panels)
                suffix = f" 等共 {count} 台" if count > 5 else f" (共 {count} 台)"
                issues.append(
                    Uncertainty(
                        location="图纸目录对账",
                        detail=(
                            f"【范围缺失】图纸目录声明图幅 {item.sheet_no or '(未标图号)'} "
                            f"《{item.sheet_title}》包含配电箱 [{sample}{suffix}]，"
                            f"但在本次提取中全未找到！疑似系统图图幅漏传或未纳入切片范围，请核对补传！"
                        ),
                        severity="ERROR",
                        source="program",
                    )
                )
            elif item.status == "PARTIAL" and item.missing_panels:
                sample = ", ".join(item.missing_panels[:5])
                count = len(item.missing_panels)
                suffix = f" 等共 {count} 台" if count > 5 else f" (共 {count} 台)"
                issues.append(
                    Uncertainty(
                        location="图纸目录对账",
                        detail=(
                            f"【部分缺失】图纸目录声明图幅 {item.sheet_no or ''} "
                            f"《{item.sheet_title}》中部分配电箱 [{sample}{suffix}] 未提取到，请核对图纸局部"
                        ),
                        severity="WARNING",
                        source="program",
                    )
                )

        return issues
