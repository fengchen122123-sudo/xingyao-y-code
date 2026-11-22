import json
import os
import pathlib
import threading
from types import SimpleNamespace

import pytest

from core.llm import _show_file_diff, chat_with_model_events


def test_blocking_call_worker_is_daemon_and_returns_result():
    import core.llm as llm_mod

    captured = {}

    def work():
        captured["daemon"] = threading.current_thread().daemon
        return "ok"

    future = llm_mod._submit_daemon_call(work)

    assert future.result(timeout=1) == "ok"
    assert captured["daemon"] is True


def _chunk(content="", tool_calls=None, reasoning="", provider_fields=None, usage=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=provider_fields,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta)],
        usage=usage,
    )


def _usage_chunk(total, prompt):
    usage = SimpleNamespace(
        total_tokens=total,
        prompt_tokens=prompt,
        prompt_tokens_details=None,
    )
    return SimpleNamespace(choices=[], usage=usage)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def test_diff_ready_contains_exact_tool_call_id():
    events = []

    _show_file_diff(
        "sample.py",
        "old\n",
        "new\n",
        lambda kind, payload: events.append((kind, payload)),
        panel_title="test diff",
        truncated_note="test",
        tool_call_id="edit-7",
    )

    assert events[0][0] == "diff_ready"
    assert events[0][1]["tool_call_id"] == "edit-7"
    assert events[0][1]["file_path"] == "sample.py"


def test_diff_ready_event_carries_full_diff_beyond_terminal_25_lines():
    old_lines = "\n".join(f"old line {i}" for i in range(60)) + "\n"
    new_lines = "\n".join(f"new line {i}" for i in range(60)) + "\n"
    events = []

    _show_file_diff(
        "big.py",
        old_lines,
        new_lines,
        lambda kind, payload: events.append((kind, payload)),
        panel_title="test diff",
        truncated_note="test",
        tool_call_id="edit-big",
    )

    payload = events[0][1]
    assert payload["diff_text"].count("\n") > 25
    assert "省略" not in payload["diff_text"]
    assert payload["diff_text"].count("-old line 59") == 1
    assert payload["diff_text"].count("+new line 59") == 1
    assert "省略" in payload["text"]


def test_write_file_execution_emits_diff_ready_for_exact_tool_call_id(mocker, tmp_path):
    import core.llm as llm

    target = tmp_path / "write-target.txt"
    events = []
    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)
    mocker.patch.object(llm, "is_outside_workspace", return_value=False)
    tc = {
        "id": "write-diff-7",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"file_path": str(target), "content": "new\n"}),
        },
    }

    llm._execute_single_tool_call(
        tc,
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    diff_event = next(event for event in events if event[0] == "diff_ready")
    assert diff_event[1]["tool_call_id"] == "write-diff-7"
    assert diff_event[1]["file_path"] == str(target)


def test_edit_file_execution_emits_diff_ready_for_exact_tool_call_id(mocker, tmp_path):
    import core.llm as llm

    target = tmp_path / "edit-target.txt"
    target.write_text("old\n", encoding="utf-8")
    events = []
    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)
    mocker.patch.object(llm, "is_outside_workspace", return_value=False)
    tc = {
        "id": "edit-diff-7",
        "function": {
            "name": "edit_file",
            "arguments": json.dumps({
                "file_path": str(target),
                "target_content": "old",
                "replacement_content": "new",
            }),
        },
    }

    llm._execute_single_tool_call(
        tc,
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    diff_event = next(event for event in events if event[0] == "diff_ready")
    assert diff_event[1]["tool_call_id"] == "edit-diff-7"
    assert diff_event[1]["file_path"] == str(target)


def test_compression_replaces_messages_in_place(mocker, monkeypatch):
    from core import app_context

    monkeypatch.setattr(app_context, "get_current_session", lambda: "compress-session")
    compressed = [{"role": "user", "content": "压缩后的历史"}]

    class FakeManager:
        def get_history_for_llm(self, session):
            assert session == "compress-session"
            return list(compressed)

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())

    compress_calls = []

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        compress_calls.append((session, force))
        assert session == "compress-session"

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        iter([_chunk("完成")]),
    ])

    messages = [{"role": "user", "content": "原始问题"}]
    original_id = id(messages)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-compress-in-place",
        consume_background_notifications=False,
    ))

    assert id(messages) == original_id
    assert {"role": "user", "content": "压缩后的历史"} in messages
    assert all(m.get("content") != "原始问题" for m in messages)
    assert messages[-1]["role"] == "assistant"
    assert events[-1].kind == "turn_finished"


def test_rate_limit_error_does_not_trigger_compression(mocker, monkeypatch):
    from core import app_context

    monkeypatch.setattr(app_context, "get_current_session", lambda: "rate-limit-session")
    compress_called = []

    class FakeManager:
        def get_history(self, session):
            return []

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        compress_called.append(session)

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("time.sleep")
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("rate limit exceeded for this model"),
    )

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        persist=False,
        turn_id="turn-rate-limit",
        consume_background_notifications=False,
    ))

    assert compress_called == []
    assert "turn_error" in [event.kind for event in events]


def test_native_reasoning_emits_exactly_one_thinking_event_group(mocker):
    mocker.patch("core.llm.completion", return_value=iter([
        _chunk(reasoning="分析过程"),
        _chunk("最终回答"),
    ]))

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "分析"}],
        persist=False,
        turn_id="turn-single-thinking",
    ))

    kinds = [event.kind for event in events]
    assert kinds.count("thinking_started") == 1
    assert kinds.count("thinking_finished") == 1
    thinking = "".join(
        event.payload["text"] for event in events if event.kind == "thinking_delta"
    )
    assert thinking == "分析过程"


def test_exception_emits_turn_error_without_turn_finished_and_rolls_back(mocker):
    mocker.patch("time.sleep")
    mocker.patch(
        "core.llm.completion",
        side_effect=RuntimeError("provider exploded"),
    )
    messages = [{"role": "user", "content": "提问"}]
    original = list(messages)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-error-event",
        consume_background_notifications=False,
    ))

    kinds = [event.kind for event in events]
    assert "turn_error" in kinds
    assert "turn_finished" not in kinds
    assert "error" in kinds
    assert messages == original


def test_repeated_tool_call_name_is_not_concatenated(mocker):
    first_response = iter([
        _chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
        ]),
        _chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", ''),
        ]),
    ])
    final_response = iter([_chunk("完成")])
    mocker.patch(
        "core.llm.completion",
        side_effect=[first_response, final_response],
    )
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-tool-name-dedup",
    ))

    assistant = next(
        message for message in messages if message.get("tool_calls")
    )
    assert assistant["tool_calls"][0]["function"]["name"] == "glob_files"


def test_multiple_usage_chunks_are_counted_once(mocker):
    import core.llm as llm

    with llm.token_usage_lock:
        baseline_total = llm.session_token_usage["cumulative_total"]
        baseline_prompt = llm.session_token_usage["cumulative_prompt"]

    mocker.patch("core.llm.completion", return_value=iter([
        _usage_chunk(10, 6),
        _chunk("回答"),
        _usage_chunk(20, 12),
    ]))

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "提问"}],
        persist=False,
        turn_id="turn-usage-once",
    ))

    with llm.token_usage_lock:
        assert llm.session_token_usage["latest_total"] == 20
        assert llm.session_token_usage["cumulative_total"] == baseline_total + 20
        assert llm.session_token_usage["cumulative_prompt"] == baseline_prompt + 12


def test_cancel_rollback_preserves_drained_notifications(mocker):
    notif = "<task-notification>后台结果</task-notification>"
    drain = mocker.patch(
        "core.tools.subagent_manager.drain_notifications",
        return_value=[notif],
    )
    cancel_event = threading.Event()

    def cancelling_stream():
        yield _chunk("部分输出")
        cancel_event.set()
        yield _chunk("被忽略")

    mocker.patch("core.llm.completion", return_value=cancelling_stream())
    messages = [{"role": "user", "content": "原始问题"}]

    try:
        list(chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id="turn-cancel-keep-notif",
            cancel_event=cancel_event,
            parent_session="session-notif",
        ))
    except KeyboardInterrupt:
        pass

    drain.assert_called_once_with("session-notif")
    assert messages == [
        {"role": "user", "content": "原始问题"},
        {"role": "user", "content": notif},
    ]


