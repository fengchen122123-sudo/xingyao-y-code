# 局域网手机远程控制 Y-code 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让手机通过局域网扫码打开 Web 页面，作为 Y-code 的第二个前端，与桌面 GUI 共享同一会话、引擎、权限与主题，实现完整远程控制。

**Architecture:** 在 Y-code 进程内新增 `core/lan_server.py`（HTTP + WebSocket 单端口服务）。关键决策：复用现有前端构建产物 `frontend/dist`，通过注入 `mobile-shim.js` 用 WebSocket 实现 `window.pywebview.api`（RPC 代理）与 `window.onBridgeEvent`（事件接收），使手机与桌面共用同一套 Vue 界面、主题 tokens 和 markdown 渲染，仅需补充响应式布局。事件广播在 `gui_bridge._emit_raw` 接入多客户端总线。

**Tech Stack:** Python（`websockets>=12.0` sync API）、现有 Vue3 + TS + Vite 前端、`qrcode>=7.4`、SQLite journal。

## Global Constraints

- 新增运行时依赖仅限：`websockets>=12.0`、`qrcode>=7.4`；不得引入其它。
- 事件沿用现有 `{"type": event_type, "data": data}` JSON 结构（`gui_bridge._emit_raw` 序列化格式）。
- 手机端只暴露 `LanServer.ALLOWED_METHODS` 白名单内的 bridge 方法，绝不暴露 `window_close`/`window_minimize`/拖拽等桌面专属方法。
- 手机操作沿用电脑当前权限模式（`full`/`confirm`/`plan`/`accept_edits`）。
- 后端 snake_case、前端 camelCase；复用现有组件不新造轮子。
- 监听 `0.0.0.0`，端口动态分配，token 由后端生成。
- 测试用 `pytest`；前端改动同步补 `.test.ts`。

---

## 文件结构映射

| 文件 | 责任 | 新增/修改 |
|---|---|---|
| `core/lan_server.py` | HTTP 静态服务 + WebSocket RPC + IP 探测 + 配对 token + 二维码 | 新增 |
| `gui_bridge.py` | 事件广播接入总线、新增 `get_remote_bind_qr`/`set_remote_token_ttl`、方法白名单 | 修改 |
| `config.py` | `remote_token_ttl` 持久化 | 修改 |
| `frontend/mobile-shim.js` | 移动端 transport shim | 新增 |
| `frontend/src/components/Sidebar.vue` | 手机图标 + 二维码弹窗 | 修改 |
| `frontend/src/components/SystemSettings.vue` | token 过期时间配置行 | 修改 |
| `frontend/src/stores/appStore.ts` | `systemSettings.remote_token_ttl` + setter | 修改 |
| `frontend/src/style.css` | 移动端响应式布局 | 修改 |
| `requirements.txt` | 新增 `websockets`、`qrcode` | 修改 |

---

## Task 1: 依赖与 lan_server 骨架

**Files:**
- Modify: `requirements.txt`
- Create: `core/lan_server.py`
- Test: `tests/test_lan_server.py`

**Interfaces（Produces）:**
- `LanServer(bridge=None, port=0)` 类
- `_detect_lan_ip() -> str`：UDP 连 `8.8.8.8:80` 取本机出口 IP，失败回退 `gethostbyname(gethostname())`
- `bind_url(token: str) -> str`：返回 `http://{lan_ip}:{port}/?token={token}`
- `broadcast(payload: str) -> None`：向所有已连接客户端 `send(payload)`，异常吞掉
- `start() -> Dict`：占位返回 `{"ok": True, "host": "0.0.0.0", "port": port, "lan_ip": lan_ip}`（Task 2 补真实服务）
- `stop() -> None`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_lan_server.py
from core.lan_server import LanServer

def test_detect_lan_ip_non_loopback():
    ip = LanServer(bridge=None)._detect_lan_ip()
    assert ip and ip != "127.0.0.1"

def test_bind_url_embeds_token():
    srv = LanServer(bridge=None); srv.port = 8765; srv.lan_ip = "192.168.1.5"
    assert srv.bind_url("abc123") == "http://192.168.1.5:8765/?token=abc123"
