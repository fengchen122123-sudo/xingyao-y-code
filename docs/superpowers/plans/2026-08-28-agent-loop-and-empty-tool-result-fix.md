# Agent 循环与空工具结果修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 Agent 回合内的确定性无进展循环：放开观察类工具重复豁免、修正拦截语义、为后台任务提供 revision/wait 协议、新增无进展熔断。

**Architecture:** 按 spec 阶段 A→D 串行实施。A 修 RepeatCallGuard 参数感知豁免与错误语义；B 给后台任务加单调 revision 与 `manage_task(wait)` 条件等待；C 新建 TurnProgressGuard 以确定性状态哈希判进展并在 6 步无进展时熔断；D 把计划 canonical 快照接入进展观察。

**Tech Stack:** Python 3、pytest。

**Spec:** `docs/superpowers/specs/2026-08-28-agent-loop-and-empty-tool-result-fix-design.md`

## Global Constraints

- 日常开发只在 `dev` 分支进行；不得主动合并到 `master` 或其他分支。
- 所有实现遵循 TDD：先写失败测试，确认失败，再写最小实现。
- Harness Journal 仍只保存哈希与状态，不写入用户正文、工具参数原文或工具结果原文。
- 不修改权限策略、缓存前缀、消息历史中间字节和用户现有配置。
- 不新增第三方 Python 依赖。
- 保留 `max_turns = 100` 不变。
- 不得覆盖或回滚工作树中已有的无关修改（当前工作树留有 token 永久化与 TOML 配置两组无关改动：config.py、core/lan_server.py、frontend/src/components/Sidebar.*、tests/test_gui_token_usage.py、tests/test_lan_token_persist.py、tests/test_token_accounting.py、tests/test_config_toml_overlay.py、docs/superpowers/specs/2026-08-28-toml-config-support-design.md）。
- 每次 `git commit` 前获得用户明确授权。
- 重复拦截正文固定为（逐字）：`🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。请检查已有结果或改变执行方案。`
- 无进展终止用户可见错误固定为（逐字）：`🚨 连续 6 轮没有检测到后台任务、计划或工具结果状态变化，回合已终止，避免继续循环消耗。`
- canonical JSON：`json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，哈希 SHA-256。

## 勘察确认的现有代码事实（实施基线）

阶段 A 相关（已核实）：

- `core/harness/tool_pipeline.py:23` `TOOL_ERROR_PREFIX = "🚨"`。
- `core/harness/tool_pipeline.py:55-57` `REPEAT_CALL_BLOCKED_TEXT` 当前为 🛑 前缀旧文案。
- `core/harness/tool_pipeline.py:62-67` `_REPEAT_GUARD_EXEMPT_TOOLS = frozenset({"plan_write","plan_update","plan_read","plan_retry"})`。
- `core/harness/tool_pipeline.py:118-153` `RepeatCallGuard`：`signature(name, arguments_json)` 用 canonical JSON；`should_execute` 查计数；`record(name, arguments_json, ok)` 只记 ok；`BLOCK_AFTER_SUCCESSES = 2`。
- `core/harness/tool_pipeline.py:156-162` `_repeat_call_blocked_result(decoded)` 返回固定 tool 消息。
- `core/harness/tool_pipeline.py:279-280` `_default_result_ok(_name, message)`：`not content.startswith(TOOL_ERROR_PREFIX)`——文案改为 🚨 后拦截结果自动判 False。
- `core/harness/tool_pipeline.py:308-319` `_execute_guarded`：`guard_exempt = decoded.name in _REPEAT_GUARD_EXEMPT_TOOLS`；拦截路径 `return _repeat_call_blocked_result(decoded), 0`，不调 record（满足"拦截不计数"）。
- `core/harness/tool_pipeline.py:84-89` `DecodedToolCall` 是 frozen dataclass，含已解析的 `arguments: dict` 与 `raw_arguments: str`。
- `core/llm.py:3384-3386+` Harness 提交 tool_result_committed 时的 ok 判定函数认 `TOOL_ERROR_PREFIX`（🚨）等前缀——文案改 🚨 后 `result_status` 自动为 `error`。
- 既有测试模板：`tests/test_tool_pipeline.py:919`（guard 单测模式）、`tests/test_tool_pipeline.py:1068`（拦截结果模型可见性，硬编码旧 🛑 文案，需随文案更新）。

（阶段 B/C/D 勘察事实待补充）

---

## Task 1（阶段 A）：RepeatCallGuard 参数感知豁免 + 拦截错误语义

**Files:**
- Modify: `core/harness/tool_pipeline.py:55-67`（文案 + 新增豁免函数）、`:308-319`（_execute_guarded 用参数感知豁免）
- Test: `tests/test_tool_pipeline.py`（更新 1068 硬编码文案；新增豁免/拦截语义测试）

**Interfaces:**
- Produces: `repeat_guard_exempt(name: str, arguments: dict) -> bool`（模块级纯函数）；`REPEAT_CALL_BLOCKED_TEXT` 新文案。
- Consumed by: Task 3 的 TurnProgressGuard 依赖"拦截结果被识别为失败"（不计进展、不污染观察哈希语义）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_tool_pipeline.py` 新增（沿用 `_batch_call` / `_parallel_executor` / `_batch_context` 既有模式）：

