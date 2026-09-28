# 任务先拆解与计划编排实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让星瑶默认"先拆解后执行"：子任务超过 3 条必须先用 `plan_write` 建立计划、逐条执行；并增加运行时兜底——多步实质操作仍未建计划时注入一次软提醒。

**Architecture:** 三条线——① 提示词规则文本（两份 md + `core/context_builder.py` 决策规则）；② 运行时兜底（`core/llm.py` 驱动循环 + `core/injection_markers.py` 前缀登记）；③ 测试守护与验收。计划机制本体零改动。

**Tech Stack:** Python / pytest + pytest-mock / PowerShell (pwsh)

**Spec:** `docs/superpowers/specs/2026-09-28-task-first-orchestration-design.md`

## Global Constraints

- 所有逐字文案以本计划的 target/replacement 为准；编辑前先 `read_file` 确认目标文本（行号为 2026-09-28 快照，会漂移，以文本匹配为准）。
- 双版提示词（`system_prompt.md` / `system_prompt_gui.md`）必须同步修改。
- 计划机制本体（`core/plan.py`、门禁 `_plan_constraint`、收尾续推、执行租约）与权限体系零改动。
- golden 重算必须与文本变更同一次提交完成，不留下"测试红"的中间态。
- 测试命令在工作目录 `D:\fengchenhao\ycode\xingyao-y-code` 下执行；plan 全局态用例必须 `bind_default_plan_session` + `try/finally` 恢复。
- 运行环境为 Windows PowerShell，命令串联用 `;`。

---

### Task 1: 提示词 md 规则文本 + 条款守护

**Files:**
- Modify: `system_prompt_gui.md`（工作流 §2 / 计划编排标题 / §1 首句）
- Modify: `system_prompt.md`（同上三处）
- Test: `tests/test_prompt_behavior_clauses.py`

**Interfaces:**
- Consumes: 无
- Produces: 新条款锚点 `"2. 先拆解后执行：除闲聊、纯问答外的请求"`、`"1. 子任务超过 3 条的任务，必须先用"`（两份 md 中均需存在）

- [ ] **Step 1:【红】先更新守护测试**

编辑 `tests/test_prompt_behavior_clauses.py`：

a) `CLAUSE_ANCHORS` 列表末尾（`"4. 发往外部服务的内容等于发布",          # I 安全边界 4` 之后）追加：

```python
    "2. 先拆解后执行：除闲聊、纯问答外的请求",      # J 工作流 2（编排前置）
    "1. 子任务超过 3 条的任务，必须先用",            # K 计划编排 1（新判据）
```

b) `test_clauses_land_in_expected_sections` 末尾（`assert section_of("4. 发往外部服务的内容等于发布") == "安全边界"` 之后）追加：

```python
    assert section_of("2. 先拆解后执行") == "工作流"
    assert section_of("1. 子任务超过 3 条的任务") == "计划编排（子任务超过 3 条）"
```

c) 模块 docstring 更新为：

```python
"""T2 行为条款就位守护（提示词工程与信源防护升级）。

行为条款（A 证据为本 / B 信源分级 / C 可读性优先 / D 自主执行 / E 收尾检查 /
F 被拒语义 / G 子代理防伪 / H 删改前看目标 / I 外部发布 / J 先拆解后执行 /
K 计划编排判据）必须同时存在于 GUI 与 CLI 两份系统提示词中。本文件把
「条款就位」变成常驻可验证断言，防止后续编辑误删或错位。
```

- [ ] **Step 2:【红】跑测试确认红灯**

Run: `python -m pytest tests/test_prompt_behavior_clauses.py -q`
Expected: FAIL —— `test_all_behavior_clauses_present` 的 missing 列表含两条新锚点；`test_clauses_land_in_expected_sections` 两条新断言失败（条款尚不存在）。

- [ ] **Step 3:【绿】改两份 md（共 6 处 edit）**

对 `system_prompt_gui.md` 与 `system_prompt.md` 各执行以下三处替换（用 edit_file，先读确认原文）：

3a. 工作流 §2（约 :98）
target：
`2. 复杂任务（3+ 文件、50+ 行改动、架构调整）先列简短可执行计划再按依赖顺序执行；小修小改（笔误、配置）直接做。`
replacement：
`2. 先拆解后执行：除闲聊、纯问答外的请求，先拆解任务（内部完成，无需单独展示）；子任务超过 3 条必须先用 `plan_write` 建立计划、按依赖逐条执行；3 条及以内直接执行。`

