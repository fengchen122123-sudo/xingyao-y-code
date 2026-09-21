"""C6-C10 Hooks 接入整合测试（核心子集）。

覆盖：
- H-T8：Stop 续跑上限（5 次后强制结束；新回合重置）——lifecycle 级；
- H-T16：无 hooks 配置时工具链输出零变化（无附加消息键）；
- H-T3：PreToolUse deny 阻断工具调用（经真实 tool_pipeline 管线）；
- H-T4：hook allow 不可提权（plan 档写操作在 hook 前已被权限链拒绝，
        hook 脚本无执行痕迹）。

更多场景（H-T6 注入可见端到端 / H-T17 入参裁剪端到端）见待续清单，
本文件聚焦"接入链已生效且无配置零影响"两个最高风险面。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import core.llm as llm
from core.hooks.lifecycle import StopContinueState
from core.tool_protocol import ToolResult, ToolSpec
from core.tool_registry import ToolRegistry


@pytest.fixture(autouse=True)
def _stable_tool_environment(monkeypatch):
    from core import permission_grants, permission_store

    class EmptyMcp:
        def get_schemas(self):
            return []

        def get_schema(self, _name):
            return None

    permission_grants._clear_all_for_tests()
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "full")
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


def _probe_registry(execute=None):
    return ToolRegistry([
        ToolSpec(
            name="probe_tool",
            description="probe",
            input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
            execute=execute or (lambda **_kwargs: ToolResult("probe-ok")),
            read_only=True,
            requires_permission=False,
            parallel_safe=True,
        )
    ])


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _install_hook_runner(tmp_path, monkeypatch, hooks_payload, *, grant=True):
    """构造隔离 HookRunner（tmp 配置/信任）并替换默认 getter 与工作目录。"""
    from core.hooks.registry import HookRegistry
    from core.hooks.runner import HookRunner
    from core.hooks.trust import HookTrustStore, normalize_workspace_identity

    import core.hooks.lifecycle as lifecycle

    proj = tmp_path / "proj"
    hooks_path = proj / ".ycode" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps({"version": 1, "hooks": hooks_payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = HookRegistry(
        user_hooks_path=tmp_path / "home" / "hooks.json",
        project_hooks_path=hooks_path,
    )
    trust = HookTrustStore(store_path=tmp_path / "hook_trust.json")
    runner = HookRunner(registry, trust)
    if grant:
        identity = normalize_workspace_identity(str(proj))
        for reg in registry.load(workspace_root=str(proj)):
            trust.grant(
                workspace_identity=identity,
                digest=reg.digest,
                source_path=reg.source_path,
                command=reg.command,
            )
    monkeypatch.setattr(lifecycle, "get_default_hook_runner", lambda: runner)
    # 让 build_hook_input 的 cwd 指向工程目录（信任判定按 workspace_identity 对齐）
    monkeypatch.setattr(lifecycle, "_current_cwd", lambda: str(proj))
    monkeypatch.setattr(lifecycle, "_current_permission_mode", lambda: "full")
    return runner, proj


# ── H-T8：Stop 续跑上限（lifecycle 级） ──


def test_stop_continue_limit_after_five_then_reset_new_turn():
    """连续 5 次放行，第 6 次强制结束；新回合（turn 变化）重置计数。"""
    state = StopContinueState()
    for _ in range(5):
        decision = state.decide("sess-1", "turn-1")
        assert decision["continue"] is True and decision["limit_hit"] is False
    blocked = state.decide("sess-1", "turn-1")
    assert blocked["continue"] is False and blocked["limit_hit"] is True

    reset = state.decide("sess-1", "turn-2")
    assert reset["continue"] is True and reset["count"] == 1


def test_evaluate_stop_uses_hook_result(monkeypatch):
    """evaluate_stop：hook 请求续跑 → 注入消息 + 计数；无请求 → None（零影响）。"""
    import core.hooks.lifecycle as lifecycle

    class _Result:
        def __init__(self, cont):
            self.additional_contexts = ["stop-ctx"]
            self.continue_after_stop = cont

    class _Runner:
        def __init__(self, cont):
            self._cont = cont

        def run(self, event, hook_input, cancel_event=None):
            return _Result(self._cont)

    state = StopContinueState()

    assert (
        lifecycle.evaluate_stop(state, None, session_id="s", turn_id="t") is None
    )  # 无 runner → None

    decision = lifecycle.evaluate_stop(
        state, _Runner(False), session_id="s", turn_id="t"
    )
    assert decision is None  # hook 未请求续跑 → 零影响

    decision = lifecycle.evaluate_stop(
        state, _Runner(True), session_id="s", turn_id="t"
    )
    assert decision["continue"] is True
    assert decision["messages"] and "stop-ctx" in decision["messages"][0]["content"]


# ── P1-1：Stop 续跑上限的 turn 恒定性（llm 接入契约） ──


def test_stop_continue_loop_keeps_same_turn_id(monkeypatch):
    """续跑循环中 llm 传给 evaluate_stop 的 turn_id 恒定，上限才真正生效。

    评审 P1-1 关注的失败模式：hook 请求续跑 → 注入 contexts → 下一轮模型
    调用 → 再次 Stop 判定；若每轮重新生成 turn_id，StopContinueState 会按
    “新回合”重置计数，5 次上限永不触发（潜在死循环）。这里真实驱动
    chat_with_model_events（mock provider 每轮返回纯文本），断言：
    1) 6 次 Stop 判定的 (session_id, turn_id) 完全一致——llm.py:2513 传的是
       _chat_with_model_generator 的函数参数 parent_turn_id（llm.py:1044
       定义，循环体内无重新赋值）；
    2) 第 6 次判定 limit_hit，回合走正常收尾（turn_finished）；
    3) 同一 state 换 turn_id 后计数重置（lifecycle 级用例已覆盖，此处顺带锁定）。
    """
    from types import SimpleNamespace

    import core.hooks.lifecycle as lifecycle
    from core.harness import turn_limits

    def _chunk(content):
        delta = SimpleNamespace(
            content=content,
            reasoning_content="",
            tool_calls=None,
            provider_specific_fields=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

    class _StopResult:
        continue_after_stop = True
        additional_contexts = ["stop-ctx"]

    class _StopRunner:
        def run(self, event, hook_input, cancel_event=None):
            return _StopResult()

    state = StopContinueState()
    seen: list[tuple[str, str]] = []
    real_evaluate_stop = lifecycle.evaluate_stop

    def spy_evaluate_stop(controller, runner, *, session_id, turn_id, **kwargs):
        seen.append((str(session_id), str(turn_id)))
        return real_evaluate_stop(
            controller, runner, session_id=session_id, turn_id=turn_id, **kwargs
        )

    monkeypatch.setattr(lifecycle, "evaluate_stop", spy_evaluate_stop)
    monkeypatch.setattr(lifecycle, "get_stop_continue_state", lambda: state)
    monkeypatch.setattr(lifecycle, "get_default_hook_runner", lambda: _StopRunner())
    monkeypatch.setattr(turn_limits, "load_turn_step_limit", lambda: 20)
    monkeypatch.setattr(llm, "completion", lambda **_kwargs: iter([_chunk("done")]))

    events = list(llm.chat_with_model_events(
        "fake-model",
        "fake-key",
        [{"role": "user", "content": "做一个多步任务"}],
        persist=False,
        turn_id="turn-stop-guard",
        parent_session="sess-stop-guard",
        parent_turn_id="turn-stop-guard",
    ))

    # 1) 恒定性：同一回合每一轮 Stop 判定都用同一个 turn_id
    assert len(seen) == 6
    assert set(seen) == {("sess-stop-guard", "turn-stop-guard")}
    # 2) 上限生效：第 6 次判定到达上限 → 正常收尾而非继续循环
    assert "turn_finished" in [event.kind for event in events]
    assert state.decide("sess-stop-guard", "turn-stop-guard")["limit_hit"] is True
    # 3) 换 turn_id（新回合）计数重置
    reset = state.decide("sess-stop-guard", "turn-next")
    assert reset["continue"] is True and reset["count"] == 1


# ── H-T16：无 hooks 配置时零影响 ──


def test_no_hooks_config_is_zero_impact(monkeypatch):
    """无任何 hooks 配置时，工具调用输出不含 hook 附加消息键、执行结果正常。"""
    monkeypatch.setattr(llm, "tool_registry", _probe_registry())
    message = _call("probe_tool", {"x": "1"})

    assert message["role"] == "tool"
    assert message["name"] == "probe_tool"
    assert "probe-ok" in message["content"]
    assert "_ycode_followup_messages" not in message


# ── H-T3：PreToolUse deny 阻断 ──


def test_pre_tool_use_block_denies_call(tmp_path, monkeypatch):
    """hook 输出 decision=block → 工具调用被阻断，模型收到拒绝原因。"""
    hook_cmd = _script(
        tmp_path,
        "block.py",
        "import json\nprint(json.dumps({'decision': 'block', 'reason': 'policy-no'}))\n",
    )
    _install_hook_runner(
        tmp_path,
        monkeypatch,
        {"PreToolUse": [{"matcher": "*", "command": hook_cmd}]},
    )
    monkeypatch.setattr(llm, "tool_registry", _probe_registry())

    message = _call("probe_tool", {"x": "1"})
    assert "被 PreToolUse hook 阻断" in message["content"]
    assert "policy-no" in message["content"]


# ── H-T4：hook allow 不可提权（plan 档写操作在 hook 前已被拒） ──


def test_hook_allow_cannot_escalate_plan_mode(tmp_path, monkeypatch):
    """plan 档下的写操作被权限链拒绝（hook 之前 return），hook 脚本无执行痕迹。

    证明"hook 的 allow 不可提权"：即便 hook 配置放行，写操作依然被拒，
    且 PreToolUse 根本不运行（marker 文件不存在 + _run_tool_hook 调用计数为 0）。
    """
    marker = tmp_path / "hook-ran.marker"
    hook_cmd = _script(
        tmp_path,
        "allow.py",
        "import json, pathlib\n"
        f"pathlib.Path(r'{marker}').write_text('ran', encoding='utf-8')\n"
        "print(json.dumps({'permission_decision': 'allow'}))\n",
    )
    _install_hook_runner(
        tmp_path,
        monkeypatch,
        {"PreToolUse": [{"matcher": "*", "command": hook_cmd}]},
    )
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "plan")
    monkeypatch.setattr(
        llm,
        "tool_registry",
        ToolRegistry([
            ToolSpec(
                name="write_file",
                description="write",
                input_schema={"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}},
                execute=lambda **_kwargs: ToolResult("written"),
                mutates_files=True,
                requires_permission=True,
                parallel_safe=False,
            )
        ]),
    )

    # P2-8：管线级计数——deny 先行路径下 _run_tool_hook 根本不应被调用
    from core.harness import tool_pipeline as tool_pipeline_module

    hook_calls = []
    real_run_tool_hook = tool_pipeline_module.ToolPipeline._run_tool_hook

    def counting_run_tool_hook(self, *args, **kwargs):
        hook_calls.append(args)
        return real_run_tool_hook(self, *args, **kwargs)

    monkeypatch.setattr(
        tool_pipeline_module.ToolPipeline, "_run_tool_hook", counting_run_tool_hook
    )

    message = _call("write_file", {"file_path": "a.txt", "content": "x"})
    # 被权限链拒绝（plan 档不允许写操作）
    assert "计划模式" in message["content"] or "拒绝" in message["content"]
    # hook 未运行：不可提权（脚本无痕迹 + 管线未进入 hook 调用点）
    assert not marker.exists()
    assert hook_calls == []


# ── H-T6 + H-T17：contexts 经 follow-up 载荷注入；项目级入参 [redacted] ──


def test_project_hook_receives_redacted_input_end_to_end(tmp_path, monkeypatch):
    """H-T17 端到端：项目级 hook 默认只收到 [redacted] 值（hook 实收内容写盘验证）。"""
    marker = tmp_path / "received.json"
    hook_cmd = _script(
        tmp_path,
        "spy.py",
        "import json, sys, pathlib\n"
        "data = json.load(sys.stdin)\n"
        "pathlib.Path(r'" + str(marker) + "').write_text("
        "json.dumps(data.get('tool_input'), ensure_ascii=False), encoding='utf-8')\n"
        "print(json.dumps({'additional_context': 'seen'}))\n",
    )
    _install_hook_runner(
        tmp_path,
        monkeypatch,
        {"PreToolUse": [{"matcher": "*", "command": hook_cmd}]},
    )
    monkeypatch.setattr(llm, "tool_registry", _probe_registry())

    _call("probe_tool", {"x": "secret-value"})
    received = json.loads(marker.read_text(encoding="utf-8"))
    assert received == {"x": "[redacted]"}


def test_hook_contexts_attached_as_followup_on_batch_path(tmp_path, monkeypatch):
    """H-T6：execute_decoded（批量链入口）把 hook contexts 挂到 follow-up 载荷，
    由 ToolBatchExecutor → record.followup_messages → llm.py 注入点进入模型消息；
    单工具兼容入口 execute_one 会主动剥离该键（旧入口无 follow-up 通道）。"""
    from core.harness.tool_pipeline import decode_tool_call
    from core.llm_execution import _build_tool_pipeline_and_context

    hook_cmd = _script(
        tmp_path,
        "ctx.py",
        "import json\nprint(json.dumps({'additional_context': 'hook-ctx-1'}))\n",
    )
    _install_hook_runner(
        tmp_path,
        monkeypatch,
        {"PreToolUse": [{"matcher": "*", "command": hook_cmd}]},
    )
    monkeypatch.setattr(llm, "tool_registry", _probe_registry())

    pipeline, context = _build_tool_pipeline_and_context(lambda *_a, **_k: None)
    decoded = decode_tool_call(
        {"id": "c1", "function": {"name": "probe_tool", "arguments": '{"x": "1"}'}}
    )
    message = pipeline.execute_decoded(decoded, context)
    followups = message.get("_ycode_followup_messages") or ()
    assert followups, "hook contexts 应挂在 follow-up 私有载荷上（H-T6 通道）"
    assert "hook-ctx-1" in followups[0]["content"]


# ── C8：SessionStart 文本辅助（一次性 + 无 runner 零影响） ──


def test_session_start_context_text_runs_once_per_session():
    """session_start_context_text：无 runner 空串；每会话只触发一次。"""
    import core.hooks.lifecycle as lifecycle

    assert lifecycle.session_start_context_text(None, session_id="s-any") == ""

    calls = []

    class _Runner:
        def run(self, event, hook_input, cancel_event=None):
            calls.append(event)

            class _R:
                additional_contexts = ["welcome-ctx"]

            return _R()

    first = lifecycle.session_start_context_text(_Runner(), session_id="sess-ut-once-1")
    second = lifecycle.session_start_context_text(_Runner(), session_id="sess-ut-once-1")
    assert first == "welcome-ctx"
    assert second == ""
    assert len(calls) == 1
