# 星瑶 Personal Agent 平台设计第二轮审查（R2）

> 当前状态：R2 静态审查已完成，总体判定为**有条件通过，r1 不能直接实施**。第四节给出阶段依赖与门禁，第五节列出 20 项必改/必验条件；本轮未运行测试、服务、容器或真机验证，任何运行结论均须以后续实测为准。

## 审查对象与基线

| 材料 | 路径 | 本基线读取情况 |
|---|---|---|
| 当前设计稿 | `docs/superpowers/specs/2026-11-13-personal-agent-platform-design.md` | 分段完整读取，共 579 行；文首 :3 标注为修订 r1 |
| 第一轮设计审查 | `docs/superpowers/specs/audit/design-review.md` | 分段完整读取，共 104 行 |
| 核心服务化审计 | `docs/superpowers/specs/audit/core-service.md` | 分段完整读取，共 283 行 |
| 远程链路与移动端审计 | `docs/superpowers/specs/audit/remote-mobile.md` | 分段完整读取，共 296 行 |
| Linux 部署与浏览器审计 | `docs/superpowers/specs/audit/platform-deploy.md` | 分段完整读取，共 204 行 |

基线说明：

- 第一轮报告 :3 记录其审查对象为 573 行；当前 r1 设计稿为 579 行。第一轮报告中的设计稿行号属于旧版上下文，本文件“附录 A”的行号均按当前 579 行版本记录，二者不得混用。
- Q1/Q2/Q4/Q5/Q6 的“原始问题”取自第一轮报告；“r1 声称修复方式”仅按当前设计稿摘录和定位，不代表 R2 已验证成立。
- 三份现状审计及第一轮设计审查仅作为后续审查线索，不直接继承其判定、严重度或“已修复”结论。

## 方法

- 本轮以静态、只读方式完整阅读上述五份文档，并直接打开相关源码核验协议生产端、消费端、调用方与反例路径；除本报告外，未修改设计稿、源码、审计报告或其他文件。
- 附录 C 记录本轮直接源码抽查；新增抽查与第一轮十处位置错开，为验证 Q 项而必要重读的重叠区间单独标明，不把重复阅读包装成新增抽样。
- 本轮未运行任何测试、服务、Linux 容器或安卓真机验证，未进行导入实验、构建、依赖安装或网络验证。
- 结论区分“文档已写明”“源码静态支持”“运行实测通过”三种证据强度，不把设计稿的声称当作实现事实；所有运行语义均为静态判断并标注未实测。
- 第一轮报告与三份现状审计仅用于定位线索，最终结论以本轮实际读取的设计稿、源码和报告所引证据为准。

## 第一轮第二节的 10 处 `file:line` 抽查清单（供后续错开）

下表照录第一轮报告第二节的引用与核对焦点，作为本轮新增抽查的避让清单；必要的重叠复核已在附录 C 末尾单独标明。

| # | 第一轮原引用 | 第一轮核对焦点 / 附带涉及区间 |
|---|---|---|
| 1 | `frameless.py:101` | 模块级 `ctypes.windll.user32` 与 :102-117 绑定；另涉及 :34 `wintypes`、:97-99 `WINFUNCTYPE` |
| 2 | `gui_bridge.py:3810` | `_shutting_down or not _window` 早退；另涉及 LAN 广播 :3824-3826、飞书 :3829-3831 |
| 3 | `credentials.py:9` | 顶层 `from ctypes import wintypes`；另涉及 :51-56 keyring 分支、:23-24 `_DataBlob` |
| 4 | `lan_server.py:1150` | 恒 `host="0.0.0.0"` |
| 5 | `capability.py:87-103` | CLI 硬禁、远程来源禁令与平台不支持文案 |
| 6 | `browser_backend.py:1212` | `headless=False`；另涉及 :1209-1215 launch 无 `args` 参数 |
| 7 | `tool_registry.py:182` | `VALID_TOOL_PROFILES=("full", "cli")` |
| 8 | `config.py:430` | `YCODE_DIR` 的 env/HOME 来源；另涉及 :450-451 两个 remote pair token 配置键 |
| 9 | `settings.py:124-137` | 第一轮注明区间语义偏移；实际另核对 :26、:37-41、:137 的浏览器默认值、定义与 embedded 平台判定 |
| 10 | `main.py:6381-6390` | `_deny_permission_request` 自动拒绝与 `set_prompt_handlers` 的 headless 权限策略范式 |

## 一、r1 修订验证

**状态：已完成本轮静态复核。**以下“设计稿 :N”均指当前 579 行 r1 文件；源码 `file:line` 均由本轮实际打开核对，三份现状审计只用于找线索，不作为最终证据。本节全部结论均为**静态审查，未运行项目测试、服务、Linux 导入实验、构建或真机验证**。“修好了”也只表示修订文本回应了第一轮问题，不表示现有源码已经实现或运行验收通过。

| Q 项 | 修订判定 | 文本闭合度 | 实测状态 |
|---|---|---|---|
| Q1 | **没修好** | 部分闭合：有 epoch 方向，但握手字段、控制帧、shim 持久化及网关时序仍未闭合 | 未实测 |
| Q2 | **没修好** | 未闭合：注册帧方向冲突，桌面离线恢复存在确定反例 | 未实测 |
| Q4 | **修好了** | 文本闭合；实现与关闭期回归仍需实测 | 未实测 |
| Q5 | **修好但引入新问题** | 原遗漏已补齐，但“守卫 wintypes”与“Structure 原样保留”互相冲突 | 未实测 |
| Q6 | **修好但引入新问题** | DB 累计方向可行，但水位落库、逐字段口径、按日与回合数语义未闭合 | 未实测 |

### Q1：worker 重启后的 seq epoch —— 没修好（文本部分闭合，需实测）

**本轮源码事实。**`core/lan_server.py:379-406` 证实 `_event_seq` 只在进程内从 0 初始化，当前没有 `boot_id`；`:528-557` 的注册/补发只接收 `last_seq` 并筛选 `seq > last_seq`，`:756-788` 的 WS 首帧只解析 `token`、`last_seq`，认证成功后直接补发，没有 `hello` 或 `seq_reset`；`:950-971` 的 `/health` 当前只是纯文本 `ok`。因此设计稿 :148 的 `boot_id`、`hello`、`seq_reset` 都是**待新增协议**，不是对现有字段的调用。`get_initial_state` 则真实存在，是 `gui_bridge.py:4377-4535` 的桥方法，可经现有 RPC Proxy 调用；其快照包含活跃会话/回合、排队消息等（`:4503-4517`），不是事件流本身。

**r1 仍有四个协议缺口。**

1. 设计稿 :145 的上游首帧仍只写 `{"token", "last_seq"}`，:148 才说重连“两者同带”，但没有把客户端→worker 的精确形状统一为 `{"token", "last_seq", "boot_id"}`；`seq_reset` 更没有说明是独立 `type`、布尔字段还是 `data` 字段，也未规定 `hello → seq_reset → replay → live` 的顺序。新增控制帧不得带 `id`，否则现有 shim 会优先按 RPC 应答处理（`frontend/mobile-shim.js:89-107`）。必须补齐逐方向、逐字段、逐时序契约，并让旧服务端“无 hello”分支与新服务端“旧客户端无 boot_id”分支都有明确行为。
2. 设计稿 :146 的冷启动顺序是先推 `worker_starting`、再等 `/health` 通过；:148 却要求 `worker_starting`/`worker_lost` 都携带从 `/health` 读取的 boot_id。启动前无法读到新 boot_id，崩溃后也未必能再读旧值。应明确网关缓存最后一次健康快照，`worker_lost` 使用缓存旧 epoch，`worker_starting` 允许空值/不带新 epoch，真正的新 epoch 以 worker `hello` 为准；同时把 `/health` 从纯文本改为 JSON 的响应契约写进 :182，而不能只说“现有 /health”。
3. 现有 `frontend/mobile-shim.js:48-75` 的 `lastSeq`、`hadConnection` 都是页面内存态，只发送 token/last_seq；`:89-116` 对任意带 `type` 的 JSON 帧都会透传给 `window.onBridgeEvent`，未知类型不会触发解析错误，但也不会重置水位或调用 `get_initial_state`。消费端同样如此：移动端桥只扇出（`frontend-mobile/src/bridge/index.ts:57-83`），store 对未知类型最终自然忽略（`frontend-mobile/src/stores/app.ts:637-655,850-915`）。所以新增 `hello`/`seq_reset` 对旧 shim 只能算“不会破坏 JSON 分发”，**不能算功能兼容**。新 gateway-shim 必须在通用扇出前拦截控制帧；若要让设计稿 :148 所称 Windows LAN 直连同步受益，还必须同时修改磁盘版 shim 与内嵌兜底副本，后者由 `core/lan_server.py:62-67,1015-1036` 证实是两套实际分发内容。设计稿 :262、:276 只写新 web/、不改既有 frontend，未安排这两处改造。
4. 设计稿 :148 写“shim 持久化 `(boot_id,last_seq)`”，P3 :538 却又明确“shim 水位内存态、杀进程重开为全新会话”。二者不能同时成立。应拍板跨页面/跨 App 进程是否保存水位，并定义持久化键按账号与 profile 隔离；若恢复旧水位，还要规定初始快照与补发/实时事件的先后合并，不能同时把杀进程重开写成“无水位全新会话”。

**还需纠正一处现状描述。**现有 shim 对 `seq <= lastSeq` 的消息只是“不回退水位”，仍然派发（`frontend/mobile-shim.js:107-115`），并非设计稿 :148 所说“旧水位吞掉全部新事件”。确定的丢失反例是：worker 重启后旧水位仍很高，之后某次断线重连时 `:553-556` 会把新进程中 `seq <= 旧水位` 的缓冲事件全部过滤掉。报告与验收应按这个真实机制表述。

