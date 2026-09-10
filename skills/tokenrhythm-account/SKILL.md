---
name: tokenrhythm-account
description: Use when 用户要求查询或管理基元律动（TokenRhythm / tokenrhythm.studio）账号数据——余额、Token 用量、调用日志、API Key、个人信息、模型价格——或要求登录该站点执行操作时
---

# 基元律动（TokenRhythm）账号操作

## 适用范围与红线

**只做站内既有功能**：查余额 / 用量 / 调用日志 / 模型价格、管理自己的 API Key、查看个人信息、调用站内开放 API。

**禁止**（发现即明确拒绝，不做任何变通）：
- 攻击、扫描、压测、爆破、绕过限流或鉴权
- 操作非本人账号；把拿到的凭据用于其他系统
- 越权读写他人数据、批量注册、抓取全站数据
- 把账号密码或 API Key 写入任何文件、日志或长期记忆

**写操作必须先用 `ask_user` 确认**：创建 / 停用 / 删除 Key、改昵称、改密码。
只读查询可直接执行。

**账号级写接口（`PUT /api/me/name`、`PUT /api/me/password`）只在用户明确指示时调用**：
它们会真实改动账号本身，**不做任何自动化试探** —— 改昵称会留下操作日志，
改密码一旦上游不校验旧密码就等于把用户锁在门外。用户没提就别碰，
查询类需求永远用不到它们。

## 前置：向用户索要凭据

开始前**必须**问用户要账号和密码（每次会话都问，不复用上一次的，不从记忆或配置文件里翻）。
密码只用于本次登录，cookie 只落系统临时目录，结束时用 `Remove-TrSession` 清理。

## 登录与调用

```powershell
. <技能目录>\scripts\tr-session.ps1
$s = New-TrSession -Account <账号> -Password <密码>
(Invoke-TrApi -Session $s -Path '/api/wallet/summary').data.availableBalanceCny
Invoke-TrApi -Session $s -Path '/api/api-keys' -Method POST -Body @{ name = 'xxx' }
Remove-TrSession -Session $s
```

`Invoke-TrApi` 已封装 cookie、CSRF 头与 UTF-8 解码，返回解析后的对象。

### CSRF 陷阱（务必先知道）

所有非 GET 请求必须带 **`Sec-Fetch-Site`** 请求头（值任意，如 `same-origin`），否则服务端一律返回
**403 CSRF_INVALID**。

这是浏览器专有头，纯脚本请求天然缺失 —— **不是 cookie 不对，也不是 CSRF token 值算错了，
不要在这两点上反复试错**。`Invoke-TrApi` 已内置，只有手写 curl 时才会踩。

## 模型连通性测试（标准动作）

用户说「测一下 XX 模型的连通性 / 能不能用」时走这套固定流程。**不需要建 Key** ——
站内 Playground 用会话就能试模型。

```powershell
. <技能目录>\scripts\tr-session.ps1
$s = New-TrSession -Account <账号> -Password <密码>
Test-TrModel -Session $s -Model 'deepseek-v4.1-flash' -Stream
Remove-TrSession -Session $s
```

返回：`Ok / LatencyMs / Content / InputTokens / OutputTokens / CostCny / TraceId / StreamOk / StreamChunks / Error`。

**四步流程：**

1. **对齐模型 ID** —— 先 `GET /api/models`。用户给的多半是简称（"deepseek-v4.1"），必须映射到准确 ID
   （`deepseek-v4.1-flash`），并记下 `status`（online / testing）。
2. **跑探针** —— `Test-TrModel`；模型支持流式时加 `-Stream` 一并验证。
3. **汇报** —— 模型 ID、状态、耗时、返回内容、Token、单次花费、traceId，缺一不可。
4. **可选核对** —— 想证明真的走了计费链路，再拉 `/api/call-logs/page?page=1&pageSize=5`
   确认该次调用已落日志（`endpoint` 显示 `/playground/send`）。

**边界：**

- 该端点**纯文本**：`content` 必须是字符串。传 `[{type:"image_url",...}]` 数组会 400，
  **视觉能力在这里测不了** —— 要测得走 `/v1/chat/completions` + API Key，而建 Key 是写操作，必须先确认。