3b. 计划编排标题（约 :131）
target：`# 计划编排（复杂任务）`
replacement：`# 计划编排（子任务超过 3 条）`

3c. 计划编排 §1 首句（约 :133，只替换首句片段，余文不动）
target：`1. 复杂任务必须先用 `plan_write` 建立可执行计划：`
replacement：`1. 子任务超过 3 条的任务，必须先用 `plan_write` 建立可执行计划：`

- [ ] **Step 4:【绿】跑测试确认通过 + 邻近回归**

Run: `python -m pytest tests/test_prompt_behavior_clauses.py tests/test_context_builder.py -q`
Expected: PASS（`test_context_builder.py` 一并回归，确认提示词契约测试无连带失败）。

- [ ] **Step 5: 提交**

```powershell
git add system_prompt.md system_prompt_gui.md tests/test_prompt_behavior_clauses.py
git commit -m "feat(prompt): 规则改为先拆解后执行，子任务超 3 条先建计划（含条款守护）"
```

---

### Task 2: 决策规则模板改文 + golden 重算 + 断言同步

**Files:**
- Modify: `core/context_builder.py`（`_DECISION_POLICY` §1、§7）
- Update: `tests/test_prompt_byte_stability.py`（2 个 golden）
- Update: `tests/test_context_injection.py`（1 个 golden 复核常量）
- Update: `tests/test_context_builder.py`（1 条断言）
- Create（临时，用完即弃）: `C:\Users\冯晨昊\.ycode\tmp\recalc_prompt_golden.py`

**Interfaces:**
- Consumes: 无
- Produces: 新 golden 值 P1（`build_stable_prompt`）、P2（`build_stable_system_message`）——由 Step 3 脚本实测产出

- [ ] **Step 1: 改 `_DECISION_POLICY`（§1、§7）**

`core/context_builder.py` 中两处替换：

1a. §1
target：
`1. 明确且简单的任务只做一次判断，然后立即执行，不为已经清楚的目标增加额外流程。`
replacement：
`1. 任务先拆解再动手；子任务不超过 3 条的直接执行（只做一次判断，不为已经清楚的目标增加额外流程）；超过 3 条的先建立计划再逐条执行。`

1b. §7
target：`7. 复杂任务先创建包含依赖、角色、文件范围、阶段、verification_type 和验证内容的计划`
replacement：`7. 子任务超过 3 条的任务先创建包含依赖、角色、文件范围、阶段、verification_type 和验证内容的计划`

- [ ] **Step 2:【红】跑测试确认红灯（4 处）**

Run: `python -m pytest tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_context_builder.py -q`
Expected: FAIL ——
1. `test_stable_prompt_has_fixed_sha256`：digest ≠ `626f1ab4…`
2. `test_stable_system_message_has_fixed_sha256`：digest ≠ `ef286e51…`
3. `test_context_prefix_section_matches_golden_digest`：≠ `626f1ab4…`
4. `test_context_builder_adds_decisive_action_and_lazy_skill_policy`：`"明确且简单的任务只做一次判断，然后立即执行" not in prompt`
其余用例应照常 PASS。

- [ ] **Step 3: 重算 golden（脚本）**

创建 `C:\Users\冯晨昊\.ycode\tmp\recalc_prompt_golden.py`：

```python
"""重算提示词 golden（与 tests/test_prompt_byte_stability.py 同口径）。用完即弃。"""

import hashlib
import json

import core.agent_service as agent_service
import core.memory_kairos as memory_kairos
import core.skills as skills
import core.subagent_registry as subagent_registry

agent_service.load_system_prompt = lambda is_gui=False: "base"
memory_kairos.build_memory_system_section = lambda: "memory"
skills.get_active_skills_catalog = lambda: "skills"
subagent_registry.get_custom_subagents_catalog = lambda: "subagents"

from core.context_builder import build_stable_prompt


def _bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ).encode("utf-8")


p1 = hashlib.sha256(
    _bytes(build_stable_prompt(agent_service.load_system_prompt()))
).hexdigest()
p2 = hashlib.sha256(_bytes(agent_service.build_stable_system_message())).hexdigest()
print("stable_prompt        :", p1)
print("stable_system_message:", p2)
```

