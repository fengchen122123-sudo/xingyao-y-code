import json
import threading
from dataclasses import FrozenInstanceError
from pathlib import Path

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
    from core import permission_grants, permission_store

    class EmptyMcp:
        def get_schemas(self):
            return []

        def get_schema(self, _name):
            return None

    permission_grants._clear_all_for_tests()
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: False)
    monkeypatch.setattr(llm, "safe_print", lambda _message: None)
    monkeypatch.setattr(llm, "_get_mcp_manager", lambda: EmptyMcp())
    monkeypatch.setattr(
        permission_store,
        "project_rules",
        lambda *_args, **_kwargs: {
            "commands": set(),
            "write_files": set(),
            "edit_files": set(),
        },
    )
    yield
    permission_grants._clear_all_for_tests()


def _auto_answer_pipeline_permission(monkeypatch, choice, asked, requests):
    from core.permission_broker import PermissionBroker

    broker = PermissionBroker()

    def activate(request):
        asked.append(request.function_name)
        requests.append(request)
        broker.resolve(request.request_id, choice)

    broker.set_prompt_handlers(activate, lambda: None)
    monkeypatch.setattr(llm, "permission_broker", broker)


def _authorization(allowed, reason="test", *, sandbox_bypass=False):
    return llm.AuthorizationResult(
        allowed,
        reason,
        sandbox_bypass=sandbox_bypass,
    )


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


def test_accept_edits_runs_query_and_normal_mutation_but_asks_sensitive(monkeypatch):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    executed = []
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
            execute=lambda command: executed.append(command) or ToolResult("ok"),
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 1, asked, requests)

    assert _call("execute_command", {"command": "git status"})["content"] == "ok"
    assert _call("execute_command", {"command": "npm run build"})["content"] == "ok"
    result = _call("execute_command", {"command": "git push origin dev"})

    assert result["content"] == llm.PERMISSION_DENIED_MESSAGE
    assert asked == ["execute_command"]
    assert executed == ["git status", "npm run build"]


def test_plan_mode_allows_read_only_command_and_denies_write(monkeypatch):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
            },
            execute=lambda command: ToolResult("query-ok"),
        ),
        ToolSpec(
            name="write_file",
            description="write",
            input_schema={"type": "object"},
            execute=lambda **_kwargs: ToolResult("must-not-run"),
            mutates_files=True,
        ),
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)

    query = _call("execute_command", {"command": "git status"})
    denied = _call("write_file", {"file_path": "plan-denied.txt", "content": "x"})

    assert query["content"] == "query-ok"
    assert denied["content"] == llm.PLAN_MODE_DENIED_MESSAGE


def test_plan_mode_denies_query_command_with_explicit_shell_override(monkeypatch):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={"type": "object"},
            execute=lambda **_kwargs: ToolResult("must-not-run"),
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)

    result = _call(
        "execute_command",
        {"command": "git status", "shell_id": "powershell"},
    )

    assert result["content"] == llm.PLAN_MODE_DENIED_MESSAGE


def test_catastrophic_command_never_enqueues_permission(monkeypatch):
    registry = ToolRegistry([
        ToolSpec(
            name="execute_command",
            description="command",
            input_schema={"type": "object"},
            execute=lambda **_kwargs: ToolResult("must-not-run"),
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    enqueued = []
    monkeypatch.setattr(
        llm.permission_broker,
        "enqueue",
        lambda *args, **kwargs: enqueued.append((args, kwargs)),
    )

    result = _call("execute_command", {"command": "format C:"})

    assert result["content"] == "🚨 危险命令已被安全策略拒绝。请改用范围明确、可恢复的操作。"
    assert enqueued == []


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
    monkeypatch.setattr(
        llm,
        "authorize_tool_call",
        lambda *_args, **_kwargs: _authorization(False, "user_denied"),
    )

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
    monkeypatch.setattr(
        llm,
        "authorize_tool_call",
        lambda *_args, **_kwargs: _authorization(True),
    )
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
    assert undo == {
        "file_path": str(target),
        "old_content": "old\n",
        "existed_before": True,
        "old_bytes": b"old\n",
    }
    assert len(events) == 1
    assert events[0][0] == "diff_ready"
    assert events[0][1]["tool_call_id"] == "call-1"
    assert events[0][1]["file_path"] == str(target)
    assert "-old" in events[0][1]["diff_text"]
    assert "+new" in events[0][1]["diff_text"]
    assert events[0][1]["added"] == 1
    assert events[0][1]["removed"] == 1


def test_injected_write_file_adapter_records_new_file_as_not_preexisting(
    tmp_path, monkeypatch
):
    from core.harness.tool_adapters import ToolAdapterSet
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline
    from core.memory import get_undo_state, set_undo_session

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "created-by-override.txt"

    def fake_write(file_path, content):
        Path(file_path).write_text(content, encoding="utf-8")
        return "ok"

    registry = ToolRegistry([ToolSpec(
        name="write_file",
        description="write",
        input_schema={"type": "object"},
        execute=lambda **_kwargs: ToolResult("must-not-run"),
        mutates_files=True,
    )])
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
    pipeline = ToolPipeline(
        adapters=ToolAdapterSet(
            write_file=fake_write,
            show_file_diff=lambda *_args, **_kwargs: None,
        ),
        plan_turn_checker=lambda _turn_id: False,
        pending_plan_checker=lambda _session_id: False,
    )
    set_undo_session("adapter-override-new-file")
    try:
        pipeline.execute_one({
            "id": "call-override",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "file_path": str(target),
                    "content": "new",
                }),
            },
        }, context)
        undo = get_undo_state("adapter-override-new-file")
    finally:
        set_undo_session(None)

    assert target.exists()
    assert undo["existed_before"] is False


