# -*- coding: utf-8 -*-
"""Unit tests for cad_extractor (CAD native vector topology extraction)."""
import unittest
import ezdxf
from extractor.cad_extractor import (
    _extract_box_metadata,
    _specific_caption,
    _text_leads_with_panel,
    _looks_like_cable,
    _unique_joined,
    assign_preview_bboxes,
    extract_cad_table_data,
    extract_panel_code,
    _is_spd_callout,
    _row_device_field,
    RE_BREAKER,
    RE_BREAKER_FALLBACK,
    RE_CABLE,
    RE_PHASE,
    RE_CIRCUIT_NO,
    PANEL_CODE_PATTERN,
)
from extractor.schema import Box, Circuit


class TestCadExtractor(unittest.TestCase):
    def test_regex_patterns(self):
        # 1. Circuit numbers
        self.assertTrue(RE_CIRCUIT_NO.match("N1"))
        self.assertTrue(RE_CIRCUIT_NO.match("WL2"))
        self.assertTrue(RE_CIRCUIT_NO.match("1WL1"))
        self.assertTrue(RE_CIRCUIT_NO.match("B1-WP2"))
        self.assertTrue(RE_CIRCUIT_NO.match("2SAL3-WL1"))
        self.assertTrue(RE_CIRCUIT_NO.match("PY-1-P1"))
        self.assertTrue(RE_CIRCUIT_NO.match("BF-2A-P1"))
        self.assertFalse(RE_CIRCUIT_NO.match("SC25"))
        # 端子号、点位号不是回路：单字母回路不能跟在任意横线前缀后，楼层前缀只留一到两位。
        self.assertFalse(RE_CIRCUIT_NO.match("S-1c-C1"))
        self.assertFalse(RE_CIRCUIT_NO.match("925P01"))

        # 2. Breakers
        self.assertTrue(RE_BREAKER.search("MCB-C16A/1P"))
        self.assertTrue(RE_BREAKER.search("RCBO/2P C20+VM(30mA,瞬时)"))
        self.assertTrue(RE_BREAKER.search("MCCB-100MA/32A/3P"))
        self.assertTrue(RE_BREAKER.search("ATS 63A/4P"))
        self.assertTrue(RE_BREAKER.search("iC65N-C16/1P"))
        self.assertTrue(RE_BREAKER_FALLBACK.search("C16A/1P"))
        # 设备位号末尾的 -D1 不是断路器极数。
        self.assertFalse(RE_BREAKER_FALLBACK.search("3/5TY-D1"))
        self.assertFalse(RE_BREAKER.search("3/5TY-D1"))
        # 曲线字母后面再跟短横线是设备位号，不是断路器额定值。
        self.assertFalse(RE_BREAKER_FALLBACK.search("B1-05"))
        self.assertFalse(RE_BREAKER_FALLBACK.search("B1-02"))
        self.assertTrue(RE_BREAKER_FALLBACK.search("C16A/1P"))

        # 相序可以重复写 L：L1/L2/L3、L1,L2,L3 与 L123、L1,N,PE 同一类。
        self.assertTrue(RE_PHASE.match("L1"))
        self.assertTrue(RE_PHASE.match("L123"))
        self.assertTrue(RE_PHASE.match("L1,2,3"))
        self.assertTrue(RE_PHASE.match("L1/L2/L3"))
        self.assertTrue(RE_PHASE.match("L1,L2,L3"))
        self.assertTrue(RE_PHASE.match("L1,N,PE"))
        self.assertTrue(RE_PHASE.match("L2,N,PE"))
        self.assertFalse(RE_PHASE.match("PE"))
        self.assertFalse(RE_PHASE.match("WL1"))
        self.assertFalse(RE_PHASE.match("照明"))

        # 3. Cables vs Breakers
        self.assertTrue(RE_CABLE.match("ZR-BV-450/750V-3x4.0 SC25 FC WC"))
        self.assertTrue(RE_CABLE.match("WDZN-YJV-0.6/1kV-5x16 SC50"))
        self.assertTrue(RE_CABLE.match("WDZ-YJY-5x16-CT/SC50-WC,FC"))
        self.assertTrue(_looks_like_cable("WDZ-YJY-5x10-CT/JDG40-WC,FC"))
        self.assertFalse(_looks_like_cable("MCCB-63L/40A/4300-30mA"))
        self.assertTrue(_is_spd_callout("CPM-R40T"))
        self.assertTrue(_is_spd_callout("SPD 4P"))
        self.assertTrue(_is_spd_callout("I级试验电涌保护器:"))
        self.assertFalse(_is_spd_callout("ISCB2 65H2+IPRU 40r"))
        self.assertEqual(_row_device_field("LC1-D32C"), "contactor")
        self.assertEqual(_row_device_field("LRD-16C"), "thermal")
        self.assertEqual(_row_device_field("LDR-21C"), "thermal")
        self.assertEqual(_row_device_field("BH-0.66-100/5 /0.5"), "ct")
        self.assertIsNone(_row_device_field("MCB-C16A/1P"))
        self.assertIsNone(_row_device_field("WDZ-YJY-5x6"))
        self.assertEqual(
            _unique_joined([("进线详上级配电箱系统图",), ("进线详上级配电箱系统图",), ("电源进线引自地下变配电室",)]),
            "进线详上级配电箱系统图 电源进线引自地下变配电室",
        )

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
            ("XFDT-AT 消防电梯配电箱", "XFDT-AT"),
            ("WDDT-AT 电梯配电箱", "WDDT-AT"),
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
            "LC1-D09C",
            "LC1-D40AC",
            "LC1-F115",
            "ESB20",
            "HND07E3Y/K",
            "HND09E3Y/K",
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
        self.assertEqual(_extract_box_metadata("C01 单相电表箱", [])["name"], "单相电表箱")
        self.assertEqual(_specific_caption("采用铠装电缆由路灯照明配电箱00AL01埋地敷设至灯具。"), "")
        self.assertEqual(meta["install"], "嵌墙安装")
        self.assertEqual(meta["ip_rating"], "IP54")
        self.assertEqual(meta["size"], "JXF")
        self.assertEqual(meta["note"], "明显消防标志,并作防火处理")
        self.assertFalse(meta["quantity_recognized"])

    def test_quantity_beside_the_title_is_the_box_count(self):
        meta = _extract_box_metadata(
            "9KX3 照明配电箱",
            [("共2台", 400, 0, 80), ("走廊照明 1台", 3000, -800, 80), ("WDZN-BYJ-2×1.5", 2000, -800, 80)],
        )
        self.assertTrue(meta["quantity_recognized"])
        self.assertEqual(meta["quantity"], 2)

        conflict = _extract_box_metadata(
            "9KX3 照明配电箱 共3台",
            [("共2台", 400, 0, 80)],
        )
        self.assertFalse(conflict["quantity_recognized"])
        self.assertEqual(conflict["quantity"], 1)

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
        self.assertEqual(raw.boxes[0].name, "插座配电箱")
        self.assertEqual(len(raw.circuits), 1)
        c = raw.circuits[0]
        self.assertEqual(c.circuit_no, "N1")
        self.assertEqual(c.phase, "L1,N,PE")
        self.assertEqual(c.breaker, "RCBO/2P C20+VM(30mA,瞬时)")
        self.assertEqual(c.cable, "ZR-BV-450/750V-3x4.0 SC25")
        self.assertEqual(c.load_name, "插座回路")

    def test_box_name_is_taken_from_the_same_code_elsewhere(self):
        """系统图上只剩箱号时，同一箱号在另一处写的名称仍属于这台箱。别的箱号借不走。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3", dxfattribs={"insert": (0, 500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, 200), "height": 200})
        msp.add_text("照明", dxfattribs={"insert": (800, 200), "height": 200})
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (300000, 300000), "height": 200})
        msp.add_text(
            "采用铠装电缆由车库照明配电箱9KX3埋地敷设至灯具。",
            dxfattribs={"insert": (300000, 280000), "height": 200},
        )
        msp.add_text("8KX3", dxfattribs={"insert": (0, -5000), "height": 200})
        msp.add_text("N2", dxfattribs={"insert": (0, -5300), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (800, -5300), "height": 200})

        raw = extract_cad_table_data(doc)
        names = {box.code: box.name for box in raw.boxes}
        self.assertEqual(names["9KX3"], "照明配电箱")
        self.assertEqual(names["8KX3"], "配电箱")

    def test_device_model_does_not_take_the_panel_whose_code_has_no_digit(self):
        """接触器型号不是箱号。没有数字的 XFDT-AT 仍是箱号，回路挂到设备编号那一行。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("设备编号", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("XFDT-AT", dxfattribs={"insert": (2500, 0), "height": 200})
        msp.add_text("设备名称", dxfattribs={"insert": (5500, 0), "height": 200})
        msp.add_text("消防电梯配电箱", dxfattribs={"insert": (8000, 0), "height": 200})
        msp.add_text("ESB20", dxfattribs={"insert": (8000, 5000), "height": 200})
        msp.add_text("WP1", dxfattribs={"insert": (8000, 4000), "height": 200})
        msp.add_text("电梯控制箱", dxfattribs={"insert": (11000, 4000), "height": 200})
        msp.add_text("设备编号", dxfattribs={"insert": (0, -30000), "height": 200})
        msp.add_text("AC-FJ1", dxfattribs={"insert": (2500, -30000), "height": 200})
        msp.add_text("设备名称", dxfattribs={"insert": (5500, -30000), "height": 200})
        msp.add_text("排风机控制箱", dxfattribs={"insert": (8000, -30000), "height": 200})
        msp.add_text("LC1-D09C", dxfattribs={"insert": (8000, -25000), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (8000, -26000), "height": 200})
        msp.add_text("排风机", dxfattribs={"insert": (11000, -26000), "height": 200})
        msp.add_text("2AN4", dxfattribs={"insert": (0, 20000), "height": 200})
        msp.add_text("2WP6", dxfattribs={"insert": (0, 19000), "height": 200})
        msp.add_text("MCB-63/C16A/1P", dxfattribs={"insert": (4000, 19000), "height": 200})
        msp.add_text("应急照明配电箱XFDT-AT（主）", dxfattribs={"insert": (9000, 19000), "height": 200})

        raw = extract_cad_table_data(doc)
        names = {box.code: box.name for box in raw.boxes}
        self.assertEqual(names.get("XFDT-AT"), "消防电梯配电箱")
        self.assertEqual(names.get("AC-FJ1"), "排风机控制箱")
        self.assertNotIn("ESB20", names)
        self.assertNotIn("LC1-D09C", names)
        loads = {(c.box, c.circuit_no): c.load_name for c in raw.circuits if c.circuit_no != "进线"}
        self.assertEqual(loads.get(("XFDT-AT", "WP1")), "电梯控制箱")
        self.assertEqual(loads.get(("AC-FJ1", "N1")), "排风机")
        self.assertEqual(loads.get(("2AN4", "2WP6")), "应急照明配电箱XFDT-AT（主）")
        self.assertNotIn(("XFDT-AT", "2WP6"), loads)
        self.assertTrue(_text_leads_with_panel("路灯照明配电箱00ALZ", "00ALZ"))
        self.assertFalse(_text_leads_with_panel("消防电梯配电箱XFDT-AT（主）", "XFDT-AT"))

    def test_catalog_sample_code_is_not_a_panel(self):
        """字母与数字交错的设备样本编号不是箱号，旁边的总开关不能另立一台箱。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, -800), "height": 200})
        msp.add_text("MCCB-63/C32A/3P", dxfattribs={"insert": (2500, -800), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (6000, -800), "height": 200})
        msp.add_text("HND07E3Y/K", dxfattribs={"insert": (0, -80000), "height": 200})
        msp.add_text("MCCB-225L/3P", dxfattribs={"insert": (4000, -80000), "height": 200})
        msp.add_text("WP1", dxfattribs={"insert": (0, -78800), "height": 200})

        raw = extract_cad_table_data(doc)
        names = {box.code: box.name for box in raw.boxes}
        self.assertEqual(names.get("9KX3"), "照明配电箱")
        self.assertNotIn("HND07E3Y/K", names)
        loads = {(c.box, c.circuit_no): c.load_name for c in raw.circuits if c.circuit_no != "进线"}
        self.assertEqual(loads.get(("9KX3", "N1")), "插座")
        incomers = {c.box: c.breaker for c in raw.circuits if c.circuit_no == "进线"}
        self.assertNotIn("HND07E3Y/K", incomers)
        self.assertNotIn("MCCB-225L", incomers.get("9KX3", ""))

    def test_symbol_legend_is_not_the_incomer_breaker(self):
        """符号说明里提到的型号不是总开关。更高处的「图中…表示…」让给本箱自己的开关。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("MCB-C63A/3P", dxfattribs={"insert": (2500, 200), "height": 200})
        msp.add_text(
            "3、图中MCCB-160L/125A/3300，表示带隔离功能塑壳断路器",
            dxfattribs={"insert": (2500, 1500), "height": 200},
        )
        msp.add_text("N1", dxfattribs={"insert": (0, -800), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (2500, -800), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (6000, -800), "height": 200})

        raw = extract_cad_table_data(doc)
        incomers = {c.box: c.breaker for c in raw.circuits if c.circuit_no == "进线"}
        self.assertEqual(incomers.get("9KX3"), "MCB-C63A/3P")
        self.assertFalse(any("表示" in (c.breaker or "") for c in raw.circuits))

    def test_breaker_closer_to_another_panel_stays_there(self):
        """没有出线的箱不收窗口里更靠近另一台箱的开关。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("7KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("C10A/1P", dxfattribs={"insert": (500, -800), "height": 200})
        msp.add_text("8KX3 照明配电箱", dxfattribs={"insert": (12000, 0), "height": 200})

        raw = extract_cad_table_data(doc)
        incomers = {c.box: c.breaker for c in raw.circuits if c.circuit_no == "进线"}
        self.assertEqual(incomers.get("7KX3"), "C10A/1P")
        self.assertNotEqual(incomers.get("8KX3"), "C10A/1P")

    def test_cabinet_schedule_measure_is_not_the_branch_rating(self):
        """和回路同一行、但更靠近表头的电流和功率，留在那一列。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("Ir", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("1443A", dxfattribs={"insert": (1200, 0), "height": 200})
        msp.add_text("无功补偿量（kVar）", dxfattribs={"insert": (2500, 0), "height": 200})
        msp.add_text("146.2", dxfattribs={"insert": (4000, 0), "height": 200})
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (8000, 2500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (8000, 0), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (5500, 0), "height": 200})
        msp.add_text("WDZ-YJY-5x16-CT", dxfattribs={"insert": (6200, 0), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (7000, 0), "height": 200})

        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.current_a, "")
        self.assertNotEqual(row.power_kw, "146.2")
        self.assertEqual(row.load_name, "插座")

    def test_only_the_incomer_row_keeps_a_spare_breaker(self):
        """和总开关同一行的隔离开关留下。离得很远的另一行开关不是进线侧器件。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 2500), "height": 200})
        msp.add_text("MCB-C63A/3P", dxfattribs={"insert": (2500, 1300), "height": 200})
        msp.add_text("32A/4P/PC/R", dxfattribs={"insert": (2800, 1200), "height": 200})
        msp.add_text("MCB-C20A/1P", dxfattribs={"insert": (8000, 1250), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (2500, 0), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (5000, 0), "height": 200})
        msp.add_text("MCCB-225L/3P", dxfattribs={"insert": (2500, -2500), "height": 200})

        raw = extract_cad_table_data(doc)
        inc = next(c.breaker for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "进线")
        branch = next(c.breaker for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        specs = [d.spec for d in raw.extra_devices if d.used_in.startswith("9KX3")]
        self.assertEqual(inc, "MCB-C63A/3P")
        self.assertEqual(branch, "MCB-C16A/1P")
        self.assertIn("32A/4P/PC/R", specs)
        self.assertNotIn("MCCB-225L/3P", specs)
        self.assertNotIn("MCB-C20A/1P", specs)

    def test_surge_model_and_class_label_belong_to_the_nearest_panel(self):
        """浪涌型号和整段的「Ⅱ级-4P」归最近的箱。试验说明句不是保护器。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, -800), "height": 200})
        msp.add_text("插座", dxfattribs={"insert": (3000, -800), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (1500, -800), "height": 200})
        msp.add_text("CPM-R40T", dxfattribs={"insert": (500, 400), "height": 200})
        msp.add_text("Ⅱ级-4P", dxfattribs={"insert": (500, 600), "height": 200})
        msp.add_text("ISCB2 65H2+IPRU 40r", dxfattribs={"insert": (500, 200), "height": 200})
        msp.add_text("II级试验8/20μs In≥30kA，", dxfattribs={"insert": (500, 1600), "height": 200})
        msp.add_text("8KX3 照明配电箱", dxfattribs={"insert": (0, 1100), "height": 200})

        raw = extract_cad_table_data(doc)
        devices = {d.used_in.split()[0]: d.spec for d in raw.extra_devices if d.name == "浪涌保护器"}
        self.assertIn("CPM-R40T", devices.get("9KX3", ""))
        self.assertIn("Ⅱ级-4P", devices.get("9KX3", ""))
        self.assertNotIn("试验", devices.get("9KX3", ""))
        self.assertNotIn("IPRU", devices.get("9KX3", ""))
        self.assertNotIn("8KX3", devices)
        loads = {c.circuit_no: c.load_name for c in raw.circuits if c.box == "9KX3" and c.circuit_no != "进线"}
        self.assertEqual(loads.get("N1"), "插座")

    def test_contactor_and_thermal_stay_on_their_circuit_row(self):
        """同一行的接触器和热继电器进对应栏。另一台箱同一行的型号留在那一台。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 2500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (1500, 0), "height": 200})
        msp.add_text("LC1-D32C", dxfattribs={"insert": (3000, 0), "height": 200})
        msp.add_text("LRD-16C", dxfattribs={"insert": (4500, 0), "height": 200})
        msp.add_text("WDZN-YJY-3x6-SC25", dxfattribs={"insert": (6000, 0), "height": 200})
        msp.add_text("双速轴流风机", dxfattribs={"insert": (8000, 0), "height": 200})
        msp.add_text("N2", dxfattribs={"insert": (0, -1600), "height": 200})
        msp.add_text("备用", dxfattribs={"insert": (8000, -1600), "height": 200})
        msp.add_text("BH-0.66-150/5", dxfattribs={"insert": (2600, -600), "height": 200})
        msp.add_text("8KX3 照明配电箱", dxfattribs={"insert": (20000, 2500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (20000, -4000), "height": 200})
        msp.add_text("LC1-D09C", dxfattribs={"insert": (23000, -4000), "height": 200})
        msp.add_text("LDR-21C", dxfattribs={"insert": (24500, -4000), "height": 200})
        msp.add_text("BH-0.66-100/5", dxfattribs={"insert": (26000, -4000), "height": 200})
        msp.add_text("风机", dxfattribs={"insert": (28000, -4000), "height": 200})

        raw = extract_cad_table_data(doc)
        own = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        other = next(c for c in raw.circuits if c.box == "8KX3" and c.circuit_no == "N1")
        self.assertEqual(own.breaker, "MCB-C16A/1P")
        self.assertEqual(own.contactor, "LC1-D32C")
        self.assertEqual(own.thermal, "LRD-16C")
        self.assertEqual(own.ct, "")
        self.assertEqual(own.load_name, "双速轴流风机")
        self.assertEqual(other.contactor, "LC1-D09C")
        self.assertEqual(other.thermal, "LDR-21C")
        self.assertEqual(other.ct, "BH-0.66-100/5")

    def test_thermal_setting_range_is_not_the_branch_current(self):
        """叠在热继电器上的整定范围并进继电器。旁边单独的电流仍留给回路。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 2500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("16A", dxfattribs={"insert": (1800, 0), "height": 200})
        msp.add_text("LC1-D32C", dxfattribs={"insert": (3000, 0), "height": 200})
        msp.add_text("LRD-16C", dxfattribs={"insert": (4500, 0), "height": 200})
        msp.add_text("9~13A", dxfattribs={"insert": (4500, 300), "height": 200})
        msp.add_text("双速轴流风机", dxfattribs={"insert": (8000, 0), "height": 200})
        msp.add_text("8KX3 照明配电箱", dxfattribs={"insert": (20000, 2500), "height": 200})
        msp.add_text("N1", dxfattribs={"insert": (20000, 0), "height": 200})
        msp.add_text("12~18A", dxfattribs={"insert": (21500, 0), "height": 200})
        msp.add_text("风机", dxfattribs={"insert": (24000, 0), "height": 200})

        raw = extract_cad_table_data(doc)
        own = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        other = next(c for c in raw.circuits if c.box == "8KX3" and c.circuit_no == "N1")
        self.assertEqual(own.thermal, "LRD-16C 9~13A")
        self.assertEqual(own.current_a, "16A")
        self.assertEqual(own.load_name, "双速轴流风机")
        self.assertEqual(other.thermal, "")
        self.assertEqual(other.current_a, "12~18A")

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

    def test_code_cell_binds_same_row_caption(self):
        """编号和箱名分在相邻单元格时仍是一台箱，不依赖某一张图的箱号。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("设备编号", dxfattribs={"insert": (0, 0), "height": 3})
        msp.add_text("9KX3", dxfattribs={"insert": (12, 0), "height": 3})
        msp.add_text("设备名称", dxfattribs={"insert": (28, 0), "height": 3})
        msp.add_text("排风机控制箱", dxfattribs={"insert": (48, 0), "height": 3})
        msp.add_text("WL1", dxfattribs={"insert": (12, -12), "height": 2.5})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (30, -12), "height": 2.5})
        raw = extract_cad_table_data(doc)
        self.assertIn("9KX3", [box.code for box in raw.boxes])
        box = next(box for box in raw.boxes if box.code == "9KX3")
        self.assertIn("控制箱", box.name + box.note + "")
        self.assertTrue(any(c.circuit_no == "WL1" and c.box == "9KX3" for c in raw.circuits))

    def test_panel_grammar_rejects_cable_and_device_specs(self):
        for text in (
            "PC20",
            "5-PC20-WC",
            "10P20",
            "ATSE-63A/4P",
            "XLP000-25A/3P",
            "AC220V",
            "WDZ-YJY-5x16-CT/SC50-WC,FC",
            "WL1",
            "C20A",
            "1X",
            "40R",
            "65H2",
            "TMY-4X",
        ):
            self.assertIsNone(PANEL_CODE_PATTERN.search(text), text)
        self.assertEqual(PANEL_CODE_PATTERN.search("9KX3 排风机控制箱").group(1), "9KX3")
        self.assertEqual(PANEL_CODE_PATTERN.search("10RDAL").group(1), "10RDAL")
        self.assertEqual(PANEL_CODE_PATTERN.search("配电箱 C01").group(1), "C01")
        self.assertEqual(PANEL_CODE_PATTERN.search("B1ATPY1 车库排烟风机配电箱").group(1), "B1ATPY1")

    def test_vertical_code_column_is_a_riser(self):
        """同一横坐标、步距稳定的不同箱号是干线列，不要求旁边写着设备名称。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        # 3RDAL 与 1RDAL 之间空一档，步距仍应看成同一列。
        ys = (1000, 840, 760, 680, 600)
        for code, y in zip(("1RDAL", "3RDAL", "4RDAL", "5RDAL", "6RDAL"), ys):
            msp.add_text(code, dxfattribs={"insert": (500, y), "height": 10})
        msp.add_text("9KX3", dxfattribs={"insert": (2000, 1000), "height": 10})
        raw = extract_cad_table_data(doc)
        codes = {box.code for box in raw.boxes}
        for code in ("1RDAL", "3RDAL", "4RDAL", "5RDAL", "6RDAL"):
            self.assertIn(code, codes)
        self.assertNotIn("9KX3", codes)

    def test_duplicate_code_keeps_the_diagram_not_the_riser_label(self):
        """同一箱号写了两处时，回路挂到旁边真有回路号的那一处。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 300})
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (200000, 0), "height": 300})
        msp.add_text("WL1", dxfattribs={"insert": (200000, -2000), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (210000, -2000), "height": 100})
        raw = extract_cad_table_data(doc)
        self.assertIn("9KX3", {box.code for box in raw.boxes})
        self.assertTrue(any(c.box == "9KX3" and c.circuit_no == "WL1" for c in raw.circuits))

    def test_plain_code_beside_circuits_beats_a_distant_caption(self):
        """系统图里只有箱号和回路号，字高并不更大，也要盖过远处干线上的同名标注。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("9KX3", dxfattribs={"insert": (200000, 0), "height": 100})
        for index, number in enumerate(("WL1", "WL2", "WL3")):
            msp.add_text(number, dxfattribs={"insert": (200000, -400 * (index + 1)), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (201200, -400), "height": 100})
        raw = extract_cad_table_data(doc)
        self.assertTrue(any(c.box == "9KX3" and c.circuit_no == "WL1" for c in raw.circuits))

    def test_stacked_diagrams_do_not_steal_each_others_circuits(self):
        """上面一台箱的标题离下面的回路更近时，回路仍留在自己这张图里。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        for y in (900, 1800, 2700):
            msp.add_text("—", dxfattribs={"insert": (0, y), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (800, 3000), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (2000, 3000), "height": 100})
        msp.add_text("8KX3 排风机控制箱", dxfattribs={"insert": (0, 4500), "height": 100})
        msp.add_text("WL2", dxfattribs={"insert": (800, 5400), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (2000, 5400), "height": 100})
        raw = extract_cad_table_data(doc)
        owners = {c.circuit_no: c.box for c in raw.circuits}
        self.assertEqual(owners.get("WL1"), "9KX3")
        self.assertEqual(owners.get("WL2"), "8KX3")

    def test_code_keeps_its_own_diagram_when_also_mentioned_nearby(self):
        """箱号在别人的系统图里被点到时，自己那张图上的回路不能因此丢。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("7KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("9KX3", dxfattribs={"insert": (0, 300), "height": 100})
        for index, number in enumerate(("WL2", "WL3", "WL4", "WL5")):
            msp.add_text(number, dxfattribs={"insert": (0, -400 * (index + 1)), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1200, -400), "height": 100})
        msp.add_text("9KX3", dxfattribs={"insert": (200000, 0), "height": 100})
        for index, number in enumerate(("WL1", "WL6", "WL7")):
            msp.add_text(number, dxfattribs={"insert": (200000, -400 * (index + 1)), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (201200, -400), "height": 100})
        raw = extract_cad_table_data(doc)
        owners = {c.circuit_no: c.box for c in raw.circuits}
        self.assertEqual(owners.get("WL1"), "9KX3")
        self.assertEqual(owners.get("WL2"), "7KX3")

    def test_whitespace_between_title_and_circuits_stays_one_diagram(self):
        """箱号和回路号之间空出十几个字高，中间没有另一台箱，仍然是同一张图。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (0, 1300), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1200, 1300), "height": 100})
        msp.add_text("WL2", dxfattribs={"insert": (0, 1700), "height": 100})
        msp.add_text("WL3", dxfattribs={"insert": (0, 2100), "height": 100})
        msp.add_text("8KX3 排风机控制箱", dxfattribs={"insert": (0, 8000), "height": 100})
        msp.add_text("WL4", dxfattribs={"insert": (0, 7600), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1200, 7600), "height": 100})
        raw = extract_cad_table_data(doc)
        owners = {c.circuit_no: c.box for c in raw.circuits}
        self.assertEqual(owners.get("WL1"), "9KX3")
        self.assertEqual(owners.get("WL4"), "8KX3")

    def test_duplicate_circuit_number_keeps_the_row_with_cable(self):
        """同一回路号空标一次、在系统图里再写一次时，留下带着电缆的那一行。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (0, 5000), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1500, -400), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = [c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "WL1"]
        self.assertEqual(len(rows), 1)
        self.assertIn("BV-3x2.5", rows[0].cable)

    def test_bare_circuit_number_is_not_a_branch_row(self):
        """旁边没有电缆、开关或负荷的回路号是引用，不算一条出线。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("WL2", dxfattribs={"insert": (0, -900), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1500, -900), "height": 100})
        raw = extract_cad_table_data(doc)
        numbers = {c.circuit_no for c in raw.circuits if c.box == "9KX3"}
        self.assertNotIn("WL1", numbers)
        self.assertIn("WL2", numbers)

    def test_circuit_row_naming_another_panel_is_a_feeder(self):
        """回路号右边紧挨着另一台箱的编号，是配出；隔开一张图的编号不是。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("WL1", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("WL2", dxfattribs={"insert": (0, -800), "height": 100})
        msp.add_text("8KX3:WL1", dxfattribs={"insert": (1200, -800), "height": 100})
        msp.add_text("WL3", dxfattribs={"insert": (0, -1200), "height": 100})
        msp.add_text("7KX3", dxfattribs={"insert": (2500, -1200), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertNotIn("WL1", rows)
        self.assertEqual(rows["WL2"].load_name, "8KX3:WL1")
        self.assertNotIn("WL3", rows)

    def test_clause_fragment_does_not_hide_the_feeder_on_either_side(self):
        """逗号拆开的桥架说明不是负荷。回路号左侧紧挨着的箱号同样是配出。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("WP2", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("8KX3:WP1", dxfattribs={"insert": (-800, 0), "height": 100})
        msp.add_text("，中间加隔板", dxfattribs={"insert": (600, 0), "height": 100})
        msp.add_text("公共用电防火桥架", dxfattribs={"insert": (400, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "WP2")
        self.assertEqual(row.load_name, "8KX3:WP1")

    def test_panel_beyond_the_outgoing_cable_is_the_feeder(self):
        """出线电缆外侧的箱号是配出。没有电缆时，更远的箱号仍然不是这一行。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("8KX3", dxfattribs={"insert": (2200, -20), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -500), "height": 100})
        msp.add_text("7KX3", dxfattribs={"insert": (2500, -500), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "8KX3")
        self.assertNotIn("N2", rows)

    def test_parallel_column_offset_does_not_hide_the_load_name(self):
        """并排另一列基线略错开时，仍按本列行距取名称。贴在开关上的栏名不盖过名称。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("N3", dxfattribs={"insert": (3000, 220), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-400, 0), "height": 100})
        msp.add_text("断路器", dxfattribs={"insert": (-250, 30), "height": 100})
        msp.add_text("功率（", dxfattribs={"insert": (-900, 20), "height": 100})
        msp.add_text("设备（名称）", dxfattribs={"insert": (-600, 10), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (-1800, 120), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -400), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "走廊照明")
        self.assertNotIn("断路器", rows["N1"].load_name)

    def test_other_column_breaker_does_not_cover_this_rows_load(self):
        """另一列更贴基线的开关不盖过本列。箱号整段出现时不是开关。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (2000, 40), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-400, 80), "height": 100})
        msp.add_text("MCCB-100C/80A/3300", dxfattribs={"insert": (1500, 10), "height": 100})
        msp.add_text("车库动力", dxfattribs={"insert": (500, 20), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (200, 0), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (1800, 40), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.load_name, "车库动力")
        self.assertIn("MCB-C16A", row.breaker)
        self.assertNotIn("MCCB-100C", row.breaker)

    def test_panel_code_is_not_a_breaker(self):
        """整段箱号不是开关，紧挨回路时按配出记下。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-400, 0), "height": 100})
        msp.add_text("B1-AL2", dxfattribs={"insert": (800, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1400, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.load_name, "B1-AL2")
        self.assertIn("MCB-C16A", row.breaker)

    def test_wrapped_load_name_is_joined_across_two_lines(self):
        """名称折成两行时拼回去。落单的「深）」和「宽x高x深」不是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-200, 0), "height": 100})
        msp.add_text("预留接线盒（车库", dxfattribs={"insert": (-700, 80), "height": 100})
        msp.add_text("道闸用电）", dxfattribs={"insert": (-520, -20), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -500), "height": 100})
        msp.add_text("深）", dxfattribs={"insert": (500, -500), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (800, -500), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "预留接线盒（车库道闸用电）")
        self.assertEqual(rows["N2"].load_name, "")

    def test_vertical_title_and_size_column_are_not_the_load(self):
        """竖排图名里的单字、尺寸栏里的「高」不盖过设备名。设备名后面的箱号仍是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 1200), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-300, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        for index, char in enumerate("配电系统"):
            msp.add_text(char, dxfattribs={"insert": (-2000, 820 - index * 400), "height": 100})
        msp.add_text("空调室外机配电箱:WDKTAP2", dxfattribs={"insert": (1200, -180), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -900), "height": 100})
        msp.add_text("x", dxfattribs={"insert": (1400, -900), "height": 100})
        msp.add_text("高", dxfattribs={"insert": (1550, -900), "height": 100})
        msp.add_text("x", dxfattribs={"insert": (1700, -900), "height": 100})
        msp.add_text("深）", dxfattribs={"insert": (1850, -890), "height": 100})
        msp.add_text("空调室外机", dxfattribs={"insert": (900, -1080), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -900), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "空调室外机配电箱:WDKTAP2")
        self.assertEqual(rows["N2"].load_name, "空调室外机")

    def test_bare_number_beyond_the_cable_is_the_power(self):
        """功率栏只有数字时，取电缆外侧更贴这一行的数。另一侧和离行更远的数不算。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -800), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-300, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("70", dxfattribs={"insert": (-1500, 0), "height": 100})
        msp.add_text("1.5", dxfattribs={"insert": (1600, -40), "height": 100})
        msp.add_text("280", dxfattribs={"insert": (2200, -400), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (900, -30), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -800), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.power_kw, "1.5")
        self.assertEqual(row.load_name, "走廊照明")

    def test_load_beside_the_power_beats_a_nearer_route_note(self):
        """功率同一行上的设备名是负荷。更贴回路号的去向说明留在备注。没有功率时去向说明仍可以是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("至消防控制室", dxfattribs={"insert": (-1600, -20), "height": 100})
        msp.add_text("稳压泵控制箱", dxfattribs={"insert": (900, -80), "height": 100})
        msp.add_text("1.5", dxfattribs={"insert": (1500, -80), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -800), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -800), "height": 100})
        msp.add_text("沿消防桥架至火灾监控设备", dxfattribs={"insert": (900, -820), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "稳压泵控制箱")
        self.assertEqual(rows["N1"].power_kw, "1.5")
        self.assertIn("至消防控制室", rows["N1"].note)
        self.assertEqual(rows["N2"].load_name, "沿消防桥架至火灾监控设备")

    def test_quantity_column_number_is_not_the_power(self):
        """和「数量」写在同一行的数字是台数。功率仍取名称栏旁边的数。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("数量", dxfattribs={"insert": (2000, -40), "height": 100})
        msp.add_text("1", dxfattribs={"insert": (2500, -40), "height": 100})
        msp.add_text("照明", dxfattribs={"insert": (900, -120), "height": 100})
        msp.add_text("0.8", dxfattribs={"insert": (1400, -120), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.power_kw, "0.8")
        self.assertEqual(row.load_name, "照明")

    def test_prose_and_bus_laying_do_not_hide_the_equipment(self):
        """换行的控制说明、只带敷设方式的总线，都不是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("当温度升至T1时启动；\n当温度低于T1时停止；", dxfattribs={"insert": (700, -20), "height": 100})
        msp.add_text("消防泵房AT-XFB（主）", dxfattribs={"insert": (1400, -90), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -800), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -800), "height": 100})
        msp.add_text("11", dxfattribs={"insert": (1500, -880), "height": 100})
        msp.add_text("RS485总线-CT/PC25", dxfattribs={"insert": (1450, -885), "height": 100})
        msp.add_text("电气火灾监控探测器", dxfattribs={"insert": (2100, -900), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "消防泵房AT-XFB（主）")
        self.assertIn("当温度升至T1时", rows["N1"].note)
        self.assertEqual(rows["N2"].load_name, "电气火灾监控探测器")
        self.assertIn("RS485", rows["N2"].note)
        self.assertEqual(rows["N2"].power_kw, "11")

    def test_rating_column_and_same_as_rest_note_are_not_the_load(self):
        """防护等级是表头。和「余同」写在一起的句子是安装注记。带单位的栏名不是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-300, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("照明", dxfattribs={"insert": (800, -80), "height": 100})
        msp.add_text("防护等级", dxfattribs={"insert": (1600, -20), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -700), "height": 100})
        msp.add_text("室外防水型，防护等级", dxfattribs={"insert": (800, -700), "height": 100})
        msp.add_text("余同", dxfattribs={"insert": (1500, -690), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -700), "height": 100})
        msp.add_text("N3", dxfattribs={"insert": (0, -1400), "height": 100})
        msp.add_text("无功补偿量（kVar）", dxfattribs={"insert": (700, -1410), "height": 100})
        msp.add_text("电容器柜", dxfattribs={"insert": (1400, -1520), "height": 100})
        msp.add_text("BV-3x6 SC32", dxfattribs={"insert": (400, -1400), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "照明")
        self.assertEqual(rows["N2"].load_name, "")
        self.assertEqual(rows["N3"].load_name, "电容器柜")

    def test_reactive_power_quantity_is_not_a_panel(self):
        """150kVar、1000kVA 是电量，不是箱号，也不能当成配出负荷。"""
        self.assertIsNone(extract_panel_code("150kVar"))
        self.assertIsNone(extract_panel_code("1000kVA"))
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("WDZ-YJY-4x50+25-CT", dxfattribs={"insert": (-400, 0), "height": 100})
        msp.add_text("150kVar", dxfattribs={"insert": (-900, 0), "height": 100})
        msp.add_text("电容器柜", dxfattribs={"insert": (800, -40), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.load_name, "电容器柜")
        self.assertNotIn("kVar", row.load_name)

    def test_wire_note_and_marking_remark_do_not_hide_the_equipment(self):
        """接线种类和箱壳标志不是负荷。防火阀、卷帘箱这类设备名要留下。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("通讯线+电源线：WDZN-RYJS-2×1.5", dxfattribs={"insert": (1500, -20), "height": 100})
        msp.add_text("电动挡烟垂壁控制箱", dxfattribs={"insert": (900, -80), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -700), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (400, -700), "height": 100})
        msp.add_text("手动控制线", dxfattribs={"insert": (-1800, -720), "height": 100})
        msp.add_text("防火卷帘箱", dxfattribs={"insert": (800, -740), "height": 100})
        msp.add_text("N3", dxfattribs={"insert": (0, -1400), "height": 100})
        msp.add_text("BV-3x6 SC32", dxfattribs={"insert": (400, -1400), "height": 100})
        msp.add_text("馈电柜（带明显消防标志）", dxfattribs={"insert": (900, -1400), "height": 100})
        msp.add_text("箱壳设明显消防标志", dxfattribs={"insert": (1600, -1410), "height": 100})
        msp.add_text("N4", dxfattribs={"insert": (0, -2100), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, -2100), "height": 100})
        msp.add_text("防水型", dxfattribs={"insert": (900, -2100), "height": 100})
        msp.add_text("灯具采用防水型", dxfattribs={"insert": (1400, -2120), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "电动挡烟垂壁控制箱")
        self.assertIn("通讯线+电源线", rows["N1"].note)
        self.assertEqual(rows["N2"].load_name, "防火卷帘箱")
        self.assertEqual(rows["N3"].load_name, "馈电柜")
        self.assertNotIn("消防标志", rows["N3"].load_name)
        self.assertEqual(rows["N4"].load_name, "")

    def test_terminal_tags_are_not_panels_just_because_the_text_is_tall(self):
        """字高更大的端子号、点位号旁边没有真回路，不能当成配电箱。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        for index in range(6):
            msp.add_text("说明", dxfattribs={"insert": (0, 2000 + index * 200), "height": 100})
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1200, -400), "height": 100})
        msp.add_text("ET1C10P03", dxfattribs={"insert": (80000, 0), "height": 400})
        msp.add_text("S-1c-C1", dxfattribs={"insert": (80000, -400), "height": 100})
        msp.add_text("925P01", dxfattribs={"insert": (80000, -800), "height": 100})
        raw = extract_cad_table_data(doc)
        codes = {box.code for box in raw.boxes}
        self.assertIn("9KX3", codes)
        self.assertNotIn("ET1C10P03", codes)
        self.assertTrue(any(c.box == "9KX3" and c.circuit_no == "N1" and "BV-3x2.5" in c.cable for c in raw.circuits))

    def test_nearest_baseline_keeps_the_row_phase_and_power(self):
        """离回路号更近的相序盖过行尾电压；半行距以内的功率仍属于这一行。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("L1,N,PE", dxfattribs={"insert": (-800, -10), "height": 100})
        msp.add_text("380/220V", dxfattribs={"insert": (-2000, -200), "height": 100})
        msp.add_text("SPD厂家配套", dxfattribs={"insert": (-3000, -20), "height": 100})
        msp.add_text("SPD厂家配套", dxfattribs={"insert": (-3000, -20), "height": 100})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (-1200, -40), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (1800, -30), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (1200, -10), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -500), "height": 100})
        msp.add_text("L2,N,PE", dxfattribs={"insert": (-800, -510), "height": 100})
        msp.add_text("0.6kW", dxfattribs={"insert": (2000, -260), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].phase, "L1,N,PE")
        self.assertEqual(rows["N1"].breaker, "MCB-C16A/1P")
        self.assertEqual(rows["N1"].load_name, "走廊照明")
        self.assertEqual(rows["N1"].power_kw, "")
        self.assertEqual(rows["N2"].phase, "L2,N,PE")
        self.assertIn("0.6", rows["N2"].power_kw)

    def test_upstream_source_stays_when_the_load_name_is_closer(self):
        """同一行已经有负荷名时，「由某箱引来」仍记在备注里。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, -400), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (1500, -400), "height": 100})
        msp.add_text("由8KX3引来", dxfattribs={"insert": (-2500, -400), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (3000, -400), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertEqual(row.load_name, "走廊照明")
        self.assertIn("由8KX3引来", row.note)

    def test_side_by_side_circuits_keep_their_own_cable(self):
        """并排两列时，电缆归离它更近的那一列，不因纵坐标差几个单位被另一列抢走。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (1400, 30), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 28), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (1100, 32), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertIn("3x2.5", rows["N1"].cable)
        self.assertIn("3x4", rows["N2"].cable)

    def test_schedule_header_does_not_become_the_load_name(self):
        """小室高度是表头。同一行更远的设备名才是负荷，带「箱」也可以。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("小室高度", dxfattribs={"insert": (400, -10), "height": 100})
        msp.add_text("屋顶风机双切箱", dxfattribs={"insert": (1600, -80), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (2800, -10), "height": 100})
        msp.add_text("N2", dxfattribs={"insert": (0, -500), "height": 100})
        msp.add_text("米安装", dxfattribs={"insert": (400, -500), "height": 100})
        raw = extract_cad_table_data(doc)
        rows = {c.circuit_no: c for c in raw.circuits if c.box == "9KX3"}
        self.assertEqual(rows["N1"].load_name, "屋顶风机双切箱")
        self.assertNotIn("N2", rows)

    def test_note_between_breaker_and_circuit_is_not_the_load(self):
        """夹在开关和回路号之间的附注留给备注，名称栏里的设备名仍是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("WP1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCCB-63/50A/3200", dxfattribs={"insert": (-2000, 0), "height": 100})
        msp.add_text("带隔离功能", dxfattribs={"insert": (-1200, 40), "height": 100})
        msp.add_text("电梯控制箱", dxfattribs={"insert": (2500, -40), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (800, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "WP1")
        self.assertEqual(row.load_name, "电梯控制箱")
        self.assertIn("带隔离功能", row.note)
        self.assertIn("MCCB-63", row.breaker)

    def test_incoming_breaker_remark_stays_off_the_load_column(self):
        """贴在进线开关上的附注不是负荷。出线电缆另一侧的设备名仍是负荷。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("WP1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("MCCB-160C/100A/4340", dxfattribs={"insert": (-800, 0), "height": 100})
        msp.add_text("带隔离功能", dxfattribs={"insert": (-650, 40), "height": 100})
        msp.add_text("MCB-C40A/3P", dxfattribs={"insert": (-400, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (300, 0), "height": 100})
        msp.add_text("空调室外机", dxfattribs={"insert": (900, -40), "height": 100})
        raw = extract_cad_table_data(doc)
        row = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "WP1")
        self.assertEqual(row.load_name, "空调室外机")
        self.assertIn("带隔离功能", row.note)
        self.assertIn("MCB-C40A", row.breaker)

    def test_incomer_does_not_borrow_the_branch_cable_or_phase(self):
        """出线行上的「进线详见」不能把该行的电缆和相序当成进线。进线只取总开关那一行。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 1600), "height": 100})
        msp.add_text("MCCB-160C/100A/3300", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("L123", dxfattribs={"insert": (500, 800), "height": 100})
        msp.add_text("WDZ-YJY-4x95+50-CT", dxfattribs={"insert": (-700, 800), "height": 100})
        msp.add_text("+WDZN-BYJ-2x2.5-CT", dxfattribs={"insert": (200, 820), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("L1", dxfattribs={"insert": (-300, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("进线详上级配电箱系统图", dxfattribs={"insert": (900, 0), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (1600, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        inc = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "进线")
        branch = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "N1")
        self.assertIn("WDZ-YJY", inc.cable)
        self.assertFalse(inc.cable.lstrip().startswith("+"))
        self.assertEqual(inc.phase, "L123")
        self.assertNotIn("BV-3x2.5", inc.cable)
        self.assertIn("BV-3x2.5", branch.cable)
        self.assertEqual(branch.phase, "L1")
        msp.add_text("8KX3 排风机控制箱", dxfattribs={"insert": (30000, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (30000, 0), "height": 100})
        msp.add_text("L1", dxfattribs={"insert": (29700, 0), "height": 100})
        msp.add_text("BV-3x4 SC25", dxfattribs={"insert": (30400, 0), "height": 100})
        msp.add_text("由01LBZ3引来", dxfattribs={"insert": (31200, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        other = next(c for c in raw.circuits if c.box == "8KX3" and c.circuit_no == "进线")
        self.assertEqual(other.phase, "")
        self.assertEqual(other.cable, "")
        self.assertIn("由01LBZ3引来", other.note)

    def test_signal_cable_is_not_the_power_incomer(self):
        """贴着总开关的双绞信号线不是电力进线。旁边有电力电缆时用电力电缆，只有信号线时进线电缆留空。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 1600), "height": 100})
        msp.add_text("ATSE-63", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("WDZN-RYJS-2x1.5-JDG20", dxfattribs={"insert": (200, 800), "height": 100})
        msp.add_text("WDZ-YJY-5x16-CT", dxfattribs={"insert": (900, 780), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (1200, 0), "height": 100})
        msp.add_text("8KX3 排风机控制箱", dxfattribs={"insert": (30000, 1600), "height": 100})
        msp.add_text("ATSE-63", dxfattribs={"insert": (30000, 800), "height": 100})
        msp.add_text("WDZN-RYJS-2x2.5-JDG20", dxfattribs={"insert": (30200, 800), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (30000, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (30400, 0), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (31200, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        powered = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "进线")
        signal_only = next(c for c in raw.circuits if c.box == "8KX3" and c.circuit_no == "进线")
        self.assertIn("WDZ-YJY", powered.cable)
        self.assertNotIn("RYJS", powered.cable)
        self.assertEqual(signal_only.cable, "")

    def test_wire_kind_label_is_not_the_power_cable(self):
        """「电源线:型号」是接线注记。进线旁边另有电力电缆时用电力电缆，注记留在备注。"""
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("9KX3 排风机控制箱", dxfattribs={"insert": (0, 1600), "height": 100})
        msp.add_text("ATSE-63", dxfattribs={"insert": (0, 800), "height": 100})
        msp.add_text("电源线:WDZN-BYJ-2x2.5-SC20-WC,CC", dxfattribs={"insert": (200, 800), "height": 100})
        msp.add_text("WDZ-YJY-5x10-CT", dxfattribs={"insert": (900, 780), "height": 100})
        msp.add_text("N1", dxfattribs={"insert": (0, 0), "height": 100})
        msp.add_text("BV-3x2.5 SC20", dxfattribs={"insert": (400, 0), "height": 100})
        msp.add_text("走廊照明", dxfattribs={"insert": (1200, 0), "height": 100})
        raw = extract_cad_table_data(doc)
        inc = next(c for c in raw.circuits if c.box == "9KX3" and c.circuit_no == "进线")
        self.assertIn("WDZ-YJY", inc.cable)
        self.assertNotIn("电源线", inc.cable)
        self.assertIn("电源线", inc.note)

    def test_preview_bbox_follows_cad_text(self):
        boxes = [Box(code="1AL1", name="照明配电箱")]
        circuits = [Circuit(box="1AL1", circuit_no="WL1")]
        texts = [
            {"text": "图框", "x": 0, "y": 0, "page": 3},
            {"text": "图框", "x": 1000, "y": 1000, "page": 3},
            {"text": "1AL1", "x": 100, "y": 800, "page": 3},
            {"text": "WL1", "x": 400, "y": 500, "page": 3},
        ]
        assign_preview_bboxes(boxes, circuits, texts, {"1AL1": 3})
        self.assertEqual(boxes[0].bbox.page, 3)
        self.assertLess(boxes[0].bbox.w, 0.5)
        self.assertEqual(circuits[0].bbox.page, 3)
        self.assertNotEqual((circuits[0].bbox.x, circuits[0].bbox.y), (boxes[0].bbox.x, boxes[0].bbox.y))


if __name__ == "__main__":
    unittest.main()
