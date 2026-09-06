"""Task 9：generic 协议 Provider 投影（schema、动态提示、批处理约束）。

全部纯函数级测试，无任何真实键鼠/浏览器/Provider 调用。
"""

import json

import pytest

from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA
from core.computer.models import ComputerControlError
from core.computer.provider_generic import (
    build_generic_computer_tool,
    build_generic_dynamic_prompt,
    validate_computer_tool_batch,
)
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
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


def test_build_generic_computer_tool_enabled_returns_schema_copy():
    tool = build_generic_computer_tool(GENERIC_CAPABILITY)
    assert tool == COMPUTER_ACTION_TOOL_SCHEMA
    # 返回副本：调用方追加 tools 时不得改写模块级常量
    assert tool is not COMPUTER_ACTION_TOOL_SCHEMA


@pytest.mark.parametrize("capability", [
    DISABLED_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
], ids=["disabled", "native"])
def test_build_generic_computer_tool_off_or_native_returns_none(capability):
    assert build_generic_computer_tool(capability) is None


def test_build_generic_dynamic_prompt_enabled_covers_all_seven_rules():
    text = build_generic_dynamic_prompt(GENERIC_CAPABILITY)
    # 设计 10.1 七条说明语义必须逐字可见
    assert "observe" in text
    # 2026-09-06 sequence 协议扩展：第 2 条改为"单个 computer_action + sequence 打包"
    assert "kind=sequence" in text
    assert "不得与其他工具混用" in text
    assert "最新截图" in text
    assert "(0,0)" in text
    assert "不得猜测动作成功" in text
    assert "登录" in text and "验证码" in text
    assert "request_user_takeover" in text
    assert "完成" in text and "不继续无意义观察" in text


@pytest.mark.parametrize("capability", [
    DISABLED_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
], ids=["disabled", "native"])
def test_build_generic_dynamic_prompt_off_or_native_returns_empty(capability):
    assert build_generic_dynamic_prompt(capability) == ""


def test_validate_batch_without_computer_calls_passes():
    validate_computer_tool_batch([])
    validate_computer_tool_batch([_call("read_file"), _call("write_file")])


def test_validate_batch_single_computer_call_passes():
    validate_computer_tool_batch([
        _call("computer_action", {"kind": "observe", "target": "browser"}),
    ])


def test_validate_batch_rejects_computer_action_without_target():
    with pytest.raises(ComputerControlError) as raised:
        validate_computer_tool_batch([
            _call("computer_action", {"kind": "click", "target": "browser"}),
            _call("write_file", {"file_path": "a.py"}, call_id="call-2"),
        ])
    assert raised.value.code == "computer_invalid_action"
    assert "一次只能调用一个" in raised.value.message
