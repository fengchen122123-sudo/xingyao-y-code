import io

import pytest
from PIL import Image

from core.screen_snip_native import SnipOverlayNative, clamp, normalize_rect


def test_clamp_keeps_value_within_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_bounds_and_overflow():
    assert clamp(-3, 0, 10) == 0, "低于下界夹到下界"
    assert clamp(13, 0, 10) == 10, "高于上界夹到上界"
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


def test_normalize_rect_forward_drag():
    assert normalize_rect(10, 10, 100, 80, 1920, 2280) == (10, 10, 90, 70)


def test_normalize_rect_reverse_drag():
    assert normalize_rect(100, 80, 10, 10, 1920, 2280) == (10, 10, 90, 70)


def test_normalize_rect_zero_area_returns_none():
    assert normalize_rect(5, 5, 5, 5, 1920, 2280) is None


def test_normalize_rect_clamps_fully_overflowing_drag():
    assert normalize_rect(-50, -50, 5000, 5000, 1920, 2280) == (0, 0, 1920, 2280)


def test_normalize_rect_clamps_partial_overflow():
    assert normalize_rect(-20, 10, 100, 50, 1920, 2280) == (0, 10, 100, 40)


def test_overlay_init_state():
    overlay = SnipOverlayNative(b"png", lambda action, rect: None)
    assert overlay._sel is None
    assert overlay._anchor is None
    assert overlay._closed is False
    assert overlay.closed is False
    assert SnipOverlayNative.MIN_SEL == 8


def test_png_bytes_to_bitmap_roundtrip():
    pytest.importorskip("clr", reason="pythonnet 未安装，跳过 GDI 位图测试")
    from core import screen_snip_native

    try:
        screen_snip_native._load()
    except Exception as exc:
        pytest.skip(f"System.Drawing 加载失败：{exc}")

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="PNG")
    bitmap = screen_snip_native.png_bytes_to_bitmap(buf.getvalue())
    try:
        assert bitmap.Width == 8 and bitmap.Height == 8
    finally:
        bitmap.Dispose()

from core import screen_snip_native  # noqa: E402  （追加段需 monkeypatch _load）


def _make_overlay():
    return SnipOverlayNative(b"png", lambda action, rect, strokes: None)


def test_pen_default_state():
    overlay = _make_overlay()
    assert overlay._mode == "select"
    assert overlay._pen_color == (239, 68, 68)
    assert overlay._strokes == []
    assert SnipOverlayNative.PEN_WIDTH == 3
    assert len(SnipOverlayNative.PEN_COLORS) == 6


def test_pen_stroke_record():
    overlay = _make_overlay()
    overlay._start_stroke(1, 2)
    overlay._extend_stroke(3, 4)
    overlay._extend_stroke(5, 6)
    overlay._end_stroke()

    assert len(overlay._strokes) == 1
    stroke = overlay._strokes[0]
    assert stroke["points"] == [(1, 2), (3, 4), (5, 6)]
    assert stroke["color"] == (239, 68, 68)
    assert stroke["width"] == SnipOverlayNative.PEN_WIDTH
    assert overlay._current_stroke is None


def test_pen_toggle_mode(monkeypatch):
    monkeypatch.setattr(screen_snip_native, "_load", lambda: (None, None, None))
    overlay = _make_overlay()
    assert overlay._mode == "select"

    overlay._toggle_pen_mode()
    assert overlay._mode == "pen"

    overlay._toggle_pen_mode()
    assert overlay._mode == "select"


def test_pen_set_color(monkeypatch):
    monkeypatch.setattr(screen_snip_native, "_load", lambda: (None, None, None))
    overlay = _make_overlay()

    overlay._set_pen_color((0, 0, 0))

    assert overlay._pen_color == (0, 0, 0)


def test_pen_clear_strokes():
    overlay = _make_overlay()
    overlay._start_stroke(1, 2)
    overlay._extend_stroke(3, 4)
    overlay._end_stroke()
    assert len(overlay._strokes) == 1

    overlay._clear_strokes()

    assert overlay._strokes == []
    assert overlay._current_stroke is None


class _FakeControls:

    def __init__(self):
        self.items = []

    def Add(self, control):
        self.items.append(control)


class _FakeEvent:

    def __iadd__(self, handler):
        return self


class _FakeControl:

    def __init__(self):
        self.Controls = _FakeControls()
        self.Click = _FakeEvent()
        self.Text = ""
        self.AutoSize = False
        self.AutoSizeMode = None
        self.FlowDirection = None
        self.BackColor = None
        self.ForeColor = None
        self.FlatStyle = None
        self.Cursor = None
        self.Padding = None
        self.Size = None


class _FakeColor:

    White = "White"

    @staticmethod
    def FromArgb(*args):
        return ("argb",) + args


def _install_fake_winforms(monkeypatch):
    from types import SimpleNamespace

    fake_system = SimpleNamespace(EventHandler=lambda handler: handler)
    fake_f = SimpleNamespace(
        FlowLayoutPanel=_FakeControl,
        Button=_FakeControl,
        FlowDirection=SimpleNamespace(LeftToRight="LeftToRight"),
        AutoSizeMode=SimpleNamespace(GrowAndShrink="GrowAndShrink"),
        FlatStyle=SimpleNamespace(Flat="Flat"),
        Cursors=SimpleNamespace(Hand="Hand"),
        EventHandler=lambda handler: handler,
        Padding=lambda value: ("Padding", value),
    )
    fake_d = SimpleNamespace(
        Color=_FakeColor,
        Size=lambda w, h: (w, h),
    )
    monkeypatch.setattr(
        screen_snip_native, "_load", lambda: (fake_system, fake_f, fake_d)
    )
    return fake_f


def test_toolbar_shrinks_to_content(monkeypatch):
    fake_f = _install_fake_winforms(monkeypatch)
    overlay = _make_overlay()

    overlay._build_toolbar(_FakeControl())

    panel = overlay._toolbar
    assert panel.AutoSize is True
    assert panel.AutoSizeMode == fake_f.AutoSizeMode.GrowAndShrink
    assert len(panel.Controls.items) == 12
