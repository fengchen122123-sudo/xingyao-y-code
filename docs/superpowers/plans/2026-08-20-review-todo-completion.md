# Review Todo Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成 `docs/REVIEW_TODO_20260820.md` 中第 1–16 项遗留问题，并用自动化测试覆盖关键回归路径。

**Architecture:** 保留现有 Harness、会话运行态和插件边界，只在已有接口上补齐身份信息与严格状态。模型、token、diff 等跨线程数据在产生时绑定其会话/模型身份，消费端不再猜测全局状态。所有行为变更先添加失败测试，再做最小实现。

**Tech Stack:** Python 3、pytest、SQLite、Vue 3、Pinia、TypeScript、Vitest、prompt_toolkit。

## Global Constraints

- 不修改 Provider 请求构造、稳定 system prompt、tools schema、metadata、`extra_body` 或 DeepSeek Prompt Ledger 字节。
- 不增加降级、兜底或后处理补丁；非法状态继续显式失败。
- GUI 后台线程不得直接触碰界面；事件仍经既有桥接队列发送。
- SQLite 写函数继续显式 `commit()`；本批次消除无变化 UPDATE，不改变事务语义。
- 当前分支是 `dev-review-fixes`，与项目“只在 dev 提交”规则不符，因此执行期间不创建 Git commit。

---

### Task 1: 补齐 CLI ask_user、凭据写入与卸载器测试

**Files:**
- Modify: `tests/test_permission_broker.py`
- Modify: `tests/test_llm_events_fixes.py`
- Modify: `tests/test_tool_pipeline.py`
- Modify: `tests/test_uninstall_engine.py`

**Interfaces:**
- Consumes: `main._resolve_confirm_custom(text) -> bool`、`ToolAdapterSet.execute_write_file(...)`、`_sandbox_outside_guard(...)`。
- Produces: CLI 自由文本失败保留、凭据写入逐次询问和旧版 `tui/` 真实删除的回归守卫。

- [x] **Step 1: 为 CLI 自由文本真实 broker 链路写失败测试**

  在线程中调用 `ask_user(...)`，等待 `main.confirm_prompt` 激活；模拟 `permission_broker.resolve` 返回 `False`，断言 `_resolve_confirm_custom("补充说明") is False`、输入缓冲区内容未清空，并确认没有继续调用 `_resolve_confirm_prompt` 选择默认选项。

- [x] **Step 2: 运行 CLI 定向测试确认失败**

  Run: `venv\Scripts\python -m pytest tests/test_permission_broker.py -q`

- [x] **Step 3: 仅在测试暴露真实缺陷时修正 Enter 提交控制流**

  保持契约：ask_user 输入非空时只调用 `_resolve_confirm_custom`；发送失败立即返回事件处理器，既不清空 `input_area.text`，也不 fall-through 到 `_resolve_confirm_prompt(selected_option)`。

- [x] **Step 4: 为 ToolAdapterSet 完整写入链路写失败测试**

  通过真实 `execute_write_file` 注入 fake `permission_checker` 与临时 `~/.ycode`，覆盖：批准后写入 `config.json`；沙盒开关打开仍询问；`config.json.` 仍询问；`plugins/x/config.json` 不询问；Windows 上 `confi~1.json` 条件测试。

- [x] **Step 5: 运行凭据与 pipeline 定向测试**

  Run: `venv\Scripts\python -m pytest tests/test_llm_events_fixes.py tests/test_tool_pipeline.py -q`

- [x] **Step 6: 让卸载 fixture 真正创建旧版 tui 目录**

  在 `_fake_install_dir` 中执行 `(install_dir / "tui").mkdir()`，使现有删除断言具备有效性。

- [x] **Step 7: 运行卸载器测试**

  Run: `venv\Scripts\python -m pytest tests/test_uninstall_engine.py -q`

### Task 2: 会话模型恢复、竞态与“新会话继承当前模型”

**Files:**
- Modify: `frontend/src/stores/appStore.modelSession.test.ts`
- Modify: `frontend/src/stores/appStore.ts`

