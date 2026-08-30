# Agent Loop Hardening Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复两轮独立审查确认的接管生命周期、终态回调、租约 fencing、进展判定和 GUI/CLI 可见性缺陷，保证子 Agent 首次超时后只能由主 Agent 延续执行，跨回合、崩溃恢复、验证失败和用户取消均有确定出口。

**Architecture:** 用持久化的 `takeover_required` 标记表达“该计划项已经发生过子 Agent 超时，后续永久禁止重新委派”的业务事实；用调度器原子 API 管理 queued/active/release/resume，并在真实主回合开始和结束处接线。终态回调采用 exactly-once claim，ProgressGuard 只接受稳定、可证明的进展信号，GUI/CLI 从计划快照恢复展示状态而不是依赖一次性事件。

**Tech Stack:** Python 3.12、pytest、threading、Vue 3、Pinia、Vitest、prompt_toolkit CLI。

**Spec:** `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md` 第 14 节；审查依据为 `docs/superpowers/reviews/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover-review-merged.md` 与 `docs/superpowers/reviews/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover-review.md`。

## Global Constraints

- 子 Agent 首次 `timed_out` 后，`takeover_required` 永久保持为 `true`；普通 `plan_retry`、状态变化或重启都不得清除。
- `takeover_required=true` 的计划项永远不得再次进入 `delegated`，也不得创建第二个子 Agent task。
- 一个会话同一时刻最多一个 `in_progress/main_takeover`；其余接管项严格按 `takeover_sequence` FIFO。
- 接管属于原尝试的延续，不增加 `attempts` 或 `delegation_count`。
- 用户取消必须进入 `cancelled`，不得自动恢复；NoProgressError、模型自然结束或系统异常未收尾时进入 `takeover_queued`，下一真实主回合继续。
- 新主回合必须在第一次 Provider 请求前认领 orphan/queued takeover；不得依赖模型先调用工具。
- 旧子 Agent 租约查询异常必须 fail-closed；灾难性命令仍直接拒绝。
- 不修改 Harness Journal 正文边界、Provider 请求黄金字节、DeepSeek Prompt Ledger 语义。
- 保留用户工作树中与本计划无关的修改；禁止批量重写或清理工作树。
- Git commit、push、merge 均未获授权。每个 Task 只记录建议提交信息，必须等用户明确授权后才能执行。
- 每完成一个 Task，立即更新本文 Execution Record；额度不足时把当前 Task 的 RED/GREEN 命令、输出和剩余步骤写入 Notes，不得把未完成项标成 completed。

## Review Verdict Matrix

| 问题 | 结论 | 本计划处理 |
|---|---|---|
| queued/orphan takeover 无生产认领路径 | 确认存在，P1 | Task 1-2 |
| `_terminate` completed/cancelled 双回调 | 确认存在，已动态复现 `['completed', 'cancelled']` | Task 4 |
| 会话名 `subagent-*` 误触发 fencing | 确认存在 | Task 5 |
| fencing 异常路径 fail-open | 确认存在 | Task 5 |
| plan_finish 失败后恢复文案无可达路径 | 确认存在；仅改文案不足 | Task 1、3 |
| plan_finish failed → retry → 新子 Agent | 确认存在，违反核心需求 | Task 1、3 |
| 非计划 run_subagent 被无条件去噪 | 确认存在 | Task 6 |
| retry 两段提交/CAS 不完整 | 确认存在 | Task 1 |
| attempts 仍进入 ProgressGuard 指纹 | 审查文档漏报，代码确认存在 | Task 6 |
| plan_updated 覆盖 GUI 接管状态 | 审查文档漏报，代码确认存在 | Task 8 |
| FIFO 晋升没有 handoff 展示事件 | 审查文档漏报，代码确认存在 | Task 8 |
| 模型可伪造隐藏 timeout 参数 | 部分成立；上限可防扩大，但仍非 system-owned | Task 7 |
| queue_position 锁外重算 | 确认存在，展示级 | Task 8 |
| files 限制未 realpath | 确认存在，纵深防御 | Task 5 |
| ToolExecutionRow 多结果 JSON 解析失败 | 确认存在，展示级 | Task 8 |
| wait_for_idle 早于 callback 完成 | 确认存在；容量清理也需同查 | Task 4 |

