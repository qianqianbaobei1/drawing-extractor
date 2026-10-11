# -*- coding: utf-8 -*-
"""CAD (DWG / DXF) 原生解析与渲染转换流水线。

本模块实现：
1. DWG -> DXF / SVG 本地无头转码；
2. DXF -> 结构化矢量实体与文本提取 (TEXT / MTEXT / INSERT 属性)；
3. DXF / SVG -> 标准 PDF 高保真光栅化，无缝对接下游 VisionExtractor 及 Web 视口。
"""

import concurrent.futures
import hashlib
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

from .cad_ledger import entity_source, join_insert_path

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

from .config import domain as _domain_top, pipeline as _pipeline_top

MAX_TEXT_ENTITIES_LIMIT = int(_pipeline_top()["cad"]["max_text_entities_limit"])

SYSTEM_KEYWORDS_INCLUDE = list(_domain_top()["cad"]["system_keywords_include"])
SYSTEM_KEYWORDS_EXCLUDE = list(_domain_top()["cad"]["system_keywords_exclude"])


def is_cad_path(path: str) -> bool:
    """检查文件是否为 DWG 或 DXF 格式。"""
    ext = os.path.splitext(path)[1].lower()
    return ext in (".dwg", ".dxf")


def find_dwg2dxf_tool() -> str | None:
    """查找系统中可用的 dwg2dxf 工具路径。"""
    common_paths = list(DWG2DXF_SEARCH_PATHS)
    for p in common_paths:
        if os.path.exists(p) and os.access(p, os.X_OK):
            return p
    return shutil.which("dwg2dxf")


def find_dwg2svg_tool() -> str | None:
    """查找系统中可用的 dwg2SVG 工具路径。"""
    common_paths = list(DWG2SVG_SEARCH_PATHS)
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


