"""渠道请求形态覆盖：按域名给指定渠道追加请求头与请求体参数。

设计约束（见 待开发/网关请求形态覆盖/设计规格与实施计划.md §4）：
- 只做形态覆盖，不参与任何缓存机制判定；
- 白名单拒绝策略为键级剔除，请求必定照常发出；
- 生效值参与指纹（route_override_sha256），便于事后归因。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from core.llm_dialects import _normalize_route_match_key

CONFIG_KEY = "route_request_overrides"
HEADER_DENY_KEYS = frozenset({
    "authorization", "content-type", "accept", "host", "content-length",
})
BODY_DENY_KEYS = frozenset({
    "model", "messages", "tools", "tool_choice", "stream", "stream_options",
    "max_tokens", "max_completion_tokens", "api_key", "api_base",
})
_ENV_REF = re.compile(r"^\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}$")
_TTL_SECONDS = 10.0
# 条目格式与 _keepalive_routes_cfg_cache 一致：(过期时间, stamp, entries)。
# 测试可直接把 entry 置 None 强制重读配置。
_cache = {"entry": None}


def _load_config_for_overrides():
    from core.model_manager import load_config
    return load_config()


def _config_stamp():
    """配置来源指纹（loader 身份 + config 文件 mtime），口径同 _qwen_cache_config_stamp。

    拿不到时返回 None：此时 stamp 恒等，只能靠 TTL 兜底（宁可不缓存，也不给出过期结论）。
    """
    try:
        from core.llm_dialects import _qwen_cache_config_stamp
        return _qwen_cache_config_stamp()
    except Exception:
        return None


def _expand(value):
    if isinstance(value, str):
        match = _ENV_REF.match(value.strip())
        if match:
            return os.environ.get(match.group(1), "")
    return value


def parse_route_request_overrides(data):
    raw = data.get(CONFIG_KEY) if isinstance(data, dict) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        match = _normalize_route_match_key(item.get("match"))
        if not match:
            continue
        headers = item.get("headers")
        body = item.get("body")
        entries.append((
            match,
            headers if isinstance(headers, dict) else {},
            body if isinstance(body, dict) else {},
        ))
    return tuple(entries)


def _filter(headers, body):
    clean_headers = {
        k: _expand(v) for k, v in headers.items()
        if str(k).lower() not in HEADER_DENY_KEYS
    }
    clean_body = {k: v for k, v in body.items() if str(k) not in BODY_DENY_KEYS}
    return clean_headers, clean_body


def _cached_entries():
    """按配置指纹取解析结果：stamp 变化立即失效，10 秒 TTL 兜底。"""
    stamp = _config_stamp()
    now = time.monotonic()
    entry = _cache.get("entry")
    if entry:
        expires, cached_stamp, entries = entry
        if cached_stamp == stamp and expires > now:
            return entries
    try:
        entries = parse_route_request_overrides(_load_config_for_overrides())
    except Exception:
        entries = ()
    _cache["entry"] = (now + _TTL_SECONDS, stamp, entries)
    return entries


def resolve_route_override(model_name, api_base):
    """返回该渠道生效的覆盖项；无匹配时三项均为空（零影响）。

    model_name 目前不参与匹配（按域名匹配即可），保留形参只为与
    _provider_request_options 的调用形状对齐，并为将来"按模型细分覆盖"留口子。
    """
    empty = {"headers": {}, "body": {}, "fingerprint": ""}
    host = _normalize_route_match_key(api_base)
    for match, headers, body in _cached_entries():
        if not host or not match:
            continue
        if host == match or match in host:
            clean_headers, clean_body = _filter(headers, body)
            if not clean_headers and not clean_body:
                return empty
            payload = json.dumps(
                {"headers": clean_headers, "body": clean_body},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            return {
                "headers": clean_headers,
                "body": clean_body,
                "fingerprint": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            }
    return empty