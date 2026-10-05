# -*- coding: utf-8 -*-
"""自动化测试：验证规格解析、报价公式、钣金估算、费率计算与本地价格记录查询。"""
import unittest
from extractor.pricing import (
    parse_component_features,
    calculate_component_unit_price,
    estimate_box_enclosure_price,
    estimate_copper_busbar_cost,
    calculate_box_quotation,
    calculate_box_ab_quotation,
    compare_brands_quotation,
    lookup_price_library,
    get_price_library_stats,
    calc_enclosure_unfolding,
    calc_copper_bar_quote,
    parse_box_dimensions,
    norm_brand,
)


class TestPricingEngine(unittest.TestCase):

    def test_feature_parsing(self):
        # 1. 常见断路器
        f1 = parse_component_features("MCB-C16A/1P")
        self.assertEqual(f1["category"], "MCB")
        self.assertEqual(f1["poles"], "1P")
        self.assertEqual(f1["current_a"], 16)
        self.assertEqual(f1["curve"], "C")

        # 2. 塑壳断路器
        f2 = parse_component_features("MCCB-160MA/125A/3P 35kA")
        self.assertEqual(f2["category"], "MCCB")
        self.assertEqual(f2["poles"], "3P")
        self.assertEqual(f2["current_a"], 125)
        self.assertEqual(f2["breaking_ka"], 35)

        # 3. 双电源转换开关
        f3 = parse_component_features("ATS-160A/4P")
        self.assertEqual(f3["category"], "ATS")
        self.assertEqual(f3["poles"], "4P")
        self.assertEqual(f3["current_a"], 160)

    def test_component_unit_price_and_discount(self):
        # 施耐德 vs 正泰
        schneider_price, list_price, basis_s = calculate_component_unit_price("MCB-C16A/1P", brand="施耐德")
        chint_price, _, basis_c = calculate_component_unit_price("MCB-C16A/1P", brand="正泰")

        self.assertGreater(list_price, 0)
        self.assertGreater(schneider_price, 0)
        self.assertGreater(chint_price, 0)
        # 国产平替单价应低于外资一线
        self.assertLess(chint_price, schneider_price)

    def test_enclosure_and_busbar(self):
        # 小型箱 vs 落地大柜
        p_small, desc_s = estimate_box_enclosure_price({"ip_rating": "IP30"}, circuits_count=4)
        p_big, desc_b = estimate_box_enclosure_price({"ip_rating": "IP55", "install_type": "落地"}, circuits_count=28)
        self.assertLess(p_small, p_big)
        self.assertIn("高防护", desc_b)

        # 铜排计算
        cost, weight, desc = estimate_copper_busbar_cost(250, box_width_m=0.8)
        self.assertGreater(weight, 0)
        self.assertGreater(cost, 0)
        self.assertIn("TM-30x4", desc)

    def test_box_quotation_and_brand_comparison(self):
        box = {"box_code": "01ATPY03", "box_type": "JXF", "ip_rating": "IP44", "install_type": "挂墙明装"}
        circuits = [
            {"circuit_type": "incoming", "breaker_spec": "ATS-160A/4P", "load_name": "双电源进线"},
            {"circuit_type": "outgoing", "breaker_spec": "MCB-C16A/1P", "load_name": "照明回路1"},
            {"circuit_type": "outgoing", "breaker_spec": "MCCB-100MA/80A/3P", "load_name": "排烟风机"},
        ]
        components = [
            {"name": "双电源转换开关", "spec": "ATS-160A/4P", "quantity": 1},
            {"name": "微型断路器", "spec": "MCB-C16A/1P", "quantity": 1},
            {"name": "塑壳断路器", "spec": "MCCB-100MA/80A/3P", "quantity": 1},
        ]

        q = calculate_box_quotation(box, circuits, components, brand="正泰")
        self.assertIn("final_tax_included", q)
        self.assertGreater(q["final_tax_included"], 0)
        self.assertGreater(q["cost_breakdown"]["enclosure_cost"], 0)
        self.assertGreater(q["cost_breakdown"]["copper_busbar_cost"], 0)

        cmp = compare_brands_quotation(box, circuits, components)
        self.assertIn("brands", cmp)
        self.assertGreater(cmp["total_saving_ratio_pct"], 10.0)

    def test_missing_current_pricing_never_hallucinates_16a(self):
        """额定电流缺失时严禁盲目默认16A计价，如实返回 0.0 元与待核备注。"""
        feats = parse_component_features("MCB-1P")
        self.assertIsNone(feats["current_a"])

        unit_p, list_p, basis = calculate_component_unit_price("MCB-1P", brand="正泰")
        self.assertEqual(unit_p, 0.0)
        self.assertEqual(list_p, 0.0)
        self.assertIn("规格电流缺失", basis)

        unit_p2, list_p2, basis2 = calculate_component_unit_price("-", brand="正泰")
        self.assertEqual(unit_p2, 0.0)
        self.assertIn("规格参数缺失", basis2)

    def test_zero_or_unspecified_busbar_cost(self):
        """进线额定电流未标注时，母排费用严禁按 63A 捏造计入，如实返回 0 元。"""
        cost, weight, desc = estimate_copper_busbar_cost(0)
        self.assertEqual(cost, 0.0)
        self.assertEqual(weight, 0.0)
        self.assertIn("未标注", desc)

        # 箱体无进线回路时，母排费用为 0，且不收取进线组装人工费
        box = {"box_code": "01AL1", "box_type": "PZ30"}
        circuits = [
            {"circuit_type": "outgoing", "breaker_spec": "MCB-C16A/1P", "load_name": "照明1"}
        ]
        q = calculate_box_quotation(box, circuits, [], brand="正泰")
        self.assertEqual(q["cost_breakdown"]["copper_busbar_cost"], 0.0)
        self.assertEqual(q["cost_breakdown"]["labor_cost"], 45.0)  # 仅 1 个出线回路 45 元，无进线 120 元

    def test_51k_price_library_stats_and_matching(self):
        """验证本地 SQLite 价格库记录数量与查询行为；不验证价格来源或有效期。"""
        stats = get_price_library_stats()
        self.assertTrue(stats["available"])
        self.assertGreater(stats["total_count"], 50000)
        self.assertIn("德力西", stats["brands"])
        self.assertIn("正泰", stats["brands"])
        self.assertIn("施耐德", stats["brands"])
        self.assertIn("上海良信", stats["brands"])

        # 品牌别名归一化
        self.assertEqual(norm_brand("良信"), "上海良信")
        self.assertEqual(norm_brand("施耐德电气"), "施耐德")
        self.assertEqual(norm_brand("正泰电器"), "正泰")
        self.assertEqual(norm_brand("delixi"), "德力西")

        # 本地价格记录精确及特征匹配查询
        mcb_chint = lookup_price_library("MCB", "正泰", poles="1P", curr_a=16)
        self.assertIsNotNone(mcb_chint)
        self.assertGreater(mcb_chint["price_tax"], 0.0)
        self.assertIn("正泰", mcb_chint["brand"])

        mcb_schneider = lookup_price_library("MCB", "施耐德", poles="1P", curr_a=16)
        self.assertIsNotNone(mcb_schneider)
        self.assertGreater(mcb_schneider["price_tax"], mcb_chint["price_tax"])

        mccb_delixi = lookup_price_library("MCCB", "德力西", poles="3P", curr_a=125)
        self.assertIsNotNone(mccb_delixi)
        self.assertGreater(mccb_delixi["price_tax"], 0.0)

    def test_sheet_metal_unfolding_area_method(self):
        """验证工业级非标箱体钣金展开面积法 (calc_enclosure_unfolding) 与尺寸解析"""
        # 1. 尺寸正则解析
        d1 = parse_box_dimensions("800*600*200")
        self.assertIsNotNone(d1)
        self.assertEqual(d1, (800.0, 600.0, 200.0))

        d2 = parse_box_dimensions("GGD 800×2200×600")
        self.assertIsNotNone(d2)
        self.assertEqual(d2, (800.0, 2200.0, 600.0))

        # 2. 600x800x250 安装板 (A=2.0) 展开面积法核算
        unfold = calc_enclosure_unfolding(600, 800, 250, box_type="安装板", material="冷轧钢板", thickness=1.5)
        self.assertEqual(unfold["A"], 2.0)
        self.assertGreater(unfold["total_area_m2"], 2.0)
        self.assertGreater(unfold["total_price"], 300.0)
        self.assertIn("钣金展开法", unfold["desc"])

        # 3. 落地动力柜 800x1800x600 2.0mm 展开面积法核算
        unfold_floor = calc_enclosure_unfolding(800, 1800, 600, box_type="安装板+支架+封板", material="冷轧钢板", thickness=2.0)
        self.assertEqual(unfold_floor["A"], 3.5)
        self.assertGreater(unfold_floor["total_area_m2"], 10.0)
        self.assertGreater(unfold_floor["total_price"], 1800.0)

        # 4. 箱体询价自动接驳展开面积法
        p_unfold, desc_u = estimate_box_enclosure_price({"box_type": "600x800x250", "ip_rating": "IP30"}, circuits_count=8)
        self.assertIn("钣金展开法", desc_u)
        self.assertAlmostEqual(p_unfold, unfold["total_price"], places=1)

    def test_power_cabinet_12m_copper_busbar_quota(self):
        """验证低压动力配电柜 12 米标准铜排定额计算 (calc_copper_bar_quote)"""
        # 1600A -> 100x10 规格, 12m, 铜密度 8.9: 重量 = 1000 * 12 * 8.9 / 1000 = 106.8 kg
        q_1600 = calc_copper_bar_quote(1600, length_m=12.0, qty=1, price_per_kg=76.50)
        self.assertEqual(q_1600["spec"], "TM-100x10")
        self.assertAlmostEqual(q_1600["weight_kg"], 106.8, places=2)
        self.assertAlmostEqual(q_1600["total_price"], round(106.8 * 76.50, 2), places=1)

        # 630A -> 60x6 规格
        q_630 = calc_copper_bar_quote(630, length_m=12.0)
        self.assertEqual(q_630["spec"], "TM-60x6")
        self.assertGreater(q_630["total_price"], 2000.0)

        # 100A -> 20x3 规格
        q_100 = calc_copper_bar_quote(100, length_m=12.0)
        self.assertEqual(q_100["spec"], "TM-20x3")

        # 未标注电流返回 0.0 与待核定提示
        q_zero = calc_copper_bar_quote(0)
        self.assertEqual(q_zero["total_price"], 0.0)
        self.assertIn("未标注", q_zero["desc"])

    def test_ab_differential_quota_model(self):
        """验证成套厂通行的 A/B 差异化成套费率定额模型 (造价极客标准六步法)"""
        box = {"box_code": "AP1", "box_type": "600x800x250", "ip_rating": "IP30"}
        circuits = [
            {"circuit_type": "incoming", "breaker_spec": "MCCB-3P-125A", "load_name": "进线"},
            {"circuit_type": "outgoing", "breaker_spec": "MCB-C16A/1P", "load_name": "照明1"},
            {"circuit_type": "outgoing", "breaker_spec": "MCB-C32A/3P", "load_name": "动力1"},
        ]
        comps = [
            {"name": "塑壳断路器", "spec": "MCCB-3P-125A", "quantity": 1},
            {"name": "微断", "spec": "MCB-C16A/1P", "quantity": 1},
            {"name": "微断", "spec": "MCB-C32A/3P", "quantity": 1},
        ]

        # 1. 国产品牌 (正泰): A=16.7%, B=19.7%
        q_dom = calculate_box_quotation(box, circuits, comps, brand="正泰")
        ab_d = q_dom["ab_quotation"]
        self.assertAlmostEqual(ab_d["A"], 0.167, places=3)
        self.assertAlmostEqual(ab_d["B"], 0.197, places=3)
        # 验证六步递推准确性
        # ① 主要元件
        self.assertEqual(ab_d["sum_main"], ab_d["steps"][0]["amount"])
        # ② 辅材 = ① × A
        self.assertAlmostEqual(ab_d["sum_aux"], round(ab_d["sum_main"] * 0.167, 2), places=1)
        # ③ 材料合计 = ① + ②
        self.assertAlmostEqual(ab_d["sum_mat"], round(ab_d["sum_main"] + ab_d["sum_aux"], 2), places=1)
        # ④ 成套费用 = ③ × B
        self.assertAlmostEqual(ab_d["sum_set"], round(ab_d["sum_mat"] * 0.197, 2), places=1)
        # ⑥ 单箱总报价 = ③ + ④ + ⑤
        self.assertAlmostEqual(ab_d["sum_total"], round(ab_d["sum_mat"] + ab_d["sum_set"] + ab_d["sum_box"], 2), places=1)

        # 2. 合资品牌 (施耐德): A=12.0%, B=18.3%
        q_jv = calculate_box_quotation(box, circuits, comps, brand="施耐德")
        ab_jv = q_jv["ab_quotation"]
        self.assertAlmostEqual(ab_jv["A"], 0.120, places=3)
        self.assertAlmostEqual(ab_jv["B"], 0.183, places=3)
        self.assertIn("合资品牌定额", ab_jv["ab_label"])

        # 3. 使用便捷函数 calculate_box_ab_quotation 返回含税 A/B 总价
        q_ab = calculate_box_ab_quotation(box, circuits, comps, brand="正泰")
        self.assertEqual(q_ab["final_tax_included"], q_ab["ab_quotation"]["sum_total"])


if __name__ == "__main__":
    unittest.main()
