from __future__ import annotations

import threading
import time

import httpx
import pytest
import uvicorn
import websockets.sync.client
from fastapi.testclient import TestClient

from core.cloud_relay.client import CloudRelayClient
from core.lan_server import LanServer
from relay_server import create_app
from relay_server.__main__ import main as relay_cli_main
from relay_server.db import connect
from relay_server.registry import RelayRegistry

SECRET = "test-jwt-secret-key-at-least-32-bytes"


class _FakeBridge:
    def __init__(self):
        self.sent = []
        self.stopped = []
        self.perm_responses = []
        self.pending_permission = None
        self.current_session = "session_main"
        self.active_model_id = "ds-chat"
        self.sessions = [
            {
                "folder_path": "E:/proj",
                "folder_name": "proj",
                "is_current": True,
                "sessions": [
                    {
                        "name": "session_main",
                        "display_name": "主会话",
                        "message_count": 4,
                        "is_current": True,
                        "is_active": False,
                    }
                ],
            },
            {
                "folder_path": "E:/other-app",
                "folder_name": "other-app",
                "is_current": False,
                "sessions": [
                    {
                        "name": "session_other",
                        "display_name": "另一个项目会话",
                        "message_count": 2,
                        "is_current": False,
                        "is_active": False,
                    }
                ],
            },
        ]
        self.models_catalog = [
            {
                "id": "prov-ds",
                "name": "DeepSeek",
                "protocol": "openai",
                "has_api_key": True,
                "models": [
                    {
                        "id": "ds-chat",
                        "name": "DeepSeek Chat",
                        "provider_id": "prov-ds",
                        "provider_name": "DeepSeek",
                        "context_window": 128000,
                        "supports_vision": False,
                        "supports_function_calling": True,
                    },
                    {
                        "id": "ds-reasoner",
                        "name": "DeepSeek Reasoner",
                        "provider_id": "prov-ds",
                        "provider_name": "DeepSeek",
                        "context_window": 128000,
                        "supports_vision": False,
                        "supports_function_calling": True,
                    },
                ],
            }
        ]
        self.messages_by_session = {
            "session_main": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "电脑上的星瑶已就绪"},
            ],
            "session_other": [
                {"role": "user", "content": "看看 other"},
                {"role": "assistant", "content": "已打开 other-app"},
            ],
        }
        self.client: CloudRelayClient | None = None

    def send_user_message(
        self,
        text: str,
        session_name: str | None = None,
        attachments=None,
        workdir: str | None = None,
        model_id: str | None = None,
        permission_mode: str | None = None,
        turn_source: str = "local",
    ):
        sess = session_name or self.current_session
        self.sent.append(
            {
                "text": text,
                "session_name": sess,
                "workdir": workdir,
                "model_id": model_id,
                "turn_source": turn_source,
            }
        )
        if self.client is not None:
            if "permission" in text:
                def _emit_perm():
                    time.sleep(0.05)
                    self.pending_permission = {
                        "request_id": "req-42",
                        "function_name": "run_command",
                        "description": "执行 pytest",
                        "options": ["允许", "拒绝"],
                    }
                    self.client.submit("turn_start", {"session_name": sess})
                    self.client.submit("permission_request", self.pending_permission)

                threading.Thread(target=_emit_perm, daemon=True).start()
            elif "slow" in text:
                def _emit_slow():
                    time.sleep(0.05)
                    self.client.submit("turn_start", {"session_name": sess})
                    self.client.submit("token_delta", {"session_name": sess, "text": "正在处理..."})

                threading.Thread(target=_emit_slow, daemon=True).start()
            else:
                def _emit_reply():
                    time.sleep(0.05)
                    self.client.submit("turn_start", {"session_name": sess})
                    reply_text = f"星瑶已处理：{text}"
                    self.messages_by_session.setdefault(sess, []).append(
                        {"role": "assistant", "content": reply_text}
                    )
                    self.client.submit(
                        "message_final", {"session_name": sess, "content": reply_text}
                    )
                    self.client.submit("turn_finish", {"session_name": sess})

                threading.Thread(target=_emit_reply, daemon=True).start()
        return {"ok": True}

    def get_initial_state(self):
        return {
            "current_session": self.current_session,
            "workdir": "E:/proj",
            "active_sessions": [],
            "active_turn_snapshots": {},
            "messages": self.messages_by_session.get(self.current_session, []),
        }

    def get_sessions(self):
        return self.sessions

    def get_model_settings(self):
        flat = [m for p in self.models_catalog for m in p["models"]]
        return {
            "providers": self.models_catalog,
            "models": flat,
            "active_model_id": self.active_model_id,
        }

    def set_model(self, model_id: str, session_name: str | None = None):
        flat = [m for p in self.models_catalog for m in p["models"]]
        matched = next((m for m in flat if m["id"] == model_id), None)
        if not matched:
            return {"ok": False, "msg": f"未找到模型: {model_id}"}
        self.active_model_id = model_id
        return {
            "ok": True,
            "active_model_id": model_id,
            "active_model_name": matched["name"],
        }

    def switch_session(self, session_name: str, target_workdir: str | None = None):
        self.current_session = session_name
        return {
            "ok": True,
            "session_name": session_name,
            "workdir": target_workdir or ("E:/other-app" if session_name == "session_other" else "E:/proj"),
            "active_model_id": self.active_model_id,
            "active_model_name": self.active_model_id,
            "messages": self.messages_by_session.get(session_name, []),
        }

    def select_workspace_by_path(self, path: str):
        return {"ok": True, "workdir": path}

    def create_session(self, title: str = "新会话", workdir: str | None = None):
        new_name = "session_created"
        self.current_session = new_name
        return {
            "ok": True,
            "session_name": new_name,
            "title": title,
            "workdir": workdir or "E:/proj",
        }

    def get_active_permission(self):
        return {"ok": True, "permission": self.pending_permission}

    def get_all_active_turn_snapshots(self):
        return {}

    def load_older_messages(self, session_name: str, before_id: int):
        return {
            "ok": True,
            "messages": self.messages_by_session.get(session_name, []),
            "has_more": False,
            "oldest_id": 1,
        }

    def stop_current_turn(self, session_name: str = ""):
        self.stopped.append(session_name)
        return {"ok": True, "idle": False}

    def respond_permission(self, request_id: str, allow: bool, always: bool = False):
        self.perm_responses.append({"request_id": request_id, "allow": allow, "always": always})
        self.pending_permission = None
        if self.client is not None:
            self.client.submit("permission_dismiss", {})
        return {"ok": True}


