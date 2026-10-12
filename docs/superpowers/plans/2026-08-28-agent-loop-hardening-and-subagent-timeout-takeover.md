# Agent 循环加固与子 Agent 超时接管 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复后台任务 revision/wait 实现中的确定性缺陷，并实现“同一计划项的子 Agent 首次超时后，由主 Agent 在原现场直接接管，禁止重新派发”的闭环。

**Architecture:** 先修复 `task_manager` 持久化、终态淘汰、参数校验和 attempts 计数，为后续状态机提供可靠基线；再把子 Agent 超时从 `failed → retry` 改为原计划项内的所有权移交，使用计划项级接管租约约束主 Agent 直接工具权限，并用显式 `plan_finish` 完成验证和收尾。ProgressGuard 继续负责普通无进展循环，同时增加接管核查阶段的有界只读进展与子 Agent 动态结果去噪。

**Tech Stack:** Python 3.12、pytest、SQLite/Harness Journal、prompt_toolkit CLI、Vue 3/Pinia GUI。

**Spec:** `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`。本计划的“超时所有权移交”要求是该 spec 的后续增补；若两者冲突，以本计划的 Global Constraints 和状态机定义为准。

**Reviewed Baseline:** `c64e94e`、`9eb98cf`、`174e1b1`、`bdebe13`（当前 `dev` 分支）。本计划不重做这四个提交，而是在其上修复审查发现的缺陷并补齐主 Agent 超时接管。

## Priority Classification

| Priority | Tasks | Execution rule |
|---|---|---|
| 必须修 | 1-7 | 属于正确性、并发状态、超时接管或无进展熔断问题；任何一项未完成都不能宣称故障已修复。 |
| 建议优化（本轮也实施） | 8-9 | 时间预算、上下文体积和 GUI/CLI 可观测性；用户已要求写入同一修复计划，因此不能静默省略，只能在交接记录中明确保持 pending。 |
| 最终验收 | 10 | 汇总验证与文档闭环；必须在 Tasks 1-9 完成后执行。 |

## Global Constraints

- 日常开发只在 `dev` 分支进行；不得主动合并到 `master` 或其他分支。
- 灾难性命令直接拒绝，不得请求授权后执行。
- 删除、Git commit、Git push 必须由用户明确授权；本计划中的提交步骤均先询问用户。
- 保留工作树中不属于本计划的现有修改，不得 reset、checkout、覆盖或顺手重构。
- 所有实现遵循 TDD：先写失败测试并确认失败，再写最小实现。
- 不修改权限四模式、白名单、沙盒边界、DeepSeek Prompt Ledger、Kimi 消息规范和 Provider 请求 golden。
- Harness Journal 继续只保存 ID、状态、序号、耗时和哈希；不得写入正文、reasoning、工具参数原文、工具结果原文或凭据。
- 子 Agent 首次超时后必须移交主 Agent；同一计划项不得再次 `run_subagent`。
- 超时接管属于同一次计划项尝试的延续，不增加 `attempts`。
- 用户主动取消不是超时接管：保持 cancelled 语义，不自动恢复、不自动续推。
- 主 Agent 接管前必须先看到子 Agent 任务 ID、阶段、耗时、错误和计划项现场；不得推倒重做已完成工作。
- 一个会话同一时刻只允许一个 active 主 Agent 接管项；其余同时超时项进入 FIFO takeover queue。
- 子 Agent 的真实 `task_id` 必须在计划项进入 delegated 状态时一并写入；不得用角色名、线程名或事后猜测值代替执行租约 ID。
- 模型不得自行把前台子 Agent 超时扩大到 2400/3600 秒；长命令使用后台任务 + `manage_task(wait)`。
- 每完成一个 Task，必须立即更新本文件的 Execution Record；中断时不得把未完成 Task 标成 completed。

---

## Authoritative State Machine

正常委派：

```text
pending
  → in_progress(execution_mode=delegated, owner=subagent:<type>)
  → completed | failed | cancelled
```

首次超时移交：

```text
in_progress(execution_mode=delegated)
  → 子 Agent timed_out
  → 先取消/封禁旧子 Agent
  → takeover_required=true（持久、不可由普通 retry 清除）
  → in_progress(execution_mode=main_takeover, owner=main:<turn_id>)
  → 主 Agent 核查现场并继续
  → plan_finish
  → completed | failed（failed 只能 plan_resume_takeover，禁止重派）
```

多个同时超时：

```text
第一个超时项 → main_takeover
后续超时项   → takeover_queued
active 项结束 → FIFO 提升下一 queued 项为 main_takeover
```

跨回合生命周期：

```text
新主回合首个 Provider 请求前 → claim 最早 takeover_queued
回合正常结束但未 plan_finish → pending(takeover_queued)，等待下一主回合
用户主动取消 → cancelled，不自动恢复、不自动续推
验证失败/阻塞/取消后用户明确继续 → plan_resume_takeover（同一次尝试）
```

调度与展示不变式：

- 终态 callback exactly-once；callback/通知未完成前不得进入 idle/drain/prune。
- 旧执行租约严格匹配 `subagent-[0-9a-f]{8}` 且查询异常 fail-closed。
- `plan_updated` 是 GUI 可恢复事实源；handoff 的 initial/claim/promote/resume 事件载荷一致，
  `queue_position` 在调度器锁内计算；CLI 同状态只打印一次。
- 计划指纹排除 attempts/task id/耗时/error；无计划的不同成功委派仍是合法进展。

明确禁止：

```text
timed_out → failed → plan_retry → pending → 新 run_subagent
```

---

## Execution Record

执行者每完成一个 Task 后更新对应行。`Commit` 只能填写真实 commit id；用户未授权提交时填写 `not-authorized`。

| Task | Status | Verification | Commit | Notes |
|---|---|---|---|---|
| 1. 后台任务终态持久化与容量 | completed | 23 passed; review clean | not-authorized | 4 个原始 RED + 2 个审查 RED 均完成闭环 |
| 2. wait 参数与任务指纹同步 | completed | 25 passed; review approved | not-authorized | 严格整数校验与首次 fingerprint 同步完成 |
| 3. attempts 与普通 retry 语义 | completed | 42 passed, 207 deselected | not-authorized | retry 不再自增 attempts；plan_retry 改显式 force |
| 4. 计划项接管状态与 FIFO | completed | 53 passed; safety 52 passed | not-authorized | transition_execution CAS + FIFO 队列完成 |
| 5. 子 Agent 超时原子移交与旧执行者封禁 | completed | 227 passed; targeted 6 passed | not-authorized | _terminate 重排 + fencing + main_takeover_required |
| 6. 主 Agent 计划项级执行权与 plan_finish | completed | 321 passed; targeted 12 passed | not-authorized | scoped 放行 + files 限制 + plan_finish |
| 7. ProgressGuard 接管适配与动态结果去噪 | completed | 117 passed; targeted 6 passed | not-authorized | 去噪 + 12 上限读取预算 + E2E |
| 8. 子 Agent 时间预算与上下文上限 | completed | 55 passed | not-authorized | schema 隐藏超时 + 前台 600s 硬上限 |
| 9. GUI/CLI 接管可见性 | completed | store 22 passed; CLI 124 passed; build ok | not-authorized | plan_item_handoff 事件 + 固定文案 |
| 10. 全量回归、文档和交接收尾 | completed (hardened 2026-08-29) | 279+167+77+30 passed; frontend 383/383; build/py_compile/diff-check ok | not-authorized | 严格审查修复后的新鲜全量验收通过；详见 remediation Task 9 |

