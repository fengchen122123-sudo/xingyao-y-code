"""GUI 截图桥接 Mixin：右面板「画笔截图」+ 屏幕「任意位置截图」。

右面板画笔截图：前端进入截图模式前先调 capture_right_panel_region 把整块
右面板（含原生 WebView2 浏览器表面，此刻仍可见）捕获为 PNG dataURL 冻结帧；
随后前端隐藏原生表面、把冻结帧铺成覆盖层供画笔圈选，裁剪在前端完成。后端
只补两件前端做不了的事：屏幕区域捕获与系统剪贴板/落盘写出。

全屏任意位置截图（2026-10-14，设计 docs/superpowers/specs/
2026-10-14-screen-snip-design.md）：前端调 screen_snip_start，后端捕获整个
虚拟桌面（core/screen_snip）并以 create_window(html=...) 打开一个覆盖虚拟
桌面、无边框置顶的遮罩窗口供框选；遮罩页的 snip_copy / snip_save /
snip_send / snip_close 由 ScreenSnipWindowApi 承接，「发到输入框」经
emit_event('snip_attachment_ready') 回传主窗口加入附件。create_window(html=)
走 winforms 后端 CoreWebView2.NavigateToString 直载 HTML 字符串，不落临时
文件、不依赖内置 HTTP server（js_api 调用经 WebView2 的 WebMessage 通道
直达 Python），规避了运行时新建窗口对 server root_path / 盘符的依赖。

坐标换算与内嵌浏览器表面定位共享同一套假设：pywebview winforms 启动即
SetProcessDPIAware()，ClientToScreen/GetClientRect 返回物理像素，
PIL.ImageGrab 的 bbox 也在同一物理空间；CSS→物理缩放复用
EmbeddedBrowserController._compute_scale（GetDpiForWindow → DeviceDpi → 1.0），
避免两处 DPI 逻辑漂移。

所有桥方法返回 dict 且吞异常（{'ok': False, 'msg': ...}），绝不抛穿 js_api；
剪贴板 GlobalAlloc/SetClipboardData、客户区测量的 ClientToScreen/GetClientRect
全部显式设 argtypes/restype=c_void_p（64 位句柄截断教训，同
core/computer/win32_adapter.py 注释）。窗体句柄 / DPI / 文件对话框都必须经
form.Invoke 同步 marshal 回 GUI 线程（js_api 方法在 pywebview 独立线程执行），
与 core/embedded_browser.py 头部约定的同一套规则。
"""

from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import struct
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 冻结帧 dataURL 的 base64 体积防线：超出且图片最长边超限时等比缩小一次
# 重编码；图片本身不大时原样返回（桥回传几 MB JSON 可用，不做硬失败）
_MAX_B64_BYTES = 6 * 1024 * 1024
_PNG_MAX_EDGE = 1920
# 解码侧原始 PNG 上限，与 save_clipboard_image 的 8MB 约定对齐
_MAX_PNG_BYTES = 8 * 1024 * 1024
_PNG_B64_PREFIX = "data:image/png;base64,"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_CF_DIB = 8
_GMEM_MOVEABLE = 0x0002


def default_panel_grab(bbox: Tuple[int, int, int, int]):
    """生产捕获入口：方法内惰性导入 PIL.ImageGrab，捕获虚拟桌面指定区域。"""
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox, all_screens=True)


# 全屏截图（screen_snip_start）的调度锁：串行化"检查已有窗口 → 捕获 → 建窗"，
# 防止双击/连点在窗口尚未登记前创建出两个遮罩窗口（后到者应走聚焦路径）。
# 必须用 RLock：start 持锁期间会调 _reveal_screen_snip_window（同样要拿这把锁），
# 普通 Lock 会自锁死。
_SCREEN_SNIP_LOCK = threading.RLock()

# 遮罩窗口的"就绪前隐藏"兜底时限（秒）：页面正常情况下会在装载完成后立即回调
# snip_ready 显示窗口；超过该时限仍未回调（脚本异常/桥注入失败）则无条件显示，
# 避免出现"点了截图没反应"。取值兼顾慢机器上的 WebView2 首次初始化。
_SCREEN_SNIP_SHOW_FALLBACK = 2.5


def default_virtual_screen_rect() -> Tuple[int, int, int, int]:
    """生产入口：惰性导入 core.screen_snip 读取虚拟桌面矩形（测试注入点）。"""
    from core.screen_snip import get_virtual_screen_rect

    return get_virtual_screen_rect()


