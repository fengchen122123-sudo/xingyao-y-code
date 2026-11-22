import base64
import io
import json
import logging
import mimetypes
import os
import queue as queue_mod
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
from core.local_image_inline import inline_local_images_in_payload
from core.remote_transport import EventReplayBuffer

logger = logging.getLogger(__name__)

PROCESS_BOOT_ID = secrets.token_hex(8)

_OUTBOUND_QUEUE_MAX = 500
_OUTBOUND_DROP_WARN_EVERY = 200

REPLAYABLE_EVENT_TYPES = frozenset({
    "notice", "error",
    "turn_start", "turn_finish", "turn_cancelled", "turn_error",
    "message_final", "message_queue",
    "permission_request", "permission_dismiss",
    "model_changed", "session_renamed", "active_session_changed",
    "plan_updated", "plan_item_handoff", "turn_undo_updated",
    "background_task_finished", "background_subagent_terminal",
    "automation_tasks_changed", "automation_run_updated",
})

_REPLAY_BUFFER_MAX = 128

_MOBILE_SHIM_FALLBACK_JS = """(function () {
  if (window.pywebview) return;

  var STORAGE_KEY = 'yc-pair-token';
  var urlToken = new URLSearchParams(window.location.search).get('token') || '';
  var token = urlToken;
  try {
    if (urlToken) {
      window.localStorage.setItem(STORAGE_KEY, urlToken);
    } else {
      token = window.localStorage.getItem(STORAGE_KEY) || '';
    }
  } catch (_) {
    // localStorage 不可用（隐私模式等）：仅本次 URL token 生效
  }
  // 地址栏清理：token 已写入 localStorage（或仅本次内存）后立即去掉 URL 上的
  // ?token=，避免残留在浏览器历史、截图与分享链接中；保留 path 与 hash。
  // 独立于上面的 try：隐私模式下 localStorage 抛错时 URL 同样要清理。
  if (urlToken) {
    try {
      if (window.history && window.history.replaceState) {
        window.history.replaceState(null, '', window.location.pathname + window.location.hash);
      }
    } catch (_) {}
  }
  var usedToken = token;
  // 云中转的完整桌面 UI / 独立移动端：服务器注入 __ycRelayDevice，直接连网关 relay。
  // 注入值有一个坑：SPA 子路由刷新时服务端会把路由名当设备号注入（/m/convo）。
  // 设备号是 UUID，注入值不像 UUID 就回退到上次记住的 UUID，否则刷新后连接的是假设备。
  var DEVICE_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  var injectedDevice = window.__ycRelayDevice || '';
  var rememberedDevice = '';
  var rememberedPrefix = '';
  try {
    rememberedDevice = window.localStorage.getItem('yc-relay-device') || '';
    rememberedPrefix = window.localStorage.getItem('yc-relay-prefix') || '';
    if (DEVICE_RE.test(injectedDevice)) {
      window.localStorage.setItem('yc-relay-device', injectedDevice);
      rememberedDevice = injectedDevice;
    }
  } catch (_) {}
  var relayDevice = DEVICE_RE.test(injectedDevice)
    ? injectedDevice
    : (DEVICE_RE.test(rememberedDevice)
      ? rememberedDevice
      : ((window.location.pathname || '').match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i) || [''])[0]);
  // 网关可能被反代到子路径（如 /xy/bot/desktop/<设备号> 或刷新后的 /xy/bot/m/convo）：
  // ws 端点必须跟着前缀走，否则固定打根路径 /relay/client 会 404。
  var relayPrefix = '';
  if (relayDevice) {
    var pathname = window.location.pathname || '';
    var relaySegs = pathname.split('/');
    var relayAt = relaySegs.indexOf(relayDevice);
    if (relayAt > 1) {
      relayPrefix = relaySegs.slice(0, relayAt - 1).join('/');
    } else {
      for (var si = 1; si < relaySegs.length; si++) {
        if (relaySegs[si] === 'm' || relaySegs[si] === 'desktop') {
          relayPrefix = relaySegs.slice(0, si).join('/');
          break;
        }
      }
      if (!relayPrefix && rememberedPrefix) relayPrefix = rememberedPrefix;
    }
    try {
      if (DEVICE_RE.test(injectedDevice)) {
        window.localStorage.setItem('yc-relay-prefix', relayPrefix);
      }
    } catch (_) {}
  }
  var wsUrl = (window.location.protocol === 'https:' ? 'wss' : 'ws') + '://' + window.location.host + (relayDevice ? relayPrefix + '/relay/client?device_id=' + encodeURIComponent(relayDevice) : '/');

  var ws = null;
  var seq = 0;
  var lastSeq = 0;
  var bootId = '';
  var sawHello = false;
  var legacy = false;
  var resyncing = false;
  var pausedSeq = 0;
  var held = [];
  var seen = {};
  var refetchOnce = false;
  var hadConnection = false;
  var pending = new Map();

  function isCapacitor() {
    return !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  }

  function watermarkKey() {
    var account = window.__ycAccountId || '';
    var profile = window.__ycProfile || (relayDevice ? 'relay:' + relayDevice : 'lan');
    return 'yc-watermark:' + window.location.origin + '|' + account + '|' + profile;
  }

  function loadWatermark() {
    if (isCapacitor()) return;
    try {
      var raw = window.localStorage.getItem(watermarkKey());
      if (!raw) return;
      var parsed = JSON.parse(raw);
      bootId = parsed.boot_id || '';
      lastSeq = Number(parsed.last_seq) || 0;
    } catch (_) {}
  }

  function saveWatermark() {
    if (isCapacitor() || legacy || (!bootId && !relayDevice)) return;
    try {
      window.localStorage.setItem(watermarkKey(), JSON.stringify({
        boot_id: bootId,
        last_seq: lastSeq,
      }));
    } catch (_) {}
  }

  loadWatermark();

  window.__ycClearTransport = function () {
    bootId = '';
    lastSeq = 0;
    seen = {};
    held = [];
    try { window.localStorage.removeItem(watermarkKey()); } catch (_) {}
    if (ws) {
      try { ws.close(); } catch (_) {}
    }
    pending.forEach(function (item) {
      if (item.timer) clearTimeout(item.timer);
      item.reject(new Error('连接已关闭'));
    });
    pending.clear();
  };

  function rpc(method, args) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) {
        reject(new Error('连接未就绪'));
        return;
      }
      var id = ++seq;
      // 设备离线/对端失联时不能无限等：否则 App 的启动页永远关不掉。
      var timer = setTimeout(function () {
        if (!pending.has(id)) return;
        pending.delete(id);
        reject(new Error('连接超时，电脑可能离线'));
      }, 25000);
      pending.set(id, { resolve: resolve, reject: reject, timer: timer });
      ws.send(JSON.stringify({ method: method, args: args, id: id }));
    });
  }

  function turnIsTerminal(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return false;
    var status = snapshot.status || snapshot.state || '';
    return status === 'completed' || status === 'cancelled' || status === 'canceled'
      || status === 'error' || status === 'failed' || status === 'stopped'
      || status === 'interrupted';
  }

  function acceptFrame(msg) {
    var frameSeq = typeof msg.seq === 'number' ? msg.seq : null;
    if (frameSeq !== null) {
      var key = (bootId || '') + ':' + frameSeq;
      if (seen[key]) return;
      seen[key] = true;
      if (frameSeq > lastSeq) lastSeq = frameSeq;
      saveWatermark();
    }
    if (resyncing) {
      held.push(msg);
      return;
    }
    if (window.onBridgeEvent) window.onBridgeEvent(msg);
  }

  function beginResync() {
    resyncing = true;
    held = [];
    pausedSeq = lastSeq;
    rpc('get_initial_state', []).then(function (state) {
      window.dispatchEvent(new CustomEvent('yc-initial-state', { detail: state }));
      var batch = held;
      held = [];
      resyncing = false;
      var snapshots = (state && state.active_turn_snapshots) || {};
      var terminal = false;
      if (snapshots && typeof snapshots === 'object') {
        Object.keys(snapshots).forEach(function (name) {
          if (turnIsTerminal(snapshots[name])) terminal = true;
        });
      }
      var uncertain = false;
      batch.forEach(function (frame) {
        if (typeof frame.seq === 'number' && frame.seq <= pausedSeq) return;
        if ((frame.type === 'token_delta' || frame.type === 'thinking_delta') && terminal) return;
        if ((frame.type === 'token_delta' || frame.type === 'thinking_delta') && !state) {
          uncertain = true;
          return;
        }
        if (window.onBridgeEvent) window.onBridgeEvent(frame);
      });
      if (uncertain && !refetchOnce) {
        refetchOnce = true;
        beginResync();
      }
    }, function () {
      resyncing = false;
      held = [];
    });
  }

  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = function () {
      sawHello = false;
      var auth = { token: token };
      if (relayDevice) { auth.device_id = relayDevice; auth.jwt = token; }
      if (bootId && !legacy) {
        auth.last_boot_id = bootId;
        auth.last_seq = lastSeq;
      } else if (hadConnection && !legacy) {
        auth.last_seq = lastSeq;
      }
      hadConnection = true;
      ws.send(JSON.stringify(auth));
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
        var p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          if (p.timer) clearTimeout(p.timer);
          if (msg.error) {
            p.reject(new Error(msg.error));
          } else {
            p.resolve(msg.result);
          }
        }
      } else if (msg && msg.type) {
        if (msg.type === 'hello') {
          sawHello = true;
          bootId = (msg.data && msg.data.boot_id) || bootId;
          saveWatermark();
          return;
        }
        if (msg.type === 'seq_reset') {
          sawHello = true;
          bootId = (msg.data && msg.data.boot_id) || bootId;
          lastSeq = 0;
          seen = {};
          saveWatermark();
          if (msg.data && msg.data.resync_required) beginResync();
          return;
        }
        if (msg.type === 'replay') {
          sawHello = true;
          var replayFrames = Array.isArray(msg.frames) ? msg.frames : [];
          for (var i = 0; i < replayFrames.length; i++) {
            var item = replayFrames[i];
            if (typeof item === 'string') {
              try { item = JSON.parse(item); } catch (_) { item = null; }
            }
            if (item && typeof item === 'object' && item.type) {
              acceptFrame(item);
            }
          }
          return;
        }
        if (msg.type === 'resync_required' || msg.type === 'device_reset') {
          sawHello = true;
          if (msg.data && msg.data.boot_id) bootId = msg.data.boot_id;
          lastSeq = 0;
          seen = {};
          saveWatermark();
          beginResync();
          return;
        }
        if (msg.type === 'device_online') {
          sawHello = true;
          if (msg.data && msg.data.boot_id) {
            bootId = msg.data.boot_id;
            saveWatermark();
          }
          if (window.onBridgeEvent) {
            window.onBridgeEvent({
              type: 'device_status',
              data: {
                device_id: (msg.data && msg.data.device_id) || relayDevice,
                online: true,
              },
            });
          }
          return;
        }
        if (msg.type === 'device_status') {
          if (window.onBridgeEvent) window.onBridgeEvent(msg);
          return;
        }
        if (!sawHello && !legacy) {
          legacy = true;
          beginResync();
        }
        acceptFrame(msg);
      }
    };
    ws.onclose = function (event) {
      pending.forEach(function (item) {
        if (item.timer) clearTimeout(item.timer);
        item.reject(new Error('连接已断开'));
      });
      pending.clear();
      if (event && event.code === 4401) {
        // 配对失效/被轮换：清掉本地 token（仅当与本次使用的相同，避免误清新 token），
        // 通知前端显示「配对已失效」，并停止重连（空 token 重连只会一直失败）
        try {
          if (window.localStorage.getItem(STORAGE_KEY) === usedToken) {
            window.localStorage.removeItem(STORAGE_KEY);
          }
        } catch (_) {}
        try {
          window.dispatchEvent(new CustomEvent('yc-pair-invalid'));
        } catch (_) {}
        return;
      }
      setTimeout(connect, 2000);
    };
    ws.onerror = function () {};
  }

  connect();
})();
"""


