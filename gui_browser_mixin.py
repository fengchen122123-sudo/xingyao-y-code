from __future__ import annotations

import inspect
import logging
import sys
import threading
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_RECT_KEYS = ("x", "y", "width", "height")

# 「这个会话还没有属于自己的页面」时代替显示的空白地址：原生控件里若还停着
# 上一个会话的网页，宁可先换成一页白纸，也不能让它在别的会话的面板里出现。
_BLANK_PAGE = "about:blank"

# 实例级控制器创建锁的模块级 guard：锁本身的创建也要防竞态——否则并发首调
# 可能各持一把锁、双检被绕过、各建一个控制器（后建者覆盖缓存引用，先建的
# 原生控件失去引用残留主窗体）。该 guard 只用于创建实例锁，绝不嵌套持有。
_LOCK_CREATION_GUARD = threading.Lock()


class BrowserBridgeMixin:
    """为 ApiBridge 提供 browser_pane_* 系列桥方法。"""

    def _browser_controller(self) -> Optional[object]:
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
            ctrl = getattr(self, "_embedded_browser", None)
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
                    content = self._browser_pane_content_session()
                    if content:
                        # 快照描述的是「控件里这一页」，归属必须是这一页的会话，
                        # 而不是发射这一刻的控件占用者：状态推送是 150ms 节流的
                        # 尾随补发，切会话那一瞬间到达的旧快照若按占用者打标，
                        # A 会话的页面地址会被当成 B 会话的状态写进 B 的界面。
                        url = str(payload.get("url") or "")
                        if url:
                            self._browser_pane_urls()[content] = url
                        payload["session_name"] = content
                    else:
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
                release = ctrl.shutdown
                ctrl.shutdown = lambda: self._release_browser_controller(ctrl, release)
            except Exception:
                logger.debug("挂接内嵌浏览器释放钩子失败", exc_info=True)
            return ctrl

    def _release_browser_controller(self, ctrl, shutdown) -> Dict[str, Any]:
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
            self._browser_pane_mark_content("")
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
        if sys.platform != "win32":
            return None, {"ok": False, "error": "unsupported_platform"}
        ctrl = self._browser_controller()
        if ctrl is None:
            return None, {"ok": False, "error": "unsupported_platform"}
        return ctrl, None

    @staticmethod
    def _is_tokenrhythm_url(url: str) -> bool:
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

    def _browser_pane_session(self, session_name: str = "") -> str:
        explicit = str(session_name or "").strip()
        if explicit:
            return explicit
        return str(getattr(self, "_current_session", "") or "")

    def _browser_pane_owner(self) -> str:
        return str(getattr(self, "_browser_pane_owner_session", "") or "")

    def _browser_pane_pending(self) -> Dict[str, Dict[str, Any]]:
        pending = getattr(self, "_browser_pane_pending_intents", None)
        if not isinstance(pending, dict):
            pending = {}
            self._browser_pane_pending_intents = pending
        return pending

    def _browser_pane_urls(self) -> Dict[str, str]:
        """每个会话最近一次加载的地址（会话级页面记账）。

        原生控件全进程唯一，同一时刻只能停一页，所以「页面内容属于谁」不能由
        「谁在占用控件」推断：切回某会话时它该看到自己上次那一页（或什么都没有），
        绝不能是别的会话留在控件里的页面。这张表就是那个依据。
        """
        urls = getattr(self, "_browser_pane_url_memory", None)
        if not isinstance(urls, dict):
            urls = {}
            self._browser_pane_url_memory = urls
        return urls

    def _browser_pane_content_session(self) -> str:
        return str(getattr(self, "_browser_pane_content_owner", "") or "")

    def _browser_pane_mark_content(self, session_name: str) -> None:
        self._browser_pane_content_owner = str(session_name or "")

    def _browser_pane_is_foreground(self, session_name: str) -> bool:
        """是否就是 UI 当前查看的会话；空值按前台处理（兼容旧前端）。"""
        current = str(getattr(self, "_current_session", "") or "")
        target = str(session_name or "").strip()
        if not target or not current:
            return True
        return target == current

    def _browser_pane_defer(self, session_name: str, url: str) -> Dict[str, Any]:
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

    @staticmethod
    def _accepts_surface_generation(method: Any) -> bool:
        try:
            sig = inspect.signature(method)
        except (TypeError, ValueError):
            return False
        if "surface_generation" in sig.parameters:
            return True
        return any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in sig.parameters.values()
        )

    def _call_surface_op(
        self,
        method: Any,
        *args: Any,
        surface_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        if surface_generation is not None and self._accepts_surface_generation(method):
            return method(*args, surface_generation=surface_generation)
        return method(*args)

    def browser_pane_open(
        self,
        rect: Dict[str, Any],
        url: str = "",
        zoom: float = 1.0,
        session_name: str = "",
        surface_generation: Optional[int] = None,
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
            self._browser_pane_owner_session = target
            page = str(url or "").strip()
            if not page:
                # 空 url = 前端只要求「把原生表面摆到这块矩形上」；该显示哪一页
                # 由会话级页面记账决定。控件里若还停着**别的会话**的页面，绝不
                # 能就这么显示出来（那正是「A 会话打开的内容出现在 B 会话」的
                # 最后一环）：优先恢复本会话自己上次的地址，没有就退到空白页。
                content = self._browser_pane_content_session()
                if content and content != target:
                    page = self._browser_pane_urls().get(target, "") or _BLANK_PAGE
            self._inject_tokenrhythm_session(ctrl, page)
            result = self._call_surface_op(
                ctrl.open, rect, page, zoom, surface_generation=surface_generation
            )
            if isinstance(result, dict) and result.get("ok") and page:
                self._browser_pane_mark_content(target)
                if page != _BLANK_PAGE:
                    self._browser_pane_urls()[target] = page
            return result
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_assign(self, url: str = "", session_name: str = "") -> Dict[str, Any]:
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
            prev_owner = self._browser_pane_owner()
            content = self._browser_pane_content_session()
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
                # 前端切会话时通常已随组件卸载 hide，这里只作跨端兜底；
                # 若当前控件已由目标会话在前台正常打开且内容归属目标会话，避免竞态误收起。
                ctrl = getattr(self, "_embedded_browser", None)
                already_showing_target = (
                    prev_owner == target
                    and content == target
                    and bool(getattr(ctrl, "_visible", False))
                )
                if ctrl is not None and not already_showing_target:
                    try:
                        ctrl.hide()
                    except Exception:
                        logger.debug("切换会话时收起内嵌浏览器失败", exc_info=True)
            return {"ok": True, "session_name": target, "activated": bool(item)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_set_bounds(
        self,
        rect: Dict[str, Any],
        zoom: float = 1.0,
        session_name: str = "",
        surface_generation: Optional[int] = None,
    ) -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            if not self._valid_rect(rect):
                return {"ok": False, "error": "invalid_rect"}
            if sys.platform != "win32":
                return {"ok": False, "error": "unsupported_platform"}
            ctrl = getattr(self, "_embedded_browser", None)
            if ctrl is None:
                return {"ok": False, "error": "not_open"}
            return self._call_surface_op(
                ctrl.set_bounds, rect, zoom, surface_generation=surface_generation
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def browser_pane_navigate(self, url: str, session_name: str = "") -> Dict[str, Any]:
        try:
            target = self._browser_pane_session(session_name)
            if not self._browser_pane_is_foreground(target):
                return self._browser_pane_defer(target, url)
            ctrl, error = self._use_controller()
            if error is not None:
                return error
            self._inject_tokenrhythm_session(ctrl, url)
            result = ctrl.navigate(url)
            if isinstance(result, dict) and result.get("ok") and not result.get("deferred"):
                self._browser_pane_owner_session = target
                self._browser_pane_mark_content(target)
                page = str(url or "").strip()
                if page:
                    self._browser_pane_urls()[target] = page
            return result
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

    def browser_pane_hide(
        self, session_name: str = "", surface_generation: Optional[int] = None
    ) -> Dict[str, Any]:
        try:
            if not self._browser_pane_is_foreground(
                self._browser_pane_session(session_name)
            ):
                return {"ok": False, "error": "not_visible"}
            ctrl = getattr(self, "_embedded_browser", None)
            if ctrl is None:
                return {"ok": True}
            return self._call_surface_op(
                ctrl.hide, surface_generation=surface_generation
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def write_preview_page(self, html_text: str, source_path: str = "") -> Dict[str, Any]:
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
            result = ctrl.get_state()
            if not isinstance(result, dict) or not result.get("ok"):
                return result
            state = result.get("state")
            if not isinstance(state, dict):
                return result
            # 校准口径与状态事件一致：快照描述的是「控件里这一页」，只能交给
            # 这一页所属的会话。别的会话来问（面板挂载/桥就绪时 calibrate）
            # 一律给 state=None，绝不把 A 会话的地址当成 B 会话的状态写进界面。
            content = self._browser_pane_content_session()
            target = self._browser_pane_session(session_name)
            if content and target and content != target:
                return {"ok": True, "state": None}
            snapshot = dict(state)
            snapshot["session_name"] = content or target
            return {"ok": True, "state": snapshot}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _confirm_embedded_browser_recreate(
        self, session_name: str, current_url: str
    ) -> bool:
        from core.permission_broker import CANCELLED_RESULT, PermissionOption
        import core.permission_broker as pb_mod

        broker = pb_mod.permission_broker
        if not broker.has_prompt_handlers():
            return False
        owner_id = str(session_name or self._current_session_safe())
        req = broker.enqueue(
            function_name="embedded_browser_recreate",
            arguments={"url": str(current_url or "")[:240]},
            owner_id=owner_id,
            parent_turn_id=owner_id,
            source="embedded_browser",
            options=(
                PermissionOption("allow_once", "刷新当前页面并接管自动化"),
                PermissionOption("deny", "取消（保留当前页面）"),
            ),
        )
        try:
            choice = req.future.result(timeout=120.0)
        except Exception:
            broker.cancel_owner(owner_id)
            return False
        return choice != CANCELLED_RESULT and choice == 0

    def _current_session_safe(self) -> str:
        return str(getattr(self, "_current_session", "") or "")

    def embedded_browser_automation_endpoint(self, session_name: str = "") -> str:
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
        prepare_fn = ctrl.prepare_automation
        try:
            sig = inspect.signature(prepare_fn)
            accepts_confirm = (
                "confirm_recreate" in sig.parameters
                or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
            )
        except (TypeError, ValueError):
            accepts_confirm = False
        if accepts_confirm:
            result = prepare_fn(
                confirm_recreate=lambda url: self._confirm_embedded_browser_recreate(
                    session_name, url
                )
            )
        else:
            result = prepare_fn()
        if not result.get("ok"):
            if result.get("error") == "recreate_confirmation_required":
                raise ComputerControlError(
                    "computer_embedded_recreate_required",
                    str(result.get("msg") or "需要用户确认刷新内置浏览器后再接管自动化"),
                )
            raise ComputerControlError(
                "computer_embedded_unavailable",
                f"内置浏览器自动化准备失败：{result.get('error')}",
            )
        if session_name:
            # 自动化接下来驱动的就是这一页：把控件里的页面记到发起会话名下。
            # 否则前端把面板摆过来（open 空 url）时会认为「控件里是别人的页面」
            # 而换成空白页，正在被操控的页面就被清掉了。
            self._browser_pane_mark_content(str(session_name))
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
            logger.debug("推送内置浏览器自动化面板事件失败", exc_info=True)
        return str(result["endpoint"])

    def browser_pane_automation_endpoint(self, session_name: str = "") -> Dict[str, Any]:
        try:
            endpoint = self.embedded_browser_automation_endpoint(session_name)
        except Exception as exc:
            code = getattr(exc, "code", "")
            if isinstance(code, str) and code:
                return {"ok": False, "error": code, "message": str(exc)}
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "endpoint": endpoint}
