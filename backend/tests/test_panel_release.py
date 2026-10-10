# -*- coding: utf-8 -*-
"""读取盘点、插入身份和配电箱放行规则。"""
import os
import shutil
import tempfile
import unittest

import ezdxf

from extractor.assemble import _parse_devices, assemble
from extractor.cad import _explode_texts, extract_cad_entities, process_cad_file
from extractor.cad_extractor import extract_cad_table_data
from extractor.cad_ledger import (
    INSUFFICIENT_RELEASE_REASON,
    apply_read_gate,
    classify_read_status,
    conversion_accepted,
    source_key,
)
from extractor.schema import BomRelease
from extractor.schema import Box, Circuit, RawExtraction, Requirement


def _raw(boxes, circuits, requirements=None, devices=None) -> RawExtraction:
    return RawExtraction(
        boxes=boxes,
        circuits=circuits,
        extra_devices=devices or [],
        requirements=requirements or [],
        uncertainties=[],
    )


class ReadStatusTests(unittest.TestCase):
    def test_exit_code_alone_does_not_mean_complete(self):
        self.assertTrue(conversion_accepted(0, True))
        self.assertFalse(conversion_accepted(0, False))
        status, reasons, measurable = classify_read_status({
            "converter": "libredwg",
            "accepted": True,
            "warning_lines": 12,
            "error_lines": 0,
            "log_missing": False,
            "entity_total": 20,
            "proxy_count": 0,
            "xref_count": 0,
            "units_known": True,
            "recovered": False,
        })
        self.assertEqual(status, "degraded")
        self.assertFalse(measurable)
        self.assertTrue(any("退出码" in item for item in reasons))

    def test_unknown_units_block_measurement_and_coverage_stays_empty(self):
        status, reasons, measurable = classify_read_status({
            "converter": "native_dxf",
            "accepted": True,
            "warning_lines": 0,
            "error_lines": 0,
            "log_missing": False,
            "entity_total": 4,
            "proxy_count": 0,
            "xref_count": 0,
            "units_known": False,
            "recovered": False,
        })
        self.assertEqual(status, "degraded")
        self.assertFalse(measurable)
        self.assertTrue(any("单位" in item for item in reasons))

    def test_svg_fallback_is_insufficient(self):
        status, _, measurable = classify_read_status({
            "converter": "svg_fallback",
            "accepted": False,
            "entity_total": 0,
            "units_known": False,
        })
        self.assertEqual(status, "insufficient")
        self.assertFalse(measurable)

    def test_insufficient_read_blocks_project_total(self):
        release = BomRelease(project_total_released=True, blocked_boxes=[], reasons=[])
        apply_read_gate(release, {"status": "insufficient"})
        self.assertFalse(release.project_total_released)
        self.assertIn(INSUFFICIENT_RELEASE_REASON, release.reasons)
        kept = BomRelease(project_total_released=True, reasons=[])
        apply_read_gate(kept, {"status": "degraded", "reasons": ["单位未设置"]})
        self.assertTrue(kept.project_total_released)


class IdentityAndInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_same_block_inserted_twice_keeps_two_identities(self):
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 4
        block = doc.blocks.new("QF")
        block.add_text("MCB-63", dxfattribs={"insert": (0, 0), "height": 2.5})
        first = doc.modelspace().add_blockref("QF", (0, 0))
        second = doc.modelspace().add_blockref("QF", (500, 0))
        cache = {}
        left = _explode_texts(first, cache)
        right = _explode_texts(second, cache)
        self.assertEqual(len(left), 1)
        self.assertEqual(len(right), 1)
        self.assertNotEqual(left[0]["source"]["insert_path"], right[0]["source"]["insert_path"])
        self.assertEqual(len({source_key(left[0]["source"]), source_key(right[0]["source"])}), 2)

    def test_dxf_read_report_has_no_coverage_rate(self):
        dxf_path = os.path.join(self.temp_dir, "panel.dxf")
        pdf_path = os.path.join(self.temp_dir, "panel.pdf")
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 4
        doc.modelspace().add_text("1AL1 照明配电箱", dxfattribs={"height": 25, "insert": (50, 500)})
        doc.saveas(dxf_path)

        entities = extract_cad_entities(dxf_path)
        self.assertTrue(entities[0]["source"]["handle"])
        self.assertEqual(entities[0]["source"]["owner"], "modelspace")

        process_cad_file(dxf_path, pdf_path)
        report_path = pdf_path + ".read.json"
        self.assertTrue(os.path.exists(report_path))
        import json
        with open(report_path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertIsNone(report["coverage_rate"])
        self.assertEqual(report["status"], "supported")
        self.assertTrue(report["measurement_allowed"])
        self.assertEqual(report["units"], "mm")
        self.assertTrue(report["file_sha256"])


class NativeEvidenceTests(unittest.TestCase):
    def test_claim_points_at_the_cad_entity_that_wrote_it(self):
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        header = msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("WL1", dxfattribs={"insert": (0, -800), "height": 200})
        breaker = msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (1500, -800), "height": 200})
        msp.add_text("走廊照明", dxfattribs={"insert": (3000, -800), "height": 200})

        raw = extract_cad_table_data(doc)
        box = next(item for item in raw.boxes if item.code == "9KX3")
        code_evidence = raw.evidence_store[box.claims["box.code"].value_evidence_ids[0]]
        self.assertEqual(code_evidence.origin, "cad_native")
        self.assertEqual(code_evidence.handle, str(header.dxf.handle))
        self.assertEqual(code_evidence.owner, "modelspace")

        circuit = next(item for item in raw.circuits if item.box == "9KX3" and item.circuit_no == "WL1")
        breaker_evidence = raw.evidence_store[circuit.claims["circuit.breaker"].value_evidence_ids[0]]
        self.assertEqual(breaker_evidence.handle, str(breaker.dxf.handle))
        self.assertNotEqual(breaker_evidence.handle, code_evidence.handle)

        result = assemble(raw)
        kept = result.evidence_store[circuit.claims["circuit.breaker"].value_evidence_ids[0]]
        self.assertEqual(kept.origin, "cad_native")
        self.assertEqual(kept.handle, str(breaker.dxf.handle))

    def test_block_attribute_keeps_its_insert_path(self):
        doc = ezdxf.new("R2010")
        block = doc.blocks.new("BOXLAB")
        block.add_attdef("TITLE", dxfattribs={"insert": (0, 0), "height": 200})
        msp = doc.modelspace()
        ref = msp.add_blockref("BOXLAB", (0, 0))
        attrib = ref.add_attrib(
            "TITLE", "9KX3 照明配电箱",
            dxfattribs={"insert": (0, 0), "height": 200},
        )
        msp.add_text("共2台", dxfattribs={"insert": (800, 0), "height": 80})
        msp.add_text("WL1", dxfattribs={"insert": (0, -800), "height": 200})
        msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (1500, -800), "height": 200})
        msp.add_text("走廊照明", dxfattribs={"insert": (3000, -800), "height": 200})

        raw = extract_cad_table_data(doc)
        box = next(item for item in raw.boxes if item.code == "9KX3")
        self.assertTrue(box.quantity_confirmed)
        self.assertEqual(box.quantity, 2)
        evidence = raw.evidence_store[box.claims["box.code"].value_evidence_ids[0]]
        self.assertEqual(evidence.handle, str(attrib.dxf.handle))
        self.assertIn("BOXLAB", evidence.insert_path)
        self.assertIn(str(ref.dxf.handle), evidence.insert_path)


