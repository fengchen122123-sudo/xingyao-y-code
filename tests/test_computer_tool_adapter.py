"""Task 9：固定 computer_action 适配器、follow-up 剥离与保留名语义。

测试全部经 fake 注入（FakeController + 虚拟 PNG 字节），
绝不真实移动鼠标/发送键盘/截屏/启动浏览器，也绝不调用真实 Provider。
"""

import base64
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from core.computer.models import ComputerControlError, ComputerOwner
from core.computer.provider_generic import (
    OBSERVATION_FOLLOWUP_TEXT,
    execute_reserved_computer_tool,
    validate_computer_tool_batch,
)
from core.computer.tool_adapter import (
    COMPUTER_ACTION_TOOL_NAME,
    RESERVED_COMPUTER_TOOL_NAMES,
    ComputerTurnLease,
    computer_action_tool_spec,
)
from core.harness.tool_pipeline import (
    ToolBatchExecutor,
    ToolExecutionContext,
    ToolPipeline,
    decode_tool_call,
)
from core.tool_protocol import ToolResult
from core.tool_registry import ToolRegistry
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    FakeController,
    fake_png_bytes,
)


def _call(name, arguments=None, *, call_id="call-1"):
    payload = arguments if arguments is not None else {}
    return {
        "id": call_id,
        "function": {
            "name": name,
            "arguments": json.dumps(payload, ensure_ascii=False),
        },
    }


class _EmptyMcp:
    def get_schemas(self):
        return []

    def get_schema(self, _name):
        return None


def _forbidden_permission_checker(*_args, **_kwargs):
    raise AssertionError("computer_action 固定适配不得触发权限询问")


def _computer_pipeline():
    return ToolPipeline(
        plan_turn_checker=lambda _turn_id: False,
        pending_plan_checker=lambda _session_id: False,
    )


def execute_one_computer_action(controller, context, *, target="browser",
                                call_id="call-1", arguments=None):
    """经生产 ToolBatchExecutor 路径执行一个 computer_action，返回 record 包装。

    错误结果没有 follow-up：observation_data_url 为 None。
    """
    lease = getattr(context, "computer_lease", None)
    assert lease is None or lease.controller is controller
    payload = arguments if arguments is not None else {"kind": "observe", "target": target}
    executor = ToolBatchExecutor(
        _computer_pipeline(),
        event_payload=lambda decoded: {"id": decoded.call_id},
        finished_event_payload=lambda decoded, message, duration_ms: {
            "id": decoded.call_id,
            "duration_ms": duration_ms,
        },
    )
    records = executor.execute(
        [_call("computer_action", payload, call_id=call_id)], context, None
    )
    record = records[0]
    followup = record.followup_messages[0] if record.followup_messages else None
    return SimpleNamespace(
        message=record.message,
        followup_messages=record.followup_messages,
        observation_data_url=(
            next(
                (
                    item["image_url"]["url"]
                    for item in followup["content"]
                    if item.get("type") == "image_url"
                ),
                None,
            )
            if followup
            else None
        ),
    )


@pytest.fixture
def fake_controller(tmp_path):
    png = tmp_path / "fake-observation.png"
    png.write_bytes(fake_png_bytes(7))
    owner = ComputerOwner(
        session_id="s-1",
        turn_id="t-1",
        session_label="测试会话",
        cancel_event=threading.Event(),
    )
    return FakeController(owner=owner, png_path=str(png))


@pytest.fixture
def context(fake_controller):
    return ToolExecutionContext(
        event_callback=None,
        parent_session="s-1",
        parent_turn_id="t-1",
        cancel_event=None,
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=_forbidden_permission_checker,
        registry=ToolRegistry(),
        mcp_manager=_EmptyMcp(),
        computer_lease=ComputerTurnLease(
            capability=GENERIC_CAPABILITY,
            owner=fake_controller.owner,
            controller=fake_controller,
            run_id=fake_controller.run_id,
        ),
    )


def test_existing_tool_result_default_is_byte_compatible():
    result = ToolResult("ok")
    assert result.to_message_content() == "ok"
    assert result.followup_messages == ()


