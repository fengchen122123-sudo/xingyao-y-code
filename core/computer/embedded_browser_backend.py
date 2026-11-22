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
_ERROR_SUMMARY_MAX_CHARS = 300


def _parse_cdp_port(endpoint: object) -> int | None:
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

    def _boot(self) -> None:
        factory = self._playwright_factory
        if factory is None:
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
                if self._worker_stop.wait(_CDP_READY_POLL_INTERVAL):
                    raise ComputerControlError(
                        "computer_browser_closed", "内置浏览器连接已被取消"
                    ) from error
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
