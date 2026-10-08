import base64
import concurrent.futures
from datetime import datetime, time as datetime_time
from functools import lru_cache
import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo
from pydantic import ValidationError

import ipaddress
import socket
from urllib.parse import urlparse

from .config import vision as _vision_config
from .schema import (Box, Circuit, ExtraDevice, RawExtraction, Requirement, Uncertainty)

# 调用参数与解析门禁全部来自 config/vision.json
_VISION = _vision_config()
_TRANSPORT = _VISION["transport"]
_GENERATION = _VISION["generation"]
_REGION_REVIEW = _GENERATION.get("region_review") or {"max_tokens": 2500, "temperature": 0.0}
_PARSE_GATE = _VISION["parse_gate"]

REQUIRED_SECTIONS = set(_PARSE_GATE["required_sections"])
JSON_REPAIR_ATTEMPTS = int(_PARSE_GATE["json_repair_attempts"])


def is_safe_model_url(url: str) -> tuple[bool, str]:
    """验证模型服务地址是否安全，防御 SSRF 与内网穿透攻击。
    严格拦截回环地址 (127.0.0.0/8, ::1)、私有内网 (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)、
    云主机元数据地址 (169.254.169.254) 以及非法非 HTTP(S) 协议。
    """
    if not url or not isinstance(url, str):
        return False, "模型服务 URL 不能为空"
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议: {parsed.scheme}，仅支持 http 或 https"
    hostname = parsed.hostname
    if not hostname:
        return False, "缺少有效的主机名"

    lower_host = hostname.lower()
    if lower_host in ("localhost", "local", "internal", "intranet") or lower_host.endswith(".local") or lower_host.endswith(".internal"):
        return False, f"安全拦截：禁止使用内网保留主机名 {hostname}"

    # 允许测试模式或开发者特许标志（仅当明确设置 ALLOW_LOCAL_MODEL=1 时允许测试桩）
    if os.environ.get("ALLOW_LOCAL_MODEL") == "1" and (lower_host == "localhost" or hostname == "127.0.0.1"):
        return True, ""

    # 知名公共大模型域名白名单（避免离线测试或未配置公网 DNS 时 getaddrinfo 报错）
    trusted_public_domains = {
        "api.openai.com", "openai.com",
        "api.deepseek.com", "deepseek.com",
        "api.anthropic.com", "anthropic.com",
        "dashscope.aliyuncs.com"
    }
    if lower_host in trusted_public_domains or any(lower_host.endswith("." + d) for d in trusted_public_domains):
        return True, ""

    try:
        ip_obj = ipaddress.ip_address(hostname)
        ips = [ip_obj]
    except ValueError:
        try:
            addr_info = socket.getaddrinfo(hostname, None)
            ips = [ipaddress.ip_address(x[4][0]) for x in addr_info]
        except Exception as e:
            return False, f"无法解析模型服务器域名 {hostname}: {e}"

    for ip in ips:
        # IPv6 Teredo 隧道前缀 (2001::/32, RFC 4380) 属于公网单播隧道，非企业私有内网资产 (fc00::/7 才是 ULA)
        if ip.version == 6 and ip in ipaddress.IPv6Network("2001::/32"):
            continue
        if ip.is_loopback:
            return False, f"安全拦截：禁止使用本地回环地址 {ip}"
        if ip.is_private:
            return False, f"安全拦截：禁止访问私有内网网段 {ip}"
        if ip.is_link_local:
            return False, f"安全拦截：禁止访问链路本地/云元数据网段 {ip}"
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, f"安全拦截：禁止访问特殊保留网段 {ip}"

    return True, ""


