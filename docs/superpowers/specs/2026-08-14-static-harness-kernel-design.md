# Y-code 固定 Harness 内核设计

## 状态

- 日期：2026-08-14
- 目标分支：`codex/harness-kernel-upgrade`
- 设计状态：已批准
- 事实源决策：`messages` 表继续作为会话唯一事实源
- 兼容性决策：GUI、TUI、命令、插件体系和模型可见行为保持不变
- 缓存决策：DeepSeek、Kimi 命中能力不得低于当前水平；提升项必须有确定性字节证据和真实指标支持

## 1. 背景

Y-code 已具备结构化计划、子代理、插件工具、MCP、权限、取消、上下文维护、DeepSeek 不可变 Prompt Ledger、Kimi 缓存亲和和运行时 TurnEvent。当前主要问题不是能力缺失，而是核心职责集中在 `core/llm.py`：请求构造、流解析、重试、工具权限、工具执行、计划约束、消息持久化、缓存账本和取消打捞相互交织。

deepseek-harness 的主要可借鉴点是明确的 Turn/Step 生命周期、只追加事件记录和工具执行管线。它的插件容器、事件作为唯一事实源、工作流 VM 和模型生成代码执行方式不适合直接移植到 Y-code。本设计只吸收结构化内核思想，并保留 Y-code 现有 Provider、缓存、插件、计划和取消机制。

## 2. 目标

1. 建立固定、非插件化的 Harness 内核，明确 Turn、Step、请求、流和工具执行边界。
2. 将 `core/llm.py` 逐步收缩为兼容外观层，不要求 GUI/TUI 或调用方同步重构。
3. 新增只追加的持久化生命周期日志，用于审计、崩溃定位和恢复状态判断。
4. 将工具执行统一为显式管线，保留现有工具结果、权限语义、插件注册和模型顺序。
5. 保持 DeepSeek 和 Kimi 的模型请求前缀、工具契约与 Provider 特有字段稳定。
6. 建立实际 Provider 调用边界的字节等价测试，阻止缓存相关回归。

## 3. 非目标

- 不采用 deepseek-harness 的插件容器或动态 Harness 插件。
- 不删除或替换 Y-code 当前 `core/plugin_loader.py`、`plugins/`、`ToolRegistry` 或 MCP。
- 不让事件日志替代 `messages` 表，也不回填旧会话的历史事件。
- 不在第一阶段新增 Kimi 不可变 Prompt Ledger。
- 不自动恢复或重放崩溃前未完成的模型请求、命令、文件写入或其他副作用工具。
- 不增加用户可见命令、设置项、页面、状态提示或新的交互流程。
- 不改变系统提示词、动态上下文文案、工具 schema、工具顺序、消息顺序或结果截断文案。
- 不引入工作流 VM、模型编写代码执行器或新的沙箱模型。
- 不借 Harness 重构之名重写计划、子代理、记忆压缩或插件体系。

## 4. 已选方案

采用“固定 Harness 内核 + 兼容外观层”方案。

### 4.1 未选择的方案

1. 最小函数拆分：风险低，但不能提供持久化 Step 状态、崩溃审计和可靠边界，收益不足。
2. 完整事件溯源：结构统一，但要求迁移会话、撤销、压缩、缓存账本和 UI 回放语义，缓存与兼容风险过高。

### 4.2 依赖方向

```text
GUI / TUI
  -> core.llm 兼容外观
    -> TurnRuntime
      -> StepRuntime
        -> 现有请求规范化链路 -> RequestEnvelope -> Provider
        -> StreamAssembler
        -> ToolPipeline -> ToolRegistry / 插件工具 / MCP
      -> TurnJournal
      -> MemoryManager(messages)

DeepSeek 官方主会话：现有请求规范化链路 -> 现有 Prompt Ledger
Kimi 官方路由：现有请求规范化链路 -> 现有缓存亲和参数
```

Harness 是固定内核，不通过插件扫描或动态导入组装。插件仍只扩展工具集合，并通过当前 `ToolRegistry` 进入 ToolPipeline。

## 5. 组件设计

### 5.1 TurnRuntime

职责：

- 为一次用户提交绑定唯一 `turn_id`、会话、工作目录和取消事件。
- 管理 Turn 状态转换和连续 Step。
- 协调消息提交、Journal、取消和最终状态。
- 将现有运行时事件继续交给 GUI/TUI 消费。

TurnRuntime 不负责组装 Provider 消息、不解析流、不执行具体工具，也不渲染界面。

