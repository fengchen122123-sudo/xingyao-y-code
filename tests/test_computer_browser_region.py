"""browser 局部观察（observe_region，E5 2026-11-03）的 fake 层测试。

覆盖：区域裁剪尺寸/origin、区域坐标系下的点击换算、越界拒绝、回到整屏观察
后坐标复位。全部走 tests.computer_fakes，绝不启动真实浏览器。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from core.computer.actions import parse_computer_action
from core.computer.browser_backend import BrowserComputerBackend
from core.computer.models import ComputerControlError
from tests.computer_fakes import FakePlaywrightRuntime, fake_png_bytes


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (255, 255, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def backend(tmp_path):
    runtime = FakePlaywrightRuntime(screenshot_bytes=fake_png_bytes(7))
    instance = BrowserComputerBackend(
        "edge", tmp_path / "edge", playwright_factory=lambda: runtime, viewport=(400, 300)
    )
    instance.start()
    yield instance
    instance.close()


def _observe_region(x=100, y=50, width=200, height=120):
    return parse_computer_action(
        {
            "kind": "observe_region",
            "target": "browser",
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        }
    )


def test_observe_region_crops_region_and_sets_origin(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    backend.observe("run-a")

    backend.execute("run-a", _observe_region())
    observation = backend.observe("run-a")

    assert observation.action == "observe_region"
    assert (observation.width, observation.height) == (200, 120)
    assert (observation.original_width, observation.original_height) == (200, 120)
    assert (observation.origin_x, observation.origin_y) == (100, 50)
    assert observation.elements == ()  # 区域观察不附 DOM 清单
    with Image.open(io.BytesIO(Path(observation.png_path).read_bytes())) as image:
        assert image.size == (200, 120)


def test_click_in_region_is_translated_to_css_coordinates(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    backend.observe("run-a")
    backend.execute("run-a", _observe_region(x=100, y=50, width=200, height=120))
    backend.observe("run-a")

    backend.execute(
        "run-a",
        parse_computer_action({"kind": "click", "target": "browser", "x": 10, "y": 20}),
    )
    assert ("click", 110, 70, "left") in page.mouse.calls


def test_region_coordinates_are_bounded_by_region_size(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    backend.observe("run-a")
    backend.execute("run-a", _observe_region(x=100, y=50, width=200, height=120))
    backend.observe("run-a")

    with pytest.raises(ComputerControlError) as raised:
        backend.execute(
            "run-a",
            parse_computer_action(
                {"kind": "click", "target": "browser", "x": 300, "y": 20}
            ),
        )
    assert raised.value.code == "computer_coordinate_out_of_bounds"
    assert "局部观察" in raised.value.message
    assert page.mouse.calls == []


def test_full_observe_resets_coordinate_frame(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    backend.observe("run-a")
    backend.execute("run-a", _observe_region(x=100, y=50, width=200, height=120))
    backend.observe("run-a")

    backend.observe("run-a")  # 回到整屏
    backend.execute(
        "run-a",
        parse_computer_action({"kind": "click", "target": "browser", "x": 10, "y": 20}),
    )
    assert ("click", 10, 20, "left") in page.mouse.calls


def test_observe_region_out_of_viewport_is_rejected(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    backend.observe("run-a")

    with pytest.raises(ComputerControlError) as raised:
        backend.execute("run-a", _observe_region(x=350, y=50, width=200, height=120))
    assert raised.value.code == "computer_coordinate_out_of_bounds"
    assert "拒绝裁剪" in raised.value.message


def test_observe_region_requires_surface_baseline(backend):
    page = backend.active_page
    page.screenshot_bytes = _png(400, 300)
    # 从未 observe 过：页面 token 未知，坐标动作前必须建立基线
    backend.execute("run-a", _observe_region())
    observation = backend.observe("run-a")
    # 首次 observe 不受影响：pending 区域仍然生效
    assert observation.action == "observe_region"
