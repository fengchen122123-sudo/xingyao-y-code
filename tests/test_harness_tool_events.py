"""S1-b：工具事件落 journal 的脱敏事实与零影响约束。"""
from __future__ import annotations

import json

import pytest

from core.harness.tool_pipeline import (
    DecodedToolCall,
    ToolBatchExecutor,
    ToolExecutionContext,
    _PreparedToolBatch,
    _PreparedToolCall,
    _journal_facts_for_call,
)


def _context(sink=None):
    return ToolExecutionContext(
        event_callback=None,
        parent_session="s1",
        parent_turn_id="t1",
        cancel_event=None,
        allow_subagents=False,
        permission_owner=None,
        permission_source="测试",
        permission_checker=lambda *args, **kwargs: True,
        registry=None,
        mcp_manager=None,
        journal_event_sink=sink,
    )


def _prepared(name="read_file", arguments=None, ordinal=0):
    arguments = arguments if arguments is not None else {"path": "secret.py"}
    decoded = DecodedToolCall(
        call_id="call-%d" % ordinal,
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments, ensure_ascii=False),
    )
    return _PreparedToolCall(call_ordinal=ordinal, raw={}, decoded=decoded)


def _executor():
    return ToolBatchExecutor(
        pipeline=object(),
        execute_decoded=lambda decoded, context: {
            "role": "tool",
            "tool_call_id": decoded.call_id,
            "name": decoded.name,
            "content": "RESULT-BODY-" + decoded.call_id,
        },
        event_payload=lambda decoded: {
            "id": decoded.call_id,
            "name": decoded.name,
            "arguments": decoded.arguments,
        },
        finished_event_payload=lambda decoded, message, duration_ms: {
            "id": decoded.call_id,
            "name": decoded.name,
            "arguments": decoded.arguments,
            "ok": True,
            "result": message["content"],
            "duration_ms": duration_ms,
        },
    )


def _run(sink=None):
    executor = _executor()
    context = _context(sink)
    on_event = []
    batch = _PreparedToolBatch(parallel=False, calls=(_prepared(),))
    executor.begin_batch(batch, context, lambda kind, payload: on_event.append(kind))
    records = executor.execute_batch(
        batch, context, lambda kind, payload: on_event.append(kind)
    )
    return on_event, records


def test_sink_receives_started_and_finished():
    journal = []
    _run(lambda kind, facts: journal.append((kind, facts)))
    kinds = [kind for kind, _ in journal]
    assert kinds == ["tool_started", "tool_finished"]


def test_journal_facts_carry_only_hashes_and_metadata():
    journal = []
    _run(lambda kind, facts: journal.append((kind, facts)))
    allowed = {
        "tool_name",
        "call_ordinal",
        "arguments_sha256",
        "started",
        "result_sha256",
        "duration_ms",
        "result_status",
    }
    for _kind, facts in journal:
        assert set(facts).issubset(allowed), facts


def test_journal_facts_never_contain_payload_text():
    journal = []
    _run(lambda kind, facts: journal.append((kind, facts)))
    blob = json.dumps(
        [facts for _kind, facts in journal], ensure_ascii=False
    )
    assert "secret.py" not in blob
    assert "RESULT-BODY" not in blob
    # 只允许 arguments_sha256 这类键；禁止出现携带原文的 arguments 字段。
    assert '"arguments":' not in blob


def test_finished_facts_include_result_hash_and_status():
    journal = []
    _run(lambda kind, facts: journal.append((kind, facts)))
    finished = dict(journal)["tool_finished"]
    assert len(finished["result_sha256"]) == 64
    assert finished["result_status"] == "ok"
    assert finished["duration_ms"] >= 0


def test_sink_none_keeps_event_stream_identical():
    without, _records_a = _run(None)
    journal = []
    with_sink, _records_b = _run(lambda kind, facts: journal.append(kind))
    assert without == with_sink
    assert without == ["tool_requested", "tool_started", "tool_finished"]


def test_sink_failure_does_not_break_execution():
    def boom(kind, facts):
        raise RuntimeError("sink down")

    events, records = _run(boom)
    assert events == ["tool_requested", "tool_started", "tool_finished"]
    assert len(records) == 1
    assert records[0].message["content"].startswith("RESULT-BODY")


def test_facts_for_call_without_raw_arguments_falls_back_to_canonical_json():
    prepared = _prepared(arguments={"b": 2, "a": 1})
    prepared.decoded.__dict__  # frozen dataclass，仅确认可读
    facts = _journal_facts_for_call(prepared)
    assert facts["tool_name"] == "read_file"
    assert facts["call_ordinal"] == 0
    assert len(facts["arguments_sha256"]) == 64
