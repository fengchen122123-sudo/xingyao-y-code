import os

import pytest

import main
from core import session_selector as selector_module
from core.memory import MemoryManager


def _capture_print(monkeypatch):
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def _quiet_ui(monkeypatch):
    monkeypatch.setattr(main, "app_instance", None)


@pytest.fixture
def manager(tmp_path):
    mm = MemoryManager(str(tmp_path / "archive_cli.db"))
    yield mm
    mm.conn.close()


def _make_session(mm, name, text="hello"):
    mm.ensure_session(name, workdir=os.getcwd(), source="CLI")
    mm.add_message(name, "user", text)
    return name


def _archived_at_of(mm, name):
    row = mm.conn.execute(
        "SELECT archived_at FROM sessions WHERE session_name = ?", (name,)
    ).fetchone()
    return row[0] if row else "NO_SUCH_SESSION"


def _use_slash_env(monkeypatch, mm, current="s1"):
    _quiet_ui(monkeypatch)
    monkeypatch.setattr(main, "manager", mm)
    monkeypatch.setattr(main, "current_session", current)


def test_sessions_archive_marks_target_and_reports(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")
    _make_session(manager, "s2")

    main.handle_slash_command("/sessions archive s2")

    assert _archived_at_of(manager, "s2")
    assert any("已归档" in line and "s2" in line for line in printed)


def test_sessions_archive_missing_target_reports_and_does_nothing(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")

    main.handle_slash_command("/sessions archive ghost")

    assert any("不存在" in line and "ghost" in line for line in printed)
    assert not any("✔" in line for line in printed)
    assert _archived_at_of(manager, "ghost") == "NO_SUCH_SESSION"


def test_sessions_archive_current_session_is_refused(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s2")
    _make_session(manager, "s1")
    _make_session(manager, "s2")

    main.handle_slash_command("/sessions archive s2")

    assert _archived_at_of(manager, "s2") is None
    assert any("不能归档当前活跃会话" in line for line in printed)


def test_sessions_archive_without_name_shows_usage_error(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")

    main.handle_slash_command("/sessions archive")

    assert any("用法错误" in line for line in printed)
    assert "archive" not in {s["name"] for s in manager.get_all_sessions()}


def test_sessions_unarchive_restores_target(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")

    main.handle_slash_command("/sessions unarchive s2")

    assert _archived_at_of(manager, "s2") is None
    assert any("已取消归档" in line and "s2" in line for line in printed)


def test_sessions_unarchive_missing_target_reports(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")

    main.handle_slash_command("/sessions unarchive ghost")

    assert any("不存在" in line and "ghost" in line for line in printed)
    assert not any("✔" in line for line in printed)


def test_sessions_unarchive_without_name_shows_usage_error(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")

    main.handle_slash_command("/sessions unarchive")

    assert any("用法错误" in line for line in printed)
    assert "unarchive" not in {s["name"] for s in manager.get_all_sessions()}


def test_sessions_archived_lists_only_archived_with_timestamp(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    for name in ("s1", "s2", "s3"):
        _make_session(manager, name)
    manager.archive_session("s2")
    manager.archive_session("s3")

    main.handle_slash_command("/sessions archived")

    text = "\n".join(printed)
    assert "s2" in text and "s3" in text
    assert "s1" not in text
    assert "归档于" in text
    # 格式对齐 /sessions all：消息数 + 归属目录（captured 文本是 HTML 的 repr，
    # Windows 路径反斜杠被转义，因此只比对目录名）
    assert "条消息" in text and os.path.basename(os.getcwd()) in text


def test_sessions_archived_empty_state(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")

    main.handle_slash_command("/sessions archived")

    assert any("没有任何已归档会话" in line for line in printed)


def test_sessions_all_hides_archived_session(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")

    main.handle_slash_command("/sessions all")

    text = "\n".join(printed)
    assert "s1" in text
    assert "s2" not in text


def test_sessions_archive_is_idempotent_and_keeps_original_timestamp(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")
    _make_session(manager, "s2")

    main.handle_slash_command("/sessions archive s2")
    first_stamp = _archived_at_of(manager, "s2")
    printed.clear()

    main.handle_slash_command("/sessions archive s2")

    assert _archived_at_of(manager, "s2") == first_stamp
    assert any("已归档" in line and "s2" in line for line in printed)


def test_switch_to_archived_session_auto_unarchives(monkeypatch, manager):
    printed = _capture_print(monkeypatch)
    _use_slash_env(monkeypatch, manager, current="s1")
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")
    for name in ("sync_plan_session", "recalc_token_usage", "print_logo", "show_session_history"):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: None)

    main.handle_slash_command("/sessions s2")

    assert main.current_session == "s2"
    assert _archived_at_of(manager, "s2") is None
    assert any("取消归档" in line for line in printed)


class _FakeEvent:
    def __init__(self, app):
        self.app = app


class _FakeApplication:

    instances = []
    script = []

    def __init__(self, layout=None, key_bindings=None, style=None, full_screen=False):
        self.layout = layout
        self.key_bindings = key_bindings
        self.exited = False
        self.renders = []
        _FakeApplication.instances.append(self)

    def exit(self):
        self.exited = True

    def run(self):
        self.renders.append(self.snapshot())
        for key in _FakeApplication.script:
            if self.exited:
                break
            bindings = self.key_bindings.get_bindings_for_keys((key,))
            assert bindings, f"会话管家未绑定按键: {key}"
            bindings[0].handler(_FakeEvent(self))
            self.renders.append(self.snapshot())

    def snapshot(self):
        control = self.layout.container.children[0].content
        return "".join(text for _style, text in control.text())


KEY_TAB = "c-i"
KEY_ENTER = "c-m"


@pytest.fixture
def fake_app(monkeypatch):
    _FakeApplication.instances = []
    _FakeApplication.script = []
    monkeypatch.setattr(selector_module, "Application", _FakeApplication)
    return _FakeApplication


def _open_selector(fake_app, mm, keys, current="s1", on_switch=None):
    fake_app.script = list(keys)
    selector_module.open_session_selector(
        on_switch=on_switch, manager=mm, current_session=current
    )
    return fake_app.instances[-1]


def test_selector_archive_removes_row_and_pulls_cursor_back(fake_app, manager):
    for name in ("s1", "s2", "s3"):
        _make_session(manager, name)

    app = _open_selector(fake_app, manager, ["down", "down", "a"], current="s3")
    last = app.renders[-1]

    assert "已归档：s1" in last
    assert "[    ] s1" not in last
    assert " > [    ] s2" in last
    assert _archived_at_of(manager, "s1")


def test_selector_refuses_archiving_current_session(fake_app, manager):
    _make_session(manager, "s1")
    _make_session(manager, "s2")

    app = _open_selector(fake_app, manager, ["a"], current="s2")
    last = app.renders[-1]

    assert "不能归档当前活跃会话" in last
    assert _archived_at_of(manager, "s2") is None


def test_selector_tab_toggles_archived_view(fake_app, manager):
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")

    app = _open_selector(fake_app, manager, [KEY_TAB], current="s1")
    first, last = app.renders[0], app.renders[-1]

    assert "s2" not in first
    assert "[    ] s2" in last
    assert "[当前] s1" not in last
    assert "[Tab] 回到活跃列表" in last


def test_selector_unarchive_from_archived_view(fake_app, manager):
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")

    app = _open_selector(fake_app, manager, [KEY_TAB, "u"], current="s1")
    last = app.renders[-1]

    assert "已取消归档：s2" in last
    assert "[    ] s2" not in last
    assert _archived_at_of(manager, "s2") is None


def test_selector_footer_hints_archive_keys(fake_app, manager):
    _make_session(manager, "s1")

    app = _open_selector(fake_app, manager, [], current="s1")

    assert "[A] 归档" in app.renders[0]
    assert "[Tab]" in app.renders[0]


def test_selector_enter_on_archived_session_unarchives(fake_app, manager):
    _make_session(manager, "s1")
    _make_session(manager, "s2")
    manager.archive_session("s2")
    switched = []

    _open_selector(
        fake_app, manager, [KEY_TAB, KEY_ENTER], current="s1", on_switch=switched.append
    )

    assert switched == ["s2"]
    assert _archived_at_of(manager, "s2") is None
