import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

JSON_CONTENT_TYPE = "application/json; charset=utf-8"

# 版本协商：客户端报的版本在自己支持的集合里才回显，否则回默认版本（规范允许
# 服务端回自己支持的版本，客户端据此降级），避免协商出一个本实现没有的语义。
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

# 请求体上限：工具入参只有一段自然语言文本，1MB 已远超真实需求；设上限是为了
# 不让畸形 Content-Length 把单线程的内存量拖垮（Http 服务对公网可达）。
MAX_BODY_BYTES = 1024 * 1024

# 唯一工具名：与工具定义、tools/call 分发共用同一常量，避免两处字符串漂移
SEND_MESSAGE_TOOL_NAME = "send_message"


def tool_definition() -> Dict[str, Any]:
    return {
        "name": SEND_MESSAGE_TOOL_NAME,
        "description": (
            "把一条中文自然语言任务发送给电脑上的星瑶（Y-code）智能体执行。"
            "星瑶会在电脑上开会话、读代码、跑命令并把结果留在电脑上。"
            "适用于用户说'让电脑上的星瑶做某某事'。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要交给星瑶执行的自然语言任务",
                },
                "session_name": {
                    "type": "string",
                    "description": "可选，指定星瑶会话名；留空则用当前会话",
                },
            },
            "required": ["text"],
        },
    }


