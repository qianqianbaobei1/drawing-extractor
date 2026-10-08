"""Build repeatable quotation rows from validated observations.

The same RawExtraction produces the same result. Unclear expressions remain in
circuits and are reported for review instead of being counted as one device.
"""
from collections import defaultdict
import re

from .schema import (
    AssembledMeta,
    Box,
    Circuit,
    Component,
    DistributionNode,
    Evidence,
    EvidenceType,
    ExtractionResult,
    ExtraDevice,
    GroundedField,
    RawExtraction,
    ReviewStatus,
    Uncertainty,
    MODEL_EVIDENCE_ORIGIN,
)
from .normalizer import parse_breaker, parse_cable
from .config import delivery, domain

_DOMAIN = domain()
_DELIVERY = delivery()
_BREAKER_CFG = _DOMAIN["breaker"]
_ESTIMATION = _DELIVERY["estimation"]
DEFAULT_DEVICE_UNIT = _ESTIMATION["default_device_unit"]
DEFAULT_ASSEMBLY_UNIT = _ESTIMATION["default_assembly_unit"]
UNNAMED_DEVICE_FALLBACK = _ESTIMATION["unnamed_device_fallback"]
UNNAMED_BOX_FALLBACK = _ESTIMATION["unnamed_box_fallback"]

BASE_TITLE = _DELIVERY["labels"]["base_title"]
AS_WRITTEN = _DELIVERY["labels"]["as_written"]
NO_SPEC = _DELIVERY["labels"]["no_spec"]


# 证据来源：assemble 只能把手上的模型自述登记为 model_vision 证据。
# 只有 cad_native / ocr / human 才是独立于模型自述的物理事实证据（见 schema.PHYSICAL_EVIDENCE_ORIGINS），
# 交叉验证由 corroborate.py 在拿到图纸原生文字后补齐。


def _model_evidence(evidence_id: str, raw_content: str, bbox=None) -> Evidence:
    """登记一条模型自述证据。标签统一带来源，避免被当成物理事实放行。"""
    return Evidence(
        evidence_id=evidence_id,
        evidence_type=EvidenceType.TEXT.value,
        raw_content=raw_content,
        bbox=bbox,
        origin=MODEL_EVIDENCE_ORIGIN,
    )


def _to_uncertainties(texts: list[str], model_texts: set[str]) -> list[Uncertainty]:
    """待核对项去重。模型看出来的事实与程序算出来的告警分开标记来源，
    否则导出再读回时会把程序告警当成模型事实永久保留。"""
    seen: set[str] = set()
    out: list[Uncertainty] = []
    for text in texts:
        item = Uncertainty.from_text(text)
        if not item.text or item.text in seen:
            continue
        seen.add(item.text)
        item.source = "model" if text in model_texts else "program"
        out.append(item)
    return out
CATEGORY_ORDER = tuple(_DOMAIN["categories"]["order"])
PREFIXES = tuple((prefix, category) for prefix, category in _BREAKER_CFG["category_prefixes"])
OTHER_CATEGORY = _BREAKER_CFG["other_category"]


def _breaker_name(spec: str) -> str:
    """按 config/domain.json 的 breaker 段把型号归到元器件类别。

    判定顺序即规则顺序：先精确匹配标准代码前缀（MCB/MCCB/RCBO/ATSE/IS-/DS-），
    再按规则表做前缀/包含判定，最后用脱扣曲线写法（C16/1P 等）按额定电流分流。
    新增系列只改 JSON，不动代码。
    """
    s = (spec or "").strip()
    upper = s.upper()
    if not upper:
        return OTHER_CATEGORY

    # 1. 显式标准代码前缀（保留 MCB+Vigi 这类“主体+附件”结构的主体类别）
    for prefix, name in PREFIXES:
        if upper.startswith(prefix):
            return name

    # 2. 规则表：前缀优先，其次关键词包含
    for rule in _BREAKER_CFG.get("category_rules", []):
        category = rule.get("category") or OTHER_CATEGORY
        if any(upper.startswith(k) for k in rule.get("prefix_any", [])):
            return category
        if any(k in upper for k in rule.get("contains_any", [])):
            return category

    # 3. 典型脱扣曲线与极数表示法：C16/1P, D32/3P, C63/2P, B10/1P 等
    curve_regex = _BREAKER_CFG.get("curve_regex") or ""
    m_curve = re.match(curve_regex, upper) if curve_regex else None
    if m_curve:
        amp = int(m_curve.group(1))
        split = float(_BREAKER_CFG.get("curve_amp_split", 0))
        return (_BREAKER_CFG["curve_le_category"] if amp <= split
                else _BREAKER_CFG["curve_gt_category"])

    return OTHER_CATEGORY


