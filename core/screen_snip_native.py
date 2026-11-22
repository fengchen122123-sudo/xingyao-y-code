from __future__ import annotations

import logging
import threading
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["clamp", "normalize_rect", "png_bytes_to_bitmap", "SnipOverlayNative"]

_System = None
_F = None
_D = None


def _load():
    global _System, _F, _D
    if _System is not None and _F is not None and _D is not None:
        return _System, _F, _D
    import clr

    clr.AddReference("System")
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    import System
    import System.Drawing as D
    import System.Windows.Forms as F

    _System, _F, _D = System, F, D
    return _System, _F, _D


def clamp(v, lo, hi):
    """把 ``v`` 夹紧到闭区间 ``[lo, hi]``。"""
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def normalize_rect(x0, y0, x1, y1, max_w, max_h):
    left = clamp(min(x0, x1), 0, max_w)
    top = clamp(min(y0, y1), 0, max_h)
    right = clamp(max(x0, x1), 0, max_w)
    bottom = clamp(max(y0, y1), 0, max_h)
    w = right - left
    h = bottom - top
    if w <= 0 or h <= 0:
        return None
    return (left, top, w, h)


def png_bytes_to_bitmap(png_bytes):
    """PNG 字节 → ``System.Drawing.Bitmap``（MemoryStream 中转）。

    GDI+ 要求源流在 Bitmap 整个生命周期内保持打开；这里从流解码后再复制一份
    独立像素并立即释放流与原位图，使返回的位图不悬挂任何流引用（否则流被 GC
    后 DrawImage 会抛"GDI+ 中发生一般性错误"）。
    """
    _System, _F, D = _load()
    ms = _System.IO.MemoryStream(png_bytes)
    try:
        decoded = D.Bitmap(ms)
        try:
            return D.Bitmap(decoded)
        finally:
            decoded.Dispose()
    finally:
        ms.Dispose()


