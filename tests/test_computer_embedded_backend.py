"""内置 WebView2 CDP 直连 backend（core/computer/embedded_browser_backend.py）的
headless 测试：不触碰 Playwright（构造函数铁律），只测身份校验、坐标域与
观察视口 hook。"""

from pathlib import Path

import pytest

from core.computer.embedded_browser_backend import EmbeddedBrowserBackend
from core.computer.models import ComputerControlError


def _backend(endpoint="http://127.0.0.1:9222", **kwargs):
    return EmbeddedBrowserBackend(endpoint, Path("."), **kwargs)


def test_endpoint_must_be_loopback():
    with pytest.raises(ComputerControlError) as excinfo:
        EmbeddedBrowserBackend("http://0.0.0.0:9222", Path("."))
    assert excinfo.value.code == "computer_invalid_config"
    with pytest.raises(ComputerControlError):
        EmbeddedBrowserBackend("", Path("."))


def test_backend_targets_browser_and_never_owns_context():
    backend = _backend()
    assert backend.target == "browser"
    # 浏览器归 GUI 所有：teardown/close 只断开 CDP 连接，绝不关 context
    assert backend._owns_context is False
    assert backend.cdp_endpoint == "http://127.0.0.1:9222"


def test_coordinate_validation_uses_project_viewport():
    backend = _backend(viewport=(800, 600))
    backend._check_coordinates(799, 599)  # 半开区间内不抛
    with pytest.raises(ComputerControlError) as excinfo:
        backend._check_coordinates(800, 0)
    assert excinfo.value.code == "computer_coordinate_out_of_bounds"


def test_observation_viewport_reports_live_metrics_with_dpr():
    backend = _backend()

    class _FakePage:
        def evaluate(self, _script):
            return {"w": 1280, "h": 800, "dpr": 1.25}

    metrics = backend._observation_viewport(_FakePage())
    assert metrics["width"] == 1280
    assert metrics["height"] == 800
    assert metrics["original_width"] == 1600
    assert metrics["original_height"] == 1000
    assert metrics["scale_x"] == 1.25
    assert metrics["scale_y"] == 1.25


def test_observation_viewport_falls_back_to_project_viewport_on_error():
    backend = _backend(viewport=(800, 600))

    class _BrokenPage:
        def evaluate(self, _script):
            raise RuntimeError("context gone")

    metrics = backend._observation_viewport(_BrokenPage())
    assert metrics["width"] == 800
    assert metrics["height"] == 600
    assert metrics["scale_x"] == 1.0