## Execution Record

| Task | Status | RED | GREEN | Notes |
|---|---|---|---|---|
| 1. 持久接管事实与原子状态机 | completed | 7 failed（字段/API/FIFO/重试缺口） | 131 passed | `takeover_required` 持久化；CAS 扩展；claim/release/resume；普通 retry/重新委派被阻断；未执行 commit。 |
| 2. 主回合开始/结束生命周期接线 | completed | 2 failed / 1 passed | 130 passed | 在 CLI/GUI 共用的 `chat_with_model_events` 中首请求前 claim、所有出口 release；用户取消落 cancelled，子 Agent 回合不认领；未执行 commit。 |
| 3. 禁止重派与显式恢复接管 | completed | 4 failed | 297 passed | 新增 `plan_resume_takeover`（schema 仅 item_id，turn 由 adapter 注入）；retry/run_subagent 双重拒绝；修正超时提示；未执行 commit。 |
| 4. exactly-once 终态回调 | completed | 1 failed（复现 completed/cancelled 双回调） | 211 passed | callback 锁内认领；finalizing 期间取消仅 signal；done_event 成为回写/通知完成屏障；idle/drain/prune 同步；未执行 commit。 |
| 5. fencing 与路径约束加固 | completed | 4 failed | 241 passed | 严格 `subagent-[0-9a-f]{8}` helper 与生成共用；fencing 前置且异常 fail-closed；files 使用 realpath/abspath/normcase；未执行 commit。 |
| 6. ProgressGuard 稳定进展语义 | completed | 3 failed / 1 passed | 132 passed | 计划指纹移除 attempts；计划绑定委派完全由计划快照判定；无计划委派采用 canonical 参数与稳定 status/result，排除 task id/耗时/error；7 个不同成功委派可完成第 8 次收尾请求；未执行 commit。 |
| 7. 系统拥有的子 Agent 时间预算 | completed | 1 failed / 1 passed | 62 passed / 201 deselected | adapter 固定传入 `timeout_seconds=None`，schema 外伪造字段无法穿透；内部 Python 调用仍可显式传 600 秒、后台仍保留 3600 秒上限；未执行 commit。 |
| 8. GUI/CLI 接管可见性闭环 | completed | 后端 2 failed；前端 3 failed / 14 passed | 后端 174 passed；前端 36 passed；build passed | 调度器锁内返回 queue_position；initial/claim/promote/resume 共用 handoff payload；plan_updated 保留完整接管事实并派生可恢复文案；多调用逐条解析接管徽标；CLI 去重测试保持通过；未执行 commit。 |
| 9. 全量回归、spec 与交接 | completed | 全前端初跑 382 passed / 1 failed（最大化启动同步缺口） | Python 279+167+77+30 passed；frontend 383 passed；build/py_compile/diff-check passed | spec 与原计划补充最终状态机；修复 App 启动时未同步宿主最大化状态；逐条验收 10/10；无已知失败或未完成项；未执行 commit。 |

---

### Task 1: 持久接管事实与原子状态机

**Files:**
- Modify: `core/plan.py`
- Modify: `core/task_scheduler.py`
- Test: `tests/test_plan.py`
- Test: `tests/test_task_scheduler.py`
- Test: `tests/test_session_parallel_isolation.py`

**Interfaces:**
- Produces: 计划项字段 `takeover_required: bool`，默认 `False`。
- Produces: `PlanManager.transition_execution(..., expected_handoff_task_id=None, expected_takeover_turn_id=None, expected_owner=None, takeover_required=None)`。
- Produces: `TaskScheduler.claim_takeover_for_turn(turn_id: str) -> dict[str, Any] | None`。
- Produces: `TaskScheduler.release_takeover_for_turn(turn_id: str, *, cancelled: bool, reason: str) -> dict[str, Any] | None`。
- Produces: `TaskScheduler.resume_takeover(item_id: str, turn_id: str) -> dict[str, Any] | None`。

- [x] **Step 1: 写持久标记和禁止清除的失败测试**

