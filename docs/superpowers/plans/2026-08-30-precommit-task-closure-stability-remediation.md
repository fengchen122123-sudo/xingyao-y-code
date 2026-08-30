# Pre-Commit Task Closure Stability Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复提交前审查已动态复现的终态完成屏障、计划接管状态破坏、文件租约丢失与测试隔离问题，使 Harness 任务闭环只有一个确定出口，并在一次完整回归通过后结束本轮稳定性检查。

**Architecture:** `done_event` 继续作为子 Agent 终态回写、通知和 handoff 全部完成后的唯一可见屏障；模型可调用的 `plan_update` 只编辑未被执行租约占用的计划元数据，所有运行状态仍由调度器原子转换。`TaskScheduler` 增加按计划项记录的文件租约账本，所有 start/delegate/claim/resume/promote/release 路径共用同一组 claim/release helper，测试则直接绑定 `core.app_context` 的持久化管理器，消除收集顺序依赖。

**Tech Stack:** Python 3.12、pytest、threading、SQLite/Harness Journal、Vue 3、Pinia、Vitest。

**Spec:** `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md` 第 14 节；前置实现与约束见 `docs/superpowers/plans/2026-08-28-agent-loop-hardening-review-remediation.md`。

## Global Constraints

- 本计划只处理 2026-08-30 提交前审查已复现的 4 个实现缺陷和 2 个测试隔离失败，不开展新的开放式缺陷扫描。
- `done_event` 是终态回调、计划回写、handoff 和通知全部落定后的唯一完成屏障；任何读取最终结果的前台路径都不得只依据 `status` 提前返回。
- `status`、`owner`、`result`、`error`、`attempts` 属于调度器拥有的运行字段，模型侧 `plan_update` 不得写入。
- `in_progress` 与 `takeover_queued` 项处于执行租约保护期，模型侧不得修改 subject、depends_on、files、verification、stage 或 kind。
- failed/blocked/cancelled 的接管项已释放运行租约时，允许先修改任务元数据，再通过 `plan_resume_takeover` 恢复；不得借 `plan_update(status="pending")` 绕过恢复入口。
- 文件租约必须按 `item_id -> claimed files` 记录；释放必须使用认领时的不可变快照，禁止读取可能已变化的计划字段猜测应释放哪些文件。路径 canonical 化继续沿用既有 Harness 工具边界，本计划不另改路径语义。
- 崩溃恢复后的 queued takeover 只有在成功认领文件租约后才能提升为 `main_takeover`；冲突时保持 queued，不得无锁运行。
- 不修改 Harness Journal 正文边界、Provider 请求构造、DeepSeek Prompt Ledger、Kimi/GLM 路由、稳定 system prompt 或 tools schema 字节。
- 严禁新增降级、兜底重试、启发式修补或吞异常；状态不变量被破坏时应尽早显式失败。
- 保留用户工作树中与本计划无关的修改；禁止批量格式化、清理或重构。
- Git commit、push、merge 未获授权。每个 Task 只给出建议提交信息，执行者必须等待用户明确授权后才能提交。

## Confirmed Evidence

| ID | 复现证据 | 风险 | 修复任务 |
|---|---|---|---|
| C1 | worker 失败并阻塞 terminal callback 时，观测到 `run_returned_before_callback=true`、`done_event=false`、`status=failed` | 前台读取半完成结果，计划回写/handoff/通知尚未闭环 | Task 1 |
| C2 | active takeover 被 `update(status="pending")` 后形成 `pending/main_takeover/takeover_required=true`，且 `ready_items=[]` | 计划项无法再次调度，任务永久卡住 | Task 2 |
| C3 | 运行项 files 从 `a.py` 改成 `b.py` 后取消，`remaining_file_locks=["a.py"]` | 原文件锁永久泄漏，新文件又未受保护 | Task 2-3 |
| C4 | 崩溃恢复 queued takeover 晋升成功后，`_in_flight_ids=[]`、`_in_flight_files=[]` | active takeover 无资源租约，可与其他任务并发覆盖 | Task 3 |
| T1 | `test_completed_tool_turn_persists_one_ordered_batch` 单独/核心组运行失败 | 测试 patch `main.manager`，实现读取 `app_context.get_manager()` | Task 4 |
| T2 | `test_cancelled_tool_turn_salvages_completed_messages` 单独/核心组运行失败 | 同 T1，测试通过依赖其他模块收集时注册入口上下文 | Task 4 |

