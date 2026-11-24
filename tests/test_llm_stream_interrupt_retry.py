"""流式中断重连重发：零产出时换连接重试一次，已有产出时禁止重发。"""

from types import SimpleNamespace

import httpx

import core.llm as llm

GOAWAY_TEXT = (
    "<ConnectionTerminated error_code:0, last_stream_id:49, additional_data:None>"
)


def _chunk(content="", reasoning=""):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _stub_manager(mocker):
    class Manager:
        @staticmethod
        def add_message(session, role, message):
            pass

        @staticmethod
        def add_messages(session, messages):
            pass

    mocker.patch("core.app_context.get_manager", return_value=Manager())


def _collect_events(mocker):
    events = []

    def _fake_notify(*args, **kwargs):
        events.append(args)
        return None

    mocker.patch("core.llm._notify_event", side_effect=_fake_notify)
    return events


def _run_turn(turn_id):
    return list(
        llm.chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "干活"}],
            persist=True,
            turn_id=turn_id,
            parent_session=f"{turn_id}-session",
        )
    )


def test_zero_output_interruption_retries_once(mocker):
    """零产出断流：必须重连重发一次，且第二次的内容要正常交付。"""
    _stub_manager(mocker)
    _collect_events(mocker)
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            def broken():
                raise httpx.RemoteProtocolError(GOAWAY_TEXT)
                yield  # pragma: no cover
            return broken()

        def healthy():
            yield _chunk(content="重连后的正文")
        return healthy()

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    output = _run_turn("turn-stream-retry-zero-output")
    text = "".join(str(item) for item in output)

    assert len(calls) == 2, f"零产出断流应重连重发一次，实际请求 {len(calls)} 次"
    assert "重连后的正文" in text, f"重连后的正文未交付，实际输出：{text!r}"


def test_partial_output_interruption_does_not_retry(mocker):
    """已经吐出可见正文后再断流：禁止重发，避免重复内容。"""
    _stub_manager(mocker)
    _collect_events(mocker)
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)

        def partial_then_broken():
            yield _chunk(content="已经生成的内容")
            raise httpx.RemoteProtocolError(GOAWAY_TEXT)

        return partial_then_broken()

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    _run_turn("turn-stream-retry-partial-output")

    assert len(calls) == 1, f"已有产出时不得重发，实际请求 {len(calls)} 次"


def test_read_timeout_is_not_retried_in_stream(mocker):
    """读超时不属于连接被回收，不触发重连重发。"""
    _stub_manager(mocker)
    _collect_events(mocker)
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)

        def times_out():
            raise httpx.ReadTimeout("读超时")
            yield  # pragma: no cover

        return times_out()

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    _run_turn("turn-stream-retry-read-timeout")

    assert len(calls) == 1, f"读超时不得重发，实际请求 {len(calls)} 次"


def test_retryable_stream_interruption_classifier():
    """异常判定：连接级断流为真，取消/超时/协议违规为假。"""
    from core.llm import _is_retryable_stream_interruption

    assert _is_retryable_stream_interruption(
        httpx.RemoteProtocolError(GOAWAY_TEXT)
    ) is True
    assert _is_retryable_stream_interruption(
        httpx.ReadError("connection reset by peer")
    ) is True
    assert _is_retryable_stream_interruption(
        httpx.ReadTimeout("timeout")
    ) is False
    assert _is_retryable_stream_interruption(KeyboardInterrupt()) is False
    assert _is_retryable_stream_interruption(
        httpx.LocalProtocolError("bad client frame")
    ) is False