@lru_cache(maxsize=1)
def _load_model_pricing() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "config", "model_pricing.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _deepseek_period(now: datetime | None, windows: list[dict]) -> str:
    override = os.environ.get("VISION_PRICING_PERIOD", "").strip().lower()
    if override in {"peak", "off_peak"}:
        return override
    local_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        local_now = local_now.astimezone(ZoneInfo("Asia/Shanghai"))
    for window in windows:
        if local_now.weekday() in window.get("weekdays", []):
            start_h, start_m = map(int, window["start"].split(":"))
            end_h, end_m = map(int, window["end"].split(":"))
            if datetime_time(start_h, start_m) <= local_now.time().replace(tzinfo=None) < datetime_time(end_h, end_m):
                return "peak"
    return "off_peak"


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  prompt_cache_hit_tokens: int = 0, prompt_cache_miss_tokens: int = 0,
                  now: datetime | None = None) -> dict:
    """Use a dated, sourced rate card. Unknown models have unknown cost, never a guessed default rate."""
    pricing = _load_model_pricing()
    model_key = (model or "").strip().lower()
    model_key = pricing.get("aliases", {}).get(model_key, model_key)
    model_rates = pricing.get("models", {}).get(model_key)
    base = {
        "currency": "CNY",
        "pricing_available": False,
        "pricing_status": "unknown_model_rate",
        "pricing_source": "",
        "pricing_effective_from": "",
        "pricing_period": "unknown",
        "cost_in": 0.0,
        "cost_out": 0.0,
        "total_cost": 0.0,
    }
    if not model_rates:
        return base

    period = _deepseek_period(now, model_rates.get("peak_windows", []))
    rates = model_rates.get(period, {})
    required_rates = ("cache_hit", "cache_miss", "output")
    if any(key not in rates for key in required_rates):
        return {**base, "pricing_status": "incomplete_rate_card", "pricing_source": model_rates.get("source", "")}

    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    hit = max(0, int(prompt_cache_hit_tokens or 0))
    miss = max(0, int(prompt_cache_miss_tokens or 0))
    if hit or miss:
        # Do not charge more cache tokens than the API reports as prompt tokens.
        hit = min(hit, prompt_tokens)
        miss = min(miss, max(0, prompt_tokens - hit))
        if hit + miss < prompt_tokens:
            miss += prompt_tokens - hit - miss
    else:
        miss = prompt_tokens

    per_million = 1_000_000.0
    cost_in = (hit * float(rates["cache_hit"]) + miss * float(rates["cache_miss"])) / per_million
    cost_out = completion_tokens * float(rates["output"]) / per_million
    return {
        "currency": pricing.get("currency", "CNY"),
        "pricing_available": True,
        "pricing_status": "estimated_peak_schedule" if period == "peak" else "estimated_off_peak_schedule",
        "pricing_source": model_rates.get("source", ""),
        "pricing_effective_from": model_rates.get("effective_from", ""),
        "pricing_period": period,
        "cost_in": round(cost_in, 8),
        "cost_out": round(cost_out, 8),
        "total_cost": round(cost_in + cost_out, 8),
    }


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _data_url(image_path: str) -> str:
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:image/png;base64,{b64}"


def post_chat(base_url: str, api_key: str, payload: dict, timeout: int | None = None,
              max_retries: int | None = None) -> dict:
    timeout = int(_TRANSPORT["timeout_s"] if timeout is None else timeout)
    max_retries = int(_TRANSPORT["max_retries"] if max_retries is None else max_retries)
    """OpenAI 兼容的 chat/completions 调用，视觉提取与助手问答共用。
    具备针对 HTTP 429 限流及临时 5xx / 网络超时的指数退避重试机制。
    """
    safe, reason = is_safe_model_url(base_url)
    if not safe:
        raise ValueError(f"模型外联调用受阻：{reason}")
    req_url = f"{base_url.rstrip('/')}/chat/completions"
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    last_exc = None
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(req_url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            # 针对 429 (Too Many Requests) 或临时 502/503/504 进行指数退避重试
            if e.code in (429, 502, 503, 504) and attempt < max_retries:
                retry_after = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                if retry_after and retry_after.isdigit():
                    wait_sec = min(float(retry_after), 30.0)
                else:
                    wait_sec = (2 ** attempt) * 1.5  # 1.5s, 3.0s, 6.0s
                time.sleep(wait_sec)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionResetError) as e:
            last_exc = e
            if attempt < max_retries:
                wait_sec = (2 ** attempt) * 1.0
                time.sleep(wait_sec)
                continue
            raise
    if last_exc:
        raise last_exc


