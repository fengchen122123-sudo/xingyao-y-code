"""观察帧契约与错误下一步建议的回归测试（2026-10-21）。

覆盖：
1) 画面与上一张完全相同时不重复附图，改为纯文本说明；
2) 显式 observe 即使画面未变也照常附图（模型要看就必须给）；
3) 正常路径保持 text + image 的成对结构，并带稳定 frame 引用；
4) 超限截图先尝试缩放，缩放不可用时退化为纯文摘略；
5) 稳定错误码在 error_message 末尾追加「下一步」，9 键 JSON 形状不变。

全部走内存构造，不启动浏览器/截图后端。
"""

import json

import pytest

from core.computer import provider_generic
from core.computer.models import ComputerControlError, ComputerObservation


def _observation(tmp_path, *, action="click", screen_sha="a" * 64, unchanged=False, size=64):
    png = tmp_path / "frame.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * size)
    return ComputerObservation(
        run_id="run-1",
        target="browser",
        action=action,
        screen_sha256=screen_sha,
        png_path=str(png),
        width=1280,
        height=720,
        screen_unchanged=unchanged,
    )


def test_unchanged_screen_omits_image(tmp_path):
    """画面未变且不是显式 observe：不附图，只给文本说明。"""
    observation = _observation(tmp_path, unchanged=True)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=2
    )

    types = [block["type"] for block in message["content"]]
    assert types == ["text"]
    assert "已省略重复图片" in message["content"][0]["text"]
    assert message["_computer_v1"]["image_omitted"] == "unchanged"


def test_explicit_observe_always_sends_image(tmp_path):
    """显式 observe 即使 screen_unchanged 也必须附图。"""
    observation = _observation(tmp_path, action="observe", unchanged=True)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=3
    )

    assert [block["type"] for block in message["content"]] == ["text", "image_url"]
    assert "image_omitted" not in message["_computer_v1"]


def test_normal_frame_pairs_text_and_image(tmp_path):
    """正常路径保持逐字契约：说明文本不变，图片紧随其后（frame 行只在省略路径）。"""
    observation = _observation(tmp_path, screen_sha="deadbeef" + "0" * 56)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=1
    )

    assert message["content"][0]["text"] == provider_generic.OBSERVATION_FOLLOWUP_TEXT
    assert message["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_oversize_frame_falls_back_to_text(tmp_path, monkeypatch):
    """超限且缩放不可用：退化为纯文摘略，绝不能把超限图塞进上下文。"""
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: None)
    observation = _observation(tmp_path, size=64)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=4
    )

    assert [block["type"] for block in message["content"]] == ["text"]
    assert message["_computer_v1"]["image_omitted"] == "too_large"


def test_oversize_frame_uses_downscaled_bytes(tmp_path, monkeypatch):
    """缩放成功时按缩放后的字节发送，并继续附图。"""
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: b"tiny")
    observation = _observation(tmp_path, size=64)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=5
    )

    assert [block["type"] for block in message["content"]] == ["text", "image_url"]
    assert message["_computer_v1"].get("image_omitted") is None


def test_elements_path_stays_text_only(tmp_path):
    """控件列表路径保持纯文本、不含截图字节，逐条列出元素引用。"""
    observation = ComputerObservation(
        run_id="run-1",
        target="desktop",
        action="list_elements",
        screen_sha256="b" * 64,
        png_path="",
        width=800,
        height=600,
        elements=({"ref": "el-1", "kind": "button", "name": "OK", "bounds": (1, 2, 3, 4)},),
    )

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=6
    )

    assert [block["type"] for block in message["content"]] == ["text", "text"]
    assert "el-1" in message["content"][1]["text"]


@pytest.mark.parametrize(
    "code",
    [
        "computer_element_stale",
        "computer_surface_changed",
        "computer_no_progress",
        "computer_coordinate_out_of_bounds",
    ],
)
def test_error_result_appends_next_step(code):
    """错误结果保持 9 键形状，同时在文案末尾给出可执行的下一步。"""
    result = provider_generic._error_result(
        "browser", "click", ComputerControlError(code, "原始说明")
    )
    payload = json.loads(result.content)

    assert set(payload) == {
        "ok",
        "target",
        "action",
        "screen_sha256",
        "width",
        "height",
        "origin_or_window",
        "error_code",
        "error_message",
    }
    assert payload["error_code"] == code
    assert "下一步：" in payload["error_message"]
    assert payload["error_message"].startswith("原始说明")


def test_error_result_without_mapping_keeps_message():
    """没有映射的码保持原文，不硬塞建议。"""
    result = provider_generic._error_result(
        "browser", "click", ComputerControlError("computer_backend_error", "后端炸了")
    )
    payload = json.loads(result.content)

    assert payload["error_message"] == "后端炸了"