Run: `python C:\Users\冯晨昊\.ycode\tmp\recalc_prompt_golden.py`
Expected: 打印两个新 sha256；与 Step 2 失败输出里的 actual 值交叉核对（应一致，否则排查 mock 口径）。

- [ ] **Step 4: 更新常量与断言**

4a. `tests/test_prompt_byte_stability.py`：把 `626f1ab481c68e14aa4a0d0fd356e5f61c94121c6b8b7d64e0f92830c1a26c9a` 替换为 P1；把 `ef286e513e9a5275cb79ecd48dd81da343f086c54575c921550985309e83c1e5` 替换为 P2。

4b. `tests/test_context_injection.py`：`_STABLE_PROMPT_GOLDEN_SHA256` 的值替换为 P1（与 4a 同值，独立复核口径）。

4c. `tests/test_context_builder.py`：
target：`    assert "明确且简单的任务只做一次判断，然后立即执行" in prompt`
replacement：`    assert "任务先拆解再动手" in prompt`

- [ ] **Step 5:【绿】复跑确认通过**

Run: `python -m pytest tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_context_builder.py -q`
Expected: PASS（全绿）。

- [ ] **Step 6: 提交**

```powershell
git add core/context_builder.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_context_builder.py
git commit -m "feat(prompt): 决策规则同步新编排判据；提示词 golden 重算"
```

---

### Task 3: 运行时兜底（编排提醒）+ 注入前缀登记

**Files:**
- Modify: `core/llm.py`（常量 + 2 个辅助函数 + 驱动循环接线 + per-turn 变量）
- Modify: `core/injection_markers.py`（`SYSTEM_INJECTED_USER_PREFIXES` 登记）
- Create: `tests/test_orchestration_nudge.py`
- Update: `tests/test_injection_markers.py`

**Interfaces:**
- Consumes（均已存在，勿重复引入）：
  - `core/llm.py` 命名空间中的 `_has_pending_plan` / `_is_plan_turn`（来自 `core.permission_terminal`，定义见 `core/permission_terminal.py:180-196`）
  - `core/llm.py:31` 已导入的 `classify_command_risk`
  - 驱动循环作用域内可用的 `tool_calls_list` / `messages` / `pending_persistence` / `flush_persistence` / `progress_guard` / `_step_progress_observation` / `_notify_event` / `allow_subagents` / `parent_session` / `parent_turn_id`
- Produces（供测试 import）：
  - `llm._ORCHESTRATION_NUDGE_THRESHOLD`（= 3）
  - `llm._ORCHESTRATION_NUDGE_TEXT`（str，见 Step 3a）
  - `llm._count_substantive_ops(tool_calls) -> int`
  - `llm._should_nudge_orchestration(*, allow_subagents, count, session_id, turn_id, already_sent) -> bool`

- [ ] **Step 1:【红】写测试文件 `tests/test_orchestration_nudge.py`（完整新建）**

