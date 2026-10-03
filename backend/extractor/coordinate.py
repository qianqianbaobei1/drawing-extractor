# -*- coding: utf-8 -*-
"""规范页面坐标系统与仿射反投影变换链 (Coordinate Transform Chain).

原则：Tile 切片仅为模型观察视口 (Viewport)，绝不能成为最终业务实体的绝对坐标。
所有在 Tile 切片或光栅化图像上定位的 BBox，必须经由严密的仿射变换矩阵（Affine Matrix）
反投影回 Canonical Page Coordinate（基于 72 DPI PDF Point，原点在页面左上角）。
"""

from __future__ import annotations
from enum import Enum
from typing import Optional, Tuple
from pydantic import BaseModel, Field


class CoordinateSpace(str, Enum):
    CAD_MODEL = "cad_model"        # CAD 绘图世界空间 (Drawing Units, 原点通常居中或自定义)
    CANONICAL_PAGE = "canonical"    # 规范页面绝对空间 (72 DPI, 1 pt = 1/72 inch, 原点左上角)
    RASTER_IMAGE = "raster_image"   # 页面光栅化像素空间 (如 300 DPI 渲染图)
    TILE_VIEWPORT = "tile_viewport" # 局部切片局部像素空间 (含 Padding/Resize)


class AffineMatrix(BaseModel):
    """2D 仿射变换矩阵:
    [ x' ]   [ a  c  tx ] [ x ]
    [ y' ] = [ b  d  ty ] [ y ]
    [ 1  ]   [ 0  0  1  ] [ 1 ]
    """
    a: float = 1.0
    b: float = 0.0
    c: float = 0.0
    d: float = 1.0
    tx: float = 0.0
    ty: float = 0.0

    def apply_point(self, x: float, y: float) -> tuple[float, float]:
        """前向仿射变换"""
        x_prime = self.a * x + self.c * y + self.tx
        y_prime = self.b * x + self.d * y + self.ty
        return round(x_prime, 4), round(y_prime, 4)

    def inverse(self) -> AffineMatrix:
        """计算逆变换矩阵，用于从局部切片像素反投影回规范页面点"""
        det = self.a * self.d - self.b * self.c
        if abs(det) < 1e-9:
            raise ValueError("矩阵奇异不可逆 (Determinant ≈ 0)")
        inv_det = 1.0 / det
        inv_a = self.d * inv_det
        inv_b = -self.b * inv_det
        inv_c = -self.c * inv_det
        inv_d = self.a * inv_det
        inv_tx = (self.c * self.ty - self.d * self.tx) * inv_det
        inv_ty = (self.b * self.tx - self.a * self.ty) * inv_det
        return AffineMatrix(
            a=inv_a, b=inv_b, c=inv_c, d=inv_d,
            tx=inv_tx, ty=inv_ty
        )


class CanonicalBBox(BaseModel):
    """规范页面绝对边界框 (以 72 DPI PDF Point 为物理基准，原点在页面左上角)"""
    x: float = Field(..., description="规范页面左上角 X (pt)")
    y: float = Field(..., description="规范页面左上角 Y (pt)")
    w: float = Field(..., description="规范页面宽度 (pt)")
    h: float = Field(..., description="规范页面高度 (pt)")
    page_index: int = Field(0, description="页面索引 (0-based)")

    @property
    def x2(self) -> float:
        return round(self.x + self.w, 4)

    @property
    def y2(self) -> float:
        return round(self.y + self.h, 4)

    def to_normalized(self, page_width_pt: float, page_height_pt: float) -> Tuple[float, float, float, float]:
        """将页面绝对 pt 转换为 0~1 的归一化浮点坐标，适配现有前端"""
        if page_width_pt <= 0 or page_height_pt <= 0:
            return 0.0, 0.0, 0.0, 0.0
        return (
            round(max(0.0, min(1.0, self.x / page_width_pt)), 4),
            round(max(0.0, min(1.0, self.y / page_height_pt)), 4),
            round(max(0.0, min(1.0, self.w / page_width_pt)), 4),
            round(max(0.0, min(1.0, self.h / page_height_pt)), 4),
        )


class CoordinateTransformChain(BaseModel):
    """图纸切片反投影变换器"""
    source_space: CoordinateSpace = CoordinateSpace.TILE_VIEWPORT
    target_space: CoordinateSpace = CoordinateSpace.CANONICAL_PAGE
    matrix: AffineMatrix = Field(default_factory=AffineMatrix)
    page_width_pt: float = 842.0   # 默认 A4 横向基准
    page_height_pt: float = 595.0
    page_index: int = 0

    @classmethod
    def from_tile_crop(
        cls,
        page_index: int,
        page_width_pt: float,
        page_height_pt: float,
        tile_crop_pt: tuple[float, float, float, float], # (crop_x, crop_y, crop_w, crop_h) 在规范页面上的区域
        tile_pixel_w: int,
        tile_pixel_h: int,
    ) -> CoordinateTransformChain:
        """构建切片像素到规范页面坐标的反投影变换器。
        
        从 Tile 局部像素 (0, 0) ~ (tile_pixel_w, tile_pixel_h)
        映射到规范页面 (crop_x, crop_y) ~ (crop_x + crop_w, crop_y + crop_h)
        """
        crop_x, crop_y, crop_w, crop_h = tile_crop_pt
        scale_x = crop_w / max(1, tile_pixel_w)
        scale_y = crop_h / max(1, tile_pixel_h)

        # 仿射变换: X_page = scale_x * X_tile + crop_x
        #           Y_page = scale_y * Y_tile + crop_y
        matrix = AffineMatrix(
            a=scale_x, b=0.0,
            c=0.0, d=scale_y,
            tx=crop_x, ty=crop_y,
        )

        return cls(
            source_space=CoordinateSpace.TILE_VIEWPORT,
            target_space=CoordinateSpace.CANONICAL_PAGE,
            matrix=matrix,
            page_width_pt=page_width_pt,
            page_height_pt=page_height_pt,
            page_index=page_index,
        )

    def transform_bbox_to_page(
        self,
        tile_x: float,
        tile_y: float,
        tile_w: float,
        tile_h: float,
    ) -> CanonicalBBox:
        """将局部切片像素坐标反投影为规范页面绝对坐标"""
        x_pt, y_pt = self.matrix.apply_point(tile_x, tile_y)
        w_pt = round(tile_w * self.matrix.a, 4)
        h_pt = round(tile_h * self.matrix.d, 4)
        return CanonicalBBox(
            x=x_pt,
            y=y_pt,
            w=w_pt,
            h=h_pt,
            page_index=self.page_index,
        )