> 2026-08-29 审查更正：上表是第一轮实现记录，不再单独作为最终可合入依据。
> 严格审查发现的主回合认领、持久 `takeover_required`、exactly-once callback、
> fencing fail-closed、稳定进展、隐藏 timeout 参数和 GUI 快照覆盖等问题，统一由
> `docs/superpowers/plans/2026-08-28-agent-loop-hardening-review-remediation.md`
> 追踪。最终结论必须以该修复计划 Task 9 的新鲜全量验证结果为准。

### Interruption Handoff Protocol

额度或时间不足时，当前执行者必须执行以下只读动作后停止：

1. 更新上表：已验证通过的 Task 标 `completed`，正在处理但未验证通过的 Task 标 `in_progress`，其余保持 `pending`。
2. 在对应 Task 的 `Implementation Notes` 下追加实际修改文件、最后一个失败测试、最后一个通过测试和未解决错误；不得只写“还没完成”。
3. 下一位执行者先完整阅读本计划和现有 spec，再检查工作树；保留已有成果，从第一个非 `completed` Task 的未勾选步骤继续。
4. 不因接手而重跑全部测试；先运行当前 Task 的定向测试，Task 10 才运行全量矩阵。
5. 不得因为前一执行者未提交而回滚其修改；以工作树和 Execution Record 共同判断现场。

### Test Helper Rule

- 优先复用现有 `tests/test_tools.py` 中的 `isolated_task_manager`、`_py_cmd`、`_wait_until`，不要另建功能重复的 fixture。
- 本计划代码块中出现的新 helper 必须在同一个测试文件、同一个 Task 内落为最小可读实现；不得把未定义 helper 留给下一位执行者猜测。
- 测试必须断言生产状态或生产 API；helper 只负责搭建数据，不能复制被测实现或直接伪造最终断言结果。

---

### Task 1: 后台任务终态持久化、墓碑与容量上限

**Files:**
- Modify: `core/task_manager.py:21-60, 110-135, 270-312, 449-625`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `_record_removed_locked(task_id: str) -> None`
- Produces: `_transition_terminal_locked(task_id, info, status, exit_code) -> bool`
- Preserves: `list_active_tasks() -> list[dict]` 的既有 GUI/CLI 结构。
- Consumed by: Task 2 的实时指纹、Task 5 的终态一致性。

- [x] **Step 1: 写自然终态磁盘复活的失败测试**

```python
def test_natural_terminal_task_is_not_merged_back_into_tasks_file(
    isolated_task_manager, tmp_path
):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("pass"))
    assert _wait_until(lambda: tm.get_task_snapshot(tid).terminal, timeout=10)
    tm._save_tasks()
    saved = json.loads(Path(tm.TASK_DB_PATH).read_text(encoding="utf-8"))
    assert tid not in saved
```

- [x] **Step 2: 写 64 条终态容量的失败测试**

```python
def test_snapshot_api_evicts_terminal_records_at_cap(isolated_task_manager):
    tm = isolated_task_manager
    for index in range(tm._TERMINAL_KEEP_CAP + 1):
        tid = f"term{index:04d}"
        tm.ACTIVE_TASKS[tid] = {
            "id": tid,
            "command": "finished",
            "pid": None,
            "process": None,
            "start_time": index,
            "status": tm.TASK_STATUS_COMPLETED,
            "exit_code": 0,
            "revision": 1,
            "last_log_size": 0,
        }
    snapshots = tm.list_task_snapshots()
    assert len([snap for snap in snapshots if snap.terminal]) == tm._TERMINAL_KEEP_CAP
    assert "term0000" not in tm.ACTIVE_TASKS
```

- [x] **Step 3: 写墓碑容量与 watcher 异常测试**

```python
def test_removed_tombstones_drop_oldest_instead_of_clearing_all(isolated_task_manager):
    tm = isolated_task_manager
    for index in range(tm._REMOVED_TIDS_CAP + 1):
        tm._record_removed_locked(f"task{index:04d}")
    assert len(tm._REMOVED_TIDS) == tm._REMOVED_TIDS_CAP
    assert "task0000" not in tm._REMOVED_TIDS
    assert f"task{tm._REMOVED_TIDS_CAP:04d}" in tm._REMOVED_TIDS


def test_watcher_poll_error_marks_task_failed_and_returns(isolated_task_manager):
    tm = isolated_task_manager
    class PollRaisesProcess:
        def poll(self):
            raise RuntimeError("poll failed")

    process = PollRaisesProcess()
    tm.ACTIVE_TASKS["pollerr1"] = {
        "id": "pollerr1",
        "command": "broken poll",
        "pid": None,
        "process": process,
        "start_time": time.time(),
        "status": tm.TASK_STATUS_RUNNING,
        "exit_code": None,
        "revision": 0,
        "last_log_size": 0,
    }
    tm._watch_task_completion("pollerr1", process, "cmd", "session")
    snap = tm.get_task_snapshot("pollerr1")
    assert snap.status == tm.TASK_STATUS_FAILED
    assert snap.terminal is True
```

- [x] **Step 4: 运行定向测试并确认失败**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py -k "terminal_task_is_not_merged or evicts_terminal or tombstones_drop_oldest or watcher_poll_error" -q
```

Expected: 新测试 FAIL，分别暴露磁盘复活、容量未执行、墓碑整体清空和 poll 异常早退。

- [x] **Step 5: 最小实现统一终态转换**

实现要求：

```python
def _transition_terminal_locked(task_id, info, status, exit_code):
    if info.get("status") in _TERMINAL_STATUSES:
        return False
    info["status"] = status
    info["exit_code"] = exit_code
    _bump_revision_locked(info)
    _evict_terminal_locked()
    return True
```

`_save_tasks()` 的跨进程合并必须排除当前进程已知 ID：

```python
known_ids = set(ACTIVE_TASKS)
for tid, info in disk_data.items():
    if (
        tid not in data
        and tid not in known_ids
        and tid not in _REMOVED_TIDS
        and isinstance(info, dict)
    ):
        data[tid] = info
```

墓碑使用“集合 + FIFO 队列”或等价有界结构，只淘汰最旧项，禁止达到 512 后整体清空。

- [x] **Step 6: 运行 Task 1 测试**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py -k "background_task or terminal or tombstone or watcher" -q
```

Expected: PASS；后台进程测试结束后无遗留子进程。

- [x] **Step 7: 更新 Execution Record；提交保持未授权**

若用户授权：

```powershell
git add core/task_manager.py tests/test_tools.py docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md
git commit -m "fix(tasks): 修复终态持久化与容量淘汰"
```

