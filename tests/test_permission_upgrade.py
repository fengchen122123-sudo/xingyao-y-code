"""权限控制统一化改造（GUI 与 CLI 一致）的核心行为回归。

覆盖：
- accept_edits 第四档模式：工作区内文件编辑自动放行、命令与沙盒外访问仍询问
- 沙盒外访问第三选项：choice 2 置位会话级放行，后续免询问；切会话收回
- 「始终允许」项目级持久化：写 permissions.json、模拟重启后仍放行、内联命令不落盘
- GUI：沙盒开关写配置；权限请求不挂超时定时器（与 TUI 无限等待对齐）、ask_user 仍挂
"""

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
    """让 check_permission_and_ask 走 broker 路径而非阻塞的 raw input()。"""
    monkeypatch.setattr(llm.app_context, "get_app_instance", lambda: _FakeApp())
    monkeypatch.setattr(llm.app_context, "get_ui_flavor", lambda: "tui")
    monkeypatch.setattr(llm, "safe_print", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm.app_context, "get_current_session", lambda: "session-test")


@pytest.fixture
def clean_permission_state():
    """清空会话白名单与沙盒会话放行，退出时同样复位，避免污染其他测试。"""
    import core.tools as tools_mod

    llm.session_allowed_commands.clear()
    llm.session_allowed_write_files.clear()
    llm.session_allowed_edit_files.clear()
    llm._last_tracked_session = None
    tools_mod.set_session_sandbox_bypass(False)
    yield
    tools_mod.set_session_sandbox_bypass(False)
    llm.session_allowed_commands.clear()
    llm.session_allowed_write_files.clear()
    llm.session_allowed_edit_files.clear()


def _auto_answer_broker(monkeypatch, choice, asked, requests):
    """构造一个同步应答的 broker：activate 即按预定 choice resolve。

    asked 记录被询问的工具名，requests 保存原始请求（供断言选项列表）。
    """
    broker = PermissionBroker()

    def activate(request):
        asked.append(request.function_name)
        requests.append(request)
        broker.resolve(request.request_id, choice)

    broker.set_prompt_handlers(activate, lambda: None)
    monkeypatch.setattr(llm, "permission_broker", broker)
    return broker


# ---------------------------------------------------------------------------
# accept_edits 第四档模式
# ---------------------------------------------------------------------------

def test_accept_edits_auto_approves_file_edits_but_asks_commands(
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
    # 编辑直接放行，不应有任何询问
    assert asked == []

    assert llm.check_permission_and_ask("execute_command", {"command": "git status"}) is True
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
    """铁律回归：full 模式也不自动放行沙盒外访问。"""
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "full")
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 0, asked, requests)

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/outside/x.txt", "operation": "read_file"}
    ) is True
    assert asked == ["sandbox_outside_access"]


# ---------------------------------------------------------------------------
# 沙盒外访问第三选项（会话级放行）
# ---------------------------------------------------------------------------

def test_sandbox_outside_access_third_option_sets_session_bypass(
    interactive_env, clean_permission_state, monkeypatch
):
    import core.tools as tools_mod

    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)  # 2 = 始终允许（本会话）

    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/outside/x.txt", "operation": "read_file"}
    ) is True
    # 选项恢复为 3 项，第三项是会话级放行
    assert len(requests[0].options) == 3
    assert "会话" in requests[0].options[2]
    assert tools_mod.is_session_sandbox_bypass_enabled() is True

    # 同会话第二次访问：免询问自动放行
    assert llm.check_permission_and_ask(
        "sandbox_outside_access", {"path": "D:/other/y.txt", "operation": "write_file"}
    ) is True
    assert asked == ["sandbox_outside_access"]


def test_check_session_reset_clears_session_sandbox_bypass(
    clean_permission_state, monkeypatch
):
    import core.tools as tools_mod

    state = {"session": "s1"}
    monkeypatch.setattr(llm.app_context, "get_current_session", lambda: state["session"])

    llm.check_session_reset()  # 建立 s1 基线
    tools_mod.set_session_sandbox_bypass(True)
    state["session"] = "s2"
    llm.check_session_reset()  # 切会话收回

    assert tools_mod.is_session_sandbox_bypass_enabled() is False


