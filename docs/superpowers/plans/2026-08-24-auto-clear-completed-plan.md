# 任务完成后自动清空计划 实施方案（v2，已修订）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 任务（计划）全部完成后，自动清空计划快照，让 GUI「计划」面板和 CLI「当前任务规划/计划窗口」都归零，不再残留已完成或已终止的计划项。

**Architecture:** 在 `core/plan.py` 提供单一入口 `maybe_clear_completed_plan(session_id=None)`——当且仅当指定会话（缺省取当前路由）的所有计划项都进入终态（completed/failed/blocked/cancelled）时清空快照。GUI 在回合结束（`finish_once`）时**显式传 `session_name`** 走 `get_plan_manager_for` 注册表路由，并补发一个空 `plan_updated` 事件；CLI 在 `turn_finished` 分支不传参（单会话默认路由），清空后补一次 `invalidate()` 触发拉取式面板重绘。前端把 `plan_updated` 处理改为显式判断 `Array.isArray(data.items)`，防御非数组载荷。

**Tech Stack:** Python 3.12（core/main/gui_bridge）+ Vue 3 + Pinia + Vitest（frontend）。

## v2 修订说明（相对 v1 的四处修正）

1. **【严重】多会话并行下清错会话**：v1 在 `finish_once` 里裸调 `maybe_clear_completed_plan()`。但 `finish_once` 位于 `_run_turn_worker` finally 末尾（gui_bridge.py:3203），而计划会话 TLS 路由在 finally 前段已被解绑（gui_bridge.py:3172-3174），此时 `get_default_plan_manager()` 退回进程级单例 `_DEFAULT_PLAN_MANAGER`——该单例在用户切换会话时被 `_set_current_session`（gui_bridge.py:2251-2252）重绑，会话 A 回合结束时若用户已切到会话 B，清掉的是 B 的计划（跨会话串扰）。v2 改为入口函数接受显式 `session_id`，GUI 传 `session_name` 走 `get_plan_manager_for` 注册表路由（条目在回合内创建后跨回合存活，`release_plan_manager` 仅在会话删除时调用，storage_dir 正确）。
2. **【中】Task 2 测试无法运行**：v1 测试 `from gui_bridge import GuiBridge` ——该类不存在（gui_bridge.py 中只有 `ApiBridge`，347 行）；且未替换 `bridge._consume_turn_events`，真实方法会发起 LLM 请求。v2 按 `test_worker_emits_attachment_notes_inline_and_finishes_with_renamed_session`（tests/test_gui_tool_events.py:312-373）的既有模板重写。
3. **【低】Task 4 TDD 失败预期不成立**：JS 中 `[]` 为 truthy，现状 `if (data.items)` 遇空数组已会清空面板——v1 的新测试会直接 PASS 而非 FAIL。v2 重新定性为「防御性加固」：真正的缺口是 `items` 为非数组真值（如 `{}`）时 `.map` 抛 TypeError；红测改为非数组守卫用例，空数组用例保留为特性测试。
4. **Task 3 补 invalidate**：CLI 计划面板是拉取式渲染（main.py:3000-3003 `get_live_plan_content` 每次读取快照），`turn_finished` 分支本身无 invalidate（main.py:1181-1200），清空后需显式触发一次重绘；v1 的「若外层已统一 invalidate 则可省略」猜测不成立（该分支结束处无统一 invalidate）。同时 Task 3 测试改用既有 `TurnEventFactory` 模式（v1 的 `_event` helper 不存在）。

## Global Constraints