def test_reasoning_fields_stripped_for_unsupported_providers(mocker):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "openai/gpt-4o",
        "fake-key",
        [
            {"role": "user", "content": "提问"},
            {
                "role": "assistant",
                "content": "历史回答",
                "reasoning_content": "历史推理",
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "历史推理", "signature": "sig"}
                ],
            },
            {"role": "user", "content": "继续"},
        ],
        persist=False,
        turn_id="turn-reasoning-strip",
        consume_background_notifications=False,
    ))

    assistant = next(
        message
        for message in captured["messages"]
        if message["role"] == "assistant"
    )
    assert "reasoning_content" not in assistant
    assert "thinking_blocks" not in assistant


def test_compression_uses_parent_session_not_main_session(mocker, monkeypatch):
    from core import app_context

    monkeypatch.setattr(app_context, "get_current_session", lambda: "main-ui-session")
    calls = []
    compressed = [{"role": "user", "content": "压缩后的子代理历史"}]

    class FakeManager:
        def get_history_for_llm(self, session):
            calls.append(("history_for_llm", session))
            return list(compressed)

    fake_manager = FakeManager()
    monkeypatch.setattr(app_context, "get_manager", lambda: fake_manager)

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        calls.append(("compress", session))

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        iter([_chunk("完成")]),
    ])

    messages = [{"role": "user", "content": "子代理原始问题"}]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-child-compress",
        parent_session="child-session",
        consume_background_notifications=False,
    ))

    assert calls == [
        ("compress", "child-session"),
        ("history_for_llm", "child-session"),
    ]
    assert {"role": "user", "content": "压缩后的子代理历史"} in messages
    assert all(m.get("content") != "子代理原始问题" for m in messages)
    assert events[-1].kind == "turn_finished"


def test_rollback_after_successful_compression_keeps_compressed_history(mocker, monkeypatch):
    from core import app_context

    monkeypatch.setattr(app_context, "get_current_session", lambda: "compress-then-fail")
    compressed = [{"role": "user", "content": "压缩后的历史"}]

    class FakeManager:
        def get_history_for_llm(self, session):
            return list(compressed)

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        pass

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)
    mocker.patch("time.sleep")
    mocker.patch("core.llm.completion", side_effect=[
        RuntimeError("prompt is too long: context window exceeded"),
        RuntimeError("provider exploded"),
    ])

    messages = [{"role": "user", "content": "原始问题"}]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-compress-then-fail",
        consume_background_notifications=False,
    ))

    assert "turn_error" in [event.kind for event in events]
    assert {"role": "user", "content": "压缩后的历史"} in messages
    assert all(m.get("content") != "原始问题" for m in messages)


def test_edit_file_dispatch_passes_replace_all(mocker, tmp_path):
    import core.llm as llm

    target = tmp_path / "data.txt"
    target.write_text("aaa bbb aaa", encoding="utf-8")
    captured = {}

    def fake_edit_file(
        file_path,
        target_content,
        replacement_content,
        replace_all=False,
        start_line=None,
        end_line=None,
    ):
        captured["replace_all"] = replace_all
        return f"{llm.TOOL_ERROR_PREFIX} 模拟失败，跳过后续 diff 读取"

    mocker.patch.object(llm, "edit_file", fake_edit_file)
    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)

    tool_call = {
        "id": "call-replace-all",
        "function": {
            "name": "edit_file",
            "arguments": (
                '{"file_path": "%s", "target_content": "aaa", '
                '"replacement_content": "ccc", "replace_all": true}'
                % str(target).replace("\\", "\\\\")
            ),
        },
    }
    llm._execute_single_tool_call(
        tool_call,
        event_callback=lambda *args, **kwargs: None,
    )

    assert captured["replace_all"] is True


def test_write_file_old_content_falls_back_to_gbk(mocker, tmp_path):
    import hashlib

    import core.llm as llm
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service
    from core.turn_undo import recorder as tu_recorder
    from core.turn_undo.models import OwnerKind

    target = tmp_path / "gbk-file.txt"
    original = "中文内容".encode("gbk")
    target.write_bytes(original)

    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = tu_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("gbk-sess", [("user", "hi")])
    session_id = storage.resolve_session_id("gbk-sess")
    checkpoint, ctx = tu_service.begin_turn(
        session_id=session_id,
        turn_id="turn-gbk-write",
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )

    mocker.patch.object(llm, "check_permission_and_ask", return_value=True)

    tool_call = {
        "id": "call-gbk-write",
        "function": {
            "name": "write_file",
            "arguments": '{"file_path": "%s", "content": "new"}' % str(target).replace("\\", "\\\\"),
        },
    }
    try:
        result = llm._execute_single_tool_call(
            tool_call,
            event_callback=lambda *args, **kwargs: None,
        )
    finally:
        tu_service.finish_turn(ctx)

    files = storage.list_files(checkpoint.checkpoint_id)
    assert len(files) == 1
    row = files[0]
    assert row.before_sha256 == hashlib.sha256(original).hexdigest()
    assert storage.blob_store.read_bytes(row.before_blob_sha256) == original
    assert result["content"].startswith("已成功将内容写入文件")


def test_generator_preamble_exception_emits_turn_error(mocker):
    mocker.patch(
        "core.memory_kairos.build_memory_system_section",
        side_effect=RuntimeError("memory subsystem exploded"),
    )

    raised = False
    events = []
    try:
        for event in chat_with_model_events(
            "fake-model",
            "fake-key",
            [{"role": "user", "content": "提问"}],
            persist=False,
            turn_id="turn-preamble-error",
            consume_background_notifications=False,
        ):
            events.append(event)
    except RuntimeError:
        raised = True

    assert raised is True
    kinds = [event.kind for event in events]
    assert "turn_error" in kinds
    assert "turn_finished" not in kinds


