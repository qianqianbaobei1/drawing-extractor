# -*- coding: utf-8 -*-
"""Build the quotation workbook from an ExtractionResult."""
import openpyxl
import os
import re
from typing import Any
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .schema import ExtractionResult
from .config import delivery as _delivery

# 交付样式全部来自 config/delivery.json：换配色、换表名、换默认平替品牌都只改配置。
_DELIVERY = _delivery()
_FONT_FAMILY = _DELIVERY["fonts"]["family"]
_FONT_LATIN = _DELIVERY["fonts"]["latin"]
_PALETTE = _PALETTE = _DELIVERY["palette"]
_SHEETS = _DELIVERY["sheets"]
_COLUMNS = _DELIVERY["columns"]
_LABELS = _DELIVERY["labels"]
BASE_TITLE = _LABELS["base_title"]
DEFAULT_TARGET_BRAND = _DELIVERY["brand"]["default_target"]
GENERIC_BRAND = _DELIVERY["brand"]["unknown"]
_EXPORT_CFG = _DELIVERY["export_gate"]
_ESTIMATION = _DELIVERY["estimation"]


def uncertainty_texts(result: ExtractionResult, source: str | None = None) -> list[str]:
    """导出与旧接口沿用的单行写法，已确认项加前缀。source 过滤模型事实或程序告警。"""
    return [f"（已确认）{item.text}" if item.resolved else item.text
            for item in result.uncertainties
            if item.text and (source is None or item.source == source)]

HDR_FONT = Font(name=_FONT_FAMILY, size=11, bold=True, color=_PALETTE["header_text"])
HDR_FILL = PatternFill("solid", fgColor=_PALETTE["header_fill"])
TITLE_FONT = Font(name=_FONT_FAMILY, size=14, bold=True)
SUB_FONT = Font(name=_FONT_FAMILY, size=10, color=_PALETTE["sub_text"])
META_FONT = Font(name=_FONT_FAMILY, size=10, color=_PALETTE["meta_text"])
CELL_FONT = Font(name=_FONT_FAMILY, size=10)
thin = Side(style="thin", color=_PALETTE["border_light"])
dark_thin = Side(style="thin", color=_PALETTE["border_dark"])
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
DARK_BORDER = Border(left=dark_thin, right=dark_thin, top=dark_thin, bottom=dark_thin)
DOUBLE_BOTTOM_BORDER = Border(left=dark_thin, right=dark_thin, top=dark_thin, bottom=Side(style="double", color=_PALETTE["meta_text"]))
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
RIGHT = Alignment(horizontal="right", vertical="center", wrap_text=True)

# 行业级成套设备报价规范配色（对标行业实际成套厂出图标准）
SUMMARY_TITLE_FONT = Font(name=_FONT_FAMILY, size=16, bold=True)
SUMMARY_HDR_FILL = PatternFill("solid", fgColor=_PALETTE["summary_header_fill"])  # 截图同款中浅灰表头
SUMMARY_HDR_FONT = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["meta_text"])
CATEGORY_FILL = PatternFill("solid", fgColor=_PALETTE["category_fill"])  # 截图同款配电箱绿色条目
CATEGORY_FONT = Font(name=_FONT_FAMILY, size=11, bold=True, color=_PALETTE["category_text"])
CARD_HDR_FILL = PatternFill("solid", fgColor=_PALETTE["card_header_fill"])  # 垂直流水卡片深蓝标题栏
CARD_SUB_FILL = PatternFill("solid", fgColor=_PALETTE["card_sub_fill"])  # 成本小计淡浅蓝条
TOTAL_FILL = PatternFill("solid", fgColor=_PALETTE["total_fill"])
TOTAL_FONT = Font(name=_FONT_FAMILY, size=10, bold=True)
LINK_FONT = Font(name=_FONT_FAMILY, size=10, color=_PALETTE["link_text"], underline="single", bold=True)
WHITE_BOLD_FONT = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["header_text"])
DARK_BOLD_FONT = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["dark_bold_text"])
BOX_HEADER_FILL = PatternFill("solid", fgColor=_PALETTE["box_header_fill"])  # 截图同款水蓝色箱头横幅（淡青水蓝）
BOX_HEADER_FONT = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["meta_text"])
ORANGE_FILL = PatternFill("solid", fgColor=_PALETTE["orange_fill"])  # 截图同款单台合计与总计浅橙底纹
ORANGE_FONT = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["meta_text"])


def _val(obj: Any, key: str, default: Any = "") -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def sanitize_excel_value(val: Any) -> Any:
    """防范 Excel / CSV 公式注入与 DDE 命令执行 (Formula Injection 防御)。
    若单元格为字符串类型且以 =、+、-、@、\t、\r 开头，且不是受信任的内部合法公式，
    自动前置单引号进行安全转义，防止客户端被恶意利用执行外部程序。
    """
    if isinstance(val, str) and val:
        if val[0] in ('=', '+', '-', '@', '\t', '\r'):
            # 允许受信任的安全内部统计公式（如 SUM, ROUND 等受控格式），阻断命令或外部 DDE 执行
            is_internal_formula = (
                val.startswith("=SUM(") or 
                val.startswith("=ROUND(") or 
                val.startswith("='") or 
                (val.startswith("=") and any(ch.isalpha() for ch in val[:4]) and not any(k in val.lower() for k in ("cmd", "powershell", "http", "dde", "exec", "mshta", "regsvr32", "|")))
            )
            if not is_internal_formula:
                return "'" + val
    return val


def _build_comps_by_box(components: list[Any]) -> dict[str, list[Any]]:
    """将元器件按所属箱体分组，支持 '1AP1、1AP2' 跨箱多箱映射。"""
    comps_by_box: dict[str, list[Any]] = {}
    for comp in components:
        used = getattr(comp, "used_in", "") if hasattr(comp, "used_in") else str(comp.get("used_in", ""))
        parts = [p.strip() for p in re.split(r"[、,，\s]+", used) if p.strip()]
        if not parts and used:
            parts = [used.strip()]
        for p in parts:
            comps_by_box.setdefault(p, []).append(comp)
        if used and used not in comps_by_box:
            comps_by_box.setdefault(used, []).append(comp)
    return comps_by_box


def _estimate_box_costs(box: Any, box_circuits: list[Any], box_components: list[Any], brand: str = "") -> dict[str, float]:
    """成套配电箱成本构成测算模型：
    1. 箱体外壳费 (box_shell)：依据安装方式、回路数及落地/明暗装尺寸估算钣金喷塑外壳；
    2. 元器件总额 (comp_total)：基于规范化参数与品牌集采折率精准计算；
    3. 辅材母排费 (bus_total)：依据进线电流匹配 TM 铜排截面与重量动态联动铜价；
    4. 安装试验工时 (labor_total)：一二次线缆组装、辅材与耐压打压测试；
    5. 单台成套单价 (unit_price) = 全项含税工业造价。
    """
    from .pricing import calculate_box_quotation

    b_dict = {
        "box_code": str(_val(box, "code", "")),
        "box_name": str(_val(box, "name", "")),
        "install_type": str(_val(box, "install", "")),
        "box_type": str(_val(box, "size", "")),
        "ip_rating": str(_val(box, "ip_rating", "")),
    }
    circ_dicts = []
    for c in box_circuits:
        circ_dicts.append({
            "circuit_type": str(_val(c, "circuit_type", "")),
            "breaker_spec": str(_val(c, "breaker", "")),
            "load_name": str(_val(c, "load_name", "")),
            "circuit_no": str(_val(c, "circuit_no", "")),
        })
    comp_dicts = []
    # 1. 显式提取回路中的开关断路器（进线及各分支回路出线）并计入元件清单
    for c in box_circuits:
        brk_spec = str(_val(c, "breaker", "")).strip()
        if not brk_spec or brk_spec in ("-", "待确认"):
            continue
        c_type = str(_val(c, "circuit_type", "")).strip()
        load_name = str(_val(c, "load_name", "")).strip()
        c_no = str(_val(c, "circuit_no", "")).strip()
        is_inc = c_type == "incoming" or "进线" in load_name or "进线" in c_no
        dev_name = "进线断路器" if is_inc else "分支断路器"
        comp_dicts.append({
            "name": dev_name,
            "spec": brk_spec,
            "quantity": 1,
        })

    # 2. 计入箱体关联的非回路或独立元器件 (如电涌保护器 SPD、电表、信号灯等)
    for comp in box_components:
        c_spec = str(_val(comp, "spec", "")).strip()
        if not c_spec or c_spec == "-":
            continue
        comp_dicts.append({
            "name": str(_val(comp, "name", "")),
            "spec": c_spec,
            "quantity": _val(comp, "quantity", 1),
        })

    # 宁可标疑、不许编造：不再自动补入虚构的双电源切换开关。
    # （原"领域智能推断"按柜号/回路数猜 ATS 规格并计入报价，属编造，已删除）

    q = calculate_box_quotation(b_dict, circ_dicts, comp_dicts, brand=brand or DEFAULT_TARGET_BRAND)
    cb = q["cost_breakdown"]
    return {
        "box_shell": cb["enclosure_cost"],
        "comp_total": cb["component_cost"],
        "bus_total": cb["copper_busbar_cost"],
        "labor_total": round(cb["labor_cost"] + cb["auxiliary_cost"] + cb["test_cert_cost"], 2),
        "unit_price": q["final_tax_included"],
    }


def _setup(ws, title, subtitle, headers, widths):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
    c = ws.cell(row=1, column=1, value=title)
    c.font = TITLE_FONT
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(headers))
    c = ws.cell(row=2, column=1, value=subtitle)
    c.font = SUB_FONT
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=3, column=j, value=h)
        c.font = HDR_FONT
        c.fill = HDR_FILL
        c.alignment = CENTER
        c.border = BORDER
    ws.row_dimensions[3].height = 28
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w
    ws.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "1:3"
    return 4


def _row(ws, r, values, height=36, center_cols=(1, 2), template=False):
    for j, v in enumerate(values, start=1):
        c = ws.cell(row=r, column=j, value=sanitize_excel_value(v))
        if not template:  # 自定义模板自带样式，只填值
            c.font = CELL_FONT
            c.alignment = CENTER if j in center_cols else LEFT
            c.border = BORDER
    if not template:
        ws.row_dimensions[r].height = height
    return r + 1


def _blank_workbook():
    return openpyxl.Workbook()


def _template_sheet(wb, name: str):
    """自定义模板里同名工作表，数据区从第 4 行开始（保留标题与表头样式）。"""
    if name not in wb.sheetnames:
        return None
    ws = wb[name]
    for row in ws.iter_rows(min_row=4):
        for cell in row:
            cell.value = None
    return ws


