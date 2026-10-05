# -*- coding: utf-8 -*-
"""保守核验提取结果中的矛盾、级配与证据政策合规性。

门禁分级设计 (ERROR / WARNING / INFO)：
- ERROR: 违背电气拓扑常理、范围缺失或严重违反证据政策（如回路重名、引用不存在的箱体、关键字段凭空编造）；
- WARNING: 存在潜在工程疑点，提示人工核对，不强制阻断（如三相偏载、负荷倒挂、相序非国标标示）；
- INFO: 工程设计提示与优化建议。
"""

from __future__ import annotations
from collections import defaultdict
from enum import Enum
from math import isfinite
import re
from typing import Any, List, Optional

from pydantic import BaseModel, Field

from .assemble import PREFIXES, _parse_devices, is_incoming_circuit
from .config import domain as _domain

_CHECKS = _domain()["checks"]
from .schema import (
    ExtractionResult,
    EvidenceType,
    FIELD_EVIDENCE_POLICY,
    PHYSICAL_EVIDENCE_ORIGINS,
    ReviewStatus,
    validate_field_evidence,
)

# 回路 breaker 字段能汇总出的元器件名称，必须与 assemble 保持一致
BREAKER_CATEGORIES = {name for _, name in PREFIXES} | {"断路器（其他）"}

# 修复型号正则：使用 [CD] 而非 [C|D]，杜绝匹配字面 '|'，支持 C16A 等后缀
BREAKER_MODEL_REGEX = re.compile(
    r"^[A-Z0-9]+(?:-[A-Z0-9]+)*-[CD][0-9]{1,3}A?/[1-4]P(?:\+N)?$",
    re.IGNORECASE
)


class CheckSeverity(str, Enum):
    ERROR = "ERROR"      # 严重错误、范围缺失或违背证据政策
    WARNING = "WARNING"  # 存疑，需人工复核
    INFO = "INFO"        # 工程提示与规范建议


class CheckIssue(BaseModel):
    rule_code: str = Field(..., description="规则代号")
    severity: str = Field(CheckSeverity.WARNING.value, description="ERROR / WARNING / INFO")
    target: str = Field("", description="对象，如箱体 1AL1 / 回路 WL3")
    detail: str = Field(..., description="问题详情描述")

    @property
    def text(self) -> str:
        """返回单行文本，便于向后兼容现有 Uncertainty.from_text 与测试断言。"""
        return self.detail


