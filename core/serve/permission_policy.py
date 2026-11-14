"""serve 进程的权限策略。

没有在线客户端，或配置了自动拒绝时，立刻拒绝并只结算一次。
有客户端时先走原来的提示，满 10 分钟仍无人应答再拒绝。
桌面 GUI 不安装这份策略，权限确认继续无限等待。
"""

from __future__ import annotations

import logging
import os
import threading

from core.permission_broker import permission_broker

logger = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 600.0


def deny_choice_index(request) -> int:
    keys = list(request.option_keys)
    if "deny" in keys:
        return keys.index("deny")
    if "choice_1" in keys:
        return keys.index("choice_1")
    if len(request.options) > 1:
        return 1
    return 0


def settle_deny(request) -> bool:
    """拒绝当前请求。请求已经不在队里时返回 False，不会再次结算。"""
    if not permission_broker.is_active(request.request_id):
        return False
    return permission_broker.resolve(request.request_id, deny_choice_index(request))


def auto_deny_enabled() -> bool:
    if os.environ.get("YCODE_SERVE_AUTO_DENY", "").strip().lower() in {"1", "true", "yes"}:
        return True
    try:
        import config

        return bool(config._load().get(config.SERVE_AUTO_DENY_KEY, False))
    except Exception:
        return False


def install_serve_permission_policy(bridge, *, client_count, wait_seconds: float = DEFAULT_WAIT_SECONDS) -> None:
    original = bridge._activate_permission_request
    dismiss = bridge._dismiss_permission_prompt

    def activate(request):
        try:
            offline = int(client_count()) <= 0
        except Exception:
            logger.exception("读取在线客户端数量失败，权限请求按无人在线拒绝")
            offline = True
        if auto_deny_enabled() or offline:
            settle_deny(request)
            return
        try:
            original(request)
        except Exception:
            logger.exception("推送权限请求失败")

        def _on_timeout():
            try:
                if settle_deny(request):
                    logger.info("权限请求等待超时，已拒绝: %s", request.request_id)
            except Exception:
                logger.debug("权限超时拒绝失败", exc_info=True)

        timer = threading.Timer(wait_seconds, _on_timeout)
        timer.daemon = True
        timer.start()

    permission_broker.set_prompt_handlers(activate, dismiss)
