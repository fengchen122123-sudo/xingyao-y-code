"""局域网手机远程控制的 HTTP + WebSocket 单端口服务。

在 Y-code 进程内起一个单端口服务，同时承载：
- HTTP：静态前端（复用 frontend/dist 构建产物）
- WebSocket：手机端 RPC（白名单桥接）与事件广播

事件广播总线见 ``broadcast``；手机通过 ``ALLOWED_METHODS`` 白名单调用 bridge。
"""

import base64
import io
import json
import mimetypes
import os
import secrets
import socket
import threading
import time

import qrcode
from http import HTTPStatus
from typing import Any, Dict, Optional

from websockets.http11 import Headers, Response
from websockets.sync.server import serve

import config
from config import save_config_value as _save_config_value

# 手机端传输 shim 的内嵌快照：_serve_shim() 优先读磁盘上的
# frontend/mobile-shim.js（随包分发、可独立更新）；磁盘文件读不到时
# （如 1.2.16 及更早的安装包漏打包该文件）回退这份内嵌内容，保证手机端
# 永远能拿到 shim，不再出现「pywebview API 不可用」。
# 注意：修改磁盘版 mobile-shim.js 后请同步更新此快照，保持两边一致。
_MOBILE_SHIM_FALLBACK_JS = """/**
 * mobile-shim.js —— 局域网手机远程控制的 transport shim（内嵌回退快照）
 *
 * 桌面上 pywebview 注入 window.pywebview.api（RPC）与 window.onBridgeEvent（事件）。
 * 手机上无 pywebview，本 shim 用 WebSocket 模拟这两者，使手机加载同一个 dist 前端、
 * 复用全部组件与主题。
 *
 * 约定（与 core/lan_server.py 对齐）：
 * - WebSocket 首条消息发送 {"token": "..."} 完成配对校验。
 * - 手机 → 电脑 RPC：{"method", "args", "id"}，args 为位置参数数组，回包 {"id", "result"}。
 * - 电脑 → 手机事件：{"type", "data"} 广播（后端 broadcast 发的是 JSON 字符串）。
 */
(function () {
  if (window.pywebview) return; // 桌面环境不注入，防御性

  var token = new URLSearchParams(window.location.search).get('token') || '';
  var wsUrl = (window.location.protocol === 'https:' ? 'wss' : 'ws') + '://' + window.location.host + '/';

  var ws = null;
  var seq = 0;
  var pending = new Map(); // id -> {resolve, reject}

  function rpc(method, args) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) {
        reject(new Error('\\u8fde\\u63a5\\u672a\\u5c31\\u7eea'));
        return;
      }
      var id = ++seq;
      pending.set(id, { resolve: resolve, reject: reject });
      ws.send(JSON.stringify({ method: method, args: args, id: id }));
    });
  }

  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = function () {
      ws.send(JSON.stringify({ token: token }));
      // 就绪后才注入 api，并触发前端等待的 pywebviewready 事件
      window.pywebview = {
        api: new Proxy({}, {
          get: function (_target, prop) {
            if (typeof prop !== 'string') return undefined;
            return function () {
              return rpc(prop, Array.prototype.slice.call(arguments));
            };
          },
        }),
      };
      window.dispatchEvent(new Event('pywebviewready'));
    };
    ws.onmessage = function (event) {
      var msg;
      try {
        msg = JSON.parse(event.data);
      } catch (_) {
        return;
      }
      if (msg && typeof msg.id !== 'undefined') {
        // RPC 响应
        var p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          if (msg.error) {
            p.reject(new Error(msg.error));
          } else {
            p.resolve(msg.result);
          }
        }
      } else if (msg && msg.type && window.onBridgeEvent) {
        // 事件广播（对象 {type, data}）
        window.onBridgeEvent(msg);
      }
    };
    ws.onclose = function () {
      setTimeout(connect, 2000); // 基础断线重连
    };
    ws.onerror = function () {};
  }

  connect();
})();
"""


