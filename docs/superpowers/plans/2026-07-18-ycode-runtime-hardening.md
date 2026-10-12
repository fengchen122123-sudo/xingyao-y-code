# Y-CODE Agent Runtime Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Y-CODE v1.0.12 with concise source-level K3 reasoning, provider-correct reasoning history, approval-aware subagent timeouts, stage diagnostics, and correct Windows command output.

**Architecture:** Replace eager skill-body injection with a lazy catalog, keep reasoning/public/provider blocks separate, account for approval waits outside the execution budget, and centralize byte decoding for process output. Preserve current TUI layout and rendering contracts.

**Tech Stack:** Python 3.13, LiteLLM, prompt_toolkit, pytest, SQLite, Windows subprocess APIs.

---

### Task 1: Lazy Skill Catalog and Decisive Prompt Policy

**Files:**
- Modify: `core/skills.py`
- Modify: `core/context_builder.py`
- Modify: `main.py`
- Modify: `system_prompt.md`
- Create: `tests/test_skills.py`
- Modify: `tests/test_context_builder.py`

- [x] **Step 1: Write failing catalog tests**

```python
def test_active_skill_catalog_exposes_metadata_without_body(tmp_path, monkeypatch):
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: demo\ndescription: Demo skill\n---\nSECRET BODY", encoding="utf-8")
    monkeypatch.setattr(skills, "get_active_skill_ids", lambda: [str(skill)])
    monkeypatch.setattr(skills, "scan_all_skills", lambda: [skills.parse_skill_md(str(skill))])
    catalog = skills.get_active_skills_catalog()
    assert "demo" in catalog
    assert "Demo skill" in catalog
    assert str(skill) in catalog
    assert "SECRET BODY" not in catalog
```

- [x] **Step 2: Run the tests and confirm they fail because the catalog API is missing**

Run: `venv\Scripts\python -m pytest -q tests\test_skills.py tests\test_context_builder.py`

- [x] **Step 3: Implement metadata-only catalog generation**

```python
def get_active_skills_catalog() -> str:
    active_ids = set(get_active_skill_ids())
    active = [skill for skill in scan_all_skills() if skill["id"] in active_ids]
    if not active:
        return ""
    lines = ["【可用技能目录】", "仅在任务直接匹配时读取对应 SKILL.md；不要加载无关技能。"]
    for skill in sorted(active, key=lambda item: (item["name"].lower(), item["id"])):
        lines.append(f'- {skill["name"]}: {skill["description"]} | {skill["id"]}')
    return "\n".join(lines)
```

Keep `get_active_skills_markdown()` as a compatibility alias returning the catalog, never full content. Change `main.py` to call the catalog-named API.

- [x] **Step 4: Add the stable decision policy in `core/context_builder.py`**

The policy must require one-pass action for explicit simple tasks, prohibit repeated reconsideration without new evidence, and require loading only a directly matching skill.

- [x] **Step 5: Run focused tests and verify prompt size no longer scales with skill bodies**

Run: `venv\Scripts\python -m pytest -q tests\test_skills.py tests\test_context_builder.py`

### Task 2: Provider-Native Reasoning Round-Trip

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm.py`
- Modify: `tests/test_llm_events.py`

- [x] **Step 1: Write failing message-shape tests**

```python
def test_tool_round_preserves_signed_thinking_blocks_without_think_text(mocker):
    signed = [{"type": "thinking", "thinking": "分析", "signature": "sig"}]
    # First response streams reasoning, signed provider block, and a tool call.
    # Second fake completion captures messages.
    # Assert assistant content excludes <think>, reasoning_content is structured,
    # and thinking_blocks equals signed before the tool result.
```

Also assert Kimi Coding K3 request kwargs contain Anthropic-native `thinking={"type": "enabled", "budget_tokens": 4096}` and `max_tokens=32000`; other providers receive neither option.

- [x] **Step 2: Run focused tests and observe the current `<think>` content failure**

Run: `venv\Scripts\python -m pytest -q tests\test_llm.py tests\test_llm_events.py`

- [x] **Step 3: Add provider option selection**

```python
def _provider_request_options(model_name: str, api_base: str | None) -> dict:
    is_kimi_coding = "api.kimi.com/coding" in str(api_base or "").lower()
    is_k3 = str(model_name or "").split("/")[-1].lower() == "k3"
    return {
        "thinking": {"type": "enabled", "budget_tokens": 4096},
        "max_tokens": 32000,
        "allowed_openai_params": ["thinking"],
    } if is_kimi_coding and is_k3 else {}
```

- [x] **Step 4: Accumulate public text, reasoning, and signed blocks separately**

Attach `content`, `reasoning_content`, and `thinking_blocks` as separate assistant fields. Preserve these fields in `_strip_media_and_merge()`. Continue yielding display tags only through the event parser; never place them in API history.

- [x] **Step 5: Run reasoning and end-to-end tests**

Run: `venv\Scripts\python -m pytest -q tests\test_llm.py tests\test_llm_events.py tests\test_agent_turn_e2e.py tests\test_memory.py`

### Task 3: Approval-Aware Timeout and Stage Diagnostics

**Files:**
- Modify: `core/permission_broker.py`
- Modify: `core/tools.py`
- Modify: `core/tool_presenter.py`
- Modify: `tests/test_permission_broker.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_tool_presenter.py`

- [x] **Step 1: Write failing owner and paused-budget tests**

```python
def test_broker_reports_active_or_queued_owner():
    broker = PermissionBroker(activate_prompt=lambda request: None)
    request = broker.enqueue("execute_command", {}, "child", "turn", "child")
    assert broker.has_owner("child") is True
    broker.resolve(request.request_id, 0)
    assert broker.has_owner("child") is False