```

- [ ] **Step 2: 跑测试确认失败**（`LanServer` 未定义）
  Run: `pytest tests/test_lan_server.py -v`

- [ ] **Step 3: 写最小实现**（字段 + IP 探测 + bind_url + broadcast 占位，`start/stop` 占位）

- [ ] **Step 4: 跑测试确认通过**

- [ ] **Step 5: 提交**
  ```bash
  git add requirements.txt core/lan_server.py tests/test_lan_server.py
  git commit -m "feat: add lan_server skeleton with ip detection and token url"
  ```

---

## Task 2: WebSocket + HTTP 服务与事件广播接入

**Files:**
- Modify: `core/lan_server.py`
- Modify: `gui_bridge.py`（`_emit_raw` 接入广播）
- Test: `tests/test_lan_server.py`

**Interfaces:**
- Consumes: `LanServer`（Task 1）
- Produces:
  - `LanServer.start()`：用 `websockets.sync.server.serve` 启动，`process_request` 参数接 `_serve_http`；真实端口回填 `self.port`。
  - `_verify_token(token) -> bool`、`_issue_token() -> str`（`secrets.token_urlsafe(24)`，过期 `time.time()+ttl`）
  - `_handle_ws(ws)`：首消息校验 token → 加入 `_clients` → 循环 `_route_message` → finally 移除
  - `_route_message(ws, raw)`：解析 `{"method","args","id"}` → 白名单校验 → `_call_bridge` → 回 `{"id","result"}`
  - `_call_bridge(method, args)`：`getattr(self.bridge, method)(**args)`，异常吞掉回 `{"ok": False}`
  - `_serve_http(request)`：路由 `/`→index、`/mobile-shim.js`、`/theme`、`/health`、其它→静态
  - `gui_bridge._emit_raw` 末尾：`if self._lan_server: self._lan_server.broadcast(payload)`（payload 已序列化，见 gui_bridge.py:1693-1708）

**实现要点：**
- `ALLOWED_METHODS` 白名单初版：`send_user_message`、`respond_permission`、`respond_permission_choice`、`get_history`、`get_session_groups`、`switch_session`、`new_session`、`cancel_turn`、`get_gui_theme`、`get_active_model`（后续按需增补）。
- `_serve_index`：读 `frontend/dist/index.html`，在 `</head>` 前注入 `<script src="/mobile-shim.js"></script>`，返回修改后 HTML。
- `_serve_static`：用 `mimetypes.guess_type` 定 Content-Type，读 `dist` 下文件；`..` 路径穿越防护（`os.path.normpath` 后校验仍以 `dist` 为前缀）。
- 线程模型：`start()` 在独立 daemon 线程里 `ws_serve(...).serve_forever()`；`websockets.sync.server.serve` 是阻塞同步 API，无需 asyncio。

- [ ] **Step 1: 写失败测试**（token 签发/校验、bind_url、broadcast 契约、白名单拒绝）

```python
import json, time
from core.lan_server import LanServer

def test_issue_and_verify_token():
    srv = LanServer(bridge=None); srv._ttl = 1
    t = srv._issue_token()
    assert srv._verify_token(t)
    srv._token_expires = time.time() - 1
    assert not srv._verify_token(t)

def test_broadcast_sends_to_clients():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._clients = [fake]
    srv.broadcast('{"type":"token_delta"}')
    assert fake.sent == ['{"type":"token_delta"}']

def test_route_rejects_non_whitelisted():
    srv = LanServer(bridge=None)
    fake = type("W", (), {"sent": [], "send": lambda self, p: self.sent.append(p)})()
    srv._route_message(fake, json.dumps({"method": "window_close", "args": {}, "id": 1}))
    assert "method not allowed" in fake.sent[0]
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（按上述 Interfaces/要点补全）
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**
  ```bash
  git add core/lan_server.py gui_bridge.py tests/test_lan_server.py
  git commit -m "feat: lan websocket rpc + event broadcast into gui_bridge"
  ```

