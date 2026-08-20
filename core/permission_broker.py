"""Serialize permission requests from root and child agent threads."""

from __future__ import annotations

import threading
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Mapping


# 取消路径使用的独立结果值，与用户的 "拒绝" (1) 明确区分
CANCELLED_RESULT = -1


@dataclass(frozen=True)
class PermissionRequest:
    request_id: str
    function_name: str
    arguments: Mapping[str, object]
    owner_id: str
    parent_turn_id: str
    source: str
    options: tuple[str, ...]
    future: Future


class PermissionBroker:
    """Own one active confirmation while preserving every caller's Future."""

    def __init__(
        self,
        activate_prompt: Callable[[PermissionRequest], None] | None = None,
        dismiss_prompt: Callable[[], None] | None = None,
    ) -> None:
        self._activate_prompt = activate_prompt or (lambda request: None)
        self._dismiss_prompt = dismiss_prompt or (lambda: None)
        self._has_prompt_handlers = activate_prompt is not None
        self._active: PermissionRequest | None = None
        self._queued: deque[PermissionRequest] = deque()
        self._lock = threading.Lock()

    def has_prompt_handlers(self) -> bool:
        """是否已有入口层注册的提示处理器。

        没有处理器时入队请求永远无人应答，调用方应直接放弃询问而不是空等。
        """
        with self._lock:
            return self._has_prompt_handlers

    def set_prompt_handlers(
        self,
        activate_prompt: Callable[[PermissionRequest], None],
        dismiss_prompt: Callable[[], None],
    ) -> None:
        with self._lock:
            self._activate_prompt = activate_prompt
            self._dismiss_prompt = dismiss_prompt
            self._has_prompt_handlers = True
            active = self._active
        # 锁外激活前复核：请求可能在此期间已被 resolve/cancel
        if active is not None and self.is_active(active.request_id):
            activate_prompt(active)

    def enqueue(
        self,
        function_name: str,
        arguments: Mapping[str, object],
        owner_id: str,
        parent_turn_id: str,
        source: str,
        options: list[str] | tuple[str, ...] | None = None,
    ) -> PermissionRequest:
        choices = tuple(options or (
            "Yes, 同意本次操作 (y)",
            "No, 拒绝本次操作 (n)",
            "Always, 始终允许此类操作 (a)",
        ))
        if not 1 <= len(choices) <= 3:
            raise ValueError("permission request options must contain 1 to 3 items")
        request = PermissionRequest(
            request_id=f"permission-{uuid.uuid4().hex[:10]}",
            function_name=str(function_name),
            arguments=dict(arguments),
            owner_id=str(owner_id),
            parent_turn_id=str(parent_turn_id),
            source=str(source),
            options=choices,
            future=Future(),
        )
        activate = False
        with self._lock:
            if self._active is None:
                self._active = request
                activate = True
            else:
                self._queued.append(request)
        if activate:
            self._activate_prompt(request)
        return request

    def resolve(self, request_id: str, choice_index: int, custom_answer: str | None = None) -> bool:
        with self._lock:
            request = self._active
            if request is None or request.request_id != request_id:
                return False
            custom: str | None = None
            if custom_answer is not None and str(custom_answer).strip():
                # 自由文本回答仅对 ask_user 开放：其余权限请求的选项语义是
                # 允许/拒绝/始终允许，文本无从映射，按取消兜底避免语义错乱
                if request.function_name == "ask_user":
                    custom = str(custom_answer).strip()
                else:
                    choice_index = CANCELLED_RESULT
            else:
                # choice_index 可能是 UI 层透传的任意值：无法解析为整数时与越界索引
                # 一样按取消处理，绝不让 int() 的 ValueError 击穿持锁路径
                try:
                    choice_index = int(choice_index)
                except (TypeError, ValueError):
                    choice_index = CANCELLED_RESULT
                # 越界索引统一按取消处理，避免调用方 KeyError / 语义不一致
                if not 0 <= choice_index < len(request.options):
                    choice_index = CANCELLED_RESULT
            self._active = self._queued.popleft() if self._queued else None
            next_request = self._active

        if not request.future.done():
            request.future.set_result(custom if custom is not None else choice_index)
        self._show_next(next_request)
        return True

    def cancel_turn(self, parent_turn_id: str) -> int:
        return self._cancel(lambda request: request.parent_turn_id == parent_turn_id)

    def cancel_owner(self, owner_id: str) -> int:
        return self._cancel(lambda request: request.owner_id == owner_id)

    def cancel_all(self) -> int:
        """Cancel every active/queued request during application shutdown."""
        return self._cancel(lambda _request: True)

    def active_request(self) -> PermissionRequest | None:
        with self._lock:
            return self._active

    def is_active(self, request_id: str) -> bool:
        with self._lock:
            return self._active is not None and self._active.request_id == request_id

    def has_owner(self, owner_id: str) -> bool:
        """Return whether an owner has an active or queued request."""
        normalized = str(owner_id)
        with self._lock:
            if self._active is not None and self._active.owner_id == normalized:
                return True
            return any(request.owner_id == normalized for request in self._queued)

    def _cancel(self, predicate: Callable[[PermissionRequest], bool]) -> int:
        cancelled: list[PermissionRequest] = []
        active_changed = False
        with self._lock:
            if self._active is not None and predicate(self._active):
                cancelled.append(self._active)
                self._active = None
                active_changed = True

            remaining: deque[PermissionRequest] = deque()
            while self._queued:
                request = self._queued.popleft()
                if predicate(request):
                    cancelled.append(request)
                else:
                    remaining.append(request)
            self._queued = remaining

            if self._active is None and self._queued:
                self._active = self._queued.popleft()
            next_request = self._active

        for request in cancelled:
            if not request.future.done():
                request.future.set_result(CANCELLED_RESULT)
        if active_changed:
            self._show_next(next_request)
        return len(cancelled)

    def _show_next(self, request: PermissionRequest | None) -> None:
        # 注意：此方法可能由后台 worker 线程通过 resolve()/cancel_turn() 间接调用。
        # _activate_prompt 和 _dismiss_prompt 回调在注册时必须已内部使用
        # schedule_ui / app_instance.loop.call_soon_threadsafe 跳回 UI 主线程，
        # 绝不能在回调内直接操作 prompt_toolkit 的 UI 状态。
        if request is None:
            self._dismiss_prompt()
        else:
            self._activate_prompt(request)


permission_broker = PermissionBroker()
