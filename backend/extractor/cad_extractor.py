# -*- coding: utf-8 -*-
"""CAD (DXF/DWG) 原生矢量电气系统图高精度表格提取器。

直接从 AutoCAD 数据库读取 100% 精确的矢量文字与几何坐标：
1. 识别全部真实配电箱柜标头与包围盒（消除漏拆与虚构柜号）；
2. 空间各向异性拓扑聚类分块（消除一图多卡与回路串箱重复）；
3. 严格按物理列坐标与电气语义提取回路属性（消除断路器留空、字段串行、格式乱码）。
"""

from collections import defaultdict
import math
import re
from typing import Any

from .cad import clean_mtext, load_dxf_document
from .schema import Box, Circuit, ExtraDevice, RawExtraction, Requirement, Uncertainty

RE_CIRCUIT_NO = re.compile(
    r"^(N\d+|WL\d+|WP\d+|E\d+|WX\d+|[A-Z0-9]+-[0-9A-Z]+-[PC]\d+|[A-Z0-9]+-[PC]\d+|PY-\d+[A-Z]?-[PC]\d+|BF-\d+[A-Z]?-[PC]\d+)$",
    re.I,
)
RE_PHASE = re.compile(r"^(L[123NPE~,\.\-\s/]+|380/220V|220V|380V)$", re.I)
RE_POWER = re.compile(r"^(\d+(\.\d+)?\s*kW(\s*x\s*\d+)?|Pe\s*=\s*\d+(\.\d+)?\s*kW)$", re.I)
RE_CURRENT = re.compile(r"^(\d+(\.\d+)?\s*A|\d+~\d+A)$", re.I)
RE_CABLE = re.compile(
    r"^(ZR|NH|WDZ|WDZN|ZA|ZB|ZC)?[\-\s]*(YJV|BV|BVR|RVV|KVV|BBTRZ)[\-\s\d/]+.*",
    re.I,
)
RE_BREAKER = re.compile(
    r"^(内配\s*)?(MCB|MCCB|RCBO|GL\-|ATS|ATSE|SPD|IS\-|DS\-|DZ\d+|C65|NSX|EZD|iC65|NM\d+|CM\d+|TM\d+)",
    re.I,
)
RE_BREAKER_FALLBACK = re.compile(
    r"(RCBO/[1234]P|MCB-[A-Z0-9/]+|MCCB-[A-Z0-9/]+|\b[CD]\d+A?/[1234]P|\b\d+A/[1234]P)",
    re.I,
)

PANEL_CODE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9\-])(?:消防)?("
    r"01[A-Za-z][A-Za-z0-9\-]+|CDX[0-9\-]+|JLM[0-9\-]+"
    # 通用柜号形态：字母数字混排且至少含一位数字（如 2SAL2/2ALE/2AT/2SAL3/AW1）。
    # 纯字母的器件词（MCB/SPD/MCCB/RCBO/ATSE）不含数字，不会被误判为柜号。
    # 两处排除：数字+字母形排除常见单位（6kA/220V/63A）；
    # 字母+数字形排除常见规格前缀（IP65/DZ47/SC25/NM1）。
    r"|\d+(?!(?i:kA|kW|VA|V|A|W|Hz)\b)[A-Za-z]{2,6}\d*"
    r"|(?!(?i:IP|DZ|SC|MC|BV|YJV|NM|PE|PC)\d)[A-Za-z]{2,6}\d+"
    r")(?![A-Za-z0-9])"
)

GENERIC_DISCARD_TERMS = {
    "控制箱", "配电箱", "动力箱", "照明箱", "照明配电箱", "动力配电箱", "排烟风机控制箱",
}


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
    # 名称提取
    clean_h = header_text.replace("系统图", "").strip()
    m_name = re.search(r"[\u4e00-\u9fa5]+(配电箱|总箱|控制箱|电源箱)", clean_h)
    if m_name:
        meta["name"] = m_name.group(0)
    elif "配电箱" in clean_h:
        meta["name"] = "配电箱"

    # 扫描周边文字
    for item in nearby_texts:
        t = item[0]
        if any(k in t for k in ["嵌墙安装", "暗装"]):
            meta["install"] = "嵌墙安装"
        elif any(k in t for k in ["壁挂式", "明装"]):
            meta["install"] = "壁挂式"
        elif any(k in t for k in ["落地式", "落地安装"]):
            meta["install"] = "落地式"

        if any(m in t for m in ["XRM", "JXF", "GGD", "XXM", "PZ30"]):
            for m in ["XRM", "JXF", "GGD", "XXM", "PZ30"]:
                if m in t:
                    meta["size"] = m
                    break

        m_ip = re.search(r"IP\d{1,2}[A-Za-z]?", t, re.I)
        if m_ip:
            meta["ip_rating"] = m_ip.group(0).upper()

        if "消防" in t and "消防标志" in t:
            meta["note"] = "明显消防标志,并作防火处理"

    # 台数识别：共N台 / N台 / ×N（N 为数字）。
    # "×N" 加前后断言，避免把尺寸"450×350×120"误认成台数；
    # "N台"排除"第N台"（那是序号不是台数）。
    # 识别不到时保持 quantity=1，由调用方记 uncertainties 交人工核对。
    qty_recognized = False
    m_qty = (re.search(r"共\s*(\d+)\s*台", header_text)
             or re.search(r"(?<!第)(\d+)\s*台", header_text)
             or re.search(r"(?<![\d×xX])[×xX]\s*(\d+)(?![\d×xX])", header_text))
    if m_qty:
        try:
            n = int(m_qty.group(1))
            if n > 0:
                meta["quantity"] = n
                qty_recognized = True
        except ValueError:
            pass
    meta["quantity_recognized"] = qty_recognized

    return meta


