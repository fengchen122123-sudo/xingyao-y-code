# Token 与缓存命中统计正确性设计

## 背景

当前实现同时维护两类含义不同的数据：

- `session_token_usage.cumulative_*` 是进程内所有 LLM 请求的累计消耗，包含主代理、子代理和辅助请求；
- `_cache_usage_states` 是按会话与缓存作用域维护的缓存命中统计。

TUI 状态栏主要读取第一类数据，GUI 还错误地使用
`cumulative_cached / cumulative_total` 计算命中率。缓存命中 token 属于输入 token，正确分母必须是
`prompt_tokens`，不能包含输出 token。现有缓存状态的 key 也只有 `session + scope`，切换模型或接口后会把两条不同缓存链混在同一会话累计值中。

本阶段只解决统计口径与会话隔离，不改变请求内容、缓存保活、上下文维护和 DeepSeek 请求协议。

## 目标

1. 服务端 usage 是真实 token 的唯一来源，本地估算只用于请求返回前的上下文窗口占用。
2. 缓存命中率始终使用 `cached_prompt_tokens / prompt_tokens`。
3. TUI 与 GUI 共用同一份统计快照，不分别实现计算公式。
4. 缓存统计按 `session + model + endpoint + scope` 隔离；用户界面只展示 `main` 作用域。
5. 同时展示本轮命中率、当前会话热路径命中率和当前会话全量命中率，避免把不同口径都叫作“命中率”。
6. token 明细异常时尽早暴露，不静默夹断、补齐或伪造服务端数据。

## 非目标

- 本阶段不修改 `token_usage` 数据库表结构。
- 本阶段不解决历史消息重写、图片 canonical 化、DeepSeek thinking 契约或 keepalive 策略。
- 本阶段不把进程内会话统计伪装成跨进程历史统计；应用重启后，三种缓存命中率从空状态重新累计。
- 本阶段不根据本地 tokenizer 反推服务端计费 token。

## 方案比较

### 方案 A：只修 GUI 分母

把 `cumulative_total` 改成 `cumulative_prompt`，改动最小，但仍然混入其他会话、模型、子代理和辅助请求。显示值可以复算，却不能回答当前会话的 DeepSeek 缓存表现。

### 方案 B：切会话时重置全局累计字典

实现简单，但会丢掉进程级计费累计；后台子代理与前台切会话并发时仍可能串账。切回旧会话也无法恢复该会话原来的统计状态。

### 方案 C：统一只读快照，分离计费累计与缓存遥测

保留进程级累计用于费用观察；缓存遥测独立按请求身份分桶，并通过一个只读接口同时提供 TUI 与 GUI 所需字段。切换会话或模型只是切换读取的桶，不清空其他桶。

采用方案 C。它直接修复口径错误和状态串扰，且无需在本阶段迁移数据库。

## 数据来源与不变量

### 服务端 usage 归一化

每个完成的流式请求只记账一次，优先读取最终 usage chunk：

- `prompt_tokens`：服务端返回的输入 token；
- `completion_tokens`：优先读取服务端字段；字段缺失时，只有在 `total_tokens >= prompt_tokens` 的前提下才使用 `total_tokens - prompt_tokens`；
- `total_tokens`：服务端返回的总 token；
- `cached_prompt_tokens`：优先读取 `prompt_tokens_details.cached_tokens`，否则读取 DeepSeek 原生 `prompt_cache_hit_tokens`；
- `cache_miss_tokens`：如果服务端明确返回 `prompt_cache_miss_tokens`，仅用于一致性检查，不替换 `prompt_tokens`。

强制不变量：

- 所有字段必须可转换为非负整数；负数或非整数是异常，不能通过 `max(0, value)` 静默修正；
- `cached_prompt_tokens <= prompt_tokens`；
- 当服务端明确提供 hit 与 miss 时，必须满足
  `prompt_cache_hit_tokens + prompt_cache_miss_tokens == prompt_tokens`；
- 当 `total_tokens`、`prompt_tokens` 和 `completion_tokens` 都存在时，必须满足
  `prompt_tokens + completion_tokens == total_tokens`。

一致性校验失败时：

- 保留原始 usage 供日志诊断；
- 拒绝把本次 usage 写入正式 token 统计与 `token_usage` 表，避免错误记录污染后续汇总；
- 缓存命中率标记为不可用；
- 记录明确警告，包含模型、接口、会话、作用域和失败的不变量；
- 不影响已经生成的模型回复。

原始 usage 只进入诊断日志，不伪装成合法计费记录。这样会形成一条显式可见的“拒绝记账”事件，优先保证现有 token 记录不被错误数据污染。

### 两类状态严格分离

进程级计费累计继续保存在 `session_token_usage.cumulative_*`，语义明确为“本进程所有 LLM 请求累计”，不再用于当前会话缓存命中率。

缓存遥测状态使用以下 key：

```text
(session_id, normalized_model, normalized_api_base, cache_scope)
```

其中模型名转小写；接口去掉末尾 `/` 并转小写；空 scope 规范化为 `main`。

每个桶保存：

