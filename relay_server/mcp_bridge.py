from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from .registry import RelayRegistry

logger = logging.getLogger(__name__)

DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY_BYTES = 1024 * 1024
RPC_TIMEOUT_SECONDS = 15.0
DEFAULT_WAIT_SECONDS = 20
MAX_WAIT_SECONDS = 60
_PARTIAL_TEXT_MAX_CHARS = 16000
_SQLITE_MAX_ID = 2_147_483_647


class DeviceOfflineError(RuntimeError):
    """桌面端星瑶当前未连接到中转服务器."""


class RpcTimeoutError(RuntimeError):
    """等待桌面端 RPC 应答超时."""


class RpcCallError(RuntimeError):
    """桌面端 RPC 返回了错误."""


def _jsonrpc_result(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _jsonrpc_error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": int(code), "message": str(message)},
    }


def _tool_ok(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(text)}], "isError": False}


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(text)}], "isError": True}


def _bearer_token(authorization: str | None) -> str:
    parts = str(authorization or "").split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _clamp_wait_seconds(raw: Any, default: int = DEFAULT_WAIT_SECONDS) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_WAIT_SECONDS, val))


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "send_message",
            "description": (
                "把一条中文自然语言任务发送给电脑上的星瑶（Y-code）智能体执行。"
                "默认最多等待 20 秒查看是否完成并直接返回星瑶的回复；"
                "如果任务耗时较长仍在运行，会返回当前状态（或等待审批的权限请求），"
                "之后可调用 get_reply 继续查看结果。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "要交给星瑶执行的自然语言任务或指令",
                    },
                    "session_name": {
                        "type": "string",
                        "description": "可选，指定星瑶会话名；留空则使用桌面当前会话",
                    },
                    "workdir": {
                        "type": "string",
                        "description": "可选，指定电脑上的工作目录路径",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "可选，指定本次使用的模型 ID（可先通过 list_models 获取）",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "可选，最多等待结果的秒数（0~60，默认 20；传 0 表示只投递不等待）",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "get_reply",
            "description": (
                "查询电脑上星瑶指定会话（或当前会话）的最新回复与运行状态。"
                "若任务仍在执行中，可传 wait_seconds 等待其完成。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "可选，会话名；留空则查询当前或最近更新的会话",
                    },
                    "wait_seconds": {
                        "type": "integer",
                        "description": "可选，若正在运行最多再等待几秒（0~60，默认 0）",
                    },
                },
            },
        },
        {
            "name": "get_status",
            "description": (
                "查看电脑上星瑶是否在线、当前会话、正在执行的回合快照（含工具调用），"
                "以及是否有等待用户确认的权限请求。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_models",
            "description": (
                "查看电脑上星瑶已配置的模型厂商、各厂商下可用的模型列表（含 model_id 与能力），"
                "以及当前激活的模型。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "switch_model",
            "description": (
                "切换电脑上星瑶当前会话（或指定会话）使用的模型。"
                "可直接传 model_id，或传 model_name（可选配 provider_name）按名称匹配。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "model_id": {
                        "type": "string",
                        "description": "目标模型 ID（来自 list_models）",
                    },
                    "model_name": {
                        "type": "string",
                        "description": "可选，当不知道 model_id 时按模型显示名匹配",
                    },
                    "provider_name": {
                        "type": "string",
                        "description": "可选，配合 model_name 指定厂商名称以消歧",
                    },
                    "session_name": {
                        "type": "string",
                        "description": "可选，指定要切换模型的会话；留空则切换桌面当前会话",
                    },
                },
            },
        },
        {
            "name": "list_workspaces",
            "description": (
                "查看电脑上星瑶当前拥有的所有工作目录分组，以及每个工作目录下的对话数量和当前所在工作目录。"
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_sessions",
            "description": (
                "列出电脑上星瑶的会话列表（按工作目录分组，包含 session_name、名称、工作目录、消息数、运行状态）。"
                "可传入 workdir 只查看某个工作目录下的对话。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workdir": {
                        "type": "string",
                        "description": "可选，按工作目录路径或分组名筛选会话",
                    }
                },
            },
        },
        {
            "name": "switch_session",
            "description": "将电脑上的星瑶切换到指定会话（可同时切换工作目录）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "目标会话的 session_name（来自 list_sessions）",
                    },
                    "target_workdir": {
                        "type": "string",
                        "description": "可选，目标工作目录路径",
                    },
                },
                "required": ["session_name"],
            },
        },
        {
            "name": "create_session",
            "description": "在指定工作目录（或自由对话）下新建一个星瑶会话并切换过去。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "workdir": {
                        "type": "string",
                        "description": "可选，工作目录路径（或 'GLOBAL' 表示自由对话）",
                    },
                    "title": {
                        "type": "string",
                        "description": "可选，会话标题（默认 '新会话'）",
                    },
                },
            },
        },
        {
            "name": "stop_task",
            "description": "停止电脑上星瑶当前正在执行的任务。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_name": {
                        "type": "string",
                        "description": "可选，要停止的会话名；留空则停止当前会话",
                    }
                },
            },
        },
        {
            "name": "respond_permission",
            "description": (
                "响应电脑上星瑶发起的权限确认请求（当 send_message 或 get_status "
                "提示有待确认的 request_id 时调用）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "request_id": {
                        "type": "string",
                        "description": "待处理的权限请求 ID",
                    },
                    "allow": {
                        "type": "boolean",
                        "description": "是否同意执行（true=允许，false=拒绝）",
                    },
                    "always": {
                        "type": "boolean",
                        "description": "可选，是否在本任务内始终允许（默认 false）",
                    },
                },
                "required": ["request_id", "allow"],
            },
        },
    ]


