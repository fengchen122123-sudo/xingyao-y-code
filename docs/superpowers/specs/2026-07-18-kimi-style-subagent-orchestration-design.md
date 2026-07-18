# Y-CODE Kimi 风格子 Agent 编排与思考流式输出设计

## 目标

将 Y-CODE 当前“总是后台启动、下一轮碰运气回填”的子 Agent 机制改为与 Kimi Code 一致的明确语义：

- 用户需要当前结果时，子 Agent 默认前台运行，主 Agent 等待结果并在同一轮汇总。
- 同一模型响应中的多个独立子 Agent 调用并行执行。
- 只有模型明确设置 `run_in_background=true` 时才后台运行。
- 权限请求统一串行排队，不允许多个线程覆盖全局确认 Future。
- 子 Agent 不能再派生子 Agent，不能消费其他任务的通知。
- 思考内容以稳定的分句/分行粒度流式写入终端历史，不恢复逐 token 重绘。

目标版本为 `v1.0.11`。启动 Logo、欢迎卡、主题、输入框和底部状态栏的结构不变。

## 当前问题

### 调度语义错误

`run_subagent()` 当前总是创建守护线程并立即返回“已启动”。子 Agent 的结果只进入全局通知队列，而且仅在下一次 `_chat_with_model_generator()` 创建时排空。因此主 Agent 无法在当前轮次完成“派出两个子 Agent 并汇总”。

### 并发权限死锁

`run_subagent` 和子 Agent 内的 `execute_command` 均会进入 `check_permission_and_ask()`。所有线程共用 `main.confirm_prompt`，新请求会覆盖上一个未完成请求，导致旧 Future 永久无人解决。

### 结果归属不明

全局通知队列没有父会话、父轮次和子 Agent 归属。任何新启动的主或子模型生成器都可以排空该队列，导致结果被错误消费。

### 思考输出两极化

逐 token 调用 `run_in_terminal()` 会让 prompt_toolkit 反复擦除和重画输入区，造成逐字换行、光标跳动或覆盖。完全缓冲到 `thinking_finished` 又失去流式反馈。

## 总体架构

### 子 Agent 任务记录

`SubagentManager` 为每个调用创建独立记录：

- `task_id`
- `parent_session`
- `parent_turn_id`
- `description`
- `run_in_background`
- `status`: `pending | running | completed | failed | timed_out | cancelled`
- `started_at` / `finished_at`
- `result` / `error`
- `cancel_event`

任务记录由 Manager 加锁管理，不允许 UI 或模型循环直接修改内部字典。

### 前台运行

`run_subagent` 新增参数：

```text
task_description: string
run_in_background: boolean = false
timeout_seconds: integer = 120
```

前台调用流程：

1. 创建任务记录并启动子 Agent。
2. 当前工具调用等待该子 Agent 进入终态。
3. 完成时把子 Agent 最终公开回答直接作为工具结果返回主 Agent。
4. 超时或取消时设置子 cancel event，并返回明确的结构化错误。
5. 主 Agent 拿到同一批所有工具结果后，继续当前 LLM 轮次并生成汇总。

不进行隐式重试或降级。超时、无公开回答和工具失败都直接暴露。

### 并行调度

`ToolSpec` 新增 `parallel_safe` 能力元数据。只读工具和 `run_subagent` 设为 `parallel_safe=True`，文件写入、编辑、普通命令等保持串行。

同一模型响应中连续的 `parallel_safe` 工具调用放入一个有界线程池。两个 `run_subagent` 会同时运行，批次在全部完成或超时后按原工具调用顺序返回结果。默认并发上限为 4，防止同一 API Key 被无界并发压垮。

### 后台运行

`run_in_background=true` 时：

- 工具立即返回 `task_id` 和当前状态。
- 任务结果保存在 Manager 记录中，不作为无归属全局字符串。
- 仅根节点的主 Agent 可以获取归属于当前会话的通知。
- 后续增加状态查询/停止接口时复用同一任务记录，不再创建第二套存储。

本次实现保留当前后台启动能力，但系统提示词必须要求：需要当前结果时不得设置后台。

## 权限协调

新增一个串行 `PermissionBroker`，成为唯一可以设置 `main.confirm_prompt` 的所有者。

1. 主 Agent 或子 Agent 把授权请求连同独立 Future 放入 Broker 队列。
2. Broker 只激活队首请求并在 TUI 线程更新确认面板。
3. 用户选择后，Broker 完成对应 Future，然后激活下一个请求。
4. 取消父轮次时，其所有未处理授权请求都以拒绝完成，不留孤儿 Future。
5. 确认面板显示请求来源（主 Agent 或子 Agent 任务简称）。

`run_subagent` 本身不被当成文件变更，但子 Agent 内部的命令、写入和编辑仍依照当前权限模式通过 Broker 授权。

## 子 Agent 隔离