```python
def test_repeat_guard_exempt_allows_readonly_manage_task_actions():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for action in ("status", "list", "wait"):
        assert repeat_guard_exempt("manage_task", {"action": action}) is True


def test_repeat_guard_exempt_blocks_side_effect_and_unknown_actions():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for action in ("kill", "stop", "terminate", "cancel", "bogus", ""):
        assert repeat_guard_exempt("manage_task", {"action": action}) is False


def test_repeat_guard_exempt_keeps_plan_tools_exempt():
    from core.harness.tool_pipeline import repeat_guard_exempt

    for name in ("plan_write", "plan_update", "plan_read", "plan_retry"):
        assert repeat_guard_exempt(name, {}) is True
    assert repeat_guard_exempt("read_file", {}) is False


def test_manage_task_status_called_three_times_all_execute():
    # manage_task(status) 连续三次均真实执行，不被 RepeatCallGuard 拦截
    executions = []
    executor = _parallel_executor(
        lambda call, _context: executions.append(call["id"])
        or _batch_tool_message(call["id"], content="status ok")
    )
    for i in range(3):
        records = executor.execute(
            [_batch_call(f"s{i}", "manage_task", '{"action":"status","task_id":"t1"}')],
            _batch_context(),
            None,
        )
    assert len(executions) == 3
    assert all("🚨" not in str(r.message.get("content", "")) for r in records)


def test_manage_task_kill_third_call_blocked_with_error_semantics():
    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

    executions = []
    executor = _parallel_executor(
        lambda call, _context: executions.append(call["id"])
        or _batch_tool_message(call["id"], content="killed")
    )
    for i in range(3):
        records = executor.execute(
            [_batch_call(f"k{i}", "manage_task", '{"action":"kill","task_id":"t1"}')],
            _batch_context(),
            None,
        )
    assert len(executions) == 2
    assert records[0].message["content"] == REPEAT_CALL_BLOCKED_TEXT
    assert REPEAT_CALL_BLOCKED_TEXT.startswith("🚨")


def test_repeat_blocked_text_exact_and_no_result_comparison_claim():
    from core.harness.tool_pipeline import REPEAT_CALL_BLOCKED_TEXT

    assert REPEAT_CALL_BLOCKED_TEXT == (
        "🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。"
        "请检查已有结果或改变执行方案。"
    )
    assert "结果未发生变化" not in REPEAT_CALL_BLOCKED_TEXT
```

同时把既有 `test_repeat_blocked_record_is_model_visible_and_not_executed`（1068 行）断言文案更新为新文案。

注意：`_parallel_executor` 每次调用 `executor.execute` 使用同一个 executor 实例时 repeat_guard 跨调用累计（`executor.repeat_guard` 是实例属性）；manage_task 的 kill 测试用同一 executor 连续 3 次 execute 即可触发第三次拦截。若现有 `_parallel_executor` 辅助函数不直接支持，参考 919/1068 行测试的构造方式适配。

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py -k "repeat_guard_exempt or manage_task_status_called or manage_task_kill or repeat_blocked" -q`
Expected: 新测试 FAIL（`repeat_guard_exempt` 不存在 / 旧文案断言不符）；更新后的 1068 测试 FAIL。

- [ ] **Step 3: 最小实现**

`core/harness/tool_pipeline.py`：

```python
REPEAT_CALL_BLOCKED_TEXT = (
    "🚨 相同工具调用已经成功执行过 2 次，本次调用未执行。"
    "请检查已有结果或改变执行方案。"
)

# （保留原注释与 frozenset 不变）
_REPEAT_GUARD_EXEMPT_TOOLS = frozenset({
    "plan_write",
    "plan_update",
    "plan_read",
    "plan_retry",
})

# 后台任务观察类 action 不消耗重复额度：模型等待后台任务时需要反复
# status/list/wait，拦截会迫使模型改用空输出命令当计时器（确定性循环根因）。
# kill/stop/terminate/cancel 与未知 action 按保守策略不豁免。
_REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS = frozenset({"status", "list", "wait"})


def repeat_guard_exempt(name: str, arguments: dict) -> bool:
    if name in _REPEAT_GUARD_EXEMPT_TOOLS:
        return True
    if name == "manage_task":
        action = str((arguments or {}).get("action", "")).strip().lower()
        return action in _REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS
    return False
```

`_execute_guarded` 改为：

```python
    def _execute_guarded(self, decoded, context):
        guard_exempt = repeat_guard_exempt(decoded.name, decoded.arguments)
        if not guard_exempt and not self.repeat_guard.should_execute(decoded.name, decoded.raw_arguments):
            return _repeat_call_blocked_result(decoded), 0
        message, duration_ms = self._execute_timed(decoded, context)
        if not guard_exempt:
            self.repeat_guard.record(
                decoded.name,
                decoded.raw_arguments,
                ok=self.result_ok(decoded.name, message),
            )
        return message, duration_ms
