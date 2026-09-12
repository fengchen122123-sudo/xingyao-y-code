"""Task 14：CLI 电脑操控运行控制（/computer status|pause|resume|stop）。

测试全部经 fake 注入（本地 FakeController 继承共享 fake + monkeypatch
get_computer_control_service / handle_computer_command），绝不真实键鼠、
截屏、热键、浏览器或网络；Ctrl+C 顺序测试只调用 main._cancel_active_turn
的既有取消路径，绝不真实发送 SIGINT。
"""

import json
import threading

import pytest

import main
from core.computer.cli import (
    ComputerCliEvent,
    ComputerCommandResult,
    handle_computer_command,
    make_cli_event_listener,
    render_computer_event,
    render_computer_snapshot,
    stop_active_run_for_cancel,
)
from core.computer.models import (
    ComputerControlError,
    ComputerOwner,
    ComputerRunSnapshot,
)
from tests.computer_fakes import FakeController as _SharedFakeController
from tests.computer_fakes import use_temp_config


def _default_owner():
    return ComputerOwner(
        session_id="session-cli",
        turn_id="turn-cli",
        session_label="CLI 测试会话",
        cancel_event=threading.Event(),
    )


def disabled_snapshot():
    """设置未开启/无活动 run 时 controller.snapshot() 的形状（全默认值）。"""
    return ComputerRunSnapshot()


class FakeController(_SharedFakeController):
    """CLI 运行控制测试用 controller fake（继承共享 FakeController）。

    - 无参构造（brief 样例逐字 ``FakeController()``），owner 缺省自动补齐；
    - ``run_status``/``browser``/``target``/``action_count``/``error_code``
      直接配置 snapshot 形状，snapshot() 返回真实 ComputerRunSnapshot；
    - pause/resume 记录调用并镜像真实 controller 语义：未知 run id 抛
      computer_invalid_state、provider_safety_required 拒绝 resume
      （computer_provider_safety_required）、恢复后回 running；
    - ``pause_error``/``resume_error`` 沿用父类 start_error/stop_error 的
      注入模式；stop 沿用父类记录（stop_calls/stop_reasons）；
    - 绝不触碰真实键鼠、浏览器或截屏。
    """

    def __init__(self, *, owner=None, run_id="run-a", status="running",
                 target="browser", browser="edge", action_count=12, **kwargs):
        super().__init__(owner=owner or _default_owner(), run_id=run_id, **kwargs)
        self.run_status = status
        self.browser = browser
        self.target = target
        self.action_count = action_count
        self.error_code = ""
        self.pause_error = None
        self.resume_error = None
        self.pause_calls = []
        self.resume_calls = []

    def snapshot(self):
        return ComputerRunSnapshot(
            run_id=self.run_id,
            owner_session_id=self.owner.session_id,
            owner_turn_id=self.owner.turn_id,
            status=self.run_status,
            target=self.target if self.run_id else "",
            browser=self.browser if self.run_id else "",
            action_count=self.action_count,
            error_code=self.error_code,
        )

    def _require_run(self, run_id):
        if run_id != self.run_id:
            raise ComputerControlError(
                "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
            )

    def pause(self, run_id, reason):
        self._require_run(run_id)
        if self.pause_error is not None:
            raise self.pause_error
        self.pause_calls.append((run_id, reason))
        self.run_status = "paused"
        return self.snapshot()

    def resume(self, run_id):
        self._require_run(run_id)
        if self.run_status == "provider_safety_required":
            raise ComputerControlError(
                "computer_provider_safety_required",
                "Provider 强制安全检查未确认，不能恢复，只允许停止",
            )
        if self.resume_error is not None:
            raise self.resume_error
        self.resume_calls.append(run_id)
        self.run_status = "running"
        return self.snapshot()

    def stop(self, run_id, reason):
        self._require_run(run_id)
        return super().stop(run_id, reason)


# ---------------------------------------------------------------------------
# brief 样例测试（逐字）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["/computer on", "/computer off", "/computer"])
def test_cli_rejects_permanent_or_incomplete_computer_commands(command):
    result = handle_computer_command(command, FakeController())
    assert result.ok is False
    assert "设置页" in result.text or "用法" in result.text


# ---------------------------------------------------------------------------
# browser 子命令（查看/切换默认浏览器；settings 均经 monkeypatch，绝不碰真实配置）
# ---------------------------------------------------------------------------


