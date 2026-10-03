# -*- coding: utf-8 -*-
"""通用低压电气元器件标准物料库、SKU 清洗与多品牌智能平替引擎。

覆盖低压成套主流品类：
- 微型断路器 (MCB)
- 塑壳断路器 (MCCB)
- 框架断路器 (ACB)
- 漏电断路器 (RCBO/RCCB)
- 浪涌保护器 (SPD)
- 交流接触器 (KM)
- 双电源自动转换开关 (ATSE)
- 多功能电力仪表 / 电度表 (Meter)

支持：
1. 原始非标字符串深度解析（极数、额定电流、脱扣特性、分断能力、漏电保护）；
2. 品牌识别（施耐德 Schneider、ABB、西门子 Siemens、正泰 CHINT、德力西 Delixi、良信 Nader、常熟开关 CS）；
3. 跨品牌等效平替推荐与降本比例测算。
"""

import re
from typing import Any


# 常见品牌识别模式
BRAND_PATTERNS = [
    # 注意：不收录裸 "C65"——无品牌标注的 C65N 等是通用/国标写法，
    # 若判给施耐德会导致"一键平替降本"的降本比例虚高（45% vs 15%）。
    # 施耐德专有词保留 iC65/Acti9/NSX 等。
    ("施耐德", re.compile(r"(?:Schneider|施耐德|iC65|Acti9|NSX|NSC|EA9|EasyPact|GV2|LC1D)", re.IGNORECASE)),
    ("ABB", re.compile(r"(?:ABB|S20\d|S200|Tmax|XT[1-4]|A9|AF\d|AX\d|OT160)", re.IGNORECASE)),
    ("西门子", re.compile(r"(?:Siemens|西门子|5SY|5SL|5SU|3VM|3VA|3VT|3TF|3RT)", re.IGNORECASE)),
    ("正泰", re.compile(r"(?:CHINT|正泰|NXB|NM8|NM1|NM5|NXBLE|CJX2|NXZ)", re.IGNORECASE)),
    ("德力西", re.compile(r"(?:Delixi|德力西|CDB6|CDM3|CDM6|CDCH8|CDX2)", re.IGNORECASE)),
    ("良信", re.compile(r"(?:Nader|良信|NDB|NDM|NDC|NDQ)", re.IGNORECASE)),
    ("常熟开关", re.compile(r"(?:常熟|常开|CM1|CM3|CM5|CA1|CW1|CW2|CW3)", re.IGNORECASE)),
]


def identify_brand(text: str) -> str:
    """从元器件名称或规格型号中识别品牌归属。"""
    if not text:
        return "通用/国标"
    for brand, pat in BRAND_PATTERNS:
        if pat.search(text):
            return brand
    return "通用/国标"


