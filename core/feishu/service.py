"""飞书通道服务：单例 + 后台线程 + 启停 + 连接状态。

生命周期范式仿 `core/idle_maintenance.py`（模块级单例 + daemon 线程），
启动/停止接线在 `gui_bridge`（仿 `_ensure_lan_server`）。

职责边界：
- 本服务只管「连接 + 事件进出 + 状态」，不含业务语义；业务逻辑由注入的
  bridge（`core/feishu/bridge.py`）实现，星瑶事件经 `on_event()` 转发给它。
- 任何公开方法都不向调用方抛异常（飞书通道故障不得影响主流程）。
"""

import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from core.feishu.config import load_feishu_config
from core.feishu._lark_ws import Client
from core.feishu._lark_ws.enum import LogLevel

logger = logging.getLogger("ycode.feishu.service")

# 事件去重窗口（秒）：飞书对未及时 ACK 的事件会重投，按 event_id 去重
DEDUP_TTL_SECONDS = 300

STATE_DISABLED = "disabled"
STATE_CONNECTING = "connecting"
STATE_CONNECTED = "connected"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"
STATE_STOPPED = "stopped"


class FeishuService:
    """飞书长连接服务。未配置时 `start()` 直接返回 False，不做任何事。"""

    def __init__(self, bridge=None) -> None:
        self._bridge = bridge
        self._thread: Optional[threading.Thread] = None
        self._client: Optional[Client] = None
        self._lock = threading.Lock()
        self._state = STATE_DISABLED
        self._last_error = ""
        self._started_at = 0.0
        self._received_events = 0
        self._duplicates = 0
        self._seen: Dict[str, float] = {}
        self._seen_lock = threading.Lock()

    # ---- 注入 ----

    def set_bridge(self, bridge) -> None:
        self._bridge = bridge
        self._wire_status_provider(bridge)

    def _wire_status_provider(self, bridge) -> None:
        """把本服务的状态快照注入桥接层，供 /status 指令展示连接态。"""
        setter = getattr(bridge, "set_status_provider", None)
        if callable(setter):
            try:
                setter(self.status)
            except Exception:
                logger.debug("注入飞书状态提供者失败", exc_info=True)

    # ---- 生命周期 ----

    def start(self) -> bool:
        """按当前配置启动长连接。未配置或已在运行返回 False（幂等，不抛异常）。"""
        try:
            cfg = load_feishu_config()
        except Exception:
            logger.exception("读取飞书配置失败")
            self._state = STATE_ERROR
            self._last_error = "读取配置失败"
            return False

        if not cfg.configured:
            self._state = STATE_DISABLED
            return False

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._wire_status_provider(self._bridge)
            try:
                client = Client(
                    app_id=cfg.app_id,
                    app_secret=cfg.app_secret,
                    on_event=self._handle_event,
                    log_level=LogLevel.INFO,
                    source="xingyao",
                )
            except Exception as e:
                logger.exception("创建飞书长连接客户端失败")
                self._state = STATE_ERROR
                self._last_error = str(e)
                return False

            client.on_connected = self._on_connected
            client.on_reconnecting = self._on_reconnecting
            client.on_reconnected = self._on_connected

            self._client = client
            self._state = STATE_CONNECTING
            self._last_error = ""
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._run, args=(client,), daemon=True, name="feishu-ws",
            )
            self._thread.start()
            logger.info("飞书长连接服务已启动（app_id=%s…）", cfg.app_id[:8])
            return True

    def _run(self, client: Client) -> None:
        try:
            client.start()
        except Exception as e:
            logger.exception("飞书长连接异常退出")
            self._state = STATE_ERROR
            self._last_error = str(e)
        finally:
            self._client = None

    def stop(self, timeout: float = 5.0) -> None:
        """停止服务（线程安全、幂等、不抛异常）。"""
        with self._lock:
            client = self._client
            thread = self._thread
        if client is not None:
            try:
                client.stop()
            except Exception:
                logger.debug("请求停止飞书长连接失败", exc_info=True)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            self._client = None
            if self._state != STATE_DISABLED:
                self._state = STATE_STOPPED

    def restart(self) -> bool:
        """配置变更后重建连接。"""
        self.stop()
        return self.start()

    # ---- 状态回调（由传输层在 ws 线程内调用）----

    def _on_connected(self) -> None:
        self._state = STATE_CONNECTED
        self._last_error = ""

    def _on_reconnecting(self) -> None:
        self._state = STATE_RECONNECTING

    # ---- 事件：星瑶 → 飞书 ----

    def on_event(self, event_type: str, data: Any) -> None:
        """星瑶事件转发入口（由 `gui_bridge._emit_raw` 调用）。

        未注入 bridge 或 bridge 抛错时静默跳过：飞书通道故障绝不能影响
        桌面端事件派发（调用点在事件热路径上）。
        """
        bridge = self._bridge
        if bridge is None:
            return
        try:
            bridge.on_agent_event(event_type, data)
        except Exception:
            logger.debug("飞书事件转发失败: %s", event_type, exc_info=True)

    # ---- 事件：飞书 → 星瑶 ----

    def _handle_event(self, payload: bytes) -> Optional[Any]:
        """飞书事件入口（在长连接线程内调用）。

        返回值会被传输层序列化后作为 ACK 回给飞书——卡片回调需要它，
        消息事件返回 None 即可。

        入站路径必须有 INFO 日志：长连接"连上了但收不到消息"是最常见的现场问题，
        没有日志就无法区分「飞书没推」与「我们丢掉了」。
        """
        try:
            event = json.loads(payload.decode("utf-8"))
        except Exception:
            logger.warning("飞书事件解析失败（非 JSON）: %s", payload[:200], exc_info=True)
            return None

        header = (event or {}).get("header") or {}
        event_id = str(header.get("event_id") or "")
        event_type = str(header.get("event_type") or "")
        if not event_type:
            # 载荷形状与预期不符时把顶层键打出来，便于定位协议差异
            logger.warning("飞书事件缺少 header.event_type，顶层键=%s",
                           sorted((event or {}).keys()) if isinstance(event, dict) else type(event).__name__)
            return None
        logger.info("飞书事件到达: type=%s id=%s", event_type, event_id or "<空>")

        if not event_id:
            # 无 event_id 无法去重：宁可不处理，也不冒重复执行用户消息的风险
            logger.warning("飞书事件缺少 event_id，已忽略（event_type=%s）", event_type)
            return None

        dedup_key = f"{event_id}:{event_type}"
        if self._is_duplicate(dedup_key):
            self._duplicates += 1
            logger.info("飞书事件重复投递，已忽略: %s", dedup_key)
            return None

        self._received_events += 1
        bridge = self._bridge
        if bridge is None:
            logger.warning("飞书事件到达但未注入 bridge，已忽略（event_type=%s）", event_type)
            return None
        try:
            return bridge.on_feishu_event(event)
        except Exception:
            logger.exception("处理飞书事件失败: %s", event_type)
            return None

    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        with self._seen_lock:
            expired = [k for k, ts in self._seen.items() if ts <= now]
            for k in expired:
                self._seen.pop(k, None)
            if key in self._seen:
                return True
            self._seen[key] = now + DEDUP_TTL_SECONDS
            return False

    # ---- 状态查询 ----

    def status(self) -> Dict[str, Any]:
        """给设置页/状态卡用的快照（不含密钥明文）。"""
        try:
            cfg = load_feishu_config()
        except Exception:
            cfg = None
        thread = self._thread
        running = bool(thread is not None and thread.is_alive())
        return {
            "enabled": bool(cfg.enabled) if cfg else False,
            "configured": bool(cfg.configured) if cfg else False,
            "has_secret": bool(cfg.has_secret) if cfg else False,
            "app_id": (cfg.app_id if cfg else "") or "",
            "approvers_count": len(cfg.approvers) if cfg else 0,
            "notify_chat_id": (cfg.notify_chat_id if cfg else "") or "",
            "state": self._state,
            "running": running,
            "last_error": self._last_error,
            "received_events": self._received_events,
            "duplicate_events": self._duplicates,
            "uptime_seconds": int(time.time() - self._started_at) if running and self._started_at else 0,
        }


# 模块级单例：与 core/idle_maintenance.py、core/cache_keepalive.py 同范式
feishu_service = FeishuService()
