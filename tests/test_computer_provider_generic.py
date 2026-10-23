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


def test_build_generic_dynamic_prompt_enabled_covers_all_eight_rules():
    text = build_generic_dynamic_prompt(GENERIC_CAPABILITY)
    # 通用化重构 Task E 元素优先学说：七条要点语义必须逐字可见
    assert "observe" in text
    # 第 2 条（E4 更新）：元素优先——浏览器 observe 附带列表、坐标回落约束
    assert "click_element" in text
    assert "set_value_element" in text
    assert "el-N" in text
    assert "禁止凭截图目测坐标" in text
    assert "元素列表为空" in text
    assert "截图坐标点击" in text
    # 第 3 条：单动作 + sequence 打包
    assert "kind=sequence" in text
    assert "不得与其他工具混用" in text
    assert "标注截图" in text
    # 第 4 条：依据观察推进 + 代数失效语义
    assert "不得猜测成功" in text
    assert "surface_generation" in text
    assert "旧坐标与旧元素引用立即作废" in text
    # 第 5 条：焦点恢复指引（桌面 list_elements 恢复路径）
    assert "list_elements" in text
    assert "焦点被抢" in text
    assert "activate_surface" in text
    assert "严禁按记忆里的旧坐标连续重试" in text
    # 第 6/7 条：接管与收尾
    assert "request_user_takeover" in text
    assert "验证码" in text
    assert "完成" in text and "不继续无意义观察" in text
    # 第 8 条：内嵌浏览器围观呈现 + 项目分辨率/browser_set_viewport 指引
    assert "右侧面板" in text
    assert "browser_set_viewport" in text
    assert "1440×900" in text


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


def test_is_persisted_observation_message_shape_variants():
    """判定助手：观察跟进 JSON 串为真；真实用户文本/列表内容/非字符串为假。"""
    import json

    from core.computer.provider_generic import (
        _observation_followup_message,
        is_observation_protocol_message,
    )

    class _Obs:
        target = "browser"
        elements = [{"ref": "el-1", "kind": "link", "name": "x",
                     "bounds": (1, 2, 3, 4), "enabled": True}]
        png_path = ""

    followup = _observation_followup_message(_Obs(), run_id="run-1", ordinal=1)
    assert is_observation_protocol_message(json.dumps(followup, ensure_ascii=False))
    # memory 解析后的消息行：_computer_v1 键提升到行顶层
    row = dict(followup, role="user")
    assert is_observation_protocol_message(row)
    assert not is_observation_protocol_message("帮我打开浏览器看看")
    assert not is_observation_protocol_message({"role": "user", "content": "文本"})
    # 真实多模态用户消息：content 是 JSON 数组，绝无 _computer_v1 键
    assert not is_observation_protocol_message(
        json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}])
    )
    assert not is_observation_protocol_message(None)
    assert not is_observation_protocol_message(123)


def test_no_screenshot_observation_projects_text_only_with_page_text():
    """B 方案：png_path 为空时绝不读图，改发纯文本观察（无截图说明 + 页面正文）。

    为什么：窗口被遮挡/最小化时 WebView2 渲染被节流，截图会卡住或失败；旧路径
    _read_observation_png("") 会抛 computer_backend_error，让观察链整体失败。
    """
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    page_text = "搜索结果第一条：示例页面正文" * 20
    observation = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="observe",
        page_text=page_text,
        screenshot_error="浏览器窗口不可见或被遮挡，本次未取到截图：TimeoutError",
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    assert all(block["type"] == "text" for block in message["content"])
    assert not any(block["type"] == "image_url" for block in message["content"])
    assert message["_computer_v1"]["image_omitted"] == "no_screenshot"
    text = "\n".join(block["text"] for block in message["content"])
    assert "本次没有截图" in text
    assert "TimeoutError" in text
    # 正文按 backend 截断后的原样进入消息（这里不再二次截断）
    assert page_text in text


def test_no_screenshot_observation_without_page_text_still_succeeds():
    """正文也缺失时仍返回纯文本观察，绝不因缺图/缺正文抛错。"""
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    observation = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="observe",
        screenshot_error="浏览器窗口不可见或被遮挡，本次未取到截图：TimeoutError",
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    assert all(block["type"] == "text" for block in message["content"])
    text = "\n".join(block["text"] for block in message["content"])
    assert "本次没有截图" in text
    assert "本次也未能读到可见文本" in text