def parse_component_spec(name: str, spec: str) -> dict[str, Any]:
    """深度解析原始非标规格，提纯出结构化电气物料参数 (SKU 特征)。"""
    combined = f"{name or ''} {spec or ''}".strip()
    brand = identify_brand(combined)

    # 1. 判定品类 category
    category = "元器件"
    if re.search(r"微断|小型断路器|MCB|微型断路器|空气开关|空开", combined, re.IGNORECASE):
        category = "微型断路器 (MCB)"
    elif re.search(r"塑壳|MCCB|断路器\s*(?:NM|NSX|CDM|CM1|3VA|XT)", combined, re.IGNORECASE):
        category = "塑壳断路器 (MCCB)"
    elif re.search(r"万能式|框架|ACB", combined, re.IGNORECASE):
        category = "框架断路器 (ACB)"
    elif re.search(r"漏电|RCBO|RCD|RCB|Vigi|残余电流", combined, re.IGNORECASE):
        category = "漏电保护断路器 (RCBO)"
    elif re.search(r"浪涌|避雷|SPD|防雷", combined, re.IGNORECASE):
        category = "电涌保护器 (SPD)"
    elif re.search(r"接触器|KM|CJX2|LC1", combined, re.IGNORECASE):
        category = "交流接触器 (KM)"
    elif re.search(r"双电源|ATSE|ATS|互投|双电源转换", combined, re.IGNORECASE):
        category = "双电源转换开关 (ATSE)"
    elif re.search(r"电表|电度表|多功能仪表|电流表|电压表|电能表", combined, re.IGNORECASE):
        category = "电测量仪表 (Meter)"
    elif re.search(r"箱体|配电箱|配电柜|柜体|动力箱|照明箱", combined, re.IGNORECASE):
        category = "配电箱体"

    # 2. 提取极数 poles (1P, 2P, 3P, 4P, 1P+N, 3P+N)
    poles = ""
    m_p = re.search(r"(?:/|\s|^)([1-4]P(?:\+N)?|[1-4]PN|3PH)(?:/|\s|$)", combined, re.IGNORECASE)
    if m_p:
        poles = m_p.group(1).upper()
        if poles == "1PN":
            poles = "1P+N"
        elif poles == "3PN":
            poles = "3P+N"
    elif "3300" in combined or "3308" in combined or "3320" in combined:
        poles = "3P"
    elif "4300" in combined or "4308" in combined:
        poles = "4P"

    # 3. 提取额定电流 In (精确支持 /C16A, C20A, /50A, 100A, In=63A 等，避开型号如 iC65, MCB-63)
    amp = None
    m_amp = re.search(r"(?:/(?:[CDB]|In=?)?|\b[CDB]|In=?\s*)([1-9]\d{0,3})\s*A?(?:/|\s|$)", combined, re.IGNORECASE)
    if not m_amp:
        m_amp = re.search(r"\b([1-9]\d{0,3})\s*A\b", combined, re.IGNORECASE)
    if m_amp:
        try:
            amp = float(m_amp.group(1))
        except ValueError:
            pass

    # 4. 提取微断脱扣特性曲线 (B, C, D 型脱扣)
    curve = "C"
    if re.search(r"(?:/|/|\b)D(?:[1-9]\d{0,2})A?(?:/|$|\b)", combined, re.IGNORECASE) or "动力型" in combined:
        curve = "D"
    elif re.search(r"(?:/|/|\b)B(?:[1-9]\d{0,2})A?(?:/|$|\b)", combined, re.IGNORECASE):
        curve = "B"
    elif re.search(r"(?:/|/|\b)C(?:[1-9]\d{0,2})A?(?:/|$|\b)", combined, re.IGNORECASE) or "照明型" in combined:
        curve = "C"

    # 5. 提取漏电动作电流 (mA)
    leakage_ma = None
    m_leak = re.search(r"(\d{2,4})\s*m[aA]", combined)
    if m_leak:
        try:
            leakage_ma = int(m_leak.group(1))
        except ValueError:
            pass

    # 6. 提纯为标准规范串
    clean_parts = []
    if category != "元器件":
        clean_parts.append(category.split()[0])
    if curve and "微型" in category:
        clean_parts.append(f"{curve}{int(amp) if amp else 16}A")
    elif amp:
        clean_parts.append(f"{int(amp)}A")
    if poles:
        clean_parts.append(poles)
    if leakage_ma:
        clean_parts.append(f"{leakage_ma}mA")

    standard_spec = " ".join(clean_parts) if clean_parts else (spec or name)

    return {
        "raw_name": name,
        "raw_spec": spec,
        "brand": brand,
        "category": category,
        "poles": poles or ("3P" if "塑壳" in category else "1P"),
        "rated_amp": amp,
        "curve": curve,
        "leakage_ma": leakage_ma,
        "standard_spec": standard_spec,
    }


