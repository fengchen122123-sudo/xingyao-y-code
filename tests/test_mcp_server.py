"""core/mcp_server.py（手机端 MCP 服务端）的单元测试。

覆盖三层：

1. **纯函数层**：``handle_message`` 的 JSON-RPC 分发与 ``authorize`` 的凭据校验
   （返回形状为 ``(HTTP 状态码, 应答体 | None)``，``None`` 表示通知不回包）。
2. **真实 HTTP 链路**：``port=0`` 起真服务 + ``http.client`` 打 127.0.0.1，端到端
   验证路由 / 鉴权 / 工具执行边界。全部本机回环，不碰外网、不碰真实 GUI/模型。
3. **生命周期与安全**：start/stop 幂等、端口占用降级、token 每次请求现取（可轮换）、
   访问日志不泄露 ``?token=``。

bridge 用裸类 ``FakeBridge`` 替身：签名与 ``ApiBridge.send_user_message`` 同形
（7 个关键字参数、``turn_source`` 默认 "local"），记录每次调用的 kwargs，
既能断言 ``turn_source="mcp"`` 的注入，也不会替实现掩盖参数绑定错误。
"""

import http.client
import json
import logging
import socket

import pytest

from core.mcp_server import (
    DEFAULT_PROTOCOL_VERSION,
    MAX_BODY_BYTES,
    SEND_MESSAGE_TOOL_NAME,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpServer,
)

# 测试用固定 token（真 token 由上层签发，这里只要可比较即可）
TOKEN = "tok-123"
WRONG_TOKEN = "tok-999"


class FakeBridge:
    """``ApiBridge.send_user_message`` 的替身：记录 kwargs，可配置返回/抛异常.

    签名与真实入口严格同形（7 个关键字参数 + turn_source 默认 "local"），
    不接 **kwargs——未知键会以 TypeError 暴露，避免实现哪天传错参数名还被
    ``**kwargs`` 吞掉导致断言空转。
    """

    def __init__(self, result=None, exc=None):
        self.calls = []
        # 默认受理成功；需要失败语义时显式传入
        self.result = {"ok": True} if result is None else result
        self.exc = exc

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
        self.calls.append(
            {
                "text": text,
                "session_name": session_name,
                "attachments": attachments,
                "workdir": workdir,
                "model_id": model_id,
                "permission_mode": permission_mode,
                "turn_source": turn_source,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.result

    @property
    def last(self):
        assert self.calls, "bridge 未被调用"
        return self.calls[-1]


@pytest.fixture
def bridge():
    return FakeBridge()


@pytest.fixture
def server(bridge):
    """真实起一个回环服务（port=0 由内核分配），用例结束必定 stop()。

    绑 127.0.0.1 而非默认 0.0.0.0：单测不需要对外可达，还避免触发系统防火墙
    弹窗。``_dial_host`` 的通配地址映射另有纯函数用例覆盖。
    """
    srv = McpServer(bridge=bridge, port=0, host="127.0.0.1", token_provider=lambda: TOKEN)
    info = srv.start()
    assert info["ok"], info
    try:
        yield srv
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# HTTP 小工具（本机回环，一次请求一连接，避免 keep-alive 串包）
# ---------------------------------------------------------------------------


def _http(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=dict(headers or {}))
        resp = conn.getresponse()
        return resp.status, resp.headers, resp.read()
    finally:
        conn.close()


def _post(srv, payload, token=TOKEN, path="/mcp"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return _http(srv, "POST", path, raw, headers)


def _rpc(srv, payload, token=TOKEN, path="/mcp"):
    """POST 一条 JSON-RPC，返回 ``(状态码, 解析后的 body 或 None)``."""
    status, _headers, raw = _post(srv, payload, token=token, path=path)
    return status, (json.loads(raw.decode("utf-8")) if raw else None)


def _call_tool(srv, name, arguments, token=TOKEN):
    return _rpc(
        srv,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        token=token,
    )


def _tool_result(body):
    """断言是工具级结果（不是 JSON-RPC 协议错误）并取出 result."""
    assert "error" not in body, body
    result = body["result"]
    assert isinstance(result["content"], list) and result["content"]
    return result


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# ---------------------------------------------------------------------------
# 1. 纯函数层：handle_message（返回 (status, body|None)）
# ---------------------------------------------------------------------------


def test_handle_message_non_object_payload_is_parse_error():
    srv = McpServer(bridge=None)
    for payload in (None, [], "ping", 3):
        status, body = srv.handle_message(payload)
        assert status == 400
        assert body["error"]["code"] == -32700


def test_handle_message_missing_method_is_invalid_request():
    srv = McpServer(bridge=None)
    status, body = srv.handle_message({"jsonrpc": "2.0", "id": 1})
    assert status == 400
    assert body["error"]["code"] == -32600
    assert body["id"] == 1


def test_handle_message_notification_returns_no_body():
    srv = McpServer(bridge=None)
    status, body = srv.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert (status, body) == (202, None)


def test_handle_message_tools_call_rejects_non_object_arguments():
    """arguments 不是对象属于「请求不合法」，必须是协议错误而非 isError 结果。"""
    srv = McpServer(bridge=None)
    status, body = srv.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": SEND_MESSAGE_TOOL_NAME, "arguments": ["x"]},
        }
    )
    assert status == 200
    assert body["error"]["code"] == -32602


def test_handle_message_without_bridge_reports_tool_error():
    """未注入 bridge 时工具级失败（不是协议错误），且绝不伪装成功。"""
    srv = McpServer(bridge=None)
    status, body = srv.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": SEND_MESSAGE_TOOL_NAME, "arguments": {"text": "你好"}},
        }
    )
    assert status == 200
    result = _tool_result(body)
    assert result["isError"] is True
    assert "bridge" in result["content"][0]["text"]


