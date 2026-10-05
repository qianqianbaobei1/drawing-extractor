# -*- coding: utf-8 -*-
"""配置层、证据来源与交叉验证的回归测试。

这些测试保护三件容易悄悄退化的事：
1. 硬编码不能被重新写回代码（关键口径必须能从 config/*.json 覆盖）；
2. 证据链不能重新变成“模型给自己作证”（纯事实字段必须有独立来源才算确认）；
3. 交叉验证拿不到原生文字时必须如实报告不可用，不得默认通过。
"""
import json
import os
import tempfile
import unittest

from extractor import config as appconfig
from extractor.corroborate import build_native_corpus, corroborate, corroboration_issues
from extractor.schema import (
    Box,
    Circuit,
    Evidence,
    EvidenceType,
    ExtractionResult,
    GroundedField,
    MODEL_EVIDENCE_ORIGIN,
    PHYSICAL_EVIDENCE_ORIGINS,
    RawExtraction,
    ReviewStatus,
    validate_field_evidence,
)


class ConfigLayerTests(unittest.TestCase):
    """配置层必须真的能覆盖代码内置值，否则“去硬编码”只是换了个地方写死。"""

    def tearDown(self):
        appconfig.reload()
        os.environ.pop("EXTRACTOR_CONFIG_OVERRIDE_DIR", None)
        os.environ.pop("TILE_OVERLAP", None)

    def test_shipped_defaults_are_always_present(self):
        for name in ("domain", "pipeline", "pricing", "delivery", "vision", "replacement"):
            data = appconfig.load(name)
            self.assertIsInstance(data, dict)
            self.assertTrue(data, f"{name}.json 不应为空")

    def test_override_dir_deep_merges_into_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pipeline.json"), "w", encoding="utf-8") as f:
                json.dump({"render": {"tile_overlap": 0.33}}, f)
            os.environ["EXTRACTOR_CONFIG_OVERRIDE_DIR"] = d
            appconfig.reload()
            render = appconfig.pipeline()["render"]
            # 覆盖生效
            self.assertEqual(render["tile_overlap"], 0.33)
            # 未覆盖的键继续跟随出厂默认，不会因为漏写而缺失
            self.assertEqual(render["tile_trigger_mm"], 320)
            self.assertIn("min_dpi", render)

    def test_env_override_wins_and_keeps_numeric_type(self):
        os.environ["TILE_OVERLAP"] = "0.2"
        appconfig.reload()
        value = appconfig.pipeline()["render"]["tile_overlap"]
        self.assertEqual(value, 0.2)
        self.assertIsInstance(value, float)

    def test_unknown_config_file_returns_empty_dict(self):
        self.assertEqual(appconfig.load("no_such_section"), {})

    def test_config_health_reports_a_real_directory(self):
        health = appconfig.config_health()
        self.assertTrue(os.path.isdir(health["config_dir"]))
        self.assertIn("pipeline.json", health["files"])

    def test_no_override_means_no_override_dir(self):
        appconfig.reload()
        self.assertEqual(appconfig.config_health()["override_dir"], "")