**Interfaces:**
- Consumes: `switchSession(sessionName, displayName?, targetWorkdir?)`、`createGlobalSession()`、`createSessionInGroup(workdir)`。
- Produces: pending 草稿的 `draftModelId` 与 runtime 模型快照，首条消息通过 `send_user_message(..., model_id)` 持久化。

- [x] **Step 1: 给 switch_session 恢复与版本竞态写失败测试**

  mock `switch_session` 返回 `active_model_id`；断言普通响应恢复模型。用 deferred Promise 暂停响应，期间 `setModel` 递增 `_sessionModelVersion`，再释放旧响应，断言旧模型不覆盖本地新选择。

- [x] **Step 2: 给两类新建会话写首条消息模型持久化测试**

  从模型 `m-current` 的已有会话分别调用 `createGlobalSession`、`createSessionInGroup`，断言 `draftModelId === 'm-current'`；再发送首条消息，断言后端第五参数为 `m-current`。

- [x] **Step 3: 运行前端模型测试确认失败**

  Run: `cd frontend; npm test -- appStore.modelSession.test.ts`

- [x] **Step 4: 在创建 pending 会话时冻结当前模型**

  在覆盖 `currentSessionName` 前保存 `const inheritedModelId = this.activeModelId` 与名称；建立新的 pending runtime 后显式写入 `modelId/modelName`，并设置 `draftModelId = inheritedModelId`。不得修改已有会话的绑定。

- [x] **Step 5: 运行前端模型测试**

  Run: `cd frontend; npm test -- appStore.modelSession.test.ts`

### Task 3: tiktoken 重试、压缩预算与截断精度

**Files:**
- Modify: `core/memory_compressor.py`
- Modify: `tests/test_memory_compressor_chunks.py`
- Create: `tests/test_memory_compressor_encoding.py`

**Interfaces:**
- Produces: `_get_encoding(timeout=10.0)` 失败后按冷却重试；`_compression_chunk_budget(window) -> int`；截断内容与预算严格一致。

- [x] **Step 1: 写失败状态冷却与单加载线程测试**

  注入 monotonic 与 fake `tiktoken.get_encoding`：第一次抛错后冷却内返回 `None`；冷却期满只启动一次重试；成功后状态变 `ready` 且后续复用同一编码器。

- [x] **Step 2: 实现锁保护的可重试状态机**

  `_encoding_state` 增加 `failed_at`，新增 `_encoding_lock` 与常量 `ENCODING_RETRY_COOLDOWN_SECONDS = 300.0`。只允许 `idle` 或冷却到期的 `failed` 转入 `loading`；加载线程在锁内发布 `ready/failed`。

- [x] **Step 3: 写 4096 小窗口预算和后缀计费失败测试**

  当时断言预算为 `max(512, int(window * 0.4)) == 1638`；后续超时修复已进一步收紧为 25%，并增加按 prompt token 动态超时。构造单条超长消息时仍断言“保留前缀 token + 截断后缀 token”不超过预算。

- [x] **Step 4: 实现统一预算函数和后缀预留**

  引入 `TRUNCATION_SUFFIX = "\n...[该条消息过长，中段已截断]..."`；二分上限使用 `chunk_token_budget - _estimate_tokens(TRUNCATION_SUFFIX)`，最终 `token_count = _estimate_tokens(content)`，不得硬写成预算值。

- [x] **Step 5: 运行压缩测试**

  Run: `venv\Scripts\python -m pytest tests/test_memory_compressor_chunks.py tests/test_memory_compressor_encoding.py -q`

### Task 4: token 基线绑定真实模型身份

**Files:**
- Modify: `core/memory.py`
- Modify: `core/agent_service.py`
- Modify: `tests/test_token_usage.py`
- Modify: `tests/test_agent_service.py`

**Interfaces:**
- Changes: `MemoryManager.get_last_token_usage(session_id)` 返回现有字段外再返回 `model_name`。
- Consumes: `model_name` 为 LiteLLM 完整模型串，如 `openai/deepseek-v4-pro`。

- [x] **Step 1: 写最近用量模型身份测试**

  写入 `provider='openai', model='m1'`，断言返回 `model_name == 'openai/m1'`；空 provider 时返回裸 model。

