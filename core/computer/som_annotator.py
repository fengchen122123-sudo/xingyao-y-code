"""SoM 编号标注截图（通用化重构 Task B；对标 microsoft/UFO、Agent-S、SoM）。

纯函数：把 UIA 元素表（ref/kind/name/bounds/enabled）按编号画到观察 PNG 上
——红色 2px 矩形框 + 左上角黑底白字 ref 标签，让模型「点编号」而不是估像素。

确定性铁律：无时间戳、无随机数、PIL PNG 编码参数固定（optimize=False），
同一画面 + 同一元素集产生同一字节。

坐标语义：bounds 是 UIA CurrentBoundingRectangle 的物理屏幕坐标
[left, top, right, bottom]；scale_x/scale_y 是 ComputerObservation 的
original/model 比例（模型坐标乘 scale 得物理坐标，见
screenshot_to_physical），故图内坐标 = (物理坐标 - 虚拟桌面原点) / scale。
origin_x/origin_y 是虚拟桌面原点（多显示器可为负；单显示器为 0）。
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

_RECT_COLOR = (255, 0, 0)
_RECT_WIDTH = 2
_LABEL_BG_COLOR = (0, 0, 0)
_LABEL_FG_COLOR = (255, 255, 255)
_LABEL_PAD = 2


def annotate_screenshot(
    png_bytes: bytes,
    elements: tuple,
    scale_x: float,
    scale_y: float,
    *,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> bytes:
    """把元素 ref 编号画到观察 PNG 上，返回标注后的 PNG 字节。

    elements 为 dict 序列（ref/kind/name/bounds/enabled）；bounds 为物理
    屏幕坐标 [left, top, right, bottom]，乘 1/scale 并减原点得图内坐标。
    单元素坐标非法/退化/完全出图时跳过该元素；矩形越界部分钳制到图内。
    scale<=0、PNG 无法打开等异常向上传播——调用方（backend）捕获后退回
    普通截图，绝不阻断观察。
    """
    if scale_x <= 0 or scale_y <= 0:
        raise ValueError("scale_x/scale_y 必须为正（original/model 比例）")
    image = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    width, height = image.size
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
        # 越界钳制到图内；钳制后退化为零面积（完全出图/零尺寸）则跳过。
        x0 = min(max(x0, 0.0), float(width))
        y0 = min(max(y0, 0.0), float(height))
        x1 = min(max(x1, 0.0), float(width))
        y1 = min(max(y1, 0.0), float(height))
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        draw.rectangle(
            [round(x0), round(y0), round(x1), round(y1)],
            outline=_RECT_COLOR,
            width=_RECT_WIDTH,
        )
        _draw_ref_label(draw, font, ref, round(x0), round(y0), width, height)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _draw_ref_label(draw, font, ref: str, x: int, y: int, width: int, height: int) -> None:
    """在 (x, y) 附近画黑底白字 ref 标签；越出图边界时向图内收缩。"""
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