class ConfigDrivenBehaviourTests(unittest.TestCase):
    """抽查若干关键口径确实来自配置而不是代码常量。"""

    def tearDown(self):
        appconfig.reload()
        os.environ.pop("EXTRACTOR_CONFIG_OVERRIDE_DIR", None)

    def test_category_order_and_breaker_rules_come_from_config(self):
        from extractor import assemble
        self.assertEqual(list(assemble.CATEGORY_ORDER), appconfig.domain()["categories"]["order"])
        self.assertEqual(assemble._breaker_name("MCCB-63/63A/3P"), "塑壳断路器")
        self.assertEqual(assemble._breaker_name(""), appconfig.domain()["breaker"]["other_category"])

    def test_custom_category_rule_is_picked_up_from_override(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "domain.json"), "w", encoding="utf-8") as f:
                json.dump({"breaker": {"category_rules": [
                    {"category": "自定义类别", "prefix_any": ["ZZTOP"]},
                ]}}, f)
            os.environ["EXTRACTOR_CONFIG_OVERRIDE_DIR"] = d
            appconfig.reload()
            import importlib
            from extractor import assemble
            importlib.reload(assemble)
            try:
                self.assertEqual(assemble._breaker_name("ZZTOP-63"), "自定义类别")
            finally:
                os.environ.pop("EXTRACTOR_CONFIG_OVERRIDE_DIR", None)
                appconfig.reload()
                importlib.reload(assemble)

    def test_pricing_and_delivery_defaults_are_configured(self):
        self.assertIn("price_per_kg", appconfig.pricing_rules()["copper"])
        self.assertTrue(appconfig.delivery()["brand"]["default_target"])
        self.assertIn("detail", appconfig.delivery()["sheets"])

    def test_cad_extractor_does_not_invent_incoming_phase(self):
        """进线相序不得凭空填；出厂配置默认为空，必须留空而不是写死三相五线制。"""
        from extractor import cad_extractor
        self.assertEqual(cad_extractor._CAD_CONFIG.get("incoming_phase_default"), "")
        import inspect
        source = inspect.getsource(cad_extractor.extract_cad_table_data)
        self.assertNotIn('phase="L1/L2/L3/N/PE"', source)


class EvidenceProvenanceTests(unittest.TestCase):
    """证据来源：模型自述不得被当成物理事实。"""

    def test_model_origin_is_not_a_physical_fact(self):
        ev = Evidence(evidence_id="e1", evidence_type=EvidenceType.TEXT.value, origin=MODEL_EVIDENCE_ORIGIN)
        self.assertFalse(ev.is_physical_fact)
        for origin in PHYSICAL_EVIDENCE_ORIGINS:
            self.assertTrue(Evidence(evidence_id="e2", evidence_type="TEXT", origin=origin).is_physical_fact)

    def test_pure_fact_field_rejects_model_only_origin(self):
        ok, msg = validate_field_evidence("box.location", "TEXT", MODEL_EVIDENCE_ORIGIN)
        self.assertFalse(ok)
        self.assertIn("box.location", msg)
        # 有独立来源就放行
        self.assertTrue(validate_field_evidence("box.location", "TEXT", "cad_native")[0])
        # 允许语义推断的字段不受来源限制
        self.assertTrue(validate_field_evidence("circuit.load_name", "MODEL", MODEL_EVIDENCE_ORIGIN)[0])

    def test_unknown_origin_is_not_trusted(self):
        self.assertFalse(validate_field_evidence("circuit.breaker", "TEXT", "")[0])

    def test_assemble_marks_its_evidence_as_model_reported(self):
        from extractor.assemble import assemble
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "2SAL2", "name": "照明配电箱"}],
            "circuits": [{"box": "2SAL2", "circuit_no": "WL1", "breaker": "MCB-63 C16A/1P"}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        result = assemble(raw, {"model": "test"})
        self.assertTrue(result.evidence_store)
        for ev in result.evidence_store.values():
            self.assertEqual(ev.origin, MODEL_EVIDENCE_ORIGIN)
        # 关键：这些字段不能因为“有 TEXT 证据”就被判成已确认
        claims = result.boxes[0].claims
        self.assertEqual(claims["box.code"].review_status, ReviewStatus.PARSED_OK.value)
        for claim in result.circuits[0].claims.values():
            self.assertNotEqual(claim.review_status, ReviewStatus.CONFIRMED.value)


class CorroborationTests(unittest.TestCase):
    """用原生文字做独立交叉验证。"""

    def _result(self, breaker="MCB-63 C16A/1P", circuit_no="WL1"):
        from extractor.assemble import assemble
        raw = RawExtraction.model_validate({
            "boxes": [{"code": "2SAL2", "name": "照明配电箱"}],
            "circuits": [{"box": "2SAL2", "circuit_no": circuit_no, "breaker": breaker}],
            "extra_devices": [], "requirements": [], "uncertainties": [],
        })
        return assemble(raw, {"model": "test"})

    def test_normalization_ignores_separators_and_case(self):
        corpus, count = build_native_corpus(["MCB63 C16A / 1P"])
        self.assertEqual(count, 1)
        self.assertIn("MCB63C16A/1P", corpus)

    def test_no_native_text_reports_unavailable_and_changes_nothing(self):
        result = self._result()
        stats = corroborate(result, [])
        self.assertFalse(stats["available"])
        self.assertEqual(stats["corroborated"], 0)
        self.assertEqual(result.circuits[0].claims["circuit.breaker"].review_status,
                         ReviewStatus.PARSED_OK.value)
        # 不可用必须如实说明，而不是假装验证通过
        notices = corroboration_issues(stats)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["severity"], "INFO")
        self.assertIn("没有可用的原生文字", notices[0]["detail"])

    def test_hit_upgrades_evidence_origin_and_status(self):
        result = self._result()
        stats = corroborate(result, ["配电箱 2SAL2 系统图", "WL1 MCB-63 C16A/1P 照明 0.8kW"])
        self.assertTrue(stats["available"])
        self.assertGreaterEqual(stats["corroborated"], 1)
        claim = result.circuits[0].claims["circuit.breaker"]
        self.assertEqual(claim.review_status, ReviewStatus.CONFIRMED.value)
        for ev_id in claim.value_evidence_ids:
            self.assertIn(result.evidence_store[ev_id].origin, PHYSICAL_EVIDENCE_ORIGINS)
        self.assertEqual(corroboration_issues(stats), [])

    def test_miss_is_reported_as_warning_not_claimed_as_error(self):
        result = self._result(breaker="MCB-63 C40A/3P", circuit_no="WL9")
        stats = corroborate(result, ["与本图无关的一段原生文字"])
        self.assertTrue(stats["available"])
        self.assertEqual(stats["values_checked"], 3)
        self.assertEqual(stats["corroborated"], 0)
        self.assertEqual(stats["unverified"], 3)
        issues = corroboration_issues(stats)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "WARNING")
        self.assertIn("未命中不等于错误", issues[0]["detail"])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            corroborate(self._result(), ["x"], source="not_a_real_origin")


