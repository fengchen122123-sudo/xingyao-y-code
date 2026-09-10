# 基元律动 API 接口清单（reference）

Base URL：`https://tokenrhythm.studio`
控制台接口前缀 `/api`，对外模型接口前缀 `/v1`。

**鉴权两种方式，不要混用：**
- 控制台接口 → 会话 cookie（`tr_session` + `tr_csrf`），由 `tr-session.ps1` 维护
- `/v1/*` 模型接口 → `Authorization: Bearer sk_tr_...`（API Key）

**状态标记**：✅ 已实测通过 ｜ ◐ 已实测但受平台条件限制 ｜ ⚠️ 尚未验证，调用前先小范围试

> 本清单只覆盖账号日常功能的公开接口，用于帮用户完成查询与管理。
> 不包含任何扫描、探测、越权或压测内容；调用一律遵循平台既有能力。

---

## 认证

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| POST | `/api/auth/login` | body `{account, password}`，返回 `data.user`，Set-Cookie 下发会话 | ✅ |
| GET | `/api/auth/me` | 校验会话，`New-TrSession` 登录后必调一次 | ✅ |
| POST | `/api/auth/logout` | 登出 | ✅ |
| POST | `/api/auth/login-or-register` | 仅短信渠道：body 需 `channel:"sms"` + `verificationToken` + `termsAccepted` | ✅ |
| POST | `/api/auth/exchange-code` | 体验卡兑换，body `{code}` | ✅ |

**`login-or-register` 注意**：`channel` 只接受 `sms`（传 `password` 返回 `400 channel 取值无效`）；
token 缺失或过短返回 `400 verificationToken 长度不足`。要拿到 `verificationToken` 必须先走短信验证码流程，
**不要代替用户索取短信验证码**。

## 账号信息

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/me` | 返回 `id / name / emailMasked / phoneMasked / status / role / joinedAt / features` | ✅ |
| PUT | `/api/me/name` | 改昵称，body `{name}`（**账号写操作**） | ⚠️ |
| PUT | `/api/me/password` | 改密码，body 含旧密码与新密码（**账号写操作**） | ⚠️ |

> 这两个接口**刻意不做自动化实测**：改昵称会写入操作日志，改密码一旦上游不校验旧密码就等于把用户锁在门外。
> 只在用户明确指示时调用，且改密码必须先确认新旧密码都由用户提供。查询类需求不需要它们。

## 余额与用量

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/wallet/summary` | 余额总览 | ✅ |
| GET | `/api/usage-summary` | 全量累计用量与成本 | ✅ |
| GET | `/api/usage/panel?range=&page=&pageSize=` | 分时间窗用量面板 | ✅ |
| GET | `/api/wallet/expiring-credits?page=&pageSize=` | 即将过期的赠送额度，返回 `{asOf, summary{expiringBalanceCny,nextExpiryAt}, list, total}` | ✅ |

**`range` 取值**：`1h` / `3h` / `today` / `3d` / `7d` / `14d` / `30d` / `all` / `custom`（custom 需再传 `startDate`、`endDate`）。

### `/api/wallet/summary` 关键字段

```json
{"currency":"CNY","availableBalanceCny":"...","giftAvailableCny":"...","giftLockedCny":"...",
 "rechargeBalanceCny":"...","debtBalanceCny":"...","frozenBalanceCny":"...",
 "giftTotalCny":"...","giftStatus":"active","voidedGiftCny":"...","asOf":"..."}
```

### `/api/usage-summary` 关键字段

```json
{"calls":N,"successCalls":N,"errorCalls":N,"abortedCalls":N,
 "inputTokens":N,"outputTokens":N,"tokenRequestCount":N,
 "imageRequestCount":N,"successfulImageCount":N,
 "costCny":"...","tokenCostCny":"...","imageCostCny":"...",
 "balanceCny":"...","frozenBalanceCny":"...","availableBalanceCny":"...",
 "expiringBalanceCny":"...","nextExpiryAt":null}
```