- [x] **Step 2: 写切模型基线测试**

  当 last 的 `model_name != current model_name` 时，即使真实值更小也必须返回 estimated；相同模型仍维持 `min(real, estimated)`。

- [x] **Step 3: 扩展 SQL 查询并实现比较**

  `SELECT` 增加 provider/model；`get_session_token_baseline` 仅在身份相同或旧记录无身份时使用 real，否则作废 real。

- [x] **Step 4: 运行 token 测试**

  Run: `venv\Scripts\python -m pytest tests/test_token_usage.py tests/test_agent_service.py -q`

### Task 5: GUI 首回合会话落库与无变化写抑制

**Files:**
- Modify: `gui_bridge.py`
- Modify: `tests/test_gui_tool_events.py`

**Interfaces:**
- Consumes: `MemoryManager.ensure_session`、`get_session_model_id`、`set_session_model_id`。
- Produces: 任意首回合开始前会话已存在；模型绑定仅在值变化时 UPDATE。

- [x] **Step 1: 写首回合与第二回合 DB 调用测试**

  fake memory 记录调用顺序，断言 `ensure_session` 先于 `set_session_model_id`；绑定相同 model id 时不调用 set，不同或空绑定时调用一次。

- [x] **Step 2: 重排 worker 初始化顺序**

  先解析并校验 `turn_workdir`，进入 workspace scope、调用 `ensure_session`、绑定 TLS；随后读取 `get_active_model(session_name)`。比较当前绑定与实际模型 id，只在不同时写入。

- [x] **Step 3: 运行 GUI worker 测试**

  Run: `venv\Scripts\python -m pytest tests/test_gui_tool_events.py tests/test_gui_security.py -q`

### Task 6: Y-code 数据路径统一按线程 workdir 解析

**Files:**
- Modify: `core/memory_kairos.py`
- Modify: `tests/test_memory_kairos.py`

**Interfaces:**
- Consumes: `core.workspace_context.get_workdir()`。
- Produces: `_resolve_runtime_path(path) -> str`，相对路径以线程 workdir 为基准，绝对路径保持不变。

- [x] **Step 1: 写相对路径基准失败测试**

  线程 workdir 位于临时项目目录、进程 cwd 指向另一目录时，断言相对路径按线程 workdir 解析；验证 YCODE_DIR 内外边界。

- [x] **Step 2: 实现统一解析并保留 realpath/commonpath 防穿越**

  `_is_within_dir` 对 path 先调用线程 workdir 解析，再 `realpath`；root 继续使用其绝对真实路径。

- [x] **Step 3: 运行记忆路径测试**

  Run: `venv\Scripts\python -m pytest tests/test_memory_kairos.py tests/test_tools.py -q`

### Task 7: 完整 diff 统计、解析缓存、主题颜色与格式

