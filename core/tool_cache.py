from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any


_LOCK = threading.RLock()
_MAX_ENTRIES = 128

_MAX_ENTRY_BYTES = 1024 * 1024
# 单张缓存表的总字节预算（16MB）。条数上限 128 配上 MB 级单条值仍可能占用数百 MB，
# 所以在条数裁剪之后再按字节预算从最旧（LRU 顺序）开始淘汰，给内存占用兜底。
_MAX_TABLE_BYTES = 16 * 1024 * 1024

_read_file_cache: OrderedDict[tuple, str] = OrderedDict()
_search_cache: OrderedDict[tuple, str] = OrderedDict()
_glob_cache: OrderedDict[tuple, str] = OrderedDict()
_lsp_cache: OrderedDict[tuple, str] = OrderedDict()

_read_file_sizes: dict[tuple, int] = {}
_search_sizes: dict[tuple, int] = {}
_glob_sizes: dict[tuple, int] = {}
_lsp_sizes: dict[tuple, int] = {}

# Shared cache scoped to a parent session.  Subagents and the main agent can
# share read/search results to avoid re-scanning the same codebase in parallel.
_shared_cache: dict[str, OrderedDict[tuple, Any]] = {}
_shared_sizes: dict[str, dict[tuple, int]] = {}

_session_tls = threading.local()

_stats: dict[str, dict[str, int]] = {}


def _record(tool: str, hit: bool) -> None:
    entry = _stats.setdefault(tool, {"hits": 0, "misses": 0})
    if hit:
        entry["hits"] += 1
    else:
        entry["misses"] += 1


