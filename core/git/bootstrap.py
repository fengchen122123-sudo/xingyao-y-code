"""进程级 Git 服务与监控单例（GUI 与 CLI 共用，规格 §6 架构图）。"""

from __future__ import annotations

import threading

from core.git.monitor import GitStateMonitor
from core.git.service import GitApplicationService

_LOCK = threading.RLock()
_SERVICE: GitApplicationService | None = None
_MONITOR: GitStateMonitor | None = None


def get_git_service() -> GitApplicationService:
    global _SERVICE
    with _LOCK:
        if _SERVICE is None:
            _SERVICE = GitApplicationService()
        return _SERVICE


def get_git_monitor() -> GitStateMonitor:
    global _MONITOR
    with _LOCK:
        if _MONITOR is None:
            _MONITOR = GitStateMonitor()
        return _MONITOR


def reset_git_singletons_for_tests() -> None:
    """测试专用：停掉监控并清空单例（conftest 的 YCODE_DATA_DIR 隔离下使用）。"""
    global _SERVICE, _MONITOR
    with _LOCK:
        if _MONITOR is not None:
            _MONITOR.stop()
        _MONITOR = None
        _SERVICE = None
