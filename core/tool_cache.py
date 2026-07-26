"""Lightweight in-process LRU caches for read-only tool results.

These caches reduce repeated input-token consumption when the LLM reads or
searches the same files multiple times within a single ycode process.  Cache
keys include file/directory modification times so stale entries are naturally
invalidated when content changes.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable


_LOCK = threading.RLock()
_MAX_ENTRIES = 128

# Per-tool caches: separate OrderedDicts keep hit/miss stats independent.
_read_file_cache: OrderedDict[tuple, str] = OrderedDict()
_search_cache: OrderedDict[tuple, str] = OrderedDict()
_glob_cache: OrderedDict[tuple, str] = OrderedDict()
_lsp_cache: OrderedDict[tuple, str] = OrderedDict()

# Shared cache scoped to a parent session.  Subagents and the main agent can
# share read/search results to avoid re-scanning the same codebase in parallel.
_shared_cache: dict[str, OrderedDict[tuple, Any]] = {}

# Thread-local session id used to route shared-cache lookups from subagents.
_session_tls = threading.local()


def _prune(cache: OrderedDict) -> None:
    while len(cache) > _MAX_ENTRIES:
        cache.popitem(last=False)


def _get_dir_mtime(path: str) -> float:
    """Best-effort mtime for a directory tree (uses top-level directory mtime).

    For large repositories, walking the entire tree to compute a composite
    mtime is too slow.  Using the top-level directory mtime is a pragmatic
    trade-off: any direct child addition/deletion invalidates the cache.
    File-level edits inside subdirectories are not detected, but that is
    acceptable for short-lived agent sessions.
    """
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def _get_file_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


# ---------------------------------------------------------------------------
# read_file cache
# ---------------------------------------------------------------------------


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
            return _read_file_cache[key]
    return None


def read_file_put(key: tuple, value: str) -> None:
    with _LOCK:
        _read_file_cache[key] = value
        _prune(_read_file_cache)


# ---------------------------------------------------------------------------
# search_code / find_definition cache
# ---------------------------------------------------------------------------


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
        cache = _search_cache if key[0] == "search_code" else _lsp_cache
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
    return None


def search_put(key: tuple, value: str) -> None:
    with _LOCK:
        cache = _search_cache if key[0] == "search_code" else _lsp_cache
        cache[key] = value
        _prune(cache)


# ---------------------------------------------------------------------------
# glob_files cache
# ---------------------------------------------------------------------------


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
            return _glob_cache[key]
    return None


def glob_put(key: tuple, value: str) -> None:
    with _LOCK:
        _glob_cache[key] = value
        _prune(_glob_cache)


# ---------------------------------------------------------------------------
# Shared session cache (for subagent exploration reuse)
# ---------------------------------------------------------------------------


def set_current_session(session_id: str | None) -> None:
    """Mark the current thread as belonging to a parent agent session."""
    _session_tls.session_id = session_id


def get_current_session() -> str | None:
    return getattr(_session_tls, "session_id", None)


def shared_get(session_id: str, key: tuple) -> Any | None:
    with _LOCK:
        session = _shared_cache.get(session_id)
        if session is None:
            return None
        return session.get(key)


def shared_put(session_id: str, key: tuple, value: Any) -> None:
    with _LOCK:
        session = _shared_cache.setdefault(session_id, OrderedDict())
        session[key] = value
        # Keep shared cache bounded to avoid unbounded growth.
        if len(session) > _MAX_ENTRIES:
            session.popitem(last=False)


def shared_clear(session_id: str) -> None:
    with _LOCK:
        _shared_cache.pop(session_id, None)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------


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


def clear_all() -> None:
    """Testing helper: reset all caches."""
    global _read_file_cache, _search_cache, _glob_cache, _lsp_cache, _shared_cache
    with _LOCK:
        _read_file_cache.clear()
        _search_cache.clear()
        _glob_cache.clear()
        _lsp_cache.clear()
        _shared_cache.clear()