def test_computer_result_adds_tool_then_user_observation(fake_controller, context):
    record = execute_one_computer_action(fake_controller, context, target="browser")
    assert record.message == {
        "role": "tool", "tool_call_id": "call-1", "name": "computer_action",
        "content": record.message["content"],
    }
    assert record.followup_messages == ({
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
            # A5：无控件元素时附图前会追加 observe_region 定位提示，故为三块
            {"type": "text", "text": "【定位提示】若目标控件较小、密集或看不清，先用 observe_region 放大该区域再点击，不要凭整屏截图目测坐标。"},
            {"type": "image_url", "image_url": {"url": record.observation_data_url}},
        ],
        "_computer_v1": {"run_id": "run-a", "ordinal": 1, "target": "browser"},
    },)


@pytest.mark.parametrize("calls", [
    [_call("computer_action"), _call("computer_action")],
    [_call("computer_action"), _call("read_file")],
], ids=["two-computer-calls", "computer-mixed-with-read"])
def test_computer_action_cannot_be_batched_or_mixed(calls):
    with pytest.raises(ComputerControlError) as raised:
        validate_computer_tool_batch(calls)
    assert raised.value.code == "computer_invalid_action"


def test_mixed_batch_rejected_before_any_side_effect(fake_controller, context):
    calls = [
        _call("computer_action", {"kind": "observe", "target": "browser"}),
        _call("read_file", {"file_path": "a.py"}, call_id="call-2"),
    ]
    with pytest.raises(ComputerControlError) as raised:
        validate_computer_tool_batch(calls)
    assert raised.value.code == "computer_invalid_action"
    assert fake_controller.execute_calls == []


def test_computer_action_executes_via_lease_with_nine_key_json(fake_controller, context):
    record = execute_one_computer_action(fake_controller, context, target="browser")
    assert fake_controller.execute_calls == [("run-a", "observe", "browser")]
    payload = {
        "ok": True,
        "target": "browser",
        "action": "observe",
        "screen_sha256": "fake-sha-1",
        "width": 1280,
        "height": 720,
        "origin_or_window": "https://fake.example",
        "error_code": "",
        "error_message": "",
    }
    assert record.message["content"] == json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def test_followup_png_data_url_only_in_user_observation(fake_controller, context):
    record = execute_one_computer_action(fake_controller, context, target="browser")
    url = record.observation_data_url
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == fake_png_bytes(7)
    assert "base64" not in record.message["content"]
    assert set(record.message) == {"role", "tool_call_id", "name", "content"}
    assert record.followup_messages[0]["content"][0]["text"] == OBSERVATION_FOLLOWUP_TEXT


def test_type_text_input_never_enters_result_or_followup(fake_controller, context):
    record = execute_one_computer_action(
        fake_controller,
        context,
        target="browser",
        arguments={"kind": "type_text", "target": "browser", "text": "秘密输入secret-input"},
    )
    dumped_followup = json.dumps(record.followup_messages, ensure_ascii=False)
    assert "秘密输入secret-input" not in record.message["content"]
    assert "秘密输入secret-input" not in dumped_followup


