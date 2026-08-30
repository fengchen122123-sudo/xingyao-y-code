# Configurable Turn Step Limit and Explicit Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把固定 100 Step 改为默认 100、范围 10–1000 的系统设置，并确保达到上限时以 `StepLimitExceeded` 失败闭环，同时清除公开输出中的空消息协议占位符。

**Architecture:** 新模块 `core/harness/turn_limits.py` 独占配置常量、校验、读取和保存；主 Agent 在 Turn 开始时读取一次并把值冻结到局部 `max_turns`。循环耗尽后完整提交最后 Step，再把 Turn 标记为 failed 并发出 `turn_error`；GUI 通过现有可扩展 system settings 快照读写该值。协议占位符统一在 `core.turn_renderer.sanitize_protocol_text()` 清理。

**Tech Stack:** Python 3.12、SQLite/Harness Journal、pytest、Vue 3、Pinia、TypeScript、Vitest。

**Spec:** `docs/superpowers/specs/2026-08-30-configurable-turn-step-limit-and-closure-design.md`

## Global Constraints

- 默认值固定为 100，合法范围固定为闭区间 10–1000，不允许无限模式。
- “轮次”只计算真实 Provider 请求/Harness Step，不计算工具调用次数，Provider 内部重试不增加 Step。
- 每个 Turn 只读取一次配置；运行中修改只影响后续 Turn。
- 达到上限必须是 `failed / StepLimitExceeded`，不得调用 `harness_runtime.complete()`，不得发送 `turn_finished`。
- 最后一个 Step 已完整提交时保持 `completed`，不得回滚工具结果或重放副作用。
- 保持 NoProgressGuard、RepeatCallGuard、计划状态机和取消语义不变。
- 不修改 Provider 请求构造、稳定 prompt、tools 顺序/schema、DeepSeek Prompt Ledger、Kimi/GLM 路由或 token 统计。
- 显式非法配置必须拒绝，不做钳位、取整、自动纠正或静默默认回退。
- 保留工作树中的用户已有修改，只触碰本计划列出的文件；不得批量格式化。
- 不更新 Provider golden/hash 来迁就实现；任何字节变化都必须视为越界并定位。
- 本计划不授权 `git commit`、push、merge 或发布；仅记录建议提交信息，等待用户明确授权。

## File Map

- Create `core/harness/turn_limits.py`: 配置常量、错误类型、严格校验、读取和持久化。
- Create `tests/test_turn_limits.py`: 配置边界与保存行为单元测试。
- Modify `core/command_shell.py`: 扩展现有 system settings 聚合快照。
- Modify `gui_bridge.py`: 暴露严格保存接口。
- Modify `tests/test_gui_system_settings.py`: Bridge 和快照测试。
- Modify `core/llm.py`: Turn 启动时冻结上限；循环耗尽映射为 StepLimitExceeded。
- Modify `tests/test_harness_runtime.py`: 小上限、终态、事件和持久化回归。
- Modify `core/turn_renderer.py`: 删除精确协议占位符。
- Modify `tests/test_turn_renderer.py`: 实时与混合文本清理测试。
- Modify `tests/test_llm_events_fixes.py`: 工具调用消息持久化时不保存公开占位正文。
- Modify `frontend/src/types/pywebview.d.ts`: 扩展快照与 Bridge 类型。
- Modify `frontend/src/stores/appStore.ts`: 默认快照和保存 action。
- Modify `frontend/src/stores/appStore.systemSettings.test.ts`: Store 成功/失败行为。
- Modify `frontend/src/components/SystemSettings.vue`: 整数输入、前端校验和保存交互。
- Create `frontend/src/components/SystemSettings.test.ts`: 设置页组件交互测试。

---

### Task 1: 建立严格的 Turn Step Limit 配置域

**Files:**
- Create: `core/harness/turn_limits.py`
- Create: `tests/test_turn_limits.py`

**Interfaces:**
- Produces: `TURN_STEP_LIMIT_CONFIG_KEY: str`
- Produces: `DEFAULT_TURN_STEP_LIMIT: int`
- Produces: `MIN_TURN_STEP_LIMIT: int`
- Produces: `MAX_TURN_STEP_LIMIT: int`
- Produces: `TurnStepLimitError(ValueError)`
- Produces: `validate_turn_step_limit(value: object) -> int`
- Produces: `load_turn_step_limit() -> int`
- Produces: `save_turn_step_limit(value: object) -> int`

- [ ] **Step 1: 写配置边界失败测试**

创建 `tests/test_turn_limits.py`，覆盖精确契约：

