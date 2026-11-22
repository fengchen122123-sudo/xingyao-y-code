import base64
import sys
from types import SimpleNamespace

import pytest

import core.screen_snip as screen_snip
from core.screen_snip import build_snip_page, get_virtual_screen_rect

_PNG_PREFIX = "data:image/png;base64,"


def png_data_url(payload: bytes = b"any-content") -> str:
    return _PNG_PREFIX + base64.b64encode(payload).decode("ascii")


def test_build_snip_page_injects_frame_once_and_keeps_critical_elements():
    data_url = png_data_url()
    page = build_snip_page(data_url, 1920, 2280)

    assert page.count(data_url) == 1
    for placeholder in ("__SNIP_FRAME_DATA_URL__", "__SNIP_ORIG_W__", "__SNIP_ORIG_H__"):
        assert placeholder not in page
    assert page.lstrip().startswith("<!DOCTYPE html>")
    assert "var ORIG_W = 1920" in page
    assert "var ORIG_H = 2280" in page
    for needle in (
        'id="frame"',
        "btn-send",
        "btn-copy",
        "btn-save",
        "btn-cancel",
        "snip_send",
        "snip_copy",
        "snip_save",
        "snip_close",
        "bridgeApi",
        "pywebview",
        "function selection",
    ):
        assert needle in page, f"遮罩页缺少关键元素：{needle}"


def test_build_snip_page_rejects_invalid_original_size():
    data_url = png_data_url()
    for bad_size in ((0, 100), (100, 0), (-1, 100), ("x", 100), (None, 100)):
        with pytest.raises(ValueError):
            build_snip_page(data_url, *bad_size)


@pytest.mark.parametrize(
    "bad",
    [
        "data:text/plain;base64,QUJD",
        "data:image/png,QUJD",
        "",
        None,
        b"\x89PNG\r\n\x1a\n",
    ],
)
def test_build_snip_page_rejects_non_image_data_url(bad):
    with pytest.raises(ValueError):
        build_snip_page(bad, 1920, 2280)


@pytest.mark.skipif(sys.platform != "win32", reason="虚拟桌面度量仅 Windows 真实可读")
def test_get_virtual_screen_rect_returns_physical_rect():
    rect = get_virtual_screen_rect()

    assert isinstance(rect, tuple) and len(rect) == 4
    assert all(isinstance(value, int) for value in rect)
    assert rect[2] > 0 and rect[3] > 0


def test_get_virtual_screen_rect_rejects_non_windows(monkeypatch):
    monkeypatch.setattr(screen_snip.sys, "platform", "linux")

    with pytest.raises(RuntimeError):
        get_virtual_screen_rect()


def test_get_virtual_screen_rect_rejects_nonpositive_metrics(monkeypatch):
    monkeypatch.setattr(screen_snip.sys, "platform", "win32")
    monkeypatch.setattr(
        screen_snip.ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(GetSystemMetrics=lambda index: 0)),
        raising=False,
    )

    with pytest.raises(RuntimeError):
        get_virtual_screen_rect()
