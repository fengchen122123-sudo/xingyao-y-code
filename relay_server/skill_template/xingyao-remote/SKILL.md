---
name: xingyao-remote
description: 通过公网中转服务器远程控制电脑上的星瑶（Y-code）智能体：下发任务、查看进度/回复、列出或切换会话、审批权限以及停止任务。当用户说“让电脑上的星瑶做某事”“查看星瑶回复/状态”“停止星瑶任务”时使用此技能。
---

# 星瑶远程控制技能（Xingyao Remote）

通过你配置的星瑶公网中转服务器，与用户电脑上运行的星瑶（Y-code）交互。

## 配置变量

使用前确认以下两个参数（必须通过环境变量 `XINGYAO_RELAY_URL`、`XINGYAO_MCP_TOKEN` 或用户配置提供，**不得硬编码明文地址或令牌**）：
- `BASE_URL`: 必须为 HTTPS 中转服务器根地址，例如 `https://your-relay.example.com:13131`（无末尾斜杠，严禁公网明文 `http://`）
- `TOKEN`: 星瑶 MCP/Skill 访问令牌（`xym_...`）

所有请求均需携带鉴权头：
`Authorization: Bearer $TOKEN`

## 核心操作

### 1. 下发任务并等待结果（最常用）

向电脑上的星瑶发送自然语言任务。`wait_seconds` 默认 20 秒（范围 0~60）；若任务较快完成会直接返回 `reply`。

```bash
curl -sS -X POST "$BASE_URL/api/skill/send" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"text": "帮我看看当前目录有哪些未提交改动", "wait_seconds": 20}'
```

可选字段：
- `session_name`: 指定会话名（留空用桌面当前会话）
- `workdir`: 指定电脑上的工作目录
- `wait_seconds`: 最多等待几秒（传 `0` 仅投递不等待）

返回 `status` 含义：
- `"finished"`: 任务完成，`reply` / `text` 即星瑶回复
- `"running"`: 仍在执行，调用「查询回复」继续等待
- `"permission_required"`: 星瑶等待权限审批（返回 `permission.request_id`、操作说明与选项），先向用户确认，再调用「审批权限」
- `"error"` / `"cancelled"` / `"offline"`: 执行失败或电脑离线

### 2. 查询最新回复 / 继续等待

```bash
curl -sS "$BASE_URL/api/skill/reply?wait_seconds=20" \
  -H "Authorization: Bearer $TOKEN"
```

可附加 `&session_name=<会话名>`。

### 3. 查看星瑶在线状态与运行快照

```bash
curl -sS "$BASE_URL/api/skill/status" \
  -H "Authorization: Bearer $TOKEN"
```

### 4. 查看与切换模型（按厂商与模型）

列出已配置的厂商及每个厂商下可用的模型（含 `model_id` 与当前激活模型）：

```bash
curl -sS "$BASE_URL/api/skill/models" \
  -H "Authorization: Bearer $TOKEN"
```

切换当前会话（或指定会话）使用的模型（支持传 `model_id` 或 `model_name` + 可选 `provider_name`）：

```bash
curl -sS -X POST "$BASE_URL/api/skill/model" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "DeepSeek Reasoner"}'
```

### 5. 查看工作目录、列出对话与切换/新建会话

列出电脑上星瑶拥有的所有工作目录：

```bash
curl -sS "$BASE_URL/api/skill/workspaces" \
  -H "Authorization: Bearer $TOKEN"
```

列出所有会话（可附加 `?workdir=<路径或目录名>` 只查看某个工作目录下的对话）：

```bash
curl -sS "$BASE_URL/api/skill/sessions" \
  -H "Authorization: Bearer $TOKEN"
```

切换到指定会话：

```bash
curl -sS -X POST "$BASE_URL/api/skill/switch-session" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"session_name": "<session_name>"}'
```

在指定工作目录下新建并切换会话：

```bash
curl -sS -X POST "$BASE_URL/api/skill/create-session" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"workdir": "E:/my-project", "title": "新任务"}'
```

### 6. 审批权限请求

当 `send` 或 `reply` 或 `status` 返回 `permission_required` 时，向用户确认是否允许，然后提交：

```bash
curl -sS -X POST "$BASE_URL/api/skill/permission" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"request_id": "<request_id>", "allow": true, "always": false}'
```

### 7. 停止当前任务

```bash
curl -sS -X POST "$BASE_URL/api/skill/stop" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

## 工作准则

1. 收到用户让电脑星瑶做事的请求时，优先调用 `POST /api/skill/send`（`wait_seconds=20`）。
2. 若返回 `status="running"`，告知用户正在执行并可继续轮询 `GET /api/skill/reply?wait_seconds=20`（最多连续轮询 2~3 次，避免长时间无反馈）。
3. 若返回 `status="permission_required"`，绝不要擅自同意高危操作，务必先把操作内容转述给用户，拿到明确同意后再调用 `/api/skill/permission`。
