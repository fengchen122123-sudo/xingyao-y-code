# CLI / GUI 统一模型配置与 Responses 协议设计

## 目标

为 GUI 增加完整的模型设置入口和两级模型选择器，同时消除 CLI、GUI 各自维护厂商与模型元数据造成的漂移。三种 API 格式必须都能真实请求：

- Anthropic Messages (`/v1/messages`)
- Chat Completions (`/chat/completions`)
- Responses (`/responses`)

## 设计原则

- 预置厂商、预置模型、上下文窗口和多模态能力只有一个代码来源。
- CLI 与 GUI 继续读写同一个 `~/.ycode/config.json`。
- 协议不是 UI 标签，而是请求链路的显式配置；不支持的事件立即报错，不静默降级。
- API Key 不通过 GUI 初始化状态回传明文。空白编辑值代表保留原值。
- 旧配置不通过厂商名称或 URL 猜测来源；缺少预置标识的条目继续作为已配置厂商使用。

## 核心目录

新增 `core/model_catalog.py`，包含：

- `API_PROTOCOLS`：协议 ID、显示名称与路径。
- `PRESET_PROVIDERS`：当前 CLI 的全部预置厂商。
- 每个预置模型的模型 ID、上下文窗口和 `supports_vision`。
- 只读查询函数，向 CLI 和 GUI 返回副本，禁止调用方修改全局目录。

`core/model_selector.py` 删除本地重复常量，直接消费核心目录。GUI bridge 同样从核心目录序列化设置页初始数据。

## 配置模型

用户配置仍以 provider 为单位持久化：

```json
{
  "id": "provider-id",
  "name": "深度求索",
  "base_url": "https://api.deepseek.com",
  "api_key": "...",
  "protocol": "responses",
  "preset_id": "deepseek",
  "models": [
    {
      "id": "model-id",
      "name": "deepseek-v4-pro",
      "context_window": 1000000,
      "supports_vision": false
    }
  ]
}
```

- 新增预置厂商时写入稳定 `preset_id`，名称、URL、协议和模型元数据来自目录，只接收 API Key。
- 已经存在但没有 `preset_id` 的旧条目不自动归类，避免错误迁移。
- 自定义厂商允许编辑名称、Base URL、API Key 和协议。
- 模型允许新增、编辑、测试、删除，模型字段包含模型 ID、上下文窗口和多模态能力。
- 删除最后一个模型不隐式删除厂商；厂商删除必须走独立、明确的操作。

## Responses 请求适配

新增协议适配入口，由 provider 的 `protocol` 决定调用方式：

- `openai` 与 `anthropic` 继续使用现有 LiteLLM completion 路径。
- `responses` 使用 LiteLLM Responses API，并传递模型、Base URL、API Key、输入、工具、流式参数与超时。

Responses 流事件转换为 Y-Code 内部统一事件：

- 输出文本增量
- 推理文本增量
- 函数调用名称与 JSON 参数增量
- 完成原因
- 输入、输出、缓存 token 用量

转换层只做确定性的字段映射。遇到未知的必需事件、残缺工具调用或无法解析的响应结构时抛出明确异常，不改走 Chat Completions。

模型测试也通过同一个协议适配入口，保证“测试成功”代表实际聊天请求格式可用。

## GUI 信息架构

### 设置入口

Sidebar 左下角增加设置按钮。点击后在主内容区打开设置页；首版设置导航只有“模型配置”，并保留返回工作区入口。聊天模型菜单中的“管理模型”直接打开同一页面。

### 模型配置页

页面分为设置导航、厂商列表和厂商详情三部分：

- 预置厂商：始终来自核心目录，选择后只填写 API Key 并保存。
- 已配置厂商：显示实际 config 中的 provider，支持启用、编辑和删除。
- 详情区：展示协议、Base URL、密钥状态和模型列表。
- 模型列表：显示模型 ID、上下文窗口、多模态标记，以及测试、编辑、删除操作。
- 添加/编辑模型弹窗：模型 ID、上下文窗口、是否支持多模态。
- 自定义厂商表单：名称、Base URL、API Key、API 格式和初始模型。

API Key 已配置时只展示遮罩状态。用户输入新值后可以临时切换可见性；保存后前端立即清空明文。

### 模型选择器

聊天输入区的选择器使用两级悬浮菜单：

- 一级列出已配置且含模型的厂商。
- 鼠标进入厂商后，二级展示该厂商模型。
- 点击模型立即切换当前模型并关闭菜单。
- 底部提供“管理模型”，进入统一设置页。

键盘焦点和鼠标悬浮共同驱动活动厂商，避免只能使用鼠标。

## Bridge 与状态同步

GUI bridge 增加完整的模型设置快照和显式命令：

- 获取协议、预置目录和已配置厂商。
- 添加预置厂商。
- 添加、更新、删除自定义厂商。
- 添加、更新、删除和测试模型。

每次成功变更后重新读取磁盘配置并返回新快照。Pinia store 不在前端推测最终状态，从返回快照覆盖本地状态，确保 CLI/GUI 共用配置时保持一致。

## 验证

- Python 单元测试：目录唯一来源、配置持久化、旧配置边界、Responses 参数和事件转换、测试请求走真实协议。
- GUI bridge 测试：密钥不泄漏、CRUD 返回最新快照。
- Vue/Vitest：设置入口、预置厂商表单、模型弹窗、多模态字段、两级模型选择器。
- `py_compile`、相关 pytest、前端 Vitest、TypeScript 检查和生产构建。
- 启动本地 GUI，验证设置页布局、悬浮菜单和主要 CRUD 流程。

## 非目标

- 不在线同步厂商模型列表。
- 不自动迁移或重命名用户已有厂商。
- 不为协议错误增加重试剥参、降级或兜底请求。
- 不在本轮增加常规、外观等其他设置页面。
