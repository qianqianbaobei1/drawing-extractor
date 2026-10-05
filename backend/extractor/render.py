# -*- coding: utf-8 -*-
"""PDF 渲染：整页概览 + 大图分块。

视觉模型服务端会把输入图按固定尺寸降采样，往单张图里堆像素是白费的：
A4 用 200 DPI 出 2337px 已经够，A1 用同样 DPI 出 6600px 反而被缩回去，小字照样丢。

所以要分两步：
- 整页概览：长边压到模型真正能看清的尺寸，用来看全局结构；
- 分块：大图切成带重叠的块，每块按同样的目标像素渲染，等于把小字放大数倍再送检。

分块网格不再写死 2x2/3x3：网格数由「页面实际尺寸 ÷ 目标块长边」推导，任何幅面
（A4 到加长图）都落在同一套规则里，并且切完立刻做一次覆盖自检——切不满页就报错，
不允许静默漏区。所有阈值在 config/pipeline.json 的 render 段，可用环境变量覆盖。

坐标一律用整页归一化值，块的偏移在 vision.py 里折算回去。
"""
import os

try:
    import pymupdf
    pymupdf.TOOLS.set_graphics_min_line_width(0)   # 确认拿到的是带 TOOLS 的新版
    import pymupdf as fitz
except (ImportError, AttributeError):
    import fitz
    import pymupdf

from .config import pipeline

_R = pipeline()["render"]

OVERVIEW_LONG_EDGE = int(_R["overview_long_edge"])   # 每张送检图的长边都落在这个数附近
MIN_DPI, MAX_DPI = int(_R["min_dpi"]), int(_R["max_dpi"])  # 下限只防除零；上限防超大页把像素吹爆
TILE_TRIGGER_MM = float(_R["tile_trigger_mm"])       # 长边超过该值即自适应分块
TILE_DENSE_DRAWINGS = int(_R["tile_dense_drawings"])  # 矢量图形段落数阈值（防 A4 紧凑打印大图）
TILE_DENSE_ITEMS = int(_R["tile_dense_items"])       # 矢量线元总数阈值
TILE_OVERLAP = float(_R["tile_overlap"])             # 边缘重叠率，确保交界处器件不被截断
TILE_TARGET_LONG_MM = float(_R["tile_target_long_mm"])  # 每块的目标长边，决定等效放大倍数
# 密集系统图（小字多、设备表密）自动收紧目标长边，用更多块换更高有效分辨率。
TILE_TARGET_LONG_MM_DENSE = float(_R.get("tile_target_long_mm_dense", _R["tile_target_long_mm"]))
TILE_MAX_COLS = int(_R["tile_max_cols"])
TILE_MAX_ROWS = int(_R["tile_max_rows"])
COVERAGE_TOL = float(_R["coverage_tolerance"])
MM_PER_INCH = float(_R["mm_per_inch"])

# 国内设计院出图常用 0.012pt 的极细线（宽度不到 1/1000 英寸）。
# 200 DPI 下覆盖率只有 3%，渲染出来是一片浅灰，人看不清、模型也难认。
# MuPDF 可以把描边强制到最小设备像素宽，这里定下限，实测线条从 0% 深色像素变成 2.5%。
MIN_LINE_WIDTH_PX = float(_R["min_line_width_px"])
pymupdf.TOOLS.set_graphics_min_line_width(MIN_LINE_WIDTH_PX)


def dpi_for(long_side_pt: float, long_edge_px: int = OVERVIEW_LONG_EDGE) -> int:
    if long_side_pt <= 0:
        return MIN_DPI
    dpi = long_edge_px / (long_side_pt / 72)
    return int(max(MIN_DPI, min(MAX_DPI, round(dpi))))


def page_size_mm(page) -> tuple[float, float]:
    return page.rect.width / 72 * MM_PER_INCH, page.rect.height / 72 * MM_PER_INCH


