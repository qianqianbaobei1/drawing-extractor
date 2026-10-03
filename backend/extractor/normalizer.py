# -*- coding: utf-8 -*-
"""结构化电气参数清洗器 (Structural Normalizer).

原则：Normalizer Before Validator（先结构化，后进规则门禁）。
业务规则严禁直接处理带有非标前缀、脱扣曲线、敷设方式等混杂的原始字符串。
所有图纸原始文本必须先提纯为结构化参数对象，清洗失败安全降级为 REVIEW，绝不抛出异常。
"""

from __future__ import annotations
from math import isfinite
import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from .catalog import BRAND_PATTERNS, identify_brand


class StructuredCable(BaseModel):
    """结构化电缆导线参数对象"""
    raw_text: str = Field(..., description="原始电缆规格文字")
    family: str = Field("", description="线缆型号系列, 如 WDZ-BYJ, YJV, BV")
    core_count: Optional[int] = Field(None, description="主相导体线芯数, 如 1, 3, 4, 5")
    section_mm2: Optional[float] = Field(None, description="主相导线截面 (mm²), 如 2.5, 4, 16")
    pe_section_mm2: Optional[float] = Field(None, description="PE保护地线/中性线截面 (mm²)")
    laying_method: str = Field("", description="敷设方式及穿管代号, 如 SC20/CC/FC")


class StructuredBreaker(BaseModel):
    """结构化断路器/保护电器参数对象"""
    raw_text: str = Field(..., description="原始断路器规格文字")
    manufacturer: str = Field("通用/国标", description="品牌厂商, 如 施耐德/正泰/ABB")
    series: str = Field("", description="产品系列, 如 iC65N, NM1, NXB-63")
    curve: Optional[str] = Field(None, description="脱扣特性曲线: B, C, D")
    rated_current: Optional[float] = Field(None, description="额定电流 (A), 如 16, 63, 100")
    poles: Optional[str] = Field(None, description="极数, 如 1P, 2P, 3P, 4P, 1P+N, 3P+N")
    breaking_capacity: Optional[str] = Field(None, description="分断能力, 如 6kA, 10kA")
    leakage_ma: Optional[int] = Field(None, description="漏电动作电流 (mA), 如 30, 100, 300")


# 常见电缆型号系列识别模式
# 核心结构: [前缀阻燃耐火代号-]型号[铠装/软线代号]
# 如: WDZ-BYJ, WDZN-BYJ, WDZA-YJV, WDZ-YJV22, ZR-YJV, NH-YJV, BV, BVR, YJV22
CABLE_FAMILY_PATTERNS = [
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?BYJ[A-Z]*)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?(?:YJV|YJY)(?:22|23|32)?)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?VV(?:22|23|32)?)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?BVR)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?BV)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
    re.compile(r"\b((?:WDZ[A-Z]*|ZR[A-Z]*|NH[A-Z]*|FS)?-?RVVP?)(?=[-_\s\d*(×x]|$)", re.IGNORECASE),
]

# 常见穿管敷设方式识别模式 (如 -SC20-CC, /MR/CT, WC/FC)
LAYING_METHOD_PATTERN = re.compile(
    r"(?:-|/|\s|^)((?:SC|PC|KBG|JDG|CT|MR|PR|CP|RC|PVC)\d{0,3}"
    r"(?:[/-](?:CC|WC|FC|WS|CE|ACC|SR|MR|CT|WE))*)\b",
    re.IGNORECASE
)


def parse_cable(raw: str) -> StructuredCable:
    """安全解析电缆导线文本，提纯出线芯数、截面与敷设方式。"""
    if not raw or not isinstance(raw, str):
        return StructuredCable(raw_text=raw or "")
    
    s = raw.strip()
    family = ""
    for pat in CABLE_FAMILY_PATTERNS:
        m = pat.search(s)
        if m:
            family = m.group(1).upper()
            break

    # 提取穿管与敷设方式
    laying = ""
    m_lay = LAYING_METHOD_PATTERN.search(s)
    if m_lay:
        laying = m_lay.group(1).upper()

    core_count: Optional[int] = None
    section_mm2: Optional[float] = None
    pe_section_mm2: Optional[float] = None

    # 匹配规格部分: 如 3x2.5+E2.5, 3*16+2*10, 4x35+1x16, 5x6, 3(1x2.5), BV-2.5
    # 1. 复杂复合线型: 如 4x35+1x16, 3*16+2*10, 3x2.5+E2.5
    m_dual = re.search(
        r"(\d+)\s*(?:[xX*×])\s*([1-9]\d{0,2}(?:\.\d+)?)\s*\+\s*(?:E|e|(\d+)\s*(?:[xX*×]))?\s*([1-9]\d{0,2}(?:\.\d+)?)",
        s
    )
    if m_dual:
        try:
            core_count = int(m_dual.group(1))
            section_mm2 = float(m_dual.group(2))
            pe_section_mm2 = float(m_dual.group(4))
        except (ValueError, TypeError):
            pass

    # 2. 单组芯线: 如 3x2.5, 5*16, 3(1x2.5)
    if section_mm2 is None:
        m_single = re.search(r"(\d+)\s*(?:[xX*×])\s*([1-9]\d{0,2}(?:\.\d+)?)", s)
        if m_single:
            try:
                core_count = int(m_single.group(1))
                section_mm2 = float(m_single.group(2))
            except (ValueError, TypeError):
                pass

    # 3. 单导线截面: 如 BV-2.5, 2.5mm2
    if section_mm2 is None:
        m_simple = re.search(r"(?:-|\b)([1-9]\d{0,2}(?:\.\d+)?)\s*(?:mm[²2]|平方)?\b", s)
        if m_simple:
            try:
                section_mm2 = float(m_simple.group(1))
                core_count = 1
            except (ValueError, TypeError):
                pass

    return StructuredCable(
        raw_text=raw,
        family=family,
        core_count=core_count,
        section_mm2=section_mm2,
        pe_section_mm2=pe_section_mm2,
        laying_method=laying,
    )


