import threading

import pytest

import core.llm as llm


class _FakeStream:
    def __init__(self, chunks=(), error=None, close_error=None):
        self._chunks = list(chunks)
        self._error = error
        self._close_error = close_error
        self.close_calls = 0

    def __iter__(self):
        if self._error is not None:
            raise self._error
        return iter(self._chunks)

    def close(self):
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error


class _WrapperWithoutClose:

    def __init__(self, inner):
        self.completion_stream = inner


def test_abort_closes_attached_stream():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)
    stream = _FakeStream()
    holder.attach(stream)

    llm.abort_inflight_streams(event)

    assert stream.close_calls == 1
    assert holder.closed is True


def test_abort_before_attach_closes_stream_immediately():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)

    llm.abort_inflight_streams(event)
    stream = _FakeStream()
    holder.attach(stream)  # attach 前已 abort：新建流必须立即关闭，避免泄漏

    assert stream.close_calls == 1
    llm._unregister_inflight_stream(event, holder)


def test_abort_is_idempotent_and_swallows_close_errors():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)
    stream = _FakeStream(close_error=RuntimeError("close boom"))
    holder.attach(stream)

    llm.abort_inflight_streams(event)
    llm.abort_inflight_streams(event)

    assert stream.close_calls == 1
    llm._unregister_inflight_stream(event, holder)


def test_close_walks_to_inner_completion_stream():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)
    inner = _FakeStream()
    holder.attach(_WrapperWithoutClose(inner))

    llm.abort_inflight_streams(event)

    assert inner.close_calls == 1
    llm._unregister_inflight_stream(event, holder)


def test_unregister_removes_holder_from_registry():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)
    assert event in llm._inflight_streams

    llm._unregister_inflight_stream(event, holder)

    assert event not in llm._inflight_streams
    stream = _FakeStream()
    holder.attach(stream)
    llm.abort_inflight_streams(event)
    assert stream.close_calls == 0


def test_abort_with_none_event_is_noop():
    llm.abort_inflight_streams(None)


def test_request_stream_registers_and_attaches(monkeypatch):
    fake = _FakeStream()
    monkeypatch.setattr(llm, "completion", lambda **kwargs: fake)
    event = threading.Event()

    stream, holder = llm._request_stream(
        "openai", {"model": "m", "messages": []}, timeout=1, cancel_event=event
    )

    assert stream is fake
    assert holder is not None
    assert holder in llm._inflight_streams[event]

    llm.abort_inflight_streams(event)
    assert fake.close_calls == 1

    llm._unregister_inflight_stream(event, holder)
    assert event not in llm._inflight_streams


def test_request_stream_failure_unregisters(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(llm, "completion", _boom)
    event = threading.Event()

    with pytest.raises(RuntimeError):
        llm._request_stream(
            "openai", {"model": "m", "messages": []}, timeout=1, cancel_event=event
        )

    assert event not in llm._inflight_streams


def test_stream_chunks_unregisters_holder_after_consumption():
    event = threading.Event()
    holder = llm._register_inflight_stream(event)
    stream = _FakeStream(chunks=["a", "b"])

    chunks = list(
        llm._stream_chunks_with_idle_notice(
            stream, None, lambda: None,
            inflight_holder=holder, cancel_event=event,
        )
    )

    assert chunks == ["a", "b"]
    assert event not in llm._inflight_streams


def test_stream_chunks_cancel_normalizes_stream_error_to_keyboard_interrupt():
    event = threading.Event()
    calls = []

    def raise_if_cancelled():
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt("用户终止了对话。")

    with pytest.raises(KeyboardInterrupt):
        list(
            llm._stream_chunks_with_idle_notice(
                _FakeStream(error=ConnectionError("stream closed")),
                None,
                raise_if_cancelled,
            )
        )


def test_subagent_terminate_aborts_inflight_streams(monkeypatch):
    import json

    from core.tools import SubagentManager

    aborted = []
    monkeypatch.setattr(
        "core.llm.abort_inflight_streams", lambda evt: aborted.append(evt)
    )

    release = threading.Event()
    manager = SubagentManager(
        runner=lambda record: release.wait(2) and "完成",
        wait_interval=0.001,
    )
    result = manager.run(
        "任务", "s1", "t1",
        run_in_background=True, timeout_seconds=30,
    )
    task_id = json.loads(result)["task_id"]
    cancel_event = manager._tasks[task_id].cancel_event

    manager._terminate(task_id, "cancelled", "用户取消")
    release.set()
    assert manager.wait_for_idle(2)

    assert cancel_event in aborted