@pytest.mark.parametrize("requested", SUPPORTED_PROTOCOL_VERSIONS)
def test_initialize_echoes_supported_protocol_version(requested):
    srv = McpServer(bridge=None)
    result = srv.initialize_result({"protocolVersion": requested})
    assert result["protocolVersion"] == requested
    assert result["serverInfo"]["name"] == "xingyao"
    assert result["capabilities"]["tools"] == {"listChanged": False}


def test_initialize_falls_back_to_default_version_for_unknown():
    srv = McpServer(bridge=None)
    for params in ({"protocolVersion": "1999-01-01"}, {}, None):
        assert srv.initialize_result(params)["protocolVersion"] == DEFAULT_PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# 2. authorize：Bearer 头优先，?token= 查询参数兜底，一致性比较
# ---------------------------------------------------------------------------


def test_authorize_without_provider_is_open():
    """token_provider 为 None 表示不鉴权（仅测试/内部直连场景）。"""
    srv = McpServer(bridge=None)
    assert srv.authorize(None, {}) is True


def test_authorize_accepts_bearer_header():
    srv = McpServer(bridge=None, token_provider=lambda: TOKEN)
    assert srv.authorize(f"Bearer {TOKEN}", {}) is True
    # 大小写不敏感（HTTP 头值里的 scheme 常被客户端写成小写）
    assert srv.authorize(f"bearer {TOKEN}", {}) is True


def test_authorize_accepts_query_token_fallback():
    """手机端 MCP 客户端只填 URL，查询参数是唯一可行通道。"""
    srv = McpServer(bridge=None, token_provider=lambda: TOKEN)
    assert srv.authorize(None, {"token": [TOKEN]}) is True
    assert srv.authorize(None, {"token": [WRONG_TOKEN]}) is False


def test_authorize_rejects_missing_and_malformed_credentials():
    srv = McpServer(bridge=None, token_provider=lambda: TOKEN)
    assert srv.authorize(None, {}) is False
    assert srv.authorize("", {}) is False
    assert srv.authorize(f"Basic {TOKEN}", {}) is False
    assert srv.authorize("Bearer", {}) is False
    assert srv.authorize(f"Bearer {WRONG_TOKEN}", {}) is False


def test_authorize_fail_closed_when_provider_yields_nothing_or_raises():
    """provider 取不到有效 token（尚未签发/读配置失败）时必须拒绝，不能放行。"""
    assert McpServer(bridge=None, token_provider=lambda: b"").authorize(f"Bearer {TOKEN}", {}) is False
    assert McpServer(bridge=None, token_provider=lambda: None).authorize(f"Bearer {TOKEN}", {}) is False

    def _boom():
        raise RuntimeError("配置读取失败")

    assert McpServer(bridge=None, token_provider=_boom).authorize(f"Bearer {TOKEN}", {}) is False


def test_authorize_accepts_str_token_from_provider():
    srv = McpServer(bridge=None, token_provider=lambda: "str-tok")
    assert srv.authorize("Bearer str-tok", {}) is True


# ---------------------------------------------------------------------------
# 3. 真实 HTTP 链路
# ---------------------------------------------------------------------------


def test_start_returns_real_port_and_dial_url(server):
    assert server.port != 0
    info = server._started_info()
    assert info["ok"] is True
    assert info["port"] == server.port
    assert info["host"] == "127.0.0.1"
    assert info["url"] == f"http://127.0.0.1:{server.port}/mcp"


def test_started_info_maps_wildcard_host_to_loopback():
    """绑 0.0.0.0 时 url 必须回 127.0.0.1（通配地址不可拨号）。"""
    srv = McpServer(port=4999, host="0.0.0.0")
    assert srv._started_info()["url"] == "http://127.0.0.1:4999/mcp"


