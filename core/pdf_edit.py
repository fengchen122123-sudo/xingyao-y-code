# -*- coding: utf-8 -*-
"""PDF 编辑后端：页面级增删/旋转/移动/插空白页 + 批注/覆盖层。

对外只暴露 ``apply_edits(src_path, ops, out_path) -> dict``。前端（右栏「文档」
页签里的 PDF 编辑器）把用户操作攒成 ops 数组交给 ``gui_bridge.save_pdf_edits``，
桥层负责路径鉴权、备份与返回封装，本模块负责真正的 PDF 加工：

1. 页面级操作（delete / rotate / move / insertBlank）用 pypdf 直接改页树——
   空白页用 ``PageObject.create_blank_page`` 自造，不依赖 reportlab 生成再读
   回来（少一次解析、也不受 reportlab 纸张尺寸表限制）。
2. 批注/覆盖（highlight / underline / strike / rect / arrow / ink / note /
   stamp / redact / text）用 reportlab 生成一张与目标页同尺寸的 overlay PDF，
   再 ``page.merge_page(overlay)`` 叠到页面内容之上——原页面内容流一个字节都
   不动，因此能保留原有文字、图片与可复制性。

坐标口径（与前端 pdf.js ``getViewport({scale:1})`` 完全一致，前端用同一份
schema）：单位是 PDF point，原点在**页面左上角**，y 轴向下，page 为 1-based。
reportlab 的原点在左下角，所以绘制时统一翻转 y：``rect_y = page_h - (y + h)``，
文字基线取 ``page_h - y - fontSize``。目标页带 /Rotate（非 0）时，用户看到的
是旋转后的坐标系，显示宽高与页面宽高互换，本模块用一次 ``canvas.transform``
完成「显示坐标 → 页面坐标」映射，让旋转页上的批注仍落在用户看到的位置。

校验全部写在本模块（而不是桥层），任何调用方都过同一道关；非法输入一律
``raise ValueError``，由桥层收敛成 ``{"ok": False, "msg": ...}``。
"""

import io
import logging
import math
import os
import re

from pypdf import PdfReader, PageObject, PdfWriter
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_LOGGER = logging.getLogger(__name__)

# 单次编辑的操作条数上限：正常 UI 交互远达不到，纯防御（避免一次请求吃满内存）
MAX_OPS = 2000
# 单个文本字段上限（批注正文 / 文本框内容）
MAX_TEXT_LEN = 4000
# 单条高亮类批注的矩形数上限（跨页框选时前端会按行拆成很多矩形）
MAX_RECTS_PER_ANNOT = 500
# 单条手写笔迹的点数上限
MAX_INK_POINTS = 5000
# 单页边长上限（point）：20000pt ≈ 7 米，超出必然是脏数据
MAX_PAGE_DIM = 20000.0
# 颜色必须是 #rrggbb（前端取色器输出的格式）
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 中文字体注册名（overlay 里的 setFont 用这个名字）
_CJK_FONT_NAME = "PDFEditCJK"
# Windows 自带中文字体候选：(路径, TTC 子字体索引)。全部缺失时退 Helvetica
# 并在返回结果里带 warning，绝不让「没字体」把整次保存变成失败。
_FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", None),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
)
_FALLBACK_FONT = "Helvetica"

# kind 取值（页面操作 + 批注/覆盖）
_PAGE_KINDS = ("page",)
_MARK_KINDS = ("annot", "redact", "text")
_ALL_KINDS = _PAGE_KINDS + _MARK_KINDS
_ANNOT_TYPES = ("highlight", "underline", "strike", "rect", "arrow", "ink", "note", "stamp")

# 各批注类型的默认颜色 / 默认不透明度
_DEFAULT_HIGHLIGHT_COLOR = "#ffd400"
_DEFAULT_MARK_COLOR = "#e5484d"
_DEFAULT_TEXT_COLOR = "#111111"
_DEFAULT_REDACT_FILL = "#ffffff"

