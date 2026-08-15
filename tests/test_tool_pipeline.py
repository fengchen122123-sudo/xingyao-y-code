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
        "🚨 用户拒绝了访问沙盒外路径的请求。沙盒外路径每次访问都需要用户确认。"
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
