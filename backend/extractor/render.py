# -*- coding: utf-8 -*-
"""PDF 渲染：整页概览 + 大图分块。

视觉模型服务端会把输入图按固定尺寸降采样，往单张图里堆像素是白费的：
A4 用 200 DPI 出 2337px 已经够，A1 用同样 DPI 出 6600px 反而被缩回去，小字照样丢。

所以要分两步：
- 整页概览：长边压到模型真正能看清的尺寸，用来看全局结构；
- 分块：大图切成带重叠的块，每块按同样的目标像素渲染，等于把小字放大数倍再送检。

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

OVERVIEW_LONG_EDGE = 2400   # 与主流视觉模型的有效分辨率对齐；每张送检图的长边都落在这个数附近
MIN_DPI, MAX_DPI = 72, 400  # 下限只防除零：A1 概览就该是 72 DPI / 2383px，不是 6600px
TILE_TRIGGER_MM = 320       # 长边超过 A3 短边（约 297~320mm）即自适应分块，避免复杂系统图大模型输出超限
TILE_OVERLAP = 0.12         # 边缘重叠率提高至 12%，确保交界处断路器与回路不被截断
MM_PER_INCH = 25.4

# 国内设计院出图常用 0.012pt 的极细线（宽度不到 1/1000 英寸）。
# 200 DPI 下覆盖率只有 3%，渲染出来是一片浅灰，人看不清、模型也难认。
# MuPDF 可以把描边强制到最小设备像素宽，这里定 1.5px：
# 实测线条从 0% 深色像素变成 2.5%，视觉上从“几乎看不见”变成清晰黑线。
MIN_LINE_WIDTH_PX = 1.5
pymupdf.TOOLS.set_graphics_min_line_width(MIN_LINE_WIDTH_PX)


def dpi_for(long_side_pt: float, long_edge_px: int = OVERVIEW_LONG_EDGE) -> int:
    if long_side_pt <= 0:
        return MIN_DPI
    dpi = long_edge_px / (long_side_pt / 72)
    return int(max(MIN_DPI, min(MAX_DPI, round(dpi))))


def page_size_mm(page) -> tuple[float, float]:
    return page.rect.width / 72 * MM_PER_INCH, page.rect.height / 72 * MM_PER_INCH


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
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        rect = page.rect
        if max(page_size_mm(page)) < min_mm:
            return []
        cols = rows = 2 if max(page_size_mm(page)) < 900 else 3
        tiles = []
        for row in range(rows):
            for col in range(cols):
                x0 = max(0.0, col / cols - TILE_OVERLAP)
                y0 = max(0.0, row / rows - TILE_OVERLAP)
                x1 = min(1.0, (col + 1) / cols + TILE_OVERLAP)
                y1 = min(1.0, (row + 1) / rows + TILE_OVERLAP)
                clip = fitz.Rect(
                    rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                    rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height,
                )
                use = dpi_for(max(clip.width, clip.height), long_edge)
                pix = page.get_pixmap(dpi=use, clip=clip)
                out = f"{pdf_path}.page{page_index + 1}.tile{row}{col}.png"
                pix.save(out)
                tiles.append({"path": out,
                              "clip": {"x": round(x0, 6), "y": round(y0, 6),
                                       "w": round(x1 - x0, 6), "h": round(y1 - y0, 6)}})
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
    for target in sys.argv[1:]:
        doc = fitz.open(target)
        pages = len(doc)
        doc.close()
        print(f"{target}  共 {pages} 页")
        for row in text_layer_stats(target):
            verdict = "有文本层" if row["words"] else "无文本层（文字已转曲）"
            print(f"  第{row['page']}页 {row['size_mm']}mm  {row['words']} 个词  {verdict}")
        tiles = plan_tiles(target, 0)
        note = f"需切块 {len(tiles)} 块" if tiles else "不需切块（整页送检）"
        print(f"  第1页 {note}")
