from __future__ import annotations

import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

MAX_GROUPS = 500
MAX_SESSIONS_PER_GROUP = 5000


class UiLayoutError(ValueError):
    """Raised when the layout snapshot fails to persist."""


def _store_path():
    try:
        import config

        return (
            config.YCODE_DIR / "ui_layout.json"
            if hasattr(config.YCODE_DIR, "__truediv__")
            else config.YCODE_DIR + "/ui_layout.json"
        )
    except Exception:
        import os
        from pathlib import Path

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "ui_layout.json")


def _clean_str_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def _clean_session_order(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, names in value.items():
        if not isinstance(key, str) or not key.strip():
            continue
        cleaned = _clean_str_list(names, MAX_SESSIONS_PER_GROUP)
        if cleaned:
            out[key.strip()] = cleaned
    return out


def _defaults() -> dict[str, Any]:
    return {"version": _STORE_VERSION, "group_order": [], "session_order": {}}


def get_ui_layout() -> dict[str, Any]:
    """读取布局快照；任何读取异常回落空快照（前端会退回 localStorage 缓存）。"""
    try:
        import config

        data = config._read_json_with_backup(_store_path(), default=None)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return _defaults()
    return {
        "version": _STORE_VERSION,
        "group_order": _clean_str_list(data.get("group_order"), MAX_GROUPS),
        "session_order": _clean_session_order(data.get("session_order")),
    }


def save_ui_layout(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise UiLayoutError("payload 必须是对象")
    snapshot = {
        "version": _STORE_VERSION,
        "group_order": _clean_str_list(payload.get("group_order"), MAX_GROUPS),
        "session_order": _clean_session_order(payload.get("session_order")),
    }
    with _STORE_LOCK:
        try:
            import config

            config._atomic_write_json(_store_path(), snapshot, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise UiLayoutError(f"保存失败：{exc}") from exc
    return snapshot
