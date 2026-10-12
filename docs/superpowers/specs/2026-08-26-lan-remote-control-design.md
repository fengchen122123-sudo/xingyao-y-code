# 局域网手机远程控制 Y-code — 设计文档

日期：2026-08-26
状态：待用户审阅
范围：完整远程控制（聊天 / 权限 / 文件 / 命令同步）+ 会话共享 + 主题一致

---

## 1. 背景与目标

用户希望在局域网内，用手机扫码打开一个 Web 页面，作为 Y-code 的「第二个前端」：

- 手机能看到电脑端的对话记录，并能发消息；
- 电脑端实时看到手机发来的消息，两端对话完全同步；
- 手机端拥有「完整远程控制」能力：聊天、权限审批、文件查看/编辑、命令执行的结果都能在手机上看到和操作；
- 手机端与电脑端共享同一份会话（手机新建会话 = 电脑侧创建）；
- 手机端主题与电脑端保持一致。

## 2. 可行性结论

**完全可行。** Y-code 的架构对此天然友好：

1. GUI 是 `pywebview` + Vue3/TS，通过 `gui_bridge.py` 与 Python 引擎通信；对话逻辑统一走 `core/agent_service.py`，CLI 与 GUI 共用同一套。
2. 对话有完整事件流（`core/turn_events.py`、`reasoning_stream.py`、`thinking_stream.py`），已在实时外推 token / 思考 / 工具卡片，接一条 WebSocket 即可推给手机。
3. 会话历史落在 SQLite（`ycode.db`，harness journal），内存 session 也可读，历史同步不是问题。

**关键理解澄清**：在 Y-code 里「文件查看 / 编辑 / 命令执行」本来就是 Agent 通过工具调用完成的（`read_file` / `edit_file` / `execute_command`），结果以「工具卡片」回显。因此手机端「完整远程控制」的实质是：

> 手机发指令 → Agent 在电脑上干活 → 工具执行过程与结果实时同步回手机 + 权限确认可在手机点同意/拒绝。

手机端**不需要**自己实现文件编辑器或终端（桌面 GUI 也没有）。

## 3. 需求清单（已与用户确认）

| 编号 | 需求 | 决策 |
|---|---|---|
| R1 | 控制范围 | 完整远程控制（聊天 + 权限 + 文件 + 命令同步） |
| R2 | 安全模型 | 扫码绑定 + 配对 token；手机操作直接沿用电脑当前权限模式 |
| R3 | 会话模型 | 手机与电脑共享同一会话池；手机新建会话 = 电脑侧创建 |
| R4 | token 过期时间 | 在「系统设置」里新增可配置项 |
| R5 | 入口 | 侧栏底部「设置」按钮右侧新增手机图标，点击弹出绑定二维码 |
| R6 | 主题 | 手机端主题与电脑端一致 |
| R7 | 范围 | 第一期 + 第二期合并实现（含会话切换/新建、token 持久绑定、断线重连、多手机） |

## 4. 总体架构

```
手机浏览器（移动端 Web 页，复用主题 tokens + 核心渲染组件）
      │  WebSocket（双向）+ HTTP（静态页 / API）
      ▼
core/lan_server.py  ← 内置于 Y-code 进程，后台线程运行
      │  复用 gui_bridge 的「提交消息 + 事件流」同一条链路
      ▼
引擎 + SQLite journal + permission_broker（桌面 / 手机共用同一份）
```

手机是「第二个客户端」，与桌面 GUI 指向同一份会话池、同一份引擎、同一份权限系统。

## 5. 核心组件设计

### 5.1 网络层 `core/lan_server.py`

- 起一个本地 HTTP 服务（发移动端静态页 + API）+ WebSocket（双向消息通道）。
- 监听 `0.0.0.0:<port>`（LAN 可达）；端口可固定或动态分配后回传。
- 后台线程运行，绝不阻塞 GUI 主线程 / 引擎主循环。
- HTTP 职责：服务移动端页面、`GET /theme`（当前主题 tokens）、`GET /health` 等。
- WebSocket 职责：手机 → 电脑（发消息、权限选择、切换/新建会话）、电脑 → 手机（流式事件、历史快照、权限请求、主题变更广播）。

### 5.2 消息 / 事件广播（多客户端化）

这是最关键的一处改动：当前桌面 GUI 是单一 webview，事件推送是单订阅者。改造方案：

- 定义统一的「事件总线」：`gui_bridge` 现有的 `_emit_*` 系列发射点，除了推给 pywebview 前端，同时广播给所有已连接的 WebSocket 客户端。
- 手机发消息 → lan_server → 复用 `gui_bridge.send_user_message()`（同一入口，含排队、会话落库、模型绑定、附件等既有逻辑）→ turn 事件经总线广播回手机。
- 事件协议采用 JSON 消息，shape 尽量复用 pywebview 现有事件结构，减少两端重复解析。

### 5.3 会话共享

- 复用现有 session 管理（`core/memory.py` 的 MemoryManager + SQLite journal）。
- 手机端与桌面端看到同一 `active_session`；手机切会话/新建会话调用与 GUI 相同的 bridge 方法。
- 手机新建会话 = 在电脑侧同一 store 创建（`send_user_message` 的 pending 会话机制天然支持）。

### 5.4 权限桥接