def grid_for(width_mm: float, height_mm: float,
             target_mm: float = TILE_TARGET_LONG_MM) -> tuple[int, int]:
    """按页面实际尺寸推导分块网格，而不是写死 2x2 / 3x3。

    每块的长边不超过 target_mm，因此每块都以接近 OVERVIEW_LONG_EDGE 的像素数渲染时，
    等效放大倍数基本恒定——A2、A1、加长图都用同一套判据，不会出现"大图反而糊"的情况。
    """
    import math
    if target_mm <= 0:
        return 1, 1
    cols = max(1, min(TILE_MAX_COLS, math.ceil(width_mm / target_mm)))
    rows = max(1, min(TILE_MAX_ROWS, math.ceil(height_mm / target_mm)))
    return cols, rows


def plan_grid_clips(cols: int, rows: int, overlap: float = TILE_OVERLAP) -> list[dict]:
    """生成 cols×rows 个归一化 clip，相邻块按 overlap 互相压边。"""
    clips = []
    for row in range(rows):
        for col in range(cols):
            x0 = max(0.0, col / cols - overlap)
            y0 = max(0.0, row / rows - overlap)
            x1 = min(1.0, (col + 1) / cols + overlap)
            y1 = min(1.0, (row + 1) / rows + overlap)
            clips.append({"x": round(x0, 6), "y": round(y0, 6),
                          "w": round(x1 - x0, 6), "h": round(y1 - y0, 6)})
    return clips


