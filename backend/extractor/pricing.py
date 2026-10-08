# -*- coding: utf-8 -*-
"""成套电气价格查找与估算函数。

本模块同时含用户提供的价格库、代码内置系数和经验估算。价格库来源/日期未在数据行中
验证；公式输出不是供应商报价、行业定额或工程认可的报价，正式报价前必须核实。
"""
import os
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

# 报价口径全部来自 config/pricing.json（可用 EXTRACTOR_CONFIG_OVERRIDE_DIR 覆盖、可调铜价）。
# 出厂默认值只是量级参考，正式报价前必须按当日行情与供应商实价核实。
from .config import pricing_rules

_PRICING = pricing_rules()
_P_COPPER = _PRICING["copper"]
_P_RATES = _PRICING["rates"]

# 数据库路径：配置里显式给路径就用，否则用 backend/data/price_library.db
_DB_CFG = _PRICING.get("db") or {}
PRICE_DB_PATH = str(_DB_CFG.get("path") or (Path(__file__).resolve().parent.parent / "data" / "price_library.db"))

COPPER_PRICE_PER_KG = float(_P_COPPER["price_per_kg"])
COPPER_DENSITY = float(_P_COPPER["density_g_cm3"])
COPPER_PRICE_SOURCE = str(_P_COPPER.get("price_source") or "")
COPPER_PRICE_UPDATED = str(_P_COPPER.get("price_updated") or "")

AUXILIARY_RATE = float(_P_RATES["auxiliary_rate"])
LABOR_OUTGOING_RATE = float(_P_RATES["labor_outgoing"])
LABOR_INCOMING_RATE = float(_P_RATES["labor_incoming"])
PROFIT_RATE_DEFAULT = float(_P_RATES.get("profit_rate", 0.08))
TAX_RATE_DEFAULT = float(_P_RATES.get("tax_rate", 0.13))
TEST_CERT_RATE = float(_P_RATES.get("test_cert_rate", 0.02))
DEFAULT_LIST_PRICE_RATIO = float(_P_RATES.get("default_list_price_ratio", 0.4))
AB_RATES = _P_RATES["ab"]

_ENCLOSURE_CFG = _PRICING.get("enclosure", {})
HEM_WH = float(_ENCLOSURE_CFG.get("hem_wh_mm", 50.0))
HEM_D = float(_ENCLOSURE_CFG.get("hem_d_mm", 20.0))
IP_PREMIUMS = dict(_ENCLOSURE_CFG.get("ip_premiums", {"IP55": 1.35, "IP65": 1.35, "IP44": 1.15}))
THICKNESS_DEFAULTS = dict(_ENCLOSURE_CFG.get("thickness_defaults", {"plastic": 1.2, "heavy_floor": 2.0, "small_box": 1.2, "default": 1.5}))
CIRCUIT_TIER_PRICES = list(_ENCLOSURE_CFG.get("circuit_tier_prices", []))

BRAND_ALIAS = dict(_PRICING["brand_alias"])
BRAND_TIERS = dict(_PRICING["brand_tiers"])
CATALOG_PRICE_BASE = {k: float(v) for k, v in _PRICING["catalog_price_base"].items() if not k.startswith("_")}
BUSBAR_SPECS = [tuple(row) for row in _PRICING["busbar_specs"]]
COPPER_STANDARD_SPECS = [tuple(row) for row in _PRICING["copper_standard_specs"]]
TYPE_A = dict(_PRICING["enclosure_type_a"])
CAT_SYNONYMS = {k: list(v) for k, v in _PRICING["category_synonyms"].items()}

# 兜底估算公式与开关：默认关闭。按公式推出来的金额不是供应商报价，不得写进报价单。
_PRICING_GATES = _PRICING.get("gates") or {}
ALLOW_FORMULA_FALLBACK_PRICING = bool(_PRICING_GATES.get("allow_formula_fallback_pricing", False))
UNKNOWN_PRICE_BASIS = str(_PRICING_GATES.get("unknown_price_basis") or "价格库未收录该规格（待人工询价）")
FORMULA_PRICE_BASIS_PREFIX = str(_PRICING_GATES.get("formula_price_basis_prefix") or "规则估算（待核）")
FALLBACK_FORMULAS = _PRICING.get("fallback_formulas") or {}
# 候选打分权重直接决定采用哪条价格记录，因此也必须可配置（改权重前先用真实图纸复核）
MATCH_SCORING = _PRICING.get("match_scoring") or {}
CURRENT_PARSE_RE = _PRICING.get("current_parse_regex") or r"(?:/|C|D|M|-)?(\d{1,4})A"
CURRENT_PARSE_FALLBACK_RE = _PRICING.get("current_parse_fallback_regex") or r"[CD](\d{1,3})"
_BRANDS = _PRICING.get("brands") or {}
DEFAULTS = _PRICING.get("defaults") or {}
REFERENCE_BRAND = str(_BRANDS.get("reference_brand") or "施耐德")
DEFAULT_BRAND = str(_BRANDS.get("default_brand") or "正泰")
JOINT_VENTURE_BRANDS = tuple(_BRANDS.get("joint_venture") or ())
COMPARISON_BRANDS = tuple(_BRANDS.get("comparison_brands") or ())
BENCHMARK_BRAND = str(_BRANDS.get("benchmark_brand") or REFERENCE_BRAND)
DOMESTIC_SUBSTITUTE = str(_BRANDS.get("domestic_substitute") or DEFAULT_BRAND)
UNKNOWN_MATERIAL_BASIS = str(_PRICING_GATES.get("unknown_material_basis") or "材料/板厚未收录（待人工询价）")
STANDARD_CURRENTS = [float(c) for c in (_PRICING.get("standard_currents") or [])]


