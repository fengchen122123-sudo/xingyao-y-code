"""core/mcp_server.py
=====================
供手机端 MCP 客户端（阶跃 Amoo 等）调用的远程 MCP 服务端。

项目里的 ``mcp`` 是**客户端**用的第三方包（core/mcp_client.py 拿它去连别人的
MCP 服务），反过来当服务端用要拖进额外传输层依赖；而远程暴露面只需要
「一个工具 + 一次 HTTP 往返」，所以这里用标准库手写协议子集：鉴权、日志脱敏、
端口回收、工具执行边界都自己掌握，也不给打包体积添依赖。

协议取 MCP Streamable HTTP 的最小可用部分：
- ``POST /mcp`` 收发 JSON-RPC 2.0，一律以单个 JSON 对象应答（不启用 SSE 长连接，
  ``GET /mcp`` 明确回 405——规范允许服务端不提供 SSE 通道）；
- ``GET /health`` 供手机端/运维探活；
- 手机端 MCP 配置里通常只能填一个 URL、填不了自定义请求头，因此 token 既认
  ``Authorization: Bearer``，也认 ``?token=`` 查询参数兜底。

唯一工具 ``send_message`` 把中文自然语言任务投给电脑上的星瑶：本服务只负责
「投递」，回合真正在桌面 GUI 里跑、结果留在电脑上——所以成功语义是
「已交给星瑶执行」，绝不代表任务已完成，也绝不伪造成功。
"""

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# JSON-RPC 应答统一 Content-Type（手机端部分客户端对 charset 敏感，显式声明）
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
    """返回 ``send_message`` 的工具定义（每次现建，防止调用方就地改坏）."""
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
    """JSON-RPC 成功应答（id 原样回显，通知路径不走这里）."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    """JSON-RPC 错误应答."""
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": int(code), "message": str(message)}}


def _tool_error(text: str) -> Dict[str, Any]:
    """工具级错误结果：用 isError 表达成败，而不是抛 JSON-RPC 错误.

    工具执行失败（bridge 拒绝、桌面未就绪）对模型/用户是可读信息，塞进
    content 里比 -32xxx 协议错误有用；协议错误只留给「请求本身不合法」。
    """
    return {"content": [{"type": "text", "text": str(text)}], "isError": True}


def _tool_ok(text: str) -> Dict[str, Any]:
    """工具级成功结果."""
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
        # bridge: gui_bridge 实例，工具执行时调用其 send_user_message(...)
        self.bridge = bridge
        # port=0 交给内核动态分配，start() 成功后把真实端口回填到 self.port
        self.port = int(port or 0)
        self.host = host
        # token_provider: 零参可调用对象，返回 bytes 或 str；每次请求现取，
        # 这样上层轮换 token 后无需重启服务。为 None 表示不做鉴权（仅测试用）。
        self._token_provider = token_provider
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> Dict[str, Any]:
        """启动后台服务；返回 ``{"ok", "port", "host", "url"}``.

        端口被占用属运行时状况（用户可能连开多个实例、端口被别的程序抢走），
        不该以异常形式把调用方打崩，故只收口 OSError 并回 ``{"ok": False}``；
        其他编程错误照常抛出，方便在开发/测试期暴露。
        """
        with self._lock:
            if self._server is not None:
                # 已在运行：幂等返回当前信息，不重复绑定端口
                return self._started_info()
            try:
                server = ThreadingHTTPServer((self.host, self.port), _McpRequestHandler)
            except OSError as exc:
                logger.warning("MCP 服务启动失败（端口绑定异常）: %s", exc)
                return {"ok": False, "msg": f"端口绑定失败: {exc}"}
            # 每个连接一个线程：手机端偶发会并发探活 + 调用，串行会被慢请求拖住
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
            # 先摘引用再关闭：并发进来第二次调用时直接看到 None 走空操作，
            # 不会对着已关闭的 socket 再 shutdown 一遍
            self._server = None
            self._thread = None
        if server is None:
            return
        try:
            # shutdown() 负责让 serve_forever 的循环退出；server_close() 释放监听端口
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
        """守护线程入口：跑 serve_forever，异常只记日志不污染主线程."""
        try:
            server.serve_forever(poll_interval=0.2)
        except Exception:
            logger.exception("MCP 服务事件循环异常退出")

    def _started_info(self) -> Dict[str, Any]:
        """当前实例的启动信息（host 报绑定位，url 报可拨号的地址）."""
        return {
            "ok": True,
            "port": self.port,
            "host": self.host,
            "url": f"http://{self._dial_host()}:{self.port}/mcp",
        }

    def _dial_host(self) -> str:
        """URL 里可拨号的主机：通配地址换成回环.

        host 绑定 0.0.0.0 是「监听所有网卡」，不是可连接的地址——直接拼进 URL
        手机端会连不上，所以这里换成 127.0.0.1；绑定了具体 IP/主机名则原样用。
        """
        host = str(self.host or "").strip()
        if host in ("", "0.0.0.0", "::", "[::]"):
            return "127.0.0.1"
        return host

    # ------------------------------------------------------------------ #
    # 鉴权
    # ------------------------------------------------------------------ #

    def authorize(self, authorization: Optional[str], query: Dict[str, List[str]]) -> bool:
        """校验请求凭据：优先 Bearer 头，其次 ``?token=`` 查询参数兜底.

        手机端 MCP 客户端往往只给一个 URL 输入框（填不了自定义请求头），查询参数
        是唯一可行的兜底通道；两种来源同一套比较逻辑，都用 secrets.compare_digest
        做常量时间比较，避免按字节缩短攻击面泄露 token 长度/前缀。
        """
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
            # provider 取不到有效 token（读配置失败/尚未签发）时 fail-closed
            return False
        try:
            return secrets.compare_digest(str(token).encode("utf-8"), expected)
        except Exception:
            return False

    @staticmethod
    def _bearer_token(authorization: Optional[str]) -> str:
        """从 ``Authorization: Bearer <token>`` 里取 token；格式不符返回空串."""
        parts = str(authorization or "").split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return ""

    def _expected_token(self) -> bytes:
        """调用 token_provider 拿到应校验的 token（统一成 bytes）."""
        provider = self._token_provider
        if provider is None:
            return b""
        try:
            raw = provider()
        except Exception:
            # provider 多半在读配置文件，失败时按「无有效 token」处理即可；
            # 异常信息里不含 token 本身，可以落日志
            logger.warning("MCP token provider 调用失败，本次请求按未授权处理", exc_info=True)
            return b""
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode("utf-8")
        return b""

    # ------------------------------------------------------------------ #
    # JSON-RPC 分发
    # ------------------------------------------------------------------ #

    def handle_message(self, payload: Any) -> Tuple[int, Optional[Dict[str, Any]]]:
        """处理一条 JSON-RPC 消息，返回 ``(HTTP 状态码, 应答体或 None)``.

        应答体为 None 表示「不回包」（通知类消息，HTTP 202 空 body）。
        """
        if not isinstance(payload, dict):
            # 合法 JSON 但不是对象（数组/标量）：同样是解析层错误
            return 400, _error(None, -32700, "parse error")
        method = payload.get("method")
        msg_id = payload.get("id")
        if not isinstance(method, str) or not method:
            return 400, _error(msg_id, -32600, "invalid request")
        if "id" not in payload:
            # JSON-RPC 通知（含 notifications/initialized）：无 id 即无应答体
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
            # listChanged=False：工具集是静态的，不会中途变化，无需推送变更通知
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "xingyao", "version": "1.0.0"},
        }

    def _tools_call(self, msg_id: Any, params: Any) -> Dict[str, Any]:
        """tools/call：请求形状不合法回 -32602，工具执行失败回 isError 结果."""
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
            # 空任务没有任何执行意义，也不必打扰桌面端排队
            return _result(msg_id, _tool_error("text 不能为空"))
        return _result(msg_id, self.dispatch_send_message(text, session_name))

    def dispatch_send_message(self, text: str, session_name: Optional[str]) -> Dict[str, Any]:
        """把任务投给桌面星瑶，返回 MCP 工具结果（真实成败，不伪造）.

        ``turn_source="mcp"`` 表示这是来自 MCP 通道的回合：入口层据此套用远程
        回合的权限约束。目前 bridge 的受控来源白名单可能尚未包含 "mcp"，届时
        bridge 会回 ``{"ok": False, "msg": "无效的回合来源"}``——那属于真实失败，
        按 isError 如实回给手机端，不能假装成功。
        """
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

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        """``POST /mcp``：鉴权 → 解析 JSON → 交给 McpServer 分发."""
        parsed = urlparse(self.path)
        if parsed.path != "/mcp":
            self._send_text(404, "not found")
            return
        mcp = self._mcp()
        if not mcp.authorize(self.headers.get("Authorization"), parse_qs(parsed.query)):
            # 401 + JSON-RPC 错误：MCP 客户端按 JSON-RPC 解析应答，纯文本会让它
            # 报「非法响应」而看不出是鉴权问题
            self._send_json(
                401,
                _error(None, -32001, "unauthorized"),
                {"WWW-Authenticate": 'Bearer realm="mcp"'},
            )
            return
        raw = self._read_body()
        if raw is None:
            return  # _read_body 已回包（体积超限/读失败）
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
            # Streamable HTTP 的 SSE 通道本实现不提供（规范允许）：明确回 405
            # 而不是挂住连接，手机端才不会一直等流式事件
            self._send_json(
                405,
                _error(None, -32600, "method not allowed: use POST /mcp"),
                {"Allow": "POST"},
            )
            return
        self._send_text(404, "not found")

    # ------------------------------------------------------------------
    # 底层读写
    # ------------------------------------------------------------------

    def _mcp(self) -> McpServer:
        return self.server.mcp_ref  # type: ignore[attr-defined]

    def _read_body(self) -> Optional[bytes]:
        """读请求体；体积超限或读失败时已回包并返回 None."""
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
            # 弱网断连/读超时：没法再往这个连接写有意义的内容，静默收口
            logger.debug("MCP 请求体读取失败（连接中断）")
            return None

    def _send_json(self, status: int, payload: Dict[str, Any], extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": JSON_CONTENT_TYPE, "Content-Length": str(len(body))}
        if extra_headers:
            headers.update(extra_headers)
        self._respond(status, headers, body)

    def _send_empty(self, status: int) -> None:
        """202 之类无 body 的应答：显式 Content-Length: 0 保证 keep-alive 不串包."""
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
        """接管访问日志：抹掉查询串后再落盘.

        BaseHTTPRequestHandler 默认把整行请求（含 ``?token=<token>``）写进日志；
        手机端用查询参数兜底鉴权时 token 会原样泄露到日志文件，所以统一先剥掉
        查询串，只留 path。鉴权失败也照样记录（不含凭据），便于排查手机端配置。
        """
        safe_args = []
        for arg in args:
            text = str(arg)
            if "?" in text:
                text = text.split("?", 1)[0]
            safe_args.append(text)
        logger.debug("MCP http: " + str(fmt), *safe_args)
