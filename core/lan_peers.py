from __future__ import annotations

import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import config
from core import app_context, credentials

logger = logging.getLogger(__name__)

_PEER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,48}$")
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_TERMINAL_STATES = frozenset({"completed", "failed", "canceled"})
_DEFAULT_WAIT_SECONDS = 30
_MAX_WAIT_SECONDS = 60
_WATCH_MAX_SECONDS = 1800.0
_MAX_LOCAL_TASKS = 200

_config_lock = threading.Lock()


def _peers_file_path() -> str:
    data_dir = str(getattr(config, "YCODE_DIR", "") or os.path.join(os.path.expanduser("~"), ".ycode"))
    return os.path.join(data_dir, "lan_peers.json")


def _local_host_slug() -> str:
    try:
        raw = socket.gethostname() or "local"
    except Exception:
        raw = "local"
    slug = _SAFE_SLUG_RE.sub("-", raw).strip("-")
    return (slug[:24] or "local").lower()


def _protect_token(raw: str) -> tuple[str, bool]:
    text = str(raw or "")
    if not text:
        return "", False
    try:
        return credentials.protect_secret(text), False
    except Exception:
        logger.warning("节点令牌加密保存失败，降级为明文落盘", exc_info=True)
        return text, True


def _unprotect_token(stored: str) -> str:
    text = str(stored or "")
    if not text:
        return ""
    try:
        return credentials.unprotect_secret(text)
    except Exception:
        logger.warning("读取节点令牌失败", exc_info=True)
        return ""


