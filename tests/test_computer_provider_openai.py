"""Task 11a：OpenAI Responses 原生事件解析、target selector 与串行动作执行。

Task 11b：原生 continuation、Provider safety 契约与脱敏持久化投影。

全部 fake 注入：Provider 流、capability 决策与 controller 均为测试替身，
绝不触发真实 Provider 请求、浏览器、截屏或键鼠。本文件还承载 Task 10 遗留
F1（capability 每 Turn 只解析一次）的收敛测试。
"""

import base64
import contextlib
import hashlib
import json
import tempfile
import threading
import unittest.mock as mock
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.llm as llm_mod
import core.model_manager as model_manager
from core.computer.models import (
    ComputerAction,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
)
from core.harness.tool_pipeline import DecodedToolCall
from core.llm import chat_with_model_events
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
    NATIVE_DUAL_TARGET_CAPABILITY,
    FakeController,
    use_temp_config,
)
from tests.request_payload_snapshot import ZETA_TOOL, ALPHA_TOOL

SESSION = "comp-native-session"
TURN = "comp-native-turn"
NATIVE_MODEL_NAME = "openai/gpt-native-fake"
OFFICIAL_OPENAI_BASE = "https://api.openai.com/v1"

# brief Step 1 逐字样例使用的统一动作常量（原生执行串行语义测试）。
MOVE = ComputerAction(kind="move", target="browser", x=10, y=20)
CLICK = ComputerAction(kind="click", target="browser", x=1, y=1)
TYPE_TEXT = ComputerAction(kind="type_text", target="browser", text="hi")

# Task 11b（brief Step 1）：continuation 样例常量（ANY_CALL 在 import 块之后定义）。
NATIVE_TURN_INSTRUCTIONS = "原生回合系统指令：先观察再操作，一次一个动作。"

CREATED_EVENT = {"type": "response.created", "response": {"id": "resp_1"}}
COMPLETED_EVENT = {"type": "response.completed", "response": {"id": "resp_1", "output": []}}
SELECTOR_DONE_EVENT = {
    "type": "response.output_item.done",
    "item": {
        "type": "function_call",
        "call_id": "call_sel",
        "name": "computer_select_target",
        "arguments": json.dumps({"target": "browser"}),
    },
}
COMPUTER_CALL_EVENT = {
    "type": "response.output_item.done",
    "item": {
        "type": "computer_call",
        "id": "cc_1",
        "call_id": "call_1",
        "status": "completed",
        "actions": [{"type": "click", "x": 1, "y": 1, "button": "left"}],
    },
}
COMPLETED_WITH_CALL_EVENT = {
    "type": "response.completed",
    "response": {"id": "resp_1", "output": [deepcopy(COMPUTER_CALL_EVENT["item"])]},
}

# Task 11a 依赖的 provider_openai 公共接口（RED 阶段应 ModuleNotFoundError）。
# Task 11b 新增：continuation/投影/绑定适配器（RED 阶段同样 ImportError）。
from core.computer.provider_openai import (  # noqa: E402
    BoundNativeController,
    NativeComputerCall,
    NativeContinuationState,
    NativeFinalText,
    NativeTargetSelection,
    SafetyCheck,
    build_computer_call_output,
    build_continuation_request,
    build_native_call_persistence_row,
    build_openai_computer_tools,
    execute_native_computer_call,
    execute_reserved_computer_selector,
    native_computer_call_from_stream,
    parse_openai_computer_events,
)

# Task 11b（brief Step 1）：continuation 样例常量（依赖上面的 import）。
ANY_CALL = NativeComputerCall("resp_any", "call_any", (CLICK,), ())
ANY_CALL_OUTPUT = {
    "type": "computer_call_output",
    "call_id": "call_any",
    "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
}


# ---------------------------------------------------------------------------
# 模块内 helper（计划 fixture 契约：Task 11a 的 helper 定义在测试模块内）
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


def _patch_request_env(monkeypatch):
    """受控请求环境：固定 tools schema、openai 协议、无记忆注入。

    与 test_computer_llm_generic 同一套最小桩，供 F1 与原生请求捕获共用。
    """
    monkeypatch.setattr(llm_mod, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL])
    monkeypatch.setattr(
        model_manager, "get_model_protocol", lambda *_args, **_kwargs: "openai"
    )
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section", lambda: ""
    )
    monkeypatch.setattr(
        llm_mod, "_describe_images_with_secondary", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(llm_mod, "estimate_tokens", lambda *_args, **_kwargs: 0)


def _quiet_runtime_context(monkeypatch):
    """屏蔽 prepare_turn_messages 的重依赖（技能/记忆/子代理/后台汇总）。"""
    import core.agent_service as agent_service
    import core.skills as skills
    import core.subagent_registry as subagent_registry

    monkeypatch.setattr(
        agent_service, "build_background_task_summary", lambda session: "", raising=True
    )
    monkeypatch.setattr(skills, "get_active_skills_catalog", lambda: "", raising=True)
    monkeypatch.setattr(
        subagent_registry, "get_custom_subagents_catalog", lambda: "", raising=True
    )


def _computer_call_events(actions, *, call_id="call_1", response_id="resp_1", **item_extra):
    """构造一个完整合法的原生 computer_call 事件序列。"""
    item = {
        "type": "computer_call",
        "id": "cc_1",
        "call_id": call_id,
        "status": "completed",
        "actions": list(actions),
    }
    item.update(item_extra)
    return [
        {"type": "response.created", "response": {"id": response_id}},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {"id": response_id, "output": []}},
    ]


def _decoded_selector(target_value):
    arguments = (
        {"target": target_value} if isinstance(target_value, str) else dict(target_value)
    )
    return DecodedToolCall(
        call_id="call_sel",
        name="computer_select_target",
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )


def _native_selection(tmp_path, *, capability=NATIVE_DUAL_TARGET_CAPABILITY):
    """双 target 原生回合的 selector 状态 + FakeController（含生命周期记录）。"""
    png_path = tmp_path / "observation-native.png"
    png_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-observation")
    owner = ComputerOwner(
        session_id=SESSION,
        turn_id=TURN,
        session_label=SESSION,
        cancel_event=threading.Event(),
    )
    controller = FakeController(
        owner=owner,
        capability=capability,
        run_id="run-native",
        png_path=str(png_path),
    )
    return NativeTargetSelection(capability, owner, controller), controller


class NativeExecutionController:
    """Task 11a 原生串行执行的最小 controller fake（窄接口协议）。

    execute(action) 内完成动作记录、本地 after observation 与并发计数，
    支持 fail_on_ordinal 注入第 N 个动作失败；绝不触碰真实键鼠/截屏。
    """

    def __init__(self, *, fail_on_ordinal=None):
        self.execute_count = 0
        self.attempted_actions = []
        self.observations = []
        self.max_concurrent_actions = 0
        self.fail_on_ordinal = fail_on_ordinal
        self.safety_entries = 0
        self._inside = 0
        self._lock = threading.Lock()

    @property
    def observation_count(self):
        return len(self.observations)

    def execute(self, action):
        with self._lock:
            self._inside += 1
            self.max_concurrent_actions = max(self.max_concurrent_actions, self._inside)
        try:
            self.execute_count += 1
            self.attempted_actions.append(action)
            observation = SimpleNamespace(
                run_id="run-native",
                target=action.target,
                action=action.kind,
                screen_sha256=f"fake-sha-{self.execute_count}",
            )
            self.observations.append(observation)
            if self.fail_on_ordinal is not None and self.execute_count >= self.fail_on_ordinal:
                raise ComputerControlError(
                    "computer_backend_error",
                    f"注入的第 {self.fail_on_ordinal} 个动作失败",
                )
            return observation
        finally:
            with self._lock:
                self._inside -= 1

    def enter_provider_safety_required(self):
        self.safety_entries += 1


@pytest.fixture
def controller():
    return NativeExecutionController()


def _run_native_responses_turn(
    *, capability, responses, controller=None, messages=None, events_out=None
):
    """在受控 responses 环境里跑一个原生回合，返回按序捕获的 Provider 请求。

    capability 显式穿参（F1 参数路径）；resolve_computer_capability 被钉死为
    断言——传参后不得再自解析。events_out 非空时追加收集回合公开事件
    （Task 11b：safety 失败等回合级断言需要 turn_error 事件）。
    """
    captured = []

    def fake_responses(**kwargs):
        captured.append(deepcopy(kwargs))
        return iter(responses[len(captured) - 1])

    patches = [
        # 关键接缝：拦截 litellm responses 入口，否则会发起真实 Provider 请求
        # （与 tests/test_llm_responses.py 的成熟接缝一致）。
        mock.patch.object(llm_mod, "_litellm_responses", fake_responses),
        mock.patch.object(llm_mod, "get_tools_schema", lambda: [ZETA_TOOL, ALPHA_TOOL]),
        mock.patch.object(
            model_manager, "get_model_protocol", lambda *_a, **_k: "responses"
        ),
        mock.patch.object(
            llm_mod, "_describe_images_with_secondary", lambda *_a, **_k: None
        ),
        mock.patch.object(llm_mod, "estimate_tokens", lambda *_a, **_k: 0),
        mock.patch("core.memory_kairos.build_memory_system_section", new=lambda: ""),
        mock.patch.object(
            llm_mod,
            "resolve_computer_capability",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("显式穿参后不得再解析 capability")
            ),
        ),
    ]
    if controller is not None:
        patches.append(
            mock.patch.object(llm_mod, "get_computer_control_service", lambda: controller)
        )
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        turn_events = list(chat_with_model_events(
            NATIVE_MODEL_NAME,
            "fake-key",
            messages or [{"role": "user", "content": "你好"}],
            OFFICIAL_OPENAI_BASE,
            persist=False,
            turn_id=TURN,
            parent_session=SESSION,
            consume_background_notifications=False,
            computer_capability=capability,
        ))
    if events_out is not None:
        events_out.extend(turn_events)
    return captured


_FINAL_TEXT_EVENTS = [
    {"type": "response.output_text.delta", "delta": "完成"},
    {"type": "response.completed", "response": {"id": "resp_cap", "output": []}},
]


def capture_responses_request(*, capability, messages=None):
    """单请求原生回合：返回第一份（也是唯一一份）Responses 请求 kwargs。"""
    captured = _run_native_responses_turn(
        capability=capability, responses=[_FINAL_TEXT_EVENTS], messages=messages
    )
    assert len(captured) == 1
    return captured[0]


# ---------------------------------------------------------------------------
# F1 收敛（Task 10 遗留）：capability 每生产 Turn 只解析一次
# ---------------------------------------------------------------------------


def test_capability_resolved_once_per_production_turn(monkeypatch, tmp_path):
    """生产 Turn = prepare_turn_messages（prompt 侧）+ chat_with_model_events
    （tools 侧）。两条链路共享同一次 resolve_computer_capability 调用：
    agent_service 解析一次后经 Turn handoff 穿参给模型事件入口，
    chat_with_model_events 不再自解析。"""
    from core.agent_service import prepare_turn_messages
    from core.memory import MemoryManager

    _patch_request_env(monkeypatch)
    _quiet_runtime_context(monkeypatch)
    calls = []

    def _counting_resolve(*args, **kwargs):
        calls.append(args)
        return DISABLED_CAPABILITY

    monkeypatch.setattr(llm_mod, "resolve_computer_capability", _counting_resolve)

    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(SESSION)
    manager.add_message(SESSION, "user", "打开浏览器看看页面")
    history = prepare_turn_messages(
        manager,
        SESSION,
        NATIVE_MODEL_NAME,
        128000,
        permission_mode="auto",
    )
    assert len(calls) == 1  # prompt 侧解析一次

    monkeypatch.setattr(
        llm_mod, "completion", lambda **kwargs: iter([_chunk("电脑操控完成。")])
    )
    list(chat_with_model_events(
        NATIVE_MODEL_NAME,
        "fake-key",
        history,
        None,
        persist=False,
        turn_id=TURN,
        consume_background_notifications=False,
    ))
    manager.close()
    assert len(calls) == 1  # tools 侧穿参复用，不再自解析