def _result(msg_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": int(code), "message": str(message)}}


def _tool_error(text: str) -> Dict[str, Any]:
    """工具级错误结果：用 isError 表达成败，而不是抛 JSON-RPC 错误.

    工具执行失败（bridge 拒绝、桌面未就绪）对模型/用户是可读信息，塞进
    content 里比 -32xxx 协议错误有用；协议错误只留给「请求本身不合法」。
    """
    return {"content": [{"type": "text", "text": str(text)}], "isError": True}


def _tool_ok(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": str(text)}], "isError": False}


class McpServer:
    """远程 MCP 服务端：ThreadingHTTPServer + daemon 线程，可随时 start/stop。"""

    def __init__(
        self,
        bridge: Any = None,
        port: int = 0,
        host: str = "0.0.0.0",
        token_provider: Optional[Callable[[], Any]] = None,
    ):
        self.bridge = bridge
        self.port = int(port or 0)
        self.host = host
        self._token_provider = token_provider
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if self._server is not None:
                # 已在运行：幂等返回当前信息，不重复绑定端口
                return self._started_info()
            try:
                server = ThreadingHTTPServer((self.host, self.port), _McpRequestHandler)
            except OSError as exc:
                logger.warning("MCP 服务启动失败（端口绑定异常）: %s", exc)
                return {"ok": False, "msg": f"端口绑定失败: {exc}"}
            server.daemon_threads = True
            server.mcp_ref = self  # type: ignore[attr-defined]
            self._server = server
            self.port = int(server.server_address[1])
            self._thread = threading.Thread(
                target=self._serve_forever,
                args=(server,),
                name="mcp-http-server",
                daemon=True,
            )
            self._thread.start()
            info = self._started_info()
            logger.info("MCP 服务已启动: %s", info["url"])
            return info

    def stop(self) -> None:
        """关闭服务；可重复调用（首次之后都是空操作）."""
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        try:
            server.shutdown()
        except Exception:
            logger.warning("MCP 服务 shutdown 异常", exc_info=True)
        try:
            server.server_close()
        except Exception:
            logger.warning("MCP 服务 server_close 异常", exc_info=True)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            # 等循环真正退出再返回，避免调用方紧接着重启时新旧监听短暂并存
            thread.join(timeout=5.0)
        logger.info("MCP 服务已停止")

    def _serve_forever(self, server: ThreadingHTTPServer) -> None:
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception:
            logger.exception("MCP 服务事件循环异常退出")

    def _started_info(self) -> Dict[str, Any]:
        return {
            "ok": True,
            "port": self.port,
            "host": self.host,
            "url": f"http://{self._dial_host()}:{self.port}/mcp",
        }

    def _dial_host(self) -> str:
        host = str(self.host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            return "127.0.0.1"
        return host

    def authorize(self, authorization: Optional[str], query: Dict[str, List[str]]) -> bool:
        provider = self._token_provider
        if provider is None:
            # 仅测试用：生产入口必须注入 token_provider
            return True
        token = self._bearer_token(authorization)
        if not token:
            token = (query.get("token") or [""])[0]
        if not token:
            return False
        expected = self._expected_token()
        if not expected:
            return False
        try:
            return secrets.compare_digest(str(token).encode("utf-8"), expected)
        except Exception:
            return False

    @staticmethod
    def _bearer_token(authorization: Optional[str]) -> str:
        parts = str(authorization or "").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return ""

    def _expected_token(self) -> bytes:
        provider = self._token_provider
        if provider is None:
            return b""
        try:
            raw = provider()
        except Exception:
            logger.warning("MCP token provider 调用失败，本次请求按未授权处理", exc_info=True)
            return b""
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode("utf-8")
        return b""

    def handle_message(self, payload: Any) -> Tuple[int, Optional[Dict[str, Any]]]:
        if not isinstance(payload, dict):
            return 400, _error(None, -32700, "parse error")
        method = payload.get("method")
        msg_id = payload.get("id")
        if not isinstance(method, str) or not method:
            return 400, _error(msg_id, -32600, "invalid request")
        if "id" not in payload:
            return 202, None
        if method == "initialize":
            return 200, _result(msg_id, self.initialize_result(payload.get("params")))
        if method == "ping":
            return 200, _result(msg_id, {})
        if method == "tools/list":
            return 200, _result(msg_id, {"tools": [tool_definition()]})
        if method == "tools/call":
            return 200, self._tools_call(msg_id, payload.get("params"))
        return 200, _error(msg_id, -32601, "method not found")

    @staticmethod
    def initialize_result(params: Any) -> Dict[str, Any]:
        """initialize 应答：回显客户端版本（认识的话），并声明只提供 tools."""
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
            "serverInfo": {"name": "xingyao", "version": "1.0.0"},
        }

    def _tools_call(self, msg_id: Any, params: Any) -> Dict[str, Any]:
        if not isinstance(params, dict):
            return _error(msg_id, -32602, "invalid params: params must be object")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _error(msg_id, -32602, "invalid params: arguments must be object")
        name = params.get("name")
        if name != SEND_MESSAGE_TOOL_NAME:
            return _error(msg_id, -32602, f"unknown tool: {name}")
        text = str(arguments.get("text") or "")
        raw_session = arguments.get("session_name")
        session_name = str(raw_session or "").strip() or None
        if not text.strip():
            return _result(msg_id, _tool_error("text 不能为空"))
        return _result(msg_id, self.dispatch_send_message(text, session_name))

    def dispatch_send_message(self, text: str, session_name: Optional[str]) -> Dict[str, Any]:
        bridge = self.bridge
        if bridge is None:
            return _tool_error("发送失败：bridge 未初始化，任务没有投递给星瑶")
        try:
            result = bridge.send_user_message(
                text=text,
                session_name=session_name,
                turn_source="mcp",
            )
        except Exception as exc:
            # 异常不能穿到 HTTP 层（对手机端就是无信息的 500）：转成工具级错误文本
            logger.exception("MCP send_message 投递失败")
            return _tool_error(f"发送失败：{exc}")
        if isinstance(result, dict) and result.get("ok"):
            return _tool_ok("已交给星瑶执行")
        msg = ""
        if isinstance(result, dict):
            msg = str(result.get("msg") or "")
        elif result is not None:
            msg = str(result)
        return _tool_error(f"发送失败：{msg}" if msg else "发送失败：星瑶未受理这条任务")


class _McpRequestHandler(BaseHTTPRequestHandler):
    """HTTP 层：路由 + 鉴权 + 访问日志脱敏，业务全部委托给 McpServer."""

    protocol_version = "HTTP/1.1"
    server_version = "YCodeMcp/1"
    # 单连接读超时：手机端弱网半开连接不能让工作线程永久挂在 readline 上
    timeout = 30

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        """``POST /mcp``：鉴权 → 解析 JSON → 交给 McpServer 分发."""
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self._send_text(404, "not found")
            return
        mcp = self._mcp()
        if not mcp.authorize(self.headers.get("Authorization"), parse_qs(parsed.query)):
            self._send_json(
                401,
                _error(None, -32001, "unauthorized"),
                {"WWW-Authenticate": 'Bearer realm="mcp"'},
            )
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, _error(None, -32700, "parse error"))
            return
        status, body = mcp.handle_message(payload)
        if body is None:
            self._send_empty(status)
        else:
            self._send_json(status, body)

    def do_GET(self):  # noqa: N802
        """``GET /health`` 探活；``GET /mcp`` 明确拒绝（不支持 SSE 长连接）."""
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send_text(200, "ok")
            return
        if parsed.path == "/mcp":
            self._send_json(
                405,
                _error(None, -32600, "method not allowed: use POST /mcp"),
                {"Allow": "POST"},
            )
            return
        self._send_text(404, "not found")

    def _mcp(self) -> McpServer:
        return self.server.mcp_ref  # type: ignore[attr-defined]

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY_BYTES:
            self._send_json(413, _error(None, -32600, "invalid request: body too large"))
            return None
        try:
            return self.rfile.read(length)
        except (OSError, ValueError):
            logger.debug("MCP 请求体读取失败（连接中断）")
            return None

    def _send_json(self, status: int, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": JSON_CONTENT_TYPE, "Content-Length": str(len(body))}
        if extra_headers:
            headers.update(extra_headers)
        self._respond(status, headers, body)

    def _send_empty(self, status: int) -> None:
        self._respond(status, {"Content-Length": "0"}, b"")

    def _send_text(self, status: int, text: str) -> None:
        body = str(text).encode("utf-8")
        self._respond(
            status,
            {"Content-Type": "text/plain; charset=utf-8", "Content-Length": str(len(body))},
            body,
        )

    def _respond(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        # 访问日志只记 path（urlparse 已剥掉查询串），token 绝不进日志
        logger.debug("MCP %s %s -> %s", self.command, urlparse(self.path).path, status)
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 手机端切后台/断网会直接掐连接，属正常现象
            pass

    def log_message(self, fmt, *args):  # noqa: N802
        safe_args = []
        for arg in args:
            text = str(arg)
            if "?" in text:
                text = text.split("?", 1)[0]
            safe_args.append(text)
        logger.debug("MCP http: " + str(fmt), *safe_args)
