from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from core import fresh_read_guard, tool_cache
from core.command_policy import classify_command_risk, session_rule_for_command
from core.command_shell import describe_selected_command_shell
from core.hooks.registry import HookRegistry
from core.permission_policy import PermissionRisk
from core.tool_output_reducer import reduce_tool_output
from core.tools import edit_file, execute_command, read_file, write_file


def _clear_read_guard():
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()


def test_consecutive_edits_succeed_without_reread(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clear_read_guard()
    tool_cache.clear_all()
    tool_cache.set_current_session("sess-consecutive")
    try:
        target = tmp_path / "demo.py"
        target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")

        assert "alpha = 1" in read_file(str(target))
        res1 = edit_file(str(target), target_content="alpha = 1", replacement_content="alpha = 10")
        assert res1.startswith("✔"), res1

        res2 = edit_file(str(target), target_content="beta = 2", replacement_content="beta = 20")
        assert res2.startswith("✔"), res2
        assert "alpha = 10" in target.read_text(encoding="utf-8")
        assert "beta = 20" in target.read_text(encoding="utf-8")
    finally:
        tool_cache.set_current_session(None)
        _clear_read_guard()


def test_write_new_file_then_edit_succeeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _clear_read_guard()
    tool_cache.clear_all()
    tool_cache.set_current_session("sess-new-edit")
    try:
        target = tmp_path / "fresh.py"
        assert write_file(str(target), "def f():\n    return 1\n").startswith("✔")
        res = edit_file(str(target), target_content="return 1", replacement_content="return 42")
        assert res.startswith("✔"), res
        assert "return 42" in target.read_text(encoding="utf-8")
    finally:
        tool_cache.set_current_session(None)
        _clear_read_guard()


def test_tool_cache_invalidated_on_write_and_edit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()
    _clear_read_guard()
    tool_cache.set_current_session("sess-cache-inv")
    try:
        target = tmp_path / "m.py"
        target.write_text("x = 1\n", encoding="utf-8")

        s_key = tool_cache.search_key("search_code", "x =", str(tmp_path))
        g_key = tool_cache.glob_key("*.py", str(tmp_path))
        r_key = tool_cache.read_file_key(str(target), 0, None, False)
        tool_cache.search_put(s_key, "stale search")
        tool_cache.glob_put(g_key, "stale glob")
        tool_cache.read_file_put(r_key, "x = 1\n")
        tool_cache.shared_put("sess-cache-inv", ("search_code", s_key), "stale shared")

        edit_file(str(target), target_content="x = 1", replacement_content="x = 2")
        assert tool_cache.search_get(s_key) is None
        assert tool_cache.glob_get(g_key) is None
        assert tool_cache.read_file_get(r_key) is None
        assert tool_cache.shared_get("sess-cache-inv", ("search_code", s_key)) is None
    finally:
        tool_cache.set_current_session(None)
        _clear_read_guard()


def test_command_policy_precision_and_safe_compounds():
    assert session_rule_for_command('git commit -m "fix bug"') == ("git", "commit")
    assert classify_command_risk("git log -p -n 3") is PermissionRisk.QUERY
    assert classify_command_risk("git branch -vv") is PermissionRisk.QUERY
    assert classify_command_risk("git branch -D old-branch") is PermissionRisk.SENSITIVE_MUTATION
    assert classify_command_risk("git branch new-branch") is PermissionRisk.SENSITIVE_MUTATION
    assert classify_command_risk("pytest -m unit") is PermissionRisk.NORMAL_MUTATION
    assert classify_command_risk("python -m py_compile a.py") is PermissionRisk.QUERY
    assert classify_command_risk("ruff check core/") is PermissionRisk.QUERY
    assert classify_command_risk("ruff check --fix core/") is PermissionRisk.NORMAL_MUTATION
    assert classify_command_risk("tsc --noEmit") is PermissionRisk.QUERY

    assert classify_command_risk("git status | head -n 10") is PermissionRisk.QUERY
    assert classify_command_risk("cd sub && git status") is PermissionRisk.QUERY
    assert classify_command_risk("cd sub && pytest tests/") is PermissionRisk.NORMAL_MUTATION
    assert classify_command_risk("git status > out.txt") is PermissionRisk.SENSITIVE_MUTATION
    assert session_rule_for_command("git status | head -n 10") is None


def test_execute_command_timeout_preserves_partial_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = execute_command(
        f'"{sys.executable}" -u -c "import time; print(\'TAIL_MARKER_123\', flush=True); time.sleep(5)"',
        timeout_seconds=1,
    )
    assert res.startswith("🚨 命令执行超时")
    assert "TAIL_MARKER_123" in res


def test_tool_output_reducer_keeps_pytest_traceback_and_diff_hunks():
    pytest_lines = [
        "============================= test session starts =============================",
        "collected 80 items",
    ] + [f"tests/test_{i:02d}.py::test_ok PASSED" for i in range(79)] + [
        "================================== FAILURES ===================================",
        "_______________________________ test_broken ___________________________________",
        'tests/test_broken.py:42: AssertionError',
        'Traceback (most recent call last):',
        '  File "tests/test_broken.py", line 42, in test_broken',
        ">       assert actual == expected",
        "E       assert 1 == 2",
        "FAILED tests/test_broken.py::test_broken - assert 1 == 2",
        "1 failed, 79 passed in 1.2s",
    ]
    sample_pytest = "\n".join(pytest_lines)
    assert len(sample_pytest) >= 2000
    reduced_pytest = reduce_tool_output("execute_command", sample_pytest)
    assert reduced_pytest is not None
    assert "tests/test_broken.py:42: AssertionError" in reduced_pytest.text
    assert '  File "tests/test_broken.py", line 42, in test_broken' in reduced_pytest.text
    assert ">       assert actual == expected" in reduced_pytest.text

    diff_lines = [
        "diff --git a/app.py b/app.py",
        "index 1111111..2222222 100644",
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -1,4 +1,4 @@",
        "-old_var = 1",
        "+new_var = 2",
    ] + [f"+extra_{i:03d} = {i}" + (" " * 20) for i in range(100)]
    sample_diff = "\n".join(diff_lines)
    assert len(sample_diff) >= 2000
    reduced_diff = reduce_tool_output("execute_command", sample_diff)
    assert reduced_diff is not None
    assert "-old_var = 1" in reduced_diff.text
    assert "+new_var = 2" in reduced_diff.text
    assert "已折叠" in reduced_diff.text


def test_hook_registry_caches_until_file_changes(tmp_path, monkeypatch):
    user_path = tmp_path / "home" / "hooks.json"
    proj_path = tmp_path / "proj" / ".ycode" / "hooks.json"
    proj_path.parent.mkdir(parents=True)
    proj_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "command": "echo a"}]}}),
        encoding="utf-8",
    )

    registry = HookRegistry(user_hooks_path=user_path, project_hooks_path=proj_path)
    load_calls = 0
    real_uncached = registry._load_file_uncached

    def _counting_load(*args, **kwargs):
        nonlocal load_calls
        load_calls += 1
        return real_uncached(*args, **kwargs)

    monkeypatch.setattr(registry, "_load_file_uncached", _counting_load)

    first = registry.load(workspace_root=tmp_path / "proj")
    second = registry.load(workspace_root=tmp_path / "proj")
    assert [r.command for r in first] == ["echo a"]
    assert [r.command for r in second] == ["echo a"]
    assert load_calls == 1

    # 改写文件内容，触发缓存失效
    time.sleep(0.02)
    proj_path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "command": "echo updated-b"}]}}),
        encoding="utf-8",
    )
    third = registry.load(workspace_root=tmp_path / "proj")
    assert [r.command for r in third] == ["echo updated-b"]
    assert load_calls == 2


def test_llm_deterministic_failure_categories_and_win_shell_description(tmp_path, monkeypatch):
    import config
    from core import llm

    assert "model_unavailable" in llm._DETERMINISTIC_API_FAILURE_CATEGORIES
    assert "policy_refusal" in llm._DETERMINISTIC_API_FAILURE_CATEGORIES

    cfg = tmp_path / "config.json"
    cfg.write_text('{"command_shell": "auto"}', encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr("core.command_shell.shutil.which", lambda _n: None)
    desc = describe_selected_command_shell(platform="win32")
    assert "cmd.exe" in desc
