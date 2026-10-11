"""tests/test_llm_stream_salvage.py —— A1-02：流式中断打捞的入库回归。

修复前：流式消费中抛通用异常（如连接中断）时，已生成并展示过的部分正文
不落库，重开会话即丢失（DB 与用户所见分叉）。
修复后：salvage_streaming_partial 把部分正文打捞为一条 assistant 消息
（带「[本轮流式中断]」标注），随后照常回滚内存残影。

构造方式与 tmp\\a102\\repro_a1_02.py 一致：mock llm.completion 为
「先 yield 一条含正文的 chunk、再 raise ConnectionError」的生成器。
"""

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
    """流式中断（通用异常）后，已生成的部分正文必须打捞落库且带中断标注。"""
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