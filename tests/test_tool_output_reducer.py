from __future__ import annotations

import core.tool_output_reducer as reducer_mod
from core.tool_output_reducer import dedupe_adjacent, reduce_tool_output, strip_ansi


def _pytest_sample() -> str:
    lines = [
        "============================= test session starts =============================",
        "platform win32 -- Python 3.11.9, pytest-8.2.0, pluggy-1.5.0",
        "rootdir: D:\\repo\\xingyao",
        "collected 160 items",
        "",
    ]
    for i in range(140):
        lines.append(
            f"tests/test_module_{i:03d}.py::test_case_{i:03d} PASSED"
            f"                                        [ {i // 2:3d}%]"
        )
    lines += [
        "",
        "================================== FAILURES ===================================",
        "_____________________________ test_case_007 __________________________________",
        "    def test_case_007():",
        ">       assert add(1, 1) == 3",
        "E       assert 2 == 3",
        "",
        "short test summary info",
        "FAILED tests/test_module_007.py::test_case_007 - assert 2 == 3",
        "ERROR tests/test_module_120.py::test_case_120 - RuntimeError: boom",
        "=================== 158 passed, 1 failed, 1 error in 2.31s ====================",
    ]
    return "\n".join(lines)


def _npm_sample() -> str:
    lines = [
        "> app@1.0.0 build D:\\repo\\app",
        "> webpack --mode production",
        "",
    ]
    for i in range(120):
        lines.append(
            f"  [build] chunk {i:03d} assets 120 KiB [emitted] [big]"
            f" ./src/modules/chunk_{i:03d}.js"
        )
    lines += [
        "npm ERR! code ELIFECYCLE",
        "npm ERR! errno 1",
        "npm ERR! app@1.0.0 build: `webpack --mode production`",
        "npm ERR! Exit status 1",
        "npm ERR! Failed at the app@1.0.0 build script.",
        "added 320 packages, and audited 321 packages in 42s",
        "3 vulnerabilities (1 moderate, 2 high)",
    ]
    return "\n".join(lines)


def _git_diff_sample() -> str:
    lines = [
        "diff --git a/core/memory.py b/core/memory.py",
        "index 1234567..89abcde 100644",
        "--- a/core/memory.py",
        "+++ b/core/memory.py",
        "@@ -1,5 +1,6 @@",
        " import datetime",
    ]
    for i in range(150):
        lines.append(f"+    value_{i:03d} = compute({i})  # 新增的一行业务逻辑")
    return "\n".join(lines)


def _plain_sample() -> str:
    return "\n".join(
        f"普通日志行 {i}: 处理完成，耗时 {i * 7} 毫秒，记录编号 {i:05d}" for i in range(300)
    )


def test_pytest_output_keeps_failures_and_shrinks():
    sample = _pytest_sample()
    assert len(sample) >= 2000

    result = reduce_tool_output("execute_command", sample)

    assert result is not None
    assert result.reducer == "pytest"
    assert result.original_chars == len(sample)
    assert "FAILED tests/test_module_007.py::test_case_007 - assert 2 == 3" in result.text
    assert "ERROR tests/test_module_120.py::test_case_120 - RuntimeError: boom" in result.text
    assert len(result.text) < len(sample) * 0.5


def test_npm_output_keeps_error_lines():
    sample = _npm_sample()
    assert len(sample) >= 2000

    result = reduce_tool_output("execute_command", sample)

    assert result is not None
    assert result.reducer == "npm"
    assert "npm ERR! code ELIFECYCLE" in result.text
    assert "added 320 packages" in result.text
    assert len(result.text) < len(sample) * 0.5


def test_git_diff_output_keeps_hunk_headers():
    sample = _git_diff_sample()
    assert len(sample) >= 2000

    result = reduce_tool_output("execute_command", sample)

    assert result is not None
    assert result.reducer == "git diff"
    assert "diff --git a/core/memory.py b/core/memory.py" in result.text
    assert "@@ -1,5 +1,6 @@" in result.text
    assert "+++ b/core/memory.py" in result.text
    assert "--- a/core/memory.py" in result.text
    assert len(result.text) < len(sample) * 0.5


def test_plain_text_without_rule_and_without_repeats_returns_none():
    sample = _plain_sample()
    assert len(sample) >= 2000

    assert reduce_tool_output("search_code", sample) is None


def test_short_text_returns_none():
    sample = "============================= test session starts =============================\n1 failed\n"
    assert len(sample) < 2000
    assert reduce_tool_output("execute_command", sample) is None


def test_non_string_input_returns_none_instead_of_raising():
    assert reduce_tool_output("execute_command", None) is None
    assert reduce_tool_output("execute_command", ["a" * 5000]) is None
    assert reduce_tool_output("execute_command", 1234567890) is None


def test_internal_exception_returns_none(monkeypatch):

    def _boom(_text):
        raise RuntimeError("模拟内部异常")

    monkeypatch.setattr(reducer_mod, "strip_ansi", _boom)
    assert reduce_tool_output("execute_command", "x" * 5000) is None


def test_dedupe_adjacent_collapses_repeated_run():
    lines = ["same line"] * 5
    assert dedupe_adjacent(lines) == [
        "same line",
        "same line",
        "... (上一行重复 3 次)",
    ]


def test_dedupe_adjacent_keeps_distinct_lines():
    lines = ["a", "b", "b", "c", "d", "d", "d"]
    assert dedupe_adjacent(lines) == ["a", "b", "b", "c", "d", "d", "... (上一行重复 1 次)"]


def test_strip_ansi_removes_color_sequences():
    assert strip_ansi("\x1b[32;1mOK\x1b[0m done") == "OK done"