P1 :511、P2 :527 已补 worker 重启验收，这是正确增量，但还需覆盖 boot_id 不匹配、hello/reset/replay 顺序、旧服务端无 hello、杀进程后的已拍板语义及重复补发。**必要修正：先统一上述协议与 shim 改造清单，再保留重启用例；当前不能判定 Q1 已闭合。**

### Q2：桌面断线、序号与重连状态 —— 没修好（文本未闭合，需实测）

**注册帧方向自相矛盾。**设计稿 :316 明确 `relay_registered` 是“网关→桌面”的注册成功回包，字段只有 `{device_id,name}`；:320 却让同一帧携带桌面 GUI 的 `boot_id`，供网关检测 GUI 重启。网关回包不能同时充当桌面→网关的 epoch 上报。应拆成桌面注册请求（携带 device token、`boot_id`）与网关注册应答（保留 `relay_registered{device_id,name}`），并另定网关→App 的 `device_reset` 精确形状，例如 `data:{device_id,boot_id}`。当前仓库没有可复用的 `CloudRelayClient`/`EventReplayBuffer` 实现，不能把这些名字当作现有能力；现有可抽取的是 `core/lan_server.py:698-718` 的打标缓冲逻辑。

**桌面 relay 断线存在确定反例。**设计稿 :320 同时声称：① GUI 不重启时 seq 续增，App 带旧 last_seq 可补发；② 设备离线期间事件不在任何缓冲；③ App 重连走状态自愈。若桌面 relay 断开 60 秒而 App 与网关的连接一直在线，期间桌面事件按②不可补发；桌面以同一 boot_id 回来后不会触发 `device_reset`，App 也没有发生重连、不会再次提交 last_seq 或主动全量自愈。于是 P4 :551 的“App 带旧 last_seq 补发不重不漏”没有触发路径，离线期间状态可永久陈旧。

现有代码还说明“共享缓冲”不能含糊处理：`core/lan_server.py:720-750` 即使客户端列表为空也会先给白名单事件打 seq 并入缓冲（`:732-735`），并非“桌面没发就没进缓冲”。抽取到桌面 relay 后，必须明确离线时是否仍打标入桌面缓冲、重连后是否上传这些帧、网关怎样处理序号空洞；否则同一句“没发就是没发”与共享现有语义会导向不同实现。

**必要修正（二选一并写全时序）。**方案 A：桌面离线也继续打标缓冲，relay 重连后按协议补传，定义 128 帧溢出和序号空洞时的重置；方案 B：离线事件明确丢弃，但每次桌面 relay 重新注册都触发所有配对 App 的水位重置与 `get_initial_state` 自愈，即使 boot_id 未变，并明确网关/App 如何发起和完成 resync。无论选哪种，都要区分“App 自身重连补发”和“桌面重新上线自愈”。P4 :551 应增加“桌面离线期间产生状态变更、App 始终保持网关连接”的反例，不能只验双方同时重连的正常路径。

### Q4：保留关闭期早退 —— 修好了（文本闭合，需实测）

设计稿 S2 :199 已明确“保留 `_shutting_down` 早退不变，仅移除 `not self._window`”，并限定窗口存在才 `_queue_js_payload`，非关闭期才执行 LAN/飞书/后续云中转广播，正面回应了第一轮歧义。

本轮源码核对：`gui_bridge.py:3801-3807` 的 `emit_event` 已先在关闭期返回，`:3809-3831` 的 `_emit_raw` 又以 `self._shutting_down or not self._window` 早退；非关闭期顺序为序列化（`:3812-3820`）→桌面 JS 队列（`:3821`）→LAN（`:3822-3826`）→飞书（`:3827-3831`）。按 S2 仅移除窗口条件、给 JS 入队加窗口判断，能保留关闭期静默与现有广播顺序，未发现 r1 新引入的文本矛盾。

**必要修正：无。**实现验收应分别断言：无窗口且非关闭期时 LAN/飞书收到而 JS 不入队；关闭期时所有 sink 均不调用；有窗口时维持上述顺序。设计稿 :221 已安排无窗口广播测试，:499 的 Windows 回归仍须实际执行。本节未运行这些测试。

### Q5：frameless Linux 导入守卫 —— 修好但引入新问题（文本部分闭合，需实测）

第一轮指出的遗漏已经写入设计稿 S1 :198：`wintypes` 导入、`WINFUNCTYPE`、`windll` 与函数内 `dwmapi` 均进入改造范围。本轮直接核对源码，`core/frameless.py:34` 确有顶层 `from ctypes import wintypes`，`:97-117` 确有模块级回调类型与 user32 绑定，`:186-245` 的 DWM 调用位于 `suppress_frame_border` 内；P1 :504 也保留了 Linux 导入实测安排。这部分修订有效。

但 S1 同一句要求把 wintypes 守卫化/惰性化，同时让“`_MONITORINFO` 等纯 Structure 保持平台无关”，与实际依赖冲突：`_MONITORINFO` 的字段在 `core/frameless.py:75-81` 直接使用 `wintypes.DWORD/RECT`，`_MINMAXINFO` 在 `:84-91` 使用 `wintypes.POINT`。若 Linux 上简单跳过 wintypes 导入而类定义原样执行，仍会在类创建时缺类型；若连类一起守卫，又要处理设计稿已指出的 `gui_bridge` 顶层 `_MONITORINFO` 导入。不能把这两个 Structure 当作不依赖 wintypes 的纯 `ctypes.Structure`。

**必要修正：**明确选择一种可导入结构：要么为 Linux 提供等价的平台中立 ctypes 类型/RECT、POINT 定义，从而保留 `_MONITORINFO`；要么把依赖 wintypes 的 Structure 一并平台隔离，并同步处理所有顶层导入与类型引用。P1 实测除 `import gui_bridge` 成功外，还应确认 Windows 原生行为不变及 Linux 不触发任何惰性绑定。以上 Linux 失败机制仍为静态判断，本轮未实测。

### Q6：/metrics 累计与网关水位 —— 修好但引入新问题（累计方向可行，文本未完全闭合，需实测）

**DB 全量累计是可实现的，且应明确其统计边界。**`core/memory.py:959-977` 证实 `token_usage` 是逐请求流水表，真实字段为 `prompt_tokens`、`completion_tokens`、`cached_tokens`、`cache_write_tokens`、`total_tokens`、`session_id`、`provider`、`model`、`api_base`、`cache_scope` 等，不是现成累计行。记账链为 `core/llm_diagnostics.py:976-1067` 校验并调用持久化，`:718-748` 经 `app_context.get_manager()` 写入，`core/memory.py:1464-1500` INSERT 后 commit；GUI 使用 `config.py:435-439` 的 `ycode_gui.db`，并在 `gui_bridge.py:1804-1807,1936-1940` 把同一 MemoryManager 注册给 app_context。按模型对全表 `SUM(prompt_tokens/completion_tokens/total_tokens)` 可以跨 worker 重启保持累计；删除会话也仅把流水改挂墓碑而不删除（`core/memory.py:3873-3904`）。无效 usage 会被拒绝（`core/llm_diagnostics.py:985-1002`），持久化异常则被降级吞掉（`:749-753`），所以“不丢不重”最多指**成功落入该表的有效记录**，不能承诺与厂商账单绝对一致。

现有 `query_token_usage_by_model` 也证明聚合不是简单单键：`core/memory.py:1702-1737` 按 `(provider,model,api_base,cache_scope)` 分组，返回名为 `prompt/completion/total` 的字段；`:1739-1769` 的按日查询另有 `requests`。r1 若决定 /metrics 只按 `model` 汇总，必须显式合并这些路由/scope，并定义 metrics 字段与 usage_daily 的映射，不能直接假设现有查询返回形状就是 metrics 契约。

**r1 新增算法仍有四个缺口。**

1. 设计稿 :183 要求网关重启后从 DB 恢复 `(account_id,model)` 水位，但 :406-414 的 schema 只有 `usage_daily(account_id,day,model,input_tokens,output_tokens,turns)`，没有保存上次累计值的表或列。“在 usage_daily 旁存”不足以实现恢复。应新增明确的水位表/列，并把“读快照→计算各字段增量→写 usage_daily→更新水位”放入同一 SQLite 事务；水位至少要分别保存 prompt、completion、total，而不是一个未指名字段的“当前累计”。
2. `input_tokens/output_tokens` 与源表 `prompt_tokens/completion_tokens` 的映射、模型改名/空模型、总量回退时各字段如何一起重置都未写明。P2 :527 只写 worker/网关重启，不覆盖换卷/清库回退、轮询后提交前崩溃及多字段口径，需补对应验收。
3. 全量累计差分只能保证总量追平，不能保证真实消耗日。worker 或网关离线跨午夜后，下一次轮询会把整段增量记到入账日；这与 :139、:363 的“按账号/日”展示存在歧义。应明确 day 是“网关入账日”还是 token_usage.date 的“实际消耗日”；若要求后者，需要按日期/流水游标取增量，而不能只靠一个全量标量差分。
4. `usage_daily.turns` 没有数据来源。token_usage 一行对应一次通过校验的 LLM 请求，并可含不同 cache_scope（`core/memory.py:1712-1719`），不是用户回合；/metrics :183 只有 active_turn 当前布尔值与 sessions 数，也无法恢复关机期间的回合数。应新增持久的累计回合口径/数据源，或删除首版 turns 承诺，不能用请求行数冒充回合数。

因此，:183 的“DB 全量累计 + 持久水位 + 回退检测”比原稿有实质进展，但当前文本还不能支撑 P2 :527 的完整“不丢不重”验收。**必要修正：补水位 schema 与事务、逐字段 metrics 契约、day/turns 口径，再扩展重启以外的回退和跨日用例。**本轮未构造数据库或运行轮询验证。