```python
"""编排提醒（多步未建计划）行为守护：触发、阈值、豁免与去重。"""

import json
from types import SimpleNamespace

import core.llm as llm
from core.llm import chat_with_model_events

NUDGE_PREFIX = "本回合已连续执行多步操作，仍未建立任务计划"


def _chunk(content="", tool_calls=None, reasoning=""):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _tool_call(index, call_id, name, arguments):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _write_calls(batch, count):
    return [
        _tool_call(
            i,
            f"{batch}-{i}",
            "write_file",
            json.dumps({"file_path": f"{batch}-{i}.txt", "content": "x"}),
        )
        for i in range(count)
    ]


def _fake_execute(tool_call, **_kwargs):
    return {
        "role": "tool",
        "tool_call_id": tool_call["id"],
        "name": tool_call["function"]["name"],
        "content": "ok",
    }


def _run(mocker, messages, responses, **kwargs):
    mocker.patch("core.llm.completion", side_effect=responses)
    mocker.patch("core.llm._execute_single_tool_call", side_effect=_fake_execute)
    return list(
        chat_with_model_events(
            "fake-model",
            "fake-key",
            messages,
            persist=False,
            turn_id=kwargs.pop("turn_id", "turn-orch-nudge"),
            consume_background_notifications=False,
            **kwargs,
        )
    )


def _nudges(messages):
    return [
        m
        for m in messages
        if m.get("role") == "user"
        and str(m.get("content", "")).startswith(NUDGE_PREFIX)
    ]


def _bind_empty_plan(session_id):
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous = plan.session_id
    bind_default_plan_session(session_id)
    plan.replace([])
    return previous


def _restore_plan(previous):
    from core.plan import bind_default_plan_session

    bind_default_plan_session(previous)


def test_count_substantive_ops_classifies_by_name_and_risk(mocker):
    from core.permission_policy import PermissionRisk

    mocker.patch(
        "core.llm.classify_command_risk",
        side_effect=lambda command: (
            PermissionRisk.QUERY
            if "status" in command
            else PermissionRisk.SENSITIVE_MUTATION
        ),
    )
    tool_calls = [
        {"function": {"name": "write_file", "arguments": "{}"}},
        {"function": {"name": "edit_file", "arguments": "{}"}},
        {"function": {"name": "read_file", "arguments": "{}"}},
        {
            "function": {
                "name": "execute_command",
                "arguments": json.dumps({"command": "git status"}),
            }
        },
        {
            "function": {
                "name": "execute_command",
                "arguments": json.dumps({"command": "mkdir out"}),
            }
        },
        {"function": {"name": "execute_command", "arguments": "{broken json"}},
    ]
    assert llm._count_substantive_ops(tool_calls) == 4


def test_should_nudge_orchestration_conditions(mocker):
    mocker.patch("core.llm._has_pending_plan", return_value=False)
    mocker.patch("core.llm._is_plan_turn", return_value=False)

    base = dict(count=3, session_id="s", turn_id="t")
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is True
    )
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=True, **base)
        is False
    )
    assert (
        llm._should_nudge_orchestration(allow_subagents=False, already_sent=False, **base)
        is False
    )
    assert (
        llm._should_nudge_orchestration(
            allow_subagents=True, already_sent=False, count=2, session_id="s", turn_id="t"
        )
        is False
    )

    mocker.patch("core.llm._has_pending_plan", return_value=True)
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is False
    )

    mocker.patch("core.llm._has_pending_plan", return_value=False)
    mocker.patch("core.llm._is_plan_turn", return_value=True)
    assert (
        llm._should_nudge_orchestration(allow_subagents=True, already_sent=False, **base)
        is False
    )


def test_three_ops_without_plan_injects_single_nudge(mocker):
    previous = _bind_empty_plan("orch-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("b1", 2))]),
            iter([_chunk(tool_calls=_write_calls("b2", 1))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "把三个文件都维护一下"}]
        _run(mocker, messages, responses, parent_session="orch-nudge-test")

        assert len(_nudges(messages)) == 1
    finally:
        _restore_plan(previous)


def test_five_ops_without_plan_still_injects_only_once(mocker):
    previous = _bind_empty_plan("orch-nudge-once-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("c1", 3))]),
            iter([_chunk(tool_calls=_write_calls("c2", 2))]),
            iter([_chunk("检查结论：文件已处理。")]),
        ]
        messages = [{"role": "user", "content": "继续维护剩下两个"}]
        _run(mocker, messages, responses, parent_session="orch-nudge-once-test")

        assert len(_nudges(messages)) == 1
    finally:
        _restore_plan(previous)


def test_two_ops_do_not_inject_nudge(mocker):
    previous = _bind_empty_plan("orch-no-nudge-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("d1", 2))]),
            iter([_chunk("检查结论：改动已处理。")]),
        ]
        messages = [{"role": "user", "content": "改两个文件"}]
        _run(mocker, messages, responses, parent_session="orch-no-nudge-test")

        assert _nudges(messages) == []
    finally:
        _restore_plan(previous)


def test_subagent_turn_does_not_inject_nudge(mocker):
    previous = _bind_empty_plan("orch-subagent-test")
    try:
        responses = [
            iter([_chunk(tool_calls=_write_calls("e1", 3))]),
            iter([_chunk("检查结论：改动已处理。")]),
        ]
        messages = [{"role": "user", "content": "批量改三个文件"}]
        _run(
            mocker,
            messages,
            responses,
            parent_session="orch-subagent-test",
            allow_subagents=False,
        )

        assert _nudges(messages) == []
    finally:
        _restore_plan(previous)
```

- [ ] **Step 2:【红】跑测试确认红灯**

Run: `python -m pytest tests/test_orchestration_nudge.py -q`
Expected: FAIL —— `AttributeError: module 'core.llm' has no attribute '_count_substantive_ops'`（函数与判定尚未实现）。

- [ ] **Step 3:【绿】实现 `core/llm.py`（3 处改动）**