## Execution Record

| Task | Status | RED | GREEN | Notes |
|---|---|---|---|---|
| 1. 终态完成屏障 | completed | `pytest tests/test_tools.py::test_foreground_waits_for_failed_terminal_callback_before_returning -q` → 1 failed，败于 `assert thread.is_alive() is True`（EXIT 1） | 定向 `pytest tests/test_tools.py::test_foreground_waits... -q` → 1 passed；回归 `-k "terminal_callback or finaliz or foreground_waits or timeout_sets_cancel"` → 4 passed, 187 deselected（EXIT 0） | 仅删除 run() 等待循环中按终态 status 提前 break 的两行；`_terminate()` 返回前已设置 done_event（已核实源码）。未执行 commit。 |
| 2. 计划模型写入边界 | completed | `pytest tests/test_plan.py -k "public_plan_update" -q` → 3 failed / 1 passed（既有防伪测试保持 PASS；EXIT 1） | 定向同上 → 4 passed；回归 `pytest tests/test_plan.py tests/test_tool_registry.py tests/test_prompt_byte_stability.py -q` → 73 passed（EXIT 0），tools schema hash 不变 | 新增 `_requested_runtime_fields` 与 `_plan_item_execution_locked`；plan_update 签名与字段顺序未变；运行字段拒绝文案按项指向 plan_retry / plan_resume_takeover / plan_finish。未执行 commit。 |
| 3. 文件租约账本与恢复晋升 | completed | `pytest tests/test_task_scheduler.py -k "claimed_file_snapshot or recovered_takeover or keeps_queue" -q` → 3 failed（a.py 租约残留 / 晋升未登记 in-flight / 冲突仍晋升；EXIT 1） | 定向同上 → 3 passed；回归 `pytest tests/test_task_scheduler.py tests/test_plan.py tests/test_session_parallel_isolation.py tests/test_tool_pipeline.py -q` → 151 passed（EXIT 0） | 新增 `_in_flight_files_by_item` 与 `_files_available_locked`/`_claim_files_locked`/`_release_files`；start/delegate/claim/resume/promote/release/shutdown 全部接入；transition 成功但 claim 失败显式抛 RuntimeError。未执行 commit。 |
| 4. 消息持久化测试隔离 | completed | `pytest tests/test_llm_events.py::test_completed_tool_turn_persists_one_ordered_batch tests/test_llm_events.py::test_cancelled_tool_turn_salvages_completed_messages -q` → 1 failed / 1 passed，错误为 `RuntimeError: 消息持久化失败：存储管理器不可用`（EXIT 1） | 独立/正序/逆序各 2 passed；核心组 `pytest tests/test_harness_runtime.py tests/test_llm_events.py tests/test_llm_events_fixes.py -q` → 154 passed（EXIT 0） | 两个测试删除 `import main` + `monkeypatch.setattr(main, "manager", ...)`，改为 `mocker.patch("core.app_context.get_manager", ...)`；conftest 未改（经核查只做全局快照/还原）。未执行 commit。 |
| 5. 提交门禁与文档收尾 | completed | 不适用（门禁任务） | 全部 EXIT 0 | py_compile（tools/plan/task_scheduler）EXIT 0；高风险组 496 passed；Provider/Token 门禁 54 passed（golden/hash 未动）；完整 `pytest -q` → 1714 passed, 2 skipped（均为既有 Windows 符号链接权限条件：test_setup_engine.py:300、test_skills.py:171），EXIT 0；前端 `npm test` 383 passed（57 文件）EXIT 0；`npm run build` EXIT 0；`git diff --check` EXIT 0；仍在 dev 分支，无意外临时文件。spec 已追加 14.2 增补。未执行 commit，等待用户授权。 |

---

### Task 1: 让前台结果严格等待 `done_event`

**Files:**
- Modify: `core/tools.py:2587-2635`
- Test: `tests/test_tools.py:450-525`

