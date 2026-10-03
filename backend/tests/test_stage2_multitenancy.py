# -*- coding: utf-8 -*-
"""第二阶段深度验收单测：多租户运行底座、租户物理/逻辑隔离、任务落盘与中断自动恢复。"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
import db
import store


class MultiTenancyIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_workdir = app.WORKDIR
        app.WORKDIR = self.temp_dir
        self.orig_data_dir = store.DATA_DIR
        store.DATA_DIR = self.temp_dir
        db.init_db()
        self.client = TestClient(app.app)
        app.jobs.clear()

    def tearDown(self):
        app.WORKDIR = self.orig_workdir
        store.DATA_DIR = self.orig_data_dir
        db.set_current_tenant("default", "admin_default")
        app.jobs.clear()

    def test_tenant_job_access_isolation(self):
        """验证租户 A 创建的任务，租户 B 访问全量接口均被 403 严格拦截。"""
        job_id = "job_tenant_alpha_01"
        job_data = {
            "job_id": job_id,
            "tenant_id": "tenant_alpha",
            "user_id": "user_alpha",
            "project": "Alpha工程",
            "filename": "图纸A.pdf",
            "status": "done",
            "progress": 100,
            "data": {
                "boxes": [{"code": "AL-1", "name": "照明箱"}],
                "circuits": [{"circuit_no": "WL1", "load_name": "照明", "breaker": "C16/1P"}],
                "components": [{"name": "微型断路器", "spec": "C16/1P", "quantity": 1}],
                "requirements": [],
                "uncertainties": [],
            },
            "summary": {"title": "Alpha照明箱", "boxes": 1, "circuits": 1, "components": 1},
            "changes": [],
        }
        app.jobs[job_id] = job_data
        app.save_job(job_id)

        # 1. 租户 Alpha 访问自己的任务 -> 200
        headers_alpha = {"X-Tenant-ID": "tenant_alpha"}
        r = self.client.get(f"/api/jobs/{job_id}", headers=headers_alpha)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["project"], "Alpha工程")

        # 2. 租户 Beta 试图读取任务详情 -> 403 Forbidden
        headers_beta = {"X-Tenant-ID": "tenant_beta"}
        r_beta = self.client.get(f"/api/jobs/{job_id}", headers=headers_beta)
        self.assertEqual(r_beta.status_code, 403)
        self.assertIn("无权访问其他租户", r_beta.json()["detail"])

        # 3. 租户 Beta 试图导出 Excel -> 403 Forbidden
        r_excel = self.client.get(f"/api/jobs/{job_id}/excel", headers=headers_beta)
        self.assertEqual(r_excel.status_code, 403)

        # 4. 租户 Beta 试图获取图纸渲染页 -> 403 Forbidden
        r_page = self.client.get(f"/api/jobs/{job_id}/page/1", headers=headers_beta)
        self.assertEqual(r_page.status_code, 403)

        # 5. 租户 Beta 试图修改数据 -> 403 Forbidden
        r_put = self.client.put(f"/api/jobs/{job_id}/data", json={"boxes": []}, headers=headers_beta)
        self.assertEqual(r_put.status_code, 403)

        # 6. 租户 Beta 试图发起 AI 对话变更 -> 403 Forbidden
        r_chat = self.client.post(f"/api/jobs/{job_id}/chat", json={"message": "改成2P开关"}, headers=headers_beta)
        self.assertEqual(r_chat.status_code, 403)

        # 7. 租户 Beta 试图一键放行存疑 -> 403 Forbidden
        r_res = self.client.post(f"/api/jobs/{job_id}/resolve_all", headers=headers_beta)
        self.assertEqual(r_res.status_code, 403)

        # 8. 租户 Beta 试图调用 AI 深度复核 -> 403 Forbidden
        r_rev = self.client.post(f"/api/jobs/{job_id}/ai_review", headers=headers_beta)
        self.assertEqual(r_rev.status_code, 403)

        # 9. 租户 Beta 试图重命名图块 -> 403 Forbidden
        r_rename = self.client.post(f"/api/jobs/{job_id}/rename_sheet", json={"page": 1, "name": "黑客箱"}, headers=headers_beta)
        self.assertEqual(r_rename.status_code, 403)

        # 10. 租户 Beta 试图获取平替方案 -> 403 Forbidden
        r_rep = self.client.get(f"/api/jobs/{job_id}/replacements", headers=headers_beta)
        self.assertEqual(r_rep.status_code, 403)

    def test_list_jobs_and_projects_isolation(self):
        """验证任务列表与项目列表按租户严格隔离，相互不可见。"""
        # Alpha 创建一个任务
        job_a = {
            "job_id": "job_a",
            "tenant_id": "tenant_alpha",
            "project": "Alpha商业广场",
            "filename": "A.pdf",
            "status": "done",
            "summary": {"boxes": 1, "circuits": 2, "components": 3},
            "changes": [],
        }
        app.jobs["job_a"] = job_a
        app.save_job("job_a")

        # Beta 创建一个任务
        job_b = {
            "job_id": "job_b",
            "tenant_id": "tenant_beta",
            "project": "Beta智能工厂",
            "filename": "B.pdf",
            "status": "done",
            "summary": {"boxes": 2, "circuits": 4, "components": 6},
            "changes": [],
        }
        app.jobs["job_b"] = job_b
        app.save_job("job_b")

        # 1. 租户 Alpha 查任务列表 -> 只有 job_a
        res_a = self.client.get("/api/jobs", headers={"X-Tenant-ID": "tenant_alpha"}).json()
        job_ids_a = [j["job_id"] for j in res_a["jobs"]]
        self.assertIn("job_a", job_ids_a)
        self.assertNotIn("job_b", job_ids_a)

        # 2. 租户 Beta 查任务列表 -> 只有 job_b
        res_b = self.client.get("/api/jobs", headers={"X-Tenant-ID": "tenant_beta"}).json()
        job_ids_b = [j["job_id"] for j in res_b["jobs"]]
        self.assertIn("job_b", job_ids_b)
        self.assertNotIn("job_a", job_ids_b)

        # 3. 租户 Alpha 查项目列表 -> 只有 Alpha商业广场
        proj_a = self.client.get("/api/projects", headers={"X-Tenant-ID": "tenant_alpha"}).json()
        pnames_a = [p["name"] for p in proj_a["projects"]]
        self.assertIn("Alpha商业广场", pnames_a)
        self.assertNotIn("Beta智能工厂", pnames_a)

        # 4. 租户 Beta 试图调取 Alpha 的项目 BOM -> 404
        bom_res = self.client.get("/api/projects/Alpha商业广场/bom", headers={"X-Tenant-ID": "tenant_beta"})
        self.assertEqual(bom_res.status_code, 404)

    def test_in_progress_job_persistence_and_startup_recovery(self):
        """验证进行中任务（extracting/rendering等）实时落盘，服务重启后自动标记为 interrupted 可恢复。"""
        job_id = "job_ongoing_001"
        app.jobs[job_id] = {
            "job_id": job_id,
            "tenant_id": "default",
            "status": "extracting",
            "progress": 65,
            "filename": "超高层供电图纸.pdf",
            "changes": [],
        }
        # 实时落盘
        app.save_job(job_id)

        # 从 SQLite 验证：进行中状态已持久化入库（解决老版本内存丢失缺陷）
        saved = db.db_get_job(job_id)
        self.assertIsNotNone(saved)
        self.assertEqual(saved["status"], "extracting")
        self.assertEqual(saved["progress"], 65)

        # 清空内存（模拟服务进程退出/重启冷启动）
        app.jobs.clear()

        # 执行冷启动恢复扫描
        recovered_count = db.db_recover_interrupted_jobs()
        self.assertGreaterEqual(recovered_count, 1)

        # 再次请求该任务接口：状态应变为 interrupted，明确告知用户可就地重跑
        r = self.client.get(f"/api/jobs/{job_id}")
        self.assertEqual(r.status_code, 200)
        info = r.json()
        self.assertEqual(info["status"], "interrupted")
        self.assertIn("意外中断", info["error"])

    def test_tenant_ai_cost_and_tokens_isolation(self):
        """验证同一项目名称在不同租户下的 AI 费用与 Token 流水彼此严格独立记账。"""
        # 租户 1 消耗 10,000 Token
        db.set_current_tenant("tenant_1")
        store.record_project_ai_usage("数据中心二期", {
            "model": "deepseek-chat",
            "prompt_tokens": 8000,
            "completion_tokens": 2000,
            "total_tokens": 10000,
            "cost_in": 0.008,
            "cost_out": 0.004,
            "total_cost": 0.012,
            "currency": "￥",
            "logs": [{"prompt_tokens": 8000}],
        }, job_id="job_t1", filename="DC_A.pdf")

        # 租户 2 在同名项目下消耗 3,000 Token
        db.set_current_tenant("tenant_2")
        store.record_project_ai_usage("数据中心二期", {
            "model": "deepseek-chat",
            "prompt_tokens": 2000,
            "completion_tokens": 1000,
            "total_tokens": 3000,
            "cost_in": 0.002,
            "cost_out": 0.002,
            "total_cost": 0.004,
            "currency": "￥",
            "logs": [{"prompt_tokens": 2000}],
        }, job_id="job_t2", filename="DC_B.pdf")

        # 验证租户 1 查看到的仅有 10000 Token
        db.set_current_tenant("tenant_1")
        logs_t1 = store.get_project_ai_logs("数据中心二期")
        self.assertEqual(logs_t1["ai_tokens_total"], 10000)
        self.assertAlmostEqual(logs_t1["ai_cost_total"], 0.012, places=4)
        self.assertEqual(len(logs_t1["ai_logs"]), 1)
        self.assertEqual(logs_t1["ai_logs"][0]["job_id"], "job_t1")

        # 验证租户 2 查看到的仅有 3000 Token
        db.set_current_tenant("tenant_2")
        logs_t2 = store.get_project_ai_logs("数据中心二期")
        self.assertEqual(logs_t2["ai_tokens_total"], 3000)
        self.assertAlmostEqual(logs_t2["ai_cost_total"], 0.004, places=4)
        self.assertEqual(len(logs_t2["ai_logs"]), 1)
        self.assertEqual(logs_t2["ai_logs"][0]["job_id"], "job_t2")


if __name__ == "__main__":
    unittest.main()