```python
import json

import pytest

import config
from core.harness.turn_limits import (
    DEFAULT_TURN_STEP_LIMIT,
    MAX_TURN_STEP_LIMIT,
    MIN_TURN_STEP_LIMIT,
    TurnStepLimitError,
    load_turn_step_limit,
    save_turn_step_limit,
    validate_turn_step_limit,
)


def _use_tmp_config(tmp_path, monkeypatch, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(path))
    return path


def test_missing_turn_step_limit_uses_legacy_default(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})
    assert load_turn_step_limit() == DEFAULT_TURN_STEP_LIMIT == 100


@pytest.mark.parametrize("value", [MIN_TURN_STEP_LIMIT, 100, MAX_TURN_STEP_LIMIT])
def test_turn_step_limit_accepts_integer_boundaries(value):
    assert validate_turn_step_limit(value) == value


@pytest.mark.parametrize("value", [9, 1001, True, False, 100.0, "100", None])
def test_turn_step_limit_rejects_invalid_explicit_values(value):
    with pytest.raises(TurnStepLimitError):
        validate_turn_step_limit(value)


def test_save_turn_step_limit_persists_validated_integer(tmp_path, monkeypatch):
    path = _use_tmp_config(tmp_path, monkeypatch, {"unrelated": "kept"})
    assert save_turn_step_limit(250) == 250
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "unrelated": "kept",
        "agent_turn_step_limit": 250,
    }


def test_save_turn_step_limit_does_not_write_invalid_value(tmp_path, monkeypatch):
    path = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})
    with pytest.raises(TurnStepLimitError):
        save_turn_step_limit(1001)
    assert json.loads(path.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 100


def test_load_turn_step_limit_rejects_explicit_invalid_config(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": "100"})
    with pytest.raises(TurnStepLimitError):
        load_turn_step_limit()


def test_save_turn_step_limit_reports_atomic_write_failure(mocker):
    mocker.patch("config.save_config_value", return_value=False)
    with pytest.raises(TurnStepLimitError, match="保存失败"):
        save_turn_step_limit(100)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而 RED**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_turn_limits.py -q
```

Expected: collection fails because `core.harness.turn_limits` does not exist.

- [ ] **Step 3: 实现最小严格配置模块**

创建 `core/harness/turn_limits.py`：

```python
"""Strict per-Turn Provider Step limit configuration."""

from __future__ import annotations

import config


TURN_STEP_LIMIT_CONFIG_KEY = "agent_turn_step_limit"
DEFAULT_TURN_STEP_LIMIT = 100
MIN_TURN_STEP_LIMIT = 10
MAX_TURN_STEP_LIMIT = 1000


class TurnStepLimitError(ValueError):
    """Raised when the configured per-Turn Step limit is invalid."""


def validate_turn_step_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TurnStepLimitError("单任务最大模型轮次必须是整数")
    if not MIN_TURN_STEP_LIMIT <= value <= MAX_TURN_STEP_LIMIT:
        raise TurnStepLimitError(
            f"单任务最大模型轮次必须在 {MIN_TURN_STEP_LIMIT}–{MAX_TURN_STEP_LIMIT} 之间"
        )
    return value


def load_turn_step_limit() -> int:
    data = config._load()
    if TURN_STEP_LIMIT_CONFIG_KEY not in data:
        return DEFAULT_TURN_STEP_LIMIT
    return validate_turn_step_limit(data[TURN_STEP_LIMIT_CONFIG_KEY])


def save_turn_step_limit(value: object) -> int:
    resolved = validate_turn_step_limit(value)
    if not config.save_config_value(TURN_STEP_LIMIT_CONFIG_KEY, resolved):
        raise TurnStepLimitError("单任务最大模型轮次保存失败")
    return resolved
```

- [ ] **Step 4: 运行配置测试确认 GREEN**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_turn_limits.py tests/test_config.py tests/test_config_toml_overlay.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: 自审 Task 1 diff**

Run:

```powershell
git diff -- core/harness/turn_limits.py tests/test_turn_limits.py
git diff --check -- core/harness/turn_limits.py tests/test_turn_limits.py
```

Check: no implicit string conversion, no `bool` acceptance, no invalid-value fallback.

Suggested commit message after explicit user authorization: `feat(harness): add strict configurable turn step limit`

---

### Task 2: 把配置接入系统设置后端快照

**Files:**
- Modify: `core/command_shell.py:124-174`
- Modify: `gui_bridge.py:2848-2865`
- Modify: `tests/test_gui_system_settings.py`

**Interfaces:**
- Consumes: `load_turn_step_limit() -> int`
- Consumes: `save_turn_step_limit(value: object) -> int`
- Produces: system settings fields `agent_turn_step_limit`, `agent_turn_step_limit_min`, `agent_turn_step_limit_max`
- Produces: `ApiBridge.set_agent_turn_step_limit(value: int) -> dict[str, Any]`