3a. 模块级常量与辅助函数。插入位置：`core/llm.py` 模块级辅助区——定位 `_ORIGINAL_EXECUTE_SINGLE_TOOL_CALL = _execute_single_tool_call`（实测约 :1059），在其附近的模块级空行处插入以下块（先 read_file 确认落点为模块级上下文）：

```python
# ── 编排提醒：多步未建计划时的软提醒（每回合最多 1 次）────────────────

_ORCHESTRATION_NUDGE_THRESHOLD = 3
_ORCHESTRATION_COUNTED_TOOLS = frozenset({"write_file", "edit_file"})
_ORCHESTRATION_COMMAND_TOOL = "execute_command"
_ORCHESTRATION_NUDGE_TEXT = (
    "本回合已连续执行多步操作，仍未建立任务计划。"
    "若任务拆解后超过 3 条子任务，请立即用 plan_write 建立计划并逐条执行；"
    "不超过 3 条可忽略本提醒，直接完成。"
)


def _count_substantive_ops(tool_calls):
    """一批工具调用中的实质操作数：write_file / edit_file / 非只读 execute_command。

    execute_command 的只读判定与计划门禁同口径（classify_command_risk）；
    参数解析失败的调用按保守原则计入，空命令不计。
    """
    from core.permission_policy import PermissionRisk

    count = 0
    for tool_call in tool_calls or ():
        function = tool_call.get("function") or {}
        name = str(function.get("name") or "")
        if name in _ORCHESTRATION_COUNTED_TOOLS:
            count += 1
            continue
        if name != _ORCHESTRATION_COMMAND_TOOL:
            continue
        raw = str(function.get("arguments") or "")
        try:
            arguments = json.loads(raw) if raw.strip() else {}
        except (TypeError, ValueError):
            count += 1
            continue
        command = str((arguments or {}).get("command") or "")
        if command and classify_command_risk(command) is not PermissionRisk.QUERY:
            count += 1
    return count


def _should_nudge_orchestration(*, allow_subagents, count, session_id, turn_id, already_sent):
    """是否注入编排提醒：主代理回合 + 达阈值 + 无计划在途 + 未提醒过。"""
    if not allow_subagents or already_sent:
        return False
    if count < _ORCHESTRATION_NUDGE_THRESHOLD:
        return False
    return not (_has_pending_plan(session_id) or _is_plan_turn(turn_id))
```

3b. per-turn 变量。搜 `plan_continue_nudges = 0`（约 :1231），紧随其后新增两行：

```python
    # 编排提醒：本回合是否已注入、实质操作累计（每回合重置）
    orchestration_nudge_sent = False
    substantive_op_count = 0
```

3c. 驱动循环接线。落点：在 `records = tuple(sorted(records, key=lambda record: record.call_ordinal,))` 代码块之后、`results = [record.message for record in records]` 之前，以当前缩进（12 空格）插入：

```python
            # 编排提醒（软）：多步实质操作仍未建计划时拉一把，每回合最多一次
            substantive_op_count += _count_substantive_ops(tool_calls_list)
            if _should_nudge_orchestration(
                allow_subagents=allow_subagents,
                count=substantive_op_count,
                session_id=parent_session,
                turn_id=parent_turn_id,
                already_sent=orchestration_nudge_sent,
            ):
                orchestration_nudge_sent = True
                orchestration_request = {
                    "role": "user",
                    "content": _ORCHESTRATION_NUDGE_TEXT,
                }
                messages.append(orchestration_request)
                pending_persistence.append(("user", orchestration_request))
                _notify_event(event_callback, "notice", {
                    "message": "已提醒模型评估是否需要建立任务计划。",
                })
                flush_persistence()
                progress_guard.observe(
                    _step_progress_observation(None, session_id=parent_session)
                )
```

- [ ] **Step 4:【绿】跑测试确认通过**

Run: `python -m pytest tests/test_orchestration_nudge.py -q`
Expected: PASS（6 个用例全绿）。

- [ ] **Step 5: 注入前缀登记**

5a. `core/injection_markers.py`：
- `SYSTEM_INJECTED_USER_PREFIXES` 元组末尾（`"计划尚未执行完毕，仍有可执行的计划项",` 之后）追加一项：
  `    "本回合已连续执行多步操作，仍未建立任务计划",`
- 同文件上方"同步铁律"注释清单末尾追加一行：
  `#   - llm.py 编排提醒（多步未建计划）`