```python
def test_timed_out_item_keeps_takeover_required_across_failure_retry_and_reload(tmp_path):
    plan = PlanManager(session_id="s", storage_dir=tmp_path)
    plan.replace([{"id": "a", "subject": "work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item("a", "subagent-1234abcd", "subagent:implement", None)
    assert scheduler.handoff_to_main("a", "subagent-1234abcd", "turn-1", "timeout")
    assert plan.snapshot()[0]["takeover_required"] is True
    assert scheduler.fail("a", "verification failed")
    assert scheduler.retry("a") is False
    reloaded = PlanManager(session_id="s", storage_dir=tmp_path)
    assert reloaded.snapshot()[0]["takeover_required"] is True
```

- [x] **Step 2: 写 queued/orphan 认领、释放和 FIFO 失败测试**

覆盖：重启后的 `pending/takeover_queued` 可被新 turn 认领；旧 `main_takeover` 绑定死亡 turn 时由新 turn 原子重绑；已有旧 queued 且无 active 时，新 timeout 不得插队；多会话同名 item 隔离；依赖未满足时明确返回不可认领原因或保持 queued，不能静默丢失。

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_task_scheduler.py tests/test_session_parallel_isolation.py -k "takeover_required or claim_takeover or release_takeover or orphan or fifo" -q
```

Expected: 新测试 FAIL，证明生产接线和持久标记尚不存在。

- [x] **Step 3: 实现持久字段和向后兼容规范化**

在 `_normalize_item` 中默认 `takeover_required=False`；若旧数据的 `execution_mode` 是 `main_takeover/takeover_queued` 或存在 `handoff_task_id`，兼容推导为 `True`。该字段只能由内部调度转换设置，不能加入模型可写的 `plan_write/plan_update` schema。

- [x] **Step 4: 扩展 CAS 并把 retry 合并为一次提交**

`transition_execution` 在同一 PlanManager 锁内同时校验 status、mode、handoff task id、turn id、owner；`TaskScheduler.retry()` 使用一次 `transition_execution`，不得先 `plan.update` 再清租约。对 `takeover_required=True` 直接返回 False，交由 Task 3 的恢复入口处理。

- [x] **Step 5: 实现 claim/release/resume**

`claim_takeover_for_turn` 必须幂等：同 turn 已 active 直接返回；旧 turn active 先保留 sequence 进入队列，再按 sequence/id 认领最早项。`release_takeover_for_turn(cancelled=True)` 标记 cancelled；非取消则转为 `pending/takeover_queued` 并清 owner/turn。`resume_takeover` 只接受 `takeover_required=True` 且 failed/blocked/cancelled/pending 的项目，不增加 attempts。

- [x] **Step 6: 修复调度器重建**

`_reset_orphaned_in_progress` 不得生成 `pending/main_takeover`。delegated 孤儿按既有失败/恢复语义处理；takeover 孤儿转成 `pending/takeover_queued`，保留 `takeover_required`、handoff task id 和 sequence。

- [x] **Step 7: 运行回归并记录**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_task_scheduler.py tests/test_session_parallel_isolation.py tests/test_tool_pipeline.py -q
```

Suggested commit: `fix(plan): close takeover state lifecycle`（未授权，不执行）。

---

### Task 2: 主回合开始/结束生命周期接线

**Files:**
- Modify: `core/llm.py`
- Modify: `main.py`
- Modify: `gui_bridge.py`
- Modify: `core/task_scheduler.py`
- Test: `tests/test_harness_runtime.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_main_ui_events.py`
- Test: `tests/test_cancellation.py`

**Interfaces:**
- Consumes: Task 1 的 `claim_takeover_for_turn`、`release_takeover_for_turn`。
- Produces: `_claim_takeover_at_turn_start(parent_session, parent_turn_id, event_callback)`，只在真实主 Agent 回合调用。

- [x] **Step 1: 写跨回合 E2E RED 测试**

覆盖以下真实路径：后台子 Agent 在派发 turn 结束后超时；自动汇总新 turn 在第一次 Provider 请求前认领；主 Agent 能 read/write/plan_finish；同计划项 SubagentManager task 数始终为 1。

- [x] **Step 2: 写 turn 结束语义 RED 测试**

```text
normal/no-progress/error without plan_finish -> pending/takeover_queued
user cancel -> cancelled + takeover_required
completed plan_finish -> no-op, remains completed
```

