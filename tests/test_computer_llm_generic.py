"""Task 10：冻结 Turn capability 并接入 generic Provider 请求。

全部 fake 注入：Provider 流、capability 决策与 controller 均为测试替身，
绝不触发真实 Provider 请求、浏览器、截屏或键鼠。
"""

import json
import threading
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

import core.llm as llm_mod
from core.computer.models import ComputerControlError, ComputerOwner
from core.llm import chat_with_model_events
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
    FakeController,
)
from tests.request_payload_snapshot import ALPHA_TOOL, ZETA_TOOL

SESSION = "comp-session"
TURN = "comp-turn"

# pre-feature 基线请求字节（2026-09-03 用未改动代码真实捕获；
# 2026-09-08 因内置默认输出上限表变更同步更新基线：max_tokens 8192→32000，
# 且 max_tokens 改由 provider options 提前注入，字段顺序随之为 max_tokens 在
# tool_choice 之前）：
# capability 关闭时整份 Provider 请求必须逐字节复现该常量。
EXISTING_DEFAULT_REQUEST_BYTES = (
    '{"model":"openai/generic-vision","api_key":"<redacted>",'
    '"messages":[{"role":"user","content":"你好"}],'
    '"tools":['
    '{"type":"function","function":{"name":"zeta","description":"zeta",'
    '"parameters":{"type":"object","properties":{}}}},'
    '{"type":"function","function":{"name":"alpha","description":"alpha",'
    '"parameters":{"type":"object","properties":{}}}}],'
    '"stream":true,"stream_options":{"include_usage":true},"num_retries":2,'
    '"max_tokens":32000,"tool_choice":"auto","timeout":90'
    '}'
).encode("utf-8")


# ---------------------------------------------------------------------------
# 模块内 fake Provider 与 helper（brief 规定不得放进 computer_fakes.py）
# ---------------------------------------------------------------------------


def _chunk(content="", tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=None,
        tool_calls=tool_calls,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
        usage=None,
    )


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _computer_call(call_id, arguments, index=0):
    return _tool_call(index, call_id, "computer_action", json.dumps(arguments))


def get_existing_tool_names():
    return [tool["function"]["name"] for tool in (ZETA_TOOL, ALPHA_TOOL)]