未授权时不提交，将 Task 1 的 Commit 写为 `not-authorized`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/task_manager.py`、`tests/test_tools.py`。原始 RED：4 failed（磁盘复活、65 条未淘汰、缺少墓碑 API、poll 异常外泄）；GREEN：4 passed。首轮审查发现 poll 异常回调被 `None → 0` 误报成功，以及第二次 poll 异常可令 watcher 永久循环；Fix round 1 新增 2 个 RED 后修复为 callback `-1`、同拍退出和旧记录身份检查。最终命令 `venv\Scripts\python.exe -m pytest tests/test_tools.py -k "background_task or terminal or tombstone or watcher" -q`：23 passed，143 deselected。Scoped re-review：3/3 findings addressed，no new breakage。未执行 Git commit。

---

### Task 2: `manage_task(wait)` 严格参数校验与实时任务指纹

**Files:**
- Modify: `core/tools.py:1507-1605`
- Modify: `core/task_manager.py:510-572`
- Verify: `core/tool_registry.py:375-395`
- Verify: `core/harness/tool_adapters.py:652-675`
- Test: `tests/test_tools.py`
- Test: `tests/test_tool_registry.py`

**Interfaces:**
- Produces: `_normalize_wait_timeout(value) -> int | str`，成功返回整数，失败返回 `🚨` 文本。
- Preserves: `manage_task(wait)` 成功 JSON 结构和 `changed` 语义。
- Consumed by: ProgressGuard 的 `background_tasks_fingerprint()`。

- [x] **Step 1: 写 timeout 严格校验失败测试**

```python
@pytest.mark.parametrize("value", [0, 61, "abc", True, 1.5])
def test_manage_task_wait_rejects_invalid_timeout(isolated_task_manager, value):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        result = manage_task("wait", task_id=tid, after_revision=0, timeout_seconds=value)
        assert result.startswith("🚨")
    finally:
        tm.kill_background_task(tid)


@pytest.mark.parametrize("value", [-1, "abc", True, 1.5])
def test_manage_task_wait_rejects_invalid_after_revision(
    isolated_task_manager, value
):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        result = manage_task(
            "wait", task_id=tid, after_revision=value, timeout_seconds=1
        )
        assert result.startswith("🚨")
    finally:
        tm.kill_background_task(tid)
```

- [x] **Step 2: 写恢复任务指纹同步测试**

```python
def test_background_fingerprint_syncs_restored_dead_task(isolated_task_manager):
    tm = isolated_task_manager
    tm.ACTIVE_TASKS["restored1"] = {
        "id": "restored1",
        "command": "restored",
        "pid": 99999999,
        "process": None,
        "start_time": time.time(),
        "create_time": None,
        "status": tm.TASK_STATUS_RUNNING,
        "exit_code": None,
        "revision": 0,
        "last_log_size": 0,
    }
    before = tm.background_tasks_fingerprint()
    snap = tm.get_task_snapshot("restored1")
    after = tm.background_tasks_fingerprint()
    assert snap.terminal is True
    assert before == after
```

测试含义：首次 fingerprint 自身就完成真实状态同步，所以随后显式 snapshot 不再改变指纹。

- [x] **Step 3: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py -k "invalid_timeout or fingerprint_syncs_restored" -q
```

- [x] **Step 4: 实现公共入口严格校验**

```python
def _normalize_wait_timeout(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return "🚨 错误：timeout_seconds 必须是 1 到 60 的整数。"
    if not 1 <= value <= 60:
        return "🚨 错误：timeout_seconds 必须是 1 到 60 的整数。"
    return value
```

`wait_task_change()` 移除 `max/min` 静默夹取，只接受已验证整数；`background_tasks_fingerprint()` 在同一条件锁内执行 `_ensure_loaded()` 后同步每个任务并执行终态淘汰。

保持 `manage_task(kill)` 既有授权、返回文案和取消行为零变化；在现有 kill 测试上加回归断言，不为复用 wait 校验器而改动 kill 分支。

- [x] **Step 5: 运行 Task 2 测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_registry.py -k "manage_task or wait or fingerprint" -q
```

- [x] **Step 6: 更新 Execution Record；提交保持未授权**

建议提交信息：`fix(tasks): 严格校验 wait 参数并同步任务指纹`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/tools.py`、`core/task_manager.py`、`tests/test_tools.py`。RED 明确复现 timeout=0 被静默夹取及恢复任务首次指纹不同步；GREEN：严格拒绝 bool/float/string/越界，内部 wait 移除转换与夹取，fingerprint 在同一条件锁内 load/sync/evict/save 后构造状态。最终命令 `venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_registry.py -k "manage_task or wait or fingerprint" -q`：25 passed，168 deselected。审查 Approved。Minor：报告的定向 `-k "invalid_timeout or fingerprint_syncs_restored"` 未单独选择 after_revision 参数化测试，完整回归已覆盖；保留到最终审查复核。未执行 Git commit。

---

### Task 3: 修正 `attempts` 与普通 retry 语义

**Files:**
- Modify: `core/task_scheduler.py:193-220, 329-341`
- Modify: `core/plan.py:626-668`
- Modify: `core/tool_registry.py`（仅当增加显式 force 参数）
- Test: `tests/test_plan.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `TaskScheduler.retry(item_id: str) -> bool`，只重置状态，不增加 attempts。
- Produces: `plan_retry(item_id: str, force: bool = False) -> str`。
- Consumed by: Task 4 的 delegation/takeover 计数。

- [x] **Step 1: 写 attempts 精确计数失败测试**

```python
def test_retry_does_not_increment_attempt_until_next_start():
    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "retry counter"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("item") is True
    assert plan.snapshot()[0]["attempts"] == 1
    assert scheduler.fail("item", "boom") is True
    assert scheduler.retry("item") is True
    assert plan.snapshot()[0]["attempts"] == 1
    assert scheduler.start_item("item") is True
    assert plan.snapshot()[0]["attempts"] == 2
```

- [x] **Step 2: 写显式 force 语义测试**

```python
def test_plan_retry_after_two_attempts_requires_explicit_force(monkeypatch):
    plan = PlanManager()
    plan.replace([{
        "id": "item", "subject": "force retry", "status": "failed",
        "attempts": 2, "error": "same failure",
    }])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    assert "拒绝盲目重试" in plan_retry("item", force=False)
    assert plan.snapshot()[0]["status"] == "failed"
    assert "已重置为 pending" in plan_retry("item", force=True)
    assert plan.snapshot()[0]["status"] == "pending"
```

- [x] **Step 3: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_tools.py -k "retry_does_not_increment or explicit_force" -q
```

- [x] **Step 4: 最小实现**

`TaskScheduler.retry()` 删除 attempts 更新；`_start_item_locked()` 保留唯一的 `attempts + 1`。`plan_retry()` 删除“再次调用即可强制”的隐式两段式文案，改用明确 `force=True`，避免同参数重复调用形成协议状态。

- [x] **Step 5: 运行计划状态机回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_tool_pipeline.py tests/test_tools.py -k "plan or retry or attempts" -q
```

- [x] **Step 6: 更新 Execution Record 并询问提交授权**

建议提交信息：`fix(plan): 统一 attempts 与显式 retry 语义`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/task_scheduler.py`、`core/plan.py`、`core/tool_registry.py`、`tests/test_plan.py`。RED：`retry_does_not_increment`（retry 自增 attempts）与 `explicit_force`（plan_retry 无 force 参数、两段式隐式协议）均失败后实现：`TaskScheduler.retry()` 删除 attempts 更新、保留 `_start_item_locked()` 唯一自增；`plan_retry(item_id, force=False)` 删除“再次调用即可强制”文案，改为显式 `force=True`；`tool_registry.py` plan_retry schema 同步增加 force 字段。最终回归 `venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_tool_pipeline.py tests/test_tools.py -k "plan or retry or attempts" -q`：42 passed，207 deselected（基线 40 + 新增 2）。未执行 Git commit。

---

### Task 4: 计划项级接管状态与 FIFO 队列

**Files:**
- Modify: `core/plan.py`
- Modify: `core/task_scheduler.py`
- Test: `tests/test_plan.py`
- Test: `tests/test_session_parallel_isolation.py`

**Interfaces:**
- Adds plan fields:
  - `execution_mode: "" | "delegated" | "main_takeover" | "takeover_queued"`
  - `delegation_count: int`
  - `handoff_task_id: str`
  - `handoff_reason: str`
  - `takeover_turn_id: str`
  - `takeover_sequence: int`