Turn 状态：

```text
created -> preparing -> running -> completed
                         |-> cancelling -> cancelled
                         |-> failed
                         `-> interrupted
```

状态转换必须由单一方法校验。非法转换抛出明确的不变量异常，不静默修正。

### 5.2 StepRuntime

一次真实模型 API 调用对应一个 Step。工具返回后再次请求模型时创建下一个 Step。

职责：

- 保存 Turn 内单调递增的 Step 序号。
- 绑定实际 Provider 路由、请求指纹、DeepSeek generation 和完成原因。
- 协调 StreamAssembler 与 ToolPipeline。
- 确保一个 Step 只有一个终态。

Step 状态：

```text
created -> requesting -> streaming -> assistant_ready
                                  |-> completed
                                  `-> tools_running -> tools_committed -> completed
```

任意非终态 Step 在进程重启后变为 `interrupted`。系统不推断工具是否完成，也不自动重放。

### 5.3 RequestEnvelope

RequestEnvelope 是实际 Provider 调用的不可变观察快照，不是新的请求构造器。

第一阶段在以下逻辑全部完成后创建：

1. 当前 `_strip_media_and_merge` 规范化；
2. DeepSeek Prompt Ledger 对齐或切代；
3. Kimi reasoning 字段补齐；
4. Provider cache control 和缓存亲和参数生成；
5. 最终 tools 选择和 Provider options 确定。

Envelope 记录：

- Provider、模型、API Base、缓存作用域；
- messages、system、tools 和 Provider options 的 SHA-256；
- 消息数、system 数、字节数；
- DeepSeek generation ID 和账本消息数；
- Kimi 缓存亲和 ID 的哈希，不记录原值；
- 实际请求原因和重试序号。

Envelope 不复制或重新排序请求对象，不经过“序列化后再反序列化”路径，也不参与发送。Provider 继续收到当前代码生成的原对象。任何 Provider 的通用重排或通用 canonicalization 均禁止加入。

### 5.4 StreamAssembler

职责：

- 组装公开正文、推理字段、工具调用、finish reason 和 usage；
- 处理跨 chunk 的工具参数与 Provider thinking 数据；
- 产生现有 GUI/TUI 可消费的运行时事件；
- 构造与当前格式相同的 assistant 消息。

Provider 契约保持固定分支，不做动态插件：

- DeepSeek thinking 的 assistant tool call 始终保留字符串类型 `reasoning_content`，允许空串；
- Kimi OpenAI 路由继续保留当前 tool call reasoning 占位语义；
- Kimi Anthropic 路由继续保留 thinking blocks 与 metadata；
- 其他 Provider 保持当前字段清理规则。

缺失必须字段属于协议错误，应让 Step 失败；不得伪造推理正文或猜测字段。

### 5.5 ToolPipeline

工具管线采用固定阶段：

```text
接收 ToolCall
  -> 参数解码
  -> 重复调用守卫
  -> 计划约束
  -> 工具契约解析
  -> 权限判定
  -> 沙盒边界判定
  -> 工具适配器执行
  -> 结果规范化
  -> 上下文截断
  -> 消息与 Journal 提交
```

规则：

- 第一阶段沿用当前参数解码、重复调用签名和模型可见错误文案，不增加新的 JSON 猜测修复。
- 每个可执行工具必须解析为完整 ToolSpec 或明确的 MCP ToolSpec 适配对象。
- 缺失能力元数据时返回明确的工具契约错误，不使用函数名猜测权限。
- 内置特殊工具通过固定适配器实现文件 Diff、Undo、计划、AskUser、子代理和后台任务语义。
- 插件工具继续从现有 ToolRegistry 获取；Harness 不加载插件。
- MCP 在连接后转成同一执行契约，但仍由现有 MCP Manager 实际调用。
- 工具内部可恢复错误返回 tool message；持久化错误、不变量错误和权限系统故障不得伪装成普通成功结果。
- 最终模型可见 tool message 的 role、tool_call_id、name、content 和截断文本必须与当前行为一致。

并发规则保持现状：

- 只有显式 `parallel_safe` 的相邻工具可组成并发批次；
- 最大并发度保持 4；
- 修改类工具和权限交互串行执行；
- started/finished 事件可按实际执行时序记录；
- 送回模型和写入 `messages` 的结果严格按原始 tool call 顺序；
- 每条事件记录 `call_ordinal`，不以线程完成顺序推断模型顺序。