# 字体注册结果缓存：(字体名, warning 或 None)。中文字体只需注册一次。
_font_state = None


# ---------------------------------------------------------------------------
# 通用校验小工具：全部 raise ValueError（桥层负责转成 ok=False）
# ---------------------------------------------------------------------------


def _require_number(value, field: str) -> float:
    """把字段收敛成有限浮点数；bool 不算数字（JSON 里 true/false 常被误传）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} 必须是有限数字")
    return number


def _require_int(value, field: str) -> int:
    """整数校验；允许 3.0 这类整值浮点（前端 JSON 序列化不区分）。"""
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ValueError(f"{field} 必须是整数")


def _require_color(value, field: str, default: str) -> str:
    """颜色校验：必须是 #rrggbb；缺省时用 default。"""
    if value is None:
        return default
    if not isinstance(value, str) or not _COLOR_RE.match(value):
        raise ValueError(f"{field} 颜色格式不合法（需形如 #rrggbb）：{value!r}")
    return value


def _require_opacity(value, default: float) -> float:
    """不透明度校验：0..1；缺省时用 default。"""
    if value is None:
        return default
    opacity = _require_number(value, "opacity")
    if opacity < 0 or opacity > 1:
        raise ValueError(f"opacity 必须在 0..1 之间：{opacity}")
    return opacity


def _stroke_width(op, default: float) -> float:
    """批注描边宽度：op["width"] 优先（0.1..50），缺省用 default。

    前端所有绘图类批注都会带 width（默认 1.5）；早先前端下发了而后端忽略，
    导致「粗细」写了不生效。这里统一读取，并保留按矩形高度自适应出来的默认值。
    """
    raw = op.get("width")
    if raw is None:
        return default
    width = _require_number(raw, "width")
    if width < 0.1 or width > 50:
        raise ValueError(f"width 必须在 0.1..50 之间：{width}")
    return width

def _require_rect(value, field: str):
    """矩形校验，返回 (x, y, w, h)；宽高必须 > 0（0 面积批注无意义，多半是前端 bug）。"""
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    x = _require_number(value.get("x"), f"{field}.x")
    y = _require_number(value.get("y"), f"{field}.y")
    w = _require_number(value.get("w"), f"{field}.w")
    h = _require_number(value.get("h"), f"{field}.h")
    if w <= 0 or h <= 0:
        raise ValueError(f"{field} 的宽高必须大于 0：w={w} h={h}")
    if w > MAX_PAGE_DIM or h > MAX_PAGE_DIM:
        raise ValueError(f"{field} 尺寸超出上限（{MAX_PAGE_DIM}pt）：w={w} h={h}")
    return x, y, w, h


def _require_point(value, field: str):
    """点校验，返回 (x, y)。"""
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return _require_number(value.get("x"), f"{field}.x"), _require_number(value.get("y"), f"{field}.y")


def _require_text(op, field: str = "text") -> str:
    """文本内容校验：非空且不超长。"""
    value = op.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空字符串")
    if len(value) > MAX_TEXT_LEN:
        raise ValueError(f"{field} 超长（{len(value)} 字，上限 {MAX_TEXT_LEN} 字）")
    return value


def _require_font_size(op, default: float) -> float:
    """字号校验：1..300；缺省时用 default。"""
    raw = op.get("fontSize")
    if raw is None:
        return default
    size = _require_number(raw, "fontSize")
    if size < 1 or size > 300:
        raise ValueError(f"fontSize 必须在 1..300 之间：{size}")
    return size


def _require_page_no(value, page_count: int, label: str) -> int:
    """页码校验（1-based），返回 1-based 页码。"""
    page_no = _require_int(value, f"{label}的 page")
    if page_no < 1 or page_no > page_count:
        raise ValueError(f"{label}的页码超出范围：{page_no}（共 {page_count} 页）")
    return page_no


# ---------------------------------------------------------------------------
# 中文字体
# ---------------------------------------------------------------------------


