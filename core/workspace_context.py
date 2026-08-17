"""线程本地工作目录上下文，替代进程级 os.chdir/Path.cwd 以支持多会话并行。"""
import contextlib
import threading
from pathlib import Path

_workspace_tls = threading.local()


def _normalize(path):
    try:
        return Path(str(path or "")).expanduser().resolve()
    except (OSError, ValueError, TypeError):
        return Path.cwd().resolve()


def set_workdir(path):
    """设置当前线程的工作目录（通常在回合/子代理线程启动时调用）。"""
    _workspace_tls.workdir = _normalize(path)


def get_workdir():
    """读取当前线程的工作目录；未显式设置时兜底返回进程 cwd。"""
    value = getattr(_workspace_tls, "workdir", None)
    if value is None:
        return Path.cwd().resolve()
    return value


def clear_workdir():
    """清除当前线程的工作目录上下文（回退到进程 cwd）。"""
    if hasattr(_workspace_tls, "workdir"):
        del _workspace_tls.workdir


@contextlib.contextmanager
def workspace_scope(path):
    """线程内临时绑定工作目录，退出时恢复原值（支持嵌套）。"""
    previous = getattr(_workspace_tls, "workdir", None)
    set_workdir(path)
    try:
        yield
    finally:
        if previous is None:
            clear_workdir()
        else:
            _workspace_tls.workdir = previous