- Produces: `TaskScheduler.handoff_to_main(item_id, source_task_id, parent_turn_id, reason) -> dict | None`
- Produces: `TaskScheduler.start_delegated_item(item_id, source_task_id, owner, event_callback) -> bool`
- Produces: `TaskScheduler.active_takeover(parent_turn_id) -> dict | None`
- Produces: `TaskScheduler.promote_next_takeover(parent_turn_id) -> dict | None`
- Produces: `PlanManager.transition_execution(...) -> dict | None`，在 PlanManager 自身锁内用 expected status/mode 做 CAS 并一次持久化全部 execution 字段。
- Consumed by: Tasks 5、6、7、9。

- [x] **Step 1: 写单项接管失败测试**

```python
def test_handoff_keeps_item_in_progress_and_same_attempt():
    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "delegated work"}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    ) is True
    item = scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "foreground timeout"
    )
    assert item["status"] == "in_progress"
    assert item["attempts"] == 1
    assert item["execution_mode"] == "main_takeover"
    assert item["owner"] == "main:turn-1"
    assert item["handoff_task_id"] == "subagent-abcd"
```

- [x] **Step 2: 写多超时 FIFO 测试**

```python
def test_multiple_handoffs_queue_and_promote_fifo():
    plan = PlanManager()
    plan.replace([{"id": item_id, "subject": item_id} for item_id in ("a", "b", "c")])
    scheduler = TaskScheduler(plan, max_workers=3)
    for item_id in ("a", "b", "c"):
        assert scheduler.start_delegated_item(
            item_id, f"s{item_id}", "subagent:implement", None
        ) is True
    scheduler.handoff_to_main("a", "sa", "turn-1", "timeout")
    scheduler.handoff_to_main("b", "sb", "turn-1", "timeout")
    scheduler.handoff_to_main("c", "sc", "turn-1", "timeout")
    assert scheduler.active_takeover("turn-1")["id"] == "a"
    items = {item["id"]: item for item in plan.snapshot()}
    assert items["b"]["execution_mode"] == "takeover_queued"
    assert items["c"]["execution_mode"] == "takeover_queued"
    scheduler.complete("a", "done")
    assert scheduler.promote_next_takeover("turn-1")["id"] == "b"
```

- [x] **Step 3: 写会话隔离测试**

两个 session 使用相同 item id 时，`active_takeover()` 必须只返回当前会话管理器中的项目，不得依赖进程级全局 active id。

- [x] **Step 4: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_session_parallel_isolation.py -k "handoff or takeover" -q
```

- [x] **Step 5: 实现规范化与调度方法**

`PlanManager._normalize_item()` 为新字段提供严格默认值；`start_delegated_item()` 在同一 scheduler 临界区内完成 attempts+1、delegation_count+1、execution_mode、owner 与 handoff_task_id 的绑定。`handoff_to_main()` 必须在 scheduler 锁内验证：项目存在、status 为 in_progress、execution_mode 为 delegated、source task id 匹配。所有多字段更新经 `PlanManager.transition_execution()` 一次提交，不得连续调用普通 `update()` 暴露半状态，也不得通过公共 `plan_update` 伪造 takeover 字段。

- [x] **Step 6: 将接管字段纳入计划持久化但限制进展指纹**

计划文件保存所有接管字段用于崩溃恢复；加载时把遗留的 `main_takeover` 降为 `takeover_queued` 并清空旧 `takeover_turn_id`，下一真实主回合通过 `promote_next_takeover(new_turn_id)` 重新认领，禁止让进程重启前的 turn id 永久占锁。普通 `plan_state_fingerprint()` 只纳入 `execution_mode` 和 active item id，不纳入动态 reason/task id，避免每个新 task id 重置停滞计数。

- [x] **Step 7: 运行 Task 4 回归并记录**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_session_parallel_isolation.py -q
```

- [x] **Step 8: 更新 Execution Record 并询问提交授权**

建议提交信息：`feat(plan): 新增计划项级主 Agent 接管状态`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/plan.py`、`core/task_scheduler.py`、`tests/test_plan.py`、`tests/test_session_parallel_isolation.py`。新增计划项执行租约字段（execution_mode/delegation_count/handoff_task_id/handoff_reason/takeover_turn_id/takeover_sequence，`_normalize_item` 严格默认值）；`PlanManager.transition_execution()` 在 PlanManager 自身锁内按 expected status/mode CAS 后一次提交全部租约字段；`TaskScheduler` 新增 `start_delegated_item`（同临界区绑定 attempts+1/delegation_count+1/execution_mode/owner/handoff_task_id）、`handoff_to_main`（锁内校验 in_progress+delegated+租约 id 匹配，已有 active 则入 FIFO 队列，否则 main_takeover+owner=main:<turn>）、`active_takeover`（按会话调度器隔离，只读自身 plan）、`promote_next_takeover`（FIFO 按 takeover_sequence，无 active 时提升并重绑新 turn）。崩溃恢复：load 时遗留 main_takeover 降级 takeover_queued 并清空旧 turn id；`plan_retry` 的 `TaskScheduler.retry()` 现在同时清空上一轮租约字段。`plan_state_fingerprint()` 只纳入 execution_mode，不含动态 task id/reason/turn id。RED：5 failed（缺少全部接管 API）；GREEN：定向 5 passed。最终回归 `venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_session_parallel_isolation.py -q`：53 passed。安全网 `tests/test_progress_guard.py tests/test_tool_pipeline.py`：52 passed。未执行 Git commit。

---

### Task 5: 子 Agent 超时原子移交与旧执行者封禁

**Files:**
- Modify: `core/tools.py:2439-2885, 3414-3648`
- Modify: `core/harness/tool_pipeline.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_tool_pipeline.py`
- Test: `tests/test_cancellation.py`

**Interfaces:**
- Produces timeout result JSON:

```json
{
  "status": "timed_out",
  "handoff": "main_takeover",
  "plan_item_id": "task-1",
  "task_id": "subagent-abcd1234",
  "inspection_required": true,
  "stage": "running",
  "execution_elapsed": 600.0,
  "error": "foreground timeout"
}
```

- Produces: `SubagentManager.is_execution_lease_active(task_id: str) -> bool`
- Produces: `SubagentManager.run(..., task_id: str | None = None)`；传入 ID 时在 manager 锁内拒绝碰撞，省略时保持现有自动生成行为。
- Consumed by: ToolPipeline 的副作用工具执行前 fencing。

- [x] **Step 1: 写“先取消、后 handoff”失败测试**

```python
def test_timeout_sets_cancel_before_handoff_callback():
    seen = {}
    def terminal_callback(task, status, _result, _error):
        seen["status"] = status
        seen["cancelled"] = task.cancel_event.is_set()
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0]),
        wait_interval=0.001,
    )
    manager.run(
        "slow", "s", "t", timeout_seconds=30,
        terminal_callback=terminal_callback,
    )
    assert seen == {"status": "timed_out", "cancelled": True}
```

- [x] **Step 2: 写计划项首次超时移交测试**

由 `run_subagent` 先生成真实 task id，再调用 `start_delegated_item()`，最后把同一 ID 传给 `SubagentManager.run()`。断言 timeout 后项目仍为 `in_progress/main_takeover`、attempts 不变、计划项 `handoff_task_id` 与结果 JSON 的 `task_id` 完全一致；不得断言 `failed`。

- [x] **Step 3: 写同项禁止重新派发测试**

```python
def test_run_subagent_rejects_redelegation_after_timeout_takeover():
    result = run_subagent(
        "retry same work", parent_session="s", parent_turn_id="t",
        plan_item_id="item", subagent_type="implement"
    )
    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "main_takeover_required"