## 调用日志

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/call-logs/page?page=&pageSize=&status=&modelId=&traceId=&startAt=&endAt=` | 分页调用日志 | ✅ |
| GET | `/api/call-logs/{traceId}/reasoning` | 取某次调用的思考链，返回 `{traceId, modelId, apiSurface, available, content}` | ✅ |
| POST | `/api/call-logs/export` | **注意是 POST**，body `{traceId?, modelId?, status?, startAt?, endAt?}`，返回异步导出任务 | ✅ |
| GET | `/api/export-tasks/page?page=&pageSize=` | 导出任务列表 | ✅ |
| GET | `/api/export-tasks/{id}/download-ticket` | 换取下载地址，返回 `{url, mode(oss\|proxy), fileName}` | ✅ |

**`status` 过滤**：空 = 全部；`error` = 仅失败（传具体数字码无效）。

**时间参数格式（踩过坑）**：`startAt` / `endAt` 必须是 `YYYY-MM-DD` 或 ISO 8601 **UTC**，
传 `2026-09-10T09:50` 这种本地时间会被拒：`调用日志时间筛选必须使用 YYYY-MM-DD 或 ISO 8601 UTC 格式`。

**导出是异步的**：`POST /call-logs/export` 先返回 `status: queued`，之后轮询
`/api/export-tasks/page` 变 `done` 才有 `downloadReady: true`（实测约 3~5 秒）。
失败时 `failedReason` 会写明原因。

返回 `data.{list, total, page, pageSize, nextCursor}`。单条记录关键字段：
`requestAt / time / modelId / keyName / status / endpoint / stream / latencyMs /
inputTokens / outputTokens / totalTokens / reasoningTokens / cacheReadTokens /
costCny / traceId / requestId / provider / clientApp / finishReason`。

> `time` 只有 `HH:mm:ss`，跨天判断要用 `requestAt`。
> 生图调用**不在此接口**中出现，只计入 `usage-summary.imageRequestCount`。

## API Key 管理

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/api-keys` | 列表，返回 `id / name / maskedKey / keyPrefix / status / createdAt / lastUsedAt` | ✅ |
| POST | `/api/api-keys` | body `{name}`，**响应含一次性完整 key**（**写操作，需确认**） | ✅ |
| POST | `/api/api-keys/{id}/disable` | 停用（**写操作，需确认**） | ✅ |
| POST | `/api/api-keys/{id}/delete` | 删除，返回 `status: deleted`（**写操作，需确认**） | ✅ |
| POST | `/api/api-keys/batch-delete` | body `{ids:[...]}`，返回 `{successCount, failedCount, failedItems[{id,message}]}` | ✅ |
| GET | `/api/api-keys/usage?range=&startDate=&endDate=` | 各 Key 用量，返回 `{range, startAt, endAt, timezone:"Asia/Shanghai", snapshotAt, list[{apiKey, ...}]}` | ✅ |
| GET | `/api/api-keys/{id}/usage-panel?range=&page=&pageSize=` | 单 Key 用量面板，返回 `{apiKey, ...}` | ✅ |
| POST | `/api/api-keys/{id}/copy-log` | 复制完整 Key 的行为上报，返回 `data: true` | ✅ |

**上限 10 个 Key**，超限报 `api_key_limit_exceeded`。
Key 状态值：`enabled` / `disabled` / `deleted`（删除后列表不再返回）。

**安全探测技巧**：想确认 CSRF 与接口连通性又不想产生副作用时，
`POST /api/api-keys/batch-delete` 传一个伪 UUID → 返回 `successCount: 0, failedCount: 1`，零影响。

## 其他控制台接口

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/models` | 模型清单 + 能力 + 定价（需登录） | ✅ |
| GET | `/api/docs` | 站点文档全文，**公开无鉴权** | ✅ |
| GET | `/api/conversations/page?keyword=&modelId=&page=&pageSize=` | 对话记录 | ✅ |
| GET | `/api/audit-logs` | 操作日志，**返回数组、不分页**（`/api/audit-logs/page` 是 404） | ✅ |
| GET | `/api/export-tasks/page?page=&pageSize=` | 导出任务 | ✅ |
| POST | `/api/playground/send` | 站内 Playground 对话，**会话鉴权，不需要 API Key** | ✅ |

`/api/audit-logs` 单条结构：`{action, description, object, source, ipMasked, networkKind, createdAt}`，
`action` 例如 `login` / `logout`。

### `/api/playground/send`（测模型连通性首选）

不用建 Key、不用 API Key，登录会话即可直接试模型 —— **验证模型连通性的最低成本方式**。
技能已封装为 `Test-TrModel -Session $s -Model <准确ID> [-Stream]`，返回耗时 / 内容 / Token / 花费 / traceId
（详见 SKILL.md「模型连通性测试」一节）；以下是它背后的原始契约。

```json
POST /api/playground/send
{"model": "deepseek-v4.1-flash-expires-on-0910",
 "messages": [{"role": "user", "content": "只回复四个字：连通正常"}],
 "stream": false,
 "reasoning": false}
