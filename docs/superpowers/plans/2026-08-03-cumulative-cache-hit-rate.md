# Cumulative Cache Hit Rate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在状态栏展示累计缓存命中 Token，并使用累计命中除以累计输入计算可复算的命中率。

**Architecture:** 在 `main.py` 增加一个无副作用的状态栏 Token 摘要格式化函数，由现有 `get_status()` 读取锁内快照后调用。统计数据来源保持不变，测试直接验证格式化函数的真实输出。

**Tech Stack:** Python 3、prompt_toolkit、pytest

## Global Constraints

- 展示顺序固定为“输入 · 输出 · 命中 · 命中率”。
- 命中率公式固定为 `cumulative_cached / cumulative_prompt * 100`。
- 累计输入为零时显示 `0%`。
- 不修改 Token 采集、厂商适配、缓存亲和或持久化逻辑。

---

### Task 1: 状态栏累计命中与命中率

**Files:**
- Modify: `main.py:2431-2465`
- Test: `tests/test_main_ui_events.py`

**Interfaces:**
- Consumes: `session_token_usage.cumulative_prompt`、`cumulative_completion`、`cumulative_cached`
- Produces: `_format_token_usage_summary(cumulative_prompt, cumulative_completion, cumulative_cached) -> str`

- [x] **Step 1: Write the failing tests**

```python
def test_format_token_usage_summary_uses_cumulative_hit_rate():
    assert main._format_token_usage_summary(315_100, 3_400, 312_000) == (
        "输入 315.1K · 输出 3.4K · 命中 312.0K · 命中率 99%"
    )


def test_format_token_usage_summary_handles_zero_input():
    assert main._format_token_usage_summary(0, 0, 0) == (
        "输入 0 · 输出 0 · 命中 0 · 命中率 0%"
    )
```

- [x] **Step 2: Run tests to verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_main_ui_events.py::test_format_token_usage_summary_uses_cumulative_hit_rate tests\test_main_ui_events.py::test_format_token_usage_summary_handles_zero_input -q
```

Expected: FAIL because `main._format_token_usage_summary` does not exist.

- [x] **Step 3: Write the minimal implementation**

Add a pure formatter to `main.py`:

```python
def _format_token_usage_summary(cumulative_prompt, cumulative_completion, cumulative_cached):
    def format_num(num):
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        if num >= 1_000:
            return f"{num / 1_000:.1f}K"
        return str(num)

    hit_rate = (
        cumulative_cached / cumulative_prompt * 100
        if cumulative_prompt > 0
        else 0.0
    )
    return (
        f"输入 {format_num(cumulative_prompt)} · "
        f"输出 {format_num(cumulative_completion)} · "
        f"命中 {format_num(cumulative_cached)} · 命中率 {hit_rate:.0f}%"
    )
```

In `get_status()`, read `cumulative_cached` under `token_usage_lock`, replace the recent-request rate output with `_format_token_usage_summary(...)`, and retain the existing context suffix.

- [x] **Step 4: Run focused tests to verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_main_ui_events.py::test_format_token_usage_summary_uses_cumulative_hit_rate tests\test_main_ui_events.py::test_format_token_usage_summary_handles_zero_input -q
```

Expected: `2 passed`.

- [x] **Step 5: Run regression verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_main_ui_events.py -q
.\.venv\Scripts\python.exe -m py_compile main.py
```

Expected: all tests pass and compile check exits with code 0.

- [x] **Step 6: Commit only the related implementation files**

```powershell
git add main.py tests/test_main_ui_events.py docs/superpowers/plans/2026-08-03-cumulative-cache-hit-rate.md
git commit -m "fix: show cumulative cache hit rate"
```
