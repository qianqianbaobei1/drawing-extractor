# -*- coding: utf-8 -*-
"""统一配置加载器：把领域词典、可标定阈值、报价口径、交付样式从代码里搬出来。

设计要点：
- `backend/config/*.json` 是随仓库发布的**出厂默认值**，不是可选项；代码读取时一定有值。
- `EXTRACTOR_CONFIG_DIR` 指向的目录可放同名 JSON，**深度合并**到出厂默认之上，
  因此现场只需覆盖要改的那几个键，其余继续跟随默认，不会因为漏写键而崩。
- 各 JSON 内的 `_env` 段声明「环境变量 -> 配置路径」映射，运行时优先级最高。
  这样 TILE_TRIGGER_MM 之类的运维开关不需要在代码里再写一遍。
- 未知键忽略、缺失键回落默认，配置写错不会让服务起不来。

读取到的配置是进程内缓存；改完文件调 reload()，或用 EXTRACTOR_CONFIG_RELOAD=1 强制不缓存。
"""
from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("EXTRACTOR_CONFIG_DIR") or (Path(__file__).resolve().parent.parent / "config"))


def _read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[config] 读取 {path} 失败，忽略该覆盖: {exc!r}")
        return {}
    return data if isinstance(data, dict) else {}


def _deep_merge(base: dict, patch: dict) -> dict:
    """递归合并：字典逐键合并，其余类型（含列表）整体替换。"""
    for key, value in patch.items():
        if key == "_env":
            continue
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _coerce(text: str, template: Any) -> Any:
    """按默认值的类型解析环境变量，避免 "12" 变成字符串走进计算。"""
    if isinstance(template, bool):
        return text.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(float(text))
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(text)
        except ValueError:
            return template
    return text


def _dig(data: dict, dotted: str) -> tuple[bool, Any]:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _assign(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            return
    node[parts[-1]] = value


def _apply_env(data: dict) -> None:
    for env_name, dotted in (data.get("_env") or {}).items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        found, current = _dig(data, dotted)
        _assign(data, dotted, _coerce(raw, current if found else raw))


@lru_cache(maxsize=None)
def _load_cached(name: str) -> dict:
    data = _read_json(CONFIG_DIR / f"{name}.json")
    if os.environ.get("EXTRACTOR_CONFIG_RELOAD") != "1":
        override_dir = os.environ.get("EXTRACTOR_CONFIG_OVERRIDE_DIR")
        if override_dir:
            override = _read_json(Path(override_dir) / f"{name}.json")
            if override:
                data = _deep_merge(data, override)
    _apply_env(data)
    return data


def load(name: str) -> dict:
    """读取一个配置段（可覆盖的字典）。返回值只读用途，不要就地修改。"""
    return copy.deepcopy(_load_cached(name))


def reload() -> None:
    _load_cached.cache_clear()


# --- 各业务段访问器 -------------------------------------------------------

def domain() -> dict:
    """领域词典与规则：图层、箱号词法、断路器分类、品牌模式、类别顺序。"""
    return load("domain")


def pipeline() -> dict:
    """可标定阈值：切片、CAD 几何、准入门禁。"""
    return load("pipeline")


def pricing_rules() -> dict:
    """报价口径：费率、品牌系数、兜底面价、铜排与钣金定额。"""
    return load("pricing")


def delivery() -> dict:
    """交付口径：Excel 样式、表名、默认平替品牌与文案。"""
    return load("delivery")


def vision() -> dict:
    """视觉模型调用参数与门禁。"""
    return load("vision")


def replacement_rules() -> dict:
    """国产化平替选型规则：品牌→系列对照、提示文案。"""
    return load("replacement")


def config_health() -> dict:
    """自检：报告实际生效的配置来源，便于现场确认覆盖是否真的加载了。"""
    files = sorted(p.name for p in CONFIG_DIR.glob("*.json")) if CONFIG_DIR.is_dir() else []
    return {
        "config_dir": str(CONFIG_DIR),
        "files": files,
        "override_dir": os.environ.get("EXTRACTOR_CONFIG_OVERRIDE_DIR", ""),
        "env_overrides_applied": sorted(
            name for name in (domain().get("_env") or {})
            if os.environ.get(name)
        ),
    }
