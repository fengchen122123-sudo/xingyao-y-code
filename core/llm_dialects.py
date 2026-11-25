from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from copy import deepcopy
from typing import Dict, List, Optional
from urllib.parse import urlsplit

import core.app_context as app_context
from core.reasoning_capabilities import (
    get_reasoning_capability,
    normalize_reasoning_model_name,
)

logger = logging.getLogger(__name__)

_REASONING_FIELD_PROVIDERS = ("anthropic", "deepseek", "moonshot")

_KIMI_NAME_RE = re.compile(r"(?:^|[^a-z0-9])k(?:2|3)(?:[._-]\d+)*(?=$|[^a-z0-9])")
_GPT_NAME_RE = re.compile(r"(?:^|[^a-z0-9])(?:gpt-[3456]|o[134](?:-|$)|codex)")
_TRANSPORT_PREFIXES = ("openai/", "anthropic/", "responses/")


def _strip_protocol_prefix(model_name):
    text = str(model_name or "").strip().lower()
    for prefix in _TRANSPORT_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):]
    return text


def _family_names(model_name):
    model = _strip_protocol_prefix(model_name)
    names = set()
    if "kimi" in model or "moonshot" in model or _KIMI_NAME_RE.search(model):
        names.add("kimi")
    if "deepseek" in model:
        names.add("deepseek")
    if "glm" in model or "zhipu" in model or "zai-org" in model or "z-ai" in model:
        names.add("glm")
    if "qwen" in model or "dashscope" in model:
        names.add("qwen")
    if "minimax" in model or "abab" in model:
        names.add("minimax")
    if "claude" in model:
        names.add("claude")
    if "gemini" in model:
        names.add("gemini")
    if "step-" in model or "stepfun" in model:
        names.add("stepfun")
    if "seed-" in model or "doubao" in model:
        names.add("seed")
    if "mimo" in model:
        names.add("mimo")
    if "longcat" in model:
        names.add("longcat")
    if "sensenova" in model:
        names.add("sensenova")
    if "agnes" in model:
        names.add("agnes")
    if not names and _GPT_NAME_RE.search(model):
        names.add("gpt")
    return frozenset(names)


def _is_kimi_family(model_name, api_base=""):
    names = _family_names(model_name)
    if "kimi" in names:
        return True
    if names:
        return False  # 名称已属其他已知族 → 域名兜底不生效
    b = str(api_base or "").lower()
    return "moonshot" in b or "kimi" in b


def _is_official_kimi_route(api_base=""):
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host == "api.kimi.com":
        return path in {"/coding", "/coding/v1", "/v1"}
    return (
        host in {"api.moonshot.cn", "api.moonshot.ai"}
        and path in {"", "/v1"}
    )


def _is_deepseek_family(model_name, api_base=""):
    names = _family_names(model_name)
    if "deepseek" in names:
        return True
    if names:
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_deepseek_route(api_base)


def _is_official_deepseek_route(api_base=""):
    normalized = str(api_base or "").rstrip("/").lower()
    return normalized in {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
    }


def _is_official_zhipu_route(api_base=""):
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    return host in {"open.bigmodel.cn", "api.z.ai"} and path in {
        "",
        "/api/paas/v4",
        "/api/coding/paas/v4",
        "/api/anthropic",
    }


def _is_glm_family(model_name, api_base=""):
    names = _family_names(model_name)
    if "glm" in names:
        return True
    if names:
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_zhipu_route(api_base)


def _is_official_dashscope_route(api_base=""):
    try:
        parsed = urlsplit(str(api_base or ""))
        host = str(parsed.hostname or "").lower()
        port = parsed.port
    except Exception:
        return False
    if (
        str(parsed.scheme or "").lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}:
        return True
    if host.endswith(".aliyuncs.com") and "dashscope" in host:
        return True
    if host.endswith(".maas.aliyuncs.com"):
        return True
    return False


def _is_qwen_family(model_name, api_base=""):
    names = _family_names(model_name)
    if "qwen" in names:
        return True
    if names:
        return False  # 名称已属其他已知族 → 域名兜底不生效
    return _is_official_dashscope_route(api_base)


_QWEN_CACHE_CONFIG_KEYS = (
    "enable_prompt_cache",
    "prompt_cache",
    "enable_cache_control",
    "cache_control",
)

_KEEPALIVE_ROUTES_CONFIG_KEY = "cache_keepalive_routes"

_keepalive_routes_cfg_cache = {"entry": None}

_AFFINITY_LANES_CONFIG_KEY = "cache_affinity_lanes"

# 历史备注：顶层 deepseek_cache_routes 配置键已废弃不再读取——is_deepseek_ledger_route
# 改为纯模型家族判定（任意网关上的 deepseek 模型一律进账本），config.py 白名单
# 保留该键仅为旧配置兼容（写入不再有任何效果）。

_qwen_cache_cfg_cache = {}
_qwen_cache_cfg_lock = threading.RLock()
_QWEN_CACHE_CFG_TTL_SECONDS = 10.0


def _qwen_cache_config_stamp():
    try:
        import config as config_module
        from core.model_manager import load_config as loader
    except Exception:
        return None
    try:
        mtime = os.stat(str(config_module._CONFIG_FILE)).st_mtime_ns
    except Exception:
        mtime = 0
    # 连同 loader 引用一起返回：持有引用可防止该对象被回收后 id 被新函数复用
    return (id(loader), loader, mtime)


def _resolve_model_config(model_name, api_base="", model_cfg=None):
    """获取模型/厂商配置（优先使用传入的 model_cfg，否则从 config.json 读取并继承 provider 缓存配置）。"""
    cfg = dict(model_cfg) if isinstance(model_cfg, dict) and model_cfg else None
    if cfg is not None and any(k in cfg for k in _QWEN_CACHE_CONFIG_KEYS):
        return cfg

    stamp = None if cfg else _qwen_cache_config_stamp()
    key = (str(model_name or "").strip(), str(api_base or "").rstrip("/").lower())
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _qwen_cache_cfg_cache.get(key)
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]

    resolved = None
    try:
        from core.model_manager import load_config

        data = load_config()
        _remember_keepalive_routes(data, stamp)
        m_str = str(model_name or "").strip()
        base_url = str(api_base or "").rstrip("/").lower()
        for provider in data.get("providers", []):
            p_base = str(provider.get("base_url") or "").rstrip("/").lower()
            for model in provider.get("models", []):
                if model.get("litellm_model") != m_str:
                    continue
                if base_url and p_base and p_base != base_url:
                    continue
                merged = {}
                for k in _QWEN_CACHE_CONFIG_KEYS:
                    if k in provider:
                        merged[k] = provider[k]
                merged.update(model)
                if cfg:
                    merged.update(cfg)
                resolved = merged
                break
            if resolved is not None:
                break
    except Exception:
        pass
    if resolved is None:
        resolved = cfg or {}
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _qwen_cache_cfg_cache[key] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, resolved,
            )
    return resolved


