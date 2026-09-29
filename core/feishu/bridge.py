"""飞书 ↔ 星瑶 桥接层：会话绑定、消息转回合、回合结果回发。

数据流：
- 飞书消息 → `on_feishu_event()` → 找到/新建绑定的星瑶会话 → `send_user_message(turn_source="feishu")`
- 星瑶事件 → `on_agent_event()`（由 FeishuService 转发）→ 回合结束时发「执行完成卡」

绑定关系持久化在 `~/.ycode/feishu_state.json`（YCODE_DATA_DIR 可覆盖），
每个飞书会话（chat）绑定一个星瑶会话，与 opencode-mobile 同模式。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

import config as app_config
from core.feishu.cards import (
    build_result_card,
    card_response,
    completion_card,
    extract_text,
    failure_card,
    strip_mention,
)
from core.feishu.client import FeishuApiClient, FeishuApiError
from core.feishu.config import FeishuConfig, load_feishu_config

logger = logging.getLogger("ycode.feishu.bridge")

TURN_SOURCE = "feishu"
EVENT_MESSAGE = "im.message.receive_v1"
EVENT_CARD_ACTION = "card.action.trigger"
STATE_FILENAME = "feishu_state.json"


def default_state_path() -> str:
    return os.path.join(app_config.YCODE_DIR, STATE_FILENAME)


class FeishuBindingStore:
    """飞书 chat ↔ 星瑶会话 的绑定关系（JSON 持久化，原子写）。"""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or default_state_path()
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"bindings": {}, "last_chat_id": ""}
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._data["bindings"] = dict(data.get("bindings") or {})
                self._data["last_chat_id"] = str(data.get("last_chat_id") or "")
        except FileNotFoundError:
            pass
        except Exception:
            logger.warning("读取飞书绑定状态失败（按空状态继续）: %s", self._path, exc_info=True)

    def _save_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            logger.warning("写入飞书绑定状态失败: %s", self._path, exc_info=True)

    def session_of(self, chat_id: str) -> str:
        with self._lock:
            entry = self._data["bindings"].get(str(chat_id)) or {}
            return str(entry.get("session") or "")

    def label_of(self, chat_id: str) -> str:
        with self._lock:
            entry = self._data["bindings"].get(str(chat_id)) or {}
            return str(entry.get("label") or "")

    def bind(self, chat_id: str, session: str, label: str) -> None:
        with self._lock:
            self._data["bindings"][str(chat_id)] = {
                "session": str(session),
                "label": str(label),
                "updated_at": int(time.time()),
            }
            self._save_locked()

    def unbind(self, chat_id: str) -> None:
        with self._lock:
            self._data["bindings"].pop(str(chat_id), None)
            self._save_locked()

    def all_bindings(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data["bindings"].items()}

    def last_chat_id(self) -> str:
        with self._lock:
            return str(self._data.get("last_chat_id") or "")

    def set_last_chat(self, chat_id: str) -> None:
        with self._lock:
            if self._data.get("last_chat_id") == str(chat_id):
                return
            self._data["last_chat_id"] = str(chat_id)
            self._save_locked()


class FeishuBridge:
    """把飞书消息接成星瑶回合，并把回合结果发回飞书。"""

    def __init__(self, bridge, api: Optional[FeishuApiClient] = None,
                 store: Optional[FeishuBindingStore] = None,
                 config_loader=load_feishu_config) -> None:
        self._bridge = bridge
        self._api = api
        self._store = store or FeishuBindingStore()
        self._load_config = config_loader
        self._lock = threading.Lock()
        self._api_lock = threading.Lock()
        # session_name → 本轮飞书回合的上下文（chat_id / 已收到的结果文本）
        self._pending: Dict[str, Dict[str, Any]] = {}

    # ---- 依赖 ----

    def _config(self) -> FeishuConfig:
        try:
            return self._load_config()
        except Exception:
            return FeishuConfig()

    def _api_client(self) -> FeishuApiClient:
        if self._api is not None:
            return self._api
        with self._api_lock:
            if self._api is None:
                cfg = self._config()
                if not cfg.configured:
                    raise FeishuApiError("飞书未配置（缺少 app_id/app_secret 或开关未打开）")
                self._api = FeishuApiClient(cfg.app_id, cfg.app_secret)
        return self._api

    # ---- 飞书 → 星瑶 ----

    def on_feishu_event(self, event: Dict[str, Any]) -> Optional[Any]:
        """飞书事件入口；返回值会被包进响应帧回给飞书（卡片回调需要）。"""
        header = (event or {}).get("header") or {}
        event_type = str(header.get("event_type") or "")
        if event_type == EVENT_MESSAGE:
            self._on_message(event)
            return None
        if event_type == EVENT_CARD_ACTION:
            return self._on_card_action(event)
        logger.debug("暂不处理的飞书事件类型: %s", event_type)
        return None

    def _on_message(self, event: Dict[str, Any]) -> None:
        body = (event or {}).get("event") or {}
        message = body.get("message") or {}
        chat_id = str(message.get("chat_id") or "")
        if not chat_id:
            return
        open_id = str(((body.get("sender") or {}).get("sender_id") or {}).get("open_id") or "")
        self._store.set_last_chat(chat_id)

        text = extract_text(message)
        if text is None:
            self._safe_send_text(chat_id, "目前只支持文本消息，图片/文件请先在星瑶桌面端发送。")
            return
        text = strip_mention(text)
        if not text:
            return

        session = self._ensure_session(chat_id, open_id)
        if not session:
            self._safe_send_text(chat_id, "创建星瑶会话失败，请稍后重试。")
            return

        self._buffer_turn(session, chat_id)
        try:
            result = self._bridge.send_user_message(text, session_name=session, turn_source=TURN_SOURCE)
        except Exception as e:
            self._drop_turn(session)
            logger.exception("飞书消息转回合失败")
            self._safe_send_text(chat_id, f"发送到星瑶失败：{e}")
            return
        if not result or not result.get("ok"):
            self._drop_turn(session)
            self._safe_send_text(chat_id, f"发送到星瑶失败：{(result or {}).get('msg') or '未知错误'}")

    def _on_card_action(self, event: Dict[str, Any]) -> Optional[Any]:
        body = (event or {}).get("event") or {}
        action = body.get("action") or {}
        value = action.get("value") or {}
        form_value = action.get("form_value") or {}
        context = body.get("context") or {}
        chat_id = str(context.get("open_chat_id") or context.get("chat_id") or self._store.last_chat_id())
        open_id = str((body.get("operator") or {}).get("open_id") or "")
        kind = str(value.get("kind") or "")

        if kind == "reply":
            text = str(form_value.get("reply") or "").strip()
            if not text:
                return card_response(build_result_card("⚠️ 没有内容", ["请先输入内容再点发送"]))
            session = self._ensure_session(chat_id, open_id)
            if not session:
                return card_response(build_result_card("❌ 发送失败", ["创建星瑶会话失败"]))
            self._buffer_turn(session, chat_id)
            try:
                result = self._bridge.send_user_message(text, session_name=session, turn_source=TURN_SOURCE)
            except Exception as e:
                self._drop_turn(session)
                logger.exception("飞书卡片追问转回合失败")
                return card_response(build_result_card("❌ 发送失败", [str(e)]))
            if not result or not result.get("ok"):
                self._drop_turn(session)
                return card_response(build_result_card("❌ 发送失败", [(result or {}).get("msg") or "未知错误"]))
            label = self._store.label_of(chat_id) or session
            return card_response(build_result_card("✅ 已发送", [f"会话 {label}，完成后推送结果"]))

        # 其余卡片动作（审批等）由后续任务接管
        logger.debug("暂不处理的卡片动作: %s", kind or action.get("name") or "?")
        return None

    # ---- 星瑶 → 飞书 ----

    def on_agent_event(self, event_type: str, data: Any) -> None:
        """星瑶事件转发入口（由 FeishuService 调用，跑在事件源线程里）。

        只处理「由飞书发起的回合」：`_pending` 里没有该会话就直接返回，
        桌面端自己的回合不会产生任何飞书消息。
        """
        if not isinstance(data, dict):
            return
        session = str(data.get("session_name") or "")
        if not session:
            return
        with self._lock:
            buffered = session in self._pending
        if not buffered:
            return

        if event_type == "message_final":
            with self._lock:
                entry = self._pending.get(session)
                if entry is not None:
                    content = data.get("content")
                    if isinstance(content, str) and content:
                        entry["content"] = content
            return

        if event_type == "turn_error":
            with self._lock:
                entry = self._pending.get(session)
                if entry is not None:
                    entry["error"] = str(data.get("message") or "未知错误")
            return

        if event_type in ("turn_finish", "turn_cancelled"):
            with self._lock:
                entry = self._pending.pop(session, None)
            if entry:
                self._flush(session, entry, cancelled=(event_type == "turn_cancelled"))

    def _flush(self, session: str, entry: Dict[str, Any], cancelled: bool) -> None:
        chat_id = str(entry.get("chat_id") or self._store.last_chat_id())
        if not chat_id:
            return
        label = self._store.label_of(chat_id) or session
        try:
            if entry.get("error"):
                card = failure_card(label, str(entry["error"]))
            else:
                card = completion_card(
                    label,
                    str(entry.get("content") or ""),
                    workdir=self._session_workdir(session),
                    cancelled=cancelled,
                )
            self._api_client().send_card(chat_id, card)
        except Exception:
            logger.exception("发送飞书回合结果失败（chat=%s）", chat_id)

    # ---- 内部工具 ----

    def _buffer_turn(self, session: str, chat_id: str) -> None:
        with self._lock:
            self._pending[session] = {"chat_id": chat_id, "content": "", "error": ""}

    def _drop_turn(self, session: str) -> None:
        with self._lock:
            self._pending.pop(session, None)

    def _ensure_session(self, chat_id: str, open_id: str) -> str:
        session = self._store.session_of(chat_id)
        if session and self._session_exists(session):
            return session
        label = self._label_for(open_id)
        try:
            result = self._bridge.create_session(label, None)
        except Exception:
            logger.exception("创建星瑶会话失败")
            return ""
        if not result or not result.get("ok"):
            return ""
        name = str(result.get("session_name") or "")
        if not name:
            return ""
        self._store.bind(chat_id, name, label)
        return name

    @staticmethod
    def _label_for(open_id: str) -> str:
        suffix = (open_id or "").strip()[:8]
        return f"飞书 · {suffix}" if suffix else "飞书 · 会话"

    def _session_exists(self, session: str) -> bool:
        try:
            groups = self._bridge.get_sessions() or []
        except Exception:
            return False
        for group in groups:
            for item in (group or {}).get("sessions") or []:
                if str(item.get("name") or "") == session:
                    return True
        return False

    def _session_workdir(self, session: str) -> str:
        try:
            groups = self._bridge.get_sessions() or []
        except Exception:
            return ""
        for group in groups:
            for item in (group or {}).get("sessions") or []:
                if str(item.get("name") or "") == session:
                    workdir = str(item.get("workdir") or "")
                    return "" if workdir == "GLOBAL" else workdir
        return ""

    def _safe_send_text(self, chat_id: str, text: str) -> None:
        try:
            self._api_client().send_text(chat_id, text)
        except Exception:
            logger.exception("发送飞书提示文本失败（chat=%s）", chat_id)
