"""星轨自动化任务：core 层服务句柄（Agent 工具访问自动化能力的唯一入口）。

用途：GUI 启动时构造 ``AutomationService`` 并注入 7 个回调（全部由 gui_bridge
侧提供，落到 AutomationStorage/AutomationEngine 的真实实现），再调用
``register(service)``；core 层的 Agent 工具通过 ``get()`` 取得句柄并调用同名
方法，从而在不 ``import gui_bridge`` 的前提下访问自动化能力。

约定：
- 本模块是纯依赖注入容器，不做任何 I/O、不 import gui_bridge / webview /
  sqlite3、不起线程、不捕获异常（异常交由回调实现方与工具层处理）；
- CLI / headless 环境未注册时 ``get()`` 返回 ``None``，工具层据此提示能力不可用；
- 这是一次「配置类」工具的句柄式接缝，与 tool_registry 中的工具 wrapper 分离。
"""

from __future__ import annotations

from typing import Callable, Optional

__all__ = ["AutomationService", "register", "unregister", "get"]


class AutomationService:
    """自动化能力的依赖注入容器：把 7 个回调原样转发给调用方。

    所有回调均为关键字参数且必须可调用；方法体只做转发，不改变参数与返回值。
    """

    def __init__(
        self,
        *,
        list_tasks: Callable,
        get_task: Callable,
        save_task: Callable,
        delete_task: Callable,
        set_enabled: Callable,
        run_now: Callable,
        get_runs: Callable,
    ) -> None:
        self._list_tasks = list_tasks
        self._get_task = get_task
        self._save_task = save_task
        self._delete_task = delete_task
        self._set_enabled = set_enabled
        self._run_now = run_now
        self._get_runs = get_runs

    def list_tasks(self):
        return self._list_tasks()

    def get_task(self, task_id):
        return self._get_task(task_id)

    def save_task(self, task: dict):
        return self._save_task(task)

    def delete_task(self, task_id):
        return self._delete_task(task_id)

    def set_enabled(self, task_id, enabled):
        return self._set_enabled(task_id, enabled)

    def run_now(self, task_id):
        return self._run_now(task_id)

    def get_runs(self, task_id, limit):
        return self._get_runs(task_id, limit)


_instance: Optional[AutomationService] = None


def register(service: AutomationService) -> None:
    """注册（或覆盖）当前进程的自动化服务句柄。"""
    global _instance
    _instance = service


def unregister() -> None:
    """清除已注册的句柄；幂等，未注册时调用不抛异常。"""
    global _instance
    _instance = None


def get() -> Optional[AutomationService]:
    """返回已注册的句柄；未注册（如 CLI/headless）时返回 ``None``。"""
    return _instance
