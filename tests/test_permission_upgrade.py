import threading
from concurrent.futures import Future

import pytest

import core.app_context as app_context
import core.llm as llm
from core.permission_broker import PermissionBroker


class _FakeLoop:
    @staticmethod
    def call_soon_threadsafe(callback):
        callback()


class _FakeApp:
    loop = _FakeLoop()

    @staticmethod
    def invalidate():
        return None


@pytest.fixture
def interactive_env(monkeypatch):
    monkeypatch.setattr(llm.app_context, "get_app_instance", lambda: _FakeApp())
    monkeypatch.setattr(llm.app_context, "get_ui_flavor", lambda: "cli")
    monkeypatch.setattr(llm, "safe_print", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm.app_context, "get_current_session", lambda: "session-test")
    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"allow_outside_workspace": False},
    )


@pytest.fixture
def clean_permission_state():
    from core import permission_grants
    import core.tools as tools_mod

    permission_grants._clear_all_for_tests()
    llm._last_tracked_session = None
    tools_mod._clear_sandbox_state_for_tests()
    yield
    tools_mod._clear_sandbox_state_for_tests()
    permission_grants._clear_all_for_tests()


def _auto_answer_broker(monkeypatch, choice, asked, requests):
    broker = PermissionBroker()

    def activate(request):
        asked.append(request.function_name)
        requests.append(request)
        broker.resolve(request.request_id, choice)

    broker.set_prompt_handlers(activate, lambda: None)
    monkeypatch.setattr(llm, "permission_broker", broker)
    return broker


def test_accept_edits_auto_approves_queries_and_edits_but_asks_sensitive_commands(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    monkeypatch.chdir(tmp_path)
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 0, asked, requests)

    assert llm.check_permission_and_ask(
        "write_file", {"file_path": str(tmp_path / "a.py"), "content": "x"}
    ) is True
    assert llm.check_permission_and_ask(
        "edit_file", {"file_path": str(tmp_path / "a.py"), "replacement_content": "y"}
    ) is True
    assert asked == []

    assert llm.check_permission_and_ask("execute_command", {"command": "git status"}) is True
    assert asked == []
    assert llm.check_permission_and_ask(
        "execute_command",
        {"command": "git push origin dev"},
    ) is True
    assert asked == ["execute_command"]


def test_accept_edits_still_asks_sandbox_outside_access(
    interactive_env, clean_permission_state, monkeypatch
):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "accept_edits")
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 0, asked, requests)

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/outside/x.txt", "operation": "read_file"}
    ) is True
    assert asked == ["sandbox_outside_access"]


def test_full_mode_still_asks_sandbox_outside_access(
    interactive_env, clean_permission_state, monkeypatch
):
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "full")
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 0, asked, requests)

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/outside/x.txt", "operation": "read_file"}
    ) is True
    assert asked == ["sandbox_outside_access"]


def test_sandbox_outside_access_third_option_sets_session_bypass(
    interactive_env, clean_permission_state, monkeypatch
):
    import core.tools as tools_mod

    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/outside/x.txt", "operation": "read_file"}
    ) is True
    assert len(requests[0].options) == 3
    assert "任务" in requests[0].options[2]
    assert tools_mod.is_session_sandbox_bypass_enabled() is True

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/other/y.txt", "operation": "read_file"}
    ) is True
    assert asked == ["sandbox_outside_access"]


def test_task_sandbox_bypass_does_not_bypass_confirm_write_permission(
    interactive_env, clean_permission_state, monkeypatch
):
    import core.tools as tools_mod

    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    tools_mod.set_session_sandbox_bypass(True)
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 1, asked, requests)

    assert llm.check_permission_and_ask(
        "sandbox_outside_access",
        {"path": "D:/outside/write.txt", "operation": "write_file"},
    ) is False
    assert asked == ["sandbox_outside_access"]
    assert requests[0].arguments["operation"] == "write_file"