- 日常代码只在 `dev` 分支提交，绝不合并 `master`；未经用户明确要求不推送远端。
- 本方案不修改 Harness 状态机、工具 schema、Prompt Ledger、Provider 请求组装、历史消息规范化、数据库字段。
- 不新增进度工具、事件类型（复用既有 `plan_updated`）、数据库字段。
- 黄金字节（byte-stability / provider golden）不得为迁就实现改写；本方案不触碰请求构造，但仍需在改动后跑一遍回归确认。
- 计划门禁铁律（`core/harness/tool_pipeline.py`）：计划被清空后（snapshot 为空），执行工具门禁必须放行，不得再要求 `plan_item_id`（tool_pipeline.py:617-621 已有该放行逻辑，本方案不触碰）。清空计划必须走 `PlanManager.clear()`（`_commit_locked` 落盘），不能直接裸改 `_items`。
- **多会话路由铁律（本次修订新增）**：GUI 回合结束路径操作计划必须显式携带 `session_id`，不得依赖 TLS 路由——`finish_once` 执行时 TLS 已解绑，裸调 `get_default_plan_manager()` 会命中可能已被其他会话重绑的进程级单例。
- CLI 终端不输出 Markdown 强调符号（`**`/`*`），计划展示用 `plan_mark`/`plan_color` 既有 ANSI 标记。
- **GUI/CLI 清空时机语义差异（有意取舍）**：GUI 的 `finish_once` 在 finally 漏斗中，正常/取消/异常/无模型早退路径都会触发清空（清空条件只看计划状态，与回合结局无关；清空全终态计划不会复活任何被取消的工作——续推 nudge 决策发生在 core.llm 回合内、turn 结束之前，llm.py:4273-4297，且空快照本就解除续推抑制）。CLI 只在 `turn_finished` 分支清空，`turn_cancelled`/`turn_error` 不清——全终态计划最迟在下一个正常回合结束时归零，行为无害且改动面最小。

---

## File Structure

| 文件 | 职责 | 变更 |
|---|---|---|
| `core/plan.py` | 计划管理器 + `maybe_clear_completed_plan(session_id=None)` 入口 | Modify |
| `tests/test_plan.py` | 核心函数单测（含两种路由路径） | Modify |
| `gui_bridge.py` | GUI 回合结束钩子 `finish_once` 显式传 session 调用清空并补发空事件 | Modify |
| `tests/test_gui_tool_events.py` | GUI 回合结束清空 + 空事件回归 | Modify |
| `main.py` | CLI `turn_finished` 分支调用清空 + invalidate | Modify |
| `tests/test_main_ui_events.py` | CLI 回合结束清空回归 | Modify |
| `frontend/src/stores/appStore.ts` | `plan_updated` 处理改为 `Array.isArray` | Modify |
| `frontend/src/stores/appStore.plan.test.ts` | 空快照清空 + 非数组守卫前端测试 | Create |

---

## Task 1: 核心入口 `maybe_clear_completed_plan(session_id=None)`

**Files:**
- Modify: `core/plan.py`（在 `get_default_plan_manager` 之后、`bind_default_plan_session` 附近插入）
- Test: `tests/test_plan.py`

**Interfaces:**
- Consumes: `get_default_plan_manager()`、`get_plan_manager_for()`、`TERMINAL_STATUSES`（均在 `core/plan.py` 内已定义）。
- Produces: `maybe_clear_completed_plan(session_id: str | None = None) -> bool`。返回 `True` 表示发生了清空，`False` 表示未清空（无计划 / 仍有 pending/in_progress 项）。GUI/CLI 依赖该返回值决定是否补发事件或刷新。

- [ ] **Step 1: 写失败测试**

在 `tests/test_plan.py` 末尾追加：