## 二、R1-R7 需求覆盖复核

**状态：已完成本轮静态复核。**本节“设计稿 :N”均指当前 579 行 r1 文件。“覆盖”表示产品语义、组件承载、关键协议与验收口径均形成闭环；“部分覆盖”表示已有明确载体，但仍有会妨碍实施或验收的缺口。判定不代表源码已经实现，本轮也未运行 pytest、前端测试、服务、容器或真机，下述验收均为**待执行建议**。第一节已展开的 Q1/Q2/Q4/Q5/Q6 在此只交叉引用；新发现仅列摘要，待第三节展开。

| 需求 | 设计证据（当前 r1 行号） | 覆盖判定 | 具体缺口或矛盾 | 可执行验收建议 |
|---|---|---|---|---|
| **R1 Windows 桌面 GUI 零回归** | 需求 :22；S1-S13 零回归约束 :194-210；浏览器改造 :227-253；云中转默认关闭 :329-340；贯穿验收 :499，P1 :512，P4 :553。 | **部分覆盖** | 已有“全量 pytest + 两个前端现有测试 + Windows 冒烟 + 未设 profile 工具表逐字节对比”的可测骨架，但“全量通过”未定义基线失败集与差分口径，少量冒烟也不能代表“GUI 全部行为”。Q5 的 Structure/wintypes 冲突使 S1 的 Linux 可导入与 Windows 路径不变尚不能按现稿同时落地；Q4 文本已闭合，仍须执行第一节所列关闭期/无窗口/有窗口三组 sink 验收。 | 在同一 Windows 环境对基线提交与候选提交分别留存 pytest、frontend/frontend-mobile 结果，要求**无新增失败**而非只看绝对通过；默认 profile、云中转关闭时 diff 工具表与配置默认值，并实机覆盖 GUI 启动、新建/切换会话、对话/中断、权限卡、LAN 配对与重连、Windows headed 浏览器、凭据重启恢复及关闭期广播。另做 S1 的 Linux import 与 Windows 原生路径双向回归。 |
| **R2 Windows CLI 零回归** | 需求 :23；S3/S4 不动 CLI 分支与 handler :200-201；新增 server 工具档 :248；贯穿/P1 验收 :499、:512。 | **部分覆盖** | 现稿只明确 CLI headless 冒烟和“未设 profile”工具表对比，未分别给出 `--cli` 交互 `run_cli` 与 `-p`/`run_headless` 的输入、退出码、输出/错误流及终端权限提示判定，也未要求 `YCODE_TOOL_PROFILE=cli` 的工具表回归。本轮源码 `gui_main.py:140-145,148-174` 证实交互与 headless 是不同分支，一次 headless 冒烟不能覆盖 R2 全句。 | 用固定假模型和隔离的临时数据目录，在基线与候选版本分别运行 `--cli` 交互脚本（含权限 y/N、退出）及 `--cli -p … --session …`；比较退出码、关键 stdout/stderr、原 CLI 数据库写入，以及未设 profile/`cli` 两档工具表快照。再断言新增 server 档不改变上述两组结果。 |
| **R3 Linux 服务器 Personal Agent + 专属极简 Web 前端** | 需求 :24；对话数据流 :112；worker `--serve` :176-225；浏览器 Linux 化与围观 :227-253；Web 页面/传输 :255-291；P1 任务与验收 :501-515。 | **部分覆盖** | `--serve`、对话/审批/围观页面及 P1 对话验收均有载体，但 Q5 的 Linux import 改法、Q1 的重启水位协议仍未闭合（见第一节）。另有 P1 围观闭环缺口：:251 的截图代理由 worker token 鉴权并经网关透传，P1 却无网关，只定义 WS token 直连和 Caddy basic-auth（:264、:507-508），未定义浏览器 HTTP 取图的鉴权与 URL 重写，无法直接支撑 :511“截图围观可见”。现状 `core/computer/preview_server.py:45-63,104-106` 也只监听 127.0.0.1，不能假定浏览器可直取。 | 从空账号卷启动 Linux 容器，经实际 Web 页面完成 token 接入、新建/切换会话、含工具与浏览器点击的一轮对话、中断/重连、权限应答及无人应答超时；让页面中的真实 `<img>` 经同源 HTTPS 成功取到截图，同时验证错误凭据失败且 worker token 不出现在浏览器 URL/访问日志。随后执行 worker 重启自愈用例；P2 再复跑 JWT 登录/refresh 版本。 |
| **R4 安卓 App（Muse/Today 风格）** | 需求 :25；复用响应式 SPA :255-264；Capacitor、首启配置与原生能力 :293-309；P3 真机验收 :533-542。 | **部分覆盖** | Agent 的登录→对话→工具卡→审批→围观最小链已有页面和真机验收，desktop profile 也有明确入口；但 P3 :538“水位内存态”与 :148“持久化水位”直接冲突（见 Q1），杀进程重开语义尚不能据稿实现。安全存储只写“Preferences + 插件承担 Keystore”（:299），未指定实际插件与配置，静态审查无法确认 JWT/refresh 的加密承载。desktop 子链另受 R5 缺口影响。 | 在干净安卓真机完成首启服务器配置、登录、对话、工具卡、审批和围观；分别做断网、切后台、杀进程重开，按最终拍板的水位语义检查状态自愈、无重复事件及登录态保持。检查所选安全存储插件的实际持久化内容/Keystore 配置，并做 Web+App 同账号并发及退出换号后的状态清理；P4 再独立验 desktop 全链。 |
| **R5 App 内“我的电脑”经服务器中转** | 需求 :26；数据流 :113；设备/relay 端点 :132-135；双后端 profile :263、:300；云中转完整设计 :311-353；P4 验收 :544-555。 | **部分覆盖** | 管理后台签发 token→桌面粘贴配置→App 设备列表→RPC/事件/审批的产品链已有设计，但 Q2 的注册帧方向与“桌面 relay 断线、App 始终在线”反例仍使补发/自愈不闭合。另有两处独立规格未定：:316 固定心跳 30s，:321 却改为 15s/45s 判离线；`/relay/client` 在 :135、:317 使用 JWT query，:480、:575 又把首帧与 query 留作待选。 | 用管理员为 A 创建设备并配置桌面，A 的 App 完成在线发现、发消息、桌面执行、事件回传及双端审批；补测“桌面 relay 断开 60s 且 App 保持网关连接”、GUI 重启、45s 离线判定、两 App 并发 RPC id、token 吊销/轮换。B 的 JWT 连 A 设备必须失败；按最终选定的 JWT 传递方式检查重连与日志不泄露。事件“不重不漏”须待 Q2 选定补传或全量 resync 方案后判定。 |
| **R6 账号登录 + 对话隔离** | 需求 :27；账号/JWT 与各 WS/REST 端点 :128-140；worker token :212；账号库 :371-417；每账号卷、workspace、HOME :419-435；token 体系 :437-445；P2 隔离验收 :525-529。 | **部分覆盖** | 一账号一容器、独立卷/workspace/HOME 和 S8 残留修复覆盖了服务端主存储；本轮也实际读到 `config.py:430-439` 的主路径及 `tokenrhythm_auth.py:63-68`、`workflows/store.py:40-45`、`zcode_import.py:381-385`、`tool_adapters.py:151-155`、`skills.py:645-661` 的残留事实。缺口在于“所有数据通道”尚未逐项闭合：P2 只概括“任何 API”，未列设备列表/状态 WS、relay、预览/MCP/飞书及管理端越权矩阵；账号禁用未逐一规定 refresh、桌面 `/relay/device` 和各既有长连的失效；Web/App 退出换号、切服务器及 agent/desktop 切换时的内存事件、设备列表和 `(boot_id,last_seq)` 清理/命名空间也未写明。模型 Key 归属仍为开放项 :572。 | 建 A/B 两账号及同名会话、不同消息/Key/记忆/文件，逐项验证 `/ws/agent`、`/api/devices`、`/ws/devices`、`/relay/client`、`/relay/device`、`/api/admin/*`、`/ws/admin`、内部 `/metrics` 与可选 MCP/飞书均不能跨账号；检查 media/exports/workspace 下 `.ycode_tasks`、project hooks/workflows 及真实 HOME 无写穿。禁用 A 后验证 access/refresh、所有用户/设备长连和 worker 按拍板语义失效且 B 不受影响；同端 A 退出→B 登录、切服务器/profile 后不得残留 A 的会话、事件、设备或水位。 |
| **R7 后台管理系统** | 需求 :28；管理数据流 :114；管理端点 :136-140；四类功能与页面 :355-365；用量表 :406-414；P2 验收 :528-529。 | **部分覆盖** | 账号、设备、worker、用量四页及对应 API 已形成基本管理面，但全新部署未定义首个管理员的 bootstrap/种子开通路径；在“无公开注册、只能由管理员建账号”（:360）的前提下，空库无法进入首个管理闭环。用量又受 Q6 的水位、day、turns 缺口影响；此外 :363 要展示会话数，`usage_daily` :406-414 无会话数字段，:183 的 `/metrics sessions` 只是当前快照，不能还原按日历史。 | 从空 `gateway.db` 按明确的 bootstrap 流程创建首个 admin，登录后建 A/B、重置密码、禁用/恢复并验证非 admin 调管理 API 为 403；完成设备 token 生成/吊销/轮换、worker start/stop/restart 与 `/ws/admin` 状态推送。用可控 metrics  fixtures 跨 worker/网关重启、回退及跨午夜核对 token、turns、sessions 的页面值与持久表，口径未定前不得以“页面有数字”判通过。 |

## 三、新发现

**状态：已完成本轮静态复核。**以下均为第一轮十处抽查之外的新证据或原问题的新缺口；Q1/Q2/Q4/Q5/Q6 本身不在此重复计数。严重度中的“阻断”表示按现稿无法完成对应阶段验收，并不表示现有 Windows 版本已经发生故障。全部结论均为**静态审查、未实测**。

