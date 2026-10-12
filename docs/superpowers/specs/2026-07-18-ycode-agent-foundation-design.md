# Y-code Agent Foundation Design

## Goal

将 Y-code 从“功能较多的终端聊天工具”升级为可靠的 Agent 编程助手：模型能够分析、规划、调用工具、处理权限、修改文件、验证结果并在中断或失败时保持状态一致；现有启动 Logo、欢迎卡片、主题、输入框和模型/会话选择界面保持不变。

## Non-goals

- 不复制 cc-mini 或其他项目的源码；只采用独立的架构设计。
- 不引入 Buddy 宠物、Linux Bubblewrap 沙箱或 advisor 双模型。
- 不替换现有 SQLite 会话存储、模型管理器和主题系统。
- 不把完整 Markdown 网页式界面引入终端；回答继续以紧凑纯文本和现有 ANSI/HTML 风格为主。
- 不在本阶段进行与 Agent 稳定性无关的大规模重构。

## Current Constraints

- `main.py` 同时承担 TUI、命令分发、后台工作线程和回答输出。
- `core/llm.py` 与 `core/tools.py` 存在直接终端输出路径，导致工具消息、权限提示和流式回答交错。
- 工具调用目前以字符串函数分派，工具能力、权限和结果展示元数据分散在多个位置。
- `system_prompt.md` 与 `main.py` 中存在重复的默认提示词。
- 当前测试收集存在依赖不一致和旧类名引用问题，不能作为可靠回归基线。

## Architecture

### TurnController

每次用户提交创建一个带 `turn_id` 的执行上下文，状态严格经过：

`idle -> preparing -> streaming -> waiting_permission -> executing_tools -> resuming_model -> completed|cancelled|failed`

Controller 负责 turn 生命周期、取消、回滚、工具循环和事件顺序，不负责终端格式化。

### TurnEvent

模型层、工具层和后台任务只能产生有序事件，不直接调用 `print_formatted_text`、Rich Console 或 `Application.invalidate`。事件包含 `turn_id`、单调递增的 `sequence`、`kind` 和结构化 payload。

第一阶段事件类型：`assistant_delta`、`thinking_delta`、`tool_requested`、`tool_started`、`tool_finished`、`permission_requested`、`permission_resolved`、`diff_ready`、`usage_updated`、`error`、`turn_finished`、`turn_cancelled`。

### TUI Renderer

主 TUI 线程消费事件并负责唯一输出入口。Renderer 维护当前回答块、思考状态、工具状态和错误状态，保证：

- 工具状态不会插入回答文本中间。
- 连续只读工具可以合并成一条摘要，但完成结果按原始调用顺序输出。
- 写文件、编辑文件、命令和权限结果各自占用稳定的状态行。
- Diff 在对应工具完成事件之后显示。
- `<think>` 或 `<thinking>` 标签跨流式 chunk 时不会泄漏到终端。
- 不输出模型私有思维链，只显示可选的简短决策摘要。

现有 Logo、状态栏和输入框不迁移到新 Renderer；只替换回答区域和工具反馈的内部输出路径。

### ToolSpec and ToolResult

每个工具都通过统一协议注册：`name`、`description`、`input_schema`、`read_only`、`mutates_files`、`requires_permission`、`cancellable`、`activity_description` 和 `execute`。

工具统一返回 `ToolResult`：`content`、`is_error`、`changed_files`、`diff` 和 `metadata`。模型循环不再通过大段 `if/elif` 同时决定执行、权限和展示。

权限仍保留 `confirm`、`plan`、`full` 三种用户模式，但判断基于 ToolSpec 能力；只读工具自动执行，副作用工具按策略确认。`always` 只在当前会话范围生效。

### Structured Plan

计划使用结构化状态，不依赖 Markdown 复选框解析。每项包含 `id`、`subject`、`status` 和 `result`，状态为 `pending`、`in_progress`、`completed`、`failed` 或 `cancelled`。计划更新通过事件渲染，不改变启动界面。

### ContextBuilder

每轮请求的上下文分层组装：基础人设、终端协议、Agent 工作流、工具摘要、当前运行上下文、项目记忆、当前计划、近期对话和必要工具结果。时间、工作目录、权限模式、模型和活动技能由程序动态注入。

压缩只裁剪本轮请求，不删除数据库原始消息；保留 tool call/tool result 对、当前计划和最近对话。压缩失败时明确报告，不进行无限重试或静默丢弃历史。

## System Prompt Design

`system_prompt.md` 只保存稳定规则：身份、表达风格、终端协议、编码工作流、工具使用原则、验证原则和安全边界。删除强制输出思维链、无限修复直到成功、无条件调用命令查询和强制 Markdown 计划等不可靠约束。

动态内容由 `ContextBuilder` 注入，技能和 MCP 描述通过注册表生成。`main.py` 的默认提示词只保留为同一来源的加载兜底，不能再维护第二份独立规则。

## Delivery Phases

1. 基线与契约：修复测试收集阻塞，新增事件、工具结果和 Renderer 的行为测试。
2. 事件流与输出：移除 LLM/工具层直接打印，接入有序事件队列和稳定流式回答渲染。
3. 工具与权限：建立 ToolSpec/ToolResult，统一权限、Diff、错误和只读并发结果。
4. 提示词与上下文：分层系统提示词，增加 ContextBuilder，修正压缩和工具链边界。
5. 计划与交互：结构化计划状态、计划事件显示和 AskUser 工具。
6. Agent 生命周期：可靠中断、回滚、子 Agent 隔离、后台任务通知和回归测试。

## Acceptance Criteria

- 启动 Logo、欢迎卡片、主题、输入框和模型/会话选择界面与改造前保持一致。
- 一轮对话的所有终端输出都能由事件序列重放，后台线程不直接写终端。
- 多个只读工具并发执行时，显示顺序稳定且不会覆盖或穿插回答。
- 流式思考标签被拆分到不同 chunk 时，终端不出现残留标签或乱码。
- 权限等待、拒绝、取消和工具失败都能结束当前 turn，不死锁、不伪造成功。
- Ctrl+C 后本轮临时消息、工具链和计划状态回滚到一致状态。
- 系统提示词与动态上下文职责清晰，运行时状态不会重复或过期。
- 每个新增核心行为都有先失败后通过的自动化测试；语法检查和测试结果在交付时明确报告。