```

- [x] **Step 4: 写旧子 Agent 延迟工具调用 fencing 测试**

超时后构造 `permission_owner=旧 task_id` 的 `write_file`/`edit_file` 调用，断言 ToolPipeline 在权限询问和 adapter 之前返回取消错误且底层 adapter 未执行。`permission_owner` 已由子 Agent LLM 调用链透传，除非定向测试证明丢失，否则不得额外修改 Provider 请求结构。查询工具同样返回取消错误，不得继续访问外部状态。

- [x] **Step 5: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_pipeline.py tests/test_cancellation.py -k "handoff or redelegation or execution_lease" -q
```

- [x] **Step 6: 重排 `_terminate` 的 timed_out/cancelled 顺序**

必须按以下顺序执行；`done_event` 是前台结果可见屏障，绝不能早于 handoff callback：

```text
锁内认领终态 → 写 status/error/finished_at → set cancel_event → 记录 timed_out
→ 取消 permission owner → abort inflight stream → 释放并发槽
→ 调用计划 terminal callback 执行 handoff
→ set done_event → 推送终态通知
```

callback 失败时保留 `timed_out` 事实并在结果中返回 `handoff=failed`，不得把它改写成普通 failed 后重新委派。

completed/failed 的既有验证回调语义保持不变。用户 cancelled 继续走 scheduler.cancel，不触发 handoff。

- [x] **Step 7: 修改 `run_subagent` terminal callback**

```python
if status == "timed_out":
    return scheduler.handoff_to_main(
        item_id, task.task_id, parent_turn_id, error or "subagent timeout"
    )
if status == "cancelled":
    scheduler.cancel(item_id, error or "子代理已取消")
else:
    scheduler.fail(item_id, error or f"子代理状态: {status}")
```

- [x] **Step 8: 运行 Task 5 回归并记录**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_pipeline.py tests/test_cancellation.py -q
```

- [x] **Step 9: 更新 Execution Record 并询问提交授权**

建议提交信息：`feat(agent): 子 Agent 超时后原子移交主 Agent`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/tools.py`、`core/harness/tool_pipeline.py`、`tests/test_tools.py`、`tests/test_tool_pipeline.py`、`tests/test_task_scheduler.py`。实现：`SubagentTask` 新增 handoff 字段；`SubagentManager.run(task_id=)` 支持调用方预生成租约 id 并在 manager 锁内拒绝碰撞；`_terminate` 重排为“锁内认领终态+写 status/error+set cancel_event → cancel_owner → abort inflight → 释放槽 → 计划回调(handoff) → set done_event → 通知”，callback 失败只追加错误详情、保留 timed_out 事实；`_foreground_result` 对 timed_out+handoff 返回计划规定 JSON（status/handoff/plan_item_id/task_id/inspection_required/stage/execution_elapsed/error）；新增 `is_execution_lease_active`；`run_subagent` 预生成真实 task_id、改用 `start_delegated_item` 绑定租约、对 main_takeover/takeover_queued 计划项返回 blocked+reason=main_takeover_required、terminal callback 对 timed_out 调用 `handoff_to_main`（成功写 handoff=main_takeover/takeover_queued，失败 handoff=failed）；ToolPipeline 在权限询问与 adapter 之前按 `permission_owner` 前缀 `subagent-` + 租约失效做 fencing（STALE_EXECUTION_LEASE_TEXT），查询工具同样拦截。RED：5 个新测试失败（回调先于取消、重新派发未拒、无 fencing、超时结果非 JSON）；GREEN：定向 6 passed。回归 `venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_pipeline.py tests/test_cancellation.py -q`：227 passed。附带：`tests/test_task_scheduler.py::test_scheduler_marks_failure_and_retry_requeues_item` 按新 attempts 语义更新断言（retry 不再立即自增）后 22 passed。安全网中 `tests/test_llm_events_fixes.py` 两个失败（pywintypes DLL 加载 / plan 模式询问断言）经基线工作树（bdebe13）对照确认为预先存在的环境/用例问题，与本次改动无关。未执行 Git commit。

---

### Task 6: 主 Agent scoped 执行权与显式 `plan_finish`

**Files:**
- Modify: `core/harness/tool_pipeline.py:610-660`
- Modify: `core/tools.py`
- Modify: `core/tool_registry.py`
- Modify: `core/harness/tool_adapters.py`
- Modify: `core/plan.py`
- Modify: `core/task_scheduler.py`
- Test: `tests/test_tool_pipeline.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_plan.py`

**Interfaces:**
- Produces: `plan_finish(item_id: str, result: str, ...) -> str`
- Tool schema:

```json
{
  "name": "plan_finish",
  "parameters": {
    "type": "object",
    "properties": {
      "item_id": {"type": "string"},
      "result": {"type": "string"}
    },
    "required": ["item_id", "result"]
  }
}
```

- Consumes: `TaskScheduler.active_takeover(parent_turn_id)`。
- Produces: active 项完成后自动 `promote_next_takeover()`。

- [x] **Step 1: 写 scoped 门禁失败测试**

```python
def test_main_takeover_allows_direct_mutation_for_current_turn(monkeypatch, tmp_path):
    _install_active_takeover(monkeypatch, item="a", turn="turn-1")
    result = _call(
        "write_file", {"file_path": str(tmp_path / "a.py"), "content": "x"},
        parent_session="session-1", parent_turn_id="turn-1",
    )
    assert "必须通过 run_subagent" not in result["content"]


def test_takeover_does_not_unlock_other_turn_or_whole_plan(monkeypatch, tmp_path):
    _install_active_takeover(monkeypatch, item="a", turn="turn-1")
    blocked = _call(
        "write_file", {"file_path": str(tmp_path / "b.py"), "content": "x"},
        parent_session="session-1", parent_turn_id="turn-2",
    )
    assert "必须通过 run_subagent" in blocked["content"]
```

在 `tests/test_tool_pipeline.py` 同段增加以下最小 helper；它只通过生产调度 API 搭建状态：

```python
def _install_active_takeover(monkeypatch, *, item, turn):
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": item, "subject": item}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_delegated_item(
        item, f"subagent-{item}", "subagent:implement", None
    )
    assert scheduler.handoff_to_main(
        item, f"subagent-{item}", turn, "timeout"
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)
    monkeypatch.setattr("core.task_scheduler.get_default_scheduler", lambda: scheduler)
    return scheduler
```

两个写文件测试显式把权限模式 monkeypatch 为自动编辑，避免权限对话遮蔽计划门禁断言；另保留至少一个 `confirm` 模式测试，证明 scoped 放行后权限系统仍然会正常询问。

若计划项声明 `files`，写文件只允许命中这些路径；未声明 files 时仍受工作区、权限模式和沙盒约束，不获得额外路径权限。

- [x] **Step 2: 写 `plan_finish` 验证测试**

覆盖：active takeover 可完成；非 active、错误 turn、queued 项均拒绝；verification command 非零时项目进入 failed 且不重新委派；成功后 FIFO 提升下一项。