### 5.6 TurnJournal

TurnJournal 是只追加的生命周期元数据日志，不是会话内容日志。

新增表：

#### `harness_turns`

- `turn_id` 主键；
- `session_id`；
- 状态；
- 开始、结束时间；
- 最后事件序号；
- 脱敏错误分类。

#### `harness_steps`

- `step_id` 主键；
- `turn_id` 和 Turn 内 ordinal 唯一；
- Provider、模型、API Base、缓存作用域；
- messages/system/tools/options 指纹；
- DeepSeek generation ID；
- 状态、耗时和 finish reason。

#### `harness_events`

- 自增 ID；
- `turn_id`、可选 `step_id`、Turn 内唯一 sequence；
- 事件类型、时间；
- 可选 `message_id`；
- 工具名、call ordinal；
- 参数哈希、结果哈希、耗时、结果状态；
- 脱敏错误码。

禁止写入事件表：

- 用户或 assistant 完整正文；
- reasoning 或原始流 chunk；
- 原始工具参数和工具结果；
- API Key、认证头、环境变量；
- Kimi 缓存亲和 ID 原值。

持久化事件使用受约束的类型，不接受任意 payload。序列化失败直接报告不变量错误，不进行浅拷贝、字符串化或丢字段降级。

## 6. 数据流

### 6.1 普通无工具回合

1. GUI/TUI 沿用现有入口提交用户消息。
2. TurnRuntime 创建 Turn 并进入 `preparing`。
3. 当前 `prepare_turn_messages` 构造稳定 system、动态上下文和历史。
4. StepRuntime 创建 Step。
5. 当前 Provider 路径完成规范化，RequestEnvelope 只读取最终请求并生成指纹。
6. 请求发送给 Provider；StreamAssembler 产生现有 UI 事件。
7. assistant 消息与引用它的 Journal 事件在同一 SQLite 事务提交。
8. Step 与 Turn 进入 `completed`。

### 6.2 工具回合

1. StreamAssembler 形成有序 tool calls。
2. Step 进入 `tools_running`。
3. ToolPipeline 按相邻 `parallel_safe` 规则分批执行。
4. `tool_started` 在执行前提交；`tool_finished` 记录哈希和状态。
5. assistant tool-call 消息和有序 tool result 消息按当前持久化边界提交；对应 committed 事件与消息同事务。
6. Step 进入 `completed`，创建下一 Step，请求模型继续。

### 6.3 DeepSeek 官方主会话

- 保留当前启用范围、generation 身份、system/tools 比较、前缀对齐、CAS 追加、切代和删除逻辑。
- 不移动现有 Ledger 追加调用的逻辑时机，不与 Journal 强行合并事务。
- RequestEnvelope 只记录 generation ID、消息数和最终请求指纹。
- DeepSeek tools 继续按当前工具名规则排序。
- 历史撤销、压缩、媒体折叠或无法证明只追加时继续切代，不修改旧 generation。

### 6.4 Kimi

- 第一阶段不新增 Prompt Ledger。
- tools 保持当前注册顺序，不采用 DeepSeek 排序规则。
- 官方路由继续使用稳定 `prompt_cache_key` 或 `metadata.user_id`。
- tool contract 继续参与缓存 lane 身份。
- OpenAI/Anthropic 路由的 reasoning、thinking、cache control 和 options 保持当前格式。
- Harness 元数据不进入 messages、tools、metadata 或 extra_body。

## 7. 原子性与一致性

- `messages` 始终是用户会话的唯一事实源。
- 新的 MemoryManager 批量提交接口返回消息 ID，并在同一事务插入引用这些 ID 的 Journal 事件。
- 不引用消息的生命周期事件可独立提交。
- Journal 写入失败时事务整体回滚，Turn 明确失败；不得“只写消息、跳过 Journal”。
- DeepSeek Ledger 保持独立 CAS 事务。Ledger 冲突继续显式失败或按当前规则切代，不回退到普通历史请求。
- 会话重命名通过稳定 `session_id` 保持 Journal 关联；无需改写历史事件。
- 删除会话时级联删除 Harness Turn/Step/Event；撤销消息时保留事件元数据，但被删除消息的引用置空。
- 旧会话不回填虚构 Turn/Step。升级后产生的新 Turn 才写 Journal。

## 8. 取消、崩溃与错误处理

### 8.1 用户取消

顺序保持现有铁律：