---

## Task 3: 配对 token 生命周期 + 二维码 + bridge 方法

**Files:**
- Modify: `core/lan_server.py`
- Modify: `gui_bridge.py`（新增 `get_remote_bind_qr`、`set_remote_token_ttl`）
- Modify: `config.py`（`remote_token_ttl` 持久化）
- Test: `tests/test_lan_server.py`

**Interfaces（Produces）:**
- `LanServer.get_bind_info() -> Dict`：签发 token，生成二维码 PNG → base64，返回 `{"ok": True, "url": bind_url, "token": token, "qr": "data:image/png;base64,...", "expires_in": ttl}`
- `LanServer.set_ttl(seconds: int) -> None`：更新 `self._ttl`
- `gui_bridge.get_remote_bind_qr() -> Dict`：`self._ensure_lan_server()` 后返回 `self._lan_server.get_bind_info()`
- `gui_bridge.set_remote_token_ttl(seconds: int) -> Dict`：持久化到 config 并同步 `lan_server.set_ttl`
- `config.py`：`REMOTE_TOKEN_TTL_KEY = "remote_token_ttl"`，默认 `86400`；读写走现有 `save_config_value` / `_load`

**实现要点：**
- 二维码用 `qrcode` 库：`qrcode.make(url)` → `img.save(buf, format="PNG")` → `base64.b64encode(buf.getvalue()).decode()`。
- `gui_bridge.__init__` 初始化 `self._lan_server = None`；`_ensure_lan_server()` 惰性创建 `LanServer(bridge=self)` 并 `start()`（读 config 的 ttl）。
- 启动入口：在 `gui_main.py` 或 `gui_bridge` 生命周期里，GUI ready 后调 `_ensure_lan_server()`（具体挂载点见 Task 8 收尾；本 Task 先提供方法并保证可手动调用）。

- [ ] **Step 1: 写失败测试**

```python
def test_get_bind_info_returns_url_token_qr():
    srv = LanServer(bridge=None); srv.port = 8765; srv.lan_ip = "192.168.1.5"
    info = srv.get_bind_info()
    assert info["ok"] and info["url"].startswith("http://192.168.1.5:8765/?token=")
    assert info["token"] and info["qr"].startswith("data:image/png;base64,")
```

- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**
  ```bash
  git add core/lan_server.py gui_bridge.py config.py tests/test_lan_server.py
  git commit -m "feat: remote pairing token + qr code + bridge methods"
  ```

---

## Task 4: 系统设置 token 过期时间配置（前端）

**Files:**
- Modify: `frontend/src/components/SystemSettings.vue`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `gui_bridge.py`（`get_system_settings` 返回体扩展，或新增独立读取）

**Interfaces:**
- Consumes: `set_remote_token_ttl`（Task 3）
- Produces: `appStore.systemSettings.remote_token_ttl: number`；`SystemSettings.vue` 新增下拉（选项：永久 `0` / 1 小时 `3600` / 12 小时 `43200` / 24 小时 `86400` / 7 天 `604800`）；改动即调 `store.setRemoteTokenTtl(value)`。

**实现要点：**
- 参照 `SystemSettings.vue` 现有 `command_shell` 的 `setting-row` + `setting-select` 结构，新增一行「远程控制 token 过期时间」。
- `appStore.ts` 的 `systemSettings` 快照新增 `remote_token_ttl: 86400`，并新增 action `setRemoteTokenTtl(seconds)` 调 `window.pywebview.api.set_remote_token_ttl(seconds)`。
- `get_system_settings`（或对应初始化）返回体补 `remote_token_ttl`，前端 onMounted 同步。

- [ ] **Step 1: 写失败测试**（`SystemSettings.test.ts`：渲染含 `remote-token-ttl` 下拉，选择后调用 setter）
- [ ] **Step 2: 跑测试确认失败**（`npm test` 或 `npx vitest run SystemSettings`，在 `frontend/` 下）
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**
  ```bash
  git add frontend/src/components/SystemSettings.vue frontend/src/stores/appStore.ts gui_bridge.py
  git commit -m "feat: system setting for remote token ttl"
```