- [x] **Step 3: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_plan.py -k "main_takeover or plan_finish" -q
```

- [x] **Step 4: 实现 `_plan_constraint` scoped 放行**

只在以下条件全部成立时放行主 Agent 修改类工具：

```python
takeover = scheduler.active_takeover(context.parent_turn_id)
allow_direct = (
    takeover is not None
    and takeover["status"] == "in_progress"
    and takeover["execution_mode"] == "main_takeover"
    and takeover["takeover_turn_id"] == context.parent_turn_id
)
```

不得沿用“所有 stalled 项 attempts>=2 后全局放行”的旧分支；该分支应删除或限制为只读诊断。

- [x] **Step 5: 实现 `plan_finish`**

在 `ToolAdapterSet` 增加专用 `execute_plan_finish()`，由 adapter 注入 `parent_turn_id`、`permission_owner`、`event_callback` 和 `cancel_event`；这些运行上下文字段不得暴露给模型 schema。抽取现有子 Agent completed 路径中的授权验证 runner，供 `run_subagent` 和 `plan_finish` 共用；不得复制两套验证逻辑。验证通过调用 scheduler.complete，失败调用 scheduler.fail，然后提升下一 queued takeover。

- [x] **Step 6: 运行 Task 6 回归并记录**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_plan.py tests/test_llm_events.py -q
```

- [x] **Step 7: 更新 Execution Record 并询问提交授权**

建议提交信息：`feat(plan): 主 Agent scoped 接管执行与显式收尾`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/harness/tool_pipeline.py`、`core/tools.py`、`core/tool_registry.py`、`core/harness/tool_adapters.py`、`core/plan.py`、`core/llm.py`、`tests/test_tool_pipeline.py`、`tests/test_tools.py`、`tests/test_plan.py`、`tests/test_llm_events.py`。实现：`_plan_constraint` 删除旧“stalled 项 attempts>=2 全局放行”分支，改为 `_active_takeover_item(context)` 经生产调度器 `active_takeover(parent_turn_id)` 做 scoped 放行（仅 active 项 + in_progress + main_takeover + takeover_turn_id 匹配当前回合）；接管项声明 files 时 write_file/edit_file 仅允许命中声明路径（`_takeover_files_violation`），未声明不获得额外路径权限；新增 `plan_finish`（core/plan.py，与 plan_write/plan_retry 同层）：校验 active 接管租约后经 scheduler.complete 完成或验证失败 failed，均自动 `promote_next_takeover`；`ToolAdapterSet.execute_plan_finish` 注入 parent_turn_id/permission_owner/event_callback/cancel_event（schema 只暴露 item_id/result，测试断言之）；抽取 `core/tools.py:make_authorized_verification_runner` 供 run_subagent 与 plan_finish 共用（未复制两套验证逻辑）；plan_finish 进入 `_PLAN_ROOT_CONTROL_TOOLS`、`_REPEAT_GUARD_EXEMPT_TOOLS` 与 llm.py `_PLAN_INTERNAL_PERMISSION_TOOLS`（文件清单漏列 llm.py，为权限分类必需的最小补充）。confirm 模式测试证明 scoped 放行后权限询问照常发生。RED：9 failed；GREEN：定向 12 passed。回归 `venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_plan.py tests/test_llm_events.py -q`：321 passed。附带更新：`test_pending_plan_rejects_root_tool_with_exact_message`（门禁文案去掉 attempts>=2 后缀）、`test_stalled_plan_after_repeated_failures_allows_direct_fix` 文档字符串改为只读诊断语义（该测试走 QUERY 豁免仍通过）。未执行 Git commit。

---

### Task 7: ProgressGuard 接管适配与伪进展去噪

**Files:**
- Modify: `core/harness/progress_guard.py`
- Modify: `core/llm.py:3396-3449, 4236-4958`
- Modify: `core/plan.py:484-541`
- Test: `tests/test_progress_guard.py`
- Test: `tests/test_harness_runtime.py`
- Test: `tests/test_llm_events.py`

**Interfaces:**
- Extend `ProgressObservation`:

```python
@dataclass(frozen=True)
class ProgressObservation:
    task_snapshots_sha256: str
    plan_snapshot_sha256: str
    tool_results_sha256: str
    mutation_committed: bool
    takeover_active: bool = False
    takeover_read_signatures: tuple[str, ...] = ()
```

- `TurnProgressGuard` 新增 `MAX_TAKEOVER_UNIQUE_READS = 12` 和 turn-local `seen_takeover_reads`。

- [x] **Step 1: 写动态子 Agent结果不算进展测试**

```python
def test_different_subagent_task_ids_do_not_reset_stall():
    first = _step_progress_observation([
        _progress_record("c1", "run_subagent", json.dumps({
            "status": "timed_out", "handoff": "main_takeover",
            "task_id": "subagent-a", "plan_item_id": "item",
        }))
    ])
    second = _step_progress_observation([
        _progress_record("c2", "run_subagent", json.dumps({
            "status": "timed_out", "handoff": "main_takeover",
            "task_id": "subagent-b", "plan_item_id": "item",
        }))
    ])
    assert first.tool_results_sha256 == second.tool_results_sha256
```

- [x] **Step 2: 写接管核查只读预算测试**

```python
def test_takeover_unique_reads_are_bounded_progress():
    from hashlib import sha256

    guard = TurnProgressGuard()
    for index in range(8):
        obs = _obs(
            takeover_active=True,
            takeover_read_signatures=(
                sha256(f"read_file:file{index}.py".encode()).hexdigest(),
            ),
        )
        assert guard.observe(obs) is True
    repeated = _obs(
        takeover_active=True,
        takeover_read_signatures=(
            sha256("read_file:file0.py".encode()).hexdigest(),
        ),
    )
    assert guard.observe(repeated) is False
```

新增测试还要证明：第 13 个新读取目标不再自动重置停滞计数，避免模型通过无限换路径绕过熔断。

- [x] **Step 3: 写真实超时重派 E2E**

Provider 第一次调用 `run_subagent` 返回 timed_out/handoff；第二轮若再次请求同 item，工具层必须 blocked 且底层 subagent manager 调用次数仍为 1。随后主 Agent direct read/write/plan_finish 完成，Turn 正常结束。

- [x] **Step 4: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_progress_guard.py tests/test_harness_runtime.py tests/test_llm_events.py -k "subagent_task_ids or takeover_unique_reads or timeout_handoff" -q
```

- [x] **Step 5: 实现动态字段去噪**

`run_subagent`、`plan_retry`、`plan_read` 不使用完整正文作为结果进展；只依据计划状态的实质转换和 mutation。计划指纹不得因 handoff task id、elapsed、error 文案或 attempts 单独变化而认定业务成果。

- [x] **Step 6: 实现 takeover 有界读取签名**

读取签名使用 `tool name + canonical arguments` 的 SHA-256，仅保存在当前 Turn 内存，不写 Journal。成功读取新目标在接管核查期可算进展；重复签名和超过 12 个的新签名不算。

