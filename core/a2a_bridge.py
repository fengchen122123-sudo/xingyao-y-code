from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

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


def _normalize_workdir_value(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val or val.upper() == "GLOBAL" or val == "全局":
        return "GLOBAL"
    try:
        return os.path.normpath(os.path.realpath(os.path.abspath(os.path.expanduser(val))))
    except Exception:
        return os.path.normpath(val)


def _workdir_eq(left: Any, right: Any) -> bool:
    nl = _normalize_workdir_value(left)
    nr = _normalize_workdir_value(right)
    if nl == "GLOBAL" or nr == "GLOBAL":
        return nl == nr
    return os.path.normcase(nl) == os.path.normcase(nr)


def _url_host_label(raw_url: Any) -> str:
    text = str(raw_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except Exception:
        return ""
    if not parsed.scheme or not parsed.hostname:
        return ""
    port_part = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port_part}"


def _format_session_display_name(raw_name: str, source: str = "GUI") -> str:
    name = str(raw_name or "")
    if str(source or "").upper() == "SECRETARY":
        try:
            from core import secretary
            sec_name = secretary.get_secretary_name(name)
            if sec_name:
                return str(sec_name)
        except Exception:
            pass
    if re.match(r"^session_[a-f0-9]{6}$", name):
        return f"新会话 ({name})"
    return name


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

    def _emit_sessions_refresh(self) -> None:
        bridge = self.bridge
        if bridge is None:
            return
        emit_fn = getattr(bridge, "emit_event", None)
        get_sessions = getattr(bridge, "get_sessions", None)
        if callable(emit_fn) and callable(get_sessions):
            try:
                emit_fn("sessions", get_sessions())
            except Exception:
                logger.debug("A2A 推送 sessions 刷新事件失败", exc_info=True)

    def _all_sessions_flat(self) -> list[dict[str, Any]]:
        bridge = self.bridge
        if bridge is None:
            return []
        mem = getattr(bridge, "_memory", None)
        if mem is not None and callable(getattr(mem, "get_all_sessions", None)):
            try:
                rows = mem.get_all_sessions(workdir=None, include_empty=True) or []
                return [dict(r) for r in rows if isinstance(r, dict)]
            except Exception:
                logger.debug("读取 MemoryManager 会话列表失败", exc_info=True)
        get_sessions = getattr(bridge, "get_sessions", None)
        if callable(get_sessions):
            try:
                raw = get_sessions() or []
            except Exception:
                return []
            flat: list[dict[str, Any]] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("sessions"), list):
                    folder_path = str(item.get("folder_path") or "")
                    for child in item["sessions"]:
                        if isinstance(child, dict):
                            entry = dict(child)
                            if not entry.get("workdir") and folder_path:
                                entry["workdir"] = folder_path
                            flat.append(entry)
                elif item.get("name"):
                    flat.append(dict(item))
            return flat
        return []

    def _apply_remote_session_access(
        self,
        session_name: str,
        *,
        permission_mode: str = "full",
        sandbox_bypass: bool = True,
        only_if_unset: bool = False,
    ) -> tuple[str, bool]:
        from core import app_context

        target = str(session_name or "").strip()
        canonical_mode = (
            app_context.normalize_permission_mode(permission_mode)
            if app_context.is_known_permission_mode(permission_mode)
            else "full"
        )
        effective_mode = canonical_mode
        bridge = self.bridge
        mem = getattr(bridge, "_memory", None) if bridge is not None else None
        if target and mem is not None:
            getter = getattr(mem, "get_session_permission_mode", None)
            setter = getattr(mem, "set_session_permission_mode", None)
            existing_mode = None
            if callable(getter):
                try:
                    existing_mode = getter(target)
                except Exception:
                    existing_mode = None
            if existing_mode and only_if_unset:
                effective_mode = str(existing_mode)
            elif callable(setter):
                try:
                    setter(target, canonical_mode)
                    effective_mode = canonical_mode
                except Exception:
                    logger.debug("绑定远程会话权限档位失败", exc_info=True)

        effective_sandbox = bool(sandbox_bypass)
        if target:
            try:
                from core.tools import (
                    is_session_sandbox_bypass_enabled,
                    set_session_sandbox_bypass,
                )

                if only_if_unset:
                    already = bool(is_session_sandbox_bypass_enabled(target))
                    if already:
                        effective_sandbox = True
                    elif effective_mode == "full" and sandbox_bypass:
                        set_session_sandbox_bypass(True, session_name=target)
                        effective_sandbox = True
                    else:
                        effective_sandbox = already
                else:
                    set_session_sandbox_bypass(bool(sandbox_bypass), session_name=target)
                    effective_sandbox = bool(sandbox_bypass)
            except Exception:
                logger.debug("设置远程会话沙盒状态失败", exc_info=True)
        return effective_mode, effective_sandbox

    def _load_model_config(self) -> dict[str, Any]:
        bridge = self.bridge
        if bridge is not None:
            loader = getattr(bridge, "_load_config", None) or getattr(bridge, "load_config", None)
            if callable(loader):
                try:
                    data = loader()
                    if isinstance(data, dict):
                        return data
                except Exception:
                    pass
        try:
            from core.model_manager import load_config
            data = load_config()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _resolve_active_model_for_session(
        self,
        session_name: str | None = None,
        cfg: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        config_data = cfg if isinstance(cfg, dict) else self._load_model_config()
        model_index: dict[str, str] = {}
        for p in config_data.get("providers", []) or []:
            if not isinstance(p, dict):
                continue
            for m in p.get("models", []) or []:
                if isinstance(m, dict) and m.get("id"):
                    model_index[str(m["id"])] = str(m.get("name") or m.get("litellm_model") or m["id"])

        bridge = self.bridge
        target = str(session_name or "").strip()
        if target and bridge is not None:
            mem = getattr(bridge, "_memory", None)
            getter = getattr(mem, "get_session_model_id", None) if mem is not None else None
            if callable(getter):
                try:
                    bound = str(getter(target) or "").strip()
                    if bound:
                        return bound, model_index.get(bound, bound)
                except Exception:
                    pass
        try:
            from core.model_manager import get_active_model
            active = get_active_model(target or None)
            if isinstance(active, dict) and active.get("id"):
                aid = str(active["id"])
                return aid, str(active.get("name") or model_index.get(aid, aid))
        except Exception:
            pass
        global_id = str(config_data.get("active_model_id") or "").strip()
        if global_id:
            return global_id, model_index.get(global_id, global_id)
        return "", ""

    def _build_session_item(self, raw: dict[str, Any], cfg: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        bridge = self.bridge
        mem = getattr(bridge, "_memory", None) if bridge is not None else None
        name = str(raw.get("name") or "")
        source = str(raw.get("source") or "GUI")
        wdir_raw = raw.get("workdir") or "GLOBAL"
        norm_wdir = _normalize_workdir_value(wdir_raw)
        perm_mode = ""
        if mem is not None and callable(getattr(mem, "get_session_permission_mode", None)):
            try:
                perm_mode = str(mem.get_session_permission_mode(name) or "")
            except Exception:
                perm_mode = ""
        if not perm_mode and bridge is not None and callable(getattr(bridge, "_effective_permission_mode", None)):
            try:
                perm_mode = str(bridge._effective_permission_mode(name) or "")
            except Exception:
                perm_mode = ""
        sb_bypass = False
        try:
            from core.tools import is_session_sandbox_bypass_enabled
            sb_bypass = bool(is_session_sandbox_bypass_enabled(name))
        except Exception:
            sb_bypass = False
        model_id, model_name = self._resolve_active_model_for_session(name, cfg=cfg)
        current_session = str(getattr(bridge, "_current_session", "") or "") if bridge is not None else ""
        return {
            "name": name,
            "display_name": _format_session_display_name(name, source=source),
            "workdir": norm_wdir,
            "is_global": norm_wdir == "GLOBAL",
            "count": int(raw.get("count") or 0),
            "updated_at": str(raw.get("updated_at") or ""),
            "created_at": str(raw.get("created_at") or ""),
            "source": source,
            "model_id": model_id,
            "model_name": model_name,
            "permission_mode": perm_mode or "confirm",
            "sandbox_bypass": sb_bypass,
            "is_current": bool(current_session and name == current_session),
        }

    def _ensure_session(self, session_name: str, workdir: str | None) -> None:
        bridge = self.bridge
        if bridge is None:
            return
        try:
            clean_name = str(session_name or "").strip()
            if not clean_name:
                return
            known = {str(s.get("name") or "") for s in self._all_sessions_flat()}
            created = clean_name not in known
            mem = getattr(bridge, "_memory", None)
            if created:
                target_wdir = _normalize_workdir_value(workdir) if workdir else "GLOBAL"
                if mem is not None and callable(getattr(mem, "ensure_session", None)):
                    mem.ensure_session(clean_name, workdir=target_wdir, source="GUI")
                else:
                    create_session = getattr(bridge, "create_session", None)
                    if callable(create_session):
                        kwargs: dict[str, Any] = {"title": clean_name}
                        if workdir:
                            kwargs["workdir"] = workdir
                        create_session(**kwargs)
            elif workdir and mem is not None and callable(getattr(mem, "update_session_workdir", None)):
                # 如果调用方明确传入 workdir，且该会话原本在 GLOBAL 下且暂无明确项目绑定，不强行覆盖已有项目目录；
                # 只有原本为 GLOBAL 且传入具体目录时才归入目标目录。
                current_wdir = ""
                if callable(getattr(mem, "get_session_workdir", None)):
                    try:
                        current_wdir = str(mem.get_session_workdir(clean_name) or "").strip()
                    except Exception:
                        current_wdir = ""
                desired = _normalize_workdir_value(workdir)
                if desired != "GLOBAL" and (not current_wdir or current_wdir == "GLOBAL"):
                    mem.update_session_workdir(clean_name, desired)

            self._apply_remote_session_access(
                clean_name,
                permission_mode="full",
                sandbox_bypass=True,
                only_if_unset=not created,
            )
            if created:
                self._emit_sessions_refresh()
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
        mem = getattr(bridge, "_memory", None) if bridge is not None else None
        perm_mode = "full"
        if mem is not None and callable(getattr(mem, "get_session_permission_mode", None)):
            try:
                bound_perm = str(mem.get_session_permission_mode(session_name) or "").strip()
                if bound_perm:
                    perm_mode = bound_perm
            except Exception:
                pass
        kwargs: dict[str, Any] = {
            "text": text,
            "session_name": session_name,
            "turn_source": "mcp",
            "permission_mode": perm_mode,
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
        active_model_id, active_model_name = self._resolve_active_model_for_session(current_session)
        perm_mode = ""
        if bridge is not None and callable(getattr(bridge, "_effective_permission_mode", None)):
            try:
                perm_mode = str(bridge._effective_permission_mode(current_session) or "")
            except Exception:
                perm_mode = ""
        if not perm_mode:
            perm_mode = "confirm"
        sb_bypass = False
        if current_session:
            try:
                from core.tools import is_session_sandbox_bypass_enabled
                sb_bypass = bool(is_session_sandbox_bypass_enabled(current_session))
            except Exception:
                sb_bypass = False
        ws_res = self.list_workspaces()
        sess_flat = self._all_sessions_flat()
        return {
            "ok": True,
            "online": bridge is not None,
            "agent": "星瑶 (Y-code)",
            "current_session": current_session,
            "active_model_id": active_model_id,
            "active_model_name": active_model_name,
            "permission_mode": perm_mode,
            "sandbox_bypass": sb_bypass,
            "workspace_count": len(ws_res.get("workspaces") or []),
            "session_count": len(sess_flat),
            "active_tasks": active_tasks,
            "active_permission": active_permission,
        }

    def list_workspaces(self) -> dict[str, Any]:
        bridge = self.bridge
        mem = getattr(bridge, "_memory", None) if bridge is not None else None
        raw_sessions = self._all_sessions_flat()
        counts: dict[str, int] = {"GLOBAL": 0}
        ordered_paths: list[str] = ["GLOBAL"]
        seen_norms: dict[str, str] = {"GLOBAL": "GLOBAL"}

        def _register_dir(raw_path: Any, increment: int = 0) -> None:
            norm = _normalize_workdir_value(raw_path)
            key = "GLOBAL" if norm == "GLOBAL" else os.path.normcase(norm)
            if key not in seen_norms:
                seen_norms[key] = norm
                ordered_paths.append(norm)
                counts[norm] = 0
            canon = seen_norms[key]
            counts[canon] = counts.get(canon, 0) + increment

        if mem is not None and callable(getattr(mem, "get_all_workspaces", None)):
            try:
                for ws in mem.get_all_workspaces() or []:
                    _register_dir(ws, 0)
            except Exception:
                logger.debug("读取 workspaces 表失败", exc_info=True)
        for sess in raw_sessions:
            _register_dir(sess.get("workdir") or "GLOBAL", 1)

        items: list[dict[str, Any]] = []
        for path in ordered_paths:
            is_global = path == "GLOBAL"
            items.append({
                "path": path,
                "label": "自由对话 (GLOBAL)" if is_global else (os.path.basename(path) or path),
                "is_global": is_global,
                "session_count": counts.get(path, 0),
            })
        items.sort(key=lambda it: (0 if it["is_global"] else 1, -int(it["session_count"]), it["path"].lower()))
        return {"ok": True, "workspaces": items}

    def list_sessions(
        self,
        workdir: str | None = None,
        include_empty: bool = True,
    ) -> dict[str, Any]:
        cfg = self._load_model_config()
        target_dir = str(workdir or "").strip()
        raw_sessions = self._all_sessions_flat()
        items: list[dict[str, Any]] = []
        for raw in raw_sessions:
            if not include_empty and int(raw.get("count") or 0) <= 0:
                continue
            item = self._build_session_item(raw, cfg=cfg)
            if target_dir and not _workdir_eq(item["workdir"], target_dir):
                continue
            items.append(item)
        items.sort(key=lambda it: (str(it.get("updated_at") or ""), str(it.get("name") or "")), reverse=True)
        return {"ok": True, "sessions": items}

    def list_providers(self) -> dict[str, Any]:
        cfg = self._load_model_config()
        providers_out: list[dict[str, Any]] = []
        for p in cfg.get("providers", []) or []:
            if not isinstance(p, dict):
                continue
            models = [m for m in (p.get("models") or []) if isinstance(m, dict)]
            preview = [str(m.get("name") or m.get("id") or "") for m in models[:5] if (m.get("name") or m.get("id"))]
            providers_out.append({
                "id": str(p.get("id") or ""),
                "name": str(p.get("name") or p.get("id") or ""),
                "protocol": str(p.get("protocol") or "openai"),
                "base_url_host": _url_host_label(p.get("base_url")),
                "model_count": len(models),
                "models_preview": preview,
            })
        return {"ok": True, "providers": providers_out}

    def list_models(
        self,
        provider: str | None = None,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._load_model_config()
        target_session = str(session_name or "").strip()
        active_id, active_name = self._resolve_active_model_for_session(target_session or None, cfg=cfg)
        prov_filter = str(provider or "").strip().lower()
        models_out: list[dict[str, Any]] = []
        for p in cfg.get("providers", []) or []:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "")
            pname = str(p.get("name") or pid)
            if prov_filter and prov_filter not in (pid.lower(), pname.lower()) and prov_filter not in pname.lower():
                continue
            for m in p.get("models", []) or []:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "")
                mname = str(m.get("name") or mid)
                models_out.append({
                    "id": mid,
                    "name": mname,
                    "provider_id": pid,
                    "provider_name": pname,
                    "litellm_model": str(m.get("litellm_model") or ""),
                    "context_window": int(m.get("context_window") or 0),
                    "is_active": bool(active_id and mid == active_id),
                })
        return {
            "ok": True,
            "session_name": target_session,
            "active_model_id": active_id,
            "active_model_name": active_name,
            "models": models_out,
        }

    def create_remote_session(
        self,
        *,
        workdir: str | None = None,
        session_name: str | None = None,
        model_id: str | None = None,
        permission_mode: str = "full",
        sandbox_bypass: bool = True,
    ) -> dict[str, Any]:
        bridge = self.bridge
        if bridge is None:
            return {"ok": False, "msg": "bridge 未初始化"}
        target_wdir = _normalize_workdir_value(workdir)
        if target_wdir != "GLOBAL" and not os.path.isdir(target_wdir):
            return {"ok": False, "msg": f"工作目录不存在: {workdir}"}

        clean_name = str(session_name or "").strip()
        if not clean_name:
            clean_name = f"remote-{uuid.uuid4().hex[:6]}"
        existing_names = {str(s.get("name") or "") for s in self._all_sessions_flat()}
        if clean_name in existing_names:
            return {"ok": False, "msg": f"会话已存在: {clean_name}"}

        mem = getattr(bridge, "_memory", None)
        if mem is not None and callable(getattr(mem, "ensure_session", None)):
            if target_wdir != "GLOBAL" and callable(getattr(mem, "add_workspace", None)):
                try:
                    mem.add_workspace(target_wdir)
                except Exception:
                    pass
            mem.ensure_session(clean_name, workdir=target_wdir, source="GUI")
        else:
            return {"ok": False, "msg": "bridge 不支持在不切换前台的情况下创建会话"}

        self._apply_remote_session_access(
            clean_name,
            permission_mode=permission_mode or "full",
            sandbox_bypass=bool(sandbox_bypass),
            only_if_unset=False,
        )

        clean_model = str(model_id or "").strip()
        if clean_model:
            model_res = self.select_remote_model(clean_model, session_name=clean_name)
            if not model_res.get("ok"):
                return {
                    "ok": False,
                    "msg": f"会话已创建但设置模型失败: {model_res.get('msg')}",
                }

        self._emit_sessions_refresh()
        cfg = self._load_model_config()
        created_row = next(
            (r for r in self._all_sessions_flat() if str(r.get("name") or "") == clean_name),
            {"name": clean_name, "workdir": target_wdir, "count": 0, "source": "GUI"},
        )
        return {"ok": True, "session": self._build_session_item(created_row, cfg=cfg)}

    def select_remote_session(
        self,
        session_name: str,
        *,
        permission_mode: str = "full",
        sandbox_bypass: bool = True,
    ) -> dict[str, Any]:
        query = str(session_name or "").strip()
        if not query:
            return {"ok": False, "msg": "session_name 不能为空"}
        raw_sessions = self._all_sessions_flat()
        exact = [s for s in raw_sessions if str(s.get("name") or "") == query]
        matched: Optional[dict[str, Any]] = None
        if exact:
            matched = exact[0]
        else:
            disp_matches = [
                s
                for s in raw_sessions
                if _format_session_display_name(
                    str(s.get("name") or ""), str(s.get("source") or "GUI")
                ).lower()
                == query.lower()
            ]
            if len(disp_matches) == 1:
                matched = disp_matches[0]
            elif len(disp_matches) > 1:
                names = ", ".join(str(s.get("name") or "") for s in disp_matches[:5])
                return {"ok": False, "msg": f"命中多个对话，请指定更精确的会话名: {names}"}
            else:
                q_low = query.lower()
                partial = [
                    s
                    for s in raw_sessions
                    if q_low in str(s.get("name") or "").lower()
                    or q_low
                    in _format_session_display_name(
                        str(s.get("name") or ""), str(s.get("source") or "GUI")
                    ).lower()
                ]
                if len(partial) == 1:
                    matched = partial[0]
                elif len(partial) > 1:
                    names = ", ".join(str(s.get("name") or "") for s in partial[:5])
                    return {"ok": False, "msg": f"命中多个候选对话，请使用更精确的名称: {names}"}

        if matched is None:
            return {"ok": False, "msg": f"未找到对话: {query}"}

        target_name = str(matched.get("name") or "")
        self._apply_remote_session_access(
            target_name,
            permission_mode=permission_mode or "full",
            sandbox_bypass=bool(sandbox_bypass),
            only_if_unset=False,
        )
        cfg = self._load_model_config()
        return {"ok": True, "session": self._build_session_item(matched, cfg=cfg)}

    def select_remote_model(
        self,
        model: str,
        *,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        query = str(model or "").strip()
        if not query:
            return {"ok": False, "msg": "model 不能为空"}
        cfg = self._load_model_config()
        candidates: list[dict[str, str]] = []
        for p in cfg.get("providers", []) or []:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or p.get("id") or "")
            pid = str(p.get("id") or "")
            for m in p.get("models", []) or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                candidates.append({
                    "id": str(m.get("id") or ""),
                    "name": str(m.get("name") or m.get("id") or ""),
                    "litellm_model": str(m.get("litellm_model") or ""),
                    "provider_id": pid,
                    "provider_name": pname,
                })

        q_low = query.lower()
        chosen: Optional[dict[str, str]] = None
        for key in ("id",):
            hits = [c for c in candidates if c[key] == query]
            if hits:
                chosen = hits[0]
                break
        if chosen is None:
            hits = [c for c in candidates if c["name"].lower() == q_low or c["litellm_model"].lower() == q_low]
            if len(hits) == 1:
                chosen = hits[0]
            elif len(hits) > 1:
                opts = ", ".join(f"{h['name']}({h['provider_name']}:{h['id']})" for h in hits[:5])
                return {"ok": False, "msg": f"命中多个同名模型，请改用模型 ID 指定: {opts}"}
        if chosen is None:
            hits = [
                c
                for c in candidates
                if q_low in c["name"].lower()
                or q_low in c["id"].lower()
                or q_low in c["litellm_model"].lower()
            ]
            if len(hits) == 1:
                chosen = hits[0]
            elif len(hits) > 1:
                opts = ", ".join(f"{h['name']}({h['id']})" for h in hits[:5])
                return {"ok": False, "msg": f"命中多个候选模型，请进一步指定: {opts}"}

        if chosen is None:
            return {"ok": False, "msg": f"未找到模型: {query}"}

        target_session = str(session_name or "").strip()
        bridge = self.bridge
        if bridge is not None and callable(getattr(bridge, "set_model", None)):
            res = bridge.set_model(chosen["id"], session_name=target_session or None)
            if isinstance(res, dict) and not res.get("ok"):
                return {"ok": False, "msg": str(res.get("msg") or "切换模型失败")}
        else:
            mem = getattr(bridge, "_memory", None) if bridge is not None else None
            if target_session and mem is not None and callable(getattr(mem, "set_session_model_id", None)):
                mem.set_session_model_id(target_session, chosen["id"])
            else:
                try:
                    from core.model_manager import set_active_model
                    set_active_model(chosen["id"])
                except Exception as exc:
                    return {"ok": False, "msg": f"切换模型失败: {exc}"}

        return {
            "ok": True,
            "session_name": target_session,
            "active_model_id": chosen["id"],
            "active_model_name": chosen["name"],
            "provider_id": chosen["provider_id"],
            "provider_name": chosen["provider_name"],
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

        if method == "xingyao/status":
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": self.get_status()}

        if method == "xingyao/workspaces":
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": self.list_workspaces()}

        if method == "xingyao/sessions":
            include_empty = bool(params.get("include_empty", True))
            return 200, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": self.list_sessions(
                    workdir=params.get("workdir"),
                    include_empty=include_empty,
                ),
            }

        if method == "xingyao/providers":
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": self.list_providers()}

        if method == "xingyao/models":
            return 200, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": self.list_models(
                    provider=params.get("provider"),
                    session_name=params.get("session_name"),
                ),
            }

        if method == "xingyao/session_create":
            res = self.create_remote_session(
                workdir=params.get("workdir"),
                session_name=params.get("session_name"),
                model_id=params.get("model_id"),
                permission_mode=str(params.get("permission_mode") or "full"),
                sandbox_bypass=bool(params.get("sandbox_bypass", True)),
            )
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": res}

        if method == "xingyao/session_select":
            res = self.select_remote_session(
                str(params.get("session_name") or ""),
                permission_mode=str(params.get("permission_mode") or "full"),
                sandbox_bypass=bool(params.get("sandbox_bypass", True)),
            )
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": res}

        if method == "xingyao/model_select":
            res = self.select_remote_model(
                str(params.get("model") or params.get("model_id") or ""),
                session_name=params.get("session_name"),
            )
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": res}

        return 200, {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