def test_final_summary_retry_removes_empty_assistant_message(mocker):
    responses = [
        iter([_chunk(tool_calls=[
            _tool_call(0, "call-1", "glob_files", '{"pattern":"*.py"}'),
        ])]),
        iter([_chunk("...")]),
        iter([_chunk("检查结论：一切正常。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-final-summary-cleanup",
        consume_background_notifications=False,
    ))

    for message in messages:
        if message["role"] == "assistant" and not message.get("tool_calls"):
            assert message.get("content")


def test_sandbox_guard_passes_through_for_inside_path(tmp_path, monkeypatch):
    import core.llm as llm_mod

    monkeypatch.chdir(tmp_path)
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(tmp_path / "inside.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert cm is None and err is None
    assert asked == [], "沙盒内路径不应触发权限询问"


def test_sandbox_guard_asks_and_denies_outside_path(tmp_path, monkeypatch):
    import core.llm as llm_mod
    import core.tools as tools_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: False)
    monkeypatch.setattr(llm_mod, "check_permission_and_ask", lambda *a, **kw: False)

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(tmp_path / "outside.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert cm is None
    assert err is not None and "拒绝" in err


def test_sandbox_guard_exempts_memory_dir_for_read_and_write(tmp_path, monkeypatch):
    import core.llm as llm_mod
    import core.memory_kairos as mk

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    ycode_dir = tmp_path / ".ycode"
    memory_dir = ycode_dir / "memory"
    memory_dir.mkdir(parents=True)
    monkeypatch.setattr(mk, "YCODE_DIR", ycode_dir)
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    for function_name in ("read_file", "write_file", "edit_file"):
        cm, err = llm_mod._sandbox_outside_guard(
            function_name, str(memory_dir / "note.md"),
            None, "owner", "turn", "主代理", None,
        )
        assert err is None, f"{function_name} 访问 memory 目录不应被拒绝"
        assert cm is not None, f"{function_name} 访问 memory 目录应获得 bypass 上下文"
    assert asked == [], "memory 目录访问不应触发权限询问"


def test_sandbox_guard_reads_ycode_dir_but_asks_before_writing_credentials(
    tmp_path, monkeypatch
):
    import core.llm as llm_mod
    import core.memory_kairos as mk

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(mk, "YCODE_DIR", tmp_path / ".ycode")
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    for rel in ("sessions/s1.jsonl", "memory/MEMORY.md", "input_history.txt"):
        cm, err = llm_mod._sandbox_outside_guard(
            "read_file", str(tmp_path / ".ycode" / rel),
            None, "owner", "turn", "主代理", None,
        )
        assert err is None, f".ycode/{rel} 不应被拒绝"
        assert cm is not None, f".ycode/{rel} 应获得 bypass 上下文"

    for rel in ("config.json", "config.json.bak", "mcp.json", "mcp.json.bak"):
        cm, err = llm_mod._sandbox_outside_guard(
            "read_file", str(tmp_path / ".ycode" / rel),
            None, "owner", "turn", "主代理", None,
        )
        assert err is None, f".ycode/{rel} 读取不应被拒绝"
        assert cm is not None, f".ycode/{rel} 读取应获得 bypass 上下文"
    assert asked == []

    for rel in ("config.json", "mcp.json"):
        cm, err = llm_mod._sandbox_outside_guard(
            "write_file", str(tmp_path / ".ycode" / rel),
            None, "owner", "turn", "主代理", None,
        )
        assert cm is None
        assert err is not None
    assert len(asked) == 2


def test_sandbox_guard_rejects_unverifiable_path_without_asking(monkeypatch):
    import core.llm as llm_mod

    asked = []
    monkeypatch.setattr(
        llm_mod,
        "is_outside_workspace",
        lambda _path: (_ for _ in ()).throw(RuntimeError("classification failed")),
    )
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append((args, kwargs)) or True,
    )

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", "unverifiable.txt", None, "owner", "turn", "主代理", None,
    )

    assert cm is None
    assert err is not None and "无法验证路径" in err
    assert asked == []


def test_sandbox_guard_toggle_on_auto_approves_outside_paths(tmp_path, monkeypatch):
    import core.llm as llm_mod
    import core.model_manager as mm

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        mm, "load_config", lambda: {"allow_outside_workspace": True}
    )
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    for function_name in ("read_file", "write_file", "edit_file", "list_directory"):
        cm, err = llm_mod._sandbox_outside_guard(
            function_name, str(tmp_path / "outside.txt"),
            None, "owner", "turn", "主代理", None,
        )
        assert err is None, f"{function_name} 开关打开时不应被拒绝"
        assert cm is not None, f"{function_name} 开关打开时应获得 bypass 上下文"
    assert asked == [], "开关打开时不应触发权限询问"


def test_sandbox_guard_toggle_on_still_asks_credential_writes(tmp_path, monkeypatch):
    import core.llm as llm_mod
    import core.memory_kairos as mk
    import core.model_manager as mm

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(mk, "YCODE_DIR", tmp_path / ".ycode")
    monkeypatch.setattr(
        mm, "load_config", lambda: {"allow_outside_workspace": True}
    )
    asked = []
    monkeypatch.setattr(
        llm_mod, "check_permission_and_ask",
        lambda *a, **kw: asked.append(1) or False,
    )

    for rel in ("config.json", "mcp.json"):
        cm, err = llm_mod._sandbox_outside_guard(
            "read_file", str(tmp_path / ".ycode" / rel),
            None, "owner", "turn", "主代理", None,
        )
        assert err is None and cm is not None
    assert asked == []

    for rel in ("config.json", "mcp.json"):
        cm, err = llm_mod._sandbox_outside_guard(
            "write_file", str(tmp_path / ".ycode" / rel),
            None, "owner", "turn", "主代理", None,
        )
        assert cm is None
        assert err is not None
    assert len(asked) == 2


def test_sandbox_guard_uses_configured_ycode_data_dir_for_credentials(monkeypatch):
    import config
    import core.llm as llm_mod
    import core.model_manager as mm

    data_dir = pathlib.Path(config.YCODE_DIR)
    monkeypatch.setattr(mm, "load_config", lambda: {"allow_outside_workspace": True})
    monkeypatch.setattr(llm_mod, "is_outside_workspace", lambda _path: True)
    asked = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append(args) or False,
    )

    for function_name, target in (
        ("read_file", data_dir / "sessions" / "s1.jsonl"),
        ("write_file", data_dir / "ycode.db"),
        ("write_file", data_dir / "memory" / "note.md"),
    ):
        bypass, error = llm_mod._sandbox_outside_guard(
            function_name, str(target), None, "owner", "turn", "主代理", None
        )
        assert error is None
        assert bypass is not None
    assert asked == []

    bypass, error = llm_mod._sandbox_outside_guard(
        "write_file",
        str(data_dir / "config.json"),
        None,
        "owner",
        "turn",
        "主代理",
        None,
    )
    assert bypass is None
    assert error is not None
    assert len(asked) == 1


def test_sandbox_guard_credential_classification_failure_is_fail_closed(
    monkeypatch,
):
    import core.llm as llm_mod
    import core.memory_kairos as mk
    import core.model_manager as mm

    monkeypatch.setattr(llm_mod, "is_outside_workspace", lambda _path: True)
    monkeypatch.setattr(mm, "load_config", lambda: {"allow_outside_workspace": True})
    monkeypatch.setattr(
        mk,
        "is_ycode_data_path",
        lambda _path: (_ for _ in ()).throw(RuntimeError("classification failed")),
    )
    asked = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append(args) or True,
    )

    bypass, error = llm_mod._sandbox_outside_guard(
        "write_file",
        str(mk.YCODE_DIR / "config.json"),
        None,
        "owner",
        "turn",
        "主代理",
        None,
    )

    assert bypass is None
    assert error is not None and "无法验证路径" in error
    assert asked == []


def test_sandbox_guard_tail_dot_credential_still_requires_confirmation(
    tmp_path, monkeypatch
):
    import core.llm as llm_mod
    import core.memory_kairos as mk
    import core.model_manager as mm

    ycode_dir = tmp_path / ".ycode"
    ycode_dir.mkdir()
    monkeypatch.setattr(mk, "YCODE_DIR", ycode_dir)
    monkeypatch.setattr(mm, "load_config", lambda: {"allow_outside_workspace": True})
    asked = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append(args) or False,
    )

    bypass, error = llm_mod._sandbox_outside_guard(
        "write_file",
        str(ycode_dir / "config.json") + ".",
        None,
        "owner",
        "turn",
        "主代理",
        None,
    )

    assert bypass is None
    assert error is not None
    assert len(asked) == 1


def test_sandbox_guard_plugin_config_is_not_a_top_level_credential(
    tmp_path, monkeypatch
):
    import core.llm as llm_mod
    import core.memory_kairos as mk

    ycode_dir = tmp_path / ".ycode"
    plugin_config = ycode_dir / "plugins" / "x" / "config.json"
    plugin_config.parent.mkdir(parents=True)
    monkeypatch.setattr(mk, "YCODE_DIR", ycode_dir)
    asked = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append(args) or False,
    )

    bypass, error = llm_mod._sandbox_outside_guard(
        "write_file", str(plugin_config), None, "owner", "turn", "主代理", None
    )

    assert error is None
    assert bypass is not None
    assert asked == []


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 short paths only")
def test_sandbox_guard_short_name_credential_requires_confirmation(
    tmp_path, monkeypatch
):
    import ctypes
    import core.llm as llm_mod
    import core.memory_kairos as mk
    import core.model_manager as mm

    ycode_dir = tmp_path / ".ycode"
    ycode_dir.mkdir()
    target = ycode_dir / "config.json"
    target.write_text("{}", encoding="utf-8")
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.kernel32.GetShortPathNameW(str(target), buffer, len(buffer))
    if not result or "~" not in buffer.value:
        pytest.skip("此卷未提供 8.3 短文件名")

    monkeypatch.setattr(mk, "YCODE_DIR", ycode_dir)
    monkeypatch.setattr(mm, "load_config", lambda: {"allow_outside_workspace": True})
    asked = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: asked.append(args) or False,
    )

    bypass, error = llm_mod._sandbox_outside_guard(
        "write_file", buffer.value, None, "owner", "turn", "主代理", None
    )

    assert bypass is None
    assert error is not None
    assert len(asked) == 1


