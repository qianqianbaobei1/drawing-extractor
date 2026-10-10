# -*- coding: utf-8 -*-
"""CAD (DXF/DWG) 原生几何解析器。

从可读取的 CAD 文字与几何实体生成箱体和回路候选。实体内容、块结构、比例、图层与
布局空间可能不完整或无法解释；输出不等同于完整图纸事实，必须经过独立证据、覆盖
与人工核验，不能承诺 100% 准确或零遗漏。
"""

from collections import defaultdict
import math
import os
import re
from typing import Any

from .cad import _explode_texts, clean_mtext, dwg_to_dxf, load_dxf_document
from .cad_ledger import entity_source, sha256_file, source_key
from .panel_release import UNCONFIRMED_QTY_NOTE, UNCONFIRMED_QTY_WARNING
from .schema import (
    Box, BBox, Circuit, Evidence, EvidenceType, ExtraDevice, GroundedField,
    RawExtraction, Requirement, ReviewStatus, Uncertainty,
)

from .config import domain as _domain, pipeline as _pipeline

_B = _domain()["breaker"]
_CAD_CONFIG = _pipeline()["cad"]
_V = _domain()["vocabulary"]
_D = _domain()["cad"]

RE_CIRCUIT_NO = re.compile(_V["circuit_no_regex"], re.I)
RE_PHASE = re.compile(_V["phase_regex"], re.I)
RE_POWER = re.compile(_V["power_regex"], re.I)
RE_CURRENT = re.compile(_V["current_regex"], re.I)
_CABLE_CFG = _domain().get("cable") or {}
_CABLE_FAMILY_RES = [
    re.compile(pattern, re.I) for pattern in _CABLE_CFG.get("family_patterns", [])
]
_SIGNAL_FAMILY_RES = [
    re.compile(pattern, re.I) for pattern in _CABLE_CFG.get("signal_family_patterns", [])
]
_LAYING_RE = re.compile(_CABLE_CFG.get("laying_method_regex") or r"$^", re.I)
_SECTION_RE = re.compile(r"\d+\s*[xX×*]\s*\d+")
_AMP_SEG_RE = re.compile(r"^\d+(?:\.\d+)?A$", re.I)
_CODE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9\-])([A-Za-z0-9]+(?:[\-/][A-Za-z0-9]+)*)(?![A-Za-z0-9])"
)

BREAKER_PREFIXES = _B["model_prefixes"]
RE_BREAKER = re.compile(_B["model_regex_template"].format(prefixes=BREAKER_PREFIXES), re.I)
RE_BREAKER_FALLBACK = re.compile(_B["fallback_regex"], re.I)
RE_DISALLOWED_PREFIXES = re.compile(_B["disallowed_prefixes_regex"], re.I)

RE_ENGINEERING_UNIT = re.compile(_V["engineering_unit_regex"], re.I)
RE_NATIONAL_ATLAS = re.compile(_V["national_atlas_regex"], re.I)
CIRCUIT_NO_MAX_DIGITS = int(_V["circuit_no_max_digits"])

# 电压/频率/功率这类工程量单独成文时不是箱号，按配置正则排除（可按项目补规则，不改代码）
NON_PANEL_TOKEN_RES = [re.compile(pattern, re.I) for pattern in _V.get("non_panel_token_regexes", [])]
_PANEL_FAMILY_SUFFIXES = frozenset(
    part.upper()
    for part in re.findall(r"[A-Za-z]{2,}", str((_domain().get("catalog") or {}).get("panel_prefix_pattern") or ""))
)


_SPD_FAMILY_RES = [re.compile(pattern, re.I) for pattern in _D.get("spd_family_patterns", [])]
_SPD_CLASS_RE = re.compile(_D.get("spd_class_regex") or r"$^")


def _is_signal_cable(text: str) -> bool:
    """双绞、屏蔽和控制电缆是信号线，不是进线电力电缆。"""
    return any(pattern.search(text or "") for pattern in _SIGNAL_FAMILY_RES)


def _is_spd_callout(text: str) -> bool:
    """浪涌型号或「Ⅱ级-4P」这种整段极数标是保护器。试验说明句不是。

    加号后面是前一台器件的附件。附件里出现浪涌系列，不把整段当成另一只保护器。
    """
    raw = (text or "").strip()
    if not raw or any(mark in raw for mark in ("。", "；", "，")):
        return False
    if _SPD_CLASS_RE.match(raw):
        return True
    if len(raw) > 32:
        return False
    head = re.split(r"[+＋]", raw, maxsplit=1)[0]
    return any(pattern.search(head) for pattern in _SPD_FAMILY_RES)


def _looks_like_cable(text: str) -> bool:
    """电缆由线缆系列词法或「截面 × 敷设」结构判定，不单独维护第二份型号表。"""
    raw = text or ""
    if any(pattern.search(raw) for pattern in _CABLE_FAMILY_RES):
        return True
    return bool(_SECTION_RE.search(raw) and _LAYING_RE.search(raw))


class _CableMatcher:
    """兼容既有 RE_CABLE.match 调用；判定与 _looks_like_cable 相同。"""

    def match(self, text: str):
        return text if _looks_like_cable(text or "") else None

    def search(self, text: str):
        return self.match(text)


RE_CABLE = _CableMatcher()


def _token_is_device_rating(token: str) -> bool:
    """分段里出现额定电流（如 63A）的是器件规格，不是箱号。"""
    parts = [part for part in re.split(r"[-/]", token) if part]
    return any(_AMP_SEG_RE.match(part) for part in parts)


def _token_rejected(token: str) -> bool:
    if not re.search(r"[A-Za-z]", token):
        return True
    # 没有数字的代号只在「字母段 + 箱种后缀」这种形状下成立，例如 XFDT-AT。
    if not re.search(r"\d", token) and not _has_panel_shape(token):
        return True
    if re.search(rf"\d{{{CIRCUIT_NO_MAX_DIGITS},}}", token):
        return True
    if RE_NATIONAL_ATLAS.match(token) or RE_ENGINEERING_UNIT.match(token):
        return True
    if RE_PHASE.match(token) or RE_CIRCUIT_NO.match(token):
        return True
    parts = [part for part in re.split(r"[-/]", token) if part]
    if any(pattern.match(token) or any(pattern.match(part) for part in parts) for pattern in NON_PANEL_TOKEN_RES):
        return True
    if re.search(r"\d+[Xx×]\d+", token) or _looks_like_cable(token):
        return True
    if re.search(r"(?:SC|PC|KBG|JDG|MT)\d", token, re.I):
        return True
    if RE_BREAKER.match(token) or re.fullmatch(r"[CDB]\d{1,3}A", token, re.I):
        return True
    if _token_is_device_rating(token):
        return True
    if RE_DISALLOWED_PREFIXES.match(token) or any(RE_DISALLOWED_PREFIXES.match(part) for part in parts):
        return True
    if not _has_panel_shape(token):
        return True
    return False


def _segment_is_panel_core(part: str) -> bool:
    """一段里要有至少两个字母再夹着数字，或单字母加至少两位数字（C01）。"""
    if re.fullmatch(r"[A-Z]\d{2,}", part, re.I):
        return True
    return bool(re.search(r"[A-Z]{2,}", part, re.I) and re.search(r"\d", part))


def _has_panel_shape(token: str) -> bool:
    """1X、40R、65H2、TMY-4X 没有这样的段。B1ATPY1、10RDAL、AW1/2/3/4-CDZ 有。

    纯字母也可以：一段是配置里的箱种后缀（AT、AL），另一段至少三个字母（XFDT-AT）。
    """
    parts = [part for part in re.split(r"[-/]", token) if part]
    if any(_segment_is_panel_core(part) for part in parts):
        return True
    letter_parts = [part for part in parts if re.fullmatch(r"[A-Z]{2,}", part, re.I)]
    if len(letter_parts) < 2:
        return False
    return any(
        part.upper() in _PANEL_FAMILY_SUFFIXES
        and any(other.upper() not in _PANEL_FAMILY_SUFFIXES and len(other) >= 3 for other in letter_parts)
        for part in letter_parts
    )


def _gap_matches_step(gap: float, step: float, slack: float) -> bool:
    """间距允许缺一档（约 2 倍步距），更大的空洞不当成同一列。"""
    if step <= 0:
        return False
    ratio = gap / step
    nearest = round(ratio)
    return 1 <= nearest <= 2 and abs(ratio - nearest) <= slack