- [x] **Step 7: 运行 ProgressGuard 全回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_progress_guard.py tests/test_harness_runtime.py tests/test_llm_events.py tests/test_plan.py -q
```

Expected: 原有“第 7 次 Provider 请求不得发送”仍保持 `request_count == 6`。

- [x] **Step 8: 更新 Execution Record 并询问提交授权**

建议提交信息：`fix(harness): 识别接管进展并屏蔽子 Agent 动态伪进展`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/harness/progress_guard.py`、`core/llm.py`、`core/plan.py`、`tests/test_progress_guard.py`、`tests/test_harness_runtime.py`、`tests/test_llm_events.py`。实现：`ProgressObservation` 扩展 `takeover_active`/`takeover_read_signatures` 字段（默认值保持旧构造兼容）；`TurnProgressGuard` 新增 `MAX_TAKEOVER_UNIQUE_READS=12` 与 turn-local `seen_takeover_reads`——接管核查期首次出现的新读取签名计为进展，重复签名与超限后的新签名一律不算；`_step_progress_observation` 把 `run_subagent`/`plan_retry`/`plan_read` 的结果正文移出进展哈希（动态 task_id/耗时/文案不再重置停滞计数，计划状态实质转换由计划指纹单独捕获），并为成功执行的只读工具（`_PLAN_READ_ONLY_TOOLS`）计算 `tool name + canonical arguments` 的 SHA-256 读取签名（仅存 Turn 内存，不写 Journal）；新增 `core/plan.py:plan_session_takeover_active`（注册表 peek，不惰性创建）判定接管核查期。E2E（`tests/test_harness_runtime.py::test_timeout_handoff_takeover_completes_without_redelegation`）用假时钟 SubagentManager 验证完整链路：首轮 run_subagent 超时移交、第二轮同项重派 blocked（manager 任务数恒为 1）、主 Agent direct write + plan_finish 完成后 Turn completed。RED：5 failed（guard 无预算字段、动态 task_id 重置停滞）；E2E 因 Task 4-6 实现先行通过，作为链路回归保留。GREEN：定向 6 passed。全回归 `venv\Scripts\python.exe -m pytest tests/test_progress_guard.py tests/test_harness_runtime.py tests/test_llm_events.py tests/test_plan.py -q`：117 passed，原有"第 7 次 Provider 请求不得发送"（request_count == 6）保持通过。未执行 Git commit。

---

### Task 8: 子 Agent 时间预算与上下文上限

**Files:**
- Modify: `core/tool_registry.py`（模型可见 schema）
- Modify: `core/tools.py:3403-3457`
- Test: `tests/test_tool_registry.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Internal defaults remain:
  - explore: 240s
  - review: 300s
  - test: 480s
  - implement: 600s
  - general: 600s
- Model-visible `run_subagent` schema no longer exposes `timeout_seconds`。
- Internal Python callers may still pass `timeout_seconds` for tests and system-owned workflows，hard maximum 600s for foreground tasks。
- `task_description` maximum: 6000 characters；超长输入返回明确失败，不截断、不静默改写。

- [x] **Step 1: 写 schema 与长度失败测试**

```python
def test_run_subagent_schema_does_not_expose_timeout_seconds():
    schema = (
        build_default_registry().get("run_subagent")
        .api_schema()["function"]["parameters"]
    )
    assert "timeout_seconds" not in schema["properties"]


def test_run_subagent_rejects_oversized_description(monkeypatch):
    result = run_subagent("x" * 6001, parent_session="s", parent_turn_id="t")
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "6000" in payload["error"]
```

- [x] **Step 2: 写前台 600 秒硬上限测试**

内部调用传 601 秒时返回参数错误；后台模式如确有长任务，仍优先要求子 Agent 启动真实后台进程并使用 `manage_task(wait)`，而不是延长子 Agent 本身生命周期。

- [x] **Step 3: 运行并确认失败**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_tools.py -k "subagent_schema or oversized_description or hard_timeout" -q
```

- [x] **Step 4: 最小实现并运行回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_tools.py -k "subagent or timeout" -q
```

- [x] **Step 5: 更新 Execution Record 并询问提交授权**

建议提交信息：`fix(agent): 限制子 Agent 时间预算与任务描述体积`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/tool_registry.py`、`core/tools.py`、`tests/test_tool_registry.py`、`tests/test_tools.py`。实现：模型可见 `run_subagent` schema 移除 `timeout_seconds`（内部默认值保持 explore 240 / review 300 / test 480 / implement、general 600 不变）；`run_subagent` 显式传入超时新增前台硬上限 600 秒（601 秒返回明确参数错误并提示 run_in_background + manage_task(wait)，内部调用同样受限；后台模式保留 3600 秒）；`task_description` 超 6000 字符明确失败（错误含上限与当前长度，不截断、不改写）。RED：3 failed（schema 仍暴露 timeout_seconds、超长描述静默通过、601 秒未拦截）；GREEN：定向 4 passed。回归 `venv\Scripts\python.exe -m pytest tests/test_tool_registry.py tests/test_tools.py -k "subagent or timeout" -q`：55 passed。附带更新两个编码旧行为的既有测试：`test_subagent_schema_defaults_to_foreground_with_bounded_timeout` → 断言 schema 隐藏 timeout_seconds；`test_run_subagent_resolves_per_role_default_timeout` 尾部显式值断言 3600 → 600/601（前台硬上限）。未执行 Git commit。

---

### Task 9: GUI/CLI 接管状态可见性

**Files:**
- Modify: `gui_bridge.py:3824-3863, 3978-3983`
- Modify: `main.py:500-535, 1061-1085, 1948-2002`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/stores/sessionRuntime.ts`
- Modify: `frontend/src/components/RightPanel.vue`
- Modify: `frontend/src/components/ToolExecutionRow.vue`
- Test: `frontend/src/stores/appStore.plan.test.ts`
- Test: `frontend/src/stores/appStore.toolEvents.test.ts`
- Test: CLI 相关 Python 测试文件中的 plan/subagent event 用例。

**Interfaces:**
- New event: `plan_item_handoff`，payload 含 `item`、`source_task_id`、`mode`、`queue_position`。
- GUI 文案固定：`子 Agent 已超时，主 Agent 正在接管并核查现场`。
- queued 文案固定：`等待主 Agent 接管（队列第 N 项）`。

- [x] **Step 1: 写 store 事件失败测试**

断言 `plan_item_handoff` 更新正确会话的计划项 execution_mode/owner，不能污染并行会话。

- [x] **Step 2: 写 CLI 事件输出失败测试**

CLI 收到 active handoff 时打印一次接管提示；重复 live update 不刷屏；queued 项显示队列位置。

- [x] **Step 3: 运行并确认失败**

```powershell
npm --prefix frontend test -- src/stores/appStore.plan.test.ts src/stores/appStore.toolEvents.test.ts
venv\Scripts\python.exe -m pytest tests/test_llm_events.py -k "handoff" -q
```

- [x] **Step 4: 实现事件透传与展示**

GUI 只展示状态，不改变调度状态；真实状态唯一来源仍是 PlanManager/TaskScheduler。接管后旧子 Agent 卡片保持 timed_out，计划卡片显示 main_takeover，两者不得合并成 completed。

- [x] **Step 5: 运行 GUI/CLI 定向回归**

```powershell
npm --prefix frontend test -- src/stores/appStore.plan.test.ts src/stores/appStore.toolEvents.test.ts
venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_session_parallel_isolation.py -q
```

- [x] **Step 6: 更新 Execution Record 并询问提交授权**

建议提交信息：`feat(gui): 展示子 Agent 超时与主 Agent 接管状态`。

**Implementation Notes:** 2026-08-28 完成。修改 `core/tools.py`、`gui_bridge.py`、`main.py`、`frontend/src/types/pywebview.d.ts`、`frontend/src/stores/appStore.ts`、`frontend/src/components/RightPanel.vue`、`frontend/src/components/ToolExecutionRow.vue`、`tests/test_main_ui_events.py`、`frontend/src/stores/appStore.plan.test.ts`。实现：`plan_item_handoff` 事件由 run_subagent terminal_callback 在 handoff 成功后发射（payload：item/source_task_id/mode/queue_position，queued 项按 takeover_sequence 计算队列位置）；gui_bridge 按会话透传；CLI `_render_ui_event` 新增 plan_item_handoff 分支（active 固定文案“子 Agent 已超时，主 Agent 正在接管并核查现场”，queued 固定文案“等待主 Agent 接管（队列第 N 项）”，同 item 同 mode 去重不刷屏；`_last_printed_plan_state` 的 global 声明提升到函数顶部以避免双声明语法错误）；store 新增 plan_item_handoff 分支按会话更新计划项 execution_mode/owner/handoff_note（reducer 对非当前会话事件处理后自动恢复当前视图，s2 状态经 sessionRuntimeByName 断言）；RightPanel 计划卡片显示 handoff_note；ToolExecutionRow 对 timed_out+handoff 结果显示固定徽标（“主 Agent 已接管”/“等待主 Agent 接管”），旧子 Agent 卡片保持 timed_out 不合并 completed。RED：CLI 1 failed + store 2 failed；GREEN：store 4 passed、CLI 2 passed。回归：`npm --prefix frontend test -- src/stores/appStore.plan.test.ts src/stores/appStore.toolEvents.test.ts` 22 passed；`venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_session_parallel_isolation.py tests/test_main_ui_events.py -q` 124 passed；全量前端 `npm --prefix frontend test` 374 passed / 1 failed（App.guiTheme.test.ts 窗口最大化句柄用例，属用户已有 GUI 主题改动范围，与本计划正交，保持不动）；`npm --prefix frontend run build` 构建通过。未执行 Git commit。

---

### Task 10: 全量回归、文档同步与最终交接

**Files:**
- Modify: `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`（追加 follow-up 引用，不重写已发布设计）
- Modify: `docs/superpowers/plans/2026-08-28-agent-loop-hardening-and-subagent-timeout-takeover.md`
- Verify all files from Tasks 1-9。

**Interfaces:**
- Produces: 可供 GUI/CLI 共用的最终 timeout takeover 行为。
- Produces: 完整 Execution Record 与可复现验证结果。

- [x] **Step 1: 运行 Python 定向功能矩阵**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_tool_registry.py tests/test_progress_guard.py -q
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_harness_journal.py tests/test_harness_message_commit.py tests/test_llm_events.py tests/test_plan.py -q
venv\Scripts\python.exe -m pytest tests/test_cancellation.py tests/test_agent_turn_e2e.py tests/test_session_parallel_isolation.py -q
```

