# -*- coding: utf-8 -*-
"""成套电气自动组价与价格核算引擎 (Pricing Engine)

核心能力：
1. 规范化元器件特征向量提取 (大类、极数、额定电流、分断能力、脱扣特性)
2. 多级物料价格匹配 (精准型号库 -> 参数特征向量对标 -> 历史基准 -> 规则估价)
3. 结构化成套报价总造价核算体系：
   - 箱体外壳基价 (按回路数/模数与防护等级推导)
   - 主元器件与辅助元件明细造价 (支持多品牌折率计算与降本平替比价)
   - 铜排母线用量与动态铜价联动公式
   - 二次控制线/辅材定额模型
   - 成套组装调试工时费与试验认证分摊
4. JEV 极速浏览器 Agent 实时采价扩展钩子
"""
import re
from typing import Dict, List, Any, Optional, Tuple

# 动态铜价基准 (元/kg，含税，可由项目配置或外部行情动态更新)
COPPER_PRICE_PER_KG = 76.50

# 辅材及二次线占比基准 (以元器件总金额的百分比计)
AUXILIARY_RATE = 0.065   # 6.5%

# 工时费率基准 (出线回路每个 45 元，进线/双电源每个 120 元)
LABOR_OUTGOING_RATE = 45.0
LABOR_INCOMING_RATE = 120.0

# 品牌价格系数与常规集采折率矩阵
BRAND_TIERS = {
    "施耐德": {"tier": 1, "mcb_discount": 0.40, "mccb_discount": 0.38, "ats_discount": 0.45, "multiplier": 1.0},
    "ABB":    {"tier": 1, "mcb_discount": 0.39, "mccb_discount": 0.37, "ats_discount": 0.44, "multiplier": 0.98},
    "西门子": {"tier": 1, "mcb_discount": 0.41, "mccb_discount": 0.39, "ats_discount": 0.46, "multiplier": 1.02},
    "良信":   {"tier": 2, "mcb_discount": 0.48, "mccb_discount": 0.45, "ats_discount": 0.50, "multiplier": 0.72},
    "正泰":   {"tier": 3, "mcb_discount": 0.55, "mccb_discount": 0.50, "ats_discount": 0.55, "multiplier": 0.58},
    "德力西": {"tier": 3, "mcb_discount": 0.53, "mccb_discount": 0.48, "ats_discount": 0.52, "multiplier": 0.55},
}

# 基础物料面价知识库 (以施耐德为基准目录面价 List Price，其余品牌按等效折率换算)
CATALOG_PRICE_BASE = {
    # 微型断路器 (MCB)
    "MCB-1P-10A": 48.0, "MCB-1P-16A": 48.0, "MCB-1P-20A": 50.0, "MCB-1P-25A": 52.0, "MCB-1P-32A": 58.0, "MCB-1P-63A": 85.0,
    "MCB-2P-16A": 115.0, "MCB-2P-20A": 118.0, "MCB-2P-25A": 122.0, "MCB-2P-32A": 135.0, "MCB-2P-63A": 195.0,
    "MCB-3P-16A": 185.0, "MCB-3P-20A": 188.0, "MCB-3P-32A": 210.0, "MCB-3P-63A": 280.0,
    "MCB-4P-32A": 285.0, "MCB-4P-63A": 380.0,
    
    # 漏电断路器 (RCBO)
    "RCBO-2P-16A": 195.0, "RCBO-2P-20A": 205.0, "RCBO-2P-25A": 215.0, "RCBO-2P-32A": 235.0, "RCBO-2P-63A": 320.0,
    "RCBO-4P-32A": 420.0, "RCBO-4P-63A": 560.0,
    
    # 塑壳断路器 (MCCB)
    "MCCB-3P-63A": 420.0, "MCCB-3P-80A": 460.0, "MCCB-3P-100A": 520.0,
    "MCCB-3P-125A": 680.0, "MCCB-3P-160A": 890.0, "MCCB-3P-200A": 1250.0, "MCCB-3P-250A": 1480.0,
    "MCCB-3P-315A": 2200.0, "MCCB-3P-400A": 2850.0, "MCCB-3P-630A": 4300.0,
    "MCCB-4P-100A": 720.0, "MCCB-4P-160A": 1180.0, "MCCB-4P-250A": 1980.0, "MCCB-4P-400A": 3600.0,
    
    # 双电源切换开关 (ATS)
    "ATS-4P-40A": 1850.0, "ATS-4P-63A": 2100.0, "ATS-4P-100A": 2600.0,
    "ATS-4P-125A": 3200.0, "ATS-4P-160A": 3900.0, "ATS-4P-250A": 5800.0,
    "ATS-4P-400A": 9600.0, "ATS-4P-630A": 14500.0,
    
    # 浪涌保护器 (SPD)
    "SPD-4P-20kA": 680.0, "SPD-4P-40kA": 920.0, "SPD-4P-65kA": 1450.0,
    
    # 接触器 (KM)
    "KM-9A": 110.0, "KM-12A": 125.0, "KM-18A": 150.0, "KM-25A": 190.0, "KM-32A": 240.0, "KM-50A": 380.0, "KM-95A": 720.0,
    
    # 辅材类 (指示灯、按钮、中间继电器、电流互感器)
    "INDICATOR": 12.0, "BUTTON": 15.0, "RELAY": 45.0, "CT-30/5": 35.0, "CT-100/5": 45.0, "CT-250/5": 65.0, "CT-400/5": 85.0,
}

