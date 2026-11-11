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


def test_explicit_observe_with_elements_attaches_image_and_position(tmp_path):
    """显式 observe 有元素时也附图：模型能看图认位置、用 el-N 精确点击。"""
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
            # bounds 是矩形四角 (left, top, right, bottom)，与 UIA/浏览器一致
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
    # 物理 (160,160)-(180,180) 换算到 100x100 模型图落在右下区域
    assert "位置=画面右下角" in text


def _dense_unnamed_elements(count=80):
    """构造「密集且几乎无名」的元素列表（图片型/网格游戏页面的典型形态）。"""
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
    """E3：密集无名元素在动作后的自动观察里不再逐行塞清单，改以截图为主。"""
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
    assert "el-1 link" not in text  # 逐行清单没有混进来


def test_dense_unnamed_elements_explicit_observe_keeps_full_list(tmp_path):
    """显式 observe 是模型主动要看：密集页面也必须给回 el-N 清单。"""
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
    """无控件列表时附图观察追加放大提示：模型只能看整屏截图估坐标，易点偏。"""
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
    # 提示必须排在 image_url 之前（先读指引再看图）
    assert types.index("image_url") == len(types) - 1


def test_with_elements_image_observation_has_no_region_hint(tmp_path):
    """有控件列表时不加放大提示：模型用 click_element 即可，加了只占 token。"""
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
    """动作后的跟进观察（非显式 observe）有元素时仍只发文本，省 token。"""
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
    """显式 observe 有元素但没截图：降级纯文本仍保留 el-N，模型不至于全盲。"""
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
    """显式 observe 有元素但截图超限：省略图片时仍给出控件列表。"""
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


# ---------------- 元素方位描述：边界场景 ----------------


def test_element_position_label_handles_negative_origin_for_monitor_above():
    """副屏在主屏上方（origin_y 为负）时，方位仍相对本帧画面判断。

    负原点是虚拟桌面的常见形态：坐标先减原点再按比例缩放，减法天然完成平移，
    不需要为负原点单开分支。若漏减原点（或错把 bounds 当 x/y/w/h），副屏元素的
    中心会整体偏出画面，退化成空串。
    """
    from core.computer.models import ComputerObservation
    from core.computer.provider_generic import _element_lines

    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="click",
        width=960,             # 副屏 1920x1080 缩半后的模型图
        height=540,
        original_width=1920,
        original_height=1080,
        origin_x=0,
        origin_y=-1080,        # 副屏位于主屏正上方 → 虚拟桌面原点 y 为负
        elements=(
            {"ref": "el-1", "kind": "ButtonControl", "name": "",
             "bounds": (1600, -1080, 1920, -720), "enabled": True},
            {"ref": "el-2", "kind": "ButtonControl", "name": "",
             "bounds": (0, -1080, 320, -720), "enabled": True},
        ),
    )
    lines = _element_lines(observation)
    # 物理中心 (1760,-900) → 模型 (880,90)：右上角
    assert "位置=画面右上角" in lines[0]
    # 物理中心 (160,-900) → 模型 (80,90)：左上角
    assert "位置=画面左上角" in lines[1]


def test_element_position_label_returns_empty_when_bounds_outside_frame():
    """中心落在画面外（跨屏元素、截图只含本屏）时返回空串，不加位置后缀。"""
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
    # 画面右侧之外（中心 x=2050 → 模型 1025 > 960）
    right_out = {"ref": "el-1", "kind": "ButtonControl", "name": "越界右",
                 "bounds": (2000, 100, 2100, 200), "enabled": True}
    # 画面左侧之外（中心 x=-150 → 模型 -75 < 0）
    left_out = {"ref": "el-2", "kind": "ButtonControl", "name": "越界左",
                "bounds": (-200, 100, -100, 200), "enabled": True}
    assert _element_position_label(right_out, observation) == ""
    assert _element_position_label(left_out, observation) == ""
    # 渲染层同样不加后缀：越界元素仍保留 ref/name，只是没有方位
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
    """退化矩形（宽或高为 0）不抛错：中心即角点，按该点判方位。"""
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
    # 四角退化：right==left、bottom==top
    for bounds, expected in (
        ((0, 0, 0, 0), "画面左上角"),
        ((1000, 1000, 1000, 1000), "画面右下角"),
        ((500, 500, 500, 500), "画面中央"),
    ):
        element = {"ref": "el-1", "kind": "ButtonControl", "name": "",
                   "bounds": bounds, "enabled": True}
        assert _element_position_label(element, square) == expected

    # 观察尺寸缺失（宽高为 0）时同样不抛错，只返回空串
    bare = ComputerObservation(run_id="run-1", target="desktop", action="click")
    assert _element_position_label(
        {"ref": "el-1", "kind": "ButtonControl", "name": "",
         "bounds": (10, 10, 20, 20), "enabled": True}, bare
    ) == ""

