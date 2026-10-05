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


# 品牌识别模式与平替选型规则全部来自 config/domain.json、config/delivery.json、config/replacement.json。
# 注意：不收录裸 "C65"——无品牌标注的 C65N 等是通用/国标写法，
# 若判给施耐德会导致"一键平替降本"的降本比例虚高（45% vs 15%）。施耐德专有词保留 iC65/Acti9/NSX 等。
from .config import delivery as _delivery, domain as _domain, replacement_rules as _replacement_rules

_CATALOG_CFG = _domain()["catalog"]
_REPLACEMENT = _replacement_rules()
_DELIVERY_CFG = _delivery()

BRAND_PATTERNS = [(name, re.compile(pattern, re.IGNORECASE))
                  for name, pattern in _CATALOG_CFG["brand_patterns"]]
GENERIC_BRAND = _CATALOG_CFG["default_brand"]
DEFAULT_TARGET_BRAND = _DELIVERY_CFG["brand"]["default_target"]
DEFAULT_DEVICE_UNIT = _DELIVERY_CFG["estimation"]["default_device_unit"]


def series_for(target_brand: str) -> dict:
    """取目标品牌的系列对照表；未知品牌回落到配置的兜底品牌。"""
    table = _REPLACEMENT["series"]
    return table.get(target_brand) or table[_REPLACEMENT["fallback_brand"]]


def identify_brand(text: str) -> str:
    """从元器件名称或规格型号中识别品牌归属。"""
    if not text:
        return GENERIC_BRAND
    for brand, pat in BRAND_PATTERNS:
        if pat.search(text):
            return brand
    return GENERIC_BRAND


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
    elif re.search(r"/[3]\d{3}\b", combined) or any(k in combined for k in ("3300", "3308", "3320", "3极", "三极")):
        poles = "3P"
    elif re.search(r"/[4]\d{3}\b", combined) or any(k in combined for k in ("4300", "4308", "4极", "四极")):
        poles = "4P"
    elif any(k in combined for k in ("2极", "二极")):
        poles = "2P"
    elif any(k in combined for k in ("1极", "单极")):
        poles = "1P"

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

    # 6. 提纯为标准规范串（严禁编造缺失参数：电流缺失标疑待核，极数缺失如实反映）
    clean_parts = []
    if category != "元器件":
        clean_parts.append(category.split()[0])
    if curve and "微型" in category:
        if amp is not None:
            clean_parts.append(f"{curve}{int(amp)}A")
        else:
            clean_parts.append(f"{curve}(待核电流)")
    elif amp is not None:
        clean_parts.append(f"{int(amp)}A")
    elif "断路器" in category:
        clean_parts.append("(待核电流)")

    if poles:
        clean_parts.append(poles)
    else:
        clean_parts.append("(待核极数)")

    if leakage_ma:
        clean_parts.append(f"{leakage_ma}mA")

    standard_spec = " ".join(clean_parts) if clean_parts else (spec or name)

    return {
        "raw_name": name,
        "raw_spec": spec,
        "brand": brand,
        "category": category,
        "poles": poles,  # 真实极数，若图纸缺失保持 None，严禁盲目编造
        "rated_amp": amp,  # 真实电流，若图纸缺失保持 None，严禁盲目填充 16A
        "curve": curve,
        "leakage_ma": leakage_ma,
        "standard_spec": standard_spec,
    }