- [ ] **Step 1: 扩展 Bridge 失败测试**

在 `tests/test_gui_system_settings.py` 增加：

```python
def test_system_settings_snapshot_exposes_turn_step_limit(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 240})

    result = _bare_bridge().get_system_settings()

    assert result["agent_turn_step_limit"] == 240
    assert result["agent_turn_step_limit_min"] == 10
    assert result["agent_turn_step_limit_max"] == 1000


def test_bridge_persists_valid_turn_step_limit(tmp_path, monkeypatch):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})

    result = _bare_bridge().set_agent_turn_step_limit(300)

    assert result["ok"] is True
    assert result["settings"]["agent_turn_step_limit"] == 300
    assert json.loads(cfg.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 300


def test_bridge_rejects_invalid_turn_step_limit_without_changing_config(
    tmp_path, monkeypatch
):
    cfg = _use_tmp_config(tmp_path, monkeypatch, {"agent_turn_step_limit": 100})

    result = _bare_bridge().set_agent_turn_step_limit(1001)

    assert result["ok"] is False
    assert "10" in result["msg"] and "1000" in result["msg"]
    assert json.loads(cfg.read_text(encoding="utf-8"))["agent_turn_step_limit"] == 100
```

- [ ] **Step 2: 运行测试确认缺少快照字段和 Bridge 方法**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py -q
```

Expected: new tests fail because the fields/method do not exist.

- [ ] **Step 3: 扩展 `get_system_settings()`**

在 `core/command_shell.py` 的 `get_system_settings()` 中从 `core.harness.turn_limits` 导入 loader 和边界常量，并把以下键并入现有返回值：

```python
"agent_turn_step_limit": load_turn_step_limit(),
"agent_turn_step_limit_min": MIN_TURN_STEP_LIMIT,
"agent_turn_step_limit_max": MAX_TURN_STEP_LIMIT,
```

不得在这里捕获 `TurnStepLimitError` 并回退 100；显式损坏配置必须暴露。

- [ ] **Step 4: 增加 Bridge setter**

在 `gui_bridge.ApiBridge` 的系统设置区加入：

```python
def set_agent_turn_step_limit(self, value: int) -> Dict[str, Any]:
    from core.harness.turn_limits import TurnStepLimitError, save_turn_step_limit

    try:
        save_turn_step_limit(value)
        return {
            "ok": True,
            "settings": self.get_system_settings(),
            "msg": "单任务最大模型轮次已更新",
        }
    except TurnStepLimitError as error:
        return {
            "ok": False,
            "msg": str(error),
            "settings": self.get_system_settings(),
        }
```

只捕获领域校验错误；未知存储异常不得伪装成保存成功。

- [ ] **Step 5: 运行后端设置测试确认 GREEN**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q
```

Expected: all selected tests pass and initial state includes the extended snapshot.

- [ ] **Step 6: 自审 Task 2 diff**

Run:

```powershell
git diff -- core/command_shell.py gui_bridge.py tests/test_gui_system_settings.py
git diff --check -- core/command_shell.py gui_bridge.py tests/test_gui_system_settings.py
```

Suggested commit message after explicit user authorization: `feat(gui): expose turn step limit in system settings`

---

### Task 3: 修复达到上限后的 Harness 终态

**Files:**
- Modify: `core/llm.py:4451-5194`
- Modify: `tests/test_harness_runtime.py:215-265`

**Interfaces:**
- Consumes: `load_turn_step_limit() -> int`
- Produces: terminal error code `StepLimitExceeded`
- Preserves: one Provider request equals one Harness Step

- [ ] **Step 1: 把现有 100 轮成功测试改写为小上限失败测试**

将 `test_round_limit_message_is_committed_before_the_last_step_completes` 改名为 `test_round_limit_fails_turn_after_committing_the_last_step`，patch 上限为 3，并断言完整终态：

```python
def test_round_limit_fails_turn_after_committing_the_last_step(mocker):
    manager, _cancel_event = _persistent_runtime_fixture(mocker)
    load_limit = mocker.patch(
        "core.harness.turn_limits.load_turn_step_limit",
        return_value=3,
    )
    call_number = 0

    def next_tool_stream(**_kwargs):
        nonlocal call_number
        call_number += 1
        return _tool_stream(
            f"call-{call_number}",
            "write_file",
            {"file_path": f"file-{call_number}.py", "content": "x"},
        )

    mocker.patch("core.llm.completion", side_effect=next_tool_stream)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fixed_tool_result)

    events = list(_persistent_events())

    turn = manager.get_harness_turn("turn-runtime")
    assert call_number == 3
    load_limit.assert_called_once_with()
    assert turn["status"] == "failed"
    assert turn["error_code"] == "StepLimitExceeded"
    steps = _steps(manager, "turn-runtime")
    assert len(steps) == 3
    assert all(step["status"] == "completed" for step in steps)
    kinds = [event.kind for event in events]
    assert kinds.count("turn_error") == 1
    assert "turn_finished" not in kinds
    history = manager.get_history("session")
    assert "3 轮" in str(history[-1]["content"])
    assert "任务未完成" in str(history[-1]["content"])
```

