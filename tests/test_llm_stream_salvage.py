from types import SimpleNamespace

import core.llm as llm


def _chunk(content=""):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def test_stream_interrupt_salvages_partial_text(mocker):
    persisted = []

    class Manager:
        @staticmethod
        def add_message(session, role, message):
            persisted.append((session, role, message))

        @staticmethod
        def add_messages(session, messages):
            persisted.append((session, "batch", list(messages)))

    def stream_then_fail(**kwargs):
        yield _chunk(content="已经生成的部分正文：这是流式中断前的内容。")
        raise ConnectionError("模拟连接中断（流中途）")

    mocker.patch("core.app_context.get_manager", return_value=Manager())
    mocker.patch("core.llm.completion", side_effect=stream_then_fail)

    list(
        llm.chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "说点东西"}],
            persist=True,
            turn_id="turn-a1-02-regression",
            parent_session="a1-02-regression-session",
        )
    )

    salvaged = [
        msg
        for _session, kind, batch in persisted
        if kind == "batch"
        for role, msg in batch
        if role == "assistant" and isinstance(msg, dict)
    ]
    target = [
        msg for msg in salvaged if "已经生成的部分正文" in str(msg.get("content") or "")
    ]
    assert target, "流中断后应打捞含部分正文的 assistant 消息"
    assert "[本轮流式中断]" in str(target[0]["content"])