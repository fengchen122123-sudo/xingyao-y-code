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
    assert tool is not COMPUTER_ACTION_TOOL_SCHEMA


@pytest.mark.parametrize("capability", [
    DISABLED_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
], ids=["disabled", "native"])
def test_build_generic_computer_tool_off_or_native_returns_none(capability):
    assert build_generic_computer_tool(capability) is None


def test_build_generic_dynamic_prompt_enabled_covers_all_eight_rules():
    text = build_generic_dynamic_prompt(GENERIC_CAPABILITY)
    assert "observe" in text
    assert "click_element" in text
    assert "set_value_element" in text
    assert "el-N" in text
    assert "禁止凭截图目测坐标" in text
    assert "元素列表为空" in text
    assert "截图坐标点击" in text
    assert "kind=sequence" in text
    assert "不得与其他工具混用" in text
    assert "标注截图" in text
    assert "不得猜测成功" in text
    assert "surface_generation" in text
    assert "旧坐标与旧元素引用立即作废" in text
    assert "list_elements" in text
    assert "焦点被抢" in text
    assert "activate_surface" in text
    assert "严禁按记忆里的旧坐标连续重试" in text
    assert "request_user_takeover" in text
    assert "验证码" in text
    assert "完成" in text and "不继续无意义观察" in text
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
    row = dict(followup, role="user")
    assert is_observation_protocol_message(row)
    assert not is_observation_protocol_message("帮我打开浏览器看看")
    assert not is_observation_protocol_message({"role": "user", "content": "文本"})
    assert not is_observation_protocol_message(
        json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}])
    )
    assert not is_observation_protocol_message(None)
    assert not is_observation_protocol_message(123)


def test_no_screenshot_observation_projects_text_only_with_page_text():
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
    assert page_text in text


def test_no_screenshot_observation_without_page_text_still_succeeds():
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


def test_explicit_observe_with_elements_attaches_image_and_position(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="observe",
        png_path=str(png),
        width=100,
        height=100,
        original_width=200,
        original_height=200,
        origin_x=0,
        origin_y=0,
        elements=(
            {"ref": "el-1", "kind": "button", "name": "",
             "bounds": (160, 160, 180, 180), "enabled": True},
        ),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    types = [block["type"] for block in message["content"]]
    assert "image_url" in types
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "el-1" in text
    assert "位置=画面右下角" in text


def _dense_unnamed_elements(count=80):
    return tuple(
        {
            "ref": f"el-{index}",
            "kind": "link",
            "name": "",
            "bounds": (index, index, index + 16, index + 16),
            "enabled": True,
        }
        for index in range(1, count + 1)
    )


def test_dense_unnamed_elements_after_action_switches_to_image(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="click_element",
        png_path=str(png),
        elements=_dense_unnamed_elements(),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    types = [block["type"] for block in message["content"]]
    assert "image_url" in types
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "已省略逐行清单" in text
    assert "el-1 link" not in text


def test_dense_unnamed_elements_explicit_observe_keeps_full_list(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="observe",
        png_path=str(png),
        elements=_dense_unnamed_elements(),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "el-1 link" in text
    assert "image_url" in [block["type"] for block in message["content"]]


def test_no_elements_image_observation_hints_observe_region(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="observe",
        png_path=str(png),
        elements=(),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    types = [block["type"] for block in message["content"]]
    assert "image_url" in types
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "observe_region" in text
    assert types.index("image_url") == len(types) - 1


def test_with_elements_image_observation_has_no_region_hint(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (16, 16), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="observe",
        png_path=str(png),
        width=100,
        height=100,
        original_width=100,
        original_height=100,
        elements=(
            {"ref": "el-5", "kind": "button", "name": "提交",
             "bounds": (10, 10, 20, 20), "enabled": True},
        ),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "el-5" in text
    assert "observe_region" not in text


def test_followup_observation_with_elements_stays_text_only(tmp_path):
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="click",
        width=100,
        height=100,
        original_width=100,
        original_height=100,
        elements=(
            {"ref": "el-2", "kind": "button", "name": "确定",
             "bounds": (10, 10, 20, 20), "enabled": True},
        ),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=2)
    assert all(block["type"] == "text" for block in message["content"])
    assert "el-2" in "\n".join(
        block["text"] for block in message["content"]
    )


def test_explicit_observe_with_elements_keeps_list_when_no_screenshot():
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="observe",
        screenshot_error="截图失败：TimeoutError",
        elements=(
            {"ref": "el-3", "kind": "button", "name": "保存",
             "bounds": (10, 10, 20, 20), "enabled": True},
        ),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=1)
    assert all(block["type"] == "text" for block in message["content"])
    assert message["_computer_v1"]["image_omitted"] == "no_screenshot"
    text = "\n".join(block["text"] for block in message["content"])
    assert "el-3" in text
    assert "本次没有截图" in text


def test_explicit_observe_with_elements_keeps_list_when_frame_too_large(
    tmp_path, monkeypatch
):
    from core.computer import provider_generic
    from core.computer.models import ComputerObservation

    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: None)
    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="observe",
        png_path=str(png),
        elements=(
            {"ref": "el-4", "kind": "button", "name": "取消",
             "bounds": (10, 10, 20, 20), "enabled": True},
        ),
    )
    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=1
    )
    assert not any(block["type"] == "image_url" for block in message["content"])
    assert message["_computer_v1"]["image_omitted"] == "too_large"
    text = "\n".join(block["text"] for block in message["content"])
    assert "el-4" in text
    assert "已省略图片" in text


