# Turn Output Feedback Design

## Goal

让终端 Agent 在工具调用后始终给用户可读结果，并让运行状态准确区分思考、干活和等待授权。

## Behavior

1. 工具完成事件携带安全截断的结果摘要。渲染器在成功工具行下方显示该摘要，错误仍通过错误事件显示。
2. 如果一个回合没有任何有意义的 assistant 正文，渲染器在结束时补一行本地说明，列出已完成工具数量，避免只看到 Markdown 标记或空白。
3. UI 顶部动画和底部状态栏使用同一 `task_status`：`thinking` 显示“星瑶正在思考”，`executing` 显示“星瑶正在干活”，`waiting` 显示“星瑶等待你的确认”。
4. 稳定提示词要求工具调用后的最终中文结论，且禁止只有 Markdown 标记或工具调用记录。

## Boundaries

- 不调整 Logo、欢迎卡、主题、选择器或输入框布局。
- 结果摘要上限为 8 行、800 字符，避免工具输出冲掉对话记录。
- 正文是否有意义由去除空白和常见 Markdown 标记后的字符数判断；本地说明只在空正文时出现。

## Verification

- 渲染器测试覆盖工具结果摘要和空结论兜底。
- UI 测试覆盖三个状态文案。
- 全量 pytest 与 py_compile 通过。

## Follow-up: Activity State And Color Semantics

- 只有模型返回 `reasoning_content` 时进入 `thinking`；请求等待、工具执行和等待最终文本都处于 `executing`。
- 工具开始、成功、失败和输出摘要分别使用青色、绿色、红色和灰色 ANSI 文本。
- 发布版本从 `1.0.3` 升级为 `1.0.4`，同步更新启动卡片和包元数据。