- 手机发起的写操作沿用电脑当前权限模式（`full` 全放行、`confirm` 弹审批）。
- 权限请求经事件总线广播给手机；手机通过 `gui_bridge.respond_permission` / `respond_permission_choice` 回传同意/拒绝/始终允许。
- 需处理「桌面与手机同时出现确认弹窗」的去重：任意一端先回应即生效，另一端弹窗标记为已处理。

### 5.5 主题同步

- GUI 主题是 token 制（`core/gui_theme.py`，`builtin.dark` / `builtin.light` / 自定义），前端 `frontend/src/themes/runtime.ts` 把它解析成 CSS 变量（`--accent`、`--border`、`--bg2` 等）。
- 手机页面加载时通过 `GET /theme` 拉取当前 active 主题的解析结果，应用同一套 CSS 变量；电脑端切换主题时经 WebSocket 广播「主题变更」，手机实时跟随。
- 目标：手机端与电脑端视觉一致（配色、字体、圆角、明暗模式）。

### 5.6 二维码 / 配对 / token

- 探测 LAN IP（排除 loopback、虚拟网卡），生成 `http://<LAN-IP>:<port>/?token=<配对token>`。
- 配对 token：一次性、可过期（过期时长见 R4）、可重置；`<port>` 后端维护有效 token 集合。
- 二维码生成：优先用现有前端二维码能力或新增轻量库（实现时择一，见 §7）。
- token 持久绑定（R7）：绑定后可签发长期会话凭证，断线重连免重复扫码（受 token 过期时长约束）。

## 6. 前端改动

| 文件 | 改动 |
|---|---|
| `frontend/src/components/Sidebar.vue` | `side-footer` 的「设置」按钮右侧新增手机图标按钮；点击调 bridge `get_remote_bind_qr()` 弹出二维码 modal |
| `frontend/src/components/SystemSettings.vue` | 「系统设置」新增一行「远程控制 token 过期时间」配置（下拉：永久 / 1 小时 / 12 小时 / 24 小时 / 7 天） |
| `frontend/src/stores/appStore.ts` | `systemSettings` 快照新增 `remote_token_ttl` 字段及 setter |
| `frontend/mobile/`（新增） | 移动端页面：消息流、输入框、流式 token、思考折叠、工具卡片、diff、权限卡片、会话列表/新建；复用主题 tokens 与核心渲染组件 |
| `gui_bridge.py` | 新增 `get_remote_bind_qr` / `set_remote_token_ttl` / 系统设置扩展；事件发射点接入多客户端总线 |

### 6.1 移动端 transport 抽象

桌面前端依赖 `window.pywebview.api`（pywebview JS 桥），手机上不存在。设计选择：

- **推荐**：复用同一套 Vue 前端与组件（`MessageBubble`、`ToolExecutionRow`、`PermCard` 等），抽象出 transport 层（`pywebview bridge` 与 `WebSocket` 两种实现）；移动端用 WebSocket 实现，共享渲染组件与主题 tokens。
- 移动端页面由 `lan_server` 服务，作为独立的移动入口/精简布局。

## 7. 关键技术风险与待确认点

1. **多客户端广播**：事件总线从单订阅者改为多订阅者，需确认 `gui_bridge` 的发射点数量与形态，评估改造面。
2. **WebSocket 依赖**：`requirements.txt` 无独立 ws 库，需新增 `websockets`/`aiohttp`（或复用 pywebview 已带依赖，实现时确认）。
3. **线程模型**：lan_server 后台线程与引擎主循环、GUI 主线程之间的并发/锁（尤其 `_turn_start_lock`、`_turn_workdir_lock`）。
4. **transport 抽象**：移动端复用 Vue 组件的改造量，需评估是「抽象 transport」还是「独立精简移动端」。
5. **二维码库**：动态二维码生成，择 Python `qrcode` 或前端 JS 二维码方案。
6. **权限弹窗去重**：桌面/手机双端同时弹窗时的先答生效、后答失效处理。

## 8. 实施分期（第一期 + 第二期合并）

**阶段 A（MVP，覆盖 R1/R2/R5/R6 主体）**
- `core/lan_server.py`：HTTP + WebSocket + LAN IP 探测
- 二维码配对 + token（含 R4 过期配置 + R5 手机图标入口）
- 手机看历史 / 发消息 + 流式同步 + 工具结果回显 + 权限确认
- 主题同步（R6）

**阶段 B（会话/稳定性，覆盖 R3/R7）**
- 会话切换 / 新建（共享会话池）
- token 持久绑定、断线重连、多手机并发
- 权限弹窗去重、事件总线多客户端化收尾

## 9. 测试与验收

- 单机 + 真机扫码联通；两端发消息实时互见。
- 权限模式 `confirm` 下手机端能收到并处理权限请求；`full` 下直接放行。
- 手机新建/切换会话，桌面侧同步可见。
- 手机端主题与电脑端一致，切换主题实时跟随。
- token 过期配置生效；过期后二维码失效，持久绑定凭证按策略过期。
- 断线重连、多手机并发无事件错乱。

## 10. 非目标（Out of Scope）

- 内网穿透 / 异地远程访问（超出「局域网」范畴，安全面大，暂不做）。
- 手机端独立实现文件编辑器或终端。