@pytest.fixture
def env(tmp_path):
    db_path = tmp_path / "relay.db"
    conn = connect(db_path)
    reg = RelayRegistry()
    device_id, device_token = reg.create_device(conn, "my-pc")
    token_id, mcp_token = reg.create_mcp_token(conn, device_id, name="amoo")
    conn.close()
    app = create_app(db_path, SECRET)
    return {
        "db_path": db_path,
        "device_id": device_id,
        "device_token": device_token,
        "token_id": token_id,
        "mcp_token": mcp_token,
        "app": app,
    }


def _mcp_rpc(client: TestClient, payload: dict, token: str | None, path: str = "/mcp"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, json=payload, headers=headers)


def _wait(predicate, timeout=10.0, message="等待超时"):
    deadline = time.time() + timeout
    while not predicate():
        if time.time() > deadline:
            raise AssertionError(message)
        time.sleep(0.02)


def test_mcp_auth_rejects_missing_wrong_and_revoked_tokens(env):
    with TestClient(env["app"]) as client:
        r = _mcp_rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == -32001

        r = _mcp_rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="bad-token")
        assert r.status_code == 401

        # 合法 token + 错误路径 device_id 拒绝
        r = _mcp_rpc(
            client,
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            token=env["mcp_token"],
            path="/mcp/other-device",
        )
        assert r.status_code == 401

        # 吊销 token 后立即拒绝
        conn = connect(env["db_path"])
        RelayRegistry().revoke_mcp_token(conn, env["token_id"])
        conn.close()
        r = _mcp_rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=env["mcp_token"])
        assert r.status_code == 401