```

要求：判断使用已严格解析完成的 `decoded.arguments`，不重复解析 `raw_arguments`。

- [ ] **Step 4: 运行确认通过**

Run: `venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py -q`
Expected: 全部 PASS（含既有 202 项中的其余测试无回归）。

- [ ] **Step 5: 拦截结果的 Journal result_status 回归测试**

确认 llm.py 提交路径对 🚨 前缀写 `result_status="error"`。在 `tests/test_llm_events.py` 或 harness 相关测试中新增/确认用例：构造被拦截的 manage_task(kill) 第三次调用，走完 Harness 提交路径，断言 `tool_result_committed.result_status == "error"`。若既有测试已覆盖前缀语义（`_default_result_ok` / llm.py 3384 判定函数），写明覆盖位置并补一条针对拦截文案的精确断言。

Run: `venv\Scripts\python.exe -m pytest tests/test_llm_events.py -q`

- [ ] **Step 6: 提交（需用户授权）**

```bash
git add core/harness/tool_pipeline.py tests/test_tool_pipeline.py tests/test_llm_events.py
git commit -m "fix(harness): RepeatCallGuard 参数感知豁免 + 拦截结果错误语义"
```

---

（阶段 B/C/D 勘察事实，已核实）

- `core/task_manager.py`：`ACTIVE_TASKS` 全局 dict + `_TASKS_LOCK = threading.RLock()`（16-17 行）。任务字段：id/command/session_id/process/pid/create_time/log_file_path/log_handle/start_time/output_encoding——**无 revision、无 status 字段、无 exit_code、无终止保留（死亡即在 `list_active_tasks()` 中删除，kill 直接 `ACTIVE_TASKS.pop`）、无条件变量**。子进程 stdout/stderr 直写日志文件（主进程不持有输出流，感知新输出只能看日志文件大小变化）。`_watch_task_completion`（87-97 行）每任务监听线程仅 `process.wait()` 后触发完成回调。`_load_tasks` 磁盘重建任务 `process=None`（依赖 `is_pid_running` + create_time 校验）。`get_task_output`（`tail_lines` 截尾、ID 白名单校验）。spec 第 6.3 节写"在 core/tools.py 内部构造快照"，实际状态在 task_manager.py——**核心改动落在 task_manager.py，tools.py 仅 manage_task 薄壳**，此为对 spec 的有意偏差。
- `core/tools.py:1507-1560` `manage_task(action, task_id=None, event_callback=None, permission_owner=None, cancel_event=None)`：list/status/kill 三分支，返回纯文本；kill 走 `check_permission_and_ask` 确认。
- `core/tool_registry.py:375-385` manage_task schema：`{"action": str, "task_id": str}`，required=["action"]，`requires_permission=False`。
- `core/harness/tool_adapters.py:652-664` `execute_manage_task`：extras 按 `inspect.signature(self.manage_task).parameters` 过滤透传——manage_task 签名加参数后此处加键即可。
- `core/llm.py:3375-3393` `_tool_result_ok(function_name, result)`：content 可 JSON 解析且含 `status` 键时按 `status in {"running","completed","selected"}` 判定；否则按 🚨 等前缀判定。manage_task 改返回 JSON 后，`status="failed"/"timed_out"/"cancelled"` 会被判为 not-ok（任务失败语义，可接受）。
- `core/llm.py:628-654` `_permission_risk(function_name, arguments, tool_spec, path_access)` → `PermissionRisk.QUERY/NORMAL_MUTATION/SENSITIVE_MUTATION`；execute_command 走 `classify_command_risk(command)`（command_policy.py:328）。
- `core/llm.py:4175-4176` 主循环 `for turn in range(max_turns):`；4178-4179 `begin_step()`；工具提交四条路径：4660（空思考催促 continue）、4710（计划续推 continue）、4762（length 截断 continue）、4854-4858（正常工具提交 mark_tools_committed + complete）；正常收尾 4733-4737 complete + `harness_runtime.complete()` + return；超上限 4860-4876；取消 4878-4902 KeyboardInterrupt → `harness_runtime.cancel()`；异常 4923-4947 → `harness_runtime.fail(type(e).__name__)`。
- `core/harness/runtime.py`：`TurnRuntime.fail(code)`（179）、`StepRuntime.fail(code)`（321）、`begin_step()`（139）。
- `core/plan.py`：`get_default_plan_manager()` 单例；`PlanManager.snapshot()`（258-260，deepcopy items，字段含 id/status/attempts/depends_on/kind/stage/subject/error 等）；`in_progress()`（262-267）；`plan_auto_continue_suppressed(session_id)`（50-52，`_PLAN_AUTO_CONTINUE_SUPPRESSED` 集合）；plan_retry 两段式（628-668：attempts>=2 且有 last_error 时先返回根因分析提示不改状态；否则 scheduler.retry 重置 pending）。
- `core/llm.py:3896-3898`：`repeat_call_guard = RepeatCallGuard()` 在会话循环入口按 Turn 创建——`progress_guard` 同位创建即可满足"新 Turn 不继承停滞计数"（spec 6.6.6）。

### 阶段 C 关键设计决策（spec 6.5 落地解释）

- **观察类工具集合**复用 `tool_pipeline.py:46-49` 的 `_PLAN_READ_ONLY_TOOLS`（read_file, list_directory, search_code, find_definition, glob_files, fetch_url, search_news, get_weather, manage_task）。
- `tool_results_sha256`：本 Step **非观察类**工具结果 content 的聚合 canonical 哈希（无此类工具时为空串哈希）。RepeatCallGuard 拦截结果（🚨 固定文案）不纳入。
- `mutation_committed`：本 Step 任一工具 `risk != QUERY` 且结果 ok。轻量判定：`write_file/edit_file` 直接视为修改类；`execute_command` 用 `classify_command_risk(command) is not PermissionRisk.QUERY`（ping/sleep/dir 等查询类命令不算）。
- **已知权衡**（spec 有意选择）：纯只读探索（连续 read/search 不同文件）不算进展，理论上一个 Turn 连续 6 步纯读会触发熔断。这是 spec 为防"假忙"作出的选择，实施时严格照做；若运行数据表明误伤探索型子代理，再单独评估。

---

## Task 2（阶段 B）：后台任务 revision + `manage_task(wait)`

**Files:**
- Modify: `core/task_manager.py`（revision/status/exit_code/终态保留/条件变量/快照/等待）
- Modify: `core/tools.py:1507-1560`（manage_task 新增 wait 分支；status/list 改返回稳定 JSON）
- Modify: `core/tool_registry.py:375-385`（schema 加 wait 参数）
- Modify: `core/harness/tool_adapters.py:652-664`（透传 after_revision/timeout_seconds）
- Test: `tests/test_tools.py`（revision/wait/cancel/JSON 结构）、`tests/test_tool_registry.py`（schema）

**Interfaces:**
- Produces（task_manager 新增）：
  - `TaskStatusSnapshot` frozen dataclass：`task_id: str, status: str, revision: int, terminal: bool, output: str, exit_code: int | None`
  - `get_task_snapshot(task_id) -> TaskStatusSnapshot | None`
  - `list_task_snapshots() -> list[TaskStatusSnapshot]`（含终态）
  - `wait_task_change(task_id, after_revision, timeout_seconds, cancel_event=None) -> TaskStatusSnapshot | None`（None = task 不存在）
  - `background_tasks_fingerprint() -> str`（全部任务 {task_id: [status, revision, terminal, exit_code]} canonical JSON 的 SHA-256，供 Task 3 进展观察）
- Produces（tools.manage_task）：签名扩展 `manage_task(action, task_id=None, after_revision=None, timeout_seconds=30, event_callback=None, permission_owner=None, cancel_event=None)`；wait/status/list 返回 JSON 文本：
  ```json
  {"task_id":"3fda6b5a","status":"running","revision":13,"changed":true,"terminal":false,"exit_code":null,"output":"新增日志内容"}
  ```
  status 与 wait 返回同一结构（status 恒 `changed` 省略或为 false 均可，实现二选一并固定；推荐 status 不含 changed 键，wait 必含）。list 返回 `{"tasks": [快照...]}`。
- Consumed by: Task 3 的 ProgressObservation.task_snapshots_sha256 ← `background_tasks_fingerprint()`。

- [ ] **Step 1: 写失败测试（tests/test_tools.py）**

覆盖 spec 阶段 B 的 5 条 + JSON 结构：

```python
def test_background_task_revision_bumps_on_new_output():
    # 启动真实后台任务（参考既有测试的命令构造），读快照 revision=0，
    # 等日志文件出现新内容后 revision 单调增加