def test_raw_terminal_credential_request_rejects_always_option(
    clean_permission_state, monkeypatch
):
    import builtins
    import core.tools as tools_mod
    from core.permission_broker import PermissionOption, PermissionOptionKey

    monkeypatch.setattr(builtins, "input", lambda _prompt: "a")
    monkeypatch.setattr(builtins, "print", lambda *_args, **_kwargs: None)
    tools_mod.set_session_sandbox_bypass(False)
    options = (
        PermissionOption(PermissionOptionKey.ALLOW_ONCE.value, "允许本次"),
        PermissionOption(PermissionOptionKey.DENY.value, "拒绝"),
    )

    assert llm.ask_in_raw_terminal(
        "sandbox_outside_access",
        {"path": "D:/outside/config.json", "operation": "write_file"},
        options=options,
    ) is False
    assert tools_mod.is_session_sandbox_bypass_enabled() is False


def test_raw_terminal_normal_sandbox_request_accepts_task_bypass_option(
    clean_permission_state, monkeypatch
):
    import builtins
    import core.tools as tools_mod
    from core.permission_broker import PermissionOption, PermissionOptionKey

    monkeypatch.setattr(builtins, "input", lambda _prompt: "a")
    monkeypatch.setattr(builtins, "print", lambda *_args, **_kwargs: None)
    options = (
        PermissionOption(PermissionOptionKey.ALLOW_ONCE.value, "允许本次"),
        PermissionOption(PermissionOptionKey.DENY.value, "拒绝"),
        PermissionOption(
            PermissionOptionKey.ALLOW_TASK_SANDBOX.value,
            "本任务内放行沙箱外访问",
        ),
    )

    assert llm.ask_in_raw_terminal(
        "sandbox_outside_access",
        {"path": "D:/outside/read.txt", "operation": "read_file"},
        options=options,
    ) is True
    assert tools_mod.is_session_sandbox_bypass_enabled() is True


def test_visible_session_switch_preserves_each_task_sandbox_bypass(
    clean_permission_state, monkeypatch
):
    import core.tools as tools_mod

    state = {"session": "s1"}
    monkeypatch.setattr(llm.app_context, "get_current_session", lambda: state["session"])

    llm.check_session_reset()
    tools_mod.set_session_sandbox_bypass(True)
    state["session"] = "s2"
    llm.check_session_reset()

    assert tools_mod.is_session_sandbox_bypass_enabled() is False

    state["session"] = "s1"
    assert tools_mod.is_session_sandbox_bypass_enabled() is True


def test_canonical_permission_path_resolves_relative_against_workdir(
    clean_permission_state, monkeypatch, tmp_path
):
    import os
    from pathlib import Path

    from core import permission_store
    from core.workspace_context import workspace_scope

    workspace = tmp_path / "project"
    (workspace / "src").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)

    with workspace_scope(str(workspace)):
        canonical = llm._canonical_permission_path("src/app.py")
        assert canonical == permission_store.canonical_file_path("src/app.py")
        assert canonical == os.path.normcase(str((workspace / "src" / "app.py")))

        llm._grant_write_add(llm._canonical_permission_path("src/app.py"))
        assert llm._grant_write_hit(llm._canonical_permission_path("src/app.py"))
        monkeypatch.chdir(workspace)
        assert llm._grant_write_hit(llm._canonical_permission_path("src/app.py"))

        # 绝对路径行为与 realpath+normcase 一致
        abs_path = str(workspace / "src" / "app.py")
        assert llm._canonical_permission_path(abs_path) == os.path.normcase(
            str(Path(abs_path).resolve())
        )


def test_always_allow_write_file_persists_across_restart(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    target = str(tmp_path / "a.py")

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 3, asked, requests)
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "x"}) is True

    canonical = permission_store.canonical_file_path(target)
    assert canonical in permission_store.project_rules()["write_files"]

    llm.session_allowed_write_files.clear()
    asked2, requests2 = [], []
    _auto_answer_broker(monkeypatch, 0, asked2, requests2)
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "y"}) is True
    assert asked2 == []

    assert permission_store.clear_project() >= 1
    asked3, requests3 = [], []
    _auto_answer_broker(monkeypatch, 0, asked3, requests3)
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "z"}) is True
    assert asked3 == ["write_file"]


