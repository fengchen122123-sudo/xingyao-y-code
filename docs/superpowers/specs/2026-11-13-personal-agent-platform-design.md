# 星瑶 Personal Agent 平台架构设计

> 修订 r3（2026-11-13）：在 r2 上闭合第三轮审查仍开着的协议打架和「点了名没给契约」的项。r2 已吸收 M01-M20 的方向；r3 把仍会让两个实施者写出两套行为的地方收成唯一契约。第 10 节是施工覆盖条款，与前文不一致时以第 10 节为准。本文仍是设计，不表示任何实现或运行验收已经完成。

日期：2026-11-13
状态：r3 施工稿；实施与实测未开始
基线：`dcb3bc01`（`dev`）。r3 引用的 file:line 按该提交核对。`audit/core-service.md`、`audit/remote-mobile.md`、`audit/platform-deploy.md` 不在该提交的树里，正文里只把它们当历史线索；实施以本文件和第 10 节为准，不依赖那三份缺失审计。
取代：`2026-11-13-server-multi-tenant-design.md`（对外 SaaS 多租户方向已废弃，仅作素材归档）
视觉参照：`website/personal-agent-prototype.html` 是静态可点原型，不是传输层，不实现协议。

---

## 1. 背景与需求

### 1.1 产品定位

星瑶从「Windows 桌面单机应用」演进为 **Personal Agent 平台**（对标 Today / Muse 的个人助理形态）：产品主人自己在 Linux 服务器上常驻一个 Agent，通过浏览器或安卓 App 随时对话、围观它操作浏览器、远程审批权限；同时保留「我的电脑」入口，从手机反向触达自己 Windows 电脑上正在运行的星瑶 GUI。**这不是对外 SaaS**：账号只给产品主人自己和极少数受邀者使用。

### 1.2 七项需求

| # | 需求 | 一句话定义 |
|---|---|---|
| R1 | Windows 桌面 GUI 零回归 | 现有 GUI 全部行为不变，改造只允许平台守卫 / 新增档位 / 新增入口 |
| R2 | Windows CLI 零回归 | `--cli` / `run_headless` 路径行为不变 |
| R3 | Linux 服务器 Personal Agent + 专属极简 Web 前端 | `--serve` 无窗口常驻 + 新响应式 SPA，Docker + Caddy 部署 |
| R4 | 安卓 App（Muse/Today 风格） | Capacitor 包装同一套 SPA，直连服务器 |
| R5 | App 内「我的电脑」 | 手机 → 服务器 → 用户 Windows 上运行中的星瑶 GUI；桌面持设备 token 主动外连注册，服务器纯转发 |
| R6 | 账号登录 + 对话隔离 | 每账号独立数据 / 会话 / 记忆 / 模型 Key，一账号一容器。受邀者各自填写自己的 Key，不复制主人的 Key |
| R7 | 后台管理系统 | 账号管理、设备管理、worker 状态、用量查看 |

### 1.3 信任模型假设（全设计的前提）

- **账号 = 产品主人自己 / 极少数受邀者**，不是不可信公网用户。账号持有者被信任「不会主动攻击平台」，但仍按「一账号一 Docker 容器」隔离（复用 core-service.md 第三节「同进程多账号必然串号，必须一账号一进程」的审计结论），容器加固做基线：非 root、CPU / 内存 / PID 限额、数据卷隔离、drop capabilities、no-new-privileges。
- **不做**对外 SaaS 级渗透防护（egress 白名单代理、注册风控、计费防刷、容器逃逸对抗性加固等）。理由：威胁模型里没有「恶意注册用户」，这些投入对个人产品是纯成本。
- **将来若转为对外服务，必须补**：① 模型 API egress 白名单代理（否则 execute_command 可外联任意地址，见 remote-mobile.md 3.4「远程回合仍可执行命令与读写文件」）；② 注册风控（邀请码 / 验证码）与配额防刷；③ 容器逃逸对抗加固（gVisor / Kata、seccomp 精细策略、只读根文件系统全面化）；④ 渗透测试与审计日志合规；⑤ 网关自身的高可用与限流。旧文档第 3 节「阶段三：对外运营加固」清单可整体回收复用。
- 网关是唯一公网入口，TLS 由 Caddy / Nginx 反代终结；worker 容器与桌面 GUI 都不直接暴露公网。

---

## 2. 现状审计结论摘要

下面这些 file:line 在 `dcb3bc01` 上打开核对过，与描述一致：frameless.py:101、gui_bridge.py:3809-3831、credentials.py:9、lan_server.py:1150、capability.py:87-103、browser_backend.py:46 与 :1209-1215、tool_registry.py:182、config.py:430-458、linux.py 全文、settings.py:26 与 :37-41、controller.py:517-528、browser_worker.py:37。`audit/` 下的三份现状审计不在这个提交里，不要当成还能打开的附件。

### 2.1 核心服务化（audit/core-service.md）

- 核心对话链无界面依赖，可复用：agent_service（headless 约束，agent_service.py:4-12）、MemoryManager 按路径实例化 + WAL（memory.py:501-510）、LanServer 构造仅需 bridge+port（lan_server.py:379-406）。
- **四处必改**：① frameless 模块级 `ctypes.windll` Linux import 即崩（frameless.py:101-110，gui_bridge 顶层 import 连带）；② `_emit_raw` 无窗口先 return，LAN/飞书广播被掐断（gui_bridge.py:3809-3831）；③ 权限确认单槽队列 + gui flavor 跳过终端回退 → 无窗口无人应答时无限挂起（permission_terminal.py:524-539、gui_bridge.py:7934-7939）；④ 进程级全局单例决定**一账号一进程**（app_context.py:16-24、permission_broker.py:263、llm.py:361-365 等）。
- LAN/MCP/飞书服务靠前端首屏触发（gui_bridge.py:4382-4398），serve 必须显式启动（gui_bridge.py:8106-8220）。
- 数据目录主干可用：`YCODE_DATA_DIR` 在 config.py:430-432 决定 `YCODE_DIR`。残留要改的行（`dcb3bc01` 上打开核对过）：tokenrhythm_auth.py:64 与 :68、workflows/store.py:44-45、workflows/skill_sync.py:17-18、hooks/zcode_import.py:385、harness/tool_adapters.py:154、skills.py:682。skills.py:650-661 只是文档字符串，真正把 `~/.agents/skills` 加进扫描的是 :682。
- 凭据加密 Linux 走 keyring 分支（credentials.py:51-56），headless 容器需 file 后端（部署项）。

### 2.2 远程链路与移动端（audit/remote-mobile.md）

- **桥接协议可复用但需加 epoch**：单端口 HTTP+WS、RPC 帧、20 种白名单事件与 128 条环形缓冲可复用；现有重连只在同一进程且未发生缓冲/队列溢出时保证窗口内补发。r2 增加 boot_id/hello/seq_reset/resync_required，不能沿用无条件“不重不漏”表述。首帧 token 鉴权、失败 close 4401 保留。
- **安全边界收敛在 `dispatch_rpc` 一个函数**（lan_server.py:821-859）：54 项 FORBIDDEN_METHODS（:224-364）、`_` 前缀拒绝、`set_permission_mode` 必须带会话名、`send_user_message` 强制 `turn_source="lan"` 且参数白名单化（:369-377、877-922）。与传输解耦，可直接搬到中转链路。
- **远程回合限制（turn_source != "local"）**：禁电脑/浏览器操控（capability.py:96-99）、禁跨会话引用（agent_service.py:1480-1484）、禁副模型路由（turn_model_override.py:163-164）、不抢占桌面当前会话（gui_bridge.py:9444）。**注意**：命令执行与文件读写工具不受 turn_source 限制，约束来自权限模式与沙盒（remote-mobile.md 3.4）。
- **桌面主动外连有完整参考**：飞书通道「凭据换接入点 → WS 长连接 → 心跳/重连/优雅停止 → 服务状态机 → 事件去重」五件套（core/feishu/_lark_ws/client.py:465-520、404-414、593-621、313-345；service.py:27-32、74-152）。但**全库无任何 relay/tunnel 实现**（remote-mobile.md 4.3），桌面侧 WS dial 客户端需新写。
- frontend-mobile 是 Vue3+Vant 的 PWA（package.json:12-31、router.ts:6-14），四页应用 + 917 行 store 可作新前端参照；mobile-shim 200 行行为（token 存储/重连/水位/4401）是传输层规格。
- GUI 关闭 ⇒ LAN 必停（gui_bridge.py:2508-2524），无独立常驻形态——「我的电脑」接受「GUI 在跑才可连」的产品语义。

### 2.3 Linux 与浏览器跨平台（audit/platform-deploy.md）

- Linux 电脑操控整体不可用的根因：`LinuxPlatformAdapter(UnavailablePlatformAdapter)`（linux.py:6-8）；平台支持是「全有全无」单开关（controller.py:517-527）。
- 浏览器链路平台中立度高：worker 子进程 JSON 行协议、`sys.executable -m` transport Linux 可用（browser_backend.py:894-928）；**11 项最小改造清单**见该报告第二节（本文 4.3 展开）。
- **最高风险**：credentials.py:9 顶层 `from ctypes import wintypes`，Linux import 期 ValueError（推断，CPython 已知行为），连带 mcp_client.py:15 / doctor.py:22——P1 首先实测。
- 工具档位机制现成：`VALID_TOOL_PROFILES=("full","cli")`（tool_registry.py:182）、CLI_EXCLUDED_TOOLS（:133-180）、插件加载后统一 unregister（:1446-1455）；`YCODE_TOOL_PROFILE=cli` 会硬禁电脑操控（capability.py:87-93），server 档位必须绕开此交互。
- 依赖：requirements.lock 是 Windows venv 快照不可用于 Linux；playwright 组件 zip 必检 node.exe（components.py:89,190）Linux 必须走 pip；镜像建议 `mcr.microsoft.com/playwright/python:v1.62.0-jammy`（与 lock 的 playwright==1.62.0 对齐）；必须设 `LANG=C.UTF-8`（子进程编码依赖 locale，process_io.py:35-44）。
- 观察截图流现成：`computer_observation` 事件（controller.py:1028）携带 `preview_url`，指向进程内 127.0.0.1 预览服务（preview_server.py:3-5，每进程随机 token、只服务当前 run 最近 3 张 PNG）——Web 围观面板复用此流。

---

## 3. 总体架构