def _browser_settings_snapshot(browser="edge", options=()):
    from core.computer.settings import BrowserOption, ComputerSettingsSnapshot

    if not options:
        options = (
            BrowserOption(browser="edge", label="Microsoft Edge", available=True),
            BrowserOption(browser="chrome", label="Google Chrome", available=False,
                          unavailable_reason="未安装"),
        )
    return ComputerSettingsSnapshot(
        browser_control_enabled=True,
        computer_control_enabled=True,
        computer_browser=browser,
        browser_options=tuple(options),
        profile_display_path="C:/profile",
        platform_supported=True,
        generation=1,
    )


def test_cli_browser_without_value_lists_options(monkeypatch):
    from core.computer import settings as settings_mod

    monkeypatch.setattr(
        settings_mod, "load_computer_settings", lambda: _browser_settings_snapshot()
    )
    result = handle_computer_command("/computer browser", FakeController())

    assert result.ok is True
    assert "默认浏览器 edge" in result.text
    assert "chrome" in result.text
    assert "未安装" in result.text
    assert "用法" in result.text
    assert "**" not in result.text


def test_cli_browser_switches_when_no_active_run(monkeypatch):
    from core.computer import settings as settings_mod

    calls = []

    def fake_set(value, active_run):
        calls.append((value, active_run))
        return _browser_settings_snapshot(browser="chrome")

    monkeypatch.setattr(settings_mod, "set_computer_browser", fake_set)
    # run_id 为空 = 无活动 run，active_run 必须为 False
    result = handle_computer_command(
        "/computer browser chrome", FakeController(run_id="", status="idle")
    )

    assert calls == [("chrome", False)]
    assert result.ok is True
    assert "已切换为 chrome" in result.text


def test_cli_browser_rejected_during_active_run(monkeypatch):
    from core.computer import settings as settings_mod

    def fake_set(value, active_run):
        assert active_run is True
        raise ComputerControlError(
            "computer_session_busy", "当前有进行中的电脑操控会话，请先停止后再切换浏览器"
        )

    monkeypatch.setattr(settings_mod, "set_computer_browser", fake_set)
    # 默认 FakeController 即 running 活动态
    result = handle_computer_command("/computer browser edge", FakeController())

    assert result.ok is False
    assert "电脑操控会话" in result.text
    assert "错误码" in result.text


def test_cli_browser_invalid_value_reports_error_code(monkeypatch):
    from core.computer import settings as settings_mod

    def fake_set(value, active_run):
        raise ComputerControlError(
            "computer_invalid_config", "computer_browser 只支持 edge 或 chrome"
        )

    monkeypatch.setattr(settings_mod, "set_computer_browser", fake_set)
    result = handle_computer_command(
        "/computer browser firefox", FakeController(run_id="", status="idle")
    )

    assert result.ok is False
    assert "错误码" in result.text


def test_cli_browser_rejects_extra_arguments():
    result = handle_computer_command(
        "/computer browser edge chrome", FakeController(run_id="", status="idle")
    )

    assert result.ok is False
    assert "用法" in result.text


def test_cli_status_when_disabled_is_plain_text():
    result = render_computer_snapshot(disabled_snapshot())
    assert result == "电脑操控: 已关闭（请在 GUI 系统设置页开启）"
    assert "**" not in result


def test_cli_trace_never_prints_typed_text():
    text = render_computer_event({"action": "type_text", "text_length": 6, "text": "secret"})
    assert text == "电脑操控: 输入 6 个字符"
    assert "secret" not in text


# ---------------------------------------------------------------------------
# 严格 parser 与纯文本 renderer
# ---------------------------------------------------------------------------


def test_cli_rejects_extra_arguments():
    result = handle_computer_command("/computer status extra", FakeController())
    assert result.ok is False
    assert "用法" in result.text


def test_cli_status_running_format_is_fixed():
    snapshot = ComputerRunSnapshot(
        run_id="run-x",
        status="running",
        target="browser",
        browser="edge",
        action_count=12,
    )
    assert render_computer_snapshot(snapshot) == "电脑操控: 运行中 · Edge · 12 步"


def test_cli_status_desktop_target_uses_desktop_label():
    snapshot = ComputerRunSnapshot(
        run_id="run-d",
        status="running",
        target="desktop",
        browser="",
        action_count=3,
    )
    assert render_computer_snapshot(snapshot) == "电脑操控: 运行中 · 桌面 · 3 步"