def test_injected_write_file_adapter_resolves_relative_path_from_workspace(
    tmp_path, monkeypatch
):
    from core.harness.tool_adapters import ToolAdapterSet
    from core.harness.tool_pipeline import ToolExecutionContext, ToolPipeline
    from core.memory import get_undo_state, set_undo_session
    from core.workspace_context import workspace_scope

    process_cwd = tmp_path / "process-cwd"
    workspace = tmp_path / "workspace"
    process_cwd.mkdir()
    workspace.mkdir()
    monkeypatch.chdir(process_cwd)
    target = workspace / "existing-empty.txt"
    target.write_bytes(b"")

    def fake_write(file_path, content):
        Path(file_path).write_text(content, encoding="utf-8")
        return "ok"

    registry = ToolRegistry([ToolSpec(
        name="write_file",
        description="write",
        input_schema={"type": "object"},
        execute=lambda **_kwargs: ToolResult("must-not-run"),
        mutates_files=True,
    )])
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
    pipeline = ToolPipeline(
        adapters=ToolAdapterSet(
            write_file=fake_write,
            show_file_diff=lambda *_args, **_kwargs: None,
        ),
        plan_turn_checker=lambda _turn_id: False,
        pending_plan_checker=lambda _session_id: False,
    )
    set_undo_session("adapter-relative-workspace")
    try:
        with workspace_scope(str(workspace)):
            pipeline.execute_one({
                "id": "call-relative",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({
                        "file_path": "existing-empty.txt",
                        "content": "updated",
                    }),
                },
            }, context)
        undo = get_undo_state("adapter-relative-workspace")
    finally:
        set_undo_session(None)

    assert target.read_text(encoding="utf-8") == "updated"
    assert not (process_cwd / "existing-empty.txt").exists()
    assert undo["file_path"] == str(target)
    assert undo["existed_before"] is True


def test_large_diff_keeps_full_counts_when_display_is_truncated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "large.txt"
    events = []
    content = "".join(f"line-{index}\n" for index in range(60))
    monkeypatch.setattr(
        llm,
        "authorize_tool_call",
        lambda *_args, **_kwargs: _authorization(True),
    )

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
        "authorize_tool_call",
        lambda function_name, *args, **kwargs: (
            prompts.append("sandbox_outside_access")
            or _authorization(True, sandbox_bypass=True)
        ),
    )

    result = _call("write_file", {"file_path": str(target), "content": "{}\n"})

    assert result["content"].startswith("已成功将内容写入文件")
    assert target.read_text(encoding="utf-8") == "{}\n"
    assert prompts.count("sandbox_outside_access") == 1