def _read_raw_peers_locked() -> list[dict[str, Any]]:
    path = _peers_file_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.warning("读取 lan_peers.json 失败，按空列表处理", exc_info=True)
        return []
    if isinstance(data, dict) and isinstance(data.get("peers"), list):
        return [dict(item) for item in data["peers"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _write_raw_peers_locked(peers: list[dict[str, Any]]) -> None:
    path = _peers_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp.{os.getpid()}"
    payload = {"version": 1, "peers": peers}
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _sanitize_peer(raw: dict[str, Any], include_secret: bool = False) -> dict[str, Any]:
    stored_token = str(raw.get("token") or "")
    out: dict[str, Any] = {
        "id": str(raw.get("id") or ""),
        "name": str(raw.get("name") or raw.get("id") or ""),
        "url": str(raw.get("url") or ""),
        "protocol": str(raw.get("protocol") or "auto"),
        "has_token": bool(stored_token),
        "token_plaintext": bool(raw.get("token_plaintext", False)) and bool(stored_token),
        "default_session": str(raw.get("default_session") or ""),
        "default_workdir": str(raw.get("default_workdir") or ""),
        "description": str(raw.get("description") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "last_status": str(raw.get("last_status") or ""),
        "last_agent_name": str(raw.get("last_agent_name") or ""),
        "last_checked_at": raw.get("last_checked_at") or "",
        "last_error": str(raw.get("last_error") or ""),
    }
    if include_secret:
        out["token"] = _unprotect_token(stored_token)
    return out


def list_peers() -> list[dict[str, Any]]:
    with _config_lock:
        rows = _read_raw_peers_locked()
        return [_sanitize_peer(item, include_secret=False) for item in rows]


def get_peer(peer_id: str, include_secret: bool = False) -> Optional[dict[str, Any]]:
    target = str(peer_id or "").strip()
    if not target:
        return None
    with _config_lock:
        for item in _read_raw_peers_locked():
            if str(item.get("id") or "").strip() == target or str(item.get("name") or "").strip() == target:
                return _sanitize_peer(item, include_secret=include_secret)
    return None


def _normalize_url(raw_url: str) -> str:
    url = str(raw_url or "").strip()
    if not url:
        raise ValueError("节点地址不能为空")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https", "ws", "wss") or not parsed.netloc:
        raise ValueError("节点地址必须以 http://、https://、ws:// 或 wss:// 开头")
    return url.rstrip("/") if scheme in ("http", "https") else url


def save_peer(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("节点配置必须为对象")
    raw_id = str(data.get("id") or "").strip()
    name = str(data.get("name") or "").strip()
    if not raw_id:
        slug_base = _SAFE_SLUG_RE.sub("-", name).strip("-").lower()
        raw_id = slug_base[:36] if slug_base else f"peer-{uuid.uuid4().hex[:6]}"
    if not _PEER_ID_RE.match(raw_id):
        raise ValueError("节点 ID 只允许字母、数字、下划线或连字符（1~48位）")
    if not name:
        name = raw_id

    url = _normalize_url(str(data.get("url") or ""))
    protocol = str(data.get("protocol") or "auto").strip().lower()
    if protocol not in ("auto", "a2a", "lan_ws"):
        raise ValueError("protocol 仅支持 auto / a2a / lan_ws")

    new_token = str(data.get("token") or "").strip()
    with _config_lock:
        rows = _read_raw_peers_locked()
        existing_idx = next(
            (idx for idx, row in enumerate(rows) if str(row.get("id") or "") == raw_id),
            None,
        )
        existing = dict(rows[existing_idx]) if existing_idx is not None else {}

        if new_token:
            stored_token, is_plain = _protect_token(new_token)
        else:
            stored_token = str(existing.get("token") or "")
            is_plain = bool(existing.get("token_plaintext", False))

        record: dict[str, Any] = {
            "id": raw_id,
            "name": name,
            "url": url,
            "protocol": protocol,
            "token": stored_token,
            "token_plaintext": is_plain,
            "default_session": str(data.get("default_session") or "").strip(),
            "default_workdir": str(data.get("default_workdir") or "").strip(),
            "description": str(data.get("description") or "").strip(),
            "enabled": bool(data.get("enabled", True)),
            "last_status": str(existing.get("last_status") or ""),
            "last_agent_name": str(existing.get("last_agent_name") or ""),
            "last_checked_at": existing.get("last_checked_at") or "",
            "last_error": str(existing.get("last_error") or ""),
        }
        if existing_idx is not None:
            rows[existing_idx] = record
        else:
            rows.append(record)
        _write_raw_peers_locked(rows)
        return _sanitize_peer(record, include_secret=False)


def delete_peer(peer_id: str) -> bool:
    target = str(peer_id or "").strip()
    if not target:
        return False
    with _config_lock:
        rows = _read_raw_peers_locked()
        kept = [row for row in rows if str(row.get("id") or "") != target]
        if len(kept) == len(rows):
            return False
        _write_raw_peers_locked(kept)
        return True


def _update_peer_probe_status(
    peer_id: str,
    *,
    status: str,
    agent_name: str = "",
    error: str = "",
) -> None:
    target = str(peer_id or "").strip()
    if not target:
        return
    with _config_lock:
        rows = _read_raw_peers_locked()
        changed = False
        for row in rows:
            if str(row.get("id") or "") == target:
                row["last_status"] = status
                if agent_name:
                    row["last_agent_name"] = agent_name
                row["last_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                row["last_error"] = error
                changed = True
                break
        if changed:
            try:
                _write_raw_peers_locked(rows)
            except Exception:
                logger.debug("回写节点探测状态失败", exc_info=True)


def _resolve_protocol(peer: dict[str, Any]) -> str:
    proto = str(peer.get("protocol") or "auto").strip().lower()
    if proto in ("a2a", "lan_ws"):
        return proto
    url = str(peer.get("url") or "").strip().lower()
    if url.startswith(("ws://", "wss://")):
        return "lan_ws"
    return "a2a"


def _a2a_urls(raw_url: str, card: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    parsed = urlsplit(str(raw_url or "").strip())
    path = parsed.path.rstrip("/")
    if path.endswith("/a2a"):
        base_path = path[:-4]
        rpc_url = urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    else:
        base_path = path
        rpc_url = ""
    base_url = urlunsplit((parsed.scheme, parsed.netloc, base_path, "", "")).rstrip("/")
    if not rpc_url and isinstance(card, dict):
        card_url = str(card.get("url") or "").strip()
        if card_url.startswith(("http://", "https://")):
            rpc_url = card_url
    if not rpc_url:
        rpc_url = f"{base_url}/a2a"
    return base_url, rpc_url


def _http_json(
    method: str,
    url: str,
    *,
    payload: Optional[dict[str, Any]] = None,
    token: str = "",
    timeout: float = 15.0,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return int(resp.status), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except Exception:
                parsed = {"raw": raw.decode("utf-8", errors="replace")[:300]}
        return int(exc.code), parsed


def _clamp_wait(raw: Any, default: int = _DEFAULT_WAIT_SECONDS) -> int:
    if raw is None or raw == "":
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return default
    return max(0, min(_MAX_WAIT_SECONDS, val))


def _ws_connect_and_auth(ws_url: str, token: str, timeout: float = 10.0):
    from websockets.sync.client import connect

    ws = connect(ws_url, open_timeout=timeout, close_timeout=3)
    try:
        ws.send(json.dumps({"token": token}, ensure_ascii=False))
        deadline = time.monotonic() + timeout
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("WebSocket 握手超时")
            raw = ws.recv(timeout=rem)
            msg = json.loads(raw) if isinstance(raw, str) else {}
            if isinstance(msg, dict) and msg.get("type") == "hello":
                return ws
    except Exception:
        try:
            ws.close()
        except Exception:
            pass
        raise


def _ws_rpc(
    ws,
    rpc_id: int,
    method: str,
    args: Any,
    *,
    timeout: float = 12.0,
) -> tuple[Any, list[dict[str, Any]]]:
    ws.send(json.dumps({"id": rpc_id, "method": method, "args": args}, ensure_ascii=False))
    events: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"LAN RPC {method} 超时")
        raw = ws.recv(timeout=rem)
        msg = json.loads(raw) if isinstance(raw, str) else {}
        if not isinstance(msg, dict):
            continue
        if msg.get("id") == rpc_id:
            if msg.get("error"):
                raise RuntimeError(str(msg.get("error")))
            return msg.get("result"), events
        if "type" in msg:
            events.append(msg)


def _extract_latest_assistant_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for item in reversed(messages):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() == "assistant":
            content = str(item.get("content") or "").strip()
            if content:
                return content
    return ""


def _parse_a2a_task_response(task: dict[str, Any]) -> dict[str, Any]:
    status_obj = task.get("status") if isinstance(task.get("status"), dict) else {}
    state = str(status_obj.get("state") or "working").strip() or "working"
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}

    reply = ""
    artifacts = task.get("artifacts")
    if isinstance(artifacts, list):
        for art in reversed(artifacts):
            if not isinstance(art, dict):
                continue
            for part in art.get("parts") or []:
                if isinstance(part, dict) and str(part.get("text") or "").strip():
                    reply = str(part.get("text") or "").strip()
                    break
            if reply:
                break

    msg_obj = status_obj.get("message") if isinstance(status_obj.get("message"), dict) else {}
    msg_texts = [
        str(p.get("text") or "").strip()
        for p in (msg_obj.get("parts") or [])
        if isinstance(p, dict) and str(p.get("text") or "").strip()
    ]
    status_text = "\n".join(msg_texts)
    if state == "completed" and not reply:
        reply = status_text

    return {
        "remote_task_id": str(task.get("id") or ""),
        "context_id": str(task.get("contextId") or ""),
        "status": state,
        "reply": reply,
        "error": str(meta.get("error") or (status_text if state in ("failed", "canceled") and not reply else "")),
        "permission": meta.get("permission") if isinstance(meta.get("permission"), dict) else None,
        "progress": meta.get("progress") if isinstance(meta.get("progress"), dict) else None,
        "remote_session": str(meta.get("session_name") or ""),
    }


def probe_peer(peer_or_id: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(peer_or_id, str):
        peer = get_peer(peer_or_id, include_secret=True)
        if peer is None:
            return {"ok": False, "online": False, "msg": f"未找到节点: {peer_or_id}"}
    else:
        peer = dict(peer_or_id)
    peer_id = str(peer.get("id") or "")
    protocol = _resolve_protocol(peer)
    token = str(peer.get("token") or "")
    t0 = time.monotonic()

    if protocol == "lan_ws":
        try:
            ws = _ws_connect_and_auth(str(peer["url"]), token, timeout=6.0)
            try:
                latency_ms = int((time.monotonic() - t0) * 1000)
            finally:
                ws.close()
            _update_peer_probe_status(peer_id, status="online", agent_name="星瑶 (Y-code)")
            return {
                "ok": True,
                "online": True,
                "protocol": "lan_ws",
                "agent_name": "星瑶 (Y-code)",
                "description": "局域网 WebSocket 直连通道",
                "latency_ms": latency_ms,
                "msg": f"连接正常（{latency_ms}ms）",
            }
        except Exception as exc:
            err_msg = str(exc)
            if "4401" in err_msg or "invalid token" in err_msg.lower():
                err_msg = "令牌无效或已过期"
            _update_peer_probe_status(peer_id, status="error", error=err_msg)
            return {"ok": False, "online": False, "protocol": "lan_ws", "msg": err_msg}

    base_url, _ = _a2a_urls(str(peer.get("url") or ""))
    agent_name = ""
    description = ""
    card_ok = False
    try:
        card_status, card_body = _http_json("GET", f"{base_url}/.well-known/agent.json", timeout=6.0)
        if card_status == 200 and isinstance(card_body, dict):
            card_ok = True
            agent_name = str(card_body.get("name") or "")
            description = str(card_body.get("description") or "")
    except Exception:
        card_ok = False

    try:
        status_code, status_body = _http_json(
            "GET", f"{base_url}/api/a2a/status", token=token, timeout=6.0
        )
    except Exception as exc:
        err_msg = f"无法连接节点: {exc}"
        _update_peer_probe_status(peer_id, status="offline", error=err_msg)
        return {"ok": False, "online": False, "protocol": "a2a", "msg": err_msg}

    latency_ms = int((time.monotonic() - t0) * 1000)
    if status_code in (401, 403):
        err_msg = "鉴权失败，请检查令牌"
        _update_peer_probe_status(peer_id, status="error", agent_name=agent_name, error=err_msg)
        return {"ok": False, "online": False, "protocol": "a2a", "agent_name": agent_name, "msg": err_msg}

    if status_code == 200 and isinstance(status_body, dict):
        if not agent_name:
            agent_name = str(status_body.get("agent") or "A2A Agent")
        _update_peer_probe_status(peer_id, status="online", agent_name=agent_name)
        return {
            "ok": True,
            "online": True,
            "protocol": "a2a",
            "agent_name": agent_name,
            "description": description,
            "status_detail": status_body,
            "latency_ms": latency_ms,
            "msg": f"连接正常（{agent_name}，{latency_ms}ms）",
        }

    if status_code == 404 and card_ok:
        _update_peer_probe_status(peer_id, status="online", agent_name=agent_name or "A2A Agent")
        return {
            "ok": True,
            "online": True,
            "protocol": "a2a",
            "agent_name": agent_name or "A2A Agent",
            "description": description,
            "latency_ms": latency_ms,
            "msg": f"已发现 A2A AgentCard（{agent_name or 'A2A Agent'}，{latency_ms}ms）",
        }

    err_msg = f"探测失败（HTTP {status_code}）"
    _update_peer_probe_status(peer_id, status="error", error=err_msg)
    return {"ok": False, "online": False, "protocol": "a2a", "msg": err_msg}


def _a2a_rpc_call(peer: dict[str, Any], method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    base_url, rpc_url = _a2a_urls(str(peer.get("url") or ""))
    token = str(peer.get("token") or "")
    payload = {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex[:8],
        "method": method,
        "params": params,
    }
    status_code, body = _http_json("POST", rpc_url, payload=payload, token=token, timeout=timeout)
    if status_code in (401, 403):
        raise RuntimeError("节点鉴权失败，请检查令牌")
    if status_code >= 400:
        raise RuntimeError(f"节点返回 HTTP {status_code}: {body}")
    if not isinstance(body, dict):
        raise RuntimeError("节点返回格式非法")
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise RuntimeError(str(msg or "A2A 调用错误"))
    result = body.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("A2A 响应缺少 result")
    return result


def _a2a_send(
    peer: dict[str, Any],
    text: str,
    *,
    session_name: str,
    workdir: Optional[str],
    wait_seconds: int,
    caller: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "session_name": session_name,
        "wait_seconds": wait_seconds,
        "hop": 0,
        "caller": caller,
    }
    if workdir:
        metadata["workdir"] = workdir
    params = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
        },
        "metadata": metadata,
    }
    res = _a2a_rpc_call(peer, "message/send", params, timeout=max(15.0, float(wait_seconds) + 15.0))
    return _parse_a2a_task_response(res)


def _a2a_get_task(peer: dict[str, Any], remote_task_id: str, *, wait_seconds: int) -> dict[str, Any]:
    res = _a2a_rpc_call(
        peer,
        "tasks/get",
        {"id": remote_task_id, "wait_seconds": wait_seconds},
        timeout=max(12.0, float(wait_seconds) + 15.0),
    )
    return _parse_a2a_task_response(res)


def _a2a_approve(
    peer: dict[str, Any],
    request_id: str,
    allow: bool,
    always: bool,
    *,
    remote_task_id: str = "",
    wait_seconds: int = 25,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "request_id": request_id,
        "allow": bool(allow),
        "always": bool(always),
        "wait_seconds": wait_seconds,
    }
    if remote_task_id:
        params["task_id"] = remote_task_id
    res = _a2a_rpc_call(
        peer,
        "xingyao/permission",
        params,
        timeout=max(15.0, float(wait_seconds) + 15.0),
    )
    if not res.get("ok", True):
        raise RuntimeError(str(res.get("msg") or "权限审批提交失败"))
    if isinstance(res.get("task"), dict):
        return _parse_a2a_task_response(res["task"])
    return {
        "remote_task_id": remote_task_id,
        "status": "working" if allow else "canceled",
        "reply": "",
        "error": "" if allow else "已拒绝权限请求",
        "permission": None,
        "remote_session": "",
    }


def _ws_consume_events(
    events: list[dict[str, Any]],
    target_session: str,
    state: dict[str, Any],
) -> None:
    for evt in events:
        etype = str(evt.get("type") or "")
        data = evt.get("data")
        if not isinstance(data, dict):
            continue
        evt_sess = str(data.get("session_name") or "").strip()
        if evt_sess and target_session and evt_sess != target_session:
            continue
        if etype == "message_final":
            role = str(data.get("role") or "").strip().lower()
            content = str(data.get("content") or data.get("text") or "").strip()
            if content and role in ("", "assistant"):
                state["reply"] = content
        elif etype == "turn_finish":
            content = str(data.get("content") or data.get("reply") or "").strip()
            if content:
                state["reply"] = content
            state["status"] = "completed"
            state["permission"] = None
            return
        elif etype == "turn_error":
            state["status"] = "failed"
            state["error"] = str(data.get("error") or data.get("msg") or data.get("message") or "执行失败")
            state["permission"] = None
            return
        elif etype == "turn_cancelled":
            state["status"] = "canceled"
            state["permission"] = None
            return
        elif etype == "permission_request":
            state["status"] = "input-required"
            state["permission"] = dict(data)
            return
        elif etype == "permission_dismiss":
            if state.get("status") == "input-required":
                state["status"] = "working"
                state["permission"] = None


def _ws_wait_events(ws, target_session: str, wait_seconds: int, state: dict[str, Any]) -> None:
    deadline = time.monotonic() + max(0, int(wait_seconds))
    while state["status"] not in _TERMINAL_STATES and state["status"] != "input-required":
        rem = deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            raw = ws.recv(timeout=min(1.0, rem))
        except TimeoutError:
            continue
        except Exception as exc:
            err_text = str(exc).lower()
            if "timed out" in err_text or "timeout" in err_text:
                continue
            raise
        try:
            msg = json.loads(raw) if isinstance(raw, str) else {}
        except Exception:
            continue
        if isinstance(msg, dict) and "type" in msg:
            _ws_consume_events([msg], target_session, state)


def _ws_fetch_latest_reply(ws, rpc_id: int, target_session: str) -> str:
    try:
        res, _ = _ws_rpc(
            ws,
            rpc_id,
            "load_older_messages",
            {"session_name": target_session, "before_id": 2_147_483_647},
            timeout=6.0,
        )
        if isinstance(res, dict) and res.get("ok"):
            return _extract_latest_assistant_text(res.get("messages"))
    except Exception:
        pass
    return ""


def _ws_send(
    peer: dict[str, Any],
    text: str,
    *,
    session_name: str,
    workdir: Optional[str],
    wait_seconds: int,
) -> dict[str, Any]:
    ws = _ws_connect_and_auth(str(peer["url"]), str(peer.get("token") or ""), timeout=10.0)
    try:
        rpc_args: dict[str, Any] = {"text": text, "session_name": session_name}
        if workdir:
            rpc_args["workdir"] = workdir
        res, buffered = _ws_rpc(ws, 1, "send_user_message", rpc_args, timeout=12.0)
        if not isinstance(res, dict) or not res.get("ok"):
            msg = str(res.get("msg") or "") if isinstance(res, dict) else ""
            return {
                "remote_task_id": "",
                "status": "failed",
                "reply": "",
                "error": f"发送失败：{msg}" if msg else "发送失败：对端未受理",
                "permission": None,
                "remote_session": session_name,
            }
        state: dict[str, Any] = {
            "status": "working",
            "reply": "",
            "error": "",
            "permission": None,
        }
        _ws_consume_events(buffered, session_name, state)
        if wait_seconds > 0 and state["status"] == "working":
            _ws_wait_events(ws, session_name, wait_seconds, state)

        if state["status"] == "completed" and not state["reply"]:
            state["reply"] = _ws_fetch_latest_reply(ws, 2, session_name)
        elif state["status"] == "working":
            try:
                perm_res, _ = _ws_rpc(ws, 3, "get_active_permission", {}, timeout=5.0)
                perm = perm_res.get("permission") if isinstance(perm_res, dict) else None
                if isinstance(perm, dict):
                    psess = str(perm.get("session_name") or "").strip()
                    if not psess or psess == session_name:
                        state["status"] = "input-required"
                        state["permission"] = perm
            except Exception:
                pass
            if state["status"] == "working":
                try:
                    snap, _ = _ws_rpc(
                        ws, 4, "get_active_turn_snapshot", {"session_name": session_name}, timeout=5.0
                    )
                    if isinstance(snap, dict):
                        state["progress"] = {
                            "tool_count": len(snap.get("tool_calls") or []),
                            "streaming_chars": len(str(snap.get("streaming_text") or "")),
                        }
                except Exception:
                    pass

        return {
            "remote_task_id": "",
            "status": state["status"],
            "reply": state["reply"],
            "error": state["error"],
            "permission": state["permission"],
            "progress": state.get("progress"),
            "remote_session": session_name,
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _ws_poll(peer: dict[str, Any], remote_session: str, *, wait_seconds: int) -> dict[str, Any]:
    ws = _ws_connect_and_auth(str(peer["url"]), str(peer.get("token") or ""), timeout=10.0)
    try:
        state: dict[str, Any] = {
            "status": "working",
            "reply": "",
            "error": "",
            "permission": None,
        }
        perm_res, buffered = _ws_rpc(ws, 1, "get_active_permission", {}, timeout=5.0)
        _ws_consume_events(buffered, remote_session, state)
        perm = perm_res.get("permission") if isinstance(perm_res, dict) else None
        if isinstance(perm, dict):
            psess = str(perm.get("session_name") or "").strip()
            if not psess or psess == remote_session:
                state["status"] = "input-required"
                state["permission"] = perm
        if state["status"] == "working":
            snap, buffered2 = _ws_rpc(
                ws, 2, "get_active_turn_snapshot", {"session_name": remote_session}, timeout=5.0
            )
            _ws_consume_events(buffered2, remote_session, state)
            if snap is None and state["status"] == "working":
                reply = _ws_fetch_latest_reply(ws, 3, remote_session)
                if reply:
                    state["status"] = "completed"
                    state["reply"] = reply
            elif isinstance(snap, dict) and wait_seconds > 0 and state["status"] == "working":
                _ws_wait_events(ws, remote_session, wait_seconds, state)
                if state["status"] == "completed" and not state["reply"]:
                    state["reply"] = _ws_fetch_latest_reply(ws, 4, remote_session)
        return {
            "remote_task_id": "",
            "status": state["status"],
            "reply": state["reply"],
            "error": state["error"],
            "permission": state["permission"],
            "remote_session": remote_session,
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _ws_approve(
    peer: dict[str, Any],
    request_id: str,
    allow: bool,
    always: bool,
    *,
    remote_session: str,
    wait_seconds: int,
) -> dict[str, Any]:
    ws = _ws_connect_and_auth(str(peer["url"]), str(peer.get("token") or ""), timeout=10.0)
    try:
        res, buffered = _ws_rpc(
            ws,
            1,
            "respond_permission",
            {"request_id": request_id, "allow": bool(allow), "always": bool(always)},
            timeout=8.0,
        )
        if isinstance(res, dict) and not res.get("ok", True):
            raise RuntimeError(str(res.get("msg") or "权限提交失败"))
        state: dict[str, Any] = {
            "status": "working" if allow else "canceled",
            "reply": "",
            "error": "" if allow else "用户拒绝了权限请求",
            "permission": None,
        }
        _ws_consume_events(buffered, remote_session, state)
        if allow and wait_seconds > 0 and state["status"] == "working":
            _ws_wait_events(ws, remote_session, wait_seconds, state)
            if state["status"] == "completed" and not state["reply"] and remote_session:
                state["reply"] = _ws_fetch_latest_reply(ws, 2, remote_session)
        return {
            "remote_task_id": "",
            "status": state["status"],
            "reply": state["reply"],
            "error": state["error"],
            "permission": state["permission"],
            "remote_session": remote_session,
        }
    finally:
        try:
            ws.close()
        except Exception:
            pass


class LanPeerTaskManager:
    """管理本机星瑶向局域网其它节点派发任务、轮询与后台完成后自动回传。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_order: list[str] = []
        self._watching: set[str] = set()
        self._shutdown_event = threading.Event()
        self._task_callback: Optional[Callable[[dict[str, Any]], None]] = None

    def set_task_callback(self, callback: Optional[Callable[[dict[str, Any]], None]]) -> None:
        with self._lock:
            self._task_callback = callback

    def shutdown(self) -> None:
        self._shutdown_event.set()

    def reset_for_tests(self) -> None:
        self._shutdown_event.clear()
        with self._lock:
            self._tasks.clear()
            self._task_order.clear()
            self._watching.clear()
            self._task_callback = None

    def _record_task(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tid = record["local_task_id"]
            self._tasks[tid] = record
            self._task_order.append(tid)
            while len(self._task_order) > _MAX_LOCAL_TASKS:
                oldest = self._task_order.pop(0)
                if oldest != tid:
                    self._tasks.pop(oldest, None)
            return dict(record)

    def get_local_task(self, local_task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._tasks.get(str(local_task_id or "").strip())
            return dict(item) if item is not None else None

    def _fire_callback(self, record: dict[str, Any]) -> None:
        with self._lock:
            cb = self._task_callback
        if cb is not None and not self._shutdown_event.is_set():
            try:
                cb(dict(record))
            except Exception:
                logger.debug("LanPeerTaskManager 回调执行异常", exc_info=True)

    def _start_watcher_if_needed(self, local_task_id: str) -> None:
        with self._lock:
            if local_task_id in self._watching or self._shutdown_event.is_set():
                return
            self._watching.add(local_task_id)
        t = threading.Thread(
            target=self._watch_task_loop,
            args=(local_task_id,),
            daemon=True,
            name=f"lan-peer-watch-{local_task_id}",
        )
        t.start()

    def _watch_task_loop(self, local_task_id: str) -> None:
        deadline = time.monotonic() + _WATCH_MAX_SECONDS
        try:
            while not self._shutdown_event.is_set() and time.monotonic() < deadline:
                snap = self.get_local_task(local_task_id)
                if snap is None:
                    return
                if snap["status"] in _TERMINAL_STATES or snap["status"] == "input-required":
                    return
                try:
                    updated = self.poll_task(local_task_id, wait_seconds=20, from_watcher=True)
                except Exception as exc:
                    logger.debug("后台监视节点任务 %s 出错: %s", local_task_id, exc)
                    if self._shutdown_event.wait(3.0):
                        return
                    continue
                status = str(updated.get("status") or "")
                if status in _TERMINAL_STATES or status == "input-required":
                    self._fire_callback(updated)
                    return
                if self._shutdown_event.wait(0.5):
                    return
        finally:
            with self._lock:
                self._watching.discard(local_task_id)

    def send_to_peer(
        self,
        peer_id: str,
        text: str,
        *,
        session_name: str = "",
        workdir: str = "",
        wait_seconds: int = _DEFAULT_WAIT_SECONDS,
        auto_report: bool = True,
    ) -> dict[str, Any]:
        turn_source = str(app_context.get_thread_turn_source() or "").strip().lower()
        if turn_source in ("mcp", "lan"):
            return {
                "ok": False,
                "status": "rejected",
                "msg": "当前回合来自远程委派，为防止跨机循环派活已禁止继续向其他节点派发任务。",
            }

        clean_text = str(text or "").strip()
        if not clean_text:
            return {"ok": False, "status": "invalid", "msg": "text 不能为空"}

        peer = get_peer(peer_id, include_secret=True)
        if peer is None:
            return {"ok": False, "status": "not_found", "msg": f"未找到节点: {peer_id}"}
        if not peer.get("enabled", True):
            return {"ok": False, "status": "disabled", "msg": f"节点 {peer['name']} 已停用"}

        protocol = _resolve_protocol(peer)
        target_session = (
            str(session_name or "").strip()
            or str(peer.get("default_session") or "").strip()
            or f"peer-from-{_local_host_slug()}"
        )
        target_workdir = str(workdir or "").strip() or str(peer.get("default_workdir") or "").strip() or None
        wait = _clamp_wait(wait_seconds, _DEFAULT_WAIT_SECONDS)
        origin_session = app_context.get_thread_session() or app_context.get_current_session() or ""

        try:
            if protocol == "lan_ws":
                res = _ws_send(
                    peer,
                    clean_text,
                    session_name=target_session,
                    workdir=target_workdir,
                    wait_seconds=wait,
                )
            else:
                res = _a2a_send(
                    peer,
                    clean_text,
                    session_name=target_session,
                    workdir=target_workdir,
                    wait_seconds=wait,
                    caller=_local_host_slug(),
                )
        except Exception as exc:
            return {"ok": False, "status": "error", "msg": f"向节点 {peer['name']} 派活失败：{exc}"}

        local_task_id = f"ltask-{uuid.uuid4().hex[:10]}"
        now = time.time()
        record = {
            "local_task_id": local_task_id,
            "peer_id": peer["id"],
            "peer_name": peer["name"],
            "protocol": protocol,
            "remote_task_id": str(res.get("remote_task_id") or ""),
            "origin_session": origin_session,
            "remote_session": str(res.get("remote_session") or target_session),
            "prompt_preview": clean_text[:120],
            "status": str(res.get("status") or "working"),
            "reply": str(res.get("reply") or ""),
            "error": str(res.get("error") or ""),
            "permission": res.get("permission"),
            "progress": res.get("progress"),
            "auto_report": bool(auto_report),
            "created_at": now,
            "updated_at": now,
        }
        saved = self._record_task(record)

        watching = False
        if saved["status"] == "working" and auto_report and origin_session:
            self._start_watcher_if_needed(local_task_id)
            watching = True
        saved["watching"] = watching
        saved["ok"] = saved["status"] not in ("failed",)
        return saved

    def poll_task(
        self,
        task_id: str,
        *,
        wait_seconds: int = 25,
        from_watcher: bool = False,
    ) -> dict[str, Any]:
        clean_id = str(task_id or "").strip()
        if not clean_id:
            return {"ok": False, "status": "invalid", "msg": "task_id 不能为空"}
        with self._lock:
            record = self._tasks.get(clean_id)
            if record is None:
                for cand in reversed(self._task_order):
                    item = self._tasks.get(cand)
                    if item and item.get("remote_task_id") == clean_id:
                        record = item
                        break
            snapshot = dict(record) if record is not None else None
        if snapshot is None:
            return {"ok": False, "status": "not_found", "msg": f"未找到远程任务: {clean_id}"}

        if snapshot["status"] in _TERMINAL_STATES:
            snapshot["ok"] = snapshot["status"] != "failed"
            return snapshot

        peer = get_peer(snapshot["peer_id"], include_secret=True)
        if peer is None:
            return {"ok": False, "status": "not_found", "msg": f"节点不存在: {snapshot['peer_id']}"}

        wait = _clamp_wait(wait_seconds, 25)
        if snapshot["protocol"] == "a2a" and snapshot["remote_task_id"]:
            res = _a2a_get_task(peer, snapshot["remote_task_id"], wait_seconds=wait)
        else:
            res = _ws_poll(peer, snapshot["remote_session"], wait_seconds=wait)

        with self._lock:
            target = self._tasks.get(snapshot["local_task_id"])
            if target is not None:
                target["status"] = str(res.get("status") or target["status"])
                if res.get("reply"):
                    target["reply"] = str(res["reply"])
                if res.get("error"):
                    target["error"] = str(res["error"])
                target["permission"] = res.get("permission")
                target["progress"] = res.get("progress")
                target["updated_at"] = time.time()
                updated = dict(target)
            else:
                updated = snapshot
        updated["ok"] = updated["status"] != "failed"
        if not from_watcher and updated["status"] == "working" and updated.get("auto_report") and updated.get("origin_session"):
            self._start_watcher_if_needed(updated["local_task_id"])
        return updated

    def approve_permission(
        self,
        peer_id: str,
        request_id: str,
        allow: bool,
        always: bool = False,
        *,
        task_id: str = "",
        wait_seconds: int = 25,
    ) -> dict[str, Any]:
        peer = get_peer(peer_id, include_secret=True)
        if peer is None:
            return {"ok": False, "msg": f"未找到节点: {peer_id}"}
        protocol = _resolve_protocol(peer)
        wait = _clamp_wait(wait_seconds, 25)

        local_rec = self.get_local_task(task_id) if task_id else None
        if local_rec is None:
            with self._lock:
                for cand_id in reversed(self._task_order):
                    cand = self._tasks.get(cand_id)
                    if cand and cand.get("peer_id") == peer["id"] and (cand.get("permission") or {}).get("request_id") == request_id:
                        local_rec = dict(cand)
                        break

        remote_tid = str((local_rec or {}).get("remote_task_id") or "")
        remote_sess = str((local_rec or {}).get("remote_session") or "")

        try:
            if protocol == "lan_ws":
                res = _ws_approve(
                    peer,
                    request_id,
                    allow,
                    always,
                    remote_session=remote_sess,
                    wait_seconds=wait,
                )
            else:
                res = _a2a_approve(
                    peer,
                    request_id,
                    allow,
                    always,
                    remote_task_id=remote_tid,
                    wait_seconds=wait,
                )
        except Exception as exc:
            return {"ok": False, "msg": f"权限确认提交失败：{exc}"}

        if local_rec is not None:
            with self._lock:
                target = self._tasks.get(local_rec["local_task_id"])
                if target is not None:
                    target["status"] = str(res.get("status") or target["status"])
                    if res.get("reply"):
                        target["reply"] = str(res["reply"])
                    if res.get("error"):
                        target["error"] = str(res["error"])
                    target["permission"] = res.get("permission")
                    target["updated_at"] = time.time()
                    updated = dict(target)
                else:
                    updated = dict(res)
            if updated.get("status") == "working" and updated.get("auto_report") and updated.get("origin_session"):
                self._start_watcher_if_needed(updated["local_task_id"])
            updated["ok"] = updated.get("status") != "failed"
            return updated

        return {"ok": True, **res}


lan_peer_manager = LanPeerTaskManager()


def _format_task_result(res: dict[str, Any]) -> str:
    if not res.get("ok") and res.get("status") in ("rejected", "invalid", "not_found", "disabled", "error"):
        return f"🚨 {res.get('msg') or res.get('error') or '远程调用失败'}"

    peer_name = res.get("peer_name") or res.get("peer_id") or "远程节点"
    task_id = res.get("local_task_id") or res.get("remote_task_id") or ""
    status = str(res.get("status") or "")

    if status == "completed":
        reply = str(res.get("reply") or "").strip() or "（对端已执行完成，本轮无文本输出）"
        return f"✅ [{peer_name}] 执行完成（task_id={task_id}）：\n\n{reply}"
    if status == "input-required":
        perm = res.get("permission") if isinstance(res.get("permission"), dict) else {}
        req_id = perm.get("request_id") or ""
        tool_name = perm.get("tool_name") or ""
        desc = perm.get("description") or "对端请求确认操作权限"
        return (
            f"⏸️ [{peer_name}] 等待权限确认（task_id={task_id}, request_id={req_id}）：\n"
            f"- 工具: {tool_name or '未知'}\n"
            f"- 说明: {desc}\n\n"
            f"请先向用户确认，再调用 lan_peer_approve(peer=\"{res.get('peer_id')}\", "
            f"request_id=\"{req_id}\", allow=true/false, task_id=\"{task_id}\")。"
        )
    if status in ("failed", "canceled"):
        err = res.get("error") or res.get("msg") or ("任务已取消" if status == "canceled" else "执行失败")
        return f"🚨 [{peer_name}] 任务{('已取消' if status == 'canceled' else '失败')}（task_id={task_id}）：{err}"

    watching_note = "已转入后台自动监听，完成后会自动把结果推回本会话。" if res.get("watching") else ""
    return (
        f"⏳ [{peer_name}] 任务仍在执行中（task_id={task_id}，status={status}）。"
        f"{watching_note} 如需立即查看可调用 lan_peer_poll(task_id=\"{task_id}\")。"
    )


def tool_lan_peer_list(probe: bool = False) -> str:
    peers = list_peers()
    if not peers:
        return "当前未配置任何局域网协作节点。请先在「设置 → 局域网协作」中添加对端星瑶或 A2A Agent。"
    lines: list[str] = []
    for p in peers:
        line = (
            f"- {p['name']} (id={p['id']}, protocol={_resolve_protocol(p)}, "
            f"url={p['url']}, enabled={p['enabled']})"
        )
        if p.get("description"):
            line += f" — {p['description']}"
        if probe and p.get("enabled", True):
            pr = probe_peer(p["id"])
            line += f" | 在线={pr.get('online')} ({pr.get('msg')})"
        elif p.get("last_status"):
            line += f" | 上次状态={p['last_status']}"
        lines.append(line)
    return "\n".join(lines)


def tool_lan_peer_send(
    peer: str,
    text: str,
    session_name: str = "",
    workdir: str = "",
    wait_seconds: int = _DEFAULT_WAIT_SECONDS,
) -> str:
    res = lan_peer_manager.send_to_peer(
        peer,
        text,
        session_name=session_name,
        workdir=workdir,
        wait_seconds=wait_seconds,
        auto_report=True,
    )
    return _format_task_result(res)


def tool_lan_peer_poll(task_id: str, wait_seconds: int = 25) -> str:
    res = lan_peer_manager.poll_task(task_id, wait_seconds=wait_seconds)
    return _format_task_result(res)


def tool_lan_peer_approve(
    peer: str,
    request_id: str,
    allow: bool,
    always: bool = False,
    task_id: str = "",
    wait_seconds: int = 25,
) -> str:
    res = lan_peer_manager.approve_permission(
        peer,
        request_id,
        allow,
        always,
        task_id=task_id,
        wait_seconds=wait_seconds,
    )
    return _format_task_result(res)
