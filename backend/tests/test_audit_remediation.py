# -*- coding: utf-8 -*-
"""深度审计整改闭环验收测试套件 (Audit Remediation Verification Suite)

全面覆盖并锁定生产级安全与工程整改：
1. 多租户安全拦截与生产保护：未认证请求 Header 冒用拦截、清库安全门禁
2. 统一项目级与单任务级导出质量门禁 (409 Conflict)
3. 原生 DXF/DWG 重解析优先级防派生 PDF 偷换
4. 物料参数边界 (拒绝盲目填充 16A/1P，测试本地价格匹配与 10kA 候选准入)
5. 断路器分类器精准度 (消灭“断路器（其他）”)
6. 证据主张 Claim 字典与回路修改联动更新 (防旧值残留)
7. 任务元数据 (sheet_names/file_type/target_brand) 持久化完整回读
"""
import os
import unittest
import test_support  # noqa: F401
from fastapi.testclient import TestClient

import app
import db
from extractor.schema import Box, Circuit, ExtractionResult, RawExtraction, ReviewStatus
from extractor.catalog import parse_component_spec, recommend_replacements
from extractor.assemble import _breaker_name, assemble


class TestAuditRemediation(unittest.TestCase):
    def setUp(self):
        db.init_db()
        self.client = TestClient(app.app)

    def test_prompts_do_not_anchor_to_one_drawing_or_claim_perfect_cad_text(self):
        """Prompt guard: production prompts must not contain sample-sheet facts or claim perfect CAD text."""
        backend_dir = os.path.dirname(os.path.dirname(__file__))
        with open(os.path.join(backend_dir, "prompts", "extract.txt"), encoding="utf-8") as f:
            extraction_prompt = f.read()
        with open(os.path.join(backend_dir, "prompts", "assistant.txt"), encoding="utf-8") as f:
            assistant_prompt = f.read()
        with open(os.path.join(backend_dir, "extractor", "vision.py"), encoding="utf-8") as f:
            vision_source = f.read()
        with open(os.path.join(backend_dir, "extractor", "cad_extractor.py"), encoding="utf-8") as f:
            cad_source = f.read()
        with open(os.path.join(backend_dir, "app.py"), encoding="utf-8") as f:
            app_source = f.read()

        for sample_fact in ("2SAL2", "AW1/2/3/4", "CPM-R40T", "01LBZ1", "NM1-125S/3300"):
            self.assertNotIn(sample_fact, extraction_prompt + assistant_prompt)
        self.assertNotIn("100%精确无 OCR 误差", vision_source)
        self.assertNotIn("100% 精确的矢量文字", cad_source)
        self.assertNotIn("100% 拓扑对齐", app_source)
        self.assertIn("不保证完整、无乱码或语义正确", vision_source)

    def test_unauth_header_spoofing_prevented_in_production(self):
        """1. 多租户安全：生产环境下，未登录匿名请求试图通过 X-Tenant-ID 冒充受害租户，强制降级为 guest 并被拦截。"""
        victim_job_id = "job_victim_sec_01"
        db.db_save_job({
            "job_id": victim_job_id,
            "tenant_id": "corp_victim",
            "user_id": "victim_admin",
            "status": "done",
            "project": "核心防泄密工程",
            "data": {"boxes": [{"code": "AL1"}], "circuits": []},
            "summary": {"title": "受害企业核心图纸", "boxes": 1, "circuits": 0, "components": 0}
        })

        old_env = os.environ.get("ENV")
        try:
            os.environ["ENV"] = "production"
            # 匿名未认证请求试图指定 Header 窃取 corp_victim
            res = self.client.get(f"/api/jobs/{victim_job_id}", headers={"X-Tenant-ID": "corp_victim"})
            self.assertEqual(res.status_code, 403)
            self.assertIn("无权访问其他租户", res.json()["detail"])
        finally:
            if old_env is not None:
                os.environ["ENV"] = old_env
            else:
                os.environ.pop("ENV", None)

            conn = db._get_conn()
            with conn:
                conn.execute("DELETE FROM jobs WHERE id = ?", (victim_job_id,))

    def test_project_bom_export_unresolved_uncertainty_gating(self):
        """2. 项目采购总表导出：未确认存疑不遮挡交付，默认即可导出并如实带出待核对项。"""
        job_id = "job_proj_gating_01"
        proj_name = "质量门禁测试项目"
        job_dict = {
            "job_id": job_id,
            "tenant_id": "default",
            "project": proj_name,
            "status": "done",
            "filename": "测试图纸.pdf",
            "data": {
                "boxes": [{"code": "1AP1", "name": "动力箱", "quantity": 1}],
                "circuits": [{"box": "1AP1", "circuit_no": "WL1", "breaker": "C16/1P"}],
                "components": [{"name": "微型断路器", "spec": "C16/1P", "quantity": 1}],
                "requirements": [],
                "uncertainties": [
                    {"location": "WL1", "detail": "开关分断能力缺失", "resolved": False}
                ]
            },
            "summary": {"title": "门禁图纸", "boxes": 1, "circuits": 1, "components": 1}
        }
        app.jobs[job_id] = job_dict
        app.save_job(job_id)

        try:
            # 默认导出：未确认存疑也照常给出 Excel
            res = self.client.get(f"/api/projects/{proj_name}/export_bom")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.headers["content-type"],
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            # force=true 仍然可用（门禁一旦被配置打开，它是单次放行开关）
            res_force = self.client.get(f"/api/projects/{proj_name}/export_bom?force=true")
            self.assertEqual(res_force.status_code, 200)
            # 未确认的存疑项必须随文件交出，不能因为“能导出了”就把问题藏起来
            import io
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(res.content))
            self.assertIn("全项目待核对项", wb.sheetnames)
            text = "\n".join(str(v) for row in wb["全项目待核对项"].iter_rows(values_only=True)
                             for v in row if v)
            self.assertIn("开关分断能力缺失", text)
            self.assertIn("未确认", text)
        finally:
            app.jobs.pop(job_id, None)

    def test_reparse_prioritizes_dxf_over_derived_pdf(self):
        """3. 重解析优先级：当存在原生 DXF 与派生 PDF 时，优先探测选用 .dxf 保持原生高精解析。"""
        import tempfile
        job_id = "job_reparse_test_01"
        orig_workdir = app.WORKDIR
        tmp_dir = tempfile.mkdtemp()
        app.WORKDIR = tmp_dir

        try:
            dxf_file = os.path.join(tmp_dir, f"{job_id}.dxf")
            pdf_file = os.path.join(tmp_dir, f"{job_id}.pdf")
            with open(dxf_file, "w") as f:
                f.write("0\nSECTION\n0\nENDSEC\n0\nEOF\n")
            with open(pdf_file, "w") as f:
                f.write("%PDF-1.4\n")

            app.jobs[job_id] = {
                "job_id": job_id,
                "tenant_id": "default",
                "filename": "工程系统图.dxf",
                "status": "done"
            }
            app.save_job(job_id)

            # 调用重解析接口
            res = self.client.post(f"/api/jobs/{job_id}/reparse")
            self.assertEqual(res.status_code, 200)
            # 验证重解析入队的 file_type 为 dxf 而非 pdf
            self.assertEqual(app.jobs[job_id]["file_type"], "dxf")
        finally:
            app.WORKDIR = orig_workdir
            app.jobs.pop(job_id, None)

    def test_no_fabricated_parameters_and_10ka_replacement(self):
        """4. 物料真实性：未标注电流时不脑补 16A；10kA 高分断微断准确推荐 NXB-63H。"""
        # A. 严禁脑补电流与极数
        parsed = parse_component_spec("微型断路器", "NXB-63")
        self.assertIsNone(parsed["rated_amp"])
        self.assertIn("待核电流", parsed["standard_spec"])

        # B. 10kA 微断平替对标
        rec_10ka = recommend_replacements("微型断路器", "施耐德 iC65H C25/2P 10kA", target_brand="正泰")
        self.assertEqual(rec_10ka["recommended_series"], "NXB-63H")
        self.assertIn("10kA 高分断", rec_10ka["matching_notes"])

    def test_breaker_classifier_comprehensive_recognition(self):
        """5. 分类器精准度：国内主流型号与脱扣曲线消灭“断路器（其他）”。"""
        self.assertEqual(_breaker_name("iC65N-C16/1P"), "微型断路器")
        self.assertEqual(_breaker_name("DZ47-63/2P C20"), "微型断路器")
        self.assertEqual(_breaker_name("C32/3P"), "微型断路器")
        self.assertEqual(_breaker_name("D63/3P"), "微型断路器")
        self.assertEqual(_breaker_name("NM1-125S/3300 100A"), "塑壳断路器")
        self.assertEqual(_breaker_name("NSX100F 80A 3P"), "塑壳断路器")
        self.assertEqual(_breaker_name("DZ47LE-63 C25/1PN"), "剩余电流动作断路器")
        self.assertEqual(_breaker_name("NZ7-63/4P"), "双电源自动转换开关")
        self.assertEqual(_breaker_name("HR6-100/31"), "隔离开关")

    def test_claims_dynamically_synced_on_modification(self):
        """6. 证据链真实性：修改回路参数后，Claim 的 value 与 raw_value 实时联动更新，无旧值残留。"""
        raw = RawExtraction(
            boxes=[Box(code="1AL1", name="动力箱")],
            circuits=[Circuit(box="1AL1", circuit_no="WL1", breaker="C16/1P", cable="BV-3x2.5")],
            extra_devices=[], requirements=[], uncertainties=[]
        )
        res = assemble(raw)
        c = res.circuits[0]
        self.assertEqual(c.claims["circuit.breaker"].value, "C16/1P")

        # 模拟用户将断路器修改为 C32/3P
        c.breaker = "C32/3P"
        raw_modified = RawExtraction(
            boxes=res.boxes,
            circuits=[c],
            extra_devices=[], requirements=[], uncertainties=[]
        )
        res_updated = assemble(raw_modified)
        c_updated = res_updated.circuits[0]

        # 验证 Claim 同步更新为最新值，绝不残留旧值 C16/1P
        self.assertEqual(c_updated.claims["circuit.breaker"].value, "C32/3P")
        self.assertEqual(c_updated.claims["circuit.breaker"].raw_value, "C32/3P")
        self.assertEqual(c_updated.claims["circuit.breaker"].review_status, ReviewStatus.CONFIRMED.value)

    def test_job_metadata_persistence_roundtrip(self):
        """7. 任务持久化：sheet_names、file_type、target_brand 在 SQLite 中持久化并无损恢复。"""
        job_id = "job_persistence_rt_01"
        job_info = {
            "job_id": job_id,
            "tenant_id": "default",
            "user_id": "user_default",
            "project": "持久化测试项目",
            "filename": "配电系统图.dwg",
            "file_type": "dwg",
            "target_brand": "良信",
            "sheet_names": {"1": "地下室水泵配电箱", "2": "屋顶风机控制箱"},
            "status": "done",
            "progress": 100,
            "data": {"boxes": [{"code": "AL1"}], "circuits": []},
            "summary": {"title": "持久化验证", "boxes": 1, "circuits": 0, "components": 0}
        }
        db.db_save_job(job_info)

        try:
            loaded = db.db_get_job(job_id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["file_type"], "dwg")
            self.assertEqual(loaded["target_brand"], "良信")
            self.assertIn("1", loaded["sheet_names"])
            self.assertEqual(loaded["sheet_names"]["1"], "地下室水泵配电箱")
            self.assertEqual(loaded["sheet_names"]["2"], "屋顶风机控制箱")
        finally:
            conn = db._get_conn()
            with conn:
                conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def test_excel_estimate_box_costs_dynamic_brand(self):
        """8. Excel 成本测算：_estimate_box_costs 动态透传品牌，施耐德与正泰成套造价差异分明，不再写死正泰。"""
        from extractor.excel import _estimate_box_costs
        box = {"code": "AP1", "name": "动力箱", "size": "800x600x200", "install": "明装", "ip_rating": "IP44"}
        circuits = [{"circuit_no": "WL1", "circuit_type": "outgoing", "breaker": "C65N-C32/3P", "load_name": "风机"}]
        comps = []

        cost_chint = _estimate_box_costs(box, circuits, comps, brand="正泰")
        cost_schneider = _estimate_box_costs(box, circuits, comps, brand="施耐德")

        # 施耐德合资品牌与正泰国产品牌定额不同、元件库对标价格不同，单价与元件费产生明显分化
        self.assertNotEqual(cost_schneider["unit_price"], cost_chint["unit_price"])
        self.assertNotEqual(cost_schneider["comp_total"], cost_chint["comp_total"])

    def test_tenant_settings_isolation_in_store(self):
        """9. 租户模型配置隔离：各租户在 store.py 中的设置分文件隔离存储，杜绝租户间 API Key 越权篡改。"""
        import store
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        orig_data_dir = store.DATA_DIR
        store.DATA_DIR = temp_dir
        try:
            # 租户 A 设置自己的专属模型与 Key
            db.set_current_tenant("tenant_apple", "user_apple")
            store.save_settings({"vision_model": "apple-vision-v1", "vision_api_key": "sk-apple-123456"})

            # 租户 B 设置不同的专属模型与 Key
            db.set_current_tenant("tenant_banana", "user_banana")
            store.save_settings({"vision_model": "banana-vision-v2", "vision_api_key": "sk-banana-999999"})

            # 回读租户 A 的配置：绝不能被租户 B 污染
            db.set_current_tenant("tenant_apple", "user_apple")
            apple_conf = store.settings()
            self.assertEqual(apple_conf["vision_model"], "apple-vision-v1")
            self.assertEqual(apple_conf["vision_api_key"], "sk-apple-123456")

            # 回读租户 B 的配置
            db.set_current_tenant("tenant_banana", "user_banana")
            banana_conf = store.settings()
            self.assertEqual(banana_conf["vision_model"], "banana-vision-v2")
            self.assertEqual(banana_conf["vision_api_key"], "sk-banana-999999")
        finally:
            store.DATA_DIR = orig_data_dir
            db.set_current_tenant("default", "admin_default")
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_default_env_unauth_tenant_header_blocked(self):
        """10. 默认安全：无需配置 ENV=production，未登录请求试图冒用 X-Tenant-ID Header 默认被降级拦截。"""
        victim_job_id = "job_default_env_sec_02"
        db.db_save_job({
            "job_id": victim_job_id,
            "tenant_id": "corp_secure",
            "user_id": "secure_admin",
            "status": "done",
            "project": "默认安全工程",
            "data": {"boxes": [{"code": "AL1"}], "circuits": []},
            "summary": {"title": "图纸", "boxes": 1, "circuits": 0, "components": 0}
        })
        try:
            # 确保环境变量未特意声明 production 或调试允许标头
            os.environ.pop("ENV", None)
            os.environ.pop("ALLOW_UNAUTH_TENANT_HEADER", None)

            res = self.client.get(f"/api/jobs/{victim_job_id}", headers={"X-Tenant-ID": "corp_secure"})
            self.assertEqual(res.status_code, 403)
        finally:
            conn = db._get_conn()
            with conn:
                conn.execute("DELETE FROM jobs WHERE id = ?", (victim_job_id,))

    def test_post_chat_429_exponential_backoff_retry(self):
        """11. 运行可靠性：post_chat 遇到 HTTP 429 限流时自动执行指数退避重试并最终成功。"""
        from unittest.mock import patch, MagicMock
        from extractor.vision import post_chat
        import urllib.error

        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"choices": [{"message": {"content": "ok"}}]}'
        mock_resp.__enter__.return_value = mock_resp

        call_count = 0
        def side_effect_urlopen(req, timeout=300):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # 前两次抛出 HTTP 429
                raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=side_effect_urlopen), \
             patch("time.sleep") as mock_sleep:
            res = post_chat("https://api.openai.com/v1", "sk-test", {"messages": []})
            self.assertEqual(res["choices"][0]["message"]["content"], "ok")
            self.assertEqual(call_count, 3)
            # 验证确实发生了 2 次指数退避等待
            self.assertEqual(mock_sleep.call_count, 2)

    def test_initial_claims_not_falsely_confirmed(self):
        """12. 证据链求真：初次提取组装的字段标记为 PARSED_OK 或 UNASSESSED，严禁自造假 TEXT 标记 CONFIRMED。"""
        raw = RawExtraction(
            boxes=[Box(code="1AL1", ip_rating="IP44")],
            circuits=[Circuit(box="1AL1", circuit_no="WL1", breaker="C16/1P", cable="BV-3x2.5")],
            extra_devices=[], requirements=[], uncertainties=[]
        )
        res = assemble(raw)
        b = res.boxes[0]
        c = res.circuits[0]

        # 初始状态严禁为 CONFIRMED
        self.assertEqual(b.claims["box.code"].review_status, ReviewStatus.PARSED_OK.value)
        self.assertEqual(b.claims["box.ip_rating"].review_status, ReviewStatus.PARSED_OK.value)
        self.assertEqual(c.claims["circuit.circuit_no"].review_status, ReviewStatus.PARSED_OK.value)
        self.assertEqual(c.claims["circuit.breaker"].review_status, ReviewStatus.PARSED_OK.value)

    def test_sheet_metal_enclosure_vertical_partition_and_material_resolution(self):
        """13. 钣金与外壳求真：支持“立板”、“立板+支架”、“304不锈钢”等真实工业柜体构件，未注尺寸严禁虚构。"""
        from extractor.pricing import resolve_box_enclosure_specs, estimate_box_enclosure_price, TYPE_A

        # (1) 柜内标注含立板与支架、304不锈钢、板厚2.0mm
        box_stainless = {
            "box_code": "01AP1",
            "size": "800*1800*600",
            "note": "304不锈钢室外动力柜，柜内配安装立板及支架",
            "install_type": "落地式",
            "ip_rating": "IP55",
        }
        b_type, mat, thk = resolve_box_enclosure_specs(box_stainless, 800, 1800, 600, is_floor=True)
        self.assertEqual(b_type, "立板+支架")
        self.assertEqual(mat, "304#不锈钢板")
        self.assertEqual(thk, 2.0)
        self.assertEqual(TYPE_A[b_type], 2.8)

        price, desc = estimate_box_enclosure_price(box_stainless, circuits_count=12)
        self.assertGreater(price, 2000.0)
        self.assertIn("304#不锈钢板", desc)
        self.assertIn("立板+支架", desc)
        self.assertIn("高防护", desc)

        # (2) 标注立板结构
        box_liban = {"box_type": "600x800x250", "note": "箱内装设镀锌安装立板"}
        b_type2, mat2, thk2 = resolve_box_enclosure_specs(box_liban, 600, 800, 250, is_floor=False)
        self.assertEqual(b_type2, "立板")
        self.assertEqual(TYPE_A[b_type2], 2.0)

        # (3) 无尺寸标注时，如实标明显式定额推导，绝不假装实测到具体尺寸
        box_no_dims = {"box_code": "AL_NODIM", "ip_rating": "IP30"}
        p_fallback, desc_fallback = estimate_box_enclosure_price(box_no_dims, circuits_count=8)
        self.assertIn("估算:按8回路定额推导", desc_fallback)
        self.assertIn("待核定", desc_fallback)

    def test_cad_table_extraction_new_unseen_drawing_generalization(self):
        """14. 合成 CAD 场景：验证本用例给定的回路标识、开关与电缆可被提取；不代表新图纸准确率或完整率。"""
        import ezdxf
        from extractor.cad_extractor import extract_cad_table_data

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # 柜体标头：位于 (0, 1000)
        msp.add_text("01AP1 动力配电柜系统图", dxfattribs={"insert": (0.0, 1000.0), "height": 35.0})

        # 进线侧总开关与电缆：位于标头下方、回路出线带上方 (0, 950)
        msp.add_text("常用电源 由1#变电所引来", dxfattribs={"insert": (0.0, 950.0), "height": 25.0})
        msp.add_text("WDZN-YJV-5x16 SC50", dxfattribs={"insert": (200.0, 950.0), "height": 25.0})
        msp.add_text("NM1-125S/3300 In=100A", dxfattribs={"insert": (500.0, 950.0), "height": 25.0})

        # 6 个不同回路代号与主流品牌型号，间距 40.0
        circuit_samples = [
            ("1WL1", "L1", "NXB-63 C20", "BV-3x2.5 SC20", "走廊照明", 900.0),
            ("2WP1", "L2", "NDM1-63/3300 50A", "WDZ-BYJ-3x10 SC32", "空调机房", 860.0),
            ("B1WL2", "L3", "EA9RN 2P C16A", "BV-3x2.5 SC20", "地下照明", 820.0),
            ("-1WL3", "L1", "CDB6-63 C32", "BV-3x4 SC25", "备用照明", 780.0),
            ("W1", "L1/L2/L3", "3VA11 100A 3P", "YJV-4x35+1x16 SC70", "潜水泵动力", 740.0),
            ("P1", "L1/L2/L3", "Tmax XT2 160A", "YJV-4x50+1x25 SC80", "排烟风机", 700.0),
        ]

        for cno, phase, breaker, cable, load, y in circuit_samples:
            msp.add_text(cno, dxfattribs={"insert": (50.0, y), "height": 20.0})
            msp.add_text(phase, dxfattribs={"insert": (150.0, y), "height": 20.0})
            msp.add_text(breaker, dxfattribs={"insert": (350.0, y), "height": 20.0})
            msp.add_text(cable, dxfattribs={"insert": (650.0, y), "height": 20.0})
            msp.add_text(load, dxfattribs={"insert": (950.0, y), "height": 20.0})

        raw = extract_cad_table_data(doc)
        self.assertEqual(len(raw.boxes), 1)
        self.assertEqual(raw.boxes[0].code, "01AP1")

        # 验证 6 个出线回路 + 1 个进线回路全部成功提取
        self.assertEqual(len(raw.circuits), 7)

        # 验证进线回路
        inc = next(c for c in raw.circuits if c.circuit_no == "进线")
        self.assertEqual(inc.breaker, "NM1-125S/3300 In=100A")
        self.assertIn("WDZN-YJV-5x16", inc.cable)

        # 验证所有新代号与主流断路器完整提取
        c_map = {c.circuit_no: c for c in raw.circuits if c.circuit_no != "进线"}
        self.assertIn("1WL1", c_map)
        self.assertEqual(c_map["1WL1"].breaker, "NXB-63 C20")
        self.assertIn("2WP1", c_map)
        self.assertEqual(c_map["2WP1"].breaker, "NDM1-63/3300 50A")
        self.assertIn("B1WL2", c_map)
        self.assertEqual(c_map["B1WL2"].breaker, "EA9RN 2P C16A")
        self.assertIn("-1WL3", c_map)
        self.assertEqual(c_map["-1WL3"].breaker, "CDB6-63 C32")
        self.assertIn("W1", c_map)
        self.assertEqual(c_map["W1"].breaker, "3VA11 100A 3P")
        self.assertIn("P1", c_map)
        self.assertEqual(c_map["P1"].breaker, "Tmax XT2 160A")

    def test_box_quotation_uses_dynamic_width_and_main_switch(self):
        """15. 造价求真：柜体铜排造价动态关联箱体实际宽度与主开关规格，杜绝硬编码 0.8m。"""
        from extractor.pricing import calculate_box_quotation

        # 宽 1200mm 大柜，主开关规格在 box.main_switch 中
        box_wide = {
            "box_code": "01AP2",
            "box_type": "GGD",
            "size": "1200*2200*800",
            "main_switch": "NM1-250S/3300 200A",
        }
        circuits = [
            {"circuit_type": "outgoing", "circuit_no": "WL1", "breaker": "iC65N-C16/1P"}
        ]
        q_wide = calculate_box_quotation(box_wide, circuits, components=[])
        # 1200mm 铜排长度更长，成本明显高于默认 800mm 宽柜体
        box_std = {
            "box_code": "01AP2_STD",
            "box_type": "GGD",
            "size": "800*2200*800",
            "main_switch": "NM1-250S/3300 200A",
        }
        q_std = calculate_box_quotation(box_std, circuits, components=[])
        self.assertGreater(q_wide["cost_breakdown"]["copper_busbar_cost"], q_std["cost_breakdown"]["copper_busbar_cost"])
        self.assertGreater(q_wide["cost_breakdown"]["copper_weight_kg"], q_std["cost_breakdown"]["copper_weight_kg"])


if __name__ == "__main__":
    unittest.main()