def test_credential_write_ignores_task_sandbox_bypass_and_requires_confirmation(
    tmp_path, monkeypatch
):
    """任务级沙箱放行不能覆盖 ~/.ycode 顶层凭据写入的逐次确认。"""
    import core.harness.tool_adapters as tool_adapters
    import core.memory_kairos as memory_kairos
    import core.model_manager as model_manager
    import core.tools as tools_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ycode_dir = tmp_path / ".ycode"
    ycode_dir.mkdir()
    target = ycode_dir / "config.json"
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(memory_kairos, "YCODE_DIR", ycode_dir)
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    monkeypatch.setattr(
        tool_adapters.os.path,
        "expanduser",
        lambda path: str(ycode_dir) if str(path) == "~/.ycode" else str(path),
    )
    monkeypatch.setattr(
        model_manager,
        "load_config",
        lambda: {"allow_outside_workspace": True},
    )
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "full")
    tools_module.set_session_sandbox_bypass(True)
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 1, asked, requests)  # deny

    try:
        result = _call(
            "write_file",
            {"file_path": str(target), "content": "must-not-write\n"},
        )
    finally:
        tools_module.set_session_sandbox_bypass(False)

    assert result["content"] == llm.PERMISSION_DENIED_MESSAGE
    assert asked == ["sandbox_outside_access"]
    assert requests[0].arguments == {
        "path": str(target),
        "operation": "write_file",
    }
    assert not target.exists()


def test_plan_mode_denies_external_write_before_permission_prompt(tmp_path, monkeypatch):
    """沙箱提示名不能把外部写入降级为 plan 模式允许的查询。"""
    import core.model_manager as model_manager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "outside.txt"
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    monkeypatch.setattr(
        model_manager,
        "load_config",
        lambda: {"allow_outside_workspace": False},
    )
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 0, asked, requests)

    result = _call(
        "write_file",
        {"file_path": str(target), "content": "must-not-write\n"},
    )

    assert result["content"] == llm.PLAN_MODE_DENIED_MESSAGE
    assert asked == []
    assert not target.exists()


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
        "authorize_tool_call",
        lambda function_name, *args, **kwargs: (
            prompts.append(function_name) or _authorization(True)
        ),
    )

    result = _call("write_file", {"file_path": str(target), "content": "plugin\n"})

    assert result["content"].startswith("已成功将内容写入文件")
    assert "sandbox_outside_access" not in prompts


def test_edit_file_missing_target_returns_exact_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "missing.txt"
    monkeypatch.setattr(
        llm,
        "authorize_tool_call",
        lambda *_args, **_kwargs: _authorization(True),
    )

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
    monkeypatch.setattr(
        llm,
        "authorize_tool_call",
        lambda *_args, **_kwargs: _authorization(True),
    )

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
            "（若计划项子代理超时，主 Agent 接管后即可直接调用工具修复。）"
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


def test_run_subagent_hidden_timeout_is_not_forwarded_to_adapter(monkeypatch):
    """模型伪造 schema 外参数时，时间预算仍必须由系统选择。"""
    received = {}

    def run_subagent(**kwargs):
        received.update(kwargs)
        return "done"

    monkeypatch.setattr(llm, "run_subagent", run_subagent)

    result = _call(
        "run_subagent",
        {
            "task_description": "检查模块",
            "subagent_type": "explore",
            "timeout_seconds": 3600,
        },
    )

    assert result["content"] == "done"
    assert received["timeout_seconds"] is None


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

    assert result["content"] == llm.PERMISSION_DENIED_MESSAGE