GUI 与 CLI 必须运行同一调度 API；禁止 CLI `finally` 把 main_takeover 误标成“子 agent 异常退出” failed。

- [x] **Step 3: 在 Provider 请求前接入 claim**

在主 Agent 的 plan session/workdir 路由已经绑定、但首个 Provider 请求尚未构造时调用。`allow_subagents=False` 的子 Agent 自身回合不得认领主接管项。认领成功发送统一 `plan_item_handoff` 与 `plan_updated` 事件。

- [x] **Step 4: 在 GUI/CLI finally 接入 release**

GUI 使用本回合 `cancelled` 事实区分 cancelled/requeue；CLI 使用 `turn_interrupted` 和 cancel_event 区分。普通 delegated 项仍使用现有子代理终态回调，不得被 takeover release 误处理。

- [x] **Step 5: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_llm_events.py tests/test_main_ui_events.py tests/test_cancellation.py tests/test_agent_turn_e2e.py -q
```

Suggested commit: `fix(agent): reclaim takeover work across turns`（未授权，不执行）。

---

### Task 3: 永久禁止重派与显式恢复接管

**Files:**
- Modify: `core/tools.py`
- Modify: `core/plan.py`
- Modify: `core/task_scheduler.py`
- Modify: `core/tool_registry.py`
- Modify: `core/harness/tool_adapters.py`
- Modify: `core/harness/tool_pipeline.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_tool_registry.py`
- Test: `tests/test_tool_pipeline.py`

**Interfaces:**
- Produces model tool: `plan_resume_takeover(item_id: str)`；运行上下文中的 turn id 由 adapter 注入，不暴露 schema。
- Consumes: Task 1 的 `resume_takeover` 与 `takeover_required`。

- [x] **Step 1: 写“验证失败后仍不能重派”的 RED 测试**

```python
def test_plan_finish_failure_cannot_be_redelegated_after_retry(...):
    # timeout -> main_takeover -> plan_finish verification failed
    assert item["status"] == "failed"
    assert item["takeover_required"] is True
    assert json.loads(run_subagent(...))["reason"] == "main_takeover_required"
    assert subagent_task_count == 1
```

测试必须继续调用普通 `plan_retry(force=True)`，断言其拒绝并明确指向 `plan_resume_takeover`，不能只测试失败后的瞬时状态。

- [x] **Step 2: 写恢复主接管 RED 测试**

`plan_resume_takeover` 对 failed/blocked/cancelled takeover 项在当前 turn 恢复 `in_progress/main_takeover`；普通项目、错误 turn、无运行上下文均拒绝；恢复不增加 attempts/delegation_count，之后 direct tool 与 `plan_finish` 可完成。

- [x] **Step 3: 实现双重拒绝**

`run_subagent` 首先检查 `takeover_required`，而不是只检查当前 `execution_mode`；`start_delegated_item` 在调度器层再次拒绝，防止其他 Python 调用方绕过工具层。

- [x] **Step 4: 注册显式恢复工具**

schema 只含 `item_id`；adapter 注入 `parent_turn_id/event_callback`；加入 plan root-control 与 repeat-guard exempt 清单。恢复成功发送 handoff/plan_updated 事件。

- [x] **Step 5: 修正文案和旧 timeout hint**

删除“决定 plan_retry 或重试子代理”的旧提示。失败文案必须区分：仍 active、queued、failed-awaiting-resume、cancelled。任何 takeover-required 状态都不得建议 `run_subagent`。

- [x] **Step 6: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_registry.py tests/test_tool_pipeline.py tests/test_plan.py -q
```

Suggested commit: `fix(agent): prevent timeout task redelegation`（未授权，不执行）。

---

### Task 4: exactly-once 终态回调与 finalization 可见性

**Files:**
- Modify: `core/tools.py`
- Modify: `core/task_scheduler.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_cancellation.py`
- Test: `tests/test_task_scheduler.py`

**Interfaces:**
- Produces invariant: 每个 SubagentTask 的 terminal callback 最多一次；`done_event` 是回调和 handoff 全部完成的唯一可见屏障。

- [x] **Step 1: 把动态复现写成 RED 测试**