class SnipOverlayNative:

    MIN_SEL = 8

    PEN_WIDTH = 3
    PEN_COLORS = [
        (239, 68, 68),
        (234, 179, 8),
        (34, 197, 94),
        (59, 130, 246),
        (255, 255, 255),
        (0, 0, 0),
    ]

    def __init__(self, png_bytes, on_action):
        self._png_bytes = png_bytes
        self._on_action = on_action
        self._form = None
        self._frame = None
        self._dark = None
        self._border_pen = None
        self._font = None
        self._text_brush = None
        self._label_bg = None
        self._toolbar = None
        self._sel = None
        self._anchor = None
        self._mode = "select"
        self._pen_color = (239, 68, 68)
        self._strokes = []
        self._current_stroke = None
        self._pen_btn = None
        self._color_buttons = {}
        self._closed = False
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        """蒙版是否已关闭（供宿主做重复调用保护）。"""
        return self._closed

    def show(self, invoker) -> bool:
        """在主窗口 GUI 线程上创建并显示蒙版；成功返回 True，失败记日志返回 False。"""

        def _create():
            self._build_form()

        try:
            _System, F, _D = _load()
            invoker.Invoke(F.MethodInvoker(_create))
            return True
        except Exception:
            logger.exception("创建原生截图蒙版失败")
            return False

    def _build_form(self):
        _System, F, D = _load()
        from core.screen_snip import get_virtual_screen_rect

        vx, vy, vw, vh = get_virtual_screen_rect()

        self._frame = png_bytes_to_bitmap(self._png_bytes)
        self._dark = D.SolidBrush(D.Color.FromArgb(100, 0, 0, 0))
        self._border_pen = D.Pen(D.Color.FromArgb(59, 130, 246), 2)
        self._font = D.Font("Microsoft YaHei UI", 9.0)
        self._text_brush = D.SolidBrush(D.Color.White)
        self._label_bg = D.SolidBrush(D.Color.FromArgb(220, 20, 24, 32))

        form = F.Form()
        form.FormBorderStyle = getattr(F.FormBorderStyle, "None")
        form.TopMost = True
        form.ShowInTaskbar = False
        form.StartPosition = F.FormStartPosition.Manual
        form.Location = D.Point(vx, vy)
        form.Size = D.Size(vw, vh)
        form.KeyPreview = True
        form.Cursor = F.Cursors.Cross
        form.BackColor = D.Color.Black
        form.SetStyle(
            F.ControlStyles.OptimizedDoubleBuffer
            | F.ControlStyles.AllPaintingInWmPaint
            | F.ControlStyles.UserPaint,
            True,
        )

        form.Paint += F.PaintEventHandler(self._on_paint)
        form.MouseDown += F.MouseEventHandler(self._on_mouse_down)
        form.MouseMove += F.MouseEventHandler(self._on_mouse_move)
        form.MouseUp += F.MouseEventHandler(self._on_mouse_up)
        form.KeyDown += F.KeyEventHandler(self._on_key_down)

        self._build_toolbar(form)

        form.Show()
        form.Activate()
        form.Focus()
        self._form = form

    def _build_toolbar(self, form):
        _System, F, D = _load()
        panel = F.FlowLayoutPanel()
        panel.FlowDirection = F.FlowDirection.LeftToRight
        panel.AutoSize = True
        # 必须显式 GrowAndShrink：AutoSize 默认 GrowOnly 只增不减，而
        # FlowLayoutPanel 默认高度 100 大于工具条内容高度，面板底部会留一条
        # 约 51px 的空白黑边（看着像"黑框偏大"，2026-10-14 实测修复）
        panel.AutoSizeMode = F.AutoSizeMode.GrowAndShrink
        panel.BackColor = D.Color.FromArgb(230, 30, 34, 44)
        panel.Padding = F.Padding(4)
        panel.Visible = False
        self._pen_btn = self._make_button("画笔", "pen", False)
        panel.Controls.Add(self._pen_btn)
        for color in self.PEN_COLORS:
            panel.Controls.Add(self._make_color_button(color))
        panel.Controls.Add(self._make_button("清除", "clear", False))
        for text, action, primary in (
            ("发到输入框", "send", True),
            ("复制", "copy", False),
            ("保存", "save", False),
            ("取消", "cancel", False),
        ):
            panel.Controls.Add(self._make_button(text, action, primary))
        form.Controls.Add(panel)
        self._toolbar = panel

    def _make_button(self, text, action, primary):
        _System, F, D = _load()
        btn = F.Button()
        btn.Text = text
        btn.AutoSize = True
        btn.FlatStyle = F.FlatStyle.Flat
        btn.ForeColor = D.Color.White
        btn.BackColor = (
            D.Color.FromArgb(59, 130, 246) if primary else D.Color.FromArgb(60, 64, 74)
        )
        btn.Cursor = F.Cursors.Hand
        btn.Click += _System.EventHandler(lambda s, e, a=action: self._do_action(a))
        return btn

    def _make_color_button(self, color):
        _System, F, D = _load()
        btn = F.Button()
        btn.Text = ""
        btn.AutoSize = False
        btn.Size = D.Size(24, 24)
        btn.FlatStyle = F.FlatStyle.Flat
        btn.BackColor = D.Color.FromArgb(color[0], color[1], color[2])
        btn.Cursor = F.Cursors.Hand
        btn.Click += _System.EventHandler(
            lambda s, e, c=color: self._set_pen_color(c)
        )
        self._color_buttons[color] = btn
        return btn

    def _set_pen_color(self, color):
        self._pen_color = color
        self._update_pen_ui()

    def _toggle_pen_mode(self):
        self._mode = "pen" if self._mode == "select" else "select"
        self._update_pen_ui()

    def _update_pen_ui(self):
        _System, _F, D = _load()
        active = self._mode == "pen"
        if self._pen_btn is not None:
            self._pen_btn.BackColor = (
                D.Color.FromArgb(234, 179, 8)
                if active
                else D.Color.FromArgb(60, 64, 74)
            )
        for color, btn in self._color_buttons.items():
            btn.FlatAppearance.BorderSize = (
                2 if (active and color == self._pen_color) else 0
            )

    def _clear_strokes(self):
        self._strokes = []
        self._current_stroke = None
        if self._form is not None:
            self._form.Invalidate()

    def _start_stroke(self, x, y):
        self._current_stroke = {
            "color": self._pen_color,
            "width": self.PEN_WIDTH,
            "points": [(x, y)],
        }
        self._strokes.append(self._current_stroke)

    def _extend_stroke(self, x, y):
        if self._current_stroke is not None:
            self._current_stroke["points"].append((x, y))

    def _end_stroke(self):
        self._current_stroke = None

    def _position_toolbar(self, form):
        _System, _F, D = _load()
        if self._toolbar is None or self._sel is None:
            return
        x, y, w, h = self._sel
        cw = form.ClientSize.Width
        ch = form.ClientSize.Height
        size = self._toolbar.PreferredSize
        tw = int(size.Width)
        th = int(size.Height)
        tx = x + w - tw
        ty = y + h + 8
        if ty + th > ch:
            ty = y - th - 8
        tx = int(clamp(tx, 0, max(0, cw - tw)))
        ty = int(clamp(ty, 0, max(0, ch - th)))
        self._toolbar.Location = D.Point(tx, ty)
        self._toolbar.Visible = True
        self._toolbar.BringToFront()

    def _on_paint(self, sender, e):
        g = e.Graphics
        if self._frame is None:
            return
        g.DrawImageUnscaled(self._frame, 0, 0)
        if self._sel is None:
            g.FillRectangle(self._dark, sender.ClientRectangle)
            hint = "按住左键拖动框选要截图的区域，Esc 取消"
            size = g.MeasureString(hint, self._font)
            hx = (sender.ClientRectangle.Width - size.Width) / 2.0
            g.DrawString(hint, self._font, self._text_brush, float(hx), 24.0)
        else:
            self._draw_selection(g, sender)
        self._draw_strokes(g)

    def _draw_selection(self, g, form):
        x, y, w, h = self._sel
        cw = form.ClientSize.Width
        ch = form.ClientSize.Height
        if y > 0:
            g.FillRectangle(self._dark, 0, 0, cw, y)
        if x > 0:
            g.FillRectangle(self._dark, 0, y, x, h)
        if x + w < cw:
            g.FillRectangle(self._dark, x + w, y, cw - (x + w), h)
        if y + h < ch:
            g.FillRectangle(self._dark, 0, y + h, cw, ch - (y + h))
        g.DrawRectangle(self._border_pen, x, y, w, h)
        label = "%d × %d" % (w, h)
        size = g.MeasureString(label, self._font)
        lx = float(x)
        ly = float(y) - float(size.Height) - 6.0
        if ly < 0:
            lx = float(x + 4)
            ly = float(y + 4)
        g.FillRectangle(
            self._label_bg,
            lx - 2.0,
            ly - 1.0,
            float(size.Width) + 4.0,
            float(size.Height) + 2.0,
        )
        g.DrawString(label, self._font, self._text_brush, lx, ly)

    def _draw_strokes(self, g):
        _System, _F, D = _load()
        for stroke in self._strokes:
            points = stroke["points"]
            if len(points) < 2:
                continue
            pen = D.Pen(
                D.Color.FromArgb(
                    stroke["color"][0], stroke["color"][1], stroke["color"][2]
                ),
                float(stroke["width"]),
            )
            try:
                pen.StartCap = D.Drawing2D.LineCap.Round
                pen.EndCap = D.Drawing2D.LineCap.Round
                pen.LineJoin = D.Drawing2D.LineJoin.Round
                g.DrawLines(
                    pen,
                    [D.Point(px, py) for px, py in points],
                )
            finally:
                pen.Dispose()

    def _on_mouse_down(self, sender, e):
        _System, F, _D = _load()
        if e.Button != F.MouseButtons.Left:
            return
        if self._toolbar is not None and self._toolbar.Visible:
            if self._toolbar.Bounds.Contains(e.X, e.Y):
                return
        if self._mode == "pen":
            self._start_stroke(e.X, e.Y)
            sender.Invalidate()
            return
        self._anchor = (e.X, e.Y)
        self._sel = None
        if self._toolbar is not None:
            self._toolbar.Visible = False
        sender.Invalidate()

    def _on_mouse_move(self, sender, e):
        if self._mode == "pen":
            if self._current_stroke is not None:
                self._extend_stroke(e.X, e.Y)
                sender.Invalidate()
            return
        if self._anchor is None:
            return
        rect = normalize_rect(
            self._anchor[0],
            self._anchor[1],
            e.X,
            e.Y,
            sender.ClientSize.Width,
            sender.ClientSize.Height,
        )
        if rect != self._sel:
            self._sel = rect
            sender.Invalidate()

    def _on_mouse_up(self, sender, e):
        if self._mode == "pen":
            self._end_stroke()
            return
        if self._anchor is None:
            return
        # 用 mouse-up 坐标定稿选区：防止快速拖动时最后一次 move 未跟上导致选区偏小
        self._sel = normalize_rect(
            self._anchor[0], self._anchor[1], e.X, e.Y,
            sender.ClientSize.Width, sender.ClientSize.Height,
        )
        self._anchor = None
        sel = self._sel
        if sel is None or sel[2] < self.MIN_SEL or sel[3] < self.MIN_SEL:
            if self._sel is not None:
                self._sel = None
                sender.Invalidate()
            return
        self._position_toolbar(sender)

    def _on_key_down(self, sender, e):
        _System, F, _D = _load()
        if e.KeyCode == F.Keys.Escape:
            self._do_action("cancel")
        elif e.KeyCode == F.Keys.Enter and self._sel is not None:
            self._do_action("send")

    def _do_action(self, action):
        if action == "pen":
            self._toggle_pen_mode()
            return
        if action == "clear":
            self._clear_strokes()
            return
        rect = self._sel
        self._close()
        try:
            self._on_action(action, rect, list(self._strokes))
        except Exception:
            logger.exception("截图动作回调失败: %s", action)

    def _close(self):
        """幂等关闭：加锁置位防重入，关窗容错，最后释放 GDI 资源。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._form is not None:
                self._form.Close()
        except Exception:
            logger.exception("关闭原生截图蒙版窗口失败")
        self._dispose_gdi()

    def _dispose_gdi(self):
        for attr in (
            "_frame",
            "_dark",
            "_border_pen",
            "_font",
            "_text_brush",
            "_label_bg",
        ):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                obj.Dispose()
            except Exception:
                pass
            setattr(self, attr, None)
