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
from urllib.parse import urlparse

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
                    # 状态事件必须带归属会话：前端只认领「当前查看会话」的状态，
                    # 否则后台会话的导航/标题/加载态会污染用户正在看的另一个会话。
                    payload = dict(state or {})
                    payload["session_name"] = self._browser_pane_owner()
                    self.emit_event("embedded_browser", payload)
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

    @staticmethod
    def _is_tokenrhythm_url(url: str) -> bool:
        """判断是否为基元律动官方站点（含子域）。"""
        try:
            host = urlparse(url or "").hostname or ""
        except Exception:
            return False
        host = host.lower()
        return host == "tokenrhythm.studio" or host.endswith(".tokenrhythm.studio")

    def _inject_tokenrhythm_session(self, ctrl, url: str) -> None:
        """打开官方控制台前，把已登录会话 cookie 灌进内嵌 WebView2 免二次登录。

        失败绝不影响导航：这里只记 debug 日志，不抛异常、不改返回值。
        安全：绝不打印 cookie 的 name/value。
        """
        if ctrl is None or not self._is_tokenrhythm_url(url):
            return
        try:
            from core import tokenrhythm_auth

            cookies = tokenrhythm_auth.export_session_cookies()
            if cookies:
                ctrl.set_session_cookies(cookies, domain="tokenrhythm.studio")
        except Exception:
            # 免登录是增强项：任何失败都只记 debug，不干扰导航本身。
            logger.debug("注入基元律动会话 cookie 失败", exc_info=True)

    # ------------------------------------------------------- 会话归属（多会话）
    #
    # 原生 WebView2 控件全进程只有一个，因此必须明确「此刻归哪个会话」：
    # - 前台会话（UI 当前查看的会话）调用：立即作用到控件；
    # - 后台会话调用：只登记待加载 URL，并通知前端在该会话自己的右栏开页签，
    #   绝不移动/显示控件——否则浏览器会盖到用户正在看的另一个会话上；
    # - 用户切到该会话时，前端调用 browser_pane_set_active_session，桥层切换
    #   归属并回放 pending（embedded_browser_activation），前端就地 open + 导航。
    # 前端直接调用不带 session_name，一律按「当前查看会话」处理（与旧版一致）。

    def _browser_pane_session(self, session_name: str = "") -> str:
        """解析本次调用的归属会话：显式入参 > UI 当前查看会话。"""
        explicit = str(session_name or "").strip()
        if explicit:
            return explicit
        return str(getattr(self, "_current_session", "") or "")

    def _browser_pane_owner(self) -> str:
        """当前占用原生控件的会话名（状态事件的归属）。"""
        return str(getattr(self, "_browser_pane_owner_session", "") or "")

    def _browser_pane_pending(self) -> Dict[str, Dict[str, Any]]:
        """后台会话的待加载意图：session_name -> {'url': str}。"""
        pending = getattr(self, "_browser_pane_pending_intents", None)
        if not isinstance(pending, dict):
            pending = {}
            self._browser_pane_pending_intents = pending
        return pending

    def _browser_pane_is_foreground(self, session_name: str) -> bool:
        """是否就是 UI 当前查看的会话；空值按前台处理（兼容旧前端）。"""
        current = str(getattr(self, "_current_session", "") or "")
        target = str(session_name or "").strip()
        if not target or not current:
            return True
        return target == current

    def _browser_pane_defer(self, session_name: str, url: str) -> Dict[str, Any]:
        """后台会话：登记待加载 URL 并通知前端在它自己的右栏开页签。"""
        target = str(session_name or "").strip() or str(
            getattr(self, "_current_session", "") or ""
        )
        self._browser_pane_pending()[target] = {"url": str(url or "")}
        try:
            self.emit_event(
                "embedded_browser_assigned",
                {"session_name": target, "url": str(url or "")},
            )
        except Exception:
            logger.debug("推送内嵌浏览器归属事件失败", exc_info=True)
        return {"ok": True, "deferred": True, "session_name": target}

    # ---------------------------------------------------------------- 桥方法

    def browser_pane_open(
        self, rect: Dict[str, Any], url: str = "", zoom: float = 1.0, session_name: str = ""
    ) -> Dict[str, Any]:
        try:
            target = self._browser_pane_session(session_name)
            if not self._browser_pane_is_foreground(target):
                return self._browser_pane_defer(target, url)
            if not self._valid_rect(rect):
                return {"ok": False, "error": "invalid_rect"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            # 先记归属再 open：controller 的 on_state 回调据此给事件打 session；
            # open 失败时归属残留无副作用（下次成功 open 会覆盖）。
            self._browser_pane_owner_session = target
            self._inject_tokenrhythm_session(ctrl, url)
            return ctrl.open(rect, url, zoom)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_assign(self, url: str = "", session_name: str = "") -> Dict[str, Any]:
        """只登记「该会话要打开这个地址」，不创建/移动原生控件。

        给模型用的 open 入口：模型不可能知道窗口布局，rect 一律由前端在页签
        激活时测量上报；前台会话收到 assigned 后立即开页签并导航。
        """
        try:
            return self._browser_pane_defer(self._browser_pane_session(session_name), url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_set_active_session(self, session_name: str = "") -> Dict[str, Any]:
        """前端切会话时通知：把原生控件归属切到该会话并回放它的待加载 URL。"""
        try:
            target = str(session_name or "").strip() or str(
                getattr(self, "_current_session", "") or ""
            )
            self._browser_pane_owner_session = target
            item = self._browser_pane_pending().pop(target, None)
            if item and item.get("url"):
                try:
                    self.emit_event(
                        "embedded_browser_activation",
                        {"session_name": target, "url": str(item["url"])},
                    )
                except Exception:
                    logger.debug("推送内嵌浏览器激活事件失败", exc_info=True)
            elif item is None:
                # 目标会话没有待显示页面：收起仍挂着旧会话内容的原生控件。
                # 前端切会话时通常已随组件卸载 hide，这里只作跨端兜底。
                ctrl = getattr(self, "_embedded_browser", None)
                if ctrl is not None:
                    try:
                        ctrl.hide()
                    except Exception:
                        logger.debug("切换会话时收起内嵌浏览器失败", exc_info=True)
            return {"ok": True, "session_name": target, "activated": bool(item)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_set_bounds(
        self, rect: Dict[str, Any], zoom: float = 1.0, session_name: str = ""
    ) -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
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

    def browser_pane_navigate(self, url: str, session_name: str = "") -> Dict[str, Any]:
        try:
            target = self._browser_pane_session(session_name)
            if not self._browser_pane_is_foreground(target):
                # 后台会话导航只更新意图，切回该会话时再真正加载
                return self._browser_pane_defer(target, url)
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            self._inject_tokenrhythm_session(ctrl, url)
            return ctrl.navigate(url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_back(self, session_name: str = "") -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.back()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_forward(self, session_name: str = "") -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.forward()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_reload(self, session_name: str = "") -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.reload()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_stop(self, session_name: str = "") -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            return ctrl.stop()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_hide(self, session_name: str = "") -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
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

    def browser_pane_get_state(self, session_name: str = "") -> Dict[str, Any]:
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
