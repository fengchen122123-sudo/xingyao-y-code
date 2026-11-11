"""内置 WebView2（GUI 右侧面板浏览器）的 CDP 直连 backend。

与外部浏览器 backend 的本质区别：浏览器进程归 GUI 所有，本 backend 只通过
Chrome DevTools Protocol「连接」它（playwright chromium.connect_over_cdp），
绝不启动、绝不关闭任何浏览器进程。用户在右侧面板实时围观星瑶的操作。

线程归属铁律与父类一致（browser_backend 模块 docstring）：构造函数不导入
也不启动 Playwright；全部 Playwright 对象只属于 worker 线程，外部调用经
有界命令队列 marshal。本 backend 固定使用线程模式——CDP 连接的生命周期
天然在 GUI 进程内，无需再派生受管子进程。

close()/teardown 只断开 CDP 连接（面板保活）；视口坐标系 = 项目分辨率
（逻辑像素，由 controller 冻结进 identity），面板以「适应窗口缩放」呈现
同一逻辑视口。高 DPI 下截图是物理像素，由 _normalize_frame 重采样回 CSS
视口尺寸，保证「模型看到的图片坐标 = 动作坐标」。
"""

from __future__ import annotations

import logging
import tempfile
import time
from pathlib import Path

from core.computer.browser_backend import BrowserComputerBackend
from core.computer.models import ComputerControlError

_LOGGER = logging.getLogger(__name__)
_CDP_READY_POLL_INTERVAL = 0.25
# CDP endpoint 必须是回环地址 + 1-65535 的合法端口
_CDP_ENDPOINT_PREFIX = "http://127.0.0.1:"
# 连接失败报错中 last_error 摘要的截断长度：CDP/Playwright 错误文本可能很长
_ERROR_SUMMARY_MAX_CHARS = 300


def _parse_cdp_port(endpoint: object) -> int | None:
    """解析 "http://127.0.0.1:<port>[/...]" 中的端口；非回环或端口非法返回 None。"""
    if type(endpoint) is not str or not endpoint.startswith(_CDP_ENDPOINT_PREFIX):
        return None
    port_text = endpoint[len(_CDP_ENDPOINT_PREFIX):].split("/", 1)[0]
    if not (port_text.isascii() and port_text.isdigit()):
        return None
    port = int(port_text)
    return port if 1 <= port <= 65535 else None


