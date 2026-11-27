from __future__ import annotations

import inspect
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 20
MAX_WAIT_SECONDS = 60
MAX_TASKS_RETAINED = 200
MAX_PEER_HOPS = 1

_SAFE_SESSION_RE = re.compile(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+")
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp_wait_seconds(raw: Any, default: int = DEFAULT_WAIT_SECONDS) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_WAIT_SECONDS, val))


def _sanitize_session_suffix(raw: str) -> str:
    cleaned = _SAFE_SESSION_RE.sub("-", str(raw or "").strip()).strip("-")
    return cleaned[:40] if cleaned else "remote"


def _extract_text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, dict):
        return ""
    parts = message.get("parts")
    if isinstance(parts, list):
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict):
                kind = str(part.get("kind") or part.get("type") or "").strip().lower()
                if kind in ("", "text"):
                    val = str(part.get("text") or "").strip()
                    if val:
                        texts.append(val)
            elif isinstance(part, str) and part.strip():
                texts.append(part.strip())
        if texts:
            return "\n".join(texts)
    return str(message.get("text") or "").strip()


def _sanitize_permission_payload(perm: Any) -> dict[str, Any] | None:
    if not isinstance(perm, dict) or not perm:
        return None
    out: dict[str, Any] = {
        "request_id": str(perm.get("request_id") or ""),
        "tool_name": str(perm.get("tool_name") or perm.get("tool") or ""),
        "description": str(perm.get("description") or perm.get("summary") or perm.get("message") or ""),
        "session_name": str(perm.get("session_name") or ""),
    }
    if isinstance(perm.get("options"), list):
        out["options"] = list(perm["options"])
    return out


@dataclass
class A2ATask:
    id: str
    context_id: str
    session_name: str
    status: str = "submitted"
    reply: str = ""
    error: str = ""
    permission: Optional[dict[str, Any]] = None
    progress: Optional[dict[str, Any]] = None
    hop: int = 0
    caller: str = ""
    created_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    baseline_msg_id: Optional[int] = None

    def to_a2a_dict(self) -> dict[str, Any]:
        text = self.reply
        if self.status == "failed" and not text:
            text = self.error or "执行失败"
        elif self.status == "canceled" and not text:
            text = self.error or "任务已取消"
        elif self.status == "input-required" and not text:
            desc = (self.permission or {}).get("description") or ""
            tool_name = (self.permission or {}).get("tool_name") or ""
            req_id = (self.permission or {}).get("request_id") or ""
            text = f"需要确认权限（request_id={req_id}）{f' [{tool_name}]' if tool_name else ''}: {desc}".strip()
        elif self.status in ("submitted", "working") and not text:
            text = "任务正在执行中"

        message = {
            "role": "agent",
            "parts": [{"kind": "text", "text": text}],
        }
        artifacts: list[dict[str, Any]] = []
        if self.reply:
            artifacts.append(
                {
                    "artifactId": f"art-{self.id}",
                    "parts": [{"kind": "text", "text": self.reply}],
                }
            )
        metadata: dict[str, Any] = {
            "session_name": self.session_name,
            "hop": self.hop,
        }
        if self.permission:
            metadata["permission"] = dict(self.permission)
        if self.progress:
            metadata["progress"] = dict(self.progress)
        if self.error:
            metadata["error"] = self.error

        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": {
                "state": self.status,
                "timestamp": self.updated_at,
                "message": message,
            },
            "artifacts": artifacts,
            "metadata": metadata,
        }


