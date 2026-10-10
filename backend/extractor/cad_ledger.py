# -*- coding: utf-8 -*-
"""CAD 读取账本。

把「转换成功」和「读全」分开。退出码 0 且输出非空只说明转换步骤返回了文件。
覆盖率保持为空：没有盲测之前，不写 0，也不写 100%。
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from typing import Any

STATUS_SUPPORTED = "supported"
STATUS_DEGRADED = "degraded"
STATUS_INSUFFICIENT = "insufficient"
INSUFFICIENT_RELEASE_REASON = "读取关键条件不足，已确认子集不得当成项目总量"

# DXF $INSUNITS。0 表示未设置，不能拿来量长度。
_UNIT_NAMES = {
    1: "in",
    2: "ft",
    3: "mi",
    4: "mm",
    5: "cm",
    6: "m",
    7: "km",
    8: "uin",
    9: "mil",
    10: "yd",
    11: "angstrom",
    12: "nm",
    13: "um",
    14: "dm",
    15: "dam",
    16: "hm",
    17: "Gm",
    18: "au",
    19: "ly",
    20: "pc",
}

_SYSTEM_BLOCKS = {"*Model_Space", "*Paper_Space", "*Paper_Space0"}
_CONVERSIONS: dict[str, dict[str, Any]] = {}


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remember_conversion(path: str, record: dict[str, Any]) -> None:
    if path:
        _CONVERSIONS[os.path.abspath(path)] = record


def conversion_for(path: str | None) -> dict[str, Any] | None:
    if not path:
        return None
    return _CONVERSIONS.get(os.path.abspath(path))


def classify_stderr(stderr: str) -> tuple[int, int, str]:
    warnings = 0
    errors = 0
    lines = (stderr or "").splitlines()
    for line in lines:
        low = line.lower()
        if "error" in low or "错误" in line:
            errors += 1
        elif "warning" in low or "警告" in line:
            warnings += 1
    excerpt = "\n".join(lines[:30])[:2000]
    return warnings, errors, excerpt


def conversion_accepted(exit_code: int | None, output_nonempty: bool) -> bool:
    """转换步骤的通过条件。它不表示对象、依赖和单位已经读全。"""
    return exit_code == 0 and output_nonempty


def entity_source(entity: Any, *, owner: str, insert_path: str, layout: str = "Model") -> dict[str, str]:
    """来源身份。只用块内子实体 handle 会把不同插入实例并成一个。"""
    handle = ""
    kind = ""
    try:
        handle = str(entity.dxf.handle or "")
    except Exception:
        handle = ""
    try:
        kind = str(entity.dxftype())
    except Exception:
        kind = ""
    return {
        "file_sha256": "",
        "handle": handle,
        "owner": owner,
        "insert_path": insert_path,
        "dxftype": kind,
        "layout": layout,
    }


def source_key(source: dict[str, Any] | None) -> str:
    src = source or {}
    return "|".join([
        str(src.get("file_sha256") or ""),
        str(src.get("insert_path") or ""),
        str(src.get("handle") or ""),
        str(src.get("owner") or ""),
    ])


def stamp_records(records: list[dict[str, Any]], source_path: str) -> str:
    """把原文件哈希写进每条文字。哈希对应该次交付的文件，不对该次转换出的 DXF。"""
    try:
        file_hash = sha256_file(source_path)
    except OSError:
        file_hash = ""
    for record in records:
        src = dict(record.get("source") or {})
        src["file_sha256"] = file_hash
        src.setdefault("handle", "")
        src.setdefault("owner", "modelspace")
        src.setdefault("insert_path", "")
        src.setdefault("layout", "Model")
        src.setdefault("dxftype", str(record.get("type") or ""))
        record["source"] = src
    return file_hash


def join_insert_path(instance_path: str, inner_path: str) -> str:
    if instance_path and inner_path:
        return f"{instance_path}/{inner_path}"
    return instance_path or inner_path


def classify_read_status(facts: dict[str, Any]) -> tuple[str, list[str], bool]:
    """返回读取状态、原因、是否允许把几何长度当工程量。"""
    reasons: list[str] = []
    converter = str(facts.get("converter") or "")
    if converter == "svg_fallback":
        reasons.append("只有视觉降级，没有原生对象账本")
        return STATUS_INSUFFICIENT, reasons, False
    if converter == "libredwg" and not facts.get("accepted"):
        reasons.append("DWG 转换未同时满足退出码 0 与非空输出")
        return STATUS_INSUFFICIENT, reasons, False
    if int(facts.get("entity_total") or 0) <= 0:
        reasons.append("模型空间没有可读实体")
        return STATUS_INSUFFICIENT, reasons, False

    status = STATUS_SUPPORTED
    if facts.get("log_missing"):
        status = STATUS_DEGRADED
        reasons.append("本次没有转换日志，不能把缓存文件存在当成无警告读全")
    if int(facts.get("warning_lines") or 0) or int(facts.get("error_lines") or 0):
        status = STATUS_DEGRADED
        reasons.append("转换日志含警告或错误，退出码 0 不代表读全")
    if int(facts.get("proxy_count") or 0):
        status = STATUS_DEGRADED
        reasons.append(f"代理对象 {int(facts['proxy_count'])} 个，看见轮廓不等于读到专业属性")
    if int(facts.get("xref_count") or 0):
        status = STATUS_DEGRADED
        reasons.append(f"外参 {int(facts['xref_count'])} 个，主文件打开不等于依赖齐全")
    if not facts.get("units_known"):
        status = STATUS_DEGRADED
        reasons.append("图纸单位未设置，禁止把几何长度当作工程量")
    if facts.get("recovered"):
        status = STATUS_DEGRADED
        reasons.append("DXF 以恢复模式读取")
    if status == STATUS_SUPPORTED:
        reasons.append("普通实体已盘点。这不表示专业对象、字体和外部依赖已经读全")
    measurement_allowed = bool(
        facts.get("units_known")
        and status == STATUS_SUPPORTED
        and not int(facts.get("proxy_count") or 0)
        and not int(facts.get("xref_count") or 0)
    )
    return status, reasons, measurement_allowed


def build_read_report(
    source_path: str,
    *,
    doc: Any = None,
    converter: str,
    dxf_path: str | None = None,
    recovered: bool = False,
) -> dict[str, Any]:
    conversion = conversion_for(source_path) or conversion_for(dxf_path)
    if converter == "native_dxf":
        conversion = {
            "tool": "ezdxf",
            "exit_code": None,
            "output_nonempty": True,
            "accepted": True,
            "warning_lines": 0,
            "error_lines": 0,
            "stderr_excerpt": "",
            "log_missing": False,
            "note": "原文件已是 DXF，未经过 DWG 转换",
        }
    elif converter == "svg_fallback":
        conversion = conversion or {
            "tool": "dwg2SVG",
            "exit_code": None,
            "output_nonempty": False,
            "accepted": False,
            "warning_lines": 0,
            "error_lines": 0,
            "stderr_excerpt": "",
            "log_missing": False,
            "note": "DWG 未能转成可读取的 DXF，只保留视觉降级",
        }
    elif conversion is None:
        conversion = {
            "tool": "libredwg",
            "exit_code": None,
            "output_nonempty": bool(dxf_path and os.path.exists(dxf_path)),
            "accepted": bool(dxf_path and os.path.exists(dxf_path) and os.path.getsize(dxf_path) > 0),
            "warning_lines": 0,
            "error_lines": 0,
            "stderr_excerpt": "",
            "log_missing": True,
            "note": "使用已有 DXF 缓存，本次未重跑转换",
        }

    try:
        file_hash = sha256_file(source_path)
        file_size = os.path.getsize(source_path)
    except OSError:
        file_hash = ""
        file_size = 0

    entity_counts: dict[str, int] = {}
    layouts: list[dict[str, Any]] = []
    xrefs: list[dict[str, str]] = []
    proxy_count = 0
    named_blocks = 0
    anonymous_blocks = 0
    dxf_version = ""
    insunits = 0
    if doc is not None:
        try:
            insunits = int(doc.header.get("$INSUNITS", 0) or 0)
        except (TypeError, ValueError):
            insunits = 0
        dxf_version = str(getattr(doc, "dxfversion", "") or "")
        counts: Counter[str] = Counter()
        try:
            for entity in doc.modelspace():
                try:
                    counts[entity.dxftype()] += 1
                except Exception:
                    counts["UNREADABLE"] += 1
        except Exception:
            counts["UNREADABLE"] += 1
        entity_counts = dict(counts)
        try:
            for layout in doc.layouts:
                entity_total = 0
                try:
                    entities = list(layout)
                except Exception:
                    entities = []
                for entity in entities:
                    entity_total += 1
                    try:
                        if entity.dxftype() == "ACAD_PROXY_ENTITY":
                            proxy_count += 1
                    except Exception:
                        continue
                layouts.append({"name": layout.name, "entities": entity_total})
        except Exception:
            layouts.append({"name": "Model", "entities": sum(entity_counts.values())})
        try:
            for block in doc.blocks:
                name = str(getattr(block, "name", "") or "")
                if name in _SYSTEM_BLOCKS or name.startswith("*Paper_Space"):
                    continue
                if name.startswith("*"):
                    anonymous_blocks += 1
                else:
                    named_blocks += 1
                try:
                    if block.block.is_xref:
                        xrefs.append({
                            "name": name,
                            "path": str(getattr(block.block.dxf, "xref_path", "") or ""),
                        })
                except Exception:
                    continue
        except Exception:
            pass

    model_entities = sum(entity_counts.values())
    facts = {
        "converter": converter,
        "accepted": bool(conversion.get("accepted")),
        "warning_lines": int(conversion.get("warning_lines") or 0),
        "error_lines": int(conversion.get("error_lines") or 0),
        "log_missing": bool(conversion.get("log_missing")),
        "entity_total": model_entities,
        "proxy_count": proxy_count,
        "xref_count": len(xrefs),
        "units_known": insunits in _UNIT_NAMES,
        "recovered": recovered,
    }
    status, reasons, measurement_allowed = classify_read_status(facts)
    return {
        "file_sha256": file_hash,
        "file_name": os.path.basename(source_path),
        "file_size": file_size,
        "extension": os.path.splitext(source_path)[1].lower(),
        "dxf_version": dxf_version,
        "converter": converter,
        "conversion": conversion,
        "status": status,
        "reasons": reasons,
        "insunits": insunits,
        "units": _UNIT_NAMES.get(insunits, ""),
        "units_known": insunits in _UNIT_NAMES,
        "measurement_allowed": measurement_allowed,
        "layouts": layouts,
        "entity_counts": entity_counts,
        "model_entities": model_entities,
        "named_blocks": named_blocks,
        "anonymous_blocks": anonymous_blocks,
        "insert_count": int(entity_counts.get("INSERT", 0)),
        "xrefs": xrefs,
        "proxy_count": proxy_count,
        "recovered": recovered,
        "coverage_rate": None,
        "clues": [
            "块定义数不是设备数",
            "同一 handle 必须连同插入路径一起看，不同插入是不同实例",
            "图层名和块名只作为线索",
            "柜内线、铜排和辅材没有装配依据时不补数量",
            "覆盖率保持为空，未做盲测前不写准确率",
        ],
    }


def apply_read_gate(release: Any, report: dict[str, Any] | None) -> Any:
    """读取关键条件不足时，已抽出的候选不能当成项目总量。"""
    if release is None or not report or report.get("status") != STATUS_INSUFFICIENT:
        return release
    release.project_total_released = False
    reasons = list(getattr(release, "reasons", []) or [])
    if INSUFFICIENT_RELEASE_REASON not in reasons:
        reasons.append(INSUFFICIENT_RELEASE_REASON)
    release.reasons = reasons
    return release


def write_read_report(out_pdf_path: str, report: dict[str, Any]) -> str:
    path = out_pdf_path + ".read.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    return path
