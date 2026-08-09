# CLI / GUI 统一模型配置与 Responses 协议 Implementation Plan

> 状态：已完成。后端全量测试、前端测试、生产构建、语法检查和本地 GUI 视觉验收均已通过。

> **执行要求：** 每个功能先写失败测试，再写最小实现；协议不允许静默降级。

**目标：** 让 CLI 与 GUI 共用预置厂商/模型目录和配置存储，新增完整 GUI 模型设置页、两级模型选择器，并真实接入 Responses API。

**架构：** `core/model_catalog.py` 是协议与预置模型的唯一来源；`core/model_manager.py` 管理共享配置；`core/llm.py` 通过协议适配入口选择 Completion 或 Responses 并统一流事件；GUI bridge 返回完整设置快照，Pinia 以快照覆盖本地状态。

**技术栈：** Python 3.12、LiteLLM、pytest、Vue 3、Pinia、TypeScript、Vitest、pywebview。

## 全局约束

- 只在 `dev` 分支实施。
- 不推测旧配置的预置来源。
- API Key 不在初始化快照中返回明文。
- Responses 错误不降级到 Chat Completions。
- 任何配置写入必须完成 SQLite/JSON 所需提交或原子保存。
- 保持稳定 system prompt 与 tools schema 的字节契约。

### Task 1：共享协议与预置模型目录

- [ ] 在 `tests/test_model_catalog.py` 写失败测试：三种协议存在、DeepSeek 与其他预置模型元数据完整、查询结果不可修改全局目录。
- [ ] 新建 `core/model_catalog.py`，从 `core/model_selector.py` 移入协议与预置厂商常量，并为厂商增加稳定 ID。
- [ ] 修改 `core/model_selector.py` 只导入共享目录，保留现有 CLI 行为。
- [ ] 运行目录与 CLI 选择器测试。

### Task 2：共享配置 CRUD

- [ ] 在 `tests/test_config.py` / `tests/test_model_selector.py` 写失败测试：预置厂商记录 `preset_id`、厂商更新/删除、模型更新、多模态字段、删除最后模型不隐式删除厂商。
- [ ] 扩展 `core/model_manager.py`：添加预置厂商、更新厂商、删除厂商、更新模型、严格校验协议。
- [ ] 保持旧配置可读；缺少 `preset_id` 的条目按已配置厂商原样返回。
- [ ] 运行配置与 CLI 回归测试。

### Task 3：Responses 协议适配

- [ ] 在 `tests/test_llm_responses.py` 写失败测试：Responses 调用参数、文本/推理/工具调用/usage 流事件转换、未知残缺事件立即失败、模型测试复用相同协议入口。
- [ ] 在 `core/llm.py` 增加 LiteLLM Responses 懒加载包装和协议分流。
- [ ] 实现 Responses 输入转换及流事件规范化，输出当前 Agent 主循环可消费的统一 chunk。
- [ ] 将 provider protocol 从模型配置传入主请求与测试请求。
- [ ] 运行 LLM、工具调用、缓存前缀稳定性回归测试。

### Task 4：GUI bridge 模型设置契约

- [ ] 写 bridge 失败测试：设置快照包含协议/预置/已配置模型元数据且不包含 API Key 明文；所有 CRUD 返回刷新后快照。
- [ ] 扩展 `gui_bridge.py`：获取设置快照、添加预置厂商、添加/更新/删除厂商、添加/更新/删除/测试模型。
- [ ] 扩展 `frontend/src/types/pywebview.d.ts`，让 TypeScript 契约与 bridge 一致。
- [ ] 运行 GUI bridge pytest。

### Task 5：Pinia 模型设置状态

- [ ] 在 `frontend/src/stores/appStore.modelSettings.test.ts` 写失败测试：加载快照、预置保存、厂商/模型 CRUD 后用返回快照覆盖、打开/关闭设置页。
- [ ] 扩展 `frontend/src/stores/appStore.ts` 的模型设置类型、状态、getter 和 actions。
- [ ] 保证失败时保留原状态并显示明确错误，不前端伪造成功。
- [ ] 运行 store Vitest。

### Task 6：设置入口与模型配置页

- [ ] 为 Sidebar 设置入口和 SettingsPage 关键交互写组件失败测试。
- [ ] 修改 `frontend/src/components/Sidebar.vue` 与 `frontend/src/App.vue`，增加设置模式和返回工作区。
- [ ] 新建 `frontend/src/components/SettingsPage.vue`，实现设置导航、预置厂商、已配置厂商和详情面板。
- [ ] 新建模型编辑弹窗，实现模型 ID、上下文窗口、多模态选项以及测试/编辑/删除操作。
- [ ] 自定义厂商表单提供三种 API 格式。
- [ ] 运行组件测试与 TypeScript 检查。

### Task 7：两级模型选择器

- [ ] 扩展 `ModelCascader` 测试：悬浮厂商显示模型、选择模型、键盘焦点、管理模型入口。
- [ ] 调整 `frontend/src/components/ModelCascader.vue` 的结构与样式，匹配参考图的两级悬浮菜单。
- [ ] 确认只展示共享配置中实际存在的模型。
- [ ] 运行相关 Vitest。

### Task 8：完整验证与视觉回归

- [ ] 运行相关 Python pytest、全量 pytest 和项目规定的 `py_compile`。
- [ ] 运行前端全量 Vitest 与 `npm run build`。
- [ ] 启动本地 GUI，检查设置入口、模型设置布局、预置厂商密钥流程、模型弹窗和级联选择器。
- [ ] 执行 `git diff --check`，审查敏感信息、旧配置兼容和错误处理。
- [ ] 更新计划勾选状态并提交最终实现。