def decode_cad_controls(text: str) -> str:
    """把 CAD 控制码还原成可读字符。DXF 与 PDF 文字层共用。

    %%U 这类排版开关直接去掉，否则位号会带上脏前缀。%%133 这类数字码还原成上标。
    认不出的控制码原样保留。直径符号沿用配电箱口径，写成 Φ。
    """
    if not text or "%" not in text:
        return text
    escapes = {
        "P": "±",
        "D": "°",
        "C": "Φ",
        "A": "°",
        "S": "/",
        "N": " ",
    }
    toggles = set("UOLKX")
    numbers = {
        "133": "³",
        "132": "²",
        "179": "³",
        "176": "°",
        "177": "±",
        "248": "Φ",
    }
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] == "%" and index + 1 < length and text[index + 1] == "%":
            rest = text[index + 2:]
            code3 = rest[:3]
            if len(code3) == 3 and code3.isdigit():
                mapped = numbers.get(code3)
                if mapped is None:
                    number = int(code3)
                    mapped = chr(number) if 32 <= number < 127 else ""
                out.append(mapped)
                index += 5
                continue
            letter = rest[:1]
            key = letter.upper()
            if key in toggles:
                out.append("")
            elif key in escapes:
                out.append(escapes[key])
            else:
                out.append(text[index:index + (3 if letter else 2)])
            index += 3 if letter else 2
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def clean_mtext(raw_text: str) -> str:
    """清理 AutoCAD MTEXT 常见的格式控制代码（如 \\P, \\A1;, \\fSimSun; 等）与非法控制字符。"""
    if not raw_text:
        return ""
    t = sanitize_surrogates(raw_text)
    # 先解 %% 控制码。ezdxf 会把 %%c 收成 Ø，配电箱口径要的是 Φ，所以不能等它先处理。
    t = decode_cad_controls(t)
    # 优先使用 ezdxf 经过实战检验的 fast_plain_mtext 剔除全部 MTEXT 格式控制序列
    try:
        from ezdxf.tools.text import fast_plain_mtext
        t = fast_plain_mtext(t)
    except Exception:
        t = t.replace(r"\P", "\n").replace(r"\p", "\n")
        t = re.sub(r"[{}]", "", t)
    # 解码 AutoCAD 的 \\U+xxxx Unicode 转义字符（如 \\U+201C -> “，\\U+201D -> ”）
    try:
        t = re.sub(r"\\+U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), t)
    except Exception:
        pass
    # 移除残留的控制码与未识别转义（如 \\ud83d, \\A1; 等）
    t = re.sub(r"\\+[A-Za-z0-9]+\;?", "", t)
    # 过滤无法在 XML / Excel / JSON 中安全显示的 ASCII 控制字符 (ASCII 0-31，保留 \t, \n, \r)
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", t)
    # 合并多余空白
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t


def dwg_to_dxf(dwg_path: str, dxf_path: str) -> bool:
    """调用 LibreDWG 的 dwg2dxf 将 DWG 转为 DXF。

    通过条件是退出码 0，并且产物通过 DXF 结构校验。转换写到临时文件，
    校验通过后才替换目标路径。失败时不改目标文件，也不到目录里挑另一张图顶替。
    警告、外参和代理对象记在转换记录里，由读取盘点决定是降级还是关键条件不足。
    """
    from .cad_ledger import (
        MIN_DWG_BYTES,
        classify_stderr,
        conversion_accepted,
        is_valid_dxf,
        remember_conversion,
    )

    def _remember(record: dict[str, Any]) -> bool:
        remember_conversion(dwg_path, record)
        if dxf_path:
            remember_conversion(dxf_path, record)
        return bool(record["accepted"])

    try:
        source_size = os.path.getsize(dwg_path)
    except OSError:
        source_size = 0
    if source_size < MIN_DWG_BYTES:
        return _remember({
            "tool": "dwg2dxf",
            "exit_code": None,
            "output_nonempty": False,
            "accepted": False,
            "dxf_valid": False,
            "warning_lines": 0,
            "error_lines": 1,
            "stderr_excerpt": "",
            "log_missing": False,
            "note": f"输入只有 {source_size} 字节，明显不是有效 DWG，未调用转换器",
        })

    tool = find_dwg2dxf_tool()
    if not tool:
        raise RuntimeError("未检测到 dwg2dxf 转换工具，请确保已安装 libredwg")

    out_dir = os.path.dirname(os.path.abspath(dxf_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    staging = dxf_path + ".converting"
    if os.path.exists(staging):
        try:
            os.remove(staging)
        except OSError:
            pass

    cmd = [tool, "-y", "-o", staging, dwg_path]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", check=False)
    staging_exists = os.path.exists(staging)
    output_nonempty = staging_exists and os.path.getsize(staging) > 0
    dxf_valid, dxf_why = is_valid_dxf(staging) if staging_exists else (False, "本图未产出 .dxf")
    accepted = conversion_accepted(res.returncode, output_nonempty) and dxf_valid
    if accepted:
        os.replace(staging, dxf_path)
    elif staging_exists:
        try:
            os.remove(staging)
        except OSError:
            pass
    warning_lines, error_lines, excerpt = classify_stderr(res.stderr or "")
    if dxf_valid:
        note = "退出码 0 且 DXF 结构校验通过。这不表示对象、依赖和单位已经读全"
    elif res.returncode != 0:
        note = "转换器未正常退出，目标文件保持原样，不用目录里其他 DXF 顶替"
    else:
        note = f"产物不是可用 DXF（{dxf_why}），已丢弃，不用目录里其他 DXF 顶替"
    return _remember({
        "tool": os.path.basename(tool),
        "exit_code": res.returncode,
        "output_nonempty": output_nonempty,
        "accepted": accepted,
        "dxf_valid": dxf_valid,
        "warning_lines": warning_lines,
        "error_lines": error_lines,
        "stderr_excerpt": excerpt,
        "log_missing": False,
        "note": note,
    })


def resolve_dwg_dxf(dwg_path: str) -> str:
    """返回这张 DWG 自己的 DXF 缓存。

    缓存名带文件大小，旁边的标记文件写源文件哈希。同目录里另一张图的 DXF，
    或只有文件名对得上的旧缓存，都不会被当成这张图。
    """
    from .cad_ledger import MIN_DWG_BYTES, is_valid_dxf, sha256_file

    if not os.path.isfile(dwg_path):
        raise RuntimeError(f"DWG 不存在: {dwg_path}")
    source_size = os.path.getsize(dwg_path)
    work_dir = os.path.dirname(os.path.abspath(dwg_path))
    base_name = os.path.splitext(os.path.basename(dwg_path))[0]
    cache_dir = os.path.join(work_dir, ".cad_cache")
    os.makedirs(cache_dir, exist_ok=True)
    cache_key = hashlib.md5(f"{base_name}_{source_size}".encode()).hexdigest()
    cached_dxf = os.path.join(cache_dir, f"{cache_key}.dxf")
    stamp_path = cached_dxf + ".src"
    if source_size < MIN_DWG_BYTES:
        dwg_to_dxf(dwg_path, cached_dxf)
        raise RuntimeError(
            f"{os.path.basename(dwg_path)} 只有 {source_size} 字节，明显不是有效 DWG，已直接判失败"
        )
    source_hash = sha256_file(dwg_path)
    valid, _why = is_valid_dxf(cached_dxf)
    stamped = ""
    if valid:
        try:
            with open(stamp_path, encoding="utf-8") as handle:
                stamped = handle.read().strip()
        except OSError:
            stamped = ""
    if valid and stamped == source_hash:
        return cached_dxf

    for stale in (cached_dxf, stamp_path):
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
    if not dwg_to_dxf(dwg_path, cached_dxf):
        raise RuntimeError(
            f"无法把 {os.path.basename(dwg_path)} 转成可用 DXF。"
            "空壳、坏文件和目录里的其他图纸都不会拿来顶替。"
        )
    try:
        with open(stamp_path, "w", encoding="utf-8") as handle:
            handle.write(source_hash)
    except OSError:
        pass
    return cached_dxf


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
        doc = ezdxf.readfile(dxf_path)
        doc.extractor_recovered = False
        return doc
    except Exception:
        from ezdxf import recover
        doc, _ = recover.readfile(dxf_path)
        doc.extractor_recovered = True
        return doc


def is_system_title(text: str) -> bool:
    """判断文字是否属于电气系统图标题。"""
    t = text.replace(" ", "").replace("\n", "").strip()
    if not t or len(t) > NOTE_TEXT_MAX_LEN:
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
                        "height": float(getattr(e.dxf, "height", UNIT_CAPTION_DEFAULT_HEIGHT) if e.dxftype() == "TEXT" else getattr(e.dxf, "char_height", UNIT_CAPTION_DEFAULT_HEIGHT)),
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
                        "height": float(getattr(attrib.dxf, "height", BLOCK_TEXT_DEFAULT_HEIGHT)),
                    })
        except Exception:
            pass

    if not candidates:
        return []

    # 3. 过滤图纸目录表（同一 X 轴紧密垂直堆叠）
    by_x: dict[int, list[dict[str, Any]]] = {}
    for c in candidates:
        bucket = round(c["x"] / TEXT_ROW_BUCKET) * TEXT_ROW_BUCKET
        by_x.setdefault(bucket, []).append(c)

    filtered = []
    for bucket, group in by_x.items():
        if len(group) >= 3:
            ys = sorted([g["y"] for g in group])
            diffs = [ys[i + 1] - ys[i] for i in range(len(ys) - 1)]
            avg_diff = sum(diffs) / len(diffs)
            if avg_diff < TEXT_ROW_ALIGN_TOL:
                continue
        filtered.extend(group)

    if not filtered:
        return []

    # 4. 去重并优先选取图签栏或大字高标题
    title_layer_scores = _DOM.get("title_layer_score_hints") or {}
    title_text_score = int(_DOM.get("title_text_score") or 0)
    sheet_map: dict[str, dict[str, Any]] = {}
    for c in filtered:
        name = c["title"]
        score = 0
        layer_upper = (c.get("layer") or "").upper()
        matched = [v for k, v in title_layer_scores.items() if k in layer_upper]
        if matched:
            score += max(matched)
        if c["height"] >= TITLE_TEXT_MIN_HEIGHT:
            score += title_text_score
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
                if pw > BLOCK_PLAN_MIN_W and ph > BLOCK_PLAN_MIN_H:
                    aspect = max(pw, ph) / min(pw, ph)
                    if BLOCK_PLAN_ASPECT_MIN <= aspect <= BLOCK_PLAN_ASPECT_MAX:
                        border_boxes.append((min_px, min_py, max_px, max_py))
        except Exception:
            pass

    # 7. 计算每张图框的实际包围盒 (Bounding Box)
    sheet_w = SHEET_DEFAULT_WIDTH
    if len(detected) >= 2:
        xs = sorted(s["x"] for s in detected)
        diffs = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)
                 if xs[i + 1] - xs[i] >= SHEET_WIDTH_GAP_MIN]
        if diffs:
            sheet_w = min(diffs)
    elif len(detected) == 1:
        # 单图自适应比例尺推导 (根据大字高推算 scale)
        th = detected[0].get("height", UNIT_CAPTION_DEFAULT_HEIGHT)
        scale = max(1.0, th / SHEET_SCALE_REFERENCE_HEIGHT) if th > SHEET_SCALE_TEXT_HEIGHT else 1.0
        sheet_w = SHEET_WIDTH_MM_BASIS * scale

    sheet_h = sheet_w / SHEET_ASPECT_RATIO  # ISO 216 标准宽高比

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
    并发渲染以改善复杂图纸处理速度；实际耗时与完整性需按图纸逐类测量。
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
    CELL_SIZE = float(_CAD["cell_size"])
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

        # 从空间网格中筛选与当前图框相交单元格的实体，减少无关图元遍历。
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
            fb_page = fallback_doc.new_page(width=RENDER_FALLBACK_PAGE[0], height=RENDER_FALLBACK_PAGE[1])
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
# 旧流水线用“系统图”标题文字的插入点和估算尺寸当切片边界，可能裁掉页面内容；
#   2. 实体只按 dxf.insert / dxf.start 归属会漏掉不含这些属性的圆弧、实体和多段线；
#   3. 多箱系统图整页送模型可能超过输出上限，需拆分并校验跨边界对象；
#   4. 图签栏里的图名文字可能被误当成独立图纸，导致页数与源图不符。
#
# v2 尝试：用图框块参照（INSERT）识别幅面、用标题分类图纸，再按箱框与箱名生成单元块。
# 该启发式仍依赖图框/图层/标注习惯，输出需经过切片覆盖检查；单元块边界不保证无遗漏。
# ---------------------------------------------------------------------------
# 几何判据与领域词典全部来自 config/pipeline.json 的 cad 段与 config/domain.json，
# 现场按设计院习惯调整时改 JSON（或设同名环境变量）即可，不必改代码。
# ---------------------------------------------------------------------------
from .config import domain as _domain, pipeline as _pipeline  # noqa: E402