def _entry_size(value: Any) -> int:
    """估算一条缓存值的 UTF-8 字节数（缓存值基本是 str，其余类型做兜底估算）。"""
    if isinstance(value, str):
        try:
            return len(value.encode("utf-8"))
        except UnicodeEncodeError:
            return len(value.encode("utf-8", "replace"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    if value is None:
        return 0
    try:
        return len(repr(value).encode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 - 计量失败不应该影响缓存写入
        return 0


def _pop_oldest(cache: OrderedDict, sizes: dict | None) -> None:
    try:
        key, _value = cache.popitem(last=False)
    except KeyError:
        return
    if sizes is not None:
        sizes.pop(key, None)


def _prune(cache: OrderedDict, sizes: dict | None = None) -> None:
    while len(cache) > _MAX_ENTRIES:
        _pop_oldest(cache, sizes)


def _evict_to_budget(cache: OrderedDict, sizes: dict | None) -> None:
    if sizes is None:
        return
    total = sum(sizes.values())
    while total > _MAX_TABLE_BYTES and cache:
        try:
            key, _value = cache.popitem(last=False)
        except KeyError:
            break
        total -= sizes.pop(key, 0)


def _cache_set(
    cache: OrderedDict,
    sizes: dict | None,
    key: tuple,
    value: Any,
) -> bool:
    size = _entry_size(value)
    if size > _MAX_ENTRY_BYTES:
        return False
    cache[key] = value
    if sizes is not None:
        sizes[key] = size
    _prune(cache, sizes)
    _evict_to_budget(cache, sizes)
    return True


def _get_dir_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _get_file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def read_file_key(
    path: str,
    offset: int,
    limit: int | None,
    compress: bool,
    with_line_numbers: bool = False,
) -> tuple:
    return (
        str(Path(path).resolve()),
        int(offset),
        limit,
        bool(compress),
        bool(with_line_numbers),
        _get_file_mtime(path),
    )


def read_file_get(key: tuple) -> str | None:
    with _LOCK:
        if key in _read_file_cache:
            _read_file_cache.move_to_end(key)
            _record("read_file", hit=True)
            return _read_file_cache[key]
        _record("read_file", hit=False)
    return None


def read_file_put(key: tuple, value: str) -> None:
    with _LOCK:
        _cache_set(_read_file_cache, _read_file_sizes, key, value)


def search_key(
    tool: str,
    query: str,
    dir_path: str,
    extensions: tuple[str, ...] | None = None,
) -> tuple:
    return (
        tool,
        query,
        str(Path(dir_path).resolve()),
        extensions,
        _get_dir_mtime(dir_path),
    )


def search_get(key: tuple) -> str | None:
    with _LOCK:
        tool = key[0]
        cache = _search_cache if tool == "search_code" else _lsp_cache
        if key in cache:
            cache.move_to_end(key)
            _record(tool, hit=True)
            return cache[key]
        _record(tool, hit=False)
    return None


def search_put(key: tuple, value: str) -> None:
    with _LOCK:
        if key[0] == "search_code":
            cache, sizes = _search_cache, _search_sizes
        else:
            cache, sizes = _lsp_cache, _lsp_sizes
        _cache_set(cache, sizes, key, value)


def glob_key(pattern: str, dir_path: str) -> tuple:
    return (
        pattern,
        str(Path(dir_path).resolve()),
        _get_dir_mtime(dir_path),
    )


def glob_get(key: tuple) -> str | None:
    with _LOCK:
        if key in _glob_cache:
            _glob_cache.move_to_end(key)
            _record("glob", hit=True)
            return _glob_cache[key]
        _record("glob", hit=False)
    return None


def glob_put(key: tuple, value: str) -> None:
    with _LOCK:
        _cache_set(_glob_cache, _glob_sizes, key, value)


def set_current_session(session_id: str | None) -> None:
    """Mark the current thread as belonging to a parent agent session."""
    _session_tls.session_id = session_id


def get_current_session() -> str | None:
    return getattr(_session_tls, "session_id", None)


def shared_get(session_id: str, key: tuple) -> Any | None:
    with _LOCK:
        session = _shared_cache.get(session_id)
        if session is None:
            _record("shared", hit=False)
            return None
        if key in session:
            session.move_to_end(key)
            _record("shared", hit=True)
            return session[key]
        _record("shared", hit=False)
        return None


def shared_put(session_id: str, key: tuple, value: Any) -> None:
    with _LOCK:
        session = _shared_cache.setdefault(session_id, OrderedDict())
        sizes = _shared_sizes.setdefault(session_id, {})
        _cache_set(session, sizes, key, value)
        if not session:
            _shared_cache.pop(session_id, None)
            _shared_sizes.pop(session_id, None)


def shared_clear(session_id: str) -> None:
    with _LOCK:
        _shared_cache.pop(session_id, None)
        _shared_sizes.pop(session_id, None)


def get_stats() -> dict:
    with _LOCK:
        tools = {name: dict(entry) for name, entry in sorted(_stats.items())}
        total_hits = sum(entry["hits"] for entry in _stats.values())
        total_misses = sum(entry["misses"] for entry in _stats.values())
        total = total_hits + total_misses
        hit_rate = total_hits / total if total else 0.0
        return {
            "tools": tools,
            "total_hits": total_hits,
            "total_misses": total_misses,
            "hit_rate": hit_rate,
        }


def reset_stats() -> None:
    """Testing helper: clear all accumulated hit/miss statistics."""
    with _LOCK:
        _stats.clear()


def truncate_lines(lines: list[str], max_lines: int, tool_name: str) -> str:
    """Truncate a list of lines with an explanatory footer."""
    if len(lines) <= max_lines:
        return "\n".join(lines)
    shown = lines[:max_lines]
    omitted = len(lines) - max_lines
    shown.append(f"... [{tool_name} 结果已截断，省略 {omitted} 行]")
    return "\n".join(shown)


def truncate_bytes(text: str, max_bytes: int, tool_name: str) -> str:
    """Truncate UTF-8 text to a byte limit without breaking multibyte chars."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    truncated = raw[:max_bytes]
    # Walk back to a valid UTF-8 boundary
    while truncated and (truncated[-1] & 0b11000000) == 0b10000000:
        truncated = truncated[:-1]
    text_out = truncated.decode("utf-8", errors="ignore")
    omitted = len(raw) - len(text_out.encode("utf-8"))
    return f"{text_out}\n... [{tool_name} 结果已截断，省略 {omitted} 字节]"


def invalidate_after_mutation(path: str | None = None) -> None:
    """写/改/删或执行变更命令后清理可能过期的检索、目录与文件读取缓存。"""
    norm_target: str | None = None
    if path:
        try:
            norm_target = os.path.normcase(str(Path(path).resolve(strict=False)))
        except OSError:
            norm_target = os.path.normcase(str(path))

    with _LOCK:
        _search_cache.clear()
        _search_sizes.clear()
        _glob_cache.clear()
        _glob_sizes.clear()
        _lsp_cache.clear()
        _lsp_sizes.clear()

        if norm_target is None:
            _read_file_cache.clear()
            _read_file_sizes.clear()
            _shared_cache.clear()
            _shared_sizes.clear()
            return

        stale_read_keys = [
            key
            for key in _read_file_cache
            if key and os.path.normcase(str(key[0])) == norm_target
        ]
        for key in stale_read_keys:
            _read_file_cache.pop(key, None)
            _read_file_sizes.pop(key, None)

        for sess_id, sess_cache in list(_shared_cache.items()):
            sess_sizes = _shared_sizes.get(sess_id)
            for s_key in list(sess_cache):
                kind = s_key[0] if isinstance(s_key, tuple) and s_key else None
                if kind == "read_file":
                    inner = s_key[1] if len(s_key) > 1 and isinstance(s_key[1], tuple) else ()
                    if not inner or os.path.normcase(str(inner[0])) == norm_target:
                        sess_cache.pop(s_key, None)
                        if sess_sizes is not None:
                            sess_sizes.pop(s_key, None)
                else:
                    sess_cache.pop(s_key, None)
                    if sess_sizes is not None:
                        sess_sizes.pop(s_key, None)


def clear_all() -> None:
    """Testing helper: reset all caches."""
    global _read_file_cache, _search_cache, _glob_cache, _lsp_cache, _shared_cache
    with _LOCK:
        _read_file_cache.clear()
        _search_cache.clear()
        _glob_cache.clear()
        _lsp_cache.clear()
        _shared_cache.clear()
        # 字节计量表随缓存一起清空，避免残留的旧尺寸干扰后续裁剪。
        _read_file_sizes.clear()
        _search_sizes.clear()
        _glob_sizes.clear()
        _lsp_sizes.clear()
        _shared_sizes.clear()