def test_cli_status_shows_error_code_for_failed_run():
    snapshot = ComputerRunSnapshot(
        run_id="run-f",
        status="failed",
        target="browser",
        browser="chrome",
        action_count=5,
        error_code="computer_secure_desktop",
    )
    text = render_computer_snapshot(snapshot)
    assert text == "电脑操控: 失败 · Chrome · 5 步 · 错误码 computer_secure_desktop"


def test_cli_status_command_renders_from_controller_snapshot():
    controller = FakeController(status="provider_safety_required", action_count=3)
    result = handle_computer_command("/computer status", controller)
    assert result.ok is True
    assert result.text == "电脑操控: Provider 安全检查 · Edge · 3 步"
    assert controller.pause_calls == []
    assert controller.stop_reasons == []


@pytest.mark.parametrize(
    "snapshot",
    [
        ComputerRunSnapshot(),
        ComputerRunSnapshot(run_id="r", status="running", target="browser", browser="edge", action_count=1),
        ComputerRunSnapshot(run_id="r", status="paused", target="desktop", browser="", action_count=2,
                            pause_reason="cli_pause"),
        ComputerRunSnapshot(run_id="r", status="waiting_for_user", target="browser", browser="edge",
                            action_count=9),
        ComputerRunSnapshot(run_id="r", status="stopped", target="browser", browser="edge", action_count=7,
                            stop_reason="cli_ctrl_c"),
        ComputerRunSnapshot(run_id="r", status="failed", target="browser", browser="chrome", action_count=4,
                            error_code="computer_backend_error"),
    ],
)
@pytest.mark.parametrize(
    "event",
    [
        {"action": "type_text", "text_length": 6, "text": "secret"},
        {"action": "navigate", "url": "https://user:pass@example.com/path?token=secret#frag"},
        {"type": "computer_action_started", "action_kind": "keypress"},
        {"action": "click"},
    ],
)
def test_cli_renders_contain_no_markdown_emphasis(snapshot, event):
    status_text = render_computer_snapshot(snapshot)
    event_text = render_computer_event(event)
    assert "*" not in status_text
    assert "**" not in status_text
    assert "*" not in event_text
    assert "**" not in event_text


# ---------------------------------------------------------------------------
# pause/resume/stop 传当前 run id 与错误呈现
# ---------------------------------------------------------------------------


def test_cli_control_commands_pass_current_run_id():
    controller = FakeController(status="paused", run_id="run-live", action_count=7)

    result = handle_computer_command("/computer resume", controller)
    assert result.ok is True
    assert controller.resume_calls == ["run-live"]
    assert result.text == "电脑操控: 运行中 · Edge · 7 步"

    result = handle_computer_command("/computer pause", controller)
    assert result.ok is True
    assert controller.pause_calls == [("run-live", "cli_pause")]
    assert result.text == "电脑操控: 已暂停 · Edge · 7 步"

    result = handle_computer_command("/computer stop", controller)
    assert result.ok is True
    assert controller.stop_reasons == [("run-live", "cli_stop")]
    assert result.text == "电脑操控: 已停止 · Edge · 7 步"


def test_cli_control_commands_require_active_run():
    controller = FakeController(status="stopped", run_id="run-done")
    for command in ("/computer pause", "/computer resume", "/computer stop"):
        result = handle_computer_command(command, controller)
        assert result.ok is False
        assert "没有进行中的操控任务" in result.text
    assert controller.pause_calls == []
    assert controller.resume_calls == []
    assert controller.stop_reasons == []

    idle = FakeController(status="idle", run_id="")
    result = handle_computer_command("/computer stop", idle)
    assert result.ok is False
    assert "没有进行中的操控任务" in result.text


def test_cli_resume_refused_under_provider_safety():
    controller = FakeController(status="provider_safety_required")
    result = handle_computer_command("/computer resume", controller)
    assert result.ok is False
    assert "computer_provider_safety_required" in result.text
    assert "不能恢复" in result.text
    assert controller.resume_calls == []


def test_cli_stop_allowed_under_provider_safety():
    controller = FakeController(status="provider_safety_required")
    result = handle_computer_command("/computer stop", controller)
    assert result.ok is True
    assert controller.stop_reasons == [("run-a", "cli_stop")]


def test_cli_controller_error_surfaces_code_and_message():
    controller = FakeController(status="running")
    controller.pause_error = ComputerControlError(
        "computer_invalid_state", "非法状态转换：running → paused"
    )
    result = handle_computer_command("/computer pause", controller)
    assert result.ok is False
    assert "computer_invalid_state" in result.text
    assert "非法状态转换" in result.text
    assert controller.pause_calls == []