def _formula_base_price(cat: str, poles: str, matched_curr: float) -> Optional[float]:
    """按配置公式推面价。仅在 gates.allow_formula_fallback_pricing 打开时使用。"""
    if not ALLOW_FORMULA_FALLBACK_PRICING:
        return None
    expr = (FALLBACK_FORMULAS.get("base") or {}).get(cat) or FALLBACK_FORMULAS.get("default_base")
    if not expr:
        return None
    poles_num = int(poles[0]) if poles and poles[0].isdigit() else 1
    try:
        return float(eval(expr, {"__builtins__": {}}, {"poles_num": poles_num,
                                                       "curr": matched_curr,
                                                       "matched_curr": matched_curr}))
    except Exception as exc:  # noqa: BLE001 - 配置写错不能拖垮报价
        print(f"[pricing] 兜底公式求值失败 ({cat}): {exc!r}")
        return None

# 钣金材料价用 "材料|厚度" 组合键存储，避免 JSON 不支持元组键
MATERIAL_PRICES = {}
for combo, value in _PRICING["material_prices"].items():
    if combo.startswith("_"):
        continue
    if "|" in combo:
        name, thickness = combo.rsplit("|", 1)
        MATERIAL_PRICES[(name, float(thickness))] = float(value)
    else:
        MATERIAL_PRICES[combo] = float(value)


def norm_brand(b: Any) -> str:
    """将任意品牌写法归一化为价格库规范品牌名"""
    if not b:
        return ""
    s = str(b).strip()
    return BRAND_ALIAS.get(s.lower(), BRAND_ALIAS.get(s, s))


def norm_str(s: Any) -> str:
    """去除空白、连字符并大写化"""
    return re.sub(r"[\s\-_/]+", "", str(s or "")).upper()