class RelayDeviceTracker:
    """订阅中转广播事件，实时缓存设备各会话的运行态、最终回复与待审批权限."""

    def __init__(self, relay: RelayRegistry):
        self.relay = relay
        self._lock = threading.Lock()
        self._devices: dict[str, dict[str, Any]] = {}
        self._waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def _ensure_state(self, device_id: str) -> dict[str, Any]:
        state = self._devices.get(device_id)
        if state is None:
            state = {
                "seq": 0,
                "active_turns": {},
                "latest_replies": {},
                "last_reply_session": "",
                "last_started_session": "",
                "pending_permission": None,
            }
            self._devices[device_id] = state
        return state

    def seq(self, device_id: str) -> int:
        with self._lock:
            return int(self._ensure_state(device_id)["seq"])

    def snapshot(self, device_id: str) -> dict[str, Any]:
        with self._lock:
            st = self._ensure_state(device_id)
            return {
                "seq": st["seq"],
                "active_turns": {
                    k: {
                        "started_at": v["started_at"],
                        "started_seq": v["started_seq"],
                        "partial_text": "".join(v["text_chunks"]),
                        "tools": list(v["tools"]),
                    }
                    for k, v in st["active_turns"].items()
                },
                "latest_replies": {k: dict(v) for k, v in st["latest_replies"].items()},
                "last_reply_session": st["last_reply_session"],
                "last_started_session": st["last_started_session"],
                "pending_permission": dict(st["pending_permission"])
                if st["pending_permission"]
                else None,
            }

    def on_frame(self, device_id: str, frame: Any) -> None:
        if not isinstance(frame, dict):
            return
        ftype = str(frame.get("type") or "")
        if not ftype:
            return
        data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
        notify = False
        with self._lock:
            st = self._ensure_state(device_id)
            if ftype in ("device_reset", "device_status"):
                if ftype == "device_reset" or data.get("online") is False:
                    st["active_turns"].clear()
                    st["pending_permission"] = None
                    st["seq"] += 1
                    notify = True
            elif ftype == "device_online":
                st["seq"] += 1
                notify = True
            elif ftype == "turn_start":
                sess = str(data.get("session_name") or "")
                st["seq"] += 1
                cur_seq = st["seq"]
                st["active_turns"][sess] = {
                    "started_at": time.time(),
                    "started_seq": cur_seq,
                    "text_chunks": [],
                    "text_len": 0,
                    "tools": [],
                }
                if sess:
                    st["last_started_session"] = sess
                notify = True
            elif ftype == "token_delta":
                sess = str(data.get("session_name") or "")
                chunk = str(data.get("text") or data.get("delta") or "")
                turn = st["active_turns"].get(sess)
                if turn is not None and chunk and turn["text_len"] < _PARTIAL_TEXT_MAX_CHARS:
                    remain = _PARTIAL_TEXT_MAX_CHARS - turn["text_len"]
                    piece = chunk[:remain]
                    turn["text_chunks"].append(piece)
                    turn["text_len"] += len(piece)
            elif ftype == "tool_start":
                sess = str(data.get("session_name") or "")
                name = str(data.get("name") or data.get("summary") or "")
                turn = st["active_turns"].get(sess)
                if turn is not None and name:
                    tools = turn["tools"]
                    tools.append(name)
                    if len(tools) > 12:
                        del tools[:-12]
            elif ftype == "message_final":
                sess = str(data.get("session_name") or "")
                st["seq"] += 1
                cur_seq = st["seq"]
                st["latest_replies"][sess] = {
                    "session_name": sess,
                    "content": str(data.get("content") or ""),
                    "reasoning_content": str(data.get("reasoning_content") or ""),
                    "status": "finished",
                    "error": "",
                    "seq": cur_seq,
                    "updated_at": time.time(),
                }
                if sess:
                    st["last_reply_session"] = sess
                notify = True
            elif ftype == "turn_finish":
                sess = str(data.get("session_name") or "")
                turn = st["active_turns"].pop(sess, None)
                st["seq"] += 1
                cur_seq = st["seq"]
                existing = st["latest_replies"].get(sess)
                if existing is None or existing.get("seq", 0) < (turn["started_seq"] if turn else 0):
                    fallback_text = "".join(turn["text_chunks"]) if turn else ""
                    st["latest_replies"][sess] = {
                        "session_name": sess,
                        "content": fallback_text,
                        "reasoning_content": "",
                        "status": "finished",
                        "error": "",
                        "seq": cur_seq,
                        "updated_at": time.time(),
                    }
                else:
                    existing["status"] = "finished"
                    existing["seq"] = cur_seq
                    existing["updated_at"] = time.time()
                if sess:
                    st["last_reply_session"] = sess
                notify = True
            elif ftype in ("turn_error", "error"):
                sess = str(data.get("session_name") or "")
                msg = str(data.get("message") or data.get("msg") or "执行出错")
                if ftype == "turn_error":
                    st["active_turns"].pop(sess, None)
                st["seq"] += 1
                cur_seq = st["seq"]
                if sess or ftype == "turn_error":
                    st["latest_replies"][sess] = {
                        "session_name": sess,
                        "content": "",
                        "reasoning_content": "",
                        "status": "error",
                        "error": msg,
                        "seq": cur_seq,
                        "updated_at": time.time(),
                    }
                    if sess:
                        st["last_reply_session"] = sess
                notify = True
            elif ftype == "turn_cancelled":
                sess = str(data.get("session_name") or "")
                st["active_turns"].pop(sess, None)
                st["seq"] += 1
                cur_seq = st["seq"]
                st["latest_replies"][sess] = {
                    "session_name": sess,
                    "content": "",
                    "reasoning_content": "",
                    "status": "cancelled",
                    "error": "任务已取消",
                    "seq": cur_seq,
                    "updated_at": time.time(),
                }
                if sess:
                    st["last_reply_session"] = sess
                notify = True
            elif ftype == "permission_request":
                st["seq"] += 1
                perm = dict(data)
                perm["_seq"] = st["seq"]
                st["pending_permission"] = perm
                notify = True
            elif ftype == "permission_dismiss":
                st["pending_permission"] = None
                st["seq"] += 1
                notify = True

            waiters = list(self._waiters.get(device_id) or ()) if notify else []

        for loop, ev in waiters:
            try:
                loop.call_soon_threadsafe(ev.set)
            except Exception:
                pass

    def _is_device_online(self, device_id: str) -> bool:
        device = self.relay.devices.get(device_id)
        return bool(device and device.get("online") and callable(device.get("send")))

    def _check_outcome(
        self,
        device_id: str,
        before_seq: int,
        session_name: str | None,
    ) -> dict[str, Any] | None:
        if not self._is_device_online(device_id):
            return {"outcome": "offline"}
        snap = self.snapshot(device_id)
        perm = snap["pending_permission"]
        if perm is not None and int(perm.get("_seq") or 0) > before_seq:
            return {"outcome": "permission", "permission": perm}
        replies = snap["latest_replies"]
        candidates = []
        for sess, info in replies.items():
            if int(info.get("seq") or 0) <= before_seq:
                continue
            if session_name and sess != session_name:
                continue
            if info.get("status") in ("finished", "error", "cancelled"):
                candidates.append(info)
        if candidates:
            candidates.sort(key=lambda item: int(item.get("seq") or 0), reverse=True)
            chosen = candidates[0]
            return {"outcome": chosen["status"], "reply": chosen}
        return None

    async def wait_for_outcome(
        self,
        device_id: str,
        before_seq: int,
        session_name: str | None = None,
        timeout: float = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any] | None:
        immediate = self._check_outcome(device_id, before_seq, session_name)
        if immediate is not None:
            return immediate
        if timeout <= 0:
            return None
        loop = asyncio.get_running_loop()
        ev = asyncio.Event()
        entry = (loop, ev)
        with self._lock:
            self._waiters.setdefault(device_id, set()).add(entry)
        deadline = loop.time() + float(timeout)
        try:
            while True:
                found = self._check_outcome(device_id, before_seq, session_name)
                if found is not None:
                    return found
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                ev.clear()
                found = self._check_outcome(device_id, before_seq, session_name)
                if found is not None:
                    return found
                try:
                    await asyncio.wait_for(ev.wait(), timeout=min(remaining, 1.0))
                except asyncio.TimeoutError:
                    continue
        finally:
            with self._lock:
                bucket = self._waiters.get(device_id)
                if bucket is not None:
                    bucket.discard(entry)
                    if not bucket:
                        self._waiters.pop(device_id, None)