def test_production_native_capability_receives_entry_api_base(monkeypatch, tmp_path):
    """审查修复轮 I-1：入口层 api_base 必须穿参到 Turn 能力解析。

    F1 收敛把解析移到 prepare 侧后，若 api_base 不穿参，官方 OpenAI 路由的
    native 能力在生产入口永远以 computer_protocol_mismatch 拒绝（fail-closed
    但不可达）。本测试走真实 resolve 链路 + 临时 config，验证入口 api_base
    真实流转为启用的 native 能力快照。
    """
    from core.agent_service import prepare_turn_messages
    from core.computer.capability import take_turn_computer_capability
    from core.memory import MemoryManager

    _quiet_runtime_context(monkeypatch)
    monkeypatch.setattr(llm_mod, "estimate_tokens", lambda *_a, **_k: 0)
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section", lambda: ""
    )
    use_temp_config(tmp_path, monkeypatch, {
        "providers": [{
            "name": "openai-official",
            "base_url": OFFICIAL_OPENAI_BASE,
            "protocol": "responses",
            "models": [{
                "id": "m-native",
                "name": "GPT Native Fake",
                "litellm_model": NATIVE_MODEL_NAME,
                "supports_vision": True,
                "supports_function_calling": True,
                "computer_protocol": "native_openai",
            }],
        }],
        "browser_control_enabled": True,
    })
    # model_manager 在 import 时复制了 config._CONFIG_FILE 的值（既有模式：
    # 同时 patch 两处，见 tests/test_config.py）
    monkeypatch.setattr(
        model_manager, "_CONFIG_FILE",
        str(tmp_path / "config.json"),
    )
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit(SESSION)
    manager.add_message(SESSION, "user", "打开浏览器看看页面")
    prepare_turn_messages(
        manager,
        SESSION,
        NATIVE_MODEL_NAME,
        128000,
        permission_mode="auto",
        api_key="fake-key",
        api_base=OFFICIAL_OPENAI_BASE,
    )
    capability = take_turn_computer_capability()
    manager.close()
    assert capability is not None
    assert capability.enabled is True
    assert capability.protocol == "native_openai"


# ---------------------------------------------------------------------------
# Step 1 样例：受控 built-in 与双 target selector 首请求
# ---------------------------------------------------------------------------


def test_native_first_request_appends_controlled_computer_tool():
    request = capture_responses_request(capability=NATIVE_BROWSER_CAPABILITY)
    assert request["tools"][-1] == {"type": "computer"}
    assert "previous_response_id" not in request


def test_native_dual_target_disables_parallel_tool_calls_and_includes_selector():
    request = capture_responses_request(capability=NATIVE_DUAL_TARGET_CAPABILITY)
    names = [tool.get("name") or tool.get("type") for tool in request["tools"]]
    assert "computer_select_target" in names and "computer" in names
    assert request["parallel_tool_calls"] is False


def test_native_dual_target_selector_schema_is_verbatim():
    request = capture_responses_request(capability=NATIVE_DUAL_TARGET_CAPABILITY)
    assert request["tools"][-2] == {
        "type": "function",
        "name": "computer_select_target",
        "description": "为本次电脑操控选择唯一目标；选择后不可更改。",
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string", "enum": ["browser", "desktop"]}},
            "required": ["target"],
            "additionalProperties": False,
        },
    }


def test_native_single_target_request_has_no_parallel_flag():
    request = capture_responses_request(capability=NATIVE_BROWSER_CAPABILITY)
    assert "parallel_tool_calls" not in request


def test_disabled_or_generic_capability_appends_no_native_tools():
    request = capture_responses_request(capability=DISABLED_CAPABILITY)
    assert request["tools"] == [
        {"type": "function", "name": "zeta", "description": "zeta",
         "parameters": {"type": "object", "properties": {}}},
        {"type": "function", "name": "alpha", "description": "alpha",
         "parameters": {"type": "object", "properties": {}}},
    ]
    generic_request = capture_responses_request(capability=GENERIC_CAPABILITY)
    assert generic_request["tools"][-1] != {"type": "computer"}


@pytest.mark.parametrize(
    "capability,target_selected,expected",
    [
        (NATIVE_BROWSER_CAPABILITY, None, ({"type": "computer"},)),
        (NATIVE_BROWSER_CAPABILITY, "browser", ({"type": "computer"},)),
        (NATIVE_DUAL_TARGET_CAPABILITY, None, ("SELECTOR", {"type": "computer"})),
        (NATIVE_DUAL_TARGET_CAPABILITY, "desktop", ({"type": "computer"},)),
        (GENERIC_CAPABILITY, None, ()),
        (DISABLED_CAPABILITY, None, ()),
        (None, None, ()),
    ],
    ids=["single", "single-selected", "dual-unselected", "dual-selected",
         "generic", "disabled", "none"],
)
def test_build_openai_computer_tools_matrix(capability, target_selected, expected):
    tools = build_openai_computer_tools(capability, target_selected)
    normalized = tuple(
        "SELECTOR" if tool.get("name") == "computer_select_target" else tool
        for tool in tools
    )
    assert normalized == expected
    # 返回的是受控深拷贝，不得暴露可变模块常量
    for tool in tools:
        if tool.get("type") == "computer":
            tool["type"] = "mutated"
            break
    assert build_openai_computer_tools(NATIVE_BROWSER_CAPABILITY, None) == (
        {"type": "computer"},
    )


# ---------------------------------------------------------------------------
# Step 1 样例：事件完整性与 selector 冲突
# ---------------------------------------------------------------------------


