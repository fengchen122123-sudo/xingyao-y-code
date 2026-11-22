from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path


_LOCK = threading.Lock()

_READ_STATE: dict[tuple[str, str], dict] = {}

MAX_TRACKED = 4096

_DISABLED_VALUES = frozenset({"0", "off", "false"})

_CHUNK_SIZE = 1024 * 1024


class StaleReadError(RuntimeError):
    """写前新鲜度校验失败：未完整读过目标文件，或文件在读取后已被改动。"""


def _fingerprint(path: Path) -> dict:
    stat = path.stat()
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return {"size": stat.st_size, "sha256": digest.hexdigest()}


def _store_fingerprint(session_id: str, path: str) -> None:
    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            return
        key_path = str(target.resolve(strict=False))
        fingerprint = _fingerprint(target)
    except OSError:
        # stat / open 失败（权限、文件消失、路径过长）一律静默放弃登记
        return

    key = (session_id, key_path)
    with _LOCK:
        _READ_STATE[key] = fingerprint
        if len(_READ_STATE) > MAX_TRACKED:
            # 简单兜底：整体清空，避免无界增长；清空后只是要求模型重新读取
            _READ_STATE.clear()


def record_read(session_id: str, path: str, *, full_read: bool) -> None:
    if not full_read:
        return
    _store_fingerprint(session_id, path)


def record_write(session_id: str, path: str) -> None:
    """写/改成功后刷新当前会话的文件指纹，使连续修改同一文件无需额外重读。"""
    _store_fingerprint(session_id, path)


def require_fresh_read(
    session_id: str,
    path: str,
    *,
    tool_name: str,
    skip: bool = False,
    require_read: bool = False,
) -> None:
    if skip:
        return

    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            return
        key_path = str(target.resolve(strict=False))
    except OSError:
        # 路径异常时保守放行，交给后续工具自身的错误处理
        return

    key = (session_id, key_path)
    with _LOCK:
        recorded = _READ_STATE.get(key)
        recorded_sha = recorded.get("sha256") if recorded else None

    if not recorded_sha:
        if not require_read:
            return
        raise StaleReadError(
            f"{tool_name} 拒绝修改：本会话尚未完整读取文件 {path}。"
            f"请先用 read_file 完整读取该文件（不要用 offset/limit 截断），然后再重试。"
        )

    try:
        current_sha = _fingerprint(target)["sha256"]
    except OSError:
        # 读不出来就无法比对，保守放行（真有问题由工具自身的读写逻辑报错）
        return

    if current_sha != recorded_sha:
        raise StaleReadError(
            f"{tool_name} 拒绝修改：文件 {path} 在本次读取后已被外部修改。"
            f"请用 read_file 重新完整读取最新内容后再重试。"
        )


def is_guard_enabled() -> bool:
    """守卫开关：环境变量 XINGYAO_FRESH_READ_GUARD 为 0/off/false 时关闭。"""
    raw = (os.environ.get("XINGYAO_FRESH_READ_GUARD") or "").strip().lower()
    return raw not in _DISABLED_VALUES
