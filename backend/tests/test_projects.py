# -*- coding: utf-8 -*-
"""项目流程测试：从图纸图签自动建项目、项目信息读写、改名一致性。

对应产品逻辑：上传图纸 → 从图签读工程名称 → 建/进项目 → 在项目里上传与查看图纸。
要点：识别不到时必须停在“未分组”等人工处理，绝不拿文件名冒充工程名称。
"""
import unittest

import test_support  # noqa: F401  必须最先导入：把数据目录隔离到临时目录
from fastapi.testclient import TestClient

from app import app, _resolve_project_info
from extractor.project_info import infer_from_filename, infer_from_texts, inferred_project_name, merge_info
from extractor.schema import ProjectInfo, RawExtraction


def _raw(project_info=None) -> RawExtraction:
    payload = {"boxes": [], "circuits": [], "extra_devices": [],
               "requirements": [], "uncertainties": []}
    if project_info is not None:
        payload["project_info"] = project_info
    return RawExtraction.model_validate(payload)


TITLE_BLOCK = [
    {"text": "工程名称：四川中烟工业有限责任公司成都卷烟厂制丝线升级改造项目"},
    {"text": "建设单位：四川中烟工业有限责任公司"},
    {"text": "设计单位：中国轻工业广州工程有限公司"},
    {"text": "工程编号：CQ-2026-0915"},
    {"text": "2SAL2 照明配电箱系统图"},
]


class ProjectInfoInferenceTests(unittest.TestCase):
    def test_reads_every_field_from_title_block(self):
        info = infer_from_texts(TITLE_BLOCK)
        self.assertTrue(info["found"])
        self.assertEqual(info["name"], "四川中烟工业有限责任公司成都卷烟厂制丝线升级改造项目")
        self.assertEqual(info["code"], "CQ-2026-0915")
        self.assertEqual(info["client"], "四川中烟工业有限责任公司")
        self.assertEqual(info["designer"], "中国轻工业广州工程有限公司")

    def test_irrelevant_text_yields_nothing(self):
        """普通图面文字里不该被硬凑出项目名。"""
        info = infer_from_texts([
            {"text": "WL1 照明 3.5kW"},
            {"text": "备用回路"},
            {"text": "过载仅报警不跳闸"},
        ])
        self.assertFalse(info["found"])
        self.assertEqual(info["name"], "")

    def test_two_fields_on_one_line_are_split(self):
        """图签常把两个字段挤在一行：抓值时要截断到下一个字段标签前。"""
        info = infer_from_texts([{"text": "工程名称：某某产业园项目  设计单位：某某设计院"}])
        self.assertEqual(info["name"], "某某产业园项目")
        self.assertEqual(info["designer"], "某某设计院")

    def test_short_or_empty_values_are_rejected(self):
        for text in ("工程名称：", "工程名称：", "项目名称：  "):
            self.assertFalse(infer_from_texts([{"text": text}])["found"])

    def test_merge_prefers_first_source(self):
        drawing = {"name": "图上工程名", "found": True, "source": "cad_native"}
        vision = {"name": "模型读的名字", "found": True, "source": "vision"}
        self.assertEqual(merge_info(vision, drawing)["name"], "模型读的名字")
        self.assertEqual(merge_info(vision, drawing)["source"], "vision")

    def test_filename_inference_strips_drawing_noise(self):
        self.assertEqual(infer_from_filename("01-制丝工房-动力配电0923.dwg"), "制丝工房-动力配电0923")
        self.assertEqual(infer_from_filename("02-锅炉房-8台柜子.pdf"), "锅炉房-8台柜子")
        self.assertEqual(infer_from_filename("电气照明921(出图)_t3.dwg"), "电气照明921")
        self.assertEqual(infer_from_filename("2SAL2.pdf"), "2SAL2")

    def test_project_name_priority(self):
        info = {"name": "图上工程名", "found": True}
        self.assertEqual(inferred_project_name(info, "随便.pdf"), ("图上工程名", "drawing"))
        self.assertEqual(inferred_project_name({}, "锅炉房-8台柜子.pdf"), ("锅炉房-8台柜子", "filename"))
        self.assertEqual(inferred_project_name({}, ""), ("", "none"))