### 3.1 按严重度排序的问题清单

#### N1｜阻断｜Linux 浏览器仍被两处未列入改造清单的硬门槛拒绝

- **证据**：设计稿 :239 只给 Linux 适配器增加 `browser_supported/desktop_supported`，:246 只准备拆 `controller.py:517-527` 的 generation 复核。但真正的 backend 工厂仍在 `core/computer/controller.py:190-192` 对全局 `adapter.supported` 直接 fail-closed；基类默认该值在 `core/computer/platforms/base.py:49-54` 恒为 False。随后 `core/computer/controller.py:575-579` 又只接受 `edge/chrome`，即使 `browser_worker.py:37` 按设计稿 :244 增加 `chromium` 也到不了 worker。
- **影响**：R3、P1 的 headless Chromium 主链路不能凭现有 11 项清单跑通；这不是测试遗漏，而是确定的两处代码路径遗漏。
- **必改**：把 per-target 支持判定同时落实到 capability、generation 复核和 `_default_backend_factory`；把 controller 的浏览器白名单改为共享定义或纳入 `chromium`。新增从 `start_run → _resolve_backend_identity → _default_backend_factory → browser_worker` 的 Linux 集成测试，不能只测 argparse choices。

#### N2｜阻断｜浏览器“默认关闭—远程开启—Linux preflight”没有可用闭环

- **证据**：`core/computer/settings.py:102-108` 对缺失开关默认 False；`core/lan_server.py:238-241` 又禁止 LAN 调 `set_browser_control_enabled`。即使预置开关绕过黑名单，`gui_bridge.py:7126-7160` 的浏览器 preflight 仍强制 `adapter.probe_hotkey()`，而设计稿 :239 明确 Linux hotkey 继续抛 `computer_unsupported_os`，基类实际行为见 `core/computer/platforms/base.py:65-69`。
- **影响**：全新账号卷没有设计内的开启路径；手工预置 `browser_control_enabled=true` 也不是现稿写明的产品流程。若只修 N1，P1 仍可能卡在能力开启或 preflight。
- **必改**：明确 server 的浏览器开关初值和受控开启入口；server 模式使用只检查 Chromium 可用性的 browser preflight，不要求 Windows 全局热键，Web 紧急停止保留。验收必须从空 `config.json` 开始，禁止靠测试手工写 true 掩盖产品链缺口。

#### N3｜高｜同一 `dispatch_rpc` 目前会把 server 回合仍强制标成 `lan`

- **证据**：设计稿 :233 要求 serve 注入 `turn_source="server"`，:326 又要求 relay 复用同一 `LanServer.dispatch_rpc` 并注入 `lan`。现状 `core/lan_server.py:854-859` 对消息入口只调用 `_force_lan_turn_source`，而 `:896-921` 在 kwargs 与位置参数两条路径都硬编码 `turn_source="lan"`。
- **影响**：若 serve 原样复用，§4.3 第 7 项的 server browser 例外永远不会生效；若为 server 全局改成 `server` 又没有连接级来源参数，P4 relay 可能反而丢失桌面远程禁令。
- **必改**：将来源变成 dispatch 的**显式、连接级、服务端传入参数**，Windows LAN 与 relay 默认 `lan`，仅 serve worker 传 `server`；客户端字段继续剥离。黑名单本身可以继承：`core/lan_server.py:821-859` 不依赖 ws，但抽取必须整体保留黑名单、`_` 前缀、会话级权限守卫、参数裁剪、来源注入和 `_call_bridge`，不能只复制 54 项名单。补同一进程中 LAN→lan、serve→server、relay→lan 三组对照测试。

#### N4｜高｜截图围观同时缺少发布器接线与浏览器可执行的 HTTP 鉴权契约

- **证据**：预览发布器只有 GUI 在 `gui_main.py:858-875,1048-1050` 调用 `attach_preview_publisher(start_computer_runtime())`；该函数实际设置 controller 发布器的位置是 `core/computer/runtime.py:344-359`，controller 初始值则为 None（`core/computer/controller.py:287,304-314`）。设计稿 :186-192 的 serve 启动序列没有对应步骤。即便补上，`core/computer/preview_server.py:45-63,104-106` 只监听随机回环端口，设计稿 :251 的代理必须另行实现。
- **鉴权矛盾**：:251 说 `/computer-preview/*` 由 worker token 鉴权，:212 又说浏览器/App 永远拿不到 worker token；P1 没有网关，只写 token 直连和 Caddy basic-auth（:264,:507-508）。普通 `<img>` 请求如何携带凭据、如何重写事件中的回环 URL，均未定义。
- **影响**：P1“浏览器点击、截图围观可见”的验收缺少两条必要链路。
- **必改**：serve 启动/关闭同时接入 computer runtime 与 preview publisher；定义事件 URL 到同源 HTTPS 的映射。P1 可由 Caddy 在 basic-auth 通过后向上游注入 worker 凭据，P2 由网关先验账号 JWT 再以 worker token 访问内部预览；不得把长期 worker token 放进图片 URL。用真实 `<img>` 请求验收 200/401/403、跨账号 404/403 和日志无 token。

#### N5｜高｜设备 token 的冒用影响被低估，且缺少重复注册与 URL 泄露规则

- **证据**：设备长期凭据放在 `/relay/device?token=...`（设计稿 :134），:361 只规定创建/吊销/轮换，:482 的泄露面没有提 query 日志，并称单独设备 token 无法触达账号。全文没有同一 device_id 同时两条注册连接的替换/拒绝规则。
- **影响**：拿到设备 token 的攻击者虽然不能自己登录账号，却可以抢先冒充桌面，等待真实主人 App 随后发出 RPC 并返回伪造事件；“无需另有 JWT 就没有影响”不能成立。重复连接还会造成事件分叉、离线状态和 RPC 应答不确定。长期 token 进入 Caddy/网关访问日志会扩大该风险。
- **必改**：设备注册采用单连接 generation，明确新连接替换旧连接并 close，或拒绝第二连接；吊销/轮换必须原子更新哈希并踢线。默认关闭 query 日志或对 `token/jwt` 参数统一脱敏，优先把设备首帧鉴权也迁出 URL。补“攻击者持设备 token 先注册—主人 App 发 RPC”和“旧桌面重连与新桌面竞争”的反例测试。

#### N6｜中｜账号 JWT 的禁用/注销闭环只覆盖 refresh 与 WS，未覆盖既有 access 的全部权限面

- **证据**：:128-140 给出 login/refresh/logout 和各端点，:383-388 只存 refresh 哈希与 revoked，:441 规定 access 无状态 30 分钟；:360、:527 要求禁用即踢 WS/停容器，但未要求每个 REST/WS/RPC 请求重新检查 `accounts.disabled/role` 或 token 版本。
- **影响**：若实现只验 JWT 签名和 exp，禁用后的 access 在剩余有效期内仍可能调用管理或用户 API；重置密码、admin 降权也有同类旧权限存续歧义。
- **必改**：明确网关统一 auth dependency 每次读取账号 disabled/role，或引入 `auth_epoch/token_version` 并在禁用、重置密码、角色变化时递增；logout、禁用、refresh 轮换、设备长连分别列出失效矩阵。验收不能只测“不能再登录”。

#### N7｜中｜P1 暴露面清单漏了默认开启的独立 MCP 监听

- **证据**：`config.py:454-458` 显示远程 MCP 默认开启且默认端口 8765；`gui_bridge.py:8154-8179` 会读取该默认值并创建服务；`core/mcp_server.py:98-136` 默认绑定 `0.0.0.0`。它复用 LAN token（`gui_bridge.py:8160-8170`），但鉴权不等于网络入口不存在。
- **影响**：严格落实“不 publish”时它不是直接公网入口；但共享 Docker 网络、误加 `ports` 或 P1 手工暴露时，都会形成 Caddy/basic-auth 之外的第二条通道，与 :472“公网只有 443”的简化叙述不一致。
- **必改**：serve 默认禁用 MCP，确需开启时按账号网络隔离并纳入端口清单；P1/P2 验收实际执行容器端口与 Docker 网络检查，断言只有预期反代入口可达，而不是只检查 Caddyfile。

#### N8｜中｜relay 的心跳与 JWT 传递仍是互相冲突的双规格

- **证据**：:316 固定 ping/pong 30 秒，:321 又固定 15 秒、45 秒判离线；:135/:317 把 `/relay/client` 写成 JWT query，:480 与 :575 却又把首帧列为后续/待选。
- **影响**：不同实现者会写出不同离线阈值和 shim；P4 :550 的 45 秒验收无法判断应配合哪一个发送间隔，也无法提前确定日志脱敏与重连行为。
- **必改**：实施前只保留一套数值和一种客户端鉴权方式；建议直接首帧 JWT，设备 token 也同样评估首帧化，并同步接口表、时序图、P4 验收。

#### N9｜中｜首个管理员没有 bootstrap，空库无法进入“仅管理员建账号”的闭环

- **证据**：:360 禁止公开注册且只允许管理员创建账号，:136 的账号创建又必须 admin JWT；全文没有空库种子账号、一次性初始化令牌或本地 CLI。
- **影响**：R7/P2 从全新部署开始时无法通过产品接口创建第一个 admin。
- **必改**：提供仅本机执行的 `gateway init-admin` 或一次性、强制改密的 bootstrap 凭据；空库初始化后关闭该入口，并加入“无凭据远程创建 admin 必须失败”的验收。

#### N10｜中｜keyring file 的“加密凭据”承诺未指定可验证配置