```python
def test_maybe_clear_completed_plan_clears_when_all_terminal(tmp_path, monkeypatch):
    """显式 session_id 路由：全部终态时清空并返回 True。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-all-terminal")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
        {"id": "b", "subject": "任务B", "status": "failed"},
    ])
    monkeypatch.setattr(plan, "get_plan_manager_for", lambda session_id, **kwargs: manager)

    assert plan.maybe_clear_completed_plan("clear-all-terminal") is True
    assert manager.snapshot() == []


def test_maybe_clear_completed_plan_keeps_pending(tmp_path, monkeypatch):
    """仍有 pending 项时不清空（跨回合任务不能被提前抹掉）。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-has-pending")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
        {"id": "b", "subject": "任务B", "status": "pending"},
    ])
    monkeypatch.setattr(plan, "get_plan_manager_for", lambda session_id, **kwargs: manager)

    assert plan.maybe_clear_completed_plan("clear-has-pending") is False
    assert len(manager.snapshot()) == 2


def test_maybe_clear_completed_plan_noop_when_empty(tmp_path, monkeypatch):
    """无计划项时返回 False，不误报清空。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-empty")
    monkeypatch.setattr(plan, "get_plan_manager_for", lambda session_id, **kwargs: manager)

    assert plan.maybe_clear_completed_plan("clear-empty") is False
    assert manager.snapshot() == []


def test_maybe_clear_completed_plan_noarg_routes_default_manager(tmp_path, monkeypatch):
    """不传 session_id（CLI 路径）：走 get_default_plan_manager 默认路由。"""
    import core.plan as plan
    manager = plan.PlanManager(storage_dir=tmp_path, session_id="clear-noarg")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "cancelled"},
    ])
    monkeypatch.setattr(plan, "get_default_plan_manager", lambda: manager)

    assert plan.maybe_clear_completed_plan() is True
    assert manager.snapshot() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_plan.py -k maybe_clear_completed_plan -v`
Expected: FAIL（`AttributeError: module 'core.plan' has no attribute 'maybe_clear_completed_plan'`）

- [ ] **Step 3: 最小实现**

在 `core/plan.py` 的 `get_default_plan_manager()` 函数之后插入：

```python
def maybe_clear_completed_plan(session_id: str | None = None) -> bool:
    """回合结束时调用：若指定会话（缺省取当前路由）的计划所有项都进入
    终态（completed/failed/blocked/cancelled），清空计划快照并返回 True
    （发生了清空）；否则返回 False。

    任务完成后计划面板/任务规划自动归零，避免已完成计划长期残留。
    只要还有 pending/in_progress 项（任务未完成）就不清空，保证跨回合任务
    的计划不会被提前抹掉。

    session_id 必须由 GUI 回合结束路径显式传入：finish_once 执行时计划会话
    TLS 路由已解绑（_run_turn_worker finally 前段），get_default_plan_manager()
    会退回进程级单例——多会话并行下该单例可能已被 _set_current_session 重绑
    到其他会话，裸用会清错会话的计划。显式 session_id 走 get_plan_manager_for
    注册表路由（条目在回合内创建后跨回合存活，storage_dir 正确）。
    CLI 单会话顺序执行，不传 session_id 走 get_default_plan_manager() 默认路由。
    """
    if session_id:
        manager = get_plan_manager_for(session_id)
    else:
        manager = get_default_plan_manager()
    items = manager.snapshot()
    if not items:
        return False
    if any(item.get("status") not in TERMINAL_STATUSES for item in items):
        return False
    manager.clear()
    return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_plan.py -v`
Expected: PASS（新增 4 个用例通过，原有 `test_plan.py` 用例不回归）

- [ ] **Step 5: 提交**

```bash
git add core/plan.py tests/test_plan.py
git commit -m "feat(plan): add maybe_clear_completed_plan with explicit session routing"
```

---

## Task 2: GUI 回合结束清空并补发空事件（显式 session 路由）

**Files:**
- Modify: `gui_bridge.py`（`finish_once` 函数体，约 2984–2991 行）
- Test: `tests/test_gui_tool_events.py`

**Interfaces:**
- Consumes: `maybe_clear_completed_plan(session_id)`（Task 1）、`self.emit_event(kind, data)`、`event_session_name`（`finish_once` 闭包外部作用域已定义，等于 `session_name` 或重命名后的值）、`session_name`（闭包捕获的函数参数，所有路径可用——包括无模型早退路径，该路径发生在 `set_current_plan_session` 之前，TLS 从未绑定，正是必须显式传参的原因之一）。
- Produces: 回合结束时若计划被清空，额外 emit 一个 `plan_updated` 事件（`items=[]`、`title="当前任务计划"`、`session_name=event_session_name`），且在 `turn_finish` 之前发出。

**为什么必须显式传 session_name（不许改成裸调）：** `finish_once` 位于 finally 末尾（约 3203 行），此时 TLS 路由已解绑（约 3172-3174 行）且 workdir 作用域已退出（约 3178-3179 行）。裸调 `maybe_clear_completed_plan()` 会命中 `_DEFAULT_PLAN_MANAGER`——多会话并行下它可能已被切换会话重绑（gui_bridge.py `_set_current_session` 约 2251-2252 行），清错会话的计划；其 storage_dir 也按进程启动目录推导，落盘路径错误。显式传 `session_name` 走 `get_plan_manager_for` 注册表：回合内计划工具首次触发 `get_default_plan_manager()` 时已注册本会话条目（storage_dir 按回合线程 workdir 推导），条目跨回合存活，`finish_once` 里取到的是同一个实例。

- [ ] **Step 1: 写失败测试**

在 `tests/test_gui_tool_events.py` 追加，**必须按既有模板 `test_worker_emits_attachment_notes_inline_and_finishes_with_renamed_session`（约 312-373 行）的结构写**：`ApiBridge.__new__(ApiBridge)`（不是不存在的 `GuiBridge`）+ 手动补齐全部实例属性 + **必须替换 `bridge._consume_turn_events`**（否则真实方法会发起 LLM 请求，测试因环境错误而非断言失败）：

```python
def test_turn_finish_emits_empty_plan_updated_when_plan_all_terminal(tmp_path, monkeypatch):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id="sess-plan-cleared")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
    ])
    monkeypatch.setattr(plan, "get_plan_manager_for", lambda session_id, **kwargs: manager)

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._cancel_events = {"sess-plan-cleared": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_lock = threading.Lock()
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    bridge._sync_active_model_context = lambda *_args: None
    bridge._session_workdir = lambda _session_name: "GLOBAL"
    bridge._subagent_progress_poller = lambda _session_name, _stop: None
    bridge._consume_turn_events = lambda *args, **kwargs: False
    bridge._maybe_auto_rename = lambda _session_name: None
    bridge._memory = type("Memory", (), {
        "ensure_session": lambda self, *args, **kwargs: None,
        "get_session_model_id": lambda self, *args: None,
        "set_session_model_id": lambda self, *args: None,
        "add_message": lambda self, *args, **kwargs: None,
        "set_workdir": lambda self, workdir: None,
    })()
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))

    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_args: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr("core.input_parser.parse_user_input", lambda text: (text, []))
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *args, **kwargs: [])
    monkeypatch.setattr("core.memory.begin_turn_checkpoint", lambda *args, **kwargs: None)
    monkeypatch.setattr("core.memory.end_turn_checkpoint", lambda: None)

    bridge._run_turn_worker("检查项目", "sess-plan-cleared")

    # 空 plan_updated 在 turn_finish 之前发出（前端先归零面板再解锁输入框）
    kinds = [kind for kind, _data in emitted]
    assert "plan_updated" in kinds
    assert kinds.index("plan_updated") < kinds.index("turn_finish")
    plan_events = [data for kind, data in emitted if kind == "plan_updated"]
    assert plan_events == [{
        "title": "当前任务计划",
        "items": [],
        "session_name": "检查项目",
    }]
    finishes = [data for kind, data in emitted if kind == "turn_finish"]
    assert finishes == [{"session_name": "检查项目"}]
    assert manager.snapshot() == []
```