_CAD = _pipeline()["cad"]
_DOM = _domain()["cad"]

ISO_LANDSCAPE_SIZES = tuple(tuple(pair) for pair in _CAD["iso_landscape_sizes"])
FRAME_MIN_DIM = float(_CAD["frame_min_dim"])            # 闭合多段线外框的最小边长（图纸单位）
FRAME_MIN_SIDE = float(_CAD["frame_min_side"])          # 块参照图框的最小短边
FRAME_MAX_ASPECT = float(_CAD["frame_max_aspect"])      # 超过此长宽比的是长条符号，不是图纸
FRAME_DEDUP_TOL = float(_CAD["frame_dedup_tol"])        # 面积容差：落在大框内即视为嵌套子块
FRAME_DUP_TOL = float(_CAD["frame_dup_tol"])
UNIT_RECT_MIN_W, UNIT_RECT_MAX_W = float(_CAD["unit_rect_min_w"]), float(_CAD["unit_rect_max_w"])
UNIT_RECT_MIN_H, UNIT_RECT_MAX_H = float(_CAD["unit_rect_min_h"]), float(_CAD["unit_rect_max_h"])
UNIT_RECT_DEDUP_TOL = float(_CAD["unit_rect_dedup_tol"])
RECT_ASPECT_MIN, RECT_ASPECT_MAX = float(_CAD["rect_aspect_min"]), float(_CAD["rect_aspect_max"])
POLYLINE_CLOSE_TOL = float(_CAD["polyline_close_tol"])
TEXT_ROW_BUCKET = float(_CAD["text_row_bucket"])
TEXT_ROW_ALIGN_TOL = float(_CAD["text_row_align_tol"])
TITLE_TEXT_MIN_HEIGHT = float(_CAD["title_text_min_height"])
SHEET_DEFAULT_WIDTH = float(_CAD["sheet_default_width"])
SHEET_WIDTH_MM_BASIS = float(_CAD["sheet_width_mm_basis"])
SHEET_ASPECT_RATIO = float(_CAD["sheet_aspect_ratio"])
SHEET_SCALE_TEXT_HEIGHT = float(_CAD["sheet_scale_text_height"])
SHEET_SCALE_REFERENCE_HEIGHT = float(_CAD["sheet_scale_reference_height"])
SHEET_WIDTH_GAP_MIN = float(_CAD["sheet_width_gap_min"])
FALLBACK_TEXT_HEIGHT = float(_CAD["fallback_text_height"])
UNIT_CAPTION_DEFAULT_HEIGHT = float(_CAD["unit_caption_default_height"])
BLOCK_TEXT_DEFAULT_HEIGHT = float(_CAD["block_text_default_height"])
CROP_OVERLAP_RATIO = float(_CAD["crop_overlap_ratio"])
CROP_WIDTH_FACTOR = float(_CAD["crop_width_factor"])
CROP_HEIGHT_RATIO = float(_CAD["crop_height_ratio"])
CROP_TOP_MAX_PAD = float(_CAD["crop_top_max_pad"])
CROP_CAPTION_GAP = float(_CAD["crop_caption_gap"])
CROP_CAPTION_GAP_ALT = float(_CAD["crop_caption_gap_alt"])
CROP_BOTTOM_GAP = float(_CAD["crop_bottom_gap"])
CROP_BELOW_PAD = float(_CAD["crop_below_pad"])
CELL_SIZE = float(_CAD["cell_size"])
BLOCK_PLAN_MIN_W = float(_CAD["block_plan_min_w"])
BLOCK_PLAN_MIN_H = float(_CAD["block_plan_min_h"])
BLOCK_PLAN_ASPECT_MIN = float(_CAD["block_plan_aspect_min"])
BLOCK_PLAN_ASPECT_MAX = float(_CAD["block_plan_aspect_max"])
RENDER_FALLBACK_PAGE = tuple(_CAD["render_fallback_page"])
DWG2DXF_SEARCH_PATHS = tuple(_CAD["dwg2dxf_search_paths"])
DWG2SVG_SEARCH_PATHS = tuple(_CAD["dwg2svg_search_paths"])
CONVERT_TIMEOUT_S = int(_CAD["convert_timeout_s"])
UNIT_CAPTION_SUFFIX = tuple(
    sorted(_DOM["unit_caption_suffix"], key=len, reverse=True)
)  # 最长后缀优先，避免「配电箱」抢走「照明配电箱」一类复合后缀
UNIT_CAPTION_MIN_LEN = int(_DOM["box_caption_min_len"])
UNIT_CAPTION_MAX_LEN = int(_DOM["box_caption_max_len"])
BOX_TITLE_MAX_LEN = int(_DOM["box_title_max_len"])
NOTE_TEXT_MAX_LEN = int(_DOM["note_text_max_len"])
UNIT_CAPTION_BAD_LAYERS = set(_DOM["unit_caption_bad_layers"])
CAPTION_ROUTE_MARKS = tuple(_DOM.get("caption_route_marks") or ("由", "引", "至", "沿", "敷设"))
CAPTION_STRIP_MARKS = tuple(_DOM.get("caption_strip_marks") or ("系统图", "接线图", "原理图"))
CAPTION_BAD_HEAD_CHARS = str(_DOM.get("caption_bad_head_chars") or "由详注如按本除并引至沿")
CAPTION_SOFT_TAIL_CHARS = str(_DOM.get("caption_soft_tail_chars") or "箱柜屏盘")
CAPTION_NOTE_MARKS = tuple(
    _DOM.get("caption_note_marks")
    or ("详见", "配置详见", "各配电箱", "各配电柜", "应符合", "不得", "本说明", "参见")
)
CAPTION_TAIL_STRIP_REGEXES = tuple(
    _DOM.get("caption_tail_strip_regexes")
    or (r"共\d+台.*$", r"(?:挂墙|壁挂|落地|明装|暗装|嵌墙).*$")
)
# 平面图/建筑图层上也有闭合箱柜外形，但不是系统图单元块，按图层前缀排掉
UNIT_RECT_BAD_LAYER_MARKERS = tuple(_DOM["unit_rect_bad_layer_markers"])
BOX_ABOVE_MIN, BOX_ABOVE_MAX = float(_CAD["box_above_min"]), float(_CAD["box_above_max"])
BOX_X_TOLERANCE = float(_CAD["box_x_tolerance"])       # 箱名与箱框水平中心的最大额外偏移
BOX_SIDE_GAP_MAX = float(_CAD.get("box_side_gap_max") or 15000.0)  # 箱名在箱框左右侧时的最大缝隙
BOX_SIDE_Y_PAD = float(_CAD.get("box_side_y_pad") or 2000.0)       # 侧旁配对允许的垂直越界
CROP_PAD_SIDE = float(_CAD["crop_pad_side"])
CROP_PAD_TOP = float(_CAD["crop_pad_top"])
CROP_PAD_BOTTOM = float(_CAD["crop_pad_bottom"])
PAGE_LONG_MM_MIN, PAGE_LONG_MM_MAX = float(_CAD["page_long_mm_min"]), float(_CAD["page_long_mm_max"])
# 配电箱单元块的页面长边固定到 A3 左右：下游按“长边 2400px”光栅化，
# 长边落在触发阈值以下才能既拿满像素又不触发再分块。
UNIT_PAGE_LONG_MM = float(_CAD["unit_page_long_mm"])
PLAN_TITLE_MARKERS = tuple(_DOM["plan_title_markers"])
SYSTEM_TITLE_MARKERS = tuple(_DOM["system_title_markers"])
TITLE_LAYER_HINTS = tuple(_DOM["title_layer_hints"])
GRID_CELL = float(_CAD["grid_cell"])


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
            if not _polyline_is_closed(e):
                continue
            b = index.boxes[i]
            w, h = b[2] - b[0], b[3] - b[1]
            if min(w, h) < FRAME_MIN_DIM:
                continue
            if not RECT_ASPECT_MIN <= (max(w, h) / max(min(w, h), 1e-6)) <= RECT_ASPECT_MAX:
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
    scale = max(float(best or FALLBACK_TEXT_HEIGHT), 1e-6)
    long_mm = long_u / scale
    if long_mm < PAGE_LONG_MM_MIN:
        scale = long_u / PAGE_LONG_MM_MIN
    elif long_mm > PAGE_LONG_MM_MAX:
        scale = long_u / PAGE_LONG_MM_MAX
    return scale


