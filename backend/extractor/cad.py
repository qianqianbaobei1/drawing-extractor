# -*- coding: utf-8 -*-
"""CAD (DWG / DXF) 原生解析与渲染转换流水线。

本模块实现：
1. DWG -> DXF / SVG 本地无头转码；
2. DXF -> 结构化矢量实体与文本提取 (TEXT / MTEXT / INSERT 属性)；
3. DXF / SVG -> 标准 PDF 高保真光栅化，无缝对接下游 VisionExtractor 及 Web 视口。
"""

import concurrent.futures
import math
import os
import re
import shutil
import subprocess
from typing import Any

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext, layout
from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration, HatchPolicy
from ezdxf.addons.drawing.pymupdf import PyMuPdfBackend
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.math import BoundingBox2d
from ezdxf.path import Path
from ezdxf.fonts import ttfonts
import pymupdf

# 防护补丁：针对 CAD 图纸中引用的生僻或特殊损坏字形，防止 fontTools 抛出 'Glyph' object has no attribute 'flags' 导致渲染中断
_orig_get_glyph_path = getattr(ttfonts.TTFontRenderer, "get_glyph_path", None)
if _orig_get_glyph_path:
    def _safe_get_glyph_path(self, char: str):
        try:
            return _orig_get_glyph_path(self, char)
        except Exception:
            fallback_path = ttfonts.GlyphPath(Path())
            self._glyph_path_cache[char] = fallback_path
            return fallback_path.clone()

    ttfonts.TTFontRenderer.get_glyph_path = _safe_get_glyph_path

MAX_TEXT_ENTITIES_LIMIT = 25000

SYSTEM_KEYWORDS_INCLUDE = [
    "系统图", "接线图", "原理图", "干线图", "拓扑图", "一次图", "二次图", "结线图"
]
SYSTEM_KEYWORDS_EXCLUDE = [
    "平面图", "布置图", "接地平面", "防雷平面", "电缆敷设", "管线综合", "抗震说明", "设计说明", "图纸目录", "防爆区域划分"
]


