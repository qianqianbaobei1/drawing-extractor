# -*- coding: utf-8 -*-
"""真实图纸准确率基准：把"解析准不准、切片有没有漏"变成可复跑的数字。

用法（默认不调用付费模型，只做本地可测的部分）：

    # 只看切片覆盖 + CAD 原生解析 + 与人工清单对账（零费用）
    ../.venv/bin/python benchmark.py 图纸目录/ --ground-truth 人工报价.xlsx

    # 加上视觉模型识别与交叉验证（会调用付费接口）
    ../.venv/bin/python benchmark.py 图纸目录/ --vision --confirm-cost

输出：
    <out>/benchmark.json    机器可读明细
    <out>/benchmark.md      人看的报告（含逐图一行结论）

设计原则：只报告能测出来的事。没有人工清单就不输出"准确率"，
没跑模型就不输出"识别率"，避免用缺失的分母编出好看的百分比。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extractor.config import config_health, pipeline  # noqa: E402
from extractor.render import coverage_report, grid_for, plan_grid_clips, render_pdf  # noqa: E402

DRAWING_SUFFIXES = (".pdf", ".dwg", ".dxf")


def norm(text) -> str:
    return re.sub(r"[\s\-_/]+", "", str(text or "")).upper()


def split_specs(value) -> set[str]:
    """把一格里的多个型号拆开：'MCB-63 C16A/1P、RCBO-63 D20A/3PN' -> {两段}。"""
    parts = re.split(r"[、,;；\n]+", str(value or ""))
    return {norm(p) for p in parts if norm(p)}


def load_ground_truth(path: Path | None) -> list[dict]:
    """读人工整理的清单（xlsx 或 csv），只要有"规格/型号"类列就够用。"""
    if not path or not path.exists():
        return []
    rows: list[dict] = []
    if path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows.append({str(k): v for k, v in row.items() if k})
        return rows
    try:
        import openpyxl
    except ImportError:
        print("[benchmark] 未安装 openpyxl，跳过人工清单对账")
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    for ws in wb.worksheets:
        header: list[str] = []
        for values in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v).strip() for v in values]
            if not any(cells):
                continue
            if not header:
                header = cells
                continue
            rows.append({header[i]: cells[i] for i in range(min(len(header), len(cells)))})
    wb.close()
    return rows


SPEC_HINTS = ("规格", "型号", "名称", "spec", "model", "商品名称", "物料")


def extract_gt_specs(rows: list[dict]) -> set[str]:
    specs: set[str] = set()
    for row in rows:
        for key, value in row.items():
            if any(hint in str(key).lower() for hint in SPEC_HINTS):
                specs |= split_specs(value)
    return specs


def slice_report(pdf_path: Path) -> dict:
    """切片体检：每一页的网格、覆盖自检、每块等效分辨率（mm/px，越小越清楚）。"""
    pages = []
    try:
        import pymupdf
    except ImportError:
        return {"error": "未安装 PyMuPDF"}
    doc = pymupdf.open(str(pdf_path))
    try:
        for idx, page in enumerate(doc):
            width_mm = page.rect.width / 72 * 25.4
            height_mm = page.rect.height / 72 * 25.4
            long_mm = max(width_mm, height_mm)
            is_dense = False
            try:
                drawings = page.get_drawings()
                r_cfg = pipeline()["render"]
                is_dense = (len(drawings) >= r_cfg["tile_dense_drawings"]
                            or sum(len(d.get("items", [])) for d in drawings) >= r_cfg["tile_dense_items"])
            except Exception:
                pass
            if long_mm < float(pipeline()["render"]["tile_trigger_mm"]) and not is_dense:
                pages.append({"page": idx + 1, "size_mm": [round(width_mm), round(height_mm)],
                              "tiled": False, "tiles": 1, "coverage": 1.0, "fully_covered": True,
                              "dense": is_dense,
                              "effective_mm_per_px": round(long_mm / float(pipeline()["render"]["overview_long_edge"]), 4)})
                continue
            target = float(pipeline()["render"]["tile_target_long_mm_dense"] if is_dense
                           else pipeline()["render"]["tile_target_long_mm"])
            cols, rows = grid_for(width_mm, height_mm, target)
            clips = plan_grid_clips(cols, rows)
            rep = coverage_report(clips)
            tile_long = max(width_mm / cols, height_mm / rows)
            pages.append({"page": idx + 1, "size_mm": [round(width_mm), round(height_mm)],
                          "tiled": True, "grid": [cols, rows], "tiles": len(clips),
                          "coverage": rep["covered_area_ratio"], "fully_covered": rep["fully_covered"],
                          "dense": is_dense, "target_long_mm": target,
                          "tile_long_mm": round(tile_long, 1),
                          "effective_mm_per_px": round(tile_long / float(pipeline()["render"]["overview_long_edge"]), 4)})
        return {"pages": pages}
    finally:
        doc.close()


def run_local(pdf_or_dxf: Path, workdir: Path) -> dict:
    """不花钱的部分：切片体检 + CAD 原生解析（仅 CAD 输入）。"""
    out: dict = {"slice": None, "cad": None}
    if pdf_or_dxf.suffix.lower() == ".pdf":
        out["slice"] = slice_report(pdf_or_dxf)
    else:
        try:
            from extractor.cad import is_cad_path
            from extractor.cad_extractor import extract_cad_table_data
            if is_cad_path(str(pdf_or_dxf)):
                if pdf_or_dxf.suffix.lower() == ".dwg":
                    from extractor.cad import dwg_to_dxf
                    dxf_path = workdir / f"{pdf_or_dxf.stem}.dxf"
                    dwg_to_dxf(str(pdf_or_dxf), str(dxf_path))
                    source = dxf_path
                else:
                    source = pdf_or_dxf
                raw = extract_cad_table_data(str(source))
                out["cad"] = {"boxes": len(raw.boxes), "circuits": len(raw.circuits),
                              "extra_devices": len(raw.extra_devices),
                              "breaker_fill": (round(sum(1 for c in raw.circuits if c.breaker)
                                                     / max(1, len(raw.circuits)), 3))}
        except Exception as exc:  # noqa: BLE001 - 基准工具要如实记录失败而不是中断
            out["cad"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def run_vision(pdf_path: Path, workdir: Path) -> dict:
    """走一次真实识别链路，记录结果、交叉验证命中率与费用。"""
    from extractor.assemble import assemble
    from extractor.corroborate import corroborate
    from extractor.vision import VisionProvider

    provider = VisionProvider()
    if not provider.configured:
        return {"error": "VISION_API_KEY 未配置"}
    started = time.time()
    images = render_pdf(str(pdf_path))
    tiles = []
    try:
        tiles = plan_tiles(str(pdf_path), 0)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"切片失败: {exc}"}
    items = [(t["path"], 1, t["clip"]) for t in tiles] or [(images[0], 1, None)]
    raw = provider.extract(items)
    result = assemble(raw, {"model": provider.model})
    native_lines: list[str] = []
    try:
        import pymupdf
        doc = pymupdf.open(str(pdf_path))
        try:
            for page in doc:
                native_lines.extend((page.get_text("text") or "").splitlines())
        finally:
            doc.close()
    except Exception:
        pass
    stats = corroborate(result, [l for l in native_lines if l.strip()], source="pdf_text")
    return {
        "model": provider.model,
        "images": len(items),
        "tiles": len(tiles),
        "boxes": len(result.boxes),
        "circuits": len(result.circuits),
        "components": len(result.components),
        "component_specs": [c.spec for c in result.components if c.spec],
        "uncertainties": len(result.uncertainties),
        "corroboration": stats,
        "usage": provider.last_usage_summary,
        "elapsed_s": round(time.time() - started, 1),
    }


def coverage_vs_truth(result: dict, gt_specs: set[str]) -> dict:
    """与人工清单对账：只统计能对上/对不上的型号，不编造分母。"""
    got = {norm(c.get("spec")) for c in result.get("components", []) if c.get("spec")}
    if not gt_specs:
        return {"ground_truth_available": False}
    hit = {s for s in gt_specs if s and s in got}
    return {
        "ground_truth_available": True,
        "ground_truth_specs": len(gt_specs),
        "extracted_specs": len(got),
        "matched_specs": len(hit),
        "recall": round(len(hit) / len(gt_specs), 4) if gt_specs else 0.0,
        "missing_examples": sorted(gt_specs - hit)[:10],
        "extra_examples": sorted(got - gt_specs)[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("targets", nargs="+", help="图纸文件或目录")
    parser.add_argument("--ground-truth", default="", help="人工整理的清单 xlsx/csv（可选，用于召回率对账）")
    parser.add_argument("--vision", action="store_true", help="调用视觉模型（付费）")
    parser.add_argument("--confirm-cost", action="store_true", help="确认承担模型调用费用")
    parser.add_argument("--out", default="benchmark_out", help="报告输出目录")
    args = parser.parse_args()

    files: list[Path] = []
    for target in args.targets:
        p = Path(target)
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.suffix.lower() in DRAWING_SUFFIXES))
        elif p.exists():
            files.append(p)
    if not files:
        print("没有找到图纸文件（支持 .pdf/.dwg/.dxf）")
        return 2

    if args.vision and not args.confirm_cost:
        print("--vision 会调用付费接口，请同时加 --confirm-cost 明确承担费用。")
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "work"
    work.mkdir(exist_ok=True)

    gt_specs = extract_gt_specs(load_ground_truth(Path(args.ground_truth) if args.ground_truth else None))
    print(f"人工清单型号数：{len(gt_specs)}（{'已加载' if gt_specs else '未提供，将不输出召回率'}）")

    reports = []
    total_cost = 0.0
    for path in files:
        print(f"\n=== {path.name} ===")
        entry: dict = {"file": path.name, "suffix": path.suffix.lower()}
        entry.update(run_local(path, work))
        if entry.get("slice"):
            bad = [p for p in entry["slice"].get("pages", []) if not p.get("fully_covered")]
            entry["slice_ok"] = not bad
            print(f"  切片：{len(entry['slice']['pages'])} 页，覆盖自检 "
                  f"{'全部通过' if not bad else f'{len(bad)} 页存在漏区'}")
        if entry.get("cad"):
            print(f"  CAD 原生：{json.dumps(entry['cad'], ensure_ascii=False)}")
        if args.vision and path.suffix.lower() == ".pdf":
            vision = run_vision(path, work)
            entry["vision"] = vision
            if "error" in vision:
                print(f"  视觉识别：失败 {vision['error']}")
            else:
                corr = vision.get("corroboration") or {}
                print(f"  视觉识别：{vision['boxes']} 箱 / {vision['circuits']} 回路 / "
                      f"{vision['components']} 元器件，{vision['images']} 图（{vision['tiles']} 块），"
                      f"{vision['elapsed_s']}s")
                print(f"  交叉验证：{'可用' if corr.get('available') else '不可用（无原生文字）'}"
                      f" {corr.get('corroborated')}/{corr.get('values_checked')} 命中"
                      f" ({corr.get('coverage_rate', 0) * 100:.1f}%)")
                usage = vision.get("usage") or {}
                total_cost += float(usage.get("total_cost") or 0.0)
        reports.append(entry)

    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": config_health(),
        "files": len(reports),
        "slice_all_ok": all(r.get("slice_ok", True) for r in reports),
        "vision_calls": sum(1 for r in reports if r.get("vision")),
        "total_cost_cny": round(total_cost, 6),
        "ground_truth": coverage_vs_truth(
            {"components": [{"spec": spec}
                            for r in reports
                            for spec in ((r.get("vision") or {}).get("component_specs") or [])]},
            gt_specs),
        "reports": reports,
    }

    (out_dir / "benchmark.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 图纸解析基准报告", "", f"生成时间：{summary['generated_at']}", ""]
    lines.append(f"- 配置目录：`{summary['config']['config_dir']}`")
    lines.append(f"- 覆盖目录：`{summary['config']['override_dir'] or '（未设置）'}`")
    lines.append(f"- 图纸数：{summary['files']}｜切片覆盖自检：{'全部通过' if summary['slice_all_ok'] else '存在漏区'}")
    lines.append(f"- 模型调用：{summary['vision_calls']} 次｜累计费用：¥{summary['total_cost_cny']}")
    lines.append("")
    lines.append("| 图纸 | 页数 | 切片 | 覆盖 | CAD 原生 | 视觉识别 | 交叉验证命中 |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in reports:
        is_pdf = r.get("suffix") == ".pdf"
        pages = len((r.get("slice") or {}).get("pages") or []) if is_pdf else "-"
        tiled = sum(1 for p in ((r.get("slice") or {}).get("pages") or []) if p.get("tiled"))
        cov = (("全部铺满" if r.get("slice_ok") else "有漏区") if is_pdf else "-")
        cad = r.get("cad")
        cad_txt = (f"{cad['boxes']}箱/{cad['circuits']}回路" if cad and "error" not in cad
                   else (f"失败" if cad else "-"))
        vis = r.get("vision")
        vis_txt = (f"{vis['boxes']}箱/{vis['circuits']}回路/{vis['components']}器件"
                   if vis and "error" not in vis else (vis["error"] if vis else "-"))
        corr = ((vis or {}).get("corroboration") or {})
        corr_txt = (f"{corr.get('corroborated', 0)}/{corr.get('values_checked', 0)}"
                    f" ({corr.get('coverage_rate', 0) * 100:.0f}%)" if corr.get("available") else "无原生文字")
        lines.append(f"| {r['file']} | {pages} | {tiled} 页切块 | {cov} | {cad_txt} | {vis_txt} | {corr_txt} |")
    lines.append("")
    gt = summary.get("ground_truth") or {}
    if gt.get("ground_truth_available"):
        lines.append(f"**与人工清单对账**：人工 {gt['ground_truth_specs']} 个型号，"
                     f"提取 {gt['extracted_specs']} 个，命中 {gt['matched_specs']} 个，"
                     f"召回率 {gt['recall'] * 100:.1f}%。")
        if gt.get("missing_examples"):
            lines.append(f"- 人工有、提取没对上的例子：{'、'.join(gt['missing_examples'])}")
        if gt.get("extra_examples"):
            lines.append(f"- 提取有、人工清单没有的例子：{'、'.join(gt['extra_examples'])}（可能是模型多提，也可能是人工清单口径不同）")
        lines.append("")
    lines.append("> 本报告只呈现实际测到的数字：未提供人工清单就不输出召回率，未加 `--vision` 就不输出识别结果。")
    lines.append("> 交叉验证“未命中”不等于错误（可能文字转曲或落在切片边界），需回到图纸逐条核对。")
    (out_dir / "benchmark.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\n报告已写入：{out_dir / 'benchmark.md'} 与 {out_dir / 'benchmark.json'}")
    if total_cost:
        print(f"本轮模型费用合计：¥{total_cost:.4f}")
    return 0 if summary["slice_all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