1. 设置 cancel event；
2. 关闭本次 response 流；
3. 打捞已形成的合法消息组；
4. 必要时为已持久化的 assistant tool calls 补取消 tool result；
5. 提交取消状态；
6. 回收当前内存 Turn 残影。

用户取消后的任务不注入接手指令，不触发计划自动续推。

### 8.2 进程崩溃

启动时在单一事务中把所有非终态 Turn/Step 标记为 `interrupted`。不修改消息内容，不补造工具结果，不自动发起模型请求，不重放副作用工具。

如果工具已经产生外部副作用但尚未提交结果，Journal 可显示其最后状态供诊断，但 Harness 不猜测是否成功。用户后续发起新回合时使用当前 `messages` 历史继续。

### 8.3 错误分类

- Provider 网络错误；
- Provider 协议错误；
- 请求不变量错误；
- 工具参数错误；
- 工具执行错误；
- 权限或沙盒错误；
- 持久化错误；
- 用户取消；
- 进程中断。

只有明确属于模型可修正范围的错误转成 tool message。请求不变量、Journal 事务和数据库错误使 Step/Turn 失败，不包装成成功或继续下一 Step。

## 9. 缓存不回退契约

### 9.1 不可改变的输入

- `build_stable_system_message` 的输出字节；
- 动态上下文追加位置和文案；
- Provider 实际收到的消息顺序、角色、键插入顺序和字段值；
- DeepSeek tools 排序结果；
- Kimi tools 原始顺序；
- DeepSeek `reasoning_content` 空串契约；
- Kimi reasoning/thinking 字段；
- Kimi 缓存亲和 ID 的生成材料；
- Provider cache control、max tokens 和 thinking options；
- DeepSeek Ledger generation 和切代规则；
- keepalive 的启用范围、cache scope 和 tool contract 指纹。

### 9.2 确定性证明

在生产代码拆分前，先从当前 dev 基线捕获 Provider 调用入口的黄金请求夹具。新代码对同一固定输入必须满足：

- messages 序列化字节完全相等；
- tools 序列化字节完全相等；
- Provider options 序列化字节完全相等；
- 缓存 session/lane 身份完全相等；
- 前一轮实际请求是下一轮的完整消息前缀，或按既有规则明确切代；
- 任何差异都使测试失败，不维护字段忽略名单。

黄金场景至少覆盖：

- DeepSeek 官方非 thinking 普通回合；
- DeepSeek 官方 thinking 工具回合；
- DeepSeek 多工具、动态上下文变化、摘要和 generation 切代；
- Kimi 官方 OpenAI 路由普通与工具回合；
- Kimi 官方 Anthropic 路由 thinking/tool/cache control；
- Kimi 工具顺序与缓存亲和 ID；
- MCP schema 加入后的工具契约；
- 取消打捞和最终总结重试；
- 非 DeepSeek/Kimi 的代表 Provider。

### 9.3 可选提升

Harness 首期的缓存提升来自减少不必要重建和更早发现指纹漂移，不主动更改缓存策略。后续优化只有同时满足以下条件才可合入：

1. 独立提交；
2. 说明改变了哪些请求字节；
3. 受影响 Provider 的黄金测试已更新并经人工确认；
4. 在相同模型、endpoint、会话序列和时间窗口下完成真实 API 对比；
5. 加权 `cached_tokens / prompt_tokens` 不低于基线；
6. 不降低上下文质量或改变工具语义。

Kimi Ledger、缓存策略统一和 Provider 通用重排不属于本规格的可选提升。

## 10. 迁移与交付阶段

### 阶段 0：冻结基线

- 捕获 Provider 调用边界黄金夹具；
- 补强 stable system/tools 的精确字节测试；
- 记录现有 GUI/TUI 事件序列和工具结果夹具；
- 不改生产行为。

### 阶段 1：模型与 Journal

- 增加 Harness 数据类型、状态机和 additive SQLite 表；
- 增加原子消息/Journal 提交接口；
- 增加启动时 interrupted 标记；
- 旧会话不回填。

### 阶段 2：Turn/Step 编排

- 用 TurnRuntime/StepRuntime 包裹现有主循环；
- `core/llm.py` 公开入口保持不变；
- 现有请求构造和工具函数暂不移动。

### 阶段 3：ToolPipeline

- 先建立旧实现与新管线的结果/事件等价测试；
- 按固定适配器逐类迁移内置工具；
- 接入现有插件 ToolRegistry 与 MCP；
- 删除已无调用的旧分派分支，不保留双执行路径或运行时开关。

