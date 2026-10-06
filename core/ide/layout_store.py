# -*- coding: utf-8 -*-
"""IDE 布局状态持久化（打开的标签、光标行、断点）。

为什么不用 localStorage 做权威副本：WebView2 的 localStorage 异步落盘，
用户改完立刻关窗会因 checksum mismatch 丢数据（详见 core/ui_layout.py 文件头
记录的同类事故）。这里沿用同一套「后端原子写 + 前端缓存」策略。

写盘走 config._atomic_write_json。读取失败回落空快照——磁盘异常不该让 IDE
打不开；但**保存**路径是例外：全量覆盖语义下，若文件存在却读不出来，回落空
快照会把其它会话的布局静默抹掉，此时必须拒绝写入（见 _read_all(strict=True)）。
"""
from __future__ import annotations

import os
import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

# 结构上限：防御性约束，正常使用远达不到
MAX_TABS = 100
MAX_BREAKPOINTS = 500


class IdeLayoutError(ValueError):
    """布局快照写入失败。"""


def _store_path():
    try:
        import config

        return (
            config.YCODE_DIR / "ide_layout.json"
            if hasattr(config.YCODE_DIR, "__truediv__")
            else config.YCODE_DIR + "/ide_layout.json"
        )
    except Exception:
        from pathlib import Path

        import os

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "ide_layout.json")


def _clean_tab(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    line = item.get("line", 1)
    if not isinstance(line, int) or line < 1:
        line = 1
    return {"path": path, "line": line}


def _clean_breakpoint(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    path = item.get("path")
    line = item.get("line")
    if not isinstance(path, str) or not path.strip():
        return None
    if not isinstance(line, int) or line < 1:
        return None
    return {"path": path, "line": line}


def _clean_session(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {"tabs": [], "active": "", "breakpoints": []}
    tabs: list[dict] = []
    for raw in payload.get("tabs") or []:
        cleaned = _clean_tab(raw)
        if cleaned is not None:
            tabs.append(cleaned)
        if len(tabs) >= MAX_TABS:
            break
    breakpoints: list[dict] = []
    for raw in payload.get("breakpoints") or []:
        cleaned = _clean_breakpoint(raw)
        if cleaned is not None:
            breakpoints.append(cleaned)
        if len(breakpoints) >= MAX_BREAKPOINTS:
            break
    active = payload.get("active")
    if not isinstance(active, str) or active not in {t["path"] for t in tabs}:
        active = tabs[0]["path"] if tabs else ""
    return {"tabs": tabs, "active": active, "breakpoints": breakpoints}


def _empty() -> dict:
    return {"version": _STORE_VERSION, "sessions": {}}


def _read_all(strict: bool = False) -> dict:
    """读取全量快照。

    ``strict=False``（读取路径）沿用「读不崩」策略：任何异常都回落空快照。
    ``strict=True``（保存路径）必须区分两种「读不到」——
    - **文件不存在**：合法，等价于空快照，可以正常写入；
    - **文件存在但读取失败**（主文件与 .bak 都不可读，或结构非法）：这是异常。
      保存是**全量覆盖**语义，此时回落空快照会让回写内容只含当前会话，
      磁盘上其它会话的布局被静默抹掉。因此必须抛错、绝不回写。
    """
    path = _store_path()
    try:
        import config

        data = config._read_json_with_backup(path, default=None)
    except Exception:
        if strict:
            raise IdeLayoutError("读取 IDE 布局失败，已拒绝写入以免清空其它会话")
        return _empty()
    if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
        sessions = {}
        for name, payload in data["sessions"].items():
            if isinstance(name, str) and name:
                sessions[name] = _clean_session(payload)
        return {"version": _STORE_VERSION, "sessions": sessions}
    if strict and _store_file_present(path):
        raise IdeLayoutError(
            "IDE 布局文件存在但无法读取（可能已损坏），已拒绝写入以免清空其它会话"
        )
    return _empty()


def _store_file_present(path) -> bool:
    """主文件或 .bak 是否存在（判断失败时保守返回 False，不影响正常写入）。

    path 可能是 str 也可能是 pathlib.Path（config.YCODE_DIR 两种形态都存在），
    统一用 os.fspath 归一后再拼 .bak。
    """
    try:
        main = os.fspath(path)
        return os.path.exists(main) or os.path.exists(main + ".bak")
    except (OSError, TypeError):
        return False


def get_ide_layout(session: str) -> dict:
    """读取指定会话的 IDE 布局；任何异常回落空布局。"""
    sessions = _read_all()["sessions"]
    payload = sessions.get(session or "")
    if payload is None:
        return {"version": _STORE_VERSION, "tabs": [], "active": "", "breakpoints": []}
    return {"version": _STORE_VERSION, **payload}


def save_ide_layout(session: str, payload: Any) -> dict:
    """全量覆盖指定会话的 IDE 布局，返回规范化后的会话快照。

    全量覆盖而非合并：标签是「整份列表」语义，合并会让已关闭的标签
    在文件里无限残留。
    """
    if not session:
        raise IdeLayoutError("session 不能为空")
    with _STORE_LOCK:
        # strict=True：文件存在却读不出来时抛 IdeLayoutError，绝不回写空快照
        # 覆盖掉磁盘上其它会话的布局。
        data = _read_all(strict=True)
        cleaned = _clean_session(payload)
        data["sessions"][session] = cleaned
        try:
            import config

            config._atomic_write_json(_store_path(), data, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise IdeLayoutError(f"保存失败：{exc}") from exc
    return {"version": _STORE_VERSION, **cleaned}