**Interfaces:**
- Consumes: `SubagentTask.done_event: threading.Event`。
- Preserves: `_complete()` / `_terminate()` 的 exactly-once callback claim 与“最后设置 done_event”顺序。
- Produces invariant: `SubagentManager.run()` 返回时，对应 task 必须满足 `done_event.is_set() is True`。

- [ ] **Step 1: 写 worker 失败期间 callback 阻塞的 RED 测试**

在 `tests/test_tools.py` 增加：

```python
def test_foreground_waits_for_failed_terminal_callback_before_returning():
    entered = threading.Event()
    release = threading.Event()
    results = []

    def runner(_record):
        raise RuntimeError("boom")

    def terminal_callback(_task, _status, _result, _error):
        entered.set()
        assert release.wait(2.0)

    manager = SubagentManager(runner=runner, wait_interval=0.001)
    thread = threading.Thread(
        target=lambda: results.append(manager.run(
            "失败任务",
            "session",
            "turn",
            timeout_seconds=30,
            terminal_callback=terminal_callback,
        )),
        daemon=True,
    )
    thread.start()
    assert entered.wait(2.0)
    task = next(iter(manager._tasks.values()))

    try:
        time.sleep(0.05)
        assert thread.is_alive() is True
        assert task.status == "failed"
        assert task.done_event.is_set() is False
        assert results == []
    finally:
        release.set()
        thread.join(timeout=2.0)

    assert thread.is_alive() is False
    assert task.done_event.is_set() is True
    assert len(results) == 1
```

- [ ] **Step 2: 运行单测并确认 RED 原因准确**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py::test_foreground_waits_for_failed_terminal_callback_before_returning -q
```

Expected: FAIL 于 `assert thread.is_alive() is True`；失败期间 task 已是 `failed`，但 `done_event` 尚未设置。

- [ ] **Step 3: 删除前台循环按 terminal status 提前退出的分支**

在 `SubagentManager.run()` 的等待循环中，删除以下语义：

```python
with self._lock:
    if task.status in _SUBAGENT_TERMINAL_STATUSES:
        break
```

保留 `while not task.done_event.wait(self._wait_interval)` 作为唯一正常退出条件。父回合取消与前台超时仍同步调用 `_terminate()`；`_terminate()` 返回前已经设置 done_event，因此后续 `break` 不会越过终态屏障。

- [ ] **Step 4: 运行终态并发回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py -k "terminal_callback or finaliz or foreground_waits or timeout_sets_cancel" -q
```

Expected: 全部 PASS；阻塞 callback 期间 active/idle/prune 语义保持原有测试约束。

- [ ] **Step 5: 建议提交点（未授权不执行）**

Suggested commit: `fix(subagent): wait for terminal finalization barrier`

---

### Task 2: 收紧模型侧 `plan_update` 写入边界

**Files:**
- Modify: `core/plan.py:788-820`
- Test: `tests/test_plan.py:138-170`

**Interfaces:**
- Preserves: `PlanManager.update()` 作为调度器和内部 Python 调用的低层原子更新接口。
- Changes: 模型工具 `plan_update(...)` 不再接受任何运行字段写入。
- Produces: `_plan_item_execution_locked(item: dict) -> bool`，只在 `status == "in_progress"` 或 `execution_mode == "takeover_queued"` 时返回 True。

- [ ] **Step 1: 写 active takeover 状态破坏的 RED 测试**

```python
def test_public_plan_update_cannot_reset_active_takeover_to_pending():
    from core.plan import get_default_plan_manager, plan_update
    from core.task_scheduler import TaskScheduler

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{"id": "item", "subject": "work"}])
    scheduler = TaskScheduler(manager)
    assert scheduler.start_delegated_item(
        "item", "subagent-abcd", "subagent:implement", None
    )
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd", "turn-1", "timeout"
    ) is not None
    before = manager.snapshot()[0]

    message = plan_update("item", status="pending")

    assert "状态变更由调度器自动管理" in message
    assert manager.snapshot()[0] == before
    manager.clear()
```

- [ ] **Step 2: 写运行中 files/元数据冻结的 RED 测试**