def test_project_whitelist_cannot_bypass_sandbox_boundary(tmp_path, monkeypatch):
    from core import permission_store

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    monkeypatch.chdir(workspace)
    canonical = llm._canonical_permission_path(str(outside))
    monkeypatch.setattr(
        permission_store,
        "project_rules",
        lambda *_args, **_kwargs: {
            "commands": set(),
            "write_files": {canonical},
            "edit_files": set(),
        },
    )
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"allow_outside_workspace": False},
    )
    monkeypatch.setattr(llm, "is_outside_workspace", lambda _path: True)
    registry = ToolRegistry([
        ToolSpec(
            name="write_file",
            description="write",
            input_schema={"type": "object"},
            execute=lambda **_kwargs: ToolResult("must-not-run"),
            mutates_files=True,
        )
    ])
    monkeypatch.setattr(llm, "tool_registry", registry)
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 1, asked, requests)

    result = _call(
        "write_file",
        {"file_path": str(outside), "content": "blocked"},
    )

    assert result["content"] == llm.PERMISSION_DENIED_MESSAGE
    assert not outside.exists()
    assert asked == ["sandbox_outside_access"]
    assert requests[0].option_keys == (
        "allow_once",
        "deny",
        "allow_task_sandbox",
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

    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

    assert executions == []

    assert executions == []
    assert records[0].message == {
        "role": "tool",
        "tool_call_id": "third",
        "name": "read_file",
        "content": REPEAT_CALL_BLOCKED_TEXT,
    }


def test_repeat_guard_exempt_allows_readonly_manage_task_actions():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for action in ("status", "list", "wait"):
        assert repeat_guard_exempt("manage_task", {"action": action}) is True


def test_repeat_guard_exempt_blocks_side_effect_and_unknown_actions():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for action in ("kill", "stop", "terminate", "cancel", "bogus", ""):
        assert repeat_guard_exempt("manage_task", {"action": action}) is False


def test_repeat_guard_exempt_keeps_plan_tools_exempt():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for name in (
        "plan_write", "plan_update", "plan_read", "plan_retry",
        "plan_resume_takeover",
    ):
        assert repeat_guard_exempt(name, {}) is True
    assert repeat_guard_exempt("read_file", {}) is False


def test_manage_task_status_called_three_times_all_execute():
    # manage_task(status) 连续三次均真实执行，不被 RepeatCallGuard 拦截
    executions = []
    executor = _parallel_executor(
        lambda call, _context: executions.append(call["id"])
        or _batch_tool_message(call["id"], content="status ok")
    )
    for i in range(3):
        records = executor.execute(
            [_batch_call(f"s{i}", "manage_task", '{"action":"status","task_id":"t1"}')],
            _batch_context(),
            None,
        )
    assert len(executions) == 3
    assert all("🚨" not in str(r.message.get("content", "")) for r in records)


def test_manage_task_kill_third_call_blocked_with_error_semantics():
    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

    executions = []
    executor = _parallel_executor(
        lambda call, _context: executions.append(call["id"])
        or _batch_tool_message(call["id"], content="killed")
    )
    for i in range(3):
        records = executor.execute(
            [_batch_call(f"k{i}", "manage_task", '{"action":"kill","task_id":"t1"}')],
            _batch_context(),
            None,
        )
    assert len(executions) == 2
    assert records[0].message["content"] == REPEAT_CALL_BLOCKED_TEXT
    assert REPEAT_CALL_BLOCKED_TEXT.startswith("🚨")


def test_repeat_blocked_text_exact_and_no_result_comparison_claim():
    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

    assert REPEAT_CALL_BLOCKED_TEXT == (
        "🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。"
        "请检查已有结果或改变执行方案。"
        "若该工具是 computer_action，窗口可能已移动，"
        "请先 list_elements 或 observe 重建状态再操作。"
    )
    assert "结果未发生变化" not in REPEAT_CALL_BLOCKED_TEXT


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


def _fenced_owner_manager(monkeypatch):
    """构造一个已超时终态的真实子代理任务，并接管进程级 manager。"""
    import core.tools as tools_module
    from core.tools import SubagentManager

    release = threading.Event()
    manager = SubagentManager(runner=lambda record: release.wait(1) and "unused")
    background = manager.run(
        "旧任务", "s1", "t1", timeout_seconds=30, run_in_background=True,
    )
    task_id = json.loads(background)["task_id"]
    assert manager._terminate(task_id, "timed_out", "foreground timeout") is True
    monkeypatch.setattr(tools_module, "subagent_manager", manager)
    release.set()
    return task_id


def _fence_context(registry, permission_owner):
    from core.harness.tool_pipeline import ToolExecutionContext

    return ToolExecutionContext(
        event_callback=None,
        parent_session="s1",
        parent_turn_id="t1",
        cancel_event=None,
        allow_subagents=True,
        permission_owner=permission_owner,
        permission_source="子代理",
        permission_checker=lambda *_args, **_kwargs: True,
        registry=registry,
        mcp_manager=object(),
    )


def test_stale_subagent_owner_tools_are_fenced_before_permission(
    tmp_path, monkeypatch
):
    """旧子 Agent 的延迟工具调用必须在权限询问与 adapter 之前返回取消错误。"""
    from core.harness.tool_pipeline import ToolPipeline

    executed = []

    def _record(value):
        executed.append(value)
        return ToolResult("should-not-happen")

    registry = ToolRegistry([
        ToolSpec(
            name="write_file",
            description="write",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            execute=lambda file_path, content: _record(file_path),
            mutates_files=True,
        ),
        ToolSpec(
            name="read_file",
            description="read",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
            execute=lambda file_path: _record(file_path),
            read_only=True,
            requires_permission=False,
        ),
    ])
    task_id = _fenced_owner_manager(monkeypatch)
    context = _fence_context(registry, task_id)
    pipeline = ToolPipeline()

    write_message = pipeline.execute_one(
        {
            "id": "call-w",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "file_path": str(tmp_path / "a.py"),
                    "content": "x",
                }),
            },
        },
        context,
    )
    read_message = pipeline.execute_one(
        {
            "id": "call-r",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"file_path": str(tmp_path / "a.py")}),
            },
        },
        context,
    )

    assert "已取消" in write_message["content"]
    assert "已取消" in read_message["content"]
    assert executed == []


