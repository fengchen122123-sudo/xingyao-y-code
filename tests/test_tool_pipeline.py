import json
import threading
from dataclasses import FrozenInstanceError

import pytest

import core.llm as llm
from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry


def _call(name, arguments, *, call_id="call-1", **context):
    raw_arguments = arguments if isinstance(arguments, str) else json.dumps(
        arguments, ensure_ascii=False
    )
    return llm._execute_single_tool_call(
        {
            "id": call_id,
            "function": {"name": name, "arguments": raw_arguments},
        },
        event_callback=context.pop("event_callback", lambda *_args: None),
        **context,
    )


@pytest.fixture(autouse=True)
def _stable_tool_environment(monkeypatch):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: False)
    monkeypatch.setattr(llm, "safe_print", lambda _message: None)


def test_invalid_json_returns_exact_tool_message():
    result = _call("plan_write", '{"items": [broken')

    assert result == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "plan_write",
        "content": (
            "工具参数 JSON 解析失败：工具参数 JSON 解析失败：Expecting value: "
            "line 1 column 12 (char 11)。请重新调用该工具，并确保 arguments 是合法、"
            "完整、未混入其他文本的 JSON。"
        ),
    }


def test_unknown_tool_returns_exact_tool_message(monkeypatch):
    class EmptyMcp:
        def get_schemas(self):
            return []

        def get_schema(self, _name):
            return None

    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: EmptyMcp())

    assert _call("missing", {}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "missing",
        "content": "🚨 未找到名为 missing 的工具！",
    }


def test_read_file_returns_exact_file_bytes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sample.txt"
    target.write_bytes("第一行\nsecond\n".encode("utf-8"))

    assert _call("read_file", {"file_path": str(target)}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "read_file",
        "content": "第一行\nsecond\n",
    }


def test_denied_write_file_returns_exact_message_without_mutating(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "denied.txt"
    monkeypatch.setattr(llm, "check_permission_and_ask", lambda *_args, **_kwargs: False)

    result = _call("write_file", {"file_path": str(target), "content": "no"})

    assert result == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "write_file",
        "content": llm.PERMISSION_DENIED_MESSAGE,
    }
    assert not target.exists()


def test_approved_write_file_records_undo_and_emits_diff(tmp_path, monkeypatch):
    from core.memory import get_undo_state, set_undo_session

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "approved.txt"
    target.write_bytes(b"old\n")
    events = []
    monkeypatch.setattr(llm, "check_permission_and_ask", lambda *_args, **_kwargs: True)
    set_undo_session("tool-pipeline-write")
    try:
        result = _call(
            "write_file",
            {"file_path": str(target), "content": "new\n"},
            event_callback=lambda kind, payload: events.append((kind, payload)),
        )
        undo = get_undo_state("tool-pipeline-write")
    finally:
        set_undo_session(None)

    assert result == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "write_file",
        "content": f"已成功将内容写入文件：{target}，并已向用户展示 Diff。",
    }
    assert target.read_bytes() == b"new\n"
    assert undo == {"file_path": str(target), "old_content": "old\n"}
    assert len(events) == 1
    assert events[0][0] == "diff_ready"
    assert events[0][1]["tool_call_id"] == "call-1"
    assert events[0][1]["file_path"] == str(target)
    assert "-old" in events[0][1]["diff_text"]
    assert "+new" in events[0][1]["diff_text"]
    assert events[0][1]["added"] == 1
    assert events[0][1]["removed"] == 1