def is_cad_path(path: str) -> bool:
    """检查文件是否为 DWG 或 DXF 格式。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in (".dwg", ".dxf")


def find_dwg2dxf_tool() -> str | None:
    """查找系统中可用的 dwg2dxf 工具路径。"""
    common_paths = [
        "/opt/homebrew/bin/dwg2dxf",
        "/usr/local/bin/dwg2dxf",
        "/usr/bin/dwg2dxf",
    ]
    for p in common_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("dwg2dxf")


def find_dwg2svg_tool() -> str | None:
    """查找系统中可用的 dwg2SVG 工具路径。"""
    common_paths = [
        "/opt/homebrew/bin/dwg2SVG",
        "/usr/local/bin/dwg2SVG",
        "/usr/bin/dwg2SVG",
    ]
    for p in common_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("dwg2SVG")


def sanitize_surrogates(text: str) -> str:
    """清理字符串中的非法 Unicode 代理对（surrogates），防止 UTF-8 编码与 JSON 序列化崩溃。"""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return text.encode("utf-8", "ignore").decode("utf-8")


def clean_mtext(raw_text: str) -> str:
    """清理 AutoCAD MTEXT 常见的格式控制代码（如 \\P, \\A1;, \\fSimSun; 等）。"""
    if not raw_text:
        return ""
    t = sanitize_surrogates(raw_text)
    # 替换特殊电气工程字符
    t = t.replace("%%c", "Φ").replace("%%C", "Φ").replace("%%d", "°").replace("%%p", "±")
    # 替换换行控制
    t = t.replace(r"\P", "\n").replace(r"\p", "\n")
    # 移除字体、堆叠、颜色等控制码 \F...; \C...; \H...; \W...; \A...;
    t = re.sub(r"\\[A-Za-z0-9]+\;?", "", t)
    # 移除花括号堆叠分组 {}
    t = re.sub(r"[{}]", "", t)
    # 合并多余空白
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t


def dwg_to_dxf(dwg_path: str, dxf_path: str) -> bool:
    """调用 LibreDWG 的 dwg2dxf 工具将 DWG 转换为 DXF。"""
    tool = find_dwg2dxf_tool()
    if not tool:
        raise RuntimeError("未检测到 dwg2dxf 转换工具，请确保已安装 libredwg")
    cmd = [tool, "-y", "-o", dxf_path, dwg_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if res.returncode == 0 and os.path.exists(dxf_path) and os.path.getsize(dxf_path) > 0:
        return True
    return False


def dwg_to_svg_fallback(dwg_path: str, svg_path: str) -> bool:
    """使用 dwg2SVG 直接将 DWG 输出为 SVG（备用路径）。"""
    tool = find_dwg2svg_tool()
    if not tool:
        return False
    cmd = [tool, dwg_path]
    with open(svg_path, "wb") as f_out:
        res = subprocess.run(cmd, stdout=f_out, stderr=subprocess.PIPE, check=False)
    return res.returncode == 0 and os.path.exists(svg_path) and os.path.getsize(svg_path) > 0


def load_dxf_document(dxf_path: str):
    """安全读取 DXF 文件，在格式异常时自动尝试 recover 模式。"""
    try:
        return ezdxf.readfile(dxf_path)
    except Exception:
        from ezdxf import recover
        doc, _ = recover.readfile(dxf_path)
        return doc


def is_system_title(text: str) -> bool:
    """判断文字是否属于电气系统图标题。"""
    t = text.replace(" ", "").replace("\n", "").strip()
    if not t or len(t) > 35:
        return False
    # 排除说明句式标点
    if any(p in t for p in ["。", "；", "，", ";", ","]):
        return False
    # 排除序号开头的施工/设计附注（如 "1.", "2）", "7.6", "(3)"）
    if re.match(r"^(\d+[\.\、\)\）]|\d+\.\d+|\(\d+\)|（\d+）)", t):
        return False
    # 排除包含说明句式词汇
    if any(k in t for k in ["详见", "详弱电", "本子项", "本配电", "安装在", "取自", "规范", "设计说明", "技术要求"]):
        return False
    has_inc = any(k in t for k in SYSTEM_KEYWORDS_INCLUDE)
    if not has_inc:
        return False
    has_exc = any(k in t for k in SYSTEM_KEYWORDS_EXCLUDE)
    if has_exc and "系统图" not in t:
        return False
    return True


def detect_system_sheets(doc: Any) -> list[dict[str, Any]]:
    """扫描 CAD 模型空间中的图框标题栏，自动识别所有电气系统图并定位其坐标与包围盒。"""
    msp = doc.modelspace()
    candidates = []

    # 1. 扫描单行文字 TEXT 与多行文字 MTEXT
    for e in list(msp.query("TEXT")) + list(msp.query("MTEXT")):
        try:
            t = getattr(e.dxf, "text", "") if e.dxftype() == "TEXT" else getattr(e, "text", "")
            cleaned = clean_mtext(t)
            if is_system_title(cleaned):
                if any(cleaned.endswith(sfx) or cleaned.startswith(sfx) for sfx in ["系统图", "接线图", "原理图", "干线图", "拓扑图"]) or (
                    "系统图" in cleaned and ("一" in cleaned or "二" in cleaned or "三" in cleaned or "四" in cleaned or "五" in cleaned or "六" in cleaned or "七" in cleaned or "八" in cleaned or "九" in cleaned or "十" in cleaned or bool(re.search(r"\d", cleaned)))
                ):
                    candidates.append({
                        "title": cleaned,
                        "x": float(e.dxf.insert.x),
                        "y": float(e.dxf.insert.y),
                        "layer": str(getattr(e.dxf, "layer", "")),
                        "height": float(getattr(e.dxf, "height", 10.0) if e.dxftype() == "TEXT" else getattr(e.dxf, "char_height", 10.0)),
                    })
        except Exception:
            pass

    # 2. 扫描块参照 INSERT (支持正规设计院带 ATTRIB 属性的图框块)
    for ins in msp.query("INSERT"):
        try:
            ins_x, ins_y = float(ins.dxf.insert.x), float(ins.dxf.insert.y)
            ins_layer = str(getattr(ins.dxf, "layer", ""))
            for attrib in getattr(ins, "attribs", []):
                val = clean_mtext(getattr(attrib.dxf, "text", ""))
                tag = str(getattr(attrib.dxf, "tag", "")).upper()
                if is_system_title(val):
                    candidates.append({
                        "title": val,
                        "x": float(attrib.dxf.insert.x) if hasattr(attrib.dxf, "insert") else ins_x,
                        "y": float(attrib.dxf.insert.y) if hasattr(attrib.dxf, "insert") else ins_y,
                        "layer": ins_layer,
                        "height": float(getattr(attrib.dxf, "height", 20.0)),
                    })
        except Exception:
            pass

    if not candidates:
        return []

    # 3. 过滤图纸目录表（同一 X 轴紧密垂直堆叠，avg dy < 4000）
    by_x: dict[int, list[dict[str, Any]]] = {}
    for c in candidates:
        bucket = round(c["x"] / 2000) * 2000
        by_x.setdefault(bucket, []).append(c)

    filtered = []
    for bucket, group in by_x.items():
        if len(group) >= 3:
            ys = sorted([g["y"] for g in group])
            diffs = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
            avg_diff = sum(diffs) / len(diffs)
            if avg_diff < 4000:
                continue
        filtered.extend(group)

    if not filtered:
        return []

    # 4. 去重并优先选取图签栏或大字高标题
    sheet_map: dict[str, dict[str, Any]] = {}
    for c in filtered:
        name = c["title"]
        score = 0
        if any(k in c["layer"] for k in ["图签", "图框", "TITLE", "PUB_TITLE", "BORDER"]):
            score += 20
        elif any(k in c["layer"] for k in ["PUB_TEXT", "01"]):
            score += 10
        if c["height"] >= 300:
            score += 5
        if name not in sheet_map or score > sheet_map[name]["score"]:
            c["score"] = score
            sheet_map[name] = c

    # 5. 按 Y 降序、X 升序排列
    detected = sorted(sheet_map.values(), key=lambda s: (-round(s["y"], -4), s["x"]))

    # 6. 扫描图框物理闭合多段线 (LWPOLYLINE / POLYLINE) 以获得高精度真实边界
    border_boxes: list[tuple[float, float, float, float]] = []
    for pl in list(msp.query("LWPOLYLINE")) + list(msp.query("POLYLINE")):
        try:
            is_closed = getattr(pl, "is_closed", False) or getattr(pl.dxf, "flags", 0) & 1
            if not is_closed:
                continue
            pts = [p[:2] for p in (list(pl.vertices()) if hasattr(pl, "vertices") else list(pl.get_points("xy")))]
            if len(pts) >= 4:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                min_px, max_px = min(xs), max(xs)
                min_py, max_py = min(ys), max(ys)
                pw, ph = max_px - min_px, max_py - min_py
                if pw > 1000 and ph > 600:
                    aspect = max(pw, ph) / min(pw, ph)
                    if 1.2 <= aspect <= 1.8:
                        border_boxes.append((min_px, min_py, max_px, max_py))
        except Exception:
            pass

    # 7. 计算每张图框的实际包围盒 (Bounding Box)
    sheet_w = 118900.0
    if len(detected) >= 2:
        xs = sorted(s["x"] for s in detected)
        diffs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] >= 80000]
        if diffs:
            sheet_w = min(diffs)
    elif len(detected) == 1:
        # 单图自适应比例尺推导 (根据大字高推算 scale)
        th = detected[0].get("height", 10.0)
        scale = max(1.0, th / 3.5) if th > 15 else 1.0
        sheet_w = 1189.0 * scale

    sheet_h = sheet_w / 1.414  # ISO 216 标准宽高比 1.414

    for s in detected:
        tx, ty = s["x"], s["y"]
        # 优先使用包围该图签锚点的真实闭合多段线
        matched_box = None
        for bx0, by0, bx1, by1 in border_boxes:
            if bx0 <= tx <= bx1 and by0 <= ty <= by1:
                matched_box = (round(bx0, 1), round(by0, 1), round(bx1, 1), round(by1, 1))
                break
        if matched_box:
            s["bbox"] = matched_box
        else:
            s["bbox"] = (
                round(tx - sheet_w * 0.88, 1),
                round(ty - sheet_h * 0.05, 1),
                round(tx + sheet_w * 0.12, 1),
                round(ty + sheet_h * 0.95, 1),
            )

    return detected


def slice_and_render_cad_sheets(doc: Any, sheets: list[dict[str, Any]], out_pdf_path: str) -> list[dict[str, Any]]:
    """针对检测出的多个电气系统图进行单图框矢量切片与拼接，输出高清多页 PDF 与对应文字元数据。

    采用空间网格索引 (Spatial Grid Indexing) 与电气实体预剪枝，消除无用图元遍历，
    并发多线程极速渲染，保障工业级超大 DWG 秒级稳定切片。
    """
    msp = doc.modelspace()
    combined_pdf = pymupdf.open()
    total_texts: list[dict[str, Any]] = []

    if not sheets:
        return []

    # 1. 电气核心实体类型白名单（过滤 SPLINE、HATCH、DIMENSION 等耗时上万条的无用非电气装饰图元）
    ALLOWED_TYPES = {
        "LINE", "LWPOLYLINE", "POLYLINE", "ARC", "CIRCLE", "TEXT", "MTEXT", "INSERT", "SOLID"
    }

    # 2. 空间网格索引加速 (Cell Size 35000)
    CELL_SIZE = 35000.0
    grid: dict[tuple[int, int], list[tuple[Any, float, float]]] = {}

    for e in msp:
        try:
            if e.dxftype() not in ALLOWED_TYPES:
                continue
            bounds = _entity_bounds(e)
            if bounds is None:
                continue
            px, py = (bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0
            gx = int(px // CELL_SIZE)
            gy = int(py // CELL_SIZE)
            grid.setdefault((gx, gy), []).append((e, px, py))
        except Exception:
            pass

    def _render_sheet_worker(idx: int, sheet: dict[str, Any]) -> tuple[int, list[dict[str, Any]], bytes]:
        sheet_name = sheet["title"]
        bx0, by0, bx1, by1 = sheet["bbox"]

        # 从空间网格中仅提取与当前图框相交单元格的实体（亚毫秒级检索）
        min_gx, max_gx = int(bx0 // CELL_SIZE), int(bx1 // CELL_SIZE)
        min_gy, max_gy = int(by0 // CELL_SIZE), int(by1 // CELL_SIZE)

        candidates = []
        for gx in range(min_gx, max_gx + 1):
            for gy in range(min_gy, max_gy + 1):
                candidates.extend(grid.get((gx, gy), []))

        sheet_doc = ezdxf.new(doc.dxfversion)
        sheet_msp = sheet_doc.modelspace()

        sheet_texts = []
        for e, px, py in candidates:
            if bx0 <= px <= bx1 and by0 <= py <= by1:
                try:
                    sheet_msp.add_foreign_entity(e, copy=True)
                    if e.dxftype() in ("TEXT", "MTEXT"):
                        raw_text = getattr(e.dxf, "text", "") if e.dxftype() == "TEXT" else getattr(e, "text", "")
                        cleaned = clean_mtext(raw_text)
                        if cleaned:
                            sheet_texts.append({
                                "type": e.dxftype(),
                                "text": cleaned,
                                "x": round(px, 1),
                                "y": round(py, 1),
                                "page": idx,
                                "sheet": sheet_name,
                            })
                except Exception:
                    pass

        try:
            # 单页渲染为高清晰度白色背景 PDF
            ctx = RenderContext(sheet_doc)
            cfg = Configuration(
                background_policy=BackgroundPolicy.WHITE,
                color_policy=ColorPolicy.COLOR,
                hatch_policy=HatchPolicy.IGNORE,
            )
            backend = SVGBackend()
            page = layout.Page.from_dxf_layout(sheet_msp)
            frontend = Frontend(ctx, backend, config=cfg)
            frontend.draw_layout(sheet_msp, finalize=True)
            svg_content = backend.get_string(page)

            clean_svg = sanitize_surrogates(svg_content).encode("utf-8", "ignore")
            page_pdf_doc = pymupdf.open(stream=clean_svg, filetype="svg")
            page_bytes = page_pdf_doc.convert_to_pdf()
        except Exception as render_err:
            print(f"[CAD] 单页切片渲染降级 (sheet={sheet_name}): {render_err}")
            # 容错降级：生成标准 A3 白色图纸占位页，保证包含提取的图框名称
            fallback_doc = pymupdf.open()
            fb_page = fallback_doc.new_page(width=1190, height=842)
            fb_page.insert_text((50, 50), f"系统图: {sheet_name} (矢量渲染降级)", fontsize=18)
            page_bytes = fallback_doc.convert_to_pdf()

        return idx, sheet_texts, page_bytes

    if len(sheets) == 1:
        idx, sheet_texts, page_bytes = _render_sheet_worker(1, sheets[0])
        total_texts.extend(sheet_texts)
        with pymupdf.open(stream=page_bytes, filetype="pdf") as single_doc:
            combined_pdf.insert_pdf(single_doc)
    else:
        max_workers = min(len(sheets), max(1, os.cpu_count() or 4))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_render_sheet_worker, idx, sheet)
                for idx, sheet in enumerate(sheets, 1)
            ]
            rendered_items = [f.result() for f in futures]

        rendered_items.sort(key=lambda x: x[0])
        for idx, sheet_texts, page_bytes in rendered_items:
            total_texts.extend(sheet_texts)
            with pymupdf.open(stream=page_bytes, filetype="pdf") as single_doc:
                combined_pdf.insert_pdf(single_doc)

    combined_pdf.save(out_pdf_path)
    return total_texts


# ---------------------------------------------------------------------------
# v2 流水线：按真实图框几何分幅，再把每张系统图拆成配电箱单元块
#
# 旧流水线用“系统图”标题文字的插入点 + 猜出来的图纸尺寸当切片边界，后果是：
#   1. 图框真实尺寸靠 X 间距最小值猜，实测猜成 49868 而真实幅面是 118900，
#      每张图被砍掉约一半幅面；
#   2. 实体只按 dxf.insert / dxf.start 归属，CIRCLE / ARC / SOLID / LWPOLYLINE /
#      POLYLINE 没有这些属性，被整类丢弃（39MB 实测丢 16,506 个），箱体轮廓、
#      母线、圆形仪表符号全部消失；
#   3. 一张 A0 系统图上排着十几个配电箱，整张当一个单元送模型，输出超 12000 token
#      被截断（任务 3bda9d34ccac 第 8 页就是这么失败的）；
#   4. 图签栏里的图名文字被当成独立图纸，页数与真实图纸数对不上。
#
# v2 改成：图框块参照（INSERT）给出精确幅面 -> 图名文字分系统图/平面图 -> 系统图内部
# 按“虚线箱框 + 箱名”配对出配电箱单元块 -> 每个单元块一页 PDF，附该块的原生 CAD 文字。
# ---------------------------------------------------------------------------

ISO_LANDSCAPE_SIZES = (
    (1189.0, 841.0), (841.0, 594.0), (594.0, 420.0), (420.0, 297.0), (297.0, 210.0),
)
FRAME_MIN_DIM = 30000.0            # 闭合多段线外框的最小边长（图纸单位）
FRAME_MIN_SIDE = 15000.0           # 块参照图框的最小短边；再小的只能是图签/箱框/符号
FRAME_MAX_ASPECT = 2.8             # 长宽比超过此值的是桥架/母线这类长条符号，不是图纸
FRAME_DEDUP_TOL = 0.02             # 面积容差：落在已知图框内部 98% 的候选视为嵌套子块
UNIT_RECT_MIN_W, UNIT_RECT_MAX_W = 3500.0, 48000.0
UNIT_RECT_MIN_H, UNIT_RECT_MAX_H = 3000.0, 52000.0
UNIT_CAPTION_SUFFIX = ("配电箱", "配电柜", "控制箱", "端子箱", "电表箱", "配电屏", "开关箱", "电源箱")
UNIT_CAPTION_BAD_LAYERS = {
    "TEL_TAB", "TEL_TITLE", "表格文字", "图框层3", "WORK", "PUB_TITLE", "图签栏", "DIM-照明",
}
# 平面图/建筑图层上也有闭合箱柜外形，但不是系统图单元块，按图层前缀排掉
UNIT_RECT_BAD_LAYER_MARKERS = ("EQUIP", "WIRE", "BEAM", "平面", "门窗", "建筑", "TEL", "FURN",
                               "DIM", "HATCH", "看线", "天棚", "楼面", "环境", "暖通")
BOX_ABOVE_MIN, BOX_ABOVE_MAX = 2000.0, 7000.0   # 箱名文字到上方箱框底边的距离窗口
BOX_X_TOLERANCE = 5000.0                       # 箱名与箱框水平中心的最大额外偏移
CROP_PAD_SIDE, CROP_PAD_TOP, CROP_PAD_BOTTOM = 500.0, 600.0, 2600.0
PAGE_LONG_MM_MIN, PAGE_LONG_MM_MAX = 240.0, 1600.0
# 配电箱单元块的页面长边固定到 A3 左右：下游按“长边 2400px”光栅化，
# 长边落在 240~320mm 才能既拿满像素又不触发大图分块（>320mm 会被再切成 4 块）。
UNIT_PAGE_LONG_MM = 297.0
PLAN_TITLE_MARKERS = ("平面图", "布置图", "剖面", "详图", "设计说明", "图纸目录", "目录",
                      "防雷平面", "接地平面", "地坪", "屋面", "立管", "门窗表")
SYSTEM_TITLE_MARKERS = ("系统图", "干线图", "原理图", "拓扑图", "接线图", "结线图", "配电图")
TITLE_LAYER_HINTS = ("图签", "TITLE", "PUB_", "图框", "图名")
GRID_CELL = 40000.0


def _entity_bounds(entity: Any) -> tuple[float, float, float, float] | None:
    """按实体类型取真实二维包围盒；取不到返回 None。

    必须分类型取锚点：CIRCLE/ARC 用 center+radius，LWPOLYLINE/POLYLINE 用顶点，
    SOLID 用 vtx0..vtx3。旧实现统一取 dxf.insert 或 dxf.start，把没有该属性的图元
    整类丢掉。
    """
    t = entity.dxftype()
    d = entity.dxf
    try:
        if t == "LINE":
            pts = [d.start, d.end]
        elif t == "POINT":
            pts = [d.location]
        elif t in ("TEXT", "MTEXT", "ATTDEF", "ATTRIB", "ACAD_TABLE", "OLE2FRAME", "SHAPE"):
            pts = [d.insert]
        elif t in ("CIRCLE", "ARC"):
            c, r = d.center, float(d.radius)
            return c.x - r, c.y - r, c.x + r, c.y + r
        elif t == "ELLIPSE":
            c, mx = d.center, d.major_axis
            r = math.hypot(float(mx.x), float(mx.y))
            return c.x - r, c.y - r, c.x + r, c.y + r
        elif t == "LWPOLYLINE":
            pts = [(p[0], p[1]) for p in entity.get_points("xy")]
        elif t == "POLYLINE":
            pts = [(p[0], p[1]) for p in entity.points()]
        elif t in ("SOLID", "TRACE", "3DFACE"):
            pts = [getattr(d, k) for k in ("vtx0", "vtx1", "vtx2", "vtx3") if d.hasattr(k)]
        elif t == "SPLINE":
            pts = list(entity.control_points) or list(entity.fit_points)
        elif t == "DIMENSION":
            pts = [getattr(d, k) for k in ("defpoint", "defpoint2", "defpoint3", "text_midpoint")
                   if d.hasattr(k)]
        elif t == "LEADER":
            pts = list(entity.vertices)
        elif t == "HATCH":
            pts = [v for p in entity.paths for v in getattr(p, "vertices", [])]
        else:
            return None
    except Exception:
        return None
    if not pts:
        return None
    xs = [float(p[0]) for p in pts]
    ys = [float(p[1]) for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _apply_matrix(m, box):
    corners = [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]
    out = [m.transform((x, y, 0.0)) for x, y in corners]
    xs = [float(p[0]) for p in out]
    ys = [float(p[1]) for p in out]
    return min(xs), min(ys), max(xs), max(ys)


def _block_bounds(doc: Any, name: str, cache: dict, depth: int = 0):
    """图块定义的局部范围（按块名缓存）。

    自己逐图元算并递归限深，而不用 ``ezdxf.bbox.extents``：后者对 AutoCAD
    Electrical 那类深层嵌套块会反复整块递归，实测一张 39MB 图要 50 秒。
    """
    if name in cache:
        return cache[name]
    cache[name] = None  # 防循环引用
    box = None
    try:
        for e in doc.blocks[name]:
            b = None
            kind = e.dxftype()
            if kind == "INSERT" and depth < 4:
                sub = _block_bounds(doc, e.dxf.name, cache, depth + 1)
                if sub is not None:
                    try:
                        b = _apply_matrix(e.matrix44(), sub)
                    except Exception:
                        b = _entity_bounds(e)
                else:
                    b = _entity_bounds(e)
            else:
                b = _entity_bounds(e)
            if b is not None:
                box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]),
                                             max(box[2], b[2]), max(box[3], b[3]))
    except Exception:
        box = None
    cache[name] = box
    return box


def _insert_bounds(doc: Any, ins: Any, cache: dict) -> tuple[float, float, float, float] | None:
    """块参照的实际包围盒：块范围四角经块参照矩阵（缩放/旋转/镜像）变换。"""
    box = _block_bounds(doc, ins.dxf.name, cache)
    if box is None:
        return None
    try:
        return _apply_matrix(ins.matrix44(), box)
    except Exception:
        return None


class GeometryIndex:
    """模型空间实体的一次性包围盒索引与空间网格。

    后续所有“这块图里有哪些图元”的判断都走它，避免每个图框/单元块重新遍历一遍
    十几万实体，也避免把实体复制进新文档（复制会丢失图层颜色、线型，还会因为
    图元类型不受支持而静默失败）。
    """

    def __init__(self, doc: Any):
        self.doc = doc
        self.entities: list[Any] = []
        self.boxes: list[tuple[float, float, float, float]] = []
        self._grid: dict[tuple[int, int], list[int]] = {}
        self.block_cache: dict = {}          # 块定义局部范围（块名 -> bbox）
        self.block_text_cache: dict = {}     # 块定义内部文字（块名 -> [文字]）
        for e in doc.modelspace():
            b = _insert_bounds(doc, e, self.block_cache) if e.dxftype() == "INSERT" else _entity_bounds(e)
            if b is None:
                continue
            i = len(self.entities)
            self.entities.append(e)
            self.boxes.append(b)
            for gx in range(int(b[0] // GRID_CELL), int(b[2] // GRID_CELL) + 1):
                for gy in range(int(b[1] // GRID_CELL), int(b[3] // GRID_CELL) + 1):
                    self._grid.setdefault((gx, gy), []).append(i)

    def query(self, rect: tuple[float, float, float, float]) -> list[int]:
        """返回包围盒与 rect 相交的实体下标（包围盒相交即算，跨框的整条线不会被切掉归属）。"""
        x0, y0, x1, y1 = rect
        seen = set()
        out = []
        for gx in range(int(x0 // GRID_CELL), int(x1 // GRID_CELL) + 1):
            for gy in range(int(y0 // GRID_CELL), int(y1 // GRID_CELL) + 1):
                for i in self._grid.get((gx, gy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    b = self.boxes[i]
                    if b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0:
                        out.append(i)
        return out

    def contains(self, rect, point) -> bool:
        return rect[0] <= point[0] <= rect[2] and rect[1] <= point[1] <= rect[3]


def detect_drawing_frames(doc: Any, index: GeometryIndex) -> list[dict[str, Any]]:
    """找出图纸上所有真实图框幅面。

    优先用图框块参照（``横式A0`` / ``2021版A0图框`` 这类），它们的范围就是精确幅面，
    不依赖任何尺寸猜测；嵌套在图框内的图签/会签栏小块按包含关系与高重叠比例剔掉。
    若无图框块，退而使用大尺寸闭合（或几何首尾重合）多段线外框；
    若仍无，则根据标题启发式推导的系统图幅面接入，确保统一走入 v2 切片与高清渲染引擎。
    """
    frames: list[dict[str, Any]] = []
    for e in doc.modelspace().query("INSERT"):
        b = _insert_bounds(doc, e, index.block_cache)
        if b is None:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if min(w, h) < FRAME_MIN_SIDE:
            continue
        if max(w, h) / max(min(w, h), 1e-6) > FRAME_MAX_ASPECT:
            continue
        frames.append({"bbox": b, "block": str(e.dxf.name), "source": "insert"})

    frames.sort(key=lambda f: -((f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1])))
    kept: list[dict[str, Any]] = []
    for f in frames:
        b = f["bbox"]
        area = (b[2] - b[0]) * (b[3] - b[1])
        nested = False
        for k in kept:
            kb = k["bbox"]
            if kb[0] <= b[0] + 10.0 and kb[2] >= b[2] - 10.0 and kb[1] <= b[1] + 10.0 and kb[3] >= b[3] - 10.0:
                nested = True
                break
            ix = max(0.0, min(b[2], kb[2]) - max(b[0], kb[0]))
            iy = max(0.0, min(b[3], kb[3]) - max(b[1], kb[1]))
            if ix * iy >= area * (1.0 - FRAME_DEDUP_TOL):
                nested = True
                break
        if not nested:
            kept.append(f)

    if not kept:
        polyline_cands = []
        for i in range(len(index.entities)):
            e = index.entities[i]
            if e.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
                continue
            is_closed = False
            try:
                if e.dxftype() == "LWPOLYLINE":
                    if e.closed:
                        is_closed = True
                    else:
                        pts = e.get_points("xy")
                        if len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 100.0:
                            is_closed = True
                else:
                    if e.is_closed:
                        is_closed = True
                    else:
                        pts = list(e.points())
                        if len(pts) >= 4 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 100.0:
                            is_closed = True
            except Exception:
                continue
            if not is_closed:
                continue
            b = index.boxes[i]
            w, h = b[2] - b[0], b[3] - b[1]
            if min(w, h) < FRAME_MIN_DIM:
                continue
            if not 1.05 <= (max(w, h) / max(min(w, h), 1e-6)) <= 2.4:
                continue
            polyline_cands.append({"bbox": b, "block": str(e.dxf.layer), "source": "polyline"})

        polyline_cands.sort(key=lambda f: -((f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1])))
        for f in polyline_cands:
            b = f["bbox"]
            area = (b[2] - b[0]) * (b[3] - b[1])
            nested = False
            for k in kept:
                kb = k["bbox"]
                if kb[0] <= b[0] + 10.0 and kb[2] >= b[2] - 10.0 and kb[1] <= b[1] + 10.0 and kb[3] >= b[3] - 10.0:
                    nested = True
                    break
                ix = max(0.0, min(b[2], kb[2]) - max(b[0], kb[0]))
                iy = max(0.0, min(b[3], kb[3]) - max(b[1], kb[1]))
                if ix * iy >= area * (1.0 - FRAME_DEDUP_TOL):
                    nested = True
                    break
            if not nested:
                kept.append(f)

    if not kept:
        # 兜底：若无标准图框块或闭合多段线，采用标题启发式推导的系统图幅面接入
        fallback_sheets = detect_system_sheets(doc)
        for s in fallback_sheets:
            kept.append({"bbox": s["bbox"], "block": s.get("title", "系统图"), "source": "title_heuristic"})

    kept.sort(key=lambda f: (-round(f["bbox"][3], -4), f["bbox"][0]))
    return kept


def _guess_plot_scale(w: float, h: float) -> float:
    """由幅面尺寸推断出图比例（1:N 的 N）。

    只用于给渲染定物理页面尺寸。按 ISO A 系列横放匹配最接近的一种，再对
    N 做一次合理区间收口，保证下游按“长边约 2400px”光栅化时不会撞到 DPI 上下限。
    """
    long_u, short_u = max(w, h), max(min(w, h), 1e-6)
    best, best_err = None, None
    for iso_long, iso_short in ISO_LANDSCAPE_SIZES:
        s1, s2 = long_u / iso_long, short_u / iso_short
        mean = (s1 + s2) / 2.0
        err = abs(s1 - s2) / max(mean, 1e-9)
        if best is None or err < best_err:
            best, best_err = mean, err
    scale = max(float(best or 100.0), 1e-6)
    long_mm = long_u / scale
    if long_mm < PAGE_LONG_MM_MIN:
        scale = long_u / PAGE_LONG_MM_MIN
    elif long_mm > PAGE_LONG_MM_MAX:
        scale = long_u / PAGE_LONG_MM_MAX
    return scale


def _looks_like_box_caption(text: str) -> bool:
    t = (text or "").strip()
    if not 3 <= len(t) <= 26 or not t.endswith(UNIT_CAPTION_SUFFIX):
        return False
    head = t[:-3].strip()
    return bool(head) and head[0] not in "由详注如按本除并"


def _frame_texts(index: GeometryIndex, rect, cached: dict) -> list[dict[str, Any]]:
    """取落在矩形内的文字（含块参照内部文字与属性）。"""
    if rect in cached:
        return cached[rect]
    out: list[dict[str, Any]] = []
    for i in index.query(rect):
        e = index.entities[i]
        kind = e.dxftype()
        if kind in ("TEXT", "MTEXT"):
            raw = e.dxf.text if kind == "TEXT" else e.text
            txt = clean_mtext(raw)
            if txt:
                out.append({"text": txt, "x": float(e.dxf.insert.x), "y": float(e.dxf.insert.y),
                            "layer": str(e.dxf.layer), "height": _text_height(e, kind)})
        elif kind == "INSERT":
            for sub in _explode_texts(e):
                if rect[0] <= sub["x"] <= rect[2] and rect[1] <= sub["y"] <= rect[3]:
                    out.append(sub)
    cached[rect] = out
    return out


def _text_height(e: Any, kind: str) -> float:
    try:
        return float(e.dxf.height if kind == "TEXT" else e.dxf.char_height)
    except Exception:
        return 10.0


def _frame_texts(index: GeometryIndex, rect, cached: dict, block_text: bool = False) -> list[dict[str, Any]]:
    """取落在矩形内的文字。

    ``block_text=False`` 只读模型空间文字（图名、箱名都在模型空间，最便宜）；
    ``block_text=True`` 再把块参照内部的文字与属性折成绝对坐标一并取出，
    供原生文字层送给视觉模型参考。
    """
    key = (rect, block_text)
    if key in cached:
        return cached[key]
    texts: list[dict[str, Any]] = []
    inserts: list[Any] = []
    for i in index.query(rect):
        e = index.entities[i]
        kind = e.dxftype()
        if kind == "TEXT" or kind == "MTEXT":
            raw = e.dxf.text if kind == "TEXT" else e.text
            txt = clean_mtext(raw)
            if txt:
                texts.append({"text": txt, "x": float(e.dxf.insert.x), "y": float(e.dxf.insert.y),
                              "layer": str(e.dxf.layer), "height": _text_height(e, kind)})
        elif kind == "INSERT":
            inserts.append(e)
    if block_text:
        for ins in inserts:
            for sub in _explode_texts(ins, index.block_text_cache):
                if rect[0] <= sub["x"] <= rect[2] and rect[1] <= sub["y"] <= rect[3]:
                    texts.append(sub)
    cached[key] = texts
    return texts


def _local_block_texts(doc: Any, name: str, cache: dict, depth: int = 0) -> list[dict[str, Any]]:
    """图块定义内部的文字，坐标已折到该块自身的局部坐标系（按块名缓存）。

    同一块名在模型空间可能被引用上千次，逐引用展开会重复几十万次；按块名缓存后
    每个块定义只展开一次。
    """
    if name in cache:
        return cache[name]
    cache[name] = []
    out: list[dict[str, Any]] = []
    try:
        for e in doc.blocks[name]:
            kind = e.dxftype()
            if kind in ("TEXT", "MTEXT", "ATTRIB"):
                raw = e.text if kind == "MTEXT" else getattr(e.dxf, "text", "")
                txt = clean_mtext(raw)
                if txt:
                    out.append({"text": txt, "x": float(e.dxf.insert.x), "y": float(e.dxf.insert.y),
                                "layer": str(getattr(e.dxf, "layer", "")), "height": _text_height(e, kind)})
            elif kind == "INSERT" and depth < 4:
                sub = _local_block_texts(doc, e.dxf.name, cache, depth + 1)
                if not sub:
                    continue
                try:
                    m = e.matrix44()
                except Exception:
                    continue
                for s in sub:
                    p = m.transform((s["x"], s["y"], 0.0))
                    out.append({**s, "x": float(p[0]), "y": float(p[1])})
    except Exception:
        pass
    cache[name] = out
    return out


def _explode_texts(ins: Any, cache: dict) -> list[dict[str, Any]]:
    """取块参照内部的文字与属性文字，坐标折到模型空间绝对坐标。"""
    out: list[dict[str, Any]] = []
    doc = getattr(ins, "doc", None)
    if doc is not None:
        local = _local_block_texts(doc, ins.dxf.name, cache)
        if local:
            try:
                m = ins.matrix44()
            except Exception:
                m = None
            for s in local:
                if m is not None:
                    p = m.transform((s["x"], s["y"], 0.0))
                    out.append({**s, "x": float(p[0]), "y": float(p[1])})
                else:
                    out.append(dict(s))
    for attrib in getattr(ins, "attribs", []):
        txt = clean_mtext(str(getattr(attrib.dxf, "text", "")))
        if txt:
            out.append({"text": txt, "x": float(attrib.dxf.insert.x), "y": float(attrib.dxf.insert.y),
                        "layer": str(getattr(attrib.dxf, "layer", "")),
                        "height": _text_height(attrib, "ATTRIB")})
    return out


def _frame_title(texts: list[dict[str, Any]]) -> str:
    """图名：优先图签栏里带“系统图/干线图/原理图”字样的短文字（避开图号、子项名称、公司名）。"""
    def pick(pool, want_marker: bool):
        best, best_h = "", 0.0
        for t in pool:
            txt = t["text"].replace("\n", "").strip()
            if not txt or len(txt) > 30:
                continue
            if any(p in txt for p in ("。", "；", "，", ";", ",")):
                continue
            if want_marker and not any(m in txt for m in SYSTEM_TITLE_MARKERS):
                continue
            h = t["height"]
            if h > best_h or (h == best_h and len(txt) > len(best)):
                best, best_h = txt, h
        return best

    on_title_layer = [t for t in texts if any(k in t["layer"].upper() for k in TITLE_LAYER_HINTS)]
    return (pick(on_title_layer, True) or pick(texts, True)
            or pick(on_title_layer, False) or pick(texts, False))


def _classify_frame(title: str) -> str:
    t = title.replace(" ", "")
    if any(m in t for m in SYSTEM_TITLE_MARKERS):
        return "system"
    if any(m in t for m in PLAN_TITLE_MARKERS):
        return "plan"
    return "other"


def _unit_rects(index: GeometryIndex, frame_rect) -> list[tuple[float, float, float, float]]:
    """图框内所有配电箱虚线箱框（小尺寸闭合多段线），已按近似重复去重。"""
    rects: list[tuple[float, float, float, float]] = []
    for i in index.query(frame_rect):
        e = index.entities[i]
        if e.dxftype() != "LWPOLYLINE":
            continue
        try:
            if not e.closed:
                continue
        except Exception:
            continue
        layer = str(e.dxf.layer or "").upper()
        if any(m in layer for m in UNIT_RECT_BAD_LAYER_MARKERS):
            continue
        b = index.boxes[i]
        w, h = b[2] - b[0], b[3] - b[1]
        if not (UNIT_RECT_MIN_W <= w <= UNIT_RECT_MAX_W and UNIT_RECT_MIN_H <= h <= UNIT_RECT_MAX_H):
            continue
        if any(abs(b[0] - r[0]) < 200 and abs(b[1] - r[1]) < 200 and abs(b[2] - r[2]) < 200
               and abs(b[3] - r[3]) < 200 for r in rects):
            continue
        rects.append(b)
    return rects


def _match_box_rect(rects, cap) -> tuple[float, float, float, float] | None:
    """给箱名文字找它所属的箱框：优先“包含箱名”的框，否则找正上方最近的框。"""
    cx, cy = cap["x"], cap["y"]
    inside = [r for r in rects if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]]
    if inside:
        return min(inside, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    cands = []
    for r in rects:
        dy = r[1] - cy
        if not BOX_ABOVE_MIN * -1 <= dy <= BOX_ABOVE_MAX:
            continue
        half = (r[2] - r[0]) / 2.0 + BOX_X_TOLERANCE
        if abs(cx - (r[0] + r[2]) / 2.0) > half:
            continue
        cands.append((abs(dy) if dy >= 0 else dy + 1e6, r))
    if not cands:
        return None
    return min(cands, key=lambda c: c[0])[1]


def _column_pitch(rects) -> float:
    """箱框按列平铺的列距（相邻列左边距的中位数），用于推最右一列单元块的右边。"""
    xs = sorted({round(r[0], -2) for r in rects})
    diffs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1) if xs[i + 1] - xs[i] > 1000]
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def _cell_crop(rect, rects, frame, caption_y: float, pitch_x: float) -> tuple[float, float, float, float]:
    """把箱框撑成完整的单元块。

    图纸上的虚线箱框只圈住断路器那一列，右侧的电缆规格栏和负荷名称栏画在框外（实测
    每页被截掉两列），箱框左侧反而是内容起点。所以：
      左边界 = 箱框左边 - 少量留白（箱框左就是内容起点）；
      右边界 = 右邻箱框的左边（相邻箱内容首尾相接，没有空带可依靠）；
      右邻不存在时用整张图的列距推；
      下边界 = 自适应上下两行配电箱间距，严格收于下邻箱框顶部之上，杜绝带进下排表头。
    """
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0

    def v_overlap(r):
        return min(r[3], y1) - max(r[1], y0) > 0.25 * min(h, r[3] - r[1])

    def h_overlap(r):
        return min(r[2], x1) - max(r[0], x0) > 0.25 * min(w, r[2] - r[0])

    right_gaps = [r[0] - x1 for r in rects if r is not rect and v_overlap(r) and r[0] >= x1]
    top_gaps = [r[1] - y1 for r in rects if r is not rect and h_overlap(r) and r[1] >= y1]
    if right_gaps:
        right = x1 + min(right_gaps)
    else:
        right = x0 + (pitch_x if pitch_x > w else w * 1.6)
    right = min(right, x1 + w * 1.6, frame[2])
    top = min(y1 + (min(top_gaps) / 2 if top_gaps else min(h * 0.25, 6000.0)), frame[3])

    # 下邻箱框自适应安全裁切（同列且位于当前箱框下方）
    bottom_cands = [r for r in rects if r is not rect and h_overlap(r) and r[3] <= y0]
    if bottom_cands:
        below_top = max(r[3] for r in bottom_cands)
        # 当前箱名下边界：箱名文字下方留白 250~350
        cap_bot = min(caption_y - 250.0, y0 - 300.0) if caption_y < y0 else y0 - 800.0
        # 严格取箱名下沿与下排箱顶的中位线，并至少高出下排箱顶 100，坚决不切入下排进线与表头
        bottom = max(cap_bot, (cap_bot + below_top) / 2.0, below_top + 100.0)
    else:
        if caption_y < y0:
            bottom = min(y0 - 800.0, caption_y - 300.0)
        else:
            bottom = y0 - CROP_PAD_BOTTOM
    bottom = max(frame[1], bottom)

    return (max(frame[0], x0 - CROP_PAD_SIDE), bottom, right, top)


def build_unit_blocks(doc: Any, index: GeometryIndex, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把系统图图框拆成配电箱单元块。

    一个配电箱 = 箱名文字 + 它上方的虚线箱框 = 一个单元块。箱框只用来定裁剪范围，
    真正决定“有几个单元”的是箱名文字，因为设计人一定给每个箱写了箱名，而框可能有
    重复或拆分。

    图框是否算系统图，由内容判定而不是靠图名猜：框里有“箱名 + 箱框”配对才算，
    一个都配不上、且图名也不是系统图类的图框（平面图、图签注释块等）整张丢掉。
    """
    text_cache: dict = {}
    blocks: list[dict[str, Any]] = []
    for frame in frames:
        fbox = frame["bbox"]
        texts = _frame_texts(index, fbox, text_cache)
        frame["title"] = _frame_title(texts)
        if _classify_frame(frame["title"]) == "plan":
            continue

        rects = _unit_rects(index, fbox)
        captions = [t for t in texts
                    if _looks_like_box_caption(t["text"])
                    and t["layer"] not in UNIT_CAPTION_BAD_LAYERS]
        scale = _guess_plot_scale(fbox[2] - fbox[0], fbox[3] - fbox[1])
        pitch_x = _column_pitch(rects)
        made = 0
        used: set[int] = set()
        for cap in sorted(captions, key=lambda t: (-t["y"], t["x"])):
            rect = _match_box_rect(rects, cap)
            if rect is None or id(rect) in used:
                continue
            crop = _cell_crop(rect, rects, fbox, cap["y"], pitch_x)
            label = cap["text"].replace(" ", "")
            long_units = max(crop[2] - crop[0], crop[3] - crop[1])
            block_scale = max(long_units / UNIT_PAGE_LONG_MM, 1e-6)
            blocks.append({"rect": crop, "label": label, "frame": frame["title"],
                           "scale": block_scale})
            made += 1
            used.add(id(rect))
        if made:
            frame["kind"] = "system"
        elif _classify_frame(frame["title"]) == "system":
            frame["kind"] = "system"
            blocks.append({"rect": fbox, "label": frame["title"] or "系统图",
                           "frame": frame["title"], "scale": scale})
        else:
            frame["kind"] = "skip"
    return blocks