```python
def test_public_plan_update_cannot_edit_execution_locked_item():
    from core.plan import get_default_plan_manager, plan_update

    manager = get_default_plan_manager()
    manager.clear()
    manager.replace([{
        "id": "item",
        "subject": "work",
        "status": "pending",
        "files": ["a.py"],
        "execution_mode": "takeover_queued",
        "takeover_required": True,
    }])
    before = manager.snapshot()[0]

    message = plan_update("item", subject="changed", files=["b.py"])

    assert "执行租约" in message
    assert manager.snapshot()[0] == before
    manager.clear()
```

再增加一个允许路径：普通 pending 项可更新 subject/files；failed takeover 项可更新 subject/files，但必须继续拒绝 status，随后只能走 `plan_resume_takeover`。

- [ ] **Step 3: 运行计划工具 RED 测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py -k "public_plan_update" -q
```

Expected: 新增的 active/queued 冻结测试 FAIL，现有 completion 防伪测试保持 PASS。

- [ ] **Step 4: 实现明确的模型写入策略**

在 `plan_update` 包装层先读取计划项，再决定是否调用 `PlanManager.update()`：

```python
def _requested_runtime_fields(*, status, owner, result, error, attempts) -> list[str]:
    values = {
        "status": status,
        "owner": owner,
        "result": result,
        "error": error,
        "attempts": attempts,
    }
    return [name for name, value in values.items() if value is not None]


def _plan_item_execution_locked(item: dict) -> bool:
    return (
        item.get("status") == "in_progress"
        or item.get("execution_mode") == "takeover_queued"
    )
```

执行规则：

1. `_requested_runtime_fields(...)` 非空时直接返回固定错误并列出字段名，绝不调用 `PlanManager.update()`。
2. 找不到 item 时返回现有“不存在”错误。
3. `_plan_item_execution_locked(item)` 为 True 时，任何元数据修改都返回“执行租约期间不可修改”。
4. 其余状态只允许修改 `subject/depends_on/files/verification/verification_type/stage/kind`。
5. 保留函数签名与 tools schema 字段顺序，避免改变稳定 tools schema 字节；只改变运行时校验。

固定错误应分别指向正确入口：普通失败项用 `plan_retry`，接管项用 `plan_resume_takeover`，完成用 `plan_finish`。

- [ ] **Step 5: 运行计划与工具 schema 回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_tool_registry.py tests/test_prompt_byte_stability.py -q
```

Expected: 全部 PASS，tools schema hash 不变。

- [ ] **Step 6: 建议提交点（未授权不执行）**

Suggested commit: `fix(plan): protect scheduler-owned execution state`

---

### Task 3: 用按项租约账本统一文件 claim/release

**Files:**
- Modify: `core/task_scheduler.py:75-140`
- Modify: `core/task_scheduler.py:190-260`
- Modify: `core/task_scheduler.py:399-445`
- Modify: `core/task_scheduler.py:530-715`
- Modify: `core/task_scheduler.py:735-810`
- Test: `tests/test_task_scheduler.py`
- Test: `tests/test_plan.py:340-455`

**Interfaces:**
- Produces: `TaskScheduler._in_flight_files_by_item: dict[str, frozenset[str]]`。
- Produces: `_files_available_locked(item_id, files) -> bool`、`_claim_files_locked(item_id, files) -> bool`、`_release_files(item_id) -> None`。
- Preserves: `_in_flight_ids` 与 `_in_flight_files` 现有查询语义；二者改由 helper 统一维护。

- [ ] **Step 1: 写释放认领时快照的 RED 测试**

```python
def test_release_uses_claimed_file_snapshot_not_mutated_plan_files():
    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "work", "files": ["a.py"]}])
    scheduler = TaskScheduler(plan)
    assert scheduler.start_item("item") is True

    # 模拟内部调用方绕过模型边界修改计划，调度器仍必须释放真实租约。
    assert plan.update("item", files=["b.py"]) is True
    assert scheduler.cancel("item") is True

    assert scheduler._in_flight_ids == set()
    assert scheduler._in_flight_files == set()
    assert scheduler._in_flight_files_by_item == {}
```

- [ ] **Step 2: 写崩溃恢复晋升重新认领租约的 RED 测试**