def _spec(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def clean_rated_amp(val: Any) -> Optional[float]:
    """安全清洗提取额定电流数值，杜绝 float("C63") 引发 ValueError 导致服务崩溃。

    支持格式：
    - 纯数值: 63, 100.5
    - 脱扣+电流: "C63", "D100", "B16", "C16/2P", "C16A/1P", "C10A"
    - 带单位: "63A", "100安"
    - 带极数/斜杠: "100/3P", "/C63/3P", "In=63A", "In: 100A"
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val) if isfinite(val) and val > 0 else None
    
    s = str(val).strip()
    if not s:
        return None

    # 1. 显式 In 标注或带单位 A: 如 In=63A, In: 100A, 100A, 63A
    m_exp = re.search(r"\bIN\s*[:=]?\s*([1-9]\d{0,3}(?:\.\d+)?)\s*A?\b", s, re.IGNORECASE)
    if m_exp:
        try:
            v = float(m_exp.group(1))
            if isfinite(v) and v > 0:
                return v
        except ValueError:
            pass

    # 2. 脱扣特性 + 电流: 如 C63, D100, B16, C16/2P, /C63/3P, C16A, C16A/1P, D32A/3P
    m_curve = re.search(r"(?:/|-|\b)[CDB]\s*([1-9]\d{0,3}(?:\.\d+)?)\s*A?(?:/[1-4]P|/|\s|\b|$)", s, re.IGNORECASE)
    if m_curve:
        try:
            v = float(m_curve.group(1))
            if isfinite(v) and v > 0:
                return v
        except ValueError:
            pass

    # 3. 显式带单位 A: 如 100A, 63A, 16A/1P
    m_a = re.search(r"(?:/|-|\b)([1-9]\d{0,3}(?:\.\d+)?)\s*A\b", s, re.IGNORECASE)
    if m_a:
        try:
            v = float(m_a.group(1))
            if isfinite(v) and v > 0:
                return v
        except ValueError:
            pass

    # 4. 开头纯数字带极数或斜杠: 如 100/3P, 63/4P, 63
    m_num = re.search(r"^([1-9]\d{0,3}(?:\.\d+)?)(?:/[1-4]P|/|\s|$)", s, re.IGNORECASE)
    if m_num:
        try:
            v = float(m_num.group(1))
            if isfinite(v) and v > 0:
                return v
        except ValueError:
            pass

    return None




def _is_valid_phase(phase: str) -> bool:
    """电气相序合法性判定：兼容工程常用的单相/三相全套国标标注格式。"""
    if not phase:
        return True
    p = re.sub(r"[\s,./~_、\-]+", "", phase).upper()
    if p in set(_CHECKS["phase_valid_tokens"]):
        return True
    return any(re.match(pattern, p) for pattern in _CHECKS["phase_valid_regexes"])


def check_result_issues(result: ExtractionResult) -> list[CheckIssue]:
    """多维度结构化工程校验器，输出带级别 (ERROR/WARNING/INFO) 的 CheckIssue 列表。"""
    issues: list[CheckIssue] = []
    boxes = {box.code.strip(): box for box in result.boxes if box.code.strip()}

    # 1. 箱体数量校验
    for box in result.boxes:
        if box.quantity <= 0:
            issues.append(CheckIssue(
                rule_code="BOX_QTY_INVALID",
                severity=CheckSeverity.ERROR.value,
                target=f"箱体 {box.code or '(未编号)'}",
                detail=f"箱体 {box.code or '(未编号)'} 数量为 {box.quantity}，请核对"
            ))

    # 2. 回路引用与相序校验
    for index, circuit in enumerate(result.circuits, 1):
        code = circuit.box.strip()
        if code and code not in boxes:
            issues.append(CheckIssue(
                rule_code="BOX_NOT_FOUND",
                severity=CheckSeverity.ERROR.value,
                target=f"第 {index} 条回路",
                detail=f"第 {index} 条回路引用箱体 {code}，箱体清单中未找到，请核对"
            ))
        if circuit.phase and not _is_valid_phase(circuit.phase):
            issues.append(CheckIssue(
                rule_code="PHASE_INVALID",
                severity=CheckSeverity.WARNING.value,
                target=f"第 {index} 条回路",
                detail=f"第 {index} 条回路相序“{circuit.phase}”不在常见值中，请对照图纸核对"
            ))

    # 3. 元器件数量与单位校验
    for index, component in enumerate(result.components, 1):
        if not isfinite(component.quantity) or component.quantity <= 0:
            issues.append(CheckIssue(
                rule_code="COMPONENT_QTY_INVALID",
                severity=CheckSeverity.ERROR.value,
                target=f"第 {index} 项元器件",
                detail=f"第 {index} 项元器件 {component.name or component.spec or '(未命名)'} 数量无效，请核对"
            ))
        if component.unit and component.unit not in set(_CHECKS["allowed_units"]):
            issues.append(CheckIssue(
                rule_code="COMPONENT_UNIT_SUSPECT",
                severity=CheckSeverity.WARNING.value,
                target=f"第 {index} 项元器件",
                detail=f"第 {index} 项元器件单位“{component.unit}”需对照图纸核对"
            ))

    # 4. 回路器件与汇总清单对比核验
    expected: dict[str, int] = defaultdict(int)
    for circuit in result.circuits:
        devices = _parse_devices(circuit.breaker, allow_combo=True)
        if not devices:
            continue
        box = boxes.get(circuit.box.strip())
        if box is None or box.quantity <= 0:
            continue
        for spec, count in devices:
            expected[_spec(spec)] += count * box.quantity

    actual: dict[str, float] = defaultdict(float)
    for component in result.components:
        if component.name in BREAKER_CATEGORIES and component.spec.strip():
            actual[_spec(component.spec)] += component.quantity

    for spec, count in expected.items():
        if spec not in actual:
            issues.append(CheckIssue(
                rule_code="BREAKER_COUNT_MISMATCH",
                severity=CheckSeverity.WARNING.value,
                target=f"断路器 {spec}",
                detail=f"断路器 {spec}：回路逐条计数 {count} 只，元器件汇总未找到同规格项，请核对"
            ))
        elif actual[spec] != count:
            issues.append(CheckIssue(
                rule_code="BREAKER_COUNT_MISMATCH",
                severity=CheckSeverity.WARNING.value,
                target=f"断路器 {spec}",
                detail=f"断路器 {spec}：回路逐条计数 {count} 只，元器件汇总 {actual[spec]:g} 只，请核对"
            ))

    # 5. 三相负荷平衡度校验（国标限值 15%，按箱体独立核验）
    circuits_by_box: dict[str, list[Circuit]] = defaultdict(list)
    for c in result.circuits:
        box_name = (c.box or "").strip() or "未指定箱体"
        circuits_by_box[box_name].append(c)

    for box_name, b_circuits in circuits_by_box.items():
        phase_loads = {"L1": 0.0, "L2": 0.0, "L3": 0.0}
        has_loads = False
        for c in b_circuits:
            if not c.power_kw:
                continue
            try:
                val_match = re.search(r"(\d+(?:\.\d+)?)", c.power_kw)
                if not val_match:
                    continue
                val = float(val_match.group(1))
                p = (c.phase or "").upper().strip()
                if p == "L1":
                    phase_loads["L1"] += val
                    has_loads = True
                elif p == "L2":
                    phase_loads["L2"] += val
                    has_loads = True
                elif p == "L3":
                    phase_loads["L3"] += val
                    has_loads = True
                elif p in set(_CHECKS["three_phase_markers"]):
                    phase_loads["L1"] += val / 3.0
                    phase_loads["L2"] += val / 3.0
                    phase_loads["L3"] += val / 3.0
                    has_loads = True
            except ValueError:
                pass

        if has_loads:
            p_vals = [phase_loads["L1"], phase_loads["L2"], phase_loads["L3"]]
            p_max, p_min = max(p_vals), min(p_vals)
            if p_max > float(_CHECKS["phase_unbalance_min_total_kw"]) and (
                    phase_loads["L1"] > 0 and phase_loads["L2"] > 0 and phase_loads["L3"] > 0):
                unbalance = ((p_max - p_min) / p_max) * 100.0
                if unbalance > float(_CHECKS["phase_unbalance_warn_pct"]):
                    box_prefix = f"（{box_name}）" if box_name != "未指定箱体" else ""
                    issues.append(CheckIssue(
                        rule_code="PHASE_UNBALANCE",
                        severity=CheckSeverity.WARNING.value,
                        target=f"{box_name} 三相负荷平衡",
                        detail=(
                            f"三相负荷平衡核验{box_prefix}：L1={phase_loads['L1']:.1f}kW, L2={phase_loads['L2']:.1f}kW, L3={phase_loads['L3']:.1f}kW，"
                            f"三相负荷不平衡度达 {unbalance:.1f}%（超出国标15%限值），存在偏载风险，请核对配电分配"
                        )
                    ))

    # 6. 进出线开关级配防越级跳闸核验（按箱体独立核验，杜绝跨箱体串报）
    for box_name, b_circuits in circuits_by_box.items():
        incoming_amps = []
        outgoing_circuits = []
        for c in b_circuits:
            breaker_spec = c.breaker or ""
            amp = clean_rated_amp(breaker_spec)
            is_incoming = is_incoming_circuit(c)
            if is_incoming and amp:
                incoming_amps.append(amp)
            elif amp and not is_incoming:
                outgoing_circuits.append((c.circuit_no or "出线回路", breaker_spec, amp))

        if incoming_amps:
            min_incoming = min(incoming_amps)
            for c_no, spec, amp in outgoing_circuits:
                if amp > min_incoming:
                    box_label = f"（{box_name}）" if box_name != "未指定箱体" else ""
                    issues.append(CheckIssue(
                        rule_code="CASCADE_OVERCURRENT",
                        severity=CheckSeverity.WARNING.value,
                        target=f"{box_name} 出线回路 {c_no}",
                        detail=(
                            f"开关级配核验{box_label}：出线 {c_no} 额定电流 {amp:g}A（{spec}）大于进线主开关 {min_incoming:g}A，"
                            f"存在越级跳闸风险，请核对图纸"
                        )
                    ))

    # 7. 字段级证据政策校验（防编造：安装位置、断路器、电缆型号绝对禁止纯 MODEL_INFERENCE）
    if result.evidence_store:
        for ev_id, ev in result.evidence_store.items():
            for field_path, allowed in FIELD_EVIDENCE_POLICY.items():
                if field_path in ev_id and ev.evidence_type not in allowed:
                    issues.append(CheckIssue(
                        rule_code="EVIDENCE_POLICY_VIOLATION",
                        severity=CheckSeverity.ERROR.value,
                        target=field_path,
                        detail=(
                            f"【证据违规拦截】字段 '{field_path}' (证据 {ev_id}) 仅含 "
                            f"'{ev.evidence_type}' 证据，违反证据政策，严禁无据臆测！"
                        )
                    ))

    # 7b. 状态一致性：标为“已确认”却拿不出任何独立于模型自述的证据，是真矛盾。
    # 注意不能因为“只有模型自述”就每次报警：图片型 PDF 本来就没第二个来源，
    # 那属于“未交叉验证”，由 corroborate 汇总成一条说明，不在这里刷屏。
    for item in list(result.boxes or []) + list(result.circuits or []):
        for field_path, claim in (getattr(item, "claims", None) or {}).items():
            if claim.review_status != ReviewStatus.CONFIRMED.value:
                continue
            origins = {
                result.evidence_store[ev_id].origin
                for ev_id in claim.value_evidence_ids
                if ev_id in result.evidence_store
            }
            if not (origins & PHYSICAL_EVIDENCE_ORIGINS):
                issues.append(CheckIssue(
                    rule_code="EVIDENCE_STATUS_MISMATCH",
                    severity=CheckSeverity.WARNING.value,
                    target=field_path,
                    detail=(
                        f"【证据状态不符】字段 '{field_path}' 被标为已确认，"
                        f"但支撑证据来源只有 {sorted(origins) or ['无']}，"
                        f"缺少独立于模型自述的物理证据"
                    )
                ))

    # 8. 图纸目录对账审计结果自动注入
    if result.reconciliation and result.reconciliation.has_catalog:
        from .catalog_reconciler import DrawingCatalogReconciler
        recon_issues = DrawingCatalogReconciler.generate_reconciliation_issues(result.reconciliation)
        for u in recon_issues:
            issues.append(CheckIssue(
                rule_code="CATALOG_RANGE_MISSING" if u.severity == "ERROR" else "CATALOG_RANGE_PARTIAL",
                severity=u.severity,
                target=u.location or "图纸目录对账",
                detail=u.detail,
            ))

    return issues


def check_result(result: ExtractionResult) -> list[str]:
    """向后兼容的核验入口，返回原始文本警告列表。"""
    issues = check_result_issues(result)
    return [issue.detail for issue in issues]