class RelayRpcBridge:
    """通过已建连的桌面 WebSocket 发起命名空间隔离的 RPC 调用."""

    def __init__(self, relay: RelayRegistry):
        self.relay = relay

    async def call(
        self,
        device_id: str,
        method: str,
        args: dict[str, Any] | list[Any] | None = None,
        *,
        timeout: float = RPC_TIMEOUT_SECONDS,
    ) -> Any:
        device = self.relay.devices.get(device_id)
        send = device.get("send") if device else None
        if not device or not device.get("online") or not callable(send):
            raise DeviceOfflineError("电脑上的星瑶未在线，请确认桌面端已启动并开启云中转")

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        app_key = f"mcp:{uuid.uuid4().hex}"
        rpc_id = uuid.uuid4().hex

        def on_frame(frame: Any) -> None:
            if not isinstance(frame, dict):
                return
            if frame.get("id") == rpc_id:
                def _resolve() -> None:
                    if not fut.done():
                        fut.set_result(frame)

                loop.call_soon_threadsafe(_resolve)
                return
            if (
                frame.get("type") == "device_status"
                and isinstance(frame.get("data"), dict)
                and frame["data"].get("online") is False
            ):
                def _offline() -> None:
                    if not fut.done():
                        fut.set_exception(DeviceOfflineError("电脑上的星瑶已断开连接"))

                loop.call_soon_threadsafe(_offline)

        item = self.relay.listen(device_id, on_frame, app_key=app_key)
        try:
            namespaced = self.relay.namespace_rpc(device_id, app_key, rpc_id)
            payload = json.dumps(
                {
                    "id": namespaced,
                    "method": method,
                    "args": {} if args is None else args,
                },
                ensure_ascii=False,
            )
            if not send(payload):
                raise DeviceOfflineError("无法向电脑上的星瑶投递指令")
            try:
                reply = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RpcTimeoutError(f"等待星瑶响应超时（{method}）") from exc
        finally:
            self.relay.unlisten(item)

        if isinstance(reply, dict) and reply.get("error"):
            raise RpcCallError(str(reply.get("error")))
        return reply.get("result") if isinstance(reply, dict) else reply


def _format_permission_summary(perm: dict[str, Any]) -> str:
    req_id = str(perm.get("request_id") or "")
    fn = str(perm.get("function_name") or perm.get("tool_name") or "操作")
    desc = str(perm.get("description") or perm.get("detail") or perm.get("command") or "").strip()
    options = perm.get("options") or []
    parts = [
        "星瑶正在等待权限确认，暂未继续执行：",
        f"- request_id: {req_id}",
        f"- 操作: {fn}",
    ]
    if desc:
        parts.append(f"- 说明: {desc[:300]}")
    if options:
        parts.append(f"- 可选项: {', '.join(str(o) for o in options)}")
    parts.append("请向用户确认后，调用 respond_permission(request_id=..., allow=true/false) 继续。")
    return "\n".join(parts)


def _extract_latest_assistant_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            err = str(msg.get("error_note") or "").strip()
            content = str(msg.get("content") or "").strip()
            if content and err:
                return f"{content}\n\n[错误] {err}"
            if content:
                return content
            if err:
                return f"[错误] {err}"
    return ""