def test_get_health_returns_ok(server):
    status, headers, body = _http(server, "GET", "/health")
    assert status == 200
    assert body == b"ok"
    assert headers.get("Content-Type", "").startswith("text/plain")


def test_post_without_authorization_is_401_jsonrpc_error(server):
    status, headers, raw = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
    assert status == 401
    body = json.loads(raw.decode("utf-8"))
    assert body["error"]["code"] == -32001
    # 401 必须是 JSON-RPC 形态：部分客户端只会按 JSON 解析应答
    assert headers.get("Content-Type", "").startswith("application/json")
    assert "Bearer" in headers.get("WWW-Authenticate", "")


@pytest.mark.parametrize("token", [WRONG_TOKEN, ""])
def test_post_with_wrong_token_is_401(server, token):
    status, _headers, raw = _post(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=token)
    assert status == 401
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == -32001


def test_post_accepts_query_token(server):
    status, body = _rpc(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        token=None,
        path=f"/mcp?token={TOKEN}",
    )
    assert status == 200
    assert body["result"] == {}


def test_initialize_over_http(server):
    status, body = _rpc(
        server,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "amoo", "version": "1.0"},
            },
        },
    )
    assert status == 200
    result = body["result"]
    assert result["serverInfo"]["name"] == "xingyao"
    assert result["protocolVersion"] == "2025-03-26"
    assert result["protocolVersion"] in SUPPORTED_PROTOCOL_VERSIONS