def get_db_connection() -> Optional[sqlite3.Connection]:
    """获取 SQLite 价格库连接"""
    if not os.path.exists(PRICE_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(f"file:{PRICE_DB_PATH}?mode=ro", uri=True)
        return conn
    except Exception:
        try:
            return sqlite3.connect(PRICE_DB_PATH)
        except Exception:
            return None


def get_price_library_stats() -> dict:
    """获取价格库全库统计信息 (总条数、品牌列表与条数)"""
    conn = get_db_connection()
    if not conn:
        return {"total_count": 0, "brands": {}, "available": False}
    try:
        c = conn.cursor()
        c.execute("SELECT count(*) FROM price_library")
        total = c.fetchone()[0]
        c.execute("SELECT brand_norm, count(*) FROM price_library GROUP BY brand_norm ORDER BY count(*) DESC")
        brands = {row[0]: row[1] for row in c.fetchall()}
        return {"total_count": total, "brands": brands, "available": True}
    finally:
        conn.close()


def lookup_price_library(
    cat_code: str,
    brand: str,
    poles: str = "",
    curr_a: Optional[int] = None,
    raw_spec: str = "",
    curve: str = "C",
    breaking_ka: int = 6,
    rel_code: str = ""
) -> Optional[dict]:
    """在 5.1 万条价格库中精确/特征匹配元器件，返回含税采购价与详情"""
    conn = get_db_connection()
    if not conn:
        return None

    b_norm = norm_brand(brand)
    c = conn.cursor()
    try:
        # 1. 精确型号对标 (若图纸已给出明确型号，如 NXB-63 或 EZ7)
        if raw_spec:
            spec_norm = norm_str(raw_spec)
            c.execute("""
                SELECT code, brand, category, model, price_tax, list_price, discount, remark
                FROM price_library
                WHERE brand_norm=? AND model_norm=?
                LIMIT 1
            """, (b_norm, spec_norm))
            r = c.fetchone()
            if r and r[4] and r[4] > 0:
                return {
                    "code": r[0], "brand": r[1], "category": r[2], "model": r[3],
                    "price_tax": float(r[4]), "list_price": float(r[5] or 0),
                    "discount": float(r[6] or 0), "match_status": "exact",
                }

        # 2. 类别 + 极数 + 额定电流特征匹配
        if curr_a is None or curr_a <= 0:
            return None

        synonyms = CAT_SYNONYMS.get(cat_code, [cat_code])
        cat_clause = " OR ".join(["category LIKE ?" for _ in synonyms])
        cat_params = [f"%{s}%" for s in synonyms]

        # 自动提取脱扣器代号 (如 3300, 3200, 4300, 4200)
        if not rel_code and raw_spec:
            m_rel = re.search(r"\b(3300|3200|4300|4200|3320|4320)\b", raw_spec)
            if m_rel:
                rel_code = m_rel.group(1)

        pole_digits = poles[0] if poles and poles[0].isdigit() else ""
        curr_patterns = [
            f"%{curr_a}A%",
            f"%C{curr_a}%",
            f"%D{curr_a}%",
            f"%/{curr_a}%",
            f"%{curr_a}/%",
        ]
        # 塑壳断路器 (MCCB) 支持标准壳架档位检索 (如 100A 查 125 壳架, 160A 查 250 壳架)
        if cat_code == "MCCB":
            if curr_a in (80, 100):
                curr_patterns.append("%125%")
            elif curr_a in (160, 200):
                curr_patterns.append("%250%")
            elif curr_a in (315, 350):
                curr_patterns.append("%400%")

        query = f"""
            SELECT code, brand, category, model, price_tax, list_price, discount, remark
            FROM price_library
            WHERE brand_norm=?
              AND ({cat_clause})
              AND category NOT LIKE '零部件%' AND category NOT LIKE '%附件%'
              AND price_tax > 0
        """

        pole_patterns = [f"%{poles}%"] if poles else ["%"]
        if pole_digits:
            pole_patterns.extend([f"%{pole_digits}极%", f"%{pole_digits}P%"])
        pole_patterns = list(dict.fromkeys(pole_patterns))

        candidates = []
        for cp in curr_patterns:
            for pp in pole_patterns:
                params = [b_norm] + cat_params + [pp, cp]
                sub_q = query + " AND model_norm LIKE ? AND model_norm LIKE ? LIMIT 20"
                c.execute(sub_q, params)
                for row in c.fetchall():
                    if row[0] not in [x["code"] for x in candidates]:
                        candidates.append({
                            "code": row[0], "brand": row[1], "category": row[2], "model": row[3],
                            "price_tax": float(row[4]), "list_price": float(row[5] or 0),
                            "discount": float(row[6] or 0),
                        })

        # 若带 P 条件候选不足，对于带数字极数的 MCCB / ATS (如 /3300, /4SZ) 扩展匹配
        if (not candidates or len(candidates) < 3) and pole_digits and cat_code in ("ATS", "MCCB"):
            for cp in curr_patterns:
                p_like = f"%/{pole_digits}%"
                params = [b_norm] + cat_params + [p_like, cp]
                sub_q = query + " AND model_norm LIKE ? AND model_norm LIKE ? LIMIT 20"
                c.execute(sub_q, params)
                for row in c.fetchall():
                    if row[0] not in [x["code"] for x in candidates]:
                        candidates.append({
                            "code": row[0], "brand": row[1], "category": row[2], "model": row[3],
                            "price_tax": float(row[4]), "list_price": float(row[5] or 0),
                            "discount": float(row[6] or 0),
                        })

        if not candidates:
            return None

        # 0. 电气性能硬门禁：分断能力 (Breaking Capacity) 绝对安全准入校验
        # 若图纸/工程需求明确指定高分断能力 (如 >=10kA)，库内低于该安全指标的候选必须硬性剔除，严禁降配冒充
        if breaking_ka > 6:
            def _get_cand_ka(cand_item: dict) -> int:
                txt = (cand_item["model"] + " " + cand_item["category"] + " " + cand_item.get("remark", "")).upper()
                m_ka = re.search(r"(\d{1,3})\s*KA\b", txt)
                if m_ka:
                    return int(m_ka.group(1))
                if re.search(r"(?:-|\b)[A-Z0-9]*H\b", txt) or "高分断" in txt:
                    return 10
                return 6

            safe_candidates = [c for c in candidates if _get_cand_ka(c) >= breaking_ka]
            if not safe_candidates:
                # 价格库无满足短路分断能力的安全物料，宁可返回 None 标疑，绝不推荐低分断物料造成灭弧隐患
                return None
            candidates = safe_candidates

        # 智能工程候选打分函数
        is_electronic_req = any(k in (raw_spec or "").upper() for k in ("电子", "MIC", "ETC", "ELECTRONIC"))
        def score_candidate(cand: dict) -> Tuple[float, float]:
            model_text = cand["model"].upper()
            cat_text = cand["category"]
            s = 0.0

            # 1. 复式脱扣器代号完全匹配 (如 3300)
            if rel_code and rel_code in model_text:
                s += float(MATCH_SCORING.get("rel_code_exact", 20.0))

            # 2. 额定电流匹配打分
            cand_curr = None
            cm = re.search(CURRENT_PARSE_RE, model_text)
            if cm:
                cand_curr = int(cm.group(1))
            else:
                cm2 = re.search(CURRENT_PARSE_FALLBACK_RE, model_text)
                if cm2:
                    cand_curr = int(cm2.group(1))

            near = float(MATCH_SCORING.get("current_near_within", 25.0))
            far = float(MATCH_SCORING.get("current_far_over", 150.0))
            if cand_curr is not None:
                if cand_curr == curr_a:
                    s += float(MATCH_SCORING.get("current_exact", 25.0))
                elif abs(cand_curr - curr_a) <= near:
                    s += float(MATCH_SCORING.get("current_near", 15.0))
                elif abs(cand_curr - curr_a) > far:
                    s += float(MATCH_SCORING.get("current_far_penalty", -80.0))

            # 3. 分断能力匹配
            if (breaking_ka >= float(MATCH_SCORING.get("breaking_ka_high_threshold", 35))
                    and ("H" in model_text or "35KA" in model_text)):
                s += float(MATCH_SCORING.get("breaking_ka_high", 10.0))
            elif (breaking_ka >= float(MATCH_SCORING.get("breaking_ka_std_threshold", 10))
                    and ("10KA" in model_text or "H" in model_text)):
                s += float(MATCH_SCORING.get("breaking_ka_std", 6.0))

            # 4. 动力多相出线回路 (3P/4P) 优先配置标准 10kA
            if poles in ("3P", "4P") and cat_code == "MCB" and ("10KA" in model_text or "H" in model_text):
                s += float(MATCH_SCORING.get("multi_pole_10ka", 8.0))

            # 5. 脱扣曲线
            if curve in model_text:
                s += float(MATCH_SCORING.get("curve_match", 5.0))

            # 6. 未要求电子脱扣器时，避免匹配高价电子式控制器
            cand_is_electronic = any(k in model_text or k in cat_text for k in ("电子", "ETC", "MIC"))
            if not is_electronic_req and cand_is_electronic:
                s += float(MATCH_SCORING.get("electronic_penalty", -15.0))

            # 7. 标准工程集采款优先于降标住宅专供
            if "专供" not in model_text and "专供" not in cat_text:
                s += float(MATCH_SCORING.get("standard_preferred", 5.0))

            return (s, -cand["price_tax"])

            return (s, -cand["price_tax"])

        candidates.sort(key=score_candidate, reverse=True)
        best = candidates[0]
        best["match_status"] = "parameter-matched"
        return best
    finally:
        conn.close()


def parse_component_features(raw_spec: str, category: str = "") -> dict:
    """从元器件规格字符串中提取特征向量 (极数、额定电流、分断能力、脱扣特性、脱扣器代号)"""
    text = (raw_spec or "").upper().replace(" ", "")
    # 识别电流 (如 16A, C20, 100MA/80A, 125A/3P)
    current_val = None
    curr_m = re.search(r"(?:/|C|D|M|-)?(\d{1,4})A", text)
    if curr_m:
        current_val = int(curr_m.group(1))
    else:
        num_m = re.search(r"[CD](\d{1,3})", text)
        if num_m:
            current_val = int(num_m.group(1))

    features = {
        "raw": raw_spec,
        "category": category.upper() if category else "OTHER",
        "poles": "1P",
        "current_a": current_val,
        "breaking_ka": 6,
        "curve": "C",
        "has_leakage": False,
        "rel_code": "",
    }

    # 1. 自动判定大类
    if "ATS" in text or "双电源" in text:
        features["category"] = "ATS"
    elif "RCBO" in text or "LE" in text or "VM" in text or "漏电" in text:
        features["category"] = "RCBO"
        features["has_leakage"] = True
    elif "MCCB" in text or "NM" in text or "NSX" in text or "塑壳" in text:
        features["category"] = "MCCB"
    elif "MCB" in text or "NXB" in text or "IC65" in text or "微断" in text or re.search(r"^[CD]\d+", text) or re.search(r"/[CD]\d+", text):
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

    # 3. 识别塑壳脱扣器代号 (如 /3300, /3200, /4300)
    rm = re.search(r"/(3300|3200|4300|4200)", text)
    if rm:
        features["rel_code"] = rm.group(1)

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
    brand: str = "",
    category: str = ""
) -> Tuple[float, float, str]:
    """计算单个元器件的 (实际采购单价, 目录面价, 计价判定依据)

    价格库记录与内置规则需区分展示。内置规则属于估算，不构成供应商报价或采购依据。
    """
    brand = (brand or REFERENCE_BRAND).strip() or REFERENCE_BRAND
    if not raw_spec or str(raw_spec).strip() in ("-", "待确认", "待定"):
        return 0.0, 0.0, "规格参数缺失（待确认）"

    feats = parse_component_features(raw_spec, category)
    cat = feats["category"]
    poles = feats["poles"]
    curr = feats["current_a"]

    # 规格未标注额定电流时，如实标疑，严禁凭空编造
    if curr is None:
        return 0.0, 0.0, "规格电流缺失（待核）"

    # 一级尝试：查询本地导入的价格记录；来源和有效期需在报价时另行核验。
    matched_lib = lookup_price_library(
        cat,
        brand,
        poles=poles,
        curr_a=curr,
        raw_spec=raw_spec,
        curve=feats.get("curve", "C"),
        breaking_ka=feats.get("breaking_ka", 6),
        rel_code=feats.get("rel_code", "")
    )
    if matched_lib:
        cost_p = round(matched_lib["price_tax"], 2)
        list_p = round(matched_lib["list_price"], 2) if matched_lib["list_price"] > 0 else round(cost_p / DEFAULT_LIST_PRICE_RATIO, 2)
        code = matched_lib["code"]
        b_name = matched_lib["brand"]
        m_name = matched_lib["model"]
        basis = f"价格库匹配[{code}] {b_name} {m_name} 含税￥{cost_p:.2f} (面价￥{list_p:.2f})"
        return cost_p, list_p, basis

    # 二级回退：标准面价知识库（CATALOG_PRICE_BASE）。命中就是命中，未命中不再编造金额。
    standard_currs = STANDARD_CURRENTS or [10, 16, 20, 25, 32, 40, 50, 63, 80, 100, 125, 160, 200, 250, 400, 630]
    matched_curr = min(standard_currs, key=lambda x: abs(x - curr))

    key = f"{cat}-{poles}-{int(matched_curr)}A"
    list_price = CATALOG_PRICE_BASE.get(key)
    exact_match = list_price is not None
    estimated = False

    if not list_price:
        list_price = _formula_base_price(cat, poles, float(matched_curr))
        estimated = list_price is not None
    if not list_price:
        # 价格库与标准面价库都没这档：如实返回待询价，别用公式凑一个数字出来冒充报价。
        # 若项目确实接受估算，在 config/pricing.json 打开 gates.allow_formula_fallback_pricing。
        return 0.0, 0.0, UNKNOWN_PRICE_BASIS

    tier_info = BRAND_TIERS.get(norm_brand(brand), BRAND_TIERS.get(REFERENCE_BRAND, {}))
    if cat in ("MCB", "RCBO"):
        discount = tier_info.get("mcb_discount", 0.45)
    elif cat == "MCCB":
        discount = tier_info.get("mccb_discount", 0.42)
    elif cat == "ATS":
        discount = tier_info.get("ats_discount", 0.48)
    else:
        discount = (tier_info.get("mcb_discount", 0.45) + tier_info.get("mccb_discount", 0.42)) / 2.0

    multiplier = tier_info.get("multiplier", 1.0)
    cost_price = round(list_price * discount * multiplier, 2)
    if exact_match:
        basis = f"标准库匹配[{key}] 面价￥{list_price} 折扣率{discount:.2f}"
    elif estimated:
        basis = f"{FORMULA_PRICE_BASIS_PREFIX}[{key}] 面价￥{list_price} 折扣率{discount:.2f}"
    return cost_price, round(list_price, 2), basis


