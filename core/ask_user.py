"""Interactive user question adapter backed by the existing TUI prompt."""

from __future__ import annotations

import json
import threading

from core.permission_broker import CANCELLED_RESULT, permission_broker

# 等待用户选择时的轮询间隔（秒），用于及时响应取消信号
_POLL_INTERVAL_SECONDS = 0.1


def normalize_question_request(
    question: str,
    options: list[str] | None,
) -> tuple[str, list[str]]:
    normalized_question = str(question or "").strip()
    if not normalized_question:
        raise ValueError("无法提问：问题内容为空。")
    if options is not None and not isinstance(options, (list, tuple)):
        raise ValueError("无法提问：选项必须是列表。")

    choices = [
        str(option).strip()
        for option in (options or ["继续", "停止"])
        if str(option).strip()
    ]
    if not 1 <= len(choices) <= 3:
        raise ValueError("无法提问：选项数量必须在 1 到 3 个之间。")
    return normalized_question, choices


def _cancelled_payload(question: str, selected_index: int) -> str:
    return json.dumps({
        "status": "cancelled",
        "question": question,
        "selected_index": selected_index,
        "selected": None,
    }, ensure_ascii=False)


def ask_user(
    question: str,
    options: list[str] | None = None,
    *,
    owner_id: str | None = None,
    parent_turn_id: str | None = None,
    source: str = "主代理",
    cancel_event: threading.Event | None = None,
) -> str:
    try:
        question, choices = normalize_question_request(question, options)
    except ValueError as error:
        return str(error)

    import main

    app = main.app_instance
    if app is None or app.loop is None:
        return "无法提问：当前终端没有活动的交互界面。"

    # owner/turn_id 统一来源：优先显式传入，turn 兜底 owner，线程名最后兜底
    resolved_owner = str(owner_id or parent_turn_id or threading.current_thread().name)
    resolved_turn = str(parent_turn_id or resolved_owner)
    request = permission_broker.enqueue(
        "ask_user",
        {"question": question},
        resolved_owner,
        resolved_turn,
        source,
        options=choices,
    )
    # 带短超时的轮询等待：取消信号到达时立即跳出，避免线程永久挂起
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                permission_broker.cancel_owner(resolved_owner)
                selected_index = CANCELLED_RESULT
                break
            try:
                selected_index = int(request.future.result(timeout=_POLL_INTERVAL_SECONDS))
                break
            except TimeoutError:
                continue
    finally:
        # 异常退出（含 KeyboardInterrupt 等 BaseException）时回收已入队请求，
        # 否则 broker 串行化会让后续所有提问/权限提示永久排队
        if not request.future.done():
            permission_broker.cancel_owner(resolved_owner)
    if selected_index < 0 or selected_index >= len(choices):
        # 取消或越界索引统一按取消处理，不再返回 selected: null 的伪成功
        return _cancelled_payload(question, selected_index)
    return json.dumps({
        "question": question,
        "selected_index": selected_index,
        "selected": choices[selected_index],
    }, ensure_ascii=False)
