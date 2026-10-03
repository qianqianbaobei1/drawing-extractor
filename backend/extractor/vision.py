import base64
import concurrent.futures
from datetime import datetime
import json
import os
import re
import threading
import urllib.request
from pydantic import ValidationError

import ipaddress
import socket
from urllib.parse import urlparse

from .schema import (Box, Circuit, ExtraDevice, RawExtraction, Requirement, Uncertainty)

REQUIRED_SECTIONS = {"boxes", "circuits", "extra_devices", "requirements", "uncertainties"}


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
        if ip.is_loopback:
            return False, f"安全拦截：禁止使用本地回环地址 {ip}"
        if ip.is_private:
            return False, f"安全拦截：禁止访问私有内网网段 {ip}"
        if ip.is_link_local:
            return False, f"安全拦截：禁止访问链路本地/云元数据网段 {ip}"
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False, f"安全拦截：禁止访问特殊保留网段 {ip}"

    return True, ""


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> dict:
    """按模型官方定价估算 Token 调用费用（单位：元）。"""
    m = (model or "").lower()
    if "deepseek" in m:
        rate_in = 1.0 / 1_000_000
        rate_out = 2.0 / 1_000_000
    elif "gpt-4o-mini" in m:
        rate_in = 1.1 / 1_000_000
        rate_out = 4.4 / 1_000_000
    elif "gpt-4" in m:
        rate_in = 18.0 / 1_000_000
        rate_out = 72.0 / 1_000_000
    elif "qwen" in m:
        rate_in = 1.5 / 1_000_000
        rate_out = 3.5 / 1_000_000
    else:
        rate_in = 2.0 / 1_000_000
        rate_out = 4.0 / 1_000_000

    cost_in = prompt_tokens * rate_in
    cost_out = completion_tokens * rate_out
    total_cost = cost_in + cost_out
    return {
        "currency": "￥",
        "cost_in": round(cost_in, 5),
        "cost_out": round(cost_out, 5),
        "total_cost": round(total_cost, 5),
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


def post_chat(base_url: str, api_key: str, payload: dict, timeout: int = 300) -> dict:
    """OpenAI 兼容的 chat/completions 调用，视觉提取与助手问答共用。"""
    safe, reason = is_safe_model_url(base_url)
    if not safe:
        raise ValueError(f"模型外联调用受阻：{reason}")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


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
    )


def concat_results(results: list) -> RawExtraction:
    """跨页拼接，不去重：不同页上编号相同的箱体要留给 assemble 报重复。"""
    out = RawExtraction(boxes=[], circuits=[], extra_devices=[],
                        requirements=[], uncertainties=[])
    for raw in results:
        out.boxes.extend(raw.boxes)
        out.circuits.extend(raw.circuits)
        out.extra_devices.extend(raw.extra_devices)
        out.requirements.extend(raw.requirements)
        out.uncertainties.extend(raw.uncertainties)
    return out


def repair_truncated_json(s: str) -> dict | None:
    """诊断工具：从因 Token 长度限制截断的 JSON 文本中挽救已完整生成的前半截。

    仅供人工诊断/排查看截断点前识别了多少内容。**禁止**用于"自愈继续"——
    用残缺数据生成 Excel 会静默丢回路，违反"宁可标疑、不许编造"与输出契约
    （截断 → 任务失败，不生成 Excel）。"""
    s = s.strip()
    if not s.startswith("{"):
        idx = s.find("{")
        if idx == -1:
            return None
        s = s[idx:]

    for end_idx in range(len(s), max(0, len(s) - 2000), -1):
        candidate = s[:end_idx].rstrip()
        if candidate.endswith(","):
            candidate = candidate[:-1]
        for closer in ["]}", "}", "]}}", "]}"]:
            try:
                obj = json.loads(candidate + closer)
                if isinstance(obj, dict) and ("boxes" in obj or "circuits" in obj):
                    for k in ["boxes", "circuits", "extra_devices", "requirements", "uncertainties"]:
                        obj.setdefault(k, [])
                    return obj
            except Exception:
                pass
    return None


