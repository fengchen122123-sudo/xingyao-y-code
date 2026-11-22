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
    observation = _observation(tmp_path, unchanged=True)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=2
    )

    types = [block["type"] for block in message["content"]]
    assert types == ["text"]
    assert "已省略重复图片" in message["content"][0]["text"]
    assert message["_computer_v1"]["image_omitted"] == "unchanged"


def test_explicit_observe_always_sends_image(tmp_path):
    observation = _observation(tmp_path, action="observe", unchanged=True)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=3
    )

    assert [block["type"] for block in message["content"]] == [
        "text", "text", "image_url",
    ]
    assert "image_omitted" not in message["_computer_v1"]


def test_normal_frame_pairs_text_and_image(tmp_path):
    observation = _observation(tmp_path, screen_sha="deadbeef" + "0" * 56)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=1
    )

    assert message["content"][0]["text"] == provider_generic.OBSERVATION_FOLLOWUP_TEXT
    assert message["content"][1]["text"] == provider_generic._OBSERVATION_BROWSER_POINT_HINT
    assert message["content"][-1]["type"] == "image_url"
    assert message["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_desktop_normal_frame_keeps_region_hint(tmp_path):
    from dataclasses import replace

    observation = replace(
        _observation(tmp_path, screen_sha="deadbeef" + "0" * 56), target="desktop"
    )

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=1
    )

    assert message["content"][1]["text"] == provider_generic._OBSERVATION_REGION_HINT


def test_oversize_frame_falls_back_to_text(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: None)
    observation = _observation(tmp_path, size=64)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=4
    )

    assert [block["type"] for block in message["content"]] == ["text"]
    assert message["_computer_v1"]["image_omitted"] == "too_large"


def test_oversize_frame_uses_downscaled_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: b"tiny")
    observation = _observation(tmp_path, size=64)

    message = provider_generic._observation_followup_message(
        observation, run_id="run-1", ordinal=5
    )

    assert [block["type"] for block in message["content"]] == [
        "text", "text", "image_url",
    ]
    assert message["content"][-1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert message["_computer_v1"].get("image_omitted") is None


def test_elements_path_stays_text_only(tmp_path):
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
    result = provider_generic._error_result(
        "browser", "click", ComputerControlError("computer_backend_error", "后端炸了")
    )
    payload = json.loads(result.content)

    assert payload["error_message"] == "后端炸了"


def _real_png_bytes(width: int, height: int, pad: int = 0) -> bytes:
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (20, 40, 80)).save(buf, format="PNG")
    return buf.getvalue() + (b"\x00" * pad)


def test_frame_reference_line_prefers_frame_id_over_screen_sha256(tmp_path):
    png = tmp_path / "f.png"
    png.write_bytes(_real_png_bytes(400, 300))
    obs = ComputerObservation(
        run_id="run-1",
        target="browser",
        action="observe",
        screen_sha256="deadbeef" * 8,
        png_path=str(png),
        width=400,
        height=300,
        frame_id="frame-99",
    )
    line = provider_generic._frame_reference_line(obs)
    assert "frame=frame-99" in line
    assert "deadbeef" not in line


def _fake_settings_snapshot():
    from core.computer.settings import ComputerSettingsSnapshot

    return ComputerSettingsSnapshot(
        browser_control_enabled=True,
        computer_control_enabled=False,
        computer_browser="edge",
        browser_options=(),
        profile_display_path="",
        platform_supported=True,
        generation=0,
    )


def test_generic_provider_syncs_downscaled_transform_to_frame_and_controller(tmp_path, monkeypatch):
    import threading
    from core.computer.actions import parse_computer_action
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner
    from tests.computer_fakes import FakePlaywrightRuntime

    big_png = _real_png_bytes(2560, 1440, pad=128)
    runtime = FakePlaywrightRuntime(screenshot_bytes=big_png)
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        viewport=(2560, 1440),
    )
    cap = ComputerCapability(
        enabled=True, protocol="generic", targets=("browser",), browser="edge"
    )
    owner = ComputerOwner("s1", "t1", "s1", threading.Event())
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        settings_loader=_fake_settings_snapshot,
        generation_provider=lambda: 0,
    )
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 5000)
    snap = None
    try:
        snap = service.start_run(owner, cap, target="browser")
        obs = service.execute(
            snap.run_id,
            owner,
            parse_computer_action({"kind": "observe", "target": "browser"}),
        )
        assert obs.frame_id
        provider_generic._observation_followup_message(
            obs, run_id=snap.run_id, ordinal=1, controller=service
        )
        frame = service.get_observation_frame(snap.run_id, obs.frame_id)
        assert frame is not None
        assert frame.coordinate_input_enabled is True
        assert frame.image_width == 1280
        assert frame.image_height == 720
        assert frame.model_to_css_x == pytest.approx(2.0)
        assert frame.model_to_css_y == pytest.approx(2.0)

        service.execute(
            snap.run_id,
            owner,
            parse_computer_action({"kind": "click", "target": "browser", "x": 100, "y": 50}),
        )
        assert ("click", 200, 100, "left") in backend.active_page.mouse.calls
    finally:
        if snap is not None:
            service.stop(snap.run_id, "test")
        backend.close()