```mermaid
flowchart TB
    subgraph clients["客户端"]
        B["浏览器<br/>web/ SPA（响应式）"]
        A["安卓 App<br/>Capacitor 壳 + 同一套 SPA"]
        ADM["管理后台<br/>web/ SPA 的 admin 路由"]
    end

    subgraph server["Linux 服务器（唯一公网入口）"]
        CADDY["Caddy / Nginx<br/>TLS 终结 + 反代"]
        GW["中心网关 gateway（新建）<br/>FastAPI + SQLite<br/>账号/JWT · worker 生命周期<br/>WS 路由 · 设备注册表 · 管理 API"]
        subgraph workers["Docker 内部网络（不暴露公网）"]
            W1["worker 容器 acct-alice<br/>星瑶 --serve<br/>ApiBridge(无窗口) + LanServer<br/>headless Chromium"]
            W2["worker 容器 acct-bob<br/>（按需启停，空闲回收）"]
        end
        V1[("卷 /srv/xingyao/accounts/alice<br/>YCODE_DATA_DIR=/data")]
        V2[("卷 /srv/xingyao/accounts/bob")]
        GDB[("gateway.db<br/>账号/设备/用量")]
    end

    subgraph desktop["用户 Windows 电脑"]
        GUI["星瑶 GUI（现状保持）<br/>+ 云中转模块（新增）<br/>LanServer.dispatch_rpc 复用"]
    end

    B -- "HTTPS / WSS（账号 JWT）" --> CADDY
    A -- "HTTPS / WSS（账号 JWT）" --> CADDY
    ADM -- "HTTPS（管理员 JWT）" --> CADDY
    CADDY --> GW
    GW -- "持 worker token 接入<br/>双向透传桥接协议" --> W1
    GW --> W2
    W1 --- V1
    W2 --- V2
    GW --- GDB
    GUI -- "出站 WSS /relay/device（首帧设备 token）<br/>主动外连 + 心跳 + 重连" --> CADDY
    A -. "「我的电脑」：/relay/client（首帧 JWT）<br/>网关双向透传（不解释业务）" .-> GW
```

关键数据流：

1. **Personal Agent 对话（R3/R4/R6）**：App/浏览器登录换 JWT → WSS `/ws/agent` 首帧 `{"jwt","last_seq"?,"last_boot_id"?}` → 网关校验 JWT、按需拉起该账号容器 → 网关持 worker token 连容器内 LanServer → 按 hello/seq_reset/replay/live 顺序双向透传桥接协议帧。网关不理解 RPC 内容。
2. **我的电脑（R5）**：桌面云中转模块出站 WSS `/relay/device`，首帧提交设备 token、device_id 与 boot_id → 网关标记设备在线并推送给 App → App 连 `/relay/client`，首帧提交 JWT、device_id 与水位 → 网关校验归属后双向透传；桌面侧 RPC 交带连接级来源参数的 `dispatch_rpc`（全部现有安全边界生效），事件从 `_emit_raw` 分流到 relay 连接。
3. **管理（R7）**：admin 角色 JWT → `/api/admin/*` REST + `/ws/admin` 状态推送；worker 用量经容器内 `/metrics`（worker token 鉴权）拉取聚合。

---

## 4. 组件详细设计

### 4.1 中心网关 gateway（新建，R3/R5/R6/R7 的枢纽）

**职责**：账号/JWT；worker 生命周期（Docker API 按需启停 + 空闲回收）；WS 路由（纯转发）；设备注册表与 relay 配对；管理 API。技术栈 FastAPI + SQLite（WAL）起步，单机单进程，规模到了再换 Postgres。

**接口定义**：

| 端点 | 方法/协议 | 鉴权 | 语义 |
|---|---|---|---|
| `/api/auth/login` | POST | 用户名+密码 | 返回 `{access_token(30min), refresh_token(30d), role}` |
| `/api/auth/refresh` | POST | refresh_token | 轮换 refresh，返回新 access |
| `/api/auth/logout` | POST | JWT | 吊销 refresh |
| `/ws/agent` | WS | 首帧 `{"jwt"[,"last_seq","last_boot_id"]}` | 该账号 worker 的桥接协议透传；每前端连接独占一条上游 worker 连接，RPC id 天然不冲突 |
| `/api/devices` | GET | JWT | 本账号设备列表 + 在线状态（App「我的电脑」入口渲染） |
| `/ws/devices` | WS | 首帧 `{"jwt"}` | 设备在线状态变更推送 `{"type":"device_status","data":{device_id,online}}` |
| `/relay/device` | WS | 首帧 `{"type":"relay_register","token","device_id","boot_id"}` | 桌面注册；15s ping/pong，45s 无心跳离线；单连接 generation |
| `/relay/client` | WS | 首帧 `{"jwt","device_id"[,"last_seq","last_boot_id"]}` | App 连指定设备；校验账号归属；长期凭据不进入 URL |
| `/api/admin/accounts` | GET/POST/PATCH | admin JWT | 账号 CRUD（创建/禁用/重置密码/配额） |
| `/api/admin/devices` | GET/POST/DELETE | admin JWT | 设备列表；生成设备 token（明文只显示一次）；吊销/轮换 |
| `/api/admin/workers` | GET/POST | admin JWT | worker 列表与状态；手动 start/stop/restart |
| `/api/admin/usage` | GET | admin JWT | 按账号/日的 token 用量与回合数 |
| `/ws/admin` | WS | 首帧 `{"jwt"}`（admin） | worker 状态变更、设备上下线实时推送 |
| `/health` | GET | 无 | 存活探针 |

**WS 路由语义（/ws/agent）**：
- 首帧 10 秒超时；JWT 无效、过期、epoch 不符或 jti 在吊销表里，close **4401**。前端先 refresh 再重连，refresh 失效才回登录页。每个 REST 请求和每条 WS 首帧都读账号当前的 `disabled/role/auth_epoch`。access JWT 带 `jti`、`auth_epoch`、`role`。登出只吊销这一枚 refresh，并把这一枚 access 的 `jti` 写入 `access_denylist` 直到它过期，同时关掉这一条 WS；不增加 `auth_epoch`，其它设备保持登录。改密、禁用、降权才 `auth_epoch += 1`，并吊销该账号全部 refresh。网关在 epoch 变化后 5 秒内主动关掉该账号全部 WS，不等下一条消息。禁用同时停容器、关闭 `/relay/device`。改密不使设备 token 失效。
- 网关为每条前端连接单独 dial 一条 worker LanServer 连接，上游首帧统一为 `{"token":worker_token,"last_seq":<number|null>,"last_boot_id":<string|null>}`；全新连接两项均为 null。worker 的每客户端有界队列承担背压，补发缓冲在 worker 侧。
- **worker epoch 的精确顺序**：LanServer 启动生成 `boot_id=secrets.token_hex(8)`，只活在本进程内存。认证成功后首先发送 `{"type":"hello","data":{"boot_id":...}}`，此帧不带 `id`。随后只走下面三条之一，再进入实时帧：
  1. 客户端没带 `last_boot_id`，或与当前 `boot_id` 不同：发送 `{"type":"seq_reset","data":{"boot_id","reason":"boot","resync_required":true}}`（不带 `id`），忽略旧 `last_seq`，**不补发** 128 窗口。客户端必须先 `get_initial_state`。
  2. `boot_id` 相同，但缓冲为空，或缓冲中最小 seq 大于 `last_seq+1`（空洞或 128 溢出）：同样发送 `seq_reset`，`reason` 为 `"gap"`，不补发残窗。
  3. `boot_id` 相同且缓冲能盖住 `last_seq+1`：不发 `seq_reset`，只按 seq 升序补发 `seq > last_seq` 的帧，然后实时帧。
  旧服务端没有 `hello` 时，旧 shim 保持现在的行为；新 shim 进入 legacy：强制 `get_initial_state`，并且不把内存水位当成跨进程事实。新服务端收到没有 `last_boot_id` 的旧客户端，按第 1 条处理。
- **shim 合并规则**：`hello` / `seq_reset` 在调用 `onBridgeEvent` 之前拦截。收到 `resync_required:true` 时，暂停把 replay/live 交给 store，调用 `get_initial_state`，用返回值整份替换会话、队列和活跃回合。当前 `get_initial_state`（gui_bridge.py:4377，快照字段在 :4503-4517）**没有**会话 revision 字段，实施时不要发明这个字段。快照里的 `active_turn_snapshots` 已终态（完成、取消、错误）之后，丢掉该回合迟到的 `token_delta` / `thinking_delta`。快照替换完成前到达的帧先按 boot_id 暂存；替换后只应用同一 boot_id、且 seq 大于「暂停前已接收的最大 seq」的增量。无法判断某个 delta 是否已经含在快照里时，丢掉该 delta 并再取一次快照，不猜测合并。去重键是 `(boot_id, seq)`，禁止按事件类型去重，`token_delta` 可以合法重复。Web 把 `(boot_id,last_seq)` 写入 localStorage，键为 `服务器 + account_id + profile`。安卓（检测到 Capacitor）禁止把水位写入 localStorage、Preferences 或任何会熬过杀进程的存储；进程死了水位自然消失，重开走全新快照。退出、换号、切服务器、切 profile 时，先关旧 WS、取消未完成 RPC，再清该命名空间的内存事件、设备列表、会话缓存和水位。
- worker 未运行：先 `docker start`，推 `{"type":"worker_starting","data":{"boot_id":null}}`；健康检查通过后才以 worker hello 为新 epoch。worker 崩溃时推 `worker_lost`，其 boot_id 取网关缓存的最后一次健康快照；缓存为空则 null。worker `/health` 改为 `{"ok":true,"boot_id":...}` JSON，网关缓存该值，不能要求启动前或崩溃后实时读取。
- 本协议同时修改 `frontend/mobile-shim.js` 与 `core/lan_server.py` 内嵌兜底副本，Windows LAN 直连同步受益；新 `gateway-shim.ts` 复用同一状态机。修改现有 shim 属于 R1 的必要兼容改造，不是“完全不改 frontend”。

**worker 生命周期**：
- 按需启动：账号首次 WS 活动时拉起；容器命名 `xingyao-worker-<account_id>`，标签 `xingyao.account=<id>`，网关重启后按标签枚举重建映射。网关只连 `http://xingyao-worker-<account_id>:8766`（第 10 节）。`docker start` 后 60 秒内 `/health` 未返回 `ok` 就向客户端推 `worker_lost` 并停止重试这一次，不无限等。
- 空闲回收：每 30s 轮询 worker `/metrics` 取 `last_activity_ts` 与 `active_turn`；空闲超 30 分钟且无进行中回合 → `docker stop`。这 30 秒是指标轮询，不是第 4.6 节的 15 秒心跳。
- 容器加固基线：非 root（uid 10001）、`cap_drop: ALL`、`security_opt: no-new-privileges`、`mem_limit: 2g`、`cpus: 2`、`pids_limit: 512`、`shm_size: 1g`、只挂本账号卷、接入内部网络 `xingyao`、**不 publish 任何端口、不挂 docker.sock**。只有网关进程能访问 docker.sock。`TZ=Asia/Shanghai`。个人产品不做 egress 白名单（信任模型 1.3）。

**改造点**：gateway 全部新建；Q1 同时修改 worker `core/lan_server.py`、`frontend/mobile-shim.js` 与内嵌 shim 兜底，Q2 复用并抽取 dispatch/replay，不再声称“无现有代码改动”。

**新建文件清单**：
```
gateway/
  app.py            # FastAPI 装配、路由注册、启动恢复（按标签枚举容器）
  auth.py           # 密码 argon2、JWT 签发/校验/刷新轮换
  db.py             # SQLite schema 与迁移（见第 5 节）
  workers.py        # Docker SDK 生命周期：start/stop/health/metrics 轮询/空闲回收
  ws_agent.py       # /ws/agent 透传管道（每连接独占上游）
  relay.py          # /relay/device + /relay/client + 设备注册表 + 在线状态推送
  admin.py          # /api/admin/* + /ws/admin
  config.py         # 网关配置（数据根、Docker 网络、镜像名、JWT 密钥、空闲阈值）
  requirements.txt  # fastapi uvicorn websockets docker argon2-cffi pyjwt
  Dockerfile        # 或宿主机 systemd 单元（首版建议宿主机直跑，需访问 docker.sock）
deploy/
  Caddyfile         # TLS + 反代 gateway
  docker-compose.yml# gateway 网络 + worker 镜像模板（worker 由网关动态创建）
  gateway.service   # systemd 单元（宿主机形态）
```

