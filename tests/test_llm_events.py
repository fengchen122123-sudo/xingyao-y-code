from types import SimpleNamespace

from core.llm import chat_with_model_events


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=tool_calls,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=None,
    )


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_event_stream_orders_parallel_read_tools(mocker):
    first_response = iter([
        _chunk(
            "先查看文件。",
            [
                _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
                _tool_call(1, "call-2", "glob_files", '{"pattern":"*.md"}'),
            ],
        )
    ])
    final_response = iter([_chunk("检查完成。")])
    mocker.patch("core.llm.completion", side_effect=[first_response, final_response])

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "检查项目"}],
        persist=False,
        turn_id="turn-test",
    ))

    kinds = [event.kind for event in events]
    requested = [
        event.payload["id"]
        for event in events
        if event.kind == "tool_requested"
    ]
    finished = [
        event.payload["id"]
        for event in events
        if event.kind == "tool_finished"
    ]

    assert requested == ["call-1", "call-2"]
    assert finished == ["call-1", "call-2"]
    assert kinds.index("tool_requested") < kinds.index("tool_finished")
    assert kinds[-1] == "turn_finished"