- **证据**：设计稿 :202 只写固定 `keyrings.alt` file 后端，:424/:429 则称模型 Key 与 keyring 数据加密；现有 `core/credentials.py:47-56` 只是调用 `keyring.set_password`，实际保护强度完全由所选后端与配置决定。
- **影响**：若实现选了明文 file backend，卷内 Key 的保护承诺不成立；即使选加密后端，主口令/密钥如何注入、重启如何解锁、备份如何恢复也未定义。
- **必改**：写明后端类、加密选项、keyring 文件位置、主秘密供给和恢复流程；验收直接检查卷内文件不含模型 Key 明文，并做容器重启与备份恢复。该判定是配置缺口，未实测任何 keyrings.alt 后端。

### 3.2 三种 token 的签发—持有—校验闭环结论

| token | 已有设计与本轮源码支撑 | 闭环判定 | 必须补齐 |
|---|---|---|---|
| 账号 JWT / refresh | :128-140 定义签发与端点；:383-388 持久 refresh 哈希；:441 定义持有方和有效期 | **部分闭合** | N6 的 access 撤销/角色版本、N8 的唯一 WS 鉴权形态、App 安全存储插件，以及换号/切服务器时客户端状态清理 |
| worker token | :212 定义网关预写 config；`core/lan_server.py:495-517,612-639` 支撑签发、持久恢复与轮换踢线；workers 表由网关持明文 | **WS 主链基本闭合，旁路未闭合** | N4 的预览 HTTP 鉴权、N7 的 MCP 独立入口；P1 浏览器持 token 是明确过渡例外，必须在 P2 验证收回 |
| 设备 token | :361/:393 定义签发、哈希存储、吊销；:443 限定只能注册 relay | **部分闭合** | N5 的单连接 generation、冒用反例、query 日志脱敏，以及禁用账号后设备连接与在途 RPC 的失效矩阵 |

### 3.3 §4.2 S1–S13 逐项核对

| 项 | 静态结论 | 本轮依据与零回归判断 |
|---|---|---|
| S1 frameless | **未闭合** | Q5：`core/frameless.py:75-99` 的 Structure/回调仍依赖 wintypes；需连同 `gui_bridge.py:47` 顶层导入一起设计。 |
| S2 `_emit_raw` | **文本闭合，待实测** | Q4：保留 `_shutting_down`，仅窗口 sink 加条件可维持现有顺序；Windows 必须做有窗口/无窗口/关闭期三组 sink 测试。 |
| S3 serve 入口 | **部分闭合** | `gui_main.py:206-234` 证明新分派必须插在重 import 前，且不能改变既有 `--cli`；设计方向可行，但 N4 的 computer runtime 启动缺失。 |
| S4 权限策略 | **部分闭合** | `gui_bridge.py:1951-1954,7915-7939` 显示现 handler 在桥构造时注册且普通权限无限等；serve 必须明确替换 handler、在线客户端判定和超时拒绝竞态，不能只加配置键。GUI/CLI 原 handler 不应改。 |
| S5 凭据后端 | **部分闭合** | `core/credentials.py:47-56` 支持非 Windows keyring 路径，但 N10 的后端/加密/恢复配置未闭合。 |
| S6 单实例锁 | **静态可行** | `gui_main.py:721-724` 证实非 Windows 现不启用 Mutex；serve 独立 flock 不改变 Windows GUI。需测 stale pid 与容器 PID 1 重启。 |
| S7 bind/URL | **静态可行，需全链修改** | `core/lan_server.py:1123-1154` 除构造外还在 start 返回与 `_serve` 写死 `0.0.0.0`；新增 bind 必须贯穿这些点，缺省保持现值才能零回归。 |
| S8 数据目录 | **方向成立，不能仅靠 HOME 验收** | 本轮直接核对 `core/tokenrhythm_auth.py:63-68`、`core/workflows/store.py:40-53`、`core/workflows/skill_sync.py:16-18`、`core/hooks/zcode_import.py:381-385`、`core/harness/tool_adapters.py:151-155`、`core/skills.py:645-674`；改走 `config.YCODE_DIR` 可保持 Windows 缺省值，仍须真实 HOME 无写入断言。 |
| S9 Windows 功能面 | **需实测** | `gui_browser_mixin.py:35-49`、`core/gui_terminal.py:118-140` 的关键平台分支有守卫，但这只能证明所读片段；完整 `import gui_bridge` 与 server 工具降级未实测。 |
| S10 调试残留 | **改文案** | `core/llm.py:379-397` 是绝对路径开关；Linux 通常 no-op，但“删除无行为风险”对使用此诊断开关的 Windows 用户不成立。应优先默认关闭的 env 开关，保留原路径兼容性说明。 |
| S11 显式起服务 | **静态可行** | `gui_bridge.py:4377-4409` 证明现状依赖首屏启动 LAN/MCP/飞书；serve 显式调用是必要改动。N4 表明还不能把它等同完整浏览器运行时启动。 |
| S12 退出收尾 | **部分闭合** | `gui_bridge.py:2419-2430,2508-2524` 可复用 shutdown；serve 还需补 computer runtime/preview/browser worker 的对应关闭链，并验证 SIGTERM 不留下 Chromium。 |
| S13 `/metrics` | **未闭合** | Q6：路由可按 serve-only 注册且 GUI 不暴露，但水位表、事务、day/turns/sessions 口径先修后再验收。 |

### 3.4 §4.3 浏览器 11 项逐项核对

| # | 静态结论 | 本轮依据 |
|---|---|---|
| 1 Linux adapter | **未闭合** | 新 per-target 标志方向正确，但 N1 的工厂全局 `supported` 与 N2 的 hotkey preflight 会绕过该设计继续拒绝。 |
| 2 settings chromium | **可行** | `core/computer/settings.py:37-55,111-125` 有统一定义和默认值入口；平台回落可保持 Windows edge/embedded 不变，错误文案与 profile 白名单需同步。 |
| 3 channel | **可行但不足** | `core/computer/browser_backend.py:46` 可扩 channel； bundled Chromium 传 None 合理，但还必须修 N1 的 controller 白名单。 |
| 4 headless/args | **可行，待实测** | 现状 `core/computer/browser_backend.py:1209-1215` 固定 headed；按平台加 Linux headless 与 args 可保持 Windows 原参数，必须做 Windows headed 对照。 |
| 5 Linux PID | **可行，待实测** | `core/computer/browser_backend.py:1240-1267` 确实写死 exe 与反斜杠；改平台分支后需验证唯一主进程识别与 fail-closed。 |
| 6 worker choices | **必要但不足** | `core/computer/browser_worker.py:31-64` 只改这里无法解决 `core/computer/controller.py:575-579` 的前置拒绝。 |
| 7 capability | **依赖 N3** | `core/computer/capability.py:81-111` 支持按 profile/来源改判定；但来源必须先由可信 dispatch 正确注入，desktop 目标仍需 Linux 恒拒。 |
| 8 per-target | **未闭合** | 设计只覆盖 `core/computer/controller.py:517-527`，漏了同文件 `:190-192` 的 backend 工厂门槛。 |
| 9 credentials | **静态可行** | `core/credentials.py:23-44` 的 `_DataBlob` 与函数引用可整体平台隔离；需 Linux import 与 Windows DPAPI 双向回归。 |
| 10 server 工具档 | **默认路径可保持不变** | `core/tool_registry.py:185-210` 未设环境变量时仍走 app_context/full；`:1444-1456` 在插件加载后过滤。新增 server 集合时必须保留该顺序，并分别快照 full/cli/server，Windows 插件 `plugins/windows_tools/plugin.py:895-907` 仅在新增 server 档多剔除。 |
| 11 POSIX 进程组 | **改造位置不完整** | 仅改 `core/computer/browser_backend.py:618-633` 的硬杀不够；`start_new_session=True` 必须加到同文件 `:445-458` 的 Popen，随后按 pgid kill 并保留 Windows Job Object 分支。 |


## 四、阶段计划复核

**状态：已完成本轮静态复核。**P1→P4 的产品交付顺序可以保留，实施顺序则须增加以下门禁。现阶段各阶段均为**有条件可执行**：协议、数据来源或鉴权契约未冻结前，可以做工件原型，但不能把页面完成或静态推断记作阶段验收通过。

### 4.1 P1：服务器单账号链路

**阶段结论：需重排；先做最小 Linux headless 垂直切片，再做完整 Web。**

1. **P1a 最小垂直切片必须先于完整 Web。**从空配置、空数据卷开始，依次打通 Linux 导入与 `--serve` 启动、连接级 `turn_source=server`、server 工具档、浏览器受控开启与 server preflight、headless Chromium 打开/点击/截图、preview publisher 与同源 HTTP 取图、SIGTERM 后 Chromium 清理。该切片先关闭 Q5、N1、N2、N3、N4、N7 及 N10 的服务端 Key 配置缺口；通过后才铺开 `/chat`、`/approvals`、围观面板和视觉打磨。这样可把 Linux 导入、浏览器工厂/白名单、hotkey preflight、预览接线和 MCP 额外监听等最高风险放在 Web 大体量投入之前。
2. **完整 Web 的依赖门。**Q1 的首帧、epoch、控制帧顺序、快照合并及磁盘版/内嵌版 shim 改造清单须先冻结，再实现流式、重连和 worker 重启自愈；否则 :511 的“不丢”没有可执行判据。P1a 与完整 Web 应分开留验收记录，完整 Web 再从空卷完成对话、工具、权限应答/超时、真实 `<img>` 取图及 worker 重启。
3. **过渡暴露面与 Windows 回归前置。**P1 保留 HTTPS + Caddy basic-auth 双重要求，但实际检查容器端口、Docker 网络和日志；浏览器持有 worker token 仅是 P1 过渡例外，P2 必须收回。S2/Q4 三组 sink 用例、Windows headed 浏览器、GUI/CLI 两套口径及默认工具表对比应随对应重构执行，不能等到 P4。
4. **开放项门。**server 档在 P1 实测先以 `ask + Web 审批` 验证；P1 出口、P2 入口前必须拍板新账号默认 `ask/full` 并写入配置与验收。P1 出口还须实测 Key 随卷及 keyring 主秘密在容器重启、最小备份恢复后的可用性，不能只以“容器重启后页面仍有 Key”代替 N10 验收。