# 铜排规格截面与额定电流载流量对照表 (母线重量 kg/m, 按铜密度 8.9g/cm3 计算)
# 铜排截面 (宽x厚 mm) -> (额定载流量 A, 单根重量 kg/m)
BUSBAR_SPECS = [
    (100, "TM-20x3", 0.534),
    (160, "TM-25x3", 0.668),
    (250, "TM-30x4", 1.068),
    (400, "TM-40x4", 1.424),
    (630, "TM-50x5", 2.225),
    (800, "TM-60x6", 3.204),
    (1000, "TM-80x8", 5.696),
    (1250, "TM-100x8", 7.120),
    (1600, "TM-100x10", 8.900),
]


def parse_component_features(raw_spec: str, category: str = "") -> dict:
    """从元器件规格字符串中提取特征向量 (极数、额定电流、分断能力、曲线类型)"""
    text = (raw_spec or "").upper().replace(" ", "")
    features = {
        "raw": raw_spec,
        "category": category.upper() if category else "OTHER",
        "poles": "1P",
        "current_a": 16,
        "breaking_ka": 6,
        "curve": "C",
        "has_leakage": False,
    }

    # 1. 自动判定大类
    if "ATS" in text or "双电源" in text:
        features["category"] = "ATS"
    elif "RCBO" in text or "LE" in text or "VM" in text or "漏电" in text:
        features["category"] = "RCBO"
        features["has_leakage"] = True
    elif "MCCB" in text or "NM" in text or "NSX" in text or "塑壳" in text:
        features["category"] = "MCCB"
    elif "MCB" in text or "NXB" in text or "IC65" in text or "微断" in text:
        features["category"] = "MCB"
    elif "SPD" in text or "浪涌" in text or "避雷" in text:
        features["category"] = "SPD"
    elif "KM" in text or "接触器" in text:
        features["category"] = "KM"
    elif "CT" in text or "互感器" in text or "/5" in text:
        features["category"] = "CT"

    # 2. 识别极数
    poles_m = re.search(r"(\d)P", text)
    if poles_m:
        features["poles"] = f"{poles_m.group(1)}P"
    elif features["category"] in ("MCCB", "KM"):
        features["poles"] = "3P"
    elif features["category"] in ("ATS", "SPD"):
        features["poles"] = "4P"

    # 3. 识别电流 (如 16A, C20, 100MA/80A, 125A/3P)
    curr_m = re.search(r"(?:/|C|D|M|-)?(\d{1,4})A", text)
    if curr_m:
        features["current_a"] = int(curr_m.group(1))
    else:
        num_m = re.search(r"[CD](\d{1,3})", text)
        if num_m:
            features["current_a"] = int(num_m.group(1))

    # 4. 识别分断能力 (如 6kA, 10kA, 35kA, 50kA)
    ka_m = re.search(r"(\d{1,3})KA", text)
    if ka_m:
        features["breaking_ka"] = int(ka_m.group(1))
    elif features["category"] == "MCCB":
        features["breaking_ka"] = 35
    elif features["category"] == "MCB":
        features["breaking_ka"] = 6

    # 5. 脱扣曲线
    if "D" in text and ("D16" in text or "D20" in text or "D32" in text or "D型" in text):
        features["curve"] = "D"

    return features


