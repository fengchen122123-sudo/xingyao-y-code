"""core.screen_snip 的 headless 测试：遮罩页模板注入与虚拟桌面度量。

刻意不调用 capture_virtual_screen：真实抓屏会把用户屏幕冻结帧拉进测试进程
（既无必要也踩隐私红线）。build_snip_page 只做字符串注入，get_virtual_screen_rect
只读 Win32 度量常量，全程不需要任何真实窗口。
"""

import base64
import sys
from types import SimpleNamespace

import pytest

import core.screen_snip as screen_snip
from core.screen_snip import build_snip_page, get_virtual_screen_rect

_PNG_PREFIX = "data:image/png;base64,"


def png_data_url(payload: bytes = b"any-content") -> str:
    return _PNG_PREFIX + base64.b64encode(payload).decode("ascii")


# -------------------------------------------------------------- build_snip_page

def test_build_snip_page_injects_frame_once_and_keeps_critical_elements():
    data_url = png_data_url()
    page = build_snip_page(data_url, 1920, 2280)

    assert page.count(data_url) == 1  # 冻结帧原样注入且只注入一次
    for placeholder in ("__SNIP_FRAME_DATA_URL__", "__SNIP_ORIG_W__", "__SNIP_ORIG_H__"):
        assert placeholder not in page  # 无占位符残留
    assert page.lstrip().startswith("<!DOCTYPE html>")
    # 原始尺寸必须注入：预览可能被压缩，选区换算以原始尺寸为准
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
        "data:text/plain;base64,QUJD",  # 非图片 MIME
        "data:image/png,QUJD",  # 缺 ;base64,
        "",  # 空串
        None,  # 非字符串
        b"\x89PNG\r\n\x1a\n",  # bytes 也不接受
    ],
)
def test_build_snip_page_rejects_non_image_data_url(bad):
    with pytest.raises(ValueError):
        build_snip_page(bad, 1920, 2280)


# ------------------------------------------------------- get_virtual_screen_rect

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