def test_element_position_label_handles_negative_origin_for_monitor_above():
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _element_lines

    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="click",
        width=960,
        height=540,
        original_width=1920,
        original_height=1080,
        origin_x=0,
        origin_y=-1080,
        elements=(
            {"ref": "el-1", "kind": "ButtonControl", "name": "",
             "bounds": (1600, -1080, 1920, -720), "enabled": True},
            {"ref": "el-2", "kind": "ButtonControl", "name": "",
             "bounds": (0, -1080, 320, -720), "enabled": True},
        ),
    )
    lines = _element_lines(observation)
    assert "位置=画面右上角" in lines[0]
    assert "位置=画面左上角" in lines[1]


def test_element_position_label_returns_empty_when_bounds_outside_frame():
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import (
        _element_lines,
        _element_position_label,
    )

    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="click",
        width=960,
        height=540,
        original_width=1920,
        original_height=1080,
    )
    right_out = {"ref": "el-1", "kind": "ButtonControl", "name": "越界右",
                 "bounds": (2000, 100, 2100, 200), "enabled": True}
    left_out = {"ref": "el-2", "kind": "ButtonControl", "name": "越界左",
                "bounds": (-200, 100, -100, 200), "enabled": True}
    assert _element_position_label(right_out, observation) == ""
    assert _element_position_label(left_out, observation) == ""
    lines = _element_lines(
        ComputerObservation(
            run_id="run-1",
            target="desktop",
            action="click",
            width=960,
            height=540,
            original_width=1920,
            original_height=1080,
            elements=(right_out,),
        )
    )
    assert "el-1" in lines[0]
    assert "位置=" not in lines[0]


def test_element_position_label_zero_size_bounds_is_safe():
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _element_position_label

    square = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="click",
        width=1000,
        height=1000,
        original_width=1000,
        original_height=1000,
    )
    for bounds, expected in (
        ((0, 0, 0, 0), "画面左上角"),
        ((1000, 1000, 1000, 1000), "画面右下角"),
        ((500, 500, 500, 500), "画面中央"),
    ):
        element = {"ref": "el-1", "kind": "ButtonControl", "name": "",
                   "bounds": bounds, "enabled": True}
        assert _element_position_label(element, square) == expected

    bare = ComputerObservation(run_id="run-1", target="desktop", action="click")
    assert _element_position_label(
        {"ref": "el-1", "kind": "ButtonControl", "name": "",
         "bounds": (10, 10, 20, 20), "enabled": True}, bare
    ) == ""


def test_element_lines_observation_with_valid_png_pairs_image_and_elements(tmp_path):
    from PIL import Image

    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _observation_followup_message

    png = tmp_path / "frame.png"
    Image.new("RGB", (32, 32), (255, 255, 255)).save(png, format="PNG")
    observation = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="click_element",
        png_path=str(png),
        width=32,
        height=32,
        original_width=32,
        original_height=32,
        frame_id="frame-7",
        elements=(
            {"ref": "el-1", "kind": "button", "name": "提交",
             "bounds": [0, 0, 16, 16], "enabled": True},
        ),
    )
    message = _observation_followup_message(observation, run_id="run-1", ordinal=2)
    types = [block["type"] for block in message["content"]]
    assert "image_url" in types
    text = "\n".join(
        block["text"] for block in message["content"] if block["type"] == "text"
    )
    assert "el-1" in text


def test_text_only_element_lines_observation_does_not_modify_coord_frame(tmp_path):
    import threading
    from PIL import Image

    from core.computer.actions import parse_computer_action
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerObservation, ComputerOwner
    from core.computer.provider_generic import _observation_followup_message
    from core.computer.settings import ComputerSettingsSnapshot
    from tests.computer_fakes import FakePlaywrightRuntime

    import io
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 20, 30)).save(buf, format="PNG")
    runtime = FakePlaywrightRuntime(screenshot_bytes=buf.getvalue())
    backend = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime, viewport=(400, 300)
    )
    cap = ComputerCapability(enabled=True, protocol="generic", targets=("browser",), browser="edge")
    owner = ComputerOwner("s1", "t1", "s1", threading.Event())
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
    snap = None
    try:
        snap = service.start_run(owner, cap, target="browser")
        obs1 = service.execute(snap.run_id, owner, parse_computer_action({"kind": "observe", "target": "browser"}))
        prior_coord_frame = backend._coord_frame
        text_only_obs = ComputerObservation(
            run_id=snap.run_id,
            target="browser",
            action="click_element",
            png_path="",
            width=400,
            height=300,
            original_width=400,
            original_height=300,
            frame_id=obs1.frame_id,
            elements=({"ref": "el-1", "kind": "button", "name": "提交", "bounds": [0, 0, 10, 10], "enabled": True},),
        )
        msg = _observation_followup_message(
            text_only_obs, run_id=snap.run_id, ordinal=2, controller=service
        )
        assert all(block["type"] == "text" for block in msg["content"])
        assert backend._coord_frame == prior_coord_frame
        active = service.get_observation_frame(snap.run_id, obs1.frame_id)
        assert active is not None and active.coordinate_input_enabled is True
    finally:
        if snap is not None:
            service.stop(snap.run_id, "test")
        backend.close()
