# 审查问题修复设计

## 目标

修复 2026-08-20 遗留项实现审查中确认的问题，使 Y-code 数据目录权限、会话模型隔离、GUI 模型绑定和 diff/CLI 回归测试与用户要求一致。

## 权限语义

- 唯一数据根目录来自 `config.YCODE_DIR`：默认 `~/.ycode`，设置 `YCODE_DATA_DIR` 后使用自定义目录。
- 数据根目录内所有文件读取不受沙盒控制。
- 数据库、普通运行数据和普通文件写入不受沙盒控制。
- 仅数据根目录顶层的 `config.json`、`config.json.bak`、`mcp.json`、`mcp.json.bak` 写入或编辑需要逐次询问。
- 凭据路径分类发生异常时失败关闭，不得因 `allow_outside_workspace=True` 自动放行。
- 插件子目录中的同名 `config.json` 仍属于普通插件文件，不触发凭据询问。

## 模型隔离

- `_active_model_tuple_or_none(session_name)` 收到显式会话名后，只允许使用该会话解析出的模型；解析为空或异常时返回 `None`，不得读取进程级 tuple。
- 无会话的 CLI/headless 调用继续兼容进程级 tuple 与全局配置。
- GUI worker 仅初始化空的会话模型绑定；若绑定已存在且与本回合快照不同，视为用户并发切换，不得写回旧模型。

## 测试与兼容性

- CLI 自定义回答测试必须执行 Enter 分支可测试的纯控制函数，验证失败时不清空输入且不回答选项。
- 大 diff 测试必须超过 25 行，同时验证展示文本截断和完整 `added/removed` 统计，并验证 GUI 桥接传递。
- 旧 ANSI/Rich diff 在缺少后端统计字段时继续正确估算。
- 子代理测试覆盖 `SubagentManager.start` 和自定义 `model=inherit`，不只测试 helper。
- ChangedFilesPanel 保留 computed 缓存与主题颜色，并补组件行为测试（若现有测试基础允许）。

## 文档

- 恢复原审查背景和“当天已修复项”，在原文上标记完成状态。
- 实施计划 checkbox 与实际完成状态一致；未验证的项目不得写为完成。

## 约束

- 不改 Provider 请求字节、稳定 system prompt、tools schema 或 Prompt Ledger。
- 不做无关重构。
- 当前分支不是 `dev`，不创建 commit。