**Files:**
- Modify: `core/harness/tool_adapters.py`
- Modify: `core/turn_renderer.py`
- Modify: `gui_bridge.py`
- Modify: `tests/test_tool_pipeline.py`
- Modify: `tests/test_turn_renderer.py`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/utils/toolTrace.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/stores/appStore.toolEvents.test.ts`
- Modify: `frontend/src/utils/turnGroups.ts`
- Modify: `frontend/src/utils/turnGroups.test.ts`
- Modify: `frontend/src/components/ChangedFilesPanel.vue`
- Modify: `frontend/src/components/ChangedFilesPanel.test.ts`

**Interfaces:**
- Changes: `diff_ready` 增加整数 `added`、`removed`，`diff_text` 仍只承载最多 25 行展示文本。
- Changes: `LegacyDiff` 增加可选 `added`、`removed`，旧历史无字段时继续本地统计。

- [x] **Step 1: 写大 diff 完整统计失败测试**

  生成超过 25 行的 diff，断言事件 `diff_text` 仍截断但 `added/removed` 等于完整 diff；桥接和 store 必须原样保留两字段。

- [x] **Step 2: 在事件产生处计算完整统计**

  对原始 `diff_text` 调用统一 `_count_diff_changes`（抽取到无 UI 依赖的小函数或复用 turn renderer 纯函数），将结果加入 payload。CLI renderer 优先采用 payload 统计，旧事件才解析 text。

- [x] **Step 3: 前端汇总优先使用后端统计**

  `collectFileChanges` 对每个 diff 使用 `added/removed`，字段缺失时才调用 `countDiffChanges(diff_text)`，保证历史兼容。

- [x] **Step 4: 为 ChangedFilesPanel 建立 computed 解析缓存**

  生成 `Map<path, DiffViewBlock[]>`；模板读取 `diffBlocksByPath.get(c.path) ?? []`，`props.changes` 或文本变化时 computed 自动失效。

- [x] **Step 5: 变量化 diff 背景色并修复测试格式**

  使用 `color-mix(in srgb, var(--green) 13%, transparent)` 与 red 对应表达式；将 `});});` 拆为独立闭合行。

- [x] **Step 6: 运行 diff 前后端测试与构建**

  Run: `venv\Scripts\python -m pytest tests/test_tool_pipeline.py tests/test_turn_renderer.py -q`

  Run: `cd frontend; npm test -- appStore.toolEvents.test.ts turnGroups.test.ts ChangedFilesPanel.test.ts`

  Run: `cd frontend; npm run build`

### Task 8: 并行会话子代理模型隔离

**Files:**
- Modify: `core/tools.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_session_parallel_isolation.py`

**Interfaces:**
- Changes: `_active_model_tuple_or_none(session_name: str | None = None) -> tuple | None`。
- Changes: `_snapshot_model_config(prefer_secondary=True, session_name=None) -> tuple | None`。
- Produces: `SubagentTask.model_config` 在创建时绑定 `parent_session` 对应的模型。

- [x] **Step 1: 写被全局覆盖的并发回归测试**

  会话 A、B 分别绑定不同模型，将全局 tuple 故意设为 B；在 A 的线程路由中创建任务，断言 `task.model_config` 是 A。对自定义 `model=inherit` 再断言实际请求模型仍为 A。

- [x] **Step 2: 显式按 parent_session 取快照**

  `SubagentManager.start` 调用 `_snapshot_model_config(session_name=parent_session)`；helper 优先 `get_active_model(session_name)`，无会话或无模型时才回落 legacy global tuple/config。

- [x] **Step 3: 修正 custom inherit 路径**

  `_run_default_subagent` 使用 `_active_model_tuple_or_none(task.parent_session)`，不读取进程级最后写入者。

- [x] **Step 4: 运行子代理与并发隔离测试**

  Run: `venv\Scripts\python -m pytest tests/test_tools.py tests/test_session_parallel_isolation.py -q`

### Task 9: 全量验证与遗留文档收口

**Files:**
- Modify: `docs/REVIEW_TODO_20260820.md`

- [x] **Step 1: 运行 Python 语法检查**

  Run: `venv\Scripts\python -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py`

- [x] **Step 2: 运行关键 Python 回归集**

  Run: `venv\Scripts\python -m pytest tests/test_permission_broker.py tests/test_llm_events_fixes.py tests/test_tool_pipeline.py tests/test_uninstall_engine.py tests/test_memory_compressor_chunks.py tests/test_memory_compressor_encoding.py tests/test_token_usage.py tests/test_agent_service.py tests/test_gui_tool_events.py tests/test_gui_security.py tests/test_memory_kairos.py tests/test_tools.py tests/test_session_parallel_isolation.py tests/test_turn_renderer.py -q`

- [x] **Step 3: 运行前端全量测试与构建**

  Run: `cd frontend; npm test`

  Run: `cd frontend; npm run build`

- [x] **Step 4: 运行 Provider 字节稳定性守卫**

  Run: `venv\Scripts\python -m pytest tests/test_prompt_byte_stability.py -q`

- [x] **Step 5: 更新遗留文档**

  将 1–16 项标记为已完成，逐项记录实现文件和验证测试；不得删除历史背景与当天已修项。

- [x] **Step 6: 检查工作区变更**

  Run: `git diff --check`

  Run: `git status --short`