### 阶段 4：RequestEnvelope 与 StreamAssembler

- 在最终 Provider 调用边界接入只读 Envelope；
- 提取流组装逻辑，保留 Provider 固定分支；
- 每次移动都运行完整黄金字节测试。

### 阶段 5：收口

- 删除已失效的内部兼容包装，但保留公开 API；
- 验证取消、计划、子代理、插件、MCP、会话操作和缓存；
- 完成真实 Provider 缓存抽检后交付。

阶段是代码审查与测试门禁，不是运行时双轨。一个阶段切换完成后删除旧内部路径，不引入静默 fallback 或长期 feature flag。

## 11. 测试设计

### 11.1 状态机

- Turn/Step 所有合法转换；
- 非法转换立即失败；
- 终态不可再次改变；
- 并发事件 sequence 唯一且单调；
- 重启只把非终态标记为 interrupted。

### 11.2 Journal 与数据库

- 消息和引用事件同事务成功；
- 任一写入失败时整体回滚；
- 会话重命名、删除、清空和撤销行为；
- 旧数据库 additive migration；
- 不写正文、参数、结果和认证信息；
- 并行工具事件带正确 call ordinal。

### 11.3 ToolPipeline

- 每种内置工具的模型可见结果等价；
- 插件 ToolSpec 与 MCP 适配；
- confirm/plan/full 权限模式；
- 沙盒内外路径；
- 参数错误、未知工具、权限系统错误；
- repeat guard；
- 相邻 parallel_safe 分批和最大并发；
- 结果按模型顺序写回；
- 工具结果截断字节等价；
- 取消时不继续提交后续结果。

### 11.4 Provider 请求

- 第 9 节全部黄金场景；
- 实际调用参数对象的 canonical JSON 字节比较；
- 连续两轮完整前缀比较；
- DeepSeek generation CAS 和切代；
- Kimi 缓存亲和与工具顺序；
- 请求重试前后只发生当前代码允许的字段变化。

### 11.5 回归

- 全量 pytest；
- AGENTS.md 指定 py_compile；
- GUI/TUI 事件序列快照；
- 计划、子代理、取消、插件、MCP 和记忆压缩专项测试；
- 可用真实凭据时运行 DeepSeek/Kimi 控制变量缓存抽检。

## 12. 验收标准

1. GUI/TUI、命令、模型可见消息和工具结果没有非设计内变化。
2. 现有插件无需修改即可继续注册和执行工具。
3. `messages` 是唯一会话事实源，Journal 不含正文副本。
4. 每个新 Turn/Step 都有明确、可查询的终态。
5. 崩溃后非终态工作被标记 interrupted，任何副作用工具均不自动重放。
6. `core/llm.py` 不再直接拥有工具策略、具体工具分派和完整流组装职责。
7. DeepSeek 与 Kimi 黄金请求字节和缓存身份相对基线完全一致。
8. DeepSeek Prompt Ledger 的适用范围、追加、CAS、切代与清理语义不变。
9. 所有现有测试和新增 Harness 测试通过。
10. 不存在双事实源、静默降级、长期双执行路径或未完成占位项。

## 13. 风险与控制

| 风险 | 控制方式 |
|---|---|
| 模块拆分改变 dict 顺序或 Provider 字段 | 先冻结调用边界黄金字节，Envelope 不参与构造 |
| Journal 与 messages 分叉 | 引用消息的事件与消息同一 SQLite 事务 |
| 并行工具结果乱序 | 区分执行事件顺序和 call ordinal，提交按模型顺序 |
| 插件工具失效 | 保留 ToolRegistry；Harness 只消费 ToolSpec |
| 崩溃后重复副作用 | 只标记 interrupted，绝不自动重放 |
| DeepSeek Ledger 回归 | 不移动适用范围、对齐、CAS 和追加时机 |
| Kimi 缓存回归 | 不新增 Ledger、不排序 tools、不改缓存亲和材料 |
| `llm.py` 一次性大重写 | 按测试门禁逐边界迁移，每阶段删除旧内部路径 |
| 敏感信息重复落盘 | Journal 采用固定字段，只存 ID、状态和哈希 |

## 14. 后续关系

本设计批准并提交后，下一步只创建详细实施计划。实施计划必须按阶段 0 到阶段 5 排序，并把缓存黄金测试放在任何生产路径修改之前。未经新的设计审批，不得把 Kimi Ledger、事件事实源迁移、插件体系改造或用户可见功能加入实施范围。