def test_large_diff_keeps_full_counts_when_display_is_truncated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "large.txt"
    events = []
    content = "".join(f"line-{index}\n" for index in range(60))
    monkeypatch.setattr(llm, "check_permission_and_ask", lambda *_args, **_kwargs: True)

    _call(
        "write_file",
        {"file_path": str(target), "content": content},
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    diff = next(payload for kind, payload in events if kind == "diff_ready")
    # 前端消费的 diff_text 传完整 diff（只有 2000 行防失控上限），
    # 25 行截断只作用于终端 ANSI 面板（text 字段）
    assert "... (省略" not in diff["diff_text"]
    assert "+line-59" in diff["diff_text"]
    assert "... (省略" in diff["text"]
    assert diff["added"] == 60
    assert diff["removed"] == 0


@pytest.mark.parametrize("allow_outside", [False, True])
def test_credential_write_prompts_through_full_pipeline_even_when_sandbox_open(
    tmp_path, monkeypatch, allow_outside
):
    import core.harness.tool_adapters as tool_adapters
    import core.memory_kairos as memory_kairos
    import core.model_manager as model_manager

    ycode_dir = tmp_path / ".ycode"
    ycode_dir.mkdir()
    target = ycode_dir / "config.json"
    original_expanduser = tool_adapters.os.path.expanduser
    monkeypatch.setattr(memory_kairos, "YCODE_DIR", ycode_dir)
    monkeypatch.setattr(
        tool_adapters.os.path,
        "expanduser",
        lambda path: str(ycode_dir) if str(path) == "~/.ycode" else original_expanduser(path),
    )
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    monkeypatch.setattr(
        model_manager,
        "load_config",
        lambda: {"allow_outside_workspace": allow_outside},
    )
    prompts = []
    monkeypatch.setattr(
        llm,
        "check_permission_and_ask",
        lambda function_name, *args, **kwargs: prompts.append(function_name) or True,
    )

    result = _call("write_file", {"file_path": str(target), "content": "{}\n"})

    assert result["content"].startswith("已成功将内容写入文件")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert prompts.count("sandbox_outside_access") == 1


def test_plugin_subdirectory_config_is_not_treated_as_top_level_credential(
    tmp_path, monkeypatch
):
    import core.harness.tool_adapters as tool_adapters
    import core.memory_kairos as memory_kairos

    ycode_dir = tmp_path / ".ycode"
    target = ycode_dir / "plugins" / "x" / "config.json"
    target.parent.mkdir(parents=True)
    original_expanduser = tool_adapters.os.path.expanduser
    monkeypatch.setattr(memory_kairos, "YCODE_DIR", ycode_dir)
    monkeypatch.setattr(
        tool_adapters.os.path,
        "expanduser",
        lambda path: str(ycode_dir) if str(path) == "~/.ycode" else original_expanduser(path),
    )
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    prompts = []
    monkeypatch.setattr(
        llm,
        "check_permission_and_ask",
        lambda function_name, *args, **kwargs: prompts.append(function_name) or True,
    )

    result = _call("write_file", {"file_path": str(target), "content": "plugin\n"})

    assert result["content"].startswith("已成功将内容写入文件")
    assert "sandbox_outside_access" not in prompts


def test_edit_file_missing_target_returns_exact_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "missing.txt"
    monkeypatch.setattr(llm, "check_permission_and_ask", lambda *_args, **_kwargs: True)

    assert _call(
        "edit_file",
        {
            "file_path": str(target),
            "target_content": "old",
            "replacement_content": "new",
        },
    ) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "edit_file",
        "content": (
            f"🚨 edit_file 目标文件不存在：{target}。"
            "edit_file 只能修改已有文件；创建新文件请改用 write_file。"
        ),
    }


def test_execute_command_uses_registry_and_returns_exact_message(monkeypatch):
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            execute=lambda command: ToolResult(f"command:{command}"),
            requires_permission=True,
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    monkeypatch.setattr(llm, "check_permission_and_ask", lambda *_args, **_kwargs: True)

    assert _call("execute_command", {"command": "echo hi"}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "execute_command",
        "content": "command:echo hi",
    }


