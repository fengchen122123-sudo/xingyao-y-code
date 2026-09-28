"""GUI 右面板「画笔截图」桥接 Mixin。

前端进入截图模式前先调 capture_right_panel_region 把整块右面板（含原生
WebView2 浏览器表面，此刻仍可见）捕获为 PNG dataURL 冻结帧；随后前端隐藏
原生表面、把冻结帧铺成覆盖层供画笔圈选，裁剪在前端完成。后端只补两件
前端做不了的事：屏幕区域捕获与系统剪贴板/落盘写出。

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
import time
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