def test_selector_and_computer_call_in_same_response_is_protocol_error(controller):
    events = [SELECTOR_DONE_EVENT, COMPUTER_CALL_EVENT]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events)
    assert raised.value.code == "computer_provider_protocol_error"


@pytest.mark.parametrize("event", [
    {"type": "response.output_item.done", "item": {"type": "computer_call", "call_id": "c", "actions": [{"type": "click", "x": 1, "y": 1}]}},
    {"type": "response.completed", "response": {"id": "resp_1", "output": [{"type": "computer_call", "actions": [{"type": "click", "x": 1, "y": 1}]}]}},
], ids=["output-item-no-response-id", "completed-no-call-id"])
def test_missing_response_or_call_id_is_protocol_error(event):
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events([event])
    assert raised.value.code == "computer_provider_protocol_error"


@pytest.mark.parametrize("events", [
    [CREATED_EVENT, SELECTOR_DONE_EVENT, COMPUTER_CALL_EVENT, COMPLETED_EVENT],
    [CREATED_EVENT, COMPUTER_CALL_EVENT, SELECTOR_DONE_EVENT, COMPLETED_EVENT],
    [CREATED_EVENT, SELECTOR_DONE_EVENT, COMPLETED_WITH_CALL_EVENT],
], ids=["selector-first", "call-first", "selector-with-completed-output"])
def test_selector_computer_coexistence_is_order_independent(events):
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"


def test_parse_valid_computer_call_maps_actions_to_unified():
    result = parse_openai_computer_events(
        [CREATED_EVENT, COMPUTER_CALL_EVENT, COMPLETED_WITH_CALL_EVENT],
        target="browser",
    )
    assert result == NativeComputerCall(
        "resp_1",
        "call_1",
        (ComputerAction(kind="click", target="browser", x=1, y=1, button="left"),),
        (),
    )


@pytest.mark.parametrize("official,unified", [
    ({"type": "screenshot"}, ComputerAction(kind="observe", target="browser")),
    ({"type": "click", "x": 5, "y": 6}, ComputerAction(kind="click", target="browser", x=5, y=6)),
    ({"type": "click", "x": 5, "y": 6, "button": "wheel", "keys": ["CTRL"]},
     ComputerAction(kind="click", target="browser", x=5, y=6, button="middle", keys=("CTRL",))),
    ({"type": "double_click", "x": 1, "y": 2},
     ComputerAction(kind="double_click", target="browser", x=1, y=2)),
    ({"type": "move", "x": 3, "y": 4}, ComputerAction(kind="move", target="browser", x=3, y=4)),
    ({"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": -3},
     ComputerAction(kind="scroll", target="browser", x=1, y=2, delta_y=-3, unit="css_pixel")),
    ({"type": "type", "text": "hi"}, ComputerAction(kind="type_text", target="browser", text="hi")),
    ({"type": "keypress", "keys": ["CTRL", "A"]},
     ComputerAction(kind="keypress", target="browser", keys=("CTRL", "A"))),
    ({"type": "keypress", "keys": ["RETURN", "ARROWLEFT"]},
     ComputerAction(kind="keypress", target="browser", keys=("ENTER", "LEFT"))),
    ({"type": "wait"}, ComputerAction(kind="wait", target="browser", duration_ms=2000)),
    ({"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}], "keys": ["SHIFT"]},
     ComputerAction(kind="drag", target="browser", path=((1, 2), (3, 4)), keys=("SHIFT",))),
], ids=["screenshot", "click", "click-wheel-ctrl", "double-click", "move", "scroll",
        "type", "keypress", "key-alias", "wait", "drag"])
def test_official_action_mapping(official, unified):
    result = parse_openai_computer_events(
        _computer_call_events([official]), target="browser"
    )
    assert isinstance(result, NativeComputerCall)
    assert result.actions == (unified,)


def test_unknown_official_action_type_is_protocol_error():
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events([{"type": "teleport", "x": 1}]), target="browser"
        )
    assert raised.value.code == "computer_provider_protocol_error"


def test_unknown_official_action_field_is_protocol_error():
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events([{"type": "click", "x": 1, "y": 2, "speed": 9}]),
            target="browser",
        )
    assert raised.value.code == "computer_provider_protocol_error"


def test_official_unsupported_button_rejected_by_unified_schema():
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events([{"type": "click", "x": 1, "y": 2, "button": "back"}]),
            target="browser",
        )
    assert raised.value.code == "computer_invalid_action"


def test_official_move_with_keys_rejected_by_unified_schema():
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events([{"type": "move", "x": 1, "y": 2, "keys": ["CTRL"]}]),
            target="browser",
        )
    assert raised.value.code == "computer_invalid_action"