def test_active_subagent_owner_tools_are_not_fenced(tmp_path, monkeypatch):
    """运行中的子代理租约有效，工具调用照常执行，不受 fencing 影响。"""
    import core.tools as tools_module
    from core.harness.tool_pipeline import ToolPipeline

    release = threading.Event()
    manager = tools_module.SubagentManager(
        runner=lambda record: release.wait(1) and "unused",
    )
    background = manager.run(
        "运行中任务", "s1", "t1", timeout_seconds=30, run_in_background=True,
    )
    task_id = json.loads(background)["task_id"]
    monkeypatch.setattr(tools_module, "subagent_manager", manager)

    executed = []
    registry = ToolRegistry([
        ToolSpec(
            name="read_file",
            description="read",
            input_schema={
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
            },
            execute=lambda file_path: (
                executed.append(file_path),
                ToolResult("content"),
            )[1],
            read_only=True,
            requires_permission=False,
        ),
    ])
    context = _fence_context(registry, task_id)
    message = ToolPipeline().execute_one(
        {
            "id": "call-r",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"file_path": str(tmp_path / "a.py")}),
            },
        },
        context,
    )

    assert message["content"] == "content"
    assert executed == [str(tmp_path / "a.py")]

    assert manager._terminate(task_id, "cancelled", "test cleanup") is True
    release.set()


def _install_active_takeover(monkeypatch, *, item, turn, files=None):
    """用生产调度 API 构造 active takeover 现场并路由默认单例。

    只搭状态，不复制被测实现：接管字段全部经 start_delegated_item/
    handoff_to_main 写入。
    """
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    raw = {"id": item, "subject": item}
    if files is not None:
        raw["files"] = list(files)
    plan.replace([raw])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        item, f"subagent-{item}", "subagent:implement", None
    )
    assert scheduler.handoff_to_main(item, f"subagent-{item}", turn, "timeout")
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    llm._mark_plan_turn(turn)
    return scheduler


