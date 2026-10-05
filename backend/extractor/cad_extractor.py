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

from .cad import clean_mtext, dwg_to_dxf, load_dxf_document
from .schema import Box, Circuit, ExtraDevice, RawExtraction, Requirement, Uncertainty

from .config import domain as _domain, pipeline as _pipeline

_B = _domain()["breaker"]
_CAD_CONFIG = _pipeline()["cad"]
_V = _domain()["vocabulary"]
_D = _domain()["cad"]

RE_CIRCUIT_NO = re.compile(_V["circuit_no_regex"], re.I)
RE_PHASE = re.compile(_V["phase_regex"], re.I)
RE_POWER = re.compile(_V["power_regex"], re.I)
RE_CURRENT = re.compile(_V["current_regex"], re.I)
RE_CABLE = re.compile(_V["cable_regex"], re.I)

BREAKER_PREFIXES = _B["model_prefixes"]
RE_BREAKER = re.compile(_B["model_regex_template"].format(prefixes=BREAKER_PREFIXES), re.I)
RE_BREAKER_FALLBACK = re.compile(_B["fallback_regex"], re.I)
RE_DISALLOWED_PREFIXES = re.compile(_B["disallowed_prefixes_regex"], re.I)

RE_ENGINEERING_UNIT = re.compile(_V["engineering_unit_regex"], re.I)
RE_NATIONAL_ATLAS = re.compile(_V["national_atlas_regex"], re.I)
CIRCUIT_NO_MAX_DIGITS = int(_V["circuit_no_max_digits"])

# 电压/频率/功率这类工程量单独成文时不是箱号，按配置正则排除（可按项目补规则，不改代码）
NON_PANEL_TOKEN_RES = [re.compile(pattern, re.I) for pattern in _V.get("non_panel_token_regexes", [])]


def extract_panel_code(text: str) -> str | None:
    """通用电气配电箱/柜体编号提取器（无项目/图纸特定硬编码）。

    依据行业通用词法结构：[可选功能/楼层前缀]+[类别字母]+[数字编号]+[可选子代号]。
    严格根据工程通用规则排除：纯工程量单位、电线电缆代号、穿管敷设代号、断路器器件前缀、国标图集编号、长数字工程编号。
    """
    clean = re.split(r"[:：]", text, maxsplit=1)[0].strip()
    clean = re.sub(r"^消防", "", clean).strip()

    for m in re.finditer(r"(?<![A-Za-z0-9\-])([A-Za-z0-9]+(?:[\-/][A-Za-z0-9]+)*)(?![A-Za-z0-9])", clean):
        token = m.group(1).strip()
        # 必须同时包含字母与数字
        if not (re.search(r"[A-Za-z]", token) and re.search(r"\d", token)):
            continue
        # 排除连续 4 位及以上数字（年份/图号/工程编号）
        if re.search(rf"\d{{{CIRCUIT_NO_MAX_DIGITS},}}", token):
            continue
        # 排除国标图集代号（如 03D702-3, 07SD101-8）
        if RE_NATIONAL_ATLAS.match(token):
            continue
        # 排除纯工程量单位（如 63A, 10kW, 220V, 50Hz, 600mm）
        if RE_ENGINEERING_UNIT.match(token):
            continue
        # 排除电缆、管材、标准、断路器器件型号前缀
        if RE_DISALLOWED_PREFIXES.match(token):
            continue
        # 排除电压/频率/功率等工程量写法（如 380/220V、DC36V、50Hz）
        if any(pat.match(token) for pat in NON_PANEL_TOKEN_RES):
            continue
        return token.upper()
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