@pytest.mark.parametrize("actions", [[], "not-a-list", None], ids=["empty", "string", "missing"])
def test_invalid_actions_container_is_protocol_error(actions):
    item = {
        "type": "computer_call",
        "call_id": "call_1",
        "status": "completed",
        "actions": actions,
    }
    events = [
        CREATED_EVENT,
        {"type": "response.output_item.done", "item": item},
        COMPLETED_EVENT,
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"


def test_preview_singular_action_item_is_rejected():
    item = {
        "type": "computer_call",
        "call_id": "call_1",
        "action": {"type": "click", "x": 1, "y": 1},
    }
    events = [
        CREATED_EVENT,
        {"type": "response.output_item.done", "item": item},
        COMPLETED_EVENT,
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"


def test_multiple_distinct_computer_calls_are_protocol_error():
    second = dict(COMPUTER_CALL_EVENT["item"], call_id="call_2")
    events = [
        CREATED_EVENT,
        COMPUTER_CALL_EVENT,
        {"type": "response.output_item.done", "item": second},
        COMPLETED_EVENT,
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"


def test_computer_call_without_selected_target_is_rejected():
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            [CREATED_EVENT, COMPUTER_CALL_EVENT, COMPLETED_EVENT], target=None
        )
    assert raised.value.code == "computer_invalid_state"


def test_parse_captures_pending_safety_checks():
    events = _computer_call_events(
        [{"type": "screenshot"}],
        pending_safety_checks=[
            {"id": "sc_1", "code": "url_match", "message": "访问的地址与任务不符"}
        ],
    )
    result = parse_openai_computer_events(events, target="browser")
    assert result.pending_safety_checks == (
        SafetyCheck(id="sc_1", code="url_match", message="访问的地址与任务不符"),
    )


def test_response_failed_terminal_is_distinct_protocol_error():
    events = [
        CREATED_EVENT,
        COMPUTER_CALL_EVENT,
        {"type": "response.failed", "response": {"id": "resp_1", "error": {"message": "boom"}}},
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"
    assert "boom" in raised.value.message
    assert raised.value.details.get("terminal_state") == "failed"


def test_response_incomplete_terminal_is_distinct_protocol_error():
    events = [
        CREATED_EVENT,
        COMPUTER_CALL_EVENT,
        {"type": "response.incomplete", "response": {
            "id": "resp_1", "incomplete_details": {"reason": "max_output_tokens"}}},
    ]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"
    assert "max_output_tokens" in raised.value.message
    assert raised.value.details.get("terminal_state") == "incomplete"


def test_missing_terminal_event_is_protocol_error():
    events = [{"type": "response.output_text.delta", "delta": "hi"}]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events, target="browser")
    assert raised.value.code == "computer_provider_protocol_error"


def test_final_text_ends_native_branch():
    events = [
        {"type": "response.output_text.delta", "delta": "任务完成"},
        {"type": "response.completed", "response": {"id": "resp_9", "output": []}},
    ]
    result = parse_openai_computer_events(events, target="browser")
    assert result == NativeFinalText(text="任务完成")


# ---------------------------------------------------------------------------
# Step 1 样例：串行动作执行与 Provider 强制安全检查
# ---------------------------------------------------------------------------


def test_pending_safety_blocks_before_execution(controller):
    call = NativeComputerCall("resp_1", "call_1", (CLICK,), (SafetyCheck("sc_1", "reason"),))
    result = execute_native_computer_call(call, controller)
    assert result.status == "provider_safety_required"
    assert controller.execute_count == 0


def test_pending_safety_enters_provider_safety_state(controller):
    call = NativeComputerCall("resp_1", "call_1", (CLICK,), (SafetyCheck("sc_1", "reason"),))
    execute_native_computer_call(call, controller)
    assert controller.safety_entries == 1
    assert controller.attempted_actions == []


def test_native_actions_are_serial_and_stop_after_first_failure(controller):
    controller.fail_on_ordinal = 2  # 第二个动作（CLICK）执行时失败
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK, TYPE_TEXT), ())
    with pytest.raises(ComputerControlError):
        execute_native_computer_call(call, controller)
    assert controller.attempted_actions == [MOVE, CLICK]  # 失败后 TYPE_TEXT 不再执行
    assert controller.max_concurrent_actions == 1
    assert controller.observation_count == 2  # 每个动作都有本地 after observation


def test_native_actions_all_succeed_return_last_observation(controller):
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK), ())
    result = execute_native_computer_call(call, controller)
    assert result.status == "ok"
    assert result.observation is controller.observations[-1]
    assert controller.attempted_actions == [MOVE, CLICK]


# ---------------------------------------------------------------------------
# assembler 合并的终态 payload → NativeComputerCall（禁止半截 JSON 执行）
# ---------------------------------------------------------------------------


def test_native_call_from_assembled_stream_payload():
    payload = {
        "response_id": "resp_1",
        "item": deepcopy(COMPUTER_CALL_EVENT["item"]),
    }
    call = native_computer_call_from_stream(payload, target="browser")
    assert call == NativeComputerCall(
        "resp_1",
        "call_1",
        (ComputerAction(kind="click", target="browser", x=1, y=1, button="left"),),
        (),
    )


def test_native_call_from_stream_payload_missing_ids_is_protocol_error():
    with pytest.raises(ComputerControlError) as raised:
        native_computer_call_from_stream(
            {"response_id": "", "item": deepcopy(COMPUTER_CALL_EVENT["item"])},
            target="browser",
        )
    assert raised.value.code == "computer_provider_protocol_error"


# ---------------------------------------------------------------------------
# selector 固定 adapter：租约建立与明确失败
# ---------------------------------------------------------------------------


def test_selector_first_call_writes_target_lease(tmp_path):
    selection, controller = _native_selection(tmp_path)
    result = execute_reserved_computer_selector(_decoded_selector("browser"), selection)
    payload = json.loads(result.content)
    assert payload["ok"] is True
    assert payload["target"] == "browser"
    assert selection.target == "browser"
    assert selection.run_id == "run-native"
    assert controller.start_calls == 1
    assert controller.start_targets == ["browser"]


def test_selector_duplicate_selection_fails(tmp_path):
    selection, controller = _native_selection(tmp_path)
    first = execute_reserved_computer_selector(_decoded_selector("browser"), selection)
    assert json.loads(first.content)["ok"] is True
    second = execute_reserved_computer_selector(_decoded_selector("browser"), selection)
    payload = json.loads(second.content)
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_invalid_action"
    assert controller.start_calls == 1


def test_selector_change_selection_fails(tmp_path):
    selection, controller = _native_selection(tmp_path)
    execute_reserved_computer_selector(_decoded_selector("browser"), selection)
    changed = execute_reserved_computer_selector(_decoded_selector("desktop"), selection)
    payload = json.loads(changed.content)
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_invalid_action"
    assert controller.start_calls == 1


def test_selector_target_not_enabled_fails(tmp_path):
    selection, controller = _native_selection(tmp_path, capability=NATIVE_BROWSER_CAPABILITY)
    result = execute_reserved_computer_selector(_decoded_selector("desktop"), selection)
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_disabled"
    assert controller.start_calls == 0


def test_selector_invalid_target_value_fails(tmp_path):
    selection, controller = _native_selection(tmp_path)
    result = execute_reserved_computer_selector(
        _decoded_selector({"nope": 1}), selection
    )
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_invalid_action"
    assert controller.start_calls == 0


