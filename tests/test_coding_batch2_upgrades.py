from __future__ import annotations

import json
from types import SimpleNamespace

from core import agent_service, fresh_read_guard, tool_cache
from core.harness.tool_adapters import ToolAdapterSet
from core.harness.tool_pipeline import ToolExecutionContext
from core.memory import MemoryManager
from core.tool_registry import CODING_INCLUDED_TOOLS, build_default_registry
from core.tools import edit_file, multi_edit, read_file, write_file
from core.turn_undo.service import validate_recorder_coverage
from core.workspace_bootstrap import build_workspace_bootstrap_context, load_workspace_rules


def test_coding_tool_profile_slims_schema_and_keeps_core():
    full_reg = build_default_registry("full")
    coding_reg = build_default_registry("coding")

    coding_names = {spec.name for spec in coding_reg.all()}
    assert coding_names <= CODING_INCLUDED_TOOLS
    assert {"read_file", "write_file", "edit_file", "multi_edit", "search_code", "execute_command"} <= coding_names
    assert "get_weather" not in coding_names
    assert "theme_manage" not in coding_names

    full_chars = len(json.dumps(full_reg.api_schemas(), ensure_ascii=False))
    coding_chars = len(json.dumps(coding_reg.api_schemas(), ensure_ascii=False))
    assert coding_chars < full_chars * 0.45
    assert validate_recorder_coverage(full_reg) == []
    assert validate_recorder_coverage(coding_reg) == []


def test_cli_flavor_uses_coding_and_gui_keeps_coding_plus_office(monkeypatch):
    import config
    from core import app_context
    from core.computer.capability import resolve_computer_capability
    from core.computer.settings import ComputerSettingsSnapshot
    from core.tool_registry import _resolve_tool_profile

    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)
    monkeypatch.setattr(config, "_load", lambda: {})

    monkeypatch.setattr(app_context, "get_ui_flavor", lambda: "cli")
    assert _resolve_tool_profile() == "coding"

    monkeypatch.setattr(app_context, "get_ui_flavor", lambda: "gui")
    assert _resolve_tool_profile() == "full"

    gui_names = {spec.name for spec in build_default_registry("full").all()}
    assert CODING_INCLUDED_TOOLS <= gui_names
    assert {"office_read", "office_write", "automation_list"} <= gui_names

    monkeypatch.setenv("YCODE_TOOL_PROFILE", "coding")
    cap = resolve_computer_capability(
        {"supports_vision": True, "supports_function_calling": True},
        {"protocol": "openai", "base_url": "https://example.com/v1"},
        ComputerSettingsSnapshot(
            browser_control_enabled=True,
            computer_control_enabled=True,
            computer_browser="edge",
            browser_options=(),
            profile_display_path="",
            platform_supported=True,
            generation=1,
        ),
        "local",
    )
    assert cap.enabled is False
    assert cap.error_code == "computer_cli_disabled"

    from core.tool_registry import ToolRegistry
    import plugins.windows_tools.plugin as wt_plugin

    wt_reg = ToolRegistry()
    wt_plugin.register(wt_reg)
    assert list(wt_reg.all()) == []


def test_workspace_bootstrap_loads_rules_and_first_turn_context(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "AGENTS.md").write_text("始终运行 pytest 验证改动。", encoding="utf-8")
    rules_dir = tmp_path / ".ycode" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").write_text("函数保持短小清晰。", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    rules_text = load_workspace_rules(tmp_path)
    assert "始终运行 pytest 验证改动" in rules_text
    assert "函数保持短小清晰" in rules_text

    snap = build_workspace_bootstrap_context(tmp_path)
    assert "Python" in snap
    assert "src/" in snap

    no_sess = agent_service.build_stable_system_message()["content"]
    assert "始终运行 pytest 验证改动" not in no_sess

    agent_service._SESSION_STABLE_SECTIONS.clear()
    try:
        sess = "ws-boot-test"
        named_sys = agent_service.build_stable_system_message(session_name=sess)["content"]
        assert "始终运行 pytest 验证改动" in named_sys

        mgr = MemoryManager(":memory:")
        mgr.add_message(sess, "user", "帮我重构模块")
        turn1 = agent_service.prepare_turn_messages(mgr, sess, "openai/gpt-test", 128000, permission_mode="full")
        assert "【工作区首轮概览】" in turn1[-1]["content"]

        mgr.add_message(sess, "assistant", "好的，我先分析一下")
        mgr.add_message(sess, "user", "继续改下一个文件")
        turn2 = agent_service.prepare_turn_messages(mgr, sess, "openai/gpt-test", 128000, permission_mode="full")
        assert "【工作区首轮概览】" not in turn2[-1]["content"]
    finally:
        agent_service._SESSION_STABLE_SECTIONS.clear()