class BomReleaseTests(unittest.TestCase):
    def test_three_poles_are_still_one_device(self):
        parsed = _parse_devices("MCB-63/C16A/3P")
        self.assertEqual(parsed, [("MCB-63/C16A/3P", 1)])

    def test_unconfirmed_box_stays_a_single_candidate(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=False)],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P", load_name="走廊照明")],
        )
        result = assemble(raw)
        self.assertFalse(result.bom_release.project_total_released)
        self.assertEqual(result.bom_release.blocked_boxes, ["AL01"])
        self.assertIn("项目总量未放行", result.title)
        breakers = [item for item in result.components if "断路" in item.name or "MCB" in item.spec]
        self.assertTrue(breakers)
        self.assertEqual(breakers[0].quantity, 1)
        self.assertTrue(any("项目总量未放行" in item.text for item in result.uncertainties))

    def test_confirmed_quantity_still_multiplies(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=2, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P", load_name="走廊照明")],
        )
        result = assemble(raw)
        self.assertTrue(result.bom_release.project_total_released)
        breakers = [item for item in result.components if item.spec == "MCB-63/C16A/1P"]
        self.assertEqual(breakers[0].quantity, 2)

    def test_reserved_spare_is_not_purchased(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL9", load_name="备用")],
        )
        result = assemble(raw)
        self.assertFalse(any(item.name != "配电箱体" for item in result.components))
        self.assertTrue(any("不计入采购数量" in item.text for item in result.uncertainties))

    def test_emergency_lighting_is_a_real_load(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL3", breaker="MCB-63/C32A/1P", load_name="备用照明")],
        )
        result = assemble(raw)
        breakers = [item for item in result.components if item.spec == "MCB-63/C32A/1P"]
        self.assertEqual(breakers[0].quantity, 1)
        self.assertNotIn("已安装备用", breakers[0].note)
        self.assertFalse(any("不计入采购数量" in item.text for item in result.uncertainties))

    def test_installed_spare_counts_one_device(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL9", breaker="MCB-63/C16A/3P", load_name="备用")],
        )
        result = assemble(raw)
        breakers = [item for item in result.components if item.spec == "MCB-63/C16A/3P"]
        self.assertEqual(len(breakers), 1)
        self.assertEqual(breakers[0].quantity, 1)
        self.assertIn("已安装备用", breakers[0].note)

    def test_busbar_without_assembly_size_is_not_estimated(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True, note="柜内配置铜排")],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P", load_name="照明")],
            requirements=[Requirement(item="说明", content="母排现场加工")],
        )
        result = assemble(raw)
        self.assertFalse(any("铜排" in item.name or "母排" in item.name for item in result.components))
        self.assertTrue(any("不补估算数量" in item.text for item in result.uncertainties))

    def test_same_code_on_two_locations_is_not_one_confirmed_box(self):
        raw = _raw(
            [
                Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True, location="一层电井"),
                Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True, location="二层电井"),
            ],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P", load_name="照明")],
        )
        result = assemble(raw)
        self.assertFalse(result.bom_release.project_total_released)
        self.assertEqual(len(result.boxes), 1)
        self.assertTrue(any("不同安装位置" in item.text for item in result.uncertainties))

    def test_decoration_note_does_not_drop_the_breaker(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P",
                     load_name="走廊照明", note="未装饰")],
        )
        result = assemble(raw)
        breakers = [item for item in result.components if item.spec == "MCB-63/C16A/1P"]
        self.assertEqual(breakers[0].quantity, 1)
        self.assertFalse(any("不计入采购数量" in item.text for item in result.uncertainties))

    def test_spare_switch_in_a_note_does_not_retitle_a_real_load(self):
        raw = _raw(
            [Box(code="AL01", name="照明配电箱", quantity=1, quantity_confirmed=True)],
            [Circuit(box="AL01", circuit_no="WL1", breaker="MCB-63/C16A/1P",
                     load_name="走廊照明", note="见备用开关说明")],
        )
        result = assemble(raw)
        breakers = [item for item in result.components if item.spec == "MCB-63/C16A/1P"]
        self.assertEqual(breakers[0].quantity, 1)
        self.assertNotIn("已安装备用", breakers[0].note)


if __name__ == "__main__":
    unittest.main()
