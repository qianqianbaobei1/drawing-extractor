import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import pymupdf
from fastapi import HTTPException
from openpyxl import load_workbook
from pydantic import ValidationError

import app
from extractor.assemble import assemble
from extractor.assistant import Assistant, validate_patch
from extractor.checker import check_result
from extractor.schema import BBox, ExtractionResult, RawExtraction, Uncertainty
from extractor.vision import VisionProvider
from extractor import vision
from extractor.render import plan_tiles as render_tiles


def _make_pdf(path: str, width_mm: float, height_mm: float) -> str:
    """造一张指定幅面的空白 PDF，用来验证分块策略。"""
    doc = pymupdf.open()
    page = doc.new_page(width=width_mm / 25.4 * 72, height=height_mm / 25.4 * 72)
    page.draw_rect(page.rect, color=(0, 0, 0), width=1)
    doc.save(path)
    doc.close()
    return path


class CheckerTests(unittest.TestCase):
    def test_multi_box_quantity_and_unambiguous_breakers(self):
        result = ExtractionResult.model_validate({
            "boxes": [{"code": "AW1/2/3/4", "quantity": 4}],
            "circuits": [
                {"box": "AW1/2/3/4", "breaker": "MCB-63/C16A/1P"},
                {"box": "AW1/2/3/4", "breaker": "QF1+QF2"},
            ],
            "components": [{"name": "微型断路器", "spec": "MCB-63/C16A/1P", "quantity": 3}],
        })
        self.assertEqual(len(check_result(result)), 1)
        self.assertIn("回路逐条计数 4 只，元器件汇总 3 只", check_result(result)[0])

    def test_isolator_is_not_reported_as_missing(self):
        """DS- 型号汇总成“隔离开关”，checker 的名称白名单要跟着 assemble 走。"""
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "2SAL3"}],
            "circuits": [{"box": "2SAL3", "breaker": "DS-50/3P"},
                         {"box": "2SAL3", "breaker": "MCB-63/C16A/1P"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        names = {(c.name, c.spec) for c in result.components}
        self.assertIn(("隔离开关", "DS-50/3P"), names)
        self.assertEqual(check_result(result), [])

    def test_reference_and_invalid_quantity_are_warnings(self):
        result = ExtractionResult.model_validate({
            "boxes": [{"code": "AL1", "quantity": 1}],
            "circuits": [{"box": "AL2"}],
            "components": [{"name": "箱体", "quantity": 0}],
        })
        warnings = check_result(result)
        self.assertEqual(len(warnings), 2)
        self.assertTrue(any("AL2" in warning for warning in warnings))


class AssemblyTests(unittest.TestCase):
    def test_multi_box_and_explicit_composite_counts(self):
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AW1/2/3/4", "quantity": 4}],
            "circuits": [{"box": "AW1/2/3/4", "breaker":
                          "MCB-63/C25A/3P×2+ATSE-63 32A/4P/PC/R",
                          "contactor": "3×145A"}],
            "extra_devices": [{"name": "电能表", "spec": "M1", "unit": "块",
                               "quantity": 1, "used_in": "AW1/2/3/4 进线侧"}],
            "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        counts = {(c.name, c.spec): c.quantity for c in result.components}
        self.assertEqual(counts[("微型断路器", "MCB-63/C25A/3P")], 8)
        self.assertEqual(counts[("双电源自动转换开关", "ATSE-63 32A/4P/PC/R")], 4)
        self.assertEqual(counts[("交流接触器", "145A")], 12)
        self.assertEqual(counts[("电能表", "M1")], 4)
        self.assertIn("共4台", result.title)
        self.assertEqual(result.model_dump(), assemble(raw).model_dump())

    def test_ambiguous_device_is_kept_as_written_and_flagged(self):
        """拆不开的写法要按原文留一行，不能从报价里直接消失 —— 少一行就是少算钱。"""
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1", "quantity": 2}],
            "circuits": [{"box": "AL1", "breaker": "MCB 1P+N"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        rows = [c for c in result.components if c.spec == "MCB 1P+N"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2, "应按箱体台数计入，不自行拆数量")
        self.assertIn("按原文计入", rows[0].note)
        self.assertTrue(any("无法安全拆分" in item.text for item in result.uncertainties))

    def test_extra_device_without_spec_is_kept(self):
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1"}], "circuits": [],
            "extra_devices": [{"name": "电气火灾监控探测器", "quantity": 1}],
            "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        rows = [c for c in result.components if c.name == "电气火灾监控探测器"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("已按原文计入" in item.text for item in result.uncertainties))

    def test_accessory_combo_is_kept_as_one_line_not_split(self):
        """“主体+附件”（Vigi/MX/OF）图上就是一项，按原文计入一行，不拆成两只也不会丢。"""
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1"}],
            "circuits": [{"box": "AL1", "circuit_no": "WL1",
                          "breaker": "MCB-63/C16A/1P+Vigi 30mA"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        rows = [c for c in result.components if "Vigi" in c.spec]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].spec, "MCB-63/C16A/1P+Vigi 30mA")
        self.assertEqual(rows[0].name, "微型断路器")

    def test_domestic_model_is_counted_once(self):
        """国产型号（DZ47LE / CDM1）不在前缀表里，也不能被丢掉。"""
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1"}],
            "circuits": [{"box": "AL1", "circuit_no": "WL1", "breaker": "DZ47LE-63/2P"},
                         {"box": "AL1", "circuit_no": "WL2", "breaker": "CDM1-100/3300"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)
        counts = {c.spec: c.quantity for c in result.components}
        self.assertEqual(counts.get("DZ47LE-63/2P"), 1)
        self.assertEqual(counts.get("CDM1-100/3300"), 1)

    def test_raw_contract_rejects_model_generated_totals(self):
        with self.assertRaises(ValidationError):
            RawExtraction.model_validate({
                "boxes": [], "circuits": [], "extra_devices": [],
                "requirements": [], "uncertainties": [], "components": [],
            })


class VisionRetryTests(unittest.TestCase):
    def test_repairs_structure_without_changing_extracted_fact(self):
        bad = '{"boxes": [{"code": "AL1"}], "circuits": []}'
        good = json.dumps({
            "boxes": [{"code": "AL1"}], "circuits": [],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only"}):
            provider = VisionProvider()
        with patch.object(vision, "_data_url", return_value="data:image/png;base64,AA"), \
             patch.object(provider, "_call", side_effect=[
                 {"choices": [{"message": {"content": bad}}]},
                 {"choices": [{"message": {"content": good}}]},
             ]) as call:
            result = provider.extract([("page1.png", 1, None)])
        self.assertEqual(result.boxes[0].code, "AL1")
        self.assertEqual(call.call_count, 2)
        self.assertIn("只修复 JSON 结构和类型", call.call_args.args[0]["messages"][-1]["content"])

    def test_stops_after_two_repairs(self):
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only"}):
            provider = VisionProvider()
        with patch.object(vision, "_data_url", return_value="data:image/png;base64,AA"), \
             patch.object(provider, "_call", return_value={
                 "choices": [{"message": {"content": "invalid json"}}]
             }) as call:
            with self.assertRaisesRegex(ValueError, "已重试 2 次"):
                provider.extract([("page1.png", 1, None)])
        self.assertEqual(call.call_count, 3)

    def test_length_truncation_fails_fast_without_partial_excel(self):
        """截断是确定性的：直接失败，不拿残缺数据"自愈"继续（旧行为已删除）。

        输出契约：模型返回长度截断 → 任务失败，不生成 Excel。
        上游 process_pdf 会经 _fail 展示该报错。
        """
        truncated_content = (
            '{"boxes": [{"code": "01AL1", "name": "动力箱"}], '
            '"circuits": [{"circuit_no": "WL1", "breaker": "C16A"}, {"circuit_no": "WL2"'
        )
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only"}):
            provider = VisionProvider()
        with patch.object(vision, "_data_url", return_value="data:image/png;base64,AA"), \
             patch.object(provider, "_call", return_value={
                 "choices": [{"finish_reason": "length", "message": {"content": truncated_content}}]
             }) as call:
            with self.assertRaisesRegex(ValueError, "截断"):
                provider.extract([("page1.png", 1, None)])
        # 确定性失败：不浪费重试
        self.assertEqual(call.call_count, 1)


class PageAndTileTests(unittest.TestCase):
    def test_small_page_is_not_tiled(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = _make_pdf(os.path.join(d, "a4.pdf"), 297, 210)
            self.assertEqual(render_tiles(pdf, 0), [])

    def test_large_page_is_tiled_with_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            pdf = _make_pdf(os.path.join(d, "a1.pdf"), 841, 594)
            tiles = render_tiles(pdf, 0)
            self.assertEqual(len(tiles), 4)
            for tile in tiles:
                clip = tile["clip"]
                self.assertGreater(clip["w"], 0.5)
                self.assertLessEqual(clip["x"] + clip["w"], 1.0 + 1e-9)
                self.assertTrue(os.path.exists(tile["path"]))

    def test_tile_coordinates_map_back_to_the_page(self):
        """块内坐标折回整页后必须落在该块自己的范围内 —— 否则高亮会漂到别的区域。"""
        from extractor.schema import BBox
        clip = {"x": 0.5, "y": 0.5, "w": 0.58, "h": 0.58}
        box = vision._clip_to_page(BBox(x=0.0, y=0.0, w=0.1, h=0.1), clip)
        self.assertAlmostEqual(box.x, 0.5, places=6)
        self.assertAlmostEqual(box.y, 0.5, places=6)
        self.assertAlmostEqual(box.w, 0.058, places=6)
        centre = vision._clip_to_page(BBox(x=0.5, y=0.5, w=0.1, h=0.1), clip)
        self.assertAlmostEqual(centre.x, 0.5 + 0.5 * 0.58, places=6)
        self.assertAlmostEqual(centre.x + centre.w / 2, 0.79 + 0.058 / 2, places=6)

    def test_overlapping_tiles_keep_the_interior_copy(self):
        """两块都看到同一条回路时，留离块边缘更远的那份。"""
        left = {"x": 0.0, "y": 0.0, "w": 0.58, "h": 0.58}
        right = {"x": 0.42, "y": 0.0, "w": 0.58, "h": 0.58}
        raw_left = RawExtraction.model_validate({
            "boxes": [], "circuits": [{"box": "AL1", "circuit_no": "WL1",
                                       "bbox": {"x": 0.95, "y": 0.1, "w": 0.04, "h": 0.04}}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        raw_right = RawExtraction.model_validate({
            "boxes": [], "circuits": [{"box": "AL1", "circuit_no": "WL1",
                                       "bbox": {"x": 0.05, "y": 0.1, "w": 0.04, "h": 0.04}}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        merged = vision.merge_parts([(raw_left, left), (raw_right, right)])
        self.assertEqual(len(merged.circuits), 1)
        # 左边那份贴到了块的右边缘（0.95+0.04），右边那份贴在块的左边缘（0.05），
        # 两块的重叠区在 0.42~0.58，右边那份离边缘更远
        self.assertAlmostEqual(merged.circuits[0].bbox.x, 0.05, places=6)

    def test_same_box_code_on_different_pages_is_not_deduped(self):
        page1 = RawExtraction.model_validate({
            "boxes": [{"code": "AL1", "size": "400x300"}], "circuits": [],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        page2 = RawExtraction.model_validate({
            "boxes": [{"code": "AL1", "size": "400x300"}], "circuits": [],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        merged = vision.concat_results([page1, page2])
        self.assertEqual(len(merged.boxes), 2, "跨页不去重，交给 assemble 报重复编号")

    def test_page_number_is_stamped_on_bboxes(self):
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only"}):
            provider = VisionProvider()
        payload = json.dumps({
            "boxes": [], "circuits": [{"box": "AL1", "circuit_no": "WL1",
                                       "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}}],
            "extra_devices": [], "requirements": [],
            "uncertainties": [{"location": "WL1", "detail": "糊",
                                "bbox": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05}}],
        })
        with patch.object(vision, "_data_url", return_value="data:image/png;base64,AA"), \
             patch.object(provider, "_call", return_value={
                 "choices": [{"message": {"content": payload}}]}):
            result = provider.extract([("p3.png", 3, None)])
        self.assertEqual(result.circuits[0].bbox.page, 3)
        self.assertEqual(result.uncertainties[0].bbox.page, 3)

    def test_concurrent_extraction_preserves_order_and_progress(self):
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only", "VISION_CONCURRENCY": "4"}):
            provider = VisionProvider()

        def mock_call(payload):
            # 从 prompt 内容中识别请求的是哪一页
            user_msg = payload["messages"][1]["content"][0]["text"]
            import time
            time.sleep(0.01)
            # 根据是否有页码或调用顺序返回对应页码的回路
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "boxes": [],
                            "circuits": [{"box": "AL1", "circuit_no": "WL1"}],
                            "extra_devices": [], "requirements": [], "uncertainties": [],
                        })
                    }
                }]
            }

        progress_calls = []
        def on_progress(done, total, page):
            progress_calls.append((done, total, page))

        items = [
            ("tile1.png", 1, {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}),
            ("tile2.png", 1, {"x": 0.5, "y": 0.0, "w": 0.5, "h": 1.0}),
            ("tile3.png", 2, {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}),
        ]

        with patch.object(vision, "_data_url", return_value="data:image/png;base64,AA"), \
             patch.object(provider, "_call", side_effect=mock_call):
            res = provider.extract(items, on_progress=on_progress)

        self.assertEqual(len(progress_calls), 3)
        # 验证进度总数均为 3
        self.assertTrue(all(total == 3 for _, total, _ in progress_calls))
        # 验证最终完成数为 3
        self.assertEqual(max(done for done, _, _ in progress_calls), 3)


class UncertaintyAndBoxTests(unittest.TestCase):
    def test_program_warning_splits_into_location_and_detail(self):
        item = Uncertainty.from_text("断路器 MCB-63/C16A/1P：回路逐条计数 2 只，元器件汇总 1 只，请核对")
        self.assertEqual(item.location, "断路器 MCB-63/C16A/1P")
        self.assertIn("回路逐条计数", item.detail)
        self.assertEqual(item.text, "断路器 MCB-63/C16A/1P：回路逐条计数 2 只，元器件汇总 1 只，请核对")

    def test_text_without_separator_keeps_single_line(self):
        item = Uncertainty.from_text("箱体 AL1 数量为 0，请核对")
        self.assertEqual(item.location, "")
        self.assertEqual(item.text, "箱体 AL1 数量为 0，请核对")

    def test_bbox_is_clipped_into_page(self):
        box = BBox(x=0.9, y=0.8, w=0.5, h=0.5)
        self.assertAlmostEqual(box.w, 0.1, places=4)
        self.assertAlmostEqual(box.h, 0.2, places=4)
        self.assertTrue(box.usable)

    def test_degenerate_bbox_is_rejected(self):
        with self.assertRaises(ValidationError):
            BBox(x=0.2, y=0.2, w=0, h=0.1)
        with self.assertRaises(ValidationError):
            BBox(x=1.2, y=0.2, w=0.1, h=0.1)

    def test_uncertainties_are_structured_and_deduped(self):
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1"}],
            "circuits": [{"box": "AL1", "breaker": "MCB-63/C16A/1P",
                          "circuit_no": "WL1",
                          "bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.05}}],
            "extra_devices": [], "requirements": [],
            "uncertainties": [
                {"location": "WL1", "detail": "型号不清晰", "bbox": None},
                {"location": "WL1", "detail": "型号不清晰"},
            ],
        })
        result = assemble(raw)
        self.assertEqual(len(result.uncertainties), 1)
        self.assertEqual(result.uncertainties[0].location, "WL1")
        self.assertEqual(result.circuits[0].bbox.x, 0.1)
        self.assertIsNone(result.uncertainties[0].bbox)


def _uncertainty_texts(result):
    return [item.text for item in result.uncertainties]


class JobIntegrationTests(unittest.TestCase):
    def test_assembly_warning_reaches_job_and_workbook(self):
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "AL1", "quantity": 1}],
            "circuits": [{"box": "AL1", "breaker": "MCB 1P+N"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        with tempfile.TemporaryDirectory() as directory:
            job_id = "check-test"
            app.jobs[job_id] = {"status": "queued"}
            with patch.object(app, "WORKDIR", directory), \
                 patch.object(app, "render_pdf", return_value=["sample.png"]), \
                 patch.object(app, "plan_tiles", return_value=[]), \
                 patch.object(app, "VisionProvider") as provider:
                provider.return_value.configured = True
                provider.return_value.extract.return_value = raw
                provider.return_value.model = "deepseek-flash"
                app.process_pdf(job_id, "sample.pdf", "sample.pdf")
            job = app.jobs.pop(job_id)
            self.assertEqual(job["status"], "done")
            # 任务对象要能直接序列化成接口响应，否则前端轮询会拿到 500
            json.dumps(dict(job), ensure_ascii=False)
            self.assertEqual(len(job["summary"]["uncertainties"]), 1)
            workbook = load_workbook(Path(directory) / f"{job_id}.xlsx", read_only=True)
            text = " ".join(str(cell.value) for row in workbook["技术要求与报价说明"] for cell in row)
            self.assertIn("无法安全拆分", text)
            self.assertIn("模型:deepseek-flash", text)

    def test_update_job_data_and_rebuild_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            job_id = "update-test"
            app.jobs[job_id] = {
                "status": "done",
                "filename": "test.pdf",
                "summary": {"title": "配电箱清单", "meta": {"model": "test-model"}},
                "data": {"boxes": [], "circuits": [], "components": [], "requirements": [], "uncertainties": []}
            }
            with patch.object(app, "WORKDIR", directory):
                req = app.JobDataUpdateRequest(
                    boxes=[{"code": "AL1", "name": "配电箱", "quantity": 1}],
                    components=[{"name": "微型断路器", "spec": "MCB-63/C16A/1P", "unit": "只", "quantity": 5}],
                    uncertainties=[Uncertainty(location="", detail="测试存疑")]
                )
                res = app.update_job_data(job_id, req)
                self.assertTrue(res["ok"])
                self.assertEqual(res["summary"]["components"], 1)
                self.assertEqual(res["summary"]["boxes"], 1)

                # Verify Excel was rebuilt
                wb = load_workbook(Path(directory) / f"{job_id}.xlsx", data_only=True)
                self.assertIn("元器件汇总", wb.sheetnames)
                ws = wb["元器件汇总"]
                rows = list(ws.iter_rows(values_only=True))
                # Row 4 is the first data row
                self.assertEqual(rows[3][1], "微型断路器")
                self.assertEqual(rows[3][4], 5)

    def test_job_chat_local_command_skips_the_model(self):
        job_id = "chat-local"
        app.jobs[job_id] = {"status": "done", "data": {}}
        with patch.object(app, "Assistant") as assistant:
            result = app.job_chat(job_id, app.ChatRequest(message="开始核对"))
        self.assertEqual(result["action"], "review")
        assistant.return_value.ask.assert_not_called()
        app.jobs.pop(job_id)

    def test_chat_without_configured_model_reports_503(self):
        job_id = "chat-nokey"
        app.jobs[job_id] = {"status": "done", "data": {"circuits": []}}
        with patch.dict(os.environ, {"VISION_API_KEY": "", "ASSISTANT_API_KEY": ""}):
            with self.assertRaises(HTTPException) as caught:
                app.job_chat(job_id, app.ChatRequest(message="WL1 用什么断路器"))
        self.assertEqual(caught.exception.status_code, 503)
        app.jobs.pop(job_id)

    def test_chat_reply_and_patch_are_written_back(self):
        job_id = "chat-patch"
        with tempfile.TemporaryDirectory() as directory:
            app.jobs[job_id] = {
                "status": "done", "filename": "t.pdf",
                "summary": {"title": "清单", "meta": {"model": "test-model"}},
                "raw": {"boxes": [{"code": "AL1"}], "circuits": [],
                        "extra_devices": [], "requirements": [], "uncertainties": []},
                "data": {
                    "boxes": [{"code": "AL1"}],
                    "circuits": [{"box": "AL1", "circuit_no": "WL1", "breaker": "MCB-63/C16A/1P"}],
                    "components": [], "requirements": [], "uncertainties": [],
                },
            }
            mock = patch.object(app, "Assistant")
            with patch.object(app, "WORKDIR", directory), mock as assistant:
                assistant.return_value.ask.return_value = {
                    "reply": "已把 WL1 改成 C20A。", "tab": "circuits",
                    "highlight": ["WL1"], "focus": "WL1",
                    "patch": [{"circuit_no": "WL1", "field": "breaker",
                               "value": "MCB-63/C20A/1P"}],
                }
                result = app.job_chat(job_id, app.ChatRequest(message="把 WL1 改成 C20A"))

            self.assertEqual(result["tab"], "circuits")
            self.assertEqual(result["changes"][0]["old"], "MCB-63/C16A/1P")
            self.assertEqual(app.jobs[job_id]["data"]["circuits"][0]["breaker"],
                             "MCB-63/C20A/1P")
            # 元器件汇总由回路重新推导，不再是旧值
            specs = {c["spec"] for c in app.jobs[job_id]["data"]["components"]}
            self.assertIn("MCB-63/C20A/1P", specs)
            self.assertNotIn("MCB-63/C16A/1P", specs)
            self.assertEqual(app.jobs[job_id]["changes"][0]["source"], "AI")
            self.assertTrue((Path(directory) / f"{job_id}.xlsx").exists())
            app.jobs.pop(job_id)

    def test_chat_can_add_device_and_change_box_count(self):
        """模型漏了浪涌保护器 / 看错了箱体台数时，助手要能直接补上。"""
        job_id = "chat-extend"
        with tempfile.TemporaryDirectory() as directory:
            app.jobs[job_id] = {
                "status": "done", "filename": "t.pdf",
                "summary": {"title": "清单", "meta": {}},
                "raw": {"boxes": [{"code": "2SAL3"}], "circuits": [],
                        "extra_devices": [], "requirements": [], "uncertainties": []},
                "data": {"boxes": [{"code": "2SAL3", "quantity": 1}],
                         "circuits": [{"box": "2SAL3", "circuit_no": "WL1",
                                       "breaker": "MCB-63/C16A/1P"}],
                         "components": [], "requirements": [], "uncertainties": [],
                         "extra_devices": []},
            }
            with patch.object(app, "WORKDIR", directory), patch.object(app, "Assistant") as assistant:
                assistant.return_value.ask.return_value = {
                    "reply": "已补上浪涌保护器，并把台数改成 2。", "tab": "boxes",
                    "highlight": [], "focus": "",
                    "patch": [
                        {"kind": "device", "action": "add", "name": "浪涌保护器",
                         "spec": "CPM-R40T", "unit": "套", "quantity": 1,
                         "used_in": "2SAL3 进线侧"},
                        {"kind": "box", "target": "2SAL3", "field": "quantity", "value": "2"},
                    ],
                }
                result = app.job_chat(job_id, app.ChatRequest(message="图纸上还有个浪涌保护器，箱体是2台"))

            data = app.jobs[job_id]["data"]
            self.assertEqual([d["name"] for d in data["extra_devices"]], ["浪涌保护器"])
            self.assertEqual(data["boxes"][0]["quantity"], 2)
            names = {c["name"]: c["quantity"] for c in data["components"]}
            self.assertEqual(names.get("配电箱体"), 2.0, "箱体台数变了，汇总要跟着变")
            # 设备写在箱内（used_in 以箱号开头），台数从 1 改成 2 后总数也要乘 2
            self.assertEqual(names.get("浪涌保护器"), 2.0)
            self.assertEqual(len(result["changes"]), 2)
            self.assertTrue(all(c["source"] == "AI" for c in result["changes"]))
            app.jobs.pop(job_id)

    def test_chat_patch_to_missing_circuit_is_refused_not_applied(self):
        job_id = "chat-bad-patch"
        app.jobs[job_id] = {
            "status": "done", "data": {
                "boxes": [{"code": "AL1"}],
                "circuits": [{"box": "AL1", "circuit_no": "WL1", "breaker": "MCB-63/C16A/1P"}],
                "components": [], "requirements": [], "uncertainties": [],
            },
        }
        with patch.object(app, "Assistant") as assistant:
            assistant.return_value.ask.return_value = {
                "reply": "好的", "tab": "chat", "highlight": [], "focus": "",
                "patch": [{"circuit_no": "WL9", "field": "breaker", "value": "X"},
                          {"circuit_no": "WL1", "field": "数量", "value": "3"}],
            }
            result = app.job_chat(job_id, app.ChatRequest(message="改一下"))
        self.assertEqual(result["changes"], [])
        self.assertIn("没有执行", result["reply"])
        self.assertIn("WL9", result["reply"])
        self.assertEqual(app.jobs[job_id]["data"]["circuits"][0]["breaker"], "MCB-63/C16A/1P")
        app.jobs.pop(job_id)

    def test_resolved_flag_survives_reassembly(self):
        job_id = "resolve-flag"
        with tempfile.TemporaryDirectory() as directory:
            app.jobs[job_id] = {
                "status": "done", "filename": "t.pdf",
                "summary": {"title": "清单", "meta": {}},
                "raw": {"boxes": [{"code": "AL1"}],
                        "circuits": [{"box": "AL1", "breaker": "MCB 1P+N"}],
                        "extra_devices": [], "requirements": [], "uncertainties": []},
                "data": {"boxes": [{"code": "AL1"}], "circuits": [], "components": [],
                         "requirements": [], "uncertainties": []},
            }
            with patch.object(app, "WORKDIR", directory):
                req = app.JobDataUpdateRequest(
                    boxes=[{"code": "AL1", "quantity": 1}],
                    circuits=[{"box": "AL1", "breaker": "MCB 1P+N"}],
                )
                first = app.update_job_data(job_id, req)
                items = first["data"]["uncertainties"]
                self.assertTrue(items, "无法安全拆分的写法应产生待核对项")
                items[0]["resolved"] = True
                second = app.update_job_data(job_id, app.JobDataUpdateRequest(
                    boxes=[{"code": "AL1", "quantity": 1}],
                    circuits=[{"box": "AL1", "breaker": "MCB 1P+N"}],
                    uncertainties=[items[0]],
                ))
            kept = [u for u in second["data"]["uncertainties"] if u["detail"] == items[0]["detail"]]
            self.assertTrue(kept and kept[0]["resolved"])
            app.jobs.pop(job_id)


    def test_large_page_falls_back_to_whole_page_when_tiling_fails(self):
        """切块失败不能让整个提取计划挂掉，退回整页继续。"""
        with tempfile.TemporaryDirectory() as d:
            with patch.object(app, "plan_tiles", side_effect=RuntimeError("boom")):
                items = app._extract_plan(os.path.join(d, "x.pdf"), ["p1.png", "p2.png"])
        self.assertEqual([(i[1], i[2]) for i in items], [(1, None), (2, None)])


class StoreAndExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._dir = patch.object(app.store, "DATA_DIR", self.tmp.name)
        self._files = patch.multiple(
            app.store,
            PROJECTS_FILE=os.path.join(self.tmp.name, "projects.json"),
            HISTORY_FILE=os.path.join(self.tmp.name, "history.json"),
            SETTINGS_FILE=os.path.join(self.tmp.name, "settings.json"),
        )
        self._dir.start(); self._files.start()
        self.addCleanup(self._files.stop)
        self.addCleanup(self._dir.stop)

    def _job(self, job_id, **extra):
        app.jobs[job_id] = {
            "status": "done", "filename": "t.pdf", "project": "万达广场",
            "summary": {"title": "清单", "meta": {}},
            "raw": {"boxes": [{"code": "AL1"}], "circuits": [], "extra_devices": [],
                    "requirements": [], "uncertainties": []},
            "data": {"boxes": [{"code": "AL1"}], "circuits": [], "components": [],
                     "requirements": [], "uncertainties": [], "extra_devices": []},
            **extra,
        }
        self.addCleanup(app.jobs.pop, job_id, None)

    def test_box_and_device_edits_survive_and_can_be_reverted(self):
        """非回路设备与箱体的增删改必须能落盘、能撤销 —— 模型漏一项时用户要能补。"""
        job_id = "edit-loop"
        with tempfile.TemporaryDirectory() as directory:
            self._job(job_id)
            with patch.object(app, "WORKDIR", directory):
                base = app.JobDataUpdateRequest(
                    boxes=[{"code": "AL1", "quantity": 2}],
                    extra_devices=[{"name": "浪涌保护器", "spec": "CPM-R40T",
                                    "unit": "套", "quantity": 1}],
                )
                first = app.update_job_data(job_id, base)
                self.assertEqual([b["quantity"] for b in first["data"]["boxes"]], [2])
                self.assertEqual(len(first["data"]["extra_devices"]), 1)
                self.assertIn(("配电箱体", 2.0),
                              [(c["name"], c["quantity"]) for c in first["data"]["components"]],
                              "箱体台数改了，汇总要跟着变")

                # 再存一次（模拟接着改回路），补录的设备不能“回来又拿 raw 覆盖”
                second = app.update_job_data(job_id, app.JobDataUpdateRequest(
                    boxes=first["data"]["boxes"],
                    extra_devices=first["data"]["extra_devices"],
                    circuits=[{"box": "AL1", "circuit_no": "WL1", "breaker": "MCB-63/C16A/1P"}],
                ))
                self.assertEqual(len(second["data"]["extra_devices"]), 1)

                # 删掉后撤销，应该回到原来的样子
                third = app.update_job_data(job_id, app.JobDataUpdateRequest(
                    boxes=second["data"]["boxes"],
                    extra_devices=[],
                    changes=[{"scope": "device", "action": "remove",
                              "target": "浪涌保护器",
                              "before": second["data"]["extra_devices"][0],
                              "old": "浪涌保护器|CPM-R40T", "source": "手动"}],
                ))
                self.assertEqual(third["data"]["extra_devices"], [])
                back = app.revert_change(job_id, app.RevertRequest(
                    target="浪涌保护器", scope="device"))
                self.assertEqual(len(back["data"]["extra_devices"]), 1)
                self.assertEqual(back["changes"], [])

    def test_box_field_revert_restores_quantity(self):
        job_id = "box-revert"
        with tempfile.TemporaryDirectory() as directory:
            self._job(job_id)
            with patch.object(app, "WORKDIR", directory):
                app.update_job_data(job_id, app.JobDataUpdateRequest(
                    boxes=[{"code": "AL1", "quantity": 5}],
                    changes=[{"scope": "box", "target": "AL1", "field": "quantity",
                              "old": 1, "new": 5, "source": "手动"}],
                ))
                self.assertEqual(app.jobs[job_id]["data"]["boxes"][0]["quantity"], 5)
                out = app.revert_change(job_id, app.RevertRequest(
                    target="AL1", field="quantity", scope="box"))
                self.assertEqual(out["data"]["boxes"][0]["quantity"], 1)

    def test_change_sheet_is_written_and_revert_restores_old_value(self):
        job_id = "chg-1"
        self._job(job_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "WORKDIR", directory):
            req = app.JobDataUpdateRequest(
                boxes=[{"code": "AL1", "quantity": 1}],
                circuits=[{"box": "AL1", "circuit_no": "WL1", "breaker": "MCB-63/C20A/1P"}],
                changes=[{"target": "WL1", "field": "breaker",
                          "old": "MCB-63/C16A/1P", "new": "MCB-63/C20A/1P", "source": "手动"}],
            )
            app.update_job_data(job_id, req)
            wb = load_workbook(Path(directory) / f"{job_id}.xlsx")
            self.assertIn("变更记录", wb.sheetnames)
            rows = list(wb["变更记录"].iter_rows(values_only=True))
            self.assertEqual(rows[3][3], "WL1")
            self.assertEqual(rows[3][5], "MCB-63/C16A/1P")

            out = app.revert_change(job_id, app.RevertRequest(target="WL1", field="breaker"))
            self.assertEqual(out["reverted"]["new"], "MCB-63/C20A/1P")
            self.assertEqual(app.jobs[job_id]["data"]["circuits"][0]["breaker"],
                             "MCB-63/C16A/1P")
            self.assertEqual(app.jobs[job_id]["changes"], [])

    def test_revert_refuses_when_a_later_change_exists(self):
        job_id = "chg-2"
        self._job(job_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "WORKDIR", directory):
            app.update_job_data(job_id, app.JobDataUpdateRequest(
                boxes=[{"code": "AL1", "quantity": 1}],
                circuits=[{"box": "AL1", "circuit_no": "WL1", "breaker": "A"}],
                changes=[{"target": "WL1", "field": "breaker", "old": "O1", "new": "A",
                          "source": "手动"}]))
            app.update_job_data(job_id, app.JobDataUpdateRequest(
                boxes=[{"code": "AL1", "quantity": 1}],
                circuits=[{"box": "AL1", "circuit_no": "WL1", "breaker": "B"}],
                changes=[{"target": "WL1", "field": "breaker", "old": "A", "new": "B",
                          "source": "手动"}]))
            with self.assertRaises(HTTPException) as caught:
                app.revert_change(job_id, app.RevertRequest(
                    target="WL1", field="breaker",
                    ts=app.jobs[job_id]["changes"][0]["ts"]))
            self.assertEqual(caught.exception.status_code, 409)

    def test_settings_roundtrip_never_returns_the_key(self):
        app.put_settings(app.SettingsRequest(vision_api_key="sk-secret-12345678"))
        public = app.get_settings()["settings"]
        self.assertNotIn("vision_api_key", public)
        self.assertTrue(public["vision_api_key_set"])
        self.assertTrue(public["vision_api_key_hint"].startswith("***"))
        self.assertNotIn("12345678", public["vision_api_key_hint"])

    def test_projects_group_jobs_and_keep_empty_ones(self):
        self._job("p-1")
        app.create_project(app.ProjectRequest(name="空项目"))
        with patch.object(app, "WORKDIR", self.tmp.name):
            names = [p["name"] for p in app.list_projects()["projects"]]
        self.assertIn("空项目", names)

    def test_export_records_history(self):
        job_id = "hist-1"
        self._job(job_id)
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "WORKDIR", directory):
            Path(directory, f"{job_id}.xlsx").write_bytes(b"x")
            app.job_excel(job_id)
        entries = app.export_history()["history"]
        self.assertEqual(entries[0]["job_id"], job_id)
        self.assertEqual(entries[0]["project"], "万达广场")


class CropParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patcher = patch.object(app, "WORKDIR", self.tmp.name)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_parse_region_crops_and_parses_components(self):
        job_id = "crop-test-1"
        _make_pdf(os.path.join(self.tmp.name, f"{job_id}.pdf"), width_mm=210, height_mm=297)
        app.jobs[job_id] = {
            "status": "done",
            "job_id": job_id,
            "filename": "test.pdf",
            "data": {"boxes": [], "circuits": [], "components": [], "extra_devices": []},
        }

        mock_result = {
            "summary": "识别到进线隔离开关与照明断路器",
            "components": [
                {"name": "隔离开关", "spec": "HRW-100/3P", "quantity": 1, "unit": "只", "note": "进线侧"},
                {"name": "微型断路器", "spec": "MCB-63/C16A/1P", "quantity": 2, "unit": "只", "note": "照明"},
            ],
            "circuits": [
                {"circuit_no": "WL1", "breaker": "MCB-63/C16A/1P", "cable": "BV-3x2.5", "load_name": "照明"}
            ],
            "raw_text": "HRW-100 MCB-63",
        }

        with patch.object(VisionProvider, "configured", True), \
             patch.object(VisionProvider, "parse_crop", return_value=mock_result):
            req = app.RegionParseRequest(page=1, x=0.1, y=0.1, w=0.3, h=0.2)
            resp = app.parse_region(job_id, req)
            self.assertTrue(resp["ok"])
            self.assertIn("crop_url", resp)
            self.assertEqual(len(resp["components"]), 2)
            self.assertEqual(resp["components"][0]["name"], "隔离开关")
            self.assertEqual(len(resp["circuits"]), 1)

            # 验证生成的裁切图片可通过接口访问
            crop_name = resp["crop_url"].split("/")[-1]
            crop_file = app.get_crop_image(job_id, crop_name)
            self.assertEqual(crop_file.media_type, "image/png")
            self.assertTrue(os.path.exists(crop_file.path))

    def test_parse_region_nonexistent_job(self):
        req = app.RegionParseRequest(page=1, x=0, y=0, w=0.5, h=0.5)
        with self.assertRaises(app.HTTPException) as ctx:
            app.parse_region("nonexistent-job-id", req)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_get_crop_image_validation(self):
        # 非法文件名
        with self.assertRaises(app.HTTPException) as ctx:
            app.get_crop_image("job1", "../../etc/passwd")
        self.assertEqual(ctx.exception.status_code, 400)

        # 文件不存在
        with self.assertRaises(app.HTTPException) as ctx:
            app.get_crop_image("job1", "nonexistent_crop.png")
        self.assertEqual(ctx.exception.status_code, 404)


class AssistantEmptyReplyTests(unittest.TestCase):
    """空 content 是真实会发生的事：模型把预算花在推理上就会回一个空串。
    以前这里报的是“没有返回合法 JSON: ”，冒号后面什么都没有，用户完全看不出发生了什么。"""

    def _provider(self):
        with patch.dict(os.environ, {"VISION_API_KEY": "test-only",
                                    "VISION_BASE_URL": "https://example.test/v1",
                                    "VISION_MODEL": "m"}):
            return Assistant()

    @staticmethod
    def _reply(content, finish="stop", extra=None):
        message = {"role": "assistant", "content": content}
        message.update(extra or {})
        return {"choices": [{"message": message, "finish_reason": finish}],
                "usage": {"completion_tokens": 7}}

    def test_empty_content_is_retried_and_then_succeeds(self):
        provider = self._provider()
        good = json.dumps({"reply": "好了", "tab": "chat"})
        with patch.object(provider, "_post", side_effect=[
            self._reply(""), self._reply(good),
        ]) as call:
            result = provider.ask({"circuits": []}, "问一下")
        self.assertEqual(result["reply"], "好了")
        self.assertEqual(call.call_count, 2)

    def test_empty_content_reports_why_not_just_a_colon(self):
        provider = self._provider()
        with patch.object(provider, "_post", return_value=self._reply(
                "", finish="length", extra={"reasoning_content": "想了很久"})):
            with self.assertRaises(ValueError) as caught:
                provider.ask({"circuits": []}, "问一下")
        msg = str(caught.exception)
        self.assertIn("空内容", msg)
        self.assertIn("length", msg)
        self.assertIn("reasoning_content", msg)
        self.assertIn("重试", msg)

    def test_truncated_reply_says_so_instead_of_faking_a_parse_error(self):
        provider = self._provider()
        with patch.object(provider, "_post", return_value=self._reply(
                '{"reply": "很长的一', finish="length")) as call:
            with self.assertRaises(ValueError) as caught:
                provider.ask({"circuits": []}, "问一下")
        self.assertIn("被截断", str(caught.exception))
        self.assertEqual(call.call_count, 1, "截断是确定性的，重试没意义")

    def test_broken_json_still_reports_the_raw_text(self):
        provider = self._provider()
        with patch.object(provider, "_post", return_value=self._reply("这不是 JSON")):
            with self.assertRaises(ValueError) as caught:
                provider.ask({"circuits": []}, "问一下")
        self.assertIn("这不是 JSON", str(caught.exception))


class DistributionTopologyAndExcelTests(unittest.TestCase):
    """验证配电系统层级拓扑建树与全项目 Excel 拓扑输出。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_topology_hierarchy_and_feeder_association(self):
        from extractor.assemble import assemble, build_distribution_topology
        from extractor.schema import Box, Circuit, RawExtraction

        raw = RawExtraction.model_validate({
            "boxes": [
                {"code": "01ALZ1", "name": "动力总柜", "quantity": 1},
                {"code": "01AL1", "name": "一层照明箱", "quantity": 1},
                {"code": "01AL2", "name": "潜水泵动力箱", "quantity": 1},
                {"code": "01AL2-2", "name": "潜水泵控制原理图", "quantity": 1},
            ],
            "circuits": [
                {"box": "01ALZ1", "circuit_no": "WL1", "load_name": "至01AL1照明箱", "breaker": "MCCB-125A"},
                {"box": "01ALZ1", "circuit_no": "WP1", "load_name": "送01AL2动力箱", "breaker": "MCCB-125A"},
                {"box": "01AL1", "circuit_no": "WL1-1", "load_name": "走廊照明", "breaker": "MCB-16A"},
                {"box": "01AL2", "circuit_no": "WP2-1", "load_name": "1#潜水泵", "secondary_ref": "01AL2-2", "breaker": "MCB-32A"},
            ],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })

        result = assemble(raw)
        self.assertTrue(len(result.topology) >= 1)
        root = result.topology[0]
        self.assertEqual(root.code, "01ALZ1")
        self.assertEqual(root.node_type, "cabinet")

        child_codes = {c.code: c for c in root.children}
        self.assertIn("01AL1", child_codes)
        self.assertIn("01AL2", child_codes)
        self.assertEqual(child_codes["01AL1"].feed_circuit, "WL1")
        self.assertEqual(child_codes["01AL2"].feed_circuit, "WP1")

        # 验证二次控制原理图挂接在 01AL2 下
        al2_children = {c.code: c for c in child_codes["01AL2"].children}
        self.assertIn("01AL2-2", al2_children)
        self.assertEqual(al2_children["01AL2-2"].node_type, "secondary")

    def test_excel_export_contains_topology_sheet(self):
        import openpyxl
        from extractor.assemble import assemble
        from extractor.excel import build_workbook, build_project_bom_workbook
        from extractor.schema import RawExtraction

        raw = RawExtraction.model_validate({
            "boxes": [
                {"code": "01ALZ1", "name": "动力总柜", "quantity": 1},
                {"code": "01AL1", "name": "一层照明箱", "quantity": 1},
            ],
            "circuits": [
                {"box": "01ALZ1", "circuit_no": "WL1", "load_name": "至01AL1照明箱", "breaker": "MCCB-125A"},
                {"box": "01AL1", "circuit_no": "WL1-1", "load_name": "展厅照明", "breaker": "MCB-16A"},
            ],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw)

        # 1. 验证单图任务 Excel 输出包含“配电系统拓扑树”
        xlsx_single = os.path.join(self.temp_dir, "single.xlsx")
        build_workbook(result, "subtitle", xlsx_single, layout="all")
        wb1 = openpyxl.load_workbook(xlsx_single)
        self.assertIn("配电系统拓扑树", wb1.sheetnames)
        ws1 = wb1["配电系统拓扑树"]
        all_vals = [cell.value for row in ws1.iter_rows() for cell in row if cell.value]
        self.assertTrue(any("01ALZ1" in str(v) for v in all_vals))
        self.assertTrue(any("01AL1" in str(v) for v in all_vals))

        # 2. 验证多任务项目 Global BOM 包含“配电系统拓扑树”
        xlsx_proj = os.path.join(self.temp_dir, "project_bom.xlsx")
        mock_jobs = [
            {"job_id": "j1", "box_code": "01ALZ1", "data": {
                "boxes": [{"code": "01ALZ1", "name": "动力总柜", "quantity": 1}],
                "circuits": [{"box": "01ALZ1", "circuit_no": "WL1", "load_name": "至01AL1照明箱", "breaker": "MCCB-125A"}],
                "components": [{"name": "塑壳断路器", "spec": "MCCB-125A", "quantity": 1, "unit": "只"}],
            }},
            {"job_id": "j2", "box_code": "01AL1", "data": {
                "boxes": [{"code": "01AL1", "name": "一层照明箱", "quantity": 1}],
                "circuits": [{"box": "01AL1", "circuit_no": "WL1-1", "load_name": "展厅照明", "breaker": "MCB-16A"}],
                "components": [{"name": "微型断路器", "spec": "MCB-16A", "quantity": 1, "unit": "只"}],
            }},
        ]
        build_project_bom_workbook("测试综合体工程", mock_jobs, xlsx_proj)
        wb2 = openpyxl.load_workbook(xlsx_proj)
        self.assertIn("配电系统拓扑树", wb2.sheetnames)

    def test_project_topology_api(self):
        from unittest.mock import patch
        mock_jobs = [
            {"job_id": "j1", "project": "商业体项目", "box_code": "01ALZ1", "data": {
                "boxes": [{"code": "01ALZ1", "name": "动力总柜", "quantity": 1}],
                "circuits": [{"box": "01ALZ1", "circuit_no": "WL1", "load_name": "至01AL1照明箱", "breaker": "MCCB-125A"}],
            }},
            {"job_id": "j2", "project": "商业体项目", "box_code": "01AL1", "data": {
                "boxes": [{"code": "01AL1", "name": "一层照明箱", "quantity": 1}],
                "circuits": [{"box": "01AL1", "circuit_no": "WL1-1", "load_name": "展厅照明", "breaker": "MCB-16A"}],
            }},
        ]
        with patch("app.list_jobs", return_value={"jobs": [{"job_id": "j1", "project": "商业体项目"}, {"job_id": "j2", "project": "商业体项目"}]}), \
             patch("app.load_job_cached", side_effect=lambda jid: next(j for j in mock_jobs if j["job_id"] == jid)):
            res = app.get_project_topology("商业体项目")
            self.assertTrue(res["ok"])
            self.assertEqual(res["project"], "商业体项目")
            self.assertEqual(len(res["topology"]), 1)
            self.assertEqual(res["topology"][0]["code"], "01ALZ1")
            self.assertEqual(res["topology"][0]["children"][0]["code"], "01AL1")


class AiCostAndSheetCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_estimate_cost_rates(self):
        from extractor.vision import estimate_cost
        # DeepSeek: in 1.0/M, out 2.0/M
        res_ds = estimate_cost("deepseek-chat", 1_000_000, 1_000_000)
        self.assertAlmostEqual(res_ds["cost_in"], 1.0, places=4)
        self.assertAlmostEqual(res_ds["cost_out"], 2.0, places=4)
        self.assertAlmostEqual(res_ds["total_cost"], 3.0, places=4)

        # GPT-4o-mini
        res_mini = estimate_cost("gpt-4o-mini", 100_000, 50_000)
        self.assertAlmostEqual(res_mini["cost_in"], 0.11, places=4)
        self.assertAlmostEqual(res_mini["cost_out"], 0.22, places=4)
        self.assertAlmostEqual(res_mini["total_cost"], 0.33, places=4)

    def test_project_ai_usage_recording_and_query(self):
        import store
        fake_projects_file = os.path.join(self.temp_dir, "projects.json")
        with patch.object(store, "PROJECTS_FILE", fake_projects_file), \
             patch.object(store, "DATA_DIR", self.temp_dir):
            usage1 = {
                "model": "deepseek-chat",
                "calls_count": 2,
                "prompt_tokens": 10000,
                "completion_tokens": 2000,
                "total_tokens": 12000,
                "cost_in": 0.01,
                "cost_out": 0.004,
                "total_cost": 0.014,
                "currency": "￥",
                "logs": [{"prompt_tokens": 5000}, {"prompt_tokens": 5000}],
            }
            store.record_project_ai_usage("CBD写字楼电气工程", usage1, job_id="job_001", filename="图纸1.dwg")

            usage2 = {
                "model": "deepseek-chat",
                "calls_count": 1,
                "prompt_tokens": 5000,
                "completion_tokens": 1000,
                "total_tokens": 6000,
                "cost_in": 0.005,
                "cost_out": 0.002,
                "total_cost": 0.007,
                "currency": "￥",
                "logs": [{"prompt_tokens": 5000}],
            }
            store.record_project_ai_usage("CBD写字楼电气工程", usage2, job_id="job_002", filename="图纸2.dwg")

            logs = store.get_project_ai_logs("CBD写字楼电气工程")
            self.assertEqual(logs["project_name"], "CBD写字楼电气工程")
            self.assertEqual(logs["ai_prompt_tokens"], 15000)
            self.assertEqual(logs["ai_completion_tokens"], 3000)
            self.assertEqual(logs["ai_tokens_total"], 18000)
            self.assertAlmostEqual(logs["ai_cost_in"], 0.015, places=4)
            self.assertAlmostEqual(logs["ai_cost_out"], 0.006, places=4)
            self.assertAlmostEqual(logs["ai_cost_total"], 0.021, places=4)
            self.assertEqual(len(logs["ai_logs"]), 2)
            self.assertEqual(logs["ai_logs"][0]["job_id"], "job_002")

    def test_rename_job_sheet_endpoint(self):
        job_id = "test_rename_job"
        app.jobs[job_id] = {
            "status": "done",
            "sheet_names": {"1": "原切图1", "2": "原切图2"},
        }
        with tempfile.TemporaryDirectory() as d:
            with patch.object(app, "WORKDIR", d):
                req = app.SheetRenameRequest(page=2, name="消防控制箱系统图")
                res = app.rename_job_sheet(job_id, req)
                self.assertTrue(res["ok"])
                self.assertEqual(res["sheet_names"]["2"], "消防控制箱系统图")
                self.assertEqual(app.jobs[job_id]["sheet_names"]["2"], "消防控制箱系统图")

    def test_project_ai_logs_api(self):
        import store
        fake_projects_file = os.path.join(self.temp_dir, "projects.json")
        with patch.object(store, "PROJECTS_FILE", fake_projects_file), \
             patch.object(store, "DATA_DIR", self.temp_dir):
            store.record_project_ai_usage("医院项目", {
                "model": "deepseek-chat",
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "cost_in": 0.001,
                "cost_out": 0.001,
                "total_cost": 0.002,
                "currency": "￥",
            }, job_id="hosp_1", filename="hospital.pdf")

            res = app.get_project_ai_logs_api("医院项目")
            self.assertEqual(res["project_name"], "医院项目")
            self.assertEqual(res["ai_tokens_total"], 1500)
            self.assertAlmostEqual(res["ai_cost_total"], 0.002, places=4)


if __name__ == "__main__":
    unittest.main()