5b. `tests/test_injection_markers.py`：
- `test_system_injected_prefixes_frozen` 的期望元组同样追加 `"本回合已连续执行多步操作，仍未建立任务计划",`
- `test_system_injected_user_text_detected` 的参数列表追加一条完整样例：
  `"本回合已连续执行多步操作，仍未建立任务计划。若任务拆解后超过 3 条子任务，请立即用 plan_write 建立计划并逐条执行；不超过 3 条可忽略本提醒，直接完成。",`

Run: `python -m pytest tests/test_injection_markers.py -q`
Expected: PASS。

- [ ] **Step 6: 回归子集**

Run: `python -m pytest tests/test_orchestration_nudge.py tests/test_injection_markers.py tests/test_llm_events.py tests/test_llm_events_fixes.py tests/test_harness_runtime.py -q`
Expected: PASS（如单跑超时，可逐文件分组执行）。

- [ ] **Step 7: 提交**

```powershell
git add core/llm.py core/injection_markers.py tests/test_orchestration_nudge.py tests/test_injection_markers.py
git commit -m "feat(llm): 多步未建计划时注入编排提醒（软提醒，每回合一次）"
```

---

### Task 4: README 同步

**Files:**
- Modify: `README.md`（两处旧判据描述）

**Interfaces:**
- Consumes: 无
- Produces: 无

- [ ] **Step 1: 更新两处描述**

1a. 约 :43（高级编排 段）
target：`- **计划执行模式**：复杂任务先用 \`plan_write\` 建立可执行计划，按依赖图逐项派发子代理，支持暂停 / 恢复 / 重试 / 取消`
replacement：`- **计划执行模式**：多步任务（拆解后子任务超过 3 条）先用 \`plan_write\` 建立可执行计划，按依赖图逐项派发子代理，支持暂停 / 恢复 / 重试 / 取消`

1b. 约 :373（计划模式（Plan Mode）段）
target：`复杂任务（3+ 文件、50+ 行改动）先用 \`plan_write\` 建立可执行计划：`
replacement：`多步任务（拆解后子任务超过 3 条）先用 \`plan_write\` 建立可执行计划：`

- [ ] **Step 2: 提交**

```powershell
git add README.md
git commit -m "docs(readme): 计划编排判据同步为子任务超过 3 条"
```

---

### Task 5: 全量回归 + 实测验收

**Files:** 无代码改动（验收任务）

- [ ] **Step 1: 相关测试全跑**

Run: `python -m pytest tests/test_prompt_behavior_clauses.py tests/test_context_builder.py tests/test_prompt_byte_stability.py tests/test_context_injection.py tests/test_injection_markers.py tests/test_orchestration_nudge.py -q`
Expected: PASS（全绿）。

- [ ] **Step 2: 重启应用**

`core/*.py` 改动需重启生效（提示词 `.md` 每轮现读、下一轮即生效；进程内代码不重载）。由用户（应用主人）完全退出并重新启动星瑶。

- [ ] **Step 3: 四场景实测（重启后）**

| 场景 | 操作 | 预期 |
|------|------|------|
| A 复杂任务 | 发送一个能拆出 4+ 步子任务的任务 | 先 `plan_write` 建计划（计划面板可见）、自动逐条执行 |
| B 小任务 | 发送一个 2 步小任务（如"改两个文件里的一个笔误"） | 不建计划、直接完成 |
| C 闲聊问答 | 发送"你好"或"今天星期几" | 行为无变化 |
| D 兜底观察 | 发送 3 条以内但会连续产生 3+ 次写操作的任务 | 第 3 次实质操作后注入一次编排提醒（可通过界面 notice「已提醒模型评估是否需要建立任务计划。」或模型后续行为观察），模型可忽略继续；D 为观察项，非阻塞 |

- [ ] **Step 4: 验收结论**

汇报：哪些场景通过、D 场景观察结果、遗留事项（如阈值 3 需不需要调）。

---

## 附：执行注意

- 任务间无强依赖（文件不重叠），建议按 1 → 2 → 3 → 4 → 5 顺序串行执行，保持 git 提交历史干净。
- 所有编辑操作先读后改；若发现文本与计划给出的 target 不一致（行漂移之外的实质差异），先停下核对 spec 与本计划，不自行发挥。
- 提交信息保持与计划一致；如仓库有其他未提交变更，只 add 本任务涉及的文件。