ACCESSORY_PREFIXES = tuple(_DOMAIN["accessory_prefixes"])


def _parse_devices(value: str, allow_combo: bool = False) -> list[tuple[str, int]] | None:
    """Recognize explicit counts only; return None for ambiguous combinations."""
    value = value.strip()
    if not value:
        return []
    pieces = re.split(r"[+＋]", value)
    if len(pieces) > 1:
        # 若第一项为标准开关，其余各项为电气附件（漏电模块VM、辅助触点OF、报警触点SD、分励脱扣MX等），则安全视为带附件的主开关
        first_is_breaker = any(pieces[0].strip().upper().startswith(p) for p, _ in PREFIXES)
        rest_are_accessories = all(
            any(p.strip().upper().startswith(acc) for acc in ACCESSORY_PREFIXES)
            for p in pieces[1:]
        )
        if first_is_breaker and rest_are_accessories:
            return [(value, 1)]
        if not allow_combo or not all(
            any(piece.strip().upper().startswith(prefix) for prefix, _ in PREFIXES)
            for piece in pieces
        ):
            return None
    parsed = []
    for piece in pieces:
        piece = piece.strip()
        match = re.fullmatch(r"(\d+)\s*[×xX*]\s*(.+)", piece)
        if match:
            count, spec = int(match[1]), match[2].strip()
        else:
            match = re.fullmatch(r"(.+?)\s*[×xX*]\s*(\d+)", piece)
            count, spec = (int(match[2]), match[1].strip()) if match else (1, piece)
        if count <= 0 or not spec or re.search(r"[+＋]", spec):
            return None
        parsed.append((spec, count))
    return parsed


def _circuit_label(circuit) -> str:
    if circuit.circuit_no:
        return circuit.circuit_no
    if circuit.load_name == "备用":
        return f"备用({circuit.phase})" if circuit.phase else "备用"
    return circuit.load_name or "未编号回路"


def is_incoming_circuit(circuit) -> bool:
    """进线回路统一判定：assemble 排序与 checker 级配核验共用。

    口径：circuit_no == "进线"，或 load_name 以"进线"开头。
    刻意不看 note 里的"总开"等自由文本——自由文本误判会导致排序错位
    与级配核验误告警；拿不准的进线应进 uncertainties 由人工确认。
    """
    cno = (getattr(circuit, "circuit_no", "") or "").strip()
    lname = (getattr(circuit, "load_name", "") or "").strip()
    return cno == "进线" or lname.startswith("进线")


def _circuit_order(circuit) -> int:
    if is_incoming_circuit(circuit):
        return 0
    if circuit.load_name == "备用" and not circuit.circuit_no:
        return 2
    return 1