使用 `entered/release` 两个 Event 阻塞 completed callback，同时调用 `cancel_foreground`。断言 callback 列表只有一个元素，并断言 SubagentTask 与计划项终态一致，不允许只断言调用次数。

- [x] **Step 2: 写 idle/capacity RED 测试**

callback 阻塞期间：`wait_for_idle` 必须为 False；`get_active_count` 必须非零；容量清理不得删除该 task；callback 完成并 set done_event 后才可 idle/prune。

- [x] **Step 3: 恢复原子 callback claim**

在 `_terminate` 锁内仅当 `invoke_callback and not task.callback_invoked` 时认领 callback。已经由 `_complete` 认领时，取消只设置 cancel_event/abort 请求，不得二次执行 terminal callback。保留“cancel_event 先于超时 handoff callback、done_event 最后”的新顺序。

- [x] **Step 4: 让状态查询以 done_event 为 finalization 屏障**

`get_active_count`、`wait_for_idle` 和 `_prune_completed_locked` 必须把 `done_event` 未设置的终态记录视为 finalizing，避免提前 idle 或容量删除。

- [x] **Step 5: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_cancellation.py tests/test_task_scheduler.py -q
```

Suggested commit: `fix(subagent): make terminal callback exactly once`（未授权，不执行）。

---

### Task 5: fencing 与接管文件约束加固

**Files:**
- Modify: `core/harness/tool_pipeline.py`
- Modify: `core/tools.py`
- Test: `tests/test_tool_pipeline.py`

**Interfaces:**
- Produces: `is_subagent_task_id(value: str) -> bool`，严格匹配 `^subagent-[0-9a-f]{8}$`。

- [x] **Step 1: 写 fencing RED 测试**

覆盖：主回合 turn id 为 `subagent-demo:12345678` 不被 fence；真正失效的 `subagent-1234abcd` 被 fence；`is_execution_lease_active` 抛异常时 fail-closed 且 adapter/权限询问均未执行。

- [x] **Step 2: 实现严格 ID 判定与 fail-closed**

任务 ID 生成和 fencing 共用同一 helper，禁止两套格式判断。异常返回 `STALE_EXECUTION_LEASE_TEXT` 或抛显式 invariant error，不得 `return None`。

- [x] **Step 3: 写 junction/symlink RED 测试**

在平台支持时创建工作区内链接指向未声明文件，断言 write/edit 被 files 限制拦截；Windows 无创建链接权限时用 monkeypatch 验证 realpath 归一化，不得静默 skip 全部逻辑。

- [x] **Step 4: 使用 realpath 归一化**

路径比较采用 `normcase(realpath(abspath(...)))`；保留工作区权限和沙盒二次检查。注释明确 `execute_command` 不受 files 精确列表限制是既有设计取舍，仍受命令权限策略约束。

- [x] **Step 5: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py -q
```

Suggested commit: `fix(harness): harden stale lease fencing`（未授权，不执行）。

---

### Task 6: ProgressGuard 稳定进展语义

**Files:**
- Modify: `core/llm.py`
- Modify: `core/plan.py`
- Modify: `core/harness/progress_guard.py`（仅在需要 turn-local delegation signature 时）
- Test: `tests/test_progress_guard.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_harness_runtime.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- Produces: 计划指纹不含 attempts/delegation_count/task id/elapsed/error 文案。
- Produces: 仅绑定了 `plan_item_id` 的 run_subagent 结果按计划状态去噪；非计划委派保留稳定进展信号。

- [x] **Step 1: 写 attempts 单独变化 RED 测试**

```python
before = plan_state_fingerprint(session)
manager.update("a", attempts=manager.snapshot()[0]["attempts"] + 1)
assert plan_state_fingerprint(session) == before
```

状态、execution_mode、depends_on 变化仍必须改变指纹。

- [x] **Step 2: 写非计划委派 RED/E2E**

连续 7 个参数不同且成功的无计划 `run_subagent` 不得在第 7 次 Provider 请求前被 NoProgressError 误杀；同参数重复调用仍受 RepeatCallGuard/NoProgressGuard 限制，不能用动态 task id 无限续命。

- [x] **Step 3: 移除 attempts 并条件去噪**

从 `plan_state_fingerprint` 删除 attempts。`_step_progress_observation` 从原 tool call 参数解析 `plan_item_id`：非空时忽略动态正文，空时使用 canonical arguments + 稳定 status/result 摘要作为进展；不得把随机 task id、耗时或错误文案直接纳入。

- [x] **Step 4: 保持 6 步熔断铁律**

原有 plan_read/manage_task 空转测试继续断言 `request_count == 6`；接管读取预算仍是第 12 个计进展、第 13 个不计。

- [x] **Step 5: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_progress_guard.py tests/test_llm_events.py tests/test_harness_runtime.py tests/test_plan.py -q
```