def parse_box_dimensions(size_str: Any) -> Optional[Tuple[float, float, float]]:
    """从尺寸规格字符串（如 '800*600*200' / '600x800x250' / 'GGD 800×2200×600'）中提取 (宽, 高, 深) 毫米数"""
    if not size_str:
        return None
    m = re.search(r"(\d{2,4})\s*[*xX×]\s*(\d{2,4})\s*[*xX×]\s*(\d{2,4})", str(size_str))
    if m:
        nums = [float(m.group(1)), float(m.group(2)), float(m.group(3))]
        depth = min(nums)
        remaining = [n for n in nums if n != depth]
        if len(remaining) < 2:
            remaining = [nums[0], nums[1]]
        width, height = remaining[0], remaining[1]
        return width, height, depth
    return None


def calc_enclosure_unfolding(
    width: float,
    height: float,
    depth: float,
    box_type: str = "安装板",
    material: str = "冷轧钢板",
    thickness: float = 1.5,
    door_material: Optional[str] = None,
    door_thickness: Optional[float] = None,
    A: Optional[float] = None,
    cover_panel_m2: float = 0.0,
    cover_panel_price: float = 0.0
) -> dict:
    """非标配电箱箱体价格计算（基于物理展开面积法）

    公式：
      底面面积 = (宽+50折边) × (高+50折边) × 底面系数A       （mm² × A）
      四周面积 = ((高+50折边) + (宽+50折边)) × (深+20折边) × 2 （mm²）
      门面积   = (宽+50折边) × (高+50折边)                   （mm²）
      箱体价格 = (底面面积 + 四周面积) × 材料加工单价 + 门面积 × 门加工单价
      封板价格 = 封板面积(m²) × 封板材料单价
      总价     = 底面价 + 四周价 + 门价 + 封板价
    """
    hem_wh = HEM_WH  # 宽/高折边 mm，来自 config/pricing.json
    hem_d = HEM_D   # 深度折边 mm，来自 config/pricing.json
    coef_a = A if A is not None else TYPE_A.get(box_type, 2.0)

    # 查取材料加工单价 (元/m²)
    mat_price = MATERIAL_PRICES.get((material, thickness))
    if mat_price is None:
        # 材料/板厚不在价格表里：不得拿某个固定单价顶上，如实标疑待询价
        return {
            "width_mm": width, "height_mm": height, "depth_mm": depth, "A": coef_a,
            "bottom_area_m2": 0.0, "side_area_m2": 0.0, "door_area_m2": 0.0,
            "total_area_m2": 0.0, "bottom_price": 0.0, "side_price": 0.0,
            "door_price": 0.0, "cover_price": 0.0, "total_price": 0.0,
            "desc": f"{material}{thickness}mm {UNKNOWN_MATERIAL_BASIS}", "price_known": False,
        }
    door_mat = door_material or material
    door_thk = door_thickness if door_thickness is not None else thickness
    door_price = MATERIAL_PRICES.get((door_mat, door_thk), mat_price)

    bottom_area_mm2 = (width + hem_wh) * (height + hem_wh) * coef_a
    side_area_mm2 = ((height + hem_wh) + (width + hem_wh)) * (depth + hem_d) * 2.0
    door_area_mm2 = (width + hem_wh) * (height + hem_wh)

    bottom_area_m2 = bottom_area_mm2 / 1_000_000.0
    side_area_m2 = side_area_mm2 / 1_000_000.0
    door_area_m2 = door_area_mm2 / 1_000_000.0
    total_area_m2 = bottom_area_m2 + side_area_m2 + door_area_m2

    bottom_price = round(bottom_area_m2 * mat_price, 2)
    side_price = round(side_area_m2 * mat_price, 2)
    door_price_total = round(door_area_m2 * door_price, 2)
    cover_price = round(cover_panel_m2 * cover_panel_price, 2)
    total_price = round(bottom_price + side_price + door_price_total + cover_price, 2)

    desc = f"钣金展开法 [{int(width)}x{int(height)}x{int(depth)} {material}{thickness}mm 结构:{box_type} A={coef_a} 展面{total_area_m2:.2f}㎡]"
    return {
        "width_mm": width,
        "height_mm": height,
        "depth_mm": depth,
        "A": coef_a,
        "bottom_area_m2": round(bottom_area_m2, 4),
        "side_area_m2": round(side_area_m2, 4),
        "door_area_m2": round(door_area_m2, 4),
        "total_area_m2": round(total_area_m2, 4),
        "bottom_price": bottom_price,
        "side_price": side_price,
        "door_price": door_price_total,
        "cover_price": cover_price,
        "total_price": total_price,
        "desc": desc,
        "price_known": True,
    }