def build_distribution_topology(boxes: list[Box], circuits: list[Circuit]) -> list[DistributionNode]:
    """构建配电系统层级拓扑树：支持 项目/一级总配电柜 -> 二级配电分箱 -> 一次出线支路 / 二次控制原理图。"""
    if not boxes and not circuits:
        return []

    # 1. 整理所有有效箱体，若有回路引用了未在 boxes 中声明的箱号则补全虚拟箱体
    all_boxes_dict: dict[str, Box] = {}
    for b in boxes:
        if b.code:
            all_boxes_dict[b.code] = b
    for c in circuits:
        if c.box and c.box not in all_boxes_dict:
            all_boxes_dict[c.box] = Box(code=c.box, name="配电箱")

    circuits_by_box: dict[str, list[Circuit]] = defaultdict(list)
    for c in circuits:
        circuits_by_box[c.box].append(c)

    # 2. 判定箱体类别 (cabinet: 一级总柜, box: 二级分箱, secondary: 二次控制原理图)
    # 规则由 config/domain.json 中的 topology 段驱动，保留标准电气柜型与盘柜代号正则兜底
    _TOPOLOGY_CFG = _DOMAIN.get("topology") or {}
    _CAB_CODE_REGEXES = [
        re.compile(p, re.IGNORECASE)
        for p in _TOPOLOGY_CFG.get("cabinet_code_regexes", [
            r"^(?:\d+)?(?:AA|ALZ|APZ|ZAP|GGD|MNS|GCK|GCS)(?:\d+)?$",
            r"^(?:[0-9A-Z]+)?(?:AL|AP)Z(?:\d+)?$",
        ])
    ]
    _CAB_NAME_PATTERNS = tuple(_TOPOLOGY_CFG.get("cabinet_name_patterns", (
        "总配电", "动力总", "进线柜", "变压器出线", "低压配电屏", "主配", "总箱", "母线联络"
    )))
    _SEC_PATTERNS = tuple(_TOPOLOGY_CFG.get("secondary_patterns", (
        "二次", "控制原理", "控制电路", "原理图", "二次接线"
    )))

    def _judge_type(box: Box) -> str:
        name = box.name or ""
        code = (box.code or "").strip()
        if any(p in name for p in _SEC_PATTERNS):
            return "secondary"
        if any(p in name for p in _CAB_NAME_PATTERNS):
            return "cabinet"
        if any(rgx.search(code) for rgx in _CAB_CODE_REGEXES):
            return "cabinet"
        return "box"

    box_types: dict[str, str] = {code: _judge_type(b) for code, b in all_boxes_dict.items()}

    # 3. 关联上下级供电关系与二次控制挂接 (parent_map: child_code -> (parent_code, feed_circuit_no))
    parent_map: dict[str, tuple[str, str]] = {}

    for src_code, src_circs in circuits_by_box.items():
        for c in src_circs:
            search_text = f"{c.load_name} {c.note}".strip()
            if not search_text:
                continue
            for target_code in all_boxes_dict:
                if target_code == src_code or target_code in parent_map:
                    continue
                # 精准语义或词边界匹配箱体代号，如 "至 01AL1", "01AL1 配电箱", "送01AL2"；严禁无边界子串模糊匹配导致 AP1 误伤 AP10
                pattern = rf"(?:^|至|送|往|引至|供|接|配电箱|\b){re.escape(target_code)}(?:配电箱|分箱|箱|柜|照明箱|动力箱|\b|$|\s)"
                if re.search(pattern, search_text, re.IGNORECASE):
                    parent_map[target_code] = (src_code, c.circuit_no)
                    if box_types.get(src_code) != "secondary":
                        box_types[src_code] = "cabinet"

    # 处理二次控制图的编号关联（如 01AL2-2 挂接在 01AL2 下，或回路 secondary_ref 指向 01AL2-2）
    for code, b in all_boxes_dict.items():
        if code in parent_map:
            continue
        if box_types[code] == "secondary":
            if "-" in code:
                base_code = code.rsplit("-", 1)[0]
                if base_code in all_boxes_dict:
                    parent_map[code] = (base_code, "")
                    continue
            for p_code, p_circs in circuits_by_box.items():
                for c in p_circs:
                    if c.secondary_ref and (c.secondary_ref == code or code in c.secondary_ref):
                        parent_map[code] = (p_code, c.circuit_no)
                        break
                if code in parent_map:
                    break

    # 4. 递归组装拓扑树节点
    visited: set[str] = set()

    def _build_node(code: str, feed_circuit: str = "", depth: int = 0) -> DistributionNode:
        visited.add(code)
        box = all_boxes_dict.get(code) or Box(code=code, name="配电箱")
        circs = circuits_by_box.get(code, [])
        node_t = box_types.get(code, "box")

        child_nodes: list[DistributionNode] = []
        for other_code, (p_code, f_cir) in parent_map.items():
            if p_code == code and other_code not in visited and depth < 6:
                child_nodes.append(_build_node(other_code, feed_circuit=f_cir, depth=depth + 1))

        explicit_secondary_refs = {c.secondary_ref for c in circs if c.secondary_ref}
        for s_ref in sorted(explicit_secondary_refs):
            if s_ref not in all_boxes_dict:
                matching_circs = [c for c in circs if c.secondary_ref == s_ref]
                c_no = matching_circs[0].circuit_no if matching_circs else ""
                c_load = matching_circs[0].load_name if matching_circs else ""
                c_kw = matching_circs[0].power_kw if matching_circs else ""
                child_nodes.append(DistributionNode(
                    id=f"sec_{code}_{s_ref}",
                    code=s_ref,
                    name=f"{c_load or c_no} 控制原理图".strip(),
                    node_type="secondary",
                    parent_code=code,
                    feed_circuit=c_no,
                    power_kw=c_kw,
                    secondary_ref=s_ref,
                    children=[],
                    note="由出线回路引申之二次控制",
                ))

        child_nodes.sort(key=lambda n: (0 if n.node_type == "box" else (1 if n.node_type == "cabinet" else 2), n.code))

        total_kw = 0.0
        for c in circs:
            try:
                if c.power_kw:
                    val = float(re.findall(r"[\d.]+", c.power_kw)[0])
                    total_kw += val
            except Exception:
                pass
        kw_str = f"{round(total_kw, 1)}kW" if total_kw > 0 else ""

        return DistributionNode(
            id=f"node_{code}",
            code=code,
            name=box.name or "配电箱",
            node_type=node_t,
            parent_code=parent_map.get(code, ("", ""))[0],
            feed_circuit=feed_circuit,
            circuits_count=len(circs),
            power_kw=kw_str,
            children=child_nodes,
            note=box.note or (f"{box.size} {box.install}".strip()),
        )

    root_codes = [code for code in all_boxes_dict if code not in parent_map]
    root_codes.sort(key=lambda c: (0 if box_types.get(c) == "cabinet" else (2 if box_types.get(c) == "secondary" else 1), c))

    roots: list[DistributionNode] = []
    for r_code in root_codes:
        if r_code not in visited:
            roots.append(_build_node(r_code))

    return roots


