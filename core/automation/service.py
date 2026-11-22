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
    global _instance
    _instance = service


def unregister() -> None:
    """清除已注册的句柄；幂等，未注册时调用不抛异常。"""
    global _instance
    _instance = None


def get() -> Optional[AutomationService]:
    """返回已注册的句柄；未注册（如 CLI/headless）时返回 ``None``。"""
    return _instance
