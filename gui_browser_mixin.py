"""GUI 内嵌浏览器桥接 Mixin。

把前端通过 pywebview js_api 上报的矩形/导航指令转发给 core.embedded_browser
的控制器，并把控制器状态通过 emit_event('embedded_browser', ...) 回推前端。

顶层只 import 轻量模块；控制器惰性单例创建，非 Windows 或创建失败时
所有桥方法统一返回 {'ok': False, 'error': 'unsupported_platform'}。
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RECT_KEYS = ("x", "y", "width", "height")

# 实例级控制器创建锁的模块级 guard：锁本身的创建也要防竞态——否则并发首调
# 可能各持一把锁、双检被绕过、各建一个控制器（后建者覆盖缓存引用，先建的
# 原生控件失去引用残留主窗体）。该 guard 只用于创建实例锁，绝不嵌套持有。
_LOCK_CREATION_GUARD = threading.Lock()


class BrowserBridgeMixin:
    """为 ApiBridge 提供 browser_pane_* 系列桥方法。"""

    def _browser_controller(self) -> Optional[object]:
        """惰性单例：只在 Windows 上创建一次内嵌浏览器控制器。

        js_api 方法由 pywebview 在各自线程派发，并发进入必须串行化创建：
        未命中缓存时取实例锁、锁内复查（双检）再创建——否则两个线程会各建
        一个控制器，后建者覆盖 self._embedded_browser，先建的原生 WebView2
        控件失去引用且残留在主窗体上（泄漏/重叠）。锁只覆盖创建段（含
        ctrl.shutdown 的包装赋值）；_release_browser_controller 自身不取该
        锁，不存在重入死锁。
        """
        ctrl = getattr(self, "_embedded_browser", None)
        if ctrl is not None:
            return ctrl
        if sys.platform != "win32":
            return None
        lock = getattr(self, "_embedded_browser_lock", None)
        if lock is None:
            # 锁的创建也要防竞态：否则并发首调可能各持一把锁、双检被绕过。
            with _LOCK_CREATION_GUARD:
                lock = getattr(self, "_embedded_browser_lock", None)
                if lock is None:
                    lock = threading.Lock()
                    try:
                        self._embedded_browser_lock = lock
                    except Exception:
                        # 宿主实例禁止写属性（非常规）：退回模块级 guard 当创建
                        # 锁——同样串行化创建，且无嵌套持锁、不引入死锁。
                        lock = _LOCK_CREATION_GUARD
        with lock:
            ctrl = getattr(self, "_embedded_browser", None)  # 双检：等锁期间可能已创建
            if ctrl is not None:
                return ctrl
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
            try:
                # 释放路径挂接：ApiBridge.shutdown 只持有控制器引用直接调
                # ctrl.shutdown()，控件 Dispose 后引用若留在实例上，后续
                # set_bounds/get_state 会打到已销毁控件。把"清引用"绑到释放动作上，
                # 释放后各桥方法与"从未打开"语义一致（实例级包装，不改 API 表面）。
                release = ctrl.shutdown  # 先取原始绑定方法，避免包装后自递归
                ctrl.shutdown = lambda: self._release_browser_controller(ctrl, release)
            except Exception:
                logger.debug("挂接内嵌浏览器释放钩子失败", exc_info=True)
            return ctrl

    def _release_browser_controller(self, ctrl, shutdown) -> Dict[str, Any]:
        """执行控制器释放，并按释放结果决定是否清掉桥层缓存引用。

        返回语义保持不变（控制器原始的 {'ok': ...} 结构）。仅在释放成功
        （shutdown 返回 ok 真值）时清引用：此后 browser_pane_set_bounds 返回
        not_open、get_state 返回 state=None、hide 返回 ok——与"未打开过"完全
        一致，不再触碰已 Dispose 的控件。释放失败（返回 ok: False 或抛异常）
        时保留引用并记 warning：Dispose 可能未执行、残留控件仍挂在主窗体上，
        清引用会让它失去唯一引用，下次 open 还会在原控件之上再叠加一个新控件。
        """
        try:
            result = shutdown()
        except Exception as exc:
            logger.exception("释放内嵌浏览器控制器失败")
            logger.warning(
                "释放失败，保留控制器引用（残留控件仍挂主窗体，避免下次 open 叠加新控件）"
            )
            return {"ok": False, "error": str(exc)}
        if isinstance(result, dict) and result.get("ok"):
            if getattr(self, "_embedded_browser", None) is ctrl:
                self._embedded_browser = None
        else:
            logger.warning(
                "释放失败（%s），保留控制器引用（残留控件仍挂主窗体，避免下次 open 叠加新控件）",
                result,
            )
        return result

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

    def browser_pane_open(self, rect: Dict[str, Any], url: str = "", zoom: float = 1.0) -> Dict[str, Any]:
        try:
            if not self._valid_rect(rect):
                return {"ok": False, "error": "invalid_rect"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.open(rect, url, zoom)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_set_bounds(self, rect: Dict[str, Any], zoom: float = 1.0) -> Dict[str, Any]:
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
            return ctrl.set_bounds(rect, zoom)
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

    def write_preview_page(self, html_text: str, source_path: str = "") -> Dict[str, Any]:
        """把前端渲染好的预览 HTML 落盘，返回可交给内嵌浏览器打开的本地路径。

        用途：右栏浏览器要按阅读样式展示 .md（浏览器直接打开 md 只显示源码
        文本）。前端用与消息气泡同一套渲染器生成完整 HTML 后交这里落盘，
        再走 browser_pane_navigate 打开该文件。与平台无关（不碰 WebView2），
        非 Windows 也能用。
        """
        try:
            from core.web_preview import write_preview_page as _write_preview_page

            path = _write_preview_page(html_text, source_path)
            return {"ok": True, "path": str(path)}
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

    # ------------------------------------------------ 内置浏览器自动化通道（JS 面入口：browser_pane_automation_endpoint）

    def embedded_browser_automation_endpoint(self, session_name: str = "") -> str:
        """内置浏览器自动化入口——仅供内部调用（computer controller 的 embedded 分支）。

        注意：本方法按链路"无降级"设计抛 ComputerControlError（gui_main 将其
        以属性方式注入 controller 作为 endpoint provider，异常由 controller
        归一为启动失败），虽经 js_api 暴露，但**不供前端/JS 直接调用**——JS
        侧应调用 browser_pane_automation_endpoint（返回 dict，不抛穿 js_api）。

        确保内嵌 WebView2 控件带 CDP 调试端口存在（幂等，必要时带端口重建），
        并通知前端把面板切到浏览器页签、按当前项目分辨率做适应窗口呈现——
        用户实时围观星瑶操作。session_name 为发起操控的会话名，随事件透传，
        前端据此把浏览器页签开进发起会话的右面板（其他会话不受打扰）。
        返回回环 endpoint；任何失败都抛 ComputerControlError（无降级：
        绝不静默改走外部浏览器）。
        """
        from core.computer.models import ComputerControlError
        from core.computer.settings import get_project_browser_viewport
        from core.workspace_context import get_workdir

        ctrl = self._browser_controller()
        if ctrl is None:
            raise ComputerControlError(
                "computer_embedded_unavailable",
                "内置浏览器仅在 Windows 桌面端 GUI 中可用；"
                "请在设置中把浏览器切换为 Edge 或 Chrome",
            )
        result = ctrl.prepare_automation()
        if not result.get("ok"):
            raise ComputerControlError(
                "computer_embedded_unavailable",
                f"内置浏览器自动化准备失败：{result.get('error')}",
            )
        try:
            width, height = get_project_browser_viewport(get_workdir())
            self.emit_event(
                "embedded_browser_automation",
                {
                    "width": width,
                    "height": height,
                    "session_name": str(session_name or ""),
                },
            )
        except Exception:
            # 面板呈现失败不阻断自动化链路：CDP 连接与截图不依赖前端状态，
            # 仅影响"围观"体验。
            logger.debug("推送内置浏览器自动化面板事件失败", exc_info=True)
        return str(result["endpoint"])

    def browser_pane_automation_endpoint(self, session_name: str = "") -> Dict[str, Any]:
        """browser_pane_* 桥面的自动化入口：包装 embedded_browser_automation_endpoint，
        把 ComputerControlError 归一为结构化 dict，绝不抛穿 js_api。

        取舍（为何不改原方法而加包装）：原方法的"抛 ComputerControlError"
        异常语义被 computer controller 的 embedded 分支依赖（错误码随异常
        传播、决定启动失败文案），且 gui_main 直接以属性方式注入该 provider、
        既有测试按字符串/异常断言——改动原方法破坏面大。故新增本包装供
        前端/JS 面调用：内部实现与所有既有调用方零改动。

        成功：{'ok': True, 'endpoint': 'http://127.0.0.1:<port>'}；
        失败：{'ok': False, 'error': <稳定错误码或异常文本>}，领域错误额外带
        'message'（中文说明）。
        """
        try:
            endpoint = self.embedded_browser_automation_endpoint(session_name)
        except Exception as exc:
            code = getattr(exc, "code", "")
            if isinstance(code, str) and code:
                return {"ok": False, "error": code, "message": str(exc)}
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "endpoint": endpoint}