### 4.2 P2：多账号网关、账号隔离与管理后台

**阶段结论：后端高风险契约先行，管理 UI 后置；现计划不能直接按“gateway 全量 + 四页”并行验收。**

1. **入口门。**P1 的完整 Web 与 worker 重启用例须通过，Q1 协议须稳定；模型 Key 归属须在 P2 入口拍板，明确受邀者共用主人 Key 或各填各的，以及这对账号开通、隔离和用量归属的影响。server 默认权限也须在本阶段入口前定稿。
2. **先冻结并实现四类后端契约，再做管理 UI。**① N9 的空库首个管理员 bootstrap；② N6 的 JWT 撤销/角色版本与 access、refresh、logout、禁用、改密、降权、各 WS/RPC/relay 长连失效矩阵；③ R6 的账号隔离与全端点越权矩阵，包括预览、metrics、设备列表/状态、relay、MCP/飞书和真实 HOME 写穿；④ Q6 的持久水位表、同事务入账、逐字段映射、回退、day/turns/sessions 口径。管理四页只消费这些已冻结契约，不能反过来以页面字段决定协议。
3. **高风险验收先于页面完成。**从空 `gateway.db` 初始化 admin，使用旧 access/refresh 验证禁用、改密和降权后的实际权限失效；建立 A/B 同名会话及不同 Key/记忆/文件做跨账号反例；以可控 fixtures 覆盖 worker/网关重启、轮询后提交前崩溃、换卷/清库回退和跨午夜。上述结果出来后，再接 `/admin/*` 四页并做端到端验收。
4. **设备 token 的边界。**若 P2 保留设备 token 管理页，本阶段先完成签发、哈希存储、吊销/轮换、日志脱敏及备份恢复口径；单连接 generation、relay 在线踢线和冒用反例归入 P4 联调，不能凭 P2 页面按钮存在就宣告 N5 闭合。账号卷、`gateway.db` 备份及 worker token 恢复语义在 P2 入口冻结，P2 出口完成恢复演练。

### 4.3 P3：Capacitor 安卓 App

**阶段结论：产品顺序可保留，当前有两条开工硬门；真机高风险应先于视觉打磨。**

1. **Q1 持久水位矛盾必须在 P3 开始前解决。**明确选择跨页面/App 进程持久化 `(boot_id,last_seq)`，或杀进程后清空水位并全量快照自愈；同时定义按服务器、账号、agent/desktop profile 隔离的键、退出换号清理、boot_id 变化、snapshot/replay/live 合并与重复事件处理。:148 与 :538 不能继续各写一套，验收也只能按最终选定的一种语义描述。
2. **安全存储必须在 P3 开始前选定。**确定实际 Capacitor 插件、Android Keystore 配置、JWT/refresh 加密承载、系统备份排除或恢复规则、卸载/换机/密钥失效后的重新登录，以及退出、换号和切服务器时的清理；不能把“Preferences + 某插件”当作可验证设计。P2 的 login/refresh/4401 与账号撤销语义须已稳定。
3. **高风险真机矩阵前置。**先做安装、WSS/refresh、后台切换、断网、杀进程重开、Keystore 持久化及大文件/大帧，再做 Muse/Today 视觉适配。最终真机验收还须覆盖 Web+App 同账号并发、权限审批、真实截图 `<img>`、退出换号无残留；Capacitor 壳与 SPA 复用边界保持不变。

### 4.4 P4：云中转“我的电脑”

**阶段结论：产品功能可留在最后，dispatch/replay 纯重构与 Windows LAN 回归必须提前；事件保证须改为有边界的口径。**

1. **重构前移。**N3 的 `dispatch_rpc` 来源参数化和共享守卫抽取是 P1 serve 的依赖，应在 P1 完成并同步跑 Windows LAN 对照。EventReplayBuffer 的纯抽取最迟在 P2 以“保持现有 LAN 语义”为限完成，并跑现有 LAN 全量回归、手机配对与重连；涉及 Q2 新语义的行为变更再进入 P4。不能把首次触碰 LAN 主链路的风险全部积压到 P4。
2. **P4 入口门。**Q2 必须选定“离线打标补传”或“离线丢弃 + 每次桌面重新注册全量 resync”并写全时序；N5 的单连接/冒用规则、N8 的唯一心跳与客户端鉴权方式、设备 token 备份恢复后有效性均须冻结。验收必须加入“桌面 relay 断开 60 秒且 App 始终保持网关连接”的反例，以及 GUI 重启、两 App 并发、token 吊销/轮换和跨账号拒绝。
3. **128 帧环形缓冲与队列丢弃下不得无条件承诺“不重不漏”。**可验收承诺仅限“事件仍在保留窗口、未发生溢出/队列丢弃且按最终协议重连”的范围；超出 128 帧、出现序号空洞或出站队列丢帧时，必须可检测并显式标记 `resync_required`/降级，触发水位重置和全量快照，使最终状态收敛。此时只能承诺状态自愈及已保留事件按约定去重，不能声称所有历史事件均未丢失。验收须主动构造溢出与队列丢弃，而非只测正常重连。

### 4.5 贯穿约束与开放项阶段门

1. **先建基线失败集。**在相同 Windows 机器、基线提交和固定依赖环境运行现有 pytest、frontend、frontend-mobile 测试，记录既有失败、跳过及环境信息；每阶段候选提交以**无新增失败**为门槛，同时说明既有失败变化，不能用“全量通过”掩盖未跑或环境差异。
2. **CLI 交互与 headless 两套口径分别验收。**`--cli` 交互 `run_cli` 覆盖权限 y/N、退出、关键 stdout/stderr 和原数据库写入；`--cli -p … --session …`/`run_headless` 单独核对输入、退出码和输出。两种模式都比较基线与候选版本，并分别快照未设 profile、`cli` 档工具表；新增 server 档不得改变这两组结果。
3. **Windows GUI 与 LAN 回归随改动发生。**每阶段保留 GUI 冒烟，Q4 三组 sink、Windows headed、DPAPI 凭据及云中转默认关闭按涉及阶段实测；dispatch/replay 每次抽取都跑 LAN 回归。Linux/容器或安卓结果不能替代 Windows 结果。

| 开放项 | 必须落入的阶段门 |
|---|---|
| 模型 Key 归属（:572） | **P2 入口**拍板，账号开通、隔离矩阵和用量面板据此实现。 |
| server 档默认权限（:573） | P1 以 `ask + Web 审批` 实测，**P1 出口 / P2 入口前**定稿并验配置默认值。 |
| `/relay/client` JWT 传递（:575） | **P4 入口**只保留一种正式方式，同步 N8、日志脱敏、重连和 shim 验收。 |
| 围观截图带宽（:576） | P1 记录真实 PNG 流量，**P3 入口前**决定是否降采样/WebP；不改变截图围观的产品边界。 |
| 备份与 token 恢复（:577） | **P1 出口**验 Key/keyring 最小恢复；**P2 入口冻结、出口演练**账号卷 + `gateway.db` + worker token；**P4 入口冻结、出口演练**设备 token 恢复/吊销语义。 |

tokenrhythm 在 P1 禁用、relay 设备并发不超过 4、P1 公网过渡期必须 basic-auth 已是裁决项，按原设计执行，不再列为待选。

## 五、总体结论

**总体判定：有条件通过（r1 不能直接实施）。**架构方向和分阶段产品边界具备继续细化的基础，Q4 文本已闭合，Q5/Q6 也有有效修订方向；但 Q1/Q2 尚有协议反例，N1/N2 阻断 P1 主链，N3-N10 及 Q6 仍会使对应实现或验收失真。这里的“通过”仅指完成下列必改项并复审后可进入实施，不构成对 r1 的直接开工许可，也不代表任何运行验收已通过。

本轮不重新翻案以下既定边界：**一账号一容器、Capacitor、单机 Docker、个人信任模型、截图事件流围观**均予保留；必改项用于补齐这些边界内的协议、安全和验收，不扩展为 SaaS 多租户、公开注册、视频流围观或跨机调度需求。

### 5.1 必改 / 必验项（共 20 项）