def _qwen_cache_enabled(model_name, api_base="", model_cfg=None):
    return _is_qwen_family(model_name, api_base)


def _qwen_cache_control_configured(model_name, api_base="", model_cfg=None):
    return _is_qwen_family(model_name, api_base)


def _normalize_route_match_key(api_base):
    raw = str(api_base or "").strip()
    if not raw:
        return ""
    candidate = raw if "://" in raw else "//" + raw
    try:
        host = str(urlsplit(candidate).hostname or "")
    except Exception:
        host = ""
    return (host or raw).lower()


def _parse_route_match_entries(data, key):
    raw = data.get(key) if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    routes = []
    for item in raw:
        entry = _normalize_route_match_key(item)
        if entry:
            routes.append(entry)
    return tuple(routes)


def _parse_keepalive_routes(data):
    return _parse_route_match_entries(data, _KEEPALIVE_ROUTES_CONFIG_KEY)


def _parse_affinity_lanes(data):
    return _parse_route_match_entries(data, _AFFINITY_LANES_CONFIG_KEY)


def _remember_keepalive_routes(data, stamp):
    if stamp is None:
        return
    routes = _parse_keepalive_routes(data)
    with _qwen_cache_cfg_lock:
        _keepalive_routes_cfg_cache["entry"] = (
            time.monotonic() + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes,
            _parse_keepalive_evidence_gate(data),
            _parse_affinity_lanes(data),
        )


def _configured_keepalive_routes():
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _keepalive_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp:
                return cached[2]
    routes = ()
    gate_enabled = True
    affinity_lanes = ()
    try:
        from core.model_manager import load_config

        data = load_config()
        routes = _parse_keepalive_routes(data)
        gate_enabled = _parse_keepalive_evidence_gate(data)
        affinity_lanes = _parse_affinity_lanes(data)
    except Exception:
        pass
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            _keepalive_routes_cfg_cache["entry"] = (
                now + _QWEN_CACHE_CFG_TTL_SECONDS, stamp, routes, gate_enabled,
                affinity_lanes,
            )
    return routes


def _keepalive_route_explicitly_enabled(api_base):
    host = _normalize_route_match_key(api_base)
    if not host:
        return False
    return any(entry in host for entry in _configured_keepalive_routes())


def _configured_affinity_lanes():
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _keepalive_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp and len(cached) >= 5:
                return cached[4]
    _configured_keepalive_routes()
    with _qwen_cache_cfg_lock:
        cached = _keepalive_routes_cfg_cache.get("entry")
        if cached and len(cached) >= 5:
            return cached[4]
    return ()


def _affinity_lane_enabled(model_name, api_base):
    host = _normalize_route_match_key(api_base)
    if not host:
        return False
    return any(entry in host for entry in _configured_affinity_lanes())


def _glm_keepalive_thinking_locked(model_name):
    if not _is_glm_family(model_name):
        return False
    from core.reasoning_capabilities import get_reasoning_capability

    capability = get_reasoning_capability(model_name)
    if capability.kind == "hidden":
        return False
    return not ({"off", "on"} & set(capability.options))


def is_deepseek_ledger_route(model_name, api_base):
    return _is_deepseek_family(model_name, api_base)


def _uses_deepseek_prompt_ledger(
    model_name, api_base, cache_scope, persist, parent_session
):
    return bool(
        persist
        and parent_session
        and str(cache_scope or "main") == "main"
        and is_deepseek_ledger_route(model_name, api_base)
    )


def _select_request_tools_for_route(model_name, api_base, tools, model_cfg=None):
    """Canonicalize tools for cache-optimized routes; preserve every other provider payload.

    覆盖 DeepSeek 家族（账本 + 工具字母序）与 Qwen 家族（断点 + 会话缓存头 +
    工具字母序）的全部渠道——tools 字母序是缓存前缀字节稳定的前提，任何开启
    缓存优化的路由都必须排序，否则 MCP 工具顺序一抖动前缀整体失效。
    """
    canonicalize = (
        _is_deepseek_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
    )
    if not canonicalize:
        return tools
    selected = deepcopy(list(tools or []))
    return sorted(
        selected,
        key=lambda tool: str(tool.get("function", {}).get("name") or ""),
    )


def _deepseek_stable_header_matches(generation, current_messages, current_tools):
    if generation["tools"] != current_tools:
        return False
    old_messages = generation["messages"]
    if not old_messages or not current_messages:
        return old_messages == current_messages
    old_system = old_messages[0] if old_messages[0].get("role") == "system" else None
    current_system = (
        current_messages[0] if current_messages[0].get("role") == "system" else None
    )
    return old_system == current_system


def _deepseek_header_rotation_reason(generation, current_messages, current_tools):
    if generation["tools"] != current_tools:
        return "header_tools_changed"
    return "header_system_changed"


def _new_tail_after_ledger_prefix(ledger_messages, current_messages):
    if current_messages[:len(ledger_messages)] == ledger_messages:
        return deepcopy(current_messages[len(ledger_messages):])
    if not ledger_messages:
        return deepcopy(current_messages)
    boundary = ledger_messages[-1]
    matches = [
        index for index, message in enumerate(current_messages)
        if message == boundary
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "DeepSeek prompt 账本无法与当前历史建立唯一追加边界"
        )
    return deepcopy(current_messages[matches[0] + 1:])


_pending_ledger_rotations = {}
_pending_ledger_rotations_lock = threading.Lock()


def mark_deepseek_generation_rotation(session_name):
    """登记指定会话在下一次账本请求时主动轮换代（幂等，一次消费）。"""
    key = str(session_name or "").strip()
    if not key:
        return
    with _pending_ledger_rotations_lock:
        _pending_ledger_rotations[key] = True


def consume_deepseek_generation_rotation(session_name) -> bool:
    """取走并清除待轮换代标记（一次登记只生效一次）。"""
    key = str(session_name or "").strip()
    if not key:
        return False
    with _pending_ledger_rotations_lock:
        return bool(_pending_ledger_rotations.pop(key, False))


