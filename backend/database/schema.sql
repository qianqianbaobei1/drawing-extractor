-- ==============================================================================
-- DEPRECATED（已废弃）：本文件未被任何代码引用。
-- 实际持久化走纯 JSON 落盘（backend/store.py），没有 SQLite/PostgreSQL 层。
-- 保留仅作历史参考，不要按此建表、不要在其上继续开发。
-- ==============================================================================
-- ==============================================================================
-- 工业成套电气图纸解析、物料管控与自动组价系统 - 生产级数据库 DDL Schema
-- 支持数据库: PostgreSQL 14+ (推荐含 pgvector / PostGIS) / SQLite 3.35+
-- ==============================================================================

-- 1. 项目主表 (Projects)
CREATE TABLE IF NOT EXISTS projects (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    project_code VARCHAR(64) UNIQUE,
    client_name VARCHAR(255),
    designer_institute VARCHAR(255),
    location VARCHAR(128),
    total_buildings INT DEFAULT 1,
    status VARCHAR(32) DEFAULT 'active', -- active, archived, bidding, won
    settings_json JSON,                  -- 项目专属性定价规则、默认品牌、折率等
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_code ON projects(project_code);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);


-- 2. 图纸工程表 (Drawings)
CREATE TABLE IF NOT EXISTS drawings (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename VARCHAR(255) NOT NULL,
    file_hash VARCHAR(64) NOT NULL,     -- SHA256 / MD5 用于秒传与图纸缓存复用
    file_type VARCHAR(16) NOT NULL,     -- dwg, dxf, pdf
    file_size_bytes BIGINT,
    sheet_name VARCHAR(128),            -- 如：01-制丝工房-电气照明921(出图)_t3
    pages_count INT DEFAULT 1,
    cad_scale DOUBLE PRECISION DEFAULT 1.0,
    cad_origin_x DOUBLE PRECISION DEFAULT 0.0,
    cad_origin_y DOUBLE PRECISION DEFAULT 0.0,
    status VARCHAR(32) DEFAULT 'pending', -- pending, slicing, parsed, completed, error
    error_message TEXT,
    metadata_json JSON,                 -- DWG 图层统计、线型、文字样式元数据
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_drawings_project ON drawings(project_id);
CREATE INDEX IF NOT EXISTS idx_drawings_hash ON drawings(file_hash);


-- 3. CAD 物理图元表与空间索引 (CAD Entities)
-- 用于毫秒级局部框选、文字-回路拓扑关联合成、OCR与CAD原文字体比对
CREATE TABLE IF NOT EXISTS cad_entities (
    id BIGSERIAL PRIMARY KEY,
    drawing_id VARCHAR(36) NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    page_num INT DEFAULT 1,
    entity_type VARCHAR(32) NOT NULL,    -- TEXT, MTEXT, LINE, LWPOLYLINE, INSERT
    layer VARCHAR(128) NOT NULL,
    color_code INT,
    text_content TEXT,                   -- 图元文字内容（若为线段则为空）
    bbox_min_x DOUBLE PRECISION NOT NULL,
    bbox_min_y DOUBLE PRECISION NOT NULL,
    bbox_max_x DOUBLE PRECISION NOT NULL,
    bbox_max_y DOUBLE PRECISION NOT NULL,
    spatial_grid_id INT,                 -- 空间网格离散化ID (网格尺寸35000)
    raw_properties_json JSON
);

CREATE INDEX IF NOT EXISTS idx_cad_entities_dwg_page ON cad_entities(drawing_id, page_num);
CREATE INDEX IF NOT EXISTS idx_cad_entities_grid ON cad_entities(spatial_grid_id);
CREATE INDEX IF NOT EXISTS idx_cad_entities_bbox ON cad_entities(bbox_min_x, bbox_min_y, bbox_max_x, bbox_max_y);


-- 4. 配电箱柜物理与电气特征表 (Boxes)
CREATE TABLE IF NOT EXISTS boxes (
    id VARCHAR(36) PRIMARY KEY,
    drawing_id VARCHAR(36) NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    box_code VARCHAR(64) NOT NULL,       -- 如: 01ATPY03, 01AL2-1A
    box_name VARCHAR(128),               -- 如: 消防双电源配电箱
    box_type VARCHAR(64),                -- JXF, GGD, GCK, MNS, XL-21, XXM, 照明配电箱
    install_type VARCHAR(64),            -- 挂墙明装, 嵌墙暗装, 落地安装
    ip_rating VARCHAR(32) DEFAULT 'IP30',-- IP30, IP44, IP55, IP65
    dimensions VARCHAR(64),              -- 800x600x200 (宽x高x深)
    width_mm INT,
    height_mm INT,
    depth_mm INT,
    pe_kw DOUBLE PRECISION,              -- 有功功率 Pe (kW)
    pj_kw DOUBLE PRECISION,              -- 计算有功功率 Pj (kW)
    ij_a DOUBLE PRECISION,               -- 计算电流 Ij (A)
    kx DOUBLE PRECISION,                 -- 需要系数 Kx
    cos_phi DOUBLE PRECISION,            -- 功率因数 cosφ
    incoming_source VARCHAR(255),        -- 进线电源来自何处
    is_firefighting BOOLEAN DEFAULT FALSE, -- 消防负荷标识
    page_num INT DEFAULT 1,
    bbox_min_x DOUBLE PRECISION,
    bbox_min_y DOUBLE PRECISION,
    bbox_max_x DOUBLE PRECISION,
    bbox_max_y DOUBLE PRECISION,
    status VARCHAR(32) DEFAULT 'unverified', -- unverified, verified, modified
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_boxes_drawing ON boxes(drawing_id);
CREATE INDEX IF NOT EXISTS idx_boxes_code ON boxes(box_code);
CREATE INDEX IF NOT EXISTS idx_boxes_type ON boxes(box_type);


-- 5. 配电回路明细表 (Circuits)
CREATE TABLE IF NOT EXISTS circuits (
    id VARCHAR(36) PRIMARY KEY,
    box_id VARCHAR(36) NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    circuit_no VARCHAR(64) NOT NULL,     -- 如: N1, N2, 1WL1, 2W1, 备用
    circuit_type VARCHAR(32) DEFAULT 'outgoing', -- incoming(进线), outgoing(出线), spare(备用), bus_tie(母联)
    phase VARCHAR(16),                   -- L1, L2, L3, L1,L2,L3, 三相
    breaker_spec VARCHAR(128) NOT NULL,  -- 原文标注: MCB-C16A/1P, MCCB-160MA/125A/3P
    breaker_frame_a INT,                 -- 壳架等级 (A): 100, 160, 250, 400
    breaker_trip_a INT,                  -- 脱扣额定电流 (A): 16, 20, 25, 32, 63, 100, 125
    breaker_poles VARCHAR(8),            -- 1P, 2P, 3P, 4P
    breaker_breaking_ka INT,             -- 额定短路分断能力: 6, 10, 35, 50
    cable_spec VARCHAR(128),             -- WDZN-BYJ-3x2.5, YJV-4x35+1x16
    cable_cores INT,                     -- 线芯数
    cable_cross_section DOUBLE PRECISION,-- 导体单芯标称截面 (mm²)
    install_method VARCHAR(64),          -- SC20/WC/CC, CT, MR
    power_kw DOUBLE PRECISION,           -- 回路设计功率 (kW)
    calculated_current_a DOUBLE PRECISION,-- 回路计算电流 (A)
    load_name VARCHAR(128),              -- 负载名称: 应急照明, 喷淋泵, 卷帘门
    load_type VARCHAR(64),               -- lighting, socket, motor, hvac, fire
    is_firefighting BOOLEAN DEFAULT FALSE, -- 消防负荷 (过载只报不跳)
    sort_order INT DEFAULT 0,            -- 系统图从左至右排序
    raw_annotation TEXT,                 -- 图纸提取完整原文
    page_num INT DEFAULT 1,
    status VARCHAR(32) DEFAULT 'unverified',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_circuits_box ON circuits(box_id);
CREATE INDEX IF NOT EXISTS idx_circuits_no ON circuits(circuit_no);
CREATE INDEX IF NOT EXISTS idx_circuits_breaker ON circuits(breaker_spec);


-- 6. 元器件物理物料明细表 (Components)
CREATE TABLE IF NOT EXISTS components (
    id VARCHAR(36) PRIMARY KEY,
    box_id VARCHAR(36) NOT NULL REFERENCES boxes(id) ON DELETE CASCADE,
    circuit_id VARCHAR(36) REFERENCES circuits(id) ON DELETE SET NULL,
    category VARCHAR(64) NOT NULL,       -- MCB, MCCB, ACB, RCBO, ATS, SPD, KM, FR, CT, FUSE, METER, ARREST
    standard_name VARCHAR(128) NOT NULL, -- 微型断路器, 塑壳断路器, 双电源转换开关
    spec VARCHAR(128) NOT NULL,          -- 规格型号
    brand VARCHAR(64) DEFAULT '待定',    -- 施耐德, ABB, 西门子, 正泰, 德力西, 良信
    poles VARCHAR(8),                    -- 1P, 2P, 3P, 4P
    rated_current_a INT,                 -- 额定电流 (A)
    breaking_capacity_ka INT,            -- 分断能力 (kA)
    accessories VARCHAR(255),            -- 附件 (分励MX, 辅助触点OF, 报警触点SD)
    quantity INT NOT NULL DEFAULT 1,
    unit VARCHAR(16) DEFAULT '台',       -- 台, 只, 套
    unit_price NUMERIC(12, 2) DEFAULT 0.00,  -- 采购单价
    list_price NUMERIC(12, 2) DEFAULT 0.00,  -- 目录面价
    discount_rate NUMERIC(5, 4) DEFAULT 1.0000, -- 折扣率
    total_price NUMERIC(12, 2) DEFAULT 0.00, -- 单项合价
    cost_source VARCHAR(64) DEFAULT 'unpriced', -- unpriced, catalog, 1688, historical, ai_estimate
    review_status VARCHAR(32) DEFAULT 'pending',-- pending, approved, modified
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_components_box ON components(box_id);
CREATE INDEX IF NOT EXISTS idx_components_cat ON components(category);
CREATE INDEX IF NOT EXISTS idx_components_spec ON components(spec);


-- 7. 技术规范与审图要求表 (Requirements)
CREATE TABLE IF NOT EXISTS requirements (
    id VARCHAR(36) PRIMARY KEY,
    drawing_id VARCHAR(36) NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    box_id VARCHAR(36) REFERENCES boxes(id) ON DELETE SET NULL,
    title VARCHAR(128) NOT NULL,         -- 标题 (如: 进线总则、消防保护规范)
    content TEXT NOT NULL,               -- 具体设计说明条文
    category VARCHAR(64) DEFAULT 'general', -- general, firefighting, busbar, enclosure
    is_mandatory BOOLEAN DEFAULT FALSE,  -- 是否涉及强制性标准 (GB强条)
    page_num INT DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_requirements_drawing ON requirements(drawing_id);


-- 8. 存疑审查与不确定项表 (Uncertainties)
CREATE TABLE IF NOT EXISTS uncertainties (
    id VARCHAR(36) PRIMARY KEY,
    drawing_id VARCHAR(36) NOT NULL REFERENCES drawings(id) ON DELETE CASCADE,
    box_id VARCHAR(36) REFERENCES boxes(id) ON DELETE SET NULL,
    circuit_id VARCHAR(36) REFERENCES circuits(id) ON DELETE SET NULL,
    location VARCHAR(128) NOT NULL,      -- 存疑位置: 如 "01KY1 进线回路"
    issue_type VARCHAR(64) NOT NULL,     -- missing_spec, cascade_risk, imbalance, code_conflict, size_missing
    severity VARCHAR(16) DEFAULT 'warning', -- error, warning, info
    detail TEXT NOT NULL,                -- 存疑描述及隐患分析
    suggested_action TEXT,               -- AI 或规范建议的处置动作
    resolved BOOLEAN DEFAULT FALSE,      -- 是否已核销
    resolved_by VARCHAR(64),             -- 核销人 (engineer, ai_review, batch)
    resolve_reason TEXT,                 -- 核销说明
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_uncertainties_drawing ON uncertainties(drawing_id);
CREATE INDEX IF NOT EXISTS idx_uncertainties_status ON uncertainties(resolved);


-- 9. 标准元器件价格知识库 (Price Master Catalog)
CREATE TABLE IF NOT EXISTS price_catalog (
    id VARCHAR(36) PRIMARY KEY,
    brand VARCHAR(64) NOT NULL,          -- 施耐德, ABB, 西门子, 正泰, 德力西, 良信
    category VARCHAR(64) NOT NULL,       -- MCB, MCCB, ACB, RCBO, ATS, SPD, KM, FR
    model_series VARCHAR(64) NOT NULL,   -- Acti9, NSX, Emax2, NXB-63, NM1, NDG3
    full_model_code VARCHAR(128) NOT NULL UNIQUE, -- 如: A9F18116, NSX100F-TM80D-3P, NXB-63-1P-C16
    poles VARCHAR(8) NOT NULL,           -- 1P, 2P, 3P, 4P
    frame_current_a INT,                 -- 壳架电流
    rated_current_a INT NOT NULL,        -- 额定电流
    breaking_capacity_ka INT,            -- 额定短路分断能力 (kA)
    trip_curve VARCHAR(16),              -- C型, D型, 热磁TM, 电子脱扣Micrologic
    standard_catalog_price NUMERIC(12, 2) NOT NULL, -- 官方目录含税面价
    tier1_discount_rate NUMERIC(5, 4) DEFAULT 0.35, -- 一级经销商/集采协议折率
    net_purchase_price NUMERIC(12, 2) NOT NULL,     -- 实际到厂采购基准价
    lead_time_days INT DEFAULT 3,        -- 供货周期(天)
    moq INT DEFAULT 1,                   -- 最小起订量
    price_source VARCHAR(64) DEFAULT 'catalog', -- catalog, b2b_crawl, historical_bid
    effective_date DATE,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_price_brand_series ON price_catalog(brand, model_series);
CREATE INDEX IF NOT EXISTS idx_price_cat_curr ON price_catalog(category, rated_current_a, poles);
CREATE INDEX IF NOT EXISTS idx_price_model ON price_catalog(full_model_code);


-- 10. 国产化对标与平替映射表 (Component Equivalents)
CREATE TABLE IF NOT EXISTS component_equivalents (
    id VARCHAR(36) PRIMARY KEY,
    source_brand VARCHAR(64) NOT NULL,   -- 原始高价品牌: 施耐德, ABB, 西门子
    source_series VARCHAR(64) NOT NULL,  -- 如: NSX100, iC65N
    target_brand VARCHAR(64) NOT NULL,   -- 平替品牌: 正泰, 德力西, 良信
    target_series VARCHAR(64) NOT NULL,  -- 平替系列: NM1/NXM, NXB-63, NDB2
    match_level VARCHAR(32) DEFAULT 'exact', -- exact(完全电气等效), equivalent(外形略有差异), downgrade(轻微降级需核定)
    typical_cost_saving_pct NUMERIC(5, 2), -- 预期降本比例 (如: 35.5%)
    compliance_notes TEXT,               -- CCC认证、分断能力实测对照说明
    is_active BOOLEAN DEFAULT TRUE
);

CREATE INDEX IF NOT EXISTS idx_equiv_source ON component_equivalents(source_brand, source_series);


-- 11. 成套报价工程单与版本表 (Quotations)
CREATE TABLE IF NOT EXISTS quotations (
    id VARCHAR(36) PRIMARY KEY,
    project_id VARCHAR(36) NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_no VARCHAR(16) NOT NULL,     -- v1.0, v1.1, v2.0
    title VARCHAR(255) NOT NULL,
    base_brand VARCHAR(64) NOT NULL,     -- 报价基准品牌 (施耐德 / 正泰 / 良信)
    box_enclosure_amount NUMERIC(14, 2) DEFAULT 0.00, -- 箱体外壳小计
    component_amount NUMERIC(14, 2) DEFAULT 0.00,     -- 元器件物料小计
    copper_busbar_amount NUMERIC(14, 2) DEFAULT 0.00, -- 铜排母线小计
    auxiliary_wire_amount NUMERIC(14, 2) DEFAULT 0.00,-- 二次线及辅材小计
    labor_assembly_amount NUMERIC(14, 2) DEFAULT 0.00,-- 人工组装调试小计
    test_and_cert_amount NUMERIC(14, 2) DEFAULT 0.00, -- 型式试验与CCC分摊
    tax_rate NUMERIC(5, 4) DEFAULT 0.1300,            -- 增值税率 13%
    profit_rate NUMERIC(5, 4) DEFAULT 0.0800,         -- 目标综合毛利率 8%
    total_tax_included NUMERIC(14, 2) NOT NULL,       -- 含税总报价金额
    status VARCHAR(32) DEFAULT 'draft',               -- draft, review, approved, sent
    created_by VARCHAR(64),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_quotations_project ON quotations(project_id, version_no);


-- 12. 审计流与数据版本追踪 (Audit Revisions)
-- 保证每一笔修改、每一条AI建议核准均有据可查、可原子级回滚
CREATE TABLE IF NOT EXISTS audit_logs (
    id BIGSERIAL PRIMARY KEY,
    project_id VARCHAR(36),
    drawing_id VARCHAR(36),
    target_table VARCHAR(32) NOT NULL,   -- boxes, circuits, components, uncertainties
    target_id VARCHAR(36) NOT NULL,
    action VARCHAR(32) NOT NULL,         -- create, update, delete, batch_resolve, ai_patch
    before_state JSON,
    after_state JSON,
    operator VARCHAR(64) NOT NULL,       -- 用户名或 "AI_ASSISTANT"
    operator_type VARCHAR(16) NOT NULL,  -- human, ai, rule_engine
    reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_logs(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_dwg ON audit_logs(drawing_id);
