from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_continuation_preserves_history_and_allowed_tools(monkeypatch):
    import core.llm as llm
    from core.tools import SubagentManager, SubagentTask, default_subagent_allowed_tools
    from core.turn_events import TurnEvent

    captured_calls = []

    def fake_chat(model, key, messages, url, **kwargs):
        captured_calls.append((list(messages), kwargs.get("allowed_tools")))
        if len(captured_calls) == 1:
            # 模拟第一轮先调工具并把 tool_calls/tool 历史追加进 messages，再给短回答
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-r1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"file_path":"a.py"}'},
                }],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": "call-r1",
                "name": "read_file",
                "content": "def hello():\n    return 42\n",
            })
            messages.append({"role": "assistant", "content": "已查看 a.py。"})
            yield TurnEvent("t1", 1, "tool_started", {"id": "call-r1", "name": "read_file"})
            yield TurnEvent("t1", 2, "tool_finished", {"id": "call-r1", "name": "read_file", "ok": True})
            yield TurnEvent("t1", 3, "assistant_delta", {"text": "已查看 a.py。"})
        else:
            yield TurnEvent(
                "t1",
                4,
                "assistant_delta",
                {"text": "已完成详细分析：a.py 中定义了 hello() 函数并返回 42，结构清晰，无需额外修改。" * 3},
            )

    monkeypatch.setattr(llm, "chat_with_model_events", fake_chat)

    mgr = SubagentManager()
    task = SubagentTask(
        task_id="subagent-cont0001",
        parent_session="s-cont",
        parent_turn_id="turn-cont",
        description="检查 a.py",
        subagent_type="explore",
        model_config=("openai/m", "k", "u"),
    )
    out = mgr._run_default_subagent(task)

    assert len(captured_calls) == 2
    cont_messages, cont_allowed_tools = captured_calls[1]
    assert cont_allowed_tools == default_subagent_allowed_tools("explore")
    assert any(m.get("role") == "tool" and m.get("name") == "read_file" for m in cont_messages)
    assert "已完成详细分析" in out


def test_continuation_turn_error_does_not_overwrite_short_answer(monkeypatch):
    import core.llm as llm
    from core.tools import SubagentManager, SubagentTask
    from core.turn_events import TurnEvent

    call_no = 0

    def fake_chat(model, key, messages, url, **kwargs):
        nonlocal call_no
        call_no += 1
        if call_no == 1:
            yield TurnEvent("t1", 1, "assistant_delta", {"text": "简短结论：正常。"})
        else:
            yield TurnEvent("t1", 2, "assistant_delta", {"text": "失败前的半截长文本" * 30})
            yield TurnEvent("t1", 3, "turn_error", {"message": "网络超时"})

    monkeypatch.setattr(llm, "chat_with_model_events", fake_chat)

    mgr = SubagentManager()
    task = SubagentTask(
        task_id="subagent-cont0002",
        parent_session="s-cont",
        parent_turn_id="turn-cont",
        description="检查状态",
        subagent_type="explore",
        model_config=("openai/m", "k", "u"),
    )
    out = mgr._run_default_subagent(task)
    assert out == "简短结论：正常。"


def test_test_role_nonzero_exit_diagnostic_completes_with_warning(monkeypatch):
    import core.llm as llm
    from core.tools import SUBAGENT_WARNING_MARKER, SubagentManager
    from core.turn_events import TurnEvent

    diag_text = "测试定位完成：test_login 因 token 过期断言失败（AssertionError），已定位至 auth.py:42。" * 3

    def fake_chat(*args, **kwargs):
        yield TurnEvent("t1", 1, "tool_started", {"id": "c1", "name": "execute_command"})
        yield TurnEvent("t1", 2, "tool_finished", {
            "id": "c1",
            "name": "execute_command",
            "ok": False,
            "summary": "pytest tests/test_auth.py — 命令执行失败（退出码 1）。1 failed",
        })
        yield TurnEvent("t1", 3, "assistant_delta", {"text": diag_text})

    monkeypatch.setattr(llm, "chat_with_model_events", fake_chat)
    mgr = SubagentManager()
    result = mgr.run("运行认证测试并定位失败原因", "s1", "t1", subagent_type="test", timeout_seconds=30)

    snap = mgr.snapshot()[0]
    assert snap.status == "completed"
    assert SUBAGENT_WARNING_MARKER in result
    assert "退出码 1" in result
    assert "auth.py:42" in result