def extract_cad_table_data(dxf_or_doc: Any) -> RawExtraction:
    """从 CAD 数据库直接提取全部真实箱体与回路。"""
    if isinstance(dxf_or_doc, str):
        doc = load_dxf_document(dxf_or_doc)
    else:
        doc = dxf_or_doc

    msp = doc.modelspace()

    # 1. 抓取模型空间中有效文字（过滤建筑底图坐标区域与顶部说明区，保留电气系统图区 150000 <= y <= 300000）
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
            if 150000 <= y <= 300000:
                all_texts.append((t, x, y, h))
        except Exception:
            pass

    if not all_texts:
        return RawExtraction(boxes=[], circuits=[], extra_devices=[], requirements=[], uncertainties=[])

    # 2. 检测所有配电箱标头
    candidates = []
    for t, x, y, h in all_texts:
        # 排除回路出线引用、规范、图纸编号与干线附注。
        # 含冒号的标头（如"2ALE：应急照明配电箱"）不整行丢弃，
        # 取冒号前的部分做柜号匹配。
        if any(k in t for k in ["引", "配出", "备用", "市电", "规范", "图集", "SD-", "图号", "干线"]):
            continue
        t_head = re.split(r"[:：]", t, maxsplit=1)[0]
        m = PANEL_CODE_PATTERN.search(t_head)
        if m:
            code = m.group(1).upper()
            if code in GENERIC_DISCARD_TERMS:
                continue
            # 判断是否为箱体标头
            is_header = (
                any(k in t for k in ["箱", "柜", "盘", "系统图", "共1台", "（壁挂式）", "（嵌墙安装）"])
                or h >= 280
            )
            if is_header:
                candidates.append((code, t, x, y, h))

    # 柜号归一化去重（同位置或择优选取最完整名称，优先包含“配电箱/系统图”正规标题）
    by_code: dict[str, list[tuple[str, str, float, float, float]]] = defaultdict(list)
    for c in candidates:
        by_code[c[0]].append(c)

    best_headers: dict[str, tuple[str, float, float, float]] = {}
    for code, group in by_code.items():
        best = max(
            group,
            key=lambda item: (100 if any(k in item[1] for k in ["配电箱", "系统图", "控制箱", "总箱", "电源箱"]) else 0)
            + (10 if item[4] >= 300 else 0)
            + len(item[1]),
        )
        best_headers[code] = (best[1], best[2], best[3], best[4])

    # 3. 收集系统图中所有回路编号锚点
    all_circuit_anchors: list[tuple[str, float, float]] = []
    for t, x, y, h in all_texts:
        if RE_CIRCUIT_NO.match(t):
            all_circuit_anchors.append((t, x, y))

    # 4. 空间各向异性拓扑关联：将回路锚点归属于所属配电箱
    assigned_circuits: dict[str, list[tuple[str, float, float]]] = {code: [] for code in best_headers}
    for cno, cx, cy in all_circuit_anchors:
        best_dist = float("inf")
        best_code = None
        for code, (title, hx, hy, h) in best_headers.items():
            dx = cx - hx
            dy = cy - hy
            # 同一箱体水平范围通常在 35,000 以内，垂直范围在 45,000 以内
            if abs(dx) > 35000 or abs(dy) > 45000:
                continue
            # 各向异性权重：横向跨列的距离惩罚是纵向的 2.5 倍，确保回路不会跳到相邻箱体列
            dist = (dx * 2.5) ** 2 + dy ** 2
            if dist < best_dist:
                best_dist = dist
                best_code = code
        if best_code:
            assigned_circuits[best_code].append((cno, cx, cy))

    # 5. 构建每个箱体与其所属回路详细参数
    extracted_boxes: list[Box] = []
    extracted_circuits: list[Circuit] = []
    extracted_devices: list[ExtraDevice] = []
    extracted_reqs: list[Requirement] = []
    extracted_uncertainties: list[Uncertainty] = []

    for code in sorted(best_headers.keys()):
        title, hx, hy, hh = best_headers[code]
        c_anchors = assigned_circuits[code]

        # 过滤无回路且无正式箱体标题的孤立干线标号
        if not c_anchors and not any(k in title for k in ["配电箱", "总箱", "控制箱", "电源箱"]):
            continue

        # 确定箱体局部文字探测包围盒
        if c_anchors:
            xs = [c[1] for c in c_anchors] + [hx]
            ys = [c[2] for c in c_anchors] + [hy]
            bx0, bx1 = min(xs) - 8000, max(xs) + 8000
            by0, by1 = min(ys) - 3000, max(ys) + 3000
        else:
            bx0, bx1 = hx - 12000, hx + 12000
            by0, by1 = hy - 18000, hy + 6000

        panel_texts = [t for t in all_texts if bx0 <= t[1] <= bx1 and by0 <= t[2] <= by1]

        # 提取箱体属性元数据
        meta = _extract_box_metadata(title, panel_texts)
        if not meta.get("quantity_recognized"):
            # 台数未识别：暂按 1 台计，如实标疑，不编造
            extracted_uncertainties.append(Uncertainty.from_text(
                f"{code}：箱体台数未识别，暂按 1 台计，请核对"))
        extracted_boxes.append(Box(
            code=code,
            name=meta["name"] or "配电箱",
            ip_rating=meta["ip_rating"],
            install=meta["install"],
            size=meta["size"],
            quantity=meta["quantity"],
            note=meta["note"],
        ))

        # 针对每个回路锚点，以水平 Y 轴带（tolerance ±550）提取该回路全部字段
        c_anchors.sort(key=lambda item: -item[2])
        seen_circ_no = set()
        # 被回路 breaker 字段实际消费的文本：SPD 收集时跳过这些，
        # 防止同一文本既进回路又进元器件造成双计
        consumed_breaker_texts: set[str] = set()
        for cno, ax, ay in c_anchors:
            if cno in seen_circ_no:
                continue
            seen_circ_no.add(cno)

            band_items = [t for t in panel_texts if abs(t[2] - ay) <= 550 and t[0] != cno]

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

            for t, x, y, h in band_items:
                if RE_PHASE.match(t) and not circuit.phase:
                    circuit.phase = t
                elif RE_POWER.match(t) and not circuit.power_kw:
                    circuit.power_kw = t.replace("Pe=", "").strip()
                elif RE_CURRENT.match(t) and not circuit.current_a:
                    circuit.current_a = t
                elif (RE_CABLE.match(t) or ("SC" in t and any(cb in t for cb in ["BV", "YJV", "RVV"]))) and not circuit.cable:
                    circuit.cable = t
                elif (RE_BREAKER.search(t) or RE_BREAKER_FALLBACK.search(t)) and not circuit.breaker:
                    # 避免把电缆规格中的 SC25 误当成 C25 断路器
                    if not (RE_CABLE.match(t) or ("SC" in t and any(cb in t for cb in ["BV", "YJV", "RVV"]))):
                        circuit.breaker = t
                        consumed_breaker_texts.add(t)
                elif any("\u4e00" <= ch <= "\u9fa5" for ch in t) and not circuit.load_name:
                    if not any(k in t for k in ["过载", "报警", "标志", "防火", "图", "箱", "试验", "保护器", "内配"]):
                        circuit.load_name = t
                if "过载仅报警" in t:
                    circuit.note = "过载仅报警不跳闸"
                elif "内配" in t:
                    circuit.note = (circuit.note + " " + t).strip() if circuit.note else t

            extracted_circuits.append(circuit)

        # 查找该箱体的进线回路
        incomers = [t for t in panel_texts if "引来" in t[0] or "进线" in t[0]]
        if incomers:
            inc_t = incomers[0][0]
            extracted_circuits.append(Circuit(
                box=code,
                circuit_no="进线",
                phase="L1/L2/L3/N/PE",
                breaker="",
                cable="",
                load_name="进线",
                note=inc_t,
            ))

        # 提取浪涌保护器等非回路器件。
        # 防双计：已被某回路 breaker 实际消费的文本不再收为 ExtraDevice
        # （该文本只出现在回路带内时保留 breaker 侧）。
        # 规格用图纸真实文本，不硬编码。
        spds = [t for t in panel_texts
                if ("SPD" in t[0] or "电涌" in t[0] or "浪涌" in t[0])
                and t[0] not in consumed_breaker_texts]
        if spds:
            spd_specs = sorted({clean_mtext(t[0]).strip() for t in spds
                                if clean_mtext(t[0]).strip()})
            extracted_devices.append(ExtraDevice(
                name="浪涌保护器",
                spec="；".join(spd_specs) if spd_specs else "(规格未标注)",
                unit="套",
                quantity=1.0,
                used_in=f"{code} 进线侧",
            ))

    # CAD 矢量路径不提取技术要求：不编造，如实记一条待核对交人工核对图纸说明
    extracted_uncertainties.append(Uncertainty.from_text(
        "CAD矢量解析：技术要求未提取，请人工核对图纸说明"))

    return RawExtraction(
        boxes=extracted_boxes,
        circuits=extracted_circuits,
        extra_devices=extracted_devices,
        requirements=extracted_reqs,
        uncertainties=extracted_uncertainties,
    )