@pytest.mark.parametrize(
    ("function_name", "arguments"),
    [
        ("read_file", '{"file_path": "unverifiable.txt"}'),
        ("write_file", '{"file_path": "unverifiable.txt", "content": "new"}'),
        ("edit_file", '{"file_path": "unverifiable.txt", "target_content": "old", "replacement_content": "new"}'),
    ],
)
def test_unverifiable_path_never_prompts_or_executes_a_file_tool(
    monkeypatch, function_name, arguments
):
    import core.llm as llm_mod

    prompted = []
    executed = []
    monkeypatch.setattr(
        llm_mod,
        "is_outside_workspace",
        lambda _path: (_ for _ in ()).throw(RuntimeError("classification failed")),
    )
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda *args, **kwargs: prompted.append((args, kwargs)) or True,
    )
    from core.tool_registry import ToolRegistry

    monkeypatch.setattr(
        ToolRegistry,
        "execute",
        lambda *args, **kwargs: (
            executed.append((args, kwargs))
            or SimpleNamespace(to_message_content=lambda: "executed")
        ),
    )
    monkeypatch.setattr(
        llm_mod,
        "write_file",
        lambda *args, **kwargs: (
            executed.append((args, kwargs))
            or f"{llm_mod.TOOL_ERROR_PREFIX} unexpected write"
        ),
    )
    monkeypatch.setattr(
        llm_mod,
        "edit_file",
        lambda *args, **kwargs: (
            executed.append((args, kwargs))
            or f"{llm_mod.TOOL_ERROR_PREFIX} unexpected edit"
        ),
    )

    result = llm_mod._execute_single_tool_call({
        "id": f"call-{function_name}",
        "function": {"name": function_name, "arguments": arguments},
    })

    assert "无法验证路径" in result["content"]
    assert prompted == []
    assert executed == []


def test_external_missing_edit_requests_sandbox_access_before_existence_probe(tmp_path, monkeypatch):
    import core.llm as llm_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "outside" / "missing.txt"
    monkeypatch.chdir(workspace)
    prompts = []
    probes = []
    monkeypatch.setattr(
        llm_mod,
        "check_permission_and_ask",
        lambda permission, arguments, **kwargs: prompts.append((permission, arguments)) or False,
    )
    import os as _os

    monkeypatch.setattr(
        _os.path,
        "isfile",
        lambda path: probes.append(path) or False,
    )

    result = llm_mod._execute_single_tool_call({
        "id": "call-missing-edit",
        "function": {
            "name": "edit_file",
            "arguments": (
                '{"file_path": "%s", "target_content": "old", "replacement_content": "new"}'
                % str(missing).replace("\\", "\\\\")
            ),
        },
    })

    assert "拒绝" in result["content"]
    assert prompts == [
        ("sandbox_outside_access", {"path": str(missing), "operation": "edit_file"})
    ]
    assert probes == []


@pytest.mark.parametrize("function_name", ["write_file", "edit_file"])
def test_sandbox_guard_requires_confirmation_for_writing_skill_file(
    tmp_path, monkeypatch, function_name
):
    import core.llm as llm_mod
    import core.skills as skills_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = tmp_path / "skills" / "example"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Example", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(skill_dir)])
    asked = []

    def deny_outside_access(permission, arguments, **_kwargs):
        asked.append((permission, arguments))
        return False

    monkeypatch.setattr(llm_mod, "check_permission_and_ask", deny_outside_access)

    cm, err = llm_mod._sandbox_outside_guard(
        function_name, str(skill_file), None, "owner", "turn", "主代理", None,
    )

    assert cm is None
    assert err is not None and "拒绝" in err
    assert asked == [
        ("sandbox_outside_access", {"path": str(skill_file), "operation": function_name})
    ]


def test_sandbox_guard_approved_bypass_allows_read(tmp_path, monkeypatch):
    import core.llm as llm_mod
    import core.tools as tools_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("外部内容", encoding="utf-8")
    monkeypatch.chdir(workspace)

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    tools_mod.set_session_sandbox_bypass(False)
    monkeypatch.setattr(llm_mod, "check_permission_and_ask", lambda *a, **kw: True)

    cm, err = llm_mod._sandbox_outside_guard(
        "read_file", str(outside / "note.txt"),
        None, "owner", "turn", "主代理", None,
    )
    assert err is None and cm is not None
    with cm:
        assert tools_mod.read_file(str(outside / "note.txt")) == "外部内容"
    import pytest as _pytest
    with _pytest.raises(ValueError, match="路径越界"):
        tools_mod.read_file(str(outside / "note.txt"))


def test_session_sandbox_bypass_flag(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})
    tools_mod.set_session_sandbox_bypass(False)
    assert tools_mod._outside_workspace_allowed() is False
    tools_mod.set_session_sandbox_bypass(True)
    try:
        assert tools_mod._outside_workspace_allowed() is True
    finally:
        tools_mod.set_session_sandbox_bypass(False)


