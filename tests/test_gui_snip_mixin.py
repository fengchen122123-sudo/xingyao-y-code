"""gui_snip_mixin.SnipBridgeMixin 的 headless 测试。

用 Dummy(Mixin) 组合类 + monkeypatch 捕获/客户区/剪贴板桩，覆盖：坐标换算
与夹紧、dataURL 校验、DIB 打包头字段、捕获编排（DPI 换算 + 越界夹紧）、
剪贴板/保存桥的分支与非 win32 拒绝。绝不触碰真实剪贴板与真实截屏。
"""

import base64
import io
import struct
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

import gui_snip_mixin
from gui_snip_mixin import (
    SnipBridgeMixin,
    decode_png_data_url,
    dib_bytes_from_image,
    physical_bbox_for_css,
)


class Dummy(SnipBridgeMixin):
    def __init__(self, form=None):
        self._window = SimpleNamespace(uid="master")
        self._form = form
        # 覆盖宿主窗体查找：headless 测试没有 winforms BrowserView 实例
        self._main_form = lambda: self._form


def make_png_data_url(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def fake_form(device_dpi=96, hwnd=12345):
    return SimpleNamespace(Handle=SimpleNamespace(ToInt32=lambda: hwnd), DeviceDpi=device_dpi)


# ------------------------------------------------------- physical_bbox_for_css

def test_bbox_scales_each_component_and_translates_by_origin():
    bbox = physical_bbox_for_css(
        {"x": 10, "y": 20, "width": 100, "height": 40},
        scale=1.25,
        client_origin=(100, 50),
        client_size=(1600, 900),
    )
    assert bbox == (100 + 12, 50 + 25, 100 + 12 + 125, 50 + 25 + 50)


def test_bbox_clamps_to_client_rect():
    # 面板短暂上报越界矩形时截到客户区内，绝不捕获窗口外内容
    bbox = physical_bbox_for_css(
        {"x": 1500, "y": -30, "width": 400, "height": 1000},
        scale=1.0,
        client_origin=(0, 0),
        client_size=(1600, 900),
    )
    assert bbox == (1500, 0, 1600, 900)


def test_bbox_rejects_invalid_and_tiny_results():
    assert physical_bbox_for_css("not-a-dict", 1.0, (0, 0), (100, 100)) is None
    assert physical_bbox_for_css({"x": 0, "y": 0, "width": -5, "height": 5}, 1.0, (0, 0), (100, 100)) is None
    assert physical_bbox_for_css({"x": "bad", "y": 0, "width": 5, "height": 5}, 1.0, (0, 0), (100, 100)) is None
    # 完全落在客户区外：夹紧后不足 2×2
    assert physical_bbox_for_css({"x": 500, "y": 500, "width": 50, "height": 50}, 1.0, (0, 0), (100, 100)) is None


# -------------------------------------------------------- decode_png_data_url

def test_decode_png_round_trip():
    png = make_png_data_url(Image.new("RGB", (3, 2), (10, 200, 30)))
    raw = decode_png_data_url(png)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")


def test_decode_png_rejects_bad_input():
    with pytest.raises(ValueError):
        decode_png_data_url("data:image/jpeg;base64,AAAA")
    with pytest.raises(ValueError):
        decode_png_data_url("data:image/png;base64,!!!not-base64!!!")
    with pytest.raises(ValueError):
        # 合法 base64 但不是 PNG 签名
        decode_png_data_url("data:image/png;base64," + base64.b64encode(b"junk").decode())
    with pytest.raises(ValueError):
        decode_png_data_url(None)


def test_decode_png_rejects_oversize(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin, "_MAX_PNG_BYTES", 4)
    png = make_png_data_url(Image.new("RGB", (8, 8)))
    with pytest.raises(ValueError):
        decode_png_data_url(png)


# --------------------------------------------------------- dib_bytes_from_image

def test_dib_header_and_pixel_layout():
    img = Image.new("RGB", (2, 2))
    # 顶行 (0,0) 红、(0,1) 绿；底行蓝、白 —— 验证底向上 + BGR
    pixels = img.load()
    pixels[0, 0] = (255, 0, 0)
    pixels[1, 0] = (0, 255, 0)
    pixels[0, 1] = (0, 0, 255)
    pixels[1, 1] = (255, 255, 255)

    dib = dib_bytes_from_image(img)
    (bi_size, w, h, planes, bitcount, compression, size_image, *_rest) = struct.unpack(
        "<IiiHHIIiiII", dib[:40]
    )
    stride = ((2 * 3 + 3) // 4) * 4  # = 8
    assert (bi_size, w, h, planes, bitcount, compression, size_image) == (40, 2, 2, 1, 24, 0, stride * 2)

    body = dib[40:]
    assert len(body) == stride * 2
    # 底向上：第一行像素是图的最后一行（x=0 蓝、x=1 白），列序 BGR，行尾补齐
    assert body[0:8] == b"\xff\x00\x00" + b"\xff\xff\xff" + b"\x00\x00"
    # 第二行是图的第一行（x=0 红、x=1 绿）
    assert body[8:16] == b"\x00\x00\xff" + b"\x00\xff\x00" + b"\x00\x00"


# ------------------------------------------------------------ capture 编排

def test_capture_requires_windows(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "linux")
    assert Dummy().capture_right_panel_region({"x": 0, "y": 0, "width": 10, "height": 10})[
        "ok"
    ] is False


def test_capture_rejects_invalid_rect_before_any_work(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    d = Dummy(form=fake_form())
    assert d.capture_right_panel_region({"x": 0, "width": 10})["ok"] is False
    assert d.capture_right_panel_region("junk")["ok"] is False


def test_capture_uses_scale_and_client_frame(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    grabbed = []
    grab_img = Image.new("RGB", (40, 20), (1, 2, 3))

    def fake_grab(bbox):
        grabbed.append(bbox)
        return grab_img

    monkeypatch.setattr(gui_snip_mixin, "default_panel_grab", fake_grab)
    monkeypatch.setattr(
        gui_snip_mixin,
        "_window_client_frame",
        lambda hwnd: ((100, 50), (1600, 900)),
    )
    d = Dummy(form=fake_form(device_dpi=120))  # scale = 1.25
    res = d.capture_right_panel_region({"x": 8, "y": 4, "width": 32, "height": 16})
    assert res["ok"] is True
    assert res["width"] == 40 and res["height"] == 20
    assert res["data_url"].startswith("data:image/png;base64,")
    # 物理区域 = 客户区原点 + css×1.25
    assert grabbed == [(110, 55, 110 + 40, 55 + 20)]


def test_capture_reports_failure_when_grab_returns_none(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    monkeypatch.setattr(gui_snip_mixin, "default_panel_grab", lambda bbox: None)
    monkeypatch.setattr(gui_snip_mixin, "_window_client_frame", lambda hwnd: ((0, 0), (800, 600)))
    d = Dummy(form=fake_form())
    assert d.capture_right_panel_region({"x": 0, "y": 0, "width": 10, "height": 10})["ok"] is False


def test_capture_downsizes_oversize_payload(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    big = Image.new("RGB", (2200, 1100))
    monkeypatch.setattr(gui_snip_mixin, "default_panel_grab", lambda bbox: big)
    monkeypatch.setattr(gui_snip_mixin, "_window_client_frame", lambda hwnd: ((0, 0), (4000, 2000)))
    monkeypatch.setattr(gui_snip_mixin, "_MAX_B64_BYTES", 10)  # 强制走缩小重编码
    d = Dummy(form=fake_form())
    res = d.capture_right_panel_region({"x": 0, "y": 0, "width": 2200, "height": 1100})
    assert res["ok"] is True
    assert res["width"] == 1920 and res["height"] == 960


# ------------------------------------------------------------ 剪贴板 / 保存

def test_snip_copy_validates_then_writes(monkeypatch):
    d = Dummy(form=fake_form())
    writes = []
    monkeypatch.setattr(gui_snip_mixin, "_set_clipboard_dib", lambda dib, hwnd: writes.append((dib, hwnd)) or True)
    data_url = make_png_data_url(Image.new("RGB", (4, 4)))

    assert d.snip_copy_clipboard(data_url) == {"ok": True}
    assert writes and writes[0][1] == 12345

    bad = d.snip_copy_clipboard("data:image/png;base64,???")
    assert bad["ok"] is False and "msg" in bad


def test_set_clipboard_dib_non_windows_is_false(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "linux")
    assert gui_snip_mixin._set_clipboard_dib(b"\x00" * 8, 0) is False


def test_snip_save_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    picked = []

    def fake_dialog(dialog, **kwargs):
        picked.append((dialog, kwargs))
        return str(tmp_path / "out.png")

    d = Dummy()
    d._window = SimpleNamespace(create_file_dialog=fake_dialog)
    data_url = make_png_data_url(Image.new("RGB", (5, 5)))

    res = d.snip_save_image(data_url, "shot.png")
    assert res["ok"] is True and res["path"].endswith("out.png")
    assert (tmp_path / "out.png").read_bytes().startswith(b"\x89PNG")
    dialog, kwargs = picked[0]
    assert kwargs["save_filename"] == "shot.png"


def test_snip_save_cancelled_and_invalid(monkeypatch, tmp_path):
    d = Dummy()
    d._window = SimpleNamespace(create_file_dialog=lambda *a, **k: None)
    data_url = make_png_data_url(Image.new("RGB", (5, 5)))

    res = d.snip_save_image(data_url)
    assert res["ok"] is False and res["msg"] == "已取消选择"

    bad = d.snip_save_image("junk")
    assert bad["ok"] is False


def test_snip_save_appends_png_suffix(tmp_path, monkeypatch):
    d = Dummy()
    d._window = SimpleNamespace(
        create_file_dialog=lambda *a, **k: str(tmp_path / "noext")
    )
    res = d.snip_save_image(make_png_data_url(Image.new("RGB", (5, 5))))
    assert res["ok"] is True and res["path"].endswith("noext.png")