### 4.2 worker：星瑶 `--serve` 常驻模式（R3/R6，R1/R2 零回归）

**职责**：容器内无窗口常驻，复用 ApiBridge + LanServer 全部现有能力；对网关暴露桥接协议与运维探针。

**接口定义**：
- 桥接协议：保留现有 RPC、事件、seq 补发和 4401。允许新增的只有不带 `id` 的控制帧 `hello` 与 `seq_reset`（形状见 4.1）。不新增 RPC 方法。4.1 与本节不一致时以 4.1 和本句为准。
- `GET /health`：返回 `{"ok":true,"boot_id":...}` JSON，供网关缓存当前/最后健康 epoch。
- `GET /metrics`（**新增**，serve 模式才注册路由；`Authorization: Bearer <worker_token>`，常量时间比较）：返回 `{"version","boot_id","uptime_s","last_activity_ts","active_turn":bool,"token_usage":[{model,prompt_tokens,completion_tokens,total_tokens}],"sessions":n,"turns_total":n}`。`token_usage` 是 `token_usage` 表按 `model` 列 `SUM` 后的全量累计（同一 model 下的 provider/api_base/cache_scope 合并掉；`model` 为 NULL 时指标里写成空字符串 `""`）。`turns_total` 的唯一数据源是：

  ```sql
  SELECT COUNT(*) FROM harness_turns
  WHERE status = 'completed'
    AND COALESCE(turn_kind, '') <> 'resume'
  ```

  表在 memory.py:695-711，状态枚举在 core/harness/models.py:7-15，`completed` 才算数。`cancelled` / `failed` / `interrupted` 以及 `turn_kind='resume'` 都不算。禁止用 `token_usage` 行数冒充回合。`sessions` 只是当前会话行数快照，不入 `usage_daily`。
- **用量入账契约**：网关表 `usage_watermarks` 存上次看到的累计值。每一轮在同一个 SQLite 事务里：读水位 → 各字段 `delta = current - watermark` → 若 prompt、completion、total、turns 任一 delta 为负，该 model 整组视为重置，delta 改取 current 全量，并写一条审计日志 → 把 delta 加进 `usage_daily` → 把水位更新为 current。映射是：`prompt_tokens → usage_daily.input_tokens`，`completion_tokens → usage_daily.output_tokens`。`total_tokens` 只留在水位里做回退检测，不进 `usage_daily`（该表没有 total 列）。`usage_daily.day` 是网关提交事务那一刻的 **Asia/Shanghai 日历日**（`YYYY-MM-DD`），代码里显式使用该时区，不依赖容器默认 UTC。这不是 `token_usage.date` 的实际消耗日；跨午夜的一整段增量记到入账日。模型改名视为新 model，不回溯改旧行。换卷、清库、轮询后提交前崩溃、跨午夜都要有测试夹具。首版用量页只展示 token 与 turns。统计只覆盖成功落进 `token_usage` 的记录，不承诺等于厂商账单。
- 进程接口：`python gui_main.py --serve`（或薄入口 `serve.py`）；env：`YCODE_DATA_DIR=/data`、`YCODE_TOOL_PROFILE=server`、`LANG=C.UTF-8`；SIGTERM/SIGINT → 优雅停机。

**启动序列**（照 core-service.md 1.3 表展开）：
1. 入口在 import GUI 重模块之前识别 `--serve`（仿 `--cli` 分发范式，gui_main.py:148-174），设 `YCODE_TOOL_PROFILE=server`（先于任何 core.llm import，同 main.py:10-11 模式）。
2. 复制：滚动日志（gui_main.py:326-372）、配置自检（:894-920，弹窗改日志+退出码）、mcp 单例（:954-957）、`set_trust_prompt(lambda: False)`（:958-969，防无终端 input 挂死）、`ApiBridge(mcp_manager=…)`（:970）、LLM 预热（可选，:662-696）。
3. 跳过：WebView2/窗口/托盘/内嵌浏览器（gui_main.py:931-938、975-1013、1020-1166）。**不要跳过 gui_main.py:858-875 的电脑运行时**；热键按下一步单独关掉。
4. **显式启动服务**：`_ensure_lan_server()`，监听第 10 节规定的固定端口；不依赖前端首屏触发。serve 默认禁用独立 MCP 监听（现默认开在 config.py:454-458，端口 8765）。确需开启时只能绑在该账号容器内、不 publish。飞书按账号配置可选。
5. **显式启动电脑运行时**：调用已存在的 `start_computer_runtime()` 与 `attach_preview_publisher()`（gui_main.py:873，函数定义在 core/computer/runtime.py:260 与 :344）。serve 传入的 `hotkey_factory` 直接抛 `ComputerControlError`，让 runtime.py:236-242 的既有降级路径跳过热键，不在 Linux 上碰 Win32。退出调用已存在的 `shutdown_process_computer_runtime("serve_exit")`（runtime.py:394）。没有 publisher 时不得声称围观已接线。
6. per-account 锁：`YCODE_DIR/serve.lock`，`fcntl.flock` + pid 文件。SIGTERM/SIGINT → `api.shutdown()` + `shutdown_process_computer_runtime("serve_exit")` + `lan.stop()`，确认 preview 端口和 Chromium 进程都没了。

**改造点清单**（对应 core-service.md 第二节 R1-R12）：

| # | 改造点 | 证据 file:line | 改法（零回归约束） |
|---|---|---|---|
| S1 | frameless Linux import 即崩 | core/frameless.py:34、75-99、101-117、204/217；gui_bridge.py:47 顶层 `from core.frameless import _MONITORINFO` | **选定一种做法，不留二选一。** 在类定义之前提供等宽类型：win32 用 `wintypes`，其它平台用 `ctypes.c_uint32` / `c_long` / 自建 RECT、POINT。`_MONITORINFO` 与 `_MINMAXINFO` 继续无条件定义，这样 gui_bridge.py:47 在 Linux 能导入。`WINFUNCTYPE`、`windll.user32`（:101-117）和函数内 `dwmapi`（:204、:217）整段放进 `sys.platform == "win32"`。禁止只删掉 `from ctypes import wintypes` 却留下字段引用。Windows 上 DWM 行为不变。gui_bridge.py:186 起的光标贴边已经先判断 win32，不用改调用顺序 |
| S2 | `_emit_raw` 无窗口掐断广播 | gui_bridge.py:3809-3831（早退 :3810-3811，LAN 广播 :3824-3826，飞书 :3829-3831） | **保留 `_shutting_down` 早退不变（审查 Q4 必改）**，仅移除 `not self._window` 条件：窗口存在才 `_queue_js_payload`；非关闭期 LAN/飞书（及 P4 云中转）广播无条件执行。有窗口时与关闭期的执行顺序均与现状完全一致 |
| S3 | serve 入口与初始化序列 | gui_main.py:148-174、923-970；gui_bridge.py:4382-4398、8106-8220 | 新增 `--serve` 分支 + `serve_main()`（上文序列）；不动 `--cli` 与 GUI 分支 |
| S4 | 权限无人应答挂死 | permission_terminal.py:524-539、551-564；gui_bridge.py:1951-1954、7915-7939 | ApiBridge 构造后 serve 必须显式替换 broker handler。默认有在线客户端时等待、10 分钟超时拒绝；**无在线客户端立即拒绝**，不创建无限等待请求；可配全自动拒绝。超时/人工应答/取消/断线/shutdown 以原子结算保证只完成一次，后到的应答返回已失效。GUI/CLI 原 handler 与无限等待语义不动 |
| S5 | 凭据后端 | credentials.py:51-56、23-44 | 非 Windows 固定 `keyrings.alt.file.EncryptedKeyring`，禁止 `PlaintextKeyring`。进程启动时 `keyring.set_keyring(...)`，文件目录只在 `/data/keyring/`（用该后端的 `file_path` 或 `XDG_DATA_HOME=/data`，二选一写进启动代码并加测试）。主口令来自 Docker secret 文件 `/run/secrets/keyring_password`，读入后赋给该后端的非交互口令入口。以容器里安装的 keyrings.alt 源码为准：若它认 `KEYRING_CRYPTFILE_PASSWORD`，就设这个变量；若只认属性，就设属性。禁止 `getpass`，stdin 不是 TTY 时必须立刻失败而不是卡住。口令不进 config.json、镜像、命令行参数和日志。重启用同一 secret 解锁。轮换口令 = 用旧口令读出全部 Key、用新口令写回、替换 secret、重启容器。验收：卷内文件检索不到模型 Key 明文。`_DataBlob` / wintypes 随浏览器清单第 9 项做平台隔离 |
| S6 | 单实例锁 | gui_main.py:699-831（:721-722 非 win32 返回 False） | serve 用 flock + pid 文件；一账号一容器由网关保证 |
| S7 | LanServer bind/对外地址 | 构造 lan_server.py:379；监听 `_serve` :1147-1154（:1150 写死 `host="0.0.0.0"`）；`start` 返回 :1145。:412-453 是 `_detect_lan_ip` / `bind_url`，里面拼的是带 `?token=` 的对外 URL，**不是监听地址** | 给构造函数加 `bind`，默认 `"0.0.0.0"`，并传到 :1150 和 :1145 的返回值。新增配置键 `remote_public_url`（当前仓库没有这个键），只替换 `bind_url` 的对外前缀，不改变监听。serve 使用第 10 节的固定端口 8766，不再 `port=0`。容器不 publish。验收看 `docker port` 和实际连入，不看 Caddyfile 文本 |
| S8 | 数据目录残留 | tokenrhythm_auth.py:64 与 :68；workflows/store.py:44-45；skill_sync.py:17-18；hooks/zcode_import.py:385；harness/tool_adapters.py:154；skills.py:682 | 改走 `config.YCODE_DIR` 或像 hooks/registry.py:36-45 那样读 `YCODE_DATA_DIR`。server 档跳过 skills.py:682 的 `~/.agents/skills`。Windows 上不设 `YCODE_DATA_DIR` 时路径仍是 `~/.ycode` |
| S9 | Windows 专属功能面 | gui_browser_mixin.py:6-7,48；gui_snip_mixin.py:365；gui_terminal.py:140；gui_main.py:858-875 | serve 不加载窗口步骤；桥方法保留 `unsupported_platform` 降级；工具表由 server 档裁剪（4.3 #10） |
| S10 | 调试残留 | llm.py:379-397 | 改为默认关闭的 env 开关；优先保留原 Windows 路径开关的兼容行为，不得仅凭 Linux no-op 就声称删除对 Windows 无风险 |
| S11 | 服务不依赖前端首屏 | gui_bridge.py:4377-4398 | S3 已覆盖：启动序列显式触发，`get_initial_state` 幂等保留 |
| S12 | 退出收尾 | gui_main.py:1173-1181；gui_bridge.py:2419-2430；lan_server.py:1188-1198 | 信号 → shutdown 链；`shutdown` 已在远程黑名单（lan_server.py:316） |
| S13 | `/metrics` 运维探针（新增） | lan_server.py:950-971（路由表）、memory.py:962（token_usage 表） | serve 模式注册 `/metrics` 路由，worker token 鉴权；GUI 模式不注册 |

