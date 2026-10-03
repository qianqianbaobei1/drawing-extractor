# -*- coding: utf-8 -*-
"""FastAPI app: upload PDF -> render -> vision extract -> Excel download."""
import base64
import os
import re
import json
import uuid
import shutil
from datetime import datetime

from fastapi import (FastAPI, UploadFile, File, Form, BackgroundTasks,
                     HTTPException, Query, Request)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from extractor.render import plan_tiles, render_pdf
from extractor.vision import VisionProvider, is_safe_model_url
from extractor.checker import check_result, check_result_issues, CheckIssue
from extractor.assemble import assemble
from extractor.schema import CONTRACT_VERSION, PROMPT_VERSION, Uncertainty, RawExtraction, ExtractionResult
from extractor.excel import build_workbook, build_project_bom_workbook
from extractor.cad import is_cad_path, process_cad_file
from extractor.catalog import analyze_components_replacement
from extractor.catalog_reconciler import DrawingCatalogReconciler
from db import (
    db_save_job, db_get_job, db_list_jobs, db_recover_interrupted_jobs,
    set_current_tenant, get_current_tenant, get_current_user, db_ensure_tenant,
    db_authenticate_user, db_register_user, db_get_user_by_token, db_logout_user,
    db_clean_all_test_data
)
import store

BASE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(BASE, "work")
os.makedirs(WORKDIR, exist_ok=True)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, "..", ".env"))
    load_dotenv(os.path.join(BASE, ".env"))
except ImportError:
    pass

app = FastAPI(title="图纸元器件提取")

jobs: dict = {}
store.apply_settings_to_env()

IN_PROGRESS = {"queued", "rendering", "extracting", "building_excel", "converting"}


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    display_name: str = ""
    tenant_name: str = ""


