# -*- coding: utf-8 -*-
"""生产级多租户持久化存储与原子事务数据库 (Database & Multitenancy Engine)。

采用 SQLite WAL 高并发事务模式（结构与 PostgreSQL 对齐）：
1. 彻底替代共享 JSON，杜绝并发写入冲突与事务竞争；
2. 强制绑定 tenant_id 与 user_id，实现企业级多租户数据全链路物理/逻辑隔离；
3. 进行中任务状态实时落盘，服务重启后自动标为中断并支持可靠恢复重跑；
4. 逐笔记录 AI 识别费用与 Token 消费审计流水。
"""
import contextvars
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
from datetime import datetime
from typing import Any

import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "extractor.db")

_local = threading.local()

def _hash_password(password: str, salt: str = "cabinet_core_salt_v2") -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()



def get_db_path() -> str:
    store_mod = sys.modules.get("store")
    target_dir = getattr(store_mod, "DATA_DIR", DATA_DIR) if store_mod else DATA_DIR
    return os.path.join(target_dir, "extractor.db")


_init_lock = threading.Lock()
_db_initialized: set[str] = set()


def _get_conn() -> sqlite3.Connection:
    target_path = get_db_path()
    if hasattr(_local, "conn") and _local.conn is not None:
        if getattr(_local, "conn_path", None) == target_path:
            return _local.conn
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None

    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    conn = sqlite3.connect(target_path, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 60000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    _local.conn = conn
    _local.conn_path = target_path

    if target_path not in _db_initialized:
        with _init_lock:
            if target_path not in _db_initialized:
                _init_tables(conn)
                _db_initialized.add(target_path)
    return _local.conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """初始化数据库表结构与多租户索引。"""
    c = conn or _get_conn()
    target_path = getattr(_local, "conn_path", get_db_path())
    with _init_lock:
        _init_tables(c)
        _db_initialized.add(target_path)


def _init_tables(conn: sqlite3.Connection) -> None:
    with conn:
        # 1. 租户企业表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            created_at TEXT NOT NULL
        );
        """)
        # 2. 用户成员表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT DEFAULT '',
            display_name TEXT DEFAULT '',
            role TEXT DEFAULT 'admin',
            token TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );
        """)
        for col, col_def in [
            ("password_hash", "TEXT DEFAULT ''"),
            ("display_name", "TEXT DEFAULT ''"),
            ("token", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {col_def};")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_token ON users(token);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);")

        # 3. 项目工程表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            name TEXT NOT NULL,
            ai_cost_total REAL DEFAULT 0.0,
            ai_tokens_total INTEGER DEFAULT 0,
            ai_prompt_tokens INTEGER DEFAULT 0,
            ai_completion_tokens INTEGER DEFAULT 0,
            ai_cost_in REAL DEFAULT 0.0,
            ai_cost_out REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, name)
        );
        """)
        # 4. 图纸提取任务表 (完全落盘，杜绝内存丢失)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT,
            project_name TEXT DEFAULT '',
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            pages INTEGER DEFAULT 1,
            box_code TEXT DEFAULT '',
            summary_json TEXT DEFAULT '{}',
            data_json TEXT DEFAULT '{}',
            changes_json TEXT DEFAULT '[]',
            error TEXT DEFAULT '',
            changes INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN changes_json TEXT DEFAULT '[]';")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_tenant ON jobs(tenant_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(tenant_id, project_name);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);")

        # 5. AI 调用审计与计费账本 (逐笔原子记账)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            job_id TEXT DEFAULT '',
            project_name TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            model TEXT DEFAULT '',
            calls_count INTEGER DEFAULT 1,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens INTEGER DEFAULT 0,
            cost_in REAL DEFAULT 0.0,
            cost_out REAL DEFAULT 0.0,
            total_cost REAL DEFAULT 0.0,
            currency TEXT DEFAULT '￥',
            details_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_logs_tenant_proj ON ai_logs(tenant_id, project_name);")

        # 6. 导出历史表
        conn.execute("""
        CREATE TABLE IF NOT EXISTS export_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            project_name TEXT DEFAULT '',
            filename TEXT DEFAULT '',
            circuits INTEGER DEFAULT 0,
            boxes INTEGER DEFAULT 0,
            uncertainties INTEGER DEFAULT 0,
            unresolved INTEGER DEFAULT 0,
            changes INTEGER DEFAULT 0,
            file_size INTEGER DEFAULT 0,
            exported_at TEXT NOT NULL
        );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_export_tenant ON export_history(tenant_id);")

        # 初始化默认租户与系统管理员 (开箱即用)
        conn.execute("""
        INSERT OR IGNORE INTO tenants (id, name, status, created_at)
        VALUES ('default', '电柜智核成套电气工程部', 'active', datetime('now', 'localtime'));
        """)
        admin_hash = _hash_password("admin123")
        conn.execute("""
        INSERT OR IGNORE INTO users (id, tenant_id, username, password_hash, display_name, role, created_at)
        VALUES ('admin_default', 'default', 'admin', ?, '系统工程师', 'admin', datetime('now', 'localtime'));
        """, (admin_hash,))
        conn.execute("""
        UPDATE users SET password_hash = ?, display_name = '系统工程师'
        WHERE username = 'admin' AND (password_hash IS NULL OR password_hash = '');
        """, (admin_hash,))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_json_dumps(val: Any) -> str:
    try:
        return json.dumps(val or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        try:
            return json.dumps(val or {}, default=str, ensure_ascii=False)
        except Exception:
            return "{}"


# ---------- 租户上下文 (基于 ContextVar，全链路贯通异步与线程池) ----------

_tenant_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_tenant", default="default")
_user_var: contextvars.ContextVar[str] = contextvars.ContextVar("current_user", default="admin_default")


def set_current_tenant(tenant_id: str, user_id: str | None = None) -> None:
    t_id = (tenant_id or "default").strip() or "default"
    u_id = (user_id or f"user_{t_id}").strip()
    _tenant_var.set(t_id)
    _user_var.set(u_id)


def get_current_tenant() -> str:
    return _tenant_var.get()


def get_current_user() -> str:
    return _user_var.get()


def db_ensure_tenant(tenant_id: str, name: str = "") -> dict:
    """确保租户记录存在。"""
    t_id = (tenant_id or "default").strip() or "default"
    t_name = (name or t_id).strip()
    now_str = _now()
    conn = _get_conn()
    with conn:
        conn.execute("""
        INSERT OR IGNORE INTO tenants (id, name, status, created_at)
        VALUES (?, ?, 'active', ?);
        """, (t_id, t_name, now_str))
        cur = conn.execute("SELECT * FROM tenants WHERE id = ?", (t_id,))
        row = cur.fetchone()
        return dict(row) if row else {"id": t_id, "name": t_name}


# ---------- 任务落盘与查询 (原子持久化与中断自动恢复) ----------

def db_save_job(job: dict) -> None:
    """持久化保存任务：无论进行中或已完成，实时原子落盘到 SQLite WAL 表。"""
    job_id = job.get("job_id")
    if not job_id:
        return
    tenant_id = job.get("tenant_id") or get_current_tenant()
    user_id = job.get("user_id") or get_current_user()
    p_name = (job.get("project") or "").strip()
    status = job.get("status") or "queued"
    now_str = _now()
    created_at = job.get("created_at") or now_str

    summary_str = _safe_json_dumps(job.get("summary") or {})
    data_str = _safe_json_dumps(job.get("data") or {})

    raw_changes = job.get("changes") or []
    if isinstance(raw_changes, list):
        changes_json_str = _safe_json_dumps(raw_changes)
        changes_count = len(raw_changes)
    else:
        changes_json_str = "[]"
        changes_count = int(raw_changes or 0)

    conn = _get_conn()
    with conn:
        conn.execute("""
        INSERT INTO jobs (id, tenant_id, user_id, project_name, filename, status, progress,
                          pages, box_code, summary_json, data_json, changes_json, error, changes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tenant_id = excluded.tenant_id,
            user_id = excluded.user_id,
            project_name = excluded.project_name,
            filename = excluded.filename,
            status = excluded.status,
            progress = excluded.progress,
            pages = excluded.pages,
            box_code = excluded.box_code,
            summary_json = excluded.summary_json,
            data_json = excluded.data_json,
            changes_json = excluded.changes_json,
            error = excluded.error,
            changes = excluded.changes,
            updated_at = excluded.updated_at;
        """, (
            job_id, tenant_id, user_id, p_name, job.get("filename", ""),
            status, job.get("progress", 0), job.get("pages", 1),
            job.get("box_code", ""), summary_str, data_str, changes_json_str,
            job.get("error", ""), changes_count,
            created_at, now_str
        ))


def db_get_job(job_id: str, tenant_id: str | None = None) -> dict | None:
    """获取单个任务，支持严格的租户隔离校验。"""
    conn = _get_conn()
    if tenant_id:
        cur = conn.execute("SELECT * FROM jobs WHERE id = ? AND tenant_id = ?", (job_id, tenant_id))
    else:
        cur = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_job(row)


def db_list_jobs(tenant_id: str | None = None) -> list[dict]:
    """获取当前租户下的所有图纸任务。"""
    t_id = tenant_id or get_current_tenant()
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM jobs WHERE tenant_id = ? ORDER BY created_at DESC", (t_id,))
    return [_row_to_job(r) for r in cur.fetchall()]


def db_recover_interrupted_jobs() -> int:
    """服务启动时，将处于中间流转状态的任务安全置为 interrupted，并提示可一键重试。"""
    conn = _get_conn()
    now_str = _now()
    with conn:
        cur = conn.execute("""
        UPDATE jobs
        SET status = 'interrupted',
            error = '服务重启，任务意外中断。可点击重新解析或重新发起。',
            updated_at = ?
        WHERE status IN ('queued', 'rendering', 'extracting', 'building_excel', 'converting');
        """, (now_str,))
        return cur.rowcount


def _row_to_job(row: sqlite3.Row) -> dict:
    try:
        summary = json.loads(row["summary_json"] or "{}")
    except Exception:
        summary = {}
    try:
        data = json.loads(row["data_json"] or "{}")
    except Exception:
        data = {}
    try:
        changes = json.loads(row["changes_json"] or "[]") if "changes_json" in row.keys() else []
    except Exception:
        changes = []
    return {
        "job_id": row["id"],
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "project": row["project_name"],
        "filename": row["filename"],
        "status": row["status"],
        "progress": row["progress"],
        "pages": row["pages"],
        "box_code": row["box_code"],
        "summary": summary,
        "data": data,
        "error": row["error"],
        "changes": changes,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------- 项目工程与多租户隔离 ----------

def db_ensure_project(name: str, tenant_id: str | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("项目名称不能为空")
    t_id = tenant_id or get_current_tenant()
    now_str = _now()
    conn = _get_conn()
    with conn:
        conn.execute("""
        INSERT OR IGNORE INTO projects (id, tenant_id, name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?);
        """, (f"{t_id}:{name}", t_id, name, now_str, now_str))
        cur = conn.execute("SELECT * FROM projects WHERE tenant_id = ? AND name = ?", (t_id, name))
        row = cur.fetchone()
        return dict(row) if row else {"name": name, "created_at": now_str}


def db_list_projects(tenant_id: str | None = None) -> list[dict]:
    t_id = tenant_id or get_current_tenant()
    conn = _get_conn()
    cur = conn.execute("SELECT * FROM projects WHERE tenant_id = ? ORDER BY updated_at DESC", (t_id,))
    return [dict(r) for r in cur.fetchall()]


def db_record_ai_usage(project_name: str, usage_summary: dict, job_id: str = "", filename: str = "", tenant_id: str | None = None) -> None:
    """原子记录 AI 消费账本并累加项目汇总。"""
    t_id = tenant_id or get_current_tenant()
    p_name = (project_name or "未分组").strip() or "未分组"
    now_str = _now()

    p_tokens = int(usage_summary.get("prompt_tokens", 0) or 0)
    c_tokens = int(usage_summary.get("completion_tokens", 0) or 0)
    t_tokens = int(usage_summary.get("total_tokens", p_tokens + c_tokens) or 0)
    cost_in = float(usage_summary.get("cost_in", 0.0) or 0.0)
    cost_out = float(usage_summary.get("cost_out", 0.0) or 0.0)
    total_cost = float(usage_summary.get("total_cost", cost_in + cost_out) or 0.0)

    db_ensure_project(p_name, tenant_id=t_id)

    conn = _get_conn()
    with conn:
        # 1. 插入逐笔消费流水
        conn.execute("""
        INSERT INTO ai_logs (tenant_id, job_id, project_name, filename, model, calls_count,
                             prompt_tokens, completion_tokens, total_tokens, cost_in, cost_out,
                             total_cost, currency, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            t_id, job_id, p_name, filename, usage_summary.get("model", ""),
            usage_summary.get("calls_count", 1), p_tokens, c_tokens, t_tokens,
            cost_in, cost_out, total_cost, usage_summary.get("currency", "￥"),
            json.dumps(usage_summary.get("logs", []), ensure_ascii=False),
            now_str
        ))
        # 2. 累加项目维度的总账
        conn.execute("""
        UPDATE projects SET
            ai_prompt_tokens = ai_prompt_tokens + ?,
            ai_completion_tokens = ai_completion_tokens + ?,
            ai_tokens_total = ai_tokens_total + ?,
            ai_cost_in = ROUND(ai_cost_in + ?, 5),
            ai_cost_out = ROUND(ai_cost_out + ?, 5),
            ai_cost_total = ROUND(ai_cost_total + ?, 5),
            updated_at = ?
        WHERE tenant_id = ? AND name = ?;
        """, (p_tokens, c_tokens, t_tokens, cost_in, cost_out, total_cost, now_str, t_id, p_name))


def db_get_ai_logs(project_name: str | None = None, tenant_id: str | None = None) -> dict:
    t_id = tenant_id or get_current_tenant()
    is_all = (project_name in ("all", "__all__", "*", None, ""))
    conn = _get_conn()

    if is_all:
        cur = conn.execute("""
        SELECT * FROM ai_logs WHERE tenant_id = ? ORDER BY id DESC LIMIT 500;
        """, (t_id,))
    else:
        p_name = (project_name or "未分组").strip() or "未分组"
        cur = conn.execute("""
        SELECT * FROM ai_logs WHERE tenant_id = ? AND project_name = ? ORDER BY id DESC LIMIT 500;
        """, (t_id, p_name))

    logs = []
    tot_cost = 0.0
    tot_tokens = 0
    tot_prompt = 0
    tot_comp = 0
    tot_cost_in = 0.0
    tot_cost_out = 0.0

    for r in cur.fetchall():
        try:
            dets = json.loads(r["details_json"] or "[]")
        except Exception:
            dets = []
        c_tot = float(r["total_cost"] or 0.0)
        c_in = float(r["cost_in"] or 0.0)
        c_out = float(r["cost_out"] or 0.0)
        p_tok = int(r["prompt_tokens"] or 0)
        c_tok = int(r["completion_tokens"] or 0)
        t_tok = int(r["total_tokens"] or (p_tok + c_tok))

        tot_cost += c_tot
        tot_cost_in += c_in
        tot_cost_out += c_out
        tot_tokens += t_tok
        tot_prompt += p_tok
        tot_comp += c_tok

        logs.append({
            "job_id": r["job_id"],
            "filename": r["filename"],
            "project_name": r["project_name"] or "未分组",
            "timestamp": r["created_at"],
            "model": r["model"],
            "calls_count": r["calls_count"],
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "total_tokens": t_tok,
            "cost_in": c_in,
            "cost_out": c_out,
            "total_cost": c_tot,
            "currency": r["currency"] or "￥",
            "details": dets,
        })

    if is_all:
        return {
            "project_name": "全部项目总览",
            "ai_cost_total": round(tot_cost, 5),
            "ai_tokens_total": tot_tokens,
            "ai_prompt_tokens": tot_prompt,
            "ai_completion_tokens": tot_comp,
            "ai_cost_in": round(tot_cost_in, 5),
            "ai_cost_out": round(tot_cost_out, 5),
            "currency": "￥",
            "ai_logs": logs,
        }

    p_name = (project_name or "未分组").strip() or "未分组"
    p_cur = conn.execute("SELECT * FROM projects WHERE tenant_id = ? AND name = ?", (t_id, p_name))
    p_row = p_cur.fetchone()
    if p_row:
        return {
            "project_name": p_name,
            "ai_cost_total": round(float(p_row["ai_cost_total"] or 0.0), 5),
            "ai_tokens_total": int(p_row["ai_tokens_total"] or 0),
            "ai_prompt_tokens": int(p_row["ai_prompt_tokens"] or 0),
            "ai_completion_tokens": int(p_row["ai_completion_tokens"] or 0),
            "ai_cost_in": round(float(p_row["ai_cost_in"] or 0.0), 5),
            "ai_cost_out": round(float(p_row["ai_cost_out"] or 0.0), 5),
            "currency": "￥",
            "ai_logs": logs,
        }
    return {
        "project_name": p_name,
        "ai_cost_total": round(tot_cost, 5),
        "ai_tokens_total": tot_tokens,
        "ai_prompt_tokens": tot_prompt,
        "ai_completion_tokens": tot_comp,
        "ai_cost_in": round(tot_cost_in, 5),
        "ai_cost_out": round(tot_cost_out, 5),
        "currency": "￥",
        "ai_logs": logs,
    }


# ---------- 导出历史原子持久化 ----------

def db_add_history(entry: dict, tenant_id: str | None = None) -> dict:
    t_id = tenant_id or get_current_tenant()
    now_str = _now()
    conn = _get_conn()
    with conn:
        conn.execute("""
        INSERT INTO export_history (tenant_id, job_id, project_name, filename, circuits,
                                    boxes, uncertainties, unresolved, changes, file_size, exported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """, (
            t_id, entry.get("job_id", ""), entry.get("project", "未分组"), entry.get("filename", ""),
            entry.get("circuits", 0), entry.get("boxes", 0), entry.get("uncertainties", 0),
            entry.get("unresolved", 0), entry.get("changes", 0), entry.get("size", 0),
            now_str
        ))
    return {**entry, "exported_at": now_str}


def db_get_history(limit: int = 100, tenant_id: str | None = None) -> list[dict]:
    t_id = tenant_id or get_current_tenant()
    conn = _get_conn()
    cur = conn.execute("""
    SELECT * FROM export_history WHERE tenant_id = ? ORDER BY id DESC LIMIT ?;
    """, (t_id, limit))
    out = []
    for r in cur.fetchall():
        out.append({
            "job_id": r["job_id"],
            "project": r["project_name"],
            "filename": r["filename"],
            "circuits": r["circuits"],
            "boxes": r["boxes"],
            "uncertainties": r["uncertainties"],
            "unresolved": r["unresolved"],
            "changes": r["changes"],
            "size": r["file_size"],
            "exported_at": r["exported_at"],
        })
    return out


# ---------- 账号登录、企业租户注册与用户鉴权 ----------

def db_authenticate_user(username: str, password: str) -> dict | None:
    """账号密码登录认证，成功返回用户信息与新 Token。"""
    username = (username or "").strip()
    if not username:
        return None
    pwd_hash = _hash_password(password or "")
    conn = _get_conn()
    cur = conn.execute("""
    SELECT u.id, u.tenant_id, u.username, u.display_name, u.role, u.password_hash,
           t.name as tenant_name, t.status as tenant_status
    FROM users u
    JOIN tenants t ON u.tenant_id = t.id
    WHERE u.username = ?;
    """, (username,))
    row = cur.fetchone()
    if not row:
        return None
    
    db_hash = row["password_hash"]
    if not db_hash and username == "admin":
        db_hash = _hash_password("admin123")
    
    if pwd_hash != db_hash:
        return None
    
    new_token = f"tk_{secrets.token_hex(24)}"
    with conn:
        conn.execute("UPDATE users SET token = ? WHERE id = ?", (new_token, row["id"]))
    
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "tenant_id": row["tenant_id"],
        "tenant_name": row["tenant_name"],
        "token": new_token,
    }


def db_register_user(username: str, password: str, display_name: str = "",
                     tenant_name: str = "", role: str = "admin") -> dict:
    """注册新用户与对应企业/工区租户。"""
    username = (username or "").strip()
    password = (password or "").strip()
    if not username or not password:
        raise ValueError("账号与密码不能为空")
    if len(password) < 6:
        raise ValueError("密码长度至少需为 6 位")
    
    conn = _get_conn()
    cur = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        raise ValueError("该账号已存在，请直接登录或更换账号")
    
    now_str = _now()
    t_name = (tenant_name or f"{username}的电气工坊").strip()
    t_id = f"tenant_{secrets.token_hex(6)}"
    u_id = f"user_{secrets.token_hex(6)}"
    pwd_hash = _hash_password(password)
    token = f"tk_{secrets.token_hex(24)}"
    d_name = (display_name or username).strip()

    with conn:
        conn.execute("""
        INSERT INTO tenants (id, name, status, created_at)
        VALUES (?, ?, 'active', ?);
        """, (t_id, t_name, now_str))
        
        conn.execute("""
        INSERT INTO users (id, tenant_id, username, password_hash, display_name, role, token, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """, (u_id, t_id, username, pwd_hash, d_name, role, token, now_str))
        
    return {
        "id": u_id,
        "username": username,
        "display_name": d_name,
        "role": role,
        "tenant_id": t_id,
        "tenant_name": t_name,
        "token": token,
    }


def db_get_user_by_token(token: str) -> dict | None:
    """根据 Token 查询用户及所在租户信息。"""
    if not token or not isinstance(token, str):
        return None
    token = token.strip()
    if not token:
        return None
    conn = _get_conn()
    cur = conn.execute("""
    SELECT u.id, u.tenant_id, u.username, u.display_name, u.role,
           t.name as tenant_name, t.status as tenant_status
    FROM users u
    JOIN tenants t ON u.tenant_id = t.id
    WHERE u.token = ?;
    """, (token,))
    row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "role": row["role"],
        "tenant_id": row["tenant_id"],
        "tenant_name": row["tenant_name"],
    }


def db_logout_user(token: str) -> bool:
    """退出登录并注销 Token。"""
    if not token:
        return False
    conn = _get_conn()
    with conn:
        conn.execute("UPDATE users SET token = '' WHERE token = ?", (token.strip(),))
    return True


def db_clean_all_test_data() -> dict:
    """清空系统内所有测试数据（jobs、projects、ai_logs、export_history、work 目录临时文件）。"""
    conn = _get_conn()
    with conn:
        c1 = conn.execute("SELECT count(*) FROM jobs;").fetchone()[0]
        c2 = conn.execute("SELECT count(*) FROM projects;").fetchone()[0]
        c3 = conn.execute("SELECT count(*) FROM ai_logs;").fetchone()[0]
        c4 = conn.execute("SELECT count(*) FROM export_history;").fetchone()[0]
        
        conn.execute("DELETE FROM jobs;")
        conn.execute("DELETE FROM projects;")
        conn.execute("DELETE FROM ai_logs;")
        conn.execute("DELETE FROM export_history;")
        
    # 清空 projects.json 和 history.json
    try:
        store_mod = sys.modules.get("store")
        target_dir = getattr(store_mod, "DATA_DIR", DATA_DIR) if store_mod else DATA_DIR
        proj_file = os.path.join(target_dir, "projects.json")
        hist_file = os.path.join(target_dir, "history.json")
        if os.path.exists(proj_file):
            with open(proj_file, "w", encoding="utf-8") as f:
                f.write("{}")
        if os.path.exists(hist_file):
            with open(hist_file, "w", encoding="utf-8") as f:
                f.write("[]")
    except Exception:
        pass

    # 清空 backend/work 目录下的全部临时测试文件
    work_dir = os.path.join(BASE, "work")
    deleted_files = 0
    if os.path.isdir(work_dir):
        for item in os.listdir(work_dir):
            item_path = os.path.join(work_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                    deleted_files += 1
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path, ignore_errors=True)
                    deleted_files += 1
            except Exception:
                pass

    return {
        "ok": True,
        "cleared_jobs": c1,
        "cleared_projects": c2,
        "cleared_ai_logs": c3,
        "cleared_export_history": c4,
        "deleted_work_files": deleted_files,
    }


# 初始化
init_db()