**worker token 供给**：网关在账号开通时生成 `secrets.token_urlsafe(24)`，预写账号卷 config.json 的 `remote_pair_token` / `remote_pair_token_expires_at`（config.py:450-451），并把 `remote_token_ttl` 写成 0。lan_server.py:498 的注释说明 TTL≤0 表示永不过期；签发实现在 :504-505。worker 启动走 `_load_token_from_config`（:630 起，`start` 在 :1126 调用）。轮换 = 网关重写 config + 重启容器。这个 token 就是 LanServer 的 WS 凭据，不是另一种凭据。P2 起浏览器和 App 不持有它，由网关在服务器侧首帧送出。P1 没有网关时，页面上的配对 token **就是这同一枚 worker token**，现有 shim 还会把它写入 localStorage（frontend/mobile-shim.js:23-31）。P1 必须在登录页和威胁说明里写明这一点；P2 删除这条直连模式。预览 HTTP 从 P1 起就不把 worker token 放进浏览器，见 4.3。

**新建文件清单**：
```
serve_main.py                 # 薄入口：设 env → 日志/自检/锁 → ApiBridge → 显式起服务 → 信号常驻
core/serve/__init__.py
core/serve/permission_policy.py  # S4 策略 handler（无客户端立即拒绝 / 在线等待+超时拒绝 / 全拒绝）
core/serve/lock.py            # flock + pid
core/serve/metrics.py         # /metrics 数据组装（读 token_usage 等）
tests/test_serve_bootstrap.py # 无窗口构造、广播不掐断、黑名单、bind 参数、权限策略
requirements-server.txt       # Linux 专用依赖（剔 pywebview/pystray，加 keyrings.alt）
requirements-server.lock      # Linux 容器内 pip freeze 生成
deploy/worker.Dockerfile      # 基于 playwright/python:v1.62.0-jammy，非 root，CJK 字体
```

### 4.3 浏览器操控 Linux 化（R3，R1 零回归）

**职责**：让 server 档 worker 在容器内用 headless Chromium 执行浏览器自动化；desktop target 保持 Windows-only；Windows GUI 的 edge/chrome/embedded 路径行为不变。

**接口定义**：不新增对外接口。`computer_action` 工具经现有 capability 判定注入（llm.py:2059-2079）；观察截图经现有 `computer_observation` 事件 + preview_server 流出（controller.py:1028、preview_server.py:3-5）。

**turn_source 决策**：新增受控来源值 **`"server"`**。共享 dispatch 签名改为 `dispatch_rpc(method,args,turn_source="lan")`，来源由服务端按连接传入且 `_force_lan_turn_source` 改为按该参数注入；Windows LAN/relay 保持 lan，serve 连接才传 server。capability 对 server **只允许 browser、恒拒 desktop**；其余远程限制与 lan 相同。同步 `gui_bridge.TURN_SOURCES`、`llm.COMPUTER_TURN_SOURCES` 与一致性测试。

**11 项最小改造清单**（对应 platform-deploy.md 第二节，逐项展开）：

| # | 文件:行 | 改法 |
|---|---|---|
| 1 | core/computer/platforms/linux.py:6-8 | 真实现替换 UnavailablePlatformAdapter：`browser_supported=True`、`desktop_supported=False`（per-target 标志，见 #8）；`find_browser_executable` 返回 `shutil.which("chromium"/"google-chrome")` 或 Playwright 自带 chromium；desktop/hotkey/surfaces 方法保持抛 `computer_unsupported_os`；`browser_window_hints` 返回 `()`（headless 无窗口，走 DOM 裁决）。顶层保持零重依赖（command_shell.py:12 连带 import） |
| 2 | core/computer/settings.py:26、37-41 | `_BROWSER_DEFINITIONS` 增加 `"chromium"`；`DEFAULT_COMPUTER_BROWSER="edge"` 按平台回落（Linux 默认 chromium，Windows 不变）；:137 embedded 判定保持 win32-only |
| 3 | core/computer/browser_backend.py:46 | `_CHANNELS` 增加 `"chromium": "chromium"`；用 Playwright 自带浏览器时 channel 传 None（:1211 条件化） |
| 4 | core/computer/browser_backend.py:1212 | `headless=False` → 按平台：Linux 恒 True，**Windows 恒 False 不变**；Linux 补 `args=["--no-sandbox","--disable-dev-shm-usage"]`（当前 launch 无 args 参数，:1209-1215） |
| 5 | core/computer/browser_backend.py:1244-1245、1257 | `_resolve_browser_pid` exe 名与 `replace("/","\\")` 按平台分支（Linux：chrome/chromium/headless_shell + `os.path.normcase`）；不改不炸但多标签裁决 fail-closed |
| 6 | core/computer/browser_worker.py:37 | `choices=("edge","chrome")` 增加 `"chromium"` |
| 7 | core/computer/capability.py:87-93 | cli 硬禁用判定保留；server 档不走 `computer_cli_disabled`，改为「允许 browser、剔除 desktop」（:105-111 targets 组装处按平台/档位过滤）；:96-99 远程禁令扩为 `turn_source not in ("local","server")`，server 来源再按 #8 过滤 desktop；:100-103 文案随平台更新 |
| 8 | core/computer/controller.py:180-231、499-527、532-598；gui_bridge.py:7126-7160 | per-target 判定同时覆盖 capability、generation 复核和 `_default_backend_factory`，工厂不得再被全局 `adapter.supported` 一刀切；controller 浏览器白名单与 settings 共享定义并纳入 chromium。**serve 空配置默认启用 browser-only 能力、desktop 恒关**；远程不开放 `set_browser_control_enabled` 扩权。server preflight 只检查 Chromium 可用性，不要求 `probe_hotkey()`；Web 紧急停止保留，Windows GUI preflight 不变 |
| 9 | core/credentials.py:9 | `from ctypes import wintypes` 移入 win32 分支（`_DataBlob` :23-24 一并惰性化）；连带解除 mcp_client.py:15 / doctor.py:22 的 Linux import 崩溃风险。**P1 第一件事在 Linux 实测此处与 frameless** |
| 10 | main.py:10-11 + core/tool_registry.py:182、133-180、1446-1455 | 新增 `YCODE_TOOL_PROFILE=server` 档：`VALID_TOOL_PROFILES=("full","cli","server")`；`SERVER_EXCLUDED_TOOLS = CLI_EXCLUDED_TOOLS | {…}` 再剔 skill/plugin/hook 管理等（保留 browser_set_viewport 与 computer_control 停止面板）；:1446 判定改 `profile in ("cli","server")` 按档位选集合；plugins/windows_tools/plugin.py:899 的 `=="cli"` 改 `in ("cli","server")`；工具描述 Windows 路径示例（tool_registry.py:1400,1405）server 档换 POSIX |
| 11 | core/computer/browser_backend.py:445-480、591-670 | POSIX 在 Popen 处设 `start_new_session=True`，硬杀按 pgid `killpg`；保留 Windows Job Object 分支，容器 PID 1 回收兜底。不能只改硬杀函数而漏建进程组 |

**围观体验（首版）**：serve 启动时显式接入 `start_computer_runtime()` 和 `attach_preview_publisher()`，`computer_observation.preview_url` 先指向容器回环预览，再由 serve 重写为同源 `/computer-preview/{token}/{run_id}/{ordinal}.png`。LanServer 新增对应反向代理，校验原始 token/run/path 后读取回环 PNG。
- P1 无网关：浏览器只持 basic-auth 会话，不持 worker token；Caddy 验证 basic-auth 后向上游 `/computer-preview/*` 注入 `Authorization: Bearer <worker_token>`。WS 配对 token 仍是过渡例外，P2 收回。
- P2 起：浏览器/App 请求网关同源预览 URL，网关先验账号 JWT 与设备/账号归属，再以 worker token 访问内部 worker；跨账号返回 403/404。长期 worker token 不得进入浏览器 URL、图片地址或访问日志。
- 验收使用真实 `<img>` 请求验证 200/401/403、跨账号拒绝和 URL 重写；首版不做 CDP screencast 视频流。

**新建文件**：无独立新文件（全部是对现有文件的守卫式修改）；新增 `tests/test_browser_linux.py`（headless 启动、chromium channel、per-target capability、server 档工具表）。

### 4.4 Web 前端 web/（新建，R3/R7 载体）

**职责**：一套响应式 Vue3 SPA，同时服务浏览器与 App（Capacitor 壳）；极简对话界面 + 浏览器围观面板 + 权限确认 + 管理后台路由。Muse/Today 风格：单列对话流、底部输入、侧滑会话抽屉，无桌面端的多栏 IDE 布局。

**技术栈**：Vue 3 + TypeScript + Vite + Pinia（与 frontend / frontend-mobile 同栈，frontend-mobile/package.json:12-31 版本可沿用）；UI 组件轻量自选（Vant 可复用）；Markdown 管线移植 frontend-mobile/src/utils/md.ts:1-5（整体预转义防注入）。

**传输层（复用 mobile-shim 协议，remote-mobile.md 第二节为规格）**：
- `gateway-shim.ts`：复用 4.1 的完整 epoch 状态机，首帧 `{"jwt"[,"last_seq","last_boot_id"]}`；hello/seq_reset/resync_required 必须先于 RPC/事件扇出处理。4401 先 refresh 再重连，refresh 失效才回登录页。RPC Proxy/onBridgeEvent/2s 退避保留，水位键按服务器+账号+profile 隔离。
- **双后端 profile**：`agent` 连 `/ws/agent`，`desktop` 连 `/relay/client` 并首帧提交 JWT/device_id。两 profile 复用协议但使用不同水位命名空间；切服务器、退出换号或切 profile 时先关闭旧 WS、取消 pending RPC，再清会话/事件/设备缓存，防止旧帧写入新账号 store。
- P1 过渡只有一种例外：浏览器直连 worker 的 WS 仍走现有 shim 的 `?token=`（frontend/mobile-shim.js:8-9、:24）。这枚 token 就是 worker token，不是另一套口令。新的 `gateway-shim.ts` 在 P1 可以读 URL 一次，然后只放进 sessionStorage，关掉标签即丢；不要为了「和旧 shim 完全一致」再写进 localStorage。P2 起删除 `?token=` 模式，网关端点全部首帧 JWT。Caddy 访问日志按第 10 节打码。预览图不走这条例外。

**页面清单**：
| 路由 | 内容 | 参照资产 |
|---|---|---|
| `/login` | 登录（P1 为 token 直连页） | — |
| `/chat` | 会话抽屉 + 对话流（token_delta 流式、思考、工具卡、排队、撤销、上传） | frontend-mobile ConvoListView/ConvoDetailView + app.ts 917 行 store（RPC 面清单见 remote-mobile.md 1.3） |
| `/chat` 内围观面板 | `computer_observation` 截图轮播 + 动作标注 + 紧急停止按钮 | frontend ComputerControlBar/RightPanel 的 reduceComputerEvent 语义 |
| `/approvals` | 权限确认收件箱（permission_request 卡片，respond_permission_* 应答） | frontend-mobile ApprovalsView.vue:113-152 |
| `/settings` | 连接状态、模型与 Key、会话权限档位、工作区、（P4）我的电脑设备列表 | frontend-mobile SettingsView |
| `/admin/*` | 管理后台（role=admin 才可见，见 4.7） | — |

