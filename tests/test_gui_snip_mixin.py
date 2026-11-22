import base64
import io
import logging
import struct
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
        self._main_form = lambda: self._form


def make_png_data_url(image: Image.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class FakeForm:

    def __init__(self, device_dpi=96, hwnd=12345, strict=False, invoke_error=None):
        self._hwnd = hwnd
        self._device_dpi = device_dpi
        self._strict = strict
        self._invoke_error = invoke_error
        self.in_gui_thread = False
        self.invocations = 0

    @property
    def Handle(self):
        if self._strict and not self.in_gui_thread:
            raise RuntimeError("WinForms 跨线程访问：Handle 只能在 GUI 线程读取")
        return SimpleNamespace(ToInt32=lambda: self._hwnd)

    @property
    def DeviceDpi(self):
        if self._strict and not self.in_gui_thread:
            raise RuntimeError("WinForms 跨线程访问：DeviceDpi 只能在 GUI 线程读取")
        return self._device_dpi

    def Invoke(self, delegate):
        if self._invoke_error is not None:
            raise self._invoke_error
        self.invocations += 1
        self.in_gui_thread = True
        try:
            delegate()
        finally:
            self.in_gui_thread = False


def fake_form(device_dpi=96, hwnd=12345, **kwargs):
    return FakeForm(device_dpi=device_dpi, hwnd=hwnd, **kwargs)


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
    assert physical_bbox_for_css({"x": 500, "y": 500, "width": 50, "height": 50}, 1.0, (0, 0), (100, 100)) is None


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
        decode_png_data_url("data:image/png;base64," + base64.b64encode(b"junk").decode())
    with pytest.raises(ValueError):
        decode_png_data_url(None)


def test_decode_png_rejects_oversize(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin, "_MAX_PNG_BYTES", 4)
    png = make_png_data_url(Image.new("RGB", (8, 8)))
    with pytest.raises(ValueError):
        decode_png_data_url(png)


def test_dib_header_and_pixel_layout():
    img = Image.new("RGB", (2, 2))
    pixels = img.load()
    pixels[0, 0] = (255, 0, 0)
    pixels[1, 0] = (0, 255, 0)
    pixels[0, 1] = (0, 0, 255)
    pixels[1, 1] = (255, 255, 255)

    dib = dib_bytes_from_image(img)
    (bi_size, w, h, planes, bitcount, compression, size_image, *_rest) = struct.unpack(
        "<IiiHHIIiiII", dib[:40]
    )
    stride = ((2 * 3 + 3) // 4) * 4
    assert (bi_size, w, h, planes, bitcount, compression, size_image) == (40, 2, 2, 1, 24, 0, stride * 2)

    body = dib[40:]
    assert len(body) == stride * 2
    assert body[0:8] == b"\xff\x00\x00" + b"\xff\xff\xff" + b"\x00\x00"
    assert body[8:16] == b"\x00\x00\xff" + b"\x00\xff\x00" + b"\x00\x00"


def test_dib_3x2_bytes_match_reference_layout():
    img = Image.new("RGB", (3, 2))
    img.putdata([
        (255, 0, 0), (0, 255, 0), (0, 0, 255),
        (10, 20, 30), (40, 50, 60), (70, 80, 90),
    ])

    dib = dib_bytes_from_image(img)

    stride = 12
    assert len(dib) == 40 + stride * 2
    assert dib[:40] == struct.pack("<IiiHHIIiiII", 40, 3, 2, 1, 24, 0, stride * 2, 0, 0, 0, 0)
    assert dib[40:] == (
        b"\x1e\x14\x0a" + b"\x3c\x32\x28" + b"\x5a\x50\x46" + b"\x00" * 3
        + b"\x00\x00\xff" + b"\x00\xff\x00" + b"\xff\x00\x00" + b"\x00" * 3
    )


@pytest.mark.parametrize("size", [(1, 1), (2, 3), (7, 5), (16, 9)])
def test_dib_matches_reference_per_pixel_conversion(size):
    import random

    random.seed(sum(size))
    img = Image.new("RGB", size)
    img.putdata([
        (random.randrange(256), random.randrange(256), random.randrange(256))
        for _ in range(size[0] * size[1])
    ])

    def reference(image: Image.Image) -> bytes:
        rgb = image.convert("RGB")
        width, height = rgb.size
        row_stride = ((width * 3 + 3) // 4) * 4
        header = struct.pack(
            "<IiiHHIIiiII", 40, width, height, 1, 24, 0, row_stride * height, 0, 0, 0, 0
        )
        pixels = bytearray(row_stride * height)
        raw = rgb.tobytes()
        for row in range(height):
            src = row * width * 3
            dst = (height - 1 - row) * row_stride
            for col in range(width):
                r, g, b = raw[src + col * 3: src + col * 3 + 3]
                pixels[dst + col * 3] = b
                pixels[dst + col * 3 + 1] = g
                pixels[dst + col * 3 + 2] = r
        return header + bytes(pixels)

    assert dib_bytes_from_image(img) == reference(img)


def test_dib_uses_pil_batch_bgr_conversion(monkeypatch):
    calls = []
    original = Image.Image.tobytes

    def spy(self, *args, **kwargs):
        calls.append(args)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "tobytes", spy)

    dib_bytes_from_image(Image.new("RGB", (3, 2)))

    assert calls == [("raw", "BGR", 12, -1)]


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
    form = fake_form(device_dpi=120)
    res = Dummy(form=form).capture_right_panel_region({"x": 8, "y": 4, "width": 32, "height": 16})
    assert res["ok"] is True
    assert res["width"] == 40 and res["height"] == 20
    assert res["data_url"].startswith("data:image/png;base64,")
    assert grabbed == [(110, 55, 110 + 40, 55 + 20)]
    assert form.invocations == 1


def test_capture_reads_handle_and_dpi_on_gui_thread(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_snip_mixin, "default_panel_grab", lambda bbox: Image.new("RGB", (40, 20))
    )
    monkeypatch.setattr(
        gui_snip_mixin, "_window_client_frame", lambda hwnd: ((100, 50), (1600, 900))
    )
    form = fake_form(device_dpi=120, strict=True)

    res = Dummy(form=form).capture_right_panel_region({"x": 8, "y": 4, "width": 32, "height": 16})

    assert res["ok"] is True, res
    assert form.invocations == 1


def test_capture_degrades_when_form_invoke_fails(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_snip_mixin, "_window_client_frame", lambda hwnd: ((0, 0), (800, 600))
    )
    form = fake_form(invoke_error=RuntimeError("control disposed"))

    res = Dummy(form=form).capture_right_panel_region({"x": 0, "y": 0, "width": 10, "height": 10})

    assert res["ok"] is False and res["msg"] == "窗口尚未就绪"


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
    monkeypatch.setattr(gui_snip_mixin, "_MAX_B64_BYTES", 10)
    d = Dummy(form=fake_form())
    res = d.capture_right_panel_region({"x": 0, "y": 0, "width": 2200, "height": 1100})
    assert res["ok"] is True
    assert res["width"] == 1920 and res["height"] == 960


def test_snip_copy_validates_then_writes(monkeypatch):
    d = Dummy(form=fake_form())
    writes = []
    monkeypatch.setattr(gui_snip_mixin, "_set_clipboard_dib", lambda dib, hwnd: writes.append((dib, hwnd)) or True)
    data_url = make_png_data_url(Image.new("RGB", (4, 4)))

    assert d.snip_copy_clipboard(data_url) == {"ok": True}
    assert writes and writes[0][1] == 12345

    bad = d.snip_copy_clipboard("data:image/png;base64,???")
    assert bad["ok"] is False and "msg" in bad


def test_snip_copy_logs_handle_access_failure(monkeypatch, caplog):

    class _BadHandle:
        def ToInt32(self):
            raise OSError("handle gone")

    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    seen = []
    monkeypatch.setattr(
        gui_snip_mixin,
        "_set_clipboard_dib",
        lambda dib, hwnd: seen.append(hwnd) or True,
    )
    form = SimpleNamespace(Handle=_BadHandle(), DeviceDpi=96)

    with caplog.at_level(logging.DEBUG, logger="gui_snip_mixin"):
        res = Dummy(form=form).snip_copy_clipboard(make_png_data_url(Image.new("RGB", (2, 2))))

    assert res == {"ok": True}
    assert seen == [0]
    assert any(record.exc_info for record in caplog.records), "句柄读取失败必须留 debug 日志"


def test_set_clipboard_dib_non_windows_is_false(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "linux")
    assert gui_snip_mixin._set_clipboard_dib(b"\x00" * 8, 0) is False


def test_snip_save_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    picked = []

    def fake_dialog(dialog, **kwargs):
        picked.append((dialog, kwargs))
        return str(tmp_path / "out.png")

    d = Dummy(form=fake_form())
    d._window = SimpleNamespace(create_file_dialog=fake_dialog)
    data_url = make_png_data_url(Image.new("RGB", (5, 5)))

    res = d.snip_save_image(data_url, "shot.png")
    assert res["ok"] is True and res["path"].endswith("out.png")
    assert (tmp_path / "out.png").read_bytes().startswith(b"\x89PNG")
    dialog, kwargs = picked[0]
    assert kwargs["save_filename"] == "shot.png"


def test_snip_save_dialog_runs_on_gui_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    form = fake_form(strict=True)
    target = tmp_path / "out.png"

    def fake_dialog(dialog, **kwargs):
        assert form.in_gui_thread, "文件对话框必须在 GUI 线程调用"
        return str(target)

    d = Dummy(form=form)
    d._window = SimpleNamespace(create_file_dialog=fake_dialog)

    res = d.snip_save_image(make_png_data_url(Image.new("RGB", (5, 5))), "shot.png")

    assert res["ok"] is True and res["path"] == str(target)
    assert form.invocations == 1


def test_snip_save_without_form_degrades_without_opening_dialog(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    called = []
    d = Dummy(form=None)
    d._window = SimpleNamespace(create_file_dialog=lambda *a, **k: called.append(1))

    res = d.snip_save_image(make_png_data_url(Image.new("RGB", (5, 5))))

    assert res == {"ok": False, "msg": "窗口尚未就绪"}
    assert called == []


def test_snip_save_cancelled_and_invalid(monkeypatch, tmp_path):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    d = Dummy(form=fake_form())
    d._window = SimpleNamespace(create_file_dialog=lambda *a, **k: None)
    data_url = make_png_data_url(Image.new("RGB", (5, 5)))

    res = d.snip_save_image(data_url)
    assert res["ok"] is False and res["msg"] == "已取消选择"

    bad = d.snip_save_image("junk")
    assert bad["ok"] is False


def test_snip_save_appends_png_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    d = Dummy(form=fake_form())
    d._window = SimpleNamespace(
        create_file_dialog=lambda *a, **k: str(tmp_path / "noext")
    )
    res = d.snip_save_image(make_png_data_url(Image.new("RGB", (5, 5))))
    assert res["ok"] is True and res["path"].endswith("noext.png")


class _RecordingWin32Func:

    def __init__(self, kind):
        self.kind = kind
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, hwnd, ptr):
        self.calls.append(hwnd)
        out = ptr._obj
        if self.kind == "origin":
            out.x, out.y = 100, 50
        else:
            out.right, out.bottom = 1600, 900
        return 1


def test_window_client_frame_declares_ctypes_signatures(monkeypatch):
    client_to_screen = _RecordingWin32Func("origin")
    get_client_rect = _RecordingWin32Func("rect")
    user32 = SimpleNamespace(
        ClientToScreen=client_to_screen, GetClientRect=get_client_rect
    )
    monkeypatch.setattr(
        gui_snip_mixin.ctypes, "windll", SimpleNamespace(user32=user32), raising=False
    )

    big_hwnd = 0x1_2345_6789
    origin, size = gui_snip_mixin._window_client_frame(big_hwnd)

    assert (origin, size) == ((100, 50), (1600, 900))
    assert client_to_screen.calls == [big_hwnd]
    for func in (client_to_screen, get_client_rect):
        assert func.argtypes[0] is gui_snip_mixin.ctypes.c_void_p, "HWND 声明为 c_void_p"
        assert func.restype is gui_snip_mixin.ctypes.c_bool


def test_window_client_frame_raises_on_win32_failure(monkeypatch):
    monkeypatch.setattr(
        gui_snip_mixin.ctypes,
        "windll",
        SimpleNamespace(
            user32=SimpleNamespace(
                ClientToScreen=lambda *a: 0, GetClientRect=lambda *a: 1
            )
        ),
        raising=False,
    )

    with pytest.raises(OSError):
        gui_snip_mixin._window_client_frame(12345)


class FakeCaptureImage:

    def __init__(self, size=(1920, 2280)):
        self.size = size
        buf = io.BytesIO()
        Image.new("RGB", (2, 2), (1, 2, 3)).save(buf, format="PNG")
        self.png = buf.getvalue()

    def save(self, buf, format=None, **kwargs):
        buf.write(self.png)


def patch_native_snip_env(monkeypatch, image=None):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "win32")
    monkeypatch.setattr(
        gui_snip_mixin, "default_virtual_screen_rect", lambda: (0, -1080, 1920, 2280)
    )
    monkeypatch.setattr(
        gui_snip_mixin,
        "default_capture_virtual_screen",
        lambda: image if image is not None else FakeCaptureImage(),
    )


def patch_native_overlay(monkeypatch):
    created = []

    class FakeOverlayNative:
        def __init__(self, png_bytes, on_action):
            self.png_bytes = png_bytes
            self.on_action = on_action
            self.show_invokers = []
            self._closed = False
            created.append(self)

        @property
        def closed(self):
            return self._closed

        def show(self, invoker):
            self.show_invokers.append(invoker)
            return True

    monkeypatch.setattr("core.screen_snip_native.SnipOverlayNative", FakeOverlayNative)
    return created


def test_screen_snip_start_uses_native_overlay(monkeypatch):
    image = FakeCaptureImage()
    patch_native_snip_env(monkeypatch, image=image)
    created = patch_native_overlay(monkeypatch)
    form = fake_form()
    d = Dummy(form=form)

    res = d.screen_snip_start()

    assert res == {"ok": True}
    assert len(created) == 1
    overlay = created[0]
    assert overlay.png_bytes == image.png
    assert overlay.on_action == d._on_snip_action
    assert overlay.show_invokers == [form]
    assert d._screen_snip_overlay is overlay
    assert d._screen_snip_source is image


def test_screen_snip_start_reuses_open_overlay(monkeypatch):
    patch_native_snip_env(monkeypatch)
    created = patch_native_overlay(monkeypatch)
    d = Dummy(form=fake_form())
    d.screen_snip_start()
    assert created[0].closed is False

    res = d.screen_snip_start()

    assert res == {"ok": True, "msg": "截图窗口已打开"}
    assert len(created) == 1, "已有存活蒙版时不得重复创建"


def test_screen_snip_start_rejects_non_windows(monkeypatch):
    monkeypatch.setattr(gui_snip_mixin.sys, "platform", "linux")
    res = Dummy().screen_snip_start()
    assert res["ok"] is False


def test_screen_snip_start_rejects_size_mismatch(monkeypatch):
    patch_native_snip_env(monkeypatch, image=FakeCaptureImage(size=(100, 100)))
    created = patch_native_overlay(monkeypatch)

    res = Dummy(form=fake_form()).screen_snip_start()

    assert res["ok"] is False and "尺寸" in res["msg"]
    assert created == [], "尺寸不匹配不得创建蒙版"


def test_screen_snip_start_fails_without_main_form(monkeypatch):
    patch_native_snip_env(monkeypatch)
    created = patch_native_overlay(monkeypatch)
    d = Dummy()

    res = d.screen_snip_start()

    assert res["ok"] is False and res["msg"] == "主窗口尚未就绪"
    assert created == [], "无主窗体不得创建蒙版"
    assert d._screen_snip_source is None, "失败路径必须释放已捕获的原图"


def test_on_snip_action_send_crops_and_emits(monkeypatch):
    d = Dummy()
    source = FakeCaptureImage()
    d._screen_snip_source = source
    emitted = []
    d.emit_event = lambda event_type, data: emitted.append((event_type, data))
    cropped = []
    monkeypatch.setattr(
        gui_snip_mixin,
        "crop_frozen_frame",
        lambda image, rect: cropped.append((image, rect)) or b"png-bytes",
    )
    monkeypatch.setattr(
        gui_snip_mixin, "save_snip_png", lambda png: r"C:\fake\snip_test.png"
    )

    d._on_snip_action("send", (10, 20, 30, 40))

    assert cropped == [(source, {"x": 10, "y": 20, "w": 30, "h": 40})]
    assert emitted == [("snip_attachment_ready", {"path": r"C:\fake\snip_test.png"})]
    assert d._screen_snip_source is None


def test_on_snip_action_cancel_does_nothing(monkeypatch):
    d = Dummy()
    d._screen_snip_source = FakeCaptureImage()
    emitted = []
    d.emit_event = lambda *a: emitted.append(a)
    calls = []
    monkeypatch.setattr(
        gui_snip_mixin, "crop_frozen_frame", lambda *a: calls.append(a) or b""
    )

    d._on_snip_action("cancel", None)

    assert calls == [], "取消不得裁剪"
    assert emitted == [], "取消不得回传事件"
    assert d._screen_snip_source is None, "取消也要释放原图"

def _white_png(size=(200, 200)):
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def test_draw_strokes_on_png_no_strokes():
    png = _white_png()
    assert gui_snip_mixin.draw_strokes_on_png(png, [], (0, 0)) is png


def test_draw_strokes_on_png_offset():
    png = _white_png((200, 200))
    strokes = [{"color": (255, 0, 0), "width": 3, "points": [(50, 50), (150, 150)]}]

    out = gui_snip_mixin.draw_strokes_on_png(png, strokes, (40, 40))

    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    img = Image.open(io.BytesIO(out)).convert("RGB")
    assert img.size == (200, 200), "合成不得改变尺寸"
    r, g, b = img.getpixel((10, 10))
    assert r > g + 100 and r > b + 100, f"相对点 (10,10) 应偏红，实际 {(r, g, b)}"


def test_draw_strokes_on_png_outside_selection():
    png = _white_png((200, 200))
    strokes = [{"color": (255, 0, 0), "width": 3, "points": [(500, 500), (600, 600)]}]

    out = gui_snip_mixin.draw_strokes_on_png(png, strokes, (0, 0))

    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    img = Image.open(io.BytesIO(out))
    assert img.size == (200, 200)


def test_draw_strokes_on_png_bad_input():
    bad = b"not-a-png"
    strokes = [{"color": (255, 0, 0), "width": 3, "points": [(1, 1), (2, 2)]}]
    # 笔迹是增强，绝不能拖垮主流程：坏输入容错原样返回
    assert gui_snip_mixin.draw_strokes_on_png(bad, strokes, (0, 0)) is bad


def test_on_snip_action_send_with_strokes(monkeypatch):
    d = Dummy()
    source = Image.new("RGB", (100, 100), (9, 9, 9))
    d._screen_snip_source = source
    emitted = []
    d.emit_event = lambda event_type, data: emitted.append((event_type, data))
    cropped = []
    fixed_png = _white_png((30, 40))

    def fake_crop(image, rect):
        cropped.append((image, rect))
        return fixed_png

    monkeypatch.setattr(gui_snip_mixin, "crop_frozen_frame", fake_crop)
    monkeypatch.setattr(
        gui_snip_mixin, "save_snip_png", lambda png: r"C:\fake\snip_strokes.png"
    )

    strokes = [{"color": (255, 0, 0), "width": 3, "points": [(15, 25), (30, 40)]}]
    d._on_snip_action("send", (10, 20, 30, 40), strokes)

    assert cropped == [(source, {"x": 10, "y": 20, "w": 30, "h": 40})]
    assert emitted == [
        ("snip_attachment_ready", {"path": r"C:\fake\snip_strokes.png"})
    ]
    assert d._screen_snip_source is None, "动作完成后必须释放原图"
    assert d._screen_snip_overlay is None


def test_on_snip_action_cancel_ignores_strokes(monkeypatch):
    d = Dummy()
    d._screen_snip_source = Image.new("RGB", (100, 100))
    emitted = []
    d.emit_event = lambda *a: emitted.append(a)
    calls = []
    monkeypatch.setattr(
        gui_snip_mixin, "crop_frozen_frame", lambda *a: calls.append(a) or b""
    )
    saved = []
    monkeypatch.setattr(
        gui_snip_mixin, "save_snip_png", lambda png: saved.append(png) or "x"
    )

    strokes = [{"color": (255, 0, 0), "width": 3, "points": [(15, 25), (30, 40)]}]
    d._on_snip_action("cancel", None, strokes)

    assert calls == [], "取消不得裁剪"
    assert saved == [], "取消不得落盘"
    assert emitted == [], "取消不得回传事件"
    assert d._screen_snip_source is None, "取消也要释放原图"
