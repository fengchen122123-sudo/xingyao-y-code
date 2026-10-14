"""core/screen_snip_native 纯逻辑的无头测试（不弹真实窗口、不依赖 .NET）。

clamp / normalize_rect / SnipOverlayNative.__init__ 均为纯 Python，可无头运行；
png_bytes_to_bitmap 需要 pythonnet 加载 System.Drawing，环境不支持时 skip。
"""

import io

import pytest
from PIL import Image

from core.screen_snip_native import SnipOverlayNative, clamp, normalize_rect


# -------------------------------------------------------------------- clamp

def test_clamp_keeps_value_within_range():
    assert clamp(5, 0, 10) == 5


def test_clamp_bounds_and_overflow():
    assert clamp(-3, 0, 10) == 0, "低于下界夹到下界"
    assert clamp(13, 0, 10) == 10, "高于上界夹到上界"
    assert clamp(0, 0, 10) == 0
    assert clamp(10, 0, 10) == 10


# ----------------------------------------------------------- normalize_rect

def test_normalize_rect_forward_drag():
    # 正向拖动：左上 → 右下
    assert normalize_rect(10, 10, 100, 80, 1920, 2280) == (10, 10, 90, 70)


def test_normalize_rect_reverse_drag():
    # 反向拖动：右下 → 左上，归一化结果与正向一致
    assert normalize_rect(100, 80, 10, 10, 1920, 2280) == (10, 10, 90, 70)


def test_normalize_rect_zero_area_returns_none():
    # 零面积（单击未拖动）视为无选区
    assert normalize_rect(5, 5, 5, 5, 1920, 2280) is None


def test_normalize_rect_clamps_fully_overflowing_drag():
    # 拖出整个屏幕：四条边全部夹紧到虚拟桌面边界
    assert normalize_rect(-50, -50, 5000, 5000, 1920, 2280) == (0, 0, 1920, 2280)


def test_normalize_rect_clamps_partial_overflow():
    # 左边界越界：左边夹到 0、宽相应收缩，其余边不动
    assert normalize_rect(-20, 10, 100, 50, 1920, 2280) == (0, 10, 100, 40)


# ------------------------------------------------------ SnipOverlayNative

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
# ------------------------------------------------------ 画笔标注（无头）

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
    # _update_pen_ui 会 _load() .NET 程序集；无头环境桩掉，按钮均为 None/空不触碰
    monkeypatch.setattr(screen_snip_native, "_load", lambda: (None, None, None))
    overlay = _make_overlay()
    assert overlay._mode == "select"

    overlay._toggle_pen_mode()
    assert overlay._mode == "pen"

    overlay._toggle_pen_mode()
    assert overlay._mode == "select"


def test_pen_set_color(monkeypatch):
    # _pen_btn=None、_color_buttons 为空时 _update_pen_ui 已有 None 保护，不报错
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

    overlay._clear_strokes()  # _form 为 None：不触发重绘、不报错

    assert overlay._strokes == []
    assert overlay._current_stroke is None