def _ensure_cjk_font():
    """注册中文字体，返回 (字体名, warning 或 None)。

    缺字体的降级必须是「能保存但中文变方块」，不能抛异常——用户改的页面结构、
    白块遮盖等操作不该因为一台机器没装微软雅黑就全部失败。
    """
    global _font_state
    if _font_state is not None:
        return _font_state
    for path, subfont_index in _FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            if subfont_index is None:
                pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, path))
            else:
                pdfmetrics.registerFont(TTFont(_CJK_FONT_NAME, path, subfontIndex=subfont_index))
        except Exception:
            _LOGGER.warning("注册中文字体失败，尝试下一个候选：%s", path, exc_info=True)
            continue
        _font_state = (_CJK_FONT_NAME, None)
        return _font_state
    warning = "未找到系统中文字体（msyh/msyhbd/simhei/simsun），中文批注可能显示异常"
    _LOGGER.warning(warning)
    _font_state = (_FALLBACK_FONT, warning)
    return _font_state


def _reset_font_cache_for_tests():
    """清空字体注册缓存（测试用：模拟「系统没有中文字体」的降级路径）。"""
    global _font_state
    _font_state = None


# ---------------------------------------------------------------------------
# 页面操作（pypdf）
# ---------------------------------------------------------------------------


def _page_mediabox_size(page):
    """取页面 MediaBox 的 (width, height, left, bottom)。"""
    media = page.mediabox
    return float(media.width), float(media.height), float(media.left), float(media.bottom)


def _page_rotation(page) -> int:
    """/Rotate 归一化到 0/90/180/270（异常值按 0 处理，避免坐标映射乱套）。"""
    try:
        rotation = int(page.rotation or 0) % 360
    except Exception:
        return 0
    return rotation if rotation in (0, 90, 180, 270) else 0


def _apply_page_ops(pages, page_ops):
    """按列表顺序应用页面操作，返回最终页序；标题提到的 page 都是「当前页序」的 1-based。"""
    pages = list(pages)
    for index, op in enumerate(page_ops):
        action = op.get("op")
        label = f"第 {index + 1} 个页面操作（{action}）"
        page_count = len(pages)
        if action == "delete":
            page_no = _require_page_no(op.get("page"), page_count, label)
            del pages[page_no - 1]
            if not pages:
                raise ValueError("不能删除全部页面（至少要保留 1 页）")
        elif action == "rotate":
            page_no = _require_page_no(op.get("page"), page_count, label)
            degrees = _require_int(op.get("degrees"), f"{label}的 degrees")
            # 契约只接受 90/180/270：0 是「不旋转」（前端不该下发），其它角度
            # 会让坐标映射与显示对不上，一律拒绝而不是静默取整。
            if degrees not in (90, 180, 270):
                raise ValueError(f"旋转角度只支持 90/180/270：{degrees}")
            pages[page_no - 1].rotate(degrees)
        elif action == "move":
            page_no = _require_page_no(op.get("page"), page_count, label)
            to_no = _require_page_no(op.get("to"), page_count, f"{label}的 to")
            page = pages.pop(page_no - 1)
            pages.insert(to_no - 1, page)
        elif action == "insertBlank":
            after = _require_int(op.get("after"), f"{label}的 after")
            if after < 0 or after > page_count:
                raise ValueError(f"{label}的 after 超出范围：{after}（0..{page_count}）")
            width, height = _blank_page_size(op, pages, after, label)
            pages.insert(after, PageObject.create_blank_page(width=width, height=height))
        else:
            raise ValueError(f"{label} 的 op 不支持（delete/rotate/move/insertBlank）")
    return pages