def calculate_component_unit_price(
    raw_spec: str,
    brand: str = "施耐德",
    category: str = ""
) -> Tuple[float, float, str]:
    """计算单个元器件的 (实际采购单价, 目录面价, 计价判定依据)"""
    feats = parse_component_features(raw_spec, category)
    cat = feats["category"]
    poles = feats["poles"]
    curr = feats["current_a"]
    
    # 查找离电流最近的标准电流档位
    standard_currs = [10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 400, 630]
    matched_curr = min(standard_currs, key=lambda x: abs(x - curr))
    
    key = f"{cat}-{poles}-{matched_curr}A"
    list_price = CATALOG_PRICE_BASE.get(key)
    exact_match = list_price is not None  # 只有精确命中才算"标准库匹配"
    
    # 若无直接匹配，使用类别规则估价
    if not list_price:
        if cat == "MCB":
            list_price = 45.0 * (int(poles[0]) if poles[0].isdigit() else 1) * (1.3 if matched_curr > 32 else 1.0)
        elif cat == "RCBO":
            list_price = 180.0 * (int(poles[0]) if poles[0].isdigit() else 2) / 2.0
        elif cat == "MCCB":
            list_price = 350.0 + matched_curr * 5.5
        elif cat == "ATS":
            list_price = 1500.0 + matched_curr * 18.0
        elif cat == "SPD":
            list_price = 850.0
        elif cat == "CT":
            list_price = 45.0
        elif cat == "KM":
            list_price = 120.0 + matched_curr * 4.0
        else:
            list_price = 35.0

    tier_info = BRAND_TIERS.get(brand, BRAND_TIERS["施耐德"])
    if cat in ("MCB", "RCBO"):
        discount = tier_info["mcb_discount"]
    elif cat == "MCCB":
        discount = tier_info["mccb_discount"]
    elif cat == "ATS":
        discount = tier_info["ats_discount"]
    else:
        discount = (tier_info["mcb_discount"] + tier_info["mccb_discount"]) / 2.0

    # 乘以品牌系数计算最终到厂采购单价
    cost_price = round(list_price * discount * tier_info["multiplier"], 2)
    if exact_match:
        basis = f"标准库匹配[{key}] 面价￥{list_price} 折扣率{discount:.2f}"
    else:
        basis = f"规则估算（待核）[{key}] 面价￥{list_price} 折扣率{discount:.2f}"
    return cost_price, round(list_price, 2), basis


def estimate_box_enclosure_price(box: dict, circuits_count: int) -> Tuple[float, str]:
    """估算配电箱外壳制造成本 (冷轧钢板/喷塑/铜排支架/门锁)，结合回路数与防护等级"""
    ip = str(box.get("ip_rating") or "IP30").upper()
    box_type = str(box.get("box_type") or "").upper()
    is_floor = "落地" in str(box.get("install_type") or "") or "GGD" in box_type or "XL" in box_type
    
    # 基础箱体尺寸推导
    if is_floor or circuits_count > 24:
        base_price = 1450.0  # 落地动力柜 / GGD / XL-21
        model_desc = "落地式配电柜外壳 (GGD/XL-21型 800x1800x600, 2.0mm冷轧板)"
    elif circuits_count > 12:
        base_price = 580.0   # 中型明装配电箱
        model_desc = "挂墙式配电箱外壳 (600x800x200, 1.5mm冷轧板)"
    elif circuits_count > 6:
        base_price = 360.0   # 小型明装照明箱
        model_desc = "明装照明配电箱外壳 (400x500x180, 1.2mm冷轧板)"
    else:
        base_price = 220.0   # 微型控制箱
        model_desc = "小型端子/控制箱外壳 (300x400x160, 1.2mm冷轧板)"

    # 防护等级系数
    if "IP55" in ip or "IP65" in ip:
        base_price *= 1.35
        model_desc += " [高防护密封条处理]"
    elif "IP44" in ip:
        base_price *= 1.15
        model_desc += " [带防溅防尘檐边]"

    return round(base_price, 2), model_desc


def estimate_copper_busbar_cost(main_current_a: int, box_width_m: float = 0.8) -> Tuple[float, float, str]:
    """根据进线主开关电流计算母排铜排重量与成本
    
    公式:
    单相母排长度约 = 进线跨接(0.6m) + 柜宽水平母线(box_width_m) + 出线支排
    三相 + N排 + PE排共需约 4.5 倍柜宽母排用量
    """
    if main_current_a <= 0:
        main_current_a = 63

    matched_spec = BUSBAR_SPECS[0]
    for spec in BUSBAR_SPECS:
        if main_current_a <= spec[0]:
            matched_spec = spec
            break
    else:
        matched_spec = BUSBAR_SPECS[-1]

    _, spec_code, weight_per_m = matched_spec
    # 三相主母线(3根) + N排(1根) + PE地排(1根)
    total_length_m = (box_width_m + 0.4) * 3 + (box_width_m + 0.2) * 2
    total_weight_kg = round(total_length_m * weight_per_m, 2)
    busbar_cost = round(total_weight_kg * COPPER_PRICE_PER_KG, 2)
    detail = f"主线电流{main_current_a}A 选用[{spec_code}] 重量{total_weight_kg}kg@￥{COPPER_PRICE_PER_KG}/kg"
    return busbar_cost, total_weight_kg, detail