def _fill_box_cards_detail_sheet(ws, title: str, subtitle: str, boxes: list[Any], circuits: list[Any], components: list[Any], target_brand: str = "") -> dict[str, int]:
    """生成按箱体垂直流水卡片展开的分项明细表，返回每个箱体卡片起始行号以供汇总表做超链接跳转。"""
    ws.title = _SHEETS["box_cards"]
    card_anchors: dict[str, int] = {}

    # 全局总标题
    ws.merge_cells("A1:J1")
    c = ws.cell(row=1, column=1, value=f"{title}——成套箱体分项卡片明细表")
    c.font = TITLE_FONT
    c.alignment = CENTER
    ws.row_dimensions[1].height = 32

    ws.merge_cells("A2:J2")
    c = ws.cell(row=2, column=1, value=subtitle)
    c.font = SUB_FONT
    c.alignment = CENTER
    ws.row_dimensions[2].height = 22

    # 列宽设定
    widths = _COLUMNS["box_cards"]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w

    # 将回路与器件按箱体归类
    circuits_by_box: dict[str, list[Any]] = {}
    for c in circuits:
        b_code = getattr(c, "box", "") if hasattr(c, "box") else str(c.get("box", ""))
        circuits_by_box.setdefault(b_code, []).append(c)

    comps_by_box = _build_comps_by_box(components)

    r = 4
    for b_idx, b in enumerate(boxes, start=1):
        code = getattr(b, "code", "") if hasattr(b, "code") else str(b.get("code", "未命名"))
        name = getattr(b, "name", "") if hasattr(b, "name") else str(b.get("name", "配电箱"))
        size = getattr(b, "size", "") if hasattr(b, "size") else str(b.get("size", "-"))
        loc = getattr(b, "location", "") if hasattr(b, "location") else str(b.get("location", "-"))
        install = getattr(b, "install", "") if hasattr(b, "install") else str(b.get("install", "-"))
        qty = getattr(b, "quantity", 1) if hasattr(b, "quantity") else (b.get("quantity", 1) or 1)
        qty_num = int(qty) if float(qty) == int(qty) else qty

        b_circuits = circuits_by_box.get(code, [])
        b_comps = comps_by_box.get(code, [])
        costs = _estimate_box_costs(b, b_circuits, b_comps, brand=target_brand or DEFAULT_TARGET_BRAND)

        # 记录超链接锚点行
        card_anchors[code] = r

        # 1. 卡片主横幅（深蓝背景，加粗白字）
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        banner_text = f"【箱柜编号：{code}】 设备名称：{name}  ｜  型号规格：{size}  ｜  安装使用部位：{loc}  ｜  安装方式：{install}  ｜  台数：{qty_num} 台"
        cell_banner = ws.cell(row=r, column=1, value=banner_text)
        cell_banner.font = WHITE_BOLD_FONT
        cell_banner.fill = CARD_HDR_FILL
        cell_banner.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[r].height = 28
        r += 1

        # 2. 价格构成小计条（浅蓝背景）
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
        cost_text = (
            f"  成套造价测算构成：箱体外壳 ¥{costs['box_shell']:.2f} ｜ "
            f"元器件总额 ¥{costs['comp_total']:.2f} ｜ "
            f"辅材母排 ¥{costs['bus_total']:.2f} ｜ "
            f"安装调试试验 ¥{costs['labor_total']:.2f} ｜ "
            f"单台合价：¥{costs['unit_price']:.2f} ｜ "
            f"总合价：¥{costs['unit_price'] * float(qty_num):.2f}"
        )
        cell_cost = ws.cell(row=r, column=1, value=cost_text)
        cell_cost.font = DARK_BOLD_FONT
        cell_cost.fill = CARD_SUB_FILL
        cell_cost.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        ws.row_dimensions[r].height = 24
        r += 1

        # 3. 回路表头
        sub_headers = ["序号", "回路编号", "回路用途/负荷名称", "器件类别", "开关/断路器规格", "接触器/附件", "设备容量(kW)", "相序", "导线型号及敷设", "工程备注"]
        for j, sh in enumerate(sub_headers, start=1):
            sc = ws.cell(row=r, column=j, value=sh)
            sc.font = SUMMARY_HDR_FONT
            sc.fill = SUMMARY_HDR_FILL
            sc.alignment = CENTER
            sc.border = DARK_BORDER
        ws.row_dimensions[r].height = 24
        r += 1

        # 4. 回路列表
        if b_circuits:
            for c_i, cir in enumerate(b_circuits, start=1):
                cir_no = getattr(cir, "circuit_no", "") if hasattr(cir, "circuit_no") else str(cir.get("circuit_no", ""))
                load_name = getattr(cir, "load_name", "") if hasattr(cir, "load_name") else str(cir.get("load_name", ""))
                breaker = getattr(cir, "breaker", "") if hasattr(cir, "breaker") else str(cir.get("breaker", ""))
                contactor = getattr(cir, "contactor", "") if hasattr(cir, "contactor") else str(cir.get("contactor", "-"))
                power_kw = getattr(cir, "power_kw", "") if hasattr(cir, "power_kw") else str(cir.get("power_kw", ""))
                phase = getattr(cir, "phase", "") if hasattr(cir, "phase") else str(cir.get("phase", ""))
                cable = getattr(cir, "cable", "") if hasattr(cir, "cable") else str(cir.get("cable", ""))
                note = getattr(cir, "note", "") if hasattr(cir, "note") else str(cir.get("note", ""))

                row_vals = [c_i, cir_no, load_name, "微断/塑壳", breaker, contactor, power_kw, phase, cable, note]
                for j, v in enumerate(row_vals, start=1):
                    cell = ws.cell(row=r, column=j, value=v)
                    cell.font = CELL_FONT
                    cell.alignment = CENTER if j in (1, 2, 4, 7, 8) else LEFT
                    cell.border = BORDER
                ws.row_dimensions[r].height = 22
                r += 1
        else:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
            c_empty = ws.cell(row=r, column=1, value="（本箱体图纸未单列支路回路，详见成套总装说明与配置）")
            c_empty.font = SUB_FONT
            c_empty.alignment = CENTER
            c_empty.border = BORDER
            ws.row_dimensions[r].height = 24
            r += 1

        # 5. 箱体附属配置元器件（若有）
        if b_comps:
            comp_names = [
                f"{getattr(cp, 'name', '') if hasattr(cp, 'name') else cp.get('name', '')} "
                f"({getattr(cp, 'spec', '') if hasattr(cp, 'spec') else cp.get('spec', '')} × "
                f"{getattr(cp, 'quantity', 1) if hasattr(cp, 'quantity') else cp.get('quantity', 1)})"
                for cp in b_comps
            ]
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=10)
            cp_cell = ws.cell(row=r, column=1, value=f"  附加装置与非回路器件：{'；'.join(comp_names)}")
            cp_cell.font = Font(name=_FONT_FAMILY, size=9, color=_PALETTE["cover_company_text"])
            cp_cell.alignment = Alignment(horizontal="left", vertical="center")
            cp_cell.fill = PatternFill("solid", fgColor=_PALETTE["card_alt_fill"])
            cp_cell.border = BORDER
            ws.row_dimensions[r].height = 22
            r += 1

        # 每个箱体卡片留 2 行空白隔离
        r += 2

    return card_anchors


def _fill_quotation_summary_sheet(ws, project_title: str, boxes: list[Any], circuits: list[Any], components: list[Any], card_anchors: dict[str, int], detail_sheet_title: str = _SHEETS["box_cards"], target_brand: str = ""):
    """高标准复刻成套设备报价(汇总)报表：
    表头包含项目单位、项目名称、联系人/电话、金额单位，配电箱绿色分类条，
    序号支持 Excel 原生超链接跳转直达箱体分项流水卡片，数量、单价、总价公式与底部自动求和。
    """
    ws.title = "成套设备报价(汇总)"

    headers = ["序号", "柜号", "箱柜名称", "箱柜型号", "单位", "数量", "单价", "总价", "备注"]
    col_widths = _COLUMNS["cover_legacy"]
    for j, w in enumerate(col_widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w

    # 第 1 行：居中大标题
    ws.merge_cells("A1:I1")
    title_cell = ws.cell(row=1, column=1, value="成套设备报价(汇总)")
    title_cell.font = SUMMARY_TITLE_FONT
    title_cell.alignment = CENTER
    ws.row_dimensions[1].height = 36

    # 第 2 行：空行
    ws.row_dimensions[2].height = 10

    # 第 3 行：项目单位
    ws.cell(row=3, column=1, value="项目单位：").font = META_FONT
    ws.row_dimensions[3].height = 20

    # 第 4 行：项目名称
    clean_proj_name = project_title.replace("——成套箱体分项卡片明细表", "").replace("图纸扒图_", "")
    ws.cell(row=4, column=1, value=f"项目名称：{clean_proj_name}").font = META_FONT
    ws.row_dimensions[4].height = 20

    # 第 5 行：联系人、联系电话、金额单位
    ws.cell(row=5, column=1, value="联系人：                          联系电话：").font = META_FONT
    c_unit = ws.cell(row=5, column=9, value="金额单位：人民币元")
    c_unit.font = META_FONT
    c_unit.alignment = RIGHT
    ws.row_dimensions[5].height = 20

    # 第 6 行：配电箱分类条目（浅绿底色，整行贯通）
    ws.merge_cells("A6:I6")
    cat_cell = ws.cell(row=6, column=1, value="配电箱")
    cat_cell.font = CATEGORY_FONT
    cat_cell.fill = CATEGORY_FILL
    cat_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col in range(1, 10):
        ws.cell(row=6, column=col).border = DARK_BORDER
    ws.row_dimensions[6].height = 24

    # 第 7 行：表头
    for j, h in enumerate(headers, start=1):
        hc = ws.cell(row=7, column=j, value=h)
        hc.font = SUMMARY_HDR_FONT
        hc.fill = SUMMARY_HDR_FILL
        hc.alignment = CENTER
        hc.border = DARK_BORDER
    ws.row_dimensions[7].height = 28

    # 循环写入各箱体
    circuits_by_box: dict[str, list[Any]] = {}
    for c in circuits:
        b_code = getattr(c, "box", "") if hasattr(c, "box") else str(c.get("box", ""))
        circuits_by_box.setdefault(b_code, []).append(c)

    comps_by_box = _build_comps_by_box(components)

    start_data_row = 8
    curr_row = start_data_row

    for i, b in enumerate(boxes, start=1):
        code = getattr(b, "code", "") if hasattr(b, "code") else str(b.get("code", "未命名"))
        name = getattr(b, "name", "") if hasattr(b, "name") else str(b.get("name", "配电箱"))
        size = getattr(b, "size", "") if hasattr(b, "size") else str(b.get("size", ""))
        loc = getattr(b, "location", "") if hasattr(b, "location") else str(b.get("location", ""))
        install = getattr(b, "install", "") if hasattr(b, "install") else str(b.get("install", ""))
        qty = getattr(b, "quantity", 1) if hasattr(b, "quantity") else (b.get("quantity", 1) or 1)
        qty_num = int(qty) if float(qty) == int(qty) else qty

        # 箱柜型号规范显示：缺失时标"待确认"，不编造型号
        box_model = size if size and size != "-" else "待确认"

        b_circuits = circuits_by_box.get(code, [])
        b_comps = comps_by_box.get(code, [])
        costs = _estimate_box_costs(b, b_circuits, b_comps, brand=target_brand or DEFAULT_TARGET_BRAND)

        # 序号：从 1 开始自然编号，带超链接跳转到分项明细卡片
        seq_num = i
        c_seq = ws.cell(row=curr_row, column=1, value=seq_num)
        c_seq.alignment = CENTER
        c_seq.border = DARK_BORDER

        card_target_row = card_anchors.get(code)
        if card_target_row:
            c_seq.hyperlink = f"#'{detail_sheet_title}'!A{card_target_row}"
            c_seq.font = LINK_FONT
        else:
            c_seq.font = CELL_FONT

        # 柜号
        c_code = ws.cell(row=curr_row, column=2, value=code)
        c_code.font = CELL_FONT
        c_code.alignment = LEFT
        c_code.border = DARK_BORDER

        # 箱柜名称
        c_name = ws.cell(row=curr_row, column=3, value=name)
        c_name.font = CELL_FONT
        c_name.alignment = LEFT
        c_name.border = DARK_BORDER

        # 箱柜型号
        c_model = ws.cell(row=curr_row, column=4, value=box_model)
        c_model.font = CELL_FONT
        c_model.alignment = LEFT
        c_model.border = DARK_BORDER

        # 单位
        c_unit_cell = ws.cell(row=curr_row, column=5, value=_ESTIMATION["default_box_unit"])
        c_unit_cell.font = CELL_FONT
        c_unit_cell.alignment = CENTER
        c_unit_cell.border = DARK_BORDER

        # 数量
        c_qty = ws.cell(row=curr_row, column=6, value=qty_num)
        c_qty.font = CELL_FONT
        c_qty.alignment = CENTER
        c_qty.border = DARK_BORDER

        # 单价
        c_price = ws.cell(row=curr_row, column=7, value=costs["unit_price"])
        c_price.font = CELL_FONT
        c_price.number_format = "#,##0.00"
        c_price.alignment = RIGHT
        c_price.border = DARK_BORDER

        # 总价公式
        c_total = ws.cell(row=curr_row, column=8, value=f"=F{curr_row}*G{curr_row}")
        c_total.font = CELL_FONT
        c_total.number_format = "#,##0.00"
        c_total.alignment = RIGHT
        c_total.border = DARK_BORDER

        # 备注（使用部位/所属楼栋车间，宁可标疑、绝不臆造具体房间）
        loc_val = str(loc).strip() if loc and str(loc).strip() and str(loc).strip() != "-" else "待确认"
        c_loc = ws.cell(row=curr_row, column=9, value=loc_val)
        c_loc.font = CELL_FONT
        c_loc.alignment = LEFT
        c_loc.border = DARK_BORDER

        ws.row_dimensions[curr_row].height = 24
        curr_row += 1

    # 底部合计行
    last_data_row = curr_row - 1
    ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=5)
    tot_label = ws.cell(row=curr_row, column=1, value="合  计")
    tot_label.font = TOTAL_FONT
    tot_label.alignment = CENTER
    tot_label.fill = TOTAL_FILL

    for c_i in range(1, 6):
        ws.cell(row=curr_row, column=c_i).border = DOUBLE_BOTTOM_BORDER
        ws.cell(row=curr_row, column=c_i).fill = TOTAL_FILL

    # 数量合计
    tot_qty = ws.cell(row=curr_row, column=6, value=f"=SUM(F{start_data_row}:F{last_data_row})")
    tot_qty.font = TOTAL_FONT
    tot_qty.alignment = CENTER
    tot_qty.fill = TOTAL_FILL
    tot_qty.border = DOUBLE_BOTTOM_BORDER

    # 单价空
    tot_dash = ws.cell(row=curr_row, column=7, value="-")
    tot_dash.font = TOTAL_FONT
    tot_dash.alignment = CENTER
    tot_dash.fill = TOTAL_FILL
    tot_dash.border = DOUBLE_BOTTOM_BORDER

    # 总价合计
    tot_price = ws.cell(row=curr_row, column=8, value=f"=SUM(H{start_data_row}:H{last_data_row})")
    tot_price.font = TOTAL_FONT
    tot_price.number_format = "#,##0.00"
    tot_price.alignment = RIGHT
    tot_price.fill = TOTAL_FILL
    tot_price.border = DOUBLE_BOTTOM_BORDER

    # 备注空
    tot_rem = ws.cell(row=curr_row, column=9, value="")
    tot_rem.fill = TOTAL_FILL
    tot_rem.border = DOUBLE_BOTTOM_BORDER

    ws.row_dimensions[curr_row].height = 28


