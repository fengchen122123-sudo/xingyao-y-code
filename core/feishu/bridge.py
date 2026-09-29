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
from typing import Any, Dict, List, Optional

import config as app_config
from core.feishu.cards import (
    build_result_card,
    card_response,
    completion_card,
    extract_text,
    failure_card,
    permission_card,
    question_card,
    strip_mention,
    target_lines,
)
from core.feishu.commands import (
    HELP_TEXT,
    agent_card,
    inbox_card,
    model_list_card,
    panel_card,
    parse_command,
    session_list_card,
    stall_card,
    status_card,
    text_card,
)
from core.feishu.client import FeishuApiClient, FeishuApiError
from core.feishu.config import FeishuConfig, load_feishu_config

logger = logging.getLogger("ycode.feishu.bridge")

TURN_SOURCE = "feishu"
EVENT_MESSAGE = "im.message.receive_v1"
EVENT_CARD_ACTION = "card.action.trigger"
STATE_FILENAME = "feishu_state.json"

# 卡住提醒：某会话持续执行但 N 分钟无任何输出时推一次，同会话 2 小时内最多一次
STALL_MINUTES = 10
STALL_COOLDOWN_SECONDS = 7200
WATCHDOG_INTERVAL_SECONDS = 60


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
        # 卡片消息 id → 权限请求 id：表单提交按钮带不了 value，只能靠
        # context.open_message_id 反查（与 opencode-mobile 的 messageId 方案一致）
        self._card_requests: Dict[str, str] = {}
        self._card_requests_limit = 500
        # 未决的权限/提问（供 /inbox 聚合）：request_id → 摘要
        self._pending_requests: Dict[str, Dict[str, Any]] = {}
        # 卡住提醒的冷却时间戳（session → 上次提醒时刻）
        self._stall_notified: Dict[str, float] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._watchdog_lock = threading.Lock()
        # 连接状态提供者（由 FeishuService 注入，供 /status 展示）
        self._status_provider = None

    def set_status_provider(self, provider) -> None:
        self._status_provider = provider

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
        if text.startswith("/"):
            self._handle_command(text, chat_id, open_id)
            return

        session = self._ensure_session(chat_id, open_id)
        if not session:
            self._safe_send_text(chat_id, "创建星瑶会话失败，请稍后重试。")
            return

        self._buffer_turn(session, chat_id, text)
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
        # 表单提交按钮只带 action.name（没有 behaviors/value），因此 kind 要两处取
        kind = str(value.get("kind") or action.get("name") or "")
        # request_id 优先取按钮 value；表单提交（无 value）用卡片消息 id 反查
        request_id = str(value.get("request_id") or "")
        if not request_id:
            request_id = self._request_id_for_message(str(context.get("open_message_id") or ""))

        if kind == "reply":
            text = str(form_value.get("reply") or "").strip()
            if not text:
                return card_response(build_result_card("⚠️ 没有内容", ["请先输入内容再点发送"]))
            session = self._ensure_session(chat_id, open_id)
            if not session:
                return card_response(build_result_card("❌ 发送失败", ["创建星瑶会话失败"]))
            self._buffer_turn(session, chat_id, text)
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

        if kind == "perm":
            return self._handle_permission_action(value, request_id, open_id)

        if kind == "ask":
            return self._handle_ask_action(value, form_value, request_id, open_id)

        if kind == "cmd":
            # 面板/列表/翻页按钮：按钮即指令（无状态，页码与参数都在 value 里）
            cmd = str(value.get("cmd") or "")
            arg = str(value.get("arg") or "")
            if cmd:
                self._handle_command(f"{cmd} {arg}".strip(), chat_id, open_id)
            return None

        # 其余卡片动作由后续任务接管
        logger.debug("暂不处理的卡片动作: %s", kind or action.get("name") or "?")
        return None

    # ---- 审批 / 提问 ----

    def _push_permission_card(self, data: Dict[str, Any]) -> None:
        request_id = str(data.get("request_id") or "")
        if not request_id:
            return
        chat_id = self._notify_chat()
        if not chat_id:
            return
        session = str(data.get("session_name") or "")
        label = self._store.label_of(chat_id) or session
        cfg = self._config()
        allowed = bool(cfg.approvers)
        hint = "" if allowed else "未配置审批白名单（设置 → 飞书集成），请在桌面端处理本次授权。"
        function_name = str(data.get("function_name") or "")
        is_question = function_name == "ask_user"
        targets = target_lines(dict(data.get("arguments") or {}))
        workdir = self._session_workdir(session)
        # 记入未决清单，供 /inbox 聚合与「▶ 处理」重推卡
        with self._lock:
            self._pending_requests[request_id] = {
                "request_id": request_id,
                "is_question": is_question,
                "title": str(data.get("summary") or ("星瑶提问" if is_question else function_name)),
                "session_label": label,
                "options": [str(o) for o in (data.get("options") or [])],
                "action_name": function_name,
                "summary": str(data.get("summary") or ""),
                "targets": targets,
                "workdir": workdir,
                "allow_always": bool(data.get("allow_always")),
            }
        try:
            if is_question:
                card = question_card(
                    question=str(data.get("summary") or data.get("details") or "星瑶有问题需要你确认"),
                    options=[str(o) for o in (data.get("options") or [])],
                    session_label=label,
                    request_id=request_id,
                    buttons_enabled=allowed,
                    hint=hint,
                )
            else:
                card = permission_card(
                    action_name=function_name,
                    summary=str(data.get("summary") or ""),
                    targets=targets,
                    session_label=label,
                    workdir=workdir,
                    request_id=request_id,
                    allow_always=bool(data.get("allow_always")),
                    buttons_enabled=allowed,
                    hint=hint,
                )
            resp = self._api_client().send_card(chat_id, card)
            # 记下卡片消息 id → 请求 id：表单提交按钮没有 value，回调只能靠
            # context.open_message_id 反查（与 opencode-mobile 的 messageId 方案一致）
            message_id = str(((resp or {}).get("data") or {}).get("message_id") or "")
            if message_id:
                self._remember_card_request(message_id, request_id)
        except Exception:
            logger.exception("推送飞书审批卡失败（request_id=%s）", request_id)

    def _remember_card_request(self, message_id: str, request_id: str) -> None:
        with self._lock:
            self._card_requests[message_id] = request_id
            while len(self._card_requests) > self._card_requests_limit:
                oldest = next(iter(self._card_requests), None)
                if oldest is None:
                    break
                self._card_requests.pop(oldest, None)

    def _request_id_for_message(self, message_id: str) -> str:
        if not message_id:
            return ""
        with self._lock:
            return self._card_requests.get(message_id, "")

    def _handle_permission_action(self, value: Dict[str, Any], request_id: str, open_id: str) -> Any:
        if not request_id:
            return card_response(build_result_card("⚠️ 无法识别请求", ["卡片与请求的对应关系已失效，请在桌面端处理"]))
        allowed, reason = self._approver_allowed(open_id)
        if not allowed:
            return card_response(build_result_card("❌ 无权操作", [reason]))
        allow = bool(value.get("allow"))
        always = bool(value.get("always"))
        try:
            result = self._bridge.respond_permission(request_id, allow, always)
        except Exception as e:
            logger.exception("飞书审批回传失败")
            return card_response(build_result_card("❌ 审批失败", [str(e)]))
        if not result or not result.get("ok"):
            return card_response(build_result_card(
                "⚠️ 已处理或已失效", [str((result or {}).get("msg") or "该权限请求已失效")]))
        with self._lock:
            self._pending_requests.pop(request_id, None)
        title = "✅ 已批准" if allow else "✖ 已拒绝"
        if allow and always:
            title = "✅ 已总是批准"
        return card_response(build_result_card(title, [str(value.get("action_label") or "")]))

    def _handle_ask_action(self, value: Dict[str, Any], form_value: Dict[str, Any],
                           request_id: str, open_id: str) -> Any:
        if not request_id:
            return card_response(build_result_card("⚠️ 无法识别请求", ["卡片与请求的对应关系已失效，请在桌面端处理"]))
        allowed, reason = self._approver_allowed(open_id)
        if not allowed:
            return card_response(build_result_card("❌ 无权操作", [reason]))
        try:
            if "index" in value:
                index = int(value.get("index"))
                result = self._bridge.respond_permission_choice(request_id, index, "")
                answer = str((value.get("options") or [None])[index] if value.get("options") else index)
            else:
                # 自由文本：桌面/移动端统一用 -1 表示自定义回答
                answer = str(form_value.get("answer") or "").strip()
                if not answer:
                    return card_response(build_result_card("⚠️ 没有内容", ["请输入回答后再提交"]))
                result = self._bridge.respond_permission_choice(request_id, -1, answer)
        except Exception as e:
            logger.exception("飞书提问回传失败")
            return card_response(build_result_card("❌ 回答失败", [str(e)]))
        if not result or not result.get("ok"):
            return card_response(build_result_card(
                "⚠️ 已处理或已失效", [str((result or {}).get("msg") or "该请求已失效")]))
        with self._lock:
            self._pending_requests.pop(request_id, None)
        return card_response(build_result_card("✅ 已回答", [f"回答：{answer}"]))

    # ---- 星瑶 → 飞书 ----

    def on_agent_event(self, event_type: str, data: Any) -> None:
        """星瑶事件转发入口（由 FeishuService 调用，跑在事件源线程里）。

        - 审批/提问不分来源：桌面端发起的回合同样可能等人拍板，人在外面时
          正是靠这条链路把审批推到飞书（与 opencode-mobile 一致）。
        - 回合结果只处理「由飞书发起的回合」：`_pending` 里没有该会话就直接返回，
          桌面端自己的回合不会产生多余的飞书消息。
        """
        if not isinstance(data, dict):
            return

        if event_type == "permission_request":
            self._push_permission_card(data)
            return

        if event_type == "permission_dismiss":
            # 审批被别处处理（桌面端/超时/取消）：清掉收件箱里的未决项
            with self._lock:
                self._pending_requests.clear()
            return

        session = str(data.get("session_name") or "")
        if not session:
            return
        # 任何事件都算「有输出」，刷新卡住判定
        self._touch_activity(session)
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

    def _buffer_turn(self, session: str, chat_id: str, text: str = "") -> None:
        now = time.time()
        with self._lock:
            self._pending[session] = {
                "chat_id": chat_id,
                "content": "",
                "error": "",
                "last_text": text,
                "started_at": now,
                "last_activity": now,
            }
        self._ensure_watchdog()

    def _drop_turn(self, session: str) -> None:
        with self._lock:
            self._pending.pop(session, None)

    def _touch_activity(self, session: str) -> None:
        """回合有输出就刷新活跃时间（卡住判定的依据）。"""
        with self._lock:
            entry = self._pending.get(session)
            if entry is not None:
                entry["last_activity"] = time.time()

    # ---- 卡住提醒 ----

    def _ensure_watchdog(self) -> None:
        with self._watchdog_lock:
            if self._watchdog is not None and self._watchdog.is_alive():
                return
            self._watchdog = threading.Thread(
                target=self._watchdog_loop, daemon=True, name="feishu-stall-watchdog")
            self._watchdog.start()

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(WATCHDOG_INTERVAL_SECONDS)
            try:
                self._check_stalls()
            except Exception:
                logger.debug("卡住检测异常", exc_info=True)

    def _check_stalls(self) -> None:
        now = time.time()
        with self._lock:
            candidates = [(name, dict(entry)) for name, entry in self._pending.items()]
        for session, entry in candidates:
            last = float(entry.get("last_activity") or entry.get("started_at") or 0)
            if not last or now - last < STALL_MINUTES * 60:
                continue
            with self._lock:
                if now - float(self._stall_notified.get(session) or 0) < STALL_COOLDOWN_SECONDS:
                    continue
                self._stall_notified[session] = now
            chat_id = str(entry.get("chat_id") or self._store.last_chat_id())
            if not chat_id:
                continue
            label = self._store.label_of(chat_id) or session
            self._safe_send_card(chat_id, stall_card(
                session_label=label,
                last_text=str(entry.get("last_text") or ""),
                minutes=int((now - last) / 60),
            ))

    # ---- 指令 ----

    def _handle_command(self, text: str, chat_id: str, open_id: str) -> None:
        cmd, arg = parse_command(text)
        if not cmd:
            return
        try:
            card = self._build_command_card(cmd, arg, chat_id, open_id)
        except Exception:
            logger.exception("处理飞书指令失败: %s", cmd)
            self._safe_send_text(chat_id, f"指令处理失败：{cmd}")
            return
        if card is not None:
            self._safe_send_card(chat_id, card)

    def _build_command_card(self, cmd: str, arg: str, chat_id: str, open_id: str):
        if cmd in ("/help", "/?", "/start"):
            return text_card("星瑶 · 帮助", HELP_TEXT)

        if cmd == "/panel":
            return panel_card()

        if cmd == "/status":
            return self._status_snapshot_card(chat_id)

        if cmd == "/agent":
            return agent_card(agent_name=self._agent_name(), model_name=self._active_model_name())

        if cmd == "/new":
            title = arg or self._label_for(open_id)
            result = self._bridge.create_session(title, None)
            if not result or not result.get("ok"):
                self._safe_send_text(chat_id, "新建会话失败，请稍后重试。")
                return None
            name = str(result.get("session_name") or "")
            self._store.bind(chat_id, name, title)
            return text_card("✅ 已新建并绑定", f"会话 **{title}**\n`{name}`", "green")

        if cmd == "/list":
            page = self._page_of(arg)
            return session_list_card(sessions=self._all_sessions(),
                                     current_session=self._store.session_of(chat_id), page=page)

        if cmd == "/switch":
            return self._command_switch(chat_id, arg)

        if cmd == "/model":
            return self._command_model(chat_id, arg)

        if cmd == "/inbox":
            if arg.startswith("open:"):
                return self._open_inbox_item(arg.split(":", 1)[1])
            return inbox_card(items=self._inbox_items(), page=self._page_of(arg))

        if cmd == "/stop":
            session = self._store.session_of(chat_id)
            if not session:
                self._safe_send_text(chat_id, "当前没有绑定会话。")
                return None
            result = self._bridge.stop_current_turn(session)
            if result and result.get("ok") is False:
                self._safe_send_text(chat_id, f"中止失败：{result.get('msg') or '未知错误'}")
            else:
                self._safe_send_text(chat_id, "⏹ 已发送中止请求。")
            return None

        self._safe_send_text(chat_id, f"未知指令：{cmd}（发 /help 看指令表）")
        return None

    def _command_switch(self, chat_id: str, arg: str):
        sessions = self._all_sessions()
        try:
            index = int(arg)
        except (TypeError, ValueError):
            self._safe_send_text(chat_id, "用法：/switch <序号>（序号见 /list）")
            return None
        if index < 1 or index > len(sessions):
            self._safe_send_text(chat_id, f"序号超出范围（1-{len(sessions)}）")
            return None
        target = sessions[index - 1]
        name = str(target.get("name") or "")
        title = str(target.get("display_name") or name)
        self._store.bind(chat_id, name, title)
        return text_card("✅ 已切换绑定", f"会话 **{title}**\n`{name}`", "green")

    def _command_model(self, chat_id: str, arg: str):
        settings = self._bridge.get_model_settings() or {}
        models = list(settings.get("models") or [])
        active = str(settings.get("active_model_id") or "")
        if not arg or arg.startswith("page:"):
            return model_list_card(models=models, active_model_id=active, page=self._page_of(arg))
        target = None
        if arg.isdigit():
            index = int(arg)
            if 1 <= index <= len(models):
                target = models[index - 1]
        else:
            for item in models:
                if arg in (str(item.get("id") or ""), str(item.get("name") or "")):
                    target = item
                    break
        if target is None:
            self._safe_send_text(chat_id, f"没找到模型：{arg}（发 /model 看列表）")
            return None
        session = self._store.session_of(chat_id) or None
        result = self._bridge.set_model(str(target.get("id") or ""), session)
        if result and result.get("ok") is False:
            self._safe_send_text(chat_id, f"切换模型失败：{result.get('msg') or '未知错误'}")
            return None
        return text_card("✅ 已切换模型",
                         f"**{target.get('name') or target.get('id')}**\n`{target.get('id')}`", "green")

    def _status_snapshot_card(self, chat_id: str):
        cfg = self._config()
        session = self._store.session_of(chat_id)
        settings = {}
        try:
            settings = self._bridge.get_model_settings() or {}
        except Exception:
            settings = {}
        active_id = str(settings.get("active_model_id") or "")
        name = self._active_model_name()
        state = "未知"
        if callable(self._status_provider):
            try:
                state = str((self._status_provider() or {}).get("state") or "未知")
            except Exception:
                state = "未知"
        return status_card(
            session_label=self._store.label_of(chat_id) or "",
            session_name=session,
            workdir=self._session_workdir(session),
            model_name=name or active_id,
            connection_state=state,
            approvers_count=len(cfg.approvers or []),
            pending_count=len(self._inbox_items()),
            agent_name=self._agent_name(),
        )

    def _inbox_items(self) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(v) for v in self._pending_requests.values()]
        return items

    def _open_inbox_item(self, request_id: str):
        with self._lock:
            item = self._pending_requests.get(request_id)
        if not item:
            return text_card("⚠️ 该待办已处理", "它可能已经被桌面端或其他人处理了。", "red")
        # 重新推一张对应的卡（按钮直接可用），不再另建状态
        cfg = self._config()
        allowed = bool(cfg.approvers)
        hint = "" if allowed else "未配置审批白名单（设置 → 飞书集成），请在桌面端处理本次授权。"
        if item.get("is_question"):
            return question_card(question=str(item.get("title") or ""), options=list(item.get("options") or []),
                                 session_label=str(item.get("session_label") or ""),
                                 request_id=request_id, buttons_enabled=allowed, hint=hint)
        return permission_card(action_name=str(item.get("action_name") or ""),
                               summary=str(item.get("summary") or ""),
                               targets=list(item.get("targets") or []),
                               session_label=str(item.get("session_label") or ""),
                               workdir=str(item.get("workdir") or ""),
                               request_id=request_id,
                               allow_always=bool(item.get("allow_always")),
                               buttons_enabled=allowed, hint=hint)

    @staticmethod
    def _page_of(arg: str) -> int:
        if arg.startswith("page:"):
            try:
                return max(1, int(arg.split(":", 1)[1]))
            except ValueError:
                return 1
        return 1

    def _all_sessions(self) -> List[Dict[str, Any]]:
        try:
            groups = self._bridge.get_sessions() or []
        except Exception:
            return []
        out: List[Dict[str, Any]] = []
        for group in groups:
            out.extend((group or {}).get("sessions") or [])
        return out

    def _agent_name(self) -> str:
        try:
            from core.persona_settings import get_agent_display_name

            return str(get_agent_display_name() or "星瑶")
        except Exception:
            return "星瑶"

    def _active_model_name(self) -> str:
        try:
            settings = self._bridge.get_model_settings() or {}
            active_id = str(settings.get("active_model_id") or "")
            for item in settings.get("models") or []:
                if str(item.get("id") or "") == active_id:
                    return str(item.get("name") or active_id)
            return active_id
        except Exception:
            return ""

    def _safe_send_card(self, chat_id: str, card: Dict[str, Any]) -> None:
        try:
            self._api_client().send_card(chat_id, card)
        except Exception:
            logger.exception("发送飞书卡片失败（chat=%s）", chat_id)

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

    def _notify_chat(self) -> str:
        """审批卡推送落点：优先配置的固定 chat，其次最后一次说话的会话。"""
        cfg = self._config()
        return cfg.notify_chat_id or self._store.last_chat_id()

    def _approver_allowed(self, open_id: str):
        """审批白名单校验（fail-closed）：未配置白名单时任何人都不放行。"""
        approvers = list(self._config().approvers or [])
        if not approvers:
            return False, "未配置审批白名单（设置 → 飞书集成），暂不可在飞书审批"
        if open_id and open_id in approvers:
            return True, ""
        return False, f"你不在审批白名单内（{open_id or '未知用户'}）"

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