- 模型 ID 带 `expires on MMDD` 的是限时模型，顺手看一眼到期日并提醒用户切换。
- **只发单次探针**，禁止循环或并发批量调用（红线：不许压测）。
- 想确认模型「有哪些能力」（视觉 / 工具 / 推理 / 缓存），查 `GET /api/models` 的
  `capabilities` 与 `modalities` 字段，比发试探请求更准也更快。

## 常见任务 → 接口

| 用户说 | 调用 |
|---|---|
| 我余额多少 | `GET /api/wallet/summary` |
| 今日 / 近 7 天用量 | `GET /api/usage/panel?range=today`（range：1h / 3h / today / 3d / 7d / 14d / 30d / all / custom） |
| 累计消耗 | `GET /api/usage-summary` |
| 调用日志 | `GET /api/call-logs/page?page=1&pageSize=20`（加 `status=error` 筛失败） |
| 我有哪些 Key | `GET /api/api-keys` |
| 建一个 Key | `POST /api/api-keys`，body `{name}` —— **先确认** |
| 删 / 停用 Key | `POST /api/api-keys/{id}/delete` 或 `/disable` —— **先确认** |
| 我的个人信息 | `GET /api/me` |
| 有哪些模型 | `GET /api/models` |
| **测某个模型能不能用** | `POST /api/playground/send`，body `{model, messages:[{role,content}], stream}` —— 会话鉴权，**不用建 Key** |
| 生成一张图 | `POST /v1/images/generations`（用 API Key 鉴权，不是会话） |
| 垫图改图 | `POST /v1/images/edits`，multipart：`model` + `prompt` + `image`（用 API Key） |
| 导出调用日志 | `POST /api/call-logs/export` → 轮询 `/api/export-tasks/page` 到 `done` → `GET /api/export-tasks/{id}/download-ticket` 取下载地址 |
| 我的登录 / 操作记录 | `GET /api/audit-logs`（**返回数组，不分页**） |

完整接口清单见 [reference.md](reference.md)。

## 单位与坑

- 接口时间戳是 **UTC**，汇报给用户时 +8 转北京时间。
- 生图调用**不进调用日志**，只计在 `usage-summary.imageRequestCount` 里。
- 完整 Key 只在创建响应里返回一次，之后仅剩掩码；Key 一旦出现在对话里即视为泄露，建议用完即删。
- 中文内容务必按 UTF-8 读文件，别直接吃 curl 的 stdout（控制台会用 GBK 解码成乱码）。
- **导出和筛选的时间参数只认 `YYYY-MM-DD` 或 ISO 8601 UTC**，传本地时间（`2026-09-10T09:50`）会被拒。
- **导出是异步的**：先 `queued`，轮询任务列表变 `done` 才有 `downloadReady: true`（实测约 3~5 秒）。
- **垫图改图报 `BAILIAN_IMAGE_ERROR ... Failed to download image` 时，先怀疑图片体积** ——
  实测 1.4MB 大图失败、1.7KB 小图同样参数一次通过。压缩后重试，别反复调参数。
- 模型 ID 里带 `expires on MMDD` 的是限时模型，查模型相关任务时顺手看一眼到期日。

## 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 403 CSRF_INVALID | 缺 `Sec-Fetch-Site` 头 | 改用 `Invoke-TrApi`，别手写 curl |
| 401 UNAUTHORIZED | 会话过期，或该 Key 已删除 | 重新 `New-TrSession` |
| 登录成功但写操作全失败 | 未先打一次 `/auth/me` | `New-TrSession` 已内置该步骤 |
| 输出中文乱码 | 控制台编码 | 写入文件后按 UTF-8 读取 |
| 400 `messages.0.content 类型错误` | 给 Playground 传了图片数组 | 该端点只吃字符串；视觉测试必须走 `/v1/chat/completions` |
| 400 `MODEL_NOT_AVAILABLE` | 模型 ID 写错或未上线 | 用 `GET /api/models` 核对准确 ID |