def resolve_box_enclosure_specs(box: dict, w: float, h: float, d: float, is_floor: bool) -> Tuple[str, str, float]:
    """从配电箱/柜体元数据与上下文动态解析钣金外壳结构类型 (b_type)、材质 (material) 与板厚 (thickness)。
    彻底消除写死“安装板”和写死“冷轧钢板”，支持图纸中真实标注的立板、横梁、不锈钢、双门、防护等结构要求。
    """
    raw_texts = [
        str(box.get("structure") or ""),
        str(box.get("box_type") or ""),
        str(box.get("type") or ""),
        str(box.get("install_type") or box.get("install") or ""),
        str(box.get("note") or ""),
        str(box.get("name") or box.get("box_name") or ""),
        str(box.get("material") or ""),
    ]
    combined = " ".join(raw_texts).strip()

    # 1. 材质解析 (Material)
    mat_explicit = str(box.get("material") or "").strip()
    if mat_explicit and any(m in mat_explicit for m in ("冷轧", "不锈钢", "塑料", "PC")):
        if "304" in mat_explicit:
            material = "304#不锈钢板"
        elif "201" in mat_explicit:
            material = "201#不锈钢板"
        elif "不锈钢" in mat_explicit:
            material = "304#不锈钢板"
        elif "塑料" in mat_explicit or "PC" in mat_explicit:
            material = "塑料面板（PC料，阻燃）"
        else:
            material = "冷轧钢板"
    elif "304" in combined:
        material = "304#不锈钢板"
    elif "201" in combined:
        material = "201#不锈钢板"
    elif "不锈钢" in combined:
        material = "304#不锈钢板"
    elif "塑料" in combined or "PC料" in combined or "PC面板" in combined:
        material = "塑料面板（PC料，阻燃）"
    else:
        material = "冷轧钢板"

    # 2. 板厚解析 (Thickness)
    thk_explicit = box.get("thickness")
    thickness = None
    if thk_explicit is not None:
        try:
            val = float(thk_explicit)
            if val > 0:
                thickness = val
        except (ValueError, TypeError):
            pass

    if thickness is None:
        m_thk = re.search(r"(?:厚度|δ|t|厚)\s*[:=]?\s*([0-2](?:\.\d+)?)\s*mm?", combined, re.IGNORECASE)
        if not m_thk:
            m_thk = re.search(r"\b([12]\.[025])\s*mm\b", combined, re.IGNORECASE)
        if m_thk:
            try:
                thickness = float(m_thk.group(1))
            except ValueError:
                pass

    if thickness is None:
        if material == "塑料面板（PC料，阻燃）":
            thickness = float(THICKNESS_DEFAULTS.get("plastic", 1.2))
        elif is_floor or max(w, h) >= 1600:
            thickness = float(THICKNESS_DEFAULTS.get("heavy_floor", 2.0))
        elif max(w, h) <= 450:
            thickness = float(THICKNESS_DEFAULTS.get("small_box", 1.2))
        else:
            thickness = float(THICKNESS_DEFAULTS.get("default", 1.5))

    # 3. 动态解析箱体/柜体内部钣金结构 (b_type)
    # 彻底杜绝写死“安装板”：支持立板、立板+支架、安装立板、双门、横梁、空箱等通用工业结构
    struct_explicit = str(box.get("structure") or "").strip()
    if struct_explicit and struct_explicit in TYPE_A:
        b_type = struct_explicit
    elif re.search(r"立板.*支架.*封板", combined):
        b_type = "立板+支架+封板"
    elif re.search(r"立板.*支架", combined):
        b_type = "立板+支架"
    elif re.search(r"立板.*横梁", combined):
        b_type = "立板+横梁"
    elif re.search(r"立板.*封板", combined):
        b_type = "立板+封板"
    elif "立板" in combined or "安装立板" in combined:
        b_type = "立板"
    elif "安装板+支架+封板+帽子" in combined or ("安装板+支架+封板" in combined and ("防雨" in combined or "帽子" in combined)):
        b_type = "安装板+支架+封板+帽子"
    elif "安装板+支架+封板" in combined or "安装版+支架+封板" in combined:
        b_type = "安装板+支架+封板"
    elif "安装板+帽子" in combined or "安装版+帽子" in combined or (("防雨" in combined or "室外" in combined) and ("帽" in combined or "檐" in combined)):
        b_type = "安装板+帽子"
    elif "空箱-横梁" in combined or ("空箱" in combined and "横梁" in combined):
        b_type = "空箱-横梁"
    elif "空箱" in combined:
        b_type = "空箱"
    elif "面盖" in combined or "单独面盖" in combined or "仅门" in combined:
        b_type = "面盖"
    elif "双门" in combined or "带内门" in combined or "二层门" in combined:
        b_type = "双门"
    elif "横梁" in combined:
        b_type = "横梁"
    else:
        # 未注结构时，根据箱体物理规格确定性推导：
        # 落地柜/高度>=1600mm：重型设备必须配备安装板+支撑支架+封板骨架
        # 挂墙小型箱体：标配单层元件安装底板
        if is_floor or max(w, h) >= 1600:
            b_type = "安装板+支架+封板"
        else:
            b_type = "安装板"

    return b_type, material, thickness