```python
def test_promote_recovered_takeover_claims_files_before_activation():
    plan = PlanManager()
    plan.replace([{
        "id": "item",
        "subject": "recovered",
        "status": "pending",
        "files": ["shared.py"],
        "execution_mode": "takeover_queued",
        "takeover_required": True,
        "takeover_sequence": 1,
    }])
    scheduler = TaskScheduler(plan)

    promoted = scheduler.promote_next_takeover("turn-2")

    assert promoted is not None
    assert promoted["execution_mode"] == "main_takeover"
    assert scheduler._in_flight_ids == {"item"}
    assert scheduler._in_flight_files == {"shared.py"}
    assert scheduler._in_flight_files_by_item == {
        "item": frozenset({"shared.py"})
    }
```

- [ ] **Step 3: 写冲突时保持 queued 的 RED 测试**

```python
def test_promote_takeover_keeps_queue_when_files_are_claimed():
    plan = PlanManager()
    plan.replace([
        {"id": "active", "subject": "active", "files": ["shared.py"]},
        {
            "id": "queued",
            "subject": "queued",
            "status": "pending",
            "files": ["shared.py"],
            "execution_mode": "takeover_queued",
            "takeover_required": True,
            "takeover_sequence": 1,
        },
    ])
    scheduler = TaskScheduler(plan, max_workers=2)
    assert scheduler.start_item("active") is True

    assert scheduler.promote_next_takeover("turn-2") is None
    queued = next(item for item in plan.snapshot() if item["id"] == "queued")
    assert queued["status"] == "pending"
    assert queued["execution_mode"] == "takeover_queued"
```

- [ ] **Step 4: 运行租约 RED 测试**

```powershell
venv\Scripts\python.exe -m pytest tests/test_task_scheduler.py -k "claimed_file_snapshot or recovered_takeover or keeps_queue" -q
```

Expected: 新测试因缺少 `_in_flight_files_by_item` 或 promote 未登记资源而 FAIL。

- [ ] **Step 5: 实现租约 helper**

在 `TaskScheduler.__init__` 初始化：

```python
self._in_flight_files_by_item: dict[str, frozenset[str]] = {}
```

helper 语义：

```python
def _files_available_locked(self, item_id: str, files) -> bool:
    requested = frozenset(str(path) for path in files)
    current = self._in_flight_files_by_item.get(item_id)
    if current is not None:
        return current == requested
    return not bool(requested & self._in_flight_files)


def _claim_files_locked(self, item_id: str, files) -> bool:
    requested = frozenset(str(path) for path in files)
    if not self._files_available_locked(item_id, requested):
        return False
    current = self._in_flight_files_by_item.get(item_id)
    if current is None:
        self._in_flight_files_by_item[item_id] = requested
        self._in_flight_ids.add(item_id)
        self._in_flight_files.update(requested)
    return True


def _release_files(self, item_id: str) -> None:
    with self._lock:
        claimed = self._in_flight_files_by_item.pop(item_id, frozenset())
        self._in_flight_files.difference_update(claimed)
        self._in_flight_ids.discard(item_id)
```

禁止 `_release_files` 再读取 `self._item(item_id)["files"]`。

- [ ] **Step 6: 把普通启动与委派路径接入 helper**

在 `_start_item_locked` 与 `start_delegated_item` 中，先检查 `_files_available_locked`，状态转换成功后调用 `_claim_files_locked`。删除这两个路径对 `_in_flight_ids.add(...)` 和 `_in_flight_files.update(...)` 的直接调用。

- [ ] **Step 7: 把 takeover 认领、恢复和晋升接入 helper**

逐一替换以下直接写集合的路径：

- `claim_takeover_for_turn`
- `resume_takeover`
- `promote_next_takeover`

每个路径先在 scheduler 锁内检查 `_files_available_locked`，状态转换成功后调用 `_claim_files_locked`。`promote_next_takeover` 必须在冲突时保持原 queued 状态；transition 成功但 claim 失败属于不变量错误，应显式抛出 `RuntimeError`，不得静默返回半完成状态。

handoff queued 继续沿用 delegated 阶段已认领的同一租约，不重复 claim，也不提前 release。