def parse_breaker(raw: str) -> StructuredBreaker:
    """安全解析断路器/保护电器规格，提纯品牌、系列、脱扣曲线、电流与极数。"""
    if not raw or not isinstance(raw, str):
        return StructuredBreaker(raw_text=raw or "")
    
    s = raw.strip()
    brand = identify_brand(s)

    # 1. 提取极数: 1P, 2P, 3P, 4P, 1P+N, 3P+N, 3PH
    poles: Optional[str] = None
    m_p = re.search(r"(?:/|\s|^|-)([1-4]P(?:\+N)?|[1-4]PN|3PH)(?:/|\s|$)", s, re.IGNORECASE)
    if m_p:
        p_str = m_p.group(1).upper()
        if p_str == "1PN":
            poles = "1P+N"
        elif p_str == "3PN":
            poles = "3P+N"
        else:
            poles = p_str
    elif "3300" in s or "3308" in s:
        poles = "3P"
    elif "4300" in s or "4308" in s:
        poles = "4P"

    # 2. 提取脱扣曲线特性: B, C, D (如 C16, C16A, C16/1P, C16A/1P, D32, D32A/3P, B10, /C63/3P)
    curve: Optional[str] = None
    m_curve_match = re.search(r"(?:/|-|\b)([CDB])\s*([1-9]\d{0,3}(?:\.\d+)?)\s*A?(?:/[1-4]P|/|\s|\b|$)", s, re.IGNORECASE)
    if m_curve_match:
        curve = m_curve_match.group(1).upper()
    elif "动力型" in s or re.search(r"\bD(?:\s*型|\s*曲线)?\b", s, re.IGNORECASE):
        curve = "D"
    elif "照明型" in s or re.search(r"\bC(?:\s*型|\s*曲线)?\b", s, re.IGNORECASE):
        curve = "C"
    elif re.search(r"\bB(?:\s*型|\s*曲线)?\b", s, re.IGNORECASE):
        curve = "B"

    # 3. 提取额定电流 In (A)
    rated_amp: Optional[float] = None
    # 优先显式 In 标注或带单位 A: 如 In=63A, In: 100A, 100A, 63A
    m_exp = re.search(r"\bIN\s*[:=]?\s*([1-9]\d{0,3}(?:\.\d+)?)\s*A?\b", s, re.IGNORECASE)
    if m_exp:
        try:
            v = float(m_exp.group(1))
            if isfinite(v) and v > 0:
                rated_amp = v
        except ValueError:
            pass

    if rated_amp is None and m_curve_match:
        try:
            v = float(m_curve_match.group(2))
            if isfinite(v) and v > 0:
                rated_amp = v
        except ValueError:
            pass

    if rated_amp is None:
        m_a = re.search(r"(?:/|-|\b)([1-9]\d{0,3}(?:\.\d+)?)\s*A\b", s, re.IGNORECASE)
        if m_a:
            try:
                v = float(m_a.group(1))
                if isfinite(v) and v > 0:
                    rated_amp = v
            except ValueError:
                pass

    if rated_amp is None:
        # 开头纯数字带极数或斜杠: 如 100/3P, 63/4P
        m_num = re.search(r"^([1-9]\d{0,3}(?:\.\d+)?)(?:/[1-4]P|/|\s|$)", s, re.IGNORECASE)
        if m_num:
            try:
                v = float(m_num.group(1))
                if isfinite(v) and v > 0:
                    rated_amp = v
            except ValueError:
                pass

    # 4. 提取漏电动作电流 (mA)
    leakage_ma: Optional[int] = None
    m_leak = re.search(r"(\d{2,4})\s*m[aA]", s)
    if m_leak:
        try:
            leakage_ma = int(m_leak.group(1))
        except ValueError:
            pass

    # 5. 提取分断能力 (kA)
    breaking_capacity: Optional[str] = None
    m_bc = re.search(r"(\d{1,2}(?:\.\d+)?)\s*kA", s, re.IGNORECASE)
    if m_bc:
        breaking_capacity = f"{m_bc.group(1)}kA"

    # 6. 提纯系列名 (如 iC65N, NXB-63, NM1-125S)
    series = ""
    m_series = re.search(
        r"\b([A-Za-z0-9]+(?:-(?![CDB]\d)[A-Za-z0-9]+)?)(?=-[CDB]\d|-/|/[1-4]P|/|\s|$)",
        s,
        re.IGNORECASE
    )
    if m_series:
        candidate = m_series.group(1).strip()
        if len(candidate) >= 3 and not candidate.isdigit() and not re.match(r"^[CDB]\d+A?$", candidate, re.IGNORECASE):
            series = candidate

    return StructuredBreaker(
        raw_text=raw,
        manufacturer=brand,
        series=series,
        curve=curve,
        rated_current=rated_amp,
        poles=poles,
        breaking_capacity=breaking_capacity,
        leakage_ma=leakage_ma,
    )