def estimate_box_enclosure_price(box: dict, circuits_count: int) -> Tuple[float, str]:
    """估算配电箱外壳制造成本。

    1. 优先使用物理钣金展开面积法（提取 box.size / dimensions 中的 WxHxD 规格，动态解析材质、板厚、立板/支架结构）；
    2. 无具体尺寸标注时，无缝回退至回路数与落地/挂墙形式定额推导，标明显式估算基准，绝不伪造尺寸。
    """
    ip = str(box.get("ip_rating") or "IP30").upper()
    box_type = str(box.get("box_type") or "").upper()
    size_raw = str(box.get("size") or box.get("box_type") or "")
    is_floor = "落地" in str(box.get("install_type") or box.get("install") or "") or "GGD" in box_type or "XL" in box_type

    # 1. 物理钣金展开面积法
    dims = parse_box_dimensions(size_raw)
    if dims:
        w, h, d = dims
        b_type, material, thk = resolve_box_enclosure_specs(box, w, h, d, is_floor)
        unfold = calc_enclosure_unfolding(w, h, d, box_type=b_type, material=material, thickness=thk)
        price = unfold["total_price"]
        desc = unfold["desc"]
        for ip_prefix, prem in IP_PREMIUMS.items():
            if ip_prefix in ip:
                price = round(price * float(prem), 2)
                desc += f" [{ip_prefix}高防护处理]"
                break
        return price, desc

    # 2. 传统定额规则兜底（未注物理尺寸时，严禁假装实测尺寸，如实标明显式估算基准）
    tier_found = None
    for tier in CIRCUIT_TIER_PRICES:
        if circuits_count >= tier.get("min_circuits", 0):
            tier_found = tier
            break
    if tier_found:
        base_price = float(tier_found["price"])
        model_desc = tier_found["desc"].format(circuits_count=circuits_count)
    elif is_floor or circuits_count > 24:
        base_price = 1450.0  # 落地动力柜 / GGD / XL-21
        model_desc = f"落地式配电柜外壳（估算:按{circuits_count}回路定额推导，GGD/XL-21参考800x1800x600 2.0mm，待核定）"
    elif circuits_count > 12:
        base_price = 580.0   # 中型明装配电箱
        model_desc = f"挂墙式配电箱外壳（估算:按{circuits_count}回路定额推导，参考600x800x200 1.5mm，待核定）"
    elif circuits_count > 6:
        base_price = 360.0   # 小型明装照明箱
        model_desc = f"明装照明配电箱外壳（估算:按{circuits_count}回路定额推导，参考400x500x180 1.2mm，待核定）"
    else:
        base_price = 220.0   # 微型控制箱
        model_desc = f"小型端子/控制箱外壳（估算:按{circuits_count}回路定额推导，参考300x400x160 1.2mm，待核定）"

    for ip_prefix, prem in IP_PREMIUMS.items():
        if ip_prefix in ip:
            base_price = round(base_price * float(prem), 2)
            model_desc += f" [{ip_prefix}高防护处理]"
            break

    return round(base_price, 2), model_desc