def test_notification_returns_202_with_empty_body(server):
    status, headers, raw = _post(server, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert status == 202
    assert raw == b""
    assert headers.get("Content-Length") == "0"


def test_tools_list_exposes_only_send_message(server):
    status, body = _rpc(server, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    tools = body["result"]["tools"]
    assert [tool["name"] for tool in tools] == [SEND_MESSAGE_TOOL_NAME]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["text"]
    assert "text" in schema["properties"]


def test_tools_call_send_message_success(server, bridge):
    status, body = _call_tool(server, SEND_MESSAGE_TOOL_NAME, {"text": "看看今天的天气"})
    assert status == 200
    result = _tool_result(body)
    assert result["isError"] is False
    # bridge 必须收到关键字参数、来源注入为 mcp、未指定会话时为 None
    assert bridge.calls == [
        {
            "text": "看看今天的天气",
            "session_name": None,
            "attachments": None,
            "workdir": None,
            "model_id": None,
            "permission_mode": None,
            "turn_source": "mcp",
        }
    ]


def test_tools_call_passes_session_name_through(server, bridge):
    status, body = _call_tool(
        server, SEND_MESSAGE_TOOL_NAME, {"text": "继续修 bug", "session_name": "会话A"}
    )
    assert status == 200
    assert _tool_result(body)["isError"] is False
    assert bridge.last["session_name"] == "会话A"
    assert bridge.last["text"] == "继续修 bug"
    assert bridge.last["turn_source"] == "mcp"


def test_tools_call_blank_session_name_is_normalized_to_none(server, bridge):
    _call_tool(server, SEND_MESSAGE_TOOL_NAME, {"text": "hi", "session_name": "   "})
    assert bridge.last["session_name"] is None


def test_tools_call_reports_bridge_rejection(server, bridge):
    """bridge 拒绝必须是 isError=true + 原样透出 msg，不能伪造成功。"""
    bridge.result = {"ok": False, "msg": "无效的回合来源"}
    status, body = _call_tool(server, SEND_MESSAGE_TOOL_NAME, {"text": "看看今天的天气"})
    assert status == 200
    result = _tool_result(body)
    assert result["isError"] is True
    assert "无效的回合来源" in result["content"][0]["text"]


def test_tools_call_reports_bridge_exception(server, bridge):
    bridge.exc = RuntimeError("桌面端未就绪")
    status, body = _call_tool(server, SEND_MESSAGE_TOOL_NAME, {"text": "看看今天的天气"})
    # 异常不得穿透成 500（手机端会得到无信息错误）
    assert status == 200
    result = _tool_result(body)
    assert result["isError"] is True
    assert "桌面端未就绪" in result["content"][0]["text"]


def test_tools_call_reports_empty_bridge_result(server, bridge):
    bridge.result = None
    status, body = _call_tool(server, SEND_MESSAGE_TOOL_NAME, {"text": "看看今天的天气"})
    assert status == 200
    assert _tool_result(body)["isError"] is True


@pytest.mark.parametrize("arguments", [{"text": ""}, {"text": "   "}, {}])
def test_tools_call_empty_text_is_tool_error_without_bridge_call(server, bridge, arguments):
    status, body = _call_tool(server, SEND_MESSAGE_TOOL_NAME, arguments)
    assert status == 200
    result = _tool_result(body)
    assert result["isError"] is True
    assert "text" in result["content"][0]["text"]
    # 空任务不该打扰桌面端排队
    assert bridge.calls == []


def test_tools_call_unknown_tool_is_invalid_params(server, bridge):
    status, body = _call_tool(server, "open_browser", {"url": "https://example.com"})
    assert status == 200
    assert body["error"]["code"] == -32602
    assert "unknown tool" in body["error"]["message"]
    assert bridge.calls == []


@pytest.mark.parametrize("method", ["resources/list", "prompts/list", "completion/complete"])
def test_unknown_method_is_method_not_found(server, method):
    status, body = _rpc(server, {"jsonrpc": "2.0", "id": 9, "method": method})
    assert status == 200
    assert body["error"]["code"] == -32601
    assert body["id"] == 9


def test_invalid_json_body_is_400_parse_error(server):
    status, _headers, raw = _http(
        server,
        "POST",
        "/mcp",
        b"{this is not json",
        {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    assert status == 400
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == -32700


def test_get_mcp_is_405(server):
    status, headers, raw = _http(server, "GET", "/mcp")
    assert status == 405
    assert headers.get("Allow") == "POST"
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == -32600


def test_unknown_path_is_404(server):
    for method in ("GET", "POST"):
        status, _headers, body = _http(server, method, "/nope")
        assert status == 404
        assert body == b"not found"


def test_oversized_body_is_rejected_before_read(server):
    """Content-Length 超上限时先回 413，不把畸形大请求读进内存。"""
    status, _headers, raw = _http(
        server,
        "POST",
        "/mcp",
        b"x",
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
            "Content-Length": str(MAX_BODY_BYTES + 1),
        },
    )
    assert status == 413
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == -32600


# ---------------------------------------------------------------------------
# 4. token 轮换：每次请求现取 provider，改值即刻生效
# ---------------------------------------------------------------------------


def test_token_rotation_takes_effect_on_next_request():
    state = {"token": "tok-old", "calls": 0}

    def provider():
        state["calls"] += 1
        return state["token"]

    srv = McpServer(port=0, host="127.0.0.1", bridge=FakeBridge(), token_provider=provider)
    info = srv.start()
    assert info["ok"], info
    try:
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        assert _rpc(srv, ping, token="tok-old")[0] == 200

        state["token"] = "tok-new"
        assert _rpc(srv, ping, token="tok-old")[0] == 401
        assert _rpc(srv, ping, token="tok-new")[0] == 200
        # 每次请求都现取，而不是 start() 时快照
        assert state["calls"] >= 3
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 5. 生命周期：start/stop 幂等、端口占用降级
# ---------------------------------------------------------------------------


def test_start_twice_is_idempotent(server):
    second = server.start()
    assert second["ok"] is True
    assert second["port"] == server.port
    # 仍是同一个监听：服务继续正常应答
    status, body = _rpc(server, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 200
    assert body["result"] == {}


def test_stop_is_idempotent_and_safe_before_start():
    srv = McpServer(bridge=FakeBridge())
    srv.stop()  # 未启动直接停：空操作
    info = srv.start()
    assert info["ok"], info
    port = srv.port
    srv.stop()
    srv.stop()
    # 端口已释放：同一端口可以重新绑定
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


def test_start_reports_failure_when_port_occupied():
    """端口被占用属运行时状况：回 {"ok": False, "msg": ...}，不抛异常。"""
    blocker = socket.socket()
    srv = McpServer(port=0, host="127.0.0.1", bridge=FakeBridge())
    try:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        occupied = blocker.getsockname()[1]
        srv.port = occupied
        info = srv.start()
        assert info["ok"] is False
        assert "端口绑定失败" in info["msg"]
        # 失败后不残留半成品服务，stop() 仍是安全空操作
        assert srv._server is None
        srv.stop()
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# 6. 日志脱敏：访问日志不含 token
# ---------------------------------------------------------------------------


def test_access_log_never_leaks_token(server, caplog):
    caplog.set_level(logging.DEBUG, logger="core.mcp_server")
    # 探活 + 用 ?token= 走一次业务路径，覆盖 log_message 与 _respond 两条日志
    assert _http(server, "GET", f"/health?token={TOKEN}")[0] == 200
    assert _rpc(
        server,
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        token=None,
        path=f"/mcp?token={TOKEN}",
    )[0] == 200

    text = caplog.text
    # 先确认日志真的产生了（否则脱敏断言恒真、零信息量）
    assert "/health" in text and "/mcp" in text
    assert TOKEN not in text
    assert "token=" not in text
