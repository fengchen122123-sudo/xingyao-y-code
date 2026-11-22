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

MOVE = ComputerAction(kind="move", target="browser", x=10, y=20)
CLICK = ComputerAction(kind="click", target="browser", x=1, y=1)
TYPE_TEXT = ComputerAction(kind="type_text", target="browser", text="hi")

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

ANY_CALL = NativeComputerCall("resp_any", "call_any", (CLICK,), ())
ANY_CALL_OUTPUT = {
    "type": "computer_call_output",
    "call_id": "call_any",
    "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
}


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
    captured = []

    def fake_responses(**kwargs):
        captured.append(deepcopy(kwargs))
        return iter(responses[len(captured) - 1])

    patches = [
        # 关键接缝：拦截 Responses 协议入口，否则会发起真实 Provider 请求
        # （与 tests/test_llm_responses.py 的成熟接缝一致）。
        mock.patch.object(llm_mod, "_dispatch_responses", fake_responses),
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
    captured = _run_native_responses_turn(
        capability=capability, responses=[_FINAL_TEXT_EVENTS], messages=messages
    )
    assert len(captured) == 1
    return captured[0]


def test_capability_resolved_once_per_production_turn(monkeypatch, tmp_path):
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
    assert len(calls) == 1

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
    assert len(calls) == 1


def test_production_native_capability_receives_entry_api_base(monkeypatch, tmp_path):
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
    for tool in tools:
        if tool.get("type") == "computer":
            tool["type"] = "mutated"
            break
    assert build_openai_computer_tools(NATIVE_BROWSER_CAPABILITY, None) == (
        {"type": "computer"},
    )


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
    controller.fail_on_ordinal = 2
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK, TYPE_TEXT), ())
    with pytest.raises(ComputerControlError):
        execute_native_computer_call(call, controller)
    assert controller.attempted_actions == [MOVE, CLICK]
    assert controller.max_concurrent_actions == 1
    assert controller.observation_count == 2


def test_native_actions_all_succeed_return_last_observation(controller):
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK), ())
    result = execute_native_computer_call(call, controller)
    assert result.status == "ok"
    assert result.observation is controller.observations[-1]
    assert controller.attempted_actions == [MOVE, CLICK]


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
    assert controller.start_calls == 1
    assert controller.start_targets == ["browser"]
    assert controller.complete_calls == 1
    assert controller.completed_run_ids == ["run-native"]
    assert controller.stop_calls == 0


# ---------------------------------------------------------------------------
# Task 11b：原生 continuation、Provider safety 契约与脱敏持久化投影
# ---------------------------------------------------------------------------


class SafetyAwareFakeController(FakeController):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.safety_entries = 0

    def enter_provider_safety_required(self, run_id):
        self.safety_entries += 1
        self.run_status = "provider_safety_required"
        return self.snapshot()


def _native_png_controller(tmp_path, *, capability=NATIVE_BROWSER_CAPABILITY):
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
    return NativeContinuationState(
        model=NATIVE_MODEL_NAME,
        tools=({"type": "computer"},),
        instructions=NATIVE_TURN_INSTRUCTIONS,
        options={},
        response_id="",
    )


def capture_continuation_request(*, response_id="resp_1", call_id="call_1", png=b"png"):
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
    assert request["model"] == NATIVE_MODEL_NAME
    assert {"type": "computer"} in request["tools"]
    assert request["previous_response_id"] == "resp_1"
    assert request["instructions"] == NATIVE_TURN_INSTRUCTIONS
    assert request["input"] == [{
        "type": "computer_call_output",
        "call_id": "call_1",
        "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
    }]
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
    assert "acknowledged_safety_checks" not in output
    assert "acknowledged_safety_checks" not in output["output"]
    assert output["call_id"] == "call_any"


def test_call_output_without_screenshot_sends_text_observation():
    observation = ComputerObservation(
        run_id="run-native",
        target="browser",
        action="observe",
        page_text="页面正文第一段",
        screenshot_error="浏览器窗口不可见或被遮挡，本次未取到截图：TimeoutError",
    )
    output = build_computer_call_output(ANY_CALL, observation)
    assert output["type"] == "computer_call_output"
    assert output["call_id"] == "call_any"
    assert isinstance(output["output"], list)
    assert all(item["type"] == "input_text" for item in output["output"])
    text = output["output"][0]["text"]
    assert "本次没有截图" in text
    assert "TimeoutError" in text
    assert "页面正文第一段" in text