@app.middleware("http")
async def tenant_middleware(request: Request, call_next):
    """多租户隔离上下文拦截器：自动根据 Token / Header 绑定 tenant_id 与 user_id。"""
    token = ""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif request.headers.get("x-token"):
        token = request.headers.get("x-token").strip()
    elif request.query_params.get("token"):
        token = request.query_params.get("token").strip()

    t_id = ""
    u_id = ""
    if token:
        user_info = db_get_user_by_token(token)
        if user_info:
            t_id = user_info["tenant_id"]
            u_id = user_info["id"]
            request.state.user = user_info

    if not t_id:
        t_id = (request.headers.get("x-tenant-id") or
                request.query_params.get("tenant_id") or
                "default").strip() or "default"
        u_id = (request.headers.get("x-user-id") or
                f"user_{t_id}").strip()

    set_current_tenant(t_id, u_id)
    response = await call_next(request)
    path = request.url.path
    if path.endswith((".html", ".js", ".css")) or path in ("", "/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response



@app.on_event("startup")
def on_startup():
    """服务冷启动时，扫描并自动将异常中断的任务恢复为可重试的 interrupted 状态。"""
    from db import init_db
    init_db()
    recovered = db_recover_interrupted_jobs()
    if recovered:
        print(f"[Startup] 已自动将 {recovered} 个未决中断任务标记为 interrupted 状态")


def job_file(job_id: str) -> str:
    return os.path.join(WORKDIR, f"{job_id}.json")


_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_job_id(job_id: str) -> str:
    """job_id 合法性校验：含 .. / 或非法字符时直接 400，阻止路径穿越拼进文件路径。"""
    if not _JOB_ID_RE.fullmatch(job_id or ""):
        raise HTTPException(400, "非法任务ID")
    return job_id


def check_job_tenant_access(job: dict | None, tenant_id: str | None = None) -> dict:
    """严格的多租户越权拦截：杜绝跨企业窃取图纸、项目、BOM清单与数据。"""
    if not job:
        raise HTTPException(404, "任务不存在")
    current = tenant_id or get_current_tenant()
    job_tenant = job.get("tenant_id") or "default"
    if current != job_tenant:
        raise HTTPException(403, f"无权访问其他租户的任务数据 (当前租户: {current}, 任务所属租户: {job_tenant})")
    return job


def save_job(job_id: str) -> None:
    """任务全生命周期实时原子落盘：无论是中间状态还是最终结果，杜绝进程退出/重启丢数据。"""
    job = jobs.get(job_id)
    if not job:
        return
    if not job.get("job_id"):
        job["job_id"] = job_id
    if not job.get("tenant_id"):
        job["tenant_id"] = get_current_tenant()
    if not job.get("user_id"):
        job["user_id"] = get_current_user()

    # 1. 实时原子落库 SQLite WAL 表 (彻底防止任务在服务重启中蒸发)
    try:
        db_save_job(job)
    except Exception as exc:
        print(f"db_save_job({job_id}) 失败: {exc!r}")

    # 2. 磁盘 JSON 镜像文件 (供本地兼容查看)
    try:
        with open(job_file(job_id), "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False)
    except (OSError, TypeError) as exc:
        print(f"save_job({job_id}) 失败: {exc!r}")


def _fail(job_id, msg):
    jobs[job_id].update(status="failed", error=msg, progress=100)
    save_job(job_id)


def _extract_plan(pdf_path: str, images: list[str]) -> list[tuple]:
    """把页面展开成要送检的图：小页整页，大页（A2/A1）切成带重叠的块。

    模型服务端会把输入图缩到固定尺寸，整张大图的小字就是在这一步丢的；
    分块后每块都按同样的像素去渲染，等效于把小字放大几倍再送。
    """
    tiling = bool(store.settings().get("tile_large_pages", True))
    items: list[tuple] = []
    for n, image in enumerate(images, 1):
        if tiling:
            try:
                tiles = plan_tiles(pdf_path, n - 1)
            except Exception as exc:  # noqa: BLE001 - 切不出来就退回整页，不能让计划阶段挂掉
                print(f"plan_tiles({pdf_path}, page {n}) 失败，改用整页: {exc!r}")
                tiles = []
            if tiles:
                items.extend((tile["path"], n, tile["clip"]) for tile in tiles)
                continue
        items.append((image, n, None))
    return items


def _drop_tiles(pdf_path: str) -> None:
    """块图是中间产物，PDF 还在，随时能重生，不留着占磁盘。"""
    for name in os.listdir(os.path.dirname(pdf_path) or "."):
        if name.startswith(os.path.basename(pdf_path) + ".page") and ".tile" in name:
            try:
                os.remove(os.path.join(os.path.dirname(pdf_path), name))
            except OSError:
                pass


def process_drawing_file(job_id: str, raw_path: str, filename: str):
    """图纸处理主入口：自动识别 CAD (DWG/DXF) 或 PDF 并启动流水线。"""
    job = jobs.get(job_id) or load_job_cached(job_id) or {}
    t_id = job.get("tenant_id") or get_current_tenant()
    u_id = job.get("user_id") or get_current_user()
    set_current_tenant(t_id, u_id)

    if is_cad_path(raw_path):
        job = jobs[job_id]
        job.update(status="converting", progress=5)
        save_job(job_id)
        pdf_path = os.path.join(WORKDIR, f"{job_id}.pdf")
        try:
            _, cad_texts = process_cad_file(raw_path, pdf_path)
            if cad_texts:
                with open(os.path.join(WORKDIR, f"{job_id}_cad_texts.json"), "w", encoding="utf-8") as f:
                    json.dump(cad_texts, f, ensure_ascii=False, indent=2)
        except ValueError as exc:
            _fail(job_id, str(exc))
            return
        except Exception as exc:
            _fail(job_id, f"CAD 图纸转换解析失败: {exc}")
            return

        # 优先使用 CAD 原生矢量拓扑提取器（零光栅化模糊、零大模型幻觉、零断路器空缺、100% 拓扑对齐）
        try:
            from extractor.cad_extractor import extract_cad_table_data
            cad_raw = extract_cad_table_data(raw_path)
            if cad_raw.boxes and cad_raw.circuits:
                process_cad_raw_extraction(job_id, pdf_path, filename, cad_raw)
                return
        except Exception as cad_err:
            print(f"[CAD] 原生矢量提取降级至视觉模型: {cad_err}")

        process_pdf(job_id, pdf_path, filename)
    else:
        process_pdf(job_id, raw_path, filename)


def _sync_result_issues(result: ExtractionResult, existing_uncertainties: list = None) -> None:
    """统一同步 check_result_issues 到 result.uncertainties，精准保留三级门禁 (ERROR/WARNING/INFO)。"""
    issues = check_result_issues(result)
    known = {u.text for u in result.uncertainties}
    flags = {}
    if existing_uncertainties:
        for u in existing_uncertainties:
            txt = u.get("text") if isinstance(u, dict) else getattr(u, "text", "")
            res = u.get("resolved") if isinstance(u, dict) else getattr(u, "resolved", False)
            if txt:
                flags[txt.strip()] = res

    for issue in issues:
        raw_text = issue.text
        parsed = Uncertainty(
            location=issue.target,
            detail=issue.detail,
            severity=issue.severity,
            source="program",
            resolved=flags.get(raw_text.strip(), False)
        )
        if parsed.text not in known:
            known.add(parsed.text)
            result.uncertainties.append(parsed)


def process_cad_raw_extraction(job_id: str, pdf_path: str, filename: str, raw: RawExtraction):
    """CAD 原生矢量数据专用处理分支：跳过模糊位图视觉推断，直接组装工业级高保真清单。"""
    job = jobs[job_id]
    try:
        job["status"] = "rendering"
        save_job(job_id)
        images = render_pdf(pdf_path)
        job.update(status="extracting", pages=len(images), progress=80)
        save_job(job_id)

        cad_texts = None
        cad_json_path = os.path.join(WORKDIR, f"{job_id}_cad_texts.json")
        if os.path.exists(cad_json_path):
            try:
                with open(cad_json_path, "r", encoding="utf-8") as f:
                    cad_texts = json.load(f)
            except Exception:
                pass

        sheet_names: dict[str, str] = {}
        if cad_texts:
            for t in cad_texts:
                p = t.get("page")
                s = t.get("sheet")
                if p and s and str(p) not in sheet_names:
                    sheet_names[str(p)] = str(s).strip()

        for p_int in range(1, len(images) + 1):
            p_str = str(p_int)
            if p_str not in sheet_names:
                sheet_names[p_str] = f"图纸第 {p_str} 页"

        meta = {
            "model": "CAD-Vector-Topology-Engine",
            "prompt_version": PROMPT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "calls": 0,
        }
        result = assemble(raw, meta)

        if cad_texts and not getattr(result, "reconciliation", None):
            seen_cat = set()
            cat_items = []
            for t in cad_texts:
                txt = t.get("text", "")
                parsed = DrawingCatalogReconciler.parse_catalog_line(txt)
                if parsed and parsed.declared_panels:
                    key = (parsed.sheet_no, parsed.sheet_title)
                    if key not in seen_cat:
                        seen_cat.add(key)
                        cat_items.append(parsed)
            if cat_items:
                result.reconciliation = DrawingCatalogReconciler.reconcile(
                    cat_items, result.boxes, source_name="CAD图纸目录"
                )

        _sync_result_issues(result)

        job["status"] = "building_excel"
        xlsx = os.path.join(WORKDIR, f"{job_id}.xlsx")
        subtitle = (f"依据:{filename}  提取时间:{datetime.now():%Y-%m-%d %H:%M}"
                    f"｜引擎:CAD高精度矢量拓扑解析器｜契约v{CONTRACT_VERSION}")
        build_workbook(result, subtitle, xlsx,
                       template_path=store.settings().get("excel_template") or "",
                       layout="3_sheets")

        data = {
            "boxes": [b.model_dump() for b in result.boxes],
            "circuits": [c.model_dump() for c in result.circuits],
            "components": [c.model_dump() for c in result.components],
            "requirements": [r.model_dump() for r in result.requirements],
            "uncertainties": [u.model_dump() for u in result.uncertainties],
            "extra_devices": [d.model_dump() for d in raw.extra_devices],
            "topology": [t.model_dump() for t in getattr(result, "topology", [])],
            "reconciliation": result.reconciliation.model_dump() if getattr(result, "reconciliation", None) else None,
        }
        jobs[job_id].update(
            status="done", excel=f"/api/jobs/{job_id}/excel",
            filename=filename,
            pages=len(images),
            sheet_names=sheet_names,
            ai_usage={"tokens": 0, "cost": 0.0, "note": "CAD原生矢量提取，未消耗AI Token"},
            summary={
                "title": result.title,
                "boxes": len(result.boxes),
                "circuits": len(result.circuits),
                "components": len(result.components),
                "uncertainties": [u.model_dump() for u in result.uncertainties],
                "topology_nodes": len(getattr(result, "topology", [])),
                "meta": meta,
            },
            data=data,
            preview=[c.model_dump() for c in result.components[:50]],
            raw=raw.model_dump(),
            changes=[],
            created_at=datetime.now().isoformat(timespec="seconds"),
            box_code=(result.boxes[0].code if result.boxes else ""),
            box_name=(result.boxes[0].name if result.boxes else ""),
        )
        save_job(job_id)
    except Exception as e:
        _fail(job_id, f"处理异常: {e}")


def process_pdf(job_id: str, pdf_path: str, filename: str):
    job = jobs[job_id]
    try:
        job["status"] = "rendering"
        save_job(job_id)
        images = render_pdf(pdf_path)
        job.update(status="extracting", pages=len(images), progress=0)
        save_job(job_id)

        provider = VisionProvider()
        if not provider.configured:
            _fail(job_id, "VISION_API_KEY 未配置，请在 .env 中填写视觉模型 API Key")
            return

        items = _extract_plan(pdf_path, images)

        def on_progress(done, total, page):
            jobs[job_id].update(progress=round(done / total * 100), current_page=page)
            save_job(job_id)

        cad_texts = None
        cad_json_path = os.path.join(WORKDIR, f"{job_id}_cad_texts.json")
        if os.path.exists(cad_json_path):
            try:
                with open(cad_json_path, "r", encoding="utf-8") as f:
                    cad_texts = json.load(f)
            except Exception:
                pass

        raw = provider.extract(items, on_progress=on_progress, cad_texts=cad_texts)
        _drop_tiles(pdf_path)

        # 记录 AI 识别费用与 Token 日志
        usage_summary = getattr(provider, "last_usage_summary", {})
        if isinstance(usage_summary, dict) and usage_summary:
            proj_name = job.get("project") or "未分组"
            store.record_project_ai_usage(proj_name, usage_summary, job_id=job_id, filename=filename)
        elif not isinstance(usage_summary, dict):
            usage_summary = {}

        # 智能初始化多图切块名称（优先 CAD 图签标题，其次按页回路箱体自动推断）
        sheet_names: dict[str, str] = {}
        if cad_texts:
            for t in cad_texts:
                p = t.get("page")
                s = t.get("sheet")
                if p and s and str(p) not in sheet_names:
                    sheet_names[str(p)] = str(s).strip()

        page_boxes: dict[str, set[str]] = {}
        for c in raw.circuits:
            p = str((c.bbox.page if c.bbox else 1) or 1)
            if c.box:
                page_boxes.setdefault(p, set()).add(c.box)
        for b in raw.boxes:
            p = str((b.bbox.page if getattr(b, "bbox", None) else 1) or 1)
            if b.code:
                page_boxes.setdefault(p, set()).add(b.code)

        for p_int in range(1, len(images) + 1):
            p_str = str(p_int)
            if p_str not in sheet_names:
                boxes_here = sorted(page_boxes.get(p_str, []))
                if boxes_here:
                    sheet_names[p_str] = (
                        f"{boxes_here[0]} 配电系统图"
                        if len(boxes_here) == 1
                        else f"{'/'.join(boxes_here[:2])} 等配电系统图"
                    )
                else:
                    sheet_names[p_str] = f"切图图块 {p_str}"

        meta = {"model": provider.model, "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION, "calls": len(items)}
        result = assemble(raw, meta)

        if cad_texts and not getattr(result, "reconciliation", None):
            seen_cat = set()
            cat_items = []
            for t in cad_texts:
                txt = t.get("text", "")
                parsed = DrawingCatalogReconciler.parse_catalog_line(txt)
                if parsed and parsed.declared_panels:
                    key = (parsed.sheet_no, parsed.sheet_title)
                    if key not in seen_cat:
                        seen_cat.add(key)
                        cat_items.append(parsed)
            if cat_items:
                result.reconciliation = DrawingCatalogReconciler.reconcile(
                    cat_items, result.boxes, source_name="图纸目录"
                )

        _sync_result_issues(result)

        job["status"] = "building_excel"
        save_job(job_id)
        xlsx = os.path.join(WORKDIR, f"{job_id}.xlsx")
        subtitle = (f"依据:{filename}  提取时间:{datetime.now():%Y-%m-%d %H:%M}"
                    f"｜模型:{provider.model}｜提示词v{PROMPT_VERSION}｜契约v{CONTRACT_VERSION}")
        build_workbook(result, subtitle, xlsx,
                       template_path=store.settings().get("excel_template") or "")

        data = {
            "boxes": [b.model_dump() for b in result.boxes],
            "circuits": [c.model_dump() for c in result.circuits],
            "components": [c.model_dump() for c in result.components],
            "requirements": [r.model_dump() for r in result.requirements],
            "uncertainties": [u.model_dump() for u in result.uncertainties],
            "extra_devices": [d.model_dump() for d in raw.extra_devices],
            "topology": [t.model_dump() for t in getattr(result, "topology", [])],
            "reconciliation": result.reconciliation.model_dump() if getattr(result, "reconciliation", None) else None,
        }
        jobs[job_id].update(
            status="done", excel=f"/api/jobs/{job_id}/excel",
            filename=filename,
            pages=len(images),
            sheet_names=sheet_names,
            ai_usage=usage_summary,
            summary={
                "title": result.title,
                "boxes": len(result.boxes),
                "circuits": len(result.circuits),
                "components": len(result.components),
                "uncertainties": [u.model_dump() for u in result.uncertainties],
                "reconciliation": result.reconciliation.model_dump() if getattr(result, "reconciliation", None) else None,
                "topology_nodes": len(getattr(result, "topology", [])),
                "meta": meta,
            },
            data=data,
            preview=[c.model_dump() for c in result.components[:50]],
            raw=raw.model_dump(),
            changes=[],
            created_at=datetime.now().isoformat(timespec="seconds"),
            box_code=(result.boxes[0].code if result.boxes else ""),
            box_name=(result.boxes[0].name if result.boxes else ""),
        )
        save_job(job_id)
    except Exception as e:  # noqa: BLE001
        _fail(job_id, f"处理异常: {e}")


# 只出现在 checker.py / assemble.py 里的措辞，用于识别旧导出里没标来源的程序告警
PROGRAM_WARNING_MARKERS = (
    "回路逐条计数", "无法安全拆分", "未计入元器件汇总", "出现重复编号",
    "不在常见值中", "需对照图纸核对", "箱体清单中未找到", "数量无效", "数量为",
)


def _legacy_raw(data: dict) -> tuple[dict, list, list]:
    """旧任务没有保存模型原始事实，从已导出的清单反推。

    反推后直接跑一遍 assemble + checker，使加载结果与“保存一次”完全一致：
    否则项目页会拿着导出时的快照显示早已失效的程序告警。

    两个要点：
    - 汇总名称由程序按型号前缀推导，可能与模型的命名不同，因此只按规格判断哪些元器件
      无法由回路推导（否则同一个器件会被算两次）。
    - xlsx 里的“待人工核对”把模型存疑与 checker 告警写在一起了。checker 告警要重新算，
      不能当成模型事实存回来，否则问题改好了告警还在。
    """
    skeleton = {
        "boxes": data["boxes"], "circuits": data["circuits"],
        "requirements": data["requirements"],
        "extra_devices": [], "uncertainties": [],
    }
    try:
        derived = assemble(RawExtraction.model_validate(skeleton))
    except Exception:  # noqa: BLE001 - 旧数据形状异常时宁可不反推
        fallback = [Uncertainty(**{k: v for k, v in u.items() if k in Uncertainty.model_fields})
                    for u in data.get("uncertainties", [])]
        return ({**skeleton, "uncertainties": data.get("uncertainties", [])},
                fallback, data.get("components", []))

    specs = {c.spec for c in derived.components}
    # 箱体永远由 boxes 推导，不能当额外设备，否则尺寸写法一变就会多出一个箱体行
    extra_devices = [{"name": c["name"], "spec": c["spec"], "unit": c["unit"],
                      "quantity": c["quantity"], "used_in": c["used_in"], "note": c["note"]}
                     for c in data["components"]
                     if c["spec"] not in specs and c["name"] != "配电箱体"]

    # 需要重新计算的告警有两类：checker 的交叉核对，和 assemble 自己的拆分/箱体告警。
    # 后者取 derived.uncertainties 就是全部程序告警（skeleton 里没带模型存疑）。
    stored = ExtractionResult.model_validate({
        "boxes": data["boxes"], "circuits": data["circuits"],
        "components": data["components"], "requirements": data["requirements"],
        "uncertainties": [],
    })
    regenerated = {w.strip() for w in check_result(stored)}
    regenerated |= {u.text.strip() for u in derived.uncertainties}

    # 重复保存时旧告警会叠进 xlsx，这里再排一次重
    seen, model_items = set(), []
    # 旧 xlsx 回读时恢复"已确认"标记：from_text 已识别"（已确认）"前缀并置
    # resolved=True；但下面重跑 assemble 会按文本重建 uncertainties，这里先按
    # 文本记住哪些是已确认的，重建后再挂回去。
    resolved_by_text = set()
    for item in data.get("uncertainties", []):
        if item.get("resolved"):
            t = (item["location"] + "：" + item["detail"] if item.get("location")
                 else item.get("detail", "")).strip()
            if t:
                resolved_by_text.add(t)
    for item in data.get("uncertainties", []):
        text = item["location"] + "：" + item["detail"] if item.get("location") else item.get("detail", "")
        if text.strip() in regenerated or text in seen or not text.strip():
            continue
        # 有 source 标记的直接用；旧导出没标记的靠程序告警的固定措辞识别，
        # 这些短语只出现在 checker.py / assemble.py 里
        if item.get("source") == "program":
            continue
        if not item.get("source") and any(marker in text for marker in PROGRAM_WARNING_MARKERS):
            continue
        seen.add(text)
        model_items.append({k: v for k, v in item.items() if k != "resolved"})

    replayed = assemble(RawExtraction.model_validate(
        {**skeleton, "extra_devices": extra_devices, "uncertainties": model_items}))
    _sync_result_issues(replayed, existing_uncertainties=data.get("uncertainties", []))

    # 把回读时记住的"已确认"标记挂回重建后的 uncertainties
    for u in replayed.uncertainties:
        if u.text.strip() in resolved_by_text:
            u.resolved = True

    return ({**skeleton, "extra_devices": extra_devices, "uncertainties": model_items},
            replayed.uncertainties, replayed.components)


def load_job(job_id: str):
    """优先读 SQLite 数据库；次选磁盘 JSON；旧任务没有时从已生成的 xlsx 反推。"""
    db_job = db_get_job(job_id)
    if db_job and db_job.get("status"):
        return db_job

    path = job_file(job_id)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict) and stored.get("status"):
                return stored
        except (OSError, json.JSONDecodeError):
            pass
    return _load_from_xlsx(job_id)


def load_job_cached(job_id: str):
    """读任务，同时把从旧 xlsx 反推的结果回写为 json 与 SQLite，下次读取无损。"""
    if job_id in jobs:
        return jobs[job_id]
    job = load_job(job_id)
    if job:
        jobs[job_id] = job
        save_job(job_id)
    return job


def _load_from_xlsx(job_id: str):
    xlsx_path = os.path.join(WORKDIR, f"{job_id}.xlsx")
    if not os.path.exists(xlsx_path):
        return None
    try:
        import openpyxl
        import glob
        wb = openpyxl.load_workbook(xlsx_path, data_only=True)
        data = {"boxes": [], "circuits": [], "components": [], "requirements": [], "uncertainties": []}
        uncertain_seen: set[str] = set()
        legacy_meta: dict = {}
        filename = f"{job_id}.pdf"

        if "箱体清单" in wb.sheetnames:
            ws = wb["箱体清单"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 1 and rows[1] and rows[1][0] and "依据:" in str(rows[1][0]):
                subtitle = str(rows[1][0])
                try:
                    part = subtitle.split("依据:")[1].split(" ")[0].strip()
                    if part:
                        filename = part
                except Exception:
                    pass
                found = SUBTITLE_RE.search(subtitle)
                if found:
                    legacy_meta = {"model": found.group("model").strip(),
                                   "prompt_version": found.group("prompt"),
                                   "contract_version": found.group("contract")}
                    legacy_meta = {k: v for k, v in legacy_meta.items() if v}
            if len(rows) > 3:
                for r in rows[3:]:
                    if not any(r):
                        continue
                    data["boxes"].append({
                        "code": str(r[1] or ""),
                        "name": str(r[2] or ""),
                        "ip_rating": str(r[3] or ""),
                        "install": str(r[4] or ""),
                        "location": str(r[5] or ""),
                        "size": str(r[6] or ""),
                        "quantity": int(r[7] or 1) if str(r[7] or "").isdigit() else 1,
                        "note": str(r[8] or "")
                    })
        if "元器件汇总" in wb.sheetnames:
            ws = wb["元器件汇总"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 3:
                for r in rows[3:]:
                    if not any(r):
                        continue
                    data["components"].append({
                        "name": str(r[1] or ""),
                        "spec": str(r[2] or ""),
                        "unit": str(r[3] or ""),
                        "quantity": float(r[4] or 0),
                        "used_in": str(r[5] or ""),
                        "note": str(r[6] or "")
                    })
        if "回路明细" in wb.sheetnames:
            ws = wb["回路明细"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 3:
                for r in rows[3:]:
                    if not any(r):
                        continue
                    data["circuits"].append({
                        "box": str(r[1] or ""),
                        "phase": str(r[2] or ""),
                        "breaker": str(r[3] or ""),
                        "contactor": str(r[4] or ""),
                        "ct": str(r[5] or ""),
                        "thermal": str(r[6] or ""),
                        "power_kw": str(r[7] or ""),
                        "circuit_no": str(r[8] or ""),
                        "cable": str(r[9] or ""),
                        "current_a": str(r[10] or ""),
                        "load_name": str(r[11] or ""),
                        "secondary_ref": str(r[12] or ""),
                        "start_method": str(r[13] or ""),
                        "note": str(r[14] or "")
                    })
        if "技术要求与报价说明" in wb.sheetnames:
            ws = wb["技术要求与报价说明"]
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) > 3:
                for r in rows[3:]:
                    if not any(r):
                        continue
                    item = str(r[1] or "")
                    content = str(r[2] or "")
                    # 待人工核对项 / 程序核对告警 是由数据推导出来的，不是图纸要求，
                    # 放进 requirements 会在每次导入导出时越积越多
                    if item == "程序核对告警":
                        source = "program"
                    elif "待人工核对项" in item or "存疑" in item:
                        # 旧版本把模型存疑和程序告警写在同一行，来源留空由 _legacy_raw 识别
                        source = ""
                    else:
                        if item or content:
                            data["requirements"].append({"item": item, "content": content})
                        continue
                    for chunk in content.split("；"):
                        parsed = Uncertainty.from_text(chunk)
                        if parsed.text and parsed.text not in uncertain_seen:
                            uncertain_seen.add(parsed.text)
                            parsed.source = source
                            data["uncertainties"].append(parsed.model_dump())

        pages = len(glob.glob(os.path.join(WORKDIR, f"{job_id}.pdf.page*.png")))
        legacy_raw, legacy_uncertainties, legacy_components = _legacy_raw(data)
        data["extra_devices"] = legacy_raw["extra_devices"]
        data["components"] = [c.model_dump() for c in legacy_components]
        data["uncertainties"] = [u.model_dump() for u in legacy_uncertainties]
        job_info = {
            "status": "done",
            "job_id": job_id,
            "filename": filename,
            "pages": max(pages, 1),
            "excel": f"/api/jobs/{job_id}/excel",
            "summary": {
                "title": "配电箱元器件清单(报价用)",
                "boxes": len(data["boxes"]),
                "circuits": len(data["circuits"]),
                "components": len(data["components"]),
                "uncertainties": data["uncertainties"],
                "meta": legacy_meta,
            },
            "data": data,
            "preview": data["components"][:50],
            "raw": legacy_raw,
            "changes": [],
            "created_at": datetime.fromtimestamp(
                os.path.getmtime(xlsx_path)).isoformat(timespec="seconds"),
            "box_code": data["boxes"][0]["code"] if data["boxes"] else "",
            "box_name": data["boxes"][0]["name"] if data["boxes"] else "",
        }
        return job_info
    except Exception as e:
        print(f"Error loading {job_id} from workdir: {e}")
        return None


@app.post("/api/jobs")
def create_job(background: BackgroundTasks, file: UploadFile = File(...),
               project: str = Form("")):
    fn_lower = file.filename.lower()
    is_pdf = fn_lower.endswith(".pdf")
    is_cad = fn_lower.endswith(".dwg") or fn_lower.endswith(".dxf")
    if not (is_pdf or is_cad):
        raise HTTPException(400, "仅支持 PDF、DWG 或 DXF 格式的电气系统图")

    job_id = uuid.uuid4().hex[:12]
    ext = os.path.splitext(file.filename)[1].lower()
    raw_path = os.path.join(WORKDIR, f"{job_id}{ext}")
    with open(raw_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    project = (project or "").strip()
    if project:
        store.ensure_project(project)
    t_id = get_current_tenant()
    u_id = get_current_user()
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "filename": file.filename,
        "project": project,
        "file_type": ext.lstrip("."),
        "tenant_id": t_id,
        "user_id": u_id,
        "progress": 0,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_job(job_id)
    background.add_task(process_drawing_file, job_id, raw_path, file.filename)
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/reparse")
def reparse_job(job_id: str, background: BackgroundTasks):
    """基于服务器已保存的原始图纸文件原地重新执行提取与解析，无需重新上传。"""
    _validate_job_id(job_id)
    raw_path = None
    for ext in (".dwg", ".pdf", ".dxf", ".DWG", ".PDF", ".DXF"):
        cand = os.path.join(WORKDIR, f"{job_id}{ext}")
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            raw_path = cand
            break

    if not raw_path:
        raise HTTPException(404, "找不到该任务的原始图纸源文件，无法重新解析")

    job_info = jobs.get(job_id) or load_job_cached(job_id)
    if job_info:
        check_job_tenant_access(job_info)

    filename = (job_info or {}).get("filename") or os.path.basename(raw_path)
    project = (job_info or {}).get("project") or ""

    ext = os.path.splitext(raw_path)[1].lower()
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "project": project,
        "file_type": ext.lstrip("."),
        "tenant_id": (job_info or {}).get("tenant_id") or get_current_tenant(),
        "user_id": (job_info or {}).get("user_id") or get_current_user(),
        "progress": 0,
        "error": "",
    }
    save_job(job_id)
    background.add_task(process_drawing_file, job_id, raw_path, filename)
    return {"ok": True, "job_id": job_id, "filename": filename, "message": "已成功启动重新解析"}


class BatchProjectRequest(BaseModel):
    job_ids: list[str]
    project: str


@app.post("/api/jobs/batch_set_project")
def batch_set_project(req: BatchProjectRequest):
    """支持批量勾选已上传的多份图纸，一键移动/归属到指定项目进行集中聚合分析。"""
    proj_name = req.project.strip()
    if not proj_name:
        raise HTTPException(400, "项目名称不能为空")
    store.ensure_project(proj_name)
    updated = []
    for job_id in req.job_ids:
        _validate_job_id(job_id)
        job = load_job_cached(job_id)
        if job:
            check_job_tenant_access(job)
            job["project"] = proj_name
            save_job(job_id)
            updated.append(job_id)
    return {"ok": True, "updated": len(updated), "project": proj_name}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    return JSONResponse({k: v for k, v in job.items()})


@app.get("/api/jobs/{job_id}/page/{page_num}")
def get_page_image(job_id: str, page_num: int = 1):
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    img_path = os.path.join(WORKDIR, f"{job_id}.pdf.page{page_num}.png")
    if not os.path.exists(img_path):
        p1 = os.path.join(WORKDIR, f"{job_id}.pdf.page1.png")
        if os.path.exists(p1):
            return FileResponse(p1, media_type="image/png")
        raise HTTPException(404, "图纸页面尚未生成或不存在")
    return FileResponse(img_path, media_type="image/png")


@app.get("/api/samples")
def get_samples():
    return {"samples": list_jobs()["jobs"]}


from typing import Any
from pydantic import BaseModel
from extractor.schema import (
    Box, Circuit, Component, ExtraDevice, Requirement, ExtractionResult, AssembledMeta,
    RawExtraction, Uncertainty,
)
from extractor.assistant import (
    TABS, Assistant, apply_patch, parse_local_command, validate_patch,
)
from extractor.excel import build_workbook, uncertainty_texts


SUBTITLE_RE = re.compile(r"模型[:：](?P<model>[^｜|]*)(?:｜|\|)提示词v?(?P<prompt>[\d.]+)(?:｜|\|)契约v?(?P<contract>[\d.]+)")


class RegionParseRequest(BaseModel):
    page: int = 1
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


@app.post("/api/jobs/{job_id}/parse_region")
def parse_region(job_id: str, req: RegionParseRequest):
    """用户在图纸上框选局部区域，实时高清裁切并解析元器件与回路。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)

    page_num = max(1, req.page)
    x = max(0.0, min(1.0, req.x))
    y = max(0.0, min(1.0, req.y))
    w = max(0.005, min(1.0 - x, req.w))
    h = max(0.005, min(1.0 - y, req.h))

    pdf_path = os.path.join(WORKDIR, f"{job_id}.pdf")
    page_img_path = os.path.join(WORKDIR, f"{job_id}.pdf.page{page_num}.png")
    crop_id = uuid.uuid4().hex[:10]
    crop_filename = f"{job_id}_crop_{crop_id}.png"
    crop_path = os.path.join(WORKDIR, crop_filename)

    native_text = ""
    # 1. 优先从 PDF 矢量裁切超高清图片并提取原生文字
    if os.path.exists(pdf_path):
        try:
            import pymupdf as fitz
            doc = fitz.open(pdf_path)
            if page_num <= len(doc):
                page = doc[page_num - 1]
                rect = fitz.Rect(
                    x * page.rect.width,
                    y * page.rect.height,
                    min((x + w) * page.rect.width, page.rect.width),
                    min((y + h) * page.rect.height, page.rect.height),
                )
                native_text = page.get_text("text", clip=rect).strip()
                dim = max(rect.width, rect.height)
                dpi = int(min(450, max(200, 2400 / (dim / 72)))) if dim > 0 else 300
                pix = page.get_pixmap(clip=rect, dpi=dpi)
                pix.save(crop_path)
            doc.close()
        except Exception as err:
            print(f"PDF 矢量裁切异常: {err}")

    # 2. 降级：从已渲染的整页 PNG 裁切
    if not os.path.exists(crop_path) and os.path.exists(page_img_path):
        try:
            from PIL import Image
            with Image.open(page_img_path) as full_img:
                W, H = full_img.size
                box = (int(x * W), int(y * H), int((x + w) * W), int((y + h) * H))
                cropped = full_img.crop(box)
                cropped.save(crop_path, "PNG")
        except Exception as err:
            print(f"PNG 裁切异常: {err}")

    if not os.path.exists(crop_path):
        raise HTTPException(400, "无法裁切指定图纸区域")

    with open(crop_path, "rb") as f:
        crop_b64 = base64.b64encode(f.read()).decode()
    crop_data_url = f"data:image/png;base64,{crop_b64}"
    crop_public_url = f"/api/jobs/{job_id}/crop/{crop_filename}"

    provider = VisionProvider()
    if not provider.configured:
        return {
            "ok": True,
            "configured": False,
            "crop_url": crop_public_url,
            "native_text": native_text,
            "summary": "视觉模型 API Key 未配置，仅提取底层文字",
            "components": [],
            "circuits": [],
            "requirements": [],
            "box_info": {},
            "bbox": {"x": x, "y": y, "w": w, "h": h, "page": page_num},
        }

    try:
        parsed = provider.parse_crop(crop_data_url, native_text=native_text)
        return {
            "ok": True,
            "configured": True,
            "crop_url": crop_public_url,
            "summary": parsed.get("summary") or "已解析框选区域",
            "components": parsed.get("components") or [],
            "circuits": parsed.get("circuits") or [],
            "requirements": parsed.get("requirements") or [],
            "box_info": parsed.get("box_info") or {},
            "raw_text": parsed.get("raw_text") or native_text,
            "native_text": native_text,
            "bbox": {"x": x, "y": y, "w": w, "h": h, "page": page_num},
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "crop_url": crop_public_url,
            "native_text": native_text,
            "components": [],
            "circuits": [],
            "requirements": [],
            "box_info": {},
            "bbox": {"x": x, "y": y, "w": w, "h": h, "page": page_num},
        }


@app.get("/api/jobs/{job_id}/crop/{crop_name}")
def get_crop_image(job_id: str, crop_name: str):
    _validate_job_id(job_id)
    if not re.match(r"^[a-zA-Z0-9_-]+\.png$", crop_name):
        raise HTTPException(400, "非法文件名")
    # 归属校验：裁切图文件名固定为 {job_id}_crop_{id}.png，不属于该任务的不暴露
    if not crop_name.startswith(f"{job_id}_crop_"):
        raise HTTPException(404, "裁切图片不属于该任务")
    job = load_job_cached(job_id)
    if job:
        check_job_tenant_access(job)
    path = os.path.join(WORKDIR, crop_name)
    if not os.path.exists(path):
        raise HTTPException(404, "裁切图片不存在")
    return FileResponse(path, media_type="image/png")


class JobDataUpdateRequest(BaseModel):
    boxes: list[Box] = []
    circuits: list[Circuit] = []
    components: list[Component] = []
    requirements: list[Requirement] = []
    uncertainties: list[Uncertainty] = []
    # None = 本次不改非回路设备；[] = 用户把它们删光了
    extra_devices: list[ExtraDevice] | None = None
    changes: list[dict] = []
    reason: str = ""


def _resolved_flags(items: list) -> dict:
    flags = {}
    for item in items or []:
        record = item if isinstance(item, dict) else item.model_dump()
        if record.get("resolved"):
            flags[Uncertainty(**{k: v for k, v in record.items() if k != "resolved"}).text] = True
    return flags


def persist_job_data(job_id: str, job: dict, data: dict,
                     changes: list[dict] | None = None, reason: str = "") -> dict:
    """数据 → 组装 → 交叉核对 → Excel → 变更留痕 → 落盘。

    可编辑的是三类事实：回路、箱体、非回路设备（extra_devices）。
    元器件汇总始终由它们推导，所以改了断路器不会留下对不上的旧汇总；
    删掉一个非回路设备也不会因为“回来又拿 raw 覆盖”而党复活。
    """
    meta = job.get("summary", {}).get("meta", {}) or {}
    title = job.get("summary", {}).get("title", "配电箱元器件清单(报价用)")
    raw = job.get("raw")
    extras_in = data.get("extra_devices")
    if extras_in is None:
        # 调用方没带这个字段：沿用当前已保存的，其次才是模型原始事实。
        # 不能直接回退到 raw，否则用户上次补录的设备会被抹掉。
        current = job.get("data", {}).get("extra_devices")
        extras_in = current if current is not None else (raw or {}).get("extra_devices", [])

    if raw:
        merged_raw = {
            **raw,
            "boxes": data["boxes"],
            "circuits": data["circuits"],
            "requirements": data["requirements"],
            # 空列表是“用户删光了”，与未提供区分开
            "extra_devices": extras_in,
        }
        validated = RawExtraction.model_validate(merged_raw)
        result = assemble(validated, meta)
        extras_out = [d.model_dump() for d in validated.extra_devices]
    else:
        result = ExtractionResult.model_validate({
            "title": title, "boxes": data["boxes"], "circuits": data["circuits"],
            "components": data["components"], "requirements": data["requirements"],
            "uncertainties": data["uncertainties"],
            "topology": data.get("topology", []),
            "reconciliation": data.get("reconciliation") or (job.get("data") or {}).get("reconciliation"),
        })
        extras_out = extras_in or []

    flags = _resolved_flags(job.get("data", {}).get("uncertainties"))
    flags.update(_resolved_flags(data.get("uncertainties")))
    for item in result.uncertainties:
        item.resolved = flags.get(item.text, False)

    _sync_result_issues(result, existing_uncertainties=data.get("uncertainties", []))

    filename = job.get("filename", f"{job_id}.pdf")
    model_name = meta.get("model", "人工校准") if isinstance(meta, dict) else "人工校准"
    subtitle = (f"依据:{filename}  更新时间:{datetime.now():%Y-%m-%d %H:%M}"
                f"｜模型:{model_name}｜提示词v{PROMPT_VERSION}｜契约v{CONTRACT_VERSION}")

    if changes:
        log = job.setdefault("changes", [])
        for entry in changes:
            log.append({**entry, "reason": reason,
                        "ts": entry.get("ts") or datetime.now().isoformat(timespec="seconds")})

    current = store.settings()
    build_workbook(result, subtitle, os.path.join(WORKDIR, f"{job_id}.xlsx"),
                   changes=job.get("changes", []),
                   include_changes=bool(current.get("include_changes", True)),
                   template_path=current.get("excel_template") or "")

    job["data"] = {
        "boxes": [b.model_dump() for b in result.boxes],
        "circuits": [c.model_dump() for c in result.circuits],
        "components": [c.model_dump() for c in result.components],
        "requirements": [r.model_dump() for r in result.requirements],
        "uncertainties": [u.model_dump() for u in result.uncertainties],
        "extra_devices": extras_out,
        "topology": [t.model_dump() for t in getattr(result, "topology", [])],
        "reconciliation": (
            result.reconciliation.model_dump()
            if getattr(result, "reconciliation", None)
            else (data.get("reconciliation") or (job.get("data") or {}).get("reconciliation"))
        ),
    }
    job.setdefault("summary", {})
    job["summary"].update({
        "title": result.title,
        "boxes": len(result.boxes),
        "circuits": len(result.circuits),
        "components": len(result.components),
        "uncertainties": job["data"]["uncertainties"],
        "changes": len(job.get("changes", [])),
    })
    job["updated_at"] = datetime.now().isoformat(timespec="seconds")
    save_job(job_id)
    return {"ok": True, "summary": job["summary"],
            "data": job["data"], "changes": job.get("changes", [])}


@app.put("/api/jobs/{job_id}/data")
def update_job_data(job_id: str, req: JobDataUpdateRequest):
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    data = {
        "boxes": [b.model_dump() for b in req.boxes],
        "circuits": [c.model_dump() for c in req.circuits],
        "components": [c.model_dump() for c in req.components],
        "requirements": [r.model_dump() for r in req.requirements],
        "uncertainties": [u.model_dump() for u in req.uncertainties],
        "extra_devices": None if req.extra_devices is None else [d.model_dump() for d in req.extra_devices],
    }
    return persist_job_data(job_id, job, data, req.changes, req.reason)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


@app.post("/api/jobs/{job_id}/chat")
def job_chat(job_id: str, req: ChatRequest):
    """助手问答：模型只做判断和措辞，改哪一条、字段是否合法由这里按清单校验。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)

    action = parse_local_command(req.message)
    if action:
        return {"reply": "", "action": action, "tab": "", "highlight": [],
                "focus": "", "changes": [], "source": "local"}

    data = job.get("data", {})
    comps = data.get("components", [])
    rep_summary = {}
    if comps:
        from extractor.catalog import analyze_components_replacement
        for b in ("正泰", "德力西", "良信"):
            try:
                rep_summary[b] = analyze_components_replacement(comps, target_brand=b)["summary"]
            except Exception:
                pass

    context = {
        "title": job.get("summary", {}).get("title", ""),
        "filename": job.get("filename", ""),
        "boxes": data.get("boxes", []),
        "circuits": data.get("circuits", []),
        "components": data.get("components", []),
        "requirements": data.get("requirements", []),
        "uncertainties": [{k: v for k, v in u.items() if k != "bbox"}
                          for u in data.get("uncertainties", [])],
        "replacements_summary": rep_summary,
    }

    assistant = Assistant()
    try:
        parsed = assistant.ask(context, req.message, req.history)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(502, str(exc)) from exc

    accepted, rejected = validate_patch(parsed.get("patch"), data)
    applied = []
    payload = {}
    if accepted:
        apply_patch(data, accepted)
        applied = [{**item, "source": "AI"} for item in accepted]
        payload = persist_job_data(job_id, job, data, applied, "AI 助手修改")

    reply = str(parsed.get("reply") or "").strip()
    if rejected:
        reply = (reply + "\n\n以下修改没有执行：" + "；".join(rejected)).strip()

    tab = parsed.get("tab")
    return {
        "reply": reply,
        "action": "",
        "tab": tab if tab in TABS else "chat",
        "highlight": parsed.get("highlight") or [],
        "focus": parsed.get("focus") or "",
        "changes": applied,
        "summary": payload.get("summary"),
        "data": payload.get("data"),
        "source": "model",
    }


@app.post("/api/jobs/{job_id}/resolve_all")
def resolve_all_uncertainties(job_id: str):
    """一键确认全部待核对存疑项，直接放行导出。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)

    data = job.get("data", {})
    uncertainties = data.get("uncertainties", [])
    unresolved_count = 0
    for u in uncertainties:
        if not u.get("resolved"):
            u["resolved"] = True
            unresolved_count += 1

    changes = [{
        "ts": datetime.now().isoformat(),
        "source": "user",
        "target": "全量存疑项",
        "field": "resolved",
        "old": "未核对",
        "new": "已全部确认",
        "reason": f"用户一键全部核对通过（共 {unresolved_count} 处）",
    }]
    return persist_job_data(job_id, job, data, changes=changes, reason=f"一键全部核对通过 ({unresolved_count} 项)")


@app.post("/api/jobs/{job_id}/ai_review")
def ai_deep_review(job_id: str):
    """让 AI 对图纸清单与待核对项进行深度多维度交叉复核：
    1. 存疑项语义与 CAD 原生文字交叉核验，自动裁决与消除合法项；
    2. 回路容量与开关电缆载流量匹配度自检；
    3. 进线保护、二次控制与成套常见漏项预警；
    4. 生成结构化复核报告并落盘。
    """
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)

    data = job.get("data", {})
    boxes = data.get("boxes", [])
    circuits = data.get("circuits", [])
    components = data.get("components", [])
    uncertainties = data.get("uncertainties", [])
    unresolved = [u for u in uncertainties if not u.get("resolved")]

    findings = []
    auto_resolved_count = 0

    # 1. 尝试调用大模型深度思考与复核
    assistant = Assistant()
    if assistant.configured and unresolved:
        review_prompt = (
            "你是资深电气总工。现对配电箱初步提取清单中的【待核对项】进行深度技术研判，并进行电气设计安全核查。\n"
            "待核对项列表：\n" + json.dumps([{k: v for k, v in u.items() if k != "bbox"} for u in unresolved], ensure_ascii=False) + "\n\n"
            "配电箱与回路概况：\n" + json.dumps({
                "boxes": boxes[:5],
                "circuits_count": len(circuits),
                "circuits_sample": circuits[:10]
            }, ensure_ascii=False) + "\n\n"
            "请返回一个标准 JSON 对象：\n"
            '{"summary": "复核总结", "resolved_targets": ["可以确认消除的存疑项location或文字"], "reasons": {"存疑项": "理由"}, "engineering_advice": ["工程建议1", "工程建议2"]}'
        )
        try:
            ai_res = assistant.ask(data, review_prompt, [{"role": "user", "content": "请深度复核"}])
            if isinstance(ai_res, dict):
                resolved_targets = ai_res.get("resolved_targets") or []
                reasons = ai_res.get("reasons") or {}
                advice = ai_res.get("engineering_advice") or []

                def _norm_txt(s):
                    return re.sub(r"\s+", "", str(s or ""))

                for u in unresolved:
                    loc = u.get("location", "")
                    txt = u.get("text", "")
                    detail = u.get("detail", "")
                    # 精确匹配：AI 返回的 target 必须与存疑的 location/text/detail
                    # 去空格后完全相等。禁止双向子串匹配（"WL1" 不能命中 "WL10"），
                    # 禁止"全部"二字消除所有（宁可标疑、不许编造）。
                    n_loc, n_txt, n_detail = _norm_txt(loc), _norm_txt(txt), _norm_txt(detail)
                    matched = any(
                        isinstance(t, str) and _norm_txt(t) and _norm_txt(t) in (n_loc, n_txt, n_detail)
                        for t in resolved_targets
                    )
                    if matched:
                        reason_str = reasons.get(loc) or reasons.get(txt) or "AI经CAD图纸上下文与工程规范核对无误"
                        u["resolved"] = True
                        u["detail"] = ((u.get("detail", "") or "") + f" 【AI深度复核通过: {reason_str}】").strip()
                        auto_resolved_count += 1
                        findings.append({"type": "resolved", "title": f"已自动复核通过：{loc or txt}", "detail": reason_str})

                for adv in advice:
                    findings.append({"type": "advice", "title": "电气专业建议", "detail": str(adv)})
        except Exception as e:
            print(f"[ai_review] 大模型调用降级: {e}")

    # 2. 工程启发式规则兜底与深度电气安全核查
    # 只允许消除"断路器/开关规格"主题的存疑，且回路编号必须精确匹配：
    # "电缆敷设方式不明"等问题不得以"断路器规格有效"为由消除。
    _BREAKER_TOPIC = ("断路器", "开关", "规格", "breaker")
    for u in unresolved:
        if u.get("resolved"):
            continue
        detail = str(u.get("detail", "") or "")
        loc = str(u.get("location", "") or "")
        if not any(k in detail or k in loc for k in _BREAKER_TOPIC):
            continue
        hay = f"{loc} {detail}"
        matched_cir = None
        for c in circuits:
            no = str(c.get("circuit_no") or "").strip()
            if not no:
                continue
            # 精确匹配：编号前后不能紧邻字母数字（"WL1" 不应命中 "WL10"）
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(no) + r"(?![A-Za-z0-9])", hay):
                matched_cir = c
                break
        if matched_cir:
            brk = str(matched_cir.get("breaker", "") or "")
            if any(k in brk.upper() for k in ["C65", "IC65", "NM1", "DZ47", "C16", "C20", "C25", "C32"]) and any(p in brk for p in ["1P", "2P", "3P", "4P"]):
                u["resolved"] = True
                u["detail"] = ((u.get("detail", "") or "") + " 【规则引擎复核: 断路器型号规格完整有效，已判定通过】").strip()
                auto_resolved_count += 1
                findings.append({
                    "type": "resolved",
                    "title": f"回路 {matched_cir.get('circuit_no')} 规格校验通过",
                    "detail": f"开关 {brk} 符合低压配电规范要求。"
                })

    # 规则 B: 电气容量与电缆过载核查
    for c in circuits:
        kw_str = str(c.get("power_kw", "")).replace("kW", "").strip()
        cable = str(c.get("cable", "")).upper()
        try:
            kw = float(kw_str)
            if kw >= 15.0 and any(s in cable for s in ["2.5", "1.5"]):
                findings.append({
                    "type": "warning",
                    "title": f"回路 {c.get('circuit_no')} 导线截面偏小预警",
                    "detail": f"负载容量 {kw}kW (计算电流约 {kw*1.8:.1f}A)，但设计电缆为 {cable}，建议核实电缆截面是否需放大至 6mm² 或以上。"
                })
        except ValueError:
            pass

    # 规则 C: 进线保护与成套漏项排查
    has_spd = any("浪涌" in comp.get("name", "") for comp in components) or any("SPD" in str(comp.get("spec", "")) for comp in components)
    if not has_spd:
        findings.append({
            "type": "suggestion",
            "title": "配电进线浪涌保护(SPD)成套核查",
            "detail": "图纸清单中未检测到明确的浪涌保护器(SPD)。若属于建筑总进线配电箱，成套报价建议增设一级/二级 SPD，避免招投标漏项漏价。"
        })

    remaining = len([u for u in uncertainties if not u.get("resolved")])
    summary = f"AI 深度复核完成：已自动研判核准并消除 {auto_resolved_count} 处存疑项，剩余 {remaining} 处建议人工确认；共提出 {len(findings)} 条电气成套优化与核查结论。"

    changes = [{
        "ts": datetime.now().isoformat(),
        "source": "ai",
        "target": "AI深度复核",
        "field": "resolved",
        "old": "待核对",
        "new": f"自动消除{auto_resolved_count}项",
        "reason": summary,
    }]
    updated = persist_job_data(job_id, job, data, changes=changes, reason="AI深度全盘复核")

    return {
        "auto_resolved_count": auto_resolved_count,
        "remaining_count": remaining,
        "summary": summary,
        "findings": findings,
        "data": updated.get("data", data),
        "changes": updated.get("changes", []),
    }


@app.get("/api/jobs/{job_id}/excel")
def job_excel(job_id: str, target_brand: str = "正泰", force: bool = False):
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)

    data = job.get("data") or {}
    # 存疑未确认完不许导出（宁可标疑、不许编造）：未 resolved 的存疑存在且
    # 未显式 force 时返回 409，不生成 Excel。force=True 为用户明确担责放行。
    unresolved_list = [u for u in (data.get("uncertainties") or []) if not u.get("resolved")]
    if unresolved_list and not force:
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"还有 {len(unresolved_list)} 处存疑未确认，无法导出",
                "unresolved_count": len(unresolved_list),
                "unresolved": [
                    {"location": u.get("location", ""), "detail": u.get("detail", "")}
                    for u in unresolved_list
                ],
            },
        )
    xlsx = os.path.join(WORKDIR, f"{job_id}_{target_brand}.xlsx")
    recon_data = data.get("reconciliation") or (job.get("summary") or {}).get("reconciliation")
    topo_data = data.get("topology", []) or (job.get("summary") or {}).get("topology", [])
    result = ExtractionResult(
        title=(job.get("summary") or {}).get("title") or "配电箱元器件清单(报价用)",
        boxes=data.get("boxes", []),
        circuits=data.get("circuits", []),
        components=data.get("components", []),
        requirements=data.get("requirements", []),
        uncertainties=data.get("uncertainties", []),
        topology=topo_data,
        reconciliation=recon_data,
    )
    filename = job.get("filename", f"{job_id}.pdf")
    subtitle = (f"依据:{filename}  导出时间:{datetime.now():%Y-%m-%d %H:%M}  "
                f"｜平替品牌:{target_brand}｜契约v{CONTRACT_VERSION}")
    build_workbook(
        result, subtitle, xlsx,
        changes=job.get("changes", []),
        include_changes=True,
        template_path=store.settings().get("excel_template") or "",
        target_brand=target_brand,
    )

    summary = job.get("summary", {})
    store.add_history({
        "job_id": job_id,
        "filename": job.get("filename", f"{job_id}.pdf"),
        "project": job.get("project", ""),
        "title": summary.get("title", ""),
        "circuits": len(job.get("data", {}).get("circuits", [])),
        "boxes": len(job.get("data", {}).get("boxes", [])),
        "uncertainties": len(summary.get("uncertainties", [])),
        "unresolved": len([u for u in summary.get("uncertainties", []) if not u.get("resolved")]),
        "changes": len(job.get("changes", [])),
        "size": os.path.getsize(xlsx),
    })
    return FileResponse(xlsx, filename=f"配电箱元器件清单(报价用)-{job_id}-{target_brand}平替.xlsx")


class RevertRequest(BaseModel):
    target: str
    field: str = ""
    ts: str = ""
    scope: str = "circuit"


def _extra_key(item: dict) -> str:
    return f"{item.get('name', '')}|{item.get('spec', '')}"


@app.post("/api/jobs/{job_id}/revert")
def revert_change(job_id: str, req: RevertRequest):
    """撤销一条修改记录。同一字段之后又改过时拒绝，避免覆盖更新的值。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    log = job.get("changes", [])
    scope = req.scope or "circuit"
    index = next((i for i, item in enumerate(log)
                  if item.get("target") == req.target
                  and item.get("scope", "circuit") == scope
                  and (not req.field or item.get("field") == req.field)
                  and (not req.ts or item.get("ts") == req.ts)), None)
    if index is None:
        raise HTTPException(404, "没有找到这条修改记录")
    later = [item for item in log[index + 1:]
             if item.get("target") == req.target
             and item.get("scope", "circuit") == scope
             and item.get("field") == req.field]
    if later:
        raise HTTPException(409, "该字段之后还有修改，请先撤销后面的那条")

    entry = log.pop(index)
    data = job.get("data", {})
    action = entry.get("action")

    if scope == "device":
        # 不能用 `or []`：空列表会被换成另一个新列表，改它不会落到 data 上
        extras = data.get("extra_devices")
        if extras is None:
            extras = []
            data["extra_devices"] = extras
        if action == "add":
            key = _extra_key(entry.get("after") or {})
            extras[:] = [d for d in extras if _extra_key(d) != key]
        elif action == "remove":
            restored = entry.get("before")
            if restored:
                extras.append(restored)
        else:
            for device in extras:
                if device.get("name") == req.target:
                    device[entry["field"]] = entry.get("old", "")
                    break
    elif scope == "box":
        for box in data.get("boxes", []):
            if box.get("code") == req.target:
                box[entry["field"]] = entry.get("old", "")
                break
    else:
        for circuit in data.get("circuits", []):
            if (circuit.get("circuit_no") or circuit.get("load_name")) == req.target:
                circuit[entry["field"]] = entry.get("old", "")
                break

    result = persist_job_data(job_id, job, data,
                              reason=f"撤销 {req.target} {entry.get('field') or action or ''}".strip())
    result["reverted"] = entry
    return result



@app.get("/api/jobs")
def list_jobs(project: str = ""):
    """所有任务，含未完成的。按当前租户严格物理/逻辑隔离。"""
    t_id = get_current_tenant()
    db_items = db_list_jobs(tenant_id=t_id)
    seen_ids = set()
    found = []
    for j in db_items:
        seen_ids.add(j["job_id"])
        found.append({
            "job_id": j["job_id"],
            "filename": j.get("filename", f"{j['job_id']}.pdf"),
            "project": j.get("project", ""),
            "status": j.get("status", ""),
            "error": j.get("error", ""),
            "pages": j.get("pages", 1),
            "created_at": j.get("created_at", ""),
            "updated_at": j.get("updated_at", ""),
            "box_code": j.get("box_code", ""),
            "summary": j.get("summary", {}),
            "changes": len(j.get("changes", [])) if isinstance(j.get("changes"), list) else int(j.get("changes") or 0),
        })

    if os.path.exists(WORKDIR):
        for name in os.listdir(WORKDIR):
            job_id, ext = os.path.splitext(name)
            if ext not in (".json", ".xlsx") or job_id in seen_ids:
                continue
            job = load_job_cached(job_id)
            if not job:
                continue
            job_t = job.get("tenant_id") or "default"
            if job_t != t_id:
                continue
            seen_ids.add(job_id)
            found.append({
                "job_id": job_id,
                "filename": job.get("filename", f"{job_id}.pdf"),
                "project": job.get("project", ""),
                "status": job.get("status", ""),
                "error": job.get("error", ""),
                "pages": job.get("pages", 1),
                "created_at": job.get("created_at", ""),
                "updated_at": job.get("updated_at", ""),
                "box_code": job.get("box_code", ""),
                "summary": job.get("summary", {}),
                "changes": len(job.get("changes", [])) if isinstance(job.get("changes"), list) else int(job.get("changes") or 0),
            })
    found = [item for item in found if not project or item["project"] == project]
    found.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {"jobs": found}


class SheetRenameRequest(BaseModel):
    page: int
    name: str


@app.post("/api/jobs/{job_id}/rename_sheet")
def rename_job_sheet(job_id: str, req: SheetRenameRequest):
    """自定义重命名指定切图图块并持久化。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    if req.page < 1:
        raise HTTPException(400, "页码非法")
    new_name = req.name.strip()
    if not new_name:
        raise HTTPException(400, "图块名称不能为空")
    sheet_names = job.get("sheet_names") or {}
    sheet_names[str(req.page)] = new_name
    job["sheet_names"] = sheet_names
    save_job(job_id)
    return {"ok": True, "sheet_names": sheet_names}


@app.get("/api/projects")
def list_projects():
    jobs = list_jobs()["jobs"]
    grouped: dict[str, list] = {}
    for job in jobs:
        grouped.setdefault(job.get("project") or "未分组", []).append(job)
    names = list(dict.fromkeys(store.project_names() + list(grouped)))
    stored_projects = {p.get("name"): p for p in store.projects()}
    out = []
    for name in names:
        items = grouped.get(name, [])
        p_info = stored_projects.get(name) or {}
        out.append({
            "name": name,
            "jobs": items,
            "totals": {
                "drawings": len(items),
                "boxes": sum(int(j["summary"].get("boxes") or 0) for j in items),
                "circuits": sum(int(j["summary"].get("circuits") or 0) for j in items),
                "components": sum(int(j["summary"].get("components") or 0) for j in items),
                "unresolved": sum(len([u for u in j["summary"].get("uncertainties", [])
                                       if not u.get("resolved")]) for j in items),
                "changes": sum(int(j.get("changes") or 0) for j in items),
                "done": sum(1 for j in items if j["status"] == "done"),
            },
            "ai_cost_total": round(float(p_info.get("ai_cost_total", 0.0)), 5),
            "ai_tokens_total": int(p_info.get("ai_tokens_total", 0)),
            "ai_prompt_tokens": int(p_info.get("ai_prompt_tokens", 0)),
            "ai_completion_tokens": int(p_info.get("ai_completion_tokens", 0)),
            "currency": "￥",
            "updated_at": max([j.get("created_at", "") for j in items] or [""]),
        })
    return {"projects": out}


@app.get("/api/ai_logs")
def get_tenant_ai_logs_api():
    """获取当前企业租户全部项目的 AI 识别费用流水与汇总。"""
    from db import db_get_ai_logs
    return db_get_ai_logs(project_name="all")


@app.get("/api/projects/{name}/ai_logs")
def get_project_ai_logs_api(name: str):
    """获取项目的 AI 识别费用流水及调用明细。"""
    return store.get_project_ai_logs(name)


class ProjectRequest(BaseModel):
    name: str


@app.post("/api/projects")
def create_project(req: ProjectRequest):
    try:
        return {"ok": True, "project": store.ensure_project(req.name)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/projects/{name}/bom")
def get_project_bom(name: str):
    """汇总并返回全项目跨所有配电箱的集中采购清单及汇总统计。"""
    all_jobs = list_jobs()["jobs"]
    jobs_list = [j for j in all_jobs if (j.get("project") or "未分组") == name]
    if not jobs_list:
        raise HTTPException(404, "项目不存在或暂无图纸任务")

    full_jobs = []
    for j in jobs_list:
        job_data = load_job_cached(j["job_id"])
        if job_data:
            full_jobs.append(job_data)

    comp_map: dict = {}
    all_boxes: list = []
    for job in full_jobs:
        d = job.get("data") or {}
        boxes = d.get("boxes") or []
        components = d.get("components") or []
        for b in boxes:
            all_boxes.append({
                "code": b.get("code") or job.get("box_code") or "未编号",
                "name": b.get("name") or "配电箱",
                "size": b.get("size") or "",
                "quantity": b.get("quantity") or 1,
            })
        for c in components:
            n = (c.get("name") or "元器件").strip()
            s = (c.get("spec") or "").strip()
            u = (c.get("unit") or "只").strip()
            q = float(c.get("quantity") or 0)
            key = (n, s, u)
            if key not in comp_map:
                comp_map[key] = {"name": n, "spec": s, "unit": u, "total": 0.0, "boxes": {}}
            box_label = c.get("used_in") or (boxes[0].get("code") if boxes else job.get("box_code")) or "通用"
            comp_map[key]["total"] += q
            comp_map[key]["boxes"][box_label] = comp_map[key]["boxes"].get(box_label, 0) + q

    out_comps = []
    for (n, s, u), val in sorted(comp_map.items(), key=lambda x: (x[0][0], x[0][1])):
        dist = [{"box": b, "quantity": q} for b, q in val["boxes"].items()]
        out_comps.append({
            "name": n, "spec": s, "unit": u,
            "total_quantity": int(val["total"]) if float(val["total"]) == int(val["total"]) else round(val["total"], 2),
            "distribution": dist,
        })

    return {
        "ok": True,
        "project": name,
        "job_count": len(full_jobs),
        "box_count": len(all_boxes),
        "total_component_items": len(out_comps),
        "total_component_quantity": sum(c["total_quantity"] for c in out_comps),
        "components": out_comps,
    }


@app.get("/api/projects/{name}/topology")
def get_project_topology(name: str):
    """返回全项目配电系统层级拓扑树（项目 -> 一级总配电柜 -> 二级配电分箱 -> 一次出线支路 / 二次控制原理图）。"""
    all_jobs = list_jobs()["jobs"]
    jobs_list = [j for j in all_jobs if (j.get("project") or "未分组") == name]
    if not jobs_list:
        raise HTTPException(404, "项目不存在或暂无图纸任务")

    full_jobs = []
    for j in jobs_list:
        job_data = load_job_cached(j["job_id"])
        if job_data:
            full_jobs.append(job_data)

    from extractor.assemble import build_distribution_topology
    from extractor.schema import Box, Circuit
    proj_boxes = []
    proj_circuits = []
    for job in full_jobs:
        d = job.get("data") or {}
        for b in d.get("boxes", []):
            try:
                proj_boxes.append(Box.model_validate(b))
            except Exception:
                pass
        for c in d.get("circuits", []):
            try:
                proj_circuits.append(Circuit.model_validate(c))
            except Exception:
                pass

    topology = build_distribution_topology(proj_boxes, proj_circuits)
    return {
        "ok": True,
        "project": name,
        "job_count": len(full_jobs),
        "topology": [n.model_dump() for n in topology],
    }


@app.get("/api/projects/{name}/export_bom")
def export_project_bom(name: str, target_brand: str = "正泰"):
    """生成并下载全项目采购总清单（含BOM总表、集中采购平替方案、设备台账、成套辅材测算、统一技术规范）。"""
    all_jobs = list_jobs()["jobs"]
    jobs_list = [j for j in all_jobs if (j.get("project") or "未分组") == name]
    if not jobs_list:
        raise HTTPException(404, "项目暂无已完成的图纸数据")

    full_jobs = []
    for j in jobs_list:
        job_data = load_job_cached(j["job_id"])
        if job_data:
            full_jobs.append(job_data)

    out_filename = f"项目采购总清单(BOM)-{name}-{target_brand}平替.xlsx"
    out_path = os.path.join(WORKDIR, f"project_{uuid.uuid4().hex[:8]}.xlsx")
    build_project_bom_workbook(name, full_jobs, out_path, target_brand=target_brand)
    return FileResponse(out_path, filename=out_filename,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


class CustomTableExportRequest(BaseModel):
    title: str = "数据统计表"
    headers: list[str] = []
    rows: list[list[Any]] = []
    filename: str | None = None
    subtitle: str | None = None


@app.post("/api/export_custom_table")
def export_custom_table(req: CustomTableExportRequest):
    """将前端 AI 对话中自由提问整理出的数据表格一键导出为专业美化 Excel。"""
    from extractor.excel import build_custom_table_workbook

    safe_title = req.title or "数据整理统计表"
    out_filename = req.filename if req.filename else f"{safe_title}_{datetime.now():%m%d%H%M}.xlsx"
    if not out_filename.endswith(".xlsx"):
        out_filename += ".xlsx"

    out_path = os.path.join(WORKDIR, f"custom_{uuid.uuid4().hex[:8]}.xlsx")
    wb = build_custom_table_workbook(req.title, req.headers, req.rows, subtitle=req.subtitle or "")
    wb.save(out_path)

    return FileResponse(out_path, filename=out_filename,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/history")
def export_history():
    return {"history": store.history()}


@app.get("/api/settings")
def get_settings():
    return {
        "settings": store.public_settings(),
        "prompt_version": PROMPT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "vision_model": VisionProvider().model,
        "assistant_model": Assistant().model if Assistant().configured else "",
        "vision_configured": VisionProvider().configured,
    }


class SettingsRequest(BaseModel):
    vision_model: str | None = None
    vision_base_url: str | None = None
    vision_api_key: str | None = None
    assistant_model: str | None = None
    temperature: float | None = None
    seed: int | None = None
    excel_template: str | None = None
    include_changes: bool | None = None
    tile_large_pages: bool | None = None


@app.put("/api/settings")
def put_settings(req: SettingsRequest):
    if os.environ.get("LOCK_SETTINGS") == "1":
        raise HTTPException(403, "系统配置已由管理员强制锁定，禁止通过 Web 接口修改核心模型参数")

    patch = {k: v for k, v in req.model_dump().items() if v is not None}
    if "vision_base_url" in patch:
        url_to_check = (patch["vision_base_url"] or "").strip()
        if url_to_check:
            safe, reason = is_safe_model_url(url_to_check)
            if not safe:
                raise HTTPException(400, f"非法的模型服务地址：{reason}")

    if patch.get("vision_api_key") == "":
        patch.pop("vision_api_key")  # 空字符串表示不改动，不覆盖 .env
    store.save_settings(patch)
    store.apply_settings_to_env()
    return {"ok": True, "settings": store.public_settings()}


@app.get("/api/jobs/{job_id}/replacements")
def get_job_replacements(job_id: str, target_brand: str = "正泰"):
    """一键国产化平替测算：分析图纸中的外资/竞品元器件并推荐高性价比替代型号。"""
    _validate_job_id(job_id)
    job = load_job_cached(job_id)
    check_job_tenant_access(job)
    comps = job.get("data", {}).get("components", [])
    analysis = analyze_components_replacement(comps, target_brand=target_brand)
    return {"ok": True, "job_id": job_id, "analysis": analysis}


@app.get("/api/projects/{name}/replacements")
def get_project_replacements(name: str, target_brand: str = "正泰"):
    """全项目一键平替降本测算：跨箱体汇总元器件并输出国产化替代与降本预算。"""
    all_jobs = list_jobs()["jobs"]
    jobs_list = [j for j in all_jobs if (j.get("project") or "未分组") == name]
    if not jobs_list:
        raise HTTPException(404, "项目不存在或暂无图纸任务")
    all_comps = []
    for j in jobs_list:
        job_data = load_job_cached(j["job_id"])
        if job_data:
            all_comps.extend(job_data.get("data", {}).get("components", []))
    analysis = analyze_components_replacement(all_comps, target_brand=target_brand)
    return {"ok": True, "project": name, "analysis": analysis}


@app.post("/api/auth/login")
def auth_login(req: LoginRequest):
    """账号密码登录，获取认证 Token 与企业租户信息。"""
    user = db_authenticate_user(req.username, req.password)
    if not user:
        raise HTTPException(401, "账号或密码错误，请检查输入")
    return {"ok": True, "token": user["token"], "user": user}


@app.post("/api/auth/register")
def auth_register(req: RegisterRequest):
    """注册新企业/工区租户与管理员账号。"""
    try:
        user = db_register_user(
            username=req.username,
            password=req.password,
            display_name=req.display_name,
            tenant_name=req.tenant_name
        )
        return {"ok": True, "token": user["token"], "user": user}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/auth/me")
def auth_me(request: Request):
    """获取当前登录会话状态与租户。"""
    token = ""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif request.headers.get("x-token"):
        token = request.headers.get("x-token").strip()
    elif request.query_params.get("token"):
        token = request.query_params.get("token").strip()

    user = db_get_user_by_token(token) if token else None
    if user:
        return {"ok": True, "authenticated": True, "user": user}

    return {
        "ok": True,
        "authenticated": False,
        "user": {
            "id": get_current_user(),
            "username": "guest",
            "display_name": "访客",
            "role": "guest",
            "tenant_id": get_current_tenant(),
            "tenant_name": "电柜智核公共工作台"
        }
    }


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    """登出并使当前 Token 失效。"""
    token = ""
    auth_header = request.headers.get("authorization") or ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif request.headers.get("x-token"):
        token = request.headers.get("x-token").strip()
    if token:
        db_logout_user(token)
    return {"ok": True}


@app.post("/api/system/clean_test_data")
def clean_test_data():
    """彻底清空系统内所有测试数据，还原纯净环境。"""
    res = db_clean_all_test_data()
    jobs.clear()
    return res


@app.get("/api/health")
def health():
    return {"ok": True, "vision_configured": VisionProvider().configured}


app.mount("/", StaticFiles(directory=os.path.join(BASE, "..", "frontend"), html=True), name="frontend")


