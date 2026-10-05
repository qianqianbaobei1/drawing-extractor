# -*- coding: utf-8 -*-
"""从图纸推断项目信息（工程名称、项目编号、建设单位、设计单位）。

背景：上传图纸后要自动建一个项目，项目名不应该来自文件名那种临时写法，
而应该来自图纸本身——国内施工图的标题栏/图签里一定有「工程名称」。

三个来源，按可信度排序，任一命中即用，绝不拼凑：
1. 视觉模型按契约返回的 project_info（它能读图签）；
2. CAD 原生矢量文字（DWG/DXF 的图签文字，最干净的确定性来源）；
3. PDF 内嵌文字层（大部分图纸文字已转曲，常常为空）。

三者都没有时，如实回报 found=False；调用方决定用什么兜底，
本模块不编造工程名称，也不把文件名当工程名称返回。

关键词表在 config/domain.json 的 project_inference 段，现场可加设计院的特殊叫法。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .config import domain

_CFG = (domain().get("project_inference") or {})

# 字段 → 关键词（命中「关键词[：: ]值」即取该值）
FIELD_KEYWORDS: dict[str, list[str]] = _CFG.get("field_keywords") or {
    "name": ["工程名称", "项目名称", "工程名", "项目名"],
    "code": ["工程编号", "项目编号", "工程号", "项目编码", "项目号"],
    "client": ["建设单位", "业主单位", "发包人", "建设单位名称"],
    "designer": ["设计单位", "设计院", "设计单位名称"],
    "location": ["建设地点", "工程地点", "项目地点"],
}
MIN_LEN = int(_CFG.get("min_name_length") or 4)
MAX_LEN = int(_CFG.get("max_name_length") or 80)
# 值后面紧跟这些词说明抓到的是下一个字段的标签，属于误抓
_STOP_WORDS = tuple(_CFG.get("value_stop_words") or
                    ("建设", "设计", "工程编号", "项目编号", "图号", "日期", "比例", "签字", "审核"))
_SEP = r"[:：\s]"

# 值的截断点 = 后面那个字段的标签。不能拿裸词去切：
# 否则“设计院”三个字会被自己的停止词“设计”切掉，值直接变空。
# 规则：标签不在值开头，且后面紧跟着分隔符（：/空格）才算新字段开始。
_LABEL_RE = re.compile(
    r"(?<=.)(?:" + "|".join(re.escape(word) for word in
                             sorted({kw for kws in FIELD_KEYWORDS.values() for kw in kws}
                                    | set(_STOP_WORDS), key=len, reverse=True)) + r")\s*[:：]"
)


def _clean(value: str) -> str:
    value = re.sub(r"[\s\u3000]+", "", str(value or ""))
    value = value.strip("：:、,，。;；-—|/")
    return value


def _usable(value: str) -> bool:
    return bool(value) and MIN_LEN <= len(value) <= MAX_LEN


def _match_fields(text: str) -> list[tuple[str, str]]:
    """从一行文字里找出所有「关键词 + 值」。图签常把多个字段挤在一行，必须全拿。"""
    found_fields: list[tuple[str, str]] = []
    for field, keywords in FIELD_KEYWORDS.items():
        for keyword in keywords:
            pattern = re.compile(rf"{re.escape(keyword)}{_SEP}*(.+)$")
            matched = pattern.search(text)
            if not matched:
                continue
            value = _clean(matched.group(1))
            if not value:
                continue
            # 值里如果又跟上了另一个字段标签（图签常把两个字段挤在一行），截断到标签前
            next_label = _LABEL_RE.search(value)
            if next_label:
                value = _clean(value[:next_label.start()])
            if _usable(value):
                found_fields.append((field, value))
                break
    return found_fields


def infer_from_texts(texts: Iterable[Any]) -> dict:
    """从原生文字行推断项目信息。返回 {found, name, code, client, designer, location, evidence}。"""
    result: dict[str, Any] = {"found": False, "name": "", "code": "", "client": "",
                              "designer": "", "location": "", "evidence": []}
    for item in texts or []:
        text = (item.get("text") if isinstance(item, dict) else item) or ""
        text = str(text)
        if not text.strip():
            continue
        for field, value in _match_fields(text):
            if result[field] or value in result["evidence"]:
                continue
            result[field] = value
            result["evidence"].append(f"[{field}] {value}")

    # 只要拿到任意一项就算找到；但「项目名」才是能不能自动建项目的关键
    result["found"] = any(result[key] for key in ("name", "code", "client", "designer"))
    return result


def merge_info(*sources: dict | None) -> dict:
    """把多个来源合并：先到先得（调用方按可信度从高到低传参）。"""
    merged: dict[str, Any] = {"found": False, "name": "", "code": "", "client": "",
                              "designer": "", "location": "", "evidence": [],
                              "source": ""}
    for source in sources:
        if not source:
            continue
        for key in ("name", "code", "client", "designer", "location"):
            if not merged[key] and source.get(key):
                merged[key] = source[key]
                if not merged["source"]:
                    merged["source"] = source.get("source") or ""
        for item in source.get("evidence") or []:
            if item not in merged["evidence"]:
                merged["evidence"].append(item)
    merged["found"] = any(merged[key] for key in ("name", "code", "client", "designer"))
    return merged


def infer_from_filename(filename: str) -> str:
    """从文件名推候选项目名。仅供 UI 预填，不作为「图上工程名称」使用。"""
    stem = re.sub(r"\.(pdf|dwg|dxf)$", "", str(filename or ""), flags=re.I)
    for pattern in (_CFG.get("filename_noise_regexes") or []):
        stem = re.sub(pattern, "", stem, flags=re.I)
    stem = _clean(stem)
    return stem if _usable(stem) else ""


def inferred_project_name(info: dict, filename: str) -> tuple[str, str]:
    """决定自动建项目时用哪个名字。返回 (名称, 来源标记)。

    来源标记直接进项目表，前端据此提示“待核对”：
    drawing = 来自图纸图签；filename = 来自文件名；none = 两者都没有。
    """
    if info and info.get("name"):
        return str(info["name"]), "drawing"
    candidate = infer_from_filename(filename)
    if candidate:
        return candidate, "filename"
    return "", "none"