- 子 Agent 调用 `chat_with_model_events(..., allow_subagents=False, consume_background_notifications=False)`。
- 子 Agent 的 API 工具 schema 中移除 `run_subagent`。
- 子 Agent 不排空主 Agent 的后台通知。
- 子 Agent 使用独立消息列表、cancel event、任务状态和最终公开回答缓冲。
- 子 Agent 不直接输出到 prompt_toolkit，仅通过 Manager 更新任务状态。

## 工具轨迹与状态

前台子 Agent 在终端显示稳定轨迹：

```text
● 星瑶正在调度子代理：查询天气
● 星瑶正在调度子代理：查询时间
✓ 子代理已完成：查询天气
✓ 子代理已完成：查询时间
```

不把子 Agent 的完整思考或工具原始输出打印到主终端。主 Agent 只获得子 Agent 最终公开结论，再统一汇总。

## 思考分句流式输出

新增独立的 `ThinkingStreamBuffer`，规则如下：

1. `thinking_delta` 只追加到内部缓冲。
2. 缓冲中出现中英文句末符号（`。！？.!?` 或显式换行）时，输出到最后一个完整句子。
3. 长时间没有标点时，缓冲达到 120 个可见字符后在最近空白处断开；中文无空白时按 120 字符断开。
4. 每次刷新都是一个完整的灰色文本块并且以换行结束，因此 `run_in_terminal()` 不需要保持半行光标。
5. `thinking_finished` 刷新剩余文本。
6. 最终思考原文仍完整持久化，分句仅影响实时显示。

这是稳定的“行级流式”，不是逐 token 伪流式。

## 超时、取消与错误

- 前台子 Agent 默认超时 120 秒，参数范围 30-3600 秒。
- 父轮次 `Ctrl+C` 会设置所有归属前台子 Agent 的 cancel event，并取消其未完成权限 Future。
- 单个子 Agent 超时不伪造结果；工具结果明确标记 `timed_out`，主 Agent 根据其他已完成结果如实汇总。
- 子 Agent 最终公开回答为空时视为失败，不显示“运行圆满成功”。
- 后台任务退出应用时依现有守护线程语义停止，不承诺进程重启后恢复。

## 系统提示词契约

活动提示词和仓库默认提示词增加：

- 互不依赖的多个子任务应在一个响应中一次性发出多个 `run_subagent` 调用。
- 当前回答依赖子 Agent 结果时，使用默认前台模式，不得设置 `run_in_background=true`。
- 只有任务可以独立继续、当前决策不依赖结果时才后台运行。
- 子 Agent 结果只对主 Agent 可见，需要用户看到时由主 Agent 汇总。
- 子 Agent 不能启动其他子 Agent。

## 文件边界

预计修改：

- `core/tools.py`: 子 Agent 任务记录、前台/后台运行、超时、取消和归属通知。
- `core/llm.py`: 并行工具分组、子 Agent 工具过滤、通知消费边界和参数传递。
- `core/tool_protocol.py` / `core/tool_registry.py`: `parallel_safe` 能力元数据和新 schema。
- `core/permission_broker.py`: 串行权限请求 Broker。
- `core/thinking_stream.py`: 分句流式缓冲器。
- `core/turn_renderer.py` / `main.py`: 消费稳定思考块、子 Agent 状态和取消联动。
- `core/tool_presenter.py`: 前台完成、后台启动、超时和失败文案。
- `system_prompt.md` 与 `%USERPROFILE%/.ycode/system_prompt.md`: 子 Agent 调度契约。
- `setup.py` / `main.py`: 版本升级到 `v1.0.11`。

预计新增或修改对应的 pytest 测试文件，不修改启动布局。

## 测试与验收

自动化覆盖：

- 两个前台 `run_subagent` 工具调用同时开始，结果按调用顺序返回。
- 主 Agent 在同一轮收到两个子 Agent 最终结论并输出汇总。
- 前台超时、Ctrl+C 取消、空结果和异常都进入正确终态。
- 并发授权请求不会覆盖 Future，严格一次显示一个。
- 取消父轮次会拒绝所有排队权限请求。
- 子 Agent 的 schema 中没有 `run_subagent`。
- 子 Agent 不会消费根 Agent 的后台通知。
- 后台调用立即返回 ID，前台调用只在结束后返回结果。
- 思考分片不逐 token 输出，完整句子会在 `thinking_finished` 前逐段刷新。
- 无标点长思考在 120 字符上限处稳定分段。
- 历史恢复仍显示完整思考原文。

真实 Windows PTY 验收：

1. 输入：`你分出两个子 agent，一个查询天气，一个查询时间，你来汇总给我`。
2. 如天气城市不明，模型应先询问用户或在任务描述中明确假设，不伪造位置。
3. 终端快速显示两条子代理开始轨迹，两个任务重叠运行。
4. 授权面板不覆盖、不消失、不死锁。
5. 两个结果返回后，主 Agent 在同一用户轮次输出天气+时间汇总。
6. 灰色思考在生成期间按完整句子流式出现，无逐字换行或光标跳动。
7. 输入框和状态栏恢复，启动首屏仅版本号变为 `v1.0.11`。