Suggested commit: `fix(harness): use stable progress signals`（未授权，不执行）。

---

### Task 7: 系统拥有的子 Agent 时间预算

**Files:**
- Modify: `core/harness/tool_adapters.py`
- Modify: `core/tool_registry.py`
- Modify: `core/tools.py`
- Test: `tests/test_tool_registry.py`
- Test: `tests/test_tool_pipeline.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Model tool schema and adapter path do not accept `timeout_seconds`。
- Internal Python callers may continue calling `run_subagent(..., timeout_seconds=...)`，foreground hard max 600s、background max 3600s。

- [x] **Step 1: 写模型伪造隐藏参数 RED 测试**

构造 raw tool call，在 schema 未声明的情况下传 `timeout_seconds=3600`，断言 adapter 调用的 run_subagent 收到 `None`，随后使用角色默认值。测试还要证明 internal direct Python 调用仍接受合法显式超时。

- [x] **Step 2: 删除 adapter 原始参数读取**

`execute_run_subagent` 不再读取 `arguments.get("timeout_seconds")`；不要依赖“schema 没展示所以模型不会传”。保留 `run_subagent` 内部上下限校验作为系统调用方防线。

- [x] **Step 3: 运行测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_tool_pipeline.py tests/test_tools.py -k "subagent or timeout" -q
```

Suggested commit: `fix(agent): keep timeout budget system-owned`（未授权，不执行）。

---

### Task 8: GUI/CLI 接管可见性闭环

**Files:**
- Modify: `core/tools.py`
- Modify: `core/task_scheduler.py`
- Modify: `gui_bridge.py`
- Modify: `main.py`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/components/RightPanel.vue`
- Modify: `frontend/src/components/ToolExecutionRow.vue`
- Modify: `frontend/src/types/pywebview.d.ts`
- Test: `tests/test_main_ui_events.py`
- Test: `frontend/src/stores/appStore.plan.test.ts`
- Create or Modify: `frontend/src/components/ToolExecutionRow.test.ts`

**Interfaces:**
- `plan_updated` 快照携带并保留 execution_mode、owner、takeover_required、handoff_reason、takeover_sequence。
- 统一 handoff event 对 initial/queued/promoted/resumed 使用同一 payload，并由后端锁内给出 queue_position。

- [x] **Step 1: 写 store 覆盖 RED 测试**

先发送 `plan_item_handoff`，再发送包含同项的 `plan_updated`，断言接管说明仍存在；切换会话再回来仍恢复。queued→main_takeover 的 plan_updated 必须把说明更新为“主 Agent 正在接管”，不能保留旧队列文案。

- [x] **Step 2: 写 FIFO 晋升事件 RED 测试**

`plan_finish`、turn-start claim、resume 三条晋升路径都必须发 `plan_item_handoff(mode=main_takeover)`；CLI 固定文案只打印一次；初始 timeout 卡片仍保持 timed_out。

- [x] **Step 3: 让 plan_updated 成为可恢复事实源**

store 映射不得只保留 id/text/status；handoff_note 应根据 execution_mode/takeover_required 派生，不能只依赖瞬时事件。完成/失败/取消后清除 active 文案，但保留必要的历史状态字段。

- [x] **Step 4: 锁内计算 queue_position**

调度器在状态转换临界区内计算展示位置并通过返回对象/事件 payload 传出，删除 `core/tools.py` 的锁外二次快照排序。

- [x] **Step 5: 修复多调用徽标解析**

ToolExecutionRow 对每个 call.result 单独 JSON.parse，选择第一个 timed_out+handoff；不得先 join 再整体解析。补 main_takeover/takeover_queued/failed/非 JSON/多调用五类组件测试。

- [x] **Step 6: 运行测试与构建**

```powershell
npm --prefix frontend test -- src/stores/appStore.plan.test.ts src/stores/appStore.toolEvents.test.ts src/components/ToolExecutionRow.test.ts
venv\Scripts\python.exe -m pytest tests/test_main_ui_events.py tests/test_llm_events.py tests/test_session_parallel_isolation.py -q
npm --prefix frontend run build
```

Suggested commit: `fix(gui): persist takeover visibility across updates`（未授权，不执行）。

---

### Task 9: 全量回归、spec 与交接收尾

**Files:**
- Modify: `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`
- Modify: `docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md`
- Modify: this plan's Execution Record

- [x] **Step 1: 更新状态机规范**

补充 `takeover_required`、turn-start claim、turn-end release、验证失败后的 `plan_resume_takeover`、用户取消、exactly-once callback 和 GUI 可恢复展示规则。原计划 Tasks 3-10 在本修复完成前不得继续写“全部闭环”。

- [x] **Step 2: 跑 Python 四组全量矩阵**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_tool_registry.py tests/test_progress_guard.py -q
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_harness_journal.py tests/test_harness_message_commit.py tests/test_llm_events.py tests/test_plan.py tests/test_task_scheduler.py -q
venv\Scripts\python.exe -m pytest tests/test_cancellation.py tests/test_agent_turn_e2e.py tests/test_session_parallel_isolation.py tests/test_main_ui_events.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py -q
```

