"""云中转公网入口：把桌面二维码从局域网地址换成网关公网地址。

云中转是纯消息通道（桌面主动外连 ``/relay/device``），既不是 HTTP 反代也没有
端口映射，所以公网页面必须由网关自己托管（``/desktop/<device_id>`` 或
``/m/<device_id>``）。手机要打开这些页面，需要的是一张网关签发的只读 remote
JWT；桌面手里只有长期 device token，因此先拿它去换票。

任何一步不可用（未开启、未连上、换票失败、网关没挂移动端页面）都返回 None，
调用方回落局域网二维码——这个模块绝不把异常抛给界面。
"""

from __future__ import annotations

import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

# 换票与页面探测都在用户点「二维码」的同步路径上，超时必须短。
_TIMEOUT_SECONDS = 6.0
_RELAY_SUFFIX = "/relay/device"
# 换票缓存：device_id -> 网关签发的票据；剩余有效期低于余量就重新换一张。
_TICKET_CACHE: dict[str, dict] = {}
_CACHE_MARGIN_SECONDS = 300


def site_base(relay_url: str) -> str:
    """把设备拨号地址换算成站点根。

    ``wss://example.com/xy/bot/relay/device`` → ``https://example.com/xy/bot``。
    反代到子路径时前缀必须保留，否则页面和 ws 端点都会打偏。
    """
    text = str(relay_url or "").strip()
    if not text:
        return ""
    if text.startswith("wss://"):
        text = "https://" + text[len("wss://"):]
    elif text.startswith("ws://"):
        text = "http://" + text[len("ws://"):]
    else:
        return ""
    text = text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    if text.endswith(_RELAY_SUFFIX):
        text = text[: -len(_RELAY_SUFFIX)]
    return text.rstrip("/")


def device_token() -> str:
    """读出并解密云中转设备令牌；拿不到就是空串（调用方据此放弃公网分支）。"""
    try:
        raw = str(config._load().get(config.CLOUD_RELAY_TOKEN_KEY) or "")
    except Exception:
        logger.debug("读取云中转配置失败", exc_info=True)
        return ""
    if not raw:
        return ""
    try:
        from core.credentials import unprotect_secret

        return unprotect_secret(raw).strip()
    except Exception:
        logger.exception("云中转设备令牌解密失败")
        return ""


def request_ticket(base: str, device_id: str, token: str) -> dict | None:
    """用设备令牌换取只读 remote JWT；失败返回 None。"""
    if not base or not device_id or not token:
        return None
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{base}/api/relay/device-token",
                json={"device_id": device_id, "device_token": token},
            )
    except Exception:
        logger.warning("云中转换票请求失败", exc_info=True)
        return None
    if resp.status_code != 200:
        logger.warning("云中转换票被拒：%s %s", resp.status_code, resp.text[:200])
        return None
    try:
        payload = resp.json()
    except Exception:
        logger.warning("云中转换票响应不是 JSON")
        return None
    if not str(payload.get("token") or ""):
        return None
    return payload


def mobile_page_available(base: str, device_id: str) -> bool:
    """探测网关是否部署了独立移动端（/m/<device_id> 返回 200）。"""
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(f"{base}/m/{device_id}")
        return resp.status_code == 200
    except Exception:
        return False


def build_pair_url(
    bridge, *, mobile: bool, ttl_seconds: int = 0, reason: dict | None = None
) -> dict | None:
    """云中转可用时返回公网二维码信息，否则返回 None 由调用方回落局域网。

    返回 ``{"url", "token", "expires_in"}``；``expires_in=0`` 表示不过期，
    与局域网配对 token 的 TTL 语义保持一致。

    回落原因写进 ``reason``（``code`` + 面向用户的 ``message``）：本模块只提供事实，
    是否在界面上提示由调用方决定，避免二维码路径里塞界面判断。
    """

    def fail(code: str, message: str) -> None:
        if reason is not None:
            reason["code"] = code
            reason["message"] = message
        return None

    hint = "已使用局域网地址（手机需与电脑连同一 WiFi）"
    try:
        service = getattr(bridge, "_cloud_relay_service", None)
        if service is None:
            return fail("disabled", f"云中转未启动，{hint}")
        status: dict[str, Any] = service.status()
        if not status.get("connected"):
            return fail("offline", f"云中转未连上，{hint}")
        device_id = str(status.get("device_id") or "")
        relay_url = str(config._load().get(config.CLOUD_RELAY_URL_KEY) or "")
        base = site_base(relay_url)
        if not device_id or not base:
            return fail("config", f"云中转缺少设备号或网关地址，{hint}")
        # 换票结果在本地缓存：二维码是「打开就生成」的高频动作，而网关对换票限流
        # （10 分钟 6 次），反复打开不能每次都去打网关。临近过期才重新换。
        payload = None
        cached = _TICKET_CACHE.get(device_id)
        now = int(time.time())
        if cached is not None and int(cached.get("expires_at") or 0) - now > _CACHE_MARGIN_SECONDS:
            payload = cached
        if payload is None:
            payload = request_ticket(base, device_id, device_token())
            if payload is not None:
                _TICKET_CACHE[device_id] = payload
        if payload is None:
            return fail("ticket", f"云中转换票失败，{hint}")
        jwt_token = str(payload.get("token") or "")
        # 移动端包没部署时退回桌面响应式页：同样能扫，只是不是专属移动端界面。
        if mobile and not mobile_page_available(base, device_id):
            page = f"/desktop/{device_id}"
        elif mobile:
            page = f"/m/{device_id}"
        else:
            page = f"/desktop/{device_id}"
        expires_at = int(payload.get("expires_at") or 0)
        remaining = max(0, expires_at - int(time.time())) if expires_at else 0
        if ttl_seconds and ttl_seconds > 0 and remaining > ttl_seconds:
            remaining = int(ttl_seconds)
        return {
            "url": f"{base}{page}?token={jwt_token}",
            "token": jwt_token,
            "expires_in": remaining,
        }
    except Exception:
        logger.exception("云中转公网二维码生成失败")
        return fail("error", f"云中转公网入口不可用，{hint}")