def test_imom_active_publish_root_reads_store(tmp_path, monkeypatch):
    import json as _json
    import os as _os

    import core.llm as llm_mod

    store_dir = tmp_path / ".ycode"
    store_dir.mkdir()
    (store_dir / "imom_projects.json").write_text(
        _json.dumps({
            "active": "proj_a",
            "projects": {"proj_a": {"publish_root": "/data/publish"}},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(_os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

    assert llm_mod._get_imom_active_publish_root() == "/data/publish"


def test_plan_mode_still_asks_sandbox_outside_read_access(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod.app_context, "get_permission_mode", lambda: "plan")
    asked = []
    monkeypatch.setattr(
        llm_mod.app_context,
        "get_permission_callback",
        lambda: (lambda *args, **kwargs: asked.append((args, kwargs)) or True),
    )

    assert llm_mod.check_permission_and_ask(
        "sandbox_outside_access", {"path": "/outside", "operation": "read_file"}
    ) is True
    assert asked == [(('sandbox_outside_access', {
        "path": "/outside",
        "operation": "read_file",
    }), {})]


def test_plan_mode_blocks_modifying_tools(monkeypatch):
    import json as _json
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod.app_context, "get_permission_mode", lambda: "plan")
    executed = []
    from core.tool_registry import ToolRegistry

    monkeypatch.setattr(
        ToolRegistry, "execute",
        lambda name, **kw: executed.append(name),
    )

    def _tc(name, args):
        return {
            "id": f"tc-{name}",
            "function": {"name": name, "arguments": _json.dumps(args)},
        }

    for name, args in (
        ("write_file", {"file_path": "a.txt", "content": "x"}),
        ("edit_file", {"file_path": "a.txt", "old_string": "a", "new_string": "b"}),
        ("execute_command", {"command": "echo hi"}),
    ):
        result = llm_mod._execute_single_tool_call(_tc(name, args))
        assert result["content"] == llm_mod.PLAN_MODE_DENIED_MESSAGE, name
    assert executed == []


def _image_msg(text="看图"):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAABBBB"}},
        ],
    }


def test_vision_model_keeps_latest_image_and_strips_older(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: True)
    messages = [
        {"role": "system", "content": "sys"},
        _image_msg("第一张"),
        {"role": "assistant", "content": "好的"},
        _image_msg("第二张"),
    ]
    out = llm_mod._strip_media_and_merge(messages, "openai/vision-model")[0]

    assert isinstance(out[1]["content"], str)
    assert "AAAABBBB" not in out[1]["content"]
    assert "[图片]" in out[1]["content"]
    assert isinstance(out[3]["content"], list)
    assert out[3]["content"][1]["type"] == "image_url"
    assert "AAAABBBB" in out[3]["content"][1]["image_url"]["url"]


def test_non_vision_model_flattens_image_without_base64_leak(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: False)
    out = llm_mod._strip_media_and_merge([_image_msg()], "openai/text-model")[0]
    assert isinstance(out[0]["content"], str)
    assert "AAAABBBB" not in out[0]["content"]
    assert "[图片]" in out[0]["content"]


def test_media_list_content_not_merged_with_text(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_supports_vision", lambda model: True)
    out = llm_mod._strip_media_and_merge(
        [{"role": "user", "content": "补充"}, _image_msg("问题")], "openai/vision-model"
    )[0]
    assert len(out) == 2
    assert out[0]["content"] == "补充"
    assert isinstance(out[1]["content"], list)


def test_supports_vision_config_flag_takes_precedence(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"providers": [{"models": [
            {"litellm_model": "openai/m1", "supports_vision": False},
            {"litellm_model": "openai/m2", "supports_vision": True},
        ]}]},
    )
    assert llm_mod._supports_vision("openai/m1") is False
    assert llm_mod._supports_vision("openai/m2") is True


def test_supports_vision_unknown_default_false(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr("core.model_manager.load_config", lambda: {"providers": []})
    assert llm_mod._supports_vision("openai/step-3.7-flash") is False
    assert llm_mod._supports_vision("anthropic/K3") is False
    assert llm_mod._supports_vision("deepseek/deepseek-chat") is False
    assert llm_mod._supports_vision(None) is False


def test_reasoning_only_turn_is_nudged_to_act(mocker):
    responses = [
        iter([_chunk(reasoning="让我先精确定位需要修改的 SQL，然后做修改。")]),
        iter([_chunk("检查结论：已定位问题并完成修改。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "使用方案A修改一下"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-nudge",
        consume_background_notifications=False,
    ))

    assert any(
        m["role"] == "user" and "只输出了思考过程" in str(m.get("content", ""))
        for m in messages
    )
    final = [m for m in messages if m["role"] == "assistant" and m.get("content")]
    assert final and "检查结论" in final[-1]["content"]
    assert "让我先精确定位" not in final[-1]["content"]


def test_reasoning_only_promotion_is_truncated(mocker):
    long_reasoning = "分析过程。" * 2000
    responses = [
        iter([_chunk(reasoning=long_reasoning)]),
        iter([_chunk(reasoning=long_reasoning)]),
        iter([_chunk(reasoning=long_reasoning)]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "使用方案A修改一下"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-truncated",
        consume_background_notifications=False,
    ))

    final = [m for m in messages if m["role"] == "assistant" and m.get("content")]
    assert final, "促升兜底仍应留下一条可见回复"
    content = final[-1]["content"]
    assert content.rstrip().endswith("[以上为模型思考过程，未生成正式回复]")
    assert "已截断" in content
    assert len(content) < 1000


def test_reasoning_only_retries_twice_before_giving_up(mocker):
    responses = [
        iter([_chunk(reasoning="第一次思考")]),
        iter([_chunk(reasoning="第二次思考")]),
        iter([_chunk(reasoning="第三次思考")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "使用方案A修改一下"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-retry-twice",
        consume_background_notifications=False,
    ))

    nudges = [
        m for m in messages
        if m["role"] == "user" and "只输出了思考过程" in str(m.get("content", ""))
    ]
    assert len(nudges) == 2, f"应注入两次催促，实际 {len(nudges)} 条"

    final = [m for m in messages if m["role"] == "assistant" and m.get("content")]
    assert final, "促升兜底仍应留下一条可见回复"
    assert "未生成正式回复" in final[-1]["content"]
    assert any(e.kind == "error" for e in events), "重试耗尽后必须发出 error 事件"


def test_reasoning_only_emits_error_when_retries_exhausted(mocker):
    responses = [
        iter([_chunk(reasoning="思考A")]),
        iter([_chunk(reasoning="思考B")]),
        iter([_chunk(reasoning="思考C")]),
        iter([_chunk(reasoning="思考D")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    messages = [{"role": "user", "content": "请完成这个任务"}]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-reasoning-only-error",
        consume_background_notifications=False,
    ))

    error_events = [e for e in events if e.kind == "error"]
    assert error_events, "重试耗尽后必须发出 error 事件"
    assert any("未完成" in str(e.payload) or "未返回" in str(e.payload) for e in error_events)


def _chunk_with_finish(content="", reasoning="", finish_reason=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def test_reasoning_fields_preserved_for_all_assistant_messages(mocker):
    captured = {}

    def fake_completion(**kwargs):
        captured.update(kwargs)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "anthropic/K3",
        "fake-key",
        [
            {"role": "user", "content": "提问"},
            {
                "role": "assistant",
                "content": "促升的思考预览……[以上为模型思考过程，未生成正式回复]",
                "reasoning_content": "废推理" * 5000,
                "thinking_blocks": [
                    {"type": "thinking", "thinking": "废推理", "signature": "sig"}
                ],
            },
            {
                "role": "assistant",
                "content": "工具轮",
                "reasoning_content": "签名链推理",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
            {"role": "user", "content": "继续"},
        ],
        "https://api.kimi.com/coding",
        persist=False,
        turn_id="turn-dead-reasoning-strip",
        consume_background_notifications=False,
    ))

    assistants = [m for m in captured["messages"] if m["role"] == "assistant"]
    no_tool = next(m for m in assistants if not m.get("tool_calls"))
    with_tool = next(m for m in assistants if m.get("tool_calls"))
    assert no_tool["reasoning_content"] == "废推理" * 5000
    assert no_tool["thinking_blocks"] == [
        {"type": "thinking", "thinking": "废推理", "signature": "sig"}
    ]
    assert with_tool["reasoning_content"] == "签名链推理"


def test_openai_reasoning_route_strips_anthropic_thinking_blocks():
    from core.llm import _strip_media_and_merge

    cleaned, _, _ = _strip_media_and_merge([
        {
            "role": "assistant",
            "content": "完成",
            "reasoning_content": "分析过程",
            "thinking_blocks": [
                {"type": "thinking", "thinking": "分析过程", "signature": "sig"},
            ],
        },
    ], "openai/kimi-for-coding")

    assert cleaned[0]["reasoning_content"] == "分析过程"
    assert "thinking_blocks" not in cleaned[0]


def test_length_truncated_retry_bumps_max_tokens(mocker):
    captured = []
    responses = [
        iter([_chunk_with_finish(reasoning="长思考" * 100, finish_reason="length")]),
        iter([_chunk_with_finish(content="最终结论", finish_reason="stop")]),
    ]

    def fake_completion(**kwargs):
        captured.append(dict(kwargs))
        return responses.pop(0)

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    messages = [{"role": "user", "content": "排查问题"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-length-retry-bump",
        consume_background_notifications=False,
    ))

    assert len(captured) == 2
    first_max = captured[0].get("max_tokens") or captured[0].get("max_completion_tokens")
    second_max = captured[1].get("max_tokens") or captured[1].get("max_completion_tokens")
    assert second_max == min(first_max * 2, 32768)
    assert any(
        m["role"] == "user" and "输出长度限制被截断" in str(m.get("content", ""))
        for m in messages
    )


def test_context_overflow_circuit_breaker_after_compression(mocker, monkeypatch):
    from core import app_context

    monkeypatch.setattr(app_context, "get_current_session", lambda: "overflow-breaker-session")

    class FakeManager:
        def get_history(self, session):
            return [{"role": "user", "content": "压缩后的历史"}]

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())

    compress_calls = []

    def fake_compress(manager, session, api_model, api_key, api_url, force=False, output_callback=None):
        compress_calls.append(session)

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", fake_compress)

    completion_calls = []

    def always_overflow(**kwargs):
        completion_calls.append(kwargs)
        raise RuntimeError("prompt is too long: context window exceeded")

    mocker.patch("core.llm.completion", side_effect=always_overflow)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "原始问题"}],
        persist=False,
        turn_id="turn-overflow-breaker",
        consume_background_notifications=False,
    ))

    assert compress_calls == ["overflow-breaker-session"]
    assert len(completion_calls) == 2
    error_events = [e for e in events if e.kind == "turn_error"]
    assert error_events, "熔断后必须向 UI 抛出 turn_error"
    assert any(
        "上下文窗口" in str(e.payload.get("message", "")) or "额度" in str(e.payload.get("message", ""))
        for e in error_events
    )


def test_request_timeout_scales_with_payload(mocker, monkeypatch):
    captured = []

    def fake_completion(**kwargs):
        captured.append(dict(kwargs))
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    monkeypatch.setattr("core.llm.estimate_tokens", lambda model, msgs: 1000)
    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "小请求"}],
        persist=False,
        turn_id="turn-timeout-small",
        consume_background_notifications=False,
    ))
    assert captured[-1]["timeout"] == 90

    monkeypatch.setattr("core.llm.estimate_tokens", lambda model, msgs: 300_000)
    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "大请求"}],
        persist=False,
        turn_id="turn-timeout-large",
        consume_background_notifications=False,
    ))
    assert captured[-1]["timeout"] == 90 + 300_000 // 1500

    monkeypatch.setattr("core.llm.estimate_tokens", lambda model, msgs: 100_000_000)
    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "超大请求"}],
        persist=False,
        turn_id="turn-timeout-huge",
        consume_background_notifications=False,
    ))
    from core.llm import API_REQUEST_MAX_TIMEOUT_SECONDS
    assert captured[-1]["timeout"] == API_REQUEST_MAX_TIMEOUT_SECONDS