def test_plan_mode_denial_is_exact_and_skips_executor(monkeypatch):
    executed = []
    registry = ToolRegistry([
        ToolSpec(
            name="mutate",
            description="mutate",
            input_schema={"type": "object"},
            execute=lambda: executed.append(True) or ToolResult("changed"),
            requires_permission=True,
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")

    assert _call("mutate", {}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "mutate",
        "content": llm.PLAN_MODE_DENIED_MESSAGE,
    }
    assert executed == []


def test_pending_plan_rejects_root_tool_with_exact_message(monkeypatch):
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("pipeline-pending")
    plan.replace([{"id": "impl", "subject": "实现"}])
    try:
        result = _call(
            "write_file",
            {"file_path": "x.txt", "content": "x"},
            parent_session="pipeline-pending",
            parent_turn_id="turn-pending",
        )
    finally:
        bind_default_plan_session(previous_session)

    assert result == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "write_file",
        "content": (
            "计划已创建：工具 write_file 必须通过 run_subagent(plan_item_id=...) 执行，"
            "不能由主 Agent 直接绕过计划调用。 当前可执行的计划项: ['impl'(实现)]。"
            "（若计划项多次失败（attempts>=2）全部停滞后，主 Agent 可直接调用工具修复。）"
        ),
    }


def test_run_subagent_disabled_returns_exact_message():
    assert _call(
        "run_subagent",
        {"task_description": "work"},
        allow_subagents=False,
    ) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "run_subagent",
        "content": "🚨 子代理不能启动其他子代理。",
    }


def test_ask_user_cancellation_preserves_exact_result(monkeypatch):
    import core.ask_user as ask_user_module

    cancelled = '{"status":"cancelled","error":"用户取消提问"}'
    monkeypatch.setattr(ask_user_module, "ask_user", lambda *_args, **_kwargs: cancelled)

    assert _call("ask_user", {"question": "继续吗？", "options": ["继续"]}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "ask_user",
        "content": cancelled,
    }


def test_manage_task_preserves_context_and_exact_result(monkeypatch):
    received = {}

    def manage_task(action, task_id=None, event_callback=None, permission_owner=None, cancel_event=None):
        received.update(
            action=action,
            task_id=task_id,
            event_callback=event_callback,
            permission_owner=permission_owner,
            cancel_event=cancel_event,
        )
        return "managed"

    callback = lambda *_args: None
    cancel_event = threading.Event()
    monkeypatch.setattr(llm, "manage_task", manage_task)

    assert _call(
        "manage_task",
        {"action": "status", "task_id": "t-1"},
        event_callback=callback,
        permission_owner="owner-1",
        cancel_event=cancel_event,
    ) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "manage_task",
        "content": "managed",
    }
    assert received == {
        "action": "status",
        "task_id": "t-1",
        "event_callback": callback,
        "permission_owner": "owner-1",
        "cancel_event": cancel_event,
    }


def test_plugin_tools_keep_strict_registry_result(monkeypatch):
    registry = ToolRegistry([
        ToolSpec(
            name="plugin_tool",
            description="plugin",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
            execute=lambda value: ToolResult(f"plugin:{value}"),
            read_only=True,
            requires_permission=False,
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)

    assert _call("plugin_tool", {"value": "ok", "ignored": "drop"}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "plugin_tool",
        "content": "plugin:ok",
    }


def test_mcp_tool_returns_exact_message(monkeypatch):
    class FakeMcp:
        schema = {
            "type": "function",
            "function": {
                "name": "remote",
                "description": "remote",
                "parameters": {"type": "object"},
            },
        }

        def get_schemas(self):
            return [self.schema]

        def get_schema(self, name):
            if name == "remote":
                return self.schema
            return None

        def call_tool_sync(self, name, arguments):
            return f"mcp:{name}:{arguments['value']}"

    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: FakeMcp())

    assert _call("remote", {"value": "ok"}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "remote",
        "content": "mcp:remote:ok",
    }


def test_pipeline_public_contracts_are_frozen_and_decode_without_coercion():
    from core.harness.tool_pipeline import (
        ToolExecutionContext,
        decode_tool_call,
    )

    registry = ToolRegistry()
    context = ToolExecutionContext(
        event_callback=None,
        parent_session=None,
        parent_turn_id=None,
        cancel_event=None,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=lambda *_args, **_kwargs: True,
        registry=registry,
        mcp_manager=object(),
    )
    decoded = decode_tool_call({
        "id": "call-1",
        "function": {"name": "plugin_tool", "arguments": '{"value":"ok"}'},
    })

    assert decoded.call_id == "call-1"
    assert decoded.name == "plugin_tool"
    assert decoded.arguments == {"value": "ok"}
    assert decoded.raw_arguments == '{"value":"ok"}'
    with pytest.raises(FrozenInstanceError):
        context.parent_session = "other"


def test_pipeline_propagates_invariant_exceptions():
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline

    class BrokenRegistry(ToolRegistry):
        def get(self, name):
            raise RuntimeError("registry invariant")

    context = ToolExecutionContext(
        event_callback=None,
        parent_session=None,
        parent_turn_id=None,
        cancel_event=None,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=lambda *_args, **_kwargs: True,
        registry=BrokenRegistry(),
        mcp_manager=object(),
    )

    with pytest.raises(RuntimeError, match="registry invariant"):
        ToolPipeline().execute_one(
            {"id": "call-1", "function": {"name": "broken", "arguments": "{}"}},
            context,
        )


def test_direct_pipeline_enforces_sandbox_boundary(tmp_path, monkeypatch):
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.chdir(workspace)
    registry = ToolRegistry([
        ToolSpec(
            name="read_file",
            description="read",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
            execute=lambda file_path: ToolResult("secret"),
            read_only=True,
            requires_permission=False,
        )
    ])
    context = ToolExecutionContext(
        event_callback=lambda *_args: None,
        parent_session=None,
        parent_turn_id=None,
        cancel_event=None,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=lambda *_args, **_kwargs: False,
        registry=registry,
        mcp_manager=object(),
    )

    result = ToolPipeline().execute_one(
        {
            "id": "call-1",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"file_path": str(outside)}),
            },
        },
        context,
    )

    assert result["content"] == (
        "🚨 用户拒绝了访问沙盒外路径的请求。沙盒外路径访问需要用户确认（可选择本会话内放行，或用 /sandbox on 长期开放）。"
    )