> 注：`threading` 与 `ApiBridge` 已在该测试文件顶部导入。`_consume_turn_events` 打桩返回 False 模拟"无工具事件的空回合"，`finish_once` 仍会在 finally 漏斗中执行，覆盖清空路径。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py::test_turn_finish_emits_empty_plan_updated_when_plan_all_terminal -v`
Expected: FAIL（无 `plan_updated` 事件发出 / 计划未被清空）

- [ ] **Step 3: 最小实现**

修改 `gui_bridge.py` 的 `finish_once` 函数体：

```python
        def finish_once():
            """turn_finish 只允许发出一次（历史实现在 end + finally 各发一次）。"""
            nonlocal turn_finished
            if turn_finished:
                return
            turn_finished = True
            from core.plan import maybe_clear_completed_plan
            # 必须显式传 session_name：此处 TLS 计划路由已在 finally 前段解绑，
            # 裸调会退回进程级单例——多会话并行下该单例可能已被切换会话重绑
            # （_set_current_session），清错会话的计划。注册表路由（回合内已
            # 创建本会话条目，跨回合存活）保证取到正确的管理器实例。
            if maybe_clear_completed_plan(session_name):
                self.emit_event("plan_updated", {
                    "title": "当前任务计划",
                    "items": [],
                    "session_name": event_session_name,
                })
            self.emit_event("turn_finish", {"session_name": event_session_name})
            self._emit_token_usage()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py -v`
Expected: PASS（新用例通过，`test_gui_tool_events.py` 既有用例不回归）

- [ ] **Step 5: 提交**

```bash
git add gui_bridge.py tests/test_gui_tool_events.py
git commit -m "feat(gui): clear completed plan with explicit session routing on turn finish"
```

---

## Task 3: CLI 回合结束清空

**Files:**
- Modify: `main.py`（`turn_finished` 分支，约 1181 行）
- Test: `tests/test_main_ui_events.py`

**Interfaces:**
- Consumes: `maybe_clear_completed_plan()`（Task 1，无参走 `get_default_plan_manager()` 默认路由——CLI 单会话顺序执行，`bind_default_plan_session(current_session)` 在回合处理前已绑定，路由正确）。
- Produces: `turn_finished` 事件处理时清空已完成的计划，清空发生时补一次 `app_instance.invalidate()`（CLI 计划面板是拉取式渲染，`get_live_plan_content`（main.py 约 3000-3003 行）每次读取 `get_default_plan_manager().snapshot()`，快照清空后需要一次重绘触发才会显示「暂无计划步骤」；`turn_finished` 分支自身无 invalidate，见 v2 修订说明第 4 条）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_main_ui_events.py` 追加，用该文件既有的 `TurnEventFactory` 模式（不存在 `_event` helper）：

```python
def test_turn_finished_clears_completed_plan(tmp_path, monkeypatch):
    import core.plan as plan

    manager = plan.PlanManager(storage_dir=tmp_path, session_id="cli-clear")
    manager.replace([
        {"id": "a", "subject": "任务A", "status": "completed"},
    ])
    monkeypatch.setattr(plan, "get_default_plan_manager", lambda: manager)

    factory = TurnEventFactory("turn-plan-clear")
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "app_instance", None)
    main.ui_renderers.pop("turn-plan-clear", None)

    main._render_ui_event(factory.emit("turn_finished"))

    assert manager.snapshot() == []
```