def _blank_page_size(op, pages, after: int, label: str):
    """新空白页尺寸：优先用 ops 给的 width/height，缺失时参照相邻页尺寸。"""
    width = op.get("width")
    height = op.get("height")
    if width is None or height is None:
        reference = pages[after - 1] if after > 0 and pages else (pages[0] if pages else None)
        if reference is None:
            raise ValueError(f"{label} 需要 width/height（文档里没有可参照的页面尺寸）")
        ref_w, ref_h, _ref_l, _ref_b = _page_mediabox_size(reference)
        width = ref_w if width is None else width
        height = ref_h if height is None else height
    width = _require_number(width, f"{label}的 width")
    height = _require_number(height, f"{label}的 height")
    if width <= 0 or height <= 0:
        raise ValueError(f"{label} 的 width/height 必须大于 0：{width}x{height}")
    if width > MAX_PAGE_DIM or height > MAX_PAGE_DIM:
        raise ValueError(f"{label} 的 width/height 超出上限（{MAX_PAGE_DIM}pt）：{width}x{height}")
    return width, height


# ---------------------------------------------------------------------------
# overlay 绘制（reportlab）
# ---------------------------------------------------------------------------


def _apply_display_transform(c, rotation: int, page_w: float, page_h: float, left: float, bottom: float) -> None:
    """把 canvas 坐标系换成「显示坐标」：x 向右、y 向上、原点在显示区左下角。

    推导（页面宽高 W×H，显示宽高对 90/270 互换）：
        0   : px = x,             py = y
        90  : px = W - y,         py = x
        180 : px = W - x,         py = y
        270 : px = W - y,         py = H - x
    reportlab 的 transform(a, b, c, d, e, f) 语义是 px = a·x + c·y + e，
    py = b·x + d·y + f（与 PDF 的 cm 一致）。
    """
    if rotation == 90:
        c.transform(0, 1, -1, 0, page_w + left, bottom)
    elif rotation == 180:
        c.transform(-1, 0, 0, 1, page_w + left, page_h + bottom)
    elif rotation == 270:
        c.transform(0, -1, -1, 0, page_w + left, page_h + bottom)
    else:
        # 无旋转时只需补上 MediaBox 原点偏移（少数 PDF 的 MediaBox 不从 0,0 开始）
        c.translate(left, bottom)


def _flip_rect(rect, display_h: float):
    """把「左上角原点 y 向下」的矩形翻成 reportlab 的左下角原点坐标。"""
    x, y, w, h = rect
    return x, display_h - (y + h), w, h


def _flip_point(point, display_h: float):
    """把「左上角原点 y 向下」的点翻成 reportlab 坐标。"""
    x, y = point
    return x, display_h - y


def _wrap_paragraph(paragraph: str, font_name: str, font_size: float, max_width: float):
    """按可用宽度做逐字贪心换行；拉丁文优先在最后一个空格处断开，避免单词被切碎。"""
    if not paragraph:
        return [""]
    lines = []
    current = ""
    for ch in paragraph:
        candidate = current + ch
        if not current or pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
            current = candidate
            continue
        cut = current.rfind(" ")
        if cut > 0 and len(current) - cut <= 24:
            lines.append(current[:cut])
            current = current[cut + 1:] + ch
        else:
            lines.append(current)
            current = ch
    lines.append(current)
    return lines


def _layout_lines(text: str, font_name: str, font_size: float, max_width: float):
    """整段文本换行：保留显式换行，逐段按宽度折行。"""
    lines = []
    for paragraph in str(text).split("\n"):
        lines.extend(_wrap_paragraph(paragraph, font_name, font_size, max_width))
    return lines


def _draw_lines(c, lines, font_name, font_size, color, x, top_y, max_lines, centered: bool = False, box_width: float = 0.0):
    """自上而下绘制已折好的文本行，返回实际绘制的行数。

    top_y 是首行框顶（reportlab 坐标，y 向上）；基线按契约取 top_y - fontSize。
    """
    line_height = font_size * 1.3
    c.saveState()
    c.setFillColor(HexColor(color))
    c.setFont(font_name, font_size)
    drawn = 0
    for line in lines:
        if drawn >= max_lines:
            break
        baseline = top_y - font_size - drawn * line_height
        if centered:
            width = pdfmetrics.stringWidth(line, font_name, font_size)
            c.drawString(x + max(0.0, (box_width - width) / 2.0), baseline, line)
        else:
            c.drawString(x, baseline, line)
        drawn += 1
    c.restoreState()
    return drawn