def build_pair_payload(url: str, token: str, expires_in: int) -> Dict:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()
    return {
        "ok": True,
        "url": url,
        "token": token,
        "qr": f"data:image/png;base64,{qr_b64}",
        "expires_in": int(expires_in),
    }


class LanServer:

    FORBIDDEN_METHODS = frozenset({
        "window_minimize", "window_toggle_maximize", "window_native_maximize",
        "window_close", "window_is_maximized", "window_begin_drag",
        "window_begin_resize", "window_confirm_close", "set_titlebar_theme",
        "select_workspace_folder", "open_folder_in_explorer", "open_path",
        "preview_office_doc",
        "pick_files", "report_dropped_files", "save_clipboard_image",
        "download_update", "cancel_download_update", "launch_installer", "check_update",
        # 电脑操控权限扩张（Task 15，设计 18：LAN 只放行 status/stop）
        "set_browser_control_enabled", "set_computer_control_enabled",
        "set_computer_browser", "pause_computer_control",
        "resume_computer_control", "acknowledge_computer_safety",
        "drain_js_queue",
        "ide_write_file", "ide_set_open_files", "ide_save_layout",
        "ide_run_file",
        "pty_terminal_write", "pty_terminal_kill",
        "lsp_set_server_path",
        "get_provider_api_key", "get_feishu_secret",
        "save_hook", "grant_hook_trust",
        "add_mcp_server",
        # 桌面持久化布局（ui_layout.json）：与已封的 ide_save_layout 同类同边界
        # （覆盖桌面下次启动的侧栏排序），此前黑名单存在同类不对称。手机端无此
        # 能力需求（frontend-mobile 不调用它）；只读的 get_ui_layout 保持放行。
        "save_ui_layout",
        "save_session_ui",
        "shutdown",
        "save_automation_task", "set_automation_task_enabled", "run_automation_task_now",
        "toggle_mcp_server",
        "trust_plugin", "toggle_plugin", "run_plugin_settings_action",
        "add_permission_rule",
        "remove_mcp_server", "open_external_url", "get_remote_bind_qr",
        "delete_skill", "delete_plugin",
        "save_pdf_edits",
    })

    MESSAGE_ENTRY_METHODS = frozenset({"send_user_message"})

    MESSAGE_ENTRY_POSITIONAL_LIMIT = 5

    def __init__(self, bridge=None, port=0, bind: str = "0.0.0.0"):
        self.bridge = bridge
        self.port = port
        self.bind = str(bind or "0.0.0.0")
        self.lan_ip = self._detect_lan_ip()
        self._clients = []
        self._clients_lock = threading.Lock()
        # 串行化「判断 + 签发 + 读取」路径（get_bind_info / set_ttl），避免并发
        # force_new 双签交错与 token/expires 不一致组合；锁顺序单向：
        # _token_issue_lock → _clients_lock（_issue_token 内部取后者），无死锁。
        self._token_issue_lock = threading.Lock()
        self._client_locks: Dict[Any, threading.Lock] = {}
        # 每客户端出站队列 + 专用发送线程：broadcast 只 put_nowait（满则丢弃），
        # 网络 IO 全部收敛到发送线程。客户端静默失联（锁屏断网、无 TCP RST）时
        # 只有该客户端自己的发送线程阻塞在 send 上，桌面回合线程与其它客户端
        # 不受影响——此前同步 broadcast 会把回合 worker 卡死到 TCP 重传超时
        # （Windows 上可达 5-15 分钟）。真实注册路径必建队列；测试直连桩
        # （只登记 _clients/_client_locks）回落同步发送，保持既有语义。
        self._outbound_queues: Dict[Any, "queue_mod.Queue"] = {}
        self._outbound_threads: Dict[Any, threading.Thread] = {}
        self._outbound_dropped = 0
        self._token = None
        self._token_expires = 0.0
        self._ttl = 86400
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._replay = EventReplayBuffer(_REPLAY_BUFFER_MAX)
        self._event_seq = 0
        self._replay_buffer = self._replay.items
        self._preview_upstreams: Dict[str, str] = {}

    def _detect_lan_ip(self) -> str:
        try:
            import psutil

            for iface, addrs in psutil.net_if_addrs().items():
                if any(key in iface.lower() for key in ("wlan", "wi-fi", "wireless", "wifi", "无线")):
                    for addr in addrs:
                        if addr.family == socket.AF_INET and not addr.address.startswith(("127.", "169.254.")):
                            return addr.address
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            pass
        return socket.gethostbyname(socket.gethostname())

    def bind_url(self, token: str, path: str = "/") -> str:
        if not path.startswith("/"):
            path = "/" + path
        if not path.endswith("/"):
            path += "/"
        public = self._public_base()
        if public:
            return f"{public}{path}?token={token}"
        return f"http://{self.lan_ip}:{self.port}{path}?token={token}"

    def _public_base(self) -> str:
        try:
            raw = str(config._load().get("remote_public_url") or "").strip()
        except Exception:
            return ""
        return raw.rstrip("/")

    def client_count(self) -> int:
        """当前已接入的 WebSocket 客户端数量。serve 权限策略用它判断有没有人在线。"""
        with self._clients_lock:
            return len(self._clients)

    def _verify_token(self, token) -> bool:
        if not token or not self._token:
            return False
        if self._token_expires is not None and time.time() >= self._token_expires:
            return False
        try:
            return secrets.compare_digest(token.encode(), self._token)
        except Exception:
            return False

    def _detach_clients_locked(self) -> list:
        clients = self._clients
        self._clients = []
        self._client_locks.clear()
        return clients

    @staticmethod
    def _close_rotated(clients: list) -> None:
        """逐个关闭被轮换的连接；close 的网络 IO 放在锁外，避免阻塞注册路径。"""
        for client in clients:
            try:
                client.close(code=4401, reason="token rotated")
            except Exception:
                pass

    def _kick_all_clients(self) -> None:
        with self._clients_lock:
            clients = self._detach_clients_locked()
        for client in clients:
            self._shutdown_outbound(client)
        self._close_rotated(clients)

    def _issue_token(self) -> str:
        new_token = secrets.token_urlsafe(24).encode()
        new_expires = None if self._ttl <= 0 else time.time() + self._ttl
        with self._clients_lock:
            self._token = new_token
            self._token_expires = new_expires
            clients = self._detach_clients_locked()
        self._close_rotated(clients)
        try:
            _save_config_value(config.REMOTE_PAIR_TOKEN_KEY, new_token.decode())
            _save_config_value(config.REMOTE_PAIR_TOKEN_EXPIRES_KEY, new_expires)
        except Exception:
            # 持久化失败不阻断签发：仅影响重启后的固定性，不影响本次可用性
            pass
        return new_token.decode()

    def _authenticate_and_register(self, ws, token) -> bool:
        """校验 token 并原子注册客户端：通过则加入列表，失败返回 False。

        校验与注册在同一把锁内完成，避免与 _issue_token 的轮换竞态：
        旧 token 校验通过的瞬间不会被并发重签钻空子。
        """
        registered, _ = self._register_and_collect_replay(ws, token, None)
        return registered

    def _register_and_collect_replay(self, ws, token, last_seq):
        with self._clients_lock:
            if not self._verify_token(token):
                return False, []
            self._clients.append(ws)
            self._client_locks[ws] = threading.Lock()
            outbound: queue_mod.Queue = queue_mod.Queue(maxsize=_OUTBOUND_QUEUE_MAX)
            self._outbound_queues[ws] = outbound
            sender = threading.Thread(
                target=self._outbound_sender_loop,
                args=(ws, outbound),
                name=f"lan-outbound-{id(ws):x}",
                daemon=True,
            )
            self._outbound_threads[ws] = sender
            sender.start()
            if last_seq is None:
                return True, []
            replay = [
                payload for seq, payload in self._replay_buffer
                if seq > last_seq
            ]
            return True, replay

    def _send_control(self, ws, event_type: str, data: dict) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        self._safe_send(ws, payload)

    def _admission_plan(self, last_boot_id, last_seq):
        with self._clients_lock:
            if not last_boot_id or last_boot_id != PROCESS_BOOT_ID:
                return "reset", "boot", []
            if not self._replay_buffer:
                return "reset", "gap", []
            oldest = self._replay_buffer[0][0]
            if last_seq is None or oldest > int(last_seq) + 1:
                return "reset", "gap", []
            items = [
                payload for seq, payload in self._replay_buffer
                if seq > int(last_seq)
            ]
            return "replay", "", items

    def _outbound_sender_loop(self, ws, outbound: "queue_mod.Queue") -> None:
        while True:
            item = outbound.get()
            try:
                if item is None:
                    return
                self._safe_send(ws, item)
            finally:
                outbound.task_done()

    def _shutdown_outbound(self, ws) -> None:
        """注销某客户端的出站队列并通知发送线程退出（幂等、非阻塞）。"""
        outbound = self._outbound_queues.pop(ws, None)
        self._outbound_threads.pop(ws, None)
        if outbound is not None:
            try:
                outbound.put_nowait(None)
            except queue_mod.Full:
                pass

    def wait_outbound_drain(self, timeout: float = 5.0) -> bool:
        """等待全部出站队列排空（测试用；生产路径无需等待）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._clients_lock:
                queues = list(self._outbound_queues.values())
            if all(q.unfinished_tasks == 0 for q in queues):
                return True
            time.sleep(0.01)
        return False

    def _send_replay(self, ws, replay_items) -> None:
        with self._clients_lock:
            lock = self._client_locks.get(ws)
        if lock is None or not replay_items:
            return
        with lock:
            for payload in replay_items:
                try:
                    ws.send(payload)
                except Exception:
                    return

    def _load_persisted_token(self, token: str, expires_at) -> None:
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
        try:
            data = config._load()
            self._load_persisted_token(
                data.get(config.REMOTE_PAIR_TOKEN_KEY),
                data.get(config.REMOTE_PAIR_TOKEN_EXPIRES_KEY),
            )
        except Exception:
            pass

    def get_bind_info(self, force_new: bool = False, path: str = "/") -> Dict:
        with self._token_issue_lock:
            if force_new or not self._token or (
                self._token_expires is not None and time.time() >= self._token_expires
            ):
                self._issue_token()
            token = self._token.decode()
            expires = self._token_expires
        url = self.bind_url(token, path=path)
        remaining = 0 if expires is None else max(0, int(expires - time.time()))
        return build_pair_payload(url, token, remaining)

    def set_ttl(self, seconds: int) -> None:
        """更新配对 token 的有效期（秒）；<= 0 表示永久有效。"""
        with self._token_issue_lock:
            self._ttl = int(seconds)

    def _safe_send(self, ws, payload: str) -> bool:
        """线程安全地向单个客户端发送 payload。

        未注册（无发送锁）意味着已注销/被轮换摘除：直接放弃（fail-closed），
        不再无锁直发，避免对已摘除连接并发 send 的竞态窗口。
        """
        with self._clients_lock:
            lock = self._client_locks.get(ws)
        if lock is None:
            return False
        with lock:
            try:
                ws.send(payload)
                return True
            except Exception:
                return False

    def _stamp_and_buffer_locked(self, payload: str) -> str:
        """白名单事件打序号并进补发缓冲；必须在 _clients_lock 内调用。"""
        stamped = self._replay.stamp(payload)
        self._event_seq = self._replay.seq
        return stamped

    def broadcast(self, payload: str, event_type: str = "") -> None:
        if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "server":
            payload = self._rewrite_preview_url(payload, event_type)
            payload = inline_local_images_in_payload(payload)
            self._note_serve_activity()
        with self._clients_lock:
            clients = list(self._clients)
            if event_type in REPLAYABLE_EVENT_TYPES:
                payload = self._stamp_and_buffer_locked(payload)
        for client in clients:
            outbound = self._outbound_queues.get(client)
            if outbound is None:
                self._safe_send(client, payload)
                continue
            try:
                outbound.put_nowait(payload)
            except queue_mod.Full:
                self._outbound_dropped += 1
                if self._outbound_dropped % _OUTBOUND_DROP_WARN_EVERY == 1:
                    logger.warning(
                        "LAN 出站队列已满，丢弃事件（累计 %d 条）；"
                        "客户端可能已失联，断线重连后经补发缓冲找回白名单事件",
                        self._outbound_dropped,
                    )

    def _handle_ws(self, ws) -> None:
        try:
            raw = ws.recv(timeout=10)
        except Exception:
            raw = None
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else None
        except Exception:
            msg = None
        last_seq = None
        last_boot_id = None
        if isinstance(msg, dict) and msg.get("last_seq") is not None:
            try:
                last_seq = int(msg["last_seq"])
            except (TypeError, ValueError):
                last_seq = None
        if isinstance(msg, dict) and isinstance(msg.get("last_boot_id"), str):
            last_boot_id = msg.get("last_boot_id").strip() or None
        token = msg.get("token") if isinstance(msg, dict) else None
        registered, _replay_ignored = self._register_and_collect_replay(
            ws, token, None
        )
        if not registered:
            try:
                ws.close(code=4401, reason="invalid token")
            except Exception:
                pass
            return
        self._send_control(ws, "hello", {"boot_id": PROCESS_BOOT_ID})
        kind, reason, replay_items = self._admission_plan(last_boot_id, last_seq)
        if kind == "reset":
            self._send_control(ws, "seq_reset", {
                "boot_id": PROCESS_BOOT_ID,
                "reason": reason,
                "resync_required": True,
            })
        elif replay_items:
            self._send_replay(ws, replay_items)
        try:
            for raw in ws:
                self._route_message(ws, raw)
        finally:
            with self._clients_lock:
                try:
                    self._clients.remove(ws)
                except ValueError:
                    pass
                self._client_locks.pop(ws, None)
            self._shutdown_outbound(ws)

    def _route_message(self, ws, raw: str) -> None:
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
            self._safe_send(ws, json.dumps({"id": msg_id, "error": "method not allowed"}))
            return
        self._safe_send(ws, inline_local_images_in_payload(json.dumps({"id": msg_id, "result": result})))

    def dispatch_rpc(self, method, args, turn_source: str | None = None) -> Dict[str, Any]:
        if not isinstance(method, str):
            return {"error": {"code": "method_not_allowed"}}
        if method in self.FORBIDDEN_METHODS:
            return {"error": {"code": "method_not_allowed"}}
        if method.startswith("_"):
            return {"error": {"code": "method_not_allowed"}}
        if method == "set_permission_mode" and not self._lan_session_arg(args):
            return {"error": {"code": "method_not_allowed"}}
        extra_kwargs = None
        if method in self.MESSAGE_ENTRY_METHODS:
            source = turn_source if turn_source in ("lan", "server") else self._connection_turn_source()
            normalized = self._force_lan_turn_source(args, turn_source=source)
            if normalized is None:
                return {"ok": False, "msg": "消息参数无法规范化，已拒绝"}
            args, extra_kwargs = normalized
        return self._call_bridge(method, args, extra_kwargs)

    @staticmethod
    def _lan_session_arg(args) -> str:
        """从 LAN 参数里取 session_name（缺省/空/类型不符一律返回空串）。

        位置通道取第 2 个位置参数（``set_permission_mode(mode, session_name)``），
        kwargs 通道取同名键——两种形态都要按 strip 后的非空串判定，否则
        ``{"session_name": "  "}`` 这类空白串会绕过守卫落到全局分支。
        """
        if isinstance(args, dict):
            value = args.get("session_name", "")
        elif isinstance(args, (list, tuple)):
            value = args[1] if len(args) > 1 else ""
        else:
            value = ""
        return str(value or "").strip()

    def _note_serve_activity(self) -> None:
        try:
            from core.serve.metrics import note_activity

            note_activity()
        except Exception:
            return

    def _connection_turn_source(self) -> str:
        if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "server":
            return "server"
        return "lan"

    def _force_lan_turn_source(self, args, turn_source: str = "lan"):
        if isinstance(args, dict):
            merged = {
                key: args[key]
                for key in ("text", "session_name", "attachments", "workdir", "model_id")
                if key in args
            }
            extra_keys = [
                k
                for k in args
                if k not in ("text", "session_name", "attachments", "workdir", "model_id")
            ]
            if extra_keys:
                logger.debug("LAN 消息入口剥离非法键: %s", extra_keys)
            merged["turn_source"] = turn_source if turn_source in ("lan", "server") else "lan"
            return merged, None
        if isinstance(args, list):
            items = list(args[: self.MESSAGE_ENTRY_POSITIONAL_LIMIT])
            if items and isinstance(items[0], dict):
                payload = dict(items[0])
                payload.pop("turn_source", None)
                payload.pop("permission_mode", None)
                items[0] = payload
            source = turn_source if turn_source in ("lan", "server") else "lan"
            return items, {"turn_source": source}
        return None

    def _call_bridge(self, method, args, extra_kwargs=None) -> Dict[str, Any]:
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
            logger.exception("LAN 桥方法 %s 执行失败", method)
            return {"ok": False, "msg": f"内部方法执行失败（{type(e).__name__}），详见桌面端日志"}

    def _bearer_token(self, request) -> str:
        header = ""
        try:
            header = request.headers.get("Authorization") or ""
        except Exception:
            header = ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return ""

    def _worker_token_matches(self, presented: str) -> bool:
        if not presented or not self._token:
            return False
        try:
            return secrets.compare_digest(presented.encode(), self._token)
        except Exception:
            return False

    def _serve_metrics(self, request):
        if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() != "server":
            return self._http_response(404, "not found", "text/plain; charset=utf-8")
        if not self._worker_token_matches(self._bearer_token(request)):
            return self._http_response(401, "unauthorized", "text/plain; charset=utf-8")
        from core.serve.metrics import collect_metrics

        body = json.dumps(collect_metrics(), ensure_ascii=False)
        return self._http_response(200, body, "application/json; charset=utf-8")

    def _rewrite_preview_url(self, payload: str, event_type: str) -> str:
        if event_type != "computer_observation":
            return payload
        try:
            parsed = json.loads(payload)
        except Exception:
            return payload
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict):
            return payload
        internal = str(data.get("preview_url") or "")
        marker = "/computer-preview/"
        index = internal.find(marker)
        if index < 0 or not internal.startswith("http://127.0.0.1"):
            return payload
        public_path = internal[index:]
        self._preview_upstreams[public_path] = internal
        data["preview_url"] = public_path
        parsed["data"] = data
        return json.dumps(parsed, ensure_ascii=False)

    def _serve_computer_preview(self, path: str, request):
        if not self._worker_token_matches(self._bearer_token(request)):
            return self._http_response(401, "unauthorized", "text/plain; charset=utf-8")
        upstream = self._preview_upstreams.get(path)
        if not upstream or not upstream.startswith("http://127.0.0.1"):
            return self._http_response(404, "not found", "text/plain; charset=utf-8")
        try:
            import urllib.request

            with urllib.request.urlopen(upstream, timeout=3) as response:
                body = response.read()
                content_type = response.headers.get("Content-Type") or "image/png"
        except Exception:
            return self._http_response(404, "not found", "text/plain; charset=utf-8")
        return self._http_response(200, body, content_type)

    def _serve_http(self, connection, request):
        if request.headers.get("Upgrade") == "websocket":
            return None
        path = request.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/mobile-shim.js":
            return self._serve_shim()
        if path in ("/m", "/m/") or path == "/m/index.html":
            return self._serve_mobile_index()
        if path == "/m/mobile-shim.js":
            return self._serve_shim()
        if path.startswith("/m/"):
            return self._serve_mobile_static(path)
        if path == "/theme":
            return self._serve_theme()
        if path == "/health":
            body = json.dumps(
                {"ok": True, "boot_id": PROCESS_BOOT_ID},
                ensure_ascii=False,
            )
            return self._http_response(200, body, "application/json; charset=utf-8")
        if path == "/metrics":
            return self._serve_metrics(request)
        if path.startswith("/computer-preview/"):
            return self._serve_computer_preview(path, request)
        return self._serve_static(path)

    @staticmethod
    def _dist_dir() -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "frontend",
            "dist",
        )

    def _http_response(
        self, status_code: int, body, content_type: str,
        cache_control: Optional[str] = None,
    ) -> Response:
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = Headers()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        if cache_control:
            headers["Cache-Control"] = cache_control
        return Response(status_code, HTTPStatus(status_code).phrase, headers, body)

    def _http_404(self) -> Response:
        return self._http_response(404, "not found", "text/plain; charset=utf-8")

    def _serve_index(self) -> Response:
        index_path = os.path.join(self._dist_dir(), "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self._http_404()
        html = html.replace(
            "</head>", '<script src="/mobile-shim.js"></script></head>', 1
        )
        return self._http_response(
            200, html, "text/html; charset=utf-8", cache_control="no-store"
        )

    def _serve_shim(self) -> Response:
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
        # shim 路径固定且会被独立升级：禁缓存，防止手机端拿到旧版协议
        return self._http_response(
            200, data, "application/javascript; charset=utf-8",
            cache_control="no-store",
        )

    def _serve_theme(self) -> Response:
        return self._http_response(
            200, json.dumps({"ok": True, "theme": {}}), "application/json; charset=utf-8"
        )

    def _serve_static(self, path: str) -> Response:
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

    @staticmethod
    def _mobile_dist_dir() -> str:
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "frontend-mobile",
            "dist",
        )

    def _serve_mobile_index(self) -> Response:
        index_path = os.path.join(self._mobile_dist_dir(), "index.html")
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self._http_404()
        html = html.replace(
            "</head>", '<script src="/m/mobile-shim.js"></script></head>', 1
        )
        return self._http_response(
            200, html, "text/html; charset=utf-8", cache_control="no-store"
        )

    def _serve_mobile_static(self, path: str) -> Response:
        """读 frontend-mobile/dist 下的静态文件，带路径穿越防护。

        SPA history 回退：vue-router createWebHistory('/m/') 的深链
        （如 /m/convo/xxx）刷新时按静态文件查找会落空——末段不含扩展名
        的缺失路径一律回退到移动端 index.html；带扩展名的资源缺失照常 404。
        """
        dist = os.path.normpath(self._mobile_dist_dir())
        rel = path[len("/m/"):] if path.startswith("/m/") else path.lstrip("/")
        full = os.path.normpath(os.path.join(dist, rel))
        if full != dist and not full.startswith(dist + os.sep):
            return self._http_404()
        if not os.path.isfile(full):
            last_seg = rel.rstrip("/").split("/")[-1] if rel.rstrip("/") else ""
            if "." not in last_seg:
                return self._serve_mobile_index()
            return self._http_404()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._http_404()
        return self._http_response(200, data, ctype)

    def start(self, *, strict_port: bool = False) -> Dict:
        self._load_token_from_config()
        # 端口同样固定：优先复用上次持久化的端口，避免每次启动换端口导致
        # 手机端需重新扫码；端口被占用时回退内核动态分配并回填持久化
        preferred = self.port or self._load_persisted_port()
        try:
            self._server = self._serve(preferred)
        except OSError:
            if strict_port:
                raise
            # 持久化端口被其他进程占用：回退动态分配，下方回填新的持久化端口
            self._server = self._serve(0)
        self.port = self._server.socket.getsockname()[1]
        self._persist_port()
        self._thread = threading.Thread(
            target=self._serve_forever_quiet,
            args=(self._server,),
            daemon=True,
            name="lan-remote-server",
        )
        self._thread.start()
        return {"ok": True, "host": self.bind, "port": self.port, "lan_ip": self.lan_ip}

    def _serve(self, port: int):
        return serve(
            self._handle_ws,
            host=self.bind,
            port=port,
            process_request=self._serve_http,
            max_size=70 * 1024 * 1024,
        )

    @staticmethod
    def _serve_forever_quiet(server) -> None:
        try:
            server.serve_forever()
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10038 or getattr(exc, "errno", None) == 10038:
                return
            raise

    def _load_persisted_port(self) -> int:
        try:
            port = int(config._load().get(config.REMOTE_PAIR_PORT_KEY) or 0)
        except Exception:
            return 0
        return port if 1024 <= port <= 65535 else 0

    def _persist_port(self) -> None:
        try:
            _save_config_value(config.REMOTE_PAIR_PORT_KEY, self.port)
        except Exception:
            pass

    def stop(self) -> None:
        """关闭服务（关闭监听 socket 让 serve_forever 退出）。"""
        for ws in list(self._outbound_queues):
            self._shutdown_outbound(ws)
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