def test_background_task_terminal_transition_bumps_revision_and_exit_code():
    # 短命令（如 python -c "pass"）退出后：快照 terminal=True、
    # status in ("completed","failed")、exit_code 为进程码、revision 增加

def test_wait_returns_immediately_when_revision_advanced():
    # wait(after_revision=旧值) 在 revision 已更大时立即返回 changed=True

def test_wait_timeout_returns_changed_false_without_error():
    # 静默任务 wait(timeout_seconds=1) 到期：changed=False，结果 JSON 无 🚨 前缀

def test_wait_returns_terminal_snapshot_immediately():
    # 已终态任务 wait 立即返回 terminal=True + exit_code，即使 revision 未超过

def test_wait_cancel_event_interrupts_immediately():
    # cancel_event 置位后 wait 走 KeyboardInterrupt 取消路径，不伪装超时

def test_manage_task_status_and_list_return_stable_json():
    # status/list 返回可 json.loads 的结构，含 task_id/status/revision/terminal/exit_code/output
```

测试命令选型注意：后台任务经 `build_command_invocation` 走系统 shell；既有 test_tools.py 若有后台任务测试沿用其模式；用仓库 venv python `-c` 短脚本最稳。任务状态全局共享，每个测试用独立 task_id 并在结束时 kill 清理。

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python.exe -m pytest tests/test_tools.py -k "revision or wait_" -q`
Expected: FAIL（get_task_snapshot/wait_task_change 不存在）。

- [ ] **Step 3: 最小实现（task_manager.py）**

```python
# 顶部新增
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_TIMED_OUT = "timed_out"
TASK_STATUS_CANCELLED = "cancelled"
_TERMINAL_STATUSES = frozenset({
    TASK_STATUS_COMPLETED, TASK_STATUS_FAILED,
    TASK_STATUS_TIMED_OUT, TASK_STATUS_CANCELLED,
})
_TASKS_COND = threading.Condition(_TASKS_LOCK)
# 终态任务保留上限，防止 ACTIVE_TASKS 无限增长
_TERMINAL_KEEP_CAP = 64


@dataclass(frozen=True)  # 顶部需 from dataclasses import dataclass
class TaskStatusSnapshot:
    task_id: str
    status: str
    revision: int
    terminal: bool
    output: str
    exit_code: int | None
```

任务记录新增字段：`status`（默认 running）、`exit_code: None`、`revision: 0`、`last_log_size: 0`。

核心辅助（全部在持锁/Condition 内完成状态变更+通知）：