- [ ] **Step 8: 接入释放与 shutdown 清理**

所有 complete/fail/cancel/release 路径继续只调用 `_release_files(item_id)`；`shutdown()` 必须同时清空 `_in_flight_files_by_item`、`_in_flight_ids` 与 `_in_flight_files`，禁止留下任一账本残片。

- [ ] **Step 9: 运行调度状态机回归**

```powershell
venv\Scripts\python.exe -m pytest tests/test_task_scheduler.py tests/test_plan.py tests/test_session_parallel_isolation.py tests/test_tool_pipeline.py -q
```

Expected: 全部 PASS；FIFO、崩溃恢复、多会话隔离和 takeover scoped 写入不回归。

- [ ] **Step 10: 建议提交点（未授权不执行）**

Suggested commit: `fix(scheduler): account file leases by plan item`

---

### Task 4: 消除消息持久化测试的收集顺序依赖

**Files:**
- Modify: `tests/test_llm_events.py:1335-1435`
- Verify: `tests/conftest.py`

**Interfaces:**
- Consumes: 生产持久化入口 `core.app_context.get_manager()`。
- Produces: 两个 legacy persistence 测试不再导入/修改 `main.manager`，且可独立运行。

- [ ] **Step 1: 记录现有独立运行 RED**

```powershell
venv\Scripts\python.exe -m pytest tests/test_llm_events.py::test_completed_tool_turn_persists_one_ordered_batch tests/test_llm_events.py::test_cancelled_tool_turn_salvages_completed_messages -q
```

Expected: 两个用例至少一个稳定 FAIL，错误为 `消息持久化失败：存储管理器不可用`。

- [ ] **Step 2: 把测试 patch 点改为真实依赖边界**

两个测试均删除：

```python
import main
monkeypatch.setattr(main, "manager", Manager())
```

替换为：

```python
mocker.patch("core.app_context.get_manager", return_value=Manager())
```

不得为让测试变绿而修改 `core/llm.py` 回退读取 `main.manager`；生产代码已经正确通过 app_context 解耦入口层。

- [ ] **Step 3: 独立、逆序和核心组验证**

```powershell
venv\Scripts\python.exe -m pytest tests/test_llm_events.py::test_cancelled_tool_turn_salvages_completed_messages tests/test_llm_events.py::test_completed_tool_turn_persists_one_ordered_batch -q

venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_llm_events.py tests/test_llm_events_fixes.py -q
```

Expected: 两条命令全部 PASS，测试结果不依赖模块收集顺序。

- [ ] **Step 4: 检查 conftest 全局恢复职责**

确认 `tests/conftest.py::_restore_entry_layer_globals` 仍只负责恢复真实全局污染，不增加“如果 manager 为空就自动导入 main”之类的补丁。测试应显式声明依赖，不能由 fixture 猜测入口环境。

- [ ] **Step 5: 建议提交点（未授权不执行）**

Suggested commit: `test(llm): isolate persistence manager dependency`

---

### Task 5: 一次性提交门禁与停止条件

**Files:**
- Modify: `docs/superpowers/plans/2026-08-30-precommit-task-closure-stability-remediation.md`（Execution Record）
- Modify: `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`（仅追加最终不变量增补，不改历史正文）

**Interfaces:**
- Consumes: Tasks 1-4 的实现与测试。
- Produces: 一次可审计的最终验证记录和明确的提交/停止结论。

- [ ] **Step 1: 运行 Python 语法检查**

```powershell
venv\Scripts\python.exe -m py_compile core/tools.py core/plan.py core/task_scheduler.py
```

Expected: EXIT 0，无输出。

- [ ] **Step 2: 运行闭环高风险组**

```powershell
venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_plan.py tests/test_task_scheduler.py tests/test_harness_runtime.py tests/test_tool_pipeline.py tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_session_parallel_isolation.py -q
```

Expected: EXIT 0，0 failed，0 errors；skip 必须逐项确认属于既有平台条件。

