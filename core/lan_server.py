"""局域网手机远程控制的 HTTP + WebSocket 单端口服务。

在 Y-code 进程内起一个单端口服务，同时承载：
- HTTP：静态前端（复用 frontend/dist 构建产物）
- WebSocket：手机端 RPC（白名单桥接）与事件广播

事件广播总线见 ``broadcast``；手机通过 ``ALLOWED_METHODS`` 白名单调用 bridge。
"""

import json
import mimetypes
import os
import secrets
import socket
import threading
import time
from http import HTTPStatus
from typing import Any, Dict, Optional

from websockets.http11 import Headers, Response
from websockets.sync.server import serve


class LanServer:
    """在 Y-code 进程内运行的局域网远程控制服务。

    与桌面 GUI 共享同一 bridge，使手机通过 Web 页面作为第二个前端。
    """

    # 手机端可调用的 bridge 方法白名单（桌面专属方法如 window_close 绝不暴露）
    ALLOWED_METHODS = frozenset(
        {
            "send_user_message",
            "respond_permission",
            "respond_permission_choice",
            "get_history",
            "get_session_groups",
            "switch_session",
            "new_session",
            "cancel_turn",
            "get_gui_theme",
            "get_active_model",
        }
    )

    def __init__(self, bridge=None, port=0):
        self.bridge = bridge
        self.port = port
        self.lan_ip = self._detect_lan_ip()
        self._clients = []
        self._clients_lock = threading.Lock()
        self._token = None
        self._token_expires = 0.0
        self._ttl = 86400
        self._server = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ #
    # 网络探测 / URL / token
    # ------------------------------------------------------------------ #

    def _detect_lan_ip(self) -> str:
        """探测本机局域网出口 IP，失败回退到 hostname 解析。"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            return socket.gethostbyname(socket.gethostname())

    def bind_url(self, token: str) -> str:
        """生成手机扫码打开时携带配对 token 的绑定 URL。"""
        return f"http://{self.lan_ip}:{self.port}/?token={token}"

    def _verify_token(self, token) -> bool:
        """校验配对 token：非空、未过期、常量时间比较。"""
        if not token or not self._token:
            return False
        if time.time() > self._token_expires:
            return False
        try:
            return secrets.compare_digest(token.encode(), self._token)
        except Exception:
            return False

    def _issue_token(self) -> str:
        """签发新配对 token（旧 token 立即失效），返回可展示的明文。"""
        self._token = secrets.token_urlsafe(24).encode()
        self._token_expires = time.time() + self._ttl
        return self._token.decode()

    # ------------------------------------------------------------------ #
    # 事件广播
    # ------------------------------------------------------------------ #

    def broadcast(self, payload: str) -> None:
        """向所有已连接客户端发送事件 payload，异常吞掉。"""
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.send(payload)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # WebSocket RPC
    # ------------------------------------------------------------------ #

    def _handle_ws(self, ws) -> None:
        """WebSocket 连接处理：首消息校验 token → 循环路由 RPC。"""
        try:
            raw = ws.recv(timeout=10)
        except Exception:
            raw = None
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        except Exception:
            msg = None
        if not isinstance(msg, dict) or not self._verify_token(msg.get("token")):
            try:
                ws.close(code=4401, reason="invalid token")
            except Exception:
                pass
            return
        with self._clients_lock:
            self._clients.append(ws)
        try:
            for raw in ws:
                self._route_message(ws, raw)
        finally:
            with self._clients_lock:
                try:
                    self._clients.remove(ws)
                except ValueError:
                    pass

    def _route_message(self, ws, raw: str) -> None:
        """解析单条 RPC 消息并回包 ``{"id", "result"}`` 或 ``{"id", "error"}``。"""
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        method = msg.get("method")
        args = msg.get("args") or {}
        msg_id = msg.get("id")
        if method not in self.ALLOWED_METHODS:
            ws.send(json.dumps({"id": msg_id, "error": "method not allowed"}))
            return
        result = self._call_bridge(method, args)
        ws.send(json.dumps({"id": msg_id, "result": result}))

    def _call_bridge(self, method, args) -> Dict[str, Any]:
        """调用白名单内的 bridge 方法，异常统一吞掉回包。"""
        if self.bridge is None:
            return {"ok": False, "msg": "bridge 未初始化"}
        fn = getattr(self.bridge, method, None)
        if fn is None:
            return {"ok": False, "msg": "未知方法"}
        try:
            return fn(**args)
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    # ------------------------------------------------------------------ #
    # HTTP 静态服务（作为 websockets process_request 回调）
    # ------------------------------------------------------------------ #

    def _serve_http(self, connection, request):
        """websockets 的 ``process_request`` 回调：HTTP 路由，WS 升级放行。"""
        # WebSocket 升级请求返回 None，交回 websockets 完成握手
        if request.headers.get("Upgrade") == "websocket":
            return None
        path = request.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/mobile-shim.js":
            return self._serve_shim()
        if path == "/theme":
            return self._serve_theme()
        if path == "/health":
            return self._http_response(200, "ok", "text/plain; charset=utf-8")
        return self._serve_static(path)

    @staticmethod
    def _dist_dir() -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "frontend",
            "dist",
        )

    def _http_response(self, status_code: int, body, content_type: str) -> Response:
        """构造一个带 Content-Type / Content-Length 的 HTTP 响应。"""
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        return Response(status_code, HTTPStatus(status_code).phrase, headers, body)

    def _http_404(self) -> Response:
        return self._http_response(404, "not found", "text/plain; charset=utf-8")

    def _serve_index(self) -> Response:
        """读 dist/index.html，在 ``</head>`` 前注入 mobile-shim。"""
        index_path = os.path.join(self._dist_dir(), "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self._http_404()
        html = html.replace(
            "</head>", '<script src="/mobile-shim.js"></script></head>', 1
        )
        return self._http_response(200, html, "text/html; charset=utf-8")

    def _serve_shim(self) -> Response:
        """mobile-shim 占位：真实内容在 Task 6 补全。"""
        return self._http_404()

    def _serve_theme(self) -> Response:
        """主题占位：Task 后续补真实主题 tokens。"""
        return self._http_response(
            200, json.dumps({"ok": True, "theme": {}}), "application/json; charset=utf-8"
        )

    def _serve_static(self, path: str) -> Response:
        """读 frontend/dist 下的静态文件，带路径穿越防护。"""
        dist = os.path.normpath(self._dist_dir())
        rel = path.lstrip("/")
        if rel == "":
            rel = "index.html"
        full = os.path.normpath(os.path.join(dist, rel))
        if full != dist and not full.startswith(dist + os.sep):
            return self._http_404()
        if not os.path.isfile(full):
            return self._http_404()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._http_404()
        return self._http_response(200, data, ctype)

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def start(self) -> Dict:
        """启动单端口 HTTP + WebSocket 服务（阻塞 serve 放到 daemon 线程）。"""
        self._server = serve(
            self._handle_ws,
            host="0.0.0.0",
            port=self.port,
            process_request=self._serve_http,
        )
        # port=0 时内核动态分配，真实端口回填
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="lan-remote-server",
        )
        self._thread.start()
        return {"ok": True, "host": "0.0.0.0", "port": self.port, "lan_ip": self.lan_ip}

    def stop(self) -> None:
        """关闭服务（关闭监听 socket 让 serve_forever 退出）。"""
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
