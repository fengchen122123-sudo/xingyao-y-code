"""GUI 内嵌浏览器桥接 Mixin。

把前端通过 pywebview js_api 上报的矩形/导航指令转发给 core.embedded_browser
的控制器，并把控制器状态通过 emit_event('embedded_browser', ...) 回推前端。

顶层只 import 轻量模块；控制器惰性单例创建，非 Windows 或创建失败时
所有桥方法统一返回 {'ok': False, 'error': 'unsupported_platform'}。
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RECT_KEYS = ("x", "y", "width", "height")


class BrowserBridgeMixin:
    """为 ApiBridge 提供 browser_pane_* 系列桥方法。"""

    def _browser_controller(self) -> Optional[object]:
        """惰性单例：只在 Windows 上创建一次内嵌浏览器控制器。"""
        ctrl = getattr(self, "_embedded_browser", None)
        if ctrl is not None:
            return ctrl
        if sys.platform != "win32":
            return None
        try:
            from core.embedded_browser import EmbeddedBrowserController
        except Exception:
            logger.warning("内嵌浏览器模块加载失败", exc_info=True)
            return None

        def _on_state(state: Dict[str, Any]) -> None:
            try:
                self.emit_event("embedded_browser", state)
            except Exception:
                logger.debug("推送内嵌浏览器状态失败", exc_info=True)

        def _get_form():
            try:
                import webview.platforms.winforms as wf

                uid = getattr(getattr(self, "_window", None), "uid", None) or "master"
                return wf.BrowserView.instances.get(uid)
            except Exception:
                logger.debug("获取主窗体失败", exc_info=True)
                return None

        try:
            ctrl = EmbeddedBrowserController(on_state=_on_state, get_form=_get_form)
        except Exception:
            logger.warning("内嵌浏览器控制器创建失败", exc_info=True)
            return None
        try:
            self._embedded_browser = ctrl
        except Exception:
            pass
        return ctrl

    @staticmethod
    def _valid_rect(rect: Any) -> bool:
        if not isinstance(rect, dict):
            return False
        return all(key in rect for key in _RECT_KEYS)

    def _use_controller(self):
        """返回 (controller, error_dict) 之一。"""
        if sys.platform != "win32":
            return None, {"ok": False, "error": "unsupported_platform"}
        ctrl = self._browser_controller()
        if ctrl is None:
            return None, {"ok": False, "error": "unsupported_platform"}
        return ctrl, None

    # ---------------------------------------------------------------- 桥方法

    def browser_pane_open(self, rect: Dict[str, Any], url: str = "") -> Dict[str, Any]:
        try:
            if not self._valid_rect(rect):
                return {"ok": False, "error": "invalid_rect"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.open(rect, url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_set_bounds(self, rect: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not self._valid_rect(rect):
                return {"ok": False, "error": "invalid_rect"}
            # 平台守卫与其余桥方法保持一致（评审 S5）；控制器不存在时
            # 返回 not_open 而非惰性创建——set_bounds 是高频心跳调用，
            # 不应成为创建入口
            if sys.platform != "win32":
                return {"ok": False, "error": "unsupported_platform"}
            ctrl = getattr(self, "_embedded_browser", None)
            if ctrl is None:
                return {"ok": False, "error": "not_open"}
            return ctrl.set_bounds(rect)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_navigate(self, url: str) -> Dict[str, Any]:
        try:
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.navigate(url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_back(self) -> Dict[str, Any]:
        try:
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.back()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_forward(self) -> Dict[str, Any]:
        try:
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.forward()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_reload(self) -> Dict[str, Any]:
        try:
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.reload()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_stop(self) -> Dict[str, Any]:
        try:
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.stop()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_hide(self) -> Dict[str, Any]:
        try:
            ctrl = getattr(self, "_embedded_browser", None)
            if ctrl is None:
                return {"ok": True}
            return ctrl.hide()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_get_state(self) -> Dict[str, Any]:
        try:
            ctrl = getattr(self, "_embedded_browser", None)
            if ctrl is None:
                return {"ok": True, "state": None}
            return ctrl.get_state()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}