def default_capture_virtual_screen():
    """生产入口：惰性导入 core.screen_snip 捕获整个虚拟桌面（测试注入点）。"""
    from core.screen_snip import capture_virtual_screen

    return capture_virtual_screen()


def encode_png_data_url(image) -> str:
    """PIL Image → PNG dataURL（与 capture_right_panel_region 的内联编码同口径）。"""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return _PNG_B64_PREFIX + base64.b64encode(buf.getvalue()).decode("ascii")


def encode_png_data_url_from_bytes(png: bytes) -> str:
    """PNG 字节 → dataURL（裁剪结果转给复用现有剪贴板/另存通道时用）。"""
    return _PNG_B64_PREFIX + base64.b64encode(png).decode("ascii")


# WebView2 NavigateToString 的 HTML 内容硬上限（2MB，超限直接抛
# "值不在预期的范围内"、窗口全黑）。遮罩页经 create_window(html=...) 直载，
# 冻结帧必须连模板一起压到这个预算内。
_NAVIGATE_TO_STRING_LIMIT = 2 * 1024 * 1024
# 模板（HTML/CSS/JS + 占位符替换）体积预留，留足余量避免踩线
_TEMPLATE_RESERVE = 64 * 1024
# 冻结帧 dataURL 允许的最大字符数（base64 膨胀已含在内，直接比字符串长度）
_FRAME_DATA_URL_BUDGET = _NAVIGATE_TO_STRING_LIMIT - _TEMPLATE_RESERVE