def test_project_command_rule_matches_prefix_family(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 3, asked, requests)
    assert llm.check_permission_and_ask(
        "execute_command",
        {"command": "git push origin"},
    ) is True

    rules = permission_store.project_rules()["commands"]
    assert ("git", "push", "origin") in rules

    llm.session_allowed_commands.clear()
    asked2, requests2 = [], []
    _auto_answer_broker(monkeypatch, 0, asked2, requests2)
    assert llm.check_permission_and_ask(
        "execute_command",
        {"command": "git push origin dev"},
    ) is True
    assert asked2 == []
    asked3, requests3 = [], []
    _auto_answer_broker(monkeypatch, 0, asked3, requests3)
    assert llm.check_permission_and_ask(
        "execute_command",
        {"command": "git commit -m change"},
    ) is True
    assert asked3 == ["execute_command"]


def test_shell_override_never_reuses_default_shell_command_allowlist(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    llm.check_session_reset()
    llm._grant_command_add(("git", "push"))

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 0, asked, requests)

    assert llm.check_permission_and_ask(
        "execute_command",
        {"command": "git push origin dev", "shell_id": "cmd"},
    ) is True
    assert asked == ["execute_command"]
    assert len(requests[0].options) == 2
    assert requests[0].arguments["shell_id"] == "cmd"


def test_inline_exec_command_always_allow_is_session_exact_only(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")

    inline_cmd = 'python -c "print(1)"'
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)
    assert llm.check_permission_and_ask("execute_command", {"command": inline_cmd}) is True

    assert permission_store.project_rules()["commands"] == set()
    assert llm.session_allowed_commands == set()
    assert inline_cmd in llm.session_allowed_exact_commands

    asked2, requests2 = [], []
    _auto_answer_broker(monkeypatch, 0, asked2, requests2)
    assert llm.check_permission_and_ask("execute_command", {"command": inline_cmd}) is True
    assert asked2 == []

    asked3, requests3 = [], []
    _auto_answer_broker(monkeypatch, 0, asked3, requests3)
    assert llm.check_permission_and_ask("execute_command", {"command": 'python -c "print(2)"'}) is True
    assert asked3 == ["execute_command"]


def test_permission_store_isolated_by_workdir(tmp_path, monkeypatch):
    from core import permission_store

    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    assert permission_store.add_command_rule(("git", "status"), workdir=proj_a) is True
    assert ("git", "status") in permission_store.project_rules(workdir=proj_a)["commands"]
    assert permission_store.project_rules(workdir=proj_b)["commands"] == set()
    assert permission_store.clear_project(workdir=proj_a) == 1
    assert permission_store.project_rules(workdir=proj_a)["commands"] == set()


def test_manual_add_and_remove_command_rule(tmp_path, monkeypatch):
    import config
    from core import permission_store

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()

    added = permission_store.add_rule("command", "git push origin", workdir=project)
    assert added.status == "added"
    assert added.normalized_value == "git push origin"
    assert ("git", "push", "origin") in permission_store.project_rules(project)["commands"]

    duplicate = permission_store.add_rule("command", "git push origin", workdir=project)
    assert duplicate.status == "exists"

    removed = permission_store.remove_rule("command", "git push origin", workdir=project)
    assert removed.status == "removed"
    assert permission_store.project_rules(project)["commands"] == set()

    missing = permission_store.remove_rule("command", "git push origin", workdir=project)
    assert missing.status == "not_found"


def test_file_rule_is_exact_and_canonical(tmp_path, monkeypatch):
    import config
    from core import permission_store

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()

    result = permission_store.add_rule("write", "src/app.py", workdir=project)
    expected = permission_store.canonical_file_path(project / "src" / "app.py")

    assert result.normalized_value == expected
    assert expected in permission_store.project_rules(project)["write_files"]


def test_store_write_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    import config
    from core import permission_store

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path / "data")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        permission_store,
        "_save",
        lambda _data: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(permission_store.PermissionStoreError, match="disk full"):
        permission_store.add_rule("edit", str(project / "a.py"), workdir=project)