def _clean_proj_title(raw_title: str) -> str:
    cleaned = (raw_title or "").replace("——" + _LABELS["detail_card_suffix"], "").replace("图纸扒图_", "").replace(BASE_TITLE, "").strip()
    if not cleaned or cleaned == "未命名项目":
        return ""  # 宁可留空、不许编造：不再回退到具体项目名
    return cleaned


def _fill_three_sheets(wb, result: ExtractionResult, subtitle: str,
                       bidder: str = "", owner: str = "", target_brand: str = ""):
    """1:1 对标行业出图标准的极简 3-Sheet 报表：
    Sheet 1: 封面 —— 项目概况、编制单位、编制说明及规范依据；
    Sheet 2: 屏柜汇总表 —— 截图同款成套设备报价(汇总)，含超链接直达、单价总价与末尾自动求和；
    Sheet 3: 屏柜分项表 —— 截图同款成套设备报价(明细)，逐箱展开淡蓝箱头、逐项元器件清单与费用小计。

    bidder/owner 为投标单位与业主单位，调用方传入；缺省留空，不硬编码具体公司。
    """
    boxes = result.boxes or []
    circuits = result.circuits or []
    components = result.components or []
    proj_name = _clean_proj_title(result.title)

    # 将回路与器件按箱体归类
    circuits_by_box: dict[str, list[Any]] = {}
    for c in circuits:
        b_code = getattr(c, "box", "") if hasattr(c, "box") else str(c.get("box", ""))
        circuits_by_box.setdefault(b_code, []).append(c)

    comps_by_box = _build_comps_by_box(components)

    # ==========================================
    # Sheet 1: 封面
    # ==========================================
    ws_cover = wb.active
    ws_cover.title = _SHEETS["cover"]

    col_widths_cover = _COLUMNS["cover"]
    for j, w in enumerate(col_widths_cover, start=1):
        ws_cover.column_dimensions[get_column_letter(j)].width = w

    ws_cover.merge_cells("A2:I2")
    c_comp = ws_cover.cell(row=2, column=1, value=bidder)
    c_comp.font = Font(name=_FONT_FAMILY, size=14, bold=True, color=_PALETTE["cover_company_text"])
    c_comp.alignment = CENTER
    ws_cover.row_dimensions[2].height = 24

    ws_cover.merge_cells("A4:I4")
    c_title = ws_cover.cell(row=4, column=1, value="成套电气设备工程报价书")
    c_title.font = Font(name=_FONT_FAMILY, size=22, bold=True, color=_PALETTE["dark_bold_text"])
    c_title.alignment = CENTER
    ws_cover.row_dimensions[4].height = 42

    ws_cover.merge_cells("A5:I5")
    c_en = ws_cover.cell(row=5, column=1, value="COMPLETE LOW-VOLTAGE ELECTRICAL EQUIPMENT QUOTATION")
    c_en.font = Font(name=_FONT_LATIN, size=10, bold=True, color=_PALETTE["cover_en_text"])
    c_en.alignment = CENTER
    ws_cover.row_dimensions[5].height = 20

    from datetime import datetime
    meta_rows = [
        ("工程项目名称", proj_name),
        ("投标报价单位", bidder),
        ("业主建设单位", owner),
        ("箱柜设备规模", f"全项目配电箱/动力柜共计 {len(boxes)} 台"),
        ("报价编制日期", f"{datetime.now():%Y年%m月%d日}"),
        ("计价格式货币", "人民币元 (RMB ¥)"),
    ]
    for idx, (label, val) in enumerate(meta_rows, start=8):
        ws_cover.merge_cells(start_row=idx, start_column=2, end_row=idx, end_column=3)
        ws_cover.merge_cells(start_row=idx, start_column=4, end_row=idx, end_column=8)
        c_lbl = ws_cover.cell(row=idx, column=2, value=label)
        c_lbl.font = Font(name=_FONT_FAMILY, size=10.5, bold=True, color=_PALETTE["dark_bold_text"])
        c_lbl.alignment = Alignment(horizontal="right", vertical="center")
        c_lbl.fill = PatternFill("solid", fgColor=_PALETTE["label_fill"])

        c_val = ws_cover.cell(row=idx, column=4, value=val)
        c_val.font = Font(name=_FONT_FAMILY, size=10.5, bold=False, color=_PALETTE["meta_text"])
        c_val.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        c_val.fill = PatternFill("solid", fgColor=_PALETTE["value_fill"])

        for col in range(2, 9):
            ws_cover.cell(row=idx, column=col).border = DARK_BORDER
        ws_cover.row_dimensions[idx].height = 26

    ws_cover.merge_cells("B16:H16")
    c_note_hdr = ws_cover.cell(row=16, column=2, value="【成套技术与编制原则说明】")
    c_note_hdr.font = Font(name=_FONT_FAMILY, size=11, bold=True, color=_PALETTE["dark_bold_text"])
    c_note_hdr.alignment = Alignment(horizontal="left", vertical="center")
    ws_cover.row_dimensions[16].height = 26

    cover_notes = [
        "1. 编制依据：本报价书依据项目配电系统设计图纸、施工说明及 GB/T 7251 系列低压成套开关设备标准编制；",
        "2. 范围涵盖：配电箱柜壳体、进线隔离开关、分支微断/漏电断路器、电涌保护器、母线铜排、二次控制辅料及组装试验税费；",
        "3. 元器件配置：断路器与保护元件满足图纸设计分断能力及脱扣曲线要求，选用工业级高可靠性产品；",
        "4. 快速查阅：点击【屏柜汇总表】中的序号超链接，可直接跳转直达【屏柜分项表】对应箱柜的逐项器件明细卡片。",
    ]
    for n_idx, text in enumerate(cover_notes, start=17):
        ws_cover.merge_cells(start_row=n_idx, start_column=2, end_row=n_idx, end_column=8)
        c_n = ws_cover.cell(row=n_idx, column=2, value=text)
        c_n.font = Font(name=_FONT_FAMILY, size=9.5, color=_PALETTE["border_dark"])
        c_n.alignment = Alignment(horizontal="left", vertical="center")
        ws_cover.row_dimensions[n_idx].height = 22

    # ==========================================
    # Sheet 3: 屏柜分项表（先渲染，以获取各箱柜卡片的行号与单台合计单元格）
    # ==========================================
    ws_detail = wb.create_sheet(_SHEETS["detail"])

    widths_detail = [8, 22, 32, 8, 10, 14, 16, 16, 20]
    for j, w in enumerate(widths_detail, start=1):
        ws_detail.column_dimensions[get_column_letter(j)].width = w

    # 顶部标题 Row 1-5
    ws_detail.merge_cells("A1:I1")
    t1 = ws_detail.cell(row=1, column=1, value=bidder)
    t1.font = SUMMARY_TITLE_FONT
    t1.alignment = CENTER
    ws_detail.row_dimensions[1].height = 36

    ws_detail.merge_cells("A2:I2")
    t2 = ws_detail.cell(row=2, column=1, value="成套设备报价(明细)")
    t2.font = Font(name=_FONT_FAMILY, size=14, bold=True)
    t2.alignment = CENTER
    ws_detail.row_dimensions[2].height = 28

    ws_detail.cell(row=3, column=1, value="项目单位：").font = META_FONT
    ws_detail.row_dimensions[3].height = 20

    ws_detail.cell(row=4, column=1, value=f"项目名称：{proj_name}").font = META_FONT
    ws_detail.row_dimensions[4].height = 20

    ws_detail.cell(row=5, column=1, value="联系人：                          联系电话：").font = META_FONT
    u_cell = ws_detail.cell(row=5, column=9, value="金额单位：人民币元")
    u_cell.font = META_FONT
    u_cell.alignment = RIGHT
    ws_detail.row_dimensions[5].height = 20

    # Row 6: 浅绿分类条
    ws_detail.merge_cells("A6:I6")
    cat_d = ws_detail.cell(row=6, column=1, value="配电箱")
    cat_d.font = CATEGORY_FONT
    cat_d.fill = CATEGORY_FILL
    cat_d.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col in range(1, 10):
        ws_detail.cell(row=6, column=col).border = DARK_BORDER
    ws_detail.row_dimensions[6].height = 24

    card_anchors: dict[str, int] = {}
    box_unit_cells: dict[str, str] = {}

    curr_d = 7
    headers_card = ["序号", "元件名称", "型号规格", "单位", "数量", "单价", "总价", "生产厂家", "备注"]

    from .pricing import calculate_component_unit_price, estimate_box_enclosure_price
    from .catalog import identify_brand

    for b_idx, b in enumerate(boxes, start=1):
        code = getattr(b, "code", "") if hasattr(b, "code") else str(b.get("code", "未命名"))
        name = getattr(b, "name", "") if hasattr(b, "name") else str(b.get("name", "配电箱"))
        size = getattr(b, "size", "") if hasattr(b, "size") else str(b.get("size", ""))
        loc = getattr(b, "location", "") if hasattr(b, "location") else str(b.get("location", ""))
        qty = getattr(b, "quantity", 1) if hasattr(b, "quantity") else (b.get("quantity", 1) or 1)
        qty_num = int(qty) if float(qty) == int(qty) else qty
        b_circuits = circuits_by_box.get(code, [])
        b_comps = comps_by_box.get(code, [])

        # 记录箱体卡片锚点行
        card_anchors[code] = curr_d

        # 1. 箱头水蓝横条（淡青色 #D9EDF7）
        ws_detail.merge_cells(start_row=curr_d, start_column=1, end_row=curr_d, end_column=3)
        c_code_banner = ws_detail.cell(row=curr_d, column=1, value=f"1-{b_idx}  柜号: {code}")
        c_code_banner.font = BOX_HEADER_FONT
        c_code_banner.alignment = Alignment(horizontal="left", vertical="center", indent=1)

        c_size_banner = ws_detail.cell(row=curr_d, column=4, value=f"型号: {size if size and size != '-' else ''}")
        c_size_banner.font = BOX_HEADER_FONT
        c_size_banner.alignment = Alignment(horizontal="left", vertical="center")

        ws_detail.merge_cells(start_row=curr_d, start_column=5, end_row=curr_d, end_column=7)
        c_name_banner = ws_detail.cell(row=curr_d, column=5, value=f"名称:  {name}")
        c_name_banner.font = BOX_HEADER_FONT
        c_name_banner.alignment = Alignment(horizontal="left", vertical="center")

        ws_detail.merge_cells(start_row=curr_d, start_column=8, end_row=curr_d, end_column=9)
        c_loc_banner = ws_detail.cell(row=curr_d, column=8, value=f"备注:{loc if loc and loc != '-' else '待确认'}")
        c_loc_banner.font = BOX_HEADER_FONT
        c_loc_banner.alignment = Alignment(horizontal="left", vertical="center")

        for col in range(1, 10):
            ws_detail.cell(row=curr_d, column=col).fill = BOX_HEADER_FILL
            ws_detail.cell(row=curr_d, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[curr_d].height = 24
        curr_d += 1

        # 2. 表头行（浅灰色 #D9D9D9）
        for j, h in enumerate(headers_card, start=1):
            hc = ws_detail.cell(row=curr_d, column=j, value=h)
            hc.font = SUMMARY_HDR_FONT
            hc.fill = SUMMARY_HDR_FILL
            hc.alignment = CENTER
            hc.border = DARK_BORDER
        ws_detail.row_dimensions[curr_d].height = 24
        curr_d += 1

        # 3. 组织该箱体的具体元器件清单
        start_item_row = curr_d
        item_seq = 1
        box_items_subtotal = 0.0

        # A. 进线主控器件（隔离开关或塑壳/微断）
        incomer_circuit = None
        for c in b_circuits:
            c_no = getattr(c, "circuit_no", "") if hasattr(c, "circuit_no") else str(c.get("circuit_no", ""))
            c_load = getattr(c, "load_name", "") if hasattr(c, "load_name") else str(c.get("load_name", ""))
            if "进线" in c_no or "进线" in c_load:
                incomer_circuit = c
                break

        incomer_spec = ""
        if incomer_circuit:
            incomer_spec = getattr(incomer_circuit, "breaker", "") if hasattr(incomer_circuit, "breaker") else str(incomer_circuit.get("breaker", ""))
        # 宁可标疑、不许编造：进线规格缺失时不再编造型号，标"待确认"、单价计0
        if not incomer_spec or incomer_spec == "-":
            vals_inc = [item_seq, "进线断路器（待确认）", "待确认", "只", 1, 0.0, 0.0, "待确认", "待补充规格型号"]
        else:
            incomer_name = "微型隔离开关" if "SW" in incomer_spec or "隔离" in incomer_spec else ("塑壳断路器" if any(k in incomer_spec for k in ["MCCB", "100A", "160A", "250A"]) else "微型断路器")
            spec_brand = identify_brand(incomer_spec)
            use_brand = spec_brand if spec_brand != GENERIC_BRAND else (target_brand or DEFAULT_TARGET_BRAND)
            u_p, source, basis = calculate_component_unit_price(incomer_spec, brand=use_brand)
            u_p = round(u_p, 2)
            remark = basis if source != "EXACT" else ""
            vals_inc = [item_seq, incomer_name, incomer_spec, "只", 1, u_p, u_p, use_brand, remark]
        box_items_subtotal += float(vals_inc[6])
        for j, v in enumerate(vals_inc, start=1):
            cell = ws_detail.cell(row=curr_d, column=j, value=v)
            cell.font = CELL_FONT
            cell.border = DARK_BORDER
            cell.alignment = CENTER if j in (1, 4, 5) else (RIGHT if j in (6, 7) else LEFT)
            if j in (6, 7):
                cell.number_format = "#,##0.00"
        ws_detail.row_dimensions[curr_d].height = 22
        curr_d += 1
        item_seq += 1

        # B. 出线分支断路器
        for cir in b_circuits:
            c_no = getattr(cir, "circuit_no", "") if hasattr(cir, "circuit_no") else str(cir.get("circuit_no", ""))
            c_load = getattr(cir, "load_name", "") if hasattr(cir, "load_name") else str(cir.get("load_name", ""))
            if "进线" in c_no or "进线" in c_load:
                continue

            brk = getattr(cir, "breaker", "") if hasattr(cir, "breaker") else str(cir.get("breaker", ""))
            # 宁可标疑、不许编造：断路器规格缺失时标"待确认"、单价计0，不走下限价
            if not brk or brk == "-":
                c_vals = [item_seq, "断路器（待确认）", "待确认", "只", 1, 0.0, 0.0, "待确认", "待补充规格型号"]
            else:
                if any(k in brk.upper() for k in ["LE", "VM", "RCBO", "ELE", "30MA", "漏电"]):
                    dev_name = "微型漏电断路器"
                elif any(k in brk.upper() for k in ["MCCB", "NM", "NSX", "160A", "250A"]):
                    dev_name = "塑壳断路器"
                else:
                    dev_name = "微型断路器"

                spec_brand = identify_brand(brk)
                use_brand = spec_brand if spec_brand != GENERIC_BRAND else (target_brand or DEFAULT_TARGET_BRAND)
                u_p, source, basis = calculate_component_unit_price(brk, brand=use_brand)
                u_p = round(u_p, 2)
                remark = basis if source != "EXACT" else ""
                c_vals = [item_seq, dev_name, brk, "只", 1, u_p, u_p, use_brand, remark]
            box_items_subtotal += float(c_vals[6])
            for j, v in enumerate(c_vals, start=1):
                cell = ws_detail.cell(row=curr_d, column=j, value=v)
                cell.font = CELL_FONT
                cell.border = DARK_BORDER
                cell.alignment = CENTER if j in (1, 4, 5) else (RIGHT if j in (6, 7) else LEFT)
                if j in (6, 7):
                    cell.number_format = "#,##0.00"
            ws_detail.row_dimensions[curr_d].height = 22
            curr_d += 1
            item_seq += 1

        # C. 箱内电涌保护器 SPD：仅真实检出时才写行，未检出整行不写（不编造、不系统性加项，禁止将 DZ47 误判为 SPD）
        spd_comp = None
        for cp in b_comps:
            c_spec = getattr(cp, "spec", "") if hasattr(cp, "spec") else str(cp.get("spec", ""))
            if "SPD" in c_spec.upper() or "浪涌" in c_spec or "防雷" in c_spec or "避雷" in c_spec:
                spd_comp = cp
                break
        if spd_comp:
            spd_spec = getattr(spd_comp, "spec", "") if hasattr(spd_comp, "spec") else str(spd_comp.get("spec", ""))
            spec_brand = identify_brand(spd_spec)
            use_brand = spec_brand if spec_brand != GENERIC_BRAND else (target_brand or DEFAULT_TARGET_BRAND)
            spd_u_p, source, basis = calculate_component_unit_price(spd_spec, brand=use_brand)
            spd_u_p = round(spd_u_p, 2)
            remark = basis if source != "EXACT" else ""
            vals_spd = [item_seq, "电涌保护器", spd_spec, "只", 1, spd_u_p, spd_u_p, use_brand, remark]
            box_items_subtotal += float(vals_spd[6])
            for j, v in enumerate(vals_spd, start=1):
                cell = ws_detail.cell(row=curr_d, column=j, value=v)
                cell.font = CELL_FONT
                cell.border = DARK_BORDER
                cell.alignment = CENTER if j in (1, 4, 5) else (RIGHT if j in (6, 7) else LEFT)
                if j in (6, 7):
                    cell.number_format = "#,##0.00"
            ws_detail.row_dimensions[curr_d].height = 22
            curr_d += 1
            item_seq += 1

        # D. 壳体外壳
        b_box_dict = b.model_dump() if hasattr(b, "model_dump") else (b if isinstance(b, dict) else vars(b))
        enclosure_p, enc_basis = estimate_box_enclosure_price(b_box_dict, len(b_circuits))
        enclosure_p = round(enclosure_p, 2)
        vals_shell = [item_seq, "壳体", size if size and size != '-' else "标准配电箱外壳", "台", 1, enclosure_p, enclosure_p, "成套定制", enc_basis or ""]
        box_items_subtotal += float(vals_shell[6])
        for j, v in enumerate(vals_shell, start=1):
            cell = ws_detail.cell(row=curr_d, column=j, value=v)
            cell.font = CELL_FONT
            cell.border = DARK_BORDER
            cell.alignment = CENTER if j in (1, 4, 5) else (RIGHT if j in (6, 7) else LEFT)
            if j in (6, 7):
                cell.number_format = "#,##0.00"
        ws_detail.row_dimensions[curr_d].height = 22
        curr_d += 1
        item_seq += 1

        end_item_row = curr_d - 1

        # 4. 箱体收尾结算行
        # (1) 小计
        r_sub = curr_d
        ws_detail.cell(row=r_sub, column=1, value="")
        ws_detail.cell(row=r_sub, column=2, value="小计").font = TOTAL_FONT
        c_sub_val = ws_detail.cell(row=r_sub, column=7, value=f"=SUM(G{start_item_row}:G{end_item_row})")
        c_sub_val.font = TOTAL_FONT
        c_sub_val.alignment = RIGHT
        c_sub_val.number_format = "#,##0.00"
        for col in range(1, 10):
            ws_detail.cell(row=r_sub, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_sub].height = 22
        curr_d += 1

        # (2) 辅料（服务端数值化输出，避免暴露底层加价模型）
        r_aux = curr_d
        ws_detail.cell(row=r_aux, column=1, value="")
        ws_detail.cell(row=r_aux, column=2, value="辅料").font = CELL_FONT
        aux_val = round(box_items_subtotal * 0.05, 2)
        c_aux_val = ws_detail.cell(row=r_aux, column=7, value=aux_val)
        c_aux_val.font = CELL_FONT
        c_aux_val.alignment = RIGHT
        c_aux_val.number_format = "#,##0.00"
        for col in range(1, 10):
            ws_detail.cell(row=r_aux, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_aux].height = 22
        curr_d += 1

        # (3) 成套制作费
        r_labor = curr_d
        labor_val = round(_ESTIMATION["labor_incoming_fixed"]
                          + max(0, len(b_circuits) - 1) * _ESTIMATION["labor_per_extra_circuit"], 2)
        ws_detail.cell(row=r_labor, column=1, value="")
        ws_detail.cell(row=r_labor, column=2, value="成套制作费").font = CELL_FONT
        c_labor_val = ws_detail.cell(row=r_labor, column=7, value=labor_val)
        c_labor_val.font = CELL_FONT
        c_labor_val.alignment = RIGHT
        c_labor_val.number_format = "#,##0.00"
        for col in range(1, 10):
            ws_detail.cell(row=r_labor, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_labor].height = 22
        curr_d += 1

        # (4) 税费（服务端数值化输出，保护税率与利润机密）
        r_tax = curr_d
        ws_detail.cell(row=r_tax, column=1, value="")
        ws_detail.cell(row=r_tax, column=2, value="税费").font = CELL_FONT
        tax_val = round((box_items_subtotal + aux_val + labor_val) * 0.06, 2)
        c_tax_val = ws_detail.cell(row=r_tax, column=7, value=tax_val)
        c_tax_val.font = CELL_FONT
        c_tax_val.alignment = RIGHT
        c_tax_val.number_format = "#,##0.00"
        for col in range(1, 10):
            ws_detail.cell(row=r_tax, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_tax].height = 22
        curr_d += 1

        # (5) 单台合计（浅橙底色 #FCE4D6）
        r_unit = curr_d
        c_unit_seq = ws_detail.cell(row=r_unit, column=1, value=item_seq)
        c_unit_seq.alignment = CENTER
        c_unit_seq.font = ORANGE_FONT
        c_unit_lbl = ws_detail.cell(row=r_unit, column=2, value="单台合计")
        c_unit_lbl.font = ORANGE_FONT
        c_unit_tot = ws_detail.cell(row=r_unit, column=7, value=f"=G{r_sub}+G{r_aux}+G{r_labor}+G{r_tax}")
        c_unit_tot.font = ORANGE_FONT
        c_unit_tot.alignment = RIGHT
        c_unit_tot.number_format = "#,##0.00"

        for col in range(1, 10):
            ws_detail.cell(row=r_unit, column=col).fill = ORANGE_FILL
            ws_detail.cell(row=r_unit, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_unit].height = 24
        box_unit_cells[code] = f"G{r_unit}"
        curr_d += 1

        # (6) 总计（浅橙底色 #FCE4D6）
        r_tot = curr_d
        ws_detail.cell(row=r_tot, column=1, value="")
        c_tot_lbl = ws_detail.cell(row=r_tot, column=2, value="总计")
        c_tot_lbl.font = ORANGE_FONT
        c_tot_u = ws_detail.cell(row=r_tot, column=4, value="台")
        c_tot_u.font = ORANGE_FONT
        c_tot_u.alignment = CENTER
        c_tot_q = ws_detail.cell(row=r_tot, column=5, value=qty_num)
        c_tot_q.font = ORANGE_FONT
        c_tot_q.alignment = CENTER
        c_tot_val = ws_detail.cell(row=r_tot, column=7, value=f"=E{r_tot}*G{r_unit}")
        c_tot_val.font = ORANGE_FONT
        c_tot_val.alignment = RIGHT
        c_tot_val.number_format = "#,##0.00"

        for col in range(1, 10):
            ws_detail.cell(row=r_tot, column=col).fill = ORANGE_FILL
            ws_detail.cell(row=r_tot, column=col).border = DARK_BORDER
        ws_detail.row_dimensions[r_tot].height = 24
        curr_d += 1

        # 留 1 行空白隔离
        curr_d += 1

    # ==========================================
    # Sheet 2: 屏柜汇总表
    # ==========================================
    ws_summary = wb.create_sheet(_SHEETS["summary"], index=1)

    widths_sum = [10, 16, 26, 20, 8, 10, 16, 18, 24]
    for j, w in enumerate(widths_sum, start=1):
        ws_summary.column_dimensions[get_column_letter(j)].width = w

    # 顶部标题 Row 1-5
    ws_summary.merge_cells("A1:I1")
    s1 = ws_summary.cell(row=1, column=1, value=bidder)
    s1.font = SUMMARY_TITLE_FONT
    s1.alignment = CENTER
    ws_summary.row_dimensions[1].height = 36

    ws_summary.merge_cells("A2:I2")
    s2 = ws_summary.cell(row=2, column=1, value="成套设备报价(汇总)")
    s2.font = Font(name=_FONT_FAMILY, size=14, bold=True)
    s2.alignment = CENTER
    ws_summary.row_dimensions[2].height = 28

    ws_summary.cell(row=3, column=1, value="项目单位：").font = META_FONT
    ws_summary.row_dimensions[3].height = 20

    ws_summary.cell(row=4, column=1, value=f"项目名称：{proj_name}").font = META_FONT
    ws_summary.row_dimensions[4].height = 20

    ws_summary.cell(row=5, column=1, value="联系人：                          联系电话：").font = META_FONT
    su_cell = ws_summary.cell(row=5, column=9, value="金额单位：人民币元")
    su_cell.font = META_FONT
    su_cell.alignment = RIGHT
    ws_summary.row_dimensions[5].height = 20

    # Row 6: 浅绿分类条
    ws_summary.merge_cells("A6:I6")
    cat_s = ws_summary.cell(row=6, column=1, value="配电箱")
    cat_s.font = CATEGORY_FONT
    cat_s.fill = CATEGORY_FILL
    cat_s.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    for col in range(1, 10):
        ws_summary.cell(row=6, column=col).border = DARK_BORDER
    ws_summary.row_dimensions[6].height = 24

    # Row 7: 表头
    headers_sum = ["序号", "柜号", "箱柜名称", "箱柜型号", "单位", "数量", "单价", "总价", "备注"]
    for j, h in enumerate(headers_sum, start=1):
        hc = ws_summary.cell(row=7, column=j, value=h)
        hc.font = SUMMARY_HDR_FONT
        hc.fill = SUMMARY_HDR_FILL
        hc.alignment = CENTER
        hc.border = DARK_BORDER
    ws_summary.row_dimensions[7].height = 28

    # 数据行
    start_sum_r = 8
    curr_sum_r = start_sum_r
    seq_base = 46023  # 对标截图序号风格，支持连续编码

    for i, b in enumerate(boxes, start=1):
        code = getattr(b, "code", "") if hasattr(b, "code") else str(b.get("code", "未命名"))
        name = getattr(b, "name", "") if hasattr(b, "name") else str(b.get("name", "配电箱"))
        size = getattr(b, "size", "") if hasattr(b, "size") else str(b.get("size", ""))
        loc = getattr(b, "location", "") if hasattr(b, "location") else str(b.get("location", ""))
        install = getattr(b, "install", "") if hasattr(b, "install") else str(b.get("install", ""))
        qty = getattr(b, "quantity", 1) if hasattr(b, "quantity") else (b.get("quantity", 1) or 1)
        qty_num = int(qty) if float(qty) == int(qty) else qty

        # 宁可留空、不许编造：图纸未标明柜型尺寸时严禁脑补为 GGD(落地)
        box_model = str(size).strip() if size and str(size).strip() and str(size).strip() != "-" else ""

        # 序号：带超链接直达分项表对应卡片
        seq_val = seq_base + (i - 1)
        c_seq = ws_summary.cell(row=curr_sum_r, column=1, value=seq_val)
        c_seq.alignment = CENTER
        c_seq.border = DARK_BORDER
        target_row = card_anchors.get(code)
        if target_row:
            c_seq.hyperlink = f"#'屏柜分项表'!A{target_row}"
            c_seq.font = LINK_FONT
        else:
            c_seq.font = CELL_FONT

        # 柜号
        c_code = ws_summary.cell(row=curr_sum_r, column=2, value=code)
        c_code.font = CELL_FONT
        c_code.alignment = LEFT
        c_code.border = DARK_BORDER

        # 箱柜名称
        c_name = ws_summary.cell(row=curr_sum_r, column=3, value=name)
        c_name.font = CELL_FONT
        c_name.alignment = LEFT
        c_name.border = DARK_BORDER

        # 箱柜型号
        c_model = ws_summary.cell(row=curr_sum_r, column=4, value=box_model)
        c_model.font = CELL_FONT
        c_model.alignment = LEFT
        c_model.border = DARK_BORDER

        # 单位
        c_u = ws_summary.cell(row=curr_sum_r, column=5, value="台")
        c_u.font = CELL_FONT
        c_u.alignment = CENTER
        c_u.border = DARK_BORDER

        # 数量
        c_q = ws_summary.cell(row=curr_sum_r, column=6, value=qty_num)
        c_q.font = CELL_FONT
        c_q.alignment = CENTER
        c_q.border = DARK_BORDER

        # 单价（公式引用分项表中的单台合计）
        unit_cell_ref = box_unit_cells.get(code)
        if unit_cell_ref:
            c_p = ws_summary.cell(row=curr_sum_r, column=7, value=f"='屏柜分项表'!{unit_cell_ref}")
        else:
            c_p = ws_summary.cell(row=curr_sum_r, column=7, value=_ESTIMATION["summary_unit_price_fallback"])
        c_p.font = CELL_FONT
        c_p.alignment = RIGHT
        c_p.number_format = "#,##0.00"
        c_p.border = DARK_BORDER

        # 总价
        c_tot = ws_summary.cell(row=curr_sum_r, column=8, value=f"=F{curr_sum_r}*G{curr_sum_r}")
        c_tot.font = CELL_FONT
        c_tot.alignment = RIGHT
        c_tot.number_format = "#,##0.00"
        c_tot.border = DARK_BORDER

        # 备注
        c_rem = ws_summary.cell(row=curr_sum_r, column=9, value=loc if loc and loc != "-" else "待确认")
        c_rem.font = CELL_FONT
        c_rem.alignment = LEFT
        c_rem.border = DARK_BORDER

        ws_summary.row_dimensions[curr_sum_r].height = 24
        curr_sum_r += 1

    # 底部合计行
    last_sum_r = curr_sum_r - 1
    ws_summary.merge_cells(start_row=curr_sum_r, start_column=1, end_row=curr_sum_r, end_column=5)
    t_sum_lbl = ws_summary.cell(row=curr_sum_r, column=1, value="合    计")
    t_sum_lbl.font = TOTAL_FONT
    t_sum_lbl.alignment = CENTER
    t_sum_lbl.fill = TOTAL_FILL

    for col in range(1, 6):
        ws_summary.cell(row=curr_sum_r, column=col).border = DOUBLE_BOTTOM_BORDER
        ws_summary.cell(row=curr_sum_r, column=col).fill = TOTAL_FILL

    c_sum_qty = ws_summary.cell(row=curr_sum_r, column=6, value=f"=SUM(F{start_sum_r}:F{last_sum_r})")
    c_sum_qty.font = TOTAL_FONT
    c_sum_qty.alignment = CENTER
    c_sum_qty.fill = TOTAL_FILL
    c_sum_qty.border = DOUBLE_BOTTOM_BORDER

    ws_summary.cell(row=curr_sum_r, column=7, value="").fill = TOTAL_FILL
    ws_summary.cell(row=curr_sum_r, column=7).border = DOUBLE_BOTTOM_BORDER

    c_sum_money = ws_summary.cell(row=curr_sum_r, column=8, value=f"=SUM(H{start_sum_r}:H{last_sum_r})")
    c_sum_money.font = TOTAL_FONT
    c_sum_money.alignment = RIGHT
    c_sum_money.number_format = "#,##0.00"
    c_sum_money.fill = TOTAL_FILL
    c_sum_money.border = DOUBLE_BOTTOM_BORDER

    ws_summary.cell(row=curr_sum_r, column=9, value="").fill = TOTAL_FILL
    ws_summary.cell(row=curr_sum_r, column=9).border = DOUBLE_BOTTOM_BORDER
    ws_summary.row_dimensions[curr_sum_r].height = 28


def _fill_sheets(wb, result, subtitle, template: bool, target_brand: str = ""):
    """构建成套设备高精度多级报表：
    Sheet 1: 成套设备报价(汇总) —— 截图同款汇总表头与超链接直达；
    Sheet 2: 箱体分项明细 —— 垂直流水卡片展开；
    Sheet 3: 箱体清单 —— 原始参数台账；
    Sheet 4: 元器件汇总 —— 全图元器件汇总；
    Sheet 5: 回路明细 —— 完整回路参数；
    Sheet 6: 技术要求与报价说明。
    """
    if template:
        # 自定义模板导出处理
        ws1 = _template_sheet(wb, "箱体清单") or wb.active
        r = 4
        for i, b in enumerate(result.boxes, 1):
            r = _row(ws1, r, [i, b.code, b.name, b.ip_rating, b.install, b.location,
                              b.size, b.quantity, b.note], template=True)

        ws2 = _template_sheet(wb, _SHEETS["components"]) or wb.create_sheet(_SHEETS["components"])
        r = 4
        for i, c in enumerate(result.components, 1):
            q = int(c.quantity) if float(c.quantity) == int(c.quantity) else c.quantity
            r = _row(ws2, r, [i, c.name, c.spec, c.unit, q, c.used_in, c.note], template=True)

        ws3 = _template_sheet(wb, _SHEETS["circuits"]) or wb.create_sheet(_SHEETS["circuits"])
        r = 4
        for i, c in enumerate(result.circuits, 1):
            r = _row(ws3, r, [i, c.box, c.phase, c.breaker, c.contactor, c.ct, c.thermal,
                              c.power_kw, c.circuit_no, c.cable, c.current_a,
                              c.load_name, c.secondary_ref, c.start_method, c.note],
                     template=True, height=44, center_cols=(1, 2, 3))
        return

    # 1. 创建 Sheet 1 (成套设备报价汇总) 与 Sheet 2 (箱体分项明细卡片)
    ws_summary = wb.active
    ws_cards = wb.create_sheet(_SHEETS["box_cards"])

    # 先渲染卡片明细，获取每个箱体的超链接行号
    card_anchors = _fill_box_cards_detail_sheet(
        ws_cards, result.title, subtitle,
        result.boxes, result.circuits, result.components,
        target_brand=target_brand or DEFAULT_TARGET_BRAND
    )

    # 渲染第一页成套报价汇总表
    _fill_quotation_summary_sheet(
        ws_summary, result.title,
        result.boxes, result.circuits, result.components,
        card_anchors, detail_sheet_title=_SHEETS["box_cards"],
        target_brand=target_brand or DEFAULT_TARGET_BRAND
    )

    # 2. Sheet 3: 箱体清单
    ws_box = wb.create_sheet(_SHEETS["boxes"])
    headers_box = ["序号", "设备编号", "设备名称", "防护等级", "安装方式", "安装位置", "参考尺寸", "数量(台)", "备注"]
    r_box = _setup(ws_box, result.title, subtitle, headers_box, _COLUMNS["boxes"])
    for i, b in enumerate(result.boxes, 1):
        r_box = _row(ws_box, r_box, [i, b.code, b.name, b.ip_rating, b.install, b.location, b.size, b.quantity, b.note])

    # 3. Sheet 4: 元器件汇总
    ws_comp = wb.create_sheet(_SHEETS["components"])
    headers_comp = ["序号", "元器件名称", "规格型号", "单位", "数量", "用于箱体/回路", "备注"]
    r_comp = _setup(ws_comp, result.title, subtitle, headers_comp, _COLUMNS["components"])
    for i, c in enumerate(result.components, 1):
        q = int(c.quantity) if float(c.quantity) == int(c.quantity) else c.quantity
        r_comp = _row(ws_comp, r_comp, [i, c.name, c.spec, c.unit, q, c.used_in, c.note])

    # 4. Sheet 5: 回路明细
    ws_cir = wb.create_sheet(_SHEETS["circuits"])
    headers_cir = ["序号", "箱体编号", "相序", "断路器", "接触器", "电流互感器", "热继电器",
                   "设备容量(kW)", "回路编号", "导线型号及敷设", "计算电流(A)",
                   "回路名称", "二次图编号", "启动方式", "备注"]
    r_cir = _setup(ws_cir, result.title, subtitle, headers_cir, _COLUMNS["circuits"])
    for i, c in enumerate(result.circuits, 1):
        r_cir = _row(ws_cir, r_cir, [i, c.box, c.phase, c.breaker, c.contactor, c.ct, c.thermal,
                                    c.power_kw, c.circuit_no, c.cable, c.current_a,
                                    c.load_name, c.secondary_ref, c.start_method, c.note],
                     height=44, center_cols=(1, 2, 3))

    # 5. Sheet 6: 技术要求与报价说明
    ws_req = wb.create_sheet(_SHEETS["requirements"])
    r_req = _setup(ws_req, result.title, subtitle, ["序号", "项目", "要求内容"], _COLUMNS["requirements"])
    reqs = list(result.requirements)
    observed = uncertainty_texts(result, "model") + [
        u.text for u in result.uncertainties if not u.source and u.text]
    if observed:
        reqs.append({"item": "待人工核对项", "content": "；".join(observed)})
    warnings = uncertainty_texts(result, "program")
    if warnings:
        reqs.append({"item": "程序核对告警", "content": "；".join(warnings)})
    for i, q in enumerate(reqs, 1):
        item = q.item if hasattr(q, "item") else q["item"]
        content = q.content if hasattr(q, "content") else q["content"]
        r_req = _row(ws_req, r_req, [i, item, content], height=64, center_cols=(1,))


def _fill_replacements(wb, components: list, target_brand: str = ""):
    """国产化平替方案：对标外资/国标物料并推荐一线国产品牌对等型号与降本测算。"""
    from .catalog import analyze_components_replacement

    comp_dicts = []
    for c in components:
        if hasattr(c, "model_dump"):
            comp_dicts.append(c.model_dump())
        elif isinstance(c, dict):
            comp_dicts.append(c)

    if not comp_dicts:
        return

    analysis = analyze_components_replacement(comp_dicts, target_brand=target_brand)
    sheet_name = f"国产化平替方案({target_brand})"
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)

    headers = [
        "序号", "元器件名称", "原图设计规格", "原厂品牌",
        f"推荐对标型号({target_brand})", "采购数量", "单位",
        "预计降本", "电气性能对标与核验说明"
    ]
    subtitle = (
        f"平替目标品牌：{target_brand} ｜ 涉及物料：{analysis['total_components']} 项 / {analysis['total_quantity']} 件 "
        f"｜ 具备平替降本空间：{analysis['replaceable_quantity']} 件 ｜ 预计整体采购降本幅度：约 {analysis['estimated_overall_saving_pct']}%"
    )
    r = _setup(ws, f"电气元器件国产化智能平替与成本对账表（{target_brand}）", subtitle, headers,
               [6, 20, 28, 12, 34, 10, 8, 12, 45])

    save_font = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["saving_text"])
    warn_font = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["warning_text"])
    for i, it in enumerate(analysis["items"], 1):
        sav_pct = it.get("estimated_saving_pct")
        sav_status = it.get("saving_status")
        if sav_pct is not None and sav_pct > 0:
            saving_text = f"↓{sav_pct}%"
        elif sav_status == "cost_increase":
            saving_text = "成本增加"
        elif sav_status == "cost_equal":
            saving_text = "平价"
        elif sav_pct is None or sav_status == "missing_price":
            saving_text = "待询价"
        else:
            saving_text = "已最优"

        curr_r = r
        r = _row(ws, r, [
            i, it["name"], it["original_spec"], it["original_brand"],
            it["recommended_model"], it["quantity"], it["unit"],
            saving_text, it["notes"]
        ], height=32, center_cols=(1, 4, 7, 8))
        if sav_pct is not None and sav_pct > 0:
            ws.cell(row=curr_r, column=8).font = save_font
        elif sav_status == "cost_increase":
            ws.cell(row=curr_r, column=8).font = warn_font


