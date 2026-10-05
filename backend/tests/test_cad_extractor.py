# -*- coding: utf-8 -*-
"""Unit tests for cad_extractor (CAD native vector topology extraction)."""
import unittest
import ezdxf
from extractor.cad_extractor import (
    _extract_box_metadata,
    extract_cad_table_data,
    RE_BREAKER,
    RE_CABLE,
    RE_CIRCUIT_NO,
    PANEL_CODE_PATTERN,
)


class TestCadExtractor(unittest.TestCase):
    def test_regex_patterns(self):
        # 1. Circuit numbers
        self.assertTrue(RE_CIRCUIT_NO.match("N1"))
        self.assertTrue(RE_CIRCUIT_NO.match("WL2"))
        self.assertTrue(RE_CIRCUIT_NO.match("PY-1-P1"))
        self.assertTrue(RE_CIRCUIT_NO.match("BF-2A-P1"))
        self.assertFalse(RE_CIRCUIT_NO.match("SC25"))

        # 2. Breakers
        self.assertTrue(RE_BREAKER.search("MCB-C16A/1P"))
        self.assertTrue(RE_BREAKER.search("RCBO/2P C20+VM(30mA,瞬时)"))
        self.assertTrue(RE_BREAKER.search("MCCB-100MA/32A/3P"))
        self.assertTrue(RE_BREAKER.search("ATS 63A/4P"))

        # 3. Cables vs Breakers
        self.assertTrue(RE_CABLE.match("ZR-BV-450/750V-3x4.0 SC25 FC WC"))
        self.assertTrue(RE_CABLE.match("WDZN-YJV-0.6/1kV-5x16 SC50"))

        # 4. Panel codes (通用多项目语法，杜绝单图硬编码)
        m = PANEL_CODE_PATTERN.search("01AC1-1 插座配电箱系统图")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "01AC1-1")

        m_xf = PANEL_CODE_PATTERN.search("消防01ATPY03配电箱系统图")
        self.assertIsNotNone(m_xf)
        self.assertEqual(m_xf.group(1), "01ATPY03")

        # 支持各式单字母/双字母/工业柜体/并排箱体通用代号
        for code_str, exp in [
            ("配电箱 C01", "C01"),
            ("配电箱 C08", "C08"),
            ("02AK01 控制箱", "02AK01"),
            ("2SAL2 照明配电箱", "2SAL2"),
            ("AW1/2/3/4-CDZ 充电桩端子箱", "AW1/2/3/4-CDZ"),
            ("1AL1 照明箱", "1AL1"),
            ("GQ1 消防水泵柜", "GQ1"),
            ("CD1 消防泵箱", "CD1"),
        ]:
            match = PANEL_CODE_PATTERN.search(code_str)
            self.assertIsNotNone(match, f"Failed to match {code_str}")
            self.assertEqual(match.group(1), exp)

        # 严格排除图纸编号、图集编号、工程单位、电缆穿管等杂项
        for invalid_str in [
            "07S20230052SD-01B-01",
            "TSW230152SD-DZ-06",
            "03D702-3",
            "07SD101-8",
            "图集01-1",
            "63A",
            "10KW",
            "220V",
            "50HZ",
            "600MM",
            "IP65",
            "SC25",
        ]:
            self.assertIsNone(PANEL_CODE_PATTERN.search(invalid_str), f"Should reject {invalid_str}")

    def test_extract_box_metadata(self):
        meta = _extract_box_metadata(
            "消防01ATPY01配电箱系统图",
            [
                ("嵌墙安装", 100, 200, 10),
                ("防护等级：IP54", 100, 210, 10),
                ("JXF", 100, 220, 10),
                ("明显消防标志,并作防火处理", 100, 230, 10),
            ],
        )
        self.assertEqual(meta["name"], "配电箱")
        self.assertEqual(meta["install"], "嵌墙安装")
        self.assertEqual(meta["ip_rating"], "IP54")
        self.assertEqual(meta["size"], "JXF")
        self.assertEqual(meta["note"], "明显消防标志,并作防火处理")

    def test_synthetic_dxf_extraction(self):
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # Add panel header
        msp.add_text("01AC1-1 插座配电箱", dxfattribs={"insert": (-10000, 200000), "height": 300})

        # Add branch circuits
        msp.add_text("N1", dxfattribs={"insert": (-9000, 195000), "height": 100})
        msp.add_text("L1,N,PE", dxfattribs={"insert": (-8000, 195000), "height": 100})
        msp.add_text("RCBO/2P C20+VM(30mA,瞬时)", dxfattribs={"insert": (-7000, 195000), "height": 100})
        msp.add_text("ZR-BV-450/750V-3x4.0 SC25", dxfattribs={"insert": (-6000, 195000), "height": 100})
        msp.add_text("插座回路", dxfattribs={"insert": (-5000, 195000), "height": 100})

        raw = extract_cad_table_data(doc)
        self.assertEqual(len(raw.boxes), 1)
        self.assertEqual(raw.boxes[0].code, "01AC1-1")
        self.assertEqual(len(raw.circuits), 1)
        c = raw.circuits[0]
        self.assertEqual(c.circuit_no, "N1")
        self.assertEqual(c.phase, "L1,N,PE")
        self.assertEqual(c.breaker, "RCBO/2P C20+VM(30mA,瞬时)")
        self.assertEqual(c.cable, "ZR-BV-450/750V-3x4.0 SC25")
        self.assertEqual(c.load_name, "插座回路")

    def test_adaptive_scale_1_to_1_meter_dxf(self):
        """验证 1:1 米制/小比例图纸（字高 3.0，行距 6.0，非数万毫米尺度）自适应聚类正确，无粘连无漏检。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # 箱体标头：字高 4.5，坐标在 (10, 50)
        msp.add_text("1AL1 照明配电箱", dxfattribs={"insert": (10.0, 50.0), "height": 4.5})

        # 回路 1：Y = 40.0，字高 2.5
        msp.add_text("WL1", dxfattribs={"insert": (12.0, 40.0), "height": 2.5})
        msp.add_text("L1", dxfattribs={"insert": (16.0, 40.0), "height": 2.5})
        msp.add_text("iC65N-C16/1P", dxfattribs={"insert": (22.0, 40.0), "height": 2.5})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (32.0, 40.0), "height": 2.5})
        msp.add_text("走廊照明", dxfattribs={"insert": (45.0, 40.0), "height": 2.5})

        # 回路 2：Y = 34.0 (间距 6.0 单位，若容差固定为 550 则两回路必定被粘连合并)
        msp.add_text("WL2", dxfattribs={"insert": (12.0, 34.0), "height": 2.5})
        msp.add_text("L2", dxfattribs={"insert": (16.0, 34.0), "height": 2.5})
        msp.add_text("iC65N-C20/1P", dxfattribs={"insert": (22.0, 34.0), "height": 2.5})
        msp.add_text("BV-3x4.0 SC20", dxfattribs={"insert": (32.0, 34.0), "height": 2.5})
        msp.add_text("应急照明", dxfattribs={"insert": (45.0, 34.0), "height": 2.5})

        raw = extract_cad_table_data(doc)
        self.assertEqual(len(raw.boxes), 1)
        self.assertEqual(raw.boxes[0].code, "1AL1")
        self.assertEqual(len(raw.circuits), 2)

        c1 = next(c for c in raw.circuits if c.circuit_no == "WL1")
        self.assertEqual(c1.breaker, "iC65N-C16/1P")
        self.assertEqual(c1.cable, "BV-3x2.5 SC20")
        self.assertEqual(c1.load_name, "走廊照明")

        c2 = next(c for c in raw.circuits if c.circuit_no == "WL2")
        self.assertEqual(c2.breaker, "iC65N-C20/1P")
        self.assertEqual(c2.cable, "BV-3x4.0 SC20")
        self.assertEqual(c2.load_name, "应急照明")


if __name__ == "__main__":
    unittest.main()