class A2ABridge:
    """将本地星瑶 Bridge 暴露为 A2A 任务执行器与 JSON-RPC / REST 适配层。"""

    def __init__(self, bridge: Any = None):
        self.bridge = bridge
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._tasks: dict[str, A2ATask] = {}
        self._task_order: list[str] = []

    def _register_task(self, task: A2ATask) -> None:
        with self._cond:
            self._tasks[task.id] = task
            self._task_order.append(task.id)
            while len(self._task_order) > MAX_TASKS_RETAINED:
                oldest = self._task_order.pop(0)
                if oldest != task.id:
                    self._tasks.pop(oldest, None)
            self._cond.notify_all()

    def _find_active_task_for_session_locked(self, session_name: str) -> Optional[A2ATask]:
        for tid in reversed(self._task_order):
            t = self._tasks.get(tid)
            if t is None:
                continue
            if t.session_name == session_name and t.status not in _TERMINAL_STATES:
                return t
        return None

    def on_event(self, event_type: str, data: Any) -> None:
        """接收 gui_bridge.emit_event 推送事件并更新对应 A2A 任务状态（绝不抛出异常）。"""
        try:
            if not isinstance(data, dict):
                return
            session_name = str(data.get("session_name") or "").strip()
            with self._cond:
                task: Optional[A2ATask] = None
                if session_name:
                    task = self._find_active_task_for_session_locked(session_name)
                if task is None and event_type == "permission_dismiss":
                    req_id = str(data.get("request_id") or "").strip()
                    if req_id:
                        for tid in reversed(self._task_order):
                            cand = self._tasks.get(tid)
                            if cand and (cand.permission or {}).get("request_id") == req_id:
                                task = cand
                                break
                if task is None:
                    return

                now = _iso_now()
                if event_type == "turn_start":
                    if task.status == "submitted":
                        task.status = "working"
                        task.updated_at = now
                        self._cond.notify_all()
                elif event_type == "message_final":
                    role = str(data.get("role") or "").strip().lower()
                    content = str(data.get("content") or data.get("text") or "").strip()
                    if content and role in ("", "assistant"):
                        task.reply = content
                        task.updated_at = now
                        self._cond.notify_all()
                elif event_type == "turn_finish":
                    content = str(data.get("content") or data.get("reply") or "").strip()
                    if content:
                        task.reply = content
                    task.status = "completed"
                    task.permission = None
                    task.progress = None
                    task.updated_at = now
                    self._cond.notify_all()
                elif event_type == "turn_error":
                    task.error = str(data.get("error") or data.get("msg") or data.get("message") or "执行失败")
                    task.status = "failed"
                    task.permission = None
                    task.updated_at = now
                    self._cond.notify_all()
                elif event_type == "turn_cancelled":
                    task.status = "canceled"
                    task.permission = None
                    task.updated_at = now
                    self._cond.notify_all()
                elif event_type == "permission_request":
                    task.permission = _sanitize_permission_payload(data)
                    task.status = "input-required"
                    task.updated_at = now
                    self._cond.notify_all()
                elif event_type == "permission_dismiss":
                    req_id = str(data.get("request_id") or "").strip()
                    cur_req = str((task.permission or {}).get("request_id") or "").strip()
                    if not req_id or not cur_req or req_id == cur_req:
                        task.permission = None
                        if task.status == "input-required":
                            task.status = "working"
                        task.updated_at = now
                        self._cond.notify_all()
        except Exception:
            logger.debug("A2ABridge.on_event 处理异常（已忽略）", exc_info=True)

    def _ensure_session(self, session_name: str, workdir: str | None) -> None:
        bridge = self.bridge
        if bridge is None:
            return
        try:
            get_sessions = getattr(bridge, "get_sessions", None)
            create_session = getattr(bridge, "create_session", None)
            if callable(get_sessions) and callable(create_session):
                existing = get_sessions() or []
                known = {str(s.get("name") or "") for s in existing if isinstance(s, dict)}
                if session_name not in known:
                    kwargs: dict[str, Any] = {"title": session_name}
                    if workdir:
                        kwargs["workdir"] = workdir
                    create_session(**kwargs)
        except Exception:
            logger.debug("A2A 预建隔离会话失败（将直接投递消息）", exc_info=True)

    def _call_send_user_message(
        self,
        text: str,
        session_name: str,
        workdir: str | None,
    ) -> Any:
        bridge = self.bridge
        fn = getattr(bridge, "send_user_message", None)
        if not callable(fn):
            raise RuntimeError("bridge 不支持 send_user_message")
        kwargs: dict[str, Any] = {
            "text": text,
            "session_name": session_name,
            "turn_source": "mcp",
        }
        if workdir:
            kwargs["workdir"] = workdir
        try:
            sig = inspect.signature(fn)
            has_varkw = any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
            )
            if not has_varkw:
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        return fn(**kwargs)

    def _last_message_id(self, session_name: str) -> Optional[int]:
        bridge = self.bridge
        if bridge is None:
            return None
        mem = getattr(bridge, "_memory", None)
        if mem is not None and callable(getattr(mem, "get_last_message_id", None)):
            try:
                return mem.get_last_message_id(session_name)
            except Exception:
                return None
        return None

    def _fetch_latest_assistant_reply(self, session_name: str, baseline_msg_id: Optional[int] = None) -> str:
        bridge = self.bridge
        if bridge is None:
            return ""
        rows: list[dict[str, Any]] = []
        try:
            get_msgs = getattr(bridge, "get_messages", None)
            if callable(get_msgs):
                res = get_msgs(session_name)
                if isinstance(res, dict) and isinstance(res.get("messages"), list):
                    rows = res["messages"]
                elif isinstance(res, list):
                    rows = res
        except Exception:
            rows = []

        if not rows:
            load_msgs = getattr(bridge, "_load_session_messages", None)
            if callable(load_msgs):
                try:
                    loaded, _, _ = load_msgs(session_name)
                    if isinstance(loaded, list):
                        rows = loaded
                except Exception:
                    rows = []

        if not rows:
            mem = getattr(bridge, "_memory", None)
            if mem is not None and callable(getattr(mem, "get_history_paged", None)):
                try:
                    paged, _ = mem.get_history_paged(session_name, limit=20)
                    if isinstance(paged, list):
                        rows = paged
                except Exception:
                    rows = []

        for msg in reversed(rows):
            if not isinstance(msg, dict):
                continue
            if str(msg.get("role") or "").strip().lower() != "assistant":
                continue
            msg_id = msg.get("_msg_id")
            if baseline_msg_id is not None and isinstance(msg_id, int) and msg_id <= baseline_msg_id:
                continue
            content = str(msg.get("content") or "").strip()
            if content:
                return content
        return ""

    def _poll_bridge_state(self, task: A2ATask, saw_running: bool) -> bool:
        """从 bridge 查询活动权限与快照作为事件兜底，返回是否观测到正在运行。"""
        bridge = self.bridge
        if bridge is None:
            return saw_running
        try:
            get_perm = getattr(bridge, "get_active_permission", None)
            if callable(get_perm):
                perm_res = get_perm()
                perm = perm_res.get("permission") if isinstance(perm_res, dict) else None
                if isinstance(perm, dict):
                    perm_sess = str(perm.get("session_name") or "").strip()
                    if not perm_sess or perm_sess == task.session_name:
                        with self._cond:
                            if task.status not in _TERMINAL_STATES:
                                task.permission = _sanitize_permission_payload(perm)
                                task.status = "input-required"
                                task.updated_at = _iso_now()
                                self._cond.notify_all()
        except Exception:
            pass

        get_snap = getattr(bridge, "get_active_turn_snapshot", None)
        if callable(get_snap):
            try:
                snap = get_snap(task.session_name)
            except Exception:
                snap = None
            if isinstance(snap, dict):
                saw_running = True
                with self._cond:
                    if task.status == "submitted":
                        task.status = "working"
                    if task.status == "working":
                        tool_calls = snap.get("tool_calls") or []
                        task.progress = {
                            "tool_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
                            "streaming_chars": len(str(snap.get("streaming_text") or "")),
                        }
                        task.updated_at = _iso_now()
            elif saw_running:
                # 曾观测到快照运行中，现在快照消失，检查是否有新回复可作为完成兜底
                with self._cond:
                    cur_status = task.status
                if cur_status in ("submitted", "working"):
                    reply = self._fetch_latest_assistant_reply(task.session_name, task.baseline_msg_id)
                    if reply:
                        with self._cond:
                            if task.status in ("submitted", "working"):
                                if not task.reply:
                                    task.reply = reply
                                task.status = "completed"
                                task.progress = None
                                task.updated_at = _iso_now()
                                self._cond.notify_all()
        return saw_running

    def _wait_task_until(self, task: A2ATask, wait_seconds: int) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, int(wait_seconds))
        saw_running = False
        while True:
            with self._cond:
                if task.status in _TERMINAL_STATES or task.status == "input-required":
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=min(0.25, remaining))
            saw_running = self._poll_bridge_state(task, saw_running)

        with self._cond:
            needs_reply_fetch = task.status == "completed" and not task.reply
            session_name = task.session_name
            baseline = task.baseline_msg_id
        if needs_reply_fetch:
            fetched = self._fetch_latest_assistant_reply(session_name, baseline)
            if fetched:
                with self._cond:
                    if not task.reply:
                        task.reply = fetched
        with self._cond:
            return task.to_a2a_dict()

    def send_message(
        self,
        text: str,
        *,
        session_name: str | None = None,
        workdir: str | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
        context_id: str | None = None,
        hop: int = 0,
        caller: str = "",
    ) -> dict[str, Any]:
        clean_text = str(text or "").strip()
        clean_context = str(context_id or "").strip() or f"ctx-{uuid.uuid4().hex[:12]}"
        try:
            hop_num = int(hop or 0)
        except (TypeError, ValueError):
            hop_num = 0
        clean_caller = str(caller or "").strip()
        clean_session = str(session_name or "").strip()
        if not clean_session:
            clean_session = f"peer-{_sanitize_session_suffix(clean_caller)}" if clean_caller else "a2a-remote"
        clean_workdir = str(workdir or "").strip() or None
        wait = _clamp_wait_seconds(wait_seconds, DEFAULT_WAIT_SECONDS)

        if hop_num >= MAX_PEER_HOPS:
            task = A2ATask(
                id=f"task-{uuid.uuid4().hex[:12]}",
                context_id=clean_context,
                session_name=clean_session,
                status="failed",
                error=f"已达跨机派发跳数上限（max_hops={MAX_PEER_HOPS}），禁止循环委派",
                hop=hop_num,
                caller=clean_caller,
            )
            self._register_task(task)
            return task.to_a2a_dict()

        if not clean_text:
            task = A2ATask(
                id=f"task-{uuid.uuid4().hex[:12]}",
                context_id=clean_context,
                session_name=clean_session,
                status="failed",
                error="text 不能为空",
                hop=hop_num,
                caller=clean_caller,
            )
            self._register_task(task)
            return task.to_a2a_dict()

        if self.bridge is None:
            task = A2ATask(
                id=f"task-{uuid.uuid4().hex[:12]}",
                context_id=clean_context,
                session_name=clean_session,
                status="failed",
                error="bridge 未初始化，无法投递任务",
                hop=hop_num,
                caller=clean_caller,
            )
            self._register_task(task)
            return task.to_a2a_dict()

        self._ensure_session(clean_session, clean_workdir)
        baseline_msg_id = self._last_message_id(clean_session)

        task = A2ATask(
            id=f"task-{uuid.uuid4().hex[:12]}",
            context_id=clean_context,
            session_name=clean_session,
            status="working",
            hop=hop_num,
            caller=clean_caller,
            baseline_msg_id=baseline_msg_id,
        )
        self._register_task(task)

        try:
            res = self._call_send_user_message(clean_text, clean_session, clean_workdir)
        except Exception as exc:
            logger.exception("A2A send_user_message 投递失败")
            with self._cond:
                task.status = "failed"
                task.error = f"发送失败：{exc}"
                task.updated_at = _iso_now()
                self._cond.notify_all()
                return task.to_a2a_dict()

        if not isinstance(res, dict) or not res.get("ok"):
            msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
            with self._cond:
                task.status = "failed"
                task.error = f"发送失败：{msg}" if msg else "发送失败：星瑶未受理该任务"
                task.updated_at = _iso_now()
                self._cond.notify_all()
                return task.to_a2a_dict()

        if wait <= 0:
            with self._cond:
                return task.to_a2a_dict()

        return self._wait_task_until(task, wait)

    def get_task(self, task_id: str, *, wait_seconds: int = 0) -> Optional[dict[str, Any]]:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return None
        with self._cond:
            task = self._tasks.get(clean_id)
        if task is None:
            return None
        wait = _clamp_wait_seconds(wait_seconds, 0)
        if wait <= 0:
            with self._cond:
                needs_reply = task.status == "completed" and not task.reply
                sess = task.session_name
                base = task.baseline_msg_id
            if needs_reply:
                fetched = self._fetch_latest_assistant_reply(sess, base)
                if fetched:
                    with self._cond:
                        if not task.reply:
                            task.reply = fetched
            with self._cond:
                return task.to_a2a_dict()
        return self._wait_task_until(task, wait)

    def cancel_task(self, task_id: str) -> Optional[dict[str, Any]]:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return None
        with self._cond:
            task = self._tasks.get(clean_id)
            if task is None:
                return None
            session_name = task.session_name
        bridge = self.bridge
        if bridge is not None:
            for attr in ("stop_current_turn", "stop_generation"):
                fn = getattr(bridge, attr, None)
                if callable(fn):
                    try:
                        sig = inspect.signature(fn)
                        if sig.parameters:
                            fn(session_name)
                        else:
                            fn()
                    except Exception:
                        logger.debug("A2A cancel_task 调用 %s 失败", attr, exc_info=True)
                    break
        with self._cond:
            if task.status not in _TERMINAL_STATES:
                task.status = "canceled"
                task.permission = None
                task.updated_at = _iso_now()
                self._cond.notify_all()
            return task.to_a2a_dict()

    def respond_permission(
        self,
        request_id: str,
        allow: bool,
        always: bool = False,
        *,
        task_id: str | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        req_id = str(request_id or "").strip()
        if not req_id:
            return {"ok": False, "msg": "request_id 不能为空"}
        bridge = self.bridge
        if bridge is None:
            return {"ok": False, "msg": "bridge 未初始化"}
        fn = getattr(bridge, "respond_permission", None)
        if not callable(fn):
            return {"ok": False, "msg": "bridge 不支持 respond_permission"}
        try:
            res = fn(req_id, bool(allow), bool(always))
        except Exception as exc:
            logger.exception("A2A respond_permission 调用失败")
            return {"ok": False, "msg": str(exc)}
        if isinstance(res, dict) and not res.get("ok", True):
            return {"ok": False, "msg": str(res.get("msg") or "权限提交失败")}

        target_task: Optional[A2ATask] = None
        clean_tid = str(task_id or "").strip()
        with self._cond:
            if clean_tid:
                target_task = self._tasks.get(clean_tid)
            if target_task is None:
                for tid in reversed(self._task_order):
                    cand = self._tasks.get(tid)
                    if cand and (cand.permission or {}).get("request_id") == req_id:
                        target_task = cand
                        break
            if target_task is not None and target_task.status == "input-required":
                target_task.permission = None
                target_task.status = "working" if allow else "canceled"
                if not allow:
                    target_task.error = "用户拒绝了权限请求"
                target_task.updated_at = _iso_now()
                self._cond.notify_all()

        out: dict[str, Any] = {"ok": True, "request_id": req_id, "allow": bool(allow)}
        if target_task is not None:
            wait = _clamp_wait_seconds(wait_seconds, DEFAULT_WAIT_SECONDS) if allow else 0
            task_dict = self._wait_task_until(target_task, wait) if wait > 0 else target_task.to_a2a_dict()
            out["task"] = task_dict
        return out

    def get_status(self) -> dict[str, Any]:
        bridge = self.bridge
        current_session = ""
        if bridge is not None:
            current_session = str(getattr(bridge, "_current_session", "") or "")
        active_permission = None
        if bridge is not None and callable(getattr(bridge, "get_active_permission", None)):
            try:
                perm_res = bridge.get_active_permission()
                if isinstance(perm_res, dict):
                    active_permission = _sanitize_permission_payload(perm_res.get("permission"))
            except Exception:
                active_permission = None
        with self._cond:
            active_tasks = sum(
                1 for t in self._tasks.values() if t.status not in _TERMINAL_STATES
            )
        return {
            "ok": True,
            "online": bridge is not None,
            "agent": "星瑶 (Y-code)",
            "current_session": current_session,
            "active_tasks": active_tasks,
            "active_permission": active_permission,
        }

    def build_agent_card(self, base_url: str, ws_url: str = "") -> dict[str, Any]:
        try:
            from core.version import __version__ as version
        except Exception:
            version = "1.0.0"
        clean_base = str(base_url or "").rstrip("/")
        card: dict[str, Any] = {
            "name": "星瑶 (Y-code)",
            "description": "星瑶本地智能体：支持跨机任务委派、代码编辑、命令执行与权限确认闭环。",
            "url": f"{clean_base}/a2a" if clean_base else "/a2a",
            "version": str(version),
            "capabilities": {
                "streaming": False,
                "pushNotifications": False,
                "stateTransitionHistory": False,
            },
            "authentication": {
                "schemes": ["Bearer"],
            },
            "defaultInputModes": ["text"],
            "defaultOutputModes": ["text"],
            "skills": [
                {
                    "id": "general-agent",
                    "name": "星瑶通用智能体",
                    "description": "在当前主机工作区执行自然语言编程、检索、命令运行与日常任务。",
                    "tags": ["coding", "local-agent", "xingyao"],
                }
            ],
        }
        if ws_url:
            card["x-xingyao-ws"] = str(ws_url)
        return card

    def handle_jsonrpc(self, payload: Any) -> tuple[int, Optional[dict[str, Any]]]:
        if not isinstance(payload, dict):
            return 400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        method = payload.get("method")
        msg_id = payload.get("id")
        if not isinstance(method, str) or not method:
            return 400, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32600, "message": "invalid request"}}
        if "id" not in payload:
            return 202, None

        params = payload.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return 200, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32602, "message": "invalid params: params must be object"},
            }

        if method == "message/send":
            meta = params.get("metadata") if isinstance(params.get("metadata"), dict) else {}
            text = _extract_text_from_message(params.get("message")) or str(params.get("text") or "").strip()
            task = self.send_message(
                text,
                session_name=meta.get("session_name") or params.get("session_name"),
                workdir=meta.get("workdir") or params.get("workdir"),
                wait_seconds=meta.get("wait_seconds", params.get("wait_seconds", DEFAULT_WAIT_SECONDS)),
                context_id=params.get("contextId") or meta.get("contextId"),
                hop=meta.get("hop", 0),
                caller=meta.get("caller", ""),
            )
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": task}

        if method == "tasks/get":
            task_id = str(params.get("id") or params.get("task_id") or "").strip()
            if not task_id:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "invalid params: id is required"},
                }
            task = self.get_task(task_id, wait_seconds=params.get("wait_seconds", 0))
            if task is None:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32001, "message": f"task not found: {task_id}"},
                }
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": task}

        if method == "tasks/cancel":
            task_id = str(params.get("id") or params.get("task_id") or "").strip()
            if not task_id:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "invalid params: id is required"},
                }
            task = self.cancel_task(task_id)
            if task is None:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32001, "message": f"task not found: {task_id}"},
                }
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": task}

        if method == "xingyao/permission":
            req_id = str(params.get("request_id") or "").strip()
            if not req_id:
                return 200, {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32602, "message": "invalid params: request_id is required"},
                }
            res = self.respond_permission(
                req_id,
                bool(params.get("allow", False)),
                bool(params.get("always", False)),
                task_id=params.get("task_id"),
                wait_seconds=params.get("wait_seconds", DEFAULT_WAIT_SECONDS),
            )
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": res}

        return 200, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