def _render_block_pdf(doc: Any, index: GeometryIndex, block: dict[str, Any], ctx: RenderContext,
                      cfg: Configuration) -> bytes:
    """把一个单元块渲染成单页 PDF：只画该块相交的实体，靠 render_box 精确裁切。"""
    x0, y0, x1, y1 = block["rect"]
    w, h = x1 - x0, y1 - y0
    scale = block["scale"]
    page = layout.Page(w / scale, h / scale, layout.Units.mm)
    settings = layout.Settings(scale=1.0 / scale)
    entities = [index.entities[i] for i in index.query((x0, y0, x1, y1))]
    backend = PyMuPdfBackend()
    frontend = Frontend(ctx, backend, config=cfg)
    frontend.set_background("#ffffff")
    frontend.draw_entities(entities)
    return backend.get_pdf_bytes(page, settings=settings,
                                 render_box=BoundingBox2d([(x0, y0), (x1, y1)]))


def render_cad_unit_blocks(doc: Any, blocks: list[dict[str, Any]], out_pdf_path: str,
                           index: GeometryIndex) -> list[dict[str, Any]]:
    """逐块渲染并合成多页 PDF，同时按页导出原生 CAD 文字。"""
    ctx = RenderContext(doc)
    ctx.set_current_layout(doc.modelspace())
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.COLOR,
                        hatch_policy=HatchPolicy.IGNORE)
    combined = pymupdf.open()
    texts: list[dict[str, Any]] = []
    text_cache: dict = {}
    try:
        for idx, block in enumerate(blocks, 1):
            try:
                page_bytes = _render_block_pdf(doc, index, block, ctx, cfg)
            except Exception as exc:  # noqa: BLE001 - 单块渲染失败不能拖垮整张图
                print(f"[CAD] 单元块渲染降级 ({block.get('label')}): {exc}")
                page_bytes = _fallback_page(block)
            with pymupdf.open(stream=page_bytes, filetype="pdf") as page_doc:
                combined.insert_pdf(page_doc)
            for t in _frame_texts(index, block["rect"], text_cache, block_text=True):
                texts.append({"type": "TEXT", "text": t["text"], "x": round(t["x"], 1),
                              "y": round(t["y"], 1), "page": idx, "sheet": block["label"],
                              "layer": t["layer"]})
    finally:
        combined.save(out_pdf_path)
        combined.close()
    texts.sort(key=lambda item: (item["page"], -item["y"], item["x"]))
    return texts