```

- `content` **必须是字符串**（实测）。传 OpenAI 那种 `[{type:"image_url",...}]` 数组会返回
  `400 BAD_REQUEST messages.0.content 类型错误`。
- 该端点**只处理纯文本，不支持图片输入**。要测视觉能力必须走 `/v1/chat/completions` + API Key。
- `stream: true` 时返回 SSE：`data: {"choices":[{"delta":{"reasoning_content":"..."}}]}`，
  末尾是 `data: {"type":"done", ..., "usage":{...}, "costCny":"..."}` 和 `data: [DONE]`。
- 非流式响应：`data.{conversationId, message{content, reasoning_content, model}, usage{inputTokens, outputTokens}, costCny, billingPending, traceId}`。
- 调用会正常计入 `/api/call-logs/page`，`endpoint` 字段显示为 `/playground/send`。

### `/api/models` 关键字段

`id / name / type(chat|image) / status(online|testing) / contextWindow / maxOutputTokens /
modalities[] / capabilities{openai,anthropic,stream,tools,reasoning,vision,video,imageEditing,...} /
protocolCapabilities{openai,anthropic,responses} / providerDisplayName /
effectiveInputPrice / effectiveOutputPrice / effectiveCacheReadPrice /
billingMode(per_1m_tokens|per_image) / pricePerImage`。

## 对外模型接口（用 API Key，`Bearer sk_tr_...`）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/v1/models` | 可用模型（仅 chat 类） | ✅ |
| POST | `/v1/chat/completions` | OpenAI 协议，支持 `stream` | ✅ |
| POST | `/v1/messages` | Anthropic 协议，需 `anthropic-version` 头；仅部分模型支持 | ✅ |
| POST | `/v1/images/generations` | body `{model, prompt, n, size, response_format:"url"}` | ✅ |
| POST | `/v1/images/edits` | multipart 垫图改图：`model` + `prompt` + `image`（1 张 ≤10MB，jpg/png） | ✅ |
| POST | `/v1/embeddings` | 向量嵌入；端点可用，但平台当前无 embedding 模型 | ◐ |

**生图模型**：`qwen-image-2.0` / `wan2.7-image`，计费 `per_image`，**¥0.20/张**。
**尺寸**：`1024x1024` / `1280x720` / `720x1280` / `1280x1280` / `1024x1792` / `1792x1024`。
返回 `data[0].url` 为 OSS 签名地址，**24 小时过期**，必须立即下载转存。

### `/v1/images/edits` 实测要点

- 大图（约 1.4MB PNG）曾触发上游拉取失败：`400 BAILIAN_IMAGE_ERROR Failed to download image ... HTTP 403`；
  换成 1.7KB 的小图后同参数一次通过。**垫图建议先压缩，失败时优先怀疑图体积，而非参数。**
- 成功返回与文生图同构：`{created, data:[{image_id, url}], cost_cny, billing_pending, trace_id}`。
- `prompt` 传中文时用 `curl -F "prompt=<提示词文件"` 从 UTF-8 文件读取，比命令行内联更稳。

### `/v1/embeddings` 现状

端点存在且能正常解析请求（实测返回 `400 MODEL_NOT_AVAILABLE 模型不可用：text-embedding-v3`），
但平台目前**没有任何 embedding 模型**，因此无法验证成功响应的结构。
用户若要求向量化，如实告知暂不可用，不要伪造结果。

---

## CSRF 机制实测结论

非 GET 请求的校验链是：cookie `tr_csrf` == 请求头 `X-CSRF-Token` **且** 请求带 `Sec-Fetch-Site` 头。

| 试验 | 请求头 | 结果 |
|---|---|---|
| A | `X-CSRF-Token` + `Origin` + `Referer` | ❌ 403 CSRF_INVALID |
| B | A + `User-Agent` + `Accept` | ❌ 403 CSRF_INVALID |
| C | B + `Sec-Fetch-Site/Mode/Dest` | ✅ 通过（404 NOT_FOUND，说明已进入业务逻辑） |
| D | `X-CSRF-Token` + `Origin` + 仅 `Sec-Fetch-Site: same-origin` | ✅ 通过 |
| E | 同 D，改用 `Sec-Fetch-Site: same-site` | ✅ 通过 |

**结论：`X-CSRF-Token` 的值一直是对的，唯一卡点是缺 `Sec-Fetch-Site` 头；该头的值不影响结果。**

**判别技巧**：想验证 CSRF 是否通过而不产生副作用，可对一个不存在的 Key ID 发
`POST /api/api-keys/00000000-0000-0000-0000-000000000000/disable`：
- 返回 `404 NOT_FOUND` → CSRF 已通过
- 返回 `403 CSRF_INVALID` → CSRF 被拦

## 站点能力开关

平台通过运行时配置控制模块开关，决定哪些功能真的可用：

```
apiBaseUrl=/api  useMock=false  passwordAuthEnabled=true
referralsEnabled=true  paymentsEnabled=false
identityVerificationEnabled=false  webToolsEnabled=true  idleTimeoutMinutes=1440
```

影响：`paymentsEnabled=false` ⇒ 充值 / 发票 / 钱包明细不可用；
`identityVerificationEnabled=false` ⇒ 实名认证不可用。
用户问到这些功能时，直接告知平台尚未开放，不要尝试绕过。
