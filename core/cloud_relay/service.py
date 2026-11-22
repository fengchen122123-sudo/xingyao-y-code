from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

MAX_WS_FRAME_BYTES = 70 * 1024 * 1024
_STATUS_FILENAME = "cloud_relay_status.json"


def runtime_status_path() -> Path:
    """运行态快照文件（不含密钥，供安装版 CLI / 自动部署流程探测 GUI 连接状态）。"""
    cfg_file = getattr(config, "_CONFIG_FILE", "") or ""
    base = Path(cfg_file).parent if cfg_file else Path(getattr(config, "YCODE_DIR", Path.home() / ".ycode"))
    return base / _STATUS_FILENAME


def write_runtime_status(detail: dict[str, Any]) -> None:
    try:
        target = runtime_status_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "state": str(detail.get("state") or "disabled"),
            "connected": bool(detail.get("connected", False)),
            "device_id": str(detail.get("device_id") or ""),
            "generation": int(detail.get("generation") or 0),
            "queued": int(detail.get("queued") or 0),
            "dropped": int(detail.get("dropped") or 0),
            "lost": int(detail.get("lost") or 0),
            "pid": os.getpid(),
            "updated_at": int(time.time()),
        }
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        logger.debug("写入云中转运行态快照失败", exc_info=True)


def configure_cloud_relay(
    url: str,
    device_id: str,
    token: str = "",
    *,
    enabled: bool = True,
) -> dict[str, Any]:
    """写入本机云中转配置（自动用 DPAPI/keyring 加密 token；不依赖系统 Python）。"""
    clean_url = str(url or "").strip()
    clean_dev = str(device_id or "").strip()
    clean_tok = str(token or "").strip()
    if enabled:
        if not clean_url or not clean_url.lower().startswith(("ws://", "wss://")):
            return {"ok": False, "msg": "网关地址必须以 ws:// 或 wss:// 开头"}
        if not clean_dev:
            return {"ok": False, "msg": "device_id 不能为空"}
    config.save_config_value(config.CLOUD_RELAY_ENABLED_KEY, bool(enabled))
    config.save_config_value(config.CLOUD_RELAY_URL_KEY, clean_url)
    config.save_config_value(config.CLOUD_RELAY_DEVICE_ID_KEY, clean_dev)
    if clean_tok:
        try:
            from core.credentials import protect_secret

            stored = protect_secret(clean_tok)
        except Exception:
            logger.warning("云中转令牌加密失败，回落明文保存", exc_info=True)
            stored = clean_tok
        config.save_config_value(config.CLOUD_RELAY_TOKEN_KEY, stored)
    data = config._load()
    return {
        "ok": True,
        "enabled": bool(data.get(config.CLOUD_RELAY_ENABLED_KEY, False)),
        "url": str(data.get(config.CLOUD_RELAY_URL_KEY) or ""),
        "device_id": str(data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or ""),
        "has_token": bool(str(data.get(config.CLOUD_RELAY_TOKEN_KEY) or "").strip()),
    }


def read_runtime_status() -> dict[str, Any]:
    """合并配置文件与运行态快照（不含任何令牌明文）。"""
    data = config._load()
    runtime: dict[str, Any] = {}
    path = runtime_status_path()
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                runtime = parsed
        except Exception:
            runtime = {}
    return {
        "ok": True,
        "settings": {
            "enabled": bool(data.get(config.CLOUD_RELAY_ENABLED_KEY, False)),
            "url": str(data.get(config.CLOUD_RELAY_URL_KEY) or ""),
            "device_id": str(data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or ""),
            "has_token": bool(str(data.get(config.CLOUD_RELAY_TOKEN_KEY) or "").strip()),
            "state": str(runtime.get("state") or "disabled"),
            "connected": bool(runtime.get("connected", False)),
            "generation": int(runtime.get("generation") or 0),
            "queued": int(runtime.get("queued") or 0),
            "dropped": int(runtime.get("dropped") or 0),
            "lost": int(runtime.get("lost") or 0),
            "pid": runtime.get("pid"),
            "updated_at": runtime.get("updated_at"),
        },
    }


def _background_factory(url: str, token: str, bridge, on_state=None):
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
    delay = 1

    def on_registered() -> None:
        nonlocal delay
        delay = 1
        report("connected")

    client.on_registered = on_registered
    rpc_host = CloudRelayService(bridge=bridge)
    client.on_rpc = rpc_host.dispatch

    def loop() -> None:
        nonlocal delay
        first = True
        while not client.closed:
            report("connecting" if first else "reconnecting")
            first = False
            try:
                from websockets.sync.client import connect

                try:
                    connection = connect(url, proxy=None, max_size=MAX_WS_FRAME_BYTES)
                except TypeError:
                    logger.warning("websockets 版本过低，无法禁用代理，建议升级到 15.0+")
                    connection = connect(url, max_size=MAX_WS_FRAME_BYTES)
                with connection as socket:
                    client._send = socket.send
                    data = config._load()
                    device_id = str(getattr(bridge, "cloud_relay_device_id", "") or "")
                    if not device_id:
                        device_id = str(data.get(config.CLOUD_RELAY_DEVICE_ID_KEY) or "") or "desktop"
                    client.pump(socket, token, device_id)
            except Exception:
                logger.exception("云中转连接中断")
                client.connected = False
                report("error")
            else:
                if not client.closed:
                    client.connected = False
                    report("reconnecting")
            finally:
                client._send = lambda _payload: None
            if client.closed:
                return
            time.sleep(delay)
            delay = min(delay * 2, 30)

    threading.Thread(target=loop, daemon=True, name="cloud-relay").start()
    return client


class CloudRelayService:
    def __init__(self, bridge=None, client_factory=None, *, persist_status: bool = False):
        self.bridge = bridge
        self._client_factory = client_factory
        self._persist_status = bool(persist_status)
        self._client = None
        # 状态迁移会从后台线程回调进来，锁必须可重入，否则 start() 里自锁。
        self._lock = threading.RLock()
        self.state = "disabled"
        self._dispatch_host = None

    def _sync_runtime_status(self) -> None:
        if self._persist_status:
            write_runtime_status(self.status())

    def _set_state(self, state: str) -> None:
        with self._lock:
            self.state = state
        self._sync_runtime_status()

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
            self._sync_runtime_status()
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
            self._sync_runtime_status()
        except Exception:
            logger.exception("云中转停止失败")

    def on_event(self, event_type: str, data: Any) -> None:
        try:
            from core.cloud_relay.client import DESKTOP_ONLY_EVENT_TYPES

            if event_type in DESKTOP_ONLY_EVENT_TYPES:
                return
            client = self._client
            if client is None:
                return
            client.submit(event_type, data)
        except Exception:
            logger.exception("云中转事件转发失败")

    def dispatch(self, method, args):
        """桌面 relay 的来源固定是 lan，客户端带的 turn_source 会被剥掉。"""
        try:
            host = self._dispatch_host
            if host is None:
                with self._lock:
                    host = self._dispatch_host
                    if host is None:
                        from core.lan_server import LanServer

                        host = LanServer(bridge=self.bridge)
                        self._dispatch_host = host
            return host.dispatch_rpc(method, args, turn_source="lan")
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
