"""每个数据目录一把 serve 锁，避免两个 worker 同时打开同一份库。

设计要点：**锁字节与 PID 文本分离**。

- Windows 用 ``msvcrt.locking`` 做字节区间锁，而字节区间锁会让被锁住的
  区间对其它句柄不可读（读会拿到 ERROR_LOCK_VIOLATION → PermissionError）。
  老实现锁偏移 0 的那一个字节，导致进程存活期间任何 ``read_text()`` 都读
  不到 serve.lock 里的 PID，诊断/测试只能靠杀进程判断。现在改为锁
  ``_LOCK_OFFSET`` 处的哨兵字节（远在 EOF 之后，Windows 允许锁 EOF 之后
  的区间），PID 文本仍写在偏移 0，于是「互斥」与「可读 PID」同时成立。
- POSIX 用 ``flock``：整文件排他锁不影响 read，无需偏移技巧。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 哨兵锁字节的偏移：远大于任何合理的 PID 文本长度，保证 PID 文本永远
# 落在未加锁区间里（Windows 上才能被其它进程读到）。
_LOCK_OFFSET = 1 << 20


def _lock(handle) -> None:
    """对哨兵字节加排他非阻塞锁；失败抛 OSError。"""
    if sys.platform == "win32":
        import msvcrt

        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle) -> None:
    if sys.platform == "win32":
        import msvcrt

        handle.seek(_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_pid(path: Path | str) -> int | None:
    """读取 serve.lock 里记录的 PID；文件缺失/内容损坏一律返回 None。

    与锁实现解耦：持有者进程存活时这个读取路径也必须成功（见模块 docstring）。
    """
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    try:
        pid = int(text)
    except ValueError:
        return None
    return pid if pid > 0 else None


class ServeLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._fh = handle
        try:
            # PID 文本写在偏移 0；加锁的是 _LOCK_OFFSET 处的哨兵字节，
            # 两者不重叠，所以这里不会被自己的锁挡住。
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except OSError:
            pass
        return True

    def release(self) -> None:
        handle = self._fh
        self._fh = None
        if handle is None:
            return
        try:
            _unlock(handle)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass
