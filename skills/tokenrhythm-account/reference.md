# 基元律动 API 接口清单（reference）

Base URL：`https://tokenrhythm.studio`（控制台 + 全部后端接口）
官网：`https://tokenrhythm.cn` —— **纯静态落地页**（Astro 构建、阿里云 OSS 托管），
`/api/*` 一律返回 OSS `NoSuchKey`，**不含任何后端接口**；它只提供产品介绍、客户端下载与控制台跳转入口。
控制台接口前缀 `/api`，对外模型接口前缀 `/v1`。

**官网提供的入口链接**：
- 控制台登录 / 注册：`https://tokenrhythm.studio/login`、`/register`
- 文档中心：`/docs`（数据来自公开接口 `GET /api/docs`）
- 客户端下载（阿里云 OSS）：
  `https://opensquilla-releases.oss-cn-beijing.aliyuncs.com/releases/latest/OpenSquilla-win-x64.exe`（Windows）、
  `.../OpenSquilla-mac-arm64.dmg`（macOS）

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
| POST | `/api/auth/register` | 注册新账号（**写操作**） | ⚠️ |
| POST | `/api/auth/sms/send` | 发送短信验证码（**写操作，会真发短信**） | ⚠️ |
| POST | `/api/auth/sms/verify` | 校验短信验证码，换取 `verificationToken` | ⚠️ |
| POST | `/api/auth/password/reset` | 重置密码（**账号写操作**） | ⚠️ |

**`login-or-register` 注意**：`channel` 只接受 `sms`（传 `password` 返回 `400 channel 取值无效`）；
token 缺失或过短返回 `400 verificationToken 长度不足`。要拿到 `verificationToken` 必须先走短信验证码流程，
**不要代替用户索取短信验证码**。

**短信类接口（`sms/send` 等）永远不自动调用**：会向用户手机真实发信、留痕并触发频控，
只在用户明确要求时执行，并把结果如实回报。

## 账号信息

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/me` | 账号信息，见下方字段表 | ✅ |
| PUT | `/api/me/name` | 改昵称，body `{name}`（**账号写操作**；该路径 GET 返回 404） | ⚠️ |
| PUT | `/api/me/password` | 改密码，body 含旧密码与新密码（**账号写操作**） | ⚠️ |
| GET | `/api/me/restriction` | 账号限制状态；无限制时 `data: null` | ✅ |
| GET | `/api/me/deletion/eligibility` | 注销资格：`{user, allowed, blockers[{code,message,resolveAt}]}` | ✅ |
| POST | `/api/me/deletion/sms/send` | 注销流程发短信（**写操作**） | ⚠️ |
| POST | `/api/me/deletion/sms/verify` | 注销流程校验短信（**写操作**） | ⚠️ |
| DELETE | `/api/me/deletion` | 提交账号注销（**不可逆，绝不自动调用**） | ⚠️ |

### `/api/me` 字段（实测）

```json
{"id":"<uuid>","name":"Y","emailMasked":null,"phoneMasked":"132****9984","status":"active",
 "role":"user","joinedAt":"2026-08-06T12:32:37.147Z",
 "features":{"manualBalanceCreditEnabled":false,"referralTestIdentityEnabled":false},
 "releaseCohort":"base"}
```

`role`：`user` / `admin`。

实测 `GET /api/me/deletion/eligibility` 对仍有充值余额的账号返回：

```json
{"user":{...},"allowed":false,
 "blockers":[{"code":"OUTSTANDING_BILL","message":"存在充值余额、欠费、非赠送冻结资金或未完成资金事项，请处理后重试","resolveAt":null}]}