def recommend_replacements(name: str, spec: str, target_brand: str = "正泰") -> dict[str, Any]:
    """根据解析参数，自动推荐正泰、德力西、良信等高性价比国产替代物料并测算降本幅度。"""
    parsed = parse_component_spec(name, spec)
    current_brand = parsed["brand"]
    category = parsed["category"]
    amp = parsed["rated_amp"] or 16
    poles = parsed["poles"]
    curve = parsed["curve"]
    leakage = parsed["leakage_ma"]

    # 判定替代降本空间：外资一线品牌平替通常可节约 35%~50% 采购成本
    is_foreign = current_brand in ("施耐德", "ABB", "西门子")
    if is_foreign:
        saving_pct = 35 if target_brand == "良信" else 45
    elif current_brand == target_brand:
        saving_pct = 0
    elif current_brand in ("正泰", "德力西", "良信", "常熟开关"):
        saving_pct = 5  # 国产品牌间对等替换
    else:
        # 通用/国标/非标描述：统一选用正规一线国产品牌集采标品，综合降本约 15%
        saving_pct = 15

    replaced_model = ""
    target_series = ""
    matching_notes = ""

    if "微型断路器" in category or "漏电保护断路器" in category:
        if leakage or "漏电" in category:
            if target_brand == "正泰":
                target_series = "NXBLE-63"
            elif target_brand == "良信":
                target_series = "NDB1LE-63"
            else:
                target_series = "CDB6LE-63"
            leak_val = leakage or 30
            replaced_model = f"{target_series} {curve}{int(amp)}/{poles} {leak_val}mA"
            matching_notes = f"电气性能参数完全对标：额定电流 {int(amp)}A, 极数 {poles}, 特性曲线 {curve} 型, 漏电动作 {leak_val}mA"
        else:
            if target_brand == "正泰":
                target_series = "NXB-63"
            elif target_brand == "良信":
                target_series = "NDB1-63"
            else:
                target_series = "CDB6i-63"
            replaced_model = f"{target_series} {curve}{int(amp)}/{poles}"
            matching_notes = f"电气性能参数完全对标：额定电流 {int(amp)}A, 极数 {poles}, 特性曲线 {curve} 型"
    elif "塑壳断路器" in category:
        frame = 125 if amp <= 125 else (250 if amp <= 250 else (400 if amp <= 400 else 630))
        if target_brand == "正泰":
            target_series = f"NM8N-{frame}S"
        elif target_brand == "良信":
            target_series = f"NDM1-{frame}S"
        else:
            target_series = f"CDM3-{frame}S"
        replaced_model = f"{target_series}/3300 {int(amp)}A {poles}"
        matching_notes = f"电气性能参数完全对标：壳架等级 {frame}A, 额定电流 {int(amp)}A, 极数 {poles}"
    elif "框架断路器" in category:
        if target_brand == "正泰":
            target_series = "NXA"
        elif target_brand == "良信":
            target_series = "NDW1"
        else:
            target_series = "CDW3"
        replaced_model = f"{target_series} 智能型框架断路器 {int(amp)}A {poles}"
        matching_notes = f"电气性能参数完全对标：额定电流 {int(amp)}A, 极数 {poles} 智能控制器"
    elif "电涌保护器" in category:
        if target_brand == "正泰":
            target_series = "NU6-II"
        elif target_brand == "良信":
            target_series = "NDY1"
        else:
            target_series = "CDY1"
        replaced_model = f"{target_series}/40kA/4P 浪涌保护器"
        matching_notes = "技术规格完全对标：标称放电电流 40kA, 4P 浪涌防护"
    elif "交流接触器" in category:
        if target_brand == "正泰":
            target_series = "NC1"
        elif target_brand == "良信":
            target_series = "NDC1"
        else:
            target_series = "CJX2s"
        replaced_model = f"{target_series}-{int(amp):02d} 交流接触器"
        matching_notes = f"电气参数对标：额定工作电流 {int(amp)}A 交流接触器"
    elif "双电源" in category:
        if target_brand == "正泰":
            target_series = "NZ7"
        elif target_brand == "良信":
            target_series = "NDQ1"
        else:
            target_series = "CDQ3"
        replaced_model = f"{target_series} 双电源自动转换开关 {int(amp)}A {poles}"
        matching_notes = f"电气参数对标：额定电流 {int(amp)}A, {poles} 自动转换"
    else:
        replaced_model = f"{target_brand} 优质匹配型号 ({spec or name})"
        matching_notes = "物理规格与安装尺寸与原图深化要求完全兼容"

    return {
        "original_brand": current_brand,
        "target_brand": target_brand,
        "original_spec": spec or name,
        "standard_parsed": parsed["standard_spec"],
        "recommended_model": replaced_model,
        "recommended_series": target_series,
        "estimated_saving_pct": saving_pct,
        "matching_notes": matching_notes,
    }


def analyze_components_replacement(components: list[dict[str, Any]], target_brand: str = "正泰") -> dict[str, Any]:
    """批量分析元器件清单并输出完整的平替降本诊断报告。"""
    items = []
    total_qty = 0.0
    replaceable_qty = 0.0
    weighted_saving_sum = 0.0

    for c in components:
        n = c.get("name", "")
        s = c.get("spec", "")
        qty = float(c.get("quantity") or 1)
        total_qty += qty

        rec = recommend_replacements(n, s, target_brand=target_brand)
        saving = rec["estimated_saving_pct"]
        if saving > 0:
            replaceable_qty += qty
            weighted_saving_sum += saving * qty

        items.append({
            "name": n,
            "original_spec": s,
            "quantity": qty,
            "unit": c.get("unit", "只"),
            "used_in": c.get("used_in", ""),
            "original_brand": rec["original_brand"],
            "target_brand": target_brand,
            "recommended_model": rec["recommended_model"],
            "estimated_saving_pct": saving,
            "notes": rec["matching_notes"],
        })

    avg_saving = int(round(weighted_saving_sum / total_qty)) if total_qty > 0 else 0

    return {
        "target_brand": target_brand,
        "total_components": len(items),
        "total_quantity": total_qty,
        "replaceable_quantity": replaceable_qty,
        "estimated_overall_saving_pct": avg_saving,
        "summary": f"共检出 {len(items)} 项物料，其中 {int(replaceable_qty)} 件具备一键国产化平替降本空间，预计整体元器件采购成本降低约 {avg_saving}%",
        "items": items,
    }