def test_selector_without_selection_context_fails():
    result = execute_reserved_computer_selector(_decoded_selector("browser"), None)
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_disabled"


# ---------------------------------------------------------------------------
# 集成：selector 单独完成一个 Step，选择后 selector 从后续请求消失
# ---------------------------------------------------------------------------


def test_native_selector_step_writes_lease_and_drops_selector_from_next_request(tmp_path):
    selection, controller = _native_selection(tmp_path)
    selector_step_events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "call_sel",
                  "name": "computer_select_target"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0,
         "delta": '{"target": "browser"}'},
        {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
    ]
    final_text_events = [
        {"type": "response.output_text.delta", "delta": "已选择浏览器。"},
        {"type": "response.completed", "response": {"id": "resp_2", "output": []}},
    ]
    captured = _run_native_responses_turn(
        capability=NATIVE_DUAL_TARGET_CAPABILITY,
        responses=[selector_step_events, final_text_events],
        controller=controller,
    )
    assert len(captured) == 2
    first_names = [tool.get("name") or tool.get("type") for tool in captured[0]["tools"]]
    assert first_names[-2:] == ["computer_select_target", "computer"]
    assert captured[0]["parallel_tool_calls"] is False
    second_names = [tool.get("name") or tool.get("type") for tool in captured[1]["tools"]]
    assert "computer_select_target" not in second_names
    assert second_names[-1] == "computer"
    assert captured[1]["parallel_tool_calls"] is False
    # selector 建立了 target 租约，Turn 正常收尾 complete_run 不留 running
    assert controller.start_calls == 1
    assert controller.start_targets == ["browser"]
    assert controller.complete_calls == 1
    assert controller.completed_run_ids == ["run-native"]
    assert controller.stop_calls == 0


# ---------------------------------------------------------------------------
# Task 11b：原生 continuation、Provider safety 契约与脱敏持久化投影
# ---------------------------------------------------------------------------