def _clip_to_page(box, clip: dict | None):
    """块内归一化坐标 → 整页归一化坐标。"""
    if clip is None:
        return box
    box.x = round(clip["x"] + box.x * clip["w"], 6)
    box.y = round(clip["y"] + box.y * clip["h"], 6)
    box.w = round(box.w * clip["w"], 6)
    box.h = round(box.h * clip["h"], 6)
    return box


def _interior_score(box, clip) -> float:
    """定位点离块边缘越远，说明这一块看得越完整，重叠区就留它。"""
    if box is None or clip is None:
        return 0.5
    cx = clip["x"] + (box.x + box.w / 2) * clip["w"]
    cy = clip["y"] + (box.y + box.h / 2) * clip["h"]
    return min(cx - clip["x"], clip["x"] + clip["w"] - cx,
               cy - clip["y"], clip["y"] + clip["h"] - cy)


def _key_circuit(c):
    return (c.box, c.circuit_no, c.phase, c.breaker, c.contactor, c.ct,
            c.thermal, c.cable, c.load_name, c.power_kw)


def _key_box(b):
    return (b.code, b.name, b.size, b.install, b.location, b.quantity)


def _key_device(d):
    return (d.name, d.spec, d.unit, d.used_in)


def _key_requirement(r):
    return (r.item, r.content)


def _key_uncertainty(u):
    return (u.location, u.detail)


def merge_parts(parts: list, ) -> RawExtraction:
    """同一页的多个块合并。块之间有重叠，同一个回路会在两块里都出现，
    保留“离块边缘最远”的那一份（就是看得最完整的那一块）。跨页不能去重，
    不同页上长得一样的备用回路是两条回路。"""
    best: dict = {}
    order: list = []
    for raw, clip in parts:
        for kind, key_fn, collection in (
            ("circuit", _key_circuit, raw.circuits),
            ("box", _key_box, raw.boxes),
            ("device", _key_device, raw.extra_devices),
            ("requirement", _key_requirement, raw.requirements),
            ("uncertainty", _key_uncertainty, raw.uncertainties),
        ):
            for item in collection:
                key = (kind, key_fn(item))
                score = _interior_score(getattr(item, "bbox", None), clip)
                if key not in best or score > best[key][0]:
                    best[key] = (score, item)
                if key not in order:
                    order.append(key)
    picked = [best[k][1] for k in order]
    return RawExtraction(
        boxes=[x for x in picked if isinstance(x, Box)],
        circuits=[x for x in picked if isinstance(x, Circuit)],
        extra_devices=[x for x in picked if isinstance(x, ExtraDevice)],
        requirements=[x for x in picked if isinstance(x, Requirement)],
        uncertainties=[x for x in picked if isinstance(x, Uncertainty)],
        project_info=_first_project_info([raw for raw, _ in parts]),
    )


def _first_project_info(raws: list):
    """一页/一张图的项目信息取第一个有内容的即可：同一份图纸的图签是一致的。"""
    for raw in raws or []:
        info = getattr(raw, "project_info", None)
        if info and any((info.name, info.code, info.client, info.designer, info.location)):
            return info
    return None


def concat_results(results: list) -> RawExtraction:
    """跨页拼接，不去重：不同页上编号相同的箱体要留给 assemble 报重复。"""
    out = RawExtraction(boxes=[], circuits=[], extra_devices=[],
                        requirements=[], uncertainties=[],
                        project_info=_first_project_info(results))
    for raw in results:
        out.boxes.extend(raw.boxes)
        out.circuits.extend(raw.circuits)
        out.extra_devices.extend(raw.extra_devices)
        out.requirements.extend(raw.requirements)
        out.uncertainties.extend(raw.uncertainties)
    return out




