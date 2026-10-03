# -*- coding: utf-8 -*-
"""29 项修复回归测试（app.py / app.js 导出门禁与存疑消除部分）。

覆盖：
- P0-8：ai_deep_review 启发式只消除"断路器/开关规格"主题 + 回路编号精确匹配
- P0-9：AI 路径 resolved_targets 精确匹配；"全部"二字不再消除所有
- P1-1：导出 409 门禁（已有用例在 test_v3_features，这里补 force=false 默认行为）
- P2-1：Uncertainty.from_text / _load_from_xlsx 识别"（已确认）"前缀
- P2-4：crop 接口归属校验
- P2-5：job_id 非法字符 400
"""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from app import app as fastapi_app, jobs, save_job, _load_from_xlsx, _validate_job_id
from extractor.schema import Uncertainty


def _mk_job(uncertainties, circuits=None):
    test_id = "test29_" + uuid.uuid4().hex[:8]
    jobs[test_id] = {
        "job_id": test_id,
        "filename": "测试图纸.pdf",
        "status": "done",
        "summary": {"title": "测试工程", "uncertainties": []},
        "data": {
            "boxes": [{"code": "1AL1", "name": "配电箱", "quantity": 1}],
            "circuits": circuits or [],
            "components": [],
            "requirements": [],
            "uncertainties": uncertainties,
        },
        "changes": [],
    }
    return test_id


class ExportGateTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app)

    def test_excel_409_默认拦截(self):
        tid = _mk_job([{"location": "WL1", "detail": "电缆敷设方式不明", "resolved": False}])
        r = self.client.get(f"/api/jobs/{tid}/excel")
        self.assertEqual(r.status_code, 409)
        body = r.json()["detail"]
        self.assertEqual(body["unresolved_count"], 1)
        self.assertEqual(body["unresolved"][0]["location"], "WL1")

    def test_excel_无存疑正常导出(self):
        tid = _mk_job([{"location": "WL1", "detail": "电缆敷设方式不明", "resolved": True}])
        r = self.client.get(f"/api/jobs/{tid}/excel")
        self.assertEqual(r.status_code, 200)

    def test_job_id_非法字符_400(self):
        for bad in ["..", "a/b", "a b", "a$b", "", "x" * 65]:
            with self.assertRaises(Exception, msg=f"job_id={bad!r} 应被拒绝"):
                _validate_job_id(bad)
        self.assertEqual(_validate_job_id("abc-123_X"), "abc-123_X")

    def test_crop_归属校验(self):
        tid = _mk_job([])
        # 不属于该任务的 crop 文件名 -> 404
        r = self.client.get(f"/api/jobs/{tid}/crop/otherjob_crop_abcd123456.png")
        self.assertEqual(r.status_code, 404)
        # 非法文件名 -> 400
        r = self.client.get(f"/api/jobs/{tid}/crop/.._x.png")
        self.assertEqual(r.status_code, 400)


class AiDeepReviewTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app)

    def test_启发式_非断路器主题不消除(self):
        """P0-8：存疑问的是电缆敷设，即使回路断路器规格完整也不消除。"""
        tid = _mk_job(
            [
                {"location": "WL1", "detail": "电缆敷设方式未注明确切管径", "resolved": False},
                {"location": "WL1", "detail": "微型断路器规格需人工核验", "resolved": False},
            ],
            circuits=[{"circuit_no": "WL1", "box": "1AL1", "breaker": "C65N-C16/1P"}],
        )
        r = self.client.post(f"/api/jobs/{tid}/ai_review")
        self.assertEqual(r.status_code, 200)
        us = r.json()["data"]["uncertainties"]
        cable = next(u for u in us if "电缆敷设" in u["detail"])
        brk = next(u for u in us if "断路器规格" in u["detail"])
        self.assertFalse(cable["resolved"], "非断路器主题的存疑不应被启发式消除")
        self.assertTrue(brk["resolved"], "断路器主题+规格完整的应消除")

    def test_启发式_回路编号精确匹配(self):
        """P0-8：WL1 不应命中 WL10 的存疑。"""
        tid = _mk_job(
            [{"location": "WL10", "detail": "断路器规格需人工核验", "resolved": False}],
            circuits=[
                {"circuit_no": "WL1", "box": "1AL1", "breaker": ""},  # 旧逻辑会先命中它
                {"circuit_no": "WL10", "box": "1AL1", "breaker": "C65N-C16/1P"},
            ],
        )
        r = self.client.post(f"/api/jobs/{tid}/ai_review")
        self.assertEqual(r.status_code, 200)
        us = r.json()["data"]["uncertainties"]
        self.assertTrue(us[0]["resolved"], "WL10 应精确匹配到 WL10 回路并消除")

    def _fake_assistant(self, resolved_targets):
        class FakeAssistant:
            configured = True

            def ask(self, data, prompt, messages):
                return {"resolved_targets": resolved_targets, "reasons": {},
                        "engineering_advice": []}

        return FakeAssistant

    def test_AI路径_精确匹配(self):
        """P0-9：target 必须精确相等；子串不得消除 WL10。"""
        tid = _mk_job(
            [
                {"location": "WL1", "detail": "断路器规格需核验", "resolved": False},
                {"location": "WL10", "detail": "断路器规格需核验", "resolved": False},
            ],
            circuits=[{"circuit_no": "WL1", "box": "1AL1", "breaker": "C65N-C16/1P"}],
        )
        with patch.object(app, "Assistant", self._fake_assistant(["WL1"])):
            r = self.client.post(f"/api/jobs/{tid}/ai_review")
        self.assertEqual(r.status_code, 200)
        us = {u["location"]: u["resolved"] for u in r.json()["data"]["uncertainties"]}
        self.assertTrue(us["WL1"])
        self.assertFalse(us["WL10"], "子串 WL1 不得消除 WL10 的存疑")

    def test_AI路径_全部二字不再消除所有(self):
        """P0-9：AI 回复含"全部"也不得批量消除。"""
        tid = _mk_job(
            [{"location": "WL1", "detail": "电缆敷设方式不明", "resolved": False}],
            circuits=[{"circuit_no": "WL1", "box": "1AL1", "breaker": "C65N-C16/1P"}],
        )
        with patch.object(app, "Assistant", self._fake_assistant(["全部回路已复核通过"])):
            r = self.client.post(f"/api/jobs/{tid}/ai_review")
        self.assertEqual(r.status_code, 200)
        us = r.json()["data"]["uncertainties"]
        self.assertFalse(us[0]["resolved"], "'全部'二字不得消除所有存疑")


class ResolvedPrefixTests(unittest.TestCase):
    def test_from_text_已确认前缀(self):
        u = Uncertainty.from_text("（已确认）WL1：断路器规格")
        self.assertEqual(u.location, "WL1")
        self.assertEqual(u.detail, "断路器规格")
        self.assertTrue(u.resolved)
        u2 = Uncertainty.from_text("WL2：电缆敷设方式")
        self.assertFalse(u2.resolved)

    def test_load_from_xlsx_已确认恢复(self):
        import openpyxl
        tmpdir = tempfile.mkdtemp()
        job_id = "xlsx29_" + uuid.uuid4().hex[:8]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "技术要求与报价说明"
        ws.append(["序号", "项目", "内容"])
        ws.append([])
        ws.append([])
        ws.append([1, "待人工核对项", "（已确认）WL1：断路器规格；WL2：电缆敷设方式"])
        xlsx_path = os.path.join(tmpdir, f"{job_id}.xlsx")
        wb.save(xlsx_path)
        with patch.object(app, "WORKDIR", tmpdir):
            job = _load_from_xlsx(job_id)
        self.assertIsNotNone(job)
        us = {u["location"]: u["resolved"] for u in job["data"]["uncertainties"]}
        self.assertIn("WL1", us)
        self.assertTrue(us["WL1"], "（已确认）前缀的存疑回读后应 resolved=True")
        self.assertIn("WL2", us)
        self.assertFalse(us["WL2"])


class ReparseTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app)

    def test_reparse_not_found(self):
        tid = "not_exist_job"
        r = self.client.post(f"/api/jobs/{tid}/reparse")
        self.assertEqual(r.status_code, 404)

    def test_reparse_success(self):
        import app
        tid = "test_reparse_" + uuid.uuid4().hex[:6]
        # 在 WORKDIR 下建一个临时源文件
        pdf_file = os.path.join(app.WORKDIR, f"{tid}.pdf")
        with open(pdf_file, "wb") as f:
            f.write(b"%PDF-1.4 test")
        try:
            with patch("app.process_drawing_file") as mock_proc:
                r = self.client.post(f"/api/jobs/{tid}/reparse")
                self.assertEqual(r.status_code, 200)
                data = r.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["job_id"], tid)
                self.assertEqual(jobs[tid]["status"], "queued")
        finally:
            if os.path.exists(pdf_file):
                os.remove(pdf_file)
            jobs.pop(tid, None)

    def test_batch_set_project(self):
        tid1 = _mk_job([])
        tid2 = _mk_job([])
        r = self.client.post("/api/jobs/batch_set_project", json={
            "job_ids": [tid1, tid2],
            "project": "工业厂房集中分析测试项目"
        })
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["updated"], 2)
        self.assertEqual(jobs[tid1]["project"], "工业厂房集中分析测试项目")
        self.assertEqual(jobs[tid2]["project"], "工业厂房集中分析测试项目")


class SecurityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(fastapi_app)

    def test_ssrf_model_url_blocked(self):
        from extractor.vision import is_safe_model_url
        bad_urls = [
            "http://127.0.0.1:8000/v1",
            "http://localhost:11434/v1",
            "http://10.0.0.5:8080/v1",
            "http://192.168.1.1/v1",
            "http://169.254.169.254/latest/meta-data",
            "ftp://api.deepseek.com",
        ]
        for u in bad_urls:
            safe, reason = is_safe_model_url(u)
            self.assertFalse(safe, f"应该拦截恶意地址: {u}, 但返回了成功: {reason}")

        good_urls = [
            "https://api.deepseek.com/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://api.openai.com/v1",
        ]
        for u in good_urls:
            safe, _ = is_safe_model_url(u)
            self.assertTrue(safe, f"合法公网地址应该放行: {u}")

    def test_put_settings_ssrf_intercepted(self):
        r = self.client.put("/api/settings", json={"vision_base_url": "http://127.0.0.1:8000/v1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("非法的模型服务地址", r.json()["detail"])

    def test_lock_settings(self):
        import os
        os.environ["LOCK_SETTINGS"] = "1"
        try:
            r = self.client.put("/api/settings", json={"temperature": 0.5})
            self.assertEqual(r.status_code, 403)
            self.assertIn("系统配置已由管理员强制锁定", r.json()["detail"])
        finally:
            os.environ.pop("LOCK_SETTINGS", None)

    def test_excel_formula_injection_sanitized(self):
        from extractor.excel import sanitize_excel_value
        malicious = [
            "=cmd|' /C calc'!A0",
            "+1+1",
            "-2+3",
            "@SUM(1,2)",
            "\t=1+1",
        ]
        for m in malicious:
            sanitized = sanitize_excel_value(m)
            self.assertTrue(str(sanitized).startswith("'"), f"{m} 未被安全转义为以单引号开头: {sanitized}")

        # 合法内部公式放行
        self.assertEqual(sanitize_excel_value("=SUM(A1:A5)"), "=SUM(A1:A5)")
        self.assertEqual(sanitize_excel_value("=ROUND(G10*2, 2)"), "=ROUND(G10*2, 2)")


if __name__ == "__main__":
    unittest.main()