def test_main_takeover_allows_direct_mutation_for_current_turn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    _install_active_takeover(monkeypatch, item="a", turn="turn-1")

    result = _call(
        "write_file",
        {"file_path": str(tmp_path / "a.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "必须通过 run_subagent" not in result["content"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x"


def test_takeover_does_not_unlock_other_turn_or_whole_plan(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    _install_active_takeover(monkeypatch, item="a", turn="turn-1")
    # 新回合在计划未完成时同样会被标记为计划回合（生产路径 llm 调 _mark_plan_turn）
    llm._mark_plan_turn("turn-2")

    blocked = _call(
        "write_file",
        {"file_path": str(tmp_path / "b.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-2",
    )

    assert "必须通过 run_subagent" in blocked["content"]
    assert not (tmp_path / "b.py").exists()


def test_takeover_scoped_unlock_still_asks_in_confirm_mode(monkeypatch, tmp_path):
    """scoped 放行只解除计划门禁，confirm 模式权限询问必须照常发生。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    _install_active_takeover(monkeypatch, item="a", turn="turn-1")
    asked, requests = [], []
    _auto_answer_pipeline_permission(monkeypatch, 0, asked, requests)

    result = _call(
        "write_file",
        {"file_path": str(tmp_path / "c.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "必须通过 run_subagent" not in result["content"]
    assert asked == ["write_file"]
    assert (tmp_path / "c.py").read_text(encoding="utf-8") == "x"


def test_takeover_files_scope_restricts_write_targets(monkeypatch, tmp_path):
    """计划项声明 files 时，接管期间的写文件只允许命中这些路径。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    _install_active_takeover(
        monkeypatch, item="a", turn="turn-1", files=["allowed.py"]
    )

    allowed = _call(
        "write_file",
        {"file_path": str(tmp_path / "allowed.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )
    denied = _call(
        "write_file",
        {"file_path": str(tmp_path / "other.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "必须通过 run_subagent" not in allowed["content"]
    assert (tmp_path / "allowed.py").read_text(encoding="utf-8") == "x"
    assert "计划接管约束" in denied["content"]
    assert "allowed.py" in denied["content"]
    assert not (tmp_path / "other.py").exists()


def test_takeover_files_scope_canonicalizes_real_paths(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    _install_active_takeover(
        monkeypatch, item="a", turn="turn-1", files=["allowed.py"]
    )
    realpath_calls = []
    original_realpath = __import__("os").path.realpath

    def recording_realpath(value, *args, **kwargs):
        realpath_calls.append(str(value))
        return original_realpath(value, *args, **kwargs)

    monkeypatch.setattr("core.harness.tool_pipeline.os.path.realpath", recording_realpath)

    denied = _call(
        "write_file",
        {"file_path": str(tmp_path / "other.py"), "content": "x"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "计划接管约束" in denied["content"]
    assert any(path.endswith("allowed.py") for path in realpath_calls)
    assert any(path.endswith("other.py") for path in realpath_calls)


def test_non_task_owner_with_subagent_prefix_is_not_fenced(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "visible.txt"
    target.write_text("visible", encoding="utf-8")

    result = _call(
        "read_file",
        {"file_path": str(target)},
        permission_owner="subagent-demo:12345678",
        parent_turn_id="subagent-demo:12345678",
    )

    assert result["content"] == "visible"


def test_fencing_lookup_failure_is_fail_closed_before_permission_and_adapter(
    monkeypatch, tmp_path
):
    import core.tools as tools_module
    from core.harness.tool_pipeline import STALE_EXECUTION_LEASE_TEXT

    class BrokenLeaseManager:
        def is_execution_lease_active(self, _task_id):
            raise RuntimeError("lease registry unavailable")

    permission_calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools_module, "subagent_manager", BrokenLeaseManager())
    monkeypatch.setattr(
        llm,
        "check_permission_and_ask",
        lambda *args, **kwargs: permission_calls.append((args, kwargs)) or True,
    )
    target = tmp_path / "must-not-read.txt"
    target.write_text("secret", encoding="utf-8")

    result = _call(
        "read_file",
        {"file_path": str(target)},
        permission_owner="subagent-1234abcd",
        parent_turn_id="child-turn",
    )

    assert result["content"] == STALE_EXECUTION_LEASE_TEXT
    assert permission_calls == []


def test_plan_finish_completes_active_takeover_and_promotes_next(
    monkeypatch, tmp_path
):
    """active 接管项 plan_finish 成功后标记 completed 并 FIFO 提升下一排队项。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")

    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "a", "subject": "first"}, {"id": "b", "subject": "second"}])
    scheduler = TaskScheduler(plan, max_workers=2)
    assert scheduler.start_delegated_item("a", "subagent-aa1", "subagent:implement", None)
    assert scheduler.start_delegated_item("b", "subagent-bb2", "subagent:implement", None)
    assert scheduler.handoff_to_main("a", "subagent-aa1", "turn-1", "timeout")
    assert scheduler.handoff_to_main("b", "subagent-bb2", "turn-1", "timeout")
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    llm._mark_plan_turn("turn-1")

    result = _call(
        "plan_finish",
        {"item_id": "a", "result": "修复完成"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "已完成" in result["content"]
    items = {item["id"]: item for item in plan.snapshot()}
    assert items["a"]["status"] == "completed"
    assert items["b"]["status"] == "in_progress"
    assert items["b"]["execution_mode"] == "main_takeover"
    assert items["b"]["takeover_turn_id"] == "turn-1"


def test_plan_finish_verification_failure_marks_failed_without_redelegation(
    monkeypatch, tmp_path
):
    """verification 命令非零退出：计划项进入 failed，绝不重新委派。"""
    import sys

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")

    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{
        "id": "a",
        "subject": "verify fail",
        "verification_type": "command",
        "verification": f'"{sys.executable}" -c "import sys; sys.exit(3)"',
    }])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item("a", "subagent-aa1", "subagent:implement", None)
    assert scheduler.handoff_to_main("a", "subagent-aa1", "turn-1", "timeout")
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    llm._mark_plan_turn("turn-1")

    result = _call(
        "plan_finish",
        {"item_id": "a", "result": "self-reported done"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "验证" in result["content"] or "失败" in result["content"]
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert item["execution_mode"] == "main_takeover"
    assert item["takeover_required"] is True

    retry = _call(
        "plan_retry",
        {"item_id": "a", "force": True},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )
    assert "plan_resume_takeover" in retry["content"]
    assert plan.snapshot()[0]["status"] == "failed"

    redelegation = _call(
        "run_subagent",
        {"task_description": "重新执行", "plan_item_id": "a"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )
    blocked = json.loads(redelegation["content"])
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "main_takeover_required"
    assert "plan_resume_takeover" in blocked["error"]

    resume_events = []
    resumed = _call(
        "plan_resume_takeover",
        {"item_id": "a"},
        parent_session="session-1",
        parent_turn_id="turn-2",
        event_callback=lambda kind, payload: resume_events.append((kind, payload)),
    )
    assert "已恢复" in resumed["content"]
    item = plan.snapshot()[0]
    assert item["status"] == "in_progress"
    assert item["execution_mode"] == "main_takeover"
    assert item["takeover_turn_id"] == "turn-2"
    assert item["attempts"] == 1
    assert item["delegation_count"] == 1
    handoffs = [
        payload for kind, payload in resume_events
        if kind == "plan_item_handoff"
    ]
    assert len(handoffs) == 1
    assert handoffs[0]["item"]["id"] == "a"
    assert handoffs[0]["mode"] == "main_takeover"
    assert handoffs[0]["queue_position"] == 0


def test_plan_resume_takeover_rejects_ordinary_item(monkeypatch):
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "ordinary", "subject": "普通项目", "status": "failed"}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )

    result = _call(
        "plan_resume_takeover",
        {"item_id": "ordinary"},
        parent_session="session-1",
        parent_turn_id="turn-1",
    )

    assert "不是超时接管项" in result["content"]
    assert plan.snapshot()[0]["status"] == "failed"


# ── Task 9：follow-up 私有载荷的 record 承载与剥离 ──────────────────


def test_tool_execution_record_default_followup_is_empty():
    from core.harness.tool_pipeline import ToolExecutionRecord

    record = ToolExecutionRecord(0, _batch_tool_message("call-1"), 12)

    assert record.followup_messages == ()


def test_tool_execution_context_computer_lease_defaults_to_none():
    assert _batch_context().computer_lease is None


def test_batch_executor_moves_private_followup_stash_into_record():
    """execute_decoded 暂存在 message 内部键上的 follow-up 必须转入 record。"""
    import core.harness.tool_pipeline as tool_pipeline

    followup = {
        "role": "user",
        "content": [{"type": "text", "text": "观察"}],
        "_computer_v1": {"run_id": "run-a", "ordinal": 1, "target": "browser"},
    }

    def execute(decoded, _context):
        message = _batch_tool_message(decoded.call_id)
        message["_ycode_followup_messages"] = (followup,)
        return message

    executor = tool_pipeline.ToolBatchExecutor(
        tool_pipeline.ToolPipeline(),
        execute_decoded=execute,
        event_payload=lambda decoded: {"id": decoded.call_id},
        finished_event_payload=lambda decoded, message, duration_ms: {
            "id": decoded.call_id,
            "duration_ms": duration_ms,
        },
    )

    records = executor.execute([_batch_call("c1", "read_file")], _batch_context(), None)

    assert records[0].followup_messages == (followup,)
    assert records[0].message == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "read_file",
        "content": "ok",
    }


def test_execute_one_strips_private_followup_stash(monkeypatch):
    """旧单调用入口没有 follow-up 通道：私有暂存必须剥离，绝不泄漏进普通 message。"""
    import core.harness.tool_pipeline as tool_pipeline

    pipeline = tool_pipeline.ToolPipeline()

    def stashed(_decoded, _context):
        message = _batch_tool_message("call-x")
        message["_ycode_followup_messages"] = ({"role": "user", "content": []},)
        return message

    monkeypatch.setattr(pipeline, "execute_decoded", stashed)

    result = pipeline.execute_one(
        {"id": "call-x", "function": {"name": "read_file", "arguments": "{}"}},
        _batch_context(),
    )

    assert result == {
        "role": "tool",
        "tool_call_id": "call-x",
        "name": "read_file",
        "content": "ok",
    }