# ---------------------------------------------------------------------------
# 事件脱敏渲染
# ---------------------------------------------------------------------------


def test_cli_trace_shows_only_url_host():
    text = render_computer_event(
        {"action": "navigate", "url": "https://user:pass@example.com/path?token=secret#frag"}
    )
    assert text == "电脑操控: 打开页面 example.com"
    assert "secret" not in text
    assert "user:pass" not in text


def test_cli_trace_derives_length_when_missing():
    text = render_computer_event({"action": "type_text", "text": "abcdef"})
    assert text == "电脑操控: 输入 6 个字符"


def test_cli_trace_accepts_controller_event_shape():
    text = render_computer_event(
        {"type": "computer_action_started", "action_kind": "type_text",
         "text_length": 4, "text": "pw!!"}
    )
    assert text == "电脑操控: 输入 4 个字符"
    assert "pw!!" not in text


def test_cli_trace_unknown_action_falls_back_to_kind():
    assert render_computer_event({"action": "screenshot_magic"}) == "电脑操控: 动作 screenshot_magic"


def test_cli_event_listener_posts_displayable_actions_only():
    posted = []
    listener = make_cli_event_listener(posted.append)
    listener({"type": "computer_state", "run_id": "r", "event_seq": 1,
              "status": "running", "target": "browser", "reason": "start"})
    listener({"type": "computer_action_started", "run_id": "r", "event_seq": 2,
              "action_ordinal": 1, "action_kind": "click", "target": "browser"})
    listener({"type": "computer_action_finished", "run_id": "r", "event_seq": 3,
              "action_ordinal": 1, "action_kind": "click", "ok": True, "error_code": ""})
    listener({"type": "computer_action_finished", "run_id": "r", "event_seq": 4,
              "action_ordinal": 2, "action_kind": "type_text", "ok": False,
              "error_code": "computer_input_rejected"})
    listener({"type": "computer_observation", "run_id": "r", "event_seq": 5,
              "screen_sha256": "sha-fake", "width": 1280, "height": 720})

    assert [event.payload["text"] for event in posted] == [
        "电脑操控: 点击",
        "电脑操控: 动作失败 · 错误码 computer_input_rejected",
    ]
    assert all(isinstance(event, ComputerCliEvent) for event in posted)


# ---------------------------------------------------------------------------
# 永久配置键不可被 CLI 命令修改
# ---------------------------------------------------------------------------


def test_cli_commands_never_touch_permanent_settings(tmp_path, monkeypatch):
    values = {
        "browser_control_enabled": True,
        "computer_control_enabled": False,
        "computer_browser": "chrome",
    }
    cfg = use_temp_config(tmp_path, monkeypatch, values)
    before = cfg.read_text(encoding="utf-8")

    controller = FakeController(status="paused")
    for command in (
        "/computer status",
        "/computer resume",
        "/computer pause",
        "/computer stop",
        "/computer on",
        "/computer off",
        "/computer",
    ):
        handle_computer_command(command, controller)

    # 运行控制命令必须一个字节都不落盘；browser 子命令是唯一例外，单独测
    assert cfg.read_text(encoding="utf-8") == before
    saved = json.loads(before)
    assert saved["browser_control_enabled"] is True
    assert saved["computer_control_enabled"] is False
    assert saved["computer_browser"] == "chrome"


def test_cli_browser_switch_updates_only_browser_key(tmp_path, monkeypatch):
    """browser 子命令是 CLI 唯一允许修改的永久设置，且只允许改 computer_browser。"""
    values = {
        "browser_control_enabled": True,
        "computer_control_enabled": False,
        "computer_browser": "chrome",
    }
    cfg = use_temp_config(tmp_path, monkeypatch, values)

    controller = FakeController(status="idle", run_id="")
    result = handle_computer_command("/computer browser edge", controller)

    assert result.ok is True
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    assert saved["computer_browser"] == "edge"
    assert saved["browser_control_enabled"] is True
    assert saved["computer_control_enabled"] is False


def test_cli_browser_no_write_during_active_run(tmp_path, monkeypatch):
    values = {
        "browser_control_enabled": True,
        "computer_control_enabled": False,
        "computer_browser": "chrome",
    }
    cfg = use_temp_config(tmp_path, monkeypatch, values)
    before = cfg.read_text(encoding="utf-8")

    controller = FakeController(status="paused")
    result = handle_computer_command("/computer browser chrome", controller)

    assert result.ok is False
    assert "错误码" in result.text
    assert cfg.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Ctrl+C：先 controller.stop 再现有 Turn cancel