> 注：`_render_ui_event` 对未知 turn_id 会自动创建 `TurnRenderer()`（main.py 约 1019-1024 行），无需预置渲染器；`_flush_public_answer` 对缺失缓冲安全（`get(turn_id, "")`）。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main_ui_events.py::test_turn_finished_clears_completed_plan -v`
Expected: FAIL（计划未被清空）

- [ ] **Step 3: 最小实现**

在 `main.py` 的 `if event.kind == "turn_finished":` 分支最前面插入清空调用与 invalidate，其余收尾逻辑保持不变：

```python
    if event.kind == "turn_finished":
        from core.plan import maybe_clear_completed_plan

        if maybe_clear_completed_plan():
            # 计划全部终态已清空：CLI 计划面板是拉取式渲染
            # （get_live_plan_content 每次读取快照），需要一次 invalidate
            # 触发重绘为「暂无计划步骤」；本分支自身无 invalidate
            if app_instance is not None:
                app_instance.invalidate()
        for fragment in renderer.flush_tool_progress():
            _print_ui_fragment(fragment)
        # ... 其余既有收尾逻辑保持不变 ...
        task_status = "idle"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv\Scripts\python.exe -m pytest tests/test_main_ui_events.py -v`
Expected: PASS（新用例通过，`test_main_ui_events.py` 既有用例不回归）

- [ ] **Step 5: 提交**

```bash
git add main.py tests/test_main_ui_events.py
git commit -m "feat(cli): clear completed plan and refresh panel on turn finish"
```

---

## Task 4: 前端 `plan_updated` 载荷防御性加固

**定性（v2 修正）：** JS 中 `[]` 为 truthy，现状 `if (data.items)` 遇空数组**已经**清空面板——GUI 侧空快照清空功能当天即生效（Task 2 补发空事件后）。本任务不是修 bug，而是防御性加固：当 `items` 为非数组真值（如 `items: {}`）时，现状会在 `data.items.map(...)` 抛 TypeError 并中断整个 `handleBridgeEvent` 分发。改为 `Array.isArray` 后非数组载荷被静默忽略，面板保持原状。

**Files:**
- Modify: `frontend/src/stores/appStore.ts`（`plan_updated` 分支，约 2136-2152 行）
- Test: `frontend/src/stores/appStore.plan.test.ts`（Create）

**Interfaces:**
- Consumes: 既有 `handleBridgeEvent` 的 `plan_updated` 事件分支；`PlanData` 类型（`frontend/src/stores/appStore.ts` 已定义 `{ title, items, progress }`）。
- Produces: `plan_updated` 携带 `items: []` 时，`this.planData` 被设为 `{ title, items: [], progress: 0 }`，前端 `RightPanel.vue` 据此显示「暂无活动计划」；`items` 非数组时忽略事件、不抛错、面板保持原状。

- [ ] **Step 1: 写测试（一个特性测试 + 一个失败测试）**

新建 `frontend/src/stores/appStore.plan.test.ts`：

```ts
import { createPinia, setActivePinia } from 'pinia';
import { beforeEach, describe, expect, it } from 'vitest';
import { useAppStore } from './appStore';

beforeEach(() => setActivePinia(createPinia()));

describe('appStore plan panel', () => {
  it('空 items 快照清空计划面板（特性测试：改动前后都必须成立）', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '当前任务计划',
      items: [{ id: 'a', text: '任务A', status: 'done' }] as any,
      progress: 100,
    };

    store.handleBridgeEvent({
      type: 'plan_updated',
      data: { title: '当前任务计划', items: [], session_name: 's1' },
    } as any);

    expect(store.planData.items).toEqual([]);
    expect(store.planData.progress).toBe(0);
  });

  it('items 非数组时忽略事件且不抛错（现状会 .map 抛 TypeError）', () => {
    const store = useAppStore();
    store.currentSessionName = 's1';
    store.planData = {
      title: '当前任务计划',
      items: [{ id: 'a', text: '任务A', status: 'done' }] as any,
      progress: 100,
    };

    expect(() => store.handleBridgeEvent({
      type: 'plan_updated',
      data: { title: '当前任务计划', items: {}, session_name: 's1' },
    } as any)).not.toThrow();

    // 非数组载荷不进入分支，面板保持原状
    expect(store.planData.items).toHaveLength(1);
  });
});
```

- [ ] **Step 2: 跑测试确认只有一个失败**

Run: `npm --prefix frontend test -- appStore.plan.test.ts`
Expected: 用例 1 **直接 PASS**（空数组在现状下已清空面板——这是特性测试，锁定既有正确行为，防止本次改动引入回归）；用例 2 **FAIL**（现状 `if (data.items)` 判 `{}` 为真，进入分支后 `data.items.map` 抛 TypeError，`not.toThrow()` 断言失败）。

- [ ] **Step 3: 最小实现**

修改 `frontend/src/stores/appStore.ts` 的 `plan_updated` 分支（仅把 `if (data.items)` 改为 `if (Array.isArray(data.items))`，其余逻辑不变）：

```ts
      } else if (type === 'plan_updated') {
        if (Array.isArray(data.items)) {
          const items = data.items.map((it: any) => ({
            id: it.id || String(Math.random()),
            // 后端计划项的正文字段是 subject（见 core/plan.py snapshot）
            text: it.subject || it.text || it.title || '',
            status: it.status === 'completed' || it.status === 'done' || it.status === 'x' ? 'done' : (it.status === 'in_progress' || it.status === '/' ? 'in_progress' : 'todo'),
          }));
          const doneCount = items.filter((i: any) => i.status === 'done').length;
          const progress = items.length > 0 ? Math.round((doneCount / items.length) * 100) : 0;
          this.planData = {
            title: data.title || '当前任务计划',
            items,
            progress,
          };
          this._captureRuntime(targetRuntimeKey);
        }
      }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `npm --prefix frontend test`