class RelayControlService:
    """统一承载 MCP 工具与 REST Skill 接口的远端星瑶控制层."""

    def __init__(self, relay: RelayRegistry, tracker: RelayDeviceTracker):
        self.relay = relay
        self.tracker = tracker
        self.rpc = RelayRpcBridge(relay)

    def is_online(self, device_id: str) -> bool:
        device = self.relay.devices.get(device_id)
        return bool(device and device.get("online") and callable(device.get("send")))

    async def _safe_rpc(
        self,
        device_id: str,
        method: str,
        args: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        try:
            return await self.rpc.call(device_id, method, args)
        except Exception:
            return None

    async def _fetch_latest_reply_from_pc(
        self, device_id: str, session_name: str | None = None
    ) -> tuple[str, str]:
        target = str(session_name or "").strip()
        if target:
            res = await self._safe_rpc(device_id, "load_older_messages", [target, _SQLITE_MAX_ID])
            if isinstance(res, dict) and res.get("ok"):
                return target, _extract_latest_assistant_from_messages(res.get("messages"))
        init = await self._safe_rpc(device_id, "get_initial_state", {})
        if isinstance(init, dict):
            cur = str(init.get("current_session") or "")
            if not target or target == cur:
                return cur, _extract_latest_assistant_from_messages(init.get("messages"))
        return target, ""

    async def send_message(
        self,
        device_id: str,
        text: str,
        *,
        session_name: str | None = None,
        workdir: str | None = None,
        model_id: str | None = None,
        wait_seconds: int = DEFAULT_WAIT_SECONDS,
    ) -> dict[str, Any]:
        clean_text = str(text or "").strip()
        if not clean_text:
            return {"ok": False, "status": "invalid", "text": "text 不能为空"}
        clean_session = str(session_name or "").strip() or None
        clean_workdir = str(workdir or "").strip() or None
        clean_model_id = str(model_id or "").strip() or None
        wait = _clamp_wait_seconds(wait_seconds, DEFAULT_WAIT_SECONDS)

        before_seq = self.tracker.seq(device_id)
        rpc_args: dict[str, Any] = {"text": clean_text}
        if clean_session is not None:
            rpc_args["session_name"] = clean_session
        if clean_workdir is not None:
            rpc_args["workdir"] = clean_workdir
        if clean_model_id is not None:
            rpc_args["model_id"] = clean_model_id

        try:
            res = await self.rpc.call(device_id, "send_user_message", rpc_args)
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "status": "error", "text": str(exc)}

        if not isinstance(res, dict) or not res.get("ok"):
            msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
            return {
                "ok": False,
                "status": "error",
                "text": f"发送失败：{msg}" if msg else "发送失败：星瑶未受理该任务",
            }

        queued = bool(res.get("queued"))
        if wait <= 0:
            note = "已加入星瑶消息队列，将在当前回合完成后执行。" if queued else "已交给星瑶执行。"
            return {
                "ok": True,
                "status": "queued" if queued else "running",
                "session_name": clean_session or "",
                "text": f"{note}可稍后调用 get_reply 查看结果。",
            }

        outcome = await self.tracker.wait_for_outcome(
            device_id, before_seq, session_name=clean_session, timeout=float(wait)
        )
        if outcome is not None:
            kind = outcome["outcome"]
            if kind == "offline":
                return {"ok": False, "status": "offline", "text": "电脑上的星瑶已离线"}
            if kind == "permission":
                perm = outcome["permission"]
                return {
                    "ok": True,
                    "status": "permission_required",
                    "permission": perm,
                    "text": _format_permission_summary(perm),
                }
            if kind == "finished":
                reply_info = outcome["reply"]
                sess = str(reply_info.get("session_name") or clean_session or "")
                content = str(reply_info.get("content") or "").strip()
                if not content and sess:
                    _, content = await self._fetch_latest_reply_from_pc(device_id, sess)
                return {
                    "ok": True,
                    "status": "finished",
                    "session_name": sess,
                    "reply": content,
                    "text": content or "星瑶已执行完成（本轮无文本输出）。",
                }
            if kind in ("error", "cancelled"):
                reply_info = outcome["reply"]
                err = str(reply_info.get("error") or "执行中断")
                return {
                    "ok": False,
                    "status": kind,
                    "session_name": str(reply_info.get("session_name") or clean_session or ""),
                    "text": f"星瑶执行未完成：{err}",
                }

        # 等待窗口到期仍在执行：汇总当前进度、工具调用与待审批权限
        return await self._running_summary(device_id, clean_session, wait)

    async def _running_summary(
        self,
        device_id: str,
        session_name: str | None,
        waited: int,
    ) -> dict[str, Any]:
        perm_res = await self._safe_rpc(device_id, "get_active_permission", {})
        if isinstance(perm_res, dict) and perm_res.get("permission"):
            perm = perm_res["permission"]
            return {
                "ok": True,
                "status": "permission_required",
                "permission": perm,
                "text": _format_permission_summary(perm),
            }

        snap = self.tracker.snapshot(device_id)
        active = snap["active_turns"]
        target_sess = session_name or snap["last_started_session"]
        turn_info = active.get(target_sess) if target_sess else None
        if turn_info is None and active:
            target_sess, turn_info = next(iter(active.items()))

        rpc_snaps = await self._safe_rpc(device_id, "get_all_active_turn_snapshots", {})
        lines = [f"星瑶仍在电脑上执行中（已等待 {waited}s）。"]
        if target_sess:
            lines.append(f"- 会话: {target_sess}")
        if isinstance(rpc_snaps, dict) and rpc_snaps:
            for sname, sdata in rpc_snaps.items():
                if target_sess and sname != target_sess:
                    continue
                tools = sdata.get("tool_calls") or []
                if tools:
                    recent = [str(t.get("name") or t.get("summary") or "tool") for t in tools[-5:]]
                    lines.append(f"- 最近工具调用: {', '.join(recent)}")
                break
        elif turn_info and turn_info.get("tools"):
            lines.append(f"- 最近工具调用: {', '.join(turn_info['tools'][-5:])}")
        if turn_info and turn_info.get("partial_text"):
            preview = turn_info["partial_text"][-800:].strip()
            if preview:
                lines.append(f"- 已输出片段:\n{preview}")
        lines.append("请稍后调用 get_reply（可设 wait_seconds=20）获取完整结果。")
        return {
            "ok": True,
            "status": "running",
            "session_name": target_sess or "",
            "text": "\n".join(lines),
        }

    async def get_reply(
        self,
        device_id: str,
        *,
        session_name: str | None = None,
        wait_seconds: int = 0,
    ) -> dict[str, Any]:
        if not self.is_online(device_id):
            return {"ok": False, "status": "offline", "text": "电脑上的星瑶未在线"}
        clean_session = str(session_name or "").strip() or None
        wait = _clamp_wait_seconds(wait_seconds, 0)

        perm_res = await self._safe_rpc(device_id, "get_active_permission", {})
        if isinstance(perm_res, dict) and perm_res.get("permission"):
            perm = perm_res["permission"]
            return {
                "ok": True,
                "status": "permission_required",
                "permission": perm,
                "text": _format_permission_summary(perm),
            }

        snap = self.tracker.snapshot(device_id)
        rpc_snaps = await self._safe_rpc(device_id, "get_all_active_turn_snapshots", {})
        is_running = False
        if isinstance(rpc_snaps, dict):
            if clean_session:
                is_running = clean_session in rpc_snaps
            else:
                is_running = bool(rpc_snaps)
        elif clean_session:
            is_running = clean_session in snap["active_turns"]
        else:
            is_running = bool(snap["active_turns"])

        if is_running and wait > 0:
            before_seq = snap["seq"]
            outcome = await self.tracker.wait_for_outcome(
                device_id, before_seq, session_name=clean_session, timeout=float(wait)
            )
            if outcome is not None:
                kind = outcome["outcome"]
                if kind == "offline":
                    return {"ok": False, "status": "offline", "text": "电脑上的星瑶已离线"}
                if kind == "permission":
                    perm = outcome["permission"]
                    return {
                        "ok": True,
                        "status": "permission_required",
                        "permission": perm,
                        "text": _format_permission_summary(perm),
                    }
                if kind == "finished":
                    reply_info = outcome["reply"]
                    sess = str(reply_info.get("session_name") or clean_session or "")
                    content = str(reply_info.get("content") or "").strip()
                    if not content and sess:
                        _, content = await self._fetch_latest_reply_from_pc(device_id, sess)
                    return {
                        "ok": True,
                        "status": "finished",
                        "session_name": sess,
                        "reply": content,
                        "text": content or "星瑶已执行完成（本轮无文本输出）。",
                    }
                if kind in ("error", "cancelled"):
                    reply_info = outcome["reply"]
                    err = str(reply_info.get("error") or "执行中断")
                    return {
                        "ok": False,
                        "status": kind,
                        "session_name": str(reply_info.get("session_name") or clean_session or ""),
                        "text": f"星瑶执行未完成：{err}",
                    }
            return await self._running_summary(device_id, clean_session, wait)

        if is_running:
            return await self._running_summary(device_id, clean_session, 0)

        target_sess = clean_session or snap["last_reply_session"] or snap["last_started_session"]
        cached = snap["latest_replies"].get(target_sess) if target_sess else None
        if cached and cached.get("content"):
            return {
                "ok": True,
                "status": cached.get("status") or "finished",
                "session_name": target_sess,
                "reply": cached["content"],
                "text": cached["content"],
            }

        resolved_sess, content = await self._fetch_latest_reply_from_pc(device_id, clean_session)
        if content:
            return {
                "ok": True,
                "status": "finished",
                "session_name": resolved_sess,
                "reply": content,
                "text": content,
            }
        return {
            "ok": True,
            "status": "idle",
            "session_name": resolved_sess,
            "reply": "",
            "text": "当前没有可返回的星瑶回复。",
        }

    async def get_status(self, device_id: str) -> dict[str, Any]:
        online = self.is_online(device_id)
        if not online:
            return {
                "ok": True,
                "online": False,
                "device_id": device_id,
                "text": "电脑上的星瑶当前离线（请确认桌面端已开启并连接到中转服务器）。",
            }
        init = await self._safe_rpc(device_id, "get_initial_state", {})
        perm_res = await self._safe_rpc(device_id, "get_active_permission", {})
        cur_sess = str(init.get("current_session") or "") if isinstance(init, dict) else ""
        workdir = str(init.get("workdir") or "") if isinstance(init, dict) else ""
        active_sessions = init.get("active_sessions") if isinstance(init, dict) else []
        active_snaps = init.get("active_turn_snapshots") if isinstance(init, dict) else {}
        perm = perm_res.get("permission") if isinstance(perm_res, dict) else None

        lines = [
            f"电脑上的星瑶在线（device_id={device_id}）",
            f"- 当前会话: {cur_sess or '未知'}",
            f"- 工作目录: {workdir or '未知'}",
            f"- 运行中会话: {', '.join(active_sessions) if active_sessions else '无'}",
        ]
        if perm:
            lines.append(_format_permission_summary(perm))
        return {
            "ok": True,
            "online": True,
            "device_id": device_id,
            "current_session": cur_sess,
            "workdir": workdir,
            "active_sessions": active_sessions or [],
            "active_turn_snapshots": active_snaps or {},
            "permission": perm,
            "text": "\n".join(lines),
        }

    async def list_models(self, device_id: str) -> dict[str, Any]:
        try:
            data = await self.rpc.call(device_id, "get_model_settings", {})
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "providers": [], "models": [], "active_model_id": "", "text": str(exc)}
        if not isinstance(data, dict):
            return {"ok": False, "providers": [], "models": [], "active_model_id": "", "text": "获取模型配置失败"}
        active_id = str(data.get("active_model_id") or "")
        providers = data.get("providers") or []
        models = data.get("models") or []
        lines = []
        for p in providers:
            if not isinstance(p, dict):
                continue
            pname = str(p.get("name") or p.get("id") or "未命名厂商")
            pid = str(p.get("id") or "")
            protocol = str(p.get("protocol") or "openai")
            has_key = "已配置Key" if p.get("has_api_key") else "未配置Key"
            p_models = p.get("models") or []
            lines.append(f"[{pname}] (provider_id={pid}, 协议={protocol}, {has_key})")
            if not p_models:
                lines.append("  （该厂商下暂无模型）")
                continue
            for m in p_models:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "")
                mname = str(m.get("name") or mid)
                ctx = m.get("context_window") or ""
                cur = " ★当前" if mid == active_id else ""
                caps = []
                if m.get("supports_vision"):
                    caps.append("视觉")
                if m.get("supports_function_calling"):
                    caps.append("工具")
                cap_str = f", 能力={'/'.join(caps)}" if caps else ""
                lines.append(f"  - {mname} (model_id={mid}, 上下文={ctx}{cap_str}){cur}")
        return {
            "ok": True,
            "active_model_id": active_id,
            "providers": providers,
            "models": models,
            "text": "\n".join(lines) if lines else "当前未配置任何模型厂商",
        }

    async def switch_model(
        self,
        device_id: str,
        *,
        model_id: str | None = None,
        model_name: str | None = None,
        provider_name: str | None = None,
        session_name: str | None = None,
    ) -> dict[str, Any]:
        target_id = str(model_id or "").strip()
        target_name = str(model_name or "").strip()
        target_provider = str(provider_name or "").strip()
        clean_session = str(session_name or "").strip() or None
        if not target_id and not target_name:
            return {"ok": False, "text": "请提供 model_id 或 model_name"}

        # 支持按 model_name + provider_name 解析出 model_id；若传入 model_id 不存在也可友好提示
        if not target_id:
            snapshot = await self.list_models(device_id)
            if not snapshot.get("ok"):
                return snapshot
            all_models = snapshot.get("models") or []
            matches = []
            for m in all_models:
                if not isinstance(m, dict):
                    continue
                mname = str(m.get("name") or "")
                pname = str(m.get("provider_name") or "")
                if target_provider and target_provider.lower() not in pname.lower():
                    continue
                if mname.lower() == target_name.lower() or target_name.lower() in mname.lower():
                    matches.append(m)
            if not matches:
                return {"ok": False, "text": f"未找到匹配模型：{target_name}"}
            exact = [m for m in matches if str(m.get("name") or "").lower() == target_name.lower()]
            if len(exact) == 1:
                matches = exact
            if len(matches) > 1:
                options = "\n".join(
                    f"  - {m.get('name')} (厂商={m.get('provider_name')}, model_id={m.get('id')})"
                    for m in matches[:10]
                )
                return {
                    "ok": False,
                    "text": f"匹配到多个同名/相似模型，请补充 provider_name 或直接传 model_id：\n{options}",
                }
            target_id = str(matches[0].get("id") or "")

        args: dict[str, Any] = {"model_id": target_id}
        if clean_session is not None:
            args["session_name"] = clean_session
        try:
            res = await self.rpc.call(device_id, "set_model", args)
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "text": str(exc)}
        if isinstance(res, dict) and res.get("ok"):
            active_name = str(res.get("active_model_name") or target_id)
            active_id = str(res.get("active_model_id") or target_id)
            scope = f"会话 {clean_session}" if clean_session else "当前会话"
            return {
                "ok": True,
                "active_model_id": active_id,
                "active_model_name": active_name,
                "text": f"已将{scope}的模型切换为：{active_name} (model_id={active_id})",
            }
        msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
        return {"ok": False, "text": f"切换模型失败：{msg}" if msg else "切换模型失败"}

    async def list_workspaces(self, device_id: str) -> dict[str, Any]:
        try:
            groups = await self.rpc.call(device_id, "get_sessions", {})
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "workspaces": [], "text": str(exc)}
        if not isinstance(groups, list):
            return {"ok": True, "workspaces": [], "text": "暂无工作目录"}
        workspaces = []
        lines = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            path = str(g.get("folder_path") or "")
            name = str(g.get("folder_name") or path or "分组")
            is_cur = bool(g.get("is_current"))
            is_global = bool(g.get("is_global"))
            is_sec = bool(g.get("is_secretary"))
            sessions = g.get("sessions") or []
            item = {
                "folder_path": path,
                "folder_name": name,
                "is_current": is_cur,
                "is_global": is_global,
                "is_secretary": is_sec,
                "session_count": len(sessions),
            }
            workspaces.append(item)
            cur_mark = " ★当前" if is_cur else ""
            lines.append(f"- {name} ({path}) — {len(sessions)} 个对话{cur_mark}")
        return {
            "ok": True,
            "workspaces": workspaces,
            "text": "\n".join(lines) if lines else "暂无工作目录",
        }

    async def list_sessions(
        self, device_id: str, *, workdir: str | None = None
    ) -> dict[str, Any]:
        try:
            groups = await self.rpc.call(device_id, "get_sessions", {})
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "groups": [], "text": str(exc)}
        if not isinstance(groups, list):
            return {"ok": True, "groups": [], "text": "暂无会话"}
        filter_text = str(workdir or "").strip().lower().replace("\\", "/")
        lines = []
        filtered_groups = []
        for g in groups:
            if not isinstance(g, dict):
                continue
            folder = str(g.get("folder_name") or g.get("folder_path") or "分组")
            folder_path = str(g.get("folder_path") or "")
            if filter_text:
                norm_path = folder_path.lower().replace("\\", "/")
                if filter_text not in norm_path and filter_text not in folder.lower():
                    continue
            filtered_groups.append(g)
            sessions = g.get("sessions") or []
            cur_group = " ★当前工作区" if g.get("is_current") else ""
            lines.append(f"[{folder}] ({folder_path}){cur_group}")
            if not sessions:
                lines.append("  （该工作目录下暂无对话）")
                continue
            for s in sessions:
                if not isinstance(s, dict):
                    continue
                name = str(s.get("name") or "")
                display = str(s.get("display_name") or name)
                cnt = s.get("message_count", 0)
                active_mark = " [运行中]" if s.get("is_active") else ""
                cur_mark = " (当前)" if s.get("is_current") else ""
                lines.append(f"  - {display} (session_name={name}, 消息数={cnt}){cur_mark}{active_mark}")
        return {
            "ok": True,
            "groups": filtered_groups,
            "text": "\n".join(lines) if lines else ("未找到匹配该工作目录的会话" if filter_text else "暂无会话"),
        }

    async def switch_session(
        self,
        device_id: str,
        session_name: str,
        *,
        target_workdir: str | None = None,
    ) -> dict[str, Any]:
        clean_session = str(session_name or "").strip()
        if not clean_session:
            return {"ok": False, "text": "session_name 不能为空"}
        args: dict[str, Any] = {"session_name": clean_session}
        clean_wdir = str(target_workdir or "").strip()
        if clean_wdir:
            args["target_workdir"] = clean_wdir
        try:
            res = await self.rpc.call(device_id, "switch_session", args)
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "text": str(exc)}
        if not isinstance(res, dict) or not res.get("ok"):
            msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
            return {"ok": False, "text": f"切换会话失败：{msg}" if msg else "切换会话失败"}
        wdir = str(res.get("workdir") or "")
        model_name = str(res.get("active_model_name") or "")
        model_id = str(res.get("active_model_id") or "")
        latest = _extract_latest_assistant_from_messages(res.get("messages"))
        lines = [
            f"已切换到会话：{clean_session}",
            f"- 工作目录: {wdir}",
        ]
        if model_name or model_id:
            lines.append(f"- 当前模型: {model_name} (model_id={model_id})")
        if latest:
            lines.append(f"- 最近一条星瑶回复:\n{latest[:500]}")
        return {
            "ok": True,
            "session_name": clean_session,
            "workdir": wdir,
            "active_model_id": model_id,
            "active_model_name": model_name,
            "text": "\n".join(lines),
        }

    async def create_session(
        self,
        device_id: str,
        *,
        workdir: str | None = None,
        title: str = "新会话",
    ) -> dict[str, Any]:
        clean_wdir = str(workdir or "").strip() or None
        clean_title = str(title or "").strip() or "新会话"
        if clean_wdir and clean_wdir != "GLOBAL":
            ws_res = await self._safe_rpc(device_id, "select_workspace_by_path", {"path": clean_wdir})
            if isinstance(ws_res, dict) and ws_res.get("ok") is False:
                return {"ok": False, "text": str(ws_res.get("msg") or "工作目录无效")}
        args: dict[str, Any] = {"title": clean_title}
        if clean_wdir:
            args["workdir"] = clean_wdir
        try:
            res = await self.rpc.call(device_id, "create_session", args)
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "text": str(exc)}
        if not isinstance(res, dict) or not res.get("ok"):
            msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
            return {"ok": False, "text": f"新建会话失败：{msg}" if msg else "新建会话失败"}
        sess_name = str(res.get("session_name") or "")
        actual_wdir = str(res.get("workdir") or clean_wdir or "")
        return {
            "ok": True,
            "session_name": sess_name,
            "workdir": actual_wdir,
            "title": clean_title,
            "text": f"已新建并切换到会话：{clean_title} (session_name={sess_name}, 工作目录={actual_wdir})",
        }

    async def stop_task(
        self, device_id: str, *, session_name: str | None = None
    ) -> dict[str, Any]:
        clean_session = str(session_name or "").strip()
        try:
            res = await self.rpc.call(
                device_id,
                "stop_current_turn",
                {"session_name": clean_session} if clean_session else {},
            )
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "text": str(exc)}
        if isinstance(res, dict) and res.get("ok"):
            if res.get("idle"):
                return {"ok": True, "idle": True, "text": "当前没有正在执行的任务。"}
            return {"ok": True, "idle": False, "text": "已向星瑶发送停止指令。"}
        msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
        return {"ok": False, "text": f"停止失败：{msg}" if msg else "停止失败"}

    async def respond_permission(
        self,
        device_id: str,
        request_id: str,
        allow: bool,
        *,
        always: bool = False,
    ) -> dict[str, Any]:
        req_id = str(request_id or "").strip()
        if not req_id:
            return {"ok": False, "text": "request_id 不能为空"}
        try:
            res = await self.rpc.call(
                device_id,
                "respond_permission",
                {"request_id": req_id, "allow": bool(allow), "always": bool(always)},
            )
        except (DeviceOfflineError, RpcTimeoutError, RpcCallError) as exc:
            return {"ok": False, "text": str(exc)}
        if isinstance(res, dict) and res.get("ok"):
            verb = "允许（本任务始终）" if (allow and always) else ("允许" if allow else "拒绝")
            return {"ok": True, "text": f"已{verb}权限请求 {req_id}。"}
        msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
        return {"ok": False, "text": f"响应失败：{msg}" if msg else "响应失败"}