```python
def _bump_revision_locked(info):
    info["revision"] = int(info.get("revision", 0)) + 1
    _TASKS_COND.notify_all()

def _sync_task_locked(tid, info):
    """锁内同步单个任务：日志大小变化/进程退出 → 状态与 revision。返回是否变化。"""
    changed = False
    try:
        size = os.path.getsize(info["log_file_path"])
    except OSError:
        size = info.get("last_log_size", 0)
    if size != info.get("last_log_size", 0):
        info["last_log_size"] = size
        _bump_revision_locked(info)
        changed = True
    if info.get("status") == TASK_STATUS_RUNNING:
        exit_code = None
        dead = False
        proc = info.get("process")
        if proc is not None:
            poll = proc.poll()
            if poll is not None:
                dead, exit_code = True, int(poll)
        elif info.get("pid") is not None:
            if not is_pid_running(info["pid"]):
                dead = True  # 重建任务无法取退出码，exit_code 保持 None
        else:
            dead = True
        if dead:
            info["status"] = (
                TASK_STATUS_COMPLETED if exit_code in (None, 0) else TASK_STATUS_FAILED
            )
            info["exit_code"] = exit_code
            try:
                if info.get("log_handle"):
                    info["log_handle"].close()
            except Exception:
                pass
            _bump_revision_locked(info)
            changed = True
    return changed
```

快照与等待：

```python
def _snapshot_locked(tid, info, tail_lines=40) -> TaskStatusSnapshot:
    return TaskStatusSnapshot(
        task_id=tid,
        status=info.get("status", TASK_STATUS_RUNNING),
        revision=int(info.get("revision", 0)),
        terminal=info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES,
        output=get_task_output(tid, tail_lines=tail_lines),
        exit_code=info.get("exit_code"),
    )

def get_task_snapshot(task_id):
    _ensure_paths(); _ensure_loaded()
    with _TASKS_COND:
        info = ACTIVE_TASKS.get(task_id)
        if info is None:
            return None
        _sync_task_locked(task_id, info)
        return _snapshot_locked(task_id, info)

def wait_task_change(task_id, after_revision, timeout_seconds, cancel_event=None):
    _ensure_paths(); _ensure_loaded()
    deadline = time.monotonic() + max(1, min(60, int(timeout_seconds)))
    with _TASKS_COND:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt("用户终止了工具执行。")
            info = ACTIVE_TASKS.get(task_id)
            if info is None:
                return None
            _sync_task_locked(task_id, info)
            snap = _snapshot_locked(task_id, info)
            if snap.revision > int(after_revision) or snap.terminal:
                return snap
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return snap  # 超时返回当前快照，changed=False 由调用方比较 revision 得出
            _TASKS_COND.wait(timeout=min(remaining, 0.5))
```

注意：`_snapshot_locked` 调 `get_task_output`（内部有文件 IO，且 `_TASKS_LOCK` 为 RLock 可重入）——保持简单，IO 在锁内可接受（尾部读有 256KB 上限）。

`start_background_task`：新任务记录带 `status=running/exit_code=None/revision=0/last_log_size=0`。

`_watch_task_completion` 改造：从纯 `process.wait()` 阻塞改为 0.5s 间隔循环——每拍 `_TASKS_COND` 内 `_sync_task_locked`（感知输出与退出），退出后触发完成回调（保留现有回调语义），并最后 bump 一次确保 wait 方唤醒。同时保留原回调签名。

`kill_background_task`：kill 成功后任务记录标 `status=cancelled`、bump revision、notify，**保留记录**（不再立即 pop）；`_save_tasks` 只落盘 running 任务（磁盘格式不变）。

`list_active_tasks()`：持锁内对每个任务 `_sync_task_locked`；返回结构保持现状且**只含 running 任务**（GUI/CLI 兼容）；不再删除死任务，改为标记终态；终态超过 `_TERMINAL_KEEP_CAP` 时淘汰最旧终态并 `_forget_task_locked`。

新增：

```python
def list_task_snapshots():
    _ensure_paths(); _ensure_loaded()
    with _TASKS_COND:
        snaps = []
        for tid, info in list(ACTIVE_TASKS.items()):
            _sync_task_locked(tid, info)
            snaps.append(_snapshot_locked(tid, info))
        return snaps

def background_tasks_fingerprint() -> str:
    import hashlib
    with _TASKS_COND:
        state = {
            tid: [info.get("status", TASK_STATUS_RUNNING),
                  int(info.get("revision", 0)),
                  info.get("status", TASK_STATUS_RUNNING) in _TERMINAL_STATUSES,
                  info.get("exit_code")]
            for tid, info in sorted(ACTIVE_TASKS.items())
        }
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: 最小实现（tools.py manage_task）**

```python
def manage_task(action, task_id=None, after_revision=None, timeout_seconds=30,
                event_callback=None, permission_owner=None, cancel_event=None):
    from core.task_manager import (
        get_task_snapshot, kill_background_task, list_task_snapshots, wait_task_change,
    )

    def _snapshot_payload(snap, changed=None):
        payload = {
            "task_id": snap.task_id, "status": snap.status, "revision": snap.revision,
            "terminal": snap.terminal, "exit_code": snap.exit_code, "output": snap.output,
        }
        if changed is not None:
            payload["changed"] = changed
        return json.dumps(payload, ensure_ascii=False)

    if action == "list":
        snaps = list_task_snapshots()
        running = [s for s in snaps if not s.terminal]
        if not running:
            return "ℹ 当前没有任何运行中的后台异步任务。"
        return json.dumps({"tasks": [_snapshot_payload(s) for s in json... ]}, ...)
