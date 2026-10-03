# -*- coding: utf-8 -*-
"""CAD (DWG / DXF) 原生解析与渲染转换单元测试。"""

import os
import shutil
import tempfile
import unittest

import ezdxf
import pymupdf

from extractor.cad import (
    clean_mtext,
    extract_cad_entities,
    is_cad_path,
    process_cad_file,
    render_dxf_to_pdf,
)


class TestCADPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_is_cad_path(self):
        self.assertTrue(is_cad_path("drawing.dwg"))
        self.assertTrue(is_cad_path("DRAWING.DWG"))
        self.assertTrue(is_cad_path("/path/to/file.dxf"))
        self.assertTrue(is_cad_path("project.DXF"))
        self.assertFalse(is_cad_path("document.pdf"))
        self.assertFalse(is_cad_path("image.png"))
        self.assertFalse(is_cad_path("data.xlsx"))

    def test_clean_mtext(self):
        # 测试 AutoCAD MTEXT 换行与控制符清洗
        raw1 = r"AL1\P照明箱"
        self.assertEqual(clean_mtext(raw1), "AL1\n照明箱")

        raw2 = r"\fSimSun;MCB-63\P\C1;C16A/1P"
        self.assertEqual(clean_mtext(raw2), "MCB-63\nC16A/1P")

        raw3 = r"%%c200\P%%d45"
        self.assertEqual(clean_mtext(raw3), "Φ200\n°45")

    def test_dxf_entity_extraction_and_rendering(self):
        dxf_path = os.path.join(self.temp_dir, "test_sample.dxf")
        pdf_path = os.path.join(self.temp_dir, "test_sample.pdf")

        # 构造模拟 CAD 配电箱系统图 DXF
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        msp.add_text("1AL1 照明配电箱", dxfattribs={"height": 25, "insert": (50, 500)})
        msp.add_text("进线开关: MCCB-63/63A/3P", dxfattribs={"height": 18, "insert": (50, 460)})
        msp.add_mtext("WL1: MCB-63/C16A/1P\\P0.8kW L1 走廊照明", dxfattribs={"char_height": 14, "insert": (50, 380)})
        msp.add_mtext("WL2: MCB-63/C16A/1P\\P1.2kW L2 展厅照明", dxfattribs={"char_height": 14, "insert": (50, 320)})
        doc.saveas(dxf_path)

        # 1. 实体提取验证
        entities = extract_cad_entities(dxf_path)
        self.assertGreaterEqual(len(entities), 4)

        # 检查排序：从上到下按 Y 降序排列
        y_coords = [e["y"] for e in entities]
        self.assertEqual(y_coords, sorted(y_coords, reverse=True))

        texts = [e["text"] for e in entities]
        self.assertTrue(any("1AL1" in t for t in texts))
        self.assertTrue(any("MCCB-63" in t for t in texts))
        self.assertTrue(any("WL1:" in t for t in texts))

        # 2. 渲染为 PDF 验证
        out_pdf, extracted = process_cad_file(dxf_path, pdf_path)
        self.assertTrue(os.path.exists(out_pdf))
        self.assertGreater(os.path.getsize(out_pdf), 1000)

        # 验证转换出的 PDF 可被 PyMuPDF 正常读取和光栅化
        pdf_doc = pymupdf.open(out_pdf)
        self.assertEqual(len(pdf_doc), 1)
        pix = pdf_doc[0].get_pixmap(dpi=100)
        self.assertGreater(pix.width, 200)
        self.assertGreater(pix.height, 200)
        pdf_doc.close()

    def test_surrogate_sanitization(self):
        from extractor.cad import sanitize_surrogates
        bad_text = "照明箱 \ud83d\ud83d 进线 \ud800 出线"
        cleaned = sanitize_surrogates(bad_text)
        # 确保清洗后可安全进行 UTF-8 编码与解码
        encoded = cleaned.encode("utf-8")
        self.assertIn("照明箱", cleaned)
        self.assertIn("出线", cleaned)
        self.assertNotIn("\ud800", cleaned)

        # clean_mtext 也应净化代理对
        mtext_cleaned = clean_mtext(r"AL1\P\ud83d测试%%c50")
        self.assertIn("AL1\n测试Φ50", mtext_cleaned)

    def test_is_system_title(self):
        from extractor.cad import is_system_title
        self.assertTrue(is_system_title("配电系统图一"))
        self.assertTrue(is_system_title("动力配电系统图"))
        self.assertTrue(is_system_title("消防应急照明配电箱接线图"))
        self.assertTrue(is_system_title("低压配电系统接线图(二)"))
        self.assertFalse(is_system_title("一层照明平面图"))
        self.assertFalse(is_system_title("防雷接地平面布置图"))
        self.assertFalse(is_system_title("电气设计说明"))
        self.assertFalse(is_system_title("图纸目录"))
        # 排除包含“系统图”但实质为施工附注或设计说明的长句
        self.assertFalse(is_system_title("2）本子项设置能耗监测管理系统和建筑设备管理系统；其系统组成和构架与能耗计量系统图详弱电专业。"))
        self.assertFalse(is_system_title("7.6 详见电气火灾监控系统图。"))
        self.assertFalse(is_system_title("7. 所有消防电源监控模块及传感器均安装在本配电柜内，详见配电柜系统图；模块的正常供电电源取自"))

    def test_detect_system_sheets_and_slicing(self):
        from extractor.cad import detect_system_sheets, MAX_TEXT_ENTITIES_LIMIT
        dxf_path = os.path.join(self.temp_dir, "multi_sheets.dxf")
        pdf_path = os.path.join(self.temp_dir, "multi_sheets.pdf")

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # 1. 模拟图纸目录表（垂直紧密排列，X~10000, dy=500）
        for y, title in enumerate(["图纸目录", "配电系统图一", "配电系统图二", "一层照明平面"], start=1):
            msp.add_text(title, dxfattribs={"height": 100, "insert": (10000, 10000 + y * 500)})

        # 2. 模拟真正的单图图签栏（横向大间距排列，ΔX=120000）
        # 图一：X=100000, Y=200000
        msp.add_text("配电系统图一", dxfattribs={"height": 400, "insert": (100000, 200000), "layer": "PUB_TITLE"})
        msp.add_text("总箱 01AL1 动力配电箱", dxfattribs={"height": 200, "insert": (60000, 230000)})
        msp.add_text("进线断路器 NM1-125S/3P 100A", dxfattribs={"height": 150, "insert": (60000, 220000)})

        # 图二：X=220000, Y=200000
        msp.add_text("配电系统图二", dxfattribs={"height": 400, "insert": (220000, 200000), "layer": "PUB_TITLE"})
        msp.add_text("空调箱 01AK1 空调动力配电箱", dxfattribs={"height": 200, "insert": (180000, 230000)})
        msp.add_text("进线断路器 NM1-63S/3P 40A", dxfattribs={"height": 150, "insert": (180000, 220000)})

        # 3. 模拟与系统图无关的平面施工图（X=340000, Y=200000）
        msp.add_text("一层电气照明平面图", dxfattribs={"height": 400, "insert": (340000, 200000)})
        msp.add_text("无关联平面灯具说明", dxfattribs={"height": 150, "insert": (300000, 220000)})

        doc.saveas(dxf_path)

        # 验证 1：自动探测应滤除目录表和平面图，仅保留图一和图二
        sheets = detect_system_sheets(doc)
        self.assertEqual(len(sheets), 2)
        titles = [s["title"] for s in sheets]
        self.assertIn("配电系统图一", titles)
        self.assertIn("配电系统图二", titles)
        self.assertNotIn("图纸目录", titles)
        self.assertNotIn("一层电气照明平面图", titles)

        # 验证 2：运行 process_cad_file 进行分幅切片并合成多页 PDF
        out_pdf, extracted = process_cad_file(dxf_path, pdf_path)
        self.assertTrue(os.path.exists(out_pdf))
        pdf_doc = pymupdf.open(out_pdf)
        self.assertEqual(len(pdf_doc), 2)  # 精准切为 2 页
        pdf_doc.close()

        # 验证 3：文字提取按页分配元数据
        p1_texts = [t["text"] for t in extracted if t.get("page") == 1]
        p2_texts = [t["text"] for t in extracted if t.get("page") == 2]
        self.assertTrue(any("01AL1" in t for t in p1_texts))
        self.assertTrue(any("01AK1" in t for t in p2_texts))
        # 且无关的平面施工图文本被空间包围盒过滤，不混入系统图
        all_extracted = [t["text"] for t in extracted]
        self.assertFalse(any("无关联平面灯具说明" in t for t in all_extracted))

    def test_construction_plan_guardrail(self):
        from unittest.mock import patch
        dxf_path = os.path.join(self.temp_dir, "pure_plan.dxf")
        pdf_path = os.path.join(self.temp_dir, "pure_plan.pdf")

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        for i in range(10):
            msp.add_text(f"二层照明平面施工图分区{i}", dxfattribs={"insert": (0, i * 100)})
        doc.saveas(dxf_path)

        with patch("extractor.cad.MAX_TEXT_ENTITIES_LIMIT", 5):
            with self.assertRaises(ValueError) as ctx:
                process_cad_file(dxf_path, pdf_path)
            self.assertIn("未检测到配电箱电气系统图", str(ctx.exception))

    def test_unit_block_slicing_by_frame_and_dashed_box(self):
        """v2 链路：图框块定幅面 → 箱名配虚线箱框定单元块 → 一个配电箱一页。

        同时守住三个旧缺陷：① 箱框右侧框外的电缆规格列不能被截掉；
        ② 圆/圆弧/多段线不能整类丢弃；③ 平面图图框不能产出单元块。
        """
        dxf_path = os.path.join(self.temp_dir, "units.dxf")
        out_pdf = os.path.join(self.temp_dir, "units.pdf")

        doc = ezdxf.new("R2010")
        # 图框块内部量测：闭环多段线给出精确幅面，不依赖任何尺寸猜测
        frame_blk = doc.blocks.new("横式A0")
        frame_blk.add_lwpolyline([(0, 0), (118900, 0), (118900, 84100), (0, 84100)], close=True)
        msp = doc.modelspace()

        for i, zh in enumerate("一二"):
            msp.add_blockref("横式A0", insert=(i * 118900, 0))
            msp.add_text(f"配电系统图{zh}", dxfattribs={
                "height": 350, "insert": (i * 118900 + 2000, 2000), "layer": "图签栏"})
            for j in (1, 2):
                x, y = i * 118900 + 3000 + (j - 1) * 40000, 30000
                msp.add_lwpolyline([(x, y), (x + 12000, y), (x + 12000, y + 20000), (x, y + 20000)],
                                   close=True, dxfattribs={"layer": "强电系统", "linetype": "DASHED2"})
                msp.add_text(f"0{i + 1}AL{j} 照明配电箱", dxfattribs={
                    "height": 300, "insert": (x + 2000, y - 900), "layer": "强电系统"})
                msp.add_text("WL1 MCB-63/C16A/1P", dxfattribs={
                    "height": 200, "insert": (x + 2000, y + 18000), "layer": "强电系统"})
                # 箱框右侧框外的电缆规格列：旧实现被截掉
                msp.add_mtext("ZR-YJV-4x6", dxfattribs={
                    "char_height": 200, "insert": (x + 26000, y + 18000), "layer": "强电系统"})
                # 旧实现用 dxf.insert/dxf.start 定位，圆与圆弧没有这些属性，被整类丢弃
                msp.add_circle((x + 6000, y + 15000), radius=800)
                msp.add_arc((x + 6000, y + 12000), radius=800, start_angle=0, end_angle=180)

        # 平面图图框：即便有图框块也不应产出单元块
        msp.add_blockref("横式A0", insert=(260000, 0))
        msp.add_text("一层照明平面图", dxfattribs={
            "height": 350, "insert": (262000, 2000), "layer": "图签栏"})
        doc.saveas(dxf_path)

        out, texts = process_cad_file(dxf_path, out_pdf)
        pdf = pymupdf.open(out)
        try:
            self.assertEqual(len(pdf), 4)                      # 2 张系统图 × 2 个配电箱
            sheets = sorted({t["sheet"] for t in texts})
            self.assertEqual(len(sheets), 4)
            self.assertTrue(sheets[0].startswith("01AL1"))
            page1 = [t["text"] for t in texts if t["sheet"].startswith("01AL1")]
            self.assertTrue(any("ZR-YJV-4x6" in t for t in page1))      # 框外电缆列没被截掉
            self.assertFalse(any("一层照明平面图" in t for t in texts))   # 平面图不进单元块
        finally:
            pdf.close()

    def test_block_reference_and_border_polyline(self):
        from extractor.cad import detect_system_sheets
        dxf_path = os.path.join(self.temp_dir, "block_ref.dxf")

        doc = ezdxf.new("R2010")
        msp = doc.modelspace()

        # 1. 创建图框块定义并在其中定义属性
        blk = doc.blocks.new(name="TITLE_BLOCK_A0")
        blk.add_attdef(tag="SHEET_TITLE", insert=(0, 0), height=300)

        # 2. 在模型空间插入块参照并附带属性 ATTRIB
        ins = msp.add_blockref("TITLE_BLOCK_A0", insert=(80000, 40000))
        ins.add_attrib(tag="SHEET_TITLE", text="低压配电系统图(三)", insert=(80000, 40000))

        # 3. 绘制真实闭合多段线外框 (LWPOLYLINE 模拟 A0 图纸框：宽 118900，高 84100)
        # 外框范围：X: [0, 118900], Y: [0, 84100]
        points = [(0, 0), (118900, 0), (118900, 84100), (0, 84100)]
        msp.add_lwpolyline(points, close=True, dxfattribs={"layer": "BORDER"})

        doc.saveas(dxf_path)

        sheets = detect_system_sheets(doc)
        self.assertEqual(len(sheets), 1)
        s = sheets[0]
        self.assertEqual(s["title"], "低压配电系统图(三)")
        # 验证物理闭合外框被精准捕获作为包围盒，而非粗略推导
        bx0, by0, bx1, by1 = s["bbox"]
        self.assertEqual((bx0, by0, bx1, by1), (0.0, 0.0, 118900.0, 84100.0))

    def test_row_pitch_adaptation_prevents_header_leak(self):
        """测试上下两排配电箱自适应行间距，确保上排单元块不会切入下排箱体的安装表头。"""
        from extractor.cad import GeometryIndex, build_unit_blocks
        doc = ezdxf.new("R2010")
        frame_blk = doc.blocks.new("横式A0")
        frame_blk.add_lwpolyline([(0, 0), (118900, 0), (118900, 84100), (0, 84100)], close=True)
        msp = doc.modelspace()
        msp.add_blockref("横式A0", insert=(0, 0))
        msp.add_text("配电系统图(一)", dxfattribs={"height": 400, "insert": (2000, 2000), "layer": "图签栏"})

        # 上排箱体：箱框 Y: [35000, 55000]，箱名在 Y: 33800
        x, y = 5000, 35000
        msp.add_lwpolyline([(x, y), (x + 15000, y), (x + 15000, y + 20000), (x, y + 20000)],
                           close=True, dxfattribs={"layer": "强电系统", "linetype": "DASHED2"})
        msp.add_text("01AP1 动力配电箱", dxfattribs={"height": 300, "insert": (x + 2000, 33800), "layer": "强电系统"})

        # 下排箱体：箱框 Y: [5000, 25000]，箱名在 Y: 3800
        # 下排箱体顶部有安装说明表头“XRM 嵌墙安装”（Y: 26000）
        y2 = 5000
        msp.add_lwpolyline([(x, y2), (x + 15000, y2), (x + 15000, y2 + 20000), (x, y2 + 20000)],
                           close=True, dxfattribs={"layer": "强电系统", "linetype": "DASHED2"})
        msp.add_text("01AP2 照明配电箱", dxfattribs={"height": 300, "insert": (x + 2000, 3800), "layer": "强电系统"})
        msp.add_text("XRM 嵌墙安装", dxfattribs={"height": 200, "insert": (x + 2000, 26000), "layer": "强电系统"})

        index = GeometryIndex(doc)
        frames = [{"bbox": (0.0, 0.0, 118900.0, 84100.0), "block": "横式A0", "source": "insert"}]
        blocks = build_unit_blocks(doc, index, frames)
        self.assertEqual(len(blocks), 2)

        # 验证上排 01AP1 的底边界必须严格高于下排箱框顶部 (25000) 与表头 (26000)
        p1_block = next(b for b in blocks if "01AP1" in b["label"])
        crop_y0 = p1_block["rect"][1]
        self.assertGreater(crop_y0, 26500)  # 严格高出下排表头，杜绝 3% 侵入

    def test_nested_title_block_deduplication(self):
        """测试大图框内部嵌套会签栏/子图签块时，子块被精准剔除，仅保留外侧真实图幅。"""
        from extractor.cad import GeometryIndex, detect_drawing_frames
        doc = ezdxf.new("R2010")
        # 1. 大图框 A0
        b_frame = doc.blocks.new("横式A0")
        b_frame.add_lwpolyline([(0, 0), (118900, 0), (118900, 84100), (0, 84100)], close=True)
        # 2. 会签栏/图签子块 (嵌套在大图框内部)
        b_stamp = doc.blocks.new("会签栏")
        b_stamp.add_lwpolyline([(0, 0), (18000, 0), (18000, 16000), (0, 16000)], close=True)

        msp = doc.modelspace()
        msp.add_blockref("横式A0", insert=(0, 0))
        msp.add_blockref("会签栏", insert=(95000, 5000))  # 坐标完全落在大图框内

        index = GeometryIndex(doc)
        frames = detect_drawing_frames(doc, index)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["block"], "横式A0")

    def test_polyline_frame_unclosed_endpoint_snapping(self):
        """测试起终点重合但未设置 closed=True 标志的多段线外框能被准确识别为图框。"""
        from extractor.cad import GeometryIndex, detect_drawing_frames
        doc = ezdxf.new("R2010")
        msp = doc.modelspace()
        # 4 个顶点首尾坐标一致，但 close=False
        points = [(0, 0), (118900, 0), (118900, 84100), (0, 84100), (0, 0)]
        msp.add_lwpolyline(points, close=False, dxfattribs={"layer": "BORDER"})

        index = GeometryIndex(doc)
        frames = detect_drawing_frames(doc, index)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["source"], "polyline")
        self.assertEqual(frames[0]["bbox"], (0.0, 0.0, 118900.0, 84100.0))


if __name__ == "__main__":
    unittest.main()