def test_test_role_permission_denied_still_fails(monkeypatch):
    import core.llm as llm
    from core.tools import SubagentManager
    from core.turn_events import TurnEvent

    def fake_chat(*args, **kwargs):
        yield TurnEvent("t1", 1, "tool_started", {"id": "c1", "name": "execute_command"})
        yield TurnEvent("t1", 2, "tool_finished", {
            "id": "c1",
            "name": "execute_command",
            "ok": False,
            "summary": "用户拒绝了执行该操作。",
        })
        yield TurnEvent("t1", 3, "assistant_delta", {"text": "命令被拒绝，无法运行测试。" * 10})

    monkeypatch.setattr(llm, "chat_with_model_events", fake_chat)
    mgr = SubagentManager()
    mgr.run("运行测试", "s1", "t1", subagent_type="test", timeout_seconds=30)

    assert mgr.snapshot()[0].status == "failed"


def test_ephemeral_subagent_overflow_compacts_locally_without_touching_main_session(
    mocker, monkeypatch, tmp_path
):
    from core import app_context
    from core.llm import chat_with_model_events

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_context, "get_current_session", lambda: "main-ui-session")

    def forbidden_compress(*args, **kwargs):
        raise AssertionError("子代理溢出不得压缩主会话")

    monkeypatch.setattr("core.memory_compressor.compress_session_memory", forbidden_compress)

    def _chunk(text="", tool_calls=None):
        delta = SimpleNamespace(content=text, reasoning_content="", tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)

    def _tc(call_id, name, args):
        return SimpleNamespace(
            index=0,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )

    huge_output = "\n".join(f"line {i}: " + ("x" * 120) for i in range(120))
    big_file = tmp_path / "big.py"
    big_file.write_text(huge_output, encoding="utf-8")
    captured_lengths = []
    attempt_count = 0

    def fake_completion(**kwargs):
        nonlocal attempt_count
        attempt_count += 1
        msgs = list(kwargs.get("messages") or [])
        captured_lengths.append(sum(len(str(m)) for m in msgs))
        if attempt_count == 1:
            return iter([_chunk(tool_calls=[_tc("c1", "read_file", {"file_path": str(big_file)})])])
        if attempt_count == 2:
            raise RuntimeError("prompt is too long: maximum context length exceeded")
        return iter([_chunk("精简后已完成分析。")])

    mocker.patch("core.llm.completion", side_effect=fake_completion)
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)

    messages = [
        {"role": "system", "content": "你是子代理"},
        {"role": "user", "content": "请分析 big.py"},
    ]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="subagent-ovf00001",
        allow_subagents=False,
        parent_session=None,
        cache_scope="subagent:explore",
        consume_background_notifications=False,
    ))

    assert events[-1].kind == "turn_finished"
    assert attempt_count == 3
    assert captured_lengths[2] < captured_lengths[1]
    assert messages[0]["content"] == "你是子代理"
    assert any(m.get("role") == "user" and m.get("content") == "请分析 big.py" for m in messages)


def test_orchestration_uses_isolated_worker_pool_from_leaf_tools():
    from core.harness.tool_pipeline import (
        _get_orchestration_worker_pool,
        _get_tool_worker_pool,
        _submit_daemon_call,
        _submit_orchestration_call,
    )

    assert _get_orchestration_worker_pool() is not _get_tool_worker_pool()
    orch_thread = _submit_orchestration_call(
        lambda: __import__("threading").current_thread().name
    ).result(timeout=5)
    tool_thread = _submit_daemon_call(
        lambda: __import__("threading").current_thread().name
    ).result(timeout=5)
    # 两者来自不同底层线程池实例（仅校验池对象隔离且都能正常执行）
    assert orch_thread == "ycode-tool-call"
    assert tool_thread == "ycode-tool-call"