def calculate_box_quotation(
    box: dict,
    circuits: list[dict],
    components: list[dict],
    brand: str = "正泰",
    profit_rate: float = 0.08,
    tax_rate: float = 0.13
) -> dict:
    """计算单个成套配电箱柜的完整工业造价明细"""
    # 1. 元器件造价合计
    comp_total = 0.0
    comp_list_total = 0.0
    priced_components = []
    
    for comp in components:
        spec = comp.get("spec") or ""
        qty = int(comp.get("quantity") or 1)
        unit_price, list_price, basis = calculate_component_unit_price(spec, brand=brand, category=comp.get("category", ""))
        item_total = round(unit_price * qty, 2)
        comp_total += item_total
        comp_list_total += list_price * qty
        priced_components.append({
            "name": comp.get("name") or comp.get("standard_name") or "元器件",
            "spec": spec,
            "quantity": qty,
            "unit": comp.get("unit") or "台",
            "unit_price": unit_price,
            "list_price": list_price,
            "total_price": item_total,
            "pricing_basis": basis,
        })

    # 2. 箱体外壳成本
    circuits_count = len(circuits)
    enclosure_cost, enclosure_desc = estimate_box_enclosure_price(box, circuits_count)

    # 3. 铜排母线成本 (按最大进线断路器或总回路估算额定电流)
    main_curr = 63
    for c in circuits:
        if c.get("circuit_type") == "incoming" or "进线" in str(c.get("load_name") or ""):
            feat = parse_component_features(c.get("breaker_spec", ""))
            main_curr = max(main_curr, feat["current_a"])
    busbar_cost, copper_weight_kg, busbar_desc = estimate_copper_busbar_cost(main_curr)

    # 4. 二次线及辅材 (接线端子、号码管、扎带、线鼻)
    auxiliary_cost = round(comp_total * AUXILIARY_RATE, 2)

    # 5. 人工组装调试费
    incoming_count = max(1, sum(1 for c in circuits if c.get("circuit_type") == "incoming"))
    outgoing_count = max(0, circuits_count - incoming_count)
    labor_cost = round(incoming_count * LABOR_INCOMING_RATE + outgoing_count * LABOR_OUTGOING_RATE, 2)

    # 6. 型式试验与CCC分摊 (一般按出厂出厂价 2%)
    factory_cost = enclosure_cost + comp_total + busbar_cost + auxiliary_cost + labor_cost
    test_cert_cost = round(factory_cost * 0.02, 2)

    # 7. 制造总成本与含税出厂报价
    total_cost = factory_cost + test_cert_cost
    subtotal_with_profit = total_cost * (1.0 + profit_rate)
    final_tax_included = round(subtotal_with_profit * (1.0 + tax_rate), 2)

    return {
        "box_code": box.get("box_code") or "未命名箱体",
        "box_name": box.get("box_name") or box.get("name") or "",
        "brand": brand,
        "circuits_count": circuits_count,
        "cost_breakdown": {
            "enclosure_cost": enclosure_cost,
            "enclosure_desc": enclosure_desc,
            "component_cost": round(comp_total, 2),
            "component_list_total": round(comp_list_total, 2),
            "copper_busbar_cost": busbar_cost,
            "copper_weight_kg": copper_weight_kg,
            "copper_desc": busbar_desc,
            "auxiliary_cost": auxiliary_cost,
            "labor_cost": labor_cost,
            "test_cert_cost": test_cert_cost,
            "factory_total_cost": round(total_cost, 2),
        },
        "profit_rate": profit_rate,
        "tax_rate": tax_rate,
        "final_tax_included": final_tax_included,
        "components": priced_components,
    }


def compare_brands_quotation(
    box: dict,
    circuits: list[dict],
    components: list[dict]
) -> dict:
    """横向多品牌比价矩阵 (外资一线 施耐德/ABB vs 国产良信/正泰/德力西)"""
    results = {}
    for brand in ("施耐德", "良信", "正泰"):
        q = calculate_box_quotation(box, circuits, components, brand=brand)
        results[brand] = {
            "final_amount": q["final_tax_included"],
            "component_cost": q["cost_breakdown"]["component_cost"],
            "factory_cost": q["cost_breakdown"]["factory_total_cost"],
        }
    
    schneider_amount = results["施耐德"]["final_amount"]
    chint_amount = results["正泰"]["final_amount"]
    saving_ratio = round((schneider_amount - chint_amount) / schneider_amount * 100, 1) if schneider_amount else 0

    return {
        "brands": results,
        "benchmark_brand": "施耐德",
        "domestic_substitute": "正泰",
        "total_saving_ratio_pct": saving_ratio,
    }
