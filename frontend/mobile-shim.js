/**
 * mobile-shim.js —— 局域网手机远程控制的 transport shim
 *
 * 桌面上 pywebview 注入 window.pywebview.api（RPC）与 window.onBridgeEvent（事件）。
 * 手机上无 pywebview，本 shim 用 WebSocket 模拟这两者，使手机加载同一个 dist 前端、
 * 复用全部组件与主题。
 *
 * 约定（与 core/lan_server.py 对齐）：
 * - WebSocket 首条消息发送 {"token": "..."} 完成配对校验。新服务端会先回
 *   hello，再按 boot_id 决定 seq_reset 或补发。重连附带 last_boot_id 与 last_seq。
 *   hello / seq_reset 不交给 onBridgeEvent。
 * - 手机 → 电脑 RPC：{"method", "args", "id"}，args 为位置参数数组，回包 {"id", "result"}。
 * - 电脑 → 手机事件：{"type", "data"} 广播（后端 broadcast 发的是 JSON 字符串），
 *   白名单事件额外携带单调递增 seq，shim 记录最后收到的 seq 作为补发水位。
 * - 配对 token：首次从 URL 参数取并写入 localStorage，随后立即用 history.replaceState
 *   清理地址栏 query（保留 path/hash），token 不残留于浏览器历史/截图/分享链接；
 *   之后刷新/重开（URL 无 token）回退读 localStorage，直到服务端 TTL 过期或被
 *   轮换（4401）才失效。
 */
(function () {
  if (window.pywebview) return; // 桌面环境不注入，防御性

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
  var usedToken = token; // 本次连接使用的 token，4401 时据此决定是否清除
  var wsUrl = (window.location.protocol === 'https:' ? 'wss' : 'ws') + '://' + window.location.host + '/';

  var ws = null;
  var seq = 0;
  var lastSeq = 0;        // 最后收到的补发事件 seq（服务器侧单调递增）
  var bootId = '';
  var sawHello = false;
  var legacy = false;
  var resyncing = false;
  var pausedSeq = 0;
  var held = [];
  var seen = {};
  var refetchOnce = false;
  var hadConnection = false; // 是否发生过连接：区分全新页面与断线重连
  var pending = new Map(); // id -> {resolve, reject}

  function isCapacitor() {
    return !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  }

  function watermarkKey() {
    var account = window.__ycAccountId || '';
    var profile = window.__ycProfile || 'lan';
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
    if (isCapacitor() || legacy || !bootId) return;
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
    pending.forEach(function (item) { item.reject(new Error('连接已关闭')); });
    pending.clear();
  };

  function rpc(method, args) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) {
        reject(new Error('连接未就绪'));
        return;
      }
      var id = ++seq;
      pending.set(id, { resolve: resolve, reject: reject });
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
        if (!sawHello && !legacy) {
          legacy = true;
          beginResync();
        }
        acceptFrame(msg);
      }
    };
    ws.onclose = function (event) {
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
      setTimeout(connect, 2000); // 基础断线重连
    };
    ws.onerror = function () {};
  }

  connect();
})();
