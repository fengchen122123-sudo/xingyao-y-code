"""界面布局偏好持久化（侧栏「项目分组 / 分组内会话」的自定义排序）。

背景（2026-09-18）：这两处排序原先只写前端 localStorage。WebView2 的
localStorage 落盘是异步的——用户拖完排序立刻重启（关闭窗口）时，leveldb
写前日志尾部会留下 checksum mismatch，重启恢复时被整段丢弃：

    Recovering log #3
    (ignoring error) 000003.log: dropping 5484 bytes; Corruption: checksum mismatch

结果是"数据明明写进文件了，但应用读不到"，表现为拖完排序一重启就复原。
因此权威副本改存数据目录下的 ui_layout.json（原子写 + 备份），localStorage
降级为即时缓存：启动时先用缓存保证首屏不闪，再由后端快照校正。

设计约束：
- 读取失败一律回落空快照，绝不影响启动（磁盘异常不该让软件打不开）；
- 写盘走 config._atomic_write_json，避免中断产生半截文件；
- 只接受受控结构（字符串数组 / 字符串数组的字典），越界与脏数据直接丢弃，
  防止前端传入任意 JSON 把偏好文件撑大或写坏。
"""

from __future__ import annotations

import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

# 结构上限：防御性约束，正常侧栏远达不到（用户实际只有个位数分组）
MAX_GROUPS = 500
MAX_SESSIONS_PER_GROUP = 5000


class UiLayoutError(ValueError):
    """Raised when the layout snapshot fails to persist."""


def _store_path():
    """布局文件路径：跟随 YCODE_DATA_DIR，缺省 ~/.ycode/ui_layout.json。"""
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
    """清洗字符串数组：丢非字符串/空串，去重，按上限截断。"""
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
    """清洗「分组路径 -> 会话名数组」分桶。"""
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
    """空快照：表示"用户还没有自定义过排序"。"""
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
    """全量覆盖写入布局快照，返回规范化后的快照。

    全量覆盖而非合并：排序是"整份列表"的语义，合并会让已删除的分组/会话
    在文件里无限残留。
    """
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
