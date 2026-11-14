"""云中转服务。公开方法不向外抛异常。未开启时不建线程。"""

from __future__ import annotations

import logging
import threading
from typing import Any

import config

logger = logging.getLogger(__name__)


class CloudRelayService:
    def __init__(self, bridge=None, client_factory=None):
        self.bridge = bridge
        self._client_factory = client_factory
        self._client = None
        self._lock = threading.Lock()
        self.state = "disabled"
        self._dispatch_host = None

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
                self.state = "disabled"
                return False
            if self._client_factory is None:
                self.state = "error"
                logger.info("云中转已开启，但当前进程没有注入连接器")
                return False
            with self._lock:
                self.state = "connecting"
                self._client = self._client_factory(url, token, self.bridge)
                self.state = "connected"
            return True
        except Exception:
            logger.exception("云中转启动失败")
            self.state = "error"
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
        return {"state": self.state}
