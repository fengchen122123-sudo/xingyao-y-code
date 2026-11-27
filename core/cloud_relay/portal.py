from __future__ import annotations

import json
import logging
import time
from typing import Any

import config

logger = logging.getLogger(__name__)

# 换票与页面探测都在用户点「二维码」的同步路径上，超时必须短。
_TIMEOUT_SECONDS = 6.0
_RELAY_SUFFIX = "/relay/device"
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


def _ttl_of(value: Any) -> int:
    """把 ttl 归一成整数秒；None / 脏数据一律按 0（永久）处理。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def request_ticket(
    base: str, device_id: str, token: str, ttl_seconds: int = 0
) -> dict | None:
    """用设备令牌换取只读 remote JWT；失败返回 None。

    ``ttl_seconds`` 随请求发给网关（0 表示永久）。旧网关不认这个字段时会忽略它，
    按自己的默认（约 23 小时）签发，客户端照实显示即可，不需要报错。
    """
    if not base or not device_id or not token:
        return None
    try:
        import httpx

        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.post(
                f"{base}/api/relay/device-token",
                json={
                    "device_id": device_id,
                    "device_token": token,
                    "ttl_seconds": _ttl_of(ttl_seconds),
                },
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


def _persisted_ticket(device_id: str) -> dict | None:
    """读回上一次签发的远端票。

    网关已改成「一设备只认最新一张票」，桌面重建内存缓存后如果直接换票，
    会把用户手机里正在用的链接当场作废；把票持久化才能让重启不打扰在用设备。
    """
    try:
        raw = str(config._load().get(config.CLOUD_RELAY_REMOTE_TICKET_KEY) or "")
    except Exception:
        logger.debug("读取云中转远端票缓存失败", exc_info=True)
        return None
    if not raw:
        return None
    try:
        from core.credentials import unprotect_secret

        data = json.loads(unprotect_secret(raw))
    except Exception:
        logger.debug("云中转远端票缓存解码失败", exc_info=True)
        return None
    if not isinstance(data, dict) or str(data.get("device_id") or "") != device_id:
        return None
    if not str(data.get("token") or ""):
        return None
    if int(data.get("expires_at") or 0) <= int(time.time()):
        return None
    # ttl 缺失（老版本写下的缓存）按 0 处理：等价于「签票时没指定有效期」，
    # 不能当成「与当前设置不同的另一种值」，否则升级后每次打开二维码都要换票。
    data["ttl_seconds"] = _ttl_of(data.get("ttl_seconds"))
    return data


def _persist_ticket(device_id: str, payload: dict) -> None:
    try:
        from core.credentials import protect_secret

        blob = json.dumps(
            {
                "device_id": device_id,
                "token": str(payload.get("token") or ""),
                "expires_at": int(payload.get("expires_at") or 0),
                # 连 ttl 一起落盘：重启后若用户已改「过期时间」设置，要能认出
                # 这张票是按旧设置签的，从而主动重签而不是继续沿用旧链接。
                "ttl_seconds": _ttl_of(payload.get("ttl_seconds")),
            }
        )
        config.save_config_value(config.CLOUD_RELAY_REMOTE_TICKET_KEY, protect_secret(blob))
    except Exception:
        logger.warning("云中转远端票缓存写入失败", exc_info=True)


def build_pair_url(
    bridge,
    *,
    mobile: bool,
    ttl_seconds: int = 0,
    reason: dict | None = None,
    force_new: bool = False,
) -> dict | None:

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
        ttl = _ttl_of(ttl_seconds)
        payload = None
        refresh_notice = ""
        cached = _TICKET_CACHE.get(device_id)
        if cached is None:
            cached = _persisted_ticket(device_id)
            if cached is not None:
                _TICKET_CACHE[device_id] = cached
        now = int(time.time())
        cache_fresh = (
            cached is not None
            and int(cached.get("expires_at") or 0) - now > _CACHE_MARGIN_SECONDS
        )
        # 缓存必须连同「当初签票用的 ttl」一起比较：票是网关按旧设置签好的，
        # 用户改了「远程控制 token 过期时间」后只有重签一张才能让新设置生效，
        # 否则缓存会把设置改动永远吞掉（面板上的有效期一动不动）。
        # ttl=0 与不传都归一成 0，不会因为 None/0 之差反复换票。
        cache_matches_ttl = cache_fresh and _ttl_of(cached.get("ttl_seconds")) == ttl
        # force_new 来自用户点「刷新」：必须真去网关换新票。缓存只服务
        # 「打开面板就出码」的高频路径，若它吞掉显式刷新，二维码和有效期
        # 就永远纹丝不动——用户看到的就是「刷新无效」。
        if cache_matches_ttl and not force_new:
            payload = cached
        if payload is None:
            try:
                payload = request_ticket(base, device_id, device_token(), ttl_seconds=ttl)
            except TypeError:
                # 兼容只接受三参数的旧换票实现（测试桩 / 插件 monkeypatch）
                payload = request_ticket(base, device_id, device_token())
            if payload is not None:
                # 以用户设置为准记录 ttl：网关会回显被夹取后的实际值（ttl=0 会回显
                # 十年），直接存回显值会让缓存与设置永远对不上，每次打开都换票。
                payload["ttl_seconds"] = ttl
                _TICKET_CACHE[device_id] = payload
                _persist_ticket(device_id, payload)
                if force_new:
                    # 网关换票即换代：旧 jti 作废，已连着的远端页面也会被踢
                    refresh_notice = "旧链接已失效，请用新二维码重新扫码"
            elif cache_fresh:
                # 换票被拒（多数是网关限流 6 次/10 分钟）：刷新失败不能把手头
                # 仍可用的链接换成兜底提示，否则用户扫到的二维码直接失效。
                payload = cached
                refresh_notice = "刷新过于频繁，已保留当前链接（稍后再试）"
        if payload is None:
            return fail("ticket", f"云中转换票失败，{hint}")
        jwt_token = str(payload.get("token") or "")
        if mobile and not mobile_page_available(base, device_id):
            page = f"/desktop/{device_id}"
        elif mobile:
            page = f"/m/{device_id}"
        else:
            page = f"/desktop/{device_id}"
        expires_at = int(payload.get("expires_at") or 0)
        # 有效期按「这张票实际用的 ttl」决定：0 表示永久（前端 formatExpiry 把 0 渲染成
        # 「永久有效」）；非 0 时服务端已按请求签发，直接用真实剩余秒数，不再二次截短。
        if _ttl_of(payload.get("ttl_seconds")) == 0:
            remaining = 0
        else:
            remaining = max(0, expires_at - int(time.time()))
        result = {
            "url": f"{base}{page}?token={jwt_token}",
            "token": jwt_token,
            "expires_in": remaining,
        }
        if refresh_notice:
            result["refresh_notice"] = refresh_notice
        return result
    except Exception:
        logger.exception("云中转公网二维码生成失败")
        return fail("error", f"云中转公网入口不可用，{hint}")
