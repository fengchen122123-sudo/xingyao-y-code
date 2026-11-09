"""wait_for 条件等待（Windows-MCP 对齐，2026-11-09）协议层测试。

Task 1 只覆盖协议层：core.computer.models / core.computer.actions 的严格解析与
schema 声明，不触碰真实 UIA 遍历、截图或轮询（执行层见 Task 2）。
"""

from __future__ import annotations

import pytest

from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA, parse_computer_action
from core.computer.models import ComputerAction, ComputerControlError


def _properties() -> dict:
    return COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]


def test_parse_wait_for_defaults():
    """condition 缺省 text_exists，timeout_ms/interval_ms 取默认值；schema 同步声明。"""
    from core.computer.actions import WAIT_FOR_CONDITIONS
    action = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "text": "保存成功",
    })
    assert isinstance(action, ComputerAction)
    assert action.kind == "wait_for"
    assert action.condition == "text_exists"
    assert action.timeout_ms == 8000
    assert action.interval_ms == 300
    assert action.text == "保存成功"
    assert action.window_name is None and action.element_ref is None

    # 显式给出的合法值原样保留
    explicit = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "text": "登录",
        "window_name": "登录窗口",
        "timeout_ms": 1000,
        "interval_ms": 2000,
    })
    assert (explicit.timeout_ms, explicit.interval_ms) == (1000, 2000)
    assert explicit.window_name == "登录窗口"

    # 新增字段对既有动作保持默认（None），不改变现有语义
    observe = parse_computer_action({"kind": "observe", "target": "desktop"})
    assert observe.condition is None
    assert observe.timeout_ms is None
    assert observe.interval_ms is None

    # schema 同步暴露 wait_for 与四个新字段（condition 为枚举，数值带边界）
    properties = _properties()
    assert "wait_for" in properties["kind"]["enum"]
    assert properties["condition"]["enum"] == list(WAIT_FOR_CONDITIONS) == [
        "text_exists",
        "element_exists",
        "element_enabled",
        "window_active",
    ]
    assert properties["timeout_ms"]["minimum"] == 1000
    assert properties["timeout_ms"]["maximum"] == 30000
    assert properties["interval_ms"]["minimum"] == 100
    assert properties["interval_ms"]["maximum"] == 2000
    assert properties["window_name"]["maxLength"] == 200


def test_parse_wait_for_rejects_unknown_condition():
    for payload in (
        {"kind": "wait_for", "target": "desktop", "condition": "network_idle", "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": "", "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": True, "text": "x"},
        {"kind": "wait_for", "target": "desktop", "condition": ["text_exists"], "text": "x"},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action(payload)
        assert raised.value.code == "computer_invalid_action"
        assert "condition" in raised.value.message


def test_parse_wait_for_rejects_missing_required_field():
    """text_exists 缺 text；window_active 缺 window_name；text 超长拒绝。"""
    for payload in (
        {"kind": "wait_for", "target": "desktop"},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists"},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists", "text": ""},
        {"kind": "wait_for", "target": "desktop", "condition": "text_exists", "text": "长" * 201},
        {"kind": "wait_for", "target": "desktop", "condition": "window_active"},
        {"kind": "wait_for", "target": "desktop", "condition": "window_active", "window_name": ""},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action(payload)
        assert raised.value.code == "computer_invalid_action"


def test_parse_wait_for_rejects_element_condition_without_element_ref():
    for condition in ("element_exists", "element_enabled"):
        for payload in (
            {"kind": "wait_for", "target": "desktop", "condition": condition},
            {"kind": "wait_for", "target": "desktop", "condition": condition, "element_ref": ""},
            {"kind": "wait_for", "target": "desktop", "condition": condition, "element_ref": 7},
        ):
            with pytest.raises(ComputerControlError) as raised:
                parse_computer_action(payload)
            assert raised.value.code == "computer_invalid_action"
            assert "element_ref" in raised.value.message

    ok = parse_computer_action({
        "kind": "wait_for",
        "target": "desktop",
        "condition": "element_enabled",
        "element_ref": "el-3",
    })
    assert ok.element_ref == "el-3" and ok.condition == "element_enabled"


def test_parse_wait_for_rejects_timeout_out_of_range():
    base = {"kind": "wait_for", "target": "desktop", "text": "x"}
    for extra in (
        {"timeout_ms": 999},
        {"timeout_ms": 30001},
        {"timeout_ms": True},
        {"timeout_ms": "8000"},
        {"timeout_ms": 8000.0},
        {"interval_ms": 99},
        {"interval_ms": 2001},
        {"interval_ms": False},
        {"interval_ms": None},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action({**base, **extra})
        assert raised.value.code == "computer_invalid_action"


def test_parse_wait_for_rejects_browser_target():
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action({
            "kind": "wait_for",
            "target": "browser",
            "text": "加载完成",
        })
    assert raised.value.code == "computer_invalid_action"
    assert "desktop" in raised.value.message


def test_parse_wait_for_rejects_unknown_field():
    for extra in (
        {"duration_ms": 3000},
        {"steps": []},
        {"url": "https://example.com"},
        {"retry": 3},
    ):
        with pytest.raises(ComputerControlError) as raised:
            parse_computer_action({
                "kind": "wait_for",
                "target": "desktop",
                "text": "x",
                **extra,
            })
        assert raised.value.code == "computer_invalid_action"
        assert "wait_for" in raised.value.message