# ---------------------------------------------------------------------------
# 「始终允许」项目级持久化
# ---------------------------------------------------------------------------

def test_always_allow_write_file_persists_across_restart(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")
    target = str(tmp_path / "a.py")

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)  # 始终允许
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "x"}) is True

    canonical = permission_store.canonical_file_path(target)
    assert canonical in permission_store.project_rules()["write_files"]

    # 模拟重启：内存会话白名单清空，仍应从持久化规则自动放行（不询问）
    llm.session_allowed_write_files.clear()
    asked2, requests2 = [], []
    _auto_answer_broker(monkeypatch, 0, asked2, requests2)
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "y"}) is True
    assert asked2 == []

    # 清空持久化规则后回到询问流程
    assert permission_store.clear_project() >= 1
    asked3, requests3 = [], []
    _auto_answer_broker(monkeypatch, 0, asked3, requests3)
    assert llm.check_permission_and_ask("write_file", {"file_path": target, "content": "z"}) is True
    assert asked3 == ["write_file"]


def test_always_allow_command_rule_matches_prefix_family(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)
    assert llm.check_permission_and_ask("execute_command", {"command": "git status"}) is True

    rules = permission_store.project_rules()["commands"]
    assert ("git", "status") in rules

    # 同前缀族的后续命令免询问（模拟重启：仅靠持久化规则）
    llm.session_allowed_commands.clear()
    asked2, requests2 = [], []
    _auto_answer_broker(monkeypatch, 0, asked2, requests2)
    assert llm.check_permission_and_ask("execute_command", {"command": "git status --short"}) is True
    assert asked2 == []
    # 前缀族之外的命令仍询问
    asked3, requests3 = [], []
    _auto_answer_broker(monkeypatch, 0, asked3, requests3)
    assert llm.check_permission_and_ask("execute_command", {"command": "git push origin main"}) is True
    assert asked3 == ["execute_command"]


def test_inline_exec_command_never_persisted(
    interactive_env, clean_permission_state, monkeypatch, tmp_path
):
    from core import permission_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(llm.app_context, "get_permission_mode", lambda: "confirm")

    asked, requests = [], []
    _auto_answer_broker(monkeypatch, 2, asked, requests)
    assert llm.check_permission_and_ask(
        "execute_command", {"command": 'python -c "print(1)"'}
    ) is True

    # 内联执行命令不生成任何持久化规则（防投毒）
    assert permission_store.project_rules()["commands"] == set()
    assert llm.session_allowed_commands == set()


def test_permission_store_isolated_by_workdir(tmp_path, monkeypatch):
    from core import permission_store

    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    assert permission_store.add_command_rule(("git", "status"), workdir=proj_a) is True
    assert ("git", "status") in permission_store.project_rules(workdir=proj_a)["commands"]
    # 另一个项目不受影响
    assert permission_store.project_rules(workdir=proj_b)["commands"] == set()
    # 清空只影响目标项目
    assert permission_store.clear_project(workdir=proj_a) == 1
    assert permission_store.project_rules(workdir=proj_a)["commands"] == set()


# ---------------------------------------------------------------------------
# GUI：沙盒开关与超时语义
# ---------------------------------------------------------------------------

def _bare_bridge():
    from gui_bridge import ApiBridge

    bridge = ApiBridge.__new__(ApiBridge)
    bridge._perm_lock = threading.Lock()
    bridge._perm_timers = {}
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
    """权限确认与 TUI 一致无限等待：不再挂 300s 自动拒绝定时器。"""
    from gui_bridge import _session_from_turn_id  # 确保模块可导入

    bridge = _bare_bridge()
    request = _make_request("execute_command", ["y", "n", "a"])
    bridge._activate_permission_request(request)
    assert bridge._perm_timers == {}


def test_gui_ask_user_still_has_timeout_timer():
    """ask_user 提问类保留超时=取消，防止无人值守时回合永久卡死。"""
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