```

> `PUT /api/me/name`、`PUT /api/me/password` **刻意不做自动化实测**：改昵称会写入操作日志，
> 改密码一旦上游不校验旧密码就等于把用户锁在门外。
> 只在用户明确指示时调用，且改密码必须先确认新旧密码都由用户提供。查询类需求不需要它们。

## 余额与用量

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/wallet/summary` | 余额总览 | ✅ |
| GET | `/api/wallet/transactions` | **钱包流水明细，无参调用**（见下方坑位） | ✅ |
| GET | `/api/wallet/expiring-credits?page=&pageSize=` | 即将过期的赠送额度，返回 `{asOf, summary{expiringBalanceCny,nextExpiryAt}, list, total}` | ✅ |
| GET | `/api/usage-summary` | 全量累计用量与成本 | ✅ |
| GET | `/api/usage/panel?range=&page=&pageSize=` | 分时间窗用量面板 | ✅ |

**`range` 取值**：`1h` / `3h` / `today` / `3d` / `7d` / `14d` / `30d` / `all` / `custom`（custom 需再传 `startDate`、`endDate`）。

**`/api/wallet/transactions` 的参数陷阱（实测）**：它走**严格 schema**，只接受自己的字段名。
带 `range` / `page` / `pageSize` / `limit` 中任意一个都会返回
`400 BAD_REQUEST 请求包含未知字段` —— **直接无参 GET 即可**，一次拿回 `data.items[]` 全量流水。

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

`/api/usage-summary` 实测还返回 `signupReward`、`tokenRequestCount`、`successfulImageCount` 等字段；
注意金额字段是**字符串**（如 `"109.90242602"`），别直接当 number 参与运算。

### `/api/wallet/transactions` 字段（实测）

```json
{"items":[{"transactionId":"<uuid>","type":"MODEL_USAGE","direction":"DEBIT","status":"POSTED",
 "amountCny":"0.00441256","currency":"CNY",
 "giftDeltaCny":"-0.00441256","rechargeDeltaCny":"0.00000000","debtDeltaCny":"0.00000000",
 "balanceAfterCny":"...","giftBalanceAfterCny":"...","rechargeBalanceAfterCny":"...","debtBalanceAfterCny":"...",
 "relatedOrderNo":null,"description":"模型调用扣费","occurredAt":"2026-09-13T08:30:16.511Z"}]}
```

`type` 实测值：`MODEL_USAGE`（模型调用扣费）；`direction`：`DEBIT` / `CREDIT`；`status`：`POSTED`。
`occurredAt` 为 UTC。

### `/api/wallet/expiring-credits` 字段（实测）

`summary.{expiringBalanceCny, nextExpiryAt}`；
`list[{id, source, sourceLabel, grantedCny, remainingCny, grantedAt, expiresAt}]`，
`source` 实测值 `REFERRAL_INVITER_REWARD`（`sourceLabel` 显示「邀请奖励」）。
**回答「我的额度什么时候过期」用这个接口最直接。**

### `/api/usage/panel` 字段（实测）

`data.summary` 除 `calls / successCalls / errorCalls / abortedCalls / inputTokens / outputTokens /
totalTokens / costCny` 外，还有缓存与推理维度：
`cacheReadTokens / cacheCreationTokens / cacheReadComplete / cacheWriteComplete /
cacheReadMissingCalls / cacheWriteMissingCalls / costBreakdownComplete / reasoningTokens`，
以及 `inputCostCny / outputCostCny / cacheReadCostCny / cacheCreationCostCny`（未拆分时为 `null`）。

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
`id / requestAt / time / modelId / keyName / status / endpoint / stream / latencyMs /
inputTokens / outputTokens / totalTokens / reasoningTokens / cacheReadTokens /
costCny / traceId / requestId / provider / clientApp / finishReason`。

2026-09 实测补充字段（同名老字段仍存在，新增的这几组更能说明调用来源）：

```json
{"id":"<uuid>","time":"16:29:27","requestAt":"2026-09-13T08:29:27.797Z",
 "endpoint":"/v1/chat/completions","apiSurface":"openai","stream":true,
 "requestId":"trace_...","source":"api_key","sourceLabel":"API Key 调用",
 "requestModelId":"deepseek-flash","actualModelId":"deepseek-flash",
 "opensquillaSessionId":null,"opensquillaTurnId":null,"opensquillaExecutionId":null,"opensquillaCallKind":null}
```

- `apiSurface`：`openai` / `anthropic`（对应 `/v1/chat/completions`、`/v1/messages`）
- `source`：`api_key` / 站内会话；`sourceLabel` 是它的中文展示名
- `requestModelId` 是调用方请求的模型，`actualModelId` 是实际路由到的模型 —— **两者不一致说明被平台路由改写**
- `opensquilla*` 字段只有 OpenSquilla 客户端发起的调用才有值，否则为 `null`

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

## 模型、文档与站点内容（**多数公开，免登录**）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/models` | 模型清单 + 能力 + 定价；**实测公开可访问，无需登录** | ✅ |
| GET | `/api/models/{id}` | 单个模型详情；**支持模糊 ID**（传 `deepseek-v4-flash` 会返回 `deepseek-v4-flash-0731`） | ✅ |
| GET | `/api/docs` | 文档全文数组，元素含 `{slug,title,category,summary,content[],contentMarkdown}`；公开 | ✅ |
| GET | `/api/docs/{slug}` | 单篇文档（实测 `/api/docs/overview`）；公开 | ✅ |
| GET | `/api/site-content` | 站点文案、导航、模型排序等运行时配置；公开 | ✅ |
| GET | `/api/legal-documents/current` | 用户协议 + 隐私政策全文（markdown）；公开 | ✅ |
| GET | `/api/search?keyword=&scope=` | 全局搜索，`scope` 取 `all` / `public` / `account` / `admin` | ✅ |

`/api/search` 返回 `data.{items[],total,page,pageSize}`，单条：
`{id, scope, type, title, summary, url, tags[], highlights[], metadata}`；`type` 实测值 `model`
（另有 doc / site / conversation / call_log / usage / api_key / audit_log / admin_* 等，结果由后端按权限裁剪）。
**免登录就能搜公开内容，是「找模型 / 找文档」最快的入口。**

`/api/docs` 实测返回 9 篇，slug 依次为：`overview`（快速开始）、`why-tokenrhythm`（关于我们·初心）、
`opensquilla`（接入 OpenSquilla）、`playground`、`api-integration`（API 接入与多协议支持）、`faq`、
`errors`（错误码）、`terms`（用户协议）、`privacy`（隐私政策）。
其中 **`errors` 是官方错误码文档** —— 遇到陌生错误码优先查它，别猜。

## 对话记录与 Playground

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/conversations/page?keyword=&modelId=&page=&pageSize=` | 对话记录列表 `{list,total,page,pageSize}` | ✅ |
| GET | `/api/conversations/{id}/messages` | 某会话的消息列表 | ⚠️ |
| GET | `/api/playground/messages?conversationId=` | 取 Playground 会话消息；无参返回 `data: []`，无效 id → `404 对话不存在或已删除` | ✅ |
| POST | `/api/playground/send` | 站内 Playground 对话，**会话鉴权，不需要 API Key** | ✅ |

## 其他控制台接口

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/audit-logs` | 操作日志，**返回数组、不分页**（`/api/audit-logs/page` 是 404） | ✅ |
| GET | `/api/export-tasks/page?page=&pageSize=` | 导出任务 | ✅ |

`/api/audit-logs` 单条结构：`{action, description, object, source, ipMasked, networkKind, createdAt}`，
`action` 例如 `login` / `logout`。

### `/api/playground/send`（测模型连通性首选）

不用建 Key、不用 API Key，登录会话即可直接试模型 —— **验证模型连通性的最低成本方式**。
技能已封装为 `Test-TrModel -Session $s -Model <准确ID> [-Stream]`，返回耗时 / 内容 / Token / 花费 / traceId
（详见 SKILL.md「模型连通性测试」一节）；以下是它背后的原始契约。

```json
POST /api/playground/send
{"model": "deepseek-v4.1-flash",
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

实测补充：`capabilities` 还含 `cacheRead` / `embeddings` / `audio` / `file` / `responses` 等布尔位，
`protocolCapabilities` 细分到每个协议（如 `openai.{available,stream,tools,reasoning,cacheRead,embeddings}`）——
**判断「模型能不能走某协议 / 支不支持工具或视觉」看这两个字段，比发试探请求更准更快。**

## 邀请与奖励

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/referrals/me` | 我的邀请码与奖励规则 | ✅ |
| GET | `/api/referrals/records?page=&pageSize=` | 邀请记录 `{list[...]}` | ✅ |

`/api/referrals/me` 实测字段：

```json
{"code":"rf_tr_...","historyVisible":true,"eligible":true,"canInvite":false,
 "registrationAllowed":false,"publicEnabled":false,"settlementEnabled":true,
 "todayRewardedCount":0,"rewardCny":"68.00000000","inviterRewardCny":"68.00000000",
 "successfulInviteCount":5,"pendingInviteCount":0,"dailyRewardLimit":10,
 "rewardValidityDays":31,"lifetimeRewardLimitCny":"680.00000000",
 "lifetimeRewardedCny":"340.00000000","lifetimeRemainingCny":"340.00000000"}
```

`/api/referrals/records` 单条：

```json
{"id":"<uuid>","invitee":{"displayName":"yo5dro","maskedPhone":"135****2219"},
 "status":"success","invitedAt":"...","activatedAt":"...","inviteeVerifiedAt":null,
 "nonGrantReasonCode":null,"inviteeRewardCny":"0.00000000","inviterRewardCny":"68.00000000",
 "inviterRewardCreditId":"<uuid>","inviterRewardGrantedAt":"...","inviterRewardExpiresAt":"..."}
```

## 对外模型接口（用 API Key，`Bearer sk_tr_...`）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/v1/models` | 可用模型（仅 chat 类） | ✅ |
| POST | `/v1/chat/completions` | OpenAI 协议，支持 `stream` | ✅ |
| POST | `/v1/messages` | Anthropic 协议，需 `anthropic-version` 头；仅部分模型支持 | ✅ |
| POST | `/v1/images/generations` | body `{model, prompt, n, size, response_format:"url"}` | ✅ |
| POST | `/v1/images/edits` | multipart 垫图改图：`model` + `prompt` + `image`（1 张 ≤10MB，jpg/png） | ✅ |
| POST | `/v1/responses` | OpenAI Responses 协议；**端点确认存在**（无 Key 请求返回 401 `authentication_error`），能否用取决于模型的 `capabilities.responses` | ◐ |

> **`/v1/*` 的错误体不统一**（2026-09-13 实测）：
> - `POST /v1/responses` → OpenAI 风格 `{"error":{"message":"未认证或登录已过期","type":"authentication_error","code":"UNAUTHORIZED"},"traceId":"..."}`
> - `GET /v1/models`、`GET /v1/images/generations` → 通用风格 `{"code":"UNAUTHORIZED","message":"未认证或登录已过期","traceId":"..."}`
>
> 但**未带 Key 一律返回 401 而不是 404** —— 看到 401 说明端点存在、只是没鉴权，别当成接口不存在。

**生图模型**：`qwen-image-2.0` / `wan2.7-image`，计费 `per_image`，**¥0.20/张**。
**尺寸**：`1024x1024` / `1280x720` / `720x1280` / `1280x1280` / `1024x1792` / `1792x1024`。
返回 `data[0].url` 为 OSS 签名地址，**24 小时过期**，必须立即下载转存。

### `/v1/images/edits` 实测要点