def test_wait_notice_emitted_during_slow_request(mocker, monkeypatch):
    import time as _time

    monkeypatch.setattr("core.llm.WAIT_NOTICE_THRESHOLD_SECONDS", 0.05)
    monkeypatch.setattr("core.llm.WAIT_NOTICE_INTERVAL_SECONDS", 0.05)

    def slow_completion(**kwargs):
        _time.sleep(0.3)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=slow_completion)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "慢请求"}],
        persist=False,
        turn_id="turn-wait-notice",
        consume_background_notifications=False,
    ))

    notices = [
        e for e in events
        if e.kind == "status" and "模型响应中" in str(e.payload.get("message", ""))
    ]
    assert notices, "慢请求期间必须上报“模型响应中”状态事件"
    assert "超时上限" in str(notices[0].payload["message"])


def test_wait_notice_flushed_realtime_not_batched_after_first_chunk(mocker, monkeypatch):
    import time as _time

    monkeypatch.setattr("core.llm.WAIT_NOTICE_THRESHOLD_SECONDS", 0.05)
    monkeypatch.setattr("core.llm.WAIT_NOTICE_INTERVAL_SECONDS", 0.05)

    completion_returned_at = []

    def slow_completion(**kwargs):
        _time.sleep(0.4)
        completion_returned_at.append(_time.monotonic())
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=slow_completion)

    notice_times = []
    for event in chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "慢请求"}],
        persist=False,
        turn_id="turn-wait-notice-realtime",
        consume_background_notifications=False,
    ):
        if (
            event.kind == "status"
            and "模型响应中" in str(event.payload.get("message", ""))
        ):
            notice_times.append(_time.monotonic())

    assert notice_times, "慢请求期间必须上报“模型响应中”状态事件"
    assert completion_returned_at, "slow_completion 必须真实返回过"
    assert min(notice_times) < completion_returned_at[0], (
        "等待提示必须在 completion 返回（首个正文可用）之前实时到达，"
        "而不是积压到首个正文 chunk 之后一次性冲刷"
    )


def test_request_wait_give_up_ends_turn_at_hard_cap_without_retry(mocker, monkeypatch):
    import time as _time

    monkeypatch.setattr("core.llm.WAIT_NOTICE_THRESHOLD_SECONDS", 0.05)
    monkeypatch.setattr("core.llm.WAIT_NOTICE_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("core.llm.REQUEST_WAIT_GIVE_UP_SECONDS", 0.25)

    def hanging_completion(**kwargs):
        _time.sleep(5)
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=hanging_completion)

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "挂起请求"}],
        persist=False,
        turn_id="turn-wait-give-up",
        consume_background_notifications=False,
    ))

    give_up_errors = [
        e for e in events
        if e.kind == "turn_error" and "仍未收到首字节" in str(e.payload.get("message", ""))
    ]
    assert give_up_errors, "等待硬上限必须以 turn_error 结束本轮"
    retry_notices = [
        e for e in events
        if e.kind == "status" and "重试" in str(e.payload.get("message", ""))
    ]
    assert not retry_notices, "等待硬上限绝不允许进入 API 重试循环"


def test_fold_old_media_for_estimate_keeps_only_latest_image():
    from core.llm import _fold_old_media_for_estimate

    base64_blob = "a" * 200_000

    def img_msg(text):
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_blob}"}},
            ],
        }

    messages = [
        img_msg("第一张图"),
        {"role": "assistant", "content": "看到了"},
        img_msg("第二张图"),
    ]
    folded = _fold_old_media_for_estimate(messages)

    assert folded[0]["content"] == "第一张图 [图片]"
    assert base64_blob not in str(folded[0])
    assert folded[2] is messages[2]
    assert base64_blob in str(folded[2])


def test_estimate_tokens_ignores_old_image_base64(mocker):
    import core.llm as llm_module
    from core.llm import estimate_tokens

    base64_blob = "b" * 200_000
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "旧图"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64_blob}"}},
            ],
        },
        {"role": "assistant", "content": "已识别"},
        {"role": "user", "content": "继续"},
    ]
    counted = []

    def fake_counter(model_name, msgs):
        counted.append(msgs)
        return 123

    llm_module._token_estimate_cache["sig"] = None
    mocker.patch("core.llm._token_counter", side_effect=fake_counter)

    assert estimate_tokens("fake-model", messages) == 123
    assert counted, "必须调用 _token_counter 计数"
    assert base64_blob not in str(counted[0])
    assert "[图片]" in str(counted[0])


def test_plan_active_does_not_prune_request_tools(mocker, monkeypatch):
    monkeypatch.setattr("core.llm._has_pending_plan", lambda session: True)

    captured = []

    def fake_completion(**kwargs):
        captured.append(dict(kwargs))
        return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "继续执行计划"}],
        persist=False,
        turn_id="turn-plan-tools-stable",
        consume_background_notifications=False,
    ))

    assert captured, "必须发出请求"
    tool_names = {s.get("function", {}).get("name") for s in (captured[0].get("tools") or [])}
    assert "write_file" in tool_names, "计划进行中不得裁剪写工具（执行层拦截兜底）"
    assert "run_subagent" in tool_names
    assert "plan_write" in tool_names


def test_greeting_turn_keeps_tools_for_cache_stability(mocker):
    captured = []

    def fake_completion(**kwargs):
        captured.append(dict(kwargs))
        return iter([_chunk("你好！")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "你好"}],
        persist=False,
        turn_id="turn-greeting-tools",
        consume_background_notifications=False,
    ))

    assert captured[0].get("tools"), "问候轮 tools 不得为 None"
    assert captured[0].get("tool_choice") == "auto"


def test_official_deepseek_thinking_omits_tool_choice(mocker, monkeypatch):
    captured = []

    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: None,
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda model, base_url=None: "openai",
    )
    mocker.patch(
        "core.llm.completion",
        side_effect=lambda **kwargs: captured.append(dict(kwargs)) or iter([_chunk("完成")]),
    )

    list(chat_with_model_events(
        "openai/deepseek-v4-pro",
        "fake-key",
        [{"role": "user", "content": "你好"}],
        "https://api.deepseek.com",
        persist=False,
        turn_id="turn-deepseek-thinking-contract",
        consume_background_notifications=False,
    ))

    assert "tool_choice" not in captured[0]
    assert captured[0]["reasoning_effort"] == "max"
    assert captured[0]["allowed_openai_params"] == ["reasoning_effort"]
    assert captured[0]["extra_body"]["thinking"] == {"type": "enabled"}


