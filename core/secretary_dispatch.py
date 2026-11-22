from __future__ import annotations

from typing import List, Optional

__all__ = [
    "SecretaryDispatchError",
    "SecretaryDispatchService",
    "register",
    "unregister",
    "get",
]


class SecretaryDispatchError(Exception):
    """秘书调度能力的使用错误（参数非法 / 目标不存在 / 目录非法等可读原因）。"""


class SecretaryDispatchService:

    def search_conversations(
        self,
        keyword: str = "",
        date_from: str = "",
        date_to: str = "",
        workdir: str = "",
        limit: int = 20,
    ) -> List[dict]:
        """检索历史会话；返回 list[dict]，每项含 session_name / display_name /
        is_secretary / workdir / updated_at / message_count / snippets。"""
        raise NotImplementedError

    def dispatch_task(
        self,
        target: str = "",
        prompt: str = "",
        workdir: str = "",
        model_id: str = "",
        permission_mode: str = "",
    ) -> dict:
        """派发任务；返回 {ok, target_session, display_name, workdir, state}。"""
        raise NotImplementedError

    def dispatch_batch(self, tasks=None) -> dict:
        raise NotImplementedError

    def resume_dispatch(self, dispatch_id: int = 0) -> dict:
        raise NotImplementedError

    def create_project(self, path: str = "") -> dict:
        raise NotImplementedError

    def open_session(self, target: str = "") -> dict:
        """切换到目标会话；返回 {ok, session_name, display_name, workdir}。"""
        raise NotImplementedError

    def list_dispatches(
        self,
        status: str = "",
        workdir: str = "",
        target: str = "",
        limit: int = 20,
    ) -> List[dict]:
        raise NotImplementedError

    def get_dispatch_result(self, dispatch_id: int = 0) -> Optional[dict]:
        raise NotImplementedError


_instance: Optional[SecretaryDispatchService] = None


def register(service) -> None:
    global _instance
    _instance = service


def unregister() -> None:
    """清除已注册的句柄；幂等，未注册时调用不抛异常。"""
    global _instance
    _instance = None


def get() -> Optional[SecretaryDispatchService]:
    """返回已注册的句柄；未注册（如 CLI/headless）时返回 ``None``。"""
    return _instance