class LanServer:
    """在 Y-code 进程内运行的局域网远程控制服务。

    与桌面 GUI 共享同一 bridge，使手机通过 Web 页面作为第二个前端。
    """

    # 手机端复用整个前端，需要调用几乎所有 bridge 方法（除桌面专属/危险方法）。
    # 因此用「黑名单」而非「白名单」：默认放行，只封禁 FORBIDDEN_METHODS 里的方法，
    # 避免白名单遗漏前端方法导致 "method not allowed"（如 get_background_tasks）。
    # 黑名单覆盖：窗口控制、桌面文件/系统交互、更新安装等桌面专属或危险方法，
    # 以及电脑操控权限扩张方法（Task 15，设计 18：手机只可查看状态与紧急停止，
    # 不得远程开启/暂停/恢复/确认安全检查）与 drain_js_queue（Task 13a 审查
    # Minor 1 的 fail-closed 默认裁决：LAN 排空 _js_queue 会偷走桌面事件并
    # 可能消费关停哨兵）。
    FORBIDDEN_METHODS = frozenset({
        # 窗口控制（桌面专属，手机无意义）
        "window_minimize", "window_toggle_maximize", "window_native_maximize",
        "window_close", "window_is_maximized", "window_begin_drag",
        "window_begin_resize", "window_confirm_close", "set_titlebar_theme",
        # 桌面文件/系统交互（手机无意义）
        "select_workspace_folder", "open_folder_in_explorer", "open_path",
        "pick_files", "report_dropped_files", "save_clipboard_image",
        # 更新/安装（危险，桌面专属）
        "download_update", "cancel_download_update", "launch_installer", "check_update",
        # 电脑操控权限扩张（Task 15，设计 18：LAN 只放行 status/stop）
        "set_browser_control_enabled", "set_computer_control_enabled",
        "set_computer_browser", "pause_computer_control",
        "resume_computer_control", "acknowledge_computer_safety",
        # GUI 事件桥内部测试 seam（等价于窃听/阻断桌面事件流）
        "drain_js_queue",
    })

    # 会创建用户 Turn 的消息入口（Task 15，设计 18）：dispatch 层由服务器
    # 内部注入 turn_source="lan" 并覆盖客户端 payload 同名字段，模型/用户
    # 正文无法伪造回合来源；本地 GUI/CLI 调用不经本层，固定 local。
    MESSAGE_ENTRY_METHODS = frozenset({"send_user_message"})

    # send_user_message(text, session_name, attachments, workdir, model_id,
    # turn_source)——turn_source 前共 5 个位置参数（gui_bridge.py 真实签名）。
    # LAN 分发把客户端位置参数裁剪至前 5 个：第 6 位（turn_source 所在位置）
    # 起的绑定通道全部剥离，turn_source 只能由服务器以 keyword 唯一注入。
    MESSAGE_ENTRY_POSITIONAL_LIMIT = 5

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
        """探测本机局域网 IP：优先 WiFi 网卡，其次默认路由出口，最后 hostname 解析。

        多网卡（WiFi + 有线 + 虚拟网卡）环境下，UDP connect 探测默认路由出口 IP
        会随路由 metric 变化而漂移（可能返回有线/虚拟网卡 IP），导致手机按错误
        IP 访问失败。这里优先识别 WLAN 网卡，保证「手机连同一 WiFi」的典型场景正确。
        """
        # 1. 优先 WiFi 网卡（WLAN / Wi-Fi / 无线），排除 loopback 与 APIPA
        try:
            import psutil

            for iface, addrs in psutil.net_if_addrs().items():
                if any(key in iface.lower() for key in ("wlan", "wi-fi", "wireless", "wifi", "无线")):
                    for addr in addrs:
                        if addr.family == socket.AF_INET and not addr.address.startswith(("127.", "169.254.")):
                            return addr.address
        except Exception:
            pass
        # 2. 回退：UDP connect 取默认路由出口 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            pass
        # 3. 最后：hostname 解析
        return socket.gethostbyname(socket.gethostname())

    def bind_url(self, token: str) -> str:
        """生成手机扫码打开时携带配对 token 的绑定 URL。"""
        return f"http://{self.lan_ip}:{self.port}/?token={token}"

    def _verify_token(self, token) -> bool:
        """校验配对 token：非空、未过期、常量时间比较；_token_expires 为 None 表示永久有效。"""
        if not token or not self._token:
            return False
        if self._token_expires is not None and time.time() > self._token_expires:
            return False
        try:
            return secrets.compare_digest(token.encode(), self._token)
        except Exception:
            return False

    def _kick_all_clients(self) -> None:
        """断开所有已连接客户端并清空列表（token 轮换时防止旧链接继续可控）。

        必须在持锁状态下调用：先摘除列表再逐个 close，close 的网络 IO 放锁外，
        避免阻塞注册路径。
        """
        with self._clients_lock:
            clients = self._clients
            self._clients = []
        for client in clients:
            try:
                client.close(code=4401, reason="token rotated")
            except Exception:
                pass

    def _issue_token(self) -> str:
        """签发新配对 token（旧 token 立即失效，已连接客户端全部断开），持久化后返回明文。

        _ttl <= 0 表示永久有效：_token_expires 置为 None，永不过期、永不重签。
        """
        self._token = secrets.token_urlsafe(24).encode()
        self._token_expires = (
            None if self._ttl <= 0 else time.time() + self._ttl
        )
        # 踢掉旧 token 时代的连接：否则已扫码手机在刷新二维码后依然可控
        self._kick_all_clients()
        try:
            _save_config_value(config.REMOTE_PAIR_TOKEN_KEY, self._token.decode())
            _save_config_value(
                config.REMOTE_PAIR_TOKEN_EXPIRES_KEY, self._token_expires
            )
        except Exception:
            # 持久化失败不阻断签发：仅影响重启后的固定性，不影响本次可用性
            pass
        return self._token.decode()

    def _authenticate_and_register(self, ws, token) -> bool:
        """校验 token 并原子注册客户端：通过则加入列表，失败返回 False。

        校验与注册在同一把锁内完成，避免与 _issue_token 的轮换竞态：
        旧 token 校验通过的瞬间不会被并发重签钻空子。
        """
        with self._clients_lock:
            if not self._verify_token(token):
                return False
            self._clients.append(ws)
            return True

    def _load_persisted_token(self, token: str, expires_at) -> None:
        """从持久层恢复配对 token；过期或为空则忽略。

        expires_at 为 None（永久 token，TTL=0 签发）时直接恢复且永不过期。
        """
        clean = str(token or "").strip()
        if not clean:
            return
        if expires_at is not None:
            try:
                expires_at = float(expires_at)
            except (TypeError, ValueError):
                return
            if time.time() >= expires_at:
                return
        self._token = clean.encode()
        self._token_expires = expires_at

    def _load_token_from_config(self) -> None:
        """启动时从 config.json 恢复未过期 token（不存在则忽略）。"""
        try:
            data = config._load()
            self._load_persisted_token(
                data.get(config.REMOTE_PAIR_TOKEN_KEY),
                data.get(config.REMOTE_PAIR_TOKEN_EXPIRES_KEY),
            )
        except Exception:
            pass

    def get_bind_info(self, force_new: bool = False) -> Dict:
        """返回手机扫码绑定信息。

        token 固定策略：未过期直接复用（多次打开/应用重启 token 不变）；
        已过期自动重签；force_new=True（前端"刷新"按钮）强制重签并使旧
        token 立即失效。expires_in 返回剩余有效秒数（而非完整 TTL）。
        """
        if force_new or not self._token or (
            self._token_expires is not None and time.time() >= self._token_expires
        ):
            self._issue_token()
        token = self._token.decode()
        url = self.bind_url(token)
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode()
        remaining = (
            0 if self._token_expires is None
            else max(0, int(self._token_expires - time.time()))
        )
        return {
            "ok": True,
            "url": url,
            "token": token,
            "qr": f"data:image/png;base64,{qr_b64}",
            # expires_in=0 表示永久有效（TTL=0 签发），前端据此跳过倒计时自动刷新
            "expires_in": remaining,
        }

    def set_ttl(self, seconds: int) -> None:
        """更新配对 token 的有效期（秒）；<= 0 表示永久有效。"""
        self._ttl = int(seconds)

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
        if not isinstance(msg, dict) or not self._authenticate_and_register(
            ws, msg.get("token")
        ):
            try:
                ws.close(code=4401, reason="invalid token")
            except Exception:
                pass
            return
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
        result = self.dispatch_rpc(method, args)
        # 禁止方法的错误形状 {"error": {"code": "method_not_allowed"}} 翻译为
        # 既有 wire 格式（字符串 error），保持手机端 shim 兼容
        error = result.get("error") if isinstance(result, dict) else None
        if isinstance(error, dict) and error.get("code") == "method_not_allowed":
            ws.send(json.dumps({"id": msg_id, "error": "method not allowed"}))
            return
        ws.send(json.dumps({"id": msg_id, "result": result}))

    def dispatch_rpc(self, method, args) -> Dict[str, Any]:
        """单次 RPC 分发核心（WebSocket 路由与直调测试共用同一边界）。

        禁止方法统一返回 ``{"error": {"code": "method_not_allowed"}}``；
        下划线前缀的私有方法（如 ``_queue_js_payload``）同样拒绝——内部
        seam 可向桌面 WebView 注入任意 JS，绕过全部 LAN 边界（Task 15
        审查 Important-1，fail-closed 默认裁决：设计 18「后端必须实施
        同一边界」）。消息入口方法由服务器内部注入 ``turn_source="lan"``
        并剥离客户端一切同名绑定通道（设计 18：LAN 回合不可伪装成本地
        回合获取电脑操控）。

        非字符串 method（Task 16 累积小修）统一回 method_not_allowed：
        修复前 unhashable 值在 FORBIDDEN_METHODS 判定处抛 TypeError、
        int/None 在 getattr 分发处抛 TypeError，WebSocket 路由未捕获直接
        断连——恶意/畸形载荷不得以异常打断服务。
        """
        if not isinstance(method, str):
            return {"error": {"code": "method_not_allowed"}}
        if method in self.FORBIDDEN_METHODS:
            return {"error": {"code": "method_not_allowed"}}
        if method.startswith("_"):
            return {"error": {"code": "method_not_allowed"}}
        extra_kwargs = None
        if method in self.MESSAGE_ENTRY_METHODS:
            normalized = self._force_lan_turn_source(args)
            if normalized is None:
                return {"ok": False, "msg": "消息参数无法规范化，已拒绝"}
            args, extra_kwargs = normalized
        return self._call_bridge(method, args, extra_kwargs)

    def _force_lan_turn_source(self, args):
        """消息入口参数规范化：剥离客户端一切 turn_source 绑定通道。

        真实 bridge 签名 send_user_message(text, session_name, attachments,
        workdir, model_id, turn_source)——turn_source 是第 6 个位置参数，
        客户端可经位置参数数组第 6 位、kwargs dict 或首参 payload dict
        绑定同名通道（Task 15 审查 Critical-1 两条绕过向量）。统一规范化
        （返回 ``(args, extra_kwargs)``；无法规范化返回 ``None`` 由调用方
        fail-closed 拒绝）：
        - list：裁剪位置参数至前 5 个（text..model_id），第 6 位起丢弃；
        - 首参为 dict（payload 形态）：删除其 turn_source 键（防夹带）；
        - dict（kwargs 形态）：删除 turn_source 键后以 lan 覆盖。
        最终 turn_source 只能由服务器经 _call_bridge 的 keyword 通道
        注入 "lan"，客户端不存在任何可达的绑定通道。
        """
        if isinstance(args, dict):
            merged = dict(args)
            merged.pop("turn_source", None)
            merged["turn_source"] = "lan"
            return merged, None
        if isinstance(args, list):
            items = list(args[: self.MESSAGE_ENTRY_POSITIONAL_LIMIT])
            if items and isinstance(items[0], dict):
                payload = dict(items[0])
                payload.pop("turn_source", None)
                items[0] = payload
            return items, {"turn_source": "lan"}
        return None

    def _call_bridge(self, method, args, extra_kwargs=None) -> Dict[str, Any]:
        """调用 bridge 方法（支持位置参数与关键字参数），异常统一吞掉回包。"""
        if self.bridge is None:
            return {"ok": False, "msg": "bridge 未初始化"}
        fn = getattr(self.bridge, method, None)
        if fn is None:
            return {"ok": False, "msg": "未知方法"}
        try:
            if isinstance(args, list):
                return fn(*args, **(extra_kwargs or {}))
            if isinstance(args, dict):
                merged = dict(args)
                if extra_kwargs:
                    merged.update(extra_kwargs)
                return fn(**merged)
            return fn(**(extra_kwargs or {}))
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
        """返回 mobile-shim.js：优先磁盘文件，缺失时回退内嵌快照。

        磁盘版随包分发、可独立更新；内嵌快照兜底旧安装包漏打包该文件
        导致的 404（手机端表现为「pywebview API 不可用」）。
        """
        shim_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "frontend",
            "mobile-shim.js",
        )
        try:
            with open(shim_path, "rb") as f:
                data = f.read()
        except OSError:
            # 回退：旧安装包未携带磁盘版时使用代码内嵌快照，保证手机可用
            data = _MOBILE_SHIM_FALLBACK_JS.encode("utf-8")
        return self._http_response(
            200, data, "application/javascript; charset=utf-8"
        )

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
        # 启动时恢复上次的配对 token：保证应用重启后二维码 token 保持固定
        self._load_token_from_config()
        # 端口同样固定：优先复用上次持久化的端口，避免每次启动换端口导致
        # 手机端需重新扫码；端口被占用时回退内核动态分配并回填持久化
        preferred = self.port or self._load_persisted_port()
        try:
            self._server = self._serve(preferred)
        except OSError:
            # 持久化端口被其他进程占用：回退动态分配，下方回填新的持久化端口
            self._server = self._serve(0)
        # port=0 时内核动态分配，真实端口回填
        self.port = self._server.socket.getsockname()[1]
        self._persist_port()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="lan-remote-server",
        )
        self._thread.start()
        return {"ok": True, "host": "0.0.0.0", "port": self.port, "lan_ip": self.lan_ip}

    def _serve(self, port: int):
        return serve(
            self._handle_ws,
            host="0.0.0.0",
            port=port,
            process_request=self._serve_http,
            max_size=70 * 1024 * 1024,  # 支持手机上传 ≤50MB 文件（base64 约 67MB）
        )

    def _load_persisted_port(self) -> int:
        """从 config.json 读取上次持久化的端口（无效/不存在返回 0=动态分配）。"""
        try:
            port = int(config._load().get(config.REMOTE_PAIR_PORT_KEY) or 0)
        except Exception:
            return 0
        return port if 1024 <= port <= 65535 else 0

    def _persist_port(self) -> None:
        """把实际绑定端口写回 config.json，供下次启动复用。"""
        try:
            _save_config_value(config.REMOTE_PAIR_PORT_KEY, self.port)
        except Exception:
            pass

    def stop(self) -> None:
        """关闭服务（关闭监听 socket 让 serve_forever 退出）。"""
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