def test_approval_wait_does_not_consume_foreground_timeout(monkeypatch):
    # Advance a fake clock beyond the timeout while broker.has_owner is true,
    # then finish the worker after approval and assert status completed.
```

- [x] **Step 2: Run focused tests and observe the wall-clock timeout failure**

Run: `venv\Scripts\python -m pytest -q tests\test_permission_broker.py tests\test_tools.py`

- [x] **Step 3: Add bounded stage records and split elapsed counters**

```python
@dataclass(frozen=True)
class SubagentStage:
    name: str
    at: float
    detail: str = ""
```

Snapshots expose `stage`, `stages`, `execution_elapsed`, and `approval_wait_elapsed`. Suppress consecutive duplicate stages and retain at most 64 entries.

- [x] **Step 4: Replace the deadline with an execution budget**

Each wait-loop tick adds elapsed time to exactly one counter based on `permission_broker.has_owner(task.task_id)`. Timeout occurs only when `execution_elapsed >= timeout_seconds`.

- [x] **Step 5: Map child events to stages and include diagnostics in failures**

Map reasoning, tool start, permission wait/resolution, composing, completed, cancelled, failed, and timed-out events. Failure JSON includes latest stage and both counters.

- [x] **Step 6: Run subagent, permission, presenter, and UI tests**

Run: `venv\Scripts\python -m pytest -q tests\test_permission_broker.py tests\test_tools.py tests\test_tool_presenter.py tests\test_main_ui_events.py`

### Task 4: Windows Command Parsing and Encoding

**Files:**
- Create: `core/process_io.py`
- Modify: `core/tools.py`
- Modify: `core/task_manager.py`
- Create: `tests/test_process_io.py`
- Modify: `tests/test_tools.py`

- [x] **Step 1: Write failing decoder and dependency-preflight tests**

```python
def test_decode_process_output_accepts_windows_oem_chinese():
    assert decode_process_output("星期六".encode("gbk"), fallback_encoding="gbk") == "星期六"

def test_non_skill_powershell_quoting_reaches_subprocess(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.subprocess, "run", lambda *args, **kwargs: calls.append(kwargs) or result)
    tools.execute_command('powershell -Command "$x = `"时间`"; Write-Output $x"')
    assert calls
```

- [x] **Step 2: Run tests and observe UTF-8 corruption/preflight rejection**

Run: `venv\Scripts\python -m pytest -q tests\test_process_io.py tests\test_tools.py`

- [x] **Step 3: Implement deterministic byte decoding**

```python
def decode_process_output(data: bytes, fallback_encoding: str | None = None) -> str:
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(fallback_encoding or platform_console_encoding(), errors="replace")
```

- [x] **Step 4: Capture foreground/background command output as bytes**

Use `text=False` in `execute_command`. Store raw background logs in binary mode and decode when reading status. Do not alter command strings.

- [x] **Step 5: Limit dependency scanning to explicit Python script candidates**

Commands without `.py` bypass tokenization. A tokenizer failure must defer to the actual shell instead of returning a skill dependency error.

- [x] **Step 6: Run process and tool tests**

Run: `venv\Scripts\python -m pytest -q tests\test_process_io.py tests\test_tools.py tests\test_task_manager.py`

### Task 5: Rendering Regression, Version, and Complete Verification

**Files:**
- Modify: `main.py`
- Modify: `setup.py`
- Modify: `tests/test_context_builder.py`
- Verify: `core/turn_renderer.py`
- Verify: `core/thinking_stream.py`
- Verify: `tests/test_turn_renderer.py`
- Verify: `tests/test_main_ui_events.py`

- [x] **Step 1: Update version assertions to v1.0.12 and verify they fail**

Run: `venv\Scripts\python -m pytest -q tests\test_context_builder.py -k version`

- [x] **Step 2: Change only the two version literals**

Set `main.py` to `v1.0.12` and `setup.py` to `1.0.12`. Do not alter startup layout.

- [x] **Step 3: Run all focused rendering and runtime tests**

Run: `venv\Scripts\python -m pytest -q tests\test_turn_renderer.py tests\test_thinking_stream.py tests\test_main_ui_events.py tests\test_agent_turn_e2e.py`

- [x] **Step 4: Run complete automated verification**

Run: `venv\Scripts\python -m pytest -q`

- [x] **Step 5: Compile changed modules**

Run: `venv\Scripts\python -m py_compile main.py core\skills.py core\context_builder.py core\llm.py core\permission_broker.py core\tools.py core\process_io.py core\task_manager.py core\turn_renderer.py`

- [x] **Step 6: Run a real K3 same-turn smoke scenario**

Verify prompt catalog size, concise native reasoning, two foreground subagents, signed thinking-block round-trip, approval-aware timing, readable Chinese command output, one final answer, and unchanged startup layout.

- [x] **Step 7: Review the final diff and fix Critical/Important findings**

Inspect only task-owned hunks while preserving all pre-existing user changes. Re-run the complete verification after every review fix.
