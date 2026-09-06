"""SoM 编号标注截图纯函数测试（通用化重构 Task B）。

全部使用真实 PIL 在内存中生成/解码 PNG（不截屏、不动键鼠）；
验证确定性字节、矩形落点、越界钳制与标签内缩。
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from core.computer.som_annotator import annotate_screenshot


def _white_png(width=1920, height=1080) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _element(ref, bounds, enabled=True):
    return {"ref": ref, "kind": "ButtonControl", "name": "控件", "bounds": bounds, "enabled": enabled}


def test_annotation_is_deterministic():
    """同输入两次标注字节完全一致（无时间戳/随机数，编码参数固定）。"""
    png = _white_png()
    elements = (_element("el-1", [200, 100, 400, 300]), _element("el-2", [1000, 500, 1400, 700]))
    first = annotate_screenshot(png, elements, 2.0, 2.0)
    second = annotate_screenshot(png, elements, 2.0, 2.0)
    assert first == second


def test_rect_lands_on_expected_image_coordinates():
    """物理 bounds [200,100,400,300] 在 scale=2 下落到图内 (100,50,200,150)。"""
    png = _white_png()
    annotated = annotate_screenshot(png, (_element("el-3", [200, 100, 400, 300]),), 2.0, 2.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    # 左边缘中点为红框，框内与背景为白
    assert image.getpixel((100, 100)) == (255, 0, 0)
    assert image.getpixel((150, 100)) == (255, 255, 255)
    # 左上角标签黑底白字
    assert image.getpixel((102, 52)) == (0, 0, 0)


def test_out_of_bounds_rect_is_clamped_and_outside_rect_skipped():
    png = _white_png()
    clamped = _element("el-1", [3800, 2100, 4000, 2300])  # 右下角部分出图 → 钳制到图内
    outside = _element("el-2", [-1000, -1000, -500, -500])  # 完全出图 → 跳过
    annotated = annotate_screenshot(png, (clamped, outside), 2.0, 2.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    # 钳制后的框仍可见（物理 (3800,2100) → 图内 (1900,1050)，右/下边钳到 1920/1080）；
    # 取右边缘中部像素，避开左上角标签覆盖区
    assert image.getpixel((1919, 1070)) == (255, 0, 0)
    # 完全出图元素不产生任何绘制：与只含钳制元素的标注像素一致
    only_clamped = Image.open(
        io.BytesIO(annotate_screenshot(png, (clamped,), 2.0, 2.0))
    ).convert("RGB")
    assert image.tobytes() == only_clamped.tobytes()


def test_negative_virtual_desktop_origin_is_applied():
    """多显示器负原点：物理坐标先减原点再除 scale。"""
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
    # 物理 (0,0)-(1920,1080) → 图内 (960,0)-(1920,540)
    assert image.getpixel((1919, 270)) == (255, 0, 0)
    assert image.getpixel((900, 270)) == (255, 255, 255)


def test_invalid_scale_and_malformed_elements_are_rejected_or_skipped():
    png = _white_png()
    with pytest.raises(ValueError):
        annotate_screenshot(png, (_element("el-1", [0, 0, 10, 10]),), 0.0, 2.0)
    # 非法元素（缺 ref/bounds 形状错误）跳过不抛
    broken = (
        {"kind": "ButtonControl", "name": "无ref", "bounds": [0, 0, 10, 10], "enabled": True},
        _element("el-2", [0, 0, 10]),
        "not-a-dict",
    )
    annotated = annotate_screenshot(png, broken, 2.0, 2.0)
    plain = annotate_screenshot(png, (), 2.0, 2.0)
    assert annotated == plain


def test_label_clamps_inside_image_border():
    """标签贴近图右下边界时向图内收缩，绝不越界。"""
    png = _white_png(40, 24)
    # 极小图 + 右下角元素：标签只能内缩到图内
    annotated = annotate_screenshot(png, (_element("el-1", [0, 0, 40, 24]),), 1.0, 1.0)
    image = Image.open(io.BytesIO(annotated)).convert("RGB")
    width, height = image.size
    assert (width, height) == (40, 24)
    # 图内应存在黑底像素（标签被钳制在图内绘制）
    blacks = [
        (x, y)
        for x in range(width)
        for y in range(height)
        if image.getpixel((x, y)) == (0, 0, 0)
    ]
    assert blacks