class ProjectResolutionTests(unittest.TestCase):
    """_resolve_project_info：决定一个上传的文件进哪个项目。"""

    def test_explicit_project_wins_over_drawing(self):
        decision = _resolve_project_info({"project": "人工指定的项目"}, _raw(), TITLE_BLOCK, "", "x.pdf")
        self.assertEqual(decision["project"], "人工指定的项目")
        self.assertEqual(decision["name_source"], "manual")

    def test_drawing_title_block_creates_project(self):
        decision = _resolve_project_info({}, _raw(), TITLE_BLOCK, "", "01-制丝工房.dwg")
        self.assertEqual(decision["project"], "四川中烟工业有限责任公司成都卷烟厂制丝线升级改造项目")
        self.assertEqual(decision["name_source"], "drawing")

    def test_vision_project_info_used_when_no_native_text(self):
        raw = _raw({"name": "模型读到的工程名称", "code": "A-1"})
        decision = _resolve_project_info({}, raw, None, "", "x.pdf")
        self.assertEqual(decision["project"], "模型读到的工程名称")
        self.assertEqual(decision["name_source"], "drawing")

    def test_filename_fallback_is_flagged_for_review(self):
        decision = _resolve_project_info({}, _raw(), [{"text": "WL1 照明"}], "", "锅炉房-8台柜子.pdf")
        self.assertEqual(decision["project"], "锅炉房-8台柜子")
        self.assertEqual(decision["name_source"], "filename")
        self.assertIn("请核对", decision["note"])

    def test_nothing_usable_stays_ungrouped(self):
        """文件名也推不出名字时不得硬造项目名，停在未分组等人工归类。"""
        decision = _resolve_project_info({}, _raw(), [], "", "")
        self.assertEqual(decision["project"], "")
        self.assertEqual(decision["name_source"], "none")


class ProjectApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _create(self, name, **meta):
        return self.client.post("/api/projects", json={"name": name, **meta})

    def test_create_with_metadata_then_read_back(self):
        res = self._create("测试工程A", project_code="A-001", client_name="甲方公司",
                           designer_institute="某设计院", status="bidding")
        self.assertEqual(res.status_code, 200)
        got = self.client.get("/api/projects/测试工程A").json()["project"]
        self.assertEqual(got["project_code"], "A-001")
        self.assertEqual(got["client_name"], "甲方公司")
        self.assertEqual(got["designer_institute"], "某设计院")
        self.assertEqual(got["status"], "bidding")
        self.assertEqual(got["name_source"], "manual")

    def test_patch_updates_only_given_fields(self):
        self._create("测试工程B", client_name="原甲方")
        res = self.client.patch("/api/projects/测试工程B", json={"designer_institute": "新设计院"})
        self.assertEqual(res.status_code, 200)
        got = self.client.get("/api/projects/测试工程B").json()["project"]
        self.assertEqual(got["designer_institute"], "新设计院")
        self.assertEqual(got["client_name"], "原甲方")   # 未传的字段不被清空

    def test_patch_unknown_project_404(self):
        self.assertEqual(self.client.patch("/api/projects/不存在", json={"note": "x"}).status_code, 404)

    def test_missing_project_404(self):
        self.assertEqual(self.client.get("/api/projects/不存在的项目").status_code, 404)

    def test_rename_moves_project_and_keeps_it_findable(self):
        self._create("旧名字工程")
        res = self.client.post("/api/projects/旧名字工程/rename", json={"new_name": "新名字工程"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get("/api/projects/新名字工程").status_code, 200)
        self.assertEqual(self.client.get("/api/projects/旧名字工程").status_code, 404)

    def test_rename_rejects_duplicate_name(self):
        self._create("工程甲")
        self._create("工程乙")
        res = self.client.post("/api/projects/工程甲/rename", json={"new_name": "工程乙"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("已存在", res.json()["detail"])

    def test_rename_rejects_blank_name(self):
        self._create("工程丙")
        self.assertEqual(self.client.post("/api/projects/工程丙/rename", json={"new_name": "  "}).status_code, 400)

    def test_ungrouped_bucket_is_hidden_when_empty(self):
        """「未分组」只是兜底桶：没有图纸时不该在项目列表里占一行。"""
        from app import visible_project_names
        self.assertEqual(visible_project_names(["未分组", "真项目"], {}), ["真项目"])
        self.assertEqual(visible_project_names(["未分组"], {"未分组": [{"job_id": "x"}]}), ["未分组"])


if __name__ == "__main__":
    unittest.main()