def test_strict_tool_executor_exception_gets_explicit_tool_error(monkeypatch):
    registry = ToolRegistry([
        ToolSpec(
            name="plugin_failure",
            description="failure",
            input_schema={"type": "object"},
            execute=lambda: (_ for _ in ()).throw(ValueError("boom")),
            read_only=True,
            requires_permission=False,
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)

    assert _call("plugin_failure", {}) == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": "plugin_failure",
        "content": "🚨 调用工具 plugin_failure 时发生系统内部错误：boom",
    }


def _batch_call(call_id, name, arguments="{}"):
    raw_arguments = arguments if isinstance(arguments, str) else json.dumps(
        arguments, ensure_ascii=False
    )
    return {
        "id": call_id,
        "function": {"name": name, "arguments": raw_arguments},
    }


def _batch_tool_message(call_id, name="read_file", content="ok"):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


def _batch_registry():
    return ToolRegistry([
        ToolSpec(
            name="read_file",
            description="read",
            input_schema={"type": "object"},
            execute=lambda: ToolResult("read"),
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
        ),
        ToolSpec(
            name="write_file",
            description="write",
            input_schema={"type": "object"},
            execute=lambda: ToolResult("write"),
            requires_permission=True,
            parallel_safe=False,
        ),
    ])


def _batch_context(*, cancel_event=None, registry=None):
    from core.harness.tool_pipeline import ToolExecutionContext

    return ToolExecutionContext(
        event_callback=None,
        parent_session=None,
        parent_turn_id=None,
        cancel_event=cancel_event,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=lambda *_args, **_kwargs: True,
        registry=registry or _batch_registry(),
        mcp_manager=object(),
    )


def _parallel_executor(execute):
    from core.harness.tool_pipeline import ToolBatchExecutor, ToolPipeline

    return ToolBatchExecutor(
        ToolPipeline(),
        execute_decoded=lambda decoded, context: execute(
            {
                "id": decoded.call_id,
                "function": {
                    "name": decoded.name,
                    "arguments": decoded.raw_arguments,
                },
            },
            context,
        ),
        event_payload=lambda decoded: {"id": decoded.call_id},
        finished_event_payload=lambda decoded, message, duration_ms: {
            "id": decoded.call_id,
            "ok": not message["content"].startswith("🚨"),
            "duration_ms": duration_ms,
        },
    )


def test_parallel_completion_order_never_changes_model_result_order():
    first_release = threading.Event()
    second_finished = threading.Event()

    def execute(call, _context):
        if call["id"] == "first":
            assert second_finished.wait(timeout=0.1)
            first_release.set()
        else:
            second_finished.set()
        return _batch_tool_message(call["id"])

    records = _parallel_executor(execute).execute(
        [_batch_call("first", "read_file"), _batch_call("second", "read_file")],
        _batch_context(),
        None,
    )

    assert first_release.is_set()
    assert [record.call_ordinal for record in records] == [0, 1]
    assert [record.message["tool_call_id"] for record in records] == [
        "first",
        "second",
    ]


def test_only_adjacent_parallel_safe_tools_share_a_batch():
    from core.harness.tool_pipeline import group_tool_batches

    registry = _batch_registry()
    calls = [
        _batch_call("r1", "read_file"),
        _batch_call("w1", "write_file"),
        _batch_call("r2", "read_file"),
    ]
    batches = group_tool_batches(calls, registry)

    assert [[call["id"] for call in batch.calls] for batch in batches] == [
        ["r1"],
        ["w1"],
        ["r2"],
    ]
    adjacent = group_tool_batches(
        [_batch_call("r1", "read_file"), _batch_call("r2", "read_file")],
        registry,
    )
    assert [[call["id"] for call in batch.calls] for batch in adjacent] == [
        ["r1", "r2"],
    ]


def test_repeat_guard_blocks_third_successful_identical_signature():
    from core.harness.tool_pipeline import RepeatCallGuard

    guard = RepeatCallGuard()
    call = _batch_call("one", "read_file", '{"file_path":"a.py"}')
    arguments = call["function"]["arguments"]

    assert guard.should_execute("read_file", arguments)
    guard.record("read_file", arguments, ok=True)
    assert guard.should_execute("read_file", arguments)
    guard.record("read_file", arguments, ok=True)
    assert guard.should_execute("read_file", arguments) is False


def test_cancel_stops_polling_without_waiting_for_daemon_tool():
    cancel_event = threading.Event()
    blocker = threading.Event()

    def blocked_execute(call, _context):
        cancel_event.set()
        blocker.wait()
        return _batch_tool_message(call["id"])

    try:
        with pytest.raises(KeyboardInterrupt):
            _parallel_executor(blocked_execute).execute(
                [
                    _batch_call("blocked", "read_file"),
                    _batch_call("also-blocked", "read_file"),
                ],
                _batch_context(cancel_event=cancel_event),
                None,
            )
    finally:
        blocker.set()


def test_parallel_tool_threads_inherit_workdir_and_thread_session(tmp_path):
    """回归：并行工具线程必须继承提交线程的 workdir 与会话绑定。

    裸 daemon 线程丢失线程本地 workdir 后，get_workdir() 兜底到进程 cwd，
    沙盒守卫会把项目内路径误判为沙盒外并弹权限询问（GUI 打包版进程
    cwd 是安装目录时必现）；线程会话绑定丢失则"本会话内放行沙盒外
    访问"等每会话状态在并行线程里全部失效。
    """
    from core.workspace_context import get_workdir, workspace_scope

    import core.app_context as app_context_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    expected_workdir = str(workspace.resolve())
    seen = []

    def execute(call, _context):
        seen.append((str(get_workdir()), app_context_module.get_thread_session()))
        return _batch_tool_message(call["id"])

    with workspace_scope(str(workspace)):
        app_context_module.set_thread_session("session-parallel-check")
        try:
            records = _parallel_executor(execute).execute(
                [
                    _batch_call("r1", "read_file"),
                    _batch_call("r2", "read_file"),
                ],
                _batch_context(),
                None,
            )
        finally:
            app_context_module.set_thread_session(None)

    assert [record.message["tool_call_id"] for record in records] == ["r1", "r2"]
    assert len(seen) == 2
    assert all(
        workdir == expected_workdir and session == "session-parallel-check"
        for workdir, session in seen
    )


def test_batch_events_cover_each_batch_before_execution_and_finish_in_model_order():
    execution_order = []
    events = []

    def execute(call, _context):
        execution_order.append(call["id"])
        return _batch_tool_message(call["id"], name=call["function"]["name"])

    records = _parallel_executor(execute).execute(
        [
            _batch_call("r1", "read_file"),
            _batch_call("r2", "read_file"),
            _batch_call("w1", "write_file"),
        ],
        _batch_context(),
        lambda kind, payload: events.append((kind, payload["id"])),
    )

    assert events == [
        ("tool_requested", "r1"),
        ("tool_requested", "r2"),
        ("tool_started", "r1"),
        ("tool_started", "r2"),
        ("tool_finished", "r1"),
        ("tool_finished", "r2"),
        ("tool_requested", "w1"),
        ("tool_started", "w1"),
        ("tool_finished", "w1"),
    ]
    assert execution_order == ["r1", "r2", "w1"]
    assert [record.call_ordinal for record in records] == [0, 1, 2]


def test_batch_executor_decodes_each_call_only_once(monkeypatch):
    import core.harness.tool_pipeline as tool_pipeline

    decode_count = 0
    original_decode = tool_pipeline.decode_tool_call

    def counting_decode(tool_call):
        nonlocal decode_count
        decode_count += 1
        return original_decode(tool_call)

    monkeypatch.setattr(tool_pipeline, "decode_tool_call", counting_decode)
    events = []
    executor = tool_pipeline.ToolBatchExecutor(
        tool_pipeline.ToolPipeline(),
        execute_decoded=lambda decoded, _context: _batch_tool_message(
            decoded.call_id,
            name=decoded.name,
        ),
    )

    records = executor.execute(
        [_batch_call("first", "read_file", '{"file_path":"a.py"}')],
        _batch_context(),
        lambda kind, payload: events.append((kind, payload)),
    )

    assert decode_count == 1
    assert records[0].message["tool_call_id"] == "first"
    assert [kind for kind, _payload in events] == [
        "tool_requested",
        "tool_started",
        "tool_finished",
    ]


def test_repeat_blocked_record_is_model_visible_and_not_executed():
    from core.harness.tool_pipeline import RepeatCallGuard

    executions = []
    guard = RepeatCallGuard()
    arguments = '{"file_path":"a.py"}'
    guard.record("read_file", arguments, ok=True)
    guard.record("read_file", arguments, ok=True)
    executor = _parallel_executor(
        lambda call, _context: executions.append(call["id"])
        or _batch_tool_message(call["id"])
    )
    executor.repeat_guard = guard

    records = executor.execute(
        [_batch_call("third", "read_file", arguments)],
        _batch_context(),
        None,
    )

    assert executions == []
    assert records[0].message == {
        "role": "tool",
        "tool_call_id": "third",
        "name": "read_file",
        "content": (
            "🛑 检测到相同的工具调用已成功执行过 2 次，"
            "结果未发生变化。请改用其他方式推进任务。"
        ),
    }


def test_tool_result_journal_draft_contains_only_hashes_and_ordered_metadata():
    from core.harness.models import HarnessEventKind
    from core.harness.tool_pipeline import (
        ToolExecutionRecord,
        build_tool_result_event_draft,
    )

    tool_call = _batch_call(
        "call-1",
        "read_file",
        '{"file_path":"a.py"}',
    )
    record = ToolExecutionRecord(
        call_ordinal=2,
        message=_batch_tool_message("call-1", content="ok"),
        duration_ms=17,
    )

    draft = build_tool_result_event_draft(
        "step-1",
        tool_call,
        record,
        message_index=3,
        ok=True,
    )

    assert draft.kind is HarnessEventKind.TOOL_RESULT_COMMITTED
    assert draft.step_id == "step-1"
    assert draft.message_index == 3
    assert draft.tool_name == "read_file"
    assert draft.call_ordinal == 2
    assert draft.arguments_sha256 == (
        "39fab66fd02dd0c0d6a9ba0eed3ab8a2eb7a1a0fdd5d45a3d901d4ca060f937a"
    )
    assert draft.result_sha256 == (
        "2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df"
    )
    assert draft.duration_ms == 17
    assert draft.result_status == "ok"
    assert not hasattr(draft, "arguments")
    assert not hasattr(draft, "result")