def _fill_changes(wb, changes):
    """变更记录：人工/语音/AI 改动逐条留痕，随清单一起交付，便于对报价做追溯。"""
    if "变更记录" in wb.sheetnames:
        del wb["变更记录"]
    ws = wb.create_sheet(_SHEETS["changes"])
    headers = ["序号", "时间", "来源", "对象", "字段", "原值", "新值", "说明"]
    r = _setup(ws, "人工修改与 AI 修改记录", "数据来自工作台编辑留痕，可用于报价追溯",
               headers, [6, 20, 12, 18, 12, 30, 30, 28])
    for i, entry in enumerate(changes or [], 1):
        r = _row(ws, r, [i, entry.get("ts", ""), entry.get("source", ""),
                         entry.get("target", ""), entry.get("field", ""),
                         entry.get("old", ""), entry.get("new", ""),
                         entry.get("reason", "")], height=24)


def _fill_topology_sheet(wb, topology: list, title: str, subtitle: str):
    """绘制配电系统层级拓扑树工作表，支持直观展示总柜 -> 分箱 -> 二次原理图挂接关系。"""
    if not topology:
        return
    sheet_name = "配电系统拓扑树"
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    headers = ["序号", "系统层级拓扑架构", "节点类别", "设备/图号", "设备名称/回路描述", "供电上级柜", "供电回路", "设备容量", "回路数", "二次图号", "工程备注/规格"]
    r = _setup(ws, title, subtitle, headers, _COLUMNS["detail"])

    type_names = {
        "cabinet": "一级总柜",
        "box": "二级分箱",
        "secondary": "二次控制",
        "circuit": "出线支路",
    }
    cab_fill = PatternFill("solid", fgColor=_PALETTE["cabinet_fill"])
    sec_font = Font(name=_FONT_FAMILY, size=10, italic=True, color=_PALETTE["border_dark"])
    bold_font = Font(name=_FONT_FAMILY, size=10, bold=True)

    flat_items = []

    def _flatten(nodes, indent=0):
        for n in nodes:
            t = getattr(n, "node_type", "box")
            code = getattr(n, "code", "")
            t_label = type_names.get(t, "配电箱")
            if indent == 0:
                tree_label = f"【{t_label}】{code}"
            else:
                tree_label = f"{'    ' * indent}└── 【{t_label}】{code}"
            flat_items.append((n, tree_label, indent))
            _flatten(getattr(n, "children", []) or [], indent + 1)

    _flatten(topology)

    for i, (n, tree_label, indent) in enumerate(flat_items, 1):
        t = getattr(n, "node_type", "box")
        t_label = type_names.get(t, "配电箱")
        c_cnt = getattr(n, "circuits_count", 0)
        c_cnt_val = c_cnt if c_cnt > 0 else "-"
        curr_row = r
        r = _row(ws, r, [
            i, tree_label, t_label, getattr(n, "code", ""),
            getattr(n, "name", ""), getattr(n, "parent_code", "") or "-",
            getattr(n, "feed_circuit", "") or "-", getattr(n, "power_kw", "") or "-",
            c_cnt_val, getattr(n, "secondary_ref", "") or "-", getattr(n, "note", "")
        ], height=28, center_cols=(1, 3, 4, 6, 7, 8, 9, 10))

        if t == "cabinet":
            for col in range(1, len(headers) + 1):
                ws.cell(row=curr_row, column=col).fill = cab_fill
            ws.cell(row=curr_row, column=2).font = bold_font
        elif t == "secondary":
            ws.cell(row=curr_row, column=2).font = sec_font


