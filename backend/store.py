# -*- coding: utf-8 -*-
"""项目、导出历史与运行设置的落盘。文件都放在 backend/data 下，纯 JSON，无数据库。

设置项可以覆盖 .env 里的同名配置：进程读取时先看设置文件，再回退到环境变量。
API Key 只在服务端流转，出参一律脱敏。
"""
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")

PROJECTS_FILE = os.path.join(DATA_DIR, "projects.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

DEFAULT_SETTINGS = {
    "vision_model": "",
    "vision_base_url": "",
    "vision_api_key": "",       # 空表示沿用 .env
    "assistant_model": "",
    "temperature": 0,
    "seed": None,
    "excel_template": "",       # 自定义模板路径，空表示用内置版式
    "include_changes": True,
    "tile_large_pages": True,   # 大图（长边 >500mm）切块识别，小图不受影响
}


def _load(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _save(path: str, payload) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------- 设置 ----------

def settings() -> dict:
    return {**DEFAULT_SETTINGS, **_load(SETTINGS_FILE, {})}


def save_settings(patch: dict) -> dict:
    merged = {**settings(), **{k: v for k, v in patch.items() if k in DEFAULT_SETTINGS}}
    _save(SETTINGS_FILE, merged)
    return merged


def public_settings() -> dict:
    """出参：Key 只回传“是否已配置”，不泄露内容。"""
    current = settings()
    key = current.get("vision_api_key") or os.environ.get("VISION_API_KEY", "")
    return {
        **{k: v for k, v in current.items() if k != "vision_api_key"},
        "vision_api_key_set": bool(key),
        "vision_api_key_hint": f"***{key[-4:]}" if len(key) > 8 else ("***" if key else ""),
    }


def apply_settings_to_env() -> None:
    """设置优先于 .env；只覆盖非空值，避免把 .env 里的 Key 抹掉。"""
    current = settings()
    for key, env_name in (("vision_model", "VISION_MODEL"),
                          ("vision_base_url", "VISION_BASE_URL"),
                          ("vision_api_key", "VISION_API_KEY"),
                          ("assistant_model", "ASSISTANT_MODEL")):
        value = current.get(key)
        if value:
            os.environ[env_name] = str(value)
    if current.get("temperature") is not None:
        os.environ["VISION_TEMPERATURE"] = str(current["temperature"])
    if current.get("seed") not in (None, ""):
        os.environ["VISION_SEED"] = str(current["seed"])
    else:
        os.environ.pop("VISION_SEED", None)


from db import (
    db_ensure_project, db_list_projects, db_record_ai_usage, db_get_ai_logs,
    db_add_history, db_get_history, get_current_tenant
)


# ---------- 项目 ----------

def projects() -> list[dict]:
    return db_list_projects()


def ensure_project(name: str) -> dict:
    return db_ensure_project(name)


def project_names() -> list[str]:
    return [item["name"] for item in projects() if item.get("name")]


def record_project_ai_usage(project_name: str, usage_summary: dict, job_id: str = "", filename: str = "") -> dict:
    """持久化记录项目的 AI 调用 token、费用汇总与详细日志（原子事务入库）。"""
    db_record_ai_usage(project_name, usage_summary, job_id=job_id, filename=filename)
    return get_project_ai_logs(project_name)


def get_project_ai_logs(project_name: str) -> dict:
    """获取项目的 AI 调用费用汇总与完整流水日志。"""
    return db_get_ai_logs(project_name)


# ---------- 导出历史 ----------

def history(limit: int = 100) -> list[dict]:
    return db_get_history(limit=limit)


def add_history(entry: dict) -> dict:
    return db_add_history(entry)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# 自动平滑迁移老版本 JSON 数据到 SQLite 数据库 (仅当首次升级且 DB 为空时执行)
def _migrate_json_to_db_once():
    try:
        current_projs = db_list_projects()
        if not current_projs and os.path.exists(PROJECTS_FILE):
            raw_projs = _load(PROJECTS_FILE, [])
            for p in raw_projs:
                pname = p.get("name")
                if pname:
                    db_ensure_project(pname)
                    # 迁移历史累积账单与调用流水
                    logs = p.get("ai_logs") or []
                    for lg in logs:
                        summary = {
                            "prompt_tokens": lg.get("prompt_tokens", 0),
                            "completion_tokens": lg.get("completion_tokens", 0),
                            "total_tokens": lg.get("total_tokens", 0),
                            "cost_in": lg.get("cost_in", 0.0),
                            "cost_out": lg.get("cost_out", 0.0),
                            "total_cost": lg.get("total_cost", 0.0),
                            "model": lg.get("model", ""),
                            "calls_count": lg.get("calls_count", 1),
                            "currency": lg.get("currency", "￥"),
                            "logs": lg.get("details", []),
                        }
                        db_record_ai_usage(pname, summary, job_id=lg.get("job_id", ""), filename=lg.get("filename", ""))

        current_hist = db_get_history(limit=1)
        if not current_hist and os.path.exists(HISTORY_FILE):
            raw_hist = _load(HISTORY_FILE, [])
            for h in reversed(raw_hist):
                db_add_history(h)
    except Exception as exc:
        print(f"[migrate] 数据自动迁移提示: {exc}")


_migrate_json_to_db_once()