def _normalize_box_caption(text: str) -> str:
    """去掉空格、括号修饰、「系统图」尾巴，以及「共N台/挂墙」安装尾注。"""
    t = re.sub(r"\s+", "", (text or "").strip())
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    for mark in CAPTION_STRIP_MARKS:
        if mark and mark in t:
            t = t.replace(mark, "")
    for pat in CAPTION_TAIL_STRIP_REGEXES:
        if pat:
            t = re.sub(pat, "", t)
    # 「配电总箱1」这类序号尾巴：只剥箱种后缀后的纯数字，不动 00ALZ 这类代号
    for sfx in UNIT_CAPTION_SUFFIX:
        m = re.search(re.escape(sfx) + r"(\d+)$", t)
        if m:
            t = t[: m.start(1)]
            break
    return t.strip(" -_/、，,")


def _is_caption_trailing_code(token: str) -> bool:
    """箱种后缀后的代号：00ALZ / JXX2 / 03ELB1；拒绝纯数字与回路号。"""
    tok = (token or "").strip(" -_/")
    if not tok or len(tok) > 16:
        return False
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", tok):
        return False
    if not re.search(r"[A-Za-z]", tok):
        return False
    if _looks_like_circuit_code(tok):
        return False
    return True


def _caption_suffix_match(text: str) -> str | None:
    """标准后缀：优先末尾匹配；也允许「…配电箱00ALZ」这种代号在后缀后。"""
    for sfx in UNIT_CAPTION_SUFFIX:
        if text.endswith(sfx):
            return sfx
    for sfx in UNIT_CAPTION_SUFFIX:
        idx = text.rfind(sfx)
        if idx < 0:
            continue
        after = text[idx + len(sfx):]
        if after and _is_caption_trailing_code(after):
            return sfx
    return None