def _fill_reconciliation_sheet(wb, reconciliation, title: str, subtitle: str):
    """图纸目录与提取覆盖对账审计表导出。"""
    if not reconciliation or not getattr(reconciliation, "has_catalog", False):
        return
    sheet_name = "图纸目录对账审计"
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    headers = ["序号", "图纸编号", "图纸名称", "目录声明箱柜", "实际覆盖提取箱柜", "缺失未提取箱柜", "对账状态"]
    sub = (
        f"目录声明总数: {reconciliation.total_declared_panels} 台 ｜ 实际覆盖提取: {reconciliation.covered_count} 台 "
        f"｜ 缺失未提取: {reconciliation.missing_count} 台 ｜ 覆盖率: {int(reconciliation.coverage_rate * 100)}%"
    )
    r = _setup(ws, f"{title}——图纸目录对账审计表", sub, headers, _COLUMNS["reconciliation"])
    items = getattr(reconciliation, "items", []) or []
    for i, item in enumerate(items, 1):
        status_text = "全部覆盖" if item.status == "COVERED" else ("整张图幅缺失" if item.status == "MISSING" else "部分覆盖")
        r = _row(
            ws, r,
            [
                i,
                item.sheet_no,
                item.sheet_title,
                "、".join(item.declared_panels) if item.declared_panels else "(无声明)",
                "、".join(item.matched_panels) if item.matched_panels else "(无覆盖)",
                "、".join(item.missing_panels) if item.missing_panels else "(无缺失)",
                status_text,
            ],
            height=32,
            center_cols=(1, 2, 7)
        )


