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
    backend._check_coordinates(799, 599)
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
    assert metrics["original_width"] == 1280
    assert metrics["original_height"] == 800
    assert metrics["scale_x"] == 1.0
    assert metrics["scale_y"] == 1.0


def test_normalize_frame_resamples_physical_pixels_to_css_viewport():
    import io

    from PIL import Image

    backend = _backend()

    class _FakePage:
        def evaluate(self, _script):
            return {"w": 1280, "h": 800, "dpr": 1.25}

    buffer = io.BytesIO()
    Image.new("RGB", (1600, 1000), (255, 255, 255)).save(buffer, format="PNG")
    normalized = backend._normalize_frame(buffer.getvalue(), _FakePage())
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.size == (1280, 800)


def test_normalize_frame_keeps_image_when_size_matches():
    import io

    from PIL import Image

    backend = _backend()

    class _FakePage:
        def evaluate(self, _script):
            return {"w": 1280, "h": 800, "dpr": 1.0}

    buffer = io.BytesIO()
    Image.new("RGB", (1280, 800), (255, 255, 255)).save(buffer, format="PNG")
    original = buffer.getvalue()
    assert backend._normalize_frame(original, _FakePage()) is original


def test_observation_viewport_falls_back_to_project_viewport_on_error():
    backend = _backend(viewport=(800, 600))

    class _BrokenPage:
        def evaluate(self, _script):
            raise RuntimeError("context gone")

    metrics = backend._observation_viewport(_BrokenPage())
    assert metrics["width"] == 800
    assert metrics["height"] == 600
    assert metrics["scale_x"] == 1.0


def test_connect_with_retry_aborts_immediately_when_stop_event_sets(monkeypatch):
    import threading
    import time

    import core.computer.embedded_browser_backend as emb

    monkeypatch.setattr(emb, "_CDP_READY_POLL_INTERVAL", 2.0)
    backend = _backend()

    class _FailingChromium:
        def connect_over_cdp(self, _endpoint):
            raise RuntimeError("not ready yet")

    class _FakePlaywright:
        chromium = _FailingChromium()

    backend._playwright = _FakePlaywright()
    backend._start_timeout = 10.0

    def trigger_stop():
        time.sleep(0.03)
        backend._worker_stop.set()

    stopper = threading.Thread(target=trigger_stop, daemon=True)
    stopper.start()
    started = time.monotonic()
    with pytest.raises(ComputerControlError) as excinfo:
        backend._connect_with_retry()
    elapsed = time.monotonic() - started
    stopper.join(timeout=1.0)
    assert excinfo.value.code == "computer_browser_closed"
    assert elapsed < 0.5