def test_custom_subagent_parses_yaml_bracket_and_multiline_lists(tmp_path, monkeypatch):
    import core.subagent_registry as reg

    with reg._cache_lock:
        reg._cache["fingerprint"] = None
        reg._cache["subagents"] = []

    (tmp_path / "bracket.md").write_text(
        "---\n"
        "name: bracket-agent\n"
        "description: 方括号列表\n"
        'allowed_tools: [read_file, "search_code", \'glob_files\']\n'
        "---\n"
        "正文 A",
        encoding="utf-8",
    )
    (tmp_path / "multiline.md").write_text(
        "---\n"
        "name: multiline-agent\n"
        "description: 多行列表\n"
        "allowed_tools:\n"
        "  - read_file\n"
        "  - fetch_url\n"
        "---\n"
        "正文 B",
        encoding="utf-8",
    )
    monkeypatch.setattr(reg, "get_agent_search_dirs", lambda: [str(tmp_path)])

    loaded = {item["name"]: item for item in reg.scan_custom_subagents()}
    assert loaded["bracket-agent"]["allowed_tools"] == {"read_file", "search_code", "glob_files"}
    assert loaded["multiline-agent"]["allowed_tools"] == {"read_file", "fetch_url"}


def test_foreground_subagent_result_ok_presenter_and_command_evidence(mocker):
    from core.llm import _tool_result_ok, chat_with_model_events
    from core.tool_presenter import summarize_tool_finished

    assert _tool_result_ok("run_subagent", {"content": "子代理执行失败（task_id=subagent-1）"}) is False
    assert _tool_result_ok("run_subagent", {"content": "子代理执行超时（task_id=subagent-1）"}) is False
    assert _tool_result_ok("run_subagent", {"content": "子代理执行已取消（task_id=subagent-1）"}) is False
    assert _tool_result_ok("run_subagent", {"content": "已完成核查：一切正常。"}) is True

    args = {"task_description": "跑测试"}
    assert summarize_tool_finished(
        "run_subagent", args, {"content": "子代理执行超时（task_id=subagent-1）"}, ok=False
    ) == "子代理执行超时：跑测试"
    assert summarize_tool_finished(
        "run_subagent", args, {"content": "子代理执行已取消（task_id=subagent-1）"}, ok=False
    ) == "子代理已取消：跑测试"

    # 前台 test 子代理完成后写文件+声称测试通过，不应再误触发未执行命令催促
    def _chunk(text="", tool_calls=None):
        delta = SimpleNamespace(content=text, reasoning_content="", tool_calls=tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=None)

    def _tc(call_id, name, arguments):
        return SimpleNamespace(
            index=0,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(arguments, ensure_ascii=False)),
        )

    responses = [
        iter([_chunk(tool_calls=[
            _tc("c1", "write_file", {"file_path": "demo.py", "content": "x = 1\n"}),
            _tc("c2", "run_subagent", {"task_description": "跑测试", "subagent_type": "test"}),
        ])]),
        iter([_chunk("已完成修改，全部测试通过。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    mocker.patch("core.llm.check_permission_and_ask", return_value=True)
    mocker.patch("core.llm.write_file", return_value="ok")
    mocker.patch("core.llm.run_subagent", return_value="pytest 8 passed in 0.2s")

    messages = [{"role": "user", "content": "写文件并跑测试"}]
    events = list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=False,
        turn_id="turn-fg-test-evidence",
        consume_background_notifications=False,
    ))
    assert events[-1].kind == "turn_finished"
    assert not any(
        m.get("role") == "user" and "没有执行任何命令" in str(m.get("content") or "")
        for m in messages
    )


def test_decide_subagent_review_packs_long_result_within_budget(mocker):
    from core import task_decision_hooks as hooks

    captured = {}

    def _fake_decide(scene, state, question, options, audit_extra=None):
        captured["state"] = state
        return "accepted"

    mocker.patch.object(hooks, "_task_decision_armed", return_value=True)
    mocker.patch("core.decision_gateway.decide", side_effect=_fake_decide)

    long_result = ("结论开头：已完成全部核心排查。" + (" 中间过程细节" * 80)) + " 总结收尾：关键测试全部通过。"
    picked = hooks.decide_subagent_review(
        description="核查长输出子代理结果",
        subagent_type="explore",
        result_text=long_result,
        tool_success_count=4,
        tool_errors=None,
        plan_item_id="p1",
    )

    assert picked == "accepted"
    state = captured["state"]
    assert len(state) <= 500
    assert len(state) > 400
    assert "结论开头：已完成全部核心排查。" in state
    assert "总结收尾：关键测试全部通过。" in state
    assert " ... " in state