---

## Task 5: 手机图标入口 + 二维码弹窗（Sidebar）

**Files:**
- Modify: `frontend/src/components/Sidebar.vue`
- Test: `frontend/src/components/Sidebar.test.ts`（若存在则更新）

**Interfaces:**
- Consumes: `get_remote_bind_qr`（Task 3，bridge 方法）
- Produces: `side-footer` 内「设置」按钮右侧新增手机图标按钮（`data-test="remote-bind-button"`），点击调用 `window.pywebview.api.get_remote_bind_qr()`，弹出二维码 modal（含二维码 `<img :src="qr">`、URL 文案、关闭按钮）。

**实现要点：**
- 复用 `AppIcon`（新增或复用已有手机图标名，如 `smartphone`，若无则在 `iconPaths.ts` 补）。
- modal 结构参照 `ConfirmModal.vue` 或 `SettingsPage.vue` 的 `modal-mask` 风格。
- 绑定信息状态：`const bindInfo = ref<{url:string;qr:string;expires_in:number}|null>(null)`。

- [ ] **Step 1: 写失败测试**（点击 `remote-bind-button` 后调用 `get_remote_bind_qr` 并渲染二维码 img）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**
  ```bash
  git add frontend/src/components/Sidebar.vue frontend/src/components/iconPaths.ts
  git commit -m "feat: phone icon + qr bind modal in sidebar"
  ```

---

## Task 6: 移动端 transport shim（mobile-shim.js）

**Files:**
- Create: `frontend/mobile-shim.js`
- Test: 手动 + `tests/test_lan_server.py`（验证 shim 被正确注入 index.html）

**Interfaces:**
- Consumes: lan_server 的 WebSocket（Task 2）、dist 静态服务
- Produces: 在 `window.pywebview` 尚未注入时（手机无 pywebview），提供：
  - `window.pywebview = { api: <Proxy> }`：`api.methodName(...args)` 返回 Promise；内部发 `{"method","args","id"}`，按 `id` resolve 对应 `{"id","result"}`。
  - `window.onBridgeEvent` 由前端自行注册；shim 负责收到 `{"type","data"}` 后 `JSON.parse` 成对象并 `window.onBridgeEvent(obj)`。
  - 就绪信号：注入后 `dispatchEvent(new Event('pywebviewready'))`；并保证 `api.get_initial_state` 是 function（满足前端 `pywebviewApiReady()` 检查）。

**实现要点（关键契约，必须精读前端再实现）：**
- 连接地址：`ws(s)://{location.host}/ws`；token 从 `location.search` 的 `?token=` 取，作为 WebSocket 首个消息发送（`{"token": "..."}`）。
- RPC 幂等：维护 `Map<id, {resolve,reject}>`；`id` 自增。
- `get_initial_state` 必须存在且返回与桌面 `gui_bridge.get_initial_state()` 相同结构（shim 直接透传后端返回）。
- 断线重连：`onclose` 指数退避重连（Task 8 细化）；重连后重新发 token。
- **绝不覆盖已存在的 `window.pywebview`**（桌面上 shim 不加载；仅手机页面加载，但加 `if (!window.pywebview)` 防御）。

- [ ] **Step 1: 写失败测试**（`tests/test_lan_server.py`：`_serve_index` 返回 HTML 含 `<script src="/mobile-shim.js"></script>`）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现**（先精读 `frontend/src/App.vue` 的 `pywebviewApiReady/onPywebviewReady/startInit` 与 `appStore.ts:1101` 的 `onBridgeEvent`、`get_initial_state` 返回结构）
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**
  ```bash
  git add frontend/mobile-shim.js core/lan_server.py tests/test_lan_server.py
  git commit -m "feat: mobile transport shim bridging websocket to pywebview api"
  ```

---

## Task 7: 移动端响应式布局

**Files:**
- Modify: `frontend/src/style.css`（或新增 `frontend/src/mobile.css` 并在 `main.ts` 引入）
- Test: 手动（真机/浏览器移动视口）