def test_computer_action_without_lease_returns_stable_disabled_error(
    fake_controller, context
):
    bare = replace(context, computer_lease=None)
    record = execute_one_computer_action(fake_controller, bare, target="browser")
    payload = json.loads(record.message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_disabled"
    assert record.followup_messages == ()
    assert fake_controller.execute_calls == []


def test_computer_action_with_disabled_capability_uses_frozen_error(
    fake_controller, context
):
    lease = replace(context.computer_lease, capability=DISABLED_CAPABILITY)
    record = execute_one_computer_action(
        fake_controller, replace(context, computer_lease=lease), target="browser"
    )
    payload = json.loads(record.message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_disabled"
    assert payload["error_message"] == "电脑操控未开启"
    assert record.followup_messages == ()
    assert fake_controller.execute_calls == []


def test_owner_mismatch_returns_stable_error_without_execution(
    fake_controller, context
):
    other_owner = ComputerOwner(
        session_id="s-other",
        turn_id="t-other",
        session_label="其他会话",
        cancel_event=threading.Event(),
    )
    lease = replace(context.computer_lease, owner=other_owner)
    record = execute_one_computer_action(
        fake_controller, replace(context, computer_lease=lease), target="browser"
    )
    payload = json.loads(record.message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_owner_mismatch"
    assert set(payload) == {
        "ok", "target", "action", "screen_sha256", "width", "height",
        "origin_or_window", "error_code", "error_message",
    }
    assert fake_controller.execute_calls == []


def test_invalid_action_payload_returns_computer_invalid_action(
    fake_controller, context
):
    record = execute_one_computer_action(
        fake_controller,
        context,
        target="browser",
        arguments={"kind": "observe", "target": "browser", "bogus": 1},
    )
    payload = json.loads(record.message["content"])
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_invalid_action"
    assert record.followup_messages == ()
    assert fake_controller.execute_calls == []


def test_controller_error_maps_to_stable_nine_key_json(fake_controller, context):
    fake_controller.execute_errors.append(
        ComputerControlError("computer_session_busy", "电脑操控正被其他会话占用")
    )
    record = execute_one_computer_action(fake_controller, context, target="browser")
    payload = json.loads(record.message["content"])
    assert set(payload) == {
        "ok", "target", "action", "screen_sha256", "width", "height",
        "origin_or_window", "error_code", "error_message",
    }
    assert payload["ok"] is False
    assert payload["error_code"] == "computer_session_busy"
    assert payload["error_message"] == "电脑操控正被其他会话占用"
    assert record.followup_messages == ()


def test_missing_png_returns_error_without_stale_image(fake_controller, context):
    fake_controller.png_path = ""
    record = execute_one_computer_action(fake_controller, context, target="browser")
    payload = json.loads(record.message["content"])
    # png 缺失走 B 方案降级观察（不再是错误），但绝不能夹带陈旧图片
    assert payload["ok"] is True
    # 降级观察仍会给模型一条纯文本 follow-up，但绝不能夹带陈旧图片
    assert all(
        item.get("type") != "image_url"
        for message in record.followup_messages
        for item in message["content"]
    )


def test_forged_non_computer_name_stays_on_normal_resolution_path(context):
    # Task 11a 起 computer_select_target 由固定 adapter 拦截（无原生双 target
    # 上下文时返回 computer_disabled，见 test_computer_provider_openai 的
    # test_selector_without_selection_context_fails）；本测试的意图是"非保留名
    # 走普通解析路径"，故改用真正非保留的伪造名。
    forged = "computer_select_browser"
    message = _computer_pipeline().execute_one(_call(forged), context)
    assert message == {
        "role": "tool",
        "tool_call_id": "call-1",
        "name": forged,
        "content": f"🚨 未找到名为 {forged} 的工具！",
    }


def test_execute_reserved_returns_none_for_non_computer_names(context):
    decoded = decode_tool_call(_call("read_file", {"file_path": "a.py"}))
    assert execute_reserved_computer_tool(decoded, context) is None


def test_computer_action_tool_spec_semantics():
    spec = computer_action_tool_spec()
    assert spec.name == COMPUTER_ACTION_TOOL_NAME
    assert spec.requires_permission is False
    assert spec.cancellable is True
    assert spec.parallel_safe is False
    assert spec.mutates_files is False
    # 规格 03 §3.2：computer_action 归 system（A 阶段评审补修缺口）。
    assert spec.side_effect_scope == "system"


def test_reserved_computer_tool_names_are_frozen():
    assert RESERVED_COMPUTER_TOOL_NAMES == frozenset({
        "computer_action", "computer_select_target",
    })
    assert COMPUTER_ACTION_TOOL_NAME == "computer_action"


def test_second_computer_action_ordinal_increments(fake_controller, context):
    first = execute_one_computer_action(fake_controller, context, target="browser")
    second = execute_one_computer_action(
        fake_controller, context, target="browser", call_id="call-2"
    )
    assert first.followup_messages[0]["_computer_v1"]["ordinal"] == 1
    assert second.followup_messages[0]["_computer_v1"]["ordinal"] == 2
    assert second.followup_messages[0]["_computer_v1"]["run_id"] == "run-a"