def test_generic_provider_disables_coordinate_input_when_oversize_and_omitted(tmp_path, monkeypatch):
    import threading
    from core.computer.actions import parse_computer_action
    from core.computer.browser_backend import BrowserComputerBackend
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner
    from tests.computer_fakes import FakePlaywrightRuntime

    big_png = _real_png_bytes(640, 480, pad=128)
    runtime = FakePlaywrightRuntime(screenshot_bytes=big_png)
    backend = BrowserComputerBackend(
        "edge",
        tmp_path / "edge",
        playwright_factory=lambda: runtime,
        viewport=(640, 480),
    )
    cap = ComputerCapability(
        enabled=True, protocol="generic", targets=("browser",), browser="edge"
    )
    owner = ComputerOwner("s1", "t1", "s1", threading.Event())
    service = ComputerControlService(
        backend_factory=lambda target, identity: backend,
        settings_loader=_fake_settings_snapshot,
        generation_provider=lambda: 0,
    )
    monkeypatch.setattr(provider_generic, "_OBSERVATION_IMAGE_MAX_BYTES", 8)
    monkeypatch.setattr(provider_generic, "_downscale_png", lambda _b: None)
    snap = None
    try:
        snap = service.start_run(owner, cap, target="browser")
        obs = service.execute(
            snap.run_id,
            owner,
            parse_computer_action({"kind": "observe", "target": "browser"}),
        )
        message = provider_generic._observation_followup_message(
            obs, run_id=snap.run_id, ordinal=1, controller=service
        )
        assert message["_computer_v1"]["image_omitted"] == "too_large"
        frame = service.get_observation_frame(snap.run_id, obs.frame_id)
        assert frame is not None
        assert frame.coordinate_input_enabled is False
        with pytest.raises(ComputerControlError) as err:
            service.execute(
                snap.run_id,
                owner,
                parse_computer_action({"kind": "click", "target": "browser", "x": 10, "y": 10}),
            )
        assert err.value.code == "computer_invalid_action"
        assert backend.active_page.mouse.calls == []
    finally:
        if snap is not None:
            service.stop(snap.run_id, "test")
        backend.close()


def test_identical_sha_across_surface_generation_is_not_marked_unchanged(tmp_path):
    import threading
    from core.computer.actions import parse_computer_action
    from core.computer.controller import ComputerControlService
    from core.computer.models import ComputerCapability, ComputerOwner

    png_path = tmp_path / "same.png"
    png_path.write_bytes(_real_png_bytes(400, 300))
    same_sha = "samehash" * 8
    generations = iter([1, 2])

    class _FakeBackend:
        target = "browser"
        def start(self): pass
        def execute(self, run_id, action): pass
        def release_inputs(self): pass
        def close(self): pass
        def is_reusable(self): return True
        def observe(self, run_id):
            gen = next(generations)
            return ComputerObservation(
                run_id=run_id,
                target="browser",
                action="observe",
                ok=True,
                screen_sha256=same_sha,
                png_path=str(png_path),
                width=400,
                height=300,
                original_width=400,
                original_height=300,
                surface_id=f"page-{gen}",
                surface_generation=gen,
                frame_id=f"frame-{gen}",
            )

    cap = ComputerCapability(enabled=True, protocol="generic", targets=("browser",), browser="edge")
    owner = ComputerOwner("s1", "t1", "s1", threading.Event())
    service = ComputerControlService(
        backend_factory=lambda target, identity: _FakeBackend(),
        settings_loader=_fake_settings_snapshot,
        generation_provider=lambda: 0,
    )
    snap = None
    try:
        snap = service.start_run(owner, cap, target="browser")
        obs1 = service.execute(snap.run_id, owner, parse_computer_action({"kind": "observe", "target": "browser"}))
        obs2 = service.execute(snap.run_id, owner, parse_computer_action({"kind": "click", "target": "browser", "x": 1, "y": 1}))
        assert obs1.frame_id != obs2.frame_id
        assert obs2.screen_unchanged is False
    finally:
        if snap is not None:
            service.stop(snap.run_id, "test")


