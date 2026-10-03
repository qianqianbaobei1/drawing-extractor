# -*- coding: utf-8 -*-
"""Model observations and the assembled quotation have separate schemas."""
from __future__ import annotations
import re
from enum import Enum
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROMPT_VERSION = "3.0"
CONTRACT_VERSION = "3.0"


class EvidenceType(str, Enum):
    TEXT = "TEXT"                      # 矢量文字或高精 OCR 文本事实
    SYMBOL = "SYMBOL"                  # 图例符号
    LINE = "LINE"                      # 母线或物理连接线
    GEOMETRY = "GEOMETRY"              # 柜体/走廊几何线框
    TABLE_CELL = "TABLE_CELL"          # 系统图表格单元格


class InferenceType(str, Enum):
    MODEL = "MODEL"                    # 多模态大模型推断
    RULE = "RULE"                      # 规范规则/公式计算推断
    HUMAN = "HUMAN"                    # 人工复核确认


class ReviewStatus(str, Enum):
    UNASSESSED = "UNASSESSED"          # 初始未核验状态 (Fail-Closed)
    CONFIRMED = "CONFIRMED"            # 物理证据确凿且校验无误
    ACCEPTED = "ACCEPTED"              # 综合证据满足规范放行
    REVIEW = "REVIEW"                  # 存疑/缺证据/弱冲突，需人工确认
    ERROR = "ERROR"                    # 明确结构性矛盾，系统阻断


class Evidence(BaseModel):
    evidence_id: str = Field(..., description="唯一证据ID")
    evidence_type: str = Field(EvidenceType.TEXT.value, description="物理证据类别 (严禁推断)")
    raw_content: str = Field("", description="提取的原始文字或图例类别")
    bbox: Optional["BBox"] = Field(None, description="图元规范页面归一化坐标")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="证据可信度")


class GroundedField(BaseModel):
    """带证据链支撑的结构化字段载体 (Claim)"""
    value: Any = None
    raw_value: str = ""
    value_evidence_ids: List[str] = Field(default_factory=list, description="证明数值存在的物理证据ID列表")
    relation_evidence_ids: List[str] = Field(default_factory=list, description="证明拓扑归属关系的几何/连线证据ID列表")
    inference_ids: List[str] = Field(default_factory=list, description="推断记录引用ID列表")
    confidence: Optional[float] = Field(None, description="未校准前为 None，严禁默认 1.0 (Fail-Closed)")
    review_status: str = Field(ReviewStatus.UNASSESSED.value, description="默认必须为 UNASSESSED (Fail-Closed)")
    notes: str = Field("", description="备注或核验说明")

    @property
    def evidence_ids(self) -> List[str]:
        """向后兼容历史 evidence_ids 访问"""
        return self.value_evidence_ids + self.relation_evidence_ids


GroundedClaim = GroundedField  # V3.5 规范别名兼容


# 字段级证据政策表（Field-Level Evidence Policy）：严格定义字段允许的证据类型
# 核心原则：防凭空编造！位置、断路器、电缆型号绝对禁止纯 MODEL 推测
FIELD_EVIDENCE_POLICY: dict[str, set[str]] = {
    # 箱体核心字段
    "box.code": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value},
    "box.location": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value}, # 严禁 MODEL 编造安装位置
    "box.ip_rating": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value},
    # 回路核心字段
    "circuit.circuit_no": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value}, # 回路编号严禁推测
    "circuit.breaker": {EvidenceType.TEXT.value, EvidenceType.SYMBOL.value, EvidenceType.TABLE_CELL.value}, # 断路器规格严禁 MODEL
    "circuit.cable": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value}, # 电缆型号严禁 MODEL
    # 允许规则或模型推断的辅助字段
    "circuit.phase": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value, "RULE", "RULE_INFERENCE"},
    "circuit.power_kw": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value, "RULE", "RULE_INFERENCE"},
    "circuit.current_a": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value, "RULE", "RULE_INFERENCE"},
    "circuit.load_name": {EvidenceType.TEXT.value, EvidenceType.TABLE_CELL.value, "MODEL", "MODEL_INFERENCE"}, # 允许语义推断用途
}


def validate_field_evidence(field_path: str, evidence_type: str) -> tuple[bool, str]:
    """校验字段是否符合证据政策要求。返回 (is_valid, violation_message)。"""
    allowed = FIELD_EVIDENCE_POLICY.get(field_path)
    if not allowed:
        return True, ""
    norm_type = str(evidence_type).upper().strip()
    if norm_type not in allowed:
        return False, (
            f"字段 '{field_path}' 违反证据政策：检测到证据/推断类型 '{evidence_type}'，"
            f"但该字段强制要求使用 [{', '.join(sorted(allowed))}] 物理事实证据，严禁无据臆测！"
        )
    return True, ""