def estimate_copper_busbar_cost(main_current_a: int, box_width_m: float = 0.8) -> Tuple[float, float, str]:
    """根据进线主开关电流计算母排铜排重量与成本。若电流未提供或 <=0，绝不默认 63A，返回待核定。"""
    if main_current_a <= 0:
        return 0.0, 0.0, "主回路额定电流未标注（未计入主铜排费用，待核定）"

    matched_spec = BUSBAR_SPECS[0]
    for spec in BUSBAR_SPECS:
        if main_current_a <= spec[0]:
            matched_spec = spec
            break
    else:
        matched_spec = BUSBAR_SPECS[-1]

    _, spec_code, weight_per_m = matched_spec
    # 三相主母线(3根) + N排(1根) + PE地排(1根)
    allow_main = float(DEFAULTS.get("busbar_allowance_main_m", 0.4))
    allow_pe_n = float(DEFAULTS.get("busbar_allowance_pe_n_m", 0.2))
    total_length_m = (box_width_m + allow_main) * 3 + (box_width_m + allow_pe_n) * 2
    total_weight_kg = round(total_length_m * weight_per_m, 2)
    busbar_cost = round(total_weight_kg * COPPER_PRICE_PER_KG, 2)
    detail = f"主线电流{main_current_a}A 选用[{spec_code}] 重量{total_weight_kg}kg@￥{COPPER_PRICE_PER_KG}/kg"
    return busbar_cost, total_weight_kg, detail


def calc_copper_bar_quote(
    main_current_a: Optional[int] = None,
    spec: Optional[str] = None,
    length_m: Optional[float] = None,
    qty: int = 1,
    density: float = COPPER_DENSITY,
    price_per_kg: float = COPPER_PRICE_PER_KG,
    cabinet_type: str = "GGD"
) -> dict:
    """计算低压动力柜（GGD/GCK/MNS）12米标准主母排铜排负荷选型与含税造价。

    公式：
      截面积 S(mm²) = 宽 × 厚
      体积 V(cm³)   = S × 长度(m)
      重量 D(kg)    = V × 8.9 ÷ 1000
      报价(元)      = D × 铜排单价(元/kg) × 根数
    """
    if length_m is None:
        length_m = float(DEFAULTS.get("busbar_length_m", 12.0))
    w, h = 0.0, 0.0
    spec_label = ""
    load_range = ""

    if spec:
        try:
            parts = spec.lower().replace("×", "x").split("x")
            w, h = float(parts[0]), float(parts[1])
            spec_label = f"TM-{int(w)}x{int(h)}"
        except Exception:
            pass
    elif main_current_a is not None and main_current_a > 0:
        matched = COPPER_STANDARD_SPECS[-1]
        for s in COPPER_STANDARD_SPECS:
            if main_current_a >= s[0]:
                matched = s
                break
        w, h, spec_label, load_range = matched[1], matched[2], f"TM-{matched[3]}", matched[4]
    else:
        return {
            "spec": "",
            "weight_kg": 0.0,
            "total_price": 0.0,
            "desc": "主回路额定电流未标注（动力母排暂未计价，待核定）",
        }

    s_mm2 = w * h
    v_cm3 = s_mm2 * length_m
    weight_kg = round(v_cm3 * density / 1000.0 * qty, 3)
    quote_total = round(weight_kg * price_per_kg, 2)
    desc = f"{cabinet_type}动力柜标准12m铜排 [{spec_label} 载流{load_range or str(main_current_a)+'A'}] 重{weight_kg}kg@￥{price_per_kg}/kg"
    return {
        "spec": spec_label,
        "width_mm": w,
        "thickness_mm": h,
        "length_m": length_m,
        "qty": qty,
        "section_mm2": s_mm2,
        "weight_kg": weight_kg,
        "unit_price_kg": price_per_kg,
        "total_price": quote_total,
        "desc": desc,
    }