def _fallback_page(block: dict[str, Any]) -> bytes:
    """单块矢量渲染失败时的白底占位页，保证页序与块序一一对应。"""
    x0, y0, x1, y1 = block["rect"]
    scale = block["scale"]
    doc = pymupdf.open()
    page = doc.new_page(width=(x1 - x0) / scale / 25.4 * 72, height=(y1 - y0) / scale / 25.4 * 72)
    page.insert_text((36, 36), f"{block.get('label', '')} (矢量渲染降级)", fontsize=14)
    return doc.convert_to_pdf()


def extract_cad_entities(dxf_path: str, doc: Any = None) -> list[dict[str, Any]]:
    """从 DXF 文件中高精度提取所有带坐标的文字及块属性实体。"""
    extracted: list[dict[str, Any]] = []
    if doc is None:
        try:
            doc = load_dxf_document(dxf_path)
        except Exception as exc:
            print(f"读取 DXF 失败: {exc}")
            return []

    msp = doc.modelspace()

    # 提取单行文字 TEXT
    for entity in msp.query("TEXT"):
        try:
            raw_text = getattr(entity.dxf, "text", "") or ""
            text = clean_mtext(raw_text)
            if not text:
                continue
            insert = entity.dxf.insert
            height = float(getattr(entity.dxf, "height", 10.0))
            rotation = float(getattr(entity.dxf, "rotation", 0.0))
            layer = sanitize_surrogates(str(getattr(entity.dxf, "layer", "0")))
            extracted.append({
                "type": "TEXT",
                "text": text,
                "x": round(float(insert.x), 2),
                "y": round(float(insert.y), 2),
                "height": round(height, 2),
                "rotation": round(rotation, 1),
                "layer": layer,
            })
        except Exception:
            continue

    # 提取多行文字 MTEXT
    for entity in msp.query("MTEXT"):
        try:
            raw_text = getattr(entity, "text", "") or ""
            text = clean_mtext(raw_text)
            if not text:
                continue
            insert = entity.dxf.insert
            height = float(getattr(entity.dxf, "char_height", 10.0))
            rotation = float(getattr(entity.dxf, "rotation", 0.0))
            layer = sanitize_surrogates(str(getattr(entity.dxf, "layer", "0")))
            extracted.append({
                "type": "MTEXT",
                "text": text,
                "x": round(float(insert.x), 2),
                "y": round(float(insert.y), 2),
                "height": round(height, 2),
                "rotation": round(rotation, 1),
                "layer": layer,
            })
        except Exception:
            continue

    # 提取图块引用 INSERT 中的属性文字
    for entity in msp.query("INSERT"):
        try:
            block_name = sanitize_surrogates(str(getattr(entity.dxf, "name", "")))
            layer = sanitize_surrogates(str(getattr(entity.dxf, "layer", "0")))
            for attrib in getattr(entity, "attribs", []):
                val = clean_mtext(str(getattr(attrib.dxf, "text", "")))
                tag = sanitize_surrogates(str(getattr(attrib.dxf, "tag", "")))
                if val:
                    extracted.append({
                        "type": "ATTRIB",
                        "block": block_name,
                        "tag": tag,
                        "text": val,
                        "x": round(float(attrib.dxf.insert.x), 2),
                        "y": round(float(attrib.dxf.insert.y), 2),
                        "height": round(float(getattr(attrib.dxf, "height", 10.0)), 2),
                        "layer": layer,
                    })
        except Exception:
            continue

    extracted.sort(key=lambda item: (-item["y"], item["x"]))
    return extracted


