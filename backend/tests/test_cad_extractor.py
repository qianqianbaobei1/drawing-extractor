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

        # 4. Panel codes
        m = PANEL_CODE_PATTERN.search("01AC1-1 插座配电箱系统图")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "01AC1-1")

        m_xf = PANEL_CODE_PATTERN.search("消防01ATPY03配电箱系统图")
        self.assertIsNotNone(m_xf)
        self.assertEqual(m_xf.group(1), "01ATPY03")

        # Exclude drawing numbers and code references
        self.assertIsNone(PANEL_CODE_PATTERN.search("07S20230052SD-01B-01"))
        self.assertIsNone(PANEL_CODE_PATTERN.search("图集01-1"))

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


if __name__ == "__main__":
    unittest.main()