def _prepare_deepseek_ledger_request(
    manager, session_name, model_name, api_base, cache_scope, messages, tools,
    *, force_new=False
):
    rotation_reason = "caller_force_new" if force_new else ""
    if not force_new and consume_deepseek_generation_rotation(session_name):
        force_new = True
        rotation_reason = "pending_rotation"
    generation = manager.get_or_create_deepseek_prompt_generation(
        session_name,
        model_name,
        api_base,
        cache_scope,
        deepcopy(messages),
        deepcopy(tools),
        force_new=force_new,
    )
    if generation.get("created"):
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason or "first_generation",
        }
    if not _deepseek_stable_header_matches(generation, messages, tools):
        rotation_reason = _deepseek_header_rotation_reason(
            generation, messages, tools
        )
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": rotation_reason,
        }
    try:
        tail = _new_tail_after_ledger_prefix(generation["messages"], messages)
    except RuntimeError:
        generation = manager.get_or_create_deepseek_prompt_generation(
            session_name,
            model_name,
            api_base,
            cache_scope,
            deepcopy(messages),
            deepcopy(tools),
            force_new=True,
        )
        return {
            "generation_id": generation["id"],
            "messages": generation["messages"],
            "tools": generation["tools"],
            "rotation_reason": "boundary_ambiguous",
        }
    if tail:
        manager.append_deepseek_prompt_messages(
            generation["id"], tail, expected_count=len(generation["messages"])
        )
        generation["messages"].extend(tail)
    return {
        "generation_id": generation["id"],
        "messages": generation["messages"],
        "tools": generation["tools"],
        "rotation_reason": "",
    }


def _deepseek_thinking_enabled(model_name, api_base="", model_cfg=None):
    if not _is_deepseek_family(model_name, api_base):
        return False
    cfg = model_cfg or {}
    if isinstance(cfg.get("enable_thinking"), bool):
        return cfg["enable_thinking"]
    if not _is_official_deepseek_route(api_base):
        return False
    model = str(model_name or "").split("/")[-1].lower()
    return "v4" in model or "reasoner" in model


_KEEPALIVE_EVIDENCE_GATE_CONFIG_KEY = "cache_keepalive_evidence_gate"
_KEEPALIVE_EVIDENCE_LOSS_THRESHOLD = 0.5
_KEEPALIVE_EVIDENCE_MIN_TURNS = 10
_KEEPALIVE_EVIDENCE_DISABLED_TTL_SECONDS = 3600.0

_keepalive_evidence_disabled_routes = {}
_keepalive_evidence_disabled_lock = threading.RLock()


def _keepalive_evidence_route_key(model_name, api_base):
    return (
        str(model_name or "").strip().lower(),
        str(api_base or "").rstrip("/").lower(),
    )


def _parse_keepalive_evidence_gate(data):
    raw = (
        data.get(_KEEPALIVE_EVIDENCE_GATE_CONFIG_KEY)
        if isinstance(data, dict)
        else None
    )
    return raw if isinstance(raw, bool) else True


def _keepalive_evidence_gate_enabled():
    stamp = _qwen_cache_config_stamp()
    now = time.monotonic()
    if stamp is not None:
        with _qwen_cache_cfg_lock:
            cached = _keepalive_routes_cfg_cache.get("entry")
            if cached and cached[0] > now and cached[1] == stamp and len(cached) >= 4:
                return bool(cached[3])
    _configured_keepalive_routes()
    with _qwen_cache_cfg_lock:
        cached = _keepalive_routes_cfg_cache.get("entry")
        if cached and len(cached) >= 4:
            return bool(cached[3])
    return True