def calculate_box_quotation(
    box: dict,
    circuits: list[dict],
    components: list[dict],
    brand: str = "",
    profit_rate: Optional[float] = None,
    tax_rate: Optional[float] = None,
    use_ab_model: bool = False,
    ab_mode: str = "auto"
) -> dict:
    """计算单个成套配电箱柜的完整工业造价明细

    兼具成套厂通行的 A/B 定额六步组价法与传统制造工时定额，
    默认输出满足既有 excel/app 契约，并挂载完整的 ab_quotation 结构。
    """
    # 1. 元器件造价合计 (全面覆盖传入元器件与回路中断路器开关)
    comp_total = 0.0
    comp_list_total = 0.0
    priced_components = []

    effective_components = list(components)
    has_breakers = any(
        "断路器" in comp.get("name", "") or "开关" in comp.get("name", "") or "微断" in comp.get("name", "")
        for comp in effective_components
    )
    if not has_breakers and circuits:
        for c in circuits:
            brk = (c.get("breaker_spec") or c.get("breaker") or "").strip()
            if not brk or brk in ("-", "待确认"):
                continue
            is_inc = c.get("circuit_type") == "incoming" or "进线" in str(c.get("load_name") or "") or "进线" in str(c.get("circuit_no") or "")
            effective_components.append({
                "name": "进线断路器" if is_inc else "分支断路器",
                "spec": brk,
                "quantity": 1,
            })

    for comp in effective_components:
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
    main_curr = 0
    for c in circuits:
        if c.get("circuit_type") == "incoming" or "进线" in str(c.get("load_name") or "") or "进线" in str(c.get("circuit_no") or ""):
            feat = parse_component_features(c.get("breaker_spec", "") or c.get("breaker", ""))
            if feat.get("current_a"):
                main_curr = max(main_curr, feat["current_a"])

    # 若回路未明确标注进线，从箱体属性或技术备注中推导主开关规格
    if main_curr == 0:
        for k in ("main_switch", "incoming_spec", "main_breaker", "note"):
            val = str(box.get(k) or "")
            if val:
                feat = parse_component_features(val)
                if feat.get("current_a"):
                    main_curr = max(main_curr, feat["current_a"])
                    break

    box_type = str(box.get("box_type") or "").upper()
    is_power_cab = "GGD" in box_type or "GCK" in box_type or "MNS" in box_type or "动力柜" in box_type
    size_raw = str(box.get("size") or box.get("box_type") or "")
    dims = parse_box_dimensions(size_raw)
    box_w_m = (dims[0] / 1000.0) if dims and dims[0] > 0 else float(DEFAULTS.get("box_width_m", 0.8))

    if main_curr > 0:
        if is_power_cab and main_curr >= 250:
            copper_q = calc_copper_bar_quote(main_curr, cabinet_type=box_type or "GGD")
            busbar_cost = copper_q["total_price"]
            copper_weight_kg = copper_q["weight_kg"]
            busbar_desc = copper_q["desc"]
        else:
            busbar_cost, copper_weight_kg, busbar_desc = estimate_copper_busbar_cost(main_curr, box_width_m=box_w_m)
    else:
        busbar_cost, copper_weight_kg, busbar_desc = 0.0, 0.0, "进线规格待确认（主母排暂未计价）"

    # 4. 二次线及辅材 (传统定额)
    auxiliary_cost = round(comp_total * AUXILIARY_RATE, 2)

    # 5. 人工组装调试费 (传统定额)
    incoming_count = sum(1 for c in circuits if c.get("circuit_type") == "incoming" or "进线" in str(c.get("load_name") or "") or "进线" in str(c.get("circuit_no") or ""))
    outgoing_count = max(0, circuits_count - incoming_count)
    labor_cost = round(incoming_count * LABOR_INCOMING_RATE + outgoing_count * LABOR_OUTGOING_RATE, 2)

    # 6. 型式试验与CCC分摊 (一般按出厂价分摊，费率来自 config/pricing.json)
    factory_cost = enclosure_cost + comp_total + busbar_cost + auxiliary_cost + labor_cost
    test_cert_cost = round(factory_cost * TEST_CERT_RATE, 2)

    # 7. 制造总成本与传统含税出厂报价
    p_rate = profit_rate if profit_rate is not None else PROFIT_RATE_DEFAULT
    t_rate = tax_rate if tax_rate is not None else TAX_RATE_DEFAULT
    total_cost = factory_cost + test_cert_cost
    subtotal_with_profit = total_cost * (1.0 + p_rate)
    trad_tax_included = round(subtotal_with_profit * (1.0 + t_rate), 2)

    # 8. 行业标准 A/B 费率定额模型计算 (造价极客标准六步法)
    is_jv = (norm_brand(brand) in JOINT_VENTURE_BRANDS) if ab_mode == "auto" else (ab_mode == "joint-venture")
    ab_info = AB_RATES["joint-venture"] if is_jv else AB_RATES["domestic"]
    A_rate = ab_info["A"]
    B_rate = ab_info["B"]

    sum_main = round(comp_total, 2)
    sum_aux = round(sum_main * A_rate, 2)
    sum_mat = round(sum_main + sum_aux, 2)
    sum_set = round(sum_mat * B_rate, 2)
    sum_box = round(enclosure_cost + busbar_cost, 2)
    sum_total = round(sum_mat + sum_set + sum_box, 2)

    ab_quotation = {
        "sum_main": sum_main,
        "sum_aux": sum_aux,
        "sum_mat": sum_mat,
        "sum_set": sum_set,
        "sum_box": sum_box,
        "sum_total": sum_total,
        "A": A_rate,
        "B": B_rate,
        "ab_label": ab_info["label"],
        "steps": [
            {"step": "①", "name": "主要元件", "amount": sum_main, "formula": "元器件含税采购合计"},
            {"step": "②", "name": "辅材", "amount": sum_aux, "formula": f"① × {A_rate*100:.1f}%"},
            {"step": "③", "name": "材料合计", "amount": sum_mat, "formula": "① + ②"},
            {"step": "④", "name": "成套费用", "amount": sum_set, "formula": f"③ × {B_rate*100:.1f}%"},
            {"step": "⑤", "name": "箱体及母排", "amount": sum_box, "formula": f"外壳 ￥{enclosure_cost:.2f} + 母排 ￥{busbar_cost:.2f}" if busbar_cost > 0 else enclosure_desc},
            {"step": "⑥", "name": "单箱总报价", "amount": sum_total, "formula": "③ + ④ + ⑤"},
        ]
    }

    final_price = sum_total if use_ab_model else trad_tax_included

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
        "ab_quotation": ab_quotation,
        "profit_rate": p_rate,
        "tax_rate": t_rate,
        "final_tax_included": final_price,
        "components": priced_components,
    }


def calculate_box_ab_quotation(
    box: dict,
    circuits: list[dict],
    components: list[dict],
    brand: str = "",
    ab_mode: str = "auto"
) -> dict:
    """便捷调用：直接采用工业 A/B 差异化定额模型计算单箱出厂报价"""
    return calculate_box_quotation(box, circuits, components, brand=brand, use_ab_model=True, ab_mode=ab_mode)


def compare_brands_quotation(
    box: dict,
    circuits: list[dict],
    components: list[dict]
) -> dict:
    """横向多品牌比价矩阵 (外资一线 施耐德/ABB vs 国产良信/正泰/德力西)"""
    results = {}
    for brand in COMPARISON_BRANDS:
        q = calculate_box_quotation(box, circuits, components, brand=brand)
        results[brand] = {
            "final_amount": q["final_tax_included"],
            "component_cost": q["cost_breakdown"]["component_cost"],
            "factory_cost": q["cost_breakdown"]["factory_total_cost"],
        }

    schneider_amount = results[BENCHMARK_BRAND]["final_amount"]
    chint_amount = results[DOMESTIC_SUBSTITUTE]["final_amount"]
    saving_ratio = round((schneider_amount - chint_amount) / schneider_amount * 100, 1) if schneider_amount else 0

    return {
        "brands": results,
        "benchmark_brand": BENCHMARK_BRAND,
        "domestic_substitute": DOMESTIC_SUBSTITUTE,
        "total_saving_ratio_pct": saving_ratio,
    }
