# -*- coding: utf-8 -*-
"""一条 DXF 走完任务入口后，读取盘点、实体身份和放行结论都留在任务上。"""
import os
import shutil
import tempfile
import unittest

import test_support  # noqa: F401
import ezdxf

import app
import db


class JobReleasePathTests(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dxf_job_keeps_inventory_identity_and_release_checks(self):
        dxf_path = os.path.join(self.temp_dir, "panel.dxf")
        doc = ezdxf.new("R2010")
        doc.header["$INSUNITS"] = 4
        msp = doc.modelspace()
        header = msp.add_text("9KX3 照明配电箱", dxfattribs={"insert": (0, 0), "height": 200})
        msp.add_text("备注：柜内配置铜排", dxfattribs={"insert": (0, 400), "height": 200})
        msp.add_text("WL1", dxfattribs={"insert": (0, -800), "height": 200})
        breaker = msp.add_text("MCB-C16A/1P", dxfattribs={"insert": (1500, -800), "height": 200})
        msp.add_text("走廊照明", dxfattribs={"insert": (3000, -800), "height": 200})
        msp.add_text("WL9", dxfattribs={"insert": (0, -1600), "height": 200})
        msp.add_text("备用", dxfattribs={"insert": (3000, -1600), "height": 200})
        doc.saveas(dxf_path)

        job_id = "job_release_path"
        app.jobs[job_id] = {
            "job_id": job_id,
            "tenant_id": "default",
            "user_id": "tester",
            "status": "queued",
            "filename": "panel.dxf",
        }
        app.process_drawing_file(job_id, dxf_path, "panel.dxf")
        job = app.jobs[job_id]
        self.assertEqual(job.get("status"), "done", job.get("error"))

        report = job["summary"]["read_report"]
        self.assertIsNone(report["coverage_rate"])
        self.assertEqual(report["status"], "supported")
        self.assertTrue(report["file_sha256"])

        checks = {item["kind"]: item for item in job["summary"]["release_checks"]}
        self.assertEqual(set(checks), {"quantity", "spare", "material"})
        self.assertFalse(checks["quantity"]["ok"])
        self.assertFalse(checks["spare"]["ok"])
        self.assertFalse(checks["material"]["ok"])
        self.assertEqual(job["data"]["release_checks"], job["summary"]["release_checks"])
        self.assertFalse(job["summary"]["bom_release"]["project_total_released"])

        store = job["raw"]["evidence_store"]
        native = [item for item in store.values() if item.get("origin") == "cad_native"]
        handles = {item.get("handle") for item in native}
        self.assertIn(str(header.dxf.handle), handles)
        self.assertIn(str(breaker.dxf.handle), handles)