1. **M01｜Q1｜P1/P3 门：统一 worker epoch 与水位协议。**写全客户端首帧、`hello`、`seq_reset` 的逐方向字段和 `hello → reset → replay → live` 顺序，定义旧服务端无 hello、旧客户端无 boot_id、`/health` JSON、`worker_starting/lost` 缓存 epoch；覆盖新 gateway-shim、磁盘版和内嵌兜底 shim。拍板跨进程水位及命名空间，实测 boot_id 不匹配、重复补发、worker 重启、杀进程重开和快照合并。
2. **M02｜Q2｜P4 门：闭合桌面 relay 重连与有限补发。**拆分桌面注册请求、网关注册应答和 `device_reset`；在离线补传与离线丢弃后强制 resync 中二选一，写全 seq 空洞、128 帧溢出、队列丢弃及 App 始终在线的时序。按 M02 的保留窗口承诺验收，删除无条件“不重不漏”。
3. **M03｜Q4｜P1 起必验：保留关闭期语义。**设计文本无需再改；实现后在 Windows 实测：无窗口且非关闭期 LAN/飞书收到而 JS 不入队，关闭期所有 sink 均不调用，有窗口时序列化→JS→LAN→飞书顺序不变，并纳入后续阶段无新增失败回归。
4. **M04｜Q5｜P1a 门：消除 wintypes/Structure 冲突。**选择平台中立 ctypes 类型，或把依赖 wintypes 的 Structure 连同顶层导入一并平台隔离；容器实测 `import core.credentials`、`import gui_bridge` 且不触发惰性原生绑定，Windows 实测无边框/DWM 原生路径不变。
5. **M05｜Q6｜P2 门：冻结 metrics 数据契约。**新增逐字段持久水位及“读快照→算增量→写 `usage_daily`→更新水位”单事务，定义 provider/model/api_base/cache_scope 合并、input/output/total 映射、模型空值/改名与回退；明确 day 为入账日或消耗日，给 turns/sessions 持久数据源或删除首版承诺。用 fixtures 验重启、提交前崩溃、回退及跨午夜。
6. **M06｜N1｜P1a 门：修全 Linux backend 拒绝链。**per-target 判定同时覆盖 capability、generation 复核和 `_default_backend_factory`，controller 白名单纳入共享的 chromium 定义；以 `start_run → _resolve_backend_identity → _default_backend_factory → browser_worker` Linux 集成用例证明能到 worker。
7. **M07｜N2｜P1a 门：补浏览器产品开启链。**明确 server 初值和受控开启入口，server preflight 只检查 Chromium 可用性且保留 Web 紧急停止；从空 `config.json` 验收，不得手工预写 `browser_control_enabled=true` 绕过。
8. **M08｜N3｜P1 门：可信连接级 turn_source。**来源仅由服务端按 LAN/serve/relay 连接注入，客户端字段继续剥离，完整保留黑名单、`_` 前缀、权限守卫、参数裁剪与 `_call_bridge`；同进程对照 LAN→lan、serve→server、relay→lan，并做 Windows LAN 回归。
9. **M09｜N4｜P1a/P2 门：接通截图发布与 HTTP 鉴权。**serve 启停接入 computer runtime/preview publisher，定义回环 URL 到同源 HTTPS 的映射及 P1 Caddy、P2 网关两种上游鉴权；真实 `<img>` 验 200/401/403、跨账号 404/403，浏览器 URL 和访问日志均无长期 worker token。
10. **M10｜N5｜P2/P4 门：限制设备 token 冒用与重复注册。**选定单连接 generation 的替换或拒绝策略，吊销/轮换原子更新哈希并踢线，统一 token/jwt 日志脱敏并优先首帧鉴权；实测攻击者抢先注册、主人 RPC、旧新桌面竞争及账号禁用后的失效矩阵。
11. **M11｜N6｜P2 门：闭合账号 JWT 撤销。**采用每请求检查 disabled/role 或 `auth_epoch/token_version`，覆盖 logout、禁用、改密、角色变化、refresh 轮换、既有 access 与全部长连；集成测试须证明旧权限在预期时刻失效，而非仅不能再登录。
12. **M12｜N7/S7｜P1/P2 门：收敛网络暴露面。**serve 默认禁用 MCP；确需开启时按账号隔离网络并列明端口。实际检查容器 publish 端口和 Docker 网络，只有预期反代入口可达，不能只审 Caddyfile。S7 的 bind 参数须贯穿构造、start 返回与实际 `_serve`，未配置时保持 `0.0.0.0`；`remote_public_url` 只改对外 URL 前缀，并做 Windows 默认值、serve 绑定与 HTTPS 对外地址的端到端验收。
13. **M13｜N8｜P4 门：只留一套 relay 双规格。**固定唯一 ping/pong 周期与离线阈值，选定首帧或 query 的客户端 JWT 正式方式，同步接口、时序、shim、日志脱敏和 45 秒验收；若保留调试兼容，须默认关闭且有独立限制。
14. **M14｜N9｜P2 门：提供首个管理员 bootstrap。**实现仅本机 `gateway init-admin` 或一次性强制改密凭据，空库初始化后永久关闭入口；从无凭据远程建 admin 必失败开始，验完整建号、改密、禁用和非 admin 403。
15. **M15｜N10｜P1/P2 门：验证真实加密 keyring 配置。**写明后端类、加密选项、文件位置、主秘密注入、重启解锁和恢复流程；直接检查卷内无模型 Key 明文，实测容器重启及备份恢复，不能仅凭使用 `keyrings.alt` 名称宣称加密。
16. **M16｜贯穿门：建立零回归差分证据。**留存基线失败集及候选无新增失败结果；Windows GUI、CLI 交互与 headless 两套口径、默认/cli/server 工具表、Q4、headed 浏览器、DPAPI、LAN 配对/重连均按 §4.5 执行。S10 诊断开关默认关闭时不得改变 Windows 行为；若保留原路径开关，须做兼容对照，不能仅以 Linux no-op 宣称无风险。缺任一适用项不得记阶段通过。
17. **M17｜产品与客户端配置门：关闭开放决策。**按 §4.5 在 P2 入口定模型 Key 归属、P1 出口/P2 入口定 server 默认权限、P3 入口定 Android 安全存储和 Q1 水位语义、P4 入口定 relay JWT；P1 测截图流量并在 P3 入口定带宽策略。每项均须有配置/协议文本和对应实测，不能只留会议结论。
18. **M18｜备份与 token 恢复门：执行恢复演练。**P1 验 Key/keyring，P2 验账号卷、`gateway.db` 与 worker token，P4 验设备 token；写明恢复后旧 token 有效、轮换或吊销的规则，以及长连和在途 RPC 的处理，并用实际备份恢复证明，不把“已复制文件”当作可恢复。
19. **M19｜R6/S8｜P2 门：闭合账号与对话全通道隔离。**建立 A/B 两账号的同名会话、不同消息/文件/Key/记忆，逐项验证 workspace、media/exports、项目 hooks/workflows 与真实 HOME 无写穿，S8 不得以 `/data/home` 兜底掩盖残留。覆盖 `/ws/agent`、设备列表/状态、relay、preview、metrics、MCP/飞书和 admin 的越权矩阵；账号禁用后各凭据、长连和 worker 按契约失效。Web/App 退出换号、切服务器及 agent/desktop profile 切换后，不得残留前一账号的会话、事件、设备与 `(boot_id,last_seq)`，并以实测记录为证。
20. **M20｜S4｜P1 门：闭合 serve 权限策略。**明确有在线客户端时等待、默认 10 分钟超时拒绝、无在线客户端处理与“全自动拒绝”配置；覆盖超时与人工应答、取消、断线及 shutdown 的竞态，确保请求只结算一次且不挂死。serve handler 必须显式替换，GUI/CLI 原 handler 不变；分别实测 serve 等待/超时/全拒绝与 Windows GUI/CLI 原权限语义。

### 5.2 本轮未实测与后续验证要求

本轮只读设计、审查材料和报告已列源码区间；**未运行 pytest、前端测试、服务、导入实验、构建、Linux 容器、网络验证或安卓真机，也未执行任何 keyring 后端实验**。后续至少补齐：

- **Windows 真机：**基线/候选测试差分，GUI 与 CLI 交互/headless 两套链路，Q4 三组 sink，Windows headed 浏览器、DPAPI 凭据、LAN 配对/重连及 dispatch/replay 抽取回归，云中转默认关闭和 relay 开启后的桌面全链。
- **Linux 与容器：**实际 import、`--serve` 启停、空配置浏览器开启、headless Chromium 点击/截图、权限超时、SIGTERM/PGID 清理、同源 `<img>` 鉴权；另验容器端口/MCP、A/B 卷与真实 HOME 隔离、worker/网关重启、metrics 崩溃事务、keyring 加密及备份恢复。
- **安卓真机：**不同 Android/WebView 组合上的 WSS、login/refresh/4401、后台与断网、杀进程重开、大文件/大帧、截图、Web+App 并发、退出换号/切服务器清理，以及 Keystore 重启保持和密钥失效恢复；P4 再验 desktop profile 全链。

以上实测产生可复查记录、20 项条件全部按所属阶段关闭后，才能把本结论推进为实施验收通过。本报告的证据范围限于静态文档与已记录源码抽查，未覆盖全量代码正确性、渗透测试、性能或长期稳定性验证。

---

## 附录 A：当前 r1 设计文档章节行号索引

以下行号只对应本次读取的 579 行 r1 文件。

| 当前行号 | 章节 / 内容 |
|---|---|
| :1-10 | 标题、r1 修订说明、日期、状态与输入 |
| :12-36 | §1 背景与需求；R1-R7 在 :18-28，信任模型在 :30-35 |
| :39-68 | §2 现状审计结论摘要；2.1 :43-49，2.2 :51-58，2.3 :60-67 |
| :71-115 | §3 总体架构与关键数据流 |
| :118-366 | §4 组件详细设计 |
| :120-174 | §4.1 中心网关；Q1 的 seq epoch 方案在 :148 |
| :176-225 | §4.2 worker `--serve`；S1-S13 在 :194-210，Q6 累计口径在 :183 |
| :227-253 | §4.3 浏览器操控 Linux 化与 11 项改造 |
| :255-291 | §4.4 Web 前端与传输层 |
| :293-309 | §4.5 安卓 App |
| :311-353 | §4.6 云中转；Q2 在 :320，背压/离线 RPC 在 :321 |
| :355-365 | §4.7 管理后台 |
| :369-445 | §5 数据与隔离；5.1 :371-417，5.2 :419-435，5.3 :437-445 |
| :449-493 | §6 安全设计；6.1 :451-474，6.2 :476-482，6.3 :484-493 |
| :497-555 | §7 分阶段实施计划 |
| :501-515 | P1：服务器单账号链路 |
| :517-531 | P2：多账号网关、隔离与管理后台 |
| :533-542 | P3：Capacitor 安卓 App |
| :544-555 | P4：云中转“我的电脑” |
| :559-568 | §8 明确不做（YAGNI） |
| :570-579 | §9 开放问题与已裁决项 |