def _draw_text_block(c, op, rect, font_name, font_size, color, display_h, warnings) -> bool:
    """在矩形内绘制自动换行文本；超出高度的行丢弃并计入 warning。"""
    x, y, w, h = _flip_rect(rect, display_h)
    padding = 2.0
    max_width = max(w - 2 * padding, 1.0)
    lines = _layout_lines(_require_text(op), font_name, font_size, max_width)
    line_height = font_size * 1.3
    max_lines = int((h - 2 * padding) // line_height)
    if max_lines <= 0:
        warnings.append(f"文本框高度 {h:.1f}pt 放不下一行 {font_size:.0f}pt 文字，已跳过绘制")
        return False
    dropped = max(0, len(lines) - max_lines)
    if dropped:
        warnings.append(f"文本框内容超出高度，已丢弃 {dropped} 行")
    _draw_lines(c, lines, font_name, font_size, color, x + padding, y + h - padding, max_lines)
    return True


def _draw_op(c, op, display_w: float, display_h: float, font_name: str, warnings) -> bool:
    """绘制单条批注/覆盖操作，返回是否真的画了东西（没画就不用叠加 overlay）。"""
    kind = op.get("kind")
    if kind == "redact":
        fill = _require_color(op.get("fill"), "fill", _DEFAULT_REDACT_FILL)
        x, y, w, h = _flip_rect(_require_rect(op.get("rect"), "rect"), display_h)
        c.saveState()
        c.setFillColor(HexColor(fill))
        c.rect(x, y, w, h, stroke=0, fill=1)
        c.restoreState()
        return True

    if kind == "text":
        rect = _require_rect(op.get("rect"), "rect")
        font_size = _require_font_size(op, 12)
        color = _require_color(op.get("color"), "color", _DEFAULT_TEXT_COLOR)
        bg = op.get("bg")
        if bg is not None:
            bg_color = _require_color(bg, "bg", _DEFAULT_TEXT_COLOR)
            x, y, w, h = _flip_rect(rect, display_h)
            c.saveState()
            c.setFillColor(HexColor(bg_color))
            c.rect(x, y, w, h, stroke=0, fill=1)
            c.restoreState()
        return _draw_text_block(c, op, rect, font_name, font_size, color, display_h, warnings)

    if kind != "annot":
        raise ValueError(f"不支持的 kind：{kind!r}")

    annot_type = op.get("type")
    if annot_type not in _ANNOT_TYPES:
        raise ValueError(f"不支持的批注类型：{annot_type!r}")

    if annot_type in ("highlight", "underline", "strike"):
        color = _require_color(op.get("color"), "color", _DEFAULT_HIGHLIGHT_COLOR)
        opacity = _require_opacity(op.get("opacity"), 0.35)
        rects = op.get("rects")
        if not isinstance(rects, (list, tuple)) or not rects:
            raise ValueError(f"{annot_type} 需要非空的 rects 数组")
        if len(rects) > MAX_RECTS_PER_ANNOT:
            raise ValueError(f"{annot_type} 的 rects 过多（{len(rects)}，上限 {MAX_RECTS_PER_ANNOT}）")
        bounds = [_flip_rect(_require_rect(item, "rects[]"), display_h) for item in rects]
        red, green, blue = _hex_rgb(color)
        c.saveState()
        if annot_type == "highlight":
            c.setFillColorRGB(red, green, blue)
            c.setFillAlpha(opacity)
            for x, y, w, h in bounds:
                c.rect(x, y, w, h, stroke=0, fill=1)
        else:
            c.setStrokeColorRGB(red, green, blue)
            c.setStrokeAlpha(opacity)
            c.setLineCap(1)
            for x, y, w, h in bounds:
                c.setLineWidth(_stroke_width(op, max(0.5, min(1.5, h * 0.08))))
                line_y = y if annot_type == "underline" else y + h / 2.0
                c.line(x, line_y, x + w, line_y)
        c.restoreState()
        return True

    if annot_type == "rect":
        color = _require_color(op.get("color"), "color", _DEFAULT_MARK_COLOR)
        opacity = _require_opacity(op.get("opacity"), 1.0)
        x, y, w, h = _flip_rect(_require_rect(op.get("rect"), "rect"), display_h)
        red, green, blue = _hex_rgb(color)
        c.saveState()
        c.setStrokeColorRGB(red, green, blue)
        c.setStrokeAlpha(opacity)
        c.setLineWidth(_stroke_width(op, 1.5))
        c.rect(x, y, w, h, stroke=1, fill=0)
        c.restoreState()
        return True

    if annot_type == "arrow":
        color = _require_color(op.get("color"), "color", _DEFAULT_MARK_COLOR)
        opacity = _require_opacity(op.get("opacity"), 1.0)
        start = _flip_point(_require_point(op.get("from"), "from"), display_h)
        end = _flip_point(_require_point(op.get("to"), "to"), display_h)
        red, green, blue = _hex_rgb(color)
        c.saveState()
        c.setStrokeColorRGB(red, green, blue)
        c.setStrokeAlpha(opacity)
        c.setLineWidth(_stroke_width(op, 1.5))
        c.setLineCap(1)
        c.line(start[0], start[1], end[0], end[1])
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        head = 9.0
        for offset in (math.radians(155), math.radians(-155)):
            c.line(end[0], end[1], end[0] + head * math.cos(angle + offset), end[1] + head * math.sin(angle + offset))
        c.restoreState()
        return True

    if annot_type == "ink":
        color = _require_color(op.get("color"), "color", _DEFAULT_MARK_COLOR)
        opacity = _require_opacity(op.get("opacity"), 1.0)
        points = op.get("points")
        if not isinstance(points, (list, tuple)) or len(points) < 2:
            raise ValueError("ink 需要至少 2 个 points")
        if len(points) > MAX_INK_POINTS:
            raise ValueError(f"ink 的 points 过多（{len(points)}，上限 {MAX_INK_POINTS}）")
        width = _require_number(op.get("width", 1.5), "width")
        if width <= 0 or width > 50:
            raise ValueError(f"ink 的 width 必须在 0..50 之间：{width}")
        path = c.beginPath()
        first = _flip_point(_require_point(points[0], "points[]"), display_h)
        path.moveTo(first[0], first[1])
        for raw in points[1:]:
            x, y = _flip_point(_require_point(raw, "points[]"), display_h)
            path.lineTo(x, y)
        red, green, blue = _hex_rgb(color)
        c.saveState()
        c.setStrokeColorRGB(red, green, blue)
        c.setStrokeAlpha(opacity)
        c.setLineWidth(width)
        c.setLineCap(1)
        c.setLineJoin(1)
        c.drawPath(path, stroke=1, fill=0)
        c.restoreState()
        return True

    if annot_type in ("note", "stamp"):
        rect = _require_rect(op.get("rect"), "rect")
        text = _require_text(op)
        color = _require_color(op.get("color"), "color", _DEFAULT_HIGHLIGHT_COLOR if annot_type == "note" else _DEFAULT_MARK_COLOR)
        opacity = _require_opacity(op.get("opacity"), 1.0)
        font_size = _require_font_size(op, 10 if annot_type == "note" else 16)
        x, y, w, h = _flip_rect(rect, display_h)
        red, green, blue = _hex_rgb(color)
        c.saveState()
        c.setStrokeColorRGB(red, green, blue)
        c.setStrokeAlpha(opacity)
        c.setFillColorRGB(red, green, blue)
        c.setFillAlpha(min(opacity, 0.15) if annot_type == "note" else 0.0)
        c.setLineWidth(1.0)
        c.rect(x, y, w, h, stroke=1, fill=1 if annot_type == "note" else 0)
        c.restoreState()
        padding = 2.0 if annot_type == "note" else 3.0
        max_width = max(w - 2 * padding, 1.0)
        lines = _layout_lines(text, font_name, font_size, max_width)
        line_height = font_size * 1.3
        max_lines = int((h - 2 * padding) // line_height)
        if max_lines <= 0:
            warnings.append(f"{'批注' if annot_type == 'note' else '图章'}框高度不足，文字已跳过绘制")
            return True
        dropped = max(0, len(lines) - max_lines)
        if dropped:
            warnings.append(f"{'批注' if annot_type == 'note' else '图章'}文字超出框体，已丢弃 {dropped} 行")
        visible = lines[:max_lines]
        # 浅色批注色（如便签默认的黄色）直接当文字色会看不清，改用深灰
        text_color = color if _is_dark(color) else _DEFAULT_TEXT_COLOR
        if annot_type == "stamp":
            # 图章文字垂直居中：整块高度对齐框中心
            block_h = len(visible) * line_height
            top_y = y + (h + block_h) / 2.0 - (line_height - font_size)
            _draw_lines(
                c, visible, font_name, font_size, text_color,
                x + padding, top_y, max_lines, centered=True, box_width=max_width,
            )
        else:
            _draw_lines(c, visible, font_name, font_size, text_color, x + padding, y + h - padding, max_lines)
        return True

    raise ValueError(f"不支持的批注类型：{annot_type!r}")


def _hex_rgb(color: str):
    """#rrggbb → (r, g, b) 浮点三元组（reportlab 需要 0..1）。"""
    return tuple(int(color[i:i + 2], 16) / 255.0 for i in (1, 3, 5))


def _is_dark(color: str) -> bool:
    """颜色是否偏深（深色图章文字沿用批注色，浅色改用深灰保证可读）。"""
    red, green, blue = _hex_rgb(color)
    return (0.299 * red + 0.587 * green + 0.114 * blue) < 0.55


def _build_overlay(page, ops, font_name: str, warnings):
    """为单页生成 overlay PDF 字节；这页没有任何可绘制内容时返回 None。"""
    page_w, page_h, left, bottom = _page_mediabox_size(page)
    rotation = _page_rotation(page)
    display_w, display_h = (page_h, page_w) if rotation in (90, 270) else (page_w, page_h)
    buffer = io.BytesIO()
    pdf_canvas = canvas.Canvas(buffer, pagesize=(page_w, page_h))
    pdf_canvas.saveState()
    _apply_display_transform(pdf_canvas, rotation, page_w, page_h, left, bottom)
    drew_anything = False
    for op in ops:
        if _draw_op(pdf_canvas, op, display_w, display_h, font_name, warnings):
            drew_anything = True
    pdf_canvas.restoreState()
    pdf_canvas.showPage()
    pdf_canvas.save()
    if not drew_anything:
        return None
    return buffer.getvalue()


def _apply_mark_ops(writer, mark_ops, font_name: str, warnings) -> None:
    """把批注/覆盖操作按页分组，逐页生成 overlay 并叠加到页面内容之上。"""
    if not mark_ops:
        return
    page_count = len(writer.pages)
    grouped = {}
    for index, op in enumerate(mark_ops):
        label = f"第 {index + 1} 条{_op_label(op)}"
        page_no = _require_page_no(op.get("page"), page_count, label)
        grouped.setdefault(page_no, []).append(op)
    for page_no in sorted(grouped):
        page = writer.pages[page_no - 1]
        overlay_bytes = _build_overlay(page, grouped[page_no], font_name, warnings)
        if not overlay_bytes:
            continue
        overlay_page = PdfReader(io.BytesIO(overlay_bytes)).pages[0]
        page.merge_page(overlay_page)


def _op_label(op) -> str:
    """操作的中文名（只用于错误信息，帮助前端定位是哪一条操作非法）。"""
    kind = op.get("kind")
    if kind == "annot":
        return f"批注（{op.get('type')}）"
    return {"redact": "遮盖", "text": "文本"}.get(kind, "操作")


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def _atomic_write(writer, out_path: str) -> None:
    """先写同目录临时文件，再 os.replace 原子替换；失败清理临时文件。"""
    absolute = os.path.abspath(out_path)
    out_dir = os.path.dirname(absolute)
    tmp_path = f"{absolute}.tmp-{os.getpid()}"
    try:
        with open(tmp_path, "wb") as handle:
            writer.write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, absolute)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            _LOGGER.warning("清理 PDF 编辑临时文件失败：%s", tmp_path, exc_info=True)
        raise


def apply_edits(src_path: str, ops, out_path: str) -> dict:
    """把前端下发的 ops 应用到一个 PDF 上，结果写到 out_path。

    参数:
        src_path: 源 PDF 路径（只读；覆盖保存由桥层先备份再让 out_path == src_path）
        ops: 操作数组，schema 见模块文档（页面操作先按顺序全部应用，批注再作用于
             应用后的最终页序）
        out_path: 输出 PDF 路径（父目录必须已存在）
    返回:
        {"path": 输出绝对路径, "pages": 页数, "warnings": [...], 有 warning 时另带 "warning"}
    异常:
        ValueError: 任何非法输入（ops 非法、页码越界、颜色/不透明度非法、源文件损坏等）
    """
    if not isinstance(src_path, str) or not src_path:
        raise ValueError("源文件路径不能为空")
    if not os.path.isfile(src_path):
        raise ValueError(f"源文件不存在：{src_path}")
    if not isinstance(out_path, str) or not out_path:
        raise ValueError("输出文件路径不能为空")
    out_absolute = os.path.abspath(out_path)
    if not os.path.isdir(os.path.dirname(out_absolute)):
        raise ValueError(f"输出目录不存在：{os.path.dirname(out_absolute)}")
    if isinstance(ops, tuple):
        ops = list(ops)
    if not isinstance(ops, list):
        raise ValueError("ops 必须是数组")
    if len(ops) > MAX_OPS:
        raise ValueError(f"操作数量过多（{len(ops)} 条，上限 {MAX_OPS} 条）")
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"第 {index + 1} 条操作不是对象")
        if op.get("kind") not in _ALL_KINDS:
            raise ValueError(f"第 {index + 1} 条操作的 kind 不支持：{op.get('kind')!r}")

    # 先整体读进内存：out_path 与 src_path 相同时（覆盖保存），后面 os.replace
    # 会替换掉源文件，必须提前松开源文件句柄，否则 Windows 上直接 PermissionError。
    with open(src_path, "rb") as handle:
        source_bytes = handle.read()
    try:
        reader = PdfReader(io.BytesIO(source_bytes))
    except Exception as exc:
        raise ValueError(f"无法解析源 PDF：{exc}") from exc
    if reader.is_encrypted:
        try:
            decrypted = reader.decrypt("")
        except Exception:
            raise ValueError("源 PDF 已加密，无法编辑") from None
        if not decrypted:
            raise ValueError("源 PDF 已加密，无法编辑")

    warnings = []
    page_ops = [op for op in ops if op.get("kind") in _PAGE_KINDS]
    mark_ops = [op for op in ops if op.get("kind") in _MARK_KINDS]
    pages = _apply_page_ops(list(reader.pages), page_ops)

    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)

    font_name, font_warning = _ensure_cjk_font()
    if font_warning:
        warnings.append(font_warning)
    _apply_mark_ops(writer, mark_ops, font_name, warnings)

    _atomic_write(writer, out_absolute)
    result = {
        "path": out_absolute,
        "pages": len(writer.pages),
        "warnings": warnings,
    }
    if warnings:
        result["warning"] = "；".join(warnings)
    return result