def test_mcp_protocol_basics_and_offline_error(env):
    with TestClient(env["app"]) as client:
        assert client.get("/mcp").status_code == 405

        r = _mcp_rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "clientInfo": {"name": "amoo", "version": "1.0"},
                },
            },
            token=env["mcp_token"],
        )
        assert r.status_code == 200
        assert r.json()["result"]["protocolVersion"] == "2025-03-26"
        assert r.json()["result"]["serverInfo"]["name"] == "xingyao-relay"

        r_notif = _mcp_rpc(
            client,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=env["mcp_token"],
        )
        assert r_notif.status_code == 202

        r_tools = _mcp_rpc(
            client,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            token=env["mcp_token"],
        )
        names = [t["name"] for t in r_tools.json()["result"]["tools"]]
        assert names == [
            "send_message",
            "get_reply",
            "get_status",
            "list_models",
            "switch_model",
            "list_workspaces",
            "list_sessions",
            "switch_session",
            "create_session",
            "stop_task",
            "respond_permission",
        ]

        # 离线调用 send_message 返回工具级错误
        r_call = _mcp_rpc(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "send_message", "arguments": {"text": "你好"}},
            },
            token=env["mcp_token"],
        )
        assert r_call.status_code == 200
        res = r_call.json()["result"]
        assert res["isError"] is True
        assert "未在线" in res["content"][0]["text"]


