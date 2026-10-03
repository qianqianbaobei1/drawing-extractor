# -*- coding: utf-8 -*-
"""自动化测试：验证箱柜两级报表（Sheet 1 汇总合价 + Sheet 2 卡片流水明细）、AI 自由整理动态导出与 DWG 空间索引加速。"""
import os
import tempfile
import unittest
import openpyxl
from fastapi.testclient import TestClient

from app import app
from extractor.schema import ExtractionResult, Box, Circuit, Component
from extractor.excel import build_workbook, build_custom_table_workbook
import ezdxf


class TestV3Features(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(app)

    def test_two_level_quotation_sheets(self):
        """测试成套设备报价汇总表（Sheet 1）与垂直流水卡片明细表（Sheet 2）以及超链接跳转。"""
        res = ExtractionResult(
            title="四川中烟工业有限责任公司成都卷烟厂制丝线升级改造项目",
            boxes=[
                Box(code="02AL01", name="照明配电箱", location="锅炉房", install="明装", size="XM-8", quantity=1),
                Box(code="02AK01", name="空调配电箱", location="锅炉房", install="明装", size="XM-10", quantity=2),
                Box(code="01LBZ1", name="照明配电总箱", location="制丝工房", install="落地", size="GGD", quantity=1),
            ],
            circuits=[
                Circuit(box="02AL01", circuit_no="1WL1", load_name="锅炉房照明1", breaker="C65N-C16/1P", power_kw="2.0", phase="L1", cable="WDZ-BYJ-3x2.5"),
                Circuit(box="02AL01", circuit_no="1WL2", load_name="锅炉房照明2", breaker="C65N-C16/1P", power_kw="2.0", phase="L2", cable="WDZ-BYJ-3x2.5"),
                Circuit(box="02AK01", circuit_no="1AK1", load_name="空调动力1", breaker="C65N-C25/3P", power_kw="7.5", phase="L1L2L3", cable="WDZ-BYJ-5x4"),
                Circuit(box="01LBZ1", circuit_no="1AP1", load_name="车间总进线", breaker="NM1-250S/3300 200A", power_kw="95.0", phase="L1L2L3", cable="YJV-4x70+1x35"),
            ],
            components=[
                Component(name="浪涌保护器", spec="CPM-R40T", unit="套", quantity=1, used_in="01LBZ1"),
                Component(name="三相电能表", spec="DTSD-100", unit="台", quantity=1, used_in="01LBZ1"),
            ]
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_file = os.path.join(tmpdir, "test_two_level.xlsx")
            build_workbook(res, "工程造价测试副标题", out_file, layout="quotation")
            self.assertTrue(os.path.exists(out_file))

            wb = openpyxl.load_workbook(out_file)
            sheets = wb.sheetnames

            # 验证前两页表名
            self.assertEqual(sheets[0], "成套设备报价(汇总)")
            self.assertEqual(sheets[1], "箱体分项明细")

            ws1 = wb["成套设备报价(汇总)"]
            # 验证大标题与项目单位/项目名称
            self.assertEqual(ws1["A1"].value, "成套设备报价(汇总)")
            self.assertIn("四川中烟", str(ws1["A4"].value))
            self.assertEqual(ws1["I5"].value, "金额单位：人民币元")
            self.assertEqual(ws1["A6"].value, "配电箱")

            # 验证第一张箱体数据行（从第 8 行开始）
            self.assertEqual(ws1["B8"].value, "02AL01")
            self.assertEqual(ws1["C8"].value, "照明配电箱")
            self.assertEqual(ws1["I8"].value, "锅炉房")
            self.assertGreater(ws1["G8"].value, 0)  # 单价测算大于0
            self.assertEqual(ws1["H8"].value, "=F8*G8")  # 总价公式

            # 验证超链接跳转存在且指向 Sheet 2 (箱体分项明细)
            self.assertIsNotNone(ws1["A8"].hyperlink)
            self.assertIn("箱体分项明细", ws1["A8"].hyperlink.target)

            # 验证底部合计行
            # 共有 3 个箱体，数据行 8, 9, 10，合计行为 11
            self.assertEqual(ws1["A11"].value, "合  计")
            self.assertEqual(ws1["F11"].value, "=SUM(F8:F10)")
            self.assertEqual(ws1["H11"].value, "=SUM(H8:H10)")

            # 验证 Sheet 2 垂直流水卡片
            ws2 = wb["箱体分项明细"]
            content_found = False
            for row in ws2.iter_rows(values_only=True):
                for val in row:
                    if val and "【箱柜编号：02AL01】" in str(val):
                        content_found = True
                        break
            self.assertTrue(content_found, "未在箱体分项明细中找到 02AL01 卡片横幅")

    def test_custom_table_export_api(self):
        """测试 AI 自由整理表格的自定义 Excel 导出接口。"""
        payload = {
            "title": "塑壳断路器分布与采购统计表",
            "headers": ["序号", "元器件名称", "规格型号", "所在配电箱", "数量", "单位", "备注"],
            "rows": [
                [1, "塑壳断路器", "NM1-125S/3300 100A", "02AL01", 1, "台", "进线总开关"],
                [2, "塑壳断路器", "NM1-250S/3300 200A", "01LBZ1", 1, "台", "动力干线开关"],
                [3, "微型断路器", "C65N-C16/1P", "02AL01", 2, "台", "照明回路开关"],
            ],
            "filename": "塑壳断路器统计.xlsx",
            "subtitle": "由 AI 助手自由整理输出"
        }

        resp = self.client.post("/api/export_custom_table", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument", resp.headers["content-type"])
        self.assertTrue(len(resp.content) > 1000)

        # 检查导出的字节流能否正常被 openpyxl 加载
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
            tf.write(resp.content)
            temp_path = tf.name

        try:
            wb = openpyxl.load_workbook(temp_path)
            ws = wb.active
            self.assertEqual(ws["A1"].value, "塑壳断路器分布与采购统计表")
            self.assertEqual(ws["A3"].value, "序号")
            self.assertEqual(ws["B3"].value, "元器件名称")
            self.assertEqual(ws["C4"].value, "NM1-125S/3300 100A")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_cad_slice_with_spatial_grid(self):
        """测试 CAD 空间网格切片与电气实体白名单剪枝机制。"""
        from extractor.cad import slice_and_render_cad_sheets

        doc = ezdxf.new("R2000")
        msp = doc.modelspace()

        # 插入测试图框和回路
        msp.add_text("消防01ATBF01配电箱系统图", dxfattribs={"insert": (10000, 10000), "height": 300})
        msp.add_text("1WL1", dxfattribs={"insert": (12000, 11000), "height": 50})
        msp.add_line((10000, 10000), (20000, 10000))
        msp.add_line((10000, 10000), (10000, 18000))

        # 插入应被白名单快速跳过的耗时 SPLINE / HATCH 实体
        msp.add_spline([(0, 0), (100, 200), (300, 400), (500, 100)])

        sheets = [{
            "title": "消防01ATBF01配电箱系统图",
            "x": 10000.0,
            "y": 10000.0,
            "bbox": (8000.0, 8000.0, 25000.0, 20000.0)
        }]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_pdf = os.path.join(tmpdir, "test_cad_slice.pdf")
            texts = slice_and_render_cad_sheets(doc, sheets, out_pdf)
            self.assertTrue(os.path.exists(out_pdf))
            self.assertGreaterEqual(len(texts), 2)
            extracted_strs = [t["text"] for t in texts]
            self.assertTrue(any("系统图" in s for s in extracted_strs))
            self.assertTrue(any("1WL1" in s for s in extracted_strs))

    def test_resolve_all_and_ai_review_endpoints(self):
        """测试一键全部核对通过接口与 AI 深度全盘复核接口。"""
        import uuid
        from app import jobs, save_job

        test_id = "test_review_" + uuid.uuid4().hex[:6]
        jobs[test_id] = {
            "job_id": test_id,
            "filename": "测试图纸.dwg",
            "status": "done",
            "summary": {"title": "测试工程配电箱", "uncertainties": []},
            "data": {
                "boxes": [{"code": "1AL1", "name": "配电箱", "quantity": 1}],
                "circuits": [
                    {"circuit_no": "WL1", "box": "1AL1", "breaker": "C65N-C16/1P", "power_kw": "2.5", "cable": "BV-3x2.5"},
                    {"circuit_no": "AP1", "box": "1AL1", "breaker": "NM1-125S/3P", "power_kw": "22.0", "cable": "BV-3x2.5"},  # 22kW用2.5平方，预期触发线缆过载预警
                ],
                "components": [],
                "requirements": [],
                "uncertainties": [
                    {"location": "WL1", "detail": "微型断路器规格需人工核验", "resolved": False},
                    {"location": "AP1", "detail": "电缆敷设方式未注明确切管径", "resolved": False},
                ]
            },
            "changes": []
        }
        save_job(test_id)

        # 1. 测试 AI 深度复核
        resp_ai = self.client.post(f"/api/jobs/{test_id}/ai_review")
        self.assertEqual(resp_ai.status_code, 200)
        data_ai = resp_ai.json()
        self.assertIn("summary", data_ai)
        self.assertIn("findings", data_ai)
        # 验证是否自动判定了 WL1 规格完整性，或触发了电缆截面偏小预警
        warning_found = any("偏小预警" in f["title"] or "过载" in f["detail"] for f in data_ai["findings"])
        self.assertTrue(warning_found, "AI 复核未能触发 22kW 电缆偏小预警")

        # 2. 测试一键全部核对通过 (resolve_all)
        resp_all = self.client.post(f"/api/jobs/{test_id}/resolve_all")
        self.assertEqual(resp_all.status_code, 200)
        updated_data = resp_all.json()
        uncertainties = updated_data.get("data", {}).get("uncertainties", [])
        self.assertTrue(all(u.get("resolved") for u in uncertainties), "未能将所有存疑项标记为已解决")

        # 3. 验证此时导出接口可成功返回 Excel
        resp_excel = self.client.get(f"/api/jobs/{test_id}/excel")
        self.assertEqual(resp_excel.status_code, 200)
        self.assertIn("application/vnd.openxmlformats-officedocument", resp_excel.headers["content-type"])

        # 4. 存疑未确认完导出必须 409（force=true 才放行）
        test_id2 = "test_export409_" + uuid.uuid4().hex[:6]
        jobs[test_id2] = {
            "job_id": test_id2,
            "filename": "测试图纸.dwg",
            "status": "done",
            "summary": {"title": "测试工程配电箱", "uncertainties": []},
            "data": {
                "boxes": [{"code": "1AL1", "name": "配电箱", "quantity": 1}],
                "circuits": [],
                "components": [],
                "requirements": [],
                "uncertainties": [
                    {"location": "WL1", "detail": "电缆敷设方式未注明确切管径", "resolved": False},
                ],
            },
            "changes": []
        }
        save_job(test_id2)
        resp_409 = self.client.get(f"/api/jobs/{test_id2}/excel")
        self.assertEqual(resp_409.status_code, 409, "有未确认存疑时导出应返回 409")
        self.assertEqual(resp_409.json()["detail"]["unresolved_count"], 1)
        resp_force = self.client.get(f"/api/jobs/{test_id2}/excel?force=true")
        self.assertEqual(resp_force.status_code, 200, "force=true 时应放行导出")


if __name__ == "__main__":
    unittest.main()