```

（list 的 tasks 元素应为 dict 而非字符串：实现时构造 dict 列表后一次 json.dumps。）

```python
    elif action == "status":
        if not task_id:
            return "🚨 错误：查看状态必须提供 task_id 参数。"
        snap = get_task_snapshot(task_id)
        if snap is None:
            return f"🚨 未找到任务 {task_id}。"
        return _snapshot_payload(snap)

    elif action == "wait":
        if not task_id:
            return "🚨 错误：等待任务必须提供 task_id 参数。"
        if after_revision is None:
            return "🚨 错误：等待任务必须提供 after_revision 参数（非负整数）。"
        try:
            after = int(after_revision)
        except (TypeError, ValueError):
            return "🚨 错误：after_revision 必须是非负整数。"
        if after < 0:
            return "🚨 错误：after_revision 必须是非负整数。"
        snap = wait_task_change(task_id, after, timeout_seconds, cancel_event=cancel_event)
        if snap is None:
            return f"🚨 未找到任务 {task_id}。"
        return _snapshot_payload(snap, changed=snap.revision > after)

    elif action == "kill":  # 保持现状不变
```

- [ ] **Step 5: schema 与 adapter**

`tool_registry.py:375-385`：

```python
        ToolSpec(
            name="manage_task",
            description=(
                "查看、等待或终止后台任务。list/status/wait 为只读操作不弹确认；"
                "仅 kill 操作会请求用户确认。等待后台任务进展优先用 wait："
                "传入上次快照的 revision，任务状态或输出变化时立即返回。"
            ),
            input_schema=_schema({
                "action": {"type": "string", "enum": ["list", "status", "wait", "kill"]},
                "task_id": {"type": "string"},
                "after_revision": {"type": "integer", "minimum": 0},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30},
            }, ["action"]),
            ...
```

`tool_adapters.py:652-664`：extras 增加 `"after_revision": arguments.get("after_revision")` 与 `"timeout_seconds": arguments.get("timeout_seconds", 30)`（现有签名过滤机制会自动透传）。

- [ ] **Step 6: 运行确认通过 + 既有测试适配**

`tests/test_tools.py`、`tests/test_tool_registry.py` 中断言旧文本格式（"最新 40 行日志输出"等）的用例同步更新为 JSON 断言。

Run: `venv\Scripts\python.exe -m pytest tests/test_tools.py tests/test_tool_registry.py tests/test_tool_pipeline.py -q`
Expected: 全 PASS。

- [ ] **Step 7: 提交（需用户授权）**

```bash
git add core/task_manager.py core/tools.py core/tool_registry.py core/harness/tool_adapters.py tests/test_tools.py tests/test_tool_registry.py
git commit -m "feat(harness): 后台任务 revision 快照与 manage_task(wait) 条件等待"
```

---

## Task 3（阶段 C）：TurnProgressGuard 与无进展熔断

**Files:**
- Create: `core/harness/progress_guard.py`
- Create: `tests/test_progress_guard.py`
- Modify: `core/llm.py`（3898 附近创建 guard；工具提交路径 observe；4176 循环顶部门禁）
- Test: `tests/test_harness_runtime.py`（E2E：6 步无进展熔断）、`tests/test_llm_events.py`（用户可见错误事件）

**Interfaces:**
- Consumes: `task_manager.background_tasks_fingerprint()`（Task 2）；`repeat_guard 拦截结果 🚨 前缀`（Task 1）。
- Produces:
  ```python
  @dataclass(frozen=True)
  class ProgressObservation:
      task_snapshots_sha256: str
      plan_snapshot_sha256: str
      tool_results_sha256: str
      mutation_committed: bool

  class TurnProgressGuard:
          MAX_STALLED_STEPS = 6
          def observe(self, observation: ProgressObservation) -> bool: ...
          @property
          def stalled_steps(self) -> int: ...
          def should_abort(self) -> bool: ...
  ```
- llm.py 内部辅助（本任务定义，Task 4 复用）：
  `_step_progress_observation(records) -> ProgressObservation`（records 为 None 表示本步无工具执行）；`plan_snapshot_sha256` 本任务恒为固定空串哈希占位，Task 4 替换为真实计划指纹。

- [ ] **Step 1: 写纯单元失败测试（tests/test_progress_guard.py）**

```python
def test_first_observation_establishes_baseline(): ...
def test_same_observation_counts_stall(): ...
def test_task_hash_change_clears_stall(): ...
def test_plan_hash_change_clears_stall(): ...
def test_result_hash_change_clears_stall(): ...
def test_mutation_clears_stall(): ...
def test_abort_at_max_stalled_steps():
    # 基线后连续 6 次相同观察 → should_abort() True；期间任一变化则清零
def test_call_id_key_order_whitespace_do_not_count():
    # 由接入层 canonical 化保证；guard 层只比哈希——本用例验证哈希输入构造
    # （放 tests/test_llm_events.py 的接入测试亦可）