NON_BOX_PATTERNS = list(_DOMAIN["cad"]["non_box_patterns"])

GENERIC_BOX_TERMS = set(_DOMAIN["cad"]["generic_box_terms"])


def _is_real_box_code(code: str) -> bool:
    code = (code or "").strip()
    if not code or code in GENERIC_BOX_TERMS:
        return False
    if not re.search(r"[A-Za-z0-9]", code):
        return False
    for pat in NON_BOX_PATTERNS:
        if re.match(pat, code, re.I):
            return False
    return True


def _is_real_box(b: Box) -> bool:
    code = (b.code or "").strip()
    name = (b.name or "").strip()
    if code in GENERIC_BOX_TERMS:
        return False
    if not code and (not name or name in GENERIC_BOX_TERMS):
        return False
    if not re.search(r"[A-Za-z0-9]", code):
        return False
    for pat in NON_BOX_PATTERNS:
        if re.match(pat, code, re.I):
            return False
    return True


def assemble(raw: RawExtraction, meta: dict | None = None) -> ExtractionResult:
    model_texts = {item.text.strip() for item in raw.uncertainties if item.text.strip()}
    warnings = [item.text.strip() for item in raw.uncertainties if item.text.strip()]

    # 1. 过滤非配电箱误识别项，并将跨切片重复箱体按柜号归一化合并
    extra_devs_from_boxes = []
    dedup_boxes_dict: dict[str, Box] = {}
    for box in raw.boxes:
        code = (box.code or "").strip()
        name = (box.name or "").strip()
        # 通用判断：若柜号或名称为二次监控仪表/模块，转入非回路设备清单
        if any(k in name for k in _DOMAIN["cad"]["secondary_device_markers"]) or code.upper().startswith("AKPM"):
            extra_devs_from_boxes.append(
                ExtraDevice(name=name or UNNAMED_DEVICE_FALLBACK, spec=code,
                            unit=DEFAULT_DEVICE_UNIT, quantity=1.0,
                            used_in=box.location or UNNAMED_BOX_FALLBACK)
            )
            continue
        if not _is_real_box(box):
            continue
        if not code:
            code = (box.name or "").strip()
        if code not in dedup_boxes_dict:
            b_copy = box.model_copy(deep=True)
            b_copy.quantity = max(1, int(float(b_copy.quantity or 1)))
            dedup_boxes_dict[code] = b_copy
        else:
            # 同一柜号在不同切片或视图出现时，多字段择优合并
            exist = dedup_boxes_dict[code]
            if len(box.name or "") > len(exist.name or ""):
                exist.name = box.name
            if not exist.size and box.size:
                exist.size = box.size
            if not exist.install and box.install:
                exist.install = box.install
            if not exist.ip_rating and box.ip_rating:
                exist.ip_rating = box.ip_rating
            if not exist.location and box.location:
                exist.location = box.location

    evidence_store: dict[str, Evidence] = dict(getattr(raw, "evidence_store", {}) or {})

    merged_boxes = list(dedup_boxes_dict.values())
    for b in merged_boxes:
        if not b.claims:
            b_code_ev_ids = []
            if b.code:
                ev_id = f"box.code.{b.code}"
                if ev_id not in evidence_store:
                    evidence_store[ev_id] = _model_evidence(
                        ev_id, b.code, getattr(b, "bbox", None))
                b_code_ev_ids.append(ev_id)

            b.claims = {
                "box.code": GroundedField(
                    value=b.code,
                    raw_value=b.code,
                    value_evidence_ids=b_code_ev_ids,
                    review_status=ReviewStatus.PARSED_OK.value if b_code_ev_ids else ReviewStatus.UNASSESSED.value
                ),
                "box.location": GroundedField(
                    value=b.location,
                    raw_value=b.location,
                    review_status=ReviewStatus.UNASSESSED.value
                ),
                "box.ip_rating": GroundedField(
                    value=b.ip_rating,
                    raw_value=b.ip_rating,
                    review_status=ReviewStatus.PARSED_OK.value if b.ip_rating else ReviewStatus.UNASSESSED.value
                )
            }
    boxes = {box.code: box for box in merged_boxes if box.code}

    # 2. 回路跨切片去重：同一箱体内完全一致的回路（编号+断路器+负荷名+导线+功率）。
    # 导线/功率不同也算不同回路——只按前四项去重会静默丢真实回路（少算钱）。
    # 被去重丢弃的回路记入 warnings（转 uncertainties），不得静默丢。
    seen_circs = set()
    dedup_circuits = []
    for c in raw.circuits:
        if c.box and not _is_real_box_code(c.box):
            continue
        c_copy = c.model_copy(deep=True)
        sig = (c_copy.box.strip(), c_copy.circuit_no.strip(), c_copy.breaker.strip(),
               c_copy.load_name.strip(), c_copy.cable.strip(), c_copy.power_kw.strip())
        if sig != ("", "", "", "", "", "") and sig in seen_circs:
            label = c_copy.circuit_no.strip() or c_copy.load_name.strip() or "（无编号）"
            warnings.append(
                f"疑似重复回路 {c_copy.box.strip()} {label}（断路器 {c_copy.breaker.strip()}）"
                f"已去重保留一条，请核对"
            )
            continue
        seen_circs.add(sig)

        # 结构化清洗断路器与导线参数 (Stage 5)
        # 若回路字段被修改过，强制更新结构化清洗参数
        if not c_copy.structured_breaker or (c_copy.breaker and c_copy.structured_breaker.get("raw") != c_copy.breaker):
            c_copy.structured_breaker = parse_breaker(c_copy.breaker).model_dump()
            c_copy.structured_breaker["raw"] = c_copy.breaker
        if not c_copy.structured_cable or (c_copy.cable and c_copy.structured_cable.get("raw") != c_copy.cable):
            c_copy.structured_cable = parse_cable(c_copy.cable).model_dump()
            c_copy.structured_cable["raw"] = c_copy.cable

        # 沉淀证据主张 Claim 字典与物理证据链 (Stage 6)
        if not c_copy.claims:
            c_no_ev_ids = []
            if c_copy.circuit_no:
                ev_id = f"circuit.{c_copy.box}.{c_copy.circuit_no}.no"
                if ev_id not in evidence_store:
                    evidence_store[ev_id] = _model_evidence(ev_id, c_copy.circuit_no, c_copy.bbox)
                c_no_ev_ids.append(ev_id)

            brk_ev_ids = []
            if c_copy.breaker:
                ev_id = f"circuit.{c_copy.box}.{c_copy.circuit_no or 'none'}.breaker"
                if ev_id not in evidence_store:
                    evidence_store[ev_id] = _model_evidence(ev_id, c_copy.breaker, c_copy.bbox)
                brk_ev_ids.append(ev_id)

            cable_ev_ids = []
            if c_copy.cable:
                ev_id = f"circuit.{c_copy.box}.{c_copy.circuit_no or 'none'}.cable"
                if ev_id not in evidence_store:
                    evidence_store[ev_id] = _model_evidence(ev_id, c_copy.cable, c_copy.bbox)
                cable_ev_ids.append(ev_id)

            c_copy.claims = {
                "circuit.circuit_no": GroundedField(
                    value=c_copy.circuit_no,
                    raw_value=c_copy.circuit_no,
                    value_evidence_ids=c_no_ev_ids,
                    review_status=ReviewStatus.PARSED_OK.value if c_no_ev_ids else ReviewStatus.UNASSESSED.value
                ),
                "circuit.breaker": GroundedField(
                    value=c_copy.breaker,
                    raw_value=c_copy.breaker,
                    value_evidence_ids=brk_ev_ids,
                    review_status=ReviewStatus.PARSED_OK.value if (brk_ev_ids and c_copy.structured_breaker and c_copy.structured_breaker.get("rated_current")) else ReviewStatus.UNASSESSED.value
                ),
                "circuit.cable": GroundedField(
                    value=c_copy.cable,
                    raw_value=c_copy.cable,
                    value_evidence_ids=cable_ev_ids,
                    review_status=ReviewStatus.PARSED_OK.value if (cable_ev_ids and c_copy.structured_cable and c_copy.structured_cable.get("section_mm2")) else ReviewStatus.UNASSESSED.value
                ),
                "circuit.phase": GroundedField(
                    value=c_copy.phase,
                    raw_value=c_copy.phase,
                    review_status=ReviewStatus.PARSED_OK.value if c_copy.phase else ReviewStatus.UNASSESSED.value
                ),
                "circuit.load_name": GroundedField(
                    value=c_copy.load_name,
                    raw_value=c_copy.load_name,
                    review_status=ReviewStatus.PARSED_OK.value if c_copy.load_name else ReviewStatus.UNASSESSED.value
                )
            }
        else:
            # 当回路已存在 Claims 时，比对并联动同步修改后的字段，坚决杜绝 Claim 残留旧值
            fields_to_check = [
                ("circuit.circuit_no", c_copy.circuit_no),
                ("circuit.breaker", c_copy.breaker),
                ("circuit.cable", c_copy.cable),
                ("circuit.phase", c_copy.phase),
                ("circuit.load_name", c_copy.load_name),
            ]
            for fname, curr_val in fields_to_check:
                if fname in c_copy.claims:
                    cf = c_copy.claims[fname]
                    if cf.raw_value != curr_val:
                        cf.value = curr_val
                        cf.raw_value = curr_val
                        cf.review_status = ReviewStatus.CONFIRMED.value
                        cf.notes = "修改同步更新"
                        # 补充修改追踪证据
                        mod_ev_id = f"modified.{c_copy.box}.{fname}.{hash(curr_val) % 10000}"
                        if mod_ev_id not in evidence_store:
                            # 值被改动过：这条证据来自人工/工作台编辑，不是模型原始自述
                            evidence_store[mod_ev_id] = Evidence(
                                evidence_id=mod_ev_id,
                                evidence_type=EvidenceType.TEXT.value,
                                raw_content=curr_val,
                                bbox=c_copy.bbox,
                                origin="human",
                            )
                        if mod_ev_id not in cf.value_evidence_ids:
                            cf.value_evidence_ids.append(mod_ev_id)

        dedup_circuits.append(c_copy)

    # Stable sort preserves the drawing order within each group.
    circuits = sorted(dedup_circuits, key=_circuit_order)
    for circuit in circuits:
        if circuit.load_name == "备用" and not circuit.circuit_no and not circuit.cable:
            note = "图上未标注回路编号及导线"
            if note not in circuit.note:
                circuit.note = f"{circuit.note}；{note}" if circuit.note else note

    grouped: dict[tuple[str, str, str], dict] = defaultdict(lambda: {"quantity": 0.0, "uses": [], "notes": []})

    def add(name: str, spec: str, unit: str, quantity: float, used_in: str, note: str = ""):
        row = grouped[(name, spec, unit)]
        row["quantity"] += quantity
        if used_in and used_in not in row["uses"]:
            row["uses"].append(used_in)
        if note and note not in row["notes"]:
            row["notes"].append(note)

    for box in merged_boxes:
        add("配电箱体", f"{box.code} {box.size}".strip(), _ESTIMATION["default_box_unit"], box.quantity,
            box.code, "，".join(part for part in (box.ip_rating, box.install) if part))

    for circuit in circuits:
        box = boxes.get(circuit.box)
        if box is None or box.quantity <= 0:
            warnings.append(f"回路 {_circuit_label(circuit)}: 箱体 {circuit.box or '(未标注)'} 数量无法确认，未计入元器件汇总")
            continue
        place = f"{circuit.box} {_circuit_label(circuit)}"
        for field, name in (("breaker", ""), ("contactor", "交流接触器"),
                            ("ct", "电流互感器"), ("thermal", "热继电器")):
            value = getattr(circuit, field)
            devices = _parse_devices(value, allow_combo=field == "breaker")
            if devices is None:
                # 宁可留一行“按原文计入、数量待确认”，也不能让这个器件从报价里消失：
                # 漏一行是少算钱，留一行标记过的错数据只是要人工看一眼。
                label = _breaker_name(value) if field == "breaker" else name
                add(label, value.strip(), DEFAULT_DEVICE_UNIT, box.quantity, place, AS_WRITTEN)
                warnings.append(
                    f"回路 {place}: {label} 的写法“{value}”无法安全拆分，"
                    f"已按图纸原文计入 1 只/台箱体，数量待人工确认"
                )
                continue
            for spec, count in devices:
                add(_breaker_name(spec) if field == "breaker" else name,
                    spec, DEFAULT_DEVICE_UNIT, count * box.quantity, place)

    seen_devices = set()
    dedup_devices = []
    for device in list(raw.extra_devices) + extra_devs_from_boxes:
        sig = (device.name.strip(), device.spec.strip(), (device.used_in or "").strip())
        if sig in seen_devices:
            continue
        seen_devices.add(sig)
        dedup_devices.append(device)

    for device in dedup_devices:
        if not device.spec.strip():
            # 同上：看不清规格也要留一行，让人工去补，而不是静默丢掉
            add(device.name, NO_SPEC, device.unit or "项", device.quantity,
                device.used_in, AS_WRITTEN)
            warnings.append(f"非回路设备 {device.name}: 规格未标注，已按原文计入，请补全规格后核对")
            continue
        quantity = device.quantity
        for box in merged_boxes:
            if box.quantity > 1 and (device.used_in == box.code or device.used_in.startswith(box.code + " ")):
                quantity *= box.quantity
                break
        add(device.name, device.spec, device.unit, quantity, device.used_in, device.note)

    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    components = [
        Component(name=name, spec=spec, unit=unit, quantity=data["quantity"],
                  used_in="、".join(data["uses"]), note="；".join(data["notes"]))
        for (name, spec, unit), data in sorted(
            grouped.items(), key=lambda item: (order.get(item[0][0], len(order)), item[0][1], item[0][2])
        )
    ]
    if len(merged_boxes) == 1:
        box = merged_boxes[0]
        title = f"{BASE_TITLE}——{box.code} {box.name}".strip()
        if box.quantity > 1:
            title += f"（共{int(box.quantity)}台）"
    elif merged_boxes:
        total_qty = int(sum(box.quantity for box in merged_boxes))
        title = f"{BASE_TITLE}（共{total_qty}台）"
    else:
        title = BASE_TITLE

    seen = set()
    requirements = []
    for requirement in raw.requirements:
        key = (requirement.item, requirement.content)
        if key not in seen:
            seen.add(key)
            requirements.append(requirement)

    topology = build_distribution_topology(merged_boxes, circuits)

    reconciliation = getattr(raw, "reconciliation", None)
    if not reconciliation and getattr(raw, "catalog_items", None):
        from .catalog_reconciler import DrawingCatalogReconciler
        reconciliation = DrawingCatalogReconciler.reconcile(raw.catalog_items, merged_boxes)

    return ExtractionResult(
        title=title, boxes=merged_boxes, circuits=circuits, components=components,
        requirements=requirements, uncertainties=_to_uncertainties(warnings, model_texts),
        topology=topology,
        reconciliation=reconciliation,
        evidence_store=evidence_store,
        meta=AssembledMeta(**(meta or {})),
    )