# ---------------------------------------------------------------------------


def test_stop_active_run_for_cancel_passes_cli_ctrl_c():
    controller = FakeController(status="running", run_id="run-live")
    assert stop_active_run_for_cancel(controller) is True
    assert controller.stop_reasons == [("run-live", "cli_ctrl_c")]


def test_stop_active_run_for_cancel_ignores_finished_runs():
    controller = FakeController(status="stopped", run_id="run-done")
    assert stop_active_run_for_cancel(controller) is False
    assert controller.stop_reasons == []

    idle = FakeController(status="idle", run_id="")
    assert stop_active_run_for_cancel(idle) is False
    assert idle.stop_reasons == []


def test_stop_active_run_for_cancel_tolerates_race():
    controller = FakeController(status="running", run_id="run-live")
    controller.stop_error = ComputerControlError(
        "computer_invalid_state", "未知的电脑操控 run：run-live"
    )
    assert stop_active_run_for_cancel(controller) is False


def test_ctrl_c_stops_computer_run_before_turn_cancel(monkeypatch):
    import core.tools as tools

    order = []
    controller = FakeController(status="running", run_id="run-live")
    original_stop = controller.stop

    def traced_stop(run_id, reason):
        order.append("computer_stop")
        return original_stop(run_id, reason)

    controller.stop = traced_stop
    cancel_event = threading.Event()
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: controller
    )
    monkeypatch.setattr(main, "active_cancel_event", cancel_event)
    monkeypatch.setattr(main, "active_turn_id", "turn-live", raising=False)
    monkeypatch.setattr(main, "current_session", "session-live")
    monkeypatch.setattr(
        main.permission_broker, "cancel_turn", lambda turn_id: order.append("turn_cancel")
    )
    monkeypatch.setattr(
        tools.subagent_manager,
        "cancel_foreground",
        lambda session, turn: order.append("subagent_cancel"),
    )

    main._cancel_active_turn()

    assert controller.stop_reasons == [("run-live", "cli_ctrl_c")]
    assert order == ["computer_stop", "turn_cancel", "subagent_cancel"]
    assert cancel_event.is_set()


def test_ctrl_c_without_active_computer_run_is_noop(monkeypatch):
    controller = FakeController(status="idle", run_id="")
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: controller
    )
    monkeypatch.setattr(main, "active_cancel_event", None)
    monkeypatch.setattr(main, "active_turn_id", None, raising=False)
    monkeypatch.setattr(main, "current_session", "session-idle")

    main._cancel_active_turn()

    assert controller.stop_reasons == []


# ---------------------------------------------------------------------------
# main.py 接线：斜杠分派、帮助表与 UI 事件通道
# ---------------------------------------------------------------------------


def test_computer_slash_command_dispatch(monkeypatch):
    fake = FakeController()
    calls = []
    printed = []

    def fake_handle(command, controller):
        calls.append((command, controller))
        return ComputerCommandResult(ok=True, text="电脑操控: 运行中 · Edge · 12 步")

    monkeypatch.setattr("core.computer.cli.handle_computer_command", fake_handle)
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: fake
    )
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )

    main.handle_slash_command("/computer status")

    assert calls == [("/computer status", fake)]
    assert any("电脑操控: 运行中 · Edge · 12 步" in line for line in printed)


def test_computer_slash_command_failure_prints_warning_color(monkeypatch):
    fake = FakeController(status="idle", run_id="")
    printed = []

    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: fake
    )
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )

    main.handle_slash_command("/computer stop")

    assert any("没有进行中的操控任务" in line for line in printed)
    assert any("ansiyellow" in line for line in printed)


def test_help_table_lists_computer_command(monkeypatch):
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )

    main.handle_slash_command("/help")

    assert any("/computer" in line and "设置页" in line for line in printed)


def test_main_renders_computer_cli_event_via_ui_fragment(monkeypatch):
    fragments = []
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_print_ui_fragment", fragments.append)
    monkeypatch.setattr(main, "app_instance", None)

    main._render_ui_event(ComputerCliEvent(payload={"text": "电脑操控: 输入 6 个字符"}))

    assert fragments == ["电脑操控: 输入 6 个字符\n"]
    # 电脑操控事件不创建 TurnRenderer（早于 renderer 查表返回）
    assert "computer-control" not in main.ui_renderers