GENERIC_DISCARD_TERMS = set(_D["generic_discard_terms"])


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
    """从 CAD 数据库提取箱体与回路候选；返回结果仍需和图面、目录进行对账。"""
    if isinstance(dxf_or_doc, str):
        if dxf_or_doc.lower().endswith(".dwg"):
            work_dir = os.path.dirname(dxf_or_doc)
            base_name = os.path.splitext(os.path.basename(dxf_or_doc))[0]
            cache_dir = os.path.join(work_dir, ".cad_cache")
            os.makedirs(cache_dir, exist_ok=True)
            cached_dxf = os.path.join(cache_dir, f"{base_name}.dxf")
            if not os.path.exists(cached_dxf) or os.path.getsize(cached_dxf) == 0:
                dwg_to_dxf(dxf_or_doc, cached_dxf)
            dxf_or_doc = cached_dxf
        doc = load_dxf_document(dxf_or_doc)
    else:
        doc = dxf_or_doc

    msp = doc.modelspace()

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
            all_texts.append((t, x, y, h))
        except Exception:
            pass

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

    # 检测所有配电箱标头
    candidates = []
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
            # 判断是否为箱体标头（关键词或字高自适应大于中位数 15%）
            is_header = (
                any(k in t for k in ["箱", "柜", "盘", "系统图", "共1台", "（壁挂式）", "（嵌墙安装）"])
                or h >= header_h_threshold
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
            + (10 if item[4] >= base_h * 1.2 else 0)
            + len(item[1]),
        )
        best_headers[code] = (best[1], best[2], best[3], best[4])

    # 3. 收集系统图中所有回路编号锚点
    all_circuit_anchors: list[tuple[str, float, float]] = []
    for t, x, y, h in all_texts:
        if RE_CIRCUIT_NO.match(t):
            all_circuit_anchors.append((t, x, y))

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
        row_y_tolerance = line_spacing * 0.45
    else:
        row_y_tolerance = base_h * float(_CAD_CONFIG["circuit_row_tolerance_ratio"])

    # 4. 空间各向异性拓扑关联：将回路锚点归属于所属配电箱
    # 自适应物理跨度阈值
    max_dx = float(_CAD_CONFIG["circuit_box_max_dx"]) * scale_factor
    max_dy = float(_CAD_CONFIG["circuit_box_max_dy"]) * scale_factor

    assigned_circuits: dict[str, list[tuple[str, float, float]]] = {code: [] for code in best_headers}
    for cno, cx, cy in all_circuit_anchors:
        best_dist = float("inf")
        best_code = None
        for code, (title, hx, hy, h) in best_headers.items():
            dx = cx - hx
            dy = cy - hy
            if abs(dx) > max_dx or abs(dy) > max_dy:
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

    for code in sorted(best_headers.keys()):
        title, hx, hy, hh = best_headers[code]
        c_anchors = assigned_circuits[code]

        # 过滤无回路且无正式箱体标题的孤立干线标号
        if not c_anchors and not any(k in title for k in ["配电箱", "总箱", "控制箱", "电源箱"]):
            continue

        # 确定箱体局部文字探测包围盒（自适应尺度）
        if c_anchors:
            xs = [c[1] for c in c_anchors] + [hx]
            ys = [c[2] for c in c_anchors] + [hy]
            bx0, bx1 = min(xs) - pad_x, max(xs) + pad_x
            by0, by1 = min(ys) - pad_y, max(ys) + pad_y
        else:
            bx0, bx1 = hx - def_win_x, hx + def_win_x
            by0, by1 = hy - def_win_y_down, hy + def_win_y_up

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

        # 针对每个回路锚点，以自适应水平 Y 轴带（tolerance ±row_y_tolerance）提取该回路全部字段
        c_anchors.sort(key=lambda item: -item[2])
        seen_circ_no = set()
        # 被回路 breaker 字段实际消费的文本：SPD 收集时跳过这些，
        # 防止同一文本既进回路又进元器件造成双计
        consumed_breaker_texts: set[str] = set()
        for cno, ax, ay in c_anchors:
            if cno in seen_circ_no:
                continue
            seen_circ_no.add(cno)

            band_items = [t for t in panel_texts if abs(t[2] - ay) <= row_y_tolerance and t[0] != cno]

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

        # 查找该箱体的进线回路与进线总断路器
        incomers = [t for t in panel_texts if any(k in t[0] for k in ["引来", "进线", "引入", "常用电源", "备用电源"])]

        # 寻找箱体内未被出线回路消费的开关电器（位于母线上方/进线侧的主断路器）
        unconsumed_breakers = [
            t for t in panel_texts
            if t[0] not in consumed_breaker_texts
            and (RE_BREAKER.search(t[0]) or RE_BREAKER_FALLBACK.search(t[0]))
            and not any(k in t[0].upper() for k in ("SPD", "浪涌", "电涌"))
            and not (RE_CABLE.match(t[0]) or ("SC" in t[0] and any(cb in t[0] for cb in ["BV", "YJV", "RVV"])))
        ]

        inc_breaker = ""
        inc_cable = ""
        inc_note = ""

        if incomers:
            inc_y = incomers[0][2]
            inc_note = " ".join(t[0] for t in incomers)
            # 优先在进线文字本身提取电缆，或在进线标高附近的水平带提取进线电缆
            for t in incomers:
                m_cb = RE_CABLE.match(t[0])
                if m_cb:
                    inc_cable = t[0]
                    break
            if not inc_cable:
                for t in panel_texts:
                    if abs(t[2] - inc_y) <= row_y_tolerance and (RE_CABLE.match(t[0]) or ("SC" in t[0] and any(cb in t[0] for cb in ["BV", "YJV", "RVV"]))):
                        inc_cable = t[0]
                        break

        if unconsumed_breakers:
            unconsumed_breakers.sort(key=lambda t: (
                100 if any(k in t[0] for k in ("MCCB", "ATS", "3P", "4P", "NM", "NSX", "C65", "NXB")) else 0,
                t[2]  # Y 坐标较高（进线总开关在上方）
            ), reverse=True)
            inc_breaker = unconsumed_breakers[0][0]
            consumed_breaker_texts.add(inc_breaker)

        if incomers or inc_breaker:
            # 进线相序不得凭空填：图纸逐字标注的相序优先，没标就留空。
            # 现场若确知该项目进线固定为三相五线制，可在 config/pipeline.json 的
            # cad.incoming_phase_default 里显式设定，届时会在备注中标注这是规则假定。
            incoming_phase = ""
            incoming_note = inc_note or ("进线总开关" if inc_breaker else "")
            for _item in panel_texts:
                if RE_PHASE.match(str(_item[0]).strip()):
                    incoming_phase = str(_item[0]).strip()
                    break
            assumption = str(_CAD_CONFIG.get("incoming_phase_default") or "")
            if not incoming_phase and assumption:
                incoming_phase = assumption
                note_extra = str(_CAD_CONFIG.get("incoming_phase_assumption_note") or "")
                if note_extra:
                    incoming_note = (incoming_note + "；" + note_extra).strip("；")
            extracted_circuits.append(Circuit(
                box=code,
                circuit_no="进线",
                phase=incoming_phase,
                breaker=inc_breaker,
                cable=inc_cable,
                load_name="进线",
                note=incoming_note,
            ))

        # 提取其他未被出线消费的独立主控制开关（如双电源备用开关/隔离刀闸），收入 extra_devices
        for extra_b in unconsumed_breakers[1:]:
            if extra_b[0] not in consumed_breaker_texts:
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
    )