- [x] **Step 3: 跑前端与语法验证**

```powershell
npm --prefix frontend test
npm --prefix frontend run build
venv\Scripts\python.exe -m py_compile core/plan.py core/task_scheduler.py core/tools.py core/llm.py core/harness/progress_guard.py core/harness/tool_pipeline.py core/harness/tool_adapters.py core/tool_registry.py gui_bridge.py main.py
git diff --check
```

已知 `App.guiTheme.test.ts` 失败必须重新核对当前基线；不能因为旧记录说“无关”就省略实际输出。

- [x] **Step 4: 逐条验收**

```text
1. 首次 timeout 后 task count 永远为 1。
2. 后台超时跨 turn 可在首次 Provider 请求前被新主回合认领。
3. crash/orphan/NoProgressError/自然结束均不会生成永久卡死状态。
4. 用户取消为 cancelled，不自动接管或续推。
5. plan_finish 验证失败后只能 plan_resume_takeover，不能 plan_retry+run_subagent。
6. terminal callback 恰好一次，idle/prune 晚于 done_event。
7. fencing 严格匹配 task id 且异常 fail-closed。
8. attempts/task id/elapsed 不制造进展；非计划合法委派不被误杀。
9. GUI/CLI 在刷新、切会话和 FIFO 晋升后仍展示正确接管状态。
10. Prompt/Provider/DeepSeek golden 与 Journal 纯净性不回归。
```

- [x] **Step 5: 完成交接记录但不提交**

在 Execution Record 写入每组命令的真实末行、已知失败和未完成项。除非用户明确授权，不执行任何 git commit/push/merge。

Suggested final commit: `fix(agent): complete deterministic timeout takeover`（未授权，不执行）。

**Final Verification (2026-08-29):**

- Python 第 1 组：`279 passed in 8.71s`。
- Python 第 2 组首次为 `1 failed, 166 passed`；根因是 E2E 仍假设模型伪造的
  `timeout_seconds=30` 会穿透 adapter，假时钟与新的 system-owned 600 秒预算不匹配。
  测试夹具改为跨过 600 秒后，单条原故障链 `1 passed`，整组新鲜复跑
  `167 passed in 71.68s`。
- Python 第 3 组：`77 passed in 2.62s`。
- Python 第 4 组：`30 passed in 2.27s`。
- 全前端首次 `382 passed / 1 failed`。核对发现不是可忽略旧失败：`App.vue` 已把
  最大化状态迁入 store，却没有在启动时调用 `window_is_maximized` 初始化 store，
  导致最大化启动仍保留 8 个 resize handle。补启动同步后，单文件 `4 passed`，
  全前端新鲜复跑 `57 files / 383 tests passed`。