def test_strip_media_retains_active_computer_frame_across_trailing_unchanged_text(monkeypatch):
    import core.llm_dialects as dialects

    monkeypatch.setattr(dialects, "_supports_vision_llm_first", lambda model: True)
    obs1 = {
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,ACTIVE_FRAME"}},
        ],
        "_computer_v1": {"run_id": "run-cur", "ordinal": 1, "target": "browser", "frame_id": "f-1"},
    }
    obs2 = {
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】本次动作后画面与上一张截图完全相同，已省略重复图片\nframe=f-1"},
        ],
        "_computer_v1": {
            "run_id": "run-cur",
            "ordinal": 2,
            "target": "browser",
            "frame_id": "f-1",
            "image_omitted": "unchanged",
        },
    }
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "打开页面"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "computer_action", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        obs1,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "computer_action", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        obs2,
    ]
    cleaned, _folded, _keep = dialects._strip_media_and_merge(
        messages, "openai/vision-model"
    )
    assert any(
        isinstance(m.get("content"), list)
        and any(b.get("type") == "image_url" and "ACTIVE_FRAME" in b["image_url"]["url"] for b in m["content"] if isinstance(b, dict))
        for m in cleaned
    )


def test_strip_media_still_folds_plain_chat_image_and_cross_run_boundaries(monkeypatch):
    import core.llm_dialects as dialects

    monkeypatch.setattr(dialects, "_supports_vision_llm_first", lambda model: True)
    plain_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD_CHAT"}},
            ],
        },
        {"role": "assistant", "content": "已看"},
        {"role": "user", "content": "继续纯文本问题"},
    ]
    cleaned_plain, _, _ = dialects._strip_media_and_merge(
        plain_messages, "openai/vision-model"
    )
    assert "OLD_CHAT" not in str(cleaned_plain)

    cross_run_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "旧run"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,OLD_RUN"}},
            ],
            "_computer_v1": {"run_id": "run-old", "ordinal": 1, "target": "browser"},
        },
        {"role": "assistant", "content": "旧任务完成"},
        {"role": "user", "content": "开始新任务"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "computer_action", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "新run无图"}],
            "_computer_v1": {"run_id": "run-new", "ordinal": 1, "target": "browser", "image_omitted": "unchanged"},
        },
    ]
    cleaned_cross, _, _ = dialects._strip_media_and_merge(
        cross_run_messages, "openai/vision-model"
    )
    assert "OLD_RUN" not in str(cleaned_cross)


def test_strip_media_drops_old_frame_when_current_frame_no_screenshot(monkeypatch):
    import core.llm_dialects as dialects

    monkeypatch.setattr(dialects, "_supports_vision_llm_first", lambda model: True)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "帧1"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,STALE_FRAME"}},
            ],
            "_computer_v1": {"run_id": "run-1", "ordinal": 1, "target": "browser", "frame_id": "f-1"},
        },
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "computer_action", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "本次没有截图"}],
            "_computer_v1": {"run_id": "run-1", "ordinal": 2, "target": "browser", "frame_id": "f-2", "image_omitted": "no_screenshot"},
        },
    ]
    cleaned, _, _ = dialects._strip_media_and_merge(messages, "openai/vision-model")
    assert "STALE_FRAME" not in str(cleaned)


def test_fold_old_media_for_estimate_retains_active_computer_frame():
    from core.llm import _fold_old_media_for_estimate

    blob = "Z" * 1000
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "帧1"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{blob}"}},
            ],
            "_computer_v1": {"run_id": "run-1", "ordinal": 1, "target": "browser"},
        },
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "computer_action", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "画面未变"}],
            "_computer_v1": {"run_id": "run-1", "ordinal": 2, "target": "browser", "image_omitted": "unchanged"},
        },
    ]
    folded = _fold_old_media_for_estimate(messages)
    assert blob in str(folded[0])
