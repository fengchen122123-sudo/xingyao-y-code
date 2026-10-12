# DeepSeek 请求级缓存诊断设计

## 目标

为每个 LLM 逻辑轮次留下可查询、无正文泄露的缓存诊断记录，使低命中事件可以区分客户端前缀变化、请求身份变化、prompt 缩短、空闲间隔和无法确定的 provider/网关行为。同一形状的网络重试属于同一逻辑轮次，只落一行。

本阶段不改变 prompt 内容、缓存保活、历史压缩或 DeepSeek 请求参数，只增加观测能力并修正 `/usage` 的归因文案。

## 方案选择

### 扩展 `token_usage`

计费查询简单，但 usage 缺失或被严格校验拒绝时没有 token_usage 行，最需要诊断的请求反而无法记录；大量哈希与形状字段也会污染计费事实表。

### 独立 `cache_request_diagnostics` 表

每个请求独立记录请求前形状和响应后 usage，允许 usage 无效/缺失；计费表继续只保存合法服务端 token。采用此方案。

### JSONL 日志

不需要数据库迁移，但无法稳定聚合、关联会话或支持 `/usage`，也难以处理并发写入，不采用。

## 隐私与稳定性边界

诊断表绝不保存 system prompt、用户消息、工具参数或工具结果正文，只保存 SHA-256、数量和规范化 JSON 字节长度。诊断代码不得修改传给 provider 的 `messages`、`tools` 或 kwargs。

哈希输入使用与请求一致的规范化 JSON：UTF-8、`ensure_ascii=False`、`sort_keys=True`、紧凑分隔符。以下字段均为请求发送前对只读副本计算：

- `messages_sha256`：完整 messages 数组；
- `system_sha256`：所有 system 消息数组；
- `tools_sha256`：实际 tools schema；
- `message_fingerprints`：不落库，仅用于进程内计算公共消息前缀长度；
- `message_count`、`system_message_count`；
- `request_bytes`、`system_bytes`、`tools_bytes`；
- `common_prefix_messages`：与同一 session/scope 上一次请求逐消息相等的前缀数量。

## 数据模型

新增表 `cache_request_diagnostics`：

```text
id INTEGER PRIMARY KEY AUTOINCREMENT
ts INTEGER NOT NULL
session_id TEXT NOT NULL
model TEXT NOT NULL
api_base TEXT NOT NULL
cache_scope TEXT NOT NULL
request_reason TEXT NOT NULL
messages_sha256 TEXT NOT NULL
system_sha256 TEXT NOT NULL
tools_sha256 TEXT NOT NULL
message_count INTEGER NOT NULL
system_message_count INTEGER NOT NULL
common_prefix_messages INTEGER NOT NULL
request_bytes INTEGER NOT NULL
system_bytes INTEGER NOT NULL
tools_bytes INTEGER NOT NULL
prompt_tokens INTEGER NULL
cached_tokens INTEGER NULL
cache_miss_tokens INTEGER NULL
cache_reported INTEGER NOT NULL
usage_valid INTEGER NOT NULL
usage_errors TEXT NOT NULL
```

索引：`(session_id, model, api_base, cache_scope, id)` 与 `ts`。

`NULL` 保留“服务端未报告”的语义；真实零命中保存整数 0。`usage_errors` 是 JSON 字符串数组，不把多个错误拼成不可解析文本。

## 数据流

1. 每个 LLM 逻辑轮次在首次 provider 调用前，在 `_strip_media_and_merge` 和实际 tools 选择完成后构建不可变 request diagnostic。
2. 请求重试如果 messages 形状完全相同，不重复构建形状状态；只有真正取得一次响应或最终失败时写一行。
3. 流结束取得最终 usage 后，复用 `_normalize_server_usage` 的结果填充 hit/miss 与有效性。
4. `stream_usage is None` 时仍写行，`usage_valid=0`，错误为 `usage missing`。
5. 正式 token 记账与诊断持久化彼此独立：诊断失败只记录日志，不得影响回复；无效 usage 不进入 `token_usage`，但必须进入诊断表。
6. DB 写入在 token 状态锁外完成，并在 `MemoryManager` 内提交事务。

## 诊断归因

`MemoryManager.query_cache_diagnostic_summary(start_date, end_date, ...)` 读取低命中事件并按时间排序。每一行只做证据充分的分类：

- `cold_start`：`request_reason == 冷启动`；
- `identity_change`：模型或接口变化；
- `tool_contract_change`：工具契约变化；
- `client_history_rewrite`：上下文压缩或历史重写；
- `prompt_drop`：同身份上一条合法 prompt 存在，当前 prompt 缩短超过 2%；
- `idle_gap`：同身份上一条请求存在，时间间隔大于配置的 keepalive 秒数，且没有更强客户端证据；
- `provider_or_unknown`：以上证据都不成立。

分类是诊断推断，不把 `provider_or_unknown` 描述成已证明的 provider 驱逐。低命中阈值沿用输入至少 20,000 且 hit/prompt < 50%；缓存明细未报告的请求单独统计，不当成 0% 命中。

## `/usage` 输出

保留 token 聚合表，在其后显示：

```text
低命中诊断 12 次：冷启动 2、历史重写 3、prompt 缩短 4、空闲间隔 1、未确定 2
```

删除“属 provider/网关侧缓存驱逐，客户端无法避免”绝对结论。存在 `provider_or_unknown` 时显示“缺少足够证据，需结合 endpoint A/B 或服务端日志确认”。

## 错误处理

- 形状哈希构建失败：请求直接暴露错误，不发送缺少诊断形状的主请求；这是纯 JSON 数据，不应存在可恢复失败。
- 诊断 DB 写入失败：记录 warning，但不撤销已完成的模型回复或合法 token 记账。
- 旧数据库通过 `CREATE TABLE IF NOT EXISTS` 与索引语句自动升级，无破坏性迁移。
- 查询遇到旧时段无诊断数据时返回零统计，不从 `token_usage` 猜测归因。

## 测试与验收

1. 相同请求哈希和字节数确定性一致；正文不出现在诊断 dict/DB。
2. 严格追加请求的 `common_prefix_messages` 等于上一请求消息数；历史中间变化时小于上一请求消息数。
3. DeepSeek hit/miss、真实 0、未报告、invalid、missing usage 均保持不同语义。
4. 一次实际 provider 请求只写一行；同形状网络重试不重复写。
5. session/model/endpoint/scope 字段准确，主代理与子代理可分开查询。
6. 分类测试逐一覆盖七类，prompt drop 使用 2% 字面阈值。
7. `/usage` 不再输出无法证明的 provider 绝对归因。
8. 相关测试、全量 pytest、py_compile、前端测试与构建全部通过。