def test_deepseek_non_thinking_keeps_auto_tool_choice(mocker, monkeypatch):
    captured = []

    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: {"enable_thinking": False},
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda model, base_url=None: "openai",
    )
    mocker.patch(
        "core.llm.completion",
        side_effect=lambda **kwargs: captured.append(dict(kwargs)) or iter([_chunk("完成")]),
    )

    list(chat_with_model_events(
        "openai/deepseek-v4-pro",
        "fake-key",
        [{"role": "user", "content": "你好"}],
        "https://api.deepseek.com",
        persist=False,
        turn_id="turn-deepseek-nonthinking-contract",
        consume_background_notifications=False,
    ))

    assert captured[0]["tool_choice"] == "auto"
    assert "allowed_openai_params" not in captured[0]
    assert captured[0]["extra_body"]["thinking"] == {"type": "disabled"}


def test_get_tools_schema_waits_for_mcp_only_once(mocker, monkeypatch):
    import core.llm as llm_mod

    calls = []
    monkeypatch.setattr(llm_mod.mcp_manager, "_ensure_connected", lambda: calls.append(1))
    monkeypatch.setattr(llm_mod, "_mcp_first_wait_done", False)

    llm_mod.get_tools_schema()
    llm_mod.get_tools_schema()

    assert calls == [1], "MCP 同步等待只允许发生在进程内首次调用"


def test_estimate_tokens_incremental_counts_only_tail(mocker):
    import core.llm as llm_mod
    from core.llm import estimate_tokens

    llm_mod._token_estimate_cache["sig"] = None
    llm_mod._token_prefix_cache.update({"model": None, "msg_count": 0, "char_len": 0, "value": 0})

    counted_batches = []

    def fake_counter(model_name, msgs):
        counted_batches.append(len(msgs))
        return sum(len(str(m.get("content", ""))) for m in msgs) // 10

    mocker.patch("core.llm._token_counter", side_effect=fake_counter)

    history = [{"role": "user", "content": "x" * 1000} for _ in range(50)]
    v1 = estimate_tokens("fake-model", history)
    assert counted_batches == [50], "首次必须全量计数"

    history2 = history + [{"role": "assistant", "content": "y" * 500}]
    v2 = estimate_tokens("fake-model", history2)
    assert counted_batches == [50, 1], "第二轮必须只计数新增的 1 条尾部消息"
    assert v2 > v1, "增量结果必须大于前值"

    v3 = estimate_tokens("fake-model", history2)
    assert v3 == v2
    assert counted_batches == [50, 1]


def test_estimate_tokens_prefix_changed_falls_back_to_full_count(mocker):
    import core.llm as llm_mod
    from core.llm import estimate_tokens

    llm_mod._token_estimate_cache["sig"] = None
    llm_mod._token_prefix_cache.update({"model": None, "msg_count": 0, "char_len": 0, "value": 0})

    counted_batches = []

    def fake_counter(model_name, msgs):
        counted_batches.append(len(msgs))
        return sum(len(str(m.get("content", ""))) for m in msgs) // 10

    mocker.patch("core.llm._token_counter", side_effect=fake_counter)

    history = [{"role": "user", "content": "x" * 1000} for _ in range(50)]
    estimate_tokens("fake-model", history)

    compressed = [{"role": "user", "content": "压缩后" * 10} for _ in range(5)]
    estimate_tokens("fake-model", compressed + [{"role": "user", "content": "新问题"}])


def test_estimate_tokens_cache_snapshot_cannot_mix_concurrent_models(monkeypatch):
    import threading
    import time
    import core.llm as llm_mod

    model_b_written = threading.Event()

    class InterleavingDict(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "sig" and value and value[0] == "model-a":
                model_b_written.wait(timeout=0.2)
            elif key == "value" and self.get("sig", (None,))[0] == "model-b":
                model_b_written.set()

    monkeypatch.setattr(
        llm_mod,
        "_token_estimate_cache",
        InterleavingDict(sig=None, value=0),
    )
    monkeypatch.setattr(
        llm_mod,
        "_token_prefix_cache",
        {"model": None, "msg_count": 0, "char_len": 0, "value": 0},
    )
    monkeypatch.setattr(
        llm_mod,
        "_token_counter",
        lambda model, messages: 100 if model == "model-a" else 200,
    )
    messages = [{"role": "user", "content": "hello"}]
    results = {}
    threads = [
        threading.Thread(
            target=lambda value=model: results.__setitem__(
                value, llm_mod.estimate_tokens(value, messages)
            )
        )
        for model in ("model-a", "model-b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert all(not thread.is_alive() for thread in threads)
    assert results == {"model-a": 100, "model-b": 200}
    assert llm_mod.estimate_tokens("model-b", messages) == 200



def test_stream_usage_recovered_from_wrapper_chunks(mocker, monkeypatch):
    import core.llm as llm_mod

    final_usage = SimpleNamespace(
        total_tokens=692,
        prompt_tokens=676,
        prompt_tokens_details=SimpleNamespace(cached_tokens=512),
    )

    class FakeStream:

        chunks = [SimpleNamespace(usage=final_usage)]

        def __iter__(self):
            return iter([_chunk("完成")])

    mocker.patch("core.llm.completion", side_effect=lambda **kwargs: FakeStream())

    with llm_mod.token_usage_lock:
        before = llm_mod.session_token_usage["cumulative_cached"]

    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "测试"}],
        persist=False,
        turn_id="turn-usage-from-wrapper-chunks",
        consume_background_notifications=False,
    ))

    assert events[-1].kind == "turn_finished"
    with llm_mod.token_usage_lock:
        assert llm_mod.session_token_usage["latest_cached"] == 512
        assert llm_mod.session_token_usage["latest_prompt"] == 676
        assert llm_mod.session_token_usage["cumulative_cached"] == before + 512


def test_stream_turn_persists_one_request_shape_diagnostic(mocker):
    import core.llm as llm_mod

    final_usage = SimpleNamespace(
        total_tokens=1100,
        prompt_tokens=1000,
        completion_tokens=100,
        prompt_cache_hit_tokens=990,
        prompt_cache_miss_tokens=10,
        prompt_tokens_details=None,
    )

    class FakeStream:
        chunks = [SimpleNamespace(usage=final_usage)]

        def __iter__(self):
            return iter([_chunk("完成")])

    captured = []
    mocker.patch("core.llm.completion", side_effect=lambda **kwargs: FakeStream())
    mocker.patch(
        "core.llm._persist_cache_request_diagnostic",
        side_effect=lambda details, usage: captured.append((details, usage)),
    )

    events = list(chat_with_model_events(
        "openai/deepseek-v4",
        "fake-key",
        [{"role": "system", "content": "stable"}, {"role": "user", "content": "test"}],
        "https://diag-stream.example/v1",
        persist=False,
        turn_id="turn-request-diagnostic",
        consume_background_notifications=False,
        cache_scope="main",
        cache_session_id="stream-diag-session",
    ))

    assert events[-1].kind == "turn_finished"
    assert len(captured) == 1
    details, usage = captured[0]
    assert details["session_id"] == "stream-diag-session"
    assert details["model"] == "openai/deepseek-v4"
    assert details["api_base"] == "https://diag-stream.example/v1"
    assert details["cache_scope"] == "main"
    assert len(details["messages_sha256"]) == 64
    assert len(details["tools_sha256"]) == 64
    assert usage is final_usage