def _cluster_ids(points: list[tuple[float, float]], gap: float) -> list[int]:
    """字高倍数以内能连起来的文字算同一张图。图与图之间的空白把它们分开。"""
    count = len(points)
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_left] = root_right

    cell = gap if gap > 0 else 1.0
    grid: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (x, y) in enumerate(points):
        grid[(int(x // cell), int(y // cell))].append(index)
    for index, (x, y) in enumerate(points):
        cx, cy = int(x // cell), int(y // cell)
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for other in grid.get((cx + ox, cy + oy), ()):
                    if other <= index:
                        continue
                    ox_pt, oy_pt = points[other]
                    if abs(ox_pt - x) <= gap and abs(oy_pt - y) <= gap:
                        union(index, other)
    return [find(index) for index in range(count)]


def _bridge_orphan_circuits(
    points: list[tuple[float, float]],
    cluster_of: list[int],
    is_circuit: list[bool],
    is_panel: list[bool],
    bridge_gap: float,
) -> list[int]:
    """没有箱号的回路团，若和最近的箱号团只隔一小段空白，就并进那张图。

    两台箱各自已经带着回路号时不合并，避免上下叠图被连成一张。
    """
    comps: dict[int, list[int]] = defaultdict(list)
    for index, cid in enumerate(cluster_of):
        comps[cid].append(index)
    panel_ids = [cid for cid, members in comps.items() if any(is_panel[i] for i in members)]
    orphan_ids = [
        cid for cid, members in comps.items()
        if any(is_circuit[i] for i in members) and not any(is_panel[i] for i in members)
    ]
    parent = {cid: cid for cid in comps}

    def find(cid: int) -> int:
        while parent[cid] != cid:
            parent[cid] = parent[parent[cid]]
            cid = parent[cid]
        return cid

    def _bounds(cid: int) -> tuple[float, float, float, float]:
        xs = [points[i][0] for i in comps[cid]]
        ys = [points[i][1] for i in comps[cid]]
        return min(xs), max(xs), min(ys), max(ys)

    panel_points = {
        cid: [points[i] for i in comps[cid] if is_panel[i]]
        for cid in panel_ids
    }
    panel_bounds = {cid: _bounds(cid) for cid in panel_ids}
    for oid in orphan_ids:
        circuits = [points[i] for i in comps[oid] if is_circuit[i]]
        ox0, ox1, oy0, oy1 = _bounds(oid)
        best_gap = None
        best_pid = None
        for pid, ppoints in panel_points.items():
            px0, px1, py0, py1 = panel_bounds[pid]
            dx = 0.0 if ox1 >= px0 and px1 >= ox0 else min(abs(ox0 - px1), abs(px0 - ox1))
            dy = 0.0 if oy1 >= py0 and py1 >= oy0 else min(abs(oy0 - py1), abs(py0 - oy1))
            if max(dx, dy) > bridge_gap:
                continue
            gap = min(max(abs(px - cx), abs(py - cy)) for px, py in ppoints for cx, cy in circuits)
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_pid = pid
        if best_pid is not None and best_gap is not None and best_gap <= bridge_gap:
            parent[find(oid)] = find(best_pid)
    return [find(cid) for cid in cluster_of]


def iter_panel_codes(text: str) -> list[str]:
    """按统一词法列出文本中的箱号。电缆、穿管、回路号、额定电流都不会出现在结果里。"""
    clean = re.split(r"[:：]", text or "", maxsplit=1)[0].strip()
    clean = re.sub(r"^消防", "", clean).strip()
    found: list[str] = []
    seen: set[str] = set()
    for match in _CODE_TOKEN_RE.finditer(clean):
        token = match.group(1).strip()
        if _token_rejected(token):
            continue
        code = token.upper()
        if code not in seen:
            seen.add(code)
            found.append(code)
    return found


def extract_panel_code(text: str) -> str | None:
    """通用电气配电箱/柜体编号提取器（无项目/图纸特定硬编码）。"""
    codes = iter_panel_codes(text)
    return codes[0] if codes else None


def _exact_feed_code(text: str) -> str | None:
    """整段就是箱号，或「箱号:回路号」。说明文字里顺带出现的编号不算配出对象。"""
    head = re.split(r"[:：]", text or "", maxsplit=1)[0].strip()
    code = extract_panel_code(head)
    if not code:
        return None
    if re.sub(r"\s+", "", head).upper() != code:
        return None
    return code


def _is_breaker_legend(text: str) -> bool:
    """「图中 MCCB…表示…」是符号说明，不是这台箱上的开关。"""
    marks = ((_domain().get("cad") or {}).get("breaker_legend_marks") or ())
    raw = text or ""
    return any(mark and mark in raw for mark in marks)


def _text_is_breaker(text: str) -> bool:
    """整段就是箱号时不是开关。B1-AL2 这类编号开头的字母数字不是极数。"""
    if not text or _looks_like_cable(text) or _exact_feed_code(text) or _is_breaker_legend(text):
        return False
    return bool(RE_BREAKER.search(text) or RE_BREAKER_FALLBACK.search(text))


_ROW_DEVICE_RES = {
    field: [re.compile(pattern, re.I) for pattern in patterns]
    for field, patterns in (_D.get("row_device_families") or {}).items()
    if field in {"contactor", "ct", "thermal"}
}
_THERMAL_SETTING_RE = re.compile(_D.get("thermal_setting_regex") or r"$^")


def _row_device_field(text: str) -> str | None:
    """同一行上的接触器、热继电器、互感器型号归到对应栏，不写进负荷名。"""
    raw = (text or "").strip()
    if not raw or len(raw) > 32 or any(mark in raw for mark in ("。", "；", "，")):
        return None
    if _text_is_breaker(raw) or _looks_like_cable(raw) or _is_spd_callout(raw):
        return None
    for field, patterns in _ROW_DEVICE_RES.items():
        if any(pattern.search(raw) for pattern in patterns):
            return field
    return None


class _PanelCodeMatcher:
    """包装 extract_panel_code 以保持与既有 re.search 接口的完全兼容。"""
    def search(self, text: str):
        code = extract_panel_code(text)
        if code:
            class _Match:
                def __init__(self, c):
                    self._c = c
                def group(self, n=1):
                    return self._c
            return _Match(code)
        return None


PANEL_CODE_PATTERN = _PanelCodeMatcher()


def assign_preview_bboxes(boxes: list, circuits: list, cad_texts: list | None,
                          page_by_box: dict[str, int] | None = None) -> None:
    """用 CAD 文字坐标给箱体和回路打页面归一化框。没有坐标时才用配置里的整页兜底。"""
    pad = float(_CAD_CONFIG.get("preview_bbox_pad", 0.015))
    min_size = float(_CAD_CONFIG.get("preview_bbox_min", 0.04))
    fallback = _CAD_CONFIG.get("preview_bbox_fallback") or [0.05, 0.05, 0.9, 0.9]
    fb_x, fb_y, fb_w, fb_h = (float(v) for v in fallback)
    preferred = page_by_box or {}

    by_page: dict[int, list] = defaultdict(list)
    for item in cad_texts or []:
        if item.get("page") is None or item.get("x") is None or item.get("y") is None:
            continue
        by_page[int(item["page"])].append(item)

    bounds: dict[int, tuple[float, float, float, float]] = {}
    for page, items in by_page.items():
        xs = [float(t["x"]) for t in items]
        ys = [float(t["y"]) for t in items]
        bounds[page] = (min(xs), max(xs), min(ys), max(ys))

    def _fallback(page: int) -> BBox:
        return BBox(x=fb_x, y=fb_y, w=fb_w, h=fb_h, page=max(1, page))

    def _rect(page: int, xs: list[float], ys: list[float]) -> BBox | None:
        if page not in bounds or not xs:
            return None
        minx, maxx, miny, maxy = bounds[page]
        spanx = max(maxx - minx, 1.0)
        spany = max(maxy - miny, 1.0)
        x0 = (min(xs) - minx) / spanx
        x1 = (max(xs) - minx) / spanx
        y0 = (maxy - max(ys)) / spany
        y1 = (maxy - min(ys)) / spany
        x = max(0.0, x0 - pad)
        y = max(0.0, y0 - pad)
        w = min(1.0 - x, max(x1 - x0, 0.0) + 2 * pad)
        h = min(1.0 - y, max(y1 - y0, 0.0) + 2 * pad)
        if w < min_size:
            x = max(0.0, min(x, 1.0 - min_size))
            w = min(min_size, 1.0 - x)
        if h < min_size:
            y = max(0.0, min(y, 1.0 - min_size))
            h = min(min_size, 1.0 - y)
        if w <= 0 or h <= 0:
            return None
        return BBox(x=round(x, 4), y=round(y, 4), w=round(min(w, 1.0), 4),
                    h=round(min(h, 1.0), 4), page=page)

    def _hits(page: int, needle: str) -> list:
        target = (needle or "").strip().upper()
        if not target:
            return []
        return [t for t in by_page.get(page, []) if str(t.get("text") or "").strip().upper() == target]

    box_bbox: dict[str, BBox] = {}
    for box in boxes:
        code = (box.code or "").strip()
        pages = sorted({int(t["page"]) for t in (cad_texts or [])
                        if str(t.get("text") or "").strip().upper() == code.upper() and t.get("page")})
        page = preferred.get(code) or (pages[0] if pages else 1)
        if pages and page not in pages:
            page = max(pages, key=lambda p: len(_hits(p, code)))
        rect = _rect(page, [float(t["x"]) for t in _hits(page, code)],
                     [float(t["y"]) for t in _hits(page, code)])
        box.bbox = rect or _fallback(page)
        if code:
            box_bbox[code] = box.bbox

    for circuit in circuits:
        page = int(getattr(box_bbox.get(circuit.box), "page", 0) or preferred.get(circuit.box) or 1)
        number = (circuit.circuit_no or "").strip()
        hits = [] if number == "进线" else _hits(page, number)
        rect = _rect(page, [float(t["x"]) for t in hits], [float(t["y"]) for t in hits]) if hits else None
        circuit.bbox = rect or box_bbox.get(circuit.box) or _fallback(page)


def _unique_joined(items: list) -> str:
    seen: list[str] = []
    for item in items:
        text = item[0] if isinstance(item, tuple) else str(item)
        cleaned = str(text).strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return " ".join(seen)

GENERIC_DISCARD_TERMS = set(_D["generic_discard_terms"])


def _text_leads_with_panel(text: str, code: str) -> bool:
    """标头以箱号开头，或箱种后缀紧贴箱号。名称中间夹着的箱号是配出对象，不是这台箱的标头。"""
    compact = re.sub(r"\s+", "", text or "")
    if compact.startswith("消防"):
        compact = compact[len("消防"):]
    head = re.split(r"[:：]", compact, maxsplit=1)[0]
    code_key = code.upper()
    if head.upper().startswith(code_key):
        return True
    suffixes = tuple(_D.get("unit_caption_suffix") or ())
    for suffix in suffixes:
        if suffix and head.startswith(suffix) and head[len(suffix):].upper().startswith(code_key):
            return True
    # 「路灯照明配电箱00ALZ」编号贴在箱种后缀后面。
    if head.upper().endswith(code_key):
        before = head[: -len(code)]
        if any(suffix and before.endswith(suffix) for suffix in suffixes):
            return True
    return False


def _specific_caption(text: str) -> str:
    """箱名是后缀前面连着的汉字。箱号插在汉字和后缀之间时，不把两边拼成一个名字。"""
    suffixes = tuple(_D.get("unit_caption_suffix") or ())
    if not text or not suffixes:
        return ""
    clean = str(text).replace("系统图", "")
    alt = "|".join(re.escape(suffix) for suffix in sorted(suffixes, key=len, reverse=True))
    found = re.findall(rf"[\u4e00-\u9fa5]+(?:{alt})", clean)
    if not found:
        return ""
    name = max(found, key=len)
    # 说明句子里顺带写到的箱名带着敷设动词，名称格不会这样写。
    route_marks = tuple(_D.get("caption_route_marks") or ())
    prefix = name
    for suffix in sorted(suffixes, key=len, reverse=True):
        if name.endswith(suffix):
            prefix = name[: -len(suffix)]
            break
    if any(mark and mark in prefix for mark in route_marks):
        return ""
    if any(mark in clean for mark in ("。", "；")):
        return ""
    return name


_QTY_HEADER = (
    re.compile(r"共\s*(\d+)\s*台"),
    re.compile(r"(?<!第)(\d+)\s*台"),
    re.compile(r"(?<![A-Za-z0-9])[×xX]\s*(\d+)(?![A-Za-z0-9])"),
)
_QTY_NEARBY = re.compile(r"共(\d+)台|[×xX](\d+)")


def _positive_quantity(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        number = int(raw)
    except ValueError:
        return None
    return number if number > 0 else None


def _nearby_box_quantity(text: str) -> int | None:
    """只认单独成行的「共N台」或「×N」。回路上的「1台」不是箱体台数。"""
    compact = re.sub(r"[\s，,。；;：:]", "", text or "")
    if not compact or len(compact) > 12:
        return None
    match = _QTY_NEARBY.fullmatch(compact)
    if not match:
        return None
    return _positive_quantity(next(group for group in match.groups() if group))


def _collect_box_quantities(header_text: str, nearby_texts: list[tuple]) -> set[int]:
    found: set[int] = set()
    for pattern in _QTY_HEADER:
        match = pattern.search(header_text or "")
        if not match:
            continue
        number = _positive_quantity(match.group(1))
        if number:
            found.add(number)
        break
    for item in nearby_texts:
        number = _nearby_box_quantity(str(item[0]) if item else "")
        if number:
            found.add(number)
    return found


def _extract_box_metadata(header_text: str, nearby_texts: list[tuple]) -> dict[str, Any]:
    """从配电箱标头及周边文字提取箱体名称、安装方式、箱体型号、防护等级等。"""
    meta = {
        "name": "",
        "install": "",
        "size": "",
        "ip_rating": "",
        "quantity": 1,
        "note": "",
    }
    # 名称提取。后缀来自配置，电表箱、配电柜和配电箱用同一套。
    clean_h = header_text.replace("系统图", "").strip()
    specific = _specific_caption(clean_h)
    if specific:
        meta["name"] = specific
    else:
        suffixes = tuple(_D.get("unit_caption_suffix") or ())
        hits = [suffix for suffix in suffixes if suffix and suffix in clean_h]
        if hits:
            meta["name"] = max(hits, key=len)

    # 扫描周边文字
    for item in nearby_texts:
        t = item[0]
        if any(k in t for k in ["嵌墙安装", "暗装"]):
            meta["install"] = "嵌墙安装"
        elif any(k in t for k in ["壁挂式", "明装"]):
            meta["install"] = "壁挂式"
        elif any(k in t for k in ["落地式", "落地安装"]):
            meta["install"] = "落地式"

        # 优先匹配真实物理尺寸 (如 800x600x200 或 600*400*160)
        m_dim = re.search(r"\b\d{2,4}\s*[xX*×]\s*\d{2,4}(?:\s*[xX*×]\s*\d{2,4})?\b", t)
        if m_dim and not meta["size"]:
            meta["size"] = m_dim.group(0).replace(" ", "")
        elif any(m in t for m in ["XRM", "JXF", "GGD", "XXM", "PZ30"]):
            for m in ["XRM", "JXF", "GGD", "XXM", "PZ30"]:
                if m in t and not meta["size"]:
                    meta["size"] = m
                    break

        m_ip = re.search(r"IP\d{1,2}[A-Za-z]?", t, re.I)
        if m_ip:
            meta["ip_rating"] = m_ip.group(0).upper()

        # 忠实摘录图纸原文备注，绝不凭空创作文学描述
        if any(k in t for k in ["消防标志", "防火", "防腐", "室外", "防爆", "特别要求", "备注"]):
            meta["note"] = t.strip()

    # 台数识别：标题上的 共N台 / N台 / 独立的 ×N，以及标题旁边单独写的 共N台、×N。
    # 乘号必须自成一词，避免把尺寸 450×350 和箱号 9KX3 里的 X3 当成台数。
    # "N台"排除"第N台"（那是序号不是台数）。旁边的「1台」多半是设备台数，不拿来当箱体台数。
    # 两处写出的台数不一致时不放行。识别不到时 quantity 仍为 1，只作为单箱候选。
    stated = _collect_box_quantities(header_text, nearby_texts)
    if len(stated) == 1:
        meta["quantity"] = stated.pop()
        meta["quantity_recognized"] = True
    else:
        meta["quantity_recognized"] = False

    return meta


def _native_claim(store: dict, value: str, source: dict, field_path: str) -> GroundedField:
    """把一个字段指回写出它的那条 CAD 文字。没有 handle 时不假装已经对上实体。"""
    evidence_ids: list[str] = []
    if value and source and source.get("handle"):
        evidence_id = "cad." + source_key(source).replace("|", ".")
        if evidence_id not in store:
            store[evidence_id] = Evidence(
                evidence_id=evidence_id,
                evidence_type=EvidenceType.TEXT.value,
                raw_content=value,
                origin="cad_native",
                file_sha256=str(source.get("file_sha256") or ""),
                handle=str(source.get("handle") or ""),
                owner=str(source.get("owner") or ""),
                insert_path=str(source.get("insert_path") or ""),
                layout=str(source.get("layout") or "Model"),
            )
        evidence_ids.append(evidence_id)
    return GroundedField(
        value=value,
        raw_value=value,
        value_evidence_ids=evidence_ids,
        confidence=None,
        review_status=ReviewStatus.PARSED_OK.value if evidence_ids else ReviewStatus.UNASSESSED.value,
    )


def _source_lookup(text_sources: dict, text: str, x: float, y: float) -> dict:
    key_text = (text or "").strip()
    if not key_text:
        return {}
    rx, ry = round(float(x), 2), round(float(y), 2)
    exact = text_sources.get((rx, ry, key_text))
    if exact:
        return dict(exact)
    for (sx, sy, stored), source in text_sources.items():
        if sx == rx and sy == ry and key_text in stored:
            return dict(source)
    return {}


def _point_of(value: str, items) -> tuple[float, float] | None:
    wanted = (value or "").strip()
    if not wanted:
        return None
    for item in items:
        if str(item[0]).strip() == wanted:
            return (float(item[1]), float(item[2]))
    return None


def _bind_observed_fields(target, store: dict, text_sources: dict, fields: list[tuple[str, str, tuple | None]]) -> None:
    claims = {}
    for field_path, value, point in fields:
        source = _source_lookup(text_sources, value, point[0], point[1]) if point else {}
        claims[field_path] = _native_claim(store, value, source, field_path)
    target.claims = claims


def extract_cad_table_data(dxf_or_doc: Any) -> RawExtraction:
    """从 CAD 数据库提取箱体与回路候选；返回结果仍需和图面、目录进行对账。"""
    source_path = dxf_or_doc if isinstance(dxf_or_doc, str) else ""
    if isinstance(dxf_or_doc, str):
        if dxf_or_doc.lower().endswith(".dwg"):
            work_dir = os.path.dirname(dxf_or_doc)
            base_name = os.path.splitext(os.path.basename(dxf_or_doc))[0]
            cache_dir = os.path.join(work_dir, ".cad_cache")
            os.makedirs(cache_dir, exist_ok=True)
            import hashlib
            file_size = os.path.getsize(dxf_or_doc) if os.path.exists(dxf_or_doc) else 0
            cache_key = hashlib.md5(f"{base_name}_{file_size}".encode()).hexdigest()
            hash_dxf = os.path.join(cache_dir, f"{cache_key}.dxf")
            named_dxf = os.path.join(cache_dir, f"{base_name}.dxf")
            if os.path.exists(hash_dxf) and os.path.getsize(hash_dxf) > 0:
                cached_dxf = hash_dxf
            elif os.path.exists(named_dxf) and os.path.getsize(named_dxf) > 0:
                cached_dxf = named_dxf
            else:
                cached_dxf = hash_dxf
                dwg_to_dxf(dxf_or_doc, cached_dxf)
            dxf_or_doc = cached_dxf
        doc = load_dxf_document(dxf_or_doc)
    else:
        doc = dxf_or_doc

    msp = doc.modelspace()
    file_hash = ""
    if source_path and os.path.exists(source_path):
        try:
            file_hash = sha256_file(source_path)
        except OSError:
            file_hash = ""
    text_sources: dict[tuple, dict] = {}
    evidence_store: dict[str, Evidence] = {}

    # 1. 抓取当前模型空间中可识别的文字实体；空间窗口按本图字高估算，不保证适用于任意布局。
    all_texts = []
    for e in list(msp.query("TEXT")) + list(msp.query("MTEXT")):
        try:
            raw_t = getattr(e.dxf, "text", "") if e.dxftype() == "TEXT" else getattr(e, "text", "")
            t = clean_mtext(raw_t).strip()
            if not t:
                continue
            x = float(e.dxf.insert.x)
            y = float(e.dxf.insert.y)
            h = float(getattr(e.dxf, "height", 0) if e.dxftype() == "TEXT" else getattr(e.dxf, "char_height", 0))
            source = entity_source(e, owner="modelspace", insert_path="")
            source["file_sha256"] = file_hash
            text_sources[(round(x, 2), round(y, 2), t)] = source
            all_texts.append((t, x, y, h))
        except Exception:
            pass

    # 块参照里的文字和属性也是图面上的字。每次插入用自己的 insert_path，不能只凭块定义 handle 合并。
    block_cache: dict = {}
    try:
        inserts = list(msp.query("INSERT"))
    except Exception:
        inserts = []
    for ins in inserts:
        try:
            exploded = _explode_texts(ins, block_cache)
        except Exception:
            continue
        for sub in exploded:
            t = str(sub.get("text") or "").strip()
            if not t:
                continue
            try:
                x = float(sub["x"])
                y = float(sub["y"])
                h = float(sub.get("height") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            key = (round(x, 2), round(y, 2), t)
            if key in text_sources:
                continue
            source = dict(sub.get("source") or {})
            source["file_sha256"] = file_hash
            source.setdefault("layout", "Model")
            text_sources[key] = source
            all_texts.append((t, x, y, h))

    if not all_texts:
        return RawExtraction(
            boxes=[],
            circuits=[],
            extra_devices=[],
            requirements=[],
            uncertainties=[Uncertainty.from_text("CAD模型空间未检出任何有效文字实体")],
        )

    # 2. 自适应物理尺度推导 (Adaptive Scale Estimation)
    # 基于全图有效文字高度中位数自适应推导图纸比例尺，彻底杜绝 1:1 或 1:100 绘图时的尺度敏感问题
    import statistics
    valid_heights = [h for _, _, _, h in all_texts if h > 0]
    med_h = (float(statistics.median(valid_heights)) if valid_heights
             else float(_CAD_CONFIG["circuit_scale_basis_height"]))
    base_h = max(med_h, 1.0)
    scale_factor = base_h / float(_CAD_CONFIG["circuit_scale_basis_height"])
    header_h_threshold = base_h * 1.15
    row_dy = base_h * float(_CAD_CONFIG.get("header_row_dy_heights", 1.2))
    caption_dx = base_h * float(_CAD_CONFIG.get("header_caption_dx_heights", 40))
    field_dx = base_h * float(_CAD_CONFIG.get("header_field_scan_dx_heights", 30))
    field_labels = [str(item) for item in _D.get("table_field_labels", [])]
    caption_suffixes = tuple(_D.get("unit_caption_suffix") or [])
    system_keywords = tuple(_D.get("system_keywords_include") or [])

    def _is_field_label(text: str) -> bool:
        stripped = text.strip()
        folded = re.sub(r"[（）()]", "", stripped)
        return any(
            stripped == label or stripped.startswith(label) or folded == label or folded.startswith(label)
            for label in field_labels
        ) and len(stripped) <= 12

    load_reject = tuple(_D.get("load_name_reject_substrings") or ())
    drawing_words = tuple(_D.get("system_keywords_exclude") or ())
    wire_kinds = tuple(_D.get("wire_kind_words") or ())

    def _without_marking_note(text: str) -> str:
        """「馈电柜（带明显消防标志）」留下设备名。整句都是标志要求时没有剩下的名称。"""
        return re.sub(r"[（(][^）)]*消防标志[^）)]*[）)]", "", text or "").strip()

    def _is_wire_kind_note(text: str) -> bool:
        """「信号线+电源线」或后面再跟一根电缆型号，是接线注记，不是负荷名。"""
        if not wire_kinds:
            return False
        head = re.split(r"[:：]", text or "", maxsplit=1)[0].strip()
        parts = [part for part in re.split(r"[+＋、,，/\s]+", head) if part]
        return bool(parts) and all(part in wire_kinds for part in parts)

    def _is_prose_note(text: str) -> bool:
        """换行或连写分号的是控制说明，不是某一回的名称。"""
        raw = text or ""
        return "\n" in raw or raw.count("；") + raw.count(";") >= 2

    def _is_bus_laying_note(text: str) -> bool:
        """「RS485总线-CT/PC25」只剩协议和敷设，没有设备名。"""
        raw = text or ""
        if not _LAYING_RE.search(raw):
            return False
        folded = _LAYING_RE.sub(" ", raw)
        for kind in wire_kinds:
            folded = folded.replace(kind, "")
        folded = re.sub(r"[A-Za-z0-9/_.+\-×xX*\s]+", "", folded)
        return not any("\u4e00" <= ch <= "\u9fff" for ch in folded)

    def _text_is_branch_load(text: str) -> bool:
        """表头、图名和安装注记不是回路负荷。箱字出现在设备名里仍然可以。"""
        text = _without_marking_note(text)
        if not text or not any("\u4e00" <= ch <= "\u9fa5" for ch in text):
            return False
        # 以逗号、分号开头的是被拆开的说明，不是设备名。
        if text[:1] in "，、；。,;":
            return False
        # 「公共用电防火桥架」是桥架标注。以「至」「沿」开头的路径可以顺带写桥架。
        if "桥架" in text and not text.startswith(("至", "沿")):
            return False
        if _is_wire_kind_note(text) or _is_prose_note(text) or _is_bus_laying_note(text):
            return False
        if _is_field_label(text):
            return False
        # 以箱号开头的图名才排除。设备名后面带箱号的，如「空调室外机配电箱:WDKTAP2」，仍是负荷。
        code = extract_panel_code(text)
        if code and any(suffix in text for suffix in caption_suffixes):
            compact = re.sub(r"\s+", "", text)
            if compact.upper().startswith(code.upper()):
                return False
        if any(keyword in text for keyword in system_keywords):
            return False
        if any(keyword in text for keyword in drawing_words):
            return False
        if any(token in text for token in load_reject):
            return False
        # 表头被拆开后括号对不上，或只剩「宽x高x深」，都不是负荷名。
        if text.count("（") + text.count("(") != text.count("）") + text.count(")"):
            return False
        folded = re.sub(r"[\s（）()xX×*]", "", text)
        if folded in {"宽高深", "宽高"}:
            return False
        return True

    def _row_caption(x: float, y: float) -> str:
        """编号格右侧、同一行上的箱名。跳过表头字段，遇到系统图标题就停。"""
        rights = [item for item in all_texts
                  if 0 < item[1] - x <= caption_dx and abs(item[2] - y) <= row_dy]
        rights.sort(key=lambda item: item[1])
        crossed_field = False
        for text, *_rest in rights:
            if _is_field_label(text):
                crossed_field = True
                continue
            if not crossed_field:
                return ""
            if any(keyword in text for keyword in system_keywords):
                return ""
            if any(suffix in text for suffix in caption_suffixes):
                return text.strip()
            return ""
        return ""

    # 检测所有配电箱标头
    candidates = []
    # 1. 参数表「设备编号」右侧单元格。距离按字高缩放，不写死毫米。
    id_labels = [label for label in field_labels if label.endswith("编号")] or ["设备编号", "箱体编号"]
    for dt, dx, dy, dh in all_texts:
        if not any(label in dt for label in id_labels):
            continue
        clean = dt
        for label in id_labels:
            clean = clean.replace(label, "")
        clean = clean.replace(":", "").replace("：", "").strip()
        code = extract_panel_code(clean)
        title = dt
        cx, cy, ch = dx, dy, dh
        if not code:
            neighbors = [item for item in all_texts
                         if abs(item[2] - dy) <= row_dy and 0 < item[1] - dx <= field_dx]
            neighbors.sort(key=lambda item: item[1])
            for ct, nx, ny, nh in neighbors:
                if _is_field_label(ct):
                    continue
                found = extract_panel_code(ct)
                if found and ct.strip().upper() == found:
                    code = found
                    title, cx, cy, ch = ct, nx, ny, nh
                    break
        if not code:
            continue
        caption = _row_caption(cx, cy)
        if caption and caption not in title:
            title = f"{title} {caption}".strip()
        candidates.append((code, title, cx, cy, ch))

    # 2. 常规图面大字标头扫描
    for t, x, y, h in all_texts:
        # 排除回路出线引用、规范、图纸编号与干线附注。
        # 含冒号的标头（如"2ALE：应急照明配电箱"）不整行丢弃，
        # 取冒号前的部分做柜号匹配。
        if any(k in t for k in ["引至", "配出", "备用", "市电", "规范", "图集", "图号", "图纸编号", "干线"]):
            continue
        t_head = re.split(r"[:：]", t, maxsplit=1)[0]
        m = PANEL_CODE_PATTERN.search(t_head)
        if m:
            code = m.group(1).upper()
            if code in GENERIC_DISCARD_TERMS:
                continue
            if RE_CIRCUIT_NO.match(code) and not any(suffix in t for suffix in caption_suffixes):
                continue
            if not _text_leads_with_panel(t_head, code):
                continue
            caption = _row_caption(x, y) if t.strip().upper() == code else ""
            title = f"{t} {caption}".strip() if caption and caption not in t else t
            is_header = (
                any(suffix in title for suffix in caption_suffixes)
                or any(keyword in title for keyword in system_keywords)
                or h >= header_h_threshold
            )
            if is_header:
                candidates.append((code, title, x, y, h))

    # 干线竖列：横坐标落在同一窄带里，纵坐标步距稳定，且是不同箱号。
    # 窄带按点的自身横坐标取，避免整张图被连成一列后步距失真。
    riser_codes: set[str] = set()
    x_tol = base_h * float(_CAD_CONFIG.get("riser_x_heights", 2))
    min_count = int(_CAD_CONFIG.get("riser_min_count", 4))
    gap_slack = float(_CAD_CONFIG.get("riser_gap_slack", 0.35))
    min_gap = base_h * 2
    max_gap = base_h * float(_CAD_CONFIG.get("riser_max_gap_heights", 40))
    exact_codes = []
    for text, x, y, h in all_texts:
        code = extract_panel_code(text)
        if code and text.strip().upper() == code:
            exact_codes.append((code, x, y, h, text))
    seen_bands: set[tuple] = set()
    for seed in exact_codes:
        band = [item for item in exact_codes if abs(item[1] - seed[1]) <= x_tol]
        band_key = tuple(sorted((item[0], round(item[1], 1), round(item[2], 1)) for item in band))
        if band_key in seen_bands:
            continue
        seen_bands.add(band_key)
        ordered = sorted(band, key=lambda item: item[2])
        current: list[tuple] = []
        step = None

        def _flush() -> None:
            distinct = {item[0] for item in current}
            if step is not None and len(distinct) >= min_count:
                for code, x, y, h, raw in current:
                    if code in distinct:
                        riser_codes.add(code)
                        candidates.append((code, raw, x, y, h))
                        distinct.discard(code)

        for item in ordered:
            if not current:
                current = [item]
                step = None
                continue
            gap = abs(item[2] - current[-1][2])
            if gap <= base_h * 0.5:
                continue
            if step is None and min_gap <= gap <= max_gap:
                step = gap
                current.append(item)
            elif step is not None and gap <= max_gap * 2 and _gap_matches_step(gap, step, gap_slack):
                current.append(item)
            else:
                _flush()
                current = [item]
                step = None
        _flush()

    # 同一张图里的文字间距有限。上下两台箱叠在一起时，回路只归这张图里的箱号。
    cluster_gap = base_h * float(_CAD_CONFIG.get("text_cluster_gap_heights", 10))
    bridge_gap = base_h * float(_CAD_CONFIG.get("text_cluster_bridge_heights", 16))
    points = [(x, y) for _, x, y, _ in all_texts]
    cluster_of = _cluster_ids(points, cluster_gap)
    is_circuit = [bool(RE_CIRCUIT_NO.match(text)) for text, *_rest in all_texts]
    is_panel = []
    for text, *_rest in all_texts:
        code = extract_panel_code(text)
        is_panel.append(bool(code) and text.strip().upper() == code)
    cluster_of = _bridge_orphan_circuits(points, cluster_of, is_circuit, is_panel, bridge_gap)
    pos_cluster = {
        (round(x, 3), round(y, 3)): cluster_of[index]
        for index, (_, x, y, _) in enumerate(all_texts)
    }
    all_circuit_anchors: list[tuple[str, float, float, int]] = []
    for index, (text, x, y, _h) in enumerate(all_texts):
        if RE_CIRCUIT_NO.match(text):
            all_circuit_anchors.append((text, x, y, cluster_of[index]))
    max_dx = float(_CAD_CONFIG["circuit_box_max_dx"]) * scale_factor
    max_dy = float(_CAD_CONFIG["circuit_box_max_dy"]) * scale_factor
    diagram_min = int(_CAD_CONFIG.get("diagram_min_circuit_support", 3))
    support_by_cluster: dict[int, int] = defaultdict(int)
    for _, _, _, cid in all_circuit_anchors:
        support_by_cluster[cid] += 1

    def _circuit_support(x: float, y: float) -> int:
        return support_by_cluster.get(pos_cluster.get((round(x, 3), round(y, 3))), 0)

    for text, x, y, h in all_texts:
        code = extract_panel_code(text)
        if not code or text.strip().upper() != code:
            continue
        cid = pos_cluster.get((round(x, 3), round(y, 3)))
        # 箱号和回路号落在同一张图里，就按这张图来挂，不要求字高更大。
        if support_by_cluster.get(cid, 0) >= diagram_min:
            candidates.append((code, text, x, y, h))

    # 柜号归一化去重（同位置或择优选取最完整名称，优先包含“配电箱/系统图”正规标题）
    by_code: dict[str, list[tuple[str, str, float, float, float]]] = defaultdict(list)
    for c in candidates:
        by_code[c[0]].append(c)

    # 同一箱号会在干线图和系统图上各写一次。每处都保留，回路挂到离它最近的那一处。
    occurrences: list[tuple[str, str, float, float, float, int]] = []
    seen_occ: set[tuple[str, float, float]] = set()
    for group in by_code.values():
        for code, title, x, y, h in group:
            key = (code, round(x, 1), round(y, 1))
            if key in seen_occ:
                continue
            seen_occ.add(key)
            cid = pos_cluster.get((round(x, 3), round(y, 3)), -1)
            occurrences.append((code, title, x, y, h, cid))
    occ_by_cluster: dict[int, list[int]] = defaultdict(list)
    for index, occ in enumerate(occurrences):
        occ_by_cluster[occ[5]].append(index)

    # 自适应推导表格回路行高容差 (Row Y Tolerance)
    # 分析回路锚点真实间距直方图；若无密集回路，则按 1.83 倍字高自适应推导
    line_spacing = None
    if len(all_circuit_anchors) >= 2:
        sorted_anchors = sorted(all_circuit_anchors, key=lambda a: (round(a[1] / max(base_h * 10, 1.0)), -a[2]))
        y_diffs = []
        for i in range(len(sorted_anchors) - 1):
            a1, a2 = sorted_anchors[i], sorted_anchors[i+1]
            if abs(a1[1] - a2[1]) <= base_h * 6:
                diff = abs(a1[2] - a2[2])
                if base_h * 1.2 <= diff <= base_h * 15:
                    y_diffs.append(diff)
        if y_diffs:
            line_spacing = statistics.median(y_diffs)

    if line_spacing and line_spacing > base_h:
        row_y_tolerance = line_spacing * float(_CAD_CONFIG.get("circuit_row_spacing_factor", 0.45))
    else:
        row_y_tolerance = base_h * float(_CAD_CONFIG["circuit_row_tolerance_ratio"])

    # 4. 回路只在同一张图里挑最近的那一处箱号。这张图没有箱号时才在距离范围内找。
    dx_weight = float(_CAD_CONFIG.get("circuit_dx_weight", 2.5))
    owned: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for cno, cx, cy, ccid in all_circuit_anchors:
        pool = occ_by_cluster.get(ccid) or range(len(occurrences))
        best_dist = float("inf")
        best_index = None
        for index in pool:
            _code, _title, x, y, _h, _cid = occurrences[index]
            dx = cx - x
            dy = cy - y
            if abs(dx) > max_dx or abs(dy) > max_dy:
                continue
            dist = (dx * dx_weight) ** 2 + dy ** 2
            if dist < best_dist:
                best_dist = dist
                best_index = index
        if best_index is not None:
            owned[occurrences[best_index][0]].append((cno, cx, cy))

    def _occ_rank(item: tuple, circuits: list[tuple[str, float, float]]) -> tuple:
        title, x, y, h = item[1], item[2], item[3], item[4]
        near = sum(1 for _, cx, cy in circuits if abs(cx - x) <= max_dx and abs(cy - y) <= max_dy)
        return (
            near,
            _circuit_support(x, y),
            100 if any(suffix in title for suffix in caption_suffixes) else 0,
            50 if any(label in title for label in id_labels) else 0,
            10 if h >= base_h * 1.2 else 0,
            len(title),
        )

    best_headers: dict[str, tuple[str, float, float, float]] = {}
    assigned_circuits: dict[str, list[tuple[str, float, float]]] = {}
    for code, group in by_code.items():
        circuits = owned.get(code) or []
        best = max(group, key=lambda item, circuits=circuits: _occ_rank(item, circuits))
        best_headers[code] = (best[1], best[2], best[3], best[4])
        # 同一箱号的几处图都算数。只留离主标注最近的一处，会把另一列回路裁掉。
        assigned_circuits[code] = list(circuits)

    # 5. 构建每个箱体与其所属回路详细参数
    extracted_boxes: list[Box] = []
    extracted_circuits: list[Circuit] = []
    extracted_devices: list[ExtraDevice] = []
    extracted_reqs: list[Requirement] = []
    extracted_uncertainties: list[Uncertainty] = []

    # 自适应包围盒外扩距离（确保覆盖多列系统图表格横向跨度，通常跨越 50~80 倍字高）
    pad_x = max(float(_CAD_CONFIG["circuit_pad_x"]) * scale_factor,
                base_h * float(_CAD_CONFIG["circuit_pad_x_height_ratio"]))
    pad_y = max(float(_CAD_CONFIG["circuit_pad_y"]) * scale_factor,
                base_h * float(_CAD_CONFIG["circuit_pad_y_height_ratio"]))
    def_win_x = max(float(_CAD_CONFIG["circuit_def_window_x"]) * scale_factor,
                    base_h * float(_CAD_CONFIG["circuit_def_window_x_height_ratio"]))
    def_win_y_down = max(float(_CAD_CONFIG["circuit_def_window_y_down"]) * scale_factor,
                         base_h * float(_CAD_CONFIG["circuit_def_window_y_down_height_ratio"]))
    def_win_y_up = max(float(_CAD_CONFIG["circuit_def_window_y_up"]) * scale_factor,
                       base_h * float(_CAD_CONFIG["circuit_def_window_y_up_height_ratio"]))

    spd_span = base_h * float(_CAD_CONFIG.get("spd_owner_heights", 32))
    class_gap = base_h * float(_CAD_CONFIG.get("spd_class_gap_heights", 4))
    spd_callouts = [
        (spd_text.strip(), spd_x, spd_y)
        for spd_text, spd_x, spd_y, _spd_h in all_texts
        if _is_spd_callout(spd_text)
    ]
    spd_models = [item for item in spd_callouts if not _SPD_CLASS_RE.match(item[0])]
    spd_by_code: dict[str, list[str]] = defaultdict(list)

    def _spd_owner(x: float, y: float) -> str | None:
        nearest = None
        for occ_code, _title, ox, oy, _oh, _cid in occurrences:
            dist = max(abs(ox - x), abs(oy - y))
            if nearest is None or dist < nearest[0]:
                nearest = (dist, occ_code)
        if nearest is not None and nearest[0] <= spd_span:
            return nearest[1]
        return None

    for spd_text, spd_x, spd_y in spd_callouts:
        if not _SPD_CLASS_RE.match(spd_text):
            continue
        if spd_models:
            model = min(spd_models, key=lambda item: max(abs(item[1] - spd_x), abs(item[2] - spd_y)))
            if max(abs(model[1] - spd_x), abs(model[2] - spd_y)) <= class_gap:
                continue
        owner = _spd_owner(spd_x, spd_y)
        if owner:
            spd_by_code[owner].append(spd_text)
    for spd_text, spd_x, spd_y in spd_models:
        owner = _spd_owner(spd_x, spd_y)
        if not owner:
            continue
        spd_by_code[owner].append(spd_text)
        for class_text, class_x, class_y in spd_callouts:
            if not _SPD_CLASS_RE.match(class_text):
                continue
            if max(abs(class_x - spd_x), abs(class_y - spd_y)) <= class_gap:
                spd_by_code[owner].append(class_text)

    unrecognized_qty: list[str] = []
    for code in sorted(best_headers.keys()):
        title, hx, hy, hh = best_headers[code]
        c_anchors = assigned_circuits[code]

        # 孤立代号丢掉；竖列里步距稳定的干线箱号留下来。
        if (
            not c_anchors
            and code not in riser_codes
            and not any(suffix in title for suffix in caption_suffixes)
        ):
            continue

        # 同一箱号可能有几块分开的系统图。每块单独扩一圈，不要把两块之间的空白连成一个大框。
        if c_anchors:
            groups: list[list[tuple[str, float, float]]] = []
            for anchor in c_anchors:
                placed = False
                for group in groups:
                    if any(abs(anchor[1] - item[1]) <= max_dx and abs(anchor[2] - item[2]) <= max_dy for item in group):
                        group.append(anchor)
                        placed = True
                        break
                if not placed:
                    groups.append([anchor])
            for group in groups:
                if any(abs(hx - item[1]) <= max_dx and abs(hy - item[2]) <= max_dy for item in group):
                    group.append(("", hx, hy))
                    break

            def _in_group(x: float, y: float) -> bool:
                for group in groups:
                    xs = [item[1] for item in group]
                    ys = [item[2] for item in group]
                    if min(xs) - pad_x <= x <= max(xs) + pad_x and min(ys) - pad_y <= y <= max(ys) + pad_y:
                        return True
                return False

            panel_texts = [t for t in all_texts if _in_group(t[1], t[2])]
        else:
            bx0, bx1 = hx - def_win_x, hx + def_win_x
            by0, by1 = hy - def_win_y_down, hy + def_win_y_up
            panel_texts = [t for t in all_texts if bx0 <= t[1] <= bx1 and by0 <= t[2] <= by1]

        # 提取箱体属性元数据
        meta = _extract_box_metadata(title, panel_texts)
        # 离回路最近的常常只是箱号。同一箱号另一处写着的名称仍然属于这台箱。
        if not _specific_caption(meta["name"]):
            borrowed: list[str] = []
            for _alt_code, alt_title, *_rest in by_code.get(code, []):
                found = _specific_caption(alt_title)
                if found:
                    borrowed.append(found)
            if borrowed:
                meta["name"] = max(set(borrowed), key=lambda item: (borrowed.count(item), len(item)))
        quantity_confirmed = bool(meta.get("quantity_recognized"))
        if not quantity_confirmed:
            # 大量箱体同时缺台数时后面合并成一条，避免淹没真正的错误。
            unrecognized_qty.append(code)
            if UNCONFIRMED_QTY_NOTE not in (meta.get("note") or ""):
                meta["note"] = (
                    f"{meta['note']}；{UNCONFIRMED_QTY_NOTE}" if meta.get("note") else UNCONFIRMED_QTY_NOTE
                )
        box = Box(
            code=code,
            name=meta["name"] or "配电箱",
            ip_rating=meta["ip_rating"],
            install=meta["install"],
            size=meta["size"],
            quantity=meta["quantity"],
            quantity_confirmed=quantity_confirmed,
            note=meta["note"],
        )
        _bind_observed_fields(box, evidence_store, text_sources, [
            ("box.code", box.code, (hx, hy)),
            ("box.location", box.location, _point_of(box.location, panel_texts)),
            ("box.ip_rating", box.ip_rating, _point_of(box.ip_rating, panel_texts)),
        ])
        extracted_boxes.append(box)

        # 同一个回路号可能在干线上空标一次、在系统图里再写一次。留下旁边有电缆或开关的那一次。
        c_anchors.sort(key=lambda item: -item[2])
        # 被回路 breaker 字段实际消费的文本：SPD 收集时跳过这些，
        # 防止同一文本既进回路又进元器件造成双计
        consumed_breaker_texts: set[str] = set()
        consumed_cable_pts: set[tuple[int, int]] = set()
        chosen_rows: dict[str, tuple[int, Circuit, str]] = {}
        # 贴在开关上的汉字是这只开关的附注。名称栏离开关有一整列。
        breaker_note_gap = base_h * float(_CAD_CONFIG.get("breaker_note_gap_heights", 2))
        breaker_points = [
            (x, y) for t, x, y, _h in panel_texts
            if _text_is_breaker(t)
        ]
        # 一行的边界是同一列里相邻回路号的中线。并排另一列基线略有错开时，不把这一列的行带压扁。
        spacing_ceiling = base_h * float(_CAD_CONFIG.get("riser_max_gap_heights", 40))
        column_x = base_h * float(_CAD_CONFIG.get("circuit_column_x_heights", 4))
        # 半个字高以内看成同一基线。并排的两列不能因为差几个绘图单位就把电缆抢走。
        baseline_slop = max(base_h * 0.5, 1.0)

        def _col_limit(anchor_x: float, anchor_y: float, text_x: float) -> float:
            direction = text_x - anchor_x
            if direction == 0:
                return pad_x
            gaps = [
                abs(other_x - anchor_x)
                for _cno, other_x, other_y in c_anchors
                if abs(other_x - anchor_x) > column_x
                and (other_x - anchor_x) * direction > 0
                and abs(other_y - anchor_y) <= spacing_ceiling
            ]
            if not gaps:
                return pad_x
            return min(gaps) / 2

        def _row_limit(anchor_x: float, anchor_y: float) -> float:
            gaps = [
                abs(other_y - anchor_y)
                for _cno, other_x, other_y in c_anchors
                if abs(other_x - anchor_x) <= column_x
                and base_h * 0.5 < abs(other_y - anchor_y) <= spacing_ceiling
            ]
            if not gaps:
                return row_y_tolerance
            return min(gaps) / 2

        row_of: dict[int, list] = defaultdict(list)
        for text_item in panel_texts:
            best_index = None
            best_key = None
            for index, (_cno, anchor_x, anchor_y) in enumerate(c_anchors):
                if abs(text_item[1] - anchor_x) > _col_limit(anchor_x, anchor_y, text_item[1]) or text_item[0] == _cno:
                    continue
                dy = abs(text_item[2] - anchor_y)
                if dy > _row_limit(anchor_x, anchor_y):
                    continue
                key = (dy // baseline_slop, abs(text_item[1] - anchor_x))
                if best_key is None or key < best_key:
                    best_key = key
                    best_index = index
            if best_index is not None:
                row_of[best_index].append(text_item)

        for index, (cno, ax, ay) in enumerate(c_anchors):
            band_items = row_of.get(index, [])

            circuit = Circuit(
                box=code,
                circuit_no=cno,
                phase="",
                breaker="",
                cable="",
                power_kw="",
                current_a="",
                load_name="",
                note="",
            )
            breaker_text = ""
            breaker_at = None
            cable_at = None
            power_at = None
            chosen: dict[str, tuple] = {}

            def _prefer(field: str, dy: float, dx: float) -> bool:
                key = (dy // baseline_slop, dx)
                previous = chosen.get(field)
                if previous is not None and key >= previous:
                    return False
                chosen[field] = key
                return True

            measure_marks = tuple(_D.get("measure_column_marks") or ())

            def _is_measure_label(text: str) -> bool:
                raw = (text or "").strip()
                if _is_field_label(raw):
                    return True
                folded = re.sub(r"[（）()\s]", "", raw)
                return any(mark and mark in folded for mark in measure_marks)

            def _closer_to_measure_label(x: float, y: float) -> bool:
                """同一行上更靠近表头的电流、功率属于那一列，不是这一回。"""
                own = abs(x - ax)
                for other, ox, oy, _oh in band_items:
                    if abs(oy - y) > baseline_slop or not _is_measure_label(other):
                        continue
                    if abs(ox - x) < own:
                        return True
                return False

            device_line = base_h * float(_CAD_CONFIG.get("row_device_baseline_heights", 2))

            def _beside_thermal(x: float, y: float) -> bool:
                """整定范围和热继电器叠在一起时，是继电器的整定值，不是回路电流。"""
                return any(
                    _row_device_field(other) == "thermal"
                    and max(abs(ox - x), abs(oy - y)) <= device_line
                    for other, ox, oy, _oh in band_items
                )

            for t, x, y, h in band_items:
                dy = abs(y - ay)
                dx = abs(x - ax)
                if RE_PHASE.match(t):
                    if _prefer("phase", dy, dx):
                        circuit.phase = t
                elif RE_POWER.match(t):
                    if not _closer_to_measure_label(x, y) and _prefer("power", dy, dx):
                        circuit.power_kw = t.replace("Pe=", "").strip()
                        power_at = (x, y)
                elif RE_CURRENT.match(t):
                    if _THERMAL_SETTING_RE.match(t.strip()) and _beside_thermal(x, y):
                        pass
                    elif not _closer_to_measure_label(x, y) and _prefer("current", dy, dx):
                        circuit.current_a = t
                elif _looks_like_cable(t) and not _is_wire_kind_note(t):
                    if _prefer("cable", dy, dx):
                        circuit.cable = t
                        cable_at = (x, y)
                        consumed_cable_pts.add((round(x), round(y)))
                elif _text_is_breaker(t):
                    # 已经落在这一行里时，取离回路号更近的开关，不让旁边一列贴基线的总开关抢走。
                    if _prefer("breaker", 0, dx):
                        circuit.breaker = t
                        breaker_text = t
                        breaker_at = (x, y)
                elif (device_field := _row_device_field(t)):
                    # 柜体表上的互感器和这一回隔着几行，不因为行带够宽就挂过来。
                    if dy <= device_line and _prefer(device_field, 0, dx):
                        setattr(circuit, device_field, t)
                if "过载仅报警" in t:
                    circuit.note = "过载仅报警不跳闸"
                elif "内配" in t:
                    circuit.note = (circuit.note + " " + t).strip() if circuit.note else t

            if circuit.thermal:
                thermal_pts = [
                    (ox, oy) for other, ox, oy, _oh in band_items
                    if other.strip() == circuit.thermal
                ]
                settings = [
                    (t.strip(), x, y)
                    for t, x, y, _h in band_items
                    if _THERMAL_SETTING_RE.match(t.strip())
                    and any(max(abs(x - ox), abs(y - oy)) <= device_line for ox, oy in thermal_pts)
                ]
                if settings and thermal_pts:
                    tx, ty = thermal_pts[0]
                    setting = min(settings, key=lambda item: max(abs(item[1] - tx), abs(item[2] - ty)))[0]
                    if setting not in circuit.thermal:
                        circuit.thermal = f"{circuit.thermal} {setting}"

            # 功率栏常常只写数字，单位在表头。取电缆外侧、更贴本行的那个数。
            # 和「数量」「备注」写在同一行的数字是台数，不是千瓦。
            label_gap = base_h * float(_CAD_CONFIG.get("power_label_gap_heights", 12))

            def _beside_field_label(x: float, y: float) -> bool:
                return any(
                    _is_field_label(other)
                    and abs(oy - y) <= baseline_slop
                    and abs(ox - x) <= label_gap
                    for other, ox, oy, _oh in band_items
                )

            if not circuit.power_kw and cable_at is not None and cable_at[0] != ax:
                cable_dx = cable_at[0] - ax
                for t, x, y, h in band_items:
                    raw = t.strip()
                    if not re.fullmatch(r"\d+(?:\.\d+)?", raw):
                        continue
                    if _beside_field_label(x, y) or _closer_to_measure_label(x, y):
                        continue
                    dx = x - ax
                    if dx * cable_dx <= cable_dx * cable_dx:
                        continue
                    if _prefer("power", abs(y - ay), abs(dx)):
                        circuit.power_kw = raw
                        power_at = (x, y)

            # 开关和回路号之间，或贴在进线开关外侧、而出线电缆在另一侧的文字，是开关附注。
            def _remember_note(raw: str) -> None:
                raw = raw.strip()
                if raw and raw not in circuit.note:
                    circuit.note = (circuit.note + " " + raw).strip()

            wrap_gap = base_h * float(_CAD_CONFIG.get("load_wrap_gap_heights", 2))
            pieces: list[tuple[str, float, float]] = []
            for t, x, y, h in band_items:
                if _is_wire_kind_note(t):
                    _remember_note(t)
                    continue
                if _is_spd_callout(t):
                    continue
                if _looks_like_cable(t) or _text_is_breaker(t) or _row_device_field(t):
                    continue
                # 括号没配对的残段先留下，和相邻残段拼回一行再判断。
                if not any("\u4e00" <= ch <= "\u9fa5" for ch in t):
                    continue
                if _is_field_label(t):
                    continue
                leading_code = extract_panel_code(t)
                if leading_code and any(suffix in t for suffix in caption_suffixes):
                    if re.sub(r"\s+", "", t).upper().startswith(leading_code.upper()):
                        continue
                if _is_wire_kind_note(t) or _is_prose_note(t) or _is_bus_laying_note(t):
                    _remember_note(t)
                    continue
                visible = _without_marking_note(t)
                if not visible or "消防标志" in visible:
                    _remember_note(t)
                    continue
                if any(keyword in visible for keyword in system_keywords) or any(keyword in visible for keyword in drawing_words):
                    continue
                if any(token in visible for token in load_reject):
                    continue
                # 「余同」旁边的句子是对多行的安装注记，不是某一回的名称。
                if any(
                    "余同" in other and abs(oy - y) <= base_h and abs(ox - x) <= base_h * 16
                    for other, ox, oy, _oh in band_items
                ):
                    continue
                stripped = visible
                single = len(stripped) == 1 and "\u4e00" <= stripped <= "\u9fff"
                if single and any(
                    len(other.strip()) == 1
                    and "\u4e00" <= other.strip() <= "\u9fff"
                    and other.strip() != stripped
                    and abs(ox - x) <= base_h
                    and 0 < abs(oy - y) <= base_h * 8
                    for other, ox, oy, _oh in band_items
                ):
                    continue
                if stripped in {"宽", "高", "深"} and any(
                    other.strip().lower() in {"x", "×", "*"} and max(abs(ox - x), abs(oy - y)) <= wrap_gap
                    for other, ox, oy, _oh in band_items
                ):
                    continue
                if any(label.startswith(stripped) and stripped != label for label in field_labels):
                    continue
                if breaker_at is not None:
                    low, high = sorted((breaker_at[0], ax))
                    if low < x < high:
                        _remember_note(t)
                        continue
                # 贴在另一只开关上的字是那只开关的附注。本行选中的开关旁边仍可以是负荷名。
                if any(
                    max(abs(x - bx), abs(y - by)) <= breaker_note_gap
                    and (
                        breaker_at is None
                        or max(abs(bx - breaker_at[0]), abs(by - breaker_at[1])) > breaker_note_gap
                    )
                    for bx, by in breaker_points
                ):
                    _remember_note(t)
                    continue
                pieces.append((stripped, x, y))
            # 名称折成两行时，上一段「……（车库」和下一段「道闸用电）」拼回去。
            pending = set(range(len(pieces)))
            groups: list[list[int]] = []
            while pending:
                start = pending.pop()
                stack = [start]
                group = [start]
                while stack:
                    current = stack.pop()
                    _, cx, cy = pieces[current]
                    for other in list(pending):
                        _, ox, oy = pieces[other]
                        if max(abs(cx - ox), abs(cy - oy)) <= wrap_gap:
                            pending.remove(other)
                            stack.append(other)
                            group.append(other)
                groups.append(group)
            load_candidates: list[tuple[str, float, float]] = []
            for group in groups:
                ordered = [pieces[index] for index in sorted(group, key=lambda index: (-pieces[index][2], pieces[index][1]))]
                joined = "".join(part[0] for part in ordered)
                unbalanced = [
                    part for part in ordered
                    if part[0].count("（") + part[0].count("(") != part[0].count("）") + part[0].count(")")
                ]
                if unbalanced and _text_is_branch_load(joined):
                    anchor_piece = min(ordered, key=lambda part: (abs(part[2] - ay) // baseline_slop, abs(part[1] - ax)))
                    load_candidates.append((joined, anchor_piece[1], anchor_piece[2]))
                    continue
                for part in ordered:
                    if _text_is_branch_load(part[0]):
                        load_candidates.append(part)
            # 贴在本行开关上的栏名（如「断路器」）让给更远的名称栏。旁边没有别的名称时仍保留。
            def _on_chosen_breaker(x: float, y: float) -> bool:
                return breaker_at is not None and max(
                    abs(x - breaker_at[0]), abs(y - breaker_at[1])
                ) <= breaker_note_gap

            outside = [item for item in load_candidates if not _on_chosen_breaker(item[1], item[2])]
            pool = outside or load_candidates
            # 名称和功率写在同一行。另一列更贴回路号的「至…」「沿…」是去向说明。
            beside_power = []
            if power_at is not None:
                beside_power = [item for item in pool if abs(item[2] - power_at[1]) <= baseline_slop]
            if beside_power:
                pool = beside_power
            for t, x, y in pool:
                if beside_power:
                    dy = abs(y - power_at[1])
                    dx = abs(x - power_at[0])
                else:
                    dy = abs(y - ay)
                    dx = abs(x - ax)
                if _prefer("load", dy, dx):
                    circuit.load_name = t
            if beside_power:
                for raw, _x, _y in load_candidates:
                    if raw != circuit.load_name and (raw.startswith("至") or raw.startswith("沿")):
                        _remember_note(raw)

            # 紧挨回路号的箱号是配出，左右都可以。已经选出出线电缆时，电缆外侧最近的箱号也是配出。
            if not circuit.load_name:
                near_feed = []
                for t, x, _y, _h in band_items:
                    dx = x - ax
                    if dx == 0 or abs(dx) > bridge_gap:
                        continue
                    fed = _exact_feed_code(t)
                    if fed and fed != code:
                        near_feed.append((abs(dx), t))
                if near_feed:
                    circuit.load_name = min(near_feed)[1].strip()
            if not circuit.load_name and cable_at is not None:
                cable_dx = cable_at[0] - ax
                if cable_dx != 0:
                    beyond = []
                    for t, x, _y, _h in band_items:
                        dx = x - ax
                        if dx * cable_dx <= cable_dx * cable_dx:
                            continue
                        fed = _exact_feed_code(t)
                        if fed and fed != code:
                            beyond.append((abs(dx), t))
                    if beyond:
                        circuit.load_name = min(beyond)[1].strip()

            # 同一行里已经有负荷名时，配出编号和「由某箱引来」仍要留下，不能被负荷列盖掉。
            upstream_markers = tuple((_domain().get("topology") or {}).get("upstream_unnamed_markers") or ())
            extras: list[str] = []
            for item in band_items:
                raw = item[0].strip()
                if not raw or raw == circuit.load_name or raw in circuit.note or raw in extras:
                    continue
                fed = _exact_feed_code(raw)
                if fed and fed != code:
                    extras.append(raw)
                    continue
                if any(marker in raw for marker in upstream_markers) and any(
                    item != code for item in iter_panel_codes(raw)
                ):
                    extras.append(raw)
            if extras:
                circuit.note = (circuit.note + " " + " ".join(extras)).strip()

            score = sum(bool(value) for value in (
                circuit.phase, circuit.breaker, circuit.cable, circuit.power_kw,
                circuit.current_a, circuit.load_name,
            ))
            previous = chosen_rows.get(cno)
            if score and (previous is None or score > previous[0]):
                _bind_observed_fields(circuit, evidence_store, text_sources, [
                    ("circuit.circuit_no", circuit.circuit_no, (ax, ay)),
                    ("circuit.breaker", circuit.breaker, breaker_at),
                    ("circuit.cable", circuit.cable, cable_at),
                    ("circuit.phase", circuit.phase, _point_of(circuit.phase, band_items)),
                    ("circuit.load_name", circuit.load_name, _point_of(circuit.load_name, band_items)),
                ])
                chosen_rows[cno] = (score, circuit, breaker_text)
        for _score, circuit, breaker_text in chosen_rows.values():
            if breaker_text:
                consumed_breaker_texts.add(breaker_text)
            extracted_circuits.append(circuit)

        # 查找该箱体的进线回路与进线总断路器
        incomers = [t for t in panel_texts if any(k in t[0] for k in ["引来", "进线", "引入", "常用电源", "备用电源"])]

        # 寻找箱体内未被出线回路消费的开关电器（位于母线上方/进线侧的主断路器）
        unconsumed_breakers = [
            t for t in panel_texts
            if t[0] not in consumed_breaker_texts
            and _text_is_breaker(t[0])
            and not any(k in t[0].upper() for k in ("SPD", "浪涌", "电涌"))
        ]

        inc_breaker = ""
        inc_cable = ""
        inc_note = ""
        inc_breaker_at = None

        def _closer_to_other_panel(x: float, y: float) -> bool:
            """这只器件离另一台箱的编号更近，就不算这台没有出线的箱。"""
            own = max(abs(x - hx), abs(y - hy))
            for other, group in by_code.items():
                if other == code:
                    continue
                for _oc, _title, ox, oy, _oh in group:
                    if max(abs(x - ox), abs(y - oy)) < own:
                        return True
            return False

        # 没有出线时，窗口很大，总开关按离本箱编号最近取。更靠近另一台箱的留在那边。
        # 有出线的系统图仍取母线上方的总开关：关键词优先，再取更高的 Y。
        if unconsumed_breakers and not c_anchors:
            owned = [item for item in unconsumed_breakers if not _closer_to_other_panel(item[1], item[2])]
            if owned:
                chosen = min(owned, key=lambda t: (
                    max(abs(t[1] - hx), abs(t[2] - hy)),
                    abs(t[2] - hy),
                    abs(t[1] - hx),
                ))
                inc_breaker = chosen[0]
                inc_breaker_at = (chosen[1], chosen[2])
                consumed_breaker_texts.add(inc_breaker)
        elif unconsumed_breakers:
            unconsumed_breakers.sort(key=lambda t: (
                100 if any(k in t[0] for k in ("MCCB", "ATS", "3P", "4P", "NM", "NSX", "C65", "NXB")) else 0,
                t[2]  # Y 坐标较高（进线总开关在上方）
            ), reverse=True)
            inc_breaker = unconsumed_breakers[0][0]
            inc_breaker_at = (unconsumed_breakers[0][1], unconsumed_breakers[0][2])
            consumed_breaker_texts.add(inc_breaker)

        if incomers:
            inc_note = _unique_joined(incomers)
            for t in incomers:
                if _looks_like_cable(t[0]) and not _is_signal_cable(t[0]) and not _is_wire_kind_note(t[0]):
                    inc_cable = t[0]
                    break

        # 进线电缆和相序贴着总开关。出线行上的「进线详见…」不能把那一行的电缆和相序借过来。
        anchor_at = inc_breaker_at
        if anchor_at is None and incomers:
            notes = incomers
            if not c_anchors:
                notes = [item for item in incomers if not _closer_to_other_panel(item[1], item[2])]
            if notes:
                anchor_at = (notes[0][1], notes[0][2])
        # 没有总开关时，「由某箱引来」若落在某一出线行上，那一行的电缆和相序仍属于出线。
        note_on_branch = (
            inc_breaker_at is None
            and anchor_at is not None
            and any(abs(ay - anchor_at[1]) <= baseline_slop for _cno, _ax, ay in c_anchors)
        )
        def _closer_to_branch(y: float) -> bool:
            """文字更靠近某一出线行，而不是总开关这一行。"""
            if anchor_at is None:
                return False
            to_breaker = abs(y - anchor_at[1])
            to_branch = min((abs(y - ay) for _cno, _ax, ay in c_anchors), default=to_breaker)
            return to_branch <= baseline_slop and to_branch < to_breaker

        if not inc_cable and anchor_at is not None and not note_on_branch:
            near_cables = [
                t for t in panel_texts
                if _looks_like_cable(t[0])
                and not _is_signal_cable(t[0])
                and not _is_wire_kind_note(t[0])
                and not t[0].lstrip().startswith(("+", "＋"))
                and (round(t[1]), round(t[2])) not in consumed_cable_pts
                and abs(t[2] - anchor_at[1]) <= row_y_tolerance
                and not _closer_to_branch(t[2])
            ]
            if near_cables:
                inc_cable = min(near_cables, key=lambda t: abs(t[1] - anchor_at[0]))[0]

        if incomers or inc_breaker:
            # 进线相序不得凭空填：只取总开关同一行上的相序，没标就留空。
            incoming_phase = ""
            incoming_note = inc_note or ("进线总开关" if inc_breaker else "")
            if anchor_at is not None and not note_on_branch:
                for t in panel_texts:
                    if not _is_wire_kind_note(t[0]):
                        continue
                    if abs(t[2] - anchor_at[1]) > row_y_tolerance or _closer_to_branch(t[2]):
                        continue
                    if t[0] not in incoming_note:
                        incoming_note = (incoming_note + " " + t[0]).strip()
                near_phases = [
                    t for t in panel_texts
                    if RE_PHASE.match(str(t[0]).strip())
                    and abs(t[2] - anchor_at[1]) <= baseline_slop
                    and not _closer_to_branch(t[2])
                ]
                if near_phases:
                    incoming_phase = min(near_phases, key=lambda t: abs(t[1] - anchor_at[0]))[0].strip()
            assumption = str(_CAD_CONFIG.get("incoming_phase_default") or "")
            if not incoming_phase and assumption:
                incoming_phase = assumption
                note_extra = str(_CAD_CONFIG.get("incoming_phase_assumption_note") or "")
                if note_extra:
                    incoming_note = (incoming_note + "；" + note_extra).strip("；")
            incoming = Circuit(
                box=code,
                circuit_no="进线",
                phase=incoming_phase,
                breaker=inc_breaker,
                cable=inc_cable,
                load_name="进线",
                note=incoming_note,
            )
            _bind_observed_fields(incoming, evidence_store, text_sources, [
                ("circuit.circuit_no", incoming.circuit_no, _point_of("进线", panel_texts)),
                ("circuit.breaker", incoming.breaker, inc_breaker_at),
                ("circuit.cable", incoming.cable, _point_of(incoming.cable, panel_texts)),
                ("circuit.phase", incoming.phase, _point_of(incoming.phase, panel_texts)),
                ("circuit.load_name", incoming.load_name, _point_of("进线", panel_texts)),
            ])
            extracted_circuits.append(incoming)

        # 和总开关同一行、同一列、又没被出线用掉的，才是备用开关或隔离刀闸。
        # 同一高度上隔着几列的微型断路器是出线，不收成进线侧器件。
        incomer_column = base_h * float(_CAD_CONFIG.get("circuit_column_x_heights", 4))
        for extra_b in unconsumed_breakers:
            if extra_b[0] in consumed_breaker_texts:
                continue
            if inc_breaker_at is None or abs(extra_b[2] - inc_breaker_at[1]) > row_y_tolerance:
                continue
            if abs(extra_b[1] - inc_breaker_at[0]) > incomer_column:
                continue
            if _closer_to_other_panel(extra_b[1], extra_b[2]):
                continue
            consumed_breaker_texts.add(extra_b[0])
            extracted_devices.append(ExtraDevice(
                name="进线侧控制保护开关",
                spec=extra_b[0],
                unit="台",
                quantity=1.0,
                used_in=f"{code} 进线侧",
            ))

        # 提取浪涌保护器等非回路器件。
        # 防双计：已被某回路 breaker 实际消费的文本不再收为 ExtraDevice
        # （该文本只出现在回路带内时保留 breaker 侧）。
        # 规格用图纸真实文本，不硬编码。
        spd_specs = sorted({
            spec for spec in spd_by_code.get(code, [])
            if spec and spec not in consumed_breaker_texts
        })
        if spd_specs:
            extracted_devices.append(ExtraDevice(
                name="浪涌保护器",
                spec="；".join(spd_specs) if spd_specs else "(规格未标注)",
                unit="套",
                quantity=1.0,
                used_in=f"{code} 进线侧",
            ))

    aggregate_min = int(_CAD_CONFIG.get("quantity_warning_aggregate_min", 8))
    if unrecognized_qty:
        if len(unrecognized_qty) >= aggregate_min:
            sample = "、".join(unrecognized_qty[:8])
            extracted_uncertainties.append(Uncertainty.from_text(
                f"全图台数：箱体台数未标明共 {len(unrecognized_qty)} 台（{sample}），{UNCONFIRMED_QTY_WARNING}"
            ))
        else:
            for code in unrecognized_qty:
                extracted_uncertainties.append(Uncertainty.from_text(
                    f"{code}：{UNCONFIRMED_QTY_WARNING}"))

    # 若未找到任何有效配电箱，如实记录存疑项，杜绝假装成功
    if not extracted_boxes:
        extracted_uncertainties.append(Uncertainty.from_text(
            "CAD模型空间未检索到有效配电箱系统图标头，请核查图层是否关闭或包含非标标头"))

    # CAD 矢量路径不提取技术要求：不编造，如实记一条待核对交人工核对图纸说明
    extracted_uncertainties.append(Uncertainty.from_text(
        "CAD矢量解析：技术要求未提取，请人工核对图纸说明"))

    return RawExtraction(
        boxes=extracted_boxes,
        circuits=extracted_circuits,
        extra_devices=extracted_devices,
        requirements=extracted_reqs,
        uncertainties=extracted_uncertainties,
        evidence_store=evidence_store,
    )