- 大图（约 1.4MB PNG）曾触发上游拉取失败：`400 BAILIAN_IMAGE_ERROR Failed to download image ... HTTP 403`；
  换成 1.7KB 的小图后同参数一次通过。**垫图建议先压缩，失败时优先怀疑图体积，而非参数。**
- 成功返回与文生图同构：`{created, data:[{image_id, url}], cost_cny, billing_pending, trace_id}`。
- `prompt` 传中文时用 `curl -F "prompt=<提示词文件"` 从 UTF-8 文件读取，比命令行内联更稳。

---

## 页面 → 接口速查（前端路由对照）

控制台是 React SPA（`apiBaseUrl=/api`），下表把前端路由与它真正发出的请求对上，
方便「页面上看到的数据」直接定位到查询接口：

| 页面路由 | 主要接口 |
|---|---|
| `/`（落地页） | `GET /api/site-content`、`GET /api/models` |
| `/login`、`/register`、`/reset-password`、`/login/result` | `POST /api/auth/login`、`/auth/login-or-register`、`/auth/sms/send`、`/auth/sms/verify`、`/auth/password/reset` |
| `/models`、`/models/:id` | `GET /api/models`、`GET /api/models/{id}` |
| `/docs`、`/docs/:slug` | `GET /api/docs`、`GET /api/docs/{slug}`、`GET /api/legal-documents/current` |
| `/playground` | `POST /api/playground/send`、`GET /api/playground/messages` |
| `/account` | `GET /api/me`、`/api/wallet/summary`、`/api/usage-summary`、`/api/api-keys`、`/api/call-logs/page` |
| `/account/keys` | `GET/POST /api/api-keys`、`/api-keys/{id}/disable`、`/api-keys/{id}/delete`、`/api-keys/usage`、`/api-keys/{id}/copy-log` |
| `/account/usage` | `GET /api/usage/panel`、`/api/api-keys/usage`、`/api-keys/{id}/usage-panel` |
| `/account/logs` | `GET /api/call-logs/page`、`/api/call-logs/{traceId}/reasoning`、`POST /api/call-logs/export`、`/api/export-tasks/page` |
| `/account/conversations`、`/account/conversations/:id` | `GET /api/conversations/page`、`GET /api/conversations/{id}/messages` |
| `/account/audit-logs` | `GET /api/audit-logs` |
| `/account/profile`、`/account/profile/deletion` | `GET /api/me`、`PUT /api/me/name`、`PUT /api/me/password`、`/api/me/deletion/*` |
| `/account/restriction` | `GET /api/me/restriction` |
| `/account/wallet/transactions` | `GET /api/wallet/transactions`、`/api/wallet/expiring-credits` |
| `/account/referrals` | `GET /api/referrals/me`、`/api/referrals/records` |

> 本清单来源：抓取控制台主 bundle（`__vite__mapDeps` 列出全部 lazy chunk）→ 逐 chunk 正则提取 `/api`
> 路径 → 登录后逐条实测。前端改版后如需增量比对，重新抓 `runtime-config.js` 与主 bundle 即可。

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

平台通过运行时配置控制模块开关，决定哪些功能真的可用。最新实测（2026-09-13）：
页面 HTML 里的 `<script src="/_releases/base/<build-hash>/runtime-config.js">` 就是这个配置源，
直接 GET 即可读到全部开关。

```
apiBaseUrl=/api             formalBackendMaintenance=false  useMock=false
passwordAuthEnabled=true    referralsEnabled=true          referralMockEnabled=false
webToolsEnabled=true        idleTimeoutMinutes=1440        noindex=false
redirectOnAuthExpired=true  showRuntimeBadge=false
landingAssetBaseUrl=https://static.tokenrhythm.studio
```

影响：

- `useMock=false` ⇒ 上面所有接口返回的都是真实后端数据，不是 mock
- `webToolsEnabled=true` ⇒ 联网工具类功能开启
- `referralsEnabled=true` ⇒ 邀请 / 奖励模块在用（见「邀请与奖励」一节）
