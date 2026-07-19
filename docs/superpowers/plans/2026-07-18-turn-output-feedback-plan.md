# Turn Output Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在工具调用型回合中显示有用结果，并准确显示“思考”和“干活”状态。

**Architecture:** `core.llm` 将工具返回内容压缩为 `tool_finished.output_preview`；`TurnRenderer` 显示预览并在空 assistant 正文时提供本地完成说明；`main.py` 只根据事件状态选择文案。系统提示词要求模型在工具后给结论。

**Tech Stack:** Python 3.12, prompt_toolkit, pytest。

---

### Task 1: Render useful tool completion feedback

**Files:**
- Modify: `core/turn_renderer.py`
- Modify: `tests/test_turn_renderer.py`

- [ ] **Step 1: Write failing renderer tests**

```python
def test_renderer_shows_tool_output_preview():
    renderer = TurnRenderer()
    renderer.consume("tool_finished", {"id": "1", "name": "list_directory", "ok": True, "output_preview": "main.py\ncore"})
    assert "main.py" in renderer.finish()

def test_renderer_explains_empty_final_answer_after_tools():
    renderer = TurnRenderer()
    renderer.consume("tool_finished", {"id": "1", "name": "read_file", "ok": True})
    renderer.consume("assistant_delta", {"text": "**\n"})
    assert "未生成文字总结" in renderer.finish()
```

- [ ] **Step 2: Run the focused tests**

Run: `venv\Scripts\python -m pytest tests/test_turn_renderer.py -q`

Expected: FAIL because `output_preview` is ignored and no empty-answer completion message exists.

- [ ] **Step 3: Implement renderer behavior**

Add a private meaningful-text check that strips whitespace and Markdown-only characters. In `_tool_finished`, append an indented preview after the success line when present. In `finish`, append `已完成 N 项工具操作，但模型未生成文字总结。` only when tools completed and no meaningful assistant text exists.

- [ ] **Step 4: Re-run focused tests**

Run: `venv\Scripts\python -m pytest tests/test_turn_renderer.py -q`

Expected: PASS.

### Task 2: Attach bounded tool output previews

**Files:**
- Modify: `core/llm.py`
- Modify: `tests/test_llm_events.py`

- [ ] **Step 1: Write a failing event test**

```python
finished = [event for event in events if event.kind == "tool_finished"]
assert finished[0].payload["output_preview"]
```

- [ ] **Step 2: Run the focused test**

Run: `venv\Scripts\python -m pytest tests/test_llm_events.py -q`

Expected: FAIL with missing `output_preview`.

- [ ] **Step 3: Implement a bounded preview helper**

Add `_tool_output_preview(result)` in `core.llm.py`. Convert content to text, keep at most eight non-empty lines and 800 characters, and add an ellipsis only when truncated. Include this value in every `tool_finished` event payload.

- [ ] **Step 4: Re-run focused test**

Run: `venv\Scripts\python -m pytest tests/test_llm_events.py tests/test_turn_renderer.py -q`

Expected: PASS.

### Task 3: Distinguish thinking from working

**Files:**
- Modify: `main.py`
- Modify: `system_prompt.md`
- Modify: `tests/test_main_ui_events.py`

- [ ] **Step 1: Write failing status-label tests**

```python
assert main._turn_activity_label("thinking") == "星瑶正在思考..."
assert main._turn_activity_label("executing") == "星瑶正在干活..."
```

- [ ] **Step 2: Run focused test**

Run: `venv\Scripts\python -m pytest tests/test_main_ui_events.py -q`

Expected: FAIL because the shared status-label helper does not exist.

- [ ] **Step 3: Implement the UI labels and prompt rule**

Use `_turn_activity_label` in the top activity animation. Keep bottom status labels semantically equivalent. Add a prompt rule requiring a concise Chinese final conclusion after tool use and prohibiting Markdown-only final text.

- [ ] **Step 4: Run focused tests**

Run: `venv\Scripts\python -m pytest tests/test_main_ui_events.py -q`

Expected: PASS.

### Task 4: Verify the integrated behavior

**Files:**
- Test: `tests/test_agent_turn_e2e.py`

- [ ] **Step 1: Extend fake-provider end-to-end assertion**

Assert rendered output includes a tool result preview, does not expose private thinking tags, and ends with a meaningful assistant conclusion.

- [ ] **Step 2: Run all tests and syntax checks**

Run:

```powershell
venv\Scripts\python -m pytest -q
venv\Scripts\python -m py_compile main.py core\llm.py core\turn_renderer.py
```

Expected: all tests pass and compilation exits with code 0.

### Task 5: Make activity state semantic and color tool output

**Files:**
- Modify: `core/llm.py`
- Modify: `core/turn_renderer.py`
- Modify: `main.py`
- Modify: `setup.py`
- Test: `tests/test_llm_events.py`
- Test: `tests/test_main_ui_events.py`
- Test: `tests/test_turn_renderer.py`

- [ ] **Step 1: Write failing tests**

Assert that a reasoning chunk emits `thinking_started` and `thinking_finished`, tool output includes ANSI color sequences, and `tool_started` switches the UI to `executing`.

- [ ] **Step 2: Implement minimum event and rendering changes**

Emit reasoning boundary events in `core.llm`; handle those boundaries and `tool_started` in `main.py`; add ANSI styles to `TurnRenderer`; change both displayed versions to `1.0.4`.

- [ ] **Step 3: Run focused and full verification**

Run `venv\Scripts\python -m pytest -q` and `venv\Scripts\python -m py_compile main.py core\llm.py core\turn_renderer.py`.
