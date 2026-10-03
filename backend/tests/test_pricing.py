# -*- coding: utf-8 -*-
"""自动化测试：验证元器件特征解析、自动组价核算、母排铜排公式与多品牌比价矩阵"""
import unittest
from extractor.pricing import (
    parse_component_features,
    calculate_component_unit_price,
    estimate_box_enclosure_price,
    estimate_copper_busbar_cost,
    calculate_box_quotation,
    compare_brands_quotation,
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


if __name__ == "__main__":
    unittest.main()