def test_provider_missing_response_id_fails_turn_without_resending_history():
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
    assert len(captured) == 1
    assert any(event.kind == "turn_error" for event in turn_events)


def test_native_computer_call_turn_executes_via_bound_adapter_and_continues(tmp_path):
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
    assert controller.start_calls == 1
    assert controller.start_targets == ["browser"]
    assert controller.complete_calls == 1
    assert controller.completed_run_ids == ["run-native"]
    assert controller.stop_calls == 0


def test_native_chained_continuation_two_calls_then_final_text(tmp_path):
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
    assert captured[1]["previous_response_id"] == "resp_1"
    assert captured[1]["input"] == [{
        "type": "computer_call_output",
        "call_id": "call_1",
        "output": {
            "type": "computer_screenshot",
            "image_url": "data:image/png;base64," + base64.b64encode(b"png").decode("ascii"),
        },
    }]
    assert captured[2]["previous_response_id"] == "resp_2"
    assert captured[2]["input"][0]["call_id"] == "call_2"
    assert controller.execute_calls == [
        ("run-native", "click", "browser"),
        ("run-native", "observe", "browser"),
    ]
    assert controller.start_calls == 1
    assert controller.complete_calls == 1
    assert controller.stop_calls == 0


def test_native_continuation_after_selector_drops_selector_from_tools(tmp_path):
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
    assert controller.start_calls == 1
    assert controller.execute_calls == [("run-native", "observe", "browser")]
    assert controller.complete_calls == 1


def test_provider_safety_required_stops_turn_without_further_step(tmp_path):
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
    assert len(captured) == 1
    # 未确认绝不执行动作，也不伪造 acknowledged safety checks
    assert controller.execute_calls == []
    assert controller.safety_entries == 1
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
    assert "访问的地址与任务不符" not in serialized_events


def test_computer_call_mixed_with_function_calls_is_protocol_error(tmp_path):
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
    assert "call_secret" not in serialized
    assert "profile-path" not in serialized
    assert row == build_native_call_persistence_row("run-native", call, observation)


def test_bound_native_controller_binds_run_and_owner(tmp_path):
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
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(
            _computer_call_events(
                [{"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300}]
            ),
            target="desktop",
        )
    assert raised.value.code == "computer_provider_protocol_error"
    assert "像素" in raised.value.message


def test_native_call_output_syncs_downscaled_frame_transform(tmp_path, monkeypatch):
    import io
    from PIL import Image
    from core.computer import provider_generic
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.controller import ComputerControlService
    from tests.computer_fakes import FakePlaywrightRuntime

    from core.computer.settings import ComputerSettingsSnapshot

    buf = io.BytesIO()
    Image.new("RGB", (2560, 1440), (10, 20, 30)).save(buf, format="PNG")
    big_png = buf.getvalue() + (b"\x00" * 128)
    runtime = FakePlaywrightRuntime(screenshot_bytes=big_png)
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        viewport=(2560, 1440),
    )
    owner = ComputerOwner(SESSION, TURN, SESSION, threading.Event())
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        settings_loader=lambda: ComputerSettingsSnapshot(
            browser_control_enabled=True,
            computer_control_enabled=False,
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=True,
            generation=0,
        ),
        generation_provider=lambda: 0,
    )
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 5000)
    snap = None
    try:
        snap = service.start_run(owner, NATIVE_BROWSER_CAPABILITY, target="browser")
        obs = service.execute(
            snap.run_id, owner, ComputerAction(kind="observe", target="browser")
        )
        output = build_computer_call_output(ANY_CALL, obs, controller=service)
        assert output["output"]["type"] == "computer_screenshot"
        frame = service.get_observation_frame(snap.run_id, obs.frame_id)
        assert frame is not None
        assert frame.image_width == 1280
        assert frame.image_height == 720
        assert frame.model_to_css_x == pytest.approx(2.0)
        assert frame.model_to_css_y == pytest.approx(2.0)
    finally:
        if snap is not None:
            service.stop(snap.run_id, "test")
        backend.close()