class EmbeddedBrowserBackend(BrowserComputerBackend):
    """CDP 直连内置 WebView2；协议与 action 集与外部浏览器完全一致。"""

    def __init__(
        self,
        cdp_endpoint: str,
        profile_dir: Path,
        *,
        viewport: tuple[int, int] = (1440, 900),
        command_timeout: float = 30.0,
        on_page_change=None,
        playwright_factory=None,
    ) -> None:
        if _parse_cdp_port(cdp_endpoint) is None:
            raise ComputerControlError(
                "computer_invalid_config",
                "内置浏览器 CDP endpoint 必须是 127.0.0.1 回环地址且端口为 1-65535 的整数",
            )
        super().__init__(
            "embedded",
            profile_dir,
            playwright_factory=playwright_factory,
            command_timeout=command_timeout,
            on_page_change=on_page_change,
            use_process_worker=False,
            viewport=viewport,
        )
        self._cdp_endpoint = cdp_endpoint
        # 浏览器归 GUI 所有：teardown 绝不关闭 context（只断开连接）。
        self._owns_context = False

    @property
    def cdp_endpoint(self) -> str:
        return self._cdp_endpoint

    # ---------------- worker 线程：CDP 启动分支 ----------------

    def _boot(self) -> None:
        """CDP 直连启动：不创建 profile 目录、不持有 profile 锁、不解析
        浏览器 PID（浏览器归 GUI 所有，窗口标题提示退化为 DOM 可见性裁决）。"""
        factory = self._playwright_factory
        if factory is None:
            # 惰性导入：只有真实启动时才触碰 playwright，且发生在 worker 线程。
            # 安装包已不再自带 playwright（见 ycode.spec）：先确保按需组件就绪，
            # 再导入；组件缺失时转成电脑操控领域错误码，提示用户去设置页下载。
            from core.components import ComponentError, ensure_playwright

            try:
                ensure_playwright()
            except ComponentError as error:
                raise ComputerControlError(
                    "playwright_component_missing", str(error)
                ) from error
            from playwright.sync_api import sync_playwright as factory
        try:
            self._runtime = factory()
            self._playwright = self._runtime.start()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_backend_start_failed",
                f"Playwright runtime 启动失败：{type(error).__name__}",
            ) from error
        browser = self._connect_with_retry()
        contexts = list(browser.contexts)
        if not contexts:
            raise ComputerControlError(
                "computer_embedded_unavailable",
                "内置浏览器未暴露可用的浏览器上下文，无法自动化",
            )
        self._context = contexts[0]
        self._screenshot_dir = Path(tempfile.mkdtemp(prefix="ycode-computer-embedded-"))
        context = self._context
        pages = [page for page in context.pages if not page.is_closed()]
        # 内置浏览器通常已有用户正在看的页面：附着到最新一页继续操作，
        # 绝不新开标签页、绝不导航去 about:blank 打断用户。
        self._active_page = pages[-1] if pages else context.new_page()
        # 初始 page 落定后再挂事件，避免启动过程误报页面变化。
        context.on("page", self._on_new_page)
        context.on("close", self._on_context_close)
        self._browser_pid = None

    def _connect_with_retry(self):
        """有界轮询 CDP 端口就绪（控件刚创建时内核异步初始化需要片刻）。"""
        deadline = time.monotonic() + self._start_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._worker_stop.is_set():
                raise ComputerControlError(
                    "computer_browser_closed", "内置浏览器连接已被取消"
                )
            try:
                return self._playwright.chromium.connect_over_cdp(self._cdp_endpoint)
            except Exception as error:
                last_error = error
                time.sleep(_CDP_READY_POLL_INTERVAL)
        if last_error is None:
            detail = "未知错误"
        else:
            summary = " ".join(str(last_error).split())
            if len(summary) > _ERROR_SUMMARY_MAX_CHARS:
                summary = summary[: _ERROR_SUMMARY_MAX_CHARS] + "…"
            detail = (
                f"{type(last_error).__name__}: {summary}"
                if summary
                else type(last_error).__name__
            )
        raise ComputerControlError(
            "computer_embedded_unavailable",
            f"无法通过 CDP 连接内置浏览器（{self._cdp_endpoint}）：{detail}",
        )

    def _observation_viewport(self, page) -> dict:
        """坐标空间 = 页面真实 CSS 视口（innerWidth/innerHeight，即项目分辨
        率经面板缩放后的逻辑值）。

        E4（2026-11-02）：高 DPI 下 Playwright 截图是 视口 × devicePixelRatio
        的物理像素，而 page.mouse 的坐标是 CSS 像素。旧实现把 PNG 原样发给
        模型、却仍按 CSS 报 width/height，模型照图片给的坐标会被当成 CSS
        坐标点击（1.25 倍缩放下越往右下偏得越多）。现在由 _normalize_frame
        把截图重采样到 CSS 视口尺寸，因此 original 与 width 同值、scale=1：
        图片坐标 = 动作坐标，三方一致。
        """
        try:
            metrics = page.evaluate(
                "() => ({ w: window.innerWidth, h: window.innerHeight,"
                " dpr: window.devicePixelRatio || 1 })"
            )
            width = max(1, int(metrics.get("w") or 0))
            height = max(1, int(metrics.get("h") or 0))
        except Exception:
            return super()._observation_viewport(page)
        return {
            "width": width,
            "height": height,
            "original_width": width,
            "original_height": height,
            "origin_x": 0,
            "origin_y": 0,
            "scale_x": 1.0,
            "scale_y": 1.0,
        }

    def _normalize_frame(self, png_bytes: bytes, page) -> bytes:
        """把截图重采样到 CSS 视口尺寸（见 _observation_viewport 的说明）。

        尽力而为：Pillow 不可用、解码失败或尺寸本来就一致时原样返回——绝不
        因为重采样失败让整条观察链报错；此时只是退回到旧的高 DPI 坐标风险。
        """
        try:
            import io

            from PIL import Image
        except Exception:
            return png_bytes
        try:
            viewport = self._observation_viewport(page)
            width = int(viewport["width"])
            height = int(viewport["height"])
            with Image.open(io.BytesIO(png_bytes)) as image:
                if image.size == (width, height):
                    return png_bytes
                resized = image.convert("RGB").resize(
                    (width, height), getattr(Image, "LANCZOS", 1)
                )
                buffer = io.BytesIO()
                resized.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue()
        except Exception:
            _LOGGER.exception("内置浏览器截图重采样失败，按原图继续观察")
            return png_bytes