class VisionProvider:
    """Calls an OpenAI-compatible vision chat API and returns parsed JSON."""

    def __init__(self):
        try:
            import store
            cfg = store.settings()
        except Exception:
            cfg = {}
        self.api_key = cfg.get("vision_api_key") or os.environ.get("VISION_API_KEY", "")
        self.base_url = (cfg.get("vision_base_url") or os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.model = cfg.get("vision_model") or os.environ.get("VISION_MODEL", "gpt-4o")
        default_temp = float(_GENERATION["temperature"])
        self.temperature = (float(cfg.get("temperature", default_temp))
                            if cfg.get("temperature") is not None
                            else _float_env("VISION_TEMPERATURE", default_temp))
        self.seed = cfg.get("seed") or os.environ.get("VISION_SEED") or _GENERATION.get("seed")
        self.detail = str(_GENERATION.get("detail") or "high")
        self.max_tokens = int(_GENERATION.get("max_tokens") or 8192)
        self.last_call_logs = []
        self.last_usage_summary = {}
        self.api_call_attempts = 0
        self._log_lock = threading.Lock()
        with open(os.path.join(os.path.dirname(__file__), "..", "prompts", "extract.txt"),
                  encoding="utf-8") as f:
            self.system_prompt = f.read()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _is_deepseek(self) -> bool:
        base = (self.base_url or "").lower()
        model = (self.model or "").lower()
        return "deepseek" in base or "deepseek" in model

    def _call(self, payload: dict) -> dict:
        self.api_call_attempts += 1
        return post_chat(self.base_url, self.api_key, payload)

    def _record_usage(self, data: dict) -> dict:
        usage = data.get("usage") or {}
        usage_available = any(k in usage for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
        p_tok = int(usage.get("prompt_tokens", 0) or 0)
        c_tok = int(usage.get("completion_tokens", 0) or 0)
        t_tok = int(usage.get("total_tokens", p_tok + c_tok) or 0)
        p_hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        p_miss = int(usage.get("prompt_cache_miss_tokens", 0) or (p_tok - p_hit))
        reasoning_tok = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
        cost_info = estimate_cost(self.model, p_tok, c_tok,
                                  prompt_cache_hit_tokens=p_hit,
                                  prompt_cache_miss_tokens=p_miss)
        if not usage_available:
            cost_info = {
                **cost_info,
                "pricing_available": False,
                "pricing_status": "api_usage_missing",
            }
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.model,
            "prompt_tokens": p_tok,
            "prompt_cache_hit_tokens": p_hit,
            "prompt_cache_miss_tokens": p_miss,
            "completion_tokens": c_tok,
            "reasoning_tokens": reasoning_tok,
            "total_tokens": t_tok,
            "cost_in": cost_info["cost_in"],
            "cost_out": cost_info["cost_out"],
            "total_cost": cost_info["total_cost"],
            "currency": cost_info["currency"],
            "pricing_available": cost_info["pricing_available"],
            "pricing_status": cost_info["pricing_status"],
            "pricing_source": cost_info["pricing_source"],
            "pricing_period": cost_info["pricing_period"],
        }
        with self._log_lock:
            self.last_call_logs.append(entry)
        return entry

    def _summarize_usage(self) -> dict:
        with self._log_lock:
            total_p = sum(l.get("prompt_tokens", 0) for l in self.last_call_logs)
            total_hit = sum(l.get("prompt_cache_hit_tokens", 0) for l in self.last_call_logs)
            total_miss = sum(l.get("prompt_cache_miss_tokens", 0) for l in self.last_call_logs)
            total_c = sum(l.get("completion_tokens", 0) for l in self.last_call_logs)
            total_r = sum(l.get("reasoning_tokens", 0) for l in self.last_call_logs)
            total_t = sum(l.get("total_tokens", 0) for l in self.last_call_logs)
            total_in = round(sum(l.get("cost_in", 0.0) for l in self.last_call_logs), 5)
            total_out = round(sum(l.get("cost_out", 0.0) for l in self.last_call_logs), 5)
            known_logs = [l for l in self.last_call_logs if l.get("pricing_available")]
            total_all = round(sum(l.get("total_cost", 0.0) for l in known_logs), 8)
            total_cost_in_known = round(sum(l.get("cost_in", 0.0) for l in known_logs), 8)
            total_cost_out_known = round(sum(l.get("cost_out", 0.0) for l in known_logs), 8)
            pricing_complete = bool(self.last_call_logs) and len(known_logs) == len(self.last_call_logs)
            self.last_usage_summary = {
                "model": self.model,
                "calls_count": len(self.last_call_logs),
                "api_call_attempts": self.api_call_attempts,
                "prompt_tokens": total_p,
                "prompt_cache_hit_tokens": total_hit,
                "prompt_cache_miss_tokens": total_miss,
                "completion_tokens": total_c,
                "reasoning_tokens": total_r,
                "total_tokens": total_t,
                "cost_in": total_cost_in_known,
                "cost_out": total_cost_out_known,
                "total_cost": total_all,
                "currency": "￥",
                "pricing_available": pricing_complete,
                "pricing_status": "complete" if pricing_complete else "partial_or_unknown",
                "pricing_sources": sorted({l.get("pricing_source", "") for l in self.last_call_logs if l.get("pricing_source")}),
                "pricing_periods": sorted({l.get("pricing_period", "") for l in self.last_call_logs if l.get("pricing_period")}),
                "logs": list(self.last_call_logs),
            }
            return self.last_usage_summary

    def extract(self, items: list, on_progress=None, cad_texts: list | None = None) -> RawExtraction:
        """items 形如 [(图片路径, 页码, 该图对应整页的归一化区域或 None=整页)]。

        支持可配置的多线程调用；按输入索引收集返回结果，避免线程完成顺序改变组装顺序。
        并发、响应稳定性与内容准确率仍需按真实工作负载测量。
        """
        with self._log_lock:
            self.last_call_logs = []
            self.last_usage_summary = {}
            self.api_call_attempts = 0
        if not items:
            return RawExtraction(boxes=[], circuits=[], extra_devices=[],
                                 requirements=[], uncertainties=[])

        # 1. 预先按页建立 CAD 原生文字索引，避免在循环中对数千条 CAD 文本做重复扫描
        cad_by_page: dict[int, list[dict]] = {}
        cad_global: list[dict] = []
        if cad_texts:
            for t in cad_texts:
                p = t.get("page")
                if p is not None:
                    cad_by_page.setdefault(p, []).append(t)
                else:
                    cad_global.append(t)

        def _get_cad_text(idx: int, page: int) -> str:
            if not cad_texts:
                return ""
            page_cad = cad_by_page.get(page)
            if not page_cad:
                page_cad = cad_global
            if not page_cad and idx == 1:
                page_cad = cad_texts
            native_lines = [t.get("text", "").strip() for t in page_cad if t.get("text")]
            return "\n".join(native_lines[:300])

        default_concurrency = int(_TRANSPORT.get("concurrency") or 6)
        concurrency = max(1, int(os.environ.get("VISION_CONCURRENCY", default_concurrency)))

        # 单张切片直接在主线程执行，零线程开销
        if len(items) == 1 or concurrency == 1:
            by_page: dict = {}
            for index, (path, page, clip) in enumerate(items, 1):
                native_cad_text = _get_cad_text(index, page)
                raw = self._extract_one(path, page, clip, native_text=native_cad_text)
                by_page.setdefault(page, []).append((raw, clip))
                if on_progress:
                    on_progress(index, len(items), page)
            self._summarize_usage()
            return concat_results([merge_parts(parts) for parts in by_page.values()])

        # 多张切片并发执行
        results: list[tuple[RawExtraction, int, dict | None] | None] = [None] * len(items)
        progress_lock = threading.Lock()
        completed_count = 0

        def _worker(idx: int, path: str, page: int, clip: dict | None):
            nonlocal completed_count
            native_cad_text = _get_cad_text(idx, page)
            raw = self._extract_one(path, page, clip, native_text=native_cad_text)
            results[idx - 1] = (raw, page, clip)
            with progress_lock:
                completed_count += 1
                if on_progress:
                    on_progress(completed_count, len(items), page)
            return raw

        max_workers = min(len(items), concurrency)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(_worker, idx, path, page, clip)
                for idx, (path, page, clip) in enumerate(items, 1)
            ]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        self._summarize_usage()
        by_page = {}
        for item in results:
            if item is not None:
                raw, page, clip = item
                by_page.setdefault(page, []).append((raw, clip))
        return concat_results([merge_parts(parts) for parts in by_page.values()])

    def _extract_one(self, image_path: str, page: int, clip: dict | None, native_text: str = "") -> RawExtraction:
        if not self.api_key:
            raise RuntimeError("VISION_API_KEY 未配置")
        content_text = "请按系统提示词提取这张配电系统图中的全部元器件信息，只输出 JSON。"
        if native_text:
            content_text += (
                "\n\n【CAD 原生文字实体摘录（只是可获得的源文字记录，不保证完整、无乱码或语义正确；"
                "请与图纸渲染图逐项核对，不得默认其覆盖全部表格、符号或回路。若二者冲突，列入 uncertainties，"
                "不要静默覆盖图纸可见内容）】：\n"
                + native_text[:4000]
            )
        content = [{"type": "text", "text": content_text}]
        content.append({"type": "image_url",
                        "image_url": {"url": _data_url(image_path), "detail": self.detail}})
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self._is_deepseek():
            payload["thinking"] = {"type": "disabled"}
        else:
            # DeepSeek 未将 seed 列为支持参数，其他兼容接口才发送
            if self.seed is not None:
                payload["seed"] = int(self.seed)
        raw = self._call_with_repair(payload)
        self._stamp(raw, page, clip)
        return raw

    def _stamp(self, raw: RawExtraction, page: int, clip: dict | None) -> None:
        """把块坐标折回整页，并写上页码。模型并不知道自己被喂的是哪一块。"""
        for item in list(raw.circuits) + list(raw.uncertainties):
            if item.bbox is None:
                continue
            item.bbox = _clip_to_page(item.bbox, clip)
            item.bbox.page = page

    def _call_with_repair(self, payload: dict) -> RawExtraction:
        """调用并用格式修复重试；重试次数来自 config/vision.json 的 parse_gate.json_repair_attempts。"""
        max_attempt = JSON_REPAIR_ATTEMPTS
        for attempt in range(max_attempt + 1):
            try:
                data = self._call(payload)
            except urllib.error.HTTPError as e:
                # Some compatible APIs do not support forced JSON output.
                body = e.read().decode(errors="ignore")
                if e.code == 400 and "response_format" in body and "response_format" in payload:
                    del payload["response_format"]
                    data = self._call(payload)
                else:
                    raise RuntimeError(f"视觉模型调用失败(HTTP {e.code}): {body[:300]}") from e

            self._record_usage(data)
            choice = data["choices"][0]
            raw_text = choice["message"]["content"]
            if choice.get("finish_reason") == "length":
                # 截断是确定性的：不浪费重试。残缺数据绝不流入 assemble/Excel，
                # 直接失败；上游 process_pdf 会经 _fail 置 job 失败并展示该报错。
                raise ValueError("模型输出因长度限制被截断，数据不完整，未生成 Excel；请缩小图纸分块后重试")
            try:
                raw = json.loads(raw_text)
                if not isinstance(raw, dict):
                    raise ValueError("顶层必须是 JSON 对象")
                missing = REQUIRED_SECTIONS - raw.keys()
                if missing:
                    raise ValueError("缺少顶层字段: " + ", ".join(sorted(missing)))
                return RawExtraction.model_validate(raw)
            except (json.JSONDecodeError, ValidationError, ValueError) as e:
                if attempt == max_attempt:
                    raise ValueError(
                        f"模型输出格式校验失败，已重试 {max_attempt} 次: {e}") from e
                payload["messages"] = [
                    *payload["messages"],
                    {"role": "assistant", "content": raw_text[:1500]},
                    {"role": "user", "content": (
                        "上次输出的 JSON 格式或字段类型有误：" + str(e)[:1000]
                        + "。请只修复 JSON 结构和类型，保留已识别的图纸事实；"
                          "不要猜测、补造或改写无法确认的型号与数量。"
                          "只输出完整 JSON。"
                    )},
                ]

    def parse_crop(self, image_data_url: str, native_text: str = "") -> dict:
        """针对用户在图纸上框选的局部区域，识别其中的元器件与回路分支。"""
        if not self.api_key:
            raise RuntimeError("VISION_API_KEY 未配置")
        with self._log_lock:
            self.last_call_logs = []
            self.last_usage_summary = {}
            self.api_call_attempts = 0
        prompt = (
            "你是资深电气施工图审图与预算工程师。用户在配电系统图中框选了一块局部区域，"
            "请仔细观察这张局部图中的所有电气图形、文字标注、说明和符号：\n"
            "1. 识别并提取该区域内包含的所有电气元器件与回路分支（若仅为图例文本或型号解释示例，不要记为采购元器件实体）；\n"
            "2. 如果该区域包含设计说明、技术要求、施工规范、选用原则（如断路器分断能力Icu、微断/漏保选用规则、动作电流与动作时间、消防控制回路保护原则、阻燃耐温要求等），将其提取到 requirements 数组中，每条包含 item（类别/项目）和 content（具体说明内容）；\n"
            "3. 如果该区域包含对具体配电箱/配电柜的箱体说明或参数表格（如设备编号、设备名称、防护等级、安装方式、安装位置、尺寸、箱体备注等），将其提取到 box_info 对象中（字段包括 code, name, ip_rating, install, location, size, note 等，无法确认项留空）；\n"
            "4. 元器件必须包含：name（元器件名称）、spec（规格型号）、quantity（数量，数值）、unit（单位，如只/套/台/米）、note（用途或位置）；\n"
            "5. 如识别到回路，提供 circuit_no（编号）、breaker（开关型号）、cable（电缆型号）、power_kw（功率kW）、load_name（负载用途）；\n"
            "6. 给出一段扼要的整体总结 summary。\n\n"
            "只输出一个 JSON 对象，结构如下：\n"
            "{\n"
            '  "summary": "简明总结",\n'
            '  "components": [\n'
            '    {"name": "微型断路器", "spec": "MCB-63/C16A/1P", "quantity": 1, "unit": "只", "note": "照明回路"}\n'
            '  ],\n'
            '  "circuits": [\n'
            '    {"circuit_no": "WL1", "breaker": "MCB-63/C16A/1P", "cable": "BV-3x2.5", "power_kw": "1.0", "load_name": "照明"}\n'
            '  ],\n'
            '  "requirements": [\n'
            '    {"item": "微型断路器分断能力", "content": "微型断路器的额定极限短路分断能力(Icu)除注明外均采用6kA"}\n'
            '  ],\n'
            '  "box_info": {\n'
            '    "code": "5DT-AT",\n'
            '    "name": "电梯配电箱",\n'
            '    "note": "电梯具有断电自动平层开门功能"\n'
            '  },\n'
            '  "raw_text": "图面识别到的主要文字"\n'
            "}"
        )
        if native_text:
            prompt += f"\n\n从图纸该区域底层提取的原生矢量文字（供核对参考）：\n{native_text}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "你只输出合法 JSON，不添加 Markdown 块标签或多余解释。"},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": self.detail}},
                ]},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": int(_REGION_REVIEW.get("max_tokens") or 2500),
            "temperature": float(_REGION_REVIEW.get("temperature", 0.0)),
        }
        if self._is_deepseek():
            payload["thinking"] = {"type": "disabled"}

        try:
            data = self._call(payload)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            if e.code == 400 and "response_format" in body:
                del payload["response_format"]
                data = self._call(payload)
            else:
                raise RuntimeError(f"视觉解析调用失败(HTTP {e.code}): {body[:300]}") from e

        self._record_usage(data)
        self._summarize_usage()
        raw_text = data["choices"][0]["message"]["content"]
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return {
                "summary": raw_text[:200],
                "components": [],
                "circuits": [],
                "raw_text": native_text,
            }