**改造点**：web/ 全部新建；为 Q1 同步修改 `frontend/mobile-shim.js` 和 `core/lan_server.py` 内嵌兜底副本，frontend-mobile store 若消费新控制帧需保持未知事件兼容。其余 desktop frontend 业务页面不改。`computer_observation` 不参与历史补发，围观面板重连后以最新事件与当前快照自愈。

**新建文件清单**：
```
web/
  package.json / vite.config.ts / tsconfig.json / index.html
  src/main.ts / src/router.ts
  src/transport/gateway-shim.ts   # JWT/token 双模、RPC Proxy、事件扇出、seq 水位、重连
  src/transport/profile.ts        # agent / desktop 双 profile 端点选择
  src/stores/app.ts               # 参照 frontend-mobile app.ts 移植（事件语义子集）
  src/stores/auth.ts / src/stores/admin.ts / src/stores/devices.ts
  src/views/LoginView.vue / ChatView.vue / ApprovalsView.vue / SettingsView.vue
  src/components/WatchPanel.vue / PermissionCard.vue / ToolCard.vue / SessionDrawer.vue
  src/admin/AdminLayout.vue / AccountsView.vue / DevicesView.vue / WorkersView.vue / UsageView.vue
  src/utils/md.ts / toolTrace.ts  # 移植
```

### 4.5 安卓 App（R4，P3）

**职责**：Muse/Today 风格的移动操作界面，直连服务器；「我的电脑」入口经网关 relay 到桌面。**首版不做原生 Kotlin**：Capacitor 包装 4.4 的同一套 SPA。

**设计**：
- `app/` Capacitor 工程：`server.url` 不写死，首启引导页填服务器地址（存 Capacitor Preferences）；WebView 加载本地打包的 web/ dist，API/WS 指向用户配置的服务器（HTTPS/WSS，证书校验默认开启）。
- 原生能力首版仅三件：安全存储、网络状态监听、应用图标/启动屏。P3 开工前在依赖锁中选定支持 Android Keystore 的 Capacitor Secure Storage 插件并记录版本/配置；JWT/refresh 只能经该插件读写，Preferences 仅存非敏感服务器地址。禁止把 refresh 明文写 Preferences；定义系统备份排除/恢复、卸载换机、Keystore 密钥失效后的重新登录，以及退出换号/切服务器时删除凭据和水位。推送通知、保活、扫码列为后续增强。
- 两个后端 profile 与 Web 完全一致（4.4）：`agent` = Personal Agent；`desktop` = 我的电脑（设备列表来自 `/api/devices`，在线状态经 `/ws/devices` 推送，离线时入口置灰并提示「电脑上的星瑶未运行」）。
- frontend-mobile 现有 PWA 不废弃：继续作为桌面 LAN 直连的手机前端；其页面与 store 是 web/ 的移植参照（remote-mobile.md 1.5 复用度结论）。

**改造点**：无现有代码改动。**新建文件**：
```
app/
  package.json / capacitor.config.ts
  android/                  # Capacitor 生成的原生工程（首版不改 Kotlin 业务代码）
  src/bootstrap/            # 首启服务器地址引导页（打包进 web dist 的独立入口）
```

### 4.6 云中转「我的电脑」（R5，P4）

**职责**：桌面 GUI 新增「云中转」模块，持设备 token 主动外连网关注册；网关纯转发；App 经网关触达桌面 ApiBridge。GUI 关闭则设备离线（接受此产品语义，与现有 LAN 生命周期一致，gui_bridge.py:2508-2524）。

**接口定义**：
- 桌面 → 网关：出站 `WSS /relay/device`，首帧 `{"type":"relay_register","token":<设备token>,"device_id":...,"boot_id":...}`；注册成功后网关回 `{"type":"relay_registered","data":{"device_id":...,"name":...,"connection_generation":n}}`。boot_id 只在注册请求中由桌面→网关上报，不与回包混用。WS ping/pong 固定 15s，45s 无心跳判离线；断线指数退避重连。长期 token/JWT 均不进入 URL 或访问日志。
- App → 网关：`WSS /relay/client`，首帧 `{"jwt":...,"device_id":...,"last_seq":<number|null>,"last_boot_id":<string|null>}`。网关在首帧同时检查账号 disabled/auth_epoch、设备归属、revoked 与当前连接 generation。
- 配对后业务帧沿用 LanServer：RPC `{"method","args","id"}` → `{"id","result"|"error"}`；事件 `{"type","data"[,"seq"]}`。网关只做 RPC id 命名空间化和传输级缓冲，不解析 method 语义。
- **单连接与冒用处理**：每个 device_id 同时只允许一条注册连接；新合法注册使 `connection_generation` 原子递增，旧连接立即 close 4401，其后的帧与在途应答全部作废。设备 token 可独立冒充桌面接收主人后续 RPC，不能写成“无影响”；因此必须轮换/吊销即踢线、日志脱敏，并测试攻击者抢先注册与旧新桌面竞争。
- **桌面侧有限补发**：EventReplayBuffer 与 seq 随 GUI 进程存活；relay 断开期间仍对白名单事件打 seq 并写入桌面 128 帧缓冲。重连注册成功后，先按序上传仍在窗口内的离线帧，再进入实时发送；网关按 `(device_id,boot_id,seq)` 去重，保留每设备 128 帧镜像。GUI 重启导致 boot_id 变化时，网关清空镜像并向所有配对 App 推 `{"type":"device_reset","data":{"device_id","boot_id"}}`，App 重置水位并 `get_initial_state`。
- **App 持续在线也必须收敛**：桌面注册成功后，网关向该设备已连上的每个 App 推一帧，不要求 App 再发「请求补发」（没有这个方法）：

  ```json
  {"type":"device_online","data":{"device_id":"...","boot_id":"...","connection_generation":1,"oldest_seq":10,"newest_seq":20}}
  ```

  `oldest_seq` / `newest_seq` 来自网关手里的 128 帧镜像；镜像为空时两个都为 null。网关接着按 seq 升序把 App 还没见过的帧推下去。出现下面任一情况时不推残窗，改发：

  ```json
  {"type":"resync_required","data":{"device_id":"...","boot_id":"...","reason":"boot|gap|overflow|queue_drop"}}
  ```

  条件：`boot_id` 变了、`oldest_seq > app.last_seq+1`、镜像因为 128 上限丢过帧、出站队列丢过帧。App 收到后停用旧 replay，清本 profile 水位，调用 `get_initial_state`。只承诺窗口内事件去重，以及溢出之后最终状态能对齐。不承诺历史事件一条不丢。桌面 `boot_id` 由 `CloudRelayClient` 每进程生成一次（`secrets.token_hex(8)`），只出现在注册请求里。
- **背压与在途 RPC**：CloudRelayClient 出站 500 条有界队列 + put_nowait，满则记录丢帧并触发 resync_required，绝不阻塞回合线程；relay 断开时在途 RPC 由网关立即回 `{"id":…,"error":"device_offline"}`，App 离线时发 RPC 同样即时报错。一台桌面并发 App 客户端上限 4。

**桌面侧设计**（参考飞书五件套，remote-mobile.md 4.1；帧层弃用 lark protobuf，用 JSON 桥接协议）：
- `CloudRelayService`：单例 + daemon 线程 + 状态机（disabled/connecting/connected/reconnecting/error/stopped，照抄 feishu/service.py:27-32、74-152）；任何公开方法不抛异常（同 :9 约定）。
- `CloudRelayClient`：WS dial + 注册 + 心跳 + 重连 + 优雅停止（照抄 _lark_ws/client.py 五件套，但用 `websockets` 库 JSON 帧，不用 vendored lark 协议栈）。
- **RPC 入站**：收到的 RPC 帧交抽取后的共享 `dispatch_rpc(method,args,turn_source)`。turn_source 只能由服务端按连接注入：Windows LAN/桌面 relay=`lan`，serve worker=`server`，客户端字段始终剥离。抽取必须整体保留 54 项黑名单、`_` 前缀拒绝、`set_permission_mode` 会话级守卫、消息参数白名单/裁剪、来源注入和 `_call_bridge`，不能只复制名单。LanServer 内部委托共享函数，Windows 默认参数与行为不变。
- **事件出站**：`_emit_raw` 在 LAN/飞书之后新增第四写 `cloud_relay.on_event(event_type, payload)`（与飞书同范式，gui_bridge.py:3829-3831）；relay 侧用共享的 seq 打标 + 环形缓冲助手（从 lan_server.py:698-718 抽出 `EventReplayBuffer`，LanServer 与 CloudRelayClient 共用，抽取为纯重构、Windows 行为不变）打标后写入外连 WS。
- **生命周期接线**：`_ensure_cloud_relay()` 双检锁（照 `_ensure_feishu_service`，gui_bridge.py:8190-8220）；配置开启时随 `get_initial_state` 后台启动（:4401-4409 同位置追加）；`shutdown()` 显式 stop（:2514-2524 同链追加）。
- **配置与 GUI**：config.json 新键 `cloud_relay_enabled`（默认 False）、`cloud_relay_url`、`cloud_relay_token`（写盘经 credentials.protect_secret 加密，照 feishu app_secret 范式 gui_bridge.py:8292-8300）。GUI 设置页新增「云中转」卡片：开关、网关地址、设备 token 粘贴框、连接状态、断开/重连按钮（新组件 CloudRelaySettings.vue，参照 FeishuSettings.vue）。
- **安全**：turn_source 复用 `"lan"` → 电脑操控/浏览器操控被禁（capability.py:96-99）、跨会话引用被禁、不抢占桌面会话；权限确认卡照常全端广播（桌面 + App 都可应答，无限等待语义在桌面 GUI 场景保留——桌面有人值守）。设备 token 可吊销、可轮换（管理后台操作，网关 close 4401 踢线）。**明确风险**：远程回合仍可在会话权限档位允许下执行命令与读写文件（remote-mobile.md 3.4），「我的电脑」等于把桌面 shell 暴露给持 JWT 的 App 端——信任模型下可接受，文档与 App 首次连接提示中写明。

**改造点清单**：

| # | 位置 | 改动 |
|---|---|---|
| C1 | gui_bridge.py:3809-3831 | `_emit_raw` 第四写 cloud_relay（S2 重构时预留 sink 列表，P4 只加一个 sink） |
| C2 | gui_bridge.py:8190-8220 范式 | 新增 `_ensure_cloud_relay()`；get_initial_state 后台启动链追加（:4401-4409） |
| C3 | gui_bridge.py:2514-2524 | shutdown 链追加 cloud_relay.stop() |
| C4 | lan_server.py:821-859、698-718 | dispatch_rpc 抽为可独立调用；EventReplayBuffer 抽出共用（纯重构） |
| C5 | config.py:460-463 范式 | 新增 cloud_relay_* 三键（默认关闭，不影响现有功能） |
| C6 | frontend/src/components/ | 新增 CloudRelaySettings.vue + 设置页挂载 |
| C7 | gateway/relay.py | 设备注册表、配对、id 映射、补发缓冲、在线状态推送（新建） |

**新建文件清单**：
```
core/cloud_relay/__init__.py
core/cloud_relay/service.py    # 状态机 + 单例 + 生命周期
core/cloud_relay/client.py     # WS dial/注册/心跳/重连/帧收发
core/cloud_relay/replay.py     # 若 EventReplayBuffer 放此处则 lan_server 反向引用（或放 core/remote_transport.py）
frontend/src/components/CloudRelaySettings.vue
tests/test_cloud_relay.py      # 注册/重连/dispatch 黑名单/事件打标/吊销踢线
gateway/relay.py               # （已列于 4.1）
```