class VisionProvider:
    """Calls an OpenAI-compatible vision chat API and returns parsed JSON."""

    def __init__(self):
        self.api_key = os.environ.get("VISION_API_KEY", "")
        self.base_url = os.environ.get("VISION_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("VISION_MODEL", "gpt-4o")
        self.temperature = _float_env("VISION_TEMPERATURE", 0.0)
        self.seed = os.environ.get("VISION_SEED") or None
        self.last_call_logs = []
        self.last_usage_summary = {}
        self._log_lock = threading.Lock()
        with open(os.path.join(os.path.dirname(__file__), "..", "prompts", "extract.txt"),
                  encoding="utf-8") as f:
            self.system_prompt = f.read()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _call(self, payload: dict) -> dict:
        return post_chat(self.base_url, self.api_key, payload)

    def _record_usage(self, data: dict) -> dict:
        usage = data.get("usage") or {}
        p_tok = int(usage.get("prompt_tokens", 0) or 0)
        c_tok = int(usage.get("completion_tokens", 0) or 0)
        t_tok = int(usage.get("total_tokens", p_tok + c_tok) or 0)
        cost_info = estimate_cost(self.model, p_tok, c_tok)
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.model,
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "total_tokens": t_tok,
            "cost_in": cost_info["cost_in"],
            "cost_out": cost_info["cost_out"],
            "total_cost": cost_info["total_cost"],
            "currency": cost_info["currency"],
        }
        with self._log_lock:
            self.last_call_logs.append(entry)
        return entry

    def _summarize_usage(self) -> dict:
        with self._log_lock:
            total_p = sum(l["prompt_tokens"] for l in self.last_call_logs)
            total_c = sum(l["completion_tokens"] for l in self.last_call_logs)
            total_t = sum(l["total_tokens"] for l in self.last_call_logs)
            total_in = round(sum(l["cost_in"] for l in self.last_call_logs), 5)
            total_out = round(sum(l["cost_out"] for l in self.last_call_logs), 5)
            total_all = round(sum(l["total_cost"] for l in self.last_call_logs), 5)
            self.last_usage_summary = {
                "model": self.model,
                "calls_count": len(self.last_call_logs),
                "prompt_tokens": total_p,
                "completion_tokens": total_c,
                "total_tokens": total_t,
                "cost_in": total_in,
                "cost_out": total_out,
                "total_cost": total_all,
                "currency": "￥",
                "logs": list(self.last_call_logs),
            }
            return self.last_usage_summary

    def extract(self, items: list, on_progress=None, cad_texts: list | None = None) -> RawExtraction:
        """items 形如 [(图片路径, 页码, 该图对应整页的归一化区域或 None=整页)]。

        支持多线程并发调用视觉大模型，动态规避单线程网络排队延迟，
        同时按页码与切片拓扑保证结果收集与组装的绝对确定性。
        """
        with self._log_lock:
            self.last_call_logs = []
            self.last_usage_summary = {}
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

        concurrency = max(1, int(os.environ.get("VISION_CONCURRENCY", "6")))

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
                "\n\n【图纸原生 CAD 几何文本层（AutoCAD 数据库直读，字符100%精确无 OCR 误差，请结合视觉图纸优先对标以下内容）】：\n"
                + native_text[:4000]
            )
        content = [{"type": "text", "text": content_text}]
        content.append({"type": "image_url",
                        "image_url": {"url": _data_url(image_path), "detail": "high"}})
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 12000,
            "temperature": self.temperature,
        }
        if self.base_url.startswith("https://api.deepseek.com"):
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
        for attempt in range(3):
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
                if attempt == 2:
                    raise ValueError(f"模型输出格式校验失败，已重试 2 次: {e}") from e
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
                    {"type": "image_url", "image_url": {"url": image_data_url, "detail": "high"}},
                ]},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": 2500,
            "temperature": 0.0,
        }
        if self.base_url.startswith("https://api.deepseek.com"):
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