class CheckerEvidenceGatingTests(unittest.TestCase):
    def test_confirmed_without_physical_evidence_is_flagged(self):
        from extractor.checker import check_result_issues
        result = ExtractionResult(
            boxes=[Box(code="2SAL2")],
            evidence_store={"box.code.2SAL2": Evidence(
                evidence_id="box.code.2SAL2", evidence_type="TEXT",
                raw_content="2SAL2", origin=MODEL_EVIDENCE_ORIGIN)},
        )
        result.boxes[0].claims = {"box.code": GroundedField(
            value="2SAL2", raw_value="2SAL2",
            value_evidence_ids=["box.code.2SAL2"],
            review_status=ReviewStatus.CONFIRMED.value)}
        codes = {issue.rule_code for issue in check_result_issues(result)}
        self.assertIn("EVIDENCE_STATUS_MISMATCH", codes)

    def test_model_only_claim_is_not_noisy_when_not_claimed_confirmed(self):
        from extractor.checker import check_result_issues
        result = ExtractionResult(
            boxes=[Box(code="2SAL2")],
            evidence_store={"box.code.2SAL2": Evidence(
                evidence_id="box.code.2SAL2", evidence_type="TEXT",
                raw_content="2SAL2", origin=MODEL_EVIDENCE_ORIGIN)},
        )
        result.boxes[0].claims = {"box.code": GroundedField(
            value="2SAL2", raw_value="2SAL2",
            value_evidence_ids=["box.code.2SAL2"],
            review_status=ReviewStatus.PARSED_OK.value)}
        codes = {issue.rule_code for issue in check_result_issues(result)}
        # 只有模型自述本身不算矛盾：图片型 PDF 本来就没有第二个来源
        self.assertNotIn("EVIDENCE_STATUS_MISMATCH", codes)


