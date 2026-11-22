from __future__ import annotations

import io
import os

import pytest
from PIL import Image, ImageFont

from core.computer.som_annotator import (
    _label_metrics,
    _load_label_font,
    annotate_screenshot,
)


def _white_png(width=1920, height=1080) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _element(ref, bounds, enabled=True):
    return {"ref": ref, "kind": "ButtonControl", "name": "控件", "bounds": bounds, "enabled": enabled}


def test_annotation_is_deterministic():
    png = _white_png()
    elements = (_element("el-1", [200, 100, 400, 300]), _element("el-2", [1000, 500, 1400, 700]))
    first = annotate_screenshot(png, elements, 2.0, 2.0)
    second = annotate_screenshot(png, elements, 2.0, 2.0)
    assert first == second


def test_rect_lands_on_expected_image_coordinates():
    png = _white_png()
    annotated = annotate_screenshot(png, (_element("el-3", [200, 100, 400, 300]),), 2.0, 2.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    assert image.getpixel((100, 100)) == (255, 0, 0)
    assert image.getpixel((150, 100)) == (255, 255, 255)
    assert image.getpixel((102, 52)) == (0, 0, 0)


def test_out_of_bounds_rect_is_clamped_and_outside_rect_skipped():
    png = _white_png()
    clamped = _element("el-1", [3000, 1500, 4000, 2300])
    outside = _element("el-2", [-1000, -1000, -500, -500])
    annotated = annotate_screenshot(png, (clamped, outside), 2.0, 2.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    assert image.getpixel((1919, 900)) == (255, 0, 0)
    assert image.getpixel((1700, 900)) == (255, 255, 255)
    only_clamped = Image.open(
        io.BytesIO(annotate_screenshot(png, (clamped,), 2.0, 2.0))
    ).convert("RGB")
    assert image.tobytes() == only_clamped.tobytes()


def test_negative_virtual_desktop_origin_is_applied():
    png = _white_png()
    annotated = annotate_screenshot(
        png,
        (_element("el-1", [0, 0, 1920, 1080]),),
        2.0,
        2.0,
        origin_x=-1920.0,
        origin_y=0.0,
    )
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    assert image.getpixel((1919, 270)) == (255, 0, 0)
    assert image.getpixel((900, 270)) == (255, 255, 255)


def test_invalid_scale_and_malformed_elements_are_rejected_or_skipped():
    png = _white_png()
    with pytest.raises(ValueError):
        annotate_screenshot(png, (_element("el-1", [0, 0, 10, 10]),), 0.0, 2.0)
    broken = (
        {"kind": "ButtonControl", "name": "无ref", "bounds": [0, 0, 10, 10], "enabled": True},
        _element("el-2", [0, 0, 10]),
        "not-a-dict",
    )
    annotated = annotate_screenshot(png, broken, 2.0, 2.0)
    plain = annotate_screenshot(png, (), 2.0, 2.0)
    assert annotated == plain


def test_label_clamps_inside_image_border():
    png = _white_png(40, 24)
    annotated = annotate_screenshot(png, (_element("el-1", [0, 0, 40, 24]),), 1.0, 1.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    width, height = image.size
    assert (width, height) == (40, 24)
    blacks = [
        (x, y)
        for x in range(width)
        for y in range(height)
        if image.getpixel((x, y)) == (0, 0, 0)
    ]
    assert blacks


@pytest.mark.parametrize(
    ("width", "height", "expected"),
    [
        (1920, 1080, (24, 3)),
        (3840, 2160, (28, 5)),
        (512, 512, (14, 2)),
    ],
)
def test_label_metrics_derives_font_size_and_rect_width(width, height, expected):
    assert _label_metrics(width, height) == expected


def test_load_label_font_prefers_truetype():
    font = _load_label_font(24)
    assert isinstance(font, ImageFont.FreeTypeFont)


def test_load_label_font_falls_back_when_all_font_files_missing(monkeypatch):
    original_truetype = ImageFont.truetype

    def _missing_file_truetype(*args, **kwargs):
        first = args[0] if args else kwargs.get("font")
        if isinstance(first, (str, os.PathLike)):
            raise OSError("模拟字体文件缺失")
        return original_truetype(*args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", _missing_file_truetype)
    font = _load_label_font(24)
    assert font.getmetrics()


def test_annotation_deterministic_on_large_image():
    png = _white_png(3840, 2160)
    elements = (_element("el-7", [400, 200, 800, 600]),)
    first = annotate_screenshot(png, elements, 2.0, 2.0)
    second = annotate_screenshot(png, elements, 2.0, 2.0)
    assert first == second