### 4.7 管理后台（R7，P2）

**职责**：账号管理、设备管理、worker 状态、用量查看。形态 = web/ SPA 的 `/admin/*` 路由（role=admin 守卫）+ 网关 `/api/admin/*`，不做独立站点。

**功能与接口**（端点定义见 4.1 表）：
- **账号**：列表/创建（用户名+初始密码，argon2 哈希）/禁用（禁用即拒登录 + 停容器 + 踢 WS）/重置密码/配额字段（磁盘上限、每日 token 上限——首版只记录展示，超额拦截列后续）。无公开注册入口，账号只能由管理员创建（信任模型）。
- **设备**：列表（归属账号、在线状态、last_seen）；生成设备 token（`secrets.token_urlsafe(32)`，DB 只存 SHA-256 哈希，明文仅在创建响应中出现一次）；吊销（置 revoked + close 4401）；轮换（新 token 生效、旧 token 立即失效）。
- **worker**：列表（账号、容器状态、uptime、last_activity、active_turn、版本）；手动 start/stop/restart；状态变更经 `/ws/admin` 实时推送。数据源 = Docker API + `/metrics` 轮询。
- **用量**：按账号/网关入账日聚合 token 与持久 turns；首版不展示没有历史数据源的按日 sessions，不做计费。
- **空库 bootstrap**：只在服务器本机执行 `gateway init-admin --username …`。口令从 TTY 读入，不进 argv，不进 shell 历史。创建首个 admin 后永久关闭；库里已有任一账号时拒绝。没有无凭据的远程建号接口。