- [ ] **Step 3: 运行 Provider 字节与 Token 门禁**

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py -q
```

Expected: EXIT 0；任何 golden/hash 变化都视为本计划越界，禁止直接更新 golden 迁就实现。

- [ ] **Step 4: 运行完整 Python 测试**

```powershell
venv\Scripts\python.exe -m pytest -q
```

Expected: EXIT 0，0 failed，0 errors；命令必须完整结束，不接受“之前跑过”或只看中途进度。

- [ ] **Step 5: 运行前端回归与构建**

当前工作树包含前端改动，提交门禁必须覆盖：

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: 两条命令均 EXIT 0。

- [ ] **Step 6: 运行工作树检查**

```powershell
git diff --check
git status --short --branch
```

Expected: `git diff --check` EXIT 0；确认仍在 `dev`，无本计划生成的临时测试目录或日志被纳入提交。

- [ ] **Step 7: 更新规范增补与 Execution Record**

在 spec 末尾只追加以下最终不变量，不重写历史章节：

1. 前台结果只能在 done_event 后可见。
2. 模型不能写调度器运行字段，执行租约期间计划元数据冻结。
3. 文件租约按 item id 保存认领快照并从同一快照释放。
4. 恢复 takeover 晋升前必须重新 claim 文件。
5. persistence 测试直接 patch app_context 边界，禁止依赖入口 import 顺序。

把本文 Execution Record 的 RED/GREEN 命令、测试数、退出码和残余风险填写完整；没有证据不得标 completed。

- [ ] **Step 8: 应用停止条件**

满足以下全部条件后，本轮结论固定为“可以提交”，不得继续发起开放式找 bug：

- Tasks 1-4 的新增测试均经过 RED -> GREEN。
- 高风险组、Provider/Token 门禁、完整 pytest、前端测试与构建全部 EXIT 0。
- 独立代码审查没有 Critical/Important 遗留；Minor 只记录，不阻断。
- 工作树没有意外文件，变更仍在 `dev`。

若任一条件失败，只处理该失败的直接根因并重跑对应门禁；禁止借机扩大重构范围。

- [ ] **Step 9: 建议最终提交（必须等待用户授权）**

建议按 Task 1-4 分成可回滚的小提交；若用户明确要求一次提交，建议提交信息：

```text
fix(harness): close task finalization and file lease gaps
```

## Acceptance Matrix

| 场景 | 必须满足的结果 |
|---|---|
| worker 失败时 terminal callback 阻塞 | `run()` 不返回，done_event=false；callback 完成后才返回 |
| active takeover 收到 `plan_update(status="pending")` | 明确拒绝，计划快照逐字段不变 |
| delegated/main_takeover/takeover_queued 执行期间修改 files | 明确拒绝，不改变租约或计划快照 |
| 内部调用方意外改变计划 files | release 仍按认领快照清空原租约，无残留 |
| 恢复 queued takeover 晋升 | 先确认无文件冲突，再同时进入 main_takeover 和 in-flight 账本 |
| 晋升时文件冲突 | 保持 pending/takeover_queued，不产生半激活状态 |
| 两个 persistence 测试独立或逆序运行 | 均 PASS，不依赖 main/gui 入口注册副作用 |
| Provider/Token golden | 字节与计数测试全部通过，不更新 golden |
| 全量测试 | 完整结束且 0 failed / 0 errors |

## Out of Scope

- 不新增自动重试、自动修复、fallback manager 或恢复性后台线程。
- 不修改 NoProgressError 阈值、RepeatCallGuard 策略或 100 Step 最后安全阀。
- 不改变 GUI/CLI 展示、主题、TOML 配置、插件、MCP 或 LAN 功能。
- 不重新审查当前工作树中与 4 个确认缺陷无关的历史改动。
- 不以本计划为由合并到 master、push 或发布。

## Execution Handoff

实施时二选一：

1. **Subagent-Driven（推荐）**：每个 Task 派一个新实现代理，Task 结束后做规格审查和代码质量审查。
2. **Inline Execution**：在当前会话按 Task 1-5 顺序执行，每个 Task 完成 RED/GREEN 和定向验证后再进入下一项。

无论选择哪种方式，都不得并行修改 `core/plan.py` 与 `core/task_scheduler.py`；Task 2 必须先于 Task 3 完成，避免共享状态机改动互相覆盖。
