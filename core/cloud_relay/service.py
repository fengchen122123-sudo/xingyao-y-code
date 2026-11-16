"""云中转服务。公开方法不向外抛异常。未开启时不建线程。"""

from __future__ import annotations

import logging
import threading
from typing import Any

import config

logger = logging.getLogger(__name__)


def _background_factory(url: str, token: str, bridge, on_state=None):
    """桌面开启云中转后在后台拨号。断线后退避重连，公开方法不把异常抛给界面。"""
    import time

    from core.cloud_relay.client import CloudRelayClient

    def report(state: str) -> None:
        if on_state is None:
            return
        try:
            on_state(state)
        except Exception:
            logger.exception("云中转状态回调失败")

    client = CloudRelayClient(send=lambda _payload: None)
    client.gateway_url = url
    client.device_token = token
    client.on_registered = lambda: report("connected")

    def on_rpc(method, args):
        service = CloudRelayService(bridge=bridge)
        return service.dispatch(method, args)

    client.on_rpc = on_rpc

    def loop() -> None:
        delay = 1
        first = True
        while not client.closed:
            report("connecting" if first else "reconnecting")
            first = False
            try:
                from websockets.sync.client import connect

                # websockets 17 起 connect() 默认 proxy=True，会读系统代理（Windows
                # 注册表 Internet Settings）。用户开关代理软件时，代理端口未监听就
                # 连不上，表现为 WinError 10061，二维码随之静默回落局域网。网关是
                # 自建域名、直连可达，这里显式禁用代理避免被本地代理状态拖累。
                try:
                    connection = connect(url, proxy=None)
                except TypeError:
                    # proxy 参数 15.0 才引入；更老的版本只能沿用默认行为（读系统代理）
                    logger.warning("websockets 版本过低，无法禁用代理，建议升级到 15.0+")
                    connection = connect(url)
                with connection as socket:
                    client._send = socket.send
                    data = config._load()
                    device_id = str(getattr(bridge, "cloud_relay_device_id", "") or "")
                    if not device_id:
                        device_id = str(data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or "") or "desktop"
                    client.pump(socket, token, device_id)
                    client.flush()
            except Exception:
                logger.exception("云中转连接中断")
                client.connected = False
                report("error")
            if client.closed:
                return
            if client.connected:
                client.connected = False
                report("reconnecting")
            time.sleep(delay)
            delay = min(delay * 2, 30)

    threading.Thread(target=loop, daemon=True, name="cloud-relay").start()
    return client


class CloudRelayService:
    def __init__(self, bridge=None, client_factory=None):
        self.bridge = bridge
        self._client_factory = client_factory
        self._client = None
        # 状态迁移会从后台线程回调进来，锁必须可重入，否则 start() 里自锁。
        self._lock = threading.RLock()
        self.state = "disabled"
        self._dispatch_host = None

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def start(self) -> bool:
        try:
            data = config._load()
            enabled = bool(data.get(config.CLOUD_RELAY_ENABLED_KEY, False))
            url = str(data.get(config.CLOUD_RELAY_URL_KEY) or "").strip()
            raw_token = str(data.get(config.CLOUD_RELAY_TOKEN_KEY) or "")
            try:
                from core.credentials import unprotect_secret

                token = unprotect_secret(raw_token).strip()
            except Exception:
                logger.exception("云中转设备 token 解密失败")
                token = ""
            if not enabled or not url or not token:
                self._set_state("disabled")
                return False
            factory = self._client_factory or _background_factory
            self._set_state("connecting")
            try:
                client = factory(url, token, self.bridge, self._set_state)
            except TypeError:
                # 老注入点只接受三个位置参数，保持兼容。
                client = factory(url, token, self.bridge)
            with self._lock:
                self._client = client
                if self.state == "connecting":
                    self.state = "connected" if getattr(client, "connected", False) else "connecting"
            return True
        except Exception:
            logger.exception("云中转启动失败")
            self._set_state("error")
            return False

    def stop(self) -> None:
        try:
            with self._lock:
                client = self._client
                self._client = None
                self.state = "stopped"
            if client is not None and hasattr(client, "close"):
                client.close()
        except Exception:
            logger.exception("云中转停止失败")

    def on_event(self, event_type: str, data: Any) -> None:
        try:
            client = self._client
            if client is None:
                return
            client.submit(event_type, data)
        except Exception:
            logger.exception("云中转事件转发失败")

    def dispatch(self, method, args):
        """桌面 relay 的来源固定是 lan，客户端带的 turn_source 会被剥掉。"""
        try:
            if self._dispatch_host is None:
                from core.lan_server import LanServer

                self._dispatch_host = LanServer(bridge=self.bridge)
            return self._dispatch_host.dispatch_rpc(method, args, turn_source="lan")
        except Exception:
            logger.exception("云中转 RPC 分发失败")
            return {"ok": False, "msg": "内部方法执行失败，详见桌面端日志"}

    def status(self) -> dict:
        client = self._client
        detail = {}
        if client is not None and hasattr(client, "status"):
            try:
                detail = client.status()
            except Exception:
                detail = {}
        return {
            "state": self.state,
            "connected": bool(detail.get("connected", False)),
            "device_id": str(detail.get("device_id") or ""),
            "boot_id": str(detail.get("boot_id") or ""),
            "generation": int(detail.get("generation") or 0),
            "queued": int(detail.get("queued") or 0),
            "dropped": int(detail.get("dropped") or 0),
            "lost": int(detail.get("lost") or 0),
        }
