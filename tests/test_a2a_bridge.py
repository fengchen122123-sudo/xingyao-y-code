import http.client
import json
import threading
import time

import pytest

from core.mcp_server import McpServer

TOKEN = "a2a-tok-123"


class FakeA2ABridge:
    def __init__(self, *, mode="complete", reply_text="B电脑执行完毕：测试全部通过"):
        self.mode = mode
        self.reply_text = reply_text
        self.sent_calls = []
        self.permission_calls = []
        self.stopped_sessions = []
        self.mcp_server = None
        self._current_session = "local-main"
        self._active_permission = None

    def send_user_message(
        self,
        text,
        session_name=None,
        attachments=None,
        workdir=None,
        model_id=None,
        permission_mode=None,
        turn_source="local",
    ):
        self.sent_calls.append(
            {
                "text": text,
                "session_name": session_name,
                "workdir": workdir,
                "turn_source": turn_source,
            }
        )
        sess = session_name or self._current_session

        def _bg():
            time.sleep(0.05)
            srv = self.mcp_server
            if srv is None:
                return
            if self.mode == "complete":
                srv.on_event("turn_start", {"session_name": sess})
                srv.on_event(
                    "message_final",
                    {"session_name": sess, "role": "assistant", "content": self.reply_text},
                )
                srv.on_event("turn_finish", {"session_name": sess})
            elif self.mode == "permission":
                perm = {
                    "request_id": "req-101",
                    "session_name": sess,
                    "tool_name": "execute_command",
                    "description": "执行 pytest",
                }
                self._active_permission = perm
                srv.on_event("permission_request", perm)
            elif self.mode == "slow":
                srv.on_event("turn_start", {"session_name": sess})

        threading.Thread(target=_bg, daemon=True).start()
        return {"ok": True}

    def respond_permission(self, request_id, allow, always=False):
        self.permission_calls.append((request_id, allow, always))
        self._active_permission = None
        srv = self.mcp_server
        sess = self.sent_calls[-1]["session_name"] if self.sent_calls else "a2a-remote"
        if srv is not None and allow:
            def _finish():
                time.sleep(0.05)
                srv.on_event("permission_dismiss", {"request_id": request_id, "session_name": sess})
                srv.on_event(
                    "message_final",
                    {"session_name": sess, "role": "assistant", "content": "审批后继续完成"},
                )
                srv.on_event("turn_finish", {"session_name": sess})

            threading.Thread(target=_finish, daemon=True).start()
        return {"ok": True}

    def stop_current_turn(self, session_name=""):
        self.stopped_sessions.append(session_name)
        return {"ok": True}

    def get_active_permission(self):
        return {"permission": self._active_permission}