- `npm --prefix frontend run build` 通过；指定 Python 文件 `py_compile` 通过；
  `git diff --check` exit code 0（仅提示 Git 将来可能做 LF→CRLF 转换，无空白错误）。
- 验收映射：原超时 E2E 覆盖单 task/禁止重派/plan_finish；主回合事件测试覆盖
  turn-start claim、自然结束重排队与用户取消；计划/调度测试覆盖 crash/orphan/FIFO/
  resume；并发测试覆盖 callback exactly-once 与 done_event 屏障；pipeline 测试覆盖严格
  task id、fail-closed 与 realpath；ProgressGuard 测试覆盖稳定信号和无计划合法委派；
  store/组件/CLI 测试覆盖刷新、切会话、晋升和多调用徽标；golden 与 Journal 测试通过。
- 工作树还包含用户/此前模型的其他未提交改动；本轮未清理、回滚或提交任何文件，
  未执行 git commit/push/merge。

---

## Executor Handoff Prompt

```text
你现在接手仓库 E:\fch\Y-code_tui（dev 分支）中“Agent 循环加固与子 Agent 超时接管”的审查修复。

先完整阅读：
1. docs/superpowers/plans/2026-08-28-agent-loop-hardening-review-remediation.md
2. docs/superpowers/reviews/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover-review-merged.md
3. docs/superpowers/reviews/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover-review.md
4. docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md
5. docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md 第 14 节
6. 仓库根目录 AGENTS.md

目标不是只修改审查文案，而是完成生产状态机闭环：
- 子 Agent 首次 timed_out 后，同一计划项永远不能重新委派子 Agent；
- 后台超时、跨 turn、崩溃恢复、NoProgressError、自然结束和验证失败都有可达的主 Agent 接管出口；
- 用户取消保持 cancelled，不自动恢复；
- terminal callback 恰好一次；
- fencing fail-closed；
- ProgressGuard 不把 attempts/task id/elapsed 当进展，同时不误杀合法的非计划委派；
- GUI/CLI 在刷新、切会话和 FIFO 晋升后仍显示正确状态。

执行规则：
- 必须使用 test-driven-development：每个 Task 先写 RED，运行确认失败，再做最小实现，再跑 GREEN 和该 Task 回归。
- 严格按修复计划 Task 1 → Task 9 顺序执行，不得跳过 P1 或只做表面文案修改。
- 每完成一个 Task，立即更新修复计划的 Execution Record，记录真实测试命令、末行、失败和剩余项。
- 如果额度即将耗尽，停止开启新 Task；把当前 Task 已改文件、RED/GREEN 输出、未完成步骤和风险写入 Notes，确保下一模型可直接继续。
- 工作树有用户其他改动，只修改计划列出的相关文件；不要清理、覆盖或格式化无关文件。
- 定位优先使用具体文件 read/rg，不对 tmp 等无权限目录做全库扫描。
- 测试统一使用 venv\Scripts\python.exe -m pytest ...；前端使用 npm --prefix frontend ...。
- 禁止灾难性命令。未经用户明确授权，禁止 git commit、push、merge、reset、checkout 或删除文件。
- 不得为了通过测试削弱断言，不得改写 Provider golden 来迁就非必要实现，不得向 Harness Journal 写用户正文、工具参数或结果。

技术裁决：
- 合并审查 P1-1、P1-2 均确认存在；P1-2 已动态复现 callback 顺序 ['completed', 'cancelled']。
- 审查中“plan_finish 失败后只改文案、引导 plan_retry”的建议不充分：当前 plan_retry 会清租约并允许新 run_subagent，违反核心需求。必须增加持久 takeover_required，并提供主 Agent 专用恢复入口。
- 还必须修复审查漏报项：attempts 仍进入 plan_state_fingerprint、plan_updated 覆盖 GUI handoff 状态、FIFO 晋升缺事件、adapter 仍读取模型伪造的隐藏 timeout_seconds。

开始前先输出：当前分支、相关文件 git status、你准备执行的 Task 编号和 RED 命令。然后直接从第一个 pending Task 开始。
```