```text
latest_prompt, latest_cached, latest_rate
cumulative_prompt, cumulative_cached, cumulative_rate
hot_prompt, hot_cached, hot_rate, hot_turns
invalidation_reason, usage_valid
```

热路径只累计客户端判定为严格追加、且 usage 校验有效的请求。冷启动、模型/接口变化、工具契约变化、上下文重写请求进入会话全量统计，但不进入热路径统计。

## 公共读取接口

在 `core/llm.py` 增加线程安全的只读接口：

```python
get_token_usage_snapshot(
    session_id,
    model_name,
    api_base,
    cache_scope="main",
) -> dict
```

返回值分为三组：

```text
window: latest_total / latest_prompt / max_window
process: cumulative_total / cumulative_prompt / cumulative_completion / cumulative_cached
cache: latest / hot / session_all / hot_turns / invalidation_reason / usage_valid
```

接口在 `token_usage_lock` 内复制数据，调用方只能读取副本。TUI 和 GUI 不再直接拼装缓存命中率公式。

当当前桶没有真实请求时，缓存统计返回 `available=false`，界面显示 `--`，不能显示成 `0%`；`0%` 只代表服务端真实返回了零命中。

## 写入流程

流式请求结束时执行一次统一记账：

1. 从最终 usage chunk 归一化服务端字段；
2. 校验字段一致性；
3. 更新进程级计费累计；
4. 仅当 `cache_scope == "main"` 且 usage 有效时，更新当前会话、模型、接口桶的缓存遥测；
5. 持久化服务端 token；
6. 推送 UI token 事件。

子代理、视觉描述、压缩摘要等非 `main` 请求仍进入进程级计费累计和持久化记录，但不会污染用户看到的主代理缓存命中率。

## 界面语义

### TUI

状态栏保持紧凑，显示当前上下文占用，并展示当前会话主代理的三种命中率：

```text
本轮 99% · 热路径 99% · 会话全量 91%
```

需要展示 token 数值的详细位置使用同一个快照：

```text
本轮 输入 36.8K / 命中 35.2K
当前会话 输入 420.0K / 命中 382.2K
```

如果 usage 不可用则显示 `--`，并在已有失效原因位置显示异常摘要。

### GUI

保留旧字段 `cached_pct` 作为兼容别名，但其值改为当前会话全量命中率，正确分母为当前桶的 `cumulative_prompt`。新增：

```text
cache_latest_pct
cache_hot_pct
cache_session_pct
cache_available
cache_hot_turns
cache_invalidation_reason
prompt_tokens
cached_tokens
```

前端类型与 store 同步扩展。旧前端只读取 `cached_pct` 时仍能工作，新前端可以明确展示三个口径。

## 会话与模型切换

- 切换会话：重新计算上下文窗口占用；缓存统计读取目标会话对应桶。
- 切回旧会话：恢复该会话在本进程内已累计的数据。
- 切换模型或 endpoint：读取新的空桶，直到首个真实 usage 返回；旧模型桶保留。
- 重命名会话：缓存遥测状态必须把旧 session key 原子迁移到新 session key，避免统计突然归零。
- 清空或删除会话：清理对应缓存遥测桶，避免同名新会话继承旧统计。

## 并发与错误处理

- 所有 token 状态的读写继续使用 `token_usage_lock`。
- 后台 worker 只更新状态并发送既有事件，不直接触碰 prompt_toolkit UI。
- 不在持锁期间做数据库 IO、日志 IO 或前端事件发送。
- usage 缺失时不增加任何真实 token 计数；请求前的本地估算只更新窗口占用字段。
- 日志与 UI 明确区分“usage 缺失”“usage 不一致”“真实 0% 命中”。

## 测试策略

按 TDD 顺序覆盖：

1. usage 归一化：OpenAI 明细、DeepSeek hit/miss、缺失 completion、异常负数、hit 超过 prompt、hit+miss 不等于 prompt、total 不守恒；
2. 聚合隔离：不同 session、model、endpoint、scope 互不污染；
3. 热路径与会话全量：冷启动只进入全量，严格追加进入两者；
4. 缺失与真实零命中的显示区别；
5. GUI 兼容字段使用 `cached / prompt`，不再使用 `cached / total`；
6. TUI 与 GUI 对同一快照输出相同的三种百分比；
7. 切换、重命名、清空、删除会话的状态生命周期；
8. 并发读取返回稳定副本，不暴露内部可变字典；
9. 现有 token 持久化测试继续通过；
10. 相关测试、全量测试与 `py_compile` 全部通过。

## 验收标准

- 给定 `prompt=1000, cached=990, completion=100, total=1100`，所有界面命中率都是 `99%`，绝不出现 `90%`。
- 两个会话交替请求后，各自的会话全量统计可独立复算。
- 同一会话切换模型或接口后，新旧统计互不合并。
- 子代理产生大量 token 后，主代理的三种命中率不变，进程级计费累计正常增长。
- 服务端返回矛盾 usage 时，界面显示不可用并有诊断信息，不展示伪造百分比。
- 所有显示的命中率都能用同时返回的 `cached_tokens / prompt_tokens` 复算。