def _bare_bridge():
    from gui_bridge import ApiBridge

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._perm_lock = threading.Lock()
    bridge._perm_timers = {}
    bridge._turn_owner_sessions = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge.emit_event = lambda *args, **kwargs: None
    return bridge


def test_gui_set_sandbox_enabled_writes_config(monkeypatch):
    import config

    saved = {}
    monkeypatch.setattr(config, "save_config_value", lambda key, value: saved.update({key: value}) or True)

    bridge = _bare_bridge()
    res = bridge.set_sandbox_enabled(True)
    assert res["ok"] is True
    assert saved.get("allow_outside_workspace") is True

    res = bridge.set_sandbox_enabled(False)
    assert res["ok"] is True
    assert saved.get("allow_outside_workspace") is False


def _make_request(function_name, options):
    from core.permission_broker import PermissionRequest

    return PermissionRequest(
        request_id=f"permission-{function_name}",
        function_name=function_name,
        arguments={},
        owner_id="owner-1",
        parent_turn_id="session:s1|turn-1",
        source="主代理",
        options=tuple(options),
        future=Future(),
    )


def test_gui_permission_request_has_no_timeout_timer(monkeypatch):
    from gui_bridge import _session_from_turn_id

    bridge = _bare_bridge()
    request = _make_request("execute_command", ["y", "n", "a"])
    bridge._activate_permission_request(request)
    assert bridge._perm_timers == {}


def test_gui_ask_user_still_has_timeout_timer():
    bridge = _bare_bridge()
    request = _make_request("ask_user", ["方案A", "方案B"])
    bridge._activate_permission_request(request)
    try:
        assert len(bridge._perm_timers) == 1
        timer = next(iter(bridge._perm_timers.values()))
        assert timer.is_alive()
    finally:
        bridge._cancel_perm_timer(request.request_id)
    assert bridge._perm_timers == {}


def test_gui_add_and_remove_permission_rule(monkeypatch):
    from core import permission_store

    bridge = _bare_bridge()
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda kind, value: type(
            "R",
            (),
            {"status": "added", "rule_type": kind, "normalized_value": value},
        )(),
    )
    monkeypatch.setattr(
        permission_store,
        "remove_rule",
        lambda kind, value: type(
            "R",
            (),
            {"status": "removed", "rule_type": kind, "normalized_value": value},
        )(),
    )

    assert bridge.add_permission_rule("command", "git status")["status"] == "added"
    assert bridge.remove_permission_rule("command", "git status")["status"] == "removed"


def test_gui_permission_rule_failure_statuses_are_not_reported_as_success(monkeypatch):
    from core import permission_store

    bridge = _bare_bridge()
    monkeypatch.setattr(
        permission_store,
        "add_rule",
        lambda kind, value: type(
            "R",
            (),
            {"status": "limit_reached", "rule_type": kind, "normalized_value": value},
        )(),
    )
    monkeypatch.setattr(
        permission_store,
        "remove_rule",
        lambda kind, value: type(
            "R",
            (),
            {"status": "not_found", "rule_type": kind, "normalized_value": value},
        )(),
    )

    assert bridge.add_permission_rule("command", "git status")["ok"] is False
    assert bridge.remove_permission_rule("command", "git status")["ok"] is False


def test_gui_permission_event_exposes_keyed_options():
    from core.permission_broker import PermissionBroker, PermissionOption

    emitted = []
    bridge = _bare_bridge()
    bridge.emit_event = lambda kind, payload: emitted.append((kind, payload))
    broker = PermissionBroker(activate_prompt=lambda _request: None)
    request = broker.enqueue(
        "write_file",
        {"file_path": "a.py"},
        "owner",
        "session:s1|turn-1",
        "主代理",
        options=(
            PermissionOption("allow_once", "允许本次"),
            PermissionOption("deny", "拒绝"),
        ),
    )

    bridge._activate_permission_request(request)

    assert emitted[0][1]["permission_options"] == [
        {"key": "allow_once", "label": "允许本次"},
        {"key": "deny", "label": "拒绝"},
    ]