def test_mcp_and_skill_e2e_with_connected_desktop(env):
    bridge = _FakeBridge()
    lan = LanServer(bridge=bridge)
    server = uvicorn.Server(
        uvicorn.Config(env["app"], host="127.0.0.1", port=0, log_level="warning")
    )
    srv_thread = threading.Thread(target=server.run, daemon=True)
    srv_thread.start()
    _wait(lambda: server.started, message="uvicorn 未能启动")
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    conn = websockets.sync.client.connect(f"ws://127.0.0.1:{port}/relay/device", open_timeout=10)
    relay_client = CloudRelayClient(send=conn.send, boot_id="test-boot")
    bridge.client = relay_client
    relay_client.on_rpc = lambda m, a: lan.dispatch_rpc(m, a, turn_source="lan")
    pump = threading.Thread(
        target=relay_client.pump,
        args=(conn, env["device_token"], env["device_id"]),
        daemon=True,
    )
    pump.start()
    try:
        _wait(lambda: relay_client.connected, message="桌面端未能完成注册")
        headers = {"Authorization": f"Bearer {env['mcp_token']}"}
        with httpx.Client(base_url=base, timeout=15.0) as http:
            # 1. MCP send_message 同步等待返回星瑶最终回复
            r = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 10,
                    "method": "tools/call",
                    "params": {
                        "name": "send_message",
                        "arguments": {"text": "检查一下当前目录", "wait_seconds": 5},
                    },
                },
            )
            assert r.status_code == 200
            result = r.json()["result"]
            assert result["isError"] is False
            assert "星瑶已处理：检查一下当前目录" in result["content"][0]["text"]
            assert bridge.sent[-1]["turn_source"] == "lan"

            # 2. 触发权限请求 + respond_permission
            r_perm = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 11,
                    "method": "tools/call",
                    "params": {
                        "name": "send_message",
                        "arguments": {"text": "run permission check", "wait_seconds": 5},
                    },
                },
            )
            perm_text = r_perm.json()["result"]["content"][0]["text"]
            assert "req-42" in perm_text

            r_allow = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 12,
                    "method": "tools/call",
                    "params": {
                        "name": "respond_permission",
                        "arguments": {"request_id": "req-42", "allow": True, "always": False},
                    },
                },
            )
            assert r_allow.json()["result"]["isError"] is False
            assert bridge.perm_responses == [{"request_id": "req-42", "allow": True, "always": False}]

            # 3. list_sessions / get_status / stop_task / slow -> get_reply
            r_sess = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 13,
                    "method": "tools/call",
                    "params": {"name": "list_sessions", "arguments": {}},
                },
            )
            assert "主会话" in r_sess.json()["result"]["content"][0]["text"]

            r_stop = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 14,
                    "method": "tools/call",
                    "params": {"name": "stop_task", "arguments": {}},
                },
            )
            assert r_stop.json()["result"]["isError"] is False
            assert len(bridge.stopped) == 1

            # 4. REST Skill 接口同样可用（支持 ?token=... 查询串）
            rs_status = http.get("/api/skill/status", headers=headers)
            assert rs_status.status_code == 200
            assert rs_status.json()["online"] is True
            assert rs_status.json()["current_session"] == "session_main"

            rs_send = http.post(
                f"/api/skill/send?token={env['mcp_token']}",
                json={"text": "Skill 测试任务", "wait_seconds": 5},
            )
            assert rs_send.status_code == 200
            assert rs_send.json()["status"] == "finished"
            assert "星瑶已处理：Skill 测试任务" in rs_send.json()["reply"]

            rs_reply = http.get("/api/skill/reply", headers=headers)
            assert rs_reply.status_code == 200
            assert "星瑶已处理：Skill 测试任务" in rs_reply.json()["reply"]

            # 5. 模型查询与切换（按名称 & 按 ID）
            r_models = http.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 15, "method": "tools/call", "params": {"name": "list_models", "arguments": {}}},
            )
            models_txt = r_models.json()["result"]["content"][0]["text"]
            assert "DeepSeek" in models_txt and "DeepSeek Reasoner" in models_txt

            r_sw_model = http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 16,
                    "method": "tools/call",
                    "params": {"name": "switch_model", "arguments": {"model_name": "DeepSeek Reasoner"}},
                },
            )
            assert r_sw_model.json()["result"]["isError"] is False
            assert bridge.active_model_id == "ds-reasoner"

            # 6. 工作目录列表、按工作目录筛选会话、切换会话、新建会话
            r_ws = http.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 17, "method": "tools/call", "params": {"name": "list_workspaces", "arguments": {}}},
            )
            ws_txt = r_ws.json()["result"]["content"][0]["text"]
            assert "other-app" in ws_txt and "E:/proj" in ws_txt

            rs_sess_filtered = http.get("/api/skill/sessions?workdir=other-app", headers=headers)
            assert rs_sess_filtered.status_code == 200
            assert "另一个项目会话" in rs_sess_filtered.json()["text"]
            assert "主会话" not in rs_sess_filtered.json()["text"]

            rs_sw_sess = http.post(
                "/api/skill/switch-session",
                headers=headers,
                json={"session_name": "session_other"},
            )
            assert rs_sw_sess.status_code == 200
            assert rs_sw_sess.json()["workdir"] == "E:/other-app"
            assert bridge.current_session == "session_other"

            rs_new_sess = http.post(
                "/api/skill/create-session",
                headers=headers,
                json={"workdir": "E:/other-app", "title": "新任务"},
            )
            assert rs_new_sess.status_code == 200
            assert rs_new_sess.json()["session_name"] == "session_created"

            rs_models = http.get("/api/skill/models", headers=headers)
            assert rs_models.status_code == 200
            assert rs_models.json()["active_model_id"] == "ds-reasoner"

            rs_ws = http.get("/api/skill/workspaces", headers=headers)
            assert rs_ws.status_code == 200
            assert len(rs_ws.json()["workspaces"]) == 2
    finally:
        relay_client.close()
        try:
            conn.close()
        except Exception:
            pass
        pump.join(timeout=5)
        server.should_exit = True
        srv_thread.join(timeout=5)


def test_cli_mcp_token_lifecycle(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli_relay.db"
    monkeypatch.setenv("RELAY_DB_PATH", str(db_path))
    assert relay_cli_main(["create-device", "--name", "pc-1"]) == 0
    out = capsys.readouterr().out
    dev_id = [line.split(": ", 1)[1].strip() for line in out.splitlines() if line.startswith("device_id:")][0]

    assert relay_cli_main(["create-mcp-token", "--device-id", dev_id, "--name", "amoo"]) == 0
    out2 = capsys.readouterr().out
    tok_id = [line.split(": ", 1)[1].strip() for line in out2.splitlines() if line.startswith("token_id:")][0]
    raw_tok = [line.split(": ", 1)[1].strip() for line in out2.splitlines() if line.startswith("mcp_token:")][0]
    assert raw_tok.startswith("xym_")

    assert relay_cli_main(["list-mcp-tokens", "--device-id", dev_id]) == 0
    out3 = capsys.readouterr().out
    assert tok_id in out3

    assert relay_cli_main(["revoke-mcp-token", tok_id]) == 0
    out4 = capsys.readouterr().out
    assert "已吊销" in out4