class ExportGateTests(unittest.TestCase):
    def test_info_never_blocks_export(self):
        import app
        items = [
            {"location": "交叉验证", "detail": "无原生文字", "severity": "INFO"},
            {"location": "WL3", "detail": "编号冲突", "severity": "WARNING"},
            {"location": "已确认项", "detail": "ok", "severity": "ERROR", "resolved": True},
        ]
        blocking = app.blocking_uncertainties(items)
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]["location"], "WL3")

    def test_missing_severity_defaults_to_blocking(self):
        import app
        self.assertEqual(len(app.blocking_uncertainties([{"location": "x", "detail": "y"}])), 1)


class SliceCoverageTests(unittest.TestCase):
    def test_grid_never_overflows_page(self):
        from extractor.render import TILE_MAX_COLS, TILE_MAX_ROWS, grid_for, plan_grid_clips
        for width, height in [(297, 210), (841, 594), (1189, 841), (4000, 300), (300, 4000)]:
            cols, rows = grid_for(width, height)
            self.assertLessEqual(cols, TILE_MAX_COLS)
            self.assertLessEqual(rows, TILE_MAX_ROWS)
            for clip in plan_grid_clips(cols, rows):
                self.assertGreaterEqual(clip["x"], 0.0)
                self.assertGreaterEqual(clip["y"], 0.0)
                self.assertLessEqual(clip["x"] + clip["w"], 1.0 + 1e-9)
                self.assertLessEqual(clip["y"] + clip["h"], 1.0 + 1e-9)


class PriceHonestyTests(unittest.TestCase):
    """价格门禁：价格库与标准面价库都未命中时，不得用公式凑一个金额冒充报价。"""

    def tearDown(self):
        appconfig.reload()
        os.environ.pop("EXTRACTOR_CONFIG_OVERRIDE_DIR", None)
        import importlib
        from extractor import pricing
        importlib.reload(pricing)

    def test_formula_fallback_is_off_by_default(self):
        from extractor.pricing import ALLOW_FORMULA_FALLBACK_PRICING, calculate_component_unit_price
        self.assertFalse(ALLOW_FORMULA_FALLBACK_PRICING)
        unit, list_price, basis = calculate_component_unit_price("KM-18A", brand="正泰")
        self.assertEqual(unit, 0.0)
        self.assertEqual(list_price, 0.0)
        self.assertIn("待人工询价", basis)

    def test_explicitly_enabling_the_gate_yields_a_labelled_estimate(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "pricing.json"), "w", encoding="utf-8") as f:
                json.dump({"gates": {"allow_formula_fallback_pricing": True}}, f)
            os.environ["EXTRACTOR_CONFIG_OVERRIDE_DIR"] = d
            appconfig.reload()
            import importlib
            from extractor import pricing
            importlib.reload(pricing)
            self.assertTrue(pricing.ALLOW_FORMULA_FALLBACK_PRICING)
            unit, list_price, basis = pricing.calculate_component_unit_price("KM-18A", brand="正泰")
            self.assertGreater(unit, 0.0)
            # 即使是估算也必须自带“不是供应商报价”的标签
            self.assertIn("估算", basis)

    def test_match_scoring_weights_are_configurable(self):
        scoring = appconfig.pricing_rules()["match_scoring"]
        for key in ("current_exact", "current_far_penalty", "curve_match"):
            self.assertIn(key, scoring)