def _http(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def _a2a_rpc(srv, method, params, token=TOKEN):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    status, _h, body = _http(srv, "POST", "/a2a", raw, headers)
    return status, json.loads(body.decode("utf-8"))


@pytest.fixture
def make_server():
    created = []

    def _create(mode="complete", reply_text="B电脑执行完毕：测试全部通过"):
        bridge = FakeA2ABridge(mode=mode, reply_text=reply_text)
        srv = McpServer(bridge=bridge, port=0, host="127.0.0.1", token_provider=lambda: TOKEN)
        bridge.mcp_server = srv
        info = srv.start()
        assert info["ok"]
        created.append(srv)
        return srv, bridge

    yield _create
    for srv in created:
        srv.stop()


def test_agent_card_public_and_no_secret(make_server):
    srv, _ = make_server()
    status, _h, body = _http(srv, "GET", "/.well-known/agent.json")
    assert status == 200
    card = json.loads(body.decode("utf-8"))
    assert card["name"] == "星瑶 (Y-code)"
    assert card["url"].endswith("/a2a")
    assert TOKEN not in json.dumps(card, ensure_ascii=False)


def test_a2a_endpoints_require_bearer_token(make_server):
    srv, _ = make_server()
    for method, path in (
        ("GET", "/api/a2a/status"),
        ("POST", "/a2a"),
        ("POST", "/api/a2a/send"),
        ("POST", "/api/a2a/permission"),
    ):
        status, _h, _b = _http(srv, method, path, body=b"{}" if method == "POST" else None)
        assert status == 401


def test_a2a_jsonrpc_send_and_complete(make_server):
    srv, bridge = make_server(mode="complete", reply_text="已检查完毕，共 0 个错误")
    status, body = _a2a_rpc(
        srv,
        "message/send",
        {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "跑一下单测"}]},
            "metadata": {"caller": "PC-A", "wait_seconds": 5},
        },
    )
    assert status == 200
    task = body["result"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "已检查完毕，共 0 个错误"
    assert bridge.sent_calls[0]["session_name"] == "peer-PC-A"
    assert bridge.sent_calls[0]["turn_source"] == "mcp"


def test_a2a_permission_required_and_approve_roundtrip(make_server):
    srv, bridge = make_server(mode="permission")
    status, body = _a2a_rpc(
        srv,
        "message/send",
        {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "执行敏感命令"}]},
            "metadata": {"wait_seconds": 5},
        },
    )
    assert status == 200
    task = body["result"]
    assert task["status"]["state"] == "input-required"
    assert task["metadata"]["permission"]["request_id"] == "req-101"

    status2, body2 = _a2a_rpc(
        srv,
        "xingyao/permission",
        {
            "request_id": "req-101",
            "allow": True,
            "always": False,
            "task_id": task["id"],
            "wait_seconds": 5,
        },
    )
    assert status2 == 200
    res = body2["result"]
    assert res["ok"] is True
    assert bridge.permission_calls == [("req-101", True, False)]
    assert res["task"]["status"]["state"] == "completed"
    assert res["task"]["artifacts"][0]["parts"][0]["text"] == "审批后继续完成"


def test_a2a_tasks_get_and_cancel(make_server):
    srv, bridge = make_server(mode="slow")
    _, body = _a2a_rpc(
        srv,
        "message/send",
        {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "长任务"}]},
            "metadata": {"session_name": "isolated-b", "wait_seconds": 0},
        },
    )
    task_id = body["result"]["id"]
    assert body["result"]["status"]["state"] == "working"

    _, get_body = _a2a_rpc(srv, "tasks/get", {"id": task_id, "wait_seconds": 0})
    assert get_body["result"]["id"] == task_id
    assert get_body["result"]["status"]["state"] == "working"

    _, cancel_body = _a2a_rpc(srv, "tasks/cancel", {"id": task_id})
    assert cancel_body["result"]["status"]["state"] == "canceled"
    assert bridge.stopped_sessions == ["isolated-b"]


def test_a2a_hop_limit_prevents_loop(make_server):
    srv, bridge = make_server()
    _, body = _a2a_rpc(
        srv,
        "message/send",
        {
            "message": {"role": "user", "parts": [{"kind": "text", "text": "二次转发"}]},
            "metadata": {"hop": 1, "wait_seconds": 2},
        },
    )
    task = body["result"]
    assert task["status"]["state"] == "failed"
    assert "跳数上限" in task["metadata"]["error"]
    assert bridge.sent_calls == []


def test_a2a_rest_status_and_send(make_server):
    srv, _ = make_server(mode="complete", reply_text="REST OK")
    status, _h, raw = _http(
        srv,
        "GET",
        "/api/a2a/status",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 200
    assert json.loads(raw.decode("utf-8"))["online"] is True

    status2, _h2, raw2 = _http(
        srv,
        "POST",
        "/api/a2a/send",
        body=json.dumps({"text": "你好", "wait_seconds": 5}).encode("utf-8"),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
    )
    assert status2 == 200
    task = json.loads(raw2.decode("utf-8"))
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "REST OK"