def test_vision_canonical_user_content_is_persisted_before_provider_request(
    mocker, monkeypatch
):
    import core.app_context as app_context
    import core.llm as llm_mod

    original = [
        {"type": "text", "text": "请分析"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    canonical = "请分析\n\n[图片分析结果]\n这是界面截图"
    cas_calls = []
    provider_messages = []

    class FakeManager:
        def replace_latest_user_message_content(self, session, expected, replacement):
            cas_calls.append((session, expected, replacement))
            return True

        def add_messages(self, session, messages):
            return None

        def add_cache_request_diagnostic(self, diagnostic):
            return None

    class FakeStream:
        chunks = [SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=100,
            prompt_tokens_details=None,
        ))]

        def __iter__(self):
            return iter([_chunk("完成")])

    def describe(messages, *args, **kwargs):
        messages[-1]["content"] = canonical

    def complete(**kwargs):
        provider_messages.append(kwargs["messages"])
        return FakeStream()

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())
    mocker.patch("core.llm._describe_images_with_secondary", side_effect=describe)
    mocker.patch("core.llm.completion", side_effect=complete)

    events = list(chat_with_model_events(
        "openai/text-model",
        "fake-key",
        [{"role": "user", "content": original}],
        "https://vision-cas.example/v1",
        persist=True,
        parent_session="vision-cas-session",
        turn_id="vision-cas-turn",
        consume_background_notifications=False,
        cache_session_id="vision-cas-session",
    ))

    assert events[-1].kind == "turn_finished"
    assert cas_calls == [("vision-cas-session", original, canonical)]
    sent_user = next(m for m in provider_messages[0] if m["role"] == "user")
    assert sent_user["content"] == canonical


def test_nonpersistent_vision_turn_does_not_replace_database_content(
    mocker, monkeypatch
):
    import core.app_context as app_context

    cas_calls = []

    class FakeManager:
        def replace_latest_user_message_content(self, *args):
            cas_calls.append(args)
            return True

        def add_cache_request_diagnostic(self, diagnostic):
            return None

    class FakeStream:
        chunks = [SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=10,
            total_tokens=110,
            prompt_cache_hit_tokens=0,
            prompt_cache_miss_tokens=100,
            prompt_tokens_details=None,
        ))]

        def __iter__(self):
            return iter([_chunk("完成")])

    def describe(messages, *args, **kwargs):
        messages[-1]["content"] = "canonical"

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())
    mocker.patch("core.llm._describe_images_with_secondary", side_effect=describe)
    mocker.patch("core.llm.completion", return_value=FakeStream())

    events = list(chat_with_model_events(
        "openai/text-model",
        "fake-key",
        [{"role": "user", "content": "original"}],
        "https://vision-cas.example/v1",
        persist=False,
        turn_id="vision-no-persist-turn",
        consume_background_notifications=False,
        cache_session_id="vision-no-persist-session",
    ))

    assert events[-1].kind == "turn_finished"
    assert cas_calls == []


@pytest.mark.parametrize("reasoning", [None, 123])
def test_deepseek_thinking_backfills_reasoning_content_string_field(
    monkeypatch, reasoning
):
    import core.llm as llm_mod

    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: None,
    )
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}],
         "reasoning_content": reasoning},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x"},
    ]

    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/deepseek-v4-pro",
        "https://api.deepseek.com",
    )

    assert fixed[0]["reasoning_content"] == ""


def test_deepseek_thinking_tool_call_without_reasoning_replays_empty_key(
    mocker, monkeypatch
):
    import core.llm as llm_mod

    monkeypatch.setattr(llm_mod, "get_tools_schema", lambda: [])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol",
        lambda model, api_base=None: "openai",
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, api_base=None: {"enable_thinking": True},
    )
    captured = []

    def fake_completion(**kwargs):
        captured.append(json.loads(json.dumps(kwargs["messages"])))
        if len(captured) == 1:
            return iter([_chunk(tool_calls=[
                _tool_call(0, "missing-reasoning", "unknown_tool", "{}"),
            ])])
        return iter([_chunk(content="完成")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    messages = [{"role": "user", "content": "执行工具"}]

    events = list(chat_with_model_events(
        "openai/deepseek-v4-pro",
        "fake-key",
        messages,
        "https://api.deepseek.com",
        persist=False,
        turn_id="deepseek-empty-reasoning",
        consume_background_notifications=False,
    ))

    assert events[-1].kind == "turn_finished"
    assistant = next(
        message for message in captured[1]
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant["reasoning_content"] == ""


def test_official_deepseek_openai_route_preserves_complete_reasoning(monkeypatch):
    import core.llm as llm_mod

    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: None,
    )
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}],
         "reasoning_content": "完整分析过程"},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x"},
    ]

    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/deepseek-v4-pro",
        "https://api.deepseek.com",
    )

    assert fixed[0]["reasoning_content"] == "完整分析过程"
    fixed2, _, _ = llm_mod._strip_media_and_merge(
        fixed,
        "openai/deepseek-v4-pro",
        "https://api.deepseek.com",
    )
    assert fixed2 == fixed


def test_non_thinking_deepseek_not_backfilled():
    import core.llm as llm_mod

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x"},
    ]
    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/deepseek-v4-flash",
        "https://aggregator.example/v1",
    )
    assert "reasoning_content" not in fixed[0]


def test_protocol_empty_content_marker_is_not_persisted_or_rendered(
    tmp_path, mocker
):
    from core.memory import MemoryManager

    marker = "[System: Empty message content sanitised to satisfy protocol]"
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    responses = [
        iter([_chunk(
            marker,
            tool_calls=[_tool_call(
                0,
                "read-1",
                "read_file",
                '{"file_path":"x.py"}',
            )],
        )]),
        iter([_chunk("检查完成。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    mocker.patch(
        "core.llm._execute_single_tool_call",
        return_value={
            "role": "tool",
            "tool_call_id": "read-1",
            "name": "read_file",
            "content": "ok",
        },
    )
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=True,
        parent_session="session",
        turn_id="turn-protocol-marker",
        consume_background_notifications=False,
    ))

    tool_request = next(
        message
        for message in messages
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert tool_request["content"] is None
    assert tool_request["tool_calls"][0]["id"] == "read-1"
    persisted_tool_request = next(
        message
        for message in manager.get_history("session")
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert persisted_tool_request["content"] is None
    assert persisted_tool_request["tool_calls"][0]["id"] == "read-1"
    assert marker not in str(manager.get_history("session"))


def test_neohorse_merges_leading_system_messages():
    import core.llm as llm_mod

    messages = [
        {"role": "system", "content": "你是子代理。"},
        {"role": "system", "content": "# Auto Memory\n- 记忆索引"},
        {"role": "user", "content": "读取文件并总结。"},
    ]
    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/neohorse-1-9b",
        "https://tokenrhythm.studio/v1",
    )
    systems = [m for m in fixed if m["role"] == "system"]
    assert len(systems) == 1
    assert fixed[0]["role"] == "system"
    assert "你是子代理。" in fixed[0]["content"]
    assert "# Auto Memory" in fixed[0]["content"]


def test_neohorse_merges_mid_conversation_system():
    import core.llm as llm_mod

    messages = [
        {"role": "system", "content": "主提示词"},
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答"},
        {"role": "system", "content": "# Auto Memory 段落"},
        {"role": "user", "content": "第二问"},
    ]
    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/neohorse-1-9b",
        "https://tokenrhythm.studio/v1",
    )
    assert [m["role"] for m in fixed] == ["system", "user", "assistant", "user"]
    assert "主提示词" in fixed[0]["content"]
    assert "# Auto Memory 段落" in fixed[0]["content"]


def test_other_providers_keep_separate_system_messages():
    import core.llm as llm_mod

    messages = [
        {"role": "system", "content": "你是子代理。"},
        {"role": "system", "content": "# Auto Memory 段落"},
        {"role": "user", "content": "任务"},
    ]
    fixed, _, _ = llm_mod._strip_media_and_merge(
        messages,
        "openai/deepseek-flash",
        "https://api.deepseek.com",
    )
    assert [m["role"] for m in fixed] == ["system", "system", "user"]