def recommend_replacements(name: str, spec: str, target_brand: str = "") -> dict[str, Any]:
    """按解析参数产生替代候选；型号、电气性能和价格需由产品资料与有效报价独立核验。"""
    from .pricing import lookup_price_library

    target_brand = (target_brand or DEFAULT_TARGET_BRAND).strip() or DEFAULT_TARGET_BRAND
    series = series_for(target_brand)
    ka_notes = _REPLACEMENT["ka_notes"]
    note_tpl = _REPLACEMENT["note_templates"]
    parsed = parse_component_spec(name, spec)
    current_brand = parsed["brand"]
    category = parsed["category"]
    amp = parsed["rated_amp"]
    poles = parsed["poles"] or "1P"
    curve = parsed["curve"] or "C"
    leakage = parsed["leakage_ma"]
    combined = f"{name} {spec}".upper()
    is_10ka = bool(re.search(r"\b10\s*K[A]?\b", combined) or re.search(r"[A-Z0-9]+[HM]\b", combined))

    replaced_model = ""
    target_series = ""
    matching_notes = ""

    if "微型断路器" in category or "漏电保护断路器" in category:
        if leakage or "漏电" in category:
            target_series = series["rcbo_high"] if is_10ka else series["rcbo_std"]

            leak_val = leakage or _REPLACEMENT["leakage_default_ma"]
            leak_note = f"漏电动作 {leak_val}mA" if leakage else f"按常规人身防护默认 {leak_val}mA (待核)"
            if amp is not None:
                replaced_model = f"{target_series} {curve}{int(amp)}/{poles} {leak_val}mA"
                ka_note = ka_notes["high"] if is_10ka else ka_notes["standard"]
                matching_notes = f"电气性能参数对标：额定电流 {int(amp)}A, 极数 {poles}, 特性曲线 {curve} 型, {ka_note}, {leak_note}"
            else:
                replaced_model = f"{target_series}系列（待核定电流）"
                matching_notes = note_tpl["await_amp"].format(target_brand=target_brand, series=target_series)
        else:
            target_series = series["mcb_high"] if is_10ka else series["mcb_std"]

            if amp is not None:
                replaced_model = f"{target_series} {curve}{int(amp)}/{poles}"
                ka_note = ka_notes["high"] if is_10ka else ka_notes["standard"]
                matching_notes = f"电气性能参数对标：额定电流 {int(amp)}A, 极数 {poles}, 特性曲线 {curve} 型, {ka_note}"
            else:
                replaced_model = f"{target_series}系列（待核定电流）"
                matching_notes = note_tpl["await_amp"].format(target_brand=target_brand, series=target_series)
    elif "塑壳断路器" in category:
        if amp is not None:
            frame = 125 if amp <= 125 else (250 if amp <= 250 else (400 if amp <= 400 else 630))
            target_series = series["mccb"].format(frame=frame)
            replaced_model = f"{target_series}/3300 {int(amp)}A {poles}"
            matching_notes = f"电气性能参数对标：壳架等级 {frame}A, 额定电流 {int(amp)}A, 极数 {poles}（请核对厂商样本）"
        else:
            target_series = series["mccb_family"]
            replaced_model = f"{target_series}（壳架与电流待核定）"
            matching_notes = note_tpl["await_amp"].format(target_brand=target_brand, series=target_series)
    elif "框架断路器" in category:
        target_series = series["acb"]
        if amp is not None:
            replaced_model = f"{target_series} 智能型框架断路器 {int(amp)}A {poles}"
            matching_notes = f"电气性能参数对标：额定电流 {int(amp)}A, 极数 {poles} 智能控制器（请核对厂商样本）"
        else:
            replaced_model = f"{target_series}系列智能型框架断路器（电流待核）"
            matching_notes = f"原图纸未标注框架电流，推荐选用{target_brand} {target_series}系列"
    elif "电涌保护器" in category or "浪涌" in category:
        target_series = series["spd"]

        # 动态提取原图标明的放电电流与极数，严禁所有箱体无脑套用 40kA/4P
        m_spd_ka = re.search(r"(\d{1,3})\s*K[A]?", combined)
        spd_ka = f"{m_spd_ka.group(1)}kA" if m_spd_ka else ""
        spd_poles = "2P" if any(p in combined for p in ("1P", "2P", "1P+N", "单相")) else ("4P" if any(p in combined for p in ("3P", "4P", "3P+N", "三相")) else "")

        if spd_ka and spd_poles:
            replaced_model = f"{target_series}/{spd_ka}/{spd_poles} 浪涌保护器"
            matching_notes = f"技术规格参数对标：标称放电电流 {spd_ka}, {spd_poles} 浪涌防护"
        elif spd_ka:
            replaced_model = f"{target_series}/{spd_ka} 浪涌保护器（极数待核）"
            matching_notes = f"技术规格参数对标：标称放电电流 {spd_ka}，请根据进线单相/三相核对极数"
        else:
            replaced_model = f"{target_series}系列 浪涌保护器（放电电流待核）"
            matching_notes = "原图未标明标称放电电流(kA)，需结合防雷分区规范(LPZ)核定定额"
    elif "交流接触器" in category:
        target_series = series["contactor"]
        if amp is None:
            # 图纸未标额定电流时不得整数格式化，否则直接抛 TypeError 打断整个平替分析
            replaced_model = f"{target_series}-?? 交流接触器（电流待核）"
            matching_notes = (f"原图纸未标注接触器额定电流，"
                              f"{note_tpl['await_amp'].format(target_brand=target_brand, series=target_series)}")
        else:
            replaced_model = f"{target_series}-{int(amp):02d} 交流接触器"
            matching_notes = f"电气参数对标：额定工作电流 {int(amp)}A 交流接触器"
    elif "双电源" in category:
        target_series = series["ats"]
        if amp is None:
            replaced_model = f"{target_series} 双电源自动转换开关（电流待核）"
            matching_notes = (f"原图纸未标注双电源额定电流，"
                              f"{note_tpl['await_amp'].format(target_brand=target_brand, series=target_series)}")
        else:
            replaced_model = f"{target_series} 双电源自动转换开关 {int(amp)}A {poles}"
            matching_notes = f"电气参数对标：额定电流 {int(amp)}A, {poles} 自动转换"
    else:
        replaced_model = f"{target_brand} 优质匹配型号 ({spec or name})"
        matching_notes = note_tpl["generic"]

    # 按本地价格库当前记录测算价差；数据来源与有效期未核验，不能称为供应商实时报价。
    p_orig = None
    p_target = None
    cat_code = _REPLACEMENT["comparison"]["unknown_family_category"]
    for key, code in _REPLACEMENT["comparison"]["families"].items():
        if key in category:
            cat_code = code
            break
    if amp is not None:
        amp_int = int(amp)
        p_orig = lookup_price_library(cat_code, current_brand, poles or "1P", amp_int, raw_spec=spec, curve=curve)
        p_target = lookup_price_library(cat_code, target_brand, poles or "1P", amp_int, raw_spec=replaced_model, curve=curve)

    orig_price_val = None
    target_price_val = None
    saving_status = "unknown_price"

    if p_orig and p_target and p_orig.get("price_tax", 0) > 0 and p_target.get("price_tax", 0) > 0:
        orig_price_val = round(float(p_orig["price_tax"]), 2)
        target_price_val = round(float(p_target["price_tax"]), 2)
        if orig_price_val > target_price_val:
            saving_pct = int(round((orig_price_val - target_price_val) / orig_price_val * 100))
            saving_status = "cost_saving"
            price_evidence = f"【实价实算：原厂￥{orig_price_val} -> 平替￥{target_price_val}，降本 {saving_pct}%】"
        else:
            # 平替价格高于或等于原厂：客观如实报告成本上升，绝对禁止硬称降本
            saving_pct = 0
            saving_status = "cost_increase" if target_price_val > orig_price_val else "cost_equal"
            inc_pct = int(round((target_price_val - orig_price_val) / orig_price_val * 100)) if orig_price_val > 0 else 0
            price_evidence = f"【价格警示：平替￥{target_price_val} 高于/等于原厂￥{orig_price_val}（溢价 {inc_pct}%），无降本空间，请复核选型】"
    else:
        # 价格库未收录原厂或平替价格：实事求是标疑待询价，严禁编造固定 35%/45% 降本率
        saving_pct = None
        saving_status = "missing_price"
        price_evidence = "【价格存疑：价格库未收录该规格对标价格，无法测算降本率，请人工询价核对】"

    if price_evidence and price_evidence not in matching_notes:
        matching_notes = f"{matching_notes} {price_evidence}".strip()

    return {
        "original_brand": current_brand,
        "target_brand": target_brand,
        "original_spec": spec or name,
        "standard_parsed": parsed["standard_spec"],
        "recommended_model": replaced_model,
        "recommended_series": target_series,
        "estimated_saving_pct": saving_pct,
        "saving_status": saving_status,
        "original_price": orig_price_val,
        "target_price": target_price_val,
        "matching_notes": matching_notes,
    }