def _disable_keepalive_lane(model_name, api_base, turns, hit_rate, ttl_seconds=None):
    """止损某车道的保活（小时级 TTL），并记录判定依据。

    日志只记键名与数字（model / api_base / turns / hit_rate / at /
    ttl_seconds），绝不记录任何 prompt 或消息正文。
    """
    ttl = (
        _KEEPALIVE_EVIDENCE_DISABLED_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _keepalive_evidence_route_key(model_name, api_base)
    with _keepalive_evidence_disabled_lock:
        _keepalive_evidence_disabled_routes[key] = time.monotonic() + ttl
    logger.warning(
        "保活按车道证据止损 model=%s api_base=%s turns=%d hit_rate=%.4f "
        "at=%s ttl_seconds=%.0f",
        model_name,
        api_base,
        int(turns),
        float(hit_rate),
        time.strftime("%Y-%m-%dT%H:%M:%S"),
        ttl,
    )
    return key


def _keepalive_evidence_route_disabled(model_name, api_base):
    key = _keepalive_evidence_route_key(model_name, api_base)
    now = time.monotonic()
    with _keepalive_evidence_disabled_lock:
        expire_at = _keepalive_evidence_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _keepalive_evidence_disabled_routes.pop(key, None)
            return False
        return True


def _keepalive_lane_disabled_by_evidence(model_name, api_base):
    """车道证据门：低命中且样本足够 → 停该车道保活；否则不动作。

    顺序即语义：开关关闭 → 不动作（与改动前逐字等价）；止损期内 → 直接停；
    样本不足 → 不动作（R3 保险丝）；命中率 >= 阈值 → 不动作（健康车道零变化）。
    """
    if not _keepalive_evidence_gate_enabled():
        return False
    if _keepalive_evidence_route_disabled(model_name, api_base):
        return True
    from core.llm_diagnostics import lane_keepalive_evidence

    turns, _prompt_tokens, _cached_tokens, hit_rate = lane_keepalive_evidence(
        model_name, api_base
    )
    if turns < _KEEPALIVE_EVIDENCE_MIN_TURNS:
        return False  # 样本不足：绝不动作（R3 保险丝）
    if hit_rate is None or hit_rate >= _KEEPALIVE_EVIDENCE_LOSS_THRESHOLD:
        return False
    _disable_keepalive_lane(model_name, api_base, turns, hit_rate)
    return True


def _supports_cache_keepalive(model_name, api_base="", model_cfg=None):
    supported = (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_glm_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
        or _keepalive_route_explicitly_enabled(api_base)
    )
    if not supported:
        return False
    return not _keepalive_lane_disabled_by_evidence(model_name, api_base)


def _supports_reasoning_fields(model_name, api_base=""):
    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _REASONING_FIELD_PROVIDERS:
        return True
    return (
        _is_kimi_family(model_name, api_base)
        or _is_deepseek_family(model_name, api_base)
        or _is_qwen_family(model_name, api_base)
    )


def _requires_tool_call_reasoning(model_name, api_base=""):
    """DeepSeek thinking 模式：带 tool_calls 的 assistant 必须回传 reasoning_content key。"""
    try:
        from core.model_manager import get_model_config_by_litellm_model
        model_cfg = get_model_config_by_litellm_model(model_name, api_base) or {}
    except Exception:
        model_cfg = {}
    return _deepseek_thinking_enabled(model_name, api_base, model_cfg)


_CACHE_CONTROL_PROVIDERS = ("anthropic",)

_cache_control_disabled_routes = {}
_cache_control_disabled_lock = threading.RLock()
_CACHE_CONTROL_REJECTION_TTL_SECONDS = 900.0


def _cache_control_route_key(model_name, api_base):
    return (str(model_name or ""), str(api_base or "").rstrip("/").lower())


def _disable_cache_control_route(model_name, api_base, ttl_seconds=None):
    ttl = (
        _CACHE_CONTROL_REJECTION_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _cache_control_route_key(model_name, api_base)
    with _cache_control_disabled_lock:
        _cache_control_disabled_routes[key] = time.monotonic() + ttl
    return key


def _cache_control_route_disabled(model_name, api_base):
    key = _cache_control_route_key(model_name, api_base)
    now = time.monotonic()
    with _cache_control_disabled_lock:
        expire_at = _cache_control_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _cache_control_disabled_routes.pop(key, None)
            return False
        return True


def _is_cache_control_rejection(api_err):
    if api_err is None:
        return False
    is_bad_request = (
        getattr(api_err, "status_code", None) == 400
        or type(api_err).__name__ == "BadRequestError"
    )
    if not is_bad_request:
        return False
    text = str(api_err).lower()
    return "cache_control" in text or "cache control" in text or "ephemeral" in text


# 被上游以 400 明确拒绝过 Kimi 缓存亲和键（prompt_cache_key / metadata.user_id）
# 的路由 -> 恢复时间戳（monotonic）。与 cache_control 降级完全同构：一次误判
# 不该把该路由的亲和优化永久关掉，TTL 到期后自动再给一次机会。
_affinity_disabled_routes = {}
_affinity_disabled_lock = threading.RLock()


def _disable_affinity_route(model_name, api_base, ttl_seconds=None):
    ttl = (
        _CACHE_CONTROL_REJECTION_TTL_SECONDS
        if ttl_seconds is None else float(ttl_seconds)
    )
    key = _cache_control_route_key(model_name, api_base)
    with _affinity_disabled_lock:
        _affinity_disabled_routes[key] = time.monotonic() + ttl
    return key


def _affinity_route_disabled(model_name, api_base):
    key = _cache_control_route_key(model_name, api_base)
    now = time.monotonic()
    with _affinity_disabled_lock:
        expire_at = _affinity_disabled_routes.get(key)
        if expire_at is None:
            return False
        if expire_at <= now:
            _affinity_disabled_routes.pop(key, None)
            return False
        return True


def _is_kimi_affinity_rejection(api_err):
    if api_err is None:
        return False
    is_bad_request = (
        getattr(api_err, "status_code", None) == 400
        or type(api_err).__name__ == "BadRequestError"
    )
    if not is_bad_request:
        return False
    text = str(api_err).lower()
    return "prompt_cache_key" in text or "user_id" in text


def _strip_cache_affinity_options(result):
    extra_body = result.get("extra_body")
    if isinstance(extra_body, dict):
        extra_body.pop("prompt_cache_key", None)
        if not extra_body:
            result.pop("extra_body", None)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("user_id", None)
        if not metadata:
            result.pop("metadata", None)


def _supports_cache_control(model_name, api_base="", model_cfg=None):
    if _cache_control_route_disabled(model_name, api_base):
        return False

    prefix = str(model_name or "").split("/")[0].lower()
    if prefix in _CACHE_CONTROL_PROVIDERS:
        return True

    return _qwen_cache_control_configured(model_name, api_base, model_cfg)


def _tool_contract_fingerprint(tools):
    payload = json.dumps(
        tools or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_CACHE_CONTROL_MAX_BREAKPOINTS = 4
_CACHE_CONTROL_STRIDE = 6


def _strip_existing_cache_control(messages):
    for msg in messages:
        if isinstance(msg, dict):
            msg.pop("cache_control", None)
            content = msg.get("content")
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        item.pop("cache_control", None)


def _is_qwen_markable_message(msg):
    if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list) and content:
        return any(
            isinstance(item, dict) and item.get("type") in ("text", "input_text")
            for item in content
        )
    return False


def _is_anthropic_markable_message(msg):
    return isinstance(msg, dict) and msg.get("role") in ("user", "assistant")


def _completed_stride_milestones(messages, end_exclusive, is_markable, max_count):
    """在 [1, end_exclusive) 内按固定步长抽取已完成分桶的末尾稳定锚点。

    分桶一旦封口（bucket_end < end_exclusive），其锚点下标就不再随后续追加而偏移，
    从而在显式断点上限（4 个）内用重叠窗口接力历史缓存，避免每次步进都把断点移走
    导致缓存退回 messages[0]。
    """
    if max_count <= 0 or end_exclusive <= _CACHE_CONTROL_STRIDE + 1:
        return []
    anchors = []
    bucket_start = 1
    while bucket_start + _CACHE_CONTROL_STRIDE <= end_exclusive:
        bucket_end = bucket_start + _CACHE_CONTROL_STRIDE
        for idx in range(bucket_end - 1, bucket_start - 1, -1):
            if is_markable(messages[idx]):
                anchors.append(idx)
                break
        bucket_start = bucket_end
    return anchors[-max_count:]


def _mark_qwen_message(msg, *, attach_cache_control):
    content = msg.get("content")
    if isinstance(content, str) and content:
        block = {"type": "text", "text": content}
        if attach_cache_control:
            block["cache_control"] = {"type": "ephemeral"}
        msg["content"] = [block]
        return True
    if isinstance(content, list) and content:
        for item in reversed(content):
            if isinstance(item, dict) and item.get("type") in ("text", "input_text"):
                if attach_cache_control:
                    item["cache_control"] = {"type": "ephemeral"}
                return True
        return False
    return False


def _apply_qwen_cache_control(messages):
    marked_indices = set()
    if messages[0].get("role") == "system":
        if _mark_qwen_message(messages[0], attach_cache_control=True):
            marked_indices.add(0)

    last_assistant_idx = None
    for i in range(len(messages) - 1, 0, -1):
        if messages[i].get("role") == "assistant" and _is_qwen_markable_message(messages[i]):
            last_assistant_idx = i
            marked_indices.add(i)
            break

    if len(messages) - 1 != last_assistant_idx:
        tail = messages[-1]
        if isinstance(tail, dict) and tail.get("role") == "user" and _is_qwen_markable_message(tail):
            marked_indices.add(len(messages) - 1)

    # 长对话（>6 条）：补齐最多 (4 - len(marked_indices)) 个已完成步长分桶的历史锚点，
    # 并保持锚点之前的所有 user / 非空 assistant 消息统一为 text block 列表形式，
    # 彻底消除断点淘汰时 str <-> list 格式翻转引发的前缀缓存全量失效。
    if len(messages) > _CACHE_CONTROL_STRIDE:
        non_system_marked = [idx for idx in marked_indices if idx > 0]
        end_exclusive = min(non_system_marked) if non_system_marked else len(messages)
        slots_left = _CACHE_CONTROL_MAX_BREAKPOINTS - len(marked_indices)
        for anchor in _completed_stride_milestones(
            messages, end_exclusive, _is_qwen_markable_message, slots_left
        ):
            marked_indices.add(anchor)

    max_non_system_idx = max((idx for idx in marked_indices if idx > 0), default=0)
    if len(messages) > _CACHE_CONTROL_STRIDE and max_non_system_idx > 0:
        for idx in range(1, max_non_system_idx + 1):
            if _is_qwen_markable_message(messages[idx]):
                _mark_qwen_message(messages[idx], attach_cache_control=(idx in marked_indices))
    else:
        for idx in sorted(marked_indices):
            if idx > 0:
                _mark_qwen_message(messages[idx], attach_cache_control=True)

    return messages


def _apply_anthropic_cache_control(messages):
    marked_indices = set()
    if messages[0].get("role") == "system":
        marked_indices.add(0)

    # <=2 条消息保持仅标记 system（兼容 Anthropic 首轮黄金字节基线）
    if len(messages) > 2:
        last_assistant_idx = None
        for i in range(len(messages) - 1, 0, -1):
            if messages[i].get("role") == "assistant":
                last_assistant_idx = i
                marked_indices.add(i)
                break

        if len(messages) - 1 != last_assistant_idx:
            tail = messages[-1]
            if isinstance(tail, dict) and tail.get("role") == "user":
                marked_indices.add(len(messages) - 1)

        if len(messages) > _CACHE_CONTROL_STRIDE:
            non_system_marked = [idx for idx in marked_indices if idx > 0]
            end_exclusive = min(non_system_marked) if non_system_marked else len(messages)
            slots_left = _CACHE_CONTROL_MAX_BREAKPOINTS - len(marked_indices)
            for anchor in _completed_stride_milestones(
                messages, end_exclusive, _is_anthropic_markable_message, slots_left
            ):
                marked_indices.add(anchor)

    for idx in sorted(marked_indices):
        messages[idx]["cache_control"] = {"type": "ephemeral"}
    return messages


def _apply_cache_control(messages, model_name, api_base="", model_cfg=None, disabled=False):
    if disabled:
        return messages
    if not _supports_cache_control(model_name, api_base, model_cfg):
        return messages
    if not messages:
        return messages

    # 清除已有标记，避免累积（_strip_media_and_merge 不传 cache_control，但兜底防御）
    _strip_existing_cache_control(messages)

    if _is_qwen_family(model_name, api_base):
        return _apply_qwen_cache_control(messages)
    return _apply_anthropic_cache_control(messages)


_vision_support_cache = {}
_vision_support_cache_lock = threading.RLock()


def _supports_vision(model_name):
    if not model_name:
        return False
    with _vision_support_cache_lock:
        return _supports_vision_locked(model_name)


def _supports_vision_llm_first(model_name):
    try:
        import core.llm as _llm
        fn = getattr(_llm, "_supports_vision", None)
        if fn is not None:
            return fn(model_name)
    except Exception:
        pass
    return _supports_vision(model_name)


def _supports_vision_locked(model_name):
    cached = _vision_support_cache.get(model_name)
    if cached is not None and cached[1] > time.monotonic():
        return cached[0]
    result = False
    try:
        from core.model_manager import load_config
        for provider in load_config().get("providers", []):
            for model in provider.get("models", []):
                if model.get("litellm_model") == model_name and "supports_vision" in model:
                    result = bool(model["supports_vision"])
                    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
                    return result
    except Exception:
        pass
    _vision_support_cache[model_name] = (result, time.monotonic() + 30)
    return result


def _flatten_media_content(content_list):
    """把多模态 list 内容折叠为纯文本：图片用短占位符，文档也折叠为占位说明，绝不内联原始 base64 数据。"""
    text_parts = []
    for item in content_list:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type in ("image", "image_url"):
                text_parts.append("[图片]")
            elif item_type == "document":
                text_parts.append("[文档]")
            else:
                text_parts.append(str(item)[:200])
        else:
            text_parts.append(str(item))
    return " ".join(text_parts)


def _requires_single_leading_system(model_name, api_base=""):
    return "neohorse" in str(model_name or "").lower()


def _merge_system_messages_into_leading(messages):
    systems = [m for m in messages if m.get("role") == "system"]
    if len(systems) <= 1:
        return messages
    merged_content = "\n\n".join(str(m.get("content") or "") for m in systems)
    rest = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": merged_content}] + rest


def _strip_media_and_merge(
    messages,
    model_name=None,
    api_base="",
    *,
    active_computer_run_id=None,
):
    from core.computer.frames import find_retained_media_index

    keep_reasoning_fields = _supports_reasoning_fields(model_name, api_base)
    keep_thinking_blocks = str(model_name or "").split("/")[0].lower() == "anthropic"
    keep_media = _supports_vision_llm_first(model_name)

    # 仅保留最近一条有效视觉输入（普通多模态最新条，或当前活跃电脑操控 run 的最新有效帧），
    # 更早历史与已失效帧全部折叠为文本占位符，避免重复发送、浪费 Token。
    last_media_idx = (
        find_retained_media_index(
            messages, active_computer_run_id=active_computer_run_id
        )
        if keep_media
        else -1
    )

    folded_count = 0
    cleaned = []
    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, list):
            if idx == last_media_idx:
                # 多模态负载保留，模型按 image_url 接收图片；
                # 剥离本地私有的 source_path 字段，避免严格校验的 provider 报 400
                cleaned_msg = {"role": role, "content": [
                    {k: v for k, v in item.items() if k != "source_path"}
                    if isinstance(item, dict) else item
                    for item in content
                ]}
            else:
                cleaned_msg = {"role": role, "content": _flatten_media_content(content)}
                folded_count += sum(
                    1
                    for item in content
                    if isinstance(item, dict) and item.get("type") in ("image", "image_url")
                )
        else:
            cleaned_msg = {"role": role, "content": str(content) if content is not None else ""}

        # 保留工具链和 provider 原生推理字段，避免工具轮次破坏签名思考块。
        if "tool_calls" in msg and msg["tool_calls"]:
            cleaned_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            cleaned_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            cleaned_msg["name"] = msg["name"]
        # 支持原生推理字段的 provider 需要收到自己上轮返回的完整消息信封。
        # 是否存在 tool_calls 不能作为删除条件：Kimi OpenAI 路由会在普通 assistant
        # 消息中返回 reasoning_content，Anthropic 路由还可能返回签名 thinking_blocks。
        if keep_reasoning_fields and msg.get("reasoning_content") is not None:
            cleaned_msg["reasoning_content"] = msg["reasoning_content"]
        if keep_reasoning_fields and keep_thinking_blocks and msg.get("thinking_blocks") is not None:
            cleaned_msg["thinking_blocks"] = deepcopy(msg["thinking_blocks"])
        # DeepSeek thinking 契约：带 tool_calls 的 assistant 必须携带
        # reasoning_content 字符串字段。流组装层正常会保留显式空串；但
        # 从数据库重建或中途切换思考开关的历史消息可能缺失该字段（None
        # 或非字符串），这里回填空串占位，避免整轮请求被契约校验卡死。
        if (
            role == "assistant"
            and cleaned_msg.get("tool_calls")
            and _requires_tool_call_reasoning(model_name, api_base)
            and not isinstance(cleaned_msg.get("reasoning_content"), str)
        ):
            cleaned_msg["reasoning_content"] = ""

        if role == "assistant" and isinstance(cleaned_msg.get("content"), str):
            if "[System: Empty message content sanitised to satisfy protocol]" in cleaned_msg["content"]:
                cleaned_msg["content"] = ""

        if role == "assistant":
            has_text = bool(cleaned_msg.get("content"))
            has_tools = bool(cleaned_msg.get("tool_calls"))
            has_reasoning = bool(cleaned_msg.get("reasoning_content") or cleaned_msg.get("thinking_blocks"))
            if not (has_text or has_tools or has_reasoning):
                continue

        cleaned.append(cleaned_msg)
        
    # 合并同角色消息 (注意：不要合并有 tool_calls 或 role 是 tool/system 的消息)
    fixed = []
    if cleaned:
        fixed.append(cleaned[0])
        for msg in cleaned[1:]:
            can_merge = (
                msg["role"] == fixed[-1]["role"] and
                msg["role"] not in ("tool", "system") and
                "tool_calls" not in msg and
                "tool_calls" not in fixed[-1] and
                "reasoning_content" not in msg and
                "reasoning_content" not in fixed[-1] and
                "thinking_blocks" not in msg and
                "thinking_blocks" not in fixed[-1] and
                not isinstance(msg["content"], list) and
                not isinstance(fixed[-1]["content"], list)
            )
            if can_merge:
                fixed[-1]["content"] = (fixed[-1]["content"] or "") + "\n" + (msg["content"] or "")
            else:
                fixed.append(msg)
    if _requires_single_leading_system(model_name, api_base):
        fixed = _merge_system_messages_into_leading(fixed)
    return fixed, folded_count, keep_media


def _resolve_cache_session_identity(cache_session_id=None):
    global _process_cache_key_fallback
    sid = str(cache_session_id or "").strip()
    if not sid:
        try:
            sid = str(app_context.get_current_session() or "").strip()
        except Exception:
            sid = ""
    if not sid:
        if not _process_cache_key_fallback:
            _process_cache_key_fallback = uuid.uuid4().hex
        sid = _process_cache_key_fallback
    return sid


def _cache_affinity_session_id(
    *,
    cache_scope="main",
    model_name="",
    api_base="",
    tool_contract="",
    cache_session_id=None,
):
    sid = _resolve_cache_session_identity(cache_session_id)

    session_hash = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    lane_material = json.dumps(
        {
            "scope": str(cache_scope or "main"),
            "model": str(model_name or "").lower(),
            "api_base": str(api_base or "").rstrip("/").lower(),
            "tool_contract": str(tool_contract or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lane_hash = hashlib.sha256(lane_material.encode("utf-8")).hexdigest()[:16]
    return f"ycode-{session_hash}-{lane_hash}"


_process_cache_key_fallback = None

_REASONING_EFFORT_LEVELS = ("low", "high", "max")
# Kimi Code / Moonshot Anthropic 兼容端点：档位翻译为原生 thinking.budget_tokens；
# kimi-for-coding（K2.8 Preview）与 K3 同为 low/high/max 三档。
_K3_REASONING_MODEL_NAMES = frozenset(
    {
        "k3",
        "k3-256k",
        "k3.1",
        "k3.1-pro",
        "k3.1-pro-highspeed",
        "kimi-k3",
        "kimi-k3-256k",
        "kimi-k3.1",
        "kimi-k3.1-pro",
        "kimi-for-coding",
    }
)
# 阶跃星辰 Anthropic 兼容端点（/step_plan/v1/messages）不接受 Anthropic 原生
# thinking/budget_tokens，推理深度由官方扩展字段 output_config.effort 控制
# （token plan 全系 LLM 均为 low/medium/high，见官方 /v1/models 的
# reasoning_effort_support_list）。自研引擎原样透传该字段。
_STEPFUN_ANTHROPIC_EFFORT_MODELS = frozenset(
    {
        "step-3.7-flash",
        "step-router-v1",
        "step-5-preview",
        "step-3.5-flash",
        "step-3.5-flash-2603",
    }
)
_ANTHROPIC_REASONING_BUDGETS = {
    "low": 1024,
    "high": 4096,
    "max": 16384,
}


def _attach_kimi_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    anthropic_route = str(model_name or "").split("/")[0].lower() == "anthropic"

    session_id = None
    if (
        _is_kimi_family(model_name, api_base)
        or _affinity_lane_enabled(model_name, api_base)
    ) and not _affinity_route_disabled(model_name, api_base):
        session_id = _cache_affinity_session_id(
            cache_scope=cache_scope,
            model_name=model_name,
            api_base=api_base,
            tool_contract=tool_contract,
            cache_session_id=cache_session_id,
        )
    if session_id:
        if anthropic_route:
            result.setdefault("metadata", {"user_id": session_id})
        else:
            result.setdefault("extra_body", {})["prompt_cache_key"] = session_id


def _attach_qwen_request_extras(
    result,
    model_name,
    model_cfg,
    *,
    api_base="",
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    scope_str = str(cache_scope or "main")
    scope_eligible = scope_str in {"main", "benchmark:main"} or scope_str.startswith("subagent")
    if scope_eligible and _qwen_cache_enabled(model_name, api_base, model_cfg):
        if not isinstance(result.get("extra_headers"), dict):
            result["extra_headers"] = {}
        result["extra_headers"]["x-dashscope-session-cache"] = "enable"


def _configured_reasoning_choice(model_cfg, capability):
    configured_choice = str(model_cfg.get("reasoning_choice") or "").strip().lower()
    if configured_choice:
        if configured_choice not in capability.options:
            raise ValueError(
                f"模型不支持思考选项 {configured_choice!r}，"
                f"仅支持 {', '.join(capability.options) or '固定开启'}"
            )
        return configured_choice

    legacy_enabled = model_cfg.get("enable_thinking")
    if legacy_enabled is False and "off" in capability.options:
        return "off"

    legacy_effort = str(model_cfg.get("reasoning_effort") or "").strip().lower()
    if legacy_effort:
        if legacy_effort not in capability.options:
            raise ValueError(
                f"模型 reasoning_effort={legacy_effort!r} 不受支持，"
                f"仅支持 {', '.join(capability.options) or '固定开启'}"
            )
        return legacy_effort

    if legacy_enabled is True and "on" in capability.options:
        return "on"
    # 历史 enable_thinking=true 迁移到无 on 档的强度模型（如 qwen 全系
    # default/off/low/medium/xhigh）：显式开启意图不能丢。default 档不注入
    # 参数（跟随服务端默认），这里取最低真实档 low——enable_thinking_effort
    # 策略会同时注入 enable_thinking=true，保住"开启思考"语义且开销最小。
    if legacy_enabled is True and "low" in capability.options:
        return "low"
    return "default"


def _apply_reasoning_choice(result, model_name, model_cfg, *, anthropic_route=False):
    from core.reasoning_capabilities import (
        get_reasoning_capability,
        normalize_reasoning_model_name,
    )

    capability = get_reasoning_capability(model_name)
    has_explicit_choice = bool(str(model_cfg.get("reasoning_choice") or "").strip())
    has_legacy_choice = (
        bool(str(model_cfg.get("reasoning_effort") or "").strip())
        or isinstance(model_cfg.get("enable_thinking"), bool)
    )
    if not has_explicit_choice and not has_legacy_choice:
        return capability

    if capability.kind == "hidden":
        return capability
    if capability.kind == "fixed":
        if has_explicit_choice:
            raise ValueError(f"模型 {model_name!r} 的思考固定开启，不能设置思考选项")
        logger.info("模型 %s 的思考固定开启，忽略历史思考配置", model_name)
        return capability

    choice = _configured_reasoning_choice(model_cfg, capability)
    if choice == "default":
        return capability

    strategy = capability.request_strategy
    if strategy == "always_on_effort":
        if anthropic_route:
            normalized_model = normalize_reasoning_model_name(model_name)
            if normalized_model in _STEPFUN_ANTHROPIC_EFFORT_MODELS:
                # 阶跃 Anthropic 兼容路由：思考深度用官方 output_config.effort 表达，
                # 不接受 K3 那套 thinking.budget_tokens（服务端按未列字段拒绝）。
                result["output_config"] = {"effort": choice}
                result["allowed_openai_params"] = ["output_config"]
                return capability
            if (
                normalized_model not in _K3_REASONING_MODEL_NAMES
                and not _is_kimi_family(model_name)
            ):
                raise ValueError("Anthropic 路由不支持该模型的 reasoning_effort 参数")
            budget_tokens = _ANTHROPIC_REASONING_BUDGETS[choice]
            result["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget_tokens,
            }
            result["max_tokens"] = max(
                int(result.get("max_tokens") or 0), budget_tokens + 1
            )
        else:
            result.setdefault("extra_body", {})["reasoning_effort"] = choice
    elif strategy == "thinking_object_effort":
        enabled = choice != "off"
        result.setdefault("extra_body", {})["thinking"] = {
            "type": "enabled" if enabled else "disabled"
        }
        if enabled:
            result["reasoning_effort"] = choice
            result["allowed_openai_params"] = ["reasoning_effort"]
        else:
            result.pop("reasoning_effort", None)
    elif strategy == "enable_thinking_effort":
        enabled = choice != "off"
        extra_body = result.setdefault("extra_body", {})
        extra_body["enable_thinking"] = enabled
        if enabled:
            extra_body["reasoning_effort"] = choice
        else:
            extra_body.pop("reasoning_effort", None)
    elif strategy == "effort_switch":
        result["reasoning_effort"] = "none" if choice == "off" else choice
        result["allowed_openai_params"] = ["reasoning_effort"]
    elif strategy == "thinking_object_toggle":
        result.setdefault("extra_body", {})["thinking"] = {
            "type": "enabled" if choice == "on" else "disabled"
        }
    elif strategy == "enable_thinking_toggle":
        result.setdefault("extra_body", {})["enable_thinking"] = choice == "on"
    elif strategy == "agnes_enable_thinking":
        # Agnes AI 的 OpenAI 兼容接口把思考开关嵌套在 chat_template_kwargs 下。
        result.setdefault("extra_body", {}).setdefault(
            "chat_template_kwargs", {}
        )["enable_thinking"] = choice == "on"
    else:
        raise ValueError(f"未实现的思考请求策略: {strategy}")
    return capability


def _assert_reasoning_request_consistent(result, capability):
    extra_body = result.get("extra_body") or {}
    top_level_thinking_enabled = (
        (result.get("thinking") or {}).get("type") == "enabled"
    )
    extra_thinking_disabled = (
        (extra_body.get("thinking") or {}).get("type") == "disabled"
        or extra_body.get("enable_thinking") is False
    )
    if top_level_thinking_enabled and extra_thinking_disabled:
        raise ValueError("请求同时包含开启和关闭两套冲突的思考参数")
    has_effort = "reasoning_effort" in result or "reasoning_effort" in extra_body
    if not has_effort:
        return

    strategy = capability.request_strategy
    if strategy in {"always_on_effort", "effort_switch"}:
        return
    if strategy == "thinking_object_effort":
        enabled = (extra_body.get("thinking") or {}).get("type") == "enabled"
    elif strategy == "enable_thinking_effort":
        enabled = extra_body.get("enable_thinking") is True
    else:
        enabled = False
    if not enabled:
        raise ValueError("请求包含思考强度，但未开启思考")


def _apply_route_request_overrides(result, model_name, api_base):
    """合并渠道请求形态覆盖；无配置时原样返回（零影响）。

    必须在 _assert_reasoning_request_consistent 之后调用：覆盖值不参与一致性
    校验，配置错误才不会抛错阻断对话（§4.4 / §4.7 的硬约束）。
    """
    from core.route_overrides import resolve_route_override

    override = resolve_route_override(model_name, api_base)
    headers = override.get("headers") or {}
    body = override.get("body") or {}
    if headers:
        merged = dict(result.get("extra_headers") or {})
        merged.update(headers)
        result["extra_headers"] = merged
    if body:
        merged = dict(result.get("extra_body") or {})
        merged.update(body)
        result["extra_body"] = merged
    return result


def _provider_cache_affinity_options(
    model_name,
    api_base,
    *,
    cache_scope,
    cache_session_id=None,
    tool_contract="",
):
    result = {}
    kimi_family = _is_kimi_family(model_name, api_base)
    lane_enabled = _affinity_lane_enabled(model_name, api_base)
    if kimi_family or lane_enabled:
        _attach_kimi_request_extras(
            result,
            model_name,
            {},
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    if _is_qwen_family(model_name, api_base) and not kimi_family:
        _attach_qwen_request_extras(
            result,
            model_name,
            {},
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    return result


_BUILTIN_DEFAULT_MAX_TOKENS = 32000


def _builtin_default_max_tokens(model_name, api_base=""):
    model = _strip_protocol_prefix(model_name)
    base = str(api_base or "").lower()
    if "kimi-k2.7-code" in model and "tokenrhythm.studio" in base:
        return 16000
    if "deepseek" in model or _is_deepseek_family(model_name, api_base):
        return 384000
    if "agnes" in model:
        return 65535
    if _is_glm_family(model_name, api_base):
        if "glm-5.3" in model:
            return 131000
        if "glm-5" in model:
            return 128000
        return _BUILTIN_DEFAULT_MAX_TOKENS
    if _is_qwen_family(model_name, api_base):
        return 131000
    if "kimi-k2.5" in model:
        return 64000
    if "kimi-k2.6" in model:
        return 128000
    if "kimi-k2.7-code" in model:
        return 128000
    if "k3" in model:
        return 131072
    if "longcat" in model:
        return 128000
    if "seed-2.1-pro" in model or "seed-2.1-turbo" in model:
        return 131000
    if "mimo-v2.6" in model or "mimo-v2.5" in model:
        return 131072
    if "minimax-m2.5" in model or "minimax-m3" in model:
        return 200000
    if "minimax-m2.7" in model:
        return 192000
    return _BUILTIN_DEFAULT_MAX_TOKENS


def _model_system_prompt_extra(model_name, api_base="", model_id=None) -> str:
    try:
        from core.model_manager import (
            get_model_config_by_id,
            get_model_config_by_litellm_model,
        )

        config_entry = None
        if model_id:
            config_entry = get_model_config_by_id(str(model_id))
        if not isinstance(config_entry, dict):
            config_entry = get_model_config_by_litellm_model(model_name, api_base)
        if not isinstance(config_entry, dict):
            return ""
        extra = config_entry.get("system_prompt_extra")
        if not isinstance(extra, str):
            return ""
        # 全空白视为未配置，避免给请求塞入一条纯空白的 system 消息
        return extra if extra.strip() else ""
    except Exception:
        return ""


def _provider_request_options(
    model_name,
    api_base,
    *,
    model_id=None,
    cache_scope="main",
    cache_session_id=None,
    tool_contract="",
):
    normalized_base = str(api_base or "").rstrip("/").lower()
    normalized_model = str(model_name or "").split("/")[-1].lower()

    try:
        from core.model_manager import (
            get_model_config_by_id,
            get_model_config_by_litellm_model,
        )
        model_cfg = (
            get_model_config_by_id(model_id)
            if model_id
            else get_model_config_by_litellm_model(model_name, api_base)
        ) or {}
    except Exception:
        if model_id:
            raise
        model_cfg = {}
    if model_id and not model_cfg:
        raise ValueError(f"未找到模型配置: {model_id}")

    cfg_max_tokens = model_cfg.get("max_tokens")
    cfg_thinking_budget = model_cfg.get("thinking_budget")
    cfg_enable_thinking = model_cfg.get("enable_thinking")
    has_reasoning_choice = bool(str(model_cfg.get("reasoning_choice") or "").strip())
    has_legacy_reasoning_choice = (
        isinstance(cfg_enable_thinking, bool)
        or bool(str(model_cfg.get("reasoning_effort") or "").strip())
    )
    from core.reasoning_capabilities import get_reasoning_capability
    configured_capability = get_reasoning_capability(model_name)
    has_registered_reasoning = configured_capability.kind != "hidden"

    kimi_family = _is_kimi_family(model_name, api_base)
    deepseek_family = _is_deepseek_family(model_name, api_base)
    deepseek_thinking = _deepseek_thinking_enabled(
        model_name, api_base, model_cfg
    )
    route_prefix = str(model_name or "").split("/")[0].lower()
    anthropic_route = route_prefix == "anthropic"

    result = {}

    if cfg_max_tokens is not None:
        result["max_tokens"] = int(cfg_max_tokens)
    else:
        result["max_tokens"] = _builtin_default_max_tokens(model_name, api_base)

    if deepseek_family:
        if not has_reasoning_choice and (
            not has_registered_reasoning or not has_legacy_reasoning_choice
        ):
            if _is_official_deepseek_route(api_base) or isinstance(cfg_enable_thinking, bool):
                result.setdefault("extra_body", {})["thinking"] = {
                    "type": "enabled" if deepseek_thinking else "disabled"
                }
            if deepseek_thinking:
                effort = str(model_cfg.get("reasoning_effort") or "max").strip().lower()
                if effort not in _REASONING_EFFORT_LEVELS:
                    raise ValueError(
                        "DeepSeek reasoning_effort 仅支持 low/high/max，"
                        f"实际为 {effort!r}"
                    )
                result["reasoning_effort"] = effort
                result["allowed_openai_params"] = ["reasoning_effort"]
    elif not has_reasoning_choice and not has_registered_reasoning and (
        cfg_max_tokens is not None
        or cfg_thinking_budget is not None
        or (cfg_enable_thinking is not None and not has_reasoning_choice)
    ):
        if cfg_enable_thinking is True or cfg_thinking_budget is not None:
            budget = int(cfg_thinking_budget) if cfg_thinking_budget is not None else 2048
            result["thinking"] = {"type": "enabled", "budget_tokens": budget}
            result["allowed_openai_params"] = ["thinking"]
    elif kimi_family:
        is_kimi_coding = "api.kimi.com/coding" in normalized_base

        def _match_any(keys):
            return any(k in normalized_model for k in keys)

        is_k3 = _match_any(("k3",)) or "for-coding" in normalized_model

        if anthropic_route:
            if is_k3:
                result.setdefault("thinking", {
                    "type": "enabled", "budget_tokens": 4096 if is_kimi_coding else 2048,
                })
            else:
                result.setdefault("thinking", {"type": "enabled", "budget_tokens": 2048})
            result.setdefault("allowed_openai_params", ["thinking"])
        # OpenAI 兼容路由（api.moonshot.cn/v1 及各类经销商）：绝不发送 Anthropic
        # 形状的 thinking 参数——Moonshot 的 /v1/chat/completions 不认这个键，
        # 发了会直接 400。Kimi 推理模型（k2.5/k2.6/k2.7/k3 等）在该路由上会原生
        # 输出 reasoning_content，无需显式开启；输出上限由内置默认表统一给出。

    if kimi_family or _affinity_lane_enabled(model_name, api_base):
        _attach_kimi_request_extras(
            result,
            model_name,
            model_cfg,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    if _is_qwen_family(model_name, api_base):
        _attach_qwen_request_extras(
            result,
            model_name,
            model_cfg,
            api_base=api_base,
            cache_scope=cache_scope,
            cache_session_id=cache_session_id,
            tool_contract=tool_contract,
        )
    capability = _apply_reasoning_choice(
        result,
        model_name,
        model_cfg,
        anthropic_route=anthropic_route,
    )
    _assert_reasoning_request_consistent(result, capability)
    return _apply_route_request_overrides(result, model_name, api_base)