- [x] **Step 2: 运行 Provider 与缓存不变式回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py -q
```

- [x] **Step 3: 运行前端测试与构建**

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

- [x] **Step 4: 运行语法检查**

```powershell
venv\Scripts\python.exe -m py_compile core/task_manager.py core/tools.py core/tool_registry.py core/task_scheduler.py core/plan.py core/llm.py core/harness/tool_pipeline.py core/harness/progress_guard.py
```

- [x] **Step 5: 人工复现原两小时故障链**

使用测试模型或可控 fake runner 让计划项子 Agent 首次超时，确认：

1. 同一计划项只创建一个 subagent task id。
2. timeout 后 UI/CLI 显示主 Agent接管。
3. 计划项保持 in_progress、attempts 不增加。
4. 主 Agent先执行只读现场核查，再执行修改。
5. 再次 `run_subagent(plan_item_id=同一项)` 被拒绝。
6. `plan_finish` 成功后计划项 completed；验证失败则 failed，不自动重新委派。
7. Harness Turn 不出现 30/40 分钟重复等待链。

- [x] **Step 6: 更新设计文档引用与本计划最终记录**

在旧 spec 末尾追加本计划路径和“超时循环属于独立故障类型”的说明；不要改写旧 spec 的历史问题陈述。Execution Record 必须填入每组测试真实最后一行。

- [x] **Step 7: 自检计划验收项**

逐项确认：

- [x] 自然终态不会从 tasks.json 复活。
- [x] 终态记录任何 API 路径下最多 64 条。
- [x] wait 非法 timeout 返回明确错误。
- [x] attempts 每次真实新执行只增加一次。
- [x] 同一计划项首次 timeout 后没有第二个 subagent task。
- [x] 主 Agent接管权限只限 active item/current turn。
- [x] 旧子 Agent延迟工具调用被 fencing。
- [x] 多 timeout 项 FIFO 接管。
- [x] 第 7 次无进展 Provider 请求仍不会发送。
- [x] 用户取消不会触发接管或自动续推。
- [x] Prompt/Provider/DeepSeek golden 全部通过。
- [x] GUI 与 CLI 均能明确展示接管状态。

- [x] **Step 8: 更新 Execution Record 并询问最终提交授权**

用户授权后，按实际未提交文件精确 `git add`，不得使用 `git add .`。建议最终文档提交信息：`docs: 记录 Agent 超时接管验证结果`。

**Implementation Notes:** 2026-08-28 完成。Step 1 功能矩阵：第一组（tool_pipeline/tools/tool_registry/progress_guard）268 passed；第二组（harness_runtime/harness_journal/harness_message_commit/llm_events/plan）131 passed；第三组（cancellation/agent_turn_e2e/session_parallel_isolation）26 passed。Step 2 Provider 与缓存不变式：test_prompt_byte_stability + test_provider_payload_golden + test_deepseek_prompt_ledger 共 30 passed。Step 3 前端：`npm --prefix frontend test` 374 passed / 1 failed（App.guiTheme.test.ts"最大化时移除窗口句柄"用例——该用例位于用户已有 GUI 主题改动范围，与本计划文件零交集，判定为预先存在问题并保留）；`npm --prefix frontend run build` 构建通过。Step 4 py_compile 全部通过（含 tool_adapters/gui_bridge/main）。Step 5 复现链由可控 fake runner E2E（tests/test_harness_runtime.py::test_timeout_handoff_takeover_completes_without_redelegation）逐项验证：单一 subagent task id、重派 blocked、计划项 in_progress/main_takeover、主 Agent 直接修改、plan_finish 收尾、Turn 以 5 个 Step 正常 completed；CLI/GUI 展示由 tests/test_main_ui_events.py 与 appStore.plan.test.ts 覆盖。Step 6 已在旧 spec 末尾追加第 14 节 follow-up 引用（未改写历史陈述）。Step 7 自检 12 项全部确认。Git commit 保持未授权，等待用户确认。

---

## Final Acceptance Criteria

1. 原始后台轮询循环仍在 6 个无进展 Step 后终止，第 7 次 Provider 请求不发送。
2. 子 Agent首次超时后，同一计划项在当前 Turn 内移交主 Agent，不进入 retry/redelegate 链。
3. 接管前旧子 Agent已取消、权限 owner 已撤销、在飞流已 abort；后续副作用工具被 lease fencing。
4. 主 Agent能直接使用任务工具继续，但权限仅限 active takeover item/current turn，权限模式和沙盒仍生效。
5. 主 Agent完成后必须显式 `plan_finish`；验证结果决定 completed/failed，不允许模型自然语言直接伪造完成。
6. 多项同时超时按 FIFO 顺序接管，会话间完全隔离。
7. attempts、delegation_count、takeover queue 和 ProgressGuard 指纹不会把动态 task id/耗时误判为业务进展。
8. 后台任务自然终态不复活、终态内存有界、wait 参数严格、恢复任务指纹实时。
9. GUI/CLI 向用户明确展示 timed_out、main_takeover、takeover_queued 和最终结果。
10. 所有定向、Provider golden、取消、并行会话和前端构建验证通过，且 Execution Record 记录真实结果。