Expected: PASS（新文件 2 个用例通过，前端全部测试文件不回归）

- [ ] **Step 5: 提交**

```bash
git add frontend/src/stores/appStore.ts frontend/src/stores/appStore.plan.test.ts
git commit -m "fix(frontend): guard plan_updated payload with Array.isArray"
```

---

## 全量回归（提交前必跑）

```bash
# 后端：核心 + GUI + CLI + 既有计划相关 + 多会话隔离（本次改动涉及会话路由，必跑）
.venv\Scripts\python.exe -m pytest tests/test_plan.py tests/test_gui_tool_events.py tests/test_main_ui_events.py tests/test_agent_turn_e2e.py tests/test_session_parallel_isolation.py -v

# 黄金字节回归（本方案不触碰请求构造，仍需确认）
.venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -v

# 前端
npm --prefix frontend test
npm --prefix frontend run build
```

## Self-Review

- **Spec 覆盖**：任务完成后清空计划 → Task 1（核心入口，含显式/默认双路由）+ Task 2（GUI 触发，显式 session 路由修复多会话串扰）+ Task 3（CLI 触发 + 拉取式面板 invalidate）+ Task 4（前端载荷防御加固，空快照清空现状已生效）；GUI 与 CLI 两侧均覆盖。
- **v1 问题闭环**：多会话清错计划（v2 修订 1）→ Task 1 显式 session_id + Task 2 显式传参 + 回归新增 `test_session_parallel_isolation.py`；GuiBridge 不存在 / 测试无法运行（修订 2）→ Task 2 测试按 `ApiBridge.__new__` 既有模板重写并打桩 `_consume_turn_events`；TDD 失败预期不成立（修订 3）→ Task 4 改为特性测试 + 非数组守卫红测；CLI invalidate 猜测（修订 4）→ Task 3 显式补 invalidate 并修正测试为 `TurnEventFactory` 模式。
- **API 名称已核实**（v2 已消除全部「实施时确认」占位）：`ApiBridge`（gui_bridge.py:347）、`_run_turn_worker`、`finish_once`（gui_bridge.py:2984）、`TurnEventFactory`（tests/test_main_ui_events.py:6 导入）、`handleBridgeEvent`（appStore.ts:1899）、`main._render_ui_event`（main.py:1015）。
- **类型一致性**：`maybe_clear_completed_plan(session_id=None) -> bool` 在 Task 1 定义，Task 2 显式传 `session_name`、Task 3 无参调用；前端 `PlanData.items` 结构在 Task 4 与既有 `appStore.ts` 保持一致。
- **语义差异已显式化**：GUI 在全部回合结束路径（正常/取消/异常/早退）清空，CLI 仅 `turn_finished`——取舍理由见 Global Constraints 末条。

---

## Execution Handoff

方案 v2 已修订完成（四处修正均已落实，无「实施时确认」占位）。两种执行方式：
1. **Subagent-Driven（推荐）**：每个 Task 派一个全新 implement subagent，任务间做 review。
2. **Inline Execution**：本会话内按 executing-plans 批量执行，带检查点。

选哪种？
