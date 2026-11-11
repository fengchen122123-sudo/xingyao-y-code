"""写前新鲜度守卫：模型修改已有文件前必须在本会话内完整读过该文件。

设计目标（最小可靠）：
1. read_file 完整返回正文时登记文件指纹（大小 + sha256）；
2. write_file / edit_file 写前校验：本会话登记过，且文件 sha256 未变化；
3. 任何异常都不能影响原有工具的正常返回，因此指纹读取失败一律由调用方吞掉。

可通过环境变量 XINGYAO_FRESH_READ_GUARD=0/off/false 关闭守卫（默认开启）。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path


_LOCK = threading.Lock()

# 键：(session_id, 规范化绝对路径)；值：{"size": int, "sha256": str}
_READ_STATE: dict[tuple[str, str], dict] = {}

# 上限保护：超出后整体清空（简单可靠，不做 LRU）
MAX_TRACKED = 4096

# 关闭守卫的环境变量取值（不区分大小写）
_DISABLED_VALUES = frozenset({"0", "off", "false"})

_CHUNK_SIZE = 1024 * 1024


class StaleReadError(RuntimeError):
    """写前新鲜度校验失败：未完整读过目标文件，或文件在读取后已被改动。"""


def _fingerprint(path: Path) -> dict:
    """计算文件指纹（字节数 + sha256 十六进制）。

    读失败直接抛 OSError，由调用方吞掉——守卫本身不允许打断工具主流程。
    """
    stat = path.stat()
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return {"size": stat.st_size, "sha256": digest.hexdigest()}


def record_read(session_id: str, path: str, *, full_read: bool) -> None:
    """登记一次完整读取。

    full_read 为 False（被 offset/limit 截断）时直接返回：只有把整个文件看完
    才算读过，截断读取不足以支撑后续的精准修改。
    """
    if not full_read:
        return
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


def require_fresh_read(
    session_id: str,
    path: str,
    *,
    tool_name: str,
    skip: bool = False,
    require_read: bool = False,
) -> None:
    """写前校验：文件内容自本会话读取后未发生变化。

    默认宽松模式：本会话没读过该文件时放行——用户直接给内容让模型覆盖是很
    常见的合法用法，拦下来会打断正常流程。真正危险的是「读过之后文件被别人
    改了，模型还按旧内容覆盖」，这一种必须拦。

    require_read=True 时切到严格模式：没读过也拦（需要保证「先读后改」纪律
    的调用方使用）。

    skip=True（守卫被关闭）或目标文件不存在（新建文件场景）时直接放行。
    """
    if skip:
        return

    try:
        target = Path(path)
        if not target.exists() or not target.is_file():
            # 新建文件：没有旧内容可被覆盖，无需新鲜度校验
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
            # 宽松模式：没读过就写，属于「用户直接给内容」的常见用法，放行。
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
