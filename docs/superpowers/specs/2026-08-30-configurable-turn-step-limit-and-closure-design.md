# Configurable Turn Step Limit and Explicit Closure Design

## 背景

2026-08-30 的真实 GUI 回合在持续产生有效进展时执行了 100 个 Harness Step 和 115 个工具调用。第 100 个 Step 成功完成 `154 passed` 的测试，但外层循环没有下一次模型请求读取该结果，随后直接写入“任务轮次超过上限（100 轮）”。该路径又调用了 `harness_runtime.complete()`，因此数据库与 GUI 把未完成任务显示成“已完成”。

这不是 `TurnProgressGuard` 应处理的无进展循环：该回合包含 29 次 `edit_file`、1 次 `write_file`、42 次 `execute_command` 以及计划状态变化，停滞计数持续被真实进展清零。问题来自固定 100 Step 的硬上限、错误的终态映射，以及内部协议占位符被展示给用户。

## 目标

1. 保留单 Turn 的模型请求安全上限，默认行为继续等价于 100 Step。
2. 允许用户在 GUI“系统设置”中把上限设置为 10–1000 的任意整数。
3. 每个 Turn 在首次 Provider 请求前读取并冻结上限；运行中的配置修改只影响后续 Turn。
4. 达到上限但模型尚未正常文本收尾时，将 Turn 明确终止为 `failed / StepLimitExceeded`。
5. 保留已经提交的 assistant、tool、代码修改和测试结果，不回滚副作用，不自动重放。
6. GUI 必须显示“任务未完成”，不得随后被 `turn_finished` 覆盖为“已完成”。
7. 清理 `[System: Empty message content sanitised to satisfy protocol]`，使其不进入实时正文、持久化公开正文或历史回放。

## 非目标

- 不提供 `0 = 无限`，也不提供关闭硬上限的入口。
- 不按模型、Provider、会话或项目分别保存上限；本期只有一个全局系统配置。
- 不自动创建后续 Turn，不自动提高上限，不在到达上限后继续调用模型。
- 不改变 `TurnProgressGuard.MAX_STALLED_STEPS = 6`、`RepeatCallGuard` 或计划调度语义。
- 不修改 Provider 请求结构、tools 顺序、稳定 system prompt、DeepSeek Prompt Ledger、Kimi/GLM 路由或 token 统计口径。
- 不把工具调用数量当成模型轮次数量。

## 术语与计数口径

- **Turn**：一次用户消息触发的完整主 Agent 回合。
- **Step / 模型轮次**：一次真实 Provider 请求。Provider 内部重试仍属于同一个 Step。
- **工具调用**：某个 Step 返回的一个 tool call；单个 Step 可以包含多个工具调用。
- **上限**：单个 Turn 最多允许创建的 Step 数量。

因此，配置值为 100 时允许创建 `step:1` 到 `step:100`，并不表示最多只能执行 100 个工具。

## 配置契约

新增 JSON/TOML 兼容配置键：

```json
{
  "agent_turn_step_limit": 100
}
```

固定常量：

```python
TURN_STEP_LIMIT_CONFIG_KEY = "agent_turn_step_limit"
DEFAULT_TURN_STEP_LIMIT = 100
MIN_TURN_STEP_LIMIT = 10
MAX_TURN_STEP_LIMIT = 1000
```

规则：

- 键不存在时返回默认值 100，并保持旧用户配置兼容。
- `bool` 不得按 Python 的 `int` 子类语义通过校验。
- 非整数、小于 10 或大于 1000时抛出明确的 `TurnStepLimitError`。
- GUI setter 必须先校验再写盘；失败返回 `ok: false`，原配置保持不变。
- 不使用截断、取整、钳位或默认值回退来掩盖显式非法值。
- 配置通过现有 `config.save_config_value()` 原子写入 `config.json`。

配置解析与校验放在新文件 `core/harness/turn_limits.py`，避免把 Harness 规则继续堆入 `core/llm.py`，也避免让前端或 Bridge 复制边界常量。

## Turn 数据流

1. GUI 或 CLI 创建 Turn。
2. `_chat_with_model_generator()` 完成准备阶段。
3. 在 `harness_runtime.mark_running()` 前调用一次 `load_turn_step_limit()`。
4. 把返回值保存在当前生成器局部变量 `max_turns` 中。
5. `for turn in range(max_turns)` 继续维持一个 Step 对应一次 Provider 请求。
6. 正常文本收尾、取消、Provider 错误和 NoProgressError 仍沿用现有出口。
7. 循环自然耗尽时执行统一的 StepLimitExceeded 出口。

运行中修改设置不会重新读取 `max_turns`。这保证同一个 Turn 的安全边界是确定的，并避免并发设置更新导致序号上限漂移。

## 达到上限时的终态语义

循环耗尽后的处理顺序必须是：

1. 构造固定用户可见消息，其中使用当前 Turn 冻结的真实上限：

   ```text
   🚨 单任务已达到模型轮次上限（N 轮），任务未完成。已保留本轮代码、工具结果和测试记录；请发送“继续”开启新回合，或在系统设置中调整上限。
   ```