```

- [ ] **Step 2: 运行确认失败** → `pytest tests/test_progress_guard.py -q` FAIL（模块不存在）。

- [ ] **Step 3: 实现 progress_guard.py**

```python
"""确定性 Turn 进展观察：只比较状态哈希与修改类提交标记，不读模型自然语言。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProgressObservation:
    task_snapshots_sha256: str
    plan_snapshot_sha256: str
    tool_results_sha256: str
    mutation_committed: bool


class TurnProgressGuard:
    MAX_STALLED_STEPS = 6

    def __init__(self) -> None:
        self._baseline: ProgressObservation | None = None
        self._stalled_steps = 0

    def observe(self, observation: ProgressObservation) -> bool:
        """记录一步观察；返回本步是否产生真实进展。"""
        progressed = (
            self._baseline is None
            or observation.task_snapshots_sha256 != self._baseline.task_snapshots_sha256
            or observation.plan_snapshot_sha256 != self._baseline.plan_snapshot_sha256
            or observation.tool_results_sha256 != self._baseline.tool_results_sha256
            or observation.mutation_committed
        )
        if progressed:
            self._stalled_steps = 0
        else:
            self._stalled_steps += 1
        self._baseline = observation
        return progressed

    @property
    def stalled_steps(self) -> int:
        return self._stalled_steps

    def should_abort(self) -> bool:
        return self._stalled_steps >= self.MAX_STALLED_STEPS
```

基线语义：首次观察（Turn 第一步）建立基线且必定算进展，保证熔断最早出现在第 7 个 Step 之前（spec 验收 4：第 7 次 Provider 请求不得发送）。

- [ ] **Step 4: llm.py 接入**

3898 附近：
```python
    from core.harness.progress_guard import TurnProgressGuard
    repeat_call_guard = RepeatCallGuard()
    progress_guard = TurnProgressGuard()
```

新增内部函数（与 `_tool_result_ok` 同区）：

```python
_OBSERVATION_TOOLS = ...  # 复用 tool_pipeline._PLAN_READ_ONLY_TOOLS 作为观察类集合

def _step_progress_observation(records):
    import hashlib
    from core.task_manager import background_tasks_fingerprint
    from core.harness.tool_pipeline import _PLAN_READ_ONLY_TOOLS
    parts, mutation = [], False
    for record in records or ():
        name = record.message.get("name", "")
        content = str(record.message.get("content", "") or "")
        if content.startswith(REPEAT_CALL_BLOCKED_TEXT):  # 拦截结果不算进展
            continue
        ok = _tool_result_ok(name, record.message)
        if name in {"write_file", "edit_file"} and ok:
            mutation = True
        elif name == "execute_command" and ok:
            from core.command_policy import classify_command_risk
            from core.permission_policy import PermissionRisk
            # 注意 records 的 message 不含原始参数；execute_command 的查询类
            # 排除在接入处通过 record 附加信息完成（见实现说明）
        if name not in _PLAN_READ_ONLY_TOOLS:
            parts.append([name, content])
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return ProgressObservation(
        task_snapshots_sha256=background_tasks_fingerprint(),
        plan_snapshot_sha256=hashlib.sha256(b"").hexdigest(),  # Task 4 替换
        tool_results_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        mutation_committed=mutation,
    )
```

实现注意：`ToolExecutionRecord.message` 只有 role/tool_call_id/name/content，不含原始参数。execute_command 的"等待命令排除"需要命令文本——方案：`ToolExecutionRecord` 不改（frozen 且跨层引用多），在 llm.py 接入处用 `tool_calls_by_id[call_id]["function"]["arguments"]`（4789 已在手）解析 command 做 classify_command_risk 判定，作为 `mutation` 的依据；execute_command 结果哈希仍纳入 tool_results_sha256（ping 结果相同 → 哈希不变 → 不算进展，自然满足 spec 排除项）。

observe 提交点（每处 `mark_tools_committed`/continue 路径）：
- 4854-4858 正常工具提交后：`progress_guard.observe(_step_progress_observation(records))`
- 4660 空思考催促 continue 前：`progress_guard.observe(_step_progress_observation(None))`
- 4710 计划续推 continue 前：同上（计划哈希 Task 4 接入后此处才会因续推产生变化；当前恒停滞 +1，符合 spec——续推打转正是要熔断的场景）
- 4762 length 截断 continue 前：同上

门禁点（4176 循环顶部）：

```python
        for turn in range(max_turns):
            raise_if_cancelled()
            if progress_guard.should_abort():
                msg = (
                    "🚨 连续 6 轮没有检测到后台任务、计划或工具结果状态变化，"
                    "回合已终止，避免继续循环消耗。"
                )
                no_progress_message = {"role": "assistant", "content": msg}
                messages.append(no_progress_message)
                if stream:
                    persisted = deepcopy(no_progress_message)
                    persisted.update(_model_display_snapshot())
                    pending_persistence.append(("assistant", persisted))
                flush_persistence()
                if harness_runtime is not None:
                    harness_runtime.fail("NoProgressError")
                _notify_event(event_callback, "error", {"message": msg})
                yield from _yield_content(msg)
                return
            if harness_runtime is not None:
                current_step = harness_runtime.begin_step()
```

（不 begin_step → 不发送第 7 次 Provider 请求，满足验收 4；不重放、不注入"再试一次"，满足 6.6.5。）

- [ ] **Step 5: Harness E2E 失败测试→实现闭环**

`tests/test_harness_runtime.py` 参考现有 fake provider 模式：构造模型连续返回同一 `manage_task(status)` 调用的回合，断言第 6 个无进展 Step 后 Turn failed 且 `error_code="NoProgressError"`、Provider 请求数停在 6。另写"第 5 步后 revision 变化清零继续"用例（可用 mutation 路径代替：第 5 步返回 write_file 调用）。

`tests/test_llm_events.py`：断言用户可见 error 事件文案为固定文本。

- [ ] **Step 6: 运行确认通过**

Run: `venv\Scripts\python.exe -m pytest tests/test_progress_guard.py tests/test_harness_runtime.py tests/test_harness_journal.py tests/test_harness_message_commit.py tests/test_llm_events.py -q`
Expected: 全 PASS。

- [ ] **Step 7: 提交（需用户授权）**

```bash
git add core/harness/progress_guard.py tests/test_progress_guard.py core/llm.py tests/test_harness_runtime.py tests/test_llm_events.py
git commit -m "feat(harness): TurnProgressGuard 确定性无进展熔断（NoProgressError）"
```

---

## Task 4（阶段 D）：计划快照接入进展观察

**Files:**
- Modify: `core/plan.py`（新增 canonical 计划指纹函数）
- Modify: `core/llm.py`（`_step_progress_observation` 的 plan_snapshot_sha256 占位替换）
- Test: `tests/test_plan.py`（指纹变化/稳定用例）、`tests/test_harness_runtime.py` 或 `tests/test_llm_events.py`（plan_retry 两段式不误杀回归）

**Interfaces:**
- Consumes: Task 3 的 `_step_progress_observation`。
- Produces: `core.plan.plan_state_fingerprint(session_id: str | None) -> str`：
  canonical JSON 内容 = `{"session": session_id, "items": [[id, status, attempts, depends_on]...按 id 排序], "in_progress": id 或 None, "auto_continue_suppressed": bool}` 的 SHA-256。不含 subject/display 文案、时间戳、result/error 正文。

- [ ] **Step 1: 写失败测试（tests/test_plan.py）**

```python
def test_plan_state_fingerprint_stable_for_same_state(): ...
def test_plan_state_fingerprint_changes_on_status_change(): ...
def test_plan_state_fingerprint_changes_on_retry_reset():
    # failed → pending（attempts+1）后指纹变化（spec：plan_retry 真正重置时清零停滞）
def test_plan_state_fingerprint_ignores_display_text():
    # 只改 subject/result/error 文案不改状态字段时指纹不变
def test_plan_state_fingerprint_tracks_auto_continue_suppression(): ...
```

- [ ] **Step 2: 运行确认失败** → FAIL（函数不存在）。

- [ ] **Step 3: 实现 plan_state_fingerprint + llm.py 接入**

```python
def plan_state_fingerprint(session_id: str | None = None) -> str:
    import hashlib
    manager = get_default_plan_manager()
    sid = session_id or manager.session_id
    items = [] if (sid and manager.session_id != str(sid)) else manager.snapshot()
    payload = {
        "session": manager.session_id,
        "items": sorted(
            ([i["id"], i["status"], int(i.get("attempts", 0)), list(i.get("depends_on", []))]
             for i in items),
            key=lambda row: row[0],
        ),
        "in_progress": (manager.in_progress() or {}).get("id"),
        "auto_continue_suppressed": plan_auto_continue_suppressed(manager.session_id) if manager.session_id else False,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

llm.py `_step_progress_observation`：`plan_snapshot_sha256=plan_state_fingerprint(parent_session)`（替换占位）。

- [ ] **Step 4: plan_retry 两段式回归**

E2E 用例：计划含 failed 项 → 模型连续 plan_read/plan_retry/run_subagent 循环且快照不变 → 触发 NoProgressError；另用例：plan_retry 第二次调用真正重置 failed→pending → 指纹变化 → 停滞计数清零、Turn 继续（参考 test_harness_runtime.py 现有计划场景模式）。

- [ ] **Step 5: 全量回归**

Run（spec 第 10 节全部四组 + 语法检查）:
```powershell
venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tools.py tests/test_tool_registry.py tests/test_progress_guard.py -q
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py tests/test_harness_journal.py tests/test_harness_message_commit.py tests/test_llm_events.py tests/test_plan.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py -q
venv\Scripts\python.exe -m pytest tests/test_cancellation.py tests/test_agent_turn_e2e.py tests/test_session_parallel_isolation.py -q
venv\Scripts\python.exe -m py_compile core/llm.py core/tools.py core/tool_registry.py core/harness/tool_pipeline.py core/harness/progress_guard.py
```
Expected: 全 PASS、编译无错。

- [ ] **Step 6: 提交（需用户授权）**

```bash
git add core/plan.py core/llm.py tests/test_plan.py tests/test_harness_runtime.py tests/test_llm_events.py
git commit -m "feat(harness): 计划 canonical 快照接入 TurnProgressGuard"
```

---

## Self-Review 记录

- Spec 覆盖：6.1→Task 1 ✓；6.2→Task 1 ✓（🚨 前缀经 `_default_result_ok`/`_tool_result_ok` 自动写 error，拦截路径不 record 已满足"不计入成功次数"；GUI 错误卡片沿用既有 🚨 展示）；6.3→Task 2 ✓；6.4→Task 2 ✓；6.5→Task 3 ✓；6.6→Task 3 ✓；6.7→Task 4 ✓；6.8 保留 max_turns=100 不改 ✓。
- spec 6.3 "在 core/tools.py 内部构造快照" → 实际状态在 task_manager.py，快照构造落在 task_manager.py（已在 Task 2 注明偏差与理由）。
- spec 6.4 "manage_task(status) 同样返回该结构" → status 省略 `changed` 键、wait 必含（Task 2 注明）。
- 验收标准 1-10 逐条可由 Task 1-4 的测试矩阵映射；观测盲区修复（拦截记 error）在 Task 1 最先合入，符合实施建议。
- 类型一致性：TaskStatusSnapshot / wait_task_change / background_tasks_fingerprint / plan_state_fingerprint / ProgressObservation / TurnProgressGuard 签名跨任务一致。