def render_dxf_to_pdf(dxf_path: str, out_pdf_path: str, doc: Any = None) -> bool:
    """使用 ezdxf 矢量渲染引擎将 DXF 转为高清白色背景 PDF。"""
    try:
        if doc is None:
            doc = load_dxf_document(dxf_path)
        msp = doc.modelspace()
        ctx = RenderContext(doc)
        cfg = Configuration(
            background_policy=BackgroundPolicy.WHITE,
            color_policy=ColorPolicy.COLOR,
            hatch_policy=HatchPolicy.IGNORE,
        )
        backend = SVGBackend()
        page = layout.Page.from_dxf_layout(msp)
        frontend = Frontend(ctx, backend, config=cfg)
        frontend.draw_layout(msp, finalize=True)
        svg_content = backend.get_string(page)

        clean_svg_bytes = sanitize_surrogates(svg_content).encode("utf-8", "ignore")
        pdf_doc = pymupdf.open(stream=clean_svg_bytes, filetype="svg")
        pdf_bytes = pdf_doc.convert_to_pdf()
        with open(out_pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return True
    except Exception as exc:
        print(f"render_dxf_to_pdf 失败: {exc}")
        return False


def render_svg_to_pdf(svg_path: str, out_pdf_path: str) -> bool:
    """将 SVG 文件直接渲染为 PDF。"""
    try:
        with open(svg_path, "rb") as f:
            svg_bytes = f.read()
        try:
            clean_svg = svg_bytes.decode("utf-8", "ignore").encode("utf-8")
        except Exception:
            clean_svg = svg_bytes
        pdf_doc = pymupdf.open(stream=clean_svg, filetype="svg")
        pdf_bytes = pdf_doc.convert_to_pdf()
        with open(out_pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return True
    except Exception as exc:
        print(f"render_svg_to_pdf 失败: {exc}")
        return False


def process_cad_file(cad_path: str, out_pdf_path: str) -> tuple[str, list[dict[str, Any]]]:
    """主入口：将 DWG 或 DXF 转为标准 PDF 并提取全部原生文字流。

    针对平铺多张图纸的 CAD 文件，自动探测所有电气系统图图框并进行多页矢量切片；
    针对单张图纸 CAD，直接高质量渲染；
    具备持久化转换缓存，二次处理 0 耗时秒开。
    """
    import hashlib

    ext = os.path.splitext(cad_path)[1].lower()
    work_dir = os.path.dirname(cad_path)
    base_name = os.path.splitext(os.path.basename(cad_path))[0]

    dxf_to_clean = None
    if ext == ".dwg":
        cache_dir = os.path.join(work_dir, ".cad_cache")
        os.makedirs(cache_dir, exist_ok=True)
        file_size = os.path.getsize(cad_path) if os.path.exists(cad_path) else 0
        cache_key = hashlib.md5(f"{base_name}_{file_size}".encode()).hexdigest()
        cached_dxf = os.path.join(cache_dir, f"{cache_key}.dxf")

        dxf_success = False
        if os.path.exists(cached_dxf) and os.path.getsize(cached_dxf) > 0:
            dxf_path = cached_dxf
            dxf_success = True
        else:
            try:
                dxf_success = dwg_to_dxf(cad_path, cached_dxf)
                if dxf_success:
                    dxf_path = cached_dxf
            except Exception as e:
                print(f"dwg2dxf 失败: {e}")

        if not dxf_success or not os.path.exists(dxf_path) or os.path.getsize(dxf_path) == 0:
            temp_svg = os.path.join(work_dir, f"{base_name}_temp.svg")
            if dwg_to_svg_fallback(cad_path, temp_svg):
                try:
                    ok = render_svg_to_pdf(temp_svg, out_pdf_path)
                finally:
                    try:
                        os.remove(temp_svg)
                    except OSError:
                        pass
                if ok and os.path.exists(out_pdf_path) and os.path.getsize(out_pdf_path) > 0:
                    return out_pdf_path, []
            raise RuntimeError(f"无法将 DWG 文件 {os.path.basename(cad_path)} 转换为预览格式")
    elif ext == ".dxf":
        dxf_path = cad_path
    else:
        raise ValueError(f"不支持的 CAD 文件格式: {ext}")

    try:
        doc = load_dxf_document(dxf_path)

        # 1. v2：按真实图框几何分幅，再把每张系统图拆成配电箱单元块，一块一页
        try:
            index = GeometryIndex(doc)
            frames = detect_drawing_frames(doc, index)
            if frames:
                blocks = build_unit_blocks(doc, index, frames)
                if blocks:
                    extracted_texts = render_cad_unit_blocks(doc, blocks, out_pdf_path, index)
                    if os.path.exists(out_pdf_path) and os.path.getsize(out_pdf_path) > 0:
                        return out_pdf_path, extracted_texts
        except Exception as exc:  # noqa: BLE001 - 几何分幅异常时退回旧链路，不能让上传直接失败
            print(f"[CAD] 几何分幅失败，回退旧切片链路: {exc}")

        # 2. 旧链路：按图签标题文字切分
        system_sheets = detect_system_sheets(doc)
        if len(system_sheets) >= 1:
            extracted_texts = slice_and_render_cad_sheets(doc, system_sheets, out_pdf_path)
            if os.path.exists(out_pdf_path) and os.path.getsize(out_pdf_path) > 0:
                return out_pdf_path, extracted_texts

        # 3. 若未探测出系统图分幅图框，检查是否属于不包含系统图的海量平面施工图
        msp = doc.modelspace()
        text_count = len(msp.query("TEXT")) + len(msp.query("MTEXT"))
        if text_count > MAX_TEXT_ENTITIES_LIMIT:
            raise ValueError(
                f"图纸包含海量文字实体 ({text_count} 条)，未检测到配电箱电气系统图（均为建筑施工平面图）。"
                f"请上传包含配电箱结线与回路的电气系统图 DWG 或 PDF 切图。"
            )

        # 4. 常规单图渲染
        extracted_texts = extract_cad_entities(dxf_path, doc=doc)
        ok = render_dxf_to_pdf(dxf_path, out_pdf_path, doc=doc)
        if not ok:
            raise RuntimeError("CAD 渲染为 PDF 失败")
        return out_pdf_path, extracted_texts
    finally:
        if dxf_to_clean:
            try:
                os.remove(dxf_to_clean)
            except OSError:
                pass