def analyze_components_replacement(components: list[dict[str, Any]], target_brand: str = "") -> dict[str, Any]:
    """批量分析元器件清单并输出完整的平替降本诊断报告。"""
    items = []
    total_qty = 0.0
    replaceable_qty = 0.0
    unquoted_qty = 0.0
    cost_increase_qty = 0.0
    weighted_saving_sum = 0.0

    for c in components:
        n = c.get("name", "")
        s = c.get("spec", "")
        qty = float(c.get("quantity") or 1)
        total_qty += qty

        rec = recommend_replacements(n, s, target_brand=target_brand)
        saving = rec["estimated_saving_pct"]
        status = rec.get("saving_status", "unknown")

        if saving is not None and saving > 0:
            replaceable_qty += qty
            weighted_saving_sum += saving * qty
        elif status == "cost_increase":
            cost_increase_qty += qty
        elif saving is None or status == "missing_price":
            unquoted_qty += qty

        items.append({
            "name": n,
            "original_spec": s,
            "quantity": qty,
            "unit": c.get("unit") or DEFAULT_DEVICE_UNIT,
            "used_in": c.get("used_in", ""),
            "original_brand": rec["original_brand"],
            "target_brand": target_brand,
            "recommended_model": rec["recommended_model"],
            "estimated_saving_pct": saving,
            "saving_status": status,
            "notes": rec["matching_notes"],
        })

    avg_saving = int(round(weighted_saving_sum / total_qty)) if total_qty > 0 else 0

    summary_parts = [f"共检出 {len(items)} 项物料（合计 {int(total_qty)} 件）"]
    if replaceable_qty > 0:
        summary_parts.append(f"其中 {int(replaceable_qty)} 件具备真实平替降本空间，采购成本预计降低约 {avg_saving}%")
    if cost_increase_qty > 0:
        summary_parts.append(f"{int(cost_increase_qty)} 件平替物料实测成本上升/溢价（不建议更换）")
    if unquoted_qty > 0:
        summary_parts.append(f"{int(unquoted_qty)} 件物料价格库未收录，需人工询价核实")

    return {
        "target_brand": target_brand,
        "total_components": len(items),
        "total_quantity": total_qty,
        "replaceable_quantity": replaceable_qty,
        "unquoted_quantity": unquoted_qty,
        "cost_increase_quantity": cost_increase_qty,
        "estimated_overall_saving_pct": avg_saving,
        "summary": "；".join(summary_parts),
        "items": items,
    }
