(function () {
  if (window.pywebview) return; // 桌面环境不注入
  var token = new URLSearchParams(location.search).get('token') || '';
  var wsUrl = (location.protocol === 'https:' ? 'wss' : 'ws') + '://' + location.host + '/';
  var ws = null;
  var seq = 0;
  var pending = new Map(); // id -> {resolve, reject}

  function rpc(method, args) {
    return new Promise(function (resolve, reject) {
      if (!ws || ws.readyState !== 1) { reject(new Error('连接未就绪')); return; }
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
      window.pywebview = { api: new Proxy({}, {
        get: function (_t, prop) {
          if (typeof prop !== 'string') return undefined;
          return function () { return rpc(prop, Array.prototype.slice.call(arguments)); };
        }
      }) };
      window.dispatchEvent(new Event('pywebviewready'));
    };
    ws.onmessage = function (e) {
      var msg;
      try { msg = JSON.parse(e.data); } catch (_) { return; }
      if (msg && typeof msg.id !== 'undefined') {
        var p = pending.get(msg.id);
        if (p) { pending.delete(msg.id); msg.error ? p.reject(new Error(msg.error)) : p.resolve(msg.result); }
      } else if (msg && msg.type && window.onBridgeEvent) {
        window.onBridgeEvent(msg);
      }
    };
    ws.onclose = function () { setTimeout(connect, 2000); };
    ws.onerror = function () {};
  }

  connect();
})();
