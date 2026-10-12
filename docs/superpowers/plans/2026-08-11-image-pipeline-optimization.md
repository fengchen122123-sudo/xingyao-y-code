# 图片发送链路优化实施计划

> 设计依据：`docs/superpowers/specs/2026-08-11-image-pipeline-optimization-design.md`

## 目标

在不修改主模型、副模型及视觉路由的前提下，提高图片预处理质量，保证编码内容与 MIME 一致，并把 GUI 的图片压缩提示从临时弹窗改成当前用户消息下方的持久提示。

## 约束

- 保留 Base64 Data URL 传输，不引入文件上传 API。
- 保留 `parse_user_input()` 的现有返回契约，避免影响 TUI。
- 不覆盖当前工作区中与本任务无关的未提交改动。
- 图片处理依赖缺失或像素超限时明确报错，不静默降级。

## 任务 1：以测试定义图片处理契约

涉及文件：

- `tests/test_input_parser.py`
- `core/input_parser.py`

步骤：

1. 新增测试，覆盖 PNG 保持 PNG、JPEG 渐进压缩、透明 PNG、EXIF 方向修正、超过旧 5 MiB 限制但未超过新限制的图片、像素上限和 Data URL MIME 与实际编码一致。
2. 运行 `tests/test_input_parser.py`，确认新增测试先失败。
3. 引入包含 `data`、`mime_type`、`extension` 的压缩结果类型。
4. 将输入上限调整为 20 MiB、像素上限设为 40,000,000；默认长边 1536、质量 90、目标 1 MiB。
5. 使用 `ImageOps.exif_transpose()` 修正方向；PNG 使用无损优化并保留透明通道；JPEG/WebP 质量从配置值每次递减 5，最低 50，选择首个达标结果，否则选择最小候选。
6. 在不需要变换且压缩结果更大时保留原图；Pillow 不可用时抛出明确错误。
7. `parse_user_input()` 使用处理结果的真实 MIME 组装 Data URL。
8. 再次运行测试，确认通过。

## 任务 2：同步配置示例和说明

涉及文件：

- `config.example.json`
- `README.md`

步骤：

1. 将图片压缩默认示例更新为长边 1536、质量 90、目标 1 MiB。
2. 补充输入大小、像素限制、格式保持和 Base64 Data URL 行为说明。
3. 检查文档中的字段名与代码读取字段完全一致。

## 任务 3：把 GUI 压缩提示写入对话消息

涉及文件：

- `tests/test_gui_tool_events.py`
- `gui_bridge.py`
- `frontend/src/stores/appStore.toolEvents.test.ts`
- `frontend/src/stores/appStore.ts`
- `frontend/src/components/MessageBubble.test.ts`
- `frontend/src/components/MessageBubble.vue`
- `frontend/src/types/pywebview.d.ts`

步骤：

1. 新增后端测试，要求附件说明通过 `user_attachment_notes` 事件发送，而不是 `notice` 弹窗。
2. 新增前端 store 测试，要求事件只更新同会话最近一条用户消息；不存在用户消息时抛出桥接契约错误。
3. 新增组件测试，要求用户消息图片或正文下方显示附件说明。
4. 分别运行后端和前端聚焦测试，确认先失败。
5. 在 `gui_bridge.py` 中发送包含 `session_name` 与 `notes` 的新事件。
6. 扩展 `ChatMessage`，增加 `attachment_notes?: string[]`，并在 store 中严格关联最近用户消息。
7. 在 `MessageBubble.vue` 中以内联弱提示样式渲染说明，不调用 toast。
8. 重新运行聚焦测试，确认通过。

## 任务 4：回归验证

步骤：

1. 运行全部 Python 测试或至少图片解析、GUI 事件相关测试。
2. 运行全部前端测试。
3. 运行前端生产构建。
4. 对修改过的 Python 文件执行编译检查。
5. 检查 `git diff --check` 和最终差异，确认无空白错误、无模型配置改动、无无关文件覆盖。

## 验收标准

- 普通截图不再被强制转成低质量 JPEG，透明 PNG 不丢透明度。
- Data URL 声明的 MIME 与实际图片编码一致。
- 大图受输入字节和解码像素双重限制，异常会明确暴露。
- GUI 不再弹出图片压缩 toast；说明固定显示在对应用户消息内。
- 主模型、副模型和视觉模型配置均未改变。
- 聚焦测试、前端全量测试、构建和 Python 编译检查通过。
