# DeepSeek 独立不可变请求账本设计

## 目标

在完全保留 Kimi 及其他 Provider 现有消息构造、显式 `cache_control`、历史维护与压缩行为的前提下，为 DeepSeek 官方接口增加独立的 append-only 请求视图，使同一会话后续请求尽可能完整复用之前请求的字节级前缀。

## 路由边界

只有同时满足以下条件的主会话请求使用新路径：

- 模型属于 DeepSeek 家族；
- endpoint 是 `https://api.deepseek.com`、`/v1` 或 `/beta`；
- `cache_scope == "main"`；
- 请求需要持久化且存在 `parent_session`。

Kimi、Anthropic、聚合网关、子代理和辅助模型继续使用现有路径。

## 架构

### 独立账本

SQLite 新增 DeepSeek prompt generation、消息和工具快照表。账本只保存真正发给 DeepSeek 的 canonical 消息，不替代用户可见的普通 `messages` 表。

每个 generation 由会话、模型、endpoint 和作用域标识。其消息只能按顺序追加；旧行不能更新。system 与工具 schema 共同构成稳定 request header：内容不变时复用当前 generation，任一字段真实变化时创建新 generation，旧快照保持只读。

### 首次引导

旧会话第一次进入 DeepSeek 路径时，以当前已构造请求作为 generation 的 seed。首次请求是冷启动；此后模型输出、工具调用结果及新用户输入同步追加到账本。

### 请求生命周期

1. `prepare_turn_messages` 保持现有逻辑，供 Kimi 与普通历史继续使用。
2. DeepSeek 请求进入 `_chat_with_model_generator` 后，从账本取得当前 generation。
3. 没有 generation 时，以当前请求 seed；已有 generation 时先比较稳定 request header，再要求其历史前缀仍有效，并只追加本轮尚未记录的尾部输入。
4. 每次实际 API 调用前，消息必须等于账本已有前缀加本轮新尾部。
5. assistant/tool 消息在普通历史落库成功后，同步 append 到 DeepSeek 账本。

### 失效与切代

以下事件创建新 generation，而不修改旧 generation：

- 模型、endpoint、system prompt 或 tool contract 改变；
- 普通历史发生撤销、摘要、媒体 canonical 化或无法证明只追加；
- 请求超过上下文并执行现有压缩；
- 账本尾部与当前请求无法安全对齐。

切代当轮允许冷启动，后续重新获得稳定前缀。

## 一致性与错误处理

- 普通历史是产品行为的事实来源；DeepSeek 账本是缓存优化视图。
- 账本写入与普通消息批量写入均经过同一个 `MemoryManager` 可重入锁，各自使用明确的 SQLite 事务。
- 账本不允许静默修补、重排或猜测对齐；发现不一致即显式切代。
- DeepSeek 账本故障不得静默回退为普通请求或伪造 append-only 数据；缺失存储管理器、并发位置冲突等状态应立即失败。

## 验证

- Kimi 请求测试证明消息和 `cache_control` 行为未改变。
- DeepSeek 连续两轮测试证明第二轮 messages 以第一轮完整输入和输出为前缀。
- 工具轮测试覆盖 assistant tool_calls、多个 tool result 和最终 assistant 的顺序。
- tool schema 冻结测试证明运行中 registry/MCP 变化不改变当前 generation。
- 切代测试覆盖撤销或历史不一致，不改写旧 generation。
