# -*- coding: utf-8 -*-
"""会话级 UI 状态持久化（每个会话当前是 IDE 模式还是对话模式）。

为什么需要后端权威副本：IDE 模式开关原先只活在前端内存里，切会话/重启就丢，
用户每次都要手动切回上次用的模式。放 localStorage 同样不行——WebView2 的
localStorage 异步落盘，改完立刻关窗会因 checksum mismatch 丢尾部写入
（详见 core/ui_layout.py 文件头的同类事故记录）。因此沿用同一套「后端原子写 +
按会话分桶」策略，与 core/ide/layout_store.py 完全同构。

写盘走 config._atomic_write_json。读取失败回落空状态——磁盘异常不该让界面
打不开；但**保存**路径是例外：全量覆盖语义下，若文件存在却读不出来，回落空
快照会把其它会话的模式静默抹掉，此时必须拒绝写入（见 _read_all(strict=True)）。
"""
from __future__ import annotations

import os
import threading
from typing import Any

_STORE_LOCK = threading.Lock()

_STORE_VERSION = 1

# 结构上限：防御性约束，正常使用远达不到（用户实际只有个位数会话）
MAX_SESSIONS = 500


class SessionUiError(ValueError):
    """会话 UI 状态写入失败。"""


def _store_path():
    try:
        import config

        return (
            config.YCODE_DIR / "session_ui.json"
            if hasattr(config.YCODE_DIR, "__truediv__")
            else config.YCODE_DIR + "/session_ui.json"
        )
    except Exception:
        from pathlib import Path

        import os

        data_dir = os.environ.get("YCODE_DATA_DIR") or os.path.join(str(Path.home()), ".ycode")
        return os.path.join(data_dir, "session_ui.json")


def _clean_session(payload: Any) -> dict:
    """字段白名单清洗：只认 ide_mode，且必须是真布尔。

    不做 ``bool(payload.get("ide_mode"))`` 这类弱转换——那样字符串 "false"、
    非零整数 1 都会被当成 True，前端传错类型反而静默打开 IDE 模式。非法值一律
    回落 False（对话模式），这是最保守的默认。
    """
    if not isinstance(payload, dict):
        return {"ide_mode": False}
    ide_mode = payload.get("ide_mode")
    if not isinstance(ide_mode, bool):
        ide_mode = False
    return {"ide_mode": ide_mode}


def _empty() -> dict:
    return {"version": _STORE_VERSION, "sessions": {}}


def _read_all(strict: bool = False) -> dict:
    """读取全量快照。

    ``strict=False``（读取路径）沿用「读不崩」策略：任何异常都回落空快照。
    ``strict=True``（保存路径）必须区分两种「读不到」——
    - **文件不存在**：合法，等价于空快照，可以正常写入；
    - **文件存在但读取失败**（主文件与 .bak 都不可读，或结构非法）：这是异常。
      保存是**全量覆盖**语义，此时回落空快照会让回写内容只含当前会话，
      磁盘上其它会话的模式被静默抹掉。因此必须抛错、绝不回写。
    """
    path = _store_path()
    try:
        import config

        data = config._read_json_with_backup(path, default=None)
    except Exception:
        if strict:
            raise SessionUiError("读取会话 UI 状态失败，已拒绝写入以免清空其它会话")
        return _empty()
    if isinstance(data, dict) and isinstance(data.get("sessions"), dict):
        sessions = {}
        for name, payload in data["sessions"].items():
            if isinstance(name, str) and name:
                sessions[name] = _clean_session(payload)
        return {"version": _STORE_VERSION, "sessions": sessions}
    if strict and _store_file_present(path):
        raise SessionUiError(
            "会话 UI 状态文件存在但无法读取（可能已损坏），已拒绝写入以免清空其它会话"
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


def get_session_ui(session: str) -> dict:
    """读取指定会话的 UI 状态；任何异常回落默认值（对话模式）。"""
    sessions = _read_all()["sessions"]
    payload = sessions.get(session or "")
    if payload is None:
        return {"version": _STORE_VERSION, "ide_mode": False}
    return {"version": _STORE_VERSION, **payload}


def save_session_ui(session: str, ui: Any) -> dict:
    """全量覆盖指定会话的 UI 状态，返回规范化后的会话快照。

    全量覆盖而非合并：白名单只有固定几个布尔字段，合并语义没有意义，还会让
    已废弃字段在文件里无限残留。
    """
    if not session:
        raise SessionUiError("session 不能为空")
    with _STORE_LOCK:
        # strict=True：文件存在却读不出来时抛 SessionUiError，绝不回写空快照
        # 覆盖掉磁盘上其它会话的状态。
        data = _read_all(strict=True)
        # 数量上限只挡「新增会话」：超限时保留既有条目，只拒绝让文件继续膨胀。
        # 已存在会话仍允许更新，否则用户连当前会话都改不了，等于整个功能失灵。
        if session not in data["sessions"] and len(data["sessions"]) >= MAX_SESSIONS:
            raise SessionUiError(f"会话 UI 状态条目已达上限 {MAX_SESSIONS}，拒绝新增会话")
        cleaned = _clean_session(ui)
        data["sessions"][session] = cleaned
        try:
            import config

            config._atomic_write_json(_store_path(), data, indent=2)
        except Exception as exc:  # pragma: no cover - 磁盘故障等极端情况
            raise SessionUiError(f"保存失败：{exc}") from exc
    return {"version": _STORE_VERSION, **cleaned}