2. 把该 assistant 消息追加到内存与持久化队列，并在当前 Step 上原子提交。
3. 完成最后一个仍处于 `tools_committed` 的 Step。
4. 调用 `harness_runtime.fail("StepLimitExceeded")`，不得调用 `complete()`。
5. 发出一个 `error` 展示事件和一个 `turn_error` 终态事件。
6. 返回生成器，外层不得再发出 `turn_finished`。

这是“失败但已安全落盘”的闭环，不是崩溃，也不是成功完成。它不会撤销工具副作用，因为历史字节和已提交工具结果是下一 Turn 继续工作的事实基础。

## NoProgressGuard 的关系

两种保护保持正交：

| 保护 | 触发条件 | 错误码 | 默认阈值 |
|---|---|---|---|
| 无进展熔断 | 连续 Step 没有确定性状态变化 | `NoProgressError` | 6 |
| 单 Turn 硬上限 | 已创建的 Step 数达到配置值 | `StepLimitExceeded` | 100 |

无进展熔断通常先发生。持续编辑、测试或推进计划的长任务可以越过无进展熔断，但最终仍受硬上限约束。

## 系统设置接口

`get_system_settings()` 的快照新增：

```json
{
  "agent_turn_step_limit": 100,
  "agent_turn_step_limit_min": 10,
  "agent_turn_step_limit_max": 1000
}
```

Bridge 新增：

```python
set_agent_turn_step_limit(value: int) -> {
    "ok": bool,
    "settings": SystemSettingsSnapshot,
    "msg": str
}
```

前端使用后端返回的 min/max，不自行硬编码第二份业务边界。设置页使用整数输入框和显式保存按钮：

- 标题：`单任务最大模型轮次`
- 说明：`一次用户任务最多发起多少次模型请求；工具调用数可能高于该值。修改后仅影响新任务。`
- 当前值合法且与已保存值不同，保存按钮才可用。
- 输入非法时显示范围错误，不调用 Bridge。
- 后端拒绝时恢复已保存快照并显示明确错误。

## 协议占位符清理

内部标记：

```text
[System: Empty message content sanitised to satisfy protocol]
```

统一由 `core.turn_renderer.sanitize_protocol_text()` 删除。该函数已经同时服务实时正文和历史渲染，`core/llm.py` 也会在构造公开 assistant 消息前调用它，因此在这里增加精确标记清理可以覆盖：

- 实时 assistant delta 聚合；
- assistant 消息持久化前的公开正文；
- GUI/CLI 历史回放；
- 旧数据库中已经存在的占位符。

只删除精确内部标记，不模糊删除普通用户文本中的 `System` 字样。带 tool calls 的 assistant 消息仍保留 tool calls、reasoning 字段与 Provider 契约所需的空 `content` 语义。

## 错误处理与兼容性

- 缺少新配置键属于正常旧配置，使用 100。
- 用户通过 GUI 输入非法值时拒绝写盘。
- 手工把配置文件写成非法值时，Turn 启动必须明确失败，不静默恢复 100。
- `StepLimitExceeded` 必须写入 `harness_turns.error_code` 和对应 `turn_status` Journal 事件。
- 最后一个 Step 应为 `completed`，因为它的 Provider 请求和工具提交已经完整结束；失败属于 Turn 层级。
- `messages` 继续是正文唯一事实源；Journal 不增加正文或配置值字段。
- 不新增数据库迁移。当前 Turn 使用的上限由最终提示文字和 Step 数量可审计，不把配置快照写入 Journal。

## 测试策略

### 配置

- 缺省值为 100。
- 10、100、1000 合法。
- 9、1001、布尔值、浮点数、数字字符串拒绝。
- 保存失败不会报告成功。

### Harness

- 测试把上限设为 3，连续修改类工具只能产生 3 个 Provider 请求。
- 第 3 个工具结果和上限提示均持久化。
- Turn 为 `failed / StepLimitExceeded`，最后 Step 为 `completed`。
- 公开事件包含一个 `turn_error`，不包含 `turn_finished`。
- 用户消息显示真实配置值 3，并明确“任务未完成”。
- resolver 在一个 Turn 中只调用一次。
- 现有 NoProgressError 测试保持通过。

### GUI

- 初始快照包含 value/min/max。
- 合法整数调用 Bridge 并应用返回快照。
- 非法值不调用 Bridge。
- Bridge 拒绝时恢复旧值并提示错误。

### 协议文本

- 精确占位符在实时渲染和历史渲染中不可见。
- 占位符与正常文本混合时只删除占位符。
- tool_calls、reasoning_content 和 thinking blocks 不丢失。

### 稳定性门禁

- Harness、LLM events、history/turn renderer 测试。
- system settings 后端与前端测试。
- Provider 请求 byte-golden、prompt byte-stability、DeepSeek Ledger 与 token accounting。
- 完整 Python 测试、前端测试、前端生产构建和 `git diff --check`。

## 验收标准

1. 默认配置下行为仍限制为 100 Step。
2. GUI 可保存 10–1000 的任意整数，新 Turn 使用新值，运行中 Turn 不变。
3. 达到上限后数据库、事件和 GUI 一致显示失败/未完成。
4. 最后一个工具结果可在历史中看到，下一 Turn 可继续。
5. `Empty message content sanitised` 不再对用户可见。
6. NoProgressGuard、Provider 请求字节、Prompt Ledger、缓存与 Token 统计均无回归。
7. 不新增自动续跑、无限模式、默认值静默回退或其他兜底路径。