class RelayMcpProtocolHandler:
    """MCP JSON-RPC 2.0 协议分发（兼容 core.mcp_server 语义）."""

    def __init__(self, control: RelayControlService):
        self.control = control

    @staticmethod
    def initialize_result(params: Any) -> dict[str, Any]:
        requested = ""
        if isinstance(params, dict):
            requested = str(params.get("protocolVersion") or "").strip()
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else DEFAULT_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "xingyao-relay", "version": "1.0.0"},
        }

    async def handle_message(self, device_id: str, payload: Any) -> tuple[int, dict[str, Any] | None]:
        if not isinstance(payload, dict):
            return 400, _jsonrpc_error(None, -32700, "parse error")
        method = payload.get("method")
        msg_id = payload.get("id")
        if not isinstance(method, str) or not method:
            return 400, _jsonrpc_error(msg_id, -32600, "invalid request")
        if "id" not in payload:
            return 202, None
        if method == "initialize":
            return 200, _jsonrpc_result(msg_id, self.initialize_result(payload.get("params")))
        if method == "ping":
            return 200, _jsonrpc_result(msg_id, {})
        if method == "tools/list":
            return 200, _jsonrpc_result(msg_id, {"tools": tool_definitions()})
        if method == "tools/call":
            return 200, await self._tools_call(device_id, msg_id, payload.get("params"))
        return 200, _jsonrpc_error(msg_id, -32601, "method not found")

    async def _tools_call(
        self, device_id: str, msg_id: Any, params: Any
    ) -> dict[str, Any]:
        if not isinstance(params, dict):
            return _jsonrpc_error(msg_id, -32602, "invalid params: params must be object")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _jsonrpc_error(msg_id, -32602, "invalid params: arguments must be object")
        name = str(params.get("name") or "")

        if name == "send_message":
            text = str(arguments.get("text") or "")
            if not text.strip():
                return _jsonrpc_result(msg_id, _tool_error("text 不能为空"))
            res = await self.control.send_message(
                device_id,
                text,
                session_name=arguments.get("session_name"),
                workdir=arguments.get("workdir"),
                model_id=arguments.get("model_id"),
                wait_seconds=_clamp_wait_seconds(arguments.get("wait_seconds"), DEFAULT_WAIT_SECONDS),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "get_reply":
            res = await self.control.get_reply(
                device_id,
                session_name=arguments.get("session_name"),
                wait_seconds=_clamp_wait_seconds(arguments.get("wait_seconds"), 0),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "get_status":
            res = await self.control.get_status(device_id)
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "list_models":
            res = await self.control.list_models(device_id)
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "switch_model":
            res = await self.control.switch_model(
                device_id,
                model_id=arguments.get("model_id"),
                model_name=arguments.get("model_name"),
                provider_name=arguments.get("provider_name"),
                session_name=arguments.get("session_name"),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "list_workspaces":
            res = await self.control.list_workspaces(device_id)
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "list_sessions":
            res = await self.control.list_sessions(
                device_id, workdir=arguments.get("workdir")
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "switch_session":
            res = await self.control.switch_session(
                device_id,
                str(arguments.get("session_name") or ""),
                target_workdir=arguments.get("target_workdir"),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "create_session":
            res = await self.control.create_session(
                device_id,
                workdir=arguments.get("workdir"),
                title=str(arguments.get("title") or "新会话"),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "stop_task":
            res = await self.control.stop_task(
                device_id, session_name=arguments.get("session_name")
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        if name == "respond_permission":
            req_id = str(arguments.get("request_id") or "").strip()
            if not req_id:
                return _jsonrpc_result(msg_id, _tool_error("request_id 不能为空"))
            if "allow" not in arguments or not isinstance(arguments.get("allow"), bool):
                return _jsonrpc_result(msg_id, _tool_error("allow 必须是布尔值 true/false"))
            res = await self.control.respond_permission(
                device_id,
                req_id,
                bool(arguments["allow"]),
                always=bool(arguments.get("always", False)),
            )
            return _jsonrpc_result(
                msg_id,
                _tool_ok(res["text"]) if res.get("ok") else _tool_error(res["text"]),
            )
        return _jsonrpc_error(msg_id, -32602, f"unknown tool: {name}")


def mount_mcp_and_skill_routes(
    app: FastAPI,
    database,
    relay: RelayRegistry,
) -> RelayControlService:
    """在中转 FastAPI 实例上挂载公网 MCP (/mcp) 与 Skill REST (/api/skill/*) 入口."""
    tracker = RelayDeviceTracker(relay)
    relay.add_observer(tracker.on_frame)
    control = RelayControlService(relay, tracker)
    mcp_handler = RelayMcpProtocolHandler(control)
    app.state.tracker = tracker
    app.state.control = control

    def _authenticate(request: Request, expected_device_id: str | None = None) -> str | None:
        token = _bearer_token(request.headers.get("authorization"))
        if not token:
            token = str(request.query_params.get("token") or "").strip()
        if not token:
            return None
        return relay.authenticate_mcp_token(
            database, token, expected_device_id=expected_device_id
        )

    async def _read_json_body(request: Request) -> tuple[bool, Any]:
        raw_cl = request.headers.get("content-length")
        if raw_cl:
            try:
                if int(raw_cl) > MAX_BODY_BYTES:
                    return False, JSONResponse(
                        status_code=413,
                        content=_jsonrpc_error(None, -32600, "invalid request: body too large"),
                    )
            except ValueError:
                pass
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return False, JSONResponse(
                status_code=413,
                content=_jsonrpc_error(None, -32600, "invalid request: body too large"),
            )
        if not body:
            return True, None
        try:
            return True, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, JSONResponse(
                status_code=400,
                content=_jsonrpc_error(None, -32700, "parse error"),
            )

    async def _handle_mcp_post(request: Request, expected_device_id: str | None = None):
        device_id = _authenticate(request, expected_device_id=expected_device_id)
        if device_id is None:
            return JSONResponse(
                status_code=401,
                content=_jsonrpc_error(None, -32001, "unauthorized"),
                headers={"WWW-Authenticate": 'Bearer realm="mcp"'},
            )
        ok, payload = await _read_json_body(request)
        if not ok:
            return payload
        status, body = await mcp_handler.handle_message(device_id, payload)
        if body is None:
            return Response(status_code=status, headers={"Content-Length": "0"})
        return JSONResponse(status_code=status, content=body)

    def _handle_mcp_get():
        return JSONResponse(
            status_code=405,
            content=_jsonrpc_error(None, -32600, "method not allowed: use POST /mcp"),
            headers={"Allow": "POST"},
        )

    @app.post("/mcp")
    async def mcp_post(request: Request):
        return await _handle_mcp_post(request)

    @app.post("/mcp/{device_id}")
    async def mcp_post_device(device_id: str, request: Request):
        return await _handle_mcp_post(request, expected_device_id=device_id)

    @app.get("/mcp")
    def mcp_get():
        return _handle_mcp_get()

    @app.get("/mcp/{device_id}")
    def mcp_get_device(device_id: str):
        return _handle_mcp_get()

    def _require_skill_device(request: Request) -> str:
        device_id = _authenticate(request)
        if device_id is None:
            raise HTTPException(
                status_code=401,
                detail="unauthorized",
                headers={"WWW-Authenticate": 'Bearer realm="xingyao-skill"'},
            )
        return device_id

    @app.get("/api/skill/status")
    async def skill_status(request: Request):
        device_id = _require_skill_device(request)
        return await control.get_status(device_id)

    @app.get("/api/skill/models")
    async def skill_models(request: Request):
        device_id = _require_skill_device(request)
        return await control.list_models(device_id)

    @app.post("/api/skill/model")
    async def skill_switch_model(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be json object")
        return await control.switch_model(
            device_id,
            model_id=body.get("model_id"),
            model_name=body.get("model_name"),
            provider_name=body.get("provider_name"),
            session_name=body.get("session_name"),
        )

    @app.get("/api/skill/workspaces")
    async def skill_workspaces(request: Request):
        device_id = _require_skill_device(request)
        return await control.list_workspaces(device_id)

    @app.get("/api/skill/sessions")
    async def skill_sessions(request: Request, workdir: str | None = None):
        device_id = _require_skill_device(request)
        return await control.list_sessions(device_id, workdir=workdir)

    @app.post("/api/skill/switch-session")
    async def skill_switch_session(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be json object")
        return await control.switch_session(
            device_id,
            str(body.get("session_name") or ""),
            target_workdir=body.get("target_workdir"),
        )

    @app.post("/api/skill/create-session")
    async def skill_create_session(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json() if (await request.body()) else {}
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            body = {}
        return await control.create_session(
            device_id,
            workdir=body.get("workdir"),
            title=str(body.get("title") or "新会话"),
        )

    @app.post("/api/skill/send")
    async def skill_send(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be json object")
        return await control.send_message(
            device_id,
            str(body.get("text") or ""),
            session_name=body.get("session_name"),
            workdir=body.get("workdir"),
            model_id=body.get("model_id"),
            wait_seconds=_clamp_wait_seconds(body.get("wait_seconds"), DEFAULT_WAIT_SECONDS),
        )

    @app.get("/api/skill/reply")
    async def skill_reply(
        request: Request,
        session_name: str | None = None,
        wait_seconds: int = 0,
    ):
        device_id = _require_skill_device(request)
        return await control.get_reply(
            device_id,
            session_name=session_name,
            wait_seconds=_clamp_wait_seconds(wait_seconds, 0),
        )

    @app.post("/api/skill/stop")
    async def skill_stop(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json() if (await request.body()) else {}
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            body = {}
        return await control.stop_task(device_id, session_name=body.get("session_name"))

    @app.post("/api/skill/permission")
    async def skill_permission(request: Request):
        device_id = _require_skill_device(request)
        try:
            body = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be json object")
        if "allow" not in body or not isinstance(body.get("allow"), bool):
            raise HTTPException(status_code=400, detail="allow must be boolean")
        return await control.respond_permission(
            device_id,
            str(body.get("request_id") or ""),
            bool(body["allow"]),
            always=bool(body.get("always", False)),
        )

    return control