class SafetyAwareFakeController(FakeController):
    """FakeController + enter_provider_safety_required 记录（11b safety 契约）。

    真实 ComputerControlService.enter_provider_safety_required(run_id) 会把 run
    转入 provider_safety_required；fake 同步记录次数与状态供断言。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.safety_entries = 0

    def enter_provider_safety_required(self, run_id):
        self.safety_entries += 1
        self.run_status = "provider_safety_required"
        return self.snapshot()


def _native_png_controller(tmp_path, *, capability=NATIVE_BROWSER_CAPABILITY):
    """单 target 原生 live-loop 回合的 FakeController（png 指向临时文件）。"""
    png_path = Path(tmp_path) / "obs-native.png"
    png_path.write_bytes(b"png")
    owner = ComputerOwner(
        session_id=SESSION,
        turn_id=TURN,
        session_label=SESSION,
        cancel_event=threading.Event(),
    )
    return FakeController(
        owner=owner,
        capability=capability,
        run_id="run-native",
        png_path=str(png_path),
    )


def turn_state_without_response_id():
    """构造缺 response id 的续传状态（brief Step 1：Provider 未返回可用 id）。"""
    return NativeContinuationState(
        model=NATIVE_MODEL_NAME,
        tools=({"type": "computer"},),
        instructions=NATIVE_TURN_INSTRUCTIONS,
        options={},
        response_id="",
    )


def capture_continuation_request(*, response_id="resp_1", call_id="call_1", png=b"png"):
    """brief Step 1 样例 helper：跑一个 computer call + 续传 + 最终文本回合。

    返回第二个（continuation）请求的捕获 kwargs。自管理临时 PNG（观测截图），
    与 brief 的零 fixture 签名保持一致。
    """
    with tempfile.TemporaryDirectory() as tmp:
        png_path = Path(tmp) / "obs-native.png"
        png_path.write_bytes(png)
        owner = ComputerOwner(
            session_id=SESSION,
            turn_id=TURN,
            session_label=SESSION,
            cancel_event=threading.Event(),
        )
        controller = FakeController(
            owner=owner,
            capability=NATIVE_BROWSER_CAPABILITY,
            run_id="run-native",
            png_path=str(png_path),
        )
        captured = _run_native_responses_turn(
            capability=NATIVE_BROWSER_CAPABILITY,
            responses=[
                _computer_call_events(
                    [{"type": "click", "x": 1, "y": 1}],
                    call_id=call_id,
                    response_id=response_id,
                ),
                _FINAL_TEXT_EVENTS,
            ],
            controller=controller,
            messages=[
                {"role": "system", "content": NATIVE_TURN_INSTRUCTIONS},
                {"role": "user", "content": "打开页面看看"},
            ],
        )
        assert len(captured) == 2
        return captured[1]


def test_native_continuation_resends_model_tools_and_previous_id():
    request = capture_continuation_request(response_id="resp_1", call_id="call_1", png=b"png")
    assert request["model"] == NATIVE_MODEL_NAME  # 重新发送同一 model
    assert {"type": "computer"} in request["tools"]  # 重新发送同序 tools
    assert request["previous_response_id"] == "resp_1"
    assert request["instructions"] == NATIVE_TURN_INSTRUCTIONS  # instructions 不自动继承，必须重发
    assert request["input"] == [{
        "type": "computer_call_output",
        "call_id": "call_1",
        "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
    }]  # 不重发完整历史，input 只有本批 call output
    assert "messages" not in request


def test_provider_returns_no_response_id_fails_without_fallback():
    with pytest.raises(ComputerControlError) as raised:
        build_continuation_request(turn_state_without_response_id(), ANY_CALL_OUTPUT)
    assert raised.value.code == "computer_provider_protocol_error"


def test_call_output_never_invents_safety_acknowledgement_fields(tmp_path):
    png_path = Path(tmp_path) / "any-obs.png"
    png_path.write_bytes(b"png")
    observation = ComputerObservation(
        run_id="run-native",
        target="browser",
        action="click",
        screen_sha256="sha-any",
        png_path=str(png_path),
        width=8,
        height=6,
    )
    output = build_computer_call_output(ANY_CALL, observation)
    assert "acknowledged_safety_checks" not in output  # GA schema 未确认前不得出现
    assert "acknowledged_safety_checks" not in output["output"]
    assert output["call_id"] == "call_any"


def test_provider_missing_response_id_fails_turn_without_resending_history():
    """流内 computer_call 载荷缺 response id：回合立即失败，没有第二次请求。"""
    call_events = [
        {"type": "response.output_item.done", "item": {
            "type": "computer_call",
            "call_id": "call_1",
            "status": "completed",
            "actions": [{"type": "click", "x": 1, "y": 1}],
        }},
        {"type": "response.completed", "response": {"id": "", "output": []}},
    ]
    turn_events = []
    captured = _run_native_responses_turn(
        capability=NATIVE_BROWSER_CAPABILITY,
        responses=[call_events],
        events_out=turn_events,
    )
    # 唯一一次请求后立即失败：没有续传，也没有"重发全部截图历史"或 generic 请求
    assert len(captured) == 1
    assert any(event.kind == "turn_error" for event in turn_events)


def test_native_computer_call_turn_executes_via_bound_adapter_and_continues(tmp_path):
    """live loop 消费 assembled.computer_call：经绑定 run_id/owner 的窄协议
    适配器串行执行，随后以 computer_call_output 续传；Turn 收尾 complete_run。"""
    controller = _native_png_controller(tmp_path)
    captured = _run_native_responses_turn(
        capability=NATIVE_BROWSER_CAPABILITY,
        responses=[
            _computer_call_events([
                {"type": "move", "x": 10, "y": 20},
                {"type": "click", "x": 1, "y": 1},
            ]),
            _FINAL_TEXT_EVENTS,
        ],
        controller=controller,
    )
    assert len(captured) == 2
    # 审查 I-2 契约：真实服务签名 execute(run_id, owner, action) 由适配器绑定，
    # 绝不把 FakeController/真实服务直传给 execute_native_computer_call。
    assert controller.execute_calls == [
        ("run-native", "move", "browser"),
        ("run-native", "click", "browser"),
    ]
    # 惰性 start_run：首个原生 computer call 执行前建立（单 target 直取唯一 target）
    assert controller.start_calls == 1
    assert controller.start_targets == ["browser"]
    # Turn 终态 complete_run，不留 running 租约
    assert controller.complete_calls == 1
    assert controller.completed_run_ids == ["run-native"]
    assert controller.stop_calls == 0


def test_native_chained_continuation_two_calls_then_final_text(tmp_path):
    """链式续传（11b 审查建议）：computer_call→computer_call→final text。

    两个动作 Step 各自以上一 response id 续传（绝不跨响应复用旧 id），
    input 每次只有上一批 call 的 computer_call_output；最终文本清空续传
    状态并正常收尾（complete_run，不留 running 租约）。
    """
    controller = _native_png_controller(tmp_path)
    first = _computer_call_events(
        [{"type": "click", "x": 1, "y": 1}], call_id="call_1", response_id="resp_1"
    )
    second = _computer_call_events(
        [{"type": "screenshot"}], call_id="call_2", response_id="resp_2"
    )
    captured = _run_native_responses_turn(
        capability=NATIVE_BROWSER_CAPABILITY,
        responses=[first, second, _FINAL_TEXT_EVENTS],
        controller=controller,
    )
    assert len(captured) == 3
    # 第二请求：以 resp_1 续传，input 只有 call_1 的输出
    assert captured[1]["previous_response_id"] == "resp_1"
    assert captured[1]["input"] == [{
        "type": "computer_call_output",
        "call_id": "call_1",
        "output": {
            "type": "computer_screenshot",
            "image_url": "data:image/png;base64," + base64.b64encode(b"png").decode("ascii"),
        },
    }]
    # 第三请求：以 resp_2 续传（不是 resp_1），input 只有 call_2 的输出
    assert captured[2]["previous_response_id"] == "resp_2"
    assert captured[2]["input"][0]["call_id"] == "call_2"
    # 两个动作都经绑定适配器执行；run 只建立一次，最终 complete 收尾
    assert controller.execute_calls == [
        ("run-native", "click", "browser"),
        ("run-native", "observe", "browser"),
    ]
    assert controller.start_calls == 1
    assert controller.complete_calls == 1
    assert controller.stop_calls == 0


def test_native_continuation_after_selector_drops_selector_from_tools(tmp_path):
    """selector 已选后，continuation 重发的同序 tools 不再含 selector。"""
    selection, controller = _native_selection(tmp_path)
    selector_step_events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "call_sel",
                  "name": "computer_select_target"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0,
         "delta": '{"target": "browser"}'},
        {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
    ]
    call_events = _computer_call_events(
        [{"type": "screenshot"}], response_id="resp_2"
    )
    captured = _run_native_responses_turn(
        capability=NATIVE_DUAL_TARGET_CAPABILITY,
        responses=[selector_step_events, call_events, _FINAL_TEXT_EVENTS],
        controller=controller,
    )
    assert len(captured) == 3
    continuation = captured[2]
    names = [tool.get("name") or tool.get("type") for tool in continuation["tools"]]
    assert "computer_select_target" not in names
    assert names[-1] == "computer"
    assert continuation["previous_response_id"] == "resp_2"
    assert continuation["parallel_tool_calls"] is False
    # selector 建立的租约被复用（不二次 start_run），动作经绑定适配器执行
    assert controller.start_calls == 1
    assert controller.execute_calls == [("run-native", "observe", "browser")]
    assert controller.complete_calls == 1


def test_provider_safety_required_stops_turn_without_further_step(tmp_path):
    """pending safety：不执行动作、不发起后续 Step，controller 停在
    provider_safety_required 等用户显式停止（设计 11.5）。"""
    png_path = Path(tmp_path) / "obs-native.png"
    png_path.write_bytes(b"png")
    owner = ComputerOwner(
        session_id=SESSION,
        turn_id=TURN,
        session_label=SESSION,
        cancel_event=threading.Event(),
    )
    controller = SafetyAwareFakeController(
        owner=owner,
        capability=NATIVE_BROWSER_CAPABILITY,
        run_id="run-native",
        png_path=str(png_path),
    )
    safety_events = _computer_call_events(
        [{"type": "click", "x": 1, "y": 1}],
        pending_safety_checks=[
            {"id": "sc_1", "code": "url_match", "message": "访问的地址与任务不符"}
        ],
    )
    turn_events = []
    captured = _run_native_responses_turn(
        capability=NATIVE_BROWSER_CAPABILITY,
        responses=[safety_events],
        controller=controller,
        events_out=turn_events,
    )
    # 后续 Step 不发起：唯一一次请求就是携带 computer_call 的那次
    assert len(captured) == 1
    # 未确认绝不执行动作，也不伪造 acknowledged safety checks
    assert controller.execute_calls == []
    assert controller.safety_entries == 1
    # controller 停在 provider_safety_required：Turn 收尾不 complete 也不 stop
    assert controller.run_status == "provider_safety_required"
    assert controller.complete_calls == 0
    assert controller.stop_calls == 0
    serialized_events = json.dumps(
        [event.payload for event in turn_events],
        ensure_ascii=False,
        default=str,
    )
    assert any(
        event.kind == "turn_error" and "安全检查" in str(event.payload.get("message", ""))
        for event in turn_events
    )
    # safety check 原文只留在解析值对象里，不进入任何公开事件
    assert "访问的地址与任务不符" not in serialized_events


def test_computer_call_mixed_with_function_calls_is_protocol_error(tmp_path):
    """computer_call 与普通 function call 同响应：整批拒绝，零执行。"""
    controller = _native_png_controller(tmp_path)
    mixed_events = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0,
         "item": {"type": "function_call", "call_id": "call_fn", "name": "zeta"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0,
         "delta": "{}"},
        {"type": "response.output_item.done", "item": {
            "type": "computer_call",
            "call_id": "call_1",
            "status": "completed",
            "actions": [{"type": "click", "x": 1, "y": 1}],
        }},
        {"type": "response.completed", "response": {"id": "resp_1", "output": []}},
    ]
    turn_events = []
    captured = _run_native_responses_turn(
        capability=NATIVE_BROWSER_CAPABILITY,
        responses=[mixed_events],
        controller=controller,
        events_out=turn_events,
    )
    assert len(captured) == 1
    assert controller.start_calls == 0
    assert controller.execute_calls == []
    assert any(event.kind == "turn_error" for event in turn_events)


def test_native_intermediate_row_projection_is_sanitized():
    """brief Step 5 逐字形状 + 脱敏：输入文字只存 code point 数与 SHA-256，
    截图只存 screen hash/尺寸；无 base64、完整 URL、response id。"""
    actions = (
        ComputerAction(kind="type_text", target="browser", text="秘密输入内容"),
        ComputerAction(kind="click", target="browser", x=3, y=4),
    )
    call = NativeComputerCall("resp_secret", "call_secret", actions, ())
    observation = ComputerObservation(
        run_id="run-native",
        target="browser",
        action="click",
        screen_sha256="sha-screen-1",
        png_path="C:/secret/profile-path/obs.png",
        width=1440,
        height=900,
    )
    row = build_native_call_persistence_row("run-native", call, observation)
    assert row["role"] == "assistant"
    assert row["content"] == ""
    projection = row["_computer_v1"]
    assert projection["kind"] == "native_call"
    assert projection["run_id"] == "run-native"
    assert projection["call_id_hash"] == hashlib.sha256(b"call_secret").hexdigest()
    assert projection["action"] == [
        {
            "kind": "type_text",
            "target": "browser",
            "text_code_points": 6,
            "text_sha256": hashlib.sha256("秘密输入内容".encode("utf-8")).hexdigest(),
        },
        {
            "kind": "click",
            "target": "browser",
            "x": 3,
            "y": 4,
            "screen_sha256": "sha-screen-1",
            "screen_width": 1440,
            "screen_height": 900,
        },
    ]
    serialized = json.dumps(row, ensure_ascii=False)
    assert "秘密输入内容" not in serialized  # 输入正文绝不持久化
    assert "resp_secret" not in serialized  # response id 绝不持久化
    assert "call_secret" not in serialized  # call id 只存 hash
    assert "profile-path" not in serialized  # 本地路径不持久化
    # 确定性投影：同一输入两次构造完全一致
    assert row == build_native_call_persistence_row("run-native", call, observation)


def test_bound_native_controller_binds_run_and_owner(tmp_path):
    """窄协议适配器把 run_id/owner 绑定到真实服务签名（审查 I-2 契约）。"""
    png_path = Path(tmp_path) / "obs-native.png"
    png_path.write_bytes(b"png")
    owner = ComputerOwner(
        session_id=SESSION,
        turn_id=TURN,
        session_label=SESSION,
        cancel_event=threading.Event(),
    )
    controller = SafetyAwareFakeController(
        owner=owner,
        capability=NATIVE_BROWSER_CAPABILITY,
        run_id="run-native",
        png_path=str(png_path),
    )
    bound = BoundNativeController(controller, "run-native", owner)
    observation = bound.execute(CLICK)
    assert observation.action == "click"
    assert controller.execute_calls == [("run-native", "click", "browser")]
    bound.enter_provider_safety_required()
    assert controller.safety_entries == 1
    assert controller.run_status == "provider_safety_required"


def test_native_scroll_on_desktop_target_is_explicitly_rejected():
    """R7：原生像素滚动在桌面 target 明确拒绝（协议错误），绝不静默近似为滚轮步进。"""
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events(
                [{"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300}]
            ),
            target="desktop",
        )
    assert raised.value.code == "computer_provider_protocol_error"
    assert "像素" in raised.value.message
