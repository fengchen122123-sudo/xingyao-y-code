# Review Findings Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 修复审查确认的权限、会话模型、GUI 竞态、diff 兼容和测试真实性问题。

**Architecture:** 以 `config.YCODE_DIR` 作为数据根目录唯一事实源；显式会话路径禁止读取进程级模型状态；把 CLI Enter 决策和 GUI 模型绑定收敛为可独立测试的纯控制边界。所有修复先以行为测试复现，再做最小实现。

**Tech Stack:** Python 3、pytest、SQLite、Vue 3、Pinia、TypeScript、Vitest。

**Spec:** `docs/superpowers/specs/2026-08-20-review-findings-remediation-design.md`

## Global Constraints

- Y-code 数据根目录以 `config.YCODE_DIR` 为准。
- 数据目录读取、数据库和普通文件写入放行；仅顶层配置/MCP 文件写入询问。
- 显式 session 不得 fallback 到进程级模型 tuple。
- 不修改 Provider 请求字节稳定区域。
- 当前分支不创建 commit。

---

### Task 1: 修复数据根目录凭据分类

**Files:**
- Modify: `core/harness/tool_adapters.py`
- Test: `tests/test_llm_events_fixes.py`
- Test: `tests/test_tool_pipeline.py`

**Interfaces:**
- Consumes: `config.YCODE_DIR`、`_sandbox_outside_guard(...)`。
- Produces: 自定义数据目录与默认目录一致的沙盒/凭据语义。

- [x] 写自定义 `YCODE_DATA_DIR` 下读取、数据库写入、普通写入放行和顶层配置写入询问的失败测试。
- [x] 写凭据分类异常时失败关闭的失败测试。
- [x] 运行新增测试，确认分别因零询问和自动放行而失败。
- [x] 改为使用 `config.YCODE_DIR` 并让分类异常返回路径验证错误。
- [x] 运行权限与完整 pipeline 测试确认通过。

### Task 2: 关闭显式会话模型的全局 fallback

**Files:**
- Modify: `core/tools.py`
- Test: `tests/test_session_parallel_isolation.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Changes: `_active_model_tuple_or_none(session_name)` 显式会话解析失败返回 `None`。
- Produces: `SubagentManager.start` 与 custom `inherit` 使用父会话模型或显式失败。

- [x] 写 session-A 解析为空但进程 tuple 为 B 时不得返回 B 的失败测试。
- [x] 写 `SubagentManager.start` 保存 A 快照及 custom inherit 使用 A 的调用链测试。
- [x] 运行新增测试确认失败。
- [x] 在显式 session 分支终止 fallback；保留 `session_name=None` 的 CLI 兼容路径。
- [x] 运行子代理与并行隔离测试确认通过。

### Task 3: 防止 GUI worker 回写过期模型

**Files:**
- Modify: `gui_bridge.py`
- Test: `tests/test_gui_tool_events.py`

**Interfaces:**
- Changes: `_ensure_session_model_binding(memory, session_name, model_id, workdir)` 仅在当前绑定为空时写入。

- [x] 写当前绑定已变为 B、worker 快照仍为 A 时不得 set 的失败测试。
- [x] 运行测试确认旧 helper 会写回 A。
- [x] 将 helper 改为只初始化空绑定；保留先建会话与同值不写。
- [x] 运行 GUI worker/security 测试确认通过。

### Task 4: 让 CLI Enter 行为真正可测试

**Files:**
- Modify: `main.py`
- Modify: `tests/test_permission_broker.py`

**Interfaces:**
- Produces: `_submit_ask_user_text(text, clear_input, invalidate) -> bool | None`；非 ask_user 返回 `None`，ask_user 无论发送成功失败均返回 bool 表示已消费 Enter。

- [x] 写真实 broker 失效时 clear/选项回答均不调用的失败测试。
- [x] 运行测试确认 helper 不存在。
- [x] 抽取最小 Enter 控制 helper，成功才清输入和 invalidate，失败仍消费事件。
- [x] Enter handler 调用 helper，只有返回 `None` 才进入选项回答。
- [x] 运行 permission broker 测试确认通过。

### Task 5: 补齐完整 diff 与旧历史兼容

**Files:**
- Modify: `frontend/src/utils/toolTrace.ts`
- Modify: `frontend/src/utils/turnGroups.ts`
- Modify: `tests/test_tool_pipeline.py`
- Modify: `tests/test_gui_tool_events.py`
- Modify: `frontend/src/utils/turnGroups.test.ts`
- Modify: `frontend/src/stores/appStore.toolEvents.test.ts`

**Interfaces:**
- Produces: 后端截断展示但保留完整统计；前端缺字段时兼容 ANSI/Rich 文本。

- [x] 写超过 25 行 diff 的后端失败测试，断言截断文本与完整统计。
- [x] 写 GUI bridge `added/removed` 转发测试。
- [x] 写 `collectFileChanges` 对 ANSI/Rich legacy diff 的失败测试。
- [x] 运行新增测试确认 ANSI 收集失败，后端/桥接覆盖缺口可观察。
- [x] 统一前端 fallback 统计为 ANSI/Rich 兼容实现，移除重复口径。
- [x] 运行 diff 前后端测试与前端构建。

### Task 6: 文档与最终验证

**Files:**
- Modify: `docs/REVIEW_TODO_20260820.md`
- Modify: `docs/superpowers/plans/2026-08-20-review-todo-completion.md`
- Modify: `docs/superpowers/plans/2026-08-20-review-findings-remediation.md`

**Interfaces:**
- Produces: 保留历史背景、状态与实际验证一致的审查记录。

- [x] 恢复原遗留背景与“当天已修复项”，逐项标记完成并保留修复说明。
- [x] 勾选两个实施计划中真实完成的步骤。
- [x] 运行 Python 全量测试、前端全量测试、生产构建、语法检查和 `git diff --check`。
- [x] 检查 `git status --short`，确认无无关变更且未创建 commit。

## Verification Results

- Python 全量：1154 passed, 2 skipped。
- Provider 请求字节稳定守卫：5 passed。
- 前端全量：28 files, 167 tests passed。
- 生产构建：`vue-tsc -b && vite build` 成功。
- Python 语法检查与 `git diff --check`：退出码 0。