**改造点**：全部新建（gateway/admin.py + web/src/admin/*）。worker 侧唯一依赖是 4.2 的 `/metrics`。

---

## 5. 数据与隔离设计

### 5.1 网关账号库 schema（SQLite WAL，`/srv/xingyao/gateway/gateway.db`）

```sql
CREATE TABLE accounts (
  id            TEXT PRIMARY KEY,          -- uuid，亦作容器名/卷名后缀
  username      TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,             -- argon2id
  role          TEXT NOT NULL DEFAULT 'user',   -- user | admin
  disabled      INTEGER NOT NULL DEFAULT 0,
  auth_epoch    INTEGER NOT NULL DEFAULT 0,      -- 禁用/改密/降权递增，旧 access 立即失效
  quota_json    TEXT NOT NULL DEFAULT '{}',     -- 磁盘/token 配额（首版仅展示）
  created_at    INTEGER NOT NULL
);
CREATE TABLE refresh_tokens (
  token_hash  TEXT PRIMARY KEY,            -- SHA-256，明文不落库
  account_id  TEXT NOT NULL REFERENCES accounts(id),
  expires_at  INTEGER NOT NULL,
  revoked     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE devices (
  id          TEXT PRIMARY KEY,            -- uuid
  account_id  TEXT NOT NULL REFERENCES accounts(id),
  name        TEXT NOT NULL,               -- 「家里的台式机」
  token_hash  TEXT UNIQUE NOT NULL,        -- SHA-256，明文仅创建时展示一次
  revoked     INTEGER NOT NULL DEFAULT 0,
  last_seen   INTEGER,                     -- 最近心跳
  created_at  INTEGER NOT NULL
);
CREATE TABLE workers (
  account_id     TEXT PRIMARY KEY REFERENCES accounts(id),
  container_name TEXT NOT NULL,
  worker_token   TEXT NOT NULL,            -- 网关必须持明文以接入 LanServer；库文件权限 600
  desired_state  TEXT NOT NULL DEFAULT 'stopped',  -- running | stopped
  last_activity  INTEGER,
  updated_at     INTEGER NOT NULL
);
CREATE TABLE usage_daily (
  account_id TEXT NOT NULL,
  day        TEXT NOT NULL,                -- 网关入账日（服务器时区），非实际消耗日
  model      TEXT NOT NULL,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  turns      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, day, model)
);
CREATE TABLE usage_watermarks (
  account_id TEXT NOT NULL,
  model      TEXT NOT NULL,
  prompt_tokens INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  total_tokens INTEGER NOT NULL DEFAULT 0,
  turns_total INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, model)
);
CREATE TABLE access_denylist (
  jti         TEXT PRIMARY KEY,
  account_id  TEXT NOT NULL REFERENCES accounts(id),
  expires_at  INTEGER NOT NULL
);
CREATE TABLE schema_migrations (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);
```

`usage_daily.day` 按 Asia/Shanghai 的日历日写入。`schema_migrations` 只有向前的整数版本；网关启动时先迁移，失败则拒绝接流量。当前脚本是 version 1。

设备在线状态是内存态（连接注册表），不落库；`last_seen` 供离线展示。审计日志首版用结构化文件日志（gateway.log），不建表。

### 5.2 每账号卷布局（worker 容器）

```
/srv/xingyao/accounts/<account_id>/
  data/            → 容器 /data（YCODE_DATA_DIR=/data）
    config.json    # 模型 Key（keyring file 后端加密）、remote_pair_token（网关预写）、cloud 无关
    ycode_gui.db   # 会话/消息/记忆/token_usage（GUI_DB_FILE，config.py:439）
    skills/  mcp.json  permissions.json  plugins/  hooks.json
    media/  exports/  tmp/  logs/  gui-themes/  memory/
    home/          # 容器 HOME=/data/home：兜住一切 Path.home() 残留（双保险）
    keyring/       # keyrings.alt file 后端数据
  workspace/       → 容器 /workspace（agent 工作区根，每账号独立，避免共享
                     <workdir>/.ycode_tasks 与 project 级 hooks/workflows 串号，
                     core-service.md 第四节「按工作区隔离的设计项」）
```

隔离三支柱：① 一账号一容器一进程；② `YCODE_DATA_DIR` + S8 残留修复；③ 容器 HOME 指向账号卷。容器只挂本账号目录。P2 验收必须另以 A/B 同名会话、不同消息/Key/记忆/文件验证 workspace、media/exports、项目 hooks/workflows 与真实 HOME 无写穿，并逐端点覆盖 `/ws/agent`、设备列表/状态、relay、preview、metrics、MCP/飞书和 admin 的越权矩阵。`/data/home` 只能兜底，不能用其存在来掩盖真实 HOME 写入。

### 5.3 token 体系（三种，签发方/持有方/泄露面各不相同）

| token | 签发方 | 持有方 | 形态与生命周期 | 用途 |
|---|---|---|---|---|
| **账号 JWT**（access + refresh） | 网关 auth.py | 浏览器 localStorage / App Keystore 安全存储 | access 30min，含 auth_epoch；refresh 30d 轮换、DB 存哈希可吊销 | 登录态；所有 REST/WS/relay 均检查 disabled/role/auth_epoch |
| **worker token** | 网关（账号开通时生成并预写账号卷 config） | **仅网关/Caddy 受控注入点**（workers 表明文）+ worker 自身 config | `token_urlsafe(24)`，TTL=0 永久；轮换=重写 config+重启容器 | LanServer、metrics 与内部预览鉴权；浏览器/App 不持有，P1 仅 WS 配对 token 是独立过渡例外 |
| **设备 token** | 网关管理后台（admin 操作生成） | 用户 Windows 桌面 config.json（credentials 加密写盘） | `token_urlsafe(32)`，DB 存 SHA-256；可吊销、可轮换，吊销即 close 4401 | 只能注册 `/relay/device`。不能调 REST，不能连 `/ws/agent`。**能冒充这台电脑并接收主人 App 随后发出的 RPC**，见 6.2 |

现有 LAN 配对 token 机制（桌面自签自发、二维码、全量轮换，lan_server.py:495-517）在 Windows GUI 本地场景**原样保留**；服务器 worker 场景签发权移到网关；「我的电脑」场景由设备 token 取代（桌面不再向手机出示二维码，配对关系在网关设备注册表）。

---

## 6. 安全设计

### 6.1 信任边界图

```mermaid
flowchart LR
    subgraph untrusted["公网（半信任：主人+受邀者）"]
        C["浏览器 / App<br/>持账号 JWT"]
    end
    subgraph edge["边界 1：TLS + 网关鉴权"]
        G["Caddy TLS → gateway<br/>JWT 校验 / 设备 token 校验<br/>唯一公网入口"]
    end
    subgraph sandbox["边界 2：容器沙箱（账号间隔离）"]
        W["worker 容器<br/>非 root · 限额 · 独立卷<br/>LanServer 黑名单 + turn_source=server"]
    end
    subgraph home["边界 3：用户桌面（用户自己的机器）"]
        D["星瑶 GUI + 云中转<br/>dispatch_rpc 黑名单<br/>turn_source=lan 远程禁令<br/>权限确认卡人工拍板"]
    end
    C -- "WSS（JWT）" --> G
    G -- "内部 docker 网络<br/>worker token" --> W
    D -- "出站 WSS（设备 token）<br/>桌面不开任何入站端口" --> G
```

- 边界 1：公网只有 443；worker 端口不 publish，serve 默认禁用独立 MCP；桌面零入站。P1/P2 必须实际检查 Docker publish、共享网络与 Caddy/网关日志，所有 token/JWT query 键统一脱敏。
- 边界 2：账号间 = OS 级容器隔离；账号内 agent 拥有容器 shell（信任模型接受）。
- 边界 3：App 对桌面的能力 = 现有 LAN 手机能力，一字不多（dispatch_rpc 同一函数）。

### 6.2 每种 token 的泄露面与缓解

| token | 泄露途径 | 影响 | 缓解 |
|---|---|---|---|
| 账号 JWT | XSS 偷 localStorage；URL query 泄露 | 冒用账号对话/审批/我的电脑 | WS 一律首帧鉴权；每请求校验 disabled/role/auth_epoch/jti；改密、禁用、降权递增 epoch 并在 5 秒内踢线；登出只废本机 refresh 和本机 jti。失效矩阵见 10.3。SPA 预转义，全站 HTTPS |
| worker token | gateway.db 或账号卷 config 被读；P1 页面持有同一枚 token | 从内部网络直连 worker；P1 时 XSS 也能从浏览器拿走它 | 不 publish；库文件 600。P2 起浏览器/App 不持有。P1 页面持有的就是这枚 token，只许放 sessionStorage，并在界面写明。预览 HTTP 从 P1 起由 Caddy 在 basic-auth 通过后注入，不进图片 URL。P2 删掉直连模式。轮换后重启容器踢线 |
| 设备 token | 桌面 config、粘贴过程或反向代理日志泄露 | **可冒充桌面并接收主人 App 后续 RPC/伪造事件**，并非无影响 | 首帧鉴权、DB 哈希、单连接 generation；吊销/轮换原子更新并 close 4401；账号禁用同时使设备连接失效；注册不授予其他 API |

### 6.3 远程回合限制沿用清单（服务器与我的电脑均生效）

沿用 remote-mobile.md 第三节全部机制，不放宽：

1. **桥方法黑名单 54 项**（lan_server.py:224-364）：窗口控制、桌面文件系统交互、更新安装、电脑操控扩权、`drain_js_queue`、终端写入/销毁、明文凭据读取（`get_provider_api_key`/`get_feishu_secret`）、hook 持久注入、MCP 写配置/启停、插件信任、`add_permission_rule` 持久放宽、`shutdown`、自动化任务链等。服务器与 relay 共用同一 `dispatch_rpc`，自动继承。
2. **消息入口强制来源**：共享 dispatch 显式接收连接级 turn_source，参数裁剪与白名单保留；LAN/relay 默认 lan，serve 才传 server，客户端无法伪造。
3. **turn_source 消费点**：电脑操控禁令（capability.py:96-99；server 来源例外仅 browser target）、跨会话引用置空（agent_service.py:1480-1484）、副模型路由仅 local（turn_model_override.py:163-164）、不抢占桌面会话（gui_bridge.py:9444）。
4. **权限确认**：一次性 `respond_permission_*` 放行、持久白名单被封；服务器侧加超时策略（S4）防挂死；桌面侧保留人工无限等待。
5. **明确不防**（信任模型内接受，App 首次连接提示写明）：远程回合在会话权限档位允许下可执行命令、读写文件（remote-mobile.md 3.4）；会话级 `set_permission_mode` 可切 full（lan_server.py:843-852 只封全局分支）。
6. 异常不外泄内部路径（lan_server.py:940-944）；凭据文件预览永远拒绝（gui_path_policy.py:101-122）。

---

## 7. 分阶段实施计划

**贯穿约束（R1/R2 零回归）**：口径见第 10.6 节。基线提交固定为 `dcb3bc01`。候选版本的门槛是「没有新增失败」，不是「全绿」。既有失败只有在同一个测试节点、同一类错误上才豁免。GUI、CLI 交互、CLI headless 分开记。Linux 或安卓的结果不能代替 Windows。

### P1：服务器单账号链路（验证核心可行性）

任务按这个顺序做，前一项没过不要并行铺下一项：
1. **P1a0 骨架（第一个可交付切片）**：S1 与 credentials.py:9 的平台隔离 → `import core.credentials`、`import gui_bridge` 在 Linux 不抛 → `--serve` 监听 127.0.0.1:8766 → `/health` 返回 JSON → S2 让无窗口的 `_emit_raw` 把事件送到 WS → S4 无客户端立即拒绝 → S6 文件锁 → S7 固定端口 → S8 路径 → MCP 不监听 8765 → tokenrhythm 的 server 档禁用。S10 诊断开关保持默认关闭。
2. **P1a1 浏览器链**：先做连接级 `turn_source=server`（M08）。没做之前，浏览器能力会被打成 `lan` 然后恒拒。然后做工厂与 chromium 白名单（M06），再做空配置默认只开 browser、server preflight 不要求热键（M07），然后 headless 点击/截图、preview、同源取图、SIGTERM 后按进程组清理 Chromium。
3. **P1b Web**：P1a1 通过之后才做 `/chat`、`/approvals`、围观面板，并改两个既有 shim。页面进度不能代替 P1a0/P1a1。
4. 部署按第 10.2 节的 Caddy 与 Dockerfile。预览由 Caddy 验权后注入 worker token。P1 的 WS 直连仍会让页面持有 worker token，这是写明的例外，不是「浏览器拿不到」。
5. server 档默认权限已经定为 `ask`（第 9 节）。P1 出口做 Key/keyring 最小备份恢复。S13 用量留到 P2。

验收标准：
- P1a0：空卷启动后进程不因 import 退出；`GET /health` 得到 `{"ok":true,"boot_id":"..."}`；测试客户端能收到一条无窗口广播；没有 WS 客户端时权限请求立即拒绝且只结算一次；`ss` 或等价命令看不到 8765。Windows 对照第 10.6 节无新增失败。
- P1a1：空 `config.json` 完成 headless Chromium 打开/点击/截图，调用链经过 `_default_backend_factory` 到 browser worker；真实 `<img>` 取图 200，错误凭据 401/403，日志里没有 token。SIGTERM 后没有 Chromium 残留。
- P1b：对话、工具、审批、中断、重连可用。worker 重启覆盖 boot 不匹配、同 boot 空洞、旧服务端无 hello、重复补发。空洞只验收 `resync_required`，不宣称历史事件不丢。
- 容器重启及第 10.5 节的最小备份恢复后，会话、记忆、Key 可用，卷内没有明文 Key。

风险：wintypes/frameless 之外可能还有未发现的 Linux import 崩溃（审计为静态推断）→ 预留排查时间，逐个 import 二分定位；headless Chromium 在容器内的字体/沙箱问题 → --no-sandbox + fonts-noto-cjk + shm 1g 预案；keyring file 后端兼容性 → 兜底方案 env 注入 Key。

### P2：多账号网关 + 账号隔离 + 管理后台

任务：
1. 模型 Key 归属和 server 默认权限已在第 9 节定稿，P2 按那个做，不再重开。先实现空库 init-admin、JWT 的 disabled/role/auth_epoch/jti、全端点隔离矩阵和 usage_watermarks 单事务，再做管理 UI。
2. gateway auth/db/workers/ws_agent/admin 与 Caddy；web 切 JWT，P1 配对 token 过渡例外全部收回；预览改由网关 JWT 鉴权后注入 worker token。
3. /metrics + 用量聚合 + 空闲回收；账号卷/gateway.db/worker token 的备份语义入口冻结、出口恢复演练。
4. dispatch/EventReplayBuffer 的纯抽取及 Windows LAN 回归最迟本阶段完成，Q2 新行为留 P4。

验收标准：
- 空库只能本机 init-admin；禁用/改密/降权后旧 access/refresh/WS/RPC 按矩阵失效，非 admin 管理请求 403。账号禁用即踢线停容器，worker/网关重启与空闲回收语义正确。
- A/B 同名会话、不同消息/Key/记忆/文件全隔离，agent/devices/relay/preview/metrics/MCP/飞书/admin 均做越权反例；workspace 与真实 HOME 无写穿。Web 换号/切服务器/profile 无前一账号缓存与水位残留。
- 用量 fixtures 覆盖 worker/网关重启、轮询后提交前崩溃、换卷/清库回退和跨午夜，水位与 usage_daily 同事务；turns 来自持久回合，不把请求数冒充回合，不展示无来源的按日 sessions。
- 管理四页在后端契约通过后验收；账号卷/gateway.db/worker token 恢复演练完成。
- 全量 pytest + 网关单测（JWT/路由/隔离）+ 双账号集成测试通过；Windows 零回归冒烟。

风险：Docker SDK 生命周期竞态（并发首连重复起容器）→ 按 account_id 加锁；SQLite 并发写 → WAL + 单进程网关足够；JWT 过期与 WS 长连的生命周期错配 → 4401+refresh 重连链路要有集成测试。

### P3：Capacitor 安卓 App

任务：开工前在锁文件里写上所选 Keystore 插件的包名和版本。水位语义已经定了：安卓不持久化 `(boot_id,last_seq)`。先做安装、WSS/refresh、后台、断网、杀进程、Keystore 与大帧，再做视觉。退出、换号、切服务器时删掉凭据、缓存和水位。不做前台服务，不做厂商保活。

验收标准：
- APK 真机安装，登录→对话→工具卡→权限审批→围观截图全链路可用。进程还在时，断网或切后台重连用内存里的 last_seq 补发。进程被系统杀掉之后，水位不在了，重开必须走 `get_initial_state`，不能靠 WebView 恢复出来的 localStorage。登录态在 Keystore 里，杀进程后仍在。
- 与浏览器端同一账号并发使用互不干扰（每连接独占上游）。
- 首版无原生业务代码（Kotlin 仅 Capacitor 模板）。

风险：Android WebView 对 WSS/大帧（70MB 上限）的支持差异 → 上传大文件真机实测；厂商后台杀进程 → 首版接受「回前台重连」语义，保活列后续。

### P4：云中转「我的电脑」

任务：core/cloud_relay/（service/client/replay）+ C1-C6 桌面接线 + gateway/relay.py（注册表/配对/id 映射/补发/在线推送）+ web/ 与 App 的 desktop profile + 设备管理页联动。

验收标准：
- 桌面 GUI 开启云中转后，App「我的电脑」显示在线；手机发消息 → 桌面 GUI 可见回合执行（turn_source=lan，电脑操控被禁）→ 事件流回 App；权限卡两端均可应答。
- 桌面 GUI 关闭 → 网关 45s 内标记离线并推送 App（心跳 15s + 容差）；重开自动恢复在线。
- 桌面 relay 断开 60s 且 App 始终保持网关连接：桌面重新上线推 device_online，窗口内离线帧按序补发且去重；GUI 重启触发 device_reset 与快照自愈。主动构造 128 帧溢出/队列丢弃，必须 resync_required 并全量自愈，不得无条件声称不重不漏。
- 设备 token 吊销/轮换即时踢线；单连接 generation 使旧桌面失效，冒用者先注册与旧新桌面竞争有确定结果；两 App RPC id 不串，保留窗口内补发正确。
- dispatch_rpc 黑名单在 relay 链路逐项抽测（至少覆盖凭据读取/hook 注入/shutdown/终端写入）；全量 pytest + Windows 零回归冒烟（云中转默认关闭，关闭时 GUI 行为与现状一致）。

风险：EventReplayBuffer / dispatch_rpc 抽取会碰到 LAN 主链路，所以现有 LAN 测试要全量回归，并用 Windows 真机做一次手机配对。企业代理和防火墙按第 10.8 节，不在 P4 再发明第三套规则。

---

## 8. 明确不做（YAGNI）

- **Kubernetes / 跨机调度 / 多 worker 负载均衡**：单机 Docker 足够。
- **原生 Kotlin App**：Capacitor 壳 + SPA；推送/保活等原生增强按需后补。
- **SaaS 级多租户防护**：egress 白名单代理、注册风控、计费防刷、对抗性容器加固、渗透测试——信任模型不成立时才做（1.3 已列回收清单）。
- **CDP screencast 视频流围观**：首版截图事件流（computer_observation）够用，视频流列后续增强。
- **进程内多租户**：审计已判死（全局单例串号），永远不做。
- **iOS App、桌面独立常驻服务（GUI 关闭仍可连）**：前者无需求；后者接受「GUI 在跑才可连」语义，托盘常驻服务列为远期可选。
- **计费系统、公开注册、邀请码风控**：账号由管理员手工创建。
- **Linux desktop 电脑操控（X11/Wayland 输入）**：linux 适配器只做 browser target，desktop 保持抛 computer_unsupported_os。

## 9. 已定决策

这些原来是开放项。r3 直接定死，避免 P2 开工时再分叉。若产品主人要改，改完再动工，不要边做边改。

1. **模型 Key：各填各的。** 开通账号不复制主人的 Key。受邀者在自己的设置页写入自己的 Key，进自己的 keyring 文件。用量记在该账号上。主人不想让对方自备 Key 时，由主人把 Key 粘进对方账号，这是一次人工复制，不是平台的共享密钥功能。
2. **server 档默认权限：`ask`。** Web 上审批。会话级可以切到 `full`，全局档位仍被 `dispatch_rpc` 拒绝。
3. **tokenrhythm**：server 档禁用。以后要开，再用 keyring 文件存会话，不在 P1 做。
4. **鉴权位置**：网关的 REST 用 `Authorization` 头；网关的 WS 全部首帧，长期 token 和 JWT 不进 URL。唯一例外是 P1 直连 worker 的 `?token=`，P2 删除。日志规则见 10.2。
5. **截图带宽**：P1 只记录真实 PNG 大小。P3 开工前再决定要不要降采样。首版保持 PNG。
6. **备份**：规则已写在 10.5，不再推迟到阶段入口「再冻结」。
7. **一台电脑同时连着的 App ≤ 4。**
8. **P1 公网**：HTTPS + 站点级 basic-auth。basic-auth 是站点门锁，不是账号隔离。P2 改为 JWT，并删掉 WS 配对 token。

---

## 10. 施工契约

本节覆盖前面点了名但没写死的部分。实施时按这里写代码和配置。下面的命令和配置是契约，**未在本环境运行过**。

### 10.1 worker 怎么被找到

- 容器内 `LanServer` 固定 `bind=0.0.0.0`、`port=8766`。不要用 `port=0`。8765 留给现在的 MCP 默认端口，serve 默认不听它。
- 同一端口提供 HTTP 和 WS，路径是 `/`。`GET /health`、`GET /metrics`、WebSocket 都在 `8766`。
- Docker 网络名 `xingyao`，不 attach 到默认 bridge 以外的公开网络。容器 DNS 名等于容器名 `xingyao-worker-<account_id>`。
- 网关连接 `http://xingyao-worker-<account_id>:8766` 和 `ws://xingyao-worker-<account_id>:8766/`。P1 单账号时 compose 里的服务名就用 `worker`，Caddy 的上游写 `worker:8766`。
- `docker start` 之后最多等 60 秒。超时则这次连接失败，推 `worker_lost`，不在后台无限重试。
- worker 容器不挂 `/var/run/docker.sock`。只有网关进程需要它。

### 10.2 Caddy

P1 用下面这份作为起点。域名、邮箱和 basic-auth 哈希由部署者填，不进 git。实施时用 `caddy adapt` 检查语法；本文件没有跑过 Caddy。

```caddyfile
{
    email {$ACME_EMAIL}
}
{$SITE_DOMAIN} {
    encode gzip
    basicauth {
        {$BASIC_USER} {$BASIC_HASH}
    }
    log {
        output file /var/log/caddy/access.log
        format filter {
            wrap json
            fields {
                uri query {
                    replace token REDACTED
                    replace jwt REDACTED
                }
            }
        }
    }
    handle /computer-preview/* {
        reverse_proxy worker:8766 {
            header_up Authorization "Bearer {$WORKER_TOKEN}"
        }
    }
    handle {
        reverse_proxy worker:8766
    }
}
```

必须满足的行为，而不是「文件里有这几行」：

- 公网只开 443。`docker port` 看不到 8766 和 8765。
- 未通过 basic-auth 的 HTTP 和 WS 都进不了 worker。
- 预览请求由 Caddy 加上 worker token。浏览器地址栏和页面 JS 里没有这枚 token。
- 访问日志里不出现 `token=`、`jwt=` 的值，也不出现 `/computer-preview/<token>/` 里的那段 token。若所用 Caddy 版本的 filter 打不掉路径段，就改成不记录 `/computer-preview/` 的访问行，并加一条测试证明日志文件里搜不到已知 token。
- P1 的 WS 仍可能由页面自己在首帧送出 worker token。这条例外只存在于 P1，日志同样要打码。
- P2 把 `basicauth` 换成网关，上游变成 `gateway:8080`。预览先由网关验 JWT，再由网关注入 worker token。

### 10.3 账号失效矩阵

| 动作 | access | refresh | 已建立的 WS | 设备 token / `/relay/device` | worker 容器 |
|---|---|---|---|---|---|
| 本机登出 | 该 jti 进 denylist，立即作废 | 只废这一枚 | 只关这一条 | 不变 | 不变 |
| 禁用账号 | epoch+1，全部作废 | 全部作废 | 5 秒内全关 | 立即 close 4401 | `docker stop` |
| 改密 | epoch+1，全部作废 | 全部作废 | 5 秒内全关 | 不变 | 不重启 |
| 降权 | epoch+1 | 全部作废，重新登录后是 user | admin 的 WS 关掉 | 不变 | 不重启 |

「5 秒内」是网关扫连接表主动关，不是等下一条业务帧。测试要覆盖：禁用后旧 access 调 `/api/devices` 得到 401，而不只是「不能再登录」。

### 10.4 用量

- 指标和 SQL 以 4.2 为准。`turns_total` 只数 `harness_turns.status='completed'` 且 `turn_kind` 不是 `resume` 的行。
- `day` 用 `zoneinfo.ZoneInfo("Asia/Shanghai")` 取提交时刻的日期。同时把网关和 worker 的 `TZ` 设为 `Asia/Shanghai`，避免日志和入账日差一天。
- 空模型写成 `""`。模型字符串变了就当成新行，不改历史。
- 轮询读到指标之后、事务提交之前崩溃：下一次用同一水位重算，不能出现两行增量。测试用注入的失败点证明这一点。

### 10.5 备份和升级

备份集合：

- 每个账号卷：`/data/config.json`、`/data/ycode_gui.db`（含 wal/shm 先 checkpoint）、`/data/keyring/`、工作区目录。
- 口令文件：`/run/secrets/keyring_password` 的离线副本，和卷分开存放。
- P2 起加上 `gateway.db`。里面有密码哈希、refresh 哈希、设备 token 哈希、worker token 明文。文件权限 600。

恢复：

- 停掉对应容器或网关，把文件放回原路径，用同一口令启动。
- 恢复是时点替换。恢复之后所有 WS 断开，用户重新登录。进行中的 RPC 直接失败，不自动重放。
- worker token 以恢复出来的 config.json 和 gateway.db 为准。两边不一致时以 gateway.db 为准，由网关再写回 config 并重启该容器。
- 已吊销的设备 token 以恢复出来的 `gateway.db` 为准。旧库会把后来的吊销「倒回去」，这是时点恢复的结果，要在运维说明里写一句，而不是假装吊销永存。
- P1 的最小演练：写入一个模型 Key，停容器，只拷走上述文件和口令，换一个空目录启动，Key 能解密，卷内文件搜不到明文。
- 升级：账号卷沿用现有 `MemoryManager` 的建表和 `ALTER`（memory.py 里已有）。网关只跑 `schema_migrations` 的向前脚本。迁移失败则进程以非 0 退出，不接流量。镜像标签等于 `core/version.py` 的版本号。换镜像时停容器、换镜像、原卷启动，不新建空卷。

桌面 GUI 的 `~/.ycode` 和服务器账号卷不是同一份数据。首版不做同步。要搬家就停应用后手工拷数据库，不在本方案里做自动迁移。

### 10.6 Windows 零回归怎么记

基线提交：`dcb3bc01`。同一台 Windows、同一套已安装依赖，不在本方案里新装包。

采集：

- `python -m pytest tests/ -q --tb=line`，保存失败节点和错误类型。
- `frontend` 与 `frontend-mobile` 各自的现有测试命令，同样保存失败节点。
- 记录 Python 版本和两条前端的 lock 文件哈希。

豁免：候选版本里，失败节点和错误类型都落在基线集合内，才算既有失败。新出现的失败、从通过变成失败、从失败变成另一种错误类型，都算新增失败。跳过原来通过的测试也算新增失败。门槛是没有新增失败。基线本身有失败时，不要求先修到全绿。

CLI 分两次，用假模型和临时 `YCODE_DATA_DIR`：

- 交互：`python gui_main.py --cli`，脚本喂一行消息、权限提示回答 `n`、再退出。比对退出码、关键 stdout/stderr、会话库里有没有那条用户消息。
- headless：`python gui_main.py --cli -p <固定提示> --session <名>`。比对退出码和输出。
- 再各跑一次 `YCODE_TOOL_PROFILE=cli`。未设置 profile 时工具表必须仍是 `full`。新增的 `server` 档不得改变上面四组结果。

GUI 至少覆盖：启动到窗口出现、新建会话、发送一条消息并中断、权限卡允许/拒绝、LAN 配对后断开再连、Windows 上 headed 浏览器能打开、DPAPI 凭据重启后仍能读、关闭窗口过程中不再向 LAN/飞书送事件。S2 改动后加三组：有窗口时顺序仍是序列化 → JS → LAN → 飞书；无窗口且未关闭时 LAN/飞书收到、JS 不入队；关闭中三个 sink 都不调用。

这些验收都还没跑。写「通过」时必须附上命令输出，不能引用本文。

### 10.7 安卓

- 同一套 SPA。运行时若存在 Capacitor，水位只用内存变量，禁止 localStorage 和 Preferences。杀进程后没有水位，重开调用 `get_initial_state`。
- 切到后台不断言连接还在。回到前台如果 WS 已死，按普通重连。不做前台服务，不引导用户关闭电池优化。
- JWT 和 refresh 只进 P3 选定的 Keystore 插件。服务器地址可以进 Preferences。
- 首版 APK 自己安装，不进 `core/updater.py`，也不塞进 Gitee Release 的 Windows 安装包附件。

### 10.8 Windows 防火墙、代理、分发

- 「我的电脑」只有出站 WSS。不需要给星瑶加 Windows 入站防火墙规则。公司电脑拦截出站 443 时，本方案不提供绕过办法。
- 代理只认进程能看见的环境变量 `HTTPS_PROXY` / `https_proxy`。不需要用户名密码的代理可以走系统环境。需要账号的代理、以及私有根证书的 TLS 拦截，首版不做配置界面。桌面若已把企业根证书放进系统信任库，就用默认 SSL 上下文；否则连接失败并在云中转卡片上显示错误，不重试到阻塞 GUI。
- Windows GUI 和 CLI 继续用现有更新器：`core/updater.py` 从 Gitee `feng-chenhao/xingyao-y-code` 拉 Release。服务器镜像不走这条链路，在服务器上构建或 `docker load`。版本号只有 `core/version.py` 一处。
- 安卓包的升级是用户重装 APK。不复用 exe 更新器。

### 10.9 仍要产品主人点头的唯一变更

第 9 节已经按推荐项写死。若主人不同意「受邀者自备 Key」或「默认 ask」，只改第 9 节对应两条和设置默认值，其它契约不用重开。

### 10.10 引用修正

`dcb3bc01` 上这几处和 r2 原稿不一致，实施时用下面的位置：

| r2 写法 | 实际 |
|---|---|
| skills.py:650-661 | 文档在 :650-661，`~/.agents/skills` 的 `_add` 在 :682 |
| lan_server.py:412-453 当作监听 bind | :412-453 是 `bind_url`（含 `?token=`）。监听在 :1150，构造在 :379，`start` 返回在 :1145 |
| 会话 revision | `get_initial_state` 没有这个字段。快照在 gui_bridge.py:4503-4517 |
| `turns_total` 未指数据源 | `harness_turns`，状态值是 `completed`（core/harness/models.py:12），不是 `finished` |
| `remote_public_url` | 仓库里还没有这个键，按 S7 新增 |
| command_shell.py:12 | 文件是 `core/command_shell.py`，:12 导入的是 settings |
| gui_browser_mixin.py:6-7 | 那两行是文档字符串；win32 判断在 :48 |

其余 r2 抽样（credentials.py:9 与 :23-24、controller.py:190-192 与 :575、tool_registry.py:182、lan_server.py:912 与 :921、lan_server.py:969-970、settings.py:102-104）在该提交上仍与描述一致。本表是静态对照，没有跑测试。