保留原测试的事件顺序断言：最后一个 `tool_result_committed` 和上限 assistant 消息都必须先于最后 Step 的终态 `step_status`。

- [ ] **Step 2: 运行新测试确认当前实现错误地标记 completed**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py::test_round_limit_fails_turn_after_committing_the_last_step -q
```

Expected: FAIL because current code hard编码 100 and calls `harness_runtime.complete()`.

- [ ] **Step 3: 在 Turn 开始时冻结配置**

在 `_chat_with_model_generator()` 进入运行循环前加入局部读取：

```python
from core.harness.turn_limits import load_turn_step_limit

max_turns = load_turn_step_limit()
if harness_runtime is not None:
    harness_runtime.mark_running()
for turn in range(max_turns):
    raise_if_cancelled()
    # 其余现有循环体保持原顺序。
```

删除 `max_turns = 100`。不要在循环内部重新调用 loader，也不要把设置值注入 Provider messages、metadata 或 extra_body。

- [ ] **Step 4: 把循环耗尽出口改为失败闭环**

替换硬编码提示和 `harness_runtime.complete()`：

```python
msg = (
    f"{TOOL_ERROR_PREFIX} 单任务已达到模型轮次上限（{max_turns} 轮），任务未完成。"
    "已保留本轮代码、工具结果和测试记录；请发送“继续”开启新回合，"
    "或在系统设置中调整上限。"
)
limit_message = {"role": "assistant", "content": msg}
messages.append(limit_message)
if stream:
    persisted_limit_message = deepcopy(limit_message)
    persisted_limit_message.update(_model_display_snapshot())
    pending_persistence.append(("assistant", persisted_limit_message))
flush_persistence()
if current_step is not None:
    current_step.complete()
if harness_runtime is not None:
    harness_runtime.fail("StepLimitExceeded")
_notify_event(event_callback, "error", {"message": msg})
_notify_event(event_callback, "turn_error", {"message": msg})
yield from _yield_content(msg)
return
```

顺序不可调整：消息提交依赖当前 Step；Step 必须先完成，Turn 才能进入 failed。

- [ ] **Step 5: 运行 Harness 定向测试确认 GREEN**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py -q
```

Expected: round-limit、NoProgressError、取消、Provider 错误与持久化测试全部通过。

- [ ] **Step 6: 运行 LLM 事件回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_main_ui_events.py -q
```

Expected: exactly one terminal failure event for StepLimitExceeded; no `turn_finished` after failure.

- [ ] **Step 7: 自审 Task 3 diff**

Run:

```powershell
git diff -- core/llm.py tests/test_harness_runtime.py
git diff --check -- core/llm.py tests/test_harness_runtime.py
```

Check: loader called once, no `max_turns = 100`, limit path contains `fail("StepLimitExceeded")`, no automatic continuation.

Suggested commit message after explicit user authorization: `fix(harness): fail turns explicitly at configured step limit`

---

### Task 4: 清除空 assistant 协议占位符

**Files:**
- Modify: `core/turn_renderer.py:72-85`
- Modify: `tests/test_turn_renderer.py:204-214`
- Modify: `tests/test_llm_events_fixes.py`

**Interfaces:**
- Consumes/Produces: `sanitize_protocol_text(text: str) -> str`
- Preserves: assistant tool_calls、reasoning_content、thinking_blocks

- [ ] **Step 1: 写精确清理失败测试**

在 `tests/test_turn_renderer.py` 增加：

```python
from core.turn_renderer import sanitize_protocol_text


def test_sanitize_protocol_text_removes_empty_content_protocol_marker():
    marker = "[System: Empty message content sanitised to satisfy protocol]"
    assert sanitize_protocol_text(marker) == ""
    assert sanitize_protocol_text(f"前文 {marker} 后文") == "前文  后文"
    assert sanitize_protocol_text("普通 System 说明") == "普通 System 说明"