def _caption_has_route_note(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    if raw[0] in CAPTION_BAD_HEAD_CHARS:
        return True
    return any(mark and raw.startswith(mark) for mark in CAPTION_ROUTE_MARKS)


def _caption_has_note_mark(text: str) -> bool:
    """说明句/注释不当箱名：配置详见各配电箱柜系统图 等。"""
    compact = re.sub(r"\s+", "", (text or "").strip())
    if not compact:
        return True
    return any(m and m in compact for m in CAPTION_NOTE_MARKS)


def _looks_like_box_caption(text: str) -> bool:
    """箱名判定：标准后缀最长匹配；允许代号在后缀后；拒说明句与路由注释。

    图名（系统图/平面图等）且抽不出箱号时不当箱名。
    """
    raw = (text or "").strip()
    if not raw or _caption_has_route_note(raw) or _caption_has_note_mark(raw):
        return False
    compact = re.sub(r"\s+", "", raw)
    # 纯图名：带系统图/平面图类词且没有箱种后缀 → 不是箱名
    title_like = any(m in compact for m in SYSTEM_TITLE_MARKERS) or any(m in compact for m in PLAN_TITLE_MARKERS)
    t = _normalize_box_caption(raw)
    if not UNIT_CAPTION_MIN_LEN <= len(t) <= UNIT_CAPTION_MAX_LEN:
        return False
    sfx = _caption_suffix_match(t)
    if sfx:
        idx = t.rfind(sfx)
        head = t[:idx].strip(" -_/")
        after = t[idx + len(sfx):]
        if after and _is_caption_trailing_code(after):
            # 「路灯照明配电箱00ALZ」：代号在后，head 可空；head 有内容时仍拒坏首字
            if head and head[0] in CAPTION_BAD_HEAD_CHARS:
                return False
            return True
        if not head or head[0] in CAPTION_BAD_HEAD_CHARS:
            return False
        return True
    if title_like:
        return False
    # 软匹配：以箱/柜/屏/盘结尾，且前面有字母数字代号
    if t and t[-1] in CAPTION_SOFT_TAIL_CHARS:
        head = t[:-1].strip(" -_/")
        if head and re.search(r"[A-Za-z0-9]", head) and head[0] not in CAPTION_BAD_HEAD_CHARS:
            return True
    return False


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
                              "layer": str(e.dxf.layer), "height": _text_height(e, kind),
                              "source": entity_source(e, owner="modelspace", insert_path="")})
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
                                "layer": str(getattr(e.dxf, "layer", "")), "height": _text_height(e, kind),
                                "source": entity_source(e, owner=name, insert_path="")})
            elif kind == "INSERT" and depth < 4:
                sub = _local_block_texts(doc, e.dxf.name, cache, depth + 1)
                if not sub:
                    continue
                try:
                    m = e.matrix44()
                except Exception:
                    continue
                nested = entity_source(e, owner=name, insert_path="")
                for s in sub:
                    p = m.transform((s["x"], s["y"], 0.0))
                    src = dict(s.get("source") or {})
                    src["insert_path"] = join_insert_path(
                        f"{e.dxf.name}@{nested['handle']}", str(src.get("insert_path") or ""))
                    src["owner"] = str(e.dxf.name)
                    out.append({**s, "x": float(p[0]), "y": float(p[1]), "source": src})
    except Exception:
        pass
    cache[name] = out
    return out


def _explode_texts(ins: Any, cache: dict) -> list[dict[str, Any]]:
    """取块参照内部的文字与属性文字，坐标折到模型空间绝对坐标。

    插入路径带上这次 INSERT 的 handle。块定义里的文字 handle 在每次插入时相同，
    不能单独拿来当器件身份。
    """
    out: list[dict[str, Any]] = []
    insert_identity = entity_source(ins, owner="modelspace", insert_path="")
    instance_path = f"{ins.dxf.name}@{insert_identity['handle']}"
    doc = getattr(ins, "doc", None)
    if doc is not None:
        local = _local_block_texts(doc, ins.dxf.name, cache)
        if local:
            try:
                m = ins.matrix44()
            except Exception:
                m = None
            for s in local:
                src = dict(s.get("source") or {})
                src["insert_path"] = join_insert_path(instance_path, str(src.get("insert_path") or ""))
                src["owner"] = str(ins.dxf.name)
                if m is not None:
                    p = m.transform((s["x"], s["y"], 0.0))
                    out.append({**s, "x": float(p[0]), "y": float(p[1]), "source": src})
                else:
                    out.append({**s, "source": src})
    for attrib in getattr(ins, "attribs", []):
        txt = clean_mtext(str(getattr(attrib.dxf, "text", "")))
        if txt:
            attrib_identity = entity_source(attrib, owner=str(ins.dxf.name), insert_path="")
            out.append({"text": txt, "x": float(attrib.dxf.insert.x), "y": float(attrib.dxf.insert.y),
                        "layer": str(getattr(attrib.dxf, "layer", "")),
                        "height": _text_height(attrib, "ATTRIB"),
                        "source": entity_source(
                            attrib,
                            owner=str(ins.dxf.name),
                            insert_path=join_insert_path(instance_path, attrib_identity["handle"]),
                        )})
    return out


def _frame_title(texts: list[dict[str, Any]]) -> str:
    """图名：优先图签栏里带“系统图/干线图/原理图”字样的短文字（避开图号、子项名称、公司名）。"""
    def pick(pool, want_marker: bool):
        best, best_h = "", 0.0
        for t in pool:
            txt = t["text"].replace("\n", "").strip()
            if not txt or len(txt) > BOX_TITLE_MAX_LEN:
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


def _polyline_is_closed(entity: Any) -> bool:
    """闭合标志为真，或首尾点在容差内重合，都算闭合。

    现场 DWG 里不少箱框是「画回起点但未勾 closed」的 LWPOLYLINE/POLYLINE，
    只认 closed 标志会整段丢掉单元块。
    """
    kind = entity.dxftype()
    try:
        if kind == "LWPOLYLINE":
            if bool(getattr(entity, "closed", False)):
                return True
            pts = list(entity.get_points("xy"))
        elif kind == "POLYLINE":
            if bool(getattr(entity, "is_closed", False)) or bool(getattr(entity.dxf, "flags", 0) & 1):
                return True
            pts = [(float(p[0]), float(p[1])) for p in entity.points()]
        else:
            return False
        if len(pts) < 4:
            return False
        return math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < POLYLINE_CLOSE_TOL
    except Exception:
        return False


def _unit_rects(index: GeometryIndex, frame_rect) -> list[tuple[float, float, float, float]]:
    """图框内所有配电箱虚线箱框（小尺寸闭合多段线），已按近似重复去重。"""
    rects: list[tuple[float, float, float, float]] = []
    for i in index.query(frame_rect):
        e = index.entities[i]
        if e.dxftype() not in ("LWPOLYLINE", "POLYLINE"):
            continue
        if not _polyline_is_closed(e):
            continue
        layer = str(e.dxf.layer or "").upper()
        if any(m in layer for m in UNIT_RECT_BAD_LAYER_MARKERS):
            continue
        b = index.boxes[i]
        w, h = b[2] - b[0], b[3] - b[1]
        if not (UNIT_RECT_MIN_W <= w <= UNIT_RECT_MAX_W and UNIT_RECT_MIN_H <= h <= UNIT_RECT_MAX_H):
            continue
        if any(abs(b[0] - r[0]) < UNIT_RECT_DEDUP_TOL and abs(b[1] - r[1]) < UNIT_RECT_DEDUP_TOL and abs(b[2] - r[2]) < UNIT_RECT_DEDUP_TOL
               and abs(b[3] - r[3]) < UNIT_RECT_DEDUP_TOL for r in rects):
            continue
        rects.append(b)
    return rects


