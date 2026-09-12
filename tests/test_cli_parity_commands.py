"""CLI 能力对齐 GUI 第一批命令的单元测试（实施计划：待开发/CLI补齐GUI功能第一批）。

覆盖 /clear-history、/persona、/shell、/open、/skills refresh；
Batch B/C（/computer browser、/queue、/paste-image、/update、/changes）
随批次追加。
"""

import os

import pytest

import main


def _capture_print(monkeypatch):
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def _quiet_ui(monkeypatch):
    monkeypatch.setattr(main, "app_instance", None)


# ---------------------------------------------------------------------------
# /clear-history
# ---------------------------------------------------------------------------

def test_clear_history_requires_confirmation(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "current_session", "s1")

    class _FakeManager:
        def clear_session_messages(self, session):
            calls.append(session)
            return 5

    monkeypatch.setattr(main, "manager", _FakeManager())

    main.handle_slash_command("/clear-history")

    assert calls == []
    assert any("确认" in line for line in printed)


def test_clear_history_confirmed_resets_token_usage(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []
    monkeypatch.setattr(main, "current_session", "s1")

    class _FakeManager:
        def clear_session_messages(self, session):
            calls.append(session)
            return 7

    monkeypatch.setattr(main, "manager", _FakeManager())

    import core.llm as llm
    monkeypatch.setattr(
        llm, "session_token_usage", {"latest_total": 99, "latest_prompt": 50, "latest_cached": 10}
    )

    main.handle_slash_command("/clear-history y")

    assert calls == ["s1"]
    assert llm.session_token_usage["latest_total"] == 0
    assert llm.session_token_usage["latest_prompt"] == 0
    assert llm.session_token_usage["latest_cached"] == 0
    assert any("已清空 7 条" in line for line in printed)


# ---------------------------------------------------------------------------
# /persona
# ---------------------------------------------------------------------------

def test_persona_show_displays_current_settings(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.persona_settings as ps
    monkeypatch.setattr(
        ps,
        "get_persona_settings",
        lambda: {"agent_name": "星瑶", "persona": "测试人设", "user_title": "老板"},
    )

    main.handle_slash_command("/persona")

    assert any("星瑶" in line for line in printed)
    assert any("老板" in line for line in printed)
    assert any("测试人设" in line for line in printed)


def test_persona_name_saves_merged_snapshot(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(
        ps,
        "get_persona_settings",
        lambda: {"agent_name": "星瑶", "persona": "人设内容", "user_title": "老板"},
    )
    monkeypatch.setattr(ps, "save_persona_settings", lambda *args: calls.append(args))

    main.handle_slash_command("/persona name 小瑶")

    assert calls == [("小瑶", "人设内容", "老板")]
    assert any("小瑶" in line for line in printed)


def test_persona_text_over_limit_rejected(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(ps, "get_persona_settings", lambda: {"agent_name": "星瑶", "persona": "", "user_title": ""})

    # 模拟核心层校验：超限抛 PersonaSettingsError（与真实 save_persona_settings 一致）
    def _fake_save(agent_name, persona, user_title=""):
        if len(persona) > ps.MAX_PERSONA_LEN:
            raise ps.PersonaSettingsError(f"人设内容过长（最多 {ps.MAX_PERSONA_LEN} 字符）")
        calls.append((agent_name, persona, user_title))

    monkeypatch.setattr(ps, "save_persona_settings", _fake_save)

    main.handle_slash_command(f"/persona text {'字' * 20001}")

    assert calls == []
    assert any("过长" in line for line in printed)


def test_persona_reset_restores_defaults(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.persona_settings as ps
    monkeypatch.setattr(ps, "get_persona_settings", lambda: {"agent_name": "自定义", "persona": "x", "user_title": "y"})
    monkeypatch.setattr(ps, "save_persona_settings", lambda *args: calls.append(args))

    main.handle_slash_command("/persona reset")

    assert calls == [(ps.DEFAULT_AGENT_NAME, "", "")]


# ---------------------------------------------------------------------------
# /shell
# ---------------------------------------------------------------------------

def _fake_shell_options():
    return [
        {"id": "cmd", "label": "命令提示符", "description": "Windows Cmd", "available": True,
         "executable": "C:/Windows/System32/cmd.exe", "unavailable_reason": ""},
        {"id": "pwsh", "label": "PowerShell", "description": "PowerShell 7", "available": False,
         "executable": "", "unavailable_reason": "未安装"},
    ]


def test_shell_list_marks_current_and_unavailable(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.command_shell as cs
    monkeypatch.setattr(cs, "list_command_shell_options", _fake_shell_options)
    monkeypatch.setattr(cs, "get_selected_command_shell", lambda: "cmd")

    main.handle_slash_command("/shell")

    assert any("cmd" in line for line in printed)
    assert any("（当前）" in line for line in printed)
    assert any("未安装" in line for line in printed)


def test_shell_set_success_and_failures(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    calls = []

    import core.command_shell as cs
    monkeypatch.setattr(cs, "list_command_shell_options", _fake_shell_options)
    monkeypatch.setattr(cs, "set_selected_command_shell", lambda shell_id: calls.append(shell_id))

    main.handle_slash_command("/shell pwsh")
    assert calls == []
    assert any("未安装" in line for line in printed)

    main.handle_slash_command("/shell nope")
    assert calls == []
    assert any("不支持" in line for line in printed)

    main.handle_slash_command("/shell cmd")
    assert calls == ["cmd"]
    assert any("命令提示符" in line for line in printed)


# ---------------------------------------------------------------------------
# /open
# ---------------------------------------------------------------------------

def test_open_default_workdir_and_relative_path(monkeypatch, tmp_path):
    opened = []
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path))

    import core.workspace_context as wc
    monkeypatch.setattr(wc, "get_workdir", lambda: str(tmp_path))

    (tmp_path / "note.txt").write_text("hi", encoding="utf-8")

    main.handle_slash_command("/open")
    assert opened == [os.path.abspath(str(tmp_path))]

    main.handle_slash_command("/open note.txt")
    assert opened[-1] == os.path.abspath(str(tmp_path / "note.txt"))


def test_open_missing_path_reports_error(monkeypatch, tmp_path):
    opened = []
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(os, "startfile", lambda path: opened.append(path))

    import core.workspace_context as wc
    monkeypatch.setattr(wc, "get_workdir", lambda: str(tmp_path))

    main.handle_slash_command("/open 不存在.png")

    assert opened == []
    assert any("路径不存在" in line for line in printed)


# ---------------------------------------------------------------------------
# /skills refresh
# ---------------------------------------------------------------------------

def test_skills_refresh_reports_counts(monkeypatch):
    printed = _capture_print(monkeypatch)
    _quiet_ui(monkeypatch)

    import core.skills as skills_mod
    fake_skills = [
        {"id": "builtin:alpha", "name": "a", "description": "d"},
        {"id": "builtin:beta", "name": "b", "description": "d"},
        {"id": "user:gamma", "name": "g", "description": "d"},
    ]
    monkeypatch.setattr(skills_mod, "scan_all_skills", lambda: fake_skills)
    monkeypatch.setattr(skills_mod, "get_active_skill_ids", lambda: ["builtin:alpha", "user:gamma"])

    main.handle_slash_command("/skills refresh")

    assert any("共 3 个技能" in line for line in printed)
    assert any("已启用 2 个" in line for line in printed)