def build_workbook(result: ExtractionResult, subtitle: str, out_path: str,
                   changes=None, include_changes: bool = True,
                   template_path: str = "", target_brand: str = "",
                   layout: str = "all", bidder: str = "", owner: str = "") -> str:
    template = bool(template_path) and os.path.exists(template_path)
    if template:
        try:
            wb = openpyxl.load_workbook(template_path)
        except Exception:  # noqa: BLE001 - 模板坏了不能拖垮导出
            template = False
            wb = _blank_workbook()
    else:
        wb = _blank_workbook()

    if layout == "3_sheets" and not template:
        _fill_three_sheets(wb, result, subtitle, bidder=bidder, owner=owner, target_brand=target_brand or DEFAULT_TARGET_BRAND)
        if getattr(result, "reconciliation", None) and result.reconciliation.has_catalog:
            _fill_reconciliation_sheet(wb, result.reconciliation, result.title, subtitle)
    else:
        _fill_sheets(wb, result, subtitle, template, target_brand=target_brand or DEFAULT_TARGET_BRAND)
        if getattr(result, "topology", None):
            _fill_topology_sheet(wb, result.topology, f"{result.title}——配电拓扑架构树", subtitle)
        if getattr(result, "reconciliation", None) and result.reconciliation.has_catalog:
            _fill_reconciliation_sheet(wb, result.reconciliation, result.title, subtitle)
        if result.components:
            _fill_replacements(wb, result.components, target_brand=target_brand or DEFAULT_TARGET_BRAND)
        if include_changes and changes:
            _fill_changes(wb, changes)

    wb.save(out_path)
    return out_path