class PanelCodeNoiseTests(unittest.TestCase):
    """箱号词法：工程量写法不得被当成箱号（审计里曾出现 380/220V、DC36V 被列为箱体候选）。"""

    def test_electrical_quantities_are_not_panel_codes(self):
        from extractor.cad_extractor import extract_panel_code
        for token in ("380/220V", "DC36V", "220V", "50Hz", "4X6A", "3X2.5"):
            self.assertIsNone(extract_panel_code(token), f"{token} 不应被判为箱号")

    def test_real_panel_codes_still_pass(self):
        from extractor.cad_extractor import extract_panel_code
        self.assertEqual(extract_panel_code("2SAL2 照明配电箱"), "2SAL2")
        self.assertEqual(extract_panel_code("AW1 配电箱"), "AW1")

    def test_noise_regexes_are_configurable(self):
        self.assertTrue(appconfig.domain()["vocabulary"]["non_panel_token_regexes"])


class ConfigCompletenessTests(unittest.TestCase):
    """配置完整性：代码里读的每个配置键都必须在 JSON 里存在。

    这是防“硬编码回潮”的关键护栏：有人往代码里新增了一个配置读取却忘了写默认值，
    测试会直接报出具体文件与键名，而不是等到现场 KeyError。
    """

    ROOTS = {
        "_CAD": "pipeline.cad", "_CAD_CONFIG": "pipeline.cad", "_R": "pipeline.render",
        "_GATES": "pipeline.gates",
        "_DOM": "domain.cad", "_BREAKER_CFG": "domain.breaker", "_V": "domain.vocabulary",
        "_D": "domain.cad", "_CHECKS": "domain.checks", "_CATALOG_CFG": "domain.catalog",
        "_CABLE_VOCAB": "domain.cable",
        "_PALETTE": "delivery.palette", "_SHEETS": "delivery.sheets",
        "_COLUMNS": "delivery.columns", "_LABELS": "delivery.labels",
        "_ESTIMATION": "delivery.estimation", "_EXPORT_CFG": "delivery.export_gate",
        "_P_COPPER": "pricing.copper", "_P_RATES": "pricing.rates",
        "_PRICING_GATES": "pricing.gates", "_BRANDS": "pricing.brands",
        "_DEFAULTS": "pricing.defaults", "DEFAULTS": "pricing.defaults",
        "MATCH_SCORING": "pricing.match_scoring",
        "_TRANSPORT": "vision.transport", "_GENERATION": "vision.generation",
        "_PARSE_GATE": "vision.parse_gate",
    }

    def test_every_config_key_read_by_code_exists(self):
        import re
        from pathlib import Path
        backend = Path(__file__).resolve().parent.parent
        missing = []
        for py in sorted((backend / "extractor").glob("*.py")) + [backend / "app.py"]:
            source = py.read_text(encoding="utf-8")
            for match in re.finditer(r'\b([A-Za-z_][A-Za-z0-9_]*)\[["\']([a-zA-Z0-9_]+)["\']\]', source):
                var, key = match.group(1), match.group(2)
                path = self.ROOTS.get(var)
                if not path:
                    continue
                node = appconfig.load(path.split(".")[0])
                for part in path.split(".")[1:]:
                    node = (node or {}).get(part) if isinstance(node, dict) else None
                if isinstance(node, dict) and key not in node:
                    missing.append(f"{py.name}: {var}['{key}'] 不在 {path}")
        self.assertEqual(missing, [], "存在代码读取但配置未定义的键：\n" + "\n".join(missing))

    def test_all_env_mappings_point_at_real_keys(self):
        for name in ("pipeline", "pricing", "delivery", "vision"):
            data = appconfig.load(name)
            for env_name, dotted in (data.get("_env") or {}).items():
                node = data
                for part in dotted.split("."):
                    self.assertIsInstance(node, dict, f"{name}.json 的 {env_name} 指向不存在的路径 {dotted}")
                    self.assertIn(part, node, f"{name}.json 的 {env_name} 指向不存在的键 {dotted}")
                    node = node[part]


if __name__ == "__main__":
    unittest.main()