## 附录 B：Q 项映射（原始问题 → r1 声称修复）

> 本表用于对照原始问题与 r1 声称修复；“原始位置”指 `audit/design-review.md`，“r1 位置”指当前 579 行设计稿，实际验证结论见第一节。

| Q 项 | 第一轮原始问题（原始位置） | r1 声称修复方式（r1 位置） |
|---|---|---|
| Q1 | worker 重启后 `_event_seq` 归零，而前端保留旧 `lastSeq`，补发与客户端水位判断均可能吞掉新事件；原稿未定义 epoch/boot_id 或水位重置协议，且原验收未覆盖 worker 进程重启。（:41-42；必改要求另见 :96） | §4.1 :148 增加 `boot_id`：worker 启动生成，hello 帧返回；shim 持久化 `(boot_id,last_seq)`，重连同带；boot_id 不匹配时忽略旧水位、补发当前缓冲并标记 `seq_reset`，前端重置水位并经 `get_initial_state` 自愈。`worker_starting`/`worker_lost` 携带 boot_id，`/health` 增加该字段。P1 :511 增加 worker 重启后前端重连用例，P2 :527 要求崩溃重启沿用同一水位自愈用例。/metrics 的 boot_id 字段另见 :183。 |
| Q2 | R5 桌面侧重连后 seq 续增还是归零未定义，网关镜像缓冲可能出现同类旧水位失效；桌面离线期间事件未进入缓冲，原稿未定义设备重新上线后的 App 水位重置与全量状态自愈，P4 原验收只测 App 侧断线。（:44-45；必改要求另见 :97） | §4.6 :320 规定桌面 EventReplayBuffer 与 seq 随 GUI 进程存活，CloudRelayClient 重连不重建缓冲且 seq 续增；短暂断网后 App 可带旧 `last_seq` 补发。GUI 重启导致 boot_id 变化，`relay_registered` 携带 boot_id；网关清空设备镜像缓冲并推 `device_reset`，App 重置水位并经 `get_initial_state` 全量自愈。设备离线期间的事件不假设可补发，重连走状态自愈。P4 :551 增加桌面 relay 断开 60 秒恢复及 GUI 进程重启两类用例。 |
| Q4 | S2 原文只写移除无窗口早退、LAN/飞书广播无条件执行，未明确保留 `_shutting_down`，字面实现会改变关闭期广播行为，与 R1 零回归及停机链存在冲突风险。（:50-51；必改要求另见 :98） | S2 当前改法在设计稿 :199 明确“保留 `_shutting_down` 早退不变”，仅移除 `not self._window` 条件；窗口存在才进入 JS 队列，非关闭期才执行 LAN/飞书及后续云中转广播，并声称有窗口与关闭期顺序保持现状。 |
| Q5 | S1 原修复清单遗漏 frameless 顶层 `wintypes` 导入与模块级 `WINFUNCTYPE`，即使只守卫 `windll`，Linux import 仍可能失败。（:53-54；必改要求另见 :99） | S1 当前改法在设计稿 :198 将范围写为全部 Windows 原生绑定区守卫化/惰性化，明确包含 :34 的 `wintypes` import、:97-99 的 `WINFUNCTYPE`、:101-117 的 `windll`、:204/:217 的 `dwmapi`；`_MONITORINFO` 等纯 Structure 保持平台无关，并声称 Windows 路径不变。P1 :504 仍安排 credentials 与 frameless 的 Linux 导入实测。 |
| Q6 | 原稿未定义 `/metrics` 的 token_usage 是进程累计还是 DB 全量累计，也未定义网关快照水位；worker 或网关重启时可能丢尾或重复计数。（:56-57；必改要求另见 :100） | §4.2 :183 规定 `/metrics` 一律返回 DB 全量累计，依赖卷持久化跨容器重启保持；网关按 `(account_id, model)` 保存上次快照水位，增量为 `max(0, 当前累计-水位)`；当前值小于水位时按重置处理、以当前值全额计增量并更新水位；网关重启后从 DB 恢复水位。P2 :527 增加 worker 重启与网关重启下“不丢不重”的验收用例。 |

### 证据使用说明

1. 附录 B 仅作 Q 项导航，r1 修订是否成立以第一节的验证结论为准。
2. 附录 C 记录本轮源码抽查；第一轮十处位置用于避让，必要重叠已单独列出。
3. “r1 文本声称”“本轮源码证据”“后续运行实测”是三种不同证据，不得相互替代。

---

## 附录 C：本轮直接源码抽查记录

下表以本轮实际打开的文件与行区为准，均用于第一、三节或交叉验证；未把三份审计报告中的引用当作源码证据。所列区间与第一轮十处抽查点错开，必要的 r1 重叠复核另列于表后。

| # | 本轮 `file:line` | 核对内容 |
|---|---|---|
| 1 | `core/lan_server.py:224-377` | RPC 黑名单、消息入口与参数边界 |
| 2 | `core/lan_server.py:495-557` | token 签发/恢复与注册补发原子性 |
| 3 | `core/lan_server.py:698-750` | 事件打标、无客户端也入缓冲、有界出站队列 |
| 4 | `core/lan_server.py:756-859` | 实际首帧、replay、dispatch 与权限守卫 |
| 5 | `core/lan_server.py:877-944` | `turn_source=lan` 的硬编码及 bridge 调用 |
| 6 | `core/lan_server.py:950-1036` | HTTP `/health`、静态路由、两套 shim 分发 |
| 7 | `core/lan_server.py:1123-1149,1151-1154` | 实际 bind、端口恢复与 start 返回 |
| 8 | `core/tool_registry.py:185-210,1436-1456` | 未设 profile 的 full 回落、插件后过滤顺序 |
| 9 | `core/computer/controller.py:180-231` | 全局 `adapter.supported` 的 backend 工厂门槛 |
| 10 | `core/computer/controller.py:499-528,532-598` | generation per-target 判定与 edge/chrome 白名单 |
| 11 | `core/computer/settings.py:102-123,145-160,193-232` | 默认开关、浏览器探测、平台快照与设置 preflight |
| 12 | `core/computer/platforms/base.py:42-75`、`core/computer/platforms/linux.py:1-8` | 默认 unsupported、hotkey/desktop 抛错与 Linux 现状 |
| 13 | `core/computer/browser_worker.py:31-64` | worker 的 edge/chrome choices 与后端创建 |
| 14 | `core/computer/browser_backend.py:441-480` | POSIX `start_new_session` 需要落入的 Popen 位置 |
| 15 | `core/computer/browser_backend.py:591-670` | 硬杀顺序与 Windows Job Object 分支 |
| 16 | `core/computer/browser_backend.py:1172-1207,1240-1267` | Playwright 惰性加载、PID 的 exe/路径匹配 |
| 17 | `core/computer/runtime.py:260-359` | runtime 单例与 preview publisher 的真实接线 |
| 18 | `core/computer/preview_server.py:31-106` | 回环绑定、随机 token、最近三张 PNG 与 URL |
| 19 | `core/mcp_server.py:98-154,206-260` | MCP 默认全网卡监听及 token 校验 |
| 20 | `gui_main.py:206-234` | CLI/browser worker 分派之后才导入 GUI 重模块 |
| 21 | `gui_main.py:699-724,858-875,923-970,1038-1051` | Windows 锁、GUI 启动与唯一 computer runtime 调用点 |
| 22 | `gui_bridge.py:1921-1962` | 桥构造时 GUI 权限 handler 的注册 |
| 23 | `gui_bridge.py:4377-4409,8106-8188` | 首屏惰性启动与 MCP 默认开启链 |
| 24 | `gui_bridge.py:7115-7160,7218-7238` | 浏览器 preflight 的热键门槛和开关 setter |
| 25 | `core/frameless.py:66-91` | `_MONITORINFO/_MINMAXINFO` 对 wintypes 的字段依赖 |
| 26 | `core/credentials.py:27-49` | DPAPI 绑定与 Linux keyring 入口前的真实依赖 |
| 27 | `core/components.py:542-582` | Playwright 优先 site-packages、组件目录后回的导入逻辑 |
| 28 | `core/permission_terminal.py:506-574` | broker 等待/取消循环，验证 serve 超时应落在策略 handler |
| 29 | `core/llm.py:379-410` | Windows 绝对路径诊断开关的实际 no-op 条件 |
| 30 | `core/computer/capability.py:105-145` | target 组装与模型协议判定，避开第一轮 :87-103 |
| 31 | `plugins/windows_tools/plugin.py:895-907` | Windows 插件的 profile 过滤位置 |
| 32 | `gui_browser_mixin.py:35-49`、`core/gui_terminal.py:118-140` | Windows 专属对象的惰性/平台分支 |
| 33 | `core/tokenrhythm_auth.py:63-68`、`core/workflows/store.py:40-53`、`core/workflows/skill_sync.py:16-18` | S8 用户目录残留第一组 |
| 34 | `core/hooks/zcode_import.py:381-385`、`core/harness/tool_adapters.py:151-155`、`core/skills.py:645-674` | S8 用户目录残留第二组及 `~/.agents` 扫描 |
| 35 | `core/lan_server.py:379-406,528-610`、`frontend/mobile-shim.js:48-137` | r1 专项的 seq/首帧/shim 消费机制 |
| 36 | `core/memory.py:959-1010,1464-1500,1669-1769`、`core/llm_diagnostics.py:976-1075` | r1 专项的 token_usage schema、写入与聚合 |

必要的重叠复核只有第一轮已经指出的 `core/frameless.py:34,97-117`、`core/credentials.py:9,23-24,51-56`、`gui_bridge.py:3810-3831`、`core/lan_server.py:1150` 及 `core/computer/browser_backend.py:1212`；它们用于验证 Q4/Q5 和核对设计引用，**不计入上述新增抽查**。