# ---------------------------------------------------------------------------
# Task 14 审查裁决四项（Task 16 落地）：stopping 文案 / 状态栏段 / 补全表 / 兜底 except
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sub", ["pause", "resume", "stop"])
def test_cli_commands_during_stopping_window_return_friendly_text(sub):
    """stopping 窗口内的控制命令：友好提示，绝不暴露内部状态机文案（裁决②）。"""
    controller = FakeController(status="stopping")
    result = handle_computer_command(f"/computer {sub}", controller)
    assert result.ok is False
    assert "停止中" in result.text
    assert "非法状态转换" not in result.text
    assert controller.stop_calls == 0  # 已在停止中，不得重复 stop
    assert controller.pause_calls == []
    assert controller.resume_calls == []


def test_status_bar_segment_only_for_active_runs():
    """状态栏段（设计 17.2，裁决①）：活动 run 固定格式，idle/终态为空。"""
    from core.computer.cli import render_computer_status_bar

    running = ComputerRunSnapshot(
        run_id="run-live", status="running", target="browser",
        browser="edge", action_count=12,
    )
    assert render_computer_status_bar(running) == "电脑操控: 运行中 · Edge · 12 步"
    assert render_computer_status_bar(disabled_snapshot()) == ""
    assert render_computer_status_bar(
        ComputerRunSnapshot(run_id="run-done", status="stopped")
    ) == ""
    assert render_computer_status_bar(
        ComputerRunSnapshot(run_id="run-err", status="failed", error_code="computer_backend_error")
    ) == ""


def test_main_status_bar_segment_reads_controller_snapshot(monkeypatch):
    """main._computer_status_bar_segment 接共享 controller 单例（裁决①）。"""
    fake = FakeController(status="running", action_count=12)
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", lambda: fake
    )
    assert main._computer_status_bar_segment() == "电脑操控: 运行中 · Edge · 12 步"


def test_main_status_bar_segment_is_empty_on_failure(monkeypatch):
    """controller 读取失败：状态栏段为空串，绝不抛错抖动布局。"""
    def boom():
        raise RuntimeError("controller unavailable")

    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service", boom
    )
    assert main._computer_status_bar_segment() == ""


def test_command_completions_include_computer():
    """输入补全表含 /computer（裁决③）。"""
    assert any(cmd == "/computer" for cmd, _desc in main.COMMAND_COMPLETIONS)


def test_cancel_fallback_survives_controller_failure(monkeypatch, caplog):
    """Ctrl+C 兜底 except（裁决④）：清理异常被记录且不中断 Turn 级取消。"""
    import logging as _logging

    def boom():
        raise RuntimeError("stop exploded")

    monkeypatch.setattr(
        "core.computer.cli.stop_active_run_for_cancel", boom
    )
    with caplog.at_level(_logging.ERROR, logger="main.computer"):
        main._stop_active_computer_run_for_cancel()  # 不得抛出
    assert any("Ctrl+C" in record.message for record in caplog.records)


def test_main_block_finally_calls_computer_runtime_shutdown_on_both_paths(monkeypatch):
    """Task 16 审查 Minor 1：__main__ headless 与交互式两条路径的 finally 都调用统一收尾。

    通过 AST 提取 main.py 的 `if __name__ == "__main__":` 块并在模块命名空间受控执行，
    模拟脚本入口（run_headless/run_cli 均替换为桩，不真实运行 CLI 主循环）。
    """
    import ast
    import pathlib
    import sys

    source = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    block = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )
    code = compile(
        ast.Module(body=block.body, type_ignores=[]),
        filename=str(main.__file__),
        mode="exec",
    )

    calls = []
    monkeypatch.setattr(main, "run_headless", lambda prompt, session_name=None: 0)
    monkeypatch.setattr(main, "run_cli", lambda: None)
    monkeypatch.setattr(
        main, "_shutdown_computer_runtime_for_cli", lambda: calls.append("shutdown")
    )

    # 路径一：headless（-p）——sys.exit 前后 finally 已执行收尾
    monkeypatch.setattr(sys, "argv", ["ycode", "-p", "headless-task"])
    with pytest.raises(SystemExit) as exc_info:
        exec(code, vars(main))
    assert exc_info.value.code == 0
    assert calls == ["shutdown"]

    # 路径二：交互式（默认分支）——run_cli 返回后 finally 执行收尾
    calls.clear()
    monkeypatch.setattr(sys, "argv", ["ycode"])
    exec(code, vars(main))
    assert calls == ["shutdown"]