def normalize_code(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


class BBox(BaseModel):
    """图纸页内的归一化坐标(0-1)，用于图-表联动与核对定位。

    模型给不出位置时整个字段为 None，程序不猜坐标。多页图纸靠 page 指明是哪一页，
    否则前端只能默认第 1 页，横向图纸多的项目定位就全错。
    """

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    page: int = Field(1, ge=1)

    @model_validator(mode="after")
    def clip_out_of_page(self):
        """坐标越界时裁进页面内；裁无可裁则视为未定位。"""
        if self.x + self.w > 1:
            self.w = round(1 - self.x, 4)
        if self.y + self.h > 1:
            self.h = round(1 - self.y, 4)
        return self

    @property
    def usable(self) -> bool:
        return self.w >= 0.01 and self.h >= 0.01


class Uncertainty(BaseModel):
    """待人工核对项。

    source 区分来源，是修正历史数据的关键标记：
    - "model"：模型在图纸上看到但拿不准的事实，修完图纸才会变；
    - "program"：assemble/checker 根据当前数据算出来的告警，数据一变就应重算；
    - ""：旧导出遗留，来源不明。
    导出到 Excel 时两类分开放，避免下次读回时把程序告警当成模型事实。
    """

    location: str = Field("", description="位置，如 WL3 回路 / 箱体图注")
    detail: str = Field("", description="具体问题")
    bbox: Optional[BBox] = Field(None, description="图纸上的位置，定位不到留空")
    resolved: bool = Field(False, description="用户是否已确认")
    source: str = Field("", description="model / program / 空")
    severity: str = Field("WARNING", description="ERROR / WARNING / INFO 三级门禁分类")

    @classmethod
    def from_text(cls, text: str) -> "Uncertainty":
        """程序生成的告警只有一行文本，按“位置：问题”拆开，供界面分栏显示。

        旧 Excel 回读时已确认项带有"（已确认）"前缀（见 extractor/excel.py 的导出
        写法）：识别并剥离该前缀，同时恢复 resolved=True，避免确认标记丢失。
        """
        text = text.strip()
        resolved = False
        if text.startswith("（已确认）"):
            resolved = True
            text = text[len("（已确认）"):].strip()
        severity = "WARNING"
        if any(tag in text for tag in ("【错误】", "[ERROR]", "【严重】", "【拒识】", "【证据违规】", "【范围缺失】")):
            severity = "ERROR"
        elif any(tag in text for tag in ("【提示】", "[INFO]", "【建议】")):
            severity = "INFO"
        head, sep, tail = text.partition("：")
        if sep and len(head) <= 40:
            return cls(location=head.strip(), detail=tail.strip(), resolved=resolved, severity=severity)
        return cls(location="", detail=text, resolved=resolved, severity=severity)

    @property
    def text(self) -> str:
        """退回“位置：问题”的单行写法，Excel 与旧接口沿用。"""
        if self.location and self.detail:
            return f"{self.location}：{self.detail}"
        return self.location or self.detail

    def __str__(self) -> str:
        return self.text


class Box(BaseModel):
    code: str = Field("", description="设备编号,如 2ALE / CD1")
    name: str = Field("", description="设备名称,如 应急照明配电箱")
    ip_rating: str = Field("", description="防护等级,如 IP30")
    install: str = Field("", description="安装方式,如 底边距地1.5m明装")
    location: str = Field("", description="安装位置")
    size: str = Field("", description="参考尺寸")
    quantity: int = Field(1, description="数量(台)")
    note: str = Field("", description="备注")
    claims: Dict[str, GroundedField] = Field(default_factory=dict, description="带证据链支撑的结构化字段字典")

    @field_validator("code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        return normalize_code(value)


class Circuit(BaseModel):
    box: str = Field("", description="所属配电箱编号")
    phase: str = Field("", description="相序,如 L1/L2/L3/L123")
    breaker: str = Field("", description="空气开关/断路器型号")
    contactor: str = Field("", description="交流接触器")
    ct: str = Field("", description="电流互感器")
    thermal: str = Field("", description="热继电器")
    power_kw: str = Field("", description="设备容量kW")
    circuit_no: str = Field("", description="回路编号")
    cable: str = Field("", description="导线型号及敷设")
    current_a: str = Field("", description="计算电流A")
    load_name: str = Field("", description="回路名称/用电设备")
    secondary_ref: str = Field("", description="二次图编号")
    start_method: str = Field("", description="启动方式")
    note: str = Field("", description="备注")
    bbox: Optional[BBox] = Field(None, description="该回路在图纸页内的归一化位置，定位不到留空")
    structured_breaker: Optional[Dict[str, Any]] = Field(None, description="结构化清洗后的断路器参数")
    structured_cable: Optional[Dict[str, Any]] = Field(None, description="结构化清洗后的线缆参数")
    claims: Dict[str, GroundedField] = Field(default_factory=dict, description="带证据链支撑的结构化字段字典")

    @field_validator("box")
    @classmethod
    def clean_box(cls, value: str) -> str:
        return normalize_code(value)


class Component(BaseModel):
    name: str = Field("", description="元器件名称")
    spec: str = Field("", description="规格型号")
    unit: str = Field("", description="单位")
    quantity: float = Field(0, allow_inf_nan=False, description="数量")
    used_in: str = Field("", description="用于箱体/回路")
    note: str = Field("", description="备注")


class Requirement(BaseModel):
    item: str = Field("", description="项目")
    content: str = Field("", description="要求内容")


class ExtraDevice(BaseModel):
    """An observed device that is not represented by a circuit field."""
    name: str
    spec: str = ""
    unit: str = ""
    quantity: float = Field(gt=0, allow_inf_nan=False)
    used_in: str = ""
    note: str = ""


class CatalogItem(BaseModel):
    sheet_no: str = Field("", description="图纸编号, 如 01B-03")
    sheet_title: str = Field("", description="图纸名称, 如 动力配电箱系统图(三)")
    declared_panels: List[str] = Field(default_factory=list, description="本图声明包含的配电箱列表")
    matched_panels: List[str] = Field(default_factory=list, description="实际已提取到的配电箱")
    missing_panels: List[str] = Field(default_factory=list, description="缺失未进流水线的配电箱")
    status: str = Field("COVERED", description="COVERED(全部覆盖) / PARTIAL(部分覆盖) / MISSING(整张图幅缺失)")


class CatalogReconciliation(BaseModel):
    has_catalog: bool = Field(False, description="图纸中是否检测到目录清单")
    catalog_source: str = Field("", description="目录来源，如 CAD文字/PDF目录页")
    total_declared_panels: int = Field(0, description="目录声明的配电箱总数")
    covered_count: int = Field(0, description="已提取覆盖的配电箱数")
    missing_count: int = Field(0, description="遗漏未提取的配电箱数")
    coverage_rate: float = Field(1.0, description="覆盖率 0.0~1.0")
    items: List[CatalogItem] = Field(default_factory=list)
    missing_box_codes: List[str] = Field(default_factory=list, description="遗漏的配电箱编号列表")


class RawExtraction(BaseModel):
    """Only facts observed by the model; no model-computed totals or title."""
    model_config = ConfigDict(extra="forbid")
    boxes: List[Box]
    circuits: List[Circuit]
    extra_devices: List[ExtraDevice]
    requirements: List[Requirement]
    uncertainties: List[Uncertainty]
    catalog_items: List[CatalogItem] = Field(default_factory=list)
    reconciliation: Optional[CatalogReconciliation] = Field(default=None)


class DistributionNode(BaseModel):
    """配电系统拓扑树节点：支持 项目 -> 一级总配电柜 -> 二级分配电箱 -> 一次出线回路 / 二次控制原理图。"""
    id: str = Field(..., description="唯一节点ID")
    code: str = Field("", description="设备/箱体/回路编号")
    name: str = Field("", description="设备名称或回路名称")
    node_type: str = Field("box", description="cabinet(一级总配电柜) / box(二级分配电箱) / circuit(出线支路) / secondary(二次控制原理图)")
    parent_code: str = Field("", description="上级供电设备编号")
    feed_circuit: str = Field("", description="上级供电出线回路编号")
    circuits_count: int = Field(0, description="下属回路总数")
    power_kw: str = Field("", description="设备容量或回路容量(kW)")
    secondary_ref: str = Field("", description="关联二次控制原理图编号")
    children: List[DistributionNode] = Field(default_factory=list, description="子节点")
    note: str = Field("", description="工程备注")


class AssembledMeta(BaseModel):
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    contract_version: str = CONTRACT_VERSION


class ExtractionResult(BaseModel):
    title: str = Field("配电箱元器件清单(报价用)", description="清单标题")
    boxes: List[Box] = Field(default_factory=list)
    circuits: List[Circuit] = Field(default_factory=list)
    components: List[Component] = Field(default_factory=list)
    requirements: List[Requirement] = Field(default_factory=list)
    uncertainties: List[Uncertainty] = Field(default_factory=list, description="图纸字迹不清、需人工核对的项")
    topology: List[DistributionNode] = Field(default_factory=list, description="配电系统拓扑树")
    reconciliation: Optional[CatalogReconciliation] = Field(None, description="图纸目录对账审计结果")
    evidence_store: Dict[str, Evidence] = Field(default_factory=dict, description="证据存储字典")
    meta: AssembledMeta = Field(default_factory=AssembledMeta)

