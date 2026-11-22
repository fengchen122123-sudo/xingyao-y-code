from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

_RECT_COLOR = (255, 0, 0)
_LABEL_BG_COLOR = (0, 0, 0)
_LABEL_FG_COLOR = (255, 255, 255)
_LABEL_PAD = 2

_FONT_CANDIDATES = ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")


def _load_label_font(pixel_size: int):
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, pixel_size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label_metrics(width: int, height: int) -> tuple[int, int]:
    base = min(width, height)
    font_size = max(14, min(28, base // 45))
    rect_width = max(2, min(5, base // 360))
    return font_size, rect_width


def annotate_screenshot(
    png_bytes: bytes,
    elements: tuple,
    scale_x: float,
    scale_y: float,
    *,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> bytes:
    if scale_x <= 0 or scale_y <= 0:
        raise ValueError("scale_x/scale_y 必须为正（original/model 比例）")
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    width, height = image.size
    font_size, rect_width = _label_metrics(width, height)
    font = _load_label_font(font_size)
    draw = ImageDraw.Draw(image)
    for element in elements:
        if not isinstance(element, dict):
            continue
        ref = str(element.get("ref", "") or "")
        bounds = element.get("bounds") or ()
        if not ref or len(bounds) != 4:
            continue
        left, top, right, bottom = (float(v) for v in bounds)
        x0 = (left - origin_x) / scale_x
        y0 = (top - origin_y) / scale_y
        x1 = (right - origin_x) / scale_x
        y1 = (bottom - origin_y) / scale_y
        x0 = min(max(x0, 0.0), float(width))
        y0 = min(max(y0, 0.0), float(height))
        x1 = min(max(x1, 0.0), float(width))
        y1 = min(max(y1, 0.0), float(height))
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        draw.rectangle(
            [round(x0), round(y0), round(x1), round(y1)],
            outline=_RECT_COLOR,
            width=rect_width,
        )
        _draw_ref_label(draw, font, ref, round(x0), round(y0), width, height)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _draw_ref_label(draw, font, ref: str, x: int, y: int, width: int, height: int) -> None:
    text_w = draw.textlength(ref, font=font)
    ascent, descent = font.getmetrics()
    text_h = ascent + descent
    box_w = int(text_w) + _LABEL_PAD * 2
    box_h = text_h
    box_x = min(max(x, 0), max(0, width - box_w))
    box_y = min(max(y, 0), max(0, height - box_h))
    draw.rectangle(
        [box_x, box_y, box_x + box_w, box_y + box_h],
        fill=_LABEL_BG_COLOR,
    )
    draw.text(
        (box_x + _LABEL_PAD, box_y),
        ref,
        fill=_LABEL_FG_COLOR,
        font=font,
    )