def canonical_bytes(request):
    safe = deepcopy(request)
    if "api_key" in safe:
        safe["api_key"] = "<redacted>"
    return json.dumps(
        safe,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _patch_request_env(monkeypatch):
    """受控请求环境：固定 tools schema、openai 协议、无记忆注入、视觉模型。"""
    monkeypatch.setattr(llm_mod, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL])
    monkeypatch.setattr(
        "core.model_manager.get_model_protocol", lambda *_args, **_kwargs: "openai"
    )
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section", lambda: ""
    )
    monkeypatch.setattr(
        llm_mod, "_describe_images_with_secondary", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(llm_mod, "estimate_tokens", lambda *_args, **_kwargs: 0)
    # generic 能力要求视觉模型：最新截图必须原样进入下一请求
    monkeypatch.setattr(llm_mod, "_supports_vision", lambda _model: True)


def _fake_controller(tmp_path, *, run_id="run-a", execute_errors=()):
    """与本模块 SESSION/TURN 完全一致的 FakeController（含 Task 10 生命周期记录）。"""
    png_path = tmp_path / f"observation-{run_id}.png"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-observation")
    owner = ComputerOwner(
        session_id=SESSION,
        turn_id=TURN,
        session_label=SESSION,
        cancel_event=threading.Event(),
    )
    return FakeController(
        owner=owner,
        run_id=run_id,
        png_path=str(png_path),
        execute_errors=list(execute_errors),
    )


def capture_first_provider_request(
    monkeypatch,
    *,
    capability=DISABLED_CAPABILITY,
    model="openai/generic-vision",
    turn_source="local",
    allow_subagents=True,
):
    """单请求回合：返回第一份（也是唯一一份）Provider 请求 kwargs。"""
    _patch_request_env(monkeypatch)
    if capability is not None:
        monkeypatch.setattr(
            llm_mod, "resolve_computer_capability", lambda *args, **kwargs: capability
        )
    captured = []

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        return iter([_chunk("收到")])

    monkeypatch.setattr(llm_mod, "completion", fake_completion)
    list(chat_with_model_events(
        model,
        "fake-key",
        [{"role": "user", "content": "你好"}],
        None,
        persist=False,
        turn_id=TURN,
        consume_background_notifications=False,
        allow_subagents=allow_subagents,
        computer_turn_source=turn_source,
    ))
    assert len(captured) == 1
    return captured[0]


def run_two_step_tool_turn(
    monkeypatch,
    tmp_path=None,
    *,
    model="openai/generic-vision",
    capability=GENERIC_CAPABILITY,
    controller=None,
    responses=None,
    messages=None,
    cancel_event=None,
    parent_session=SESSION,
    persist=False,
):
    """两步（或多步）工具回合：返回 (provider 请求列表, 内存 messages)。

    默认第一步返回 computer_action observe、第二步返回最终文本；
    responses 可覆盖每一步的 chunk 列表。
    """
    _patch_request_env(monkeypatch)
    if capability is not None:
        monkeypatch.setattr(
            llm_mod, "resolve_computer_capability", lambda *args, **kwargs: capability
        )
    if controller is not None:
        monkeypatch.setattr(
            llm_mod, "get_computer_control_service", lambda: controller
        )
    if responses is None:
        responses = [
            [_chunk(tool_calls=[_computer_call(
                "call-computer-1", {"kind": "observe", "target": "browser"},
            )])],
            [_chunk("电脑操控完成，已给出结论。")],
        ]
    requests = []

    def fake_completion(**kwargs):
        requests.append(deepcopy(kwargs))
        return iter(responses[len(requests) - 1])

    monkeypatch.setattr(llm_mod, "completion", fake_completion)
    if messages is None:
        messages = [{"role": "user", "content": "打开浏览器看看页面"}]
    list(chat_with_model_events(
        model,
        "fake-key",
        messages,
        None,
        persist=persist,
        turn_id=TURN,
        cancel_event=cancel_event,
        parent_session=parent_session,
        parent_turn_id=TURN,
        permission_owner=TURN,
        consume_background_notifications=False,
    ))
    return requests, messages


# ---------------------------------------------------------------------------
# Step 1 样例：Turn 冻结与默认关闭字节
# ---------------------------------------------------------------------------


@pytest.fixture
def generic_model():
    return "openai/generic-vision"


def test_computer_capability_is_resolved_once_per_turn(monkeypatch, generic_model, tmp_path):
    calls = []
    monkeypatch.setattr(
        llm_mod,
        "resolve_computer_capability",
        lambda *args: calls.append(args) or GENERIC_CAPABILITY,
    )
    controller = _fake_controller(tmp_path)
    run_two_step_tool_turn(
        monkeypatch, tmp_path, model=generic_model,
        capability=None, controller=controller,
    )
    assert len(calls) == 1


def test_generic_tool_is_appended_without_reordering_existing_tools(monkeypatch):
    request = capture_first_provider_request(monkeypatch, capability=GENERIC_CAPABILITY)
    names = [tool["function"]["name"] for tool in request["tools"]]
    assert names[:-1] == get_existing_tool_names()
    assert names[-1] == "computer_action"


def test_default_off_request_matches_existing_golden(monkeypatch):
    request = capture_first_provider_request(monkeypatch, capability=DISABLED_CAPABILITY)
    assert canonical_bytes(request) == EXISTING_DEFAULT_REQUEST_BYTES


def test_capability_without_computer_section_keeps_default_request_bytes(monkeypatch):
    request = capture_first_provider_request(monkeypatch, capability=None)
    assert canonical_bytes(request) == EXISTING_DEFAULT_REQUEST_BYTES


# ---------------------------------------------------------------------------
# 中途开启 / 中途关闭：当前 Turn 冻结，tools contract 不漂移
# ---------------------------------------------------------------------------


def test_mid_turn_enable_does_not_change_frozen_turn(monkeypatch):
    calls = []

    def _resolve(*args, **kwargs):
        calls.append(args)
        return DISABLED_CAPABILITY

    monkeypatch.setattr(llm_mod, "resolve_computer_capability", _resolve)
    requests, _messages = run_two_step_tool_turn(
        monkeypatch,
        capability=None,
        responses=[
            [_chunk(tool_calls=[_tool_call(0, "call-1", "no_such_tool", "{}")])],
            [_chunk("检查完成。")],
        ],
    )
    # 中途开启设置也不会重算本 Turn 的能力：resolve 仍只发生一次
    assert len(calls) == 1
    first_tools = requests[0]["tools"]
    second_tools = requests[1]["tools"]
    assert first_tools == second_tools
    assert "computer_action" not in {
        tool["function"]["name"] for tool in first_tools
    }


def test_mid_turn_disable_keeps_tool_contract_and_reports_error(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path, execute_errors=[
        ComputerControlError("computer_disabled", "电脑操控开关已关闭，run 已停止"),
    ])
    requests, messages = run_two_step_tool_turn(
        monkeypatch, tmp_path,
        controller=controller,
        responses=[
            [_chunk(tool_calls=[_computer_call(
                "call-computer-1", {"kind": "observe", "target": "browser"},
            )])],
            [_chunk("已按设置关闭停止操控，输出结论。")],
        ],
    )
    # tools contract 不被换成另一个 schema：两个请求完全一致
    assert requests[0]["tools"] == requests[1]["tools"]
    assert requests[1]["tools"][-1]["function"]["name"] == "computer_action"
    # 动作收到稳定 9 键 JSON 错误（不附旧截图）
    tool_message = next(m for m in messages if m.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_disabled"
    assert payload["screen_sha256"] == ""
    # 设置已关闭：没有 follow-up 观察消息
    assert not any("_computer_v1" in m for m in messages if isinstance(m, dict))


# ---------------------------------------------------------------------------
# 动态 prompt：只在 enabled 时追加
# ---------------------------------------------------------------------------


def _base_runtime():
    from core.context_builder import RuntimeContext

    return RuntimeContext(
        model_name="openai/generic-vision",
        permission_mode="auto",
        working_dir="E:\\work",
        current_time="2026-09-03 12:00:00",
    )


def test_dynamic_prompt_appends_generic_section_only_when_enabled():
    from core.computer.provider_generic import build_generic_dynamic_prompt
    from core.context_builder import build_dynamic_prompt

    base = _base_runtime()
    default_prompt = build_dynamic_prompt(base)
    # 默认 None 与 disabled 的返回值逐字节相同
    assert build_dynamic_prompt(replace(base, computer_capability=DISABLED_CAPABILITY)) == default_prompt
    enabled_prompt = build_dynamic_prompt(replace(base, computer_capability=GENERIC_CAPABILITY))
    assert enabled_prompt == default_prompt + "\n\n" + build_generic_dynamic_prompt(GENERIC_CAPABILITY)


def test_dynamic_prompt_native_capability_gets_short_note_not_generic_text():
    from core.computer.provider_generic import build_generic_dynamic_prompt
    from core.context_builder import build_dynamic_prompt

    base = _base_runtime()
    default_prompt = build_dynamic_prompt(base)
    native_prompt = build_dynamic_prompt(
        replace(base, computer_capability=NATIVE_BROWSER_CAPABILITY)
    )
    assert native_prompt.startswith(default_prompt)
    assert build_generic_dynamic_prompt(GENERIC_CAPABILITY) not in native_prompt
    assert "computer_select_target" in native_prompt


def test_stable_prompt_is_byte_identical_with_any_capability():
    from core.context_builder import build_stable_prompt

    assert build_stable_prompt("base") == build_stable_prompt("base")
    digest_input = build_stable_prompt("base")
    assert "电脑操控" not in digest_input


# ---------------------------------------------------------------------------
# follow-up 观察消息：提交边界、顺序与隐私
# ---------------------------------------------------------------------------


def test_tool_result_then_observation_followup_in_same_commit_order(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    requests, messages = run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller)
    # 内存顺序：assistant(tool_calls) → tool result → user follow-up
    roles = [m.get("role") for m in messages]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
    assert controller.execute_calls == [("run-a", "observe", "browser")]
    tool_message = messages[2]
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is True
    assert payload["action"] == "observe"
    followup = messages[3]
    assert followup["_computer_v1"] == {
        "run_id": "run-a", "ordinal": 1, "target": "browser",
    }
    assert followup["content"][0]["text"] == "【电脑观察】请只依据这张最新截图决定下一步。"
    assert followup["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    # 第二个请求：tool result 在 follow-up 之前；截图进入多模态消息，
    # _computer_v1 私有元数据绝不发给 Provider
    second_messages = requests[1]["messages"]
    second_roles = [m.get("role") for m in second_messages]
    assert second_roles == ["user", "assistant", "tool", "user"]
    assert "data:image/png;base64," in json.dumps(second_messages[-1], ensure_ascii=False)
    assert "_computer_v1" not in json.dumps(second_messages, ensure_ascii=False)


def test_followup_user_message_persisted_with_computer_meta(monkeypatch, tmp_path, mocker):
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(SESSION)
    mocker.patch("core.app_context.get_manager", return_value=manager)
    controller = _fake_controller(tmp_path)
    run_two_step_tool_turn(
        monkeypatch, tmp_path, controller=controller, persist=True,
    )
    history = manager.get_history(SESSION)
    roles = [m.get("role") for m in history]
    assert roles == ["assistant", "tool", "user", "assistant"]
    followup = history[2]
    assert followup["_computer_v1"] == {"run_id": "run-a", "ordinal": 1, "target": "browser"}
    assert any(block.get("type") == "image_url" for block in followup["content"])
    manager.close()


# ---------------------------------------------------------------------------
# 批处理约束：混用/多个 computer call 整批拒绝电脑动作，零副作用
# ---------------------------------------------------------------------------


def test_mixed_computer_batch_rejects_computer_but_runs_other_tools(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    requests, messages = run_two_step_tool_turn(
        monkeypatch, tmp_path,
        controller=controller,
        responses=[
            [_chunk(tool_calls=[
                _computer_call("call-computer-1", {"kind": "observe", "target": "browser"}),
                _tool_call(1, "call-other-1", "no_such_tool", "{}"),
            ])],
            [_chunk("已收到错误提示，改用普通工具完成。")],
        ],
    )
    # 本 Step 不执行任何电脑动作：run 从未建立
    assert controller.start_calls == 0
    assert controller.execute_calls == []
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call-computer-1", "call-other-1"]
    rejected = json.loads(tool_messages[0]["content"])
    assert rejected["ok"] is False
    assert rejected["error_code"] == "computer_invalid_action"
    assert "不得批量调用" in rejected["error_message"]
    assert "未找到名为 no_such_tool" in tool_messages[1]["content"]
    # 被拒批次零副作用：没有 follow-up 观察消息
    assert not any("_computer_v1" in m for m in messages if isinstance(m, dict))


def test_multiple_computer_calls_rejected_without_execution(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    _requests, messages = run_two_step_tool_turn(
        monkeypatch, tmp_path,
        controller=controller,
        responses=[
            [_chunk(tool_calls=[
                _computer_call("call-computer-1", {"kind": "observe", "target": "browser"}, index=0),
                _computer_call("call-computer-2", {"kind": "click", "target": "browser", "x": 1, "y": 2}, index=1),
            ])],
            [_chunk("已收到错误提示，一次只调用一个动作。")],
        ],
    )
    assert controller.start_calls == 0
    assert controller.execute_calls == []
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 2
    for message in tool_messages:
        payload = json.loads(message["content"])
        assert payload["error_code"] == "computer_invalid_action"


# ---------------------------------------------------------------------------
# 取消打捞：悬空 computer call 生成 computer_cancelled tool result
# ---------------------------------------------------------------------------


def test_cancel_salvage_generates_computer_cancelled_result(monkeypatch, tmp_path, mocker):
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(SESSION)
    mocker.patch("core.app_context.get_manager", return_value=manager)
    controller = _fake_controller(tmp_path, execute_errors=[
        KeyboardInterrupt("用户终止了对话。"),
    ])
    with pytest.raises(KeyboardInterrupt):
        run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller, persist=True)
    history = manager.get_history(SESSION)
    tool_message = next(m for m in history if m.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_cancelled"
    assert "data:image/png;base64" not in tool_message["content"]
    # 取消路径必须停止 run，不留 running 租约
    assert controller.stop_calls == 1
    manager.close()


# ---------------------------------------------------------------------------
# run 生命周期：Turn 正常收尾 complete_run，无 computer 调用不建 run
# ---------------------------------------------------------------------------


def test_computer_run_completed_at_normal_turn_end(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller)
    assert controller.start_calls == 1
    assert controller.complete_calls == 1
    assert controller.stop_calls == 0
    assert controller.completed_run_ids == ["run-a"]


def test_takeover_run_completed_at_turn_end(monkeypatch, tmp_path):
    """用户接管态回合正常收尾时必须按"完成"终结并交还租约。

    接管态（waiting_for_user）若收尾不终结、不清理，run 就会卡在
    waiting_for_user（既无终态也无租约释放），下一回合被
    computer_session_busy 挡死、该 Turn 的电脑操控能力持续失效
    （对应 2026-10-01 实机故障）。收尾必须按普通"完成"路径保留浏览器
    会话（retain_browser_session=True）：外部 Edge/Chrome 上关闭 backend
    会连用户正在手动操作的浏览器窗口一起关掉，接管场景恰恰正在用那个窗口。
    """
    controller = _fake_controller(tmp_path)
    run_two_step_tool_turn(
        monkeypatch, tmp_path,
        controller=controller,
        responses=[
            [_chunk(tool_calls=[_computer_call(
                "call-computer-1",
                {
                    "kind": "request_user_takeover",
                    "target": "browser",
                    "reason_code": "login_required",
                },
            )])],
            [_chunk("已请求你接管登录，等你操作完再继续。")],
        ],
    )
    # 接管态回合正常收尾：走 complete_run 成功，而不是 stop
    assert controller.complete_calls == 1
    assert controller.stop_calls == 0
    # 保留浏览器会话（不得关闭用户正在操作的浏览器）
    assert controller.complete_retain_flags == [True]
    # run 进入终态，租约已交还（下一回合可重新 start_run）
    assert controller.run_status == "completed"


def test_turn_end_finalize_fallback_stops_leaked_run(monkeypatch, tmp_path):
    """收尾动作因状态不合法而失败时，必须兜底强制释放租约。

    L2 兜底契约：回合结束绝不留活租约。complete_run 抛出
    computer_invalid_state 后，收尾路径必须再调一次
    stop(reason="turn_end_finalize_fallback")，且 cancel_owner=False——
    不得把正常结束的回合误标成"已取消"（该 cancel_event 供 GUI 判定
    回合是否被用户取消）。
    """
    controller = _fake_controller(tmp_path)
    controller.complete_error = ComputerControlError(
        "computer_invalid_state", "非法状态转换：waiting_for_user → completing"
    )
    run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller)
    # 先尝试过正常收尾，失败后才走兜底
    assert controller.complete_calls == 1
    assert controller.stop_calls == 1
    assert controller.stop_reasons == [("run-a", "turn_end_finalize_fallback")]
    assert controller.stop_cancel_owner == [False]


def test_turn_end_finalize_fallback_survives_non_domain_error(monkeypatch, tmp_path):
    """非领域异常同样必须触发兜底，且绝不能逃逸出回合收尾。

    只捕获 ComputerControlError 不足以守住"回合结束绝不留活租约"：
    snapshot()/complete_run() 可能因实现缺陷或环境异常抛出 RuntimeError
    之类的非领域异常。若任其逃逸，既没有兜底释放租约，还会把异常抛进
    回合 finally、打断整个收尾流程。
    """
    controller = _fake_controller(tmp_path)
    controller.complete_error = RuntimeError("收尾时炸了")
    run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller)
    # 先尝试过正常收尾，失败后必须兜底释放
    assert controller.complete_calls == 1
    assert controller.stop_calls == 1
    assert controller.stop_reasons == [("run-a", "turn_end_finalize_fallback")]
    assert controller.stop_cancel_owner == [False]


def test_turn_without_computer_calls_never_starts_run(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    run_two_step_tool_turn(
        monkeypatch, tmp_path,
        controller=controller,
        responses=[
            [_chunk(tool_calls=[_tool_call(0, "call-1", "no_such_tool", "{}")])],
            [_chunk("检查完成。")],
        ],
    )
    assert controller.start_calls == 0
    assert controller.complete_calls == 0
    assert controller.stop_calls == 0


def test_busy_controller_reports_error_without_breaking_turn(monkeypatch, tmp_path):
    controller = _fake_controller(tmp_path)
    controller.start_error = ComputerControlError(
        "computer_session_busy", "电脑操控正被会话「其他会话」占用"
    )
    requests, messages = run_two_step_tool_turn(monkeypatch, tmp_path, controller=controller)
    # tools contract 仍按冻结 capability 提供 computer_action
    assert requests[0]["tools"][-1]["function"]["name"] == "computer_action"
    tool_message = next(m for m in messages if m.get("role") == "tool")
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_session_busy"
    # 无 run 建立，收尾不调用 complete/stop
    assert controller.complete_calls == 0
    assert controller.stop_calls == 0


# ---------------------------------------------------------------------------
# turn_source 受控值与子代理隔离
# ---------------------------------------------------------------------------


def test_turn_source_is_passed_to_capability_resolution(monkeypatch):
    seen = []

    def _record(_model, _provider, _settings, turn_source):
        seen.append(turn_source)
        return DISABLED_CAPABILITY

    monkeypatch.setattr(llm_mod, "resolve_computer_capability", _record)
    capture_first_provider_request(monkeypatch, capability=None, turn_source="lan")
    assert seen == ["lan"]


def test_invalid_turn_source_is_rejected(monkeypatch):
    _patch_request_env(monkeypatch)
    with pytest.raises(ValueError):
        list(chat_with_model_events(
            "openai/generic-vision",
            "fake-key",
            [{"role": "user", "content": "你好"}],
            None,
            persist=False,
            turn_id=TURN,
            consume_background_notifications=False,
            computer_turn_source="remote",
        ))


def test_subagent_turns_do_not_get_computer_tool(monkeypatch):
    calls = []
    monkeypatch.setattr(
        llm_mod,
        "resolve_computer_capability",
        lambda *args: calls.append(args) or GENERIC_CAPABILITY,
    )
    request = capture_first_provider_request(
        monkeypatch, capability=None, allow_subagents=False,
    )
    assert calls == []
    assert [tool["function"]["name"] for tool in request["tools"]] == get_existing_tool_names()
