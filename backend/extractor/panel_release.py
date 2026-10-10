# -*- coding: utf-8 -*-
"""配电箱候选 BOM 的放行规则。

数量和规格分开。图面没有写明的台数只保留单箱候选。
备用开关要区分已经安装和仅预留。极数不把一件器件乘成多件。
柜内线和铜排没有装配尺寸时不补数量。
"""
from __future__ import annotations

import re
from typing import Any

UNCONFIRMED_QTY_NOTE = "图面未标明台数，按单箱候选保留，项目总量未放行"
UNCONFIRMED_QTY_WARNING = "图面未标明台数，已保留单箱候选，项目总量未放行"
CANDIDATE_FLAG = "单箱候选，项目总量未放行"
INSTALLED_SPARE_NOTE = "已安装备用，按一件器件计入，极数不另乘"

_MATERIAL_TERMS = ("铜排", "母排", "柜内线", "板厚", "导轨", "接线端子")
_MEASURE = re.compile(r"\d+(?:\.\d+)?\s*(?:mm2|mm²|mm|cm|m)\b", re.I)
_SPARE_LOADS = {"备用", "备用回路", "备用开关"}
_RESERVED_LOADS = {"预留", "预留回路", "备用预留"}
# 「未装」后面紧跟「饰」是未装饰，不是未安装。
_NOT_INSTALLED = re.compile(r"未安装|不安装|仅预留|未装(?!饰)")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _not_installed(text: str) -> bool:
    return _NOT_INSTALLED.search(_compact(text)) is not None


def spare_disposition(circuit: Any) -> str:
    """active / installed_spare / reserved。

    只认回路名称本身是备用或预留。备用照明、备用电源不在这里面。
    备注里顺带写到备用开关，不改变已经写明的负荷。
    备用并且写了断路器或接触器规格，按已安装的一件计入。
    没有器件规格，或注明未安装、仅预留，不计入采购数量。
    """
    load = _compact(str(getattr(circuit, "load_name", "") or ""))
    note = _compact(str(getattr(circuit, "note", "") or ""))
    has_device = bool(
        str(getattr(circuit, "breaker", "") or "").strip()
        or str(getattr(circuit, "contactor", "") or "").strip()
    )
    named_spare = load in _SPARE_LOADS or (not load and note in _SPARE_LOADS)
    named_reserved = load in _RESERVED_LOADS or (not load and note in _RESERVED_LOADS)
    not_installed = _not_installed(note) or _not_installed(load)
    if not named_spare and not named_reserved and not not_installed:
        return "active"
    if not_installed or (named_reserved and not has_device) or (named_spare and not has_device):
        return "reserved"
    if has_device:
        return "installed_spare"
    return "reserved"


def box_multiplier(box: Any) -> tuple[int, str]:
    """返回 (参与汇总的台数, 候选标记)。未确认台数时按单箱候选，不放大成项目总量。"""
    confirmed = bool(getattr(box, "quantity_confirmed", True))
    try:
        quantity = int(float(getattr(box, "quantity", 1) or 1))
    except (TypeError, ValueError):
        quantity = 1
        confirmed = False
    if quantity <= 0 or not confirmed:
        return 1, CANDIDATE_FLAG
    return quantity, ""


def release_lines(result: Any) -> list[dict[str, Any]]:
    """导出前要单独看见的三条放行结论。没有触发的规则不占一行。"""
    lines: list[dict[str, Any]] = []
    release = getattr(result, "bom_release", None)
    if release is not None and not release.project_total_released:
        lines.append({
            "kind": "quantity",
            "ok": False,
            "title": "台数未放行",
            "note": "；".join(release.reasons) or "单箱候选不能当成项目总量",
        })
    uncertainties = list(getattr(result, "uncertainties", []) or [])
    spare = [item.text for item in uncertainties if "不计入采购数量" in item.text]
    if spare:
        lines.append({
            "kind": "spare",
            "ok": False,
            "title": "备用未计入采购",
            "note": "；".join(spare),
        })
    materials = [
        item.text for item in uncertainties
        if "不补估算数量" in item.text or "不把这段文字换算成采购数量" in item.text
    ]
    if materials:
        lines.append({
            "kind": "material",
            "ok": False,
            "title": "柜内材料待核",
            "note": "；".join(materials),
        })
    return lines


def material_gap_lines(raw: Any) -> list[str]:
    """图面提到柜内材料但给不出装配尺寸时，留待核，不生成数量。"""
    parts: list[str] = []
    for box in getattr(raw, "boxes", []) or []:
        parts.append(str(getattr(box, "note", "") or ""))
    for circuit in getattr(raw, "circuits", []) or []:
        parts.append(" ".join([
            str(getattr(circuit, "note", "") or ""),
            str(getattr(circuit, "cable", "") or ""),
            str(getattr(circuit, "load_name", "") or ""),
        ]))
    for requirement in getattr(raw, "requirements", []) or []:
        parts.append(str(getattr(requirement, "content", "") or ""))
        parts.append(str(getattr(requirement, "item", "") or ""))
    for device in getattr(raw, "extra_devices", []) or []:
        parts.append(" ".join([
            str(getattr(device, "name", "") or ""),
            str(getattr(device, "spec", "") or ""),
            str(getattr(device, "note", "") or ""),
        ]))
    text = "\n".join(parts)
    hits = [term for term in _MATERIAL_TERMS if term in text]
    if not hits:
        return []
    names = "、".join(hits)
    if _MEASURE.search(text):
        return [f"【提示】{names}：图面有相关文字，仍缺可复核的装配路径，不把这段文字换算成采购数量"]
    return [f"【提示】{names}：缺装配尺寸，保持待核，不补估算数量"]