def coverage_report(clips: list[dict]) -> dict:
    """自检切块是否铺满整页。

    块是矩形，x 与 y 方向可分别做区间并集；两者都满覆盖才算切满。
    返回覆盖率和是否有缝隙，调用方据此决定是否继续送检。
    """
    def _union(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
        merged: list[list[float]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1] + COVERAGE_TOL:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return [(a, b) for a, b in merged]

    def _patchy(union: list[tuple[float, float]]) -> bool:
        if not union:
            return True
        return union[0][0] > COVERAGE_TOL or union[-1][1] < 1.0 - COVERAGE_TOL or len(union) > 1

    x_union = _union([(c["x"], c["x"] + c["w"]) for c in clips])
    y_union = _union([(c["y"], c["y"] + c["h"]) for c in clips])
    x_covered = sum(b - a for a, b in x_union)
    y_covered = sum(b - a for a, b in y_union)
    return {
        "clip_count": len(clips),
        "x_covered": round(x_covered, 6),
        "y_covered": round(y_covered, 6),
        "covered_area_ratio": round(min(x_covered, 1.0) * min(y_covered, 1.0), 6),
        "fully_covered": not _patchy(x_union) and not _patchy(y_union),
    }


def render_pdf(pdf_path: str, long_edge: int = OVERVIEW_LONG_EDGE,
               dpi: int | None = None) -> list[str]:
    """每页渲染一张概览图，返回图片路径列表。"""
    doc = fitz.open(pdf_path)
    paths = []
    try:
        for i, page in enumerate(doc):
            use = dpi or dpi_for(max(page.rect.width, page.rect.height), long_edge)
            pix = page.get_pixmap(dpi=use)
            out = f"{pdf_path}.page{i + 1}.png"
            pix.save(out)
            paths.append(out)
    finally:
        doc.close()
    return paths


def plan_tiles(pdf_path: str, page_index: int, long_edge: int = OVERVIEW_LONG_EDGE,
               min_mm: float = TILE_TRIGGER_MM) -> list[dict]:
    """大图切块。返回 [{path, clip:{x,y,w,h}}]，clip 是整页归一化区域。

    页面不够大时返回空列表，调用方直接送整页概览。
    切块网格由页面尺寸推导；切完做覆盖自检，切不满页直接抛错而不是静默漏区。
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        rect = page.rect
        width_mm, height_mm = page_size_mm(page)
        long_mm = max(width_mm, height_mm)
        is_large = long_mm >= min_mm
        is_dense = False
        try:
            drawings = page.get_drawings()
            if (len(drawings) >= TILE_DENSE_DRAWINGS
                    or sum(len(d.get("items", [])) for d in drawings) >= TILE_DENSE_ITEMS):
                is_dense = True
        except Exception:
            pass

        if not is_large and not is_dense:
            return []

        cols, rows = grid_for(width_mm, height_mm,
                              TILE_TARGET_LONG_MM_DENSE if is_dense else TILE_TARGET_LONG_MM)
        clips = plan_grid_clips(cols, rows)
        report = coverage_report(clips)
        if not report["fully_covered"]:
            # 网格算法自身出问题就必须立刻暴露，不能带着漏区继续烧模型额度
            raise RuntimeError(
                f"切块覆盖自检未通过：{report}（页面 {width_mm:.0f}x{height_mm:.0f}mm，网格 {cols}x{rows}）"
            )

        tiles = []
        for index, clip in enumerate(clips):
            box = fitz.Rect(
                rect.x0 + clip["x"] * rect.width, rect.y0 + clip["y"] * rect.height,
                rect.x0 + (clip["x"] + clip["w"]) * rect.width,
                rect.y0 + (clip["y"] + clip["h"]) * rect.height,
            )
            use = dpi_for(max(box.width, box.height), long_edge)
            pix = page.get_pixmap(dpi=use, clip=box)
            out = f"{pdf_path}.page{page_index + 1}.tile{index}.png"
            pix.save(out)
            tiles.append({
                "path": out,
                "clip": clip,
                "row": index // cols,
                "col": index % cols,
                "dpi": use,
                "dense": is_dense,
                "page_size_mm": [round(width_mm, 1), round(height_mm, 1)],
                "coverage": report,
            })
        return tiles
    finally:
        doc.close()


def text_layer_stats(pdf_path: str) -> list[dict]:
    """每页有没有可用的文本层。

    国内设计院出图普遍把文字转曲，实测项目里的 2SAL3/2SAL2 都是 0 个词，
    所以这里只如实报数，不做“有文本层就能直接提字”的假设。
    判断混合流水线值不值得做之前，先拿它在真实图纸上跑一遍：
        python -m extractor.render 图纸.pdf
    """
    doc = fitz.open(pdf_path)
    try:
        out = []
        for i, page in enumerate(doc):
            words = page.get_text("words")
            width_mm, height_mm = page_size_mm(page)
            heights = sorted(w[3] - w[1] for w in words)
            out.append({
                "page": i + 1,
                "words": len(words),
                "size_mm": f"{width_mm:.0f}x{height_mm:.0f}",
                "median_word_pt": round(heights[len(heights) // 2], 1) if heights else 0,
            })
        return out
    finally:
        doc.close()


if __name__ == "__main__":  # python -m extractor.render 图纸.pdf
    import sys
    if len(sys.argv) < 2:
        print("用法: python -m extractor.render 图纸.pdf [更多图纸...]")
        print("当前生效的切片口径:")
        for key, value in pipeline()["render"].items():
            print(f"  {key} = {value}")
        raise SystemExit(0)
    for target in sys.argv[1:]:
        doc = fitz.open(target)
        pages = len(doc)
        doc.close()
        print(f"{target}  共 {pages} 页")
        for row in text_layer_stats(target):
            verdict = "有文本层" if row["words"] else "无文本层（文字已转曲）"
            print(f"  第{row['page']}页 {row['size_mm']}mm  {row['words']} 个词  {verdict}")
        tiles = plan_tiles(target, 0)
        if tiles:
            rep = tiles[0]["coverage"]
            print(f"  第1页 需切块 {len(tiles)} 块，覆盖 {rep['covered_area_ratio']:.4f}"
                  f"（{'已铺满' if rep['fully_covered'] else '存在漏区'}）")
        else:
            print("  第1页 不需切块（整页送检）")