def test_read_file_rejects_binary_and_write_preserves_bom_and_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()

    bin_file = tmp_path / "logo.png"
    bin_file.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    res_bin = read_file(str(bin_file))
    assert res_bin.startswith("🚨") and "二进制" in res_bin

    # UTF-8 BOM 保留 + Python 语法自检警告
    bom_py = tmp_path / "bom_mod.py"
    bom_py.write_bytes(b"\xef\xbb\xbfx = 1\n")
    read_file(str(bom_py))
    res_write = write_file(str(bom_py), "def broken(:\n    pass\n")
    assert res_write.startswith("✔")
    assert "静态语法检查" in res_write
    assert bom_py.read_bytes().startswith(b"\xef\xbb\xbf")


def test_edit_file_indent_shift_closest_match_and_diagnostics(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()

    py_file = tmp_path / "nested.py"
    py_file.write_text(
        "class Service:\n"
        "    def run(self):\n"
        "        value = compute_old(1)\n"
        "        return value + 1\n",
        encoding="utf-8",
    )
    res = edit_file(
        str(py_file),
        target_content="    value = compute_old(1)\n    return value + 1",
        replacement_content="    value = compute_new(2)\n    return value * 2",
    )
    assert res.startswith("✔"), res
    text = py_file.read_text(encoding="utf-8")
    assert "        value = compute_new(2)\n        return value * 2\n" in text

    res_fail = edit_file(
        str(py_file),
        target_content="        value = compute_typo(999)\n        return value * 2",
        replacement_content="pass",
    )
    assert res_fail.startswith("🚨")
    assert "最接近的代码段" in res_fail

    cfg = tmp_path / "cfg.json"
    cfg.write_text('{\n  "ok": true\n}\n', encoding="utf-8")
    res_json = edit_file(str(cfg), target_content='"ok": true', replacement_content='"ok": true,')
    assert res_json.startswith("✔")
    assert "JSON" in res_json


def test_multi_edit_atomic_and_adapter_single_read(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool_cache.clear_all()
    with fresh_read_guard._LOCK:
        fresh_read_guard._READ_STATE.clear()
    tool_cache.set_current_session("sess-multi")
    try:
        mod = tmp_path / "multi.py"
        original = "A = 1\nB = 2\nC = 3\n"
        mod.write_bytes(original.encode("utf-8"))
        read_file(str(mod))

        fail_res = multi_edit(
            str(mod),
            [
                {"target_content": "A = 1", "replacement_content": "A = 100"},
                {"target_content": "NON_EXISTENT", "replacement_content": "X = 0"},
            ],
        )
        assert fail_res.startswith("🚨")
        assert mod.read_text(encoding="utf-8") == original

        read_text_calls = 0
        captured_diffs = []

        def _spy_read_text(_p):
            nonlocal read_text_calls
            read_text_calls += 1
            return ""

        adapters = ToolAdapterSet(
            read_text=_spy_read_text,
            show_file_diff=lambda path, old, new, *_args, **_kw: captured_diffs.append((old, new)),
        )
        ctx = ToolExecutionContext(
            event_callback=None,
            cancel_event=None,
            permission_owner="main",
            permission_source="主代理",
            parent_session="sess-multi",
            parent_turn_id="t1",
            registry=build_default_registry("coding"),
            mcp_manager=None,
            permission_checker=lambda *a, **k: True,
            allow_subagents=True,
        )
        multi_spec = ctx.registry.get("multi_edit")
        resolved_tool = SimpleNamespace(
            spec=multi_spec,
            source="builtin",
            execute=multi_spec.execute,
        )
        out = adapters.execute(
            resolved_tool,
            {
                "file_path": str(mod),
                "edits": [
                    {"target_content": "A = 1", "replacement_content": "A = 10"},
                    {"target_content": "C = 3", "replacement_content": "C = 30"},
                ],
            },
            ctx,
        )
        assert not out.is_error, out.content
        assert read_text_calls == 0
        assert mod.read_text(encoding="utf-8") == "A = 10\nB = 2\nC = 30\n"
        assert captured_diffs == [(original, "A = 10\nB = 2\nC = 30\n")]
    finally:
        tool_cache.set_current_session(None)
        with fresh_read_guard._LOCK:
            fresh_read_guard._READ_STATE.clear()
