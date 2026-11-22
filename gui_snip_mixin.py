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

_MAX_B64_BYTES = 6 * 1024 * 1024
_PNG_MAX_EDGE = 1920
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
_FRAME_DATA_URL_BUDGET = _NAVIGATE_TO_STRING_LIMIT - _TEMPLATE_RESERVE


def _jpeg_data_url(image, quality: int) -> str:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def encode_frozen_frame(image) -> str:
    from PIL import Image

    source = image.convert("RGB")
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
    rgb = img.convert("RGB")
    width, height = rgb.size
    stride = ((width * 3 + 3) // 4) * 4
    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        24,
        0,
        stride * height,
        0, 0, 0, 0,
    )
    return header + rgb.tobytes("raw", "BGR", stride, -1)


def _set_clipboard_dib(dib: bytes, hwnd: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
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
            allocated = None
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
        try:
            import webview.platforms.winforms as wf

            uid = getattr(getattr(self, "_window", None), "uid", None) or "master"
            return wf.BrowserView.instances.get(uid)
        except Exception:
            logger.debug("获取主窗体失败", exc_info=True)
            return None

    def _invoke_gui(self, form, callback):
        if form is None:
            return False, None
        box: Dict[str, Any] = {}

        def _run() -> None:
            handle = getattr(form, "Handle", None)
            if handle is not None and hasattr(handle, "ToInt32") and handle.ToInt32() == 0:
                box["ready"] = False
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
                hwnd = int(form.Handle.ToInt32())
                if not hwnd:
                    return None
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

    def screen_snip_start(self) -> Dict[str, Any]:
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
                    return {"ok": False, "msg": "屏幕捕获尺寸异常，请重试"}

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

    def __init__(self, bridge: Any) -> None:
        self._bridge = bridge

    def _live_window(self):
        win = getattr(self._bridge, "_screen_snip_window", None)
        if win is None or win.events.closed.is_set():
            return None
        return win

    def _crop_selection(self, rect: Any) -> bytes:
        source = getattr(self._bridge, "_screen_snip_source", None)
        if source is None:
            raise RuntimeError("截图原图已释放，请重新截图")
        if not isinstance(rect, dict):
            raise ValueError("选区参数无效")
        return crop_frozen_frame(source, rect)

    def _destroy_window(self, win) -> None:
        if getattr(self._bridge, "_screen_snip_window", None) is win:
            self._bridge._screen_snip_window = None
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

    def snip_ready(self) -> Dict[str, Any]:
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