def _jpeg_data_url(image, quality: int) -> str:
    """单次 JPEG 编码为 dataURL（JPEG 体积可控，是绕开 2MB 上限的关键）。"""
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def encode_frozen_frame(image) -> str:
    """把整屏冻结帧编码为体积可控的预览 dataURL（遮罩窗口显示用）。

    为什么不是 PNG：``create_window(html=...)`` 走 WebView2 的
    ``NavigateToString``，该接口对 HTML 内容有 2MB 硬上限；1920x2280 的整屏
    PNG 转 base64 后轻松到 2.6MB，必然超限（现象：遮罩窗口全黑）。因此改用
    JPEG 逐级降质/缩放，把 **dataURL 长度本身**压进预算——预览只负责"看得清、
    框得准"，最终输出由 Python 从原始全分辨率图裁剪（见 crop_frozen_frame），
    不损失画质。
    """
    from PIL import Image

    source = image.convert("RGB")
    # 先原尺寸降质，再逐步缩边——绝大多数桌面画面第一档即可通过
    attempts = [(85, None), (80, 2560), (75, 1920), (70, 1600), (65, 1280), (60, 1024)]
    for quality, max_edge in attempts:
        current = source
        if max_edge is not None and max(source.size) > max_edge:
            scale = max_edge / float(max(source.size))
            current = source.resize(
                (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
                Image.Resampling.LANCZOS,
            )
        data_url = _jpeg_data_url(current, quality)
        if len(data_url) <= _FRAME_DATA_URL_BUDGET:
            return data_url
    # 兜底：极端噪声画面（照片级壁纸）仍超限时再狠压一档，保证窗口一定能开
    scale = 800 / float(max(source.size)) if max(source.size) > 800 else 1.0
    fallback = source.resize(
        (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
        Image.Resampling.LANCZOS,
    )
    return _jpeg_data_url(fallback, 45)


def crop_frozen_frame(image, rect: Dict[str, Any]) -> bytes:
    """从原始全分辨率冻结帧按物理像素矩形裁剪，返回 PNG 字节。

    裁剪放在 Python 端的原因：页面预览是被压缩/缩小过的，且页面内联原图会撞
    NavigateToString 的 2MB 上限；页面只回报坐标，这里用原图输出，保证截图
    与屏幕 1:1 无损。
    """
    from PIL import Image

    left = max(0, int(rect.get("x", 0)))
    top = max(0, int(rect.get("y", 0)))
    width = max(1, int(rect.get("w", 1)))
    height = max(1, int(rect.get("h", 1)))
    right = min(image.width, left + width)
    bottom = min(image.height, top + height)
    if right <= left or bottom <= top:
        raise ValueError("选区超出屏幕范围")
    cropped = image.crop((left, top, right, bottom))
    buf = io.BytesIO()
    cropped.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def draw_strokes_on_png(png: bytes, strokes, offset) -> bytes:
    """把画笔笔迹合成到裁剪后的 PNG 上（无笔迹原样返回，不重新编码）。

    strokes 坐标是全屏（冻结帧）物理像素；offset=(ox,oy) 为裁剪选区左上角物理
    像素坐标——合成时减 offset 得到 cropped 内相对坐标。任何异常只记日志并返回
    原始 png（笔迹是增强，绝不能拖垮截图主流程）。
    """
    if not strokes:
        return png
    try:
        from PIL import Image, ImageDraw
        ox, oy = offset
        img = Image.open(io.BytesIO(png))
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        for stroke in strokes:
            pts = [(p[0] - ox, p[1] - oy) for p in stroke.get("points", [])]
            if len(pts) < 2:
                continue
            color = tuple(stroke.get("color", (239, 68, 68)))
            width = int(stroke.get("width", 3))
            draw.line(pts, fill=color, width=width, joint="curve")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        logger.exception("画笔笔迹合成失败")
        return png


def save_snip_png(png: bytes) -> str:
    """把截图 PNG 写入受信粘贴目录并登记，返回路径。

    复用 gui_bridge.save_clipboard_image 的通道约定（%TEMP%/ycode_paste/ +
    core.tools.register_paste_file 受信登记，模型可直接读取）；命名 snip_
    前缀与剪贴板粘贴的 paste_ 前缀区分，附件 chip 显示名更可读。
    """
    paste_dir = os.path.join(tempfile.gettempdir(), "ycode_paste")
    os.makedirs(paste_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ms = int(time.time() * 1000) % 1000
    path = os.path.join(paste_dir, f"snip_{ts}_{ms:03d}_{uuid.uuid4().hex[:6]}.png")
    with open(path, "wb") as fh:
        fh.write(png)
    from core.tools import register_paste_file

    register_paste_file(path)
    return path


def _window_client_frame(
    hwnd: int,
) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """窗口客户区在屏幕上的物理原点与尺寸（物理像素）。

    独立成模块级函数以便测试注入，绝不依赖真实 Win32。
    """
    user32 = ctypes.windll.user32

    class _POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    # 64 位句柄截断教训（同文件头注释 / win32_adapter）：windll 默认按 32 位
    # int 传递与返回，句柄 > 2**31 时会 OverflowError 或被截断成错误句柄，
    # HWND 与返回值必须显式声明（c_void_p / BOOL）。
    user32.ClientToScreen.argtypes = (ctypes.c_void_p, ctypes.POINTER(_POINT))
    user32.ClientToScreen.restype = ctypes.c_bool
    user32.GetClientRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_RECT))
    user32.GetClientRect.restype = ctypes.c_bool

    origin = _POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise OSError("ClientToScreen 失败")
    rect = _RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetClientRect 失败")
    return (int(origin.x), int(origin.y)), (int(rect.right), int(rect.bottom))


def physical_bbox_for_css(
    rect: Any,
    scale: float,
    client_origin: Tuple[int, int],
    client_size: Tuple[int, int],
) -> Optional[Tuple[int, int, int, int]]:
    """CSS 矩形 → 物理像素 bbox（屏幕坐标），夹紧到窗口客户区内。

    分量换算与 embedded_browser.css_rect_to_physical 同样逐项取整，
    保证捕获区域与原生表面定位逐像素一致。无效输入 / 夹紧后不足 2×2
    返回 None。
    """
    if not isinstance(rect, dict):
        return None
    try:
        factor = float(scale) if scale else 1.0
    except (TypeError, ValueError):
        factor = 1.0

    def _px(value: Any) -> Optional[int]:
        try:
            return int(round(float(value) * factor))
        except (TypeError, ValueError):
            return None

    px = [_px(rect.get(k)) for k in ("x", "y", "width", "height")]
    if any(p is None for p in px):
        return None
    x, y, width, height = (int(p) for p in px)  # type: ignore[arg-type]
    if width <= 0 or height <= 0:
        return None

    origin_x, origin_y = client_origin
    client_w, client_h = client_size
    left = origin_x + x
    top = origin_y + y
    right = left + width
    bottom = top + height
    # 夹紧到客户区半开区间 [origin, origin+size)
    left = max(left, origin_x)
    top = max(top, origin_y)
    right = min(right, origin_x + client_w)
    bottom = min(bottom, origin_y + client_h)
    if right - left < 2 or bottom - top < 2:
        return None
    return (left, top, right, bottom)


def decode_png_data_url(data_url: Any) -> bytes:
    """校验并解码 PNG dataURL；任何不合法都抛 ValueError（消息面向用户）。"""
    if not isinstance(data_url, str) or not data_url.startswith(_PNG_B64_PREFIX):
        raise ValueError("无效的图片数据")
    b64 = data_url[len(_PNG_B64_PREFIX):]
    if not b64 or len(b64) > _MAX_PNG_BYTES * 4 // 3 + 1024:
        raise ValueError("图片超过 8MB，无法处理")
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:
        raise ValueError("图片数据无法解码") from exc
    if len(raw) > _MAX_PNG_BYTES:
        raise ValueError("图片超过 8MB，无法处理")
    if not raw.startswith(_PNG_SIGNATURE):
        raise ValueError("不支持的图片格式")
    return raw


def dib_bytes_from_image(img: Any) -> bytes:
    """PIL Image → 24 位底向上 CF_DIB 字节（BITMAPINFOHEADER + 像素）。

    走 PIL 原生批量转换（raw BGR + 行对齐 stride + orientation=-1 即底向上），
    与旧的全 Python 三重循环逐字节等价，但不再有 ~622 万次（1920×1080）
    解释器循环——该路径在 js_api 线程同步执行，逐像素循环会直接卡住桥调用。
    """
    rgb = img.convert("RGB")
    width, height = rgb.size
    stride = ((width * 3 + 3) // 4) * 4
    header = struct.pack(
        "<IiiHHIIiiII",
        40,              # biSize
        width,           # biWidth
        height,          # biHeight（正数 = 底向上）
        1,               # biPlanes
        24,              # biBitCount
        0,               # biCompression = BI_RGB
        stride * height, # biSizeImage
        0, 0, 0, 0,      # 分辨率/调色板字段
    )
    # raw BGR：逐行 BGR（列序与 CF_DIB 一致）；stride：行尾补齐到 4 字节对齐；
    # orientation=-1：行序自底向上（与 BI_RGB 正 biHeight 约定一致）
    return header + rgb.tobytes("raw", "BGR", stride, -1)


def _set_clipboard_dib(dib: bytes, hwnd: int) -> bool:
    """把 DIB 写入系统剪贴板（CF_DIB）；成功后内存句柄归属系统。"""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 64 位下 windll 默认 restype 是 32 位 int，会把句柄截断（同
        # win32_adapter 的教训），句柄相关 API 全部显式声明
        user32.OpenClipboard.argtypes = (ctypes.c_void_p,)
        user32.OpenClipboard.restype = ctypes.c_bool
        user32.EmptyClipboard.argtypes = ()
        user32.EmptyClipboard.restype = ctypes.c_bool
        user32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
        user32.SetClipboardData.restype = ctypes.c_void_p
        user32.CloseClipboard.argtypes = ()
        user32.CloseClipboard.restype = ctypes.c_bool
        kernel32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalUnlock.restype = ctypes.c_bool
        kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
        kernel32.GlobalFree.restype = ctypes.c_void_p

        if not user32.OpenClipboard(hwnd or None):
            return False
        allocated = None
        try:
            user32.EmptyClipboard()
            allocated = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(dib))
            if not allocated:
                return False
            ptr = kernel32.GlobalLock(allocated)
            if not ptr:
                kernel32.GlobalFree(allocated)
                allocated = None
                return False
            try:
                ctypes.memmove(ptr, dib, len(dib))
            finally:
                kernel32.GlobalUnlock(allocated)
            if not user32.SetClipboardData(_CF_DIB, allocated):
                kernel32.GlobalFree(allocated)
                allocated = None
                return False
            allocated = None  # 成功后句柄归剪贴板所有
            return True
        finally:
            if allocated:
                kernel32.GlobalFree(allocated)
            user32.CloseClipboard()
    except Exception:
        logger.debug("写剪贴板失败", exc_info=True)
        return False


class SnipBridgeMixin:
    """为 ApiBridge 提供右面板画笔截图的捕获 / 剪贴板 / 保存桥方法。"""

    def _main_form(self):
        """主窗体查找（与 BrowserBridgeMixin._get_form 同一套约定）。"""
        try:
            import webview.platforms.winforms as wf

            uid = getattr(getattr(self, "_window", None), "uid", None) or "master"
            return wf.BrowserView.instances.get(uid)
        except Exception:
            logger.debug("获取主窗体失败", exc_info=True)
            return None

    def _invoke_gui(self, form, callback):
        """把回调同步 marshal 到 GUI 线程执行，返回 (ok, 返回值)。

        与 core/embedded_browser.EmbeddedBrowserController._invoke 同一套约定
        （桥方法从 js_api 线程进入，WinForms/DPI 必须回到 GUI 线程）。窗体的
        Handle 也只能在 GUI 线程内读——WinForms 跨线程访问会抛
        InvalidOperationException，所以连"句柄是否就绪"的探测都放在委托里。
        句柄未创建（ToInt32()==0）、窗体已销毁（Invoke 抛异常）时返回
        (False, None)，由调用方按既有 error dict 降级，绝不抛穿桥层。
        """
        if form is None:
            return False, None
        box: Dict[str, Any] = {}

        def _run() -> None:
            handle = getattr(form, "Handle", None)
            if handle is not None and hasattr(handle, "ToInt32") and handle.ToInt32() == 0:
                box["ready"] = False  # 句柄还没建起来
                return
            box["ready"] = True
            box["value"] = callback()

        try:
            from core.embedded_browser import _make_delegate

            form.Invoke(_make_delegate(_run))
        except Exception:
            logger.debug("GUI 线程 Invoke 失败（窗体可能已销毁）", exc_info=True)
            return False, None
        if not box.get("ready", False):
            return False, None
        return True, box.get("value")

    # ---------------------------------------------------------------- 桥方法

    def capture_right_panel_region(self, rect: Dict[str, Any]) -> Dict[str, Any]:
        """捕获右面板当前像素（含原生浏览器表面）为 PNG dataURL 冻结帧。

        必须在前端隐藏原生表面之前调用——冻结帧要包含浏览器内容。
        """
        try:
            if sys.platform != "win32":
                return {"ok": False, "msg": "面板截图仅 Windows 桌面端可用"}
            form = self._main_form()
            if form is None:
                return {"ok": False, "msg": "窗口尚未就绪"}

            def _read_geometry():
                # 句柄 / DPI 换算 / 客户区测量全部在 GUI 线程内完成（form.Invoke）
                hwnd = int(form.Handle.ToInt32())
                if not hwnd:
                    return None
                # 复用内嵌浏览器的 DPI 换算：GetDpiForWindow → DeviceDpi → 1.0
                from core.embedded_browser import EmbeddedBrowserController

                scale = EmbeddedBrowserController._compute_scale(form)
                origin, client_size = _window_client_frame(hwnd)
                return scale, origin, client_size

            ok, geometry = self._invoke_gui(form, _read_geometry)
            if not ok or geometry is None:
                return {"ok": False, "msg": "窗口尚未就绪"}
            scale, origin, client_size = geometry
            bbox = physical_bbox_for_css(rect, scale, origin, client_size)
            if bbox is None:
                return {"ok": False, "msg": "截图区域无效"}
            image = default_panel_grab(bbox)
            if image is None:
                return {"ok": False, "msg": "屏幕捕获失败"}
            width, height = image.size
            if width <= 0 or height <= 0:
                return {"ok": False, "msg": "屏幕捕获失败"}

            def _encode(im) -> str:
                buf = io.BytesIO()
                im.save(buf, format="PNG")
                return _PNG_B64_PREFIX + base64.b64encode(buf.getvalue()).decode("ascii")

            data_url = _encode(image)
            if len(data_url) > _MAX_B64_BYTES and max(width, height) > _PNG_MAX_EDGE:
                from PIL import Image

                ratio = _PNG_MAX_EDGE / max(width, height)
                resized = image.resize(
                    (max(1, int(width * ratio)), max(1, int(height * ratio))),
                    Image.Resampling.LANCZOS,
                )
                data_url = _encode(resized)
                width, height = resized.size
            return {
                "ok": True,
                "data_url": data_url,
                "width": width,
                "height": height,
            }
        except Exception as exc:
            logger.exception("capture_right_panel_region 失败")
            return {"ok": False, "msg": str(exc)}

    def snip_copy_clipboard(self, data_url: str) -> Dict[str, Any]:
        """把画笔截图 PNG 写入系统剪贴板（CF_DIB，粘贴到任意应用可用）。"""
        try:
            png = decode_png_data_url(data_url)
        except ValueError as exc:
            return {"ok": False, "msg": str(exc)}
        except Exception as exc:
            logger.exception("snip_copy_clipboard 解码失败")
            return {"ok": False, "msg": str(exc)}
        try:
            from PIL import Image

            with Image.open(io.BytesIO(png)) as img:
                dib = dib_bytes_from_image(img)
            hwnd = 0
            form = self._main_form()
            if form is not None:
                try:
                    # 句柄只能在 GUI 线程读（WinForms 跨线程访问约束）
                    ok, value = self._invoke_gui(form, lambda: int(form.Handle.ToInt32()))
                    hwnd = int(value) if (ok and value) else 0
                except Exception:
                    # 降级语义不变（hwnd=0），但必须留痕，不再无声吞掉
                    logger.debug("读取窗口句柄失败（剪贴板以 hwnd=0 写入）", exc_info=True)
                    hwnd = 0
            if not _set_clipboard_dib(dib, hwnd):
                return {"ok": False, "msg": "写入剪贴板失败"}
            return {"ok": True}
        except Exception as exc:
            logger.exception("snip_copy_clipboard 失败")
            return {"ok": False, "msg": str(exc)}

    def snip_save_image(self, data_url: str, suggested_name: str = "") -> Dict[str, Any]:
        """画笔截图另存为：弹系统保存对话框，写入所选路径。"""
        try:
            png = decode_png_data_url(data_url)
        except ValueError as exc:
            return {"ok": False, "msg": str(exc)}
        except Exception as exc:
            logger.exception("snip_save_image 解码失败")
            return {"ok": False, "msg": str(exc)}
        try:
            import webview

            if not getattr(self, "_window", None):
                return {"ok": False, "msg": "窗口尚未就绪"}
            form = self._main_form()
            if form is None:
                return {"ok": False, "msg": "窗口尚未就绪"}
            home = Path.home()
            directory = home / "Pictures"
            if not directory.is_dir():
                directory = home
            name = str(suggested_name or "").strip()
            if not name:
                name = f"ycode截图_{time.strftime('%Y%m%d_%H%M%S')}.png"
            name = os.path.basename(name).replace("/", "_").replace("\\", "_") or "ycode截图.png"

            def _ask_save_path():
                # 文件对话框是 WinForms 控件操作，必须在 GUI 线程弹出
                return self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    directory=str(directory),
                    save_filename=name,
                )

            ok, result = self._invoke_gui(form, _ask_save_path)
            if not ok:
                return {"ok": False, "msg": "窗口尚未就绪"}
            path = ""
            if isinstance(result, (list, tuple)):
                path = str(result[0]) if result else ""
            elif result:
                path = str(result)
            if not path:
                return {"ok": False, "msg": "已取消选择"}
            if not Path(path).suffix:
                path += ".png"
            with open(path, "wb") as fh:
                fh.write(png)
            return {"ok": True, "path": path}
        except Exception as exc:
            logger.exception("snip_save_image 失败")
            return {"ok": False, "msg": str(exc)}

    # ------------------------------------------------ 全屏任意位置截图（入口）

    def screen_snip_start(self) -> Dict[str, Any]:
        """捕获整个虚拟桌面并弹出原生全屏截图蒙版（2026-10-14 原生重写）。

        在 pywebview 桥调用线程执行：捕获屏幕后，经主窗口 Form 的 Invoke 把原生
        蒙版（core/screen_snip_native.SnipOverlayNative，WinForms+GDI）的创建与
        显示 marshal 到 GUI 消息循环线程。原生窗口创建即显示——无 WebView2 初始化
        黑屏、无窗口尺寸削减、可完整盖住任务栏（POC 实测验证）。重复调用保护：
        已有存活蒙版时不重复弹出。所有失败归一 {"ok": False, "msg": ...}，绝不抛穿。
        """
        try:
            with _SCREEN_SNIP_LOCK:
                existing = getattr(self, "_screen_snip_overlay", None)
                if existing is not None and not existing.closed:
                    return {"ok": True, "msg": "截图窗口已打开"}
                self._screen_snip_overlay = None

                if sys.platform != "win32":
                    return {"ok": False, "msg": "全屏截图仅 Windows 桌面端可用"}

                rect = default_virtual_screen_rect()
                image = default_capture_virtual_screen()
                if tuple(getattr(image, "size", ())) != (rect[2], rect[3]):
                    # 与 core/computer/windows_backend 同一口径：截图尺寸与
                    # Win32 度量不同源时宁可失败，也不给用户一张错位的冻结帧
                    return {"ok": False, "msg": "屏幕捕获尺寸异常，请重试"}

                # 原图保留供动作阶段裁剪（无损）；蒙版背景也用原图 PNG——原生
                # GDI 绘制不走 NavigateToString，无 2MB 上限，无需压缩预览
                self._screen_snip_source = image
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                png = buf.getvalue()

                main_form = self._main_form()
                if main_form is None:
                    self._screen_snip_source = None
                    return {"ok": False, "msg": "主窗口尚未就绪"}

                from core.screen_snip_native import SnipOverlayNative

                overlay = SnipOverlayNative(png, self._on_snip_action)
                if not overlay.show(main_form):
                    self._screen_snip_source = None
                    return {"ok": False, "msg": "截图蒙版显示失败"}
                self._screen_snip_overlay = overlay
                return {"ok": True}
        except Exception as exc:
            # 异常路径清掉整屏原图引用（4K 屏约 30MB+），避免滞留到下次截图
            self._screen_snip_source = None
            logger.exception("screen_snip_start 失败")
            return {"ok": False, "msg": str(exc)}

    def _reveal_screen_snip_window(self) -> None:
        """显示遮罩窗口（幂等）：页面就绪回调 / 超时兜底 / 重复调用三条路径共用。

        线程安全：加锁 + 标志位，保证"就绪上报"与"超时兜底"并发时只显示一次。
        窗口操作经 pywebview 内部 Invoke marshal 回 GUI 线程（已核实实现）。
        """
        with _SCREEN_SNIP_LOCK:
            if getattr(self, "_screen_snip_shown", False):
                return
            window = getattr(self, "_screen_snip_window", None)
            if window is None or window.events.closed.is_set():
                return
            self._screen_snip_shown = True
            timer = getattr(self, "_screen_snip_timer", None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    logger.debug("取消截图窗口兜底计时器失败", exc_info=True)
        try:
            window.show()
        except Exception:
            # 显示失败则回滚标志，允许后续（重复调用 / 超时兜底）再试一次
            self._screen_snip_shown = False
            logger.debug("显示截图窗口失败", exc_info=True)

    def _focus_screen_snip_window(self, window) -> None:
        """把已有截图窗口拉回前台（best-effort；失败只留痕，不影响返回）。"""
        try:
            window.restore()
        except Exception:
            logger.debug("还原截图窗口失败", exc_info=True)
        try:
            import webview.platforms.winforms as wf

            form = wf.BrowserView.instances.get(getattr(window, "uid", None))
            hwnd = int(form.Handle.ToInt32()) if form is not None else 0
            if hwnd:
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow.argtypes = (ctypes.c_void_p,)
                user32.SetForegroundWindow.restype = ctypes.c_bool
                user32.SetForegroundWindow(hwnd)
        except Exception:
            logger.debug("聚焦截图窗口失败", exc_info=True)

    def _on_snip_action(self, action: str, rect, strokes=None) -> None:
        """原生截图蒙版的动作回调（在 GUI 线程被调用，蒙版窗口已先行关闭）。

        rect 为冻结帧物理像素矩形 (x,y,w,h) 或 None（cancel）；strokes 为画笔笔迹
        （冻结帧物理像素坐标）。裁剪从原始全分辨率图执行，再合成笔迹，输出无损。
        """
        self._screen_snip_overlay = None
        try:
            if action == "cancel" or not rect:
                return
            source = self._screen_snip_source
            if source is None:
                return
            x, y, w, h = rect
            png = crop_frozen_frame(source, {"x": x, "y": y, "w": w, "h": h})
            if strokes:
                # 笔迹坐标是全屏物理像素，减选区偏移得到裁剪图内坐标
                png = draw_strokes_on_png(png, strokes, (x, y))
            if action == "copy":
                self.snip_copy_clipboard(encode_png_data_url_from_bytes(png))
            elif action == "save":
                self.snip_save_image(encode_png_data_url_from_bytes(png))
            elif action == "send":
                path = save_snip_png(png)
                self.emit_event("snip_attachment_ready", {"path": path})
        except Exception:
            logger.exception("截图动作处理失败: %s", action)
        finally:
            self._screen_snip_source = None


class ScreenSnipWindowApi:
    """截图遮罩窗口的 js_api（设计 2026-10-14）。

    遮罩页 JS 调用 snip_copy / snip_save / snip_send / snip_close；pywebview
    的 js_bridge_call 每次调用起独立线程，因此窗口操作（hide/show/destroy）
    全部经 pywebview 内部 Invoke marshal 回 GUI 线程（其实现在
    webview/platforms/winforms.py 已核实）。暴露面按 dir() 枚举生成：以下划
    线开头的属性（_bridge / _live_window 等）不会被注入 JS 端函数清单。
    """

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    # ---------------------------------------------------------------- 内部

    def _live_window(self):
        """当前存活截图窗口；已关闭/不存在返回 None。"""
        win = getattr(self._bridge, "_screen_snip_window", None)
        if win is None or win.events.closed.is_set():
            return None
        return win

    def _crop_selection(self, rect: Any) -> bytes:
        """从原始全分辨率冻结帧裁剪选区，返回 PNG 字节（页面只回报坐标）。"""
        source = getattr(self._bridge, "_screen_snip_source", None)
        if source is None:
            raise RuntimeError("截图原图已释放，请重新截图")
        if not isinstance(rect, dict):
            raise ValueError("选区参数无效")
        return crop_frozen_frame(source, rect)

    def _destroy_window(self, win) -> None:
        """关闭窗口并把引用置空；destroy 失败只留痕，不改变本动作结果。"""
        if getattr(self._bridge, "_screen_snip_window", None) is win:
            self._bridge._screen_snip_window = None
        # 原图随窗口一起释放：整屏位图内存不小，留着只会白占（下次截图会重捕）
        self._bridge._screen_snip_source = None
        # 就绪兜底计时器与显示标志一并复位，供下一次截图使用
        timer = getattr(self._bridge, "_screen_snip_timer", None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                logger.debug("取消截图窗口兜底计时器失败", exc_info=True)
        self._bridge._screen_snip_timer = None
        try:
            win.destroy()
        except Exception:
            logger.debug("关闭截图窗口失败（可能已关闭）", exc_info=True)

    # ---------------------------------------------------------------- 桥方法

    def snip_ready(self) -> Dict[str, Any]:
        """遮罩页就绪回报：冻结帧已可绘制，显示窗口（对齐微信"画面瞬现"）。

        窗口是 hidden=True 创建的，显示时机由本回调决定；宿主另有超时兜底，
        本方法重复调用无害（_reveal_screen_snip_window 幂等）。
        """
        try:
            self._bridge._reveal_screen_snip_window()
            return {"ok": True}
        except Exception as exc:
            logger.exception("截图窗口就绪上报失败")
            return {"ok": False, "msg": str(exc)}

    def snip_copy(self, rect: Any) -> Dict[str, Any]:
        """选区复制到系统剪贴板；成功后关闭遮罩窗口。"""
        try:
            win = self._live_window()
            if win is None:
                return {"ok": False, "msg": "截图窗口已关闭"}
            png = self._crop_selection(rect)
            res = self._bridge.snip_copy_clipboard(encode_png_data_url_from_bytes(png))
            if not res.get("ok"):
                return res
            self._destroy_window(win)
            return {"ok": True}
        except Exception as exc:
            logger.exception("截图窗口复制失败")
            return {"ok": False, "msg": str(exc)}

    def snip_save(self, rect: Any, suggested_name: str = "") -> Dict[str, Any]:
        """选区另存为 PNG；成功后关闭遮罩窗口，取消/失败则恢复遮罩继续操作。

        保存对话框弹出前先隐藏遮罩：遮罩窗口是置顶（TopMost）窗口，不隐藏
        会盖住非置顶的系统保存对话框。
        """
        try:
            win = self._live_window()
            if win is None:
                return {"ok": False, "msg": "截图窗口已关闭"}
            png = self._crop_selection(rect)
            data_url = encode_png_data_url_from_bytes(png)
            hidden = False
            try:
                win.hide()
                hidden = True
            except Exception:
                logger.debug("隐藏截图窗口失败（继续弹保存对话框）", exc_info=True)
            res = self._bridge.snip_save_image(data_url, suggested_name)
            if res.get("ok"):
                self._destroy_window(win)
                return res
            if hidden:
                try:
                    win.show()
                except Exception:
                    logger.debug("恢复截图窗口失败", exc_info=True)
            return res
        except Exception as exc:
            logger.exception("截图窗口保存失败")
            return {"ok": False, "msg": str(exc)}

    def snip_send(self, rect: Any) -> Dict[str, Any]:
        """发到输入框：裁剪 → PNG 落盘受信目录 → 事件回传主窗口 → 关闭遮罩窗口。"""
        try:
            win = self._live_window()
            if win is None:
                return {"ok": False, "msg": "截图窗口已关闭"}
            png = self._crop_selection(rect)
            path = save_snip_png(png)
            self._bridge.emit_event("snip_attachment_ready", {"path": path})
            self._destroy_window(win)
            return {"ok": True}
        except Exception as exc:
            logger.exception("截图发到输入框失败")
            return {"ok": False, "msg": str(exc)}

    def snip_close(self) -> Dict[str, Any]:
        """取消：关闭遮罩窗口（幂等）。"""
        try:
            win = self._live_window()
            if win is not None:
                self._destroy_window(win)
            return {"ok": True}
        except Exception as exc:
            logger.exception("关闭截图窗口失败")
            return {"ok": False, "msg": str(exc)}