```

在 `tests/test_llm_events_fixes.py` 增加持久化 tool-call stream 回归，同时检查内存 canonical 消息和数据库历史：

```python
def test_protocol_empty_content_marker_is_not_persisted_or_rendered(
    tmp_path, mocker
):
    from core.memory import MemoryManager

    marker = "[System: Empty message content sanitised to satisfy protocol]"
    manager = MemoryManager(str(tmp_path / "runtime.db"))
    manager.create_session_explicit("session")
    mocker.patch("core.app_context.get_manager", return_value=manager)
    responses = [
        iter([_chunk(
            marker,
            tool_calls=[_tool_call(
                0,
                "read-1",
                "read_file",
                '{"file_path":"x.py"}',
            )],
        )]),
        iter([_chunk("检查完成。")]),
    ]
    mocker.patch("core.llm.completion", side_effect=responses)
    mocker.patch(
        "core.llm._execute_single_tool_call",
        return_value={
            "role": "tool",
            "tool_call_id": "read-1",
            "name": "read_file",
            "content": "ok",
        },
    )
    messages = [{"role": "user", "content": "检查"}]

    list(chat_with_model_events(
        "fake-model",
        "fake-key",
        messages,
        persist=True,
        parent_session="session",
        turn_id="turn-protocol-marker",
        consume_background_notifications=False,
    ))

    tool_request = next(
        message
        for message in messages
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert tool_request["content"] is None
    assert tool_request["tool_calls"][0]["id"] == "read-1"
    persisted_tool_request = next(
        message
        for message in manager.get_history("session")
        if message["role"] == "assistant" and message.get("tool_calls")
    )
    assert persisted_tool_request["content"] is None
    assert persisted_tool_request["tool_calls"][0]["id"] == "read-1"
    assert marker not in str(manager.get_history("session"))
```

- [ ] **Step 2: 运行测试确认 marker 当前仍泄漏**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_turn_renderer.py -q
```

Expected: new sanitizer test fails because current function does not remove the marker.

- [ ] **Step 3: 在统一 sanitizer 删除精确 marker**

在 `sanitize_protocol_text()` 中增加固定常量和精确替换：

```python
_EMPTY_CONTENT_PROTOCOL_MARKER = (
    "[System: Empty message content sanitised to satisfy protocol]"
)


def sanitize_protocol_text(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    text = text.replace(_EMPTY_CONTENT_PROTOCOL_MARKER, "")
    text = re.sub(r'<tool_call[^>]*>.*?</tool_call>', '', text, flags=re.DOTALL)
    text = re.sub(r'</?tool_call[^>]*>', '', text)
    text = re.sub(r'\[Advisor consultation[^\]]*\]', '', text)
    text = re.sub(r'\[Advisor review\]', '', text)
    text = re.sub(r'\[End of advisor consultation[^\]]*\]', '', text)
    text = re.sub(r'\(\s*advisor[^\)]*\)', '', text, flags=re.IGNORECASE)
    return text
```

不要用宽泛的 `\[System:.*\]` 正则，避免删除用户正常正文。

- [ ] **Step 4: 运行 renderer、history 与 LLM 事件测试确认 GREEN**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_turn_renderer.py tests/test_history_renderer.py tests/test_llm_events_fixes.py -q
```

Expected: marker 不在公开/历史文本中，tool calls 和 reasoning 字段保持原样。

- [ ] **Step 5: 自审 Task 4 diff**

Run:

```powershell
git diff -- core/turn_renderer.py tests/test_turn_renderer.py tests/test_llm_events_fixes.py
git diff --check -- core/turn_renderer.py tests/test_turn_renderer.py tests/test_llm_events_fixes.py
```

Suggested commit message after explicit user authorization: `fix(rendering): hide empty assistant protocol marker`

---

### Task 5: 增加 GUI 数字设置与前端类型

**Files:**
- Modify: `frontend/src/types/pywebview.d.ts:93-101,370-373`
- Modify: `frontend/src/stores/appStore.ts:295-302,1371-1421`
- Modify: `frontend/src/stores/appStore.systemSettings.test.ts`
- Modify: `frontend/src/components/SystemSettings.vue`
- Create: `frontend/src/components/SystemSettings.test.ts`

**Interfaces:**
- Consumes: system settings value/min/max snapshot
- Consumes: `set_agent_turn_step_limit(value: number)` Bridge method
- Produces: `store.setAgentTurnStepLimit(value: number): Promise<boolean>`

- [ ] **Step 1: 扩展 TypeScript 接口并先写 Store 失败测试**

给 `SystemSettingsSnapshot` 增加三个 `number` 字段，并给 `PywebviewApi` 增加：

```typescript
set_agent_turn_step_limit(
  value: number,
): Promise<{ ok: boolean; settings: SystemSettingsSnapshot; msg?: string }>;
```

扩展 `appStore.systemSettings.test.ts` 的 `settings` fixture，并增加：

```typescript
it('persists a turn step limit and applies the returned snapshot', async () => {
  const next = structuredClone(settings);
  next.agent_turn_step_limit = 300;
  const setLimit = vi.fn().mockResolvedValue({ ok: true, settings: next });
  (window as any).pywebview = { api: { set_agent_turn_step_limit: setLimit } };
  const store = useAppStore();
  store.systemSettings = structuredClone(settings);

  const ok = await store.setAgentTurnStepLimit(300);

  expect(ok).toBe(true);
  expect(setLimit).toHaveBeenCalledWith(300);
  expect(store.systemSettings.agent_turn_step_limit).toBe(300);
});

it('restores the previous snapshot when saving the limit fails', async () => {
  const setLimit = vi.fn().mockResolvedValue({ ok: false, settings, msg: '范围错误' });
  (window as any).pywebview = { api: { set_agent_turn_step_limit: setLimit } };
  const store = useAppStore();
  store.systemSettings = structuredClone(settings);

  const ok = await store.setAgentTurnStepLimit(1001);

  expect(ok).toBe(false);
  expect(store.systemSettings.agent_turn_step_limit).toBe(100);
});
```

- [ ] **Step 2: 运行 Store 测试确认 action 缺失**

Run:

```powershell
Set-Location frontend
npm test -- src/stores/appStore.systemSettings.test.ts
```

Expected: FAIL because `setAgentTurnStepLimit` and snapshot fields do not exist.

- [ ] **Step 3: 实现 Store 默认值和保存 action**

默认快照增加：

```typescript
agent_turn_step_limit: 100,
agent_turn_step_limit_min: 10,
agent_turn_step_limit_max: 1000,
```

新增 action，成功应用后端完整快照；失败恢复 `result.settings || previous`：

```typescript
async setAgentTurnStepLimit(value: number): Promise<boolean> {
  const previous = this.systemSettings;
  const api = this._api();
  if (!api) return false;
  try {
    const result = await api.set_agent_turn_step_limit(value);
    if (result && result.ok) {
      this.systemSettings = result.settings;
      return true;
    }
    this.systemSettings = result?.settings || previous;
    this.notifyModal(
      '系统设置',
      '单任务最大模型轮次设置失败: ' + (result?.msg || '未知错误'),
    );
    return false;
  } catch (err) {
    this.systemSettings = previous;
    this.connected = false;
    this.notifyModal('系统设置', '单任务最大模型轮次设置失败: ' + err);
    return false;
  }
},
```

- [ ] **Step 4: 为 SystemSettings 组件先写交互失败测试**

创建 `frontend/src/components/SystemSettings.test.ts`，使用完整 fixture 和挂载初始化：

```typescript
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import SystemSettings from './SystemSettings.vue';
import { useAppStore } from '../stores/appStore';


const settings = {
  command_shell: 'auto',
  command_shell_options: [],
  command_shell_available: true,
  command_shell_error: '',
  remote_token_ttl: 86400,
  agent_turn_step_limit: 100,
  agent_turn_step_limit_min: 10,
  agent_turn_step_limit_max: 1000,
};


beforeEach(() => {
  setActivePinia(createPinia());
});


it('saves a valid custom turn step limit', async () => {
  const store = useAppStore();
  store.systemSettings = structuredClone(settings);
  const save = vi.spyOn(store, 'setAgentTurnStepLimit').mockResolvedValue(true);
  const wrapper = mount(SystemSettings);

  await wrapper.get('[data-test="turn-step-limit-input"]').setValue('300');
  await wrapper.get('[data-test="save-turn-step-limit"]').trigger('click');

  expect(save).toHaveBeenCalledWith(300);
});

it('does not call the backend for an out-of-range value', async () => {
  const store = useAppStore();
  store.systemSettings = structuredClone(settings);
  const save = vi.spyOn(store, 'setAgentTurnStepLimit').mockResolvedValue(true);
  const wrapper = mount(SystemSettings);

  await wrapper.get('[data-test="turn-step-limit-input"]').setValue('1001');

  expect(wrapper.get('[data-test="turn-step-limit-error"]').text()).toContain('10–1000');
  expect(wrapper.get('[data-test="save-turn-step-limit"]').attributes('disabled')).toBeDefined();
  expect(save).not.toHaveBeenCalled();
});
```

测试 fixture 必须包含后端返回的 value/min/max，不能在断言中复制另一套隐藏边界。

- [ ] **Step 5: 实现数字输入和显式保存**

在 `SystemSettings.vue` 增加第三个 setting row：

```vue
<input
  data-test="turn-step-limit-input"
  v-model="turnStepLimitDraft"
  class="setting-number"
  type="number"
  step="1"
  :min="store.systemSettings.agent_turn_step_limit_min"
  :max="store.systemSettings.agent_turn_step_limit_max"
  :disabled="saving"
/>
<button
  data-test="save-turn-step-limit"
  type="button"
  :disabled="saving || !!turnStepLimitError || !turnStepLimitChanged"
  @click="saveTurnStepLimit"
>
  保存
</button>
```

把 Vue import 扩展为 `computed, ref, watch`，并使用下面的确定性草稿逻辑；不得 `Math.round()` 或 clamp：

```typescript
const turnStepLimitDraft = ref(String(store.systemSettings.agent_turn_step_limit));

watch(
  () => store.systemSettings.agent_turn_step_limit,
  value => { turnStepLimitDraft.value = String(value); },
);

const parsedTurnStepLimit = computed(() => Number(turnStepLimitDraft.value));
const turnStepLimitError = computed(() => {
  const value = parsedTurnStepLimit.value;
  const min = store.systemSettings.agent_turn_step_limit_min;
  const max = store.systemSettings.agent_turn_step_limit_max;
  if (!turnStepLimitDraft.value.trim() || !Number.isInteger(value)) {
    return '请输入整数';
  }
  if (value < min || value > max) {
    return `请输入 ${min}–${max} 之间的整数`;
  }
  return '';
});
const turnStepLimitChanged = computed(
  () => !turnStepLimitError.value
    && parsedTurnStepLimit.value !== store.systemSettings.agent_turn_step_limit,
);

async function saveTurnStepLimit() {
  if (saving.value || turnStepLimitError.value || !turnStepLimitChanged.value) return;
  saving.value = true;
  try {
    const saved = await store.setAgentTurnStepLimit(parsedTurnStepLimit.value);
    if (!saved) {
      turnStepLimitDraft.value = String(store.systemSettings.agent_turn_step_limit);
    }
  } finally {
    saving.value = false;
  }
}
```

- [ ] **Step 6: 运行前端定向测试确认 GREEN**

Run:

```powershell
Set-Location frontend
npm test -- src/stores/appStore.systemSettings.test.ts src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts
```

Expected: all selected tests pass.

- [ ] **Step 7: 运行类型检查和生产构建**

Run:

```powershell
Set-Location frontend
npm run build
```

Expected: `vue-tsc -b` and Vite build exit 0.

- [ ] **Step 8: 自审 Task 5 diff**

Run from repository root:

```powershell
git diff -- frontend/src/types/pywebview.d.ts frontend/src/stores/appStore.ts frontend/src/stores/appStore.systemSettings.test.ts frontend/src/components/SystemSettings.vue frontend/src/components/SystemSettings.test.ts
git diff --check -- frontend/src
```

Suggested commit message after explicit user authorization: `feat(gui): configure per-turn model step limit`

---

### Task 6: 执行完整稳定性门禁并形成可提交结论

**Files:**
- Modify only if a test exposes an in-scope defect in Tasks 1–5.
- Do not update golden/hash expectations to make a failure disappear.

**Interfaces:**
- Verifies all interfaces and invariants introduced by Tasks 1–5.

- [ ] **Step 1: 运行 Python 语法检查**

Run:

```powershell
venv\Scripts\python.exe -m py_compile core\harness\turn_limits.py core\command_shell.py core\llm.py core\turn_renderer.py gui_bridge.py
```

Expected: exit 0 with no output.

- [ ] **Step 2: 运行本修复高风险定向测试**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_turn_limits.py tests/test_gui_system_settings.py tests/test_harness_runtime.py tests/test_turn_renderer.py tests/test_history_renderer.py tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_main_ui_events.py -q
```

Expected: all selected tests pass.

- [ ] **Step 3: 运行 Provider 字节与 Token 稳定性门禁**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py tests/test_cache_keepalive.py -q
```

Expected: all selected tests pass with no golden changes.

- [ ] **Step 4: 运行完整 Python 测试**

Run:

```powershell
venv\Scripts\python.exe -m pytest -q
```

Expected: exit 0. Record the exact passed/skipped count and duration.

- [ ] **Step 5: 运行完整前端测试和生产构建**

Run:

```powershell
Set-Location frontend
npm test
npm run build
```

Expected: both commands exit 0. Return to repository root afterward.

- [ ] **Step 6: 检查变更边界和工作树**

Run:

```powershell
git diff --check
git status --short --branch
git diff --stat
```

Expected:

- branch remains `dev`;
- no whitespace errors;
- no unexpected generated files;
- no Provider golden/hash modifications;
- all unrelated pre-existing user changes remain intact.

- [ ] **Step 7: 形成最终交付报告并停止**

报告必须包含：

- 配置键、默认值、范围和 Turn 冻结语义；
- `StepLimitExceeded` 的数据库、Step、事件和 GUI 证据；
- 协议占位符实时/历史清理证据；
- 每个 Task 的 RED → GREEN 命令和结果；
- 完整 pytest、前端测试、构建和字节稳定性结果；
- 变更文件列表；
- 剩余风险或未完成项；
- 是否达到“当前可以提交”。

达到条件后停止，不开展新一轮开放式找 bug，不执行 commit。

Suggested commit message after explicit user authorization: `fix(harness): make turn limits configurable and fail closed`

## Execution Record

执行者必须在实施过程中把每条命令的真实退出码、测试数量、持续时间和必要诊断追加到本节。未经实际运行不得填写通过结论。

| Task | RED evidence | GREEN evidence | Status |
|---|---|---|---|
| Task 1 | `venv\Scripts\python.exe -m pytest tests/test_turn_limits.py -q` → collection error：`ModuleNotFoundError: No module named 'core.harness.turn_limits'`，1 error in 0.09s（预期：模块尚未创建） | `venv\Scripts\python.exe -m pytest tests/test_turn_limits.py tests/test_config.py tests/test_config_toml_overlay.py -q` → 50 passed in 1.20s，exit 0；`git diff --check` 干净 | done |
| Task 2 | `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py -q` → 3 failed（`KeyError: 'agent_turn_step_limit'`；`AttributeError: 'ApiBridge' object has no attribute 'set_agent_turn_step_limit'` ×2），3 passed in 0.50s，exit 1 | `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q` → 11 passed in 0.60s，exit 0；快照 diff 纯新增（+8/+18/+32），`git diff --check` 干净 | done |
| Task 3 | `venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py::test_round_limit_fails_turn_after_committing_the_last_step -q` → `assert 100 == 3`（硬编码 100 轮、loader 未被调用），1 failed in 2.61s，exit 1 | `venv\Scripts\python.exe -m pytest tests/test_harness_runtime.py -q` → 14 passed in 19.80s，exit 0；`venv\Scripts\python.exe -m pytest tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_main_ui_events.py -q` → 193 passed in 184.67s，exit 0；自审：无 `max_turns = 100`，loader 每 Turn 仅调一次，出口为 `fail("StepLimitExceeded")` + error + turn_error，`git diff --check` 干净 | done |
| Task 4 | `venv\Scripts\python.exe -m pytest tests/test_turn_renderer.py tests/test_llm_events_fixes.py::test_protocol_empty_content_marker_is_not_persisted_or_rendered -q` → 2 failed（sanitizer 原样返回 marker；marker 泄漏进 canonical assistant content），14 passed in 2.74s，exit 1 | `venv\Scripts\python.exe -m pytest tests/test_turn_renderer.py tests/test_history_renderer.py tests/test_llm_events_fixes.py -q` → 105 passed in 125.96s，exit 0；快照 diff 纯新增（+7/+8/+60），`git diff --check` 干净 | done |
| Task 5 | Store：`npm test -- src/stores/appStore.systemSettings.test.ts` → `TypeError: store.setAgentTurnStepLimit is not a function`，2 failed 2 passed，Duration 1.02s，exit 1；组件：`npm test -- src/components/SystemSettings.test.ts` → `Unable to get [data-test="turn-step-limit-input"]`，2 failed，Duration 0.83s，exit 1；修复轮 1（自审发现）：`type="number"` 的 v-model 自动转 number 导致 `.trim()` 崩溃 → 用 `String(...)` 统一校验后通过 | `npm test -- src/stores/appStore.systemSettings.test.ts src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts` → 3 files / 22 passed，Duration 1.69s，exit 0；`npm run build`（vue-tsc -b && vite build）→ built in 433ms，exit 0；`git diff --check -- frontend/src` 干净 | done |
| Task 6 | 不适用 | Step 1 `py_compile`（turn_limits/command_shell/llm/turn_renderer/gui_bridge）exit 0 无输出；Step 2 高风险定向 8 文件 256 passed in 188.88s exit 0；Step 3 字节/Token 稳定性（prompt_byte_stability + deepseek_prompt_ledger + token_accounting + cache_keepalive）52 passed in 2.58s exit 0，无 golden 变更；Step 4 完整 `pytest -q` 1736 passed, 2 skipped in 226.72s exit 0；Step 5 前端完整 `npm test` 57 files / 387 passed in 23.09s exit 0，`npm run build`（vue-tsc + vite）built in 398ms exit 0；Step 6 `git diff --check` exit 0，分支保持 dev（ahead 6），无意外生成文件，无 golden/hash 修改，用户既有修改完好。修复轮：SystemSettings.test.ts 在 HEAD 已存在（计划标注 Create 有误），初版误整体覆盖，已恢复原 3 个测试并合并新 2 个测试（diff vs HEAD 纯新增 +49），恢复后前端全套与构建重跑通过 | done |