def _match_box_rect(rects, cap) -> tuple[float, float, float, float] | None:
    """给箱名找箱框：内含 → 正上方 → 左右侧旁（Y 重叠）。"""
    cx, cy = cap["x"], cap["y"]
    inside = [r for r in rects if r[0] <= cx <= r[2] and r[1] <= cy <= r[3]]
    if inside:
        return max(inside, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    cands = []
    for r in rects:
        dy = r[1] - cy
        if not BOX_ABOVE_MIN * -1 <= dy <= BOX_ABOVE_MAX:
            continue
        half = (r[2] - r[0]) / 2.0 + BOX_X_TOLERANCE
        if abs(cx - (r[0] + r[2]) / 2.0) > half:
            continue
        cands.append((abs(dy) if dy >= 0 else dy + 1e6, r))
    if cands:
        min_dy = min(c[0] for c in cands)
        best_cands = [c[1] for c in cands if abs(c[0] - min_dy) < 500.0]
        return max(best_cands, key=lambda r: (r[2] - r[0]) * (r[3] - r[1]))
    # 侧旁：箱名在框左右，垂直方向与框重叠（消控 03ELB1 等）
    side = []
    for r in rects:
        if not (r[1] - BOX_SIDE_Y_PAD <= cy <= r[3] + BOX_SIDE_Y_PAD):
            continue
        if cx < r[0]:
            gap = r[0] - cx
        elif cx > r[2]:
            gap = cx - r[2]
        else:
            continue
        if gap <= BOX_SIDE_GAP_MAX:
            side.append((gap, r))
    if not side:
        return None
    side.sort(key=lambda x: (x[0], -(x[1][2] - x[1][0]) * (x[1][3] - x[1][1])))
    return side[0][1]


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

    图纸上的虚线箱框只圈住断路器那一列，右侧的电缆规格栏和负荷名称栏画在框外，
    箱框左侧是内容起点。必须过滤出真正的主体箱框作为邻接边界，杜绝将箱内局部表格列
    误作为右邻截断。
    """
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0

    # 过滤出独立主体单元框（排除局部小表头与细分列矩形），防止将内部电表栏/负荷栏误判为右邻单元
    substantial = [r for r in rects if (r[2] - r[0]) >= 5000.0 and (r[3] - r[1]) >= 8000.0]
    eval_rects = substantial if len(substantial) >= 2 else rects

    def v_overlap(r):
        return min(r[3], y1) - max(r[1], y0) > CROP_OVERLAP_RATIO * min(h, r[3] - r[1])

    def h_overlap(r):
        return min(r[2], x1) - max(r[0], x0) > CROP_OVERLAP_RATIO * min(w, r[2] - r[0])

    right_gaps = [r[0] - x1 for r in eval_rects if r is not rect and v_overlap(r) and r[0] >= x1]
    top_gaps = [r[1] - y1 for r in eval_rects if r is not rect and h_overlap(r) and r[1] >= y1]
    if right_gaps:
        right = x1 + min(right_gaps)
    else:
        right = x0 + (pitch_x if pitch_x > w else w * CROP_WIDTH_FACTOR)
    right = min(right, x1 + w * CROP_WIDTH_FACTOR, frame[2])
    top = min(y1 + (min(top_gaps) / 2 if top_gaps
                    else min(h * CROP_HEIGHT_RATIO, CROP_TOP_MAX_PAD)), frame[3])

    # 下邻箱框自适应安全裁切（同列且位于当前箱框下方）
    bottom_cands = [r for r in eval_rects if r is not rect and h_overlap(r) and r[3] <= y0]
    if bottom_cands:
        below_top = max(r[3] for r in bottom_cands)
        # 当前箱名下边界：箱名文字下方留白
        cap_bot = (min(caption_y - CROP_CAPTION_GAP, y0 - CROP_CAPTION_GAP_ALT)
                   if caption_y < y0 else y0 - CROP_BOTTOM_GAP)
        # 严格取箱名下沿与下排箱顶的中位线，并至少高出下排箱顶，坚决不切入下排进线与表头
        bottom = max(cap_bot, (cap_bot + below_top) / 2.0, below_top + CROP_BELOW_PAD)
    else:
        if caption_y < y0:
            bottom = min(y0 - CROP_BOTTOM_GAP, caption_y - CROP_CAPTION_GAP_ALT)
        else:
            bottom = y0 - CROP_PAD_BOTTOM
    bottom = max(frame[1], bottom)

    return (max(frame[0], x0 - CROP_PAD_SIDE), bottom, right, top)


def _looks_like_circuit_code(code: str) -> bool:
    """回路号/支路号不当箱号：WL/WP… 或 AL1-1 / 1AL1-1 这类带支路后缀。"""
    from .cad_extractor import _text_is_circuit_anchor
    raw = (code or "").strip()
    if not raw:
        return True
    if _text_is_circuit_anchor(raw):
        return True
    # 箱号后再挂 -数字 支路（AL1-1、2SAL1-3）；纯箱号 01B-03 保留
    if re.fullmatch(r"\d*[A-Za-z]{2,}\d+[A-Za-z]?-\d+[A-Za-z]?", raw):
        return True
    if re.fullmatch(r"(?:WL|WP|WX|WD|WS|WY|WE|QF)\d+[A-Za-z]?", raw, re.I):
        return True
    return False


def _caption_leading_code(caption: str) -> str | None:
    """从箱名开头抽出代号，兼容 01B-03 / 2SAL1 / AL1 等 caption 前缀。"""
    from .cad_extractor import extract_panel_code
    raw = re.sub(r"\s+", " ", (caption or "").strip())
    if not raw:
        return None
    # 优先走统一词法
    code = extract_panel_code(raw)
    if code and not _looks_like_circuit_code(code):
        return code
    # 前缀兜底：开头连续字母数字与短横线，碰到中文/空白停下
    m = re.match(r"^([A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)", re.sub(r"\s+", "", raw))
    if not m:
        return None
    token = m.group(1).upper()
    if _looks_like_circuit_code(token):
        return None
    # 至少含一个字母和一个数字，避免把「一层」拼音残片当箱号
    if re.search(r"[A-Z]", token) and re.search(r"\d", token):
        return token
    return None


def _caption_trailing_code(caption: str) -> str | None:
    """从「…配电箱00ALZ / 应急照明箱03ELB1」后缀后抽出代号。"""
    from .cad_extractor import extract_panel_code
    t = _normalize_box_caption(caption or "")
    if not t:
        return None
    for sfx in UNIT_CAPTION_SUFFIX:
        idx = t.rfind(sfx)
        if idx < 0:
            continue
        after = t[idx + len(sfx):]
        if not _is_caption_trailing_code(after):
            continue
        code = extract_panel_code(after)
        if code and not _looks_like_circuit_code(code):
            return code
        token = after.upper()
        if re.search(r"[A-Z]", token) and re.search(r"\d", token) and not _looks_like_circuit_code(token):
            return token
    return None


def _caption_panel_code(caption: str) -> str | None:
    """caption 上的箱号：前缀优先，其次后缀后代号。"""
    return _caption_leading_code(caption) or _caption_trailing_code(caption)


def _extract_unit_box_code(crop_texts: list[dict[str, Any]], caption: str | None = None) -> str | None:
    """从单元块文本中提取配电箱编号；caption 优先，拒绝回路号抢码。"""
    from .cad_extractor import extract_panel_code
    if caption:
        code = _caption_panel_code(caption)
        if code:
            return code
    for t in crop_texts:
        txt = t.get("text", "")
        if "设备编号" in txt or "箱体编号" in txt:
            clean = txt.replace("设备编号", "").replace("箱体编号", "").replace(":", "").replace("：", "").strip()
            if clean:
                c = extract_panel_code(clean)
                if c and not _looks_like_circuit_code(c):
                    return c
            nearby = [t2 for t2 in crop_texts if abs(t2["y"] - t["y"]) < 600 and t2["x"] > t["x"]]
            if nearby:
                nearby.sort(key=lambda t2: t2["x"])
                for n in nearby:
                    c = extract_panel_code(n.get("text", ""))
                    if c and not _looks_like_circuit_code(c):
                        return c
    for t in crop_texts:
        c = extract_panel_code(t.get("text", ""))
        if c and not _looks_like_circuit_code(c):
            return c
    return None


def build_unit_blocks(doc: Any, index: GeometryIndex, frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把系统图图框拆成配电箱单元块。

    一个配电箱 = 箱名文字 + 它上方的虚线箱框 = 一个单元块。箱框只用来定裁剪范围，
    真正决定“有几个单元”的是箱名文字，因为设计人一定给每个箱写了箱名，而框可能有
    重复或拆分。

    图框是否算系统图，由内容判定而不是靠图名猜：框里有“箱名 + 箱框”配对才算，
    一个都配不上、且图名也不是系统图类的图框（平面图、图签注释块等）整张丢掉。
    配不上但图名是系统图时，整框降级并显式标记 split_source=frame_fallback。
    """
    text_cache: dict = {}
    blocks: list[dict[str, Any]] = []
    for frame in frames:
        fbox = frame["bbox"]
        texts = _frame_texts(index, fbox, text_cache)
        frame["title"] = _frame_title(texts)
        if _classify_frame(frame["title"]) == "plan":
            frame["kind"] = "plan"
            frame["missed_captions"] = []
            continue

        rects = _unit_rects(index, fbox)
        captions = [t for t in texts
                    if _looks_like_box_caption(t["text"])
                    and t["layer"] not in UNIT_CAPTION_BAD_LAYERS]
        scale = _guess_plot_scale(fbox[2] - fbox[0], fbox[3] - fbox[1])
        pitch_x = _column_pitch(rects)
        made = 0
        used: set[int] = set()
        matched_ids: set[int] = set()
        for cap in sorted(captions, key=lambda t: (-t["y"], t["x"])):
            rect = _match_box_rect(rects, cap)
            if rect is None or id(rect) in used:
                continue
            crop = _cell_crop(rect, rects, fbox, cap["y"], pitch_x)
            crop_texts = _frame_texts(index, crop, text_cache, block_text=True)
            box_code = _extract_unit_box_code(crop_texts, caption=cap["text"])
            cap_name = cap["text"].replace(" ", "")
            label = f"{box_code} {cap_name}" if box_code else cap_name

            long_units = max(crop[2] - crop[0], crop[3] - crop[1])
            block_scale = max(long_units / UNIT_PAGE_LONG_MM, 1e-6)
            blocks.append({
                "rect": crop,
                "label": label,
                "frame": frame["title"],
                "scale": block_scale,
                "box_code": box_code or "",
                "split_source": "unit",
                "split_reason": "",
            })
            made += 1
            used.add(id(rect))
            matched_ids.add(id(cap))
        missed = [t["text"] for t in captions if id(t) not in matched_ids]
        frame["missed_captions"] = missed
        if made:
            frame["kind"] = "system"
        elif _classify_frame(frame["title"]) == "system":
            frame["kind"] = "system"
            reason = "no_caption_rect_pair"
            if captions and not rects:
                reason = "captions_without_unit_rects"
            elif captions:
                reason = "captions_unmatched_to_rects"
            elif not captions and not rects:
                reason = "no_captions_or_rects"
            blocks.append({
                "rect": fbox,
                "label": frame["title"] or "系统图",
                "frame": frame["title"],
                "scale": scale,
                "box_code": "",
                "split_source": "frame_fallback",
                "split_reason": reason,
                "missed_captions": missed,
            })
        else:
            frame["kind"] = "skip"
    return blocks


def summarize_unit_split(frames: list[dict[str, Any]], blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总单元块切分结果，供 read_report / 不确定性清单使用。"""
    unit_n = sum(1 for b in blocks if b.get("split_source") == "unit")
    fallback_n = sum(1 for b in blocks if b.get("split_source") == "frame_fallback")
    missed: list[str] = []
    for frame in frames:
        missed.extend(frame.get("missed_captions") or [])
    for block in blocks:
        missed.extend(block.get("missed_captions") or [])
    # 保序去重
    seen: set[str] = set()
    missed_unique: list[str] = []
    for item in missed:
        if item not in seen:
            seen.add(item)
            missed_unique.append(item)
    uncertainties: list[str] = []
    if fallback_n:
        uncertainties.append(f"有 {fallback_n} 个图框未能拆成单元块，已整框降级（frame_fallback）")
    if missed_unique:
        sample = "、".join(missed_unique[:8])
        more = f" 等{len(missed_unique)}个" if len(missed_unique) > 8 else ""
        uncertainties.append(f"箱名未配上箱框：{sample}{more}")
    return {
        "mode": "unit" if unit_n and not fallback_n else ("mixed" if unit_n else "frame_fallback"),
        "unit_blocks": unit_n,
        "frame_fallbacks": fallback_n,
        "total_blocks": len(blocks),
        "missed_captions": missed_unique,
        "uncertainties": uncertainties,
    }


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
                              "box_code": block.get("box_code") or "",
                              "layer": t["layer"],
                              "split_source": block.get("split_source") or "unit",
                              "split_reason": block.get("split_reason") or ""})
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
            height = float(getattr(entity.dxf, "height", UNIT_CAPTION_DEFAULT_HEIGHT))
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
                "source": entity_source(entity, owner="modelspace", insert_path=""),
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
            height = float(getattr(entity.dxf, "char_height", UNIT_CAPTION_DEFAULT_HEIGHT))
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
                "source": entity_source(entity, owner="modelspace", insert_path=""),
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
                    insert_identity = entity_source(entity, owner="modelspace", insert_path="")
                    attrib_identity = entity_source(attrib, owner=block_name, insert_path="")
                    extracted.append({
                        "type": "ATTRIB",
                        "block": block_name,
                        "tag": tag,
                        "text": val,
                        "x": round(float(attrib.dxf.insert.x), 2),
                        "y": round(float(attrib.dxf.insert.y), 2),
                        "height": round(float(getattr(attrib.dxf, "height", UNIT_CAPTION_DEFAULT_HEIGHT)), 2),
                        "layer": layer,
                        "source": entity_source(
                            attrib,
                            owner=block_name,
                            insert_path=join_insert_path(
                                f"{block_name}@{insert_identity['handle']}",
                                attrib_identity["handle"],
                            ),
                        ),
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
    """主入口：尝试将 DWG 或 DXF 转为 PDF，并提取可识别的原生文字记录。

    针对平铺多张图纸的 CAD 文件，尝试探测电气系统图图框并进行多页矢量切片；
    针对单张图纸 CAD，直接高质量渲染；
    具备持久化转换缓存，二次处理 0 耗时秒开。
    """
    ext = os.path.splitext(cad_path)[1].lower()
    work_dir = os.path.dirname(cad_path)
    base_name = os.path.splitext(os.path.basename(cad_path))[0]

    from .cad_ledger import build_read_report, is_valid_dxf, stamp_records, write_read_report

    dxf_to_clean = None
    converter = "native_dxf"
    dxf_path = cad_path
    split_summary: dict[str, Any] = {
        "mode": "unset",
        "unit_blocks": 0,
        "frame_fallbacks": 0,
        "total_blocks": 0,
        "missed_captions": [],
        "uncertainties": [],
    }

    def _stamp_split(texts: list[dict[str, Any]], source: str, reason: str = "") -> None:
        for rec in texts:
            rec.setdefault("split_source", source)
            if reason and not rec.get("split_reason"):
                rec["split_reason"] = reason

    def _deliver(texts: list[dict[str, Any]], doc: Any, used_converter: str) -> tuple[str, list[dict[str, Any]]]:
        stamp_records(texts, cad_path)
        report = build_read_report(
            cad_path,
            doc=doc,
            converter=used_converter,
            dxf_path=dxf_path if used_converter != "svg_fallback" else None,
            recovered=bool(getattr(doc, "extractor_recovered", False)) if doc is not None else False,
            split=split_summary,
        )
        write_read_report(out_pdf_path, report)
        return out_pdf_path, texts

    if ext == ".dwg":
        converter = "libredwg"
        dxf_success = False
        try:
            dxf_path = resolve_dwg_dxf(cad_path)
            dxf_success = True
        except Exception as e:
            print(f"dwg2dxf 失败: {e}")

        if not dxf_success or not is_valid_dxf(dxf_path)[0]:
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
                    return _deliver([], None, "svg_fallback")
            raise RuntimeError(f"无法将 DWG 文件 {os.path.basename(cad_path)} 转换为预览格式")
    elif ext == ".dxf":
        dxf_path = cad_path
        converter = "native_dxf"
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
                split_summary.update(summarize_unit_split(frames, blocks))
                if blocks:
                    extracted_texts = render_cad_unit_blocks(doc, blocks, out_pdf_path, index)
                    if os.path.exists(out_pdf_path) and os.path.getsize(out_pdf_path) > 0:
                        return _deliver(extracted_texts, doc, converter)
                split_summary.setdefault("uncertainties", []).append(
                    "几何分幅未产出可渲染单元块，回退标题切片/整图"
                )
            else:
                split_summary.update({
                    "mode": "no_frames",
                    "uncertainties": ["未检测到图框，回退标题切片/整图"],
                })
        except Exception as exc:  # noqa: BLE001 - 几何分幅异常时退回旧链路，不能让上传直接失败
            print(f"[CAD] 几何分幅失败，回退旧切片链路: {exc}")
            split_summary.update({
                "mode": "geometry_failed",
                "uncertainties": [f"几何分幅异常，已回退：{exc}"],
            })

        # 2. 旧链路：按图签标题文字切分
        system_sheets = detect_system_sheets(doc)
        if len(system_sheets) >= 1:
            extracted_texts = slice_and_render_cad_sheets(doc, system_sheets, out_pdf_path)
            if os.path.exists(out_pdf_path) and os.path.getsize(out_pdf_path) > 0:
                _stamp_split(extracted_texts, "sheet_fallback", split_summary.get("mode", "sheet_fallback"))
                if split_summary.get("mode") in {"unset", "no_frames", "geometry_failed", "frame_fallback"}:
                    split_summary["mode"] = "sheet_fallback"
                split_summary.setdefault("uncertainties", []).append(
                    f"已使用标题切片回退，共 {len(system_sheets)} 个系统图幅面"
                )
                return _deliver(extracted_texts, doc, converter)

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
        _stamp_split(extracted_texts, "page_fallback", "full_page_render")
        split_summary["mode"] = "page_fallback"
        split_summary.setdefault("uncertainties", []).append("未检出系统图分幅，已整图渲染")
        return _deliver(extracted_texts, doc, converter)
    finally:
        if dxf_to_clean:
            try:
                os.remove(dxf_to_clean)
            except OSError:
                pass