def build_project_bom_workbook(project_name: str, jobs: list[dict], out_path: str, target_brand: str = "") -> str:
    """生成全项目跨配电箱的大型集中采购总清单（Global BOM）与成套辅料测算表。"""
    from datetime import datetime
    wb = _blank_workbook()

    comp_map: dict = {}
    all_boxes: list = []
    all_reqs: list = []
    seen_reqs: set = set()

    for job in jobs:
        data = job.get("data") or {}
        boxes = data.get("boxes") or []
        components = data.get("components") or []
        requirements = data.get("requirements") or []

        for b in boxes:
            all_boxes.append({
                "code": b.get("code") or job.get("box_code") or "未编号",
                "name": b.get("name") or "配电箱",
                "ip_rating": b.get("ip_rating") or "IP30",
                "install": b.get("install") or "",
                "location": b.get("location") or "",
                "size": b.get("size") or "",
                "quantity": b.get("quantity") or 1,
                "circuits_count": len(data.get("circuits") or []),
            })

        for c in components:
            name = (c.get("name") or "元器件").strip()
            spec = (c.get("spec") or "").strip()
            unit = (c.get("unit") or "只").strip()
            qty = float(c.get("quantity") or 0)
            key = (name, spec, unit)
            if key not in comp_map:
                comp_map[key] = {"name": name, "spec": spec, "unit": unit, "total": 0.0, "boxes": {}, "notes": set()}

            box_label = c.get("used_in") or (boxes[0].get("code") if boxes else job.get("box_code")) or "通用"
            comp_map[key]["total"] += qty
            comp_map[key]["boxes"][box_label] = comp_map[key]["boxes"].get(box_label, 0) + qty
            if c.get("note"):
                comp_map[key]["notes"].add(c.get("note"))

        for r in requirements:
            r_item = r.get("item") or "设计说明"
            r_content = r.get("content") or ""
            r_key = (r_item, r_content)
            if r_key not in seen_reqs:
                seen_reqs.add(r_key)
                all_reqs.append({"item": r_item, "content": r_content})

    # 收集全项目待核对项：导出不再被存疑项拦住，就更要把它们如实随文件交出，
    # 否则"能导出"就变成了"问题被藏起来"。
    all_unresolved: list[dict] = []
    for job in jobs:
        for u in (job.get("data") or {}).get("uncertainties") or []:
            if not isinstance(u, dict):
                continue
            all_unresolved.append({
                "filename": job.get("filename") or job.get("job_id") or "",
                "location": u.get("location") or "",
                "detail": u.get("detail") or "",
                "severity": (u.get("severity") or "WARNING").upper(),
                "resolved": bool(u.get("resolved")),
            })
    pending_count = sum(1 for u in all_unresolved if not u["resolved"])

    subtitle = (f"项目：{project_name} ｜ 涵盖 {len(jobs)} 份图纸、{len(all_boxes)} 台配电箱 ｜ "
                f"生成时间：{datetime.now():%Y-%m-%d %H:%M}"
                + (f" ｜ ⚠ 有 {pending_count} 处待核对项未确认，见「{_SHEETS['unresolved']}」" if pending_count else ""))

    # 1. Sheet 1: 全项目成套设备报价(汇总)
    ws_summary = wb.active
    ws_cards = wb.create_sheet(_SHEETS["box_cards"])

    # 聚合所有回路与元器件
    all_circuits = []
    all_components = []
    for job in jobs:
        d = job.get("data") or {}
        all_circuits.extend(d.get("circuits") or [])
        all_components.extend(d.get("components") or [])

    # 渲染垂直流水卡片分项明细，获得各箱锚点行号
    card_anchors = _fill_box_cards_detail_sheet(
        ws_cards, project_name, subtitle,
        all_boxes, all_circuits, all_components,
        target_brand=target_brand or DEFAULT_TARGET_BRAND
    )

    # 渲染全项目成套报价汇总表（带超链接锚定卡片）
    _fill_quotation_summary_sheet(
        ws_summary, project_name,
        all_boxes, all_circuits, all_components,
        card_anchors, detail_sheet_title=_SHEETS["box_cards"],
        target_brand=target_brand or DEFAULT_TARGET_BRAND
    )

    # 2. Sheet 3: 全项目采购总清单 (BOM)
    ws1 = wb.create_sheet(_SHEETS["bom"])
    headers1 = ["序号", "元器件名称", "规格型号", "单位", "全项目总采购量", "各配电箱分布明细", "参考单价(元)", "预估合价(元)", "备注"]
    r1 = _setup(ws1, f"【{project_name}】电气元器件集中采购总清单(BOM)", subtitle, headers1, _COLUMNS["bom"])

    def _sort_key(item):
        name = item["name"]
        spec = item["spec"]
        if "塑壳" in name or "MCCB" in spec or "隔离开关" in name:
            rank = 1
        elif "断路器" in name or "微断" in name or "漏电" in name or "MCB" in spec or "RCB" in spec:
            rank = 2
        elif "接触器" in name or "继电器" in name:
            rank = 3
        elif "浪涌" in name or "电表" in name or "互感器" in name or "SPD" in spec:
            rank = 4
        else:
            rank = 5
        return (rank, name, spec)

    sorted_comps = sorted(comp_map.values(), key=_sort_key)
    for i, c in enumerate(sorted_comps, 1):
        q = int(c["total"]) if float(c["total"]) == int(c["total"]) else round(c["total"], 2)
        dist_str = "；".join(f"{b} ({int(k) if float(k) == int(k) else k}{c['unit']})" for b, k in c["boxes"].items())
        notes_str = "；".join(c["notes"])
        curr_row = r1
        r1 = _row(ws1, r1, [i, c["name"], c["spec"], c["unit"], q, dist_str, "", f"=E{curr_row}*G{curr_row}", notes_str], height=32, center_cols=(1, 4))

    # Sheet 2: 集中采购国产化平替对账表 (针对 target_brand)
    from .catalog import analyze_components_replacement
    raw_for_rep = [
        {"name": c["name"], "spec": c["spec"], "quantity": c["total"], "unit": c["unit"], "used_in": "；".join(c["boxes"].keys())}
        for c in sorted_comps
    ]
    rep_analysis = analyze_components_replacement(raw_for_rep, target_brand=target_brand)
    ws_rep = wb.create_sheet(f"{_SHEETS['replacements_prefix']}({target_brand})")
    headers_rep = ["序号", "元器件名称", "原图设计规格", "原厂品牌", f"推荐平替型号({target_brand})", "集中采购总量", "单位", "预计降本", "各配电箱分布", "对标依据与核验说明"]
    rep_sub = (
        f"全项目集中平替目标品牌：{target_brand} ｜ 涉及品种：{len(sorted_comps)} 项 ｜ "
        f"可降本采购总量：{rep_analysis['replaceable_quantity']} 件 ｜ 预计元器件总采购额直降约：{rep_analysis['estimated_overall_saving_pct']}%"
    )
    r_rep = _setup(ws_rep, f"【{project_name}】电气元器件集中采购国产化平替与降本对账表（{target_brand}）", rep_sub, headers_rep,
                   [6, 20, 26, 12, 32, 14, 8, 12, 36, 42])
    save_font = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["saving_text"])
    warn_font = Font(name=_FONT_FAMILY, size=10, bold=True, color=_PALETTE["warning_text"])
    for i, it in enumerate(rep_analysis["items"], 1):
        sav_pct = it.get("estimated_saving_pct")
        sav_status = it.get("saving_status")
        if sav_pct is not None and sav_pct > 0:
            saving_text = f"↓{sav_pct}%"
        elif sav_status == "cost_increase":
            saving_text = "成本增加"
        elif sav_status == "cost_equal":
            saving_text = "平价"
        elif sav_pct is None or sav_status == "missing_price":
            saving_text = "待询价"
        else:
            saving_text = "已最优"

        curr_r = r_rep
        r_rep = _row(ws_rep, r_rep, [
            i, it["name"], it["original_spec"], it["original_brand"],
            it["recommended_model"], it["quantity"], it["unit"],
            saving_text, it["used_in"], it["notes"]
        ], height=32, center_cols=(1, 4, 7, 8))
        if sav_pct is not None and sav_pct > 0:
            ws_rep.cell(row=curr_r, column=8).font = save_font
        elif sav_status == "cost_increase":
            ws_rep.cell(row=curr_r, column=8).font = warn_font

    # Sheet 3: 配电箱成套设备台账
    ws2 = wb.create_sheet(_SHEETS["ledger"])
    headers2 = ["序号", "配电箱编号", "设备名称", "防护等级", "安装方式", "安装位置", "参考尺寸(mm)", "台数", "回路总数", "备注"]
    r2 = _setup(ws2, f"【{project_name}】配电箱/配电柜成套台账", subtitle, headers2, _COLUMNS["ledger"])
    for i, b in enumerate(all_boxes, 1):
        r2 = _row(ws2, r2, [i, b["code"], b["name"], b["ip_rating"], b["install"], b["location"], b["size"], b["quantity"], b["circuits_count"], ""], height=26, center_cols=(1, 4, 8, 9))

    # Sheet 3: 成套外壳与辅材概算
    ws3 = wb.create_sheet(_SHEETS["aux_estimate"])
    headers3 = ["序号", "配电箱编号", "回路数", "外壳估算形式/尺寸", "箱壳估算基准(元)", "铜排母线及端子辅料(元)", "装配测试工时费(元)", "单台成套制造辅价(元)", "台数", "小计(元)"]
    r3 = _setup(ws3, f"【{project_name}】配电箱成套辅料与柜体制造费测算", "基于工程经验的辅材、箱体钣金与组装试验费估算模板（单价公式可按项目调整）", headers3, _COLUMNS["aux_estimate"])
    for i, b in enumerate(all_boxes, 1):
        c_cnt = max(1, b["circuits_count"])
        box_base = (_ESTIMATION["enclosure_base_surface"] if "明装" in b["install"]
                    else _ESTIMATION["enclosure_base_floor"])
        acc_base = c_cnt * _ESTIMATION["accessory_per_circuit"]
        labor_base = c_cnt * _ESTIMATION["labor_per_circuit"] + _ESTIMATION["labor_fixed"]
        curr_row = r3
        r3 = _row(ws3, r3, [i, b["code"], c_cnt, b["size"] or f"{c_cnt}极外壳", box_base, acc_base, labor_base, f"=E{curr_row}+F{curr_row}+G{curr_row}", b["quantity"], f"=H{curr_row}*I{curr_row}"], height=26, center_cols=(1, 3, 9))

    # Sheet 4: 项目统一技术规范与说明
    ws4 = wb.create_sheet(_SHEETS["project_requirements"])
    headers4 = ["序号", "项目/分类", "要求内容及设计原则"]
    r4 = _setup(ws4, f"【{project_name}】全项目电气技术要求汇总", "汇集该项目全部图纸的设计说明、分断能力标准及特殊保护原则", headers4, _COLUMNS["project_requirements"])
    for i, req in enumerate(all_reqs, 1):
        r4 = _row(ws4, r4, [i, req["item"], req["content"]], height=32, center_cols=(1,))

    # Sheet 5: 全项目配电系统拓扑树与电气设备分级
    from .assemble import build_distribution_topology
    from .schema import Box, Circuit
    proj_boxes = []
    proj_circuits = []
    for job in jobs:
        d = job.get("data") or {}
        for b in d.get("boxes") or []:
            try:
                proj_boxes.append(Box.model_validate(b))
            except Exception:
                pass
        for c in d.get("circuits") or []:
            try:
                proj_circuits.append(Circuit.model_validate(c))
            except Exception:
                pass

    proj_topology = build_distribution_topology(proj_boxes, proj_circuits)
    if proj_topology:
        _fill_topology_sheet(
            wb, proj_topology,
            f"【{project_name}】全项目配电系统拓扑树与电气设备分级",
            f"跨图纸层级关联：涵盖全项目 {len(all_boxes)} 台配电柜/箱及一二次控制原理图"
        )

    # 最后一页：待核对项总表（有则列出，没有也给出说明，避免读者误以为漏了）
    ws_u = wb.create_sheet(_SHEETS["unresolved"])
    headers_u = ["序号", "来源图纸", "位置", "待核对问题", "级别", "状态"]
    r_u = _setup(
        ws_u, f"【{project_name}】全项目待核对项",
        (f"跨 {len(jobs)} 份图纸汇总 {len(all_unresolved)} 条，其中未确认 {pending_count} 条"
         "；导出不被这些项遮挡，但报价前应逐条核对"),
        headers_u, _COLUMNS["unresolved"])
    if all_unresolved:
        # 未确认的排前面，同级别内按图纸名稳定排序
        all_unresolved.sort(key=lambda u: (u["resolved"], u["filename"], u["severity"]))
        for i, u in enumerate(all_unresolved, 1):
            r_u = _row(ws_u, r_u,
                       [i, u["filename"], u["location"], u["detail"], u["severity"],
                        "已确认" if u["resolved"] else "未确认"],
                       height=30, center_cols=(1, 5, 6))
    else:
        _row(ws_u, r_u, ["", "", "", "本项目的图纸没有待核对项", "", ""], height=30, center_cols=(1,))

    wb.save(out_path)
    return out_path