**Interfaces:** 无新接口；纯样式。

**实现要点：**
- 用 `@media (max-width: 768px)` 或视口检测：手机上默认折叠侧栏、隐藏右面板与窗口控制按钮（`.window-controls`、`#btn-toggle-right` 等桌面专属控件）。
- 顶部栏、输入框、消息气泡适配触屏（增大点击热区、`font-size` 不小于 16px 防 iOS 缩放）。
- 主题 CSS 变量沿用现有 `--accent`/`--border`/`--bg*`，不改主题运行时。

- [ ] **Step 1: 确认现有布局断点与桌面专属控件清单**
- [ ] **Step 2: 实现移动端媒体查询**
- [ ] **Step 3: 手动验证**（手机访问或浏览器 DevTools 移动视口，确认侧栏折叠、控件隐藏、输入可用）
- [ ] **Step 4: 提交**
  ```bash
  git add frontend/src/style.css
  git commit -m "feat: responsive layout for mobile remote control"
  ```

---

## Task 8: 生命周期挂载 + 会话/权限/稳定性收尾

**Files:**
- Modify: `gui_bridge.py` 或 `gui_main.py`（GUI ready 后 `_ensure_lan_server()`）
- Modify: `core/lan_server.py`（断线重连、多客户端、权限去重、token 重置）

**Interfaces:**
- Consumes: Task 2-6
- Produces: 完整可用链路。

**实现要点：**
- **挂载点**：在 GUI 初始化完成（`get_initial_state` 被调用后 / pywebview ready 后）惰性 `_ensure_lan_server()`，确保 `self.bridge` 就绪、`dist` 已构建。
- **断线重连**：手机 shim `onclose` 后指数退避重连；服务端 token 有效期内重连免扫码（持久绑定）。
- **多手机并发**：`_clients` 列表广播天然支持；`send_user_message` 已有多会话排队机制，无需额外锁。
- **权限去重**：`respond_permission` 已幂等（`permission_broker.resolve` 返回 False 表示已处理），双端先答生效、后答 `{"ok":False}`；前端 PermCard 收到已处理状态后置灰即可（若需前端配合则补）。
- **token 重置**：`get_remote_bind_qr` 每次点击重新签发 token（旧 token 失效）；TTL 到期后 `_verify_token` 拒绝。
- **方法白名单扩充**：按需补 `get_file_tree`、`read_file_preview` 等只读方法，写操作类保持需审批。

- [ ] **Step 1: 写失败测试**（token 重置后旧 token 失效；TTL 到期拒绝）
- [ ] **Step 2: 跑测试确认失败**
- [ ] **Step 3: 实现挂载与收尾**
- [ ] **Step 4: 全链路手动验证**（真机扫码 → 看历史 → 发消息 → 电脑端同步 → 权限审批 → 切会话）
- [ ] **Step 5: 提交**
  ```bash
  git add gui_bridge.py core/lan_server.py
  git commit -m "feat: lan remote control lifecycle + stability"
  ```

---

## Self-Review

- **Spec 覆盖**：R1（完整控制）→ Task 2/8；R2（token+沿用权限）→ Task 3/8；R3（会话共享）→ Task 2/8；R4（token 过期配置）→ Task 4；R5（手机图标入口）→ Task 5；R6（主题一致）→ 复用 dist + Task 7；R7（一二期合并）→ Task 8。
- **占位符扫描**：无 TBD/TODO；接口签名在 Interfaces 块明确定义。
- **类型一致性**：`LanServer.broadcast(payload: str)` / `_route_message(ws, raw: str)` / `ALLOWED_METHODS` 在 Task 2 定义并被 Task 3/8 引用，命名一致；`set_remote_token_ttl` / `get_remote_bind_qr` 在 Task 3 定义、Task 4/5 引用，一致。

## Execution Handoff

计划已保存。两种执行方式：**Subagent-Driven（推荐）** 或 **Inline**。本环境按系统 plan 编排工具（`plan_write` + `run_subagent`）执行，逐 task 派发子代理、每 task 提交后评审。