def build_custom_table_workbook(title: str, headers: list[str], rows: list[list[Any]], subtitle: str = "") -> openpyxl.Workbook:
    """构建自定义/AI智能对话动态导出的 Excel 工作簿。
    
    支持根据用户提问内容自由生成工整专业的高颜值表格，支持数字自动对齐与列宽自适应。
    """
    from datetime import datetime

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = (title[:28] if title else "数据统计表").replace("/", "_").replace("\\", "_")

    col_cnt = max(len(headers), 1)

    # 1. 标题行
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_cnt)
    c1 = ws.cell(row=1, column=1, value=title or "数据整理统计表")
    c1.font = TITLE_FONT
    c1.alignment = CENTER
    ws.row_dimensions[1].height = 32

    # 2. 副标题 / 说明
    sub_text = subtitle or f"生成时间：{datetime.now():%Y-%m-%d %H:%M} ｜ 由 AI 配电箱成套专家智能整理生成"
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_cnt)
    c2 = ws.cell(row=2, column=1, value=sub_text)
    c2.font = SUB_FONT
    c2.alignment = CENTER
    ws.row_dimensions[2].height = 20

    # 3. 表头
    for j, h in enumerate(headers, start=1):
        cell = ws.cell(row=3, column=j, value=h)
        cell.font = HDR_FONT
        cell.fill = HDR_FILL
        cell.alignment = CENTER
        cell.border = BORDER
    ws.row_dimensions[3].height = 26

    # 4. 数据行
    col_max_lens = [len(str(h).encode("gbk", "ignore")) for h in headers]
    for r_idx, row_vals in enumerate(rows, start=4):
        ws.row_dimensions[r_idx].height = 24
        for c_idx, val in enumerate(row_vals, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=sanitize_excel_value(val))
            cell.font = CELL_FONT
            cell.border = BORDER

            # 文本与数字对齐
            is_number = isinstance(val, (int, float))
            if not is_number and isinstance(val, str):
                cleaned_val = val.replace(",", "").strip()
                if cleaned_val.replace(".", "", 1).isdigit() and len(cleaned_val) < 12:
                    is_number = True

            if c_idx == 1 and not is_number:
                cell.alignment = CENTER
            elif is_number:
                cell.alignment = RIGHT
                if isinstance(val, float):
                    cell.number_format = "#,##0.00"
            else:
                cell.alignment = LEFT

            val_len = len(str(val or "").encode("gbk", "ignore"))
            if c_idx - 1 < len(col_max_lens):
                col_max_lens[c_idx - 1] = max(col_max_lens[c_idx - 1], val_len)

    # 5. 设置列宽
    for j, max_len in enumerate(col_max_lens, start=1):
        width = max(10, min(max_len + 4, 45))
        ws.column_dimensions[get_column_letter(j)].width = width

    return wb

