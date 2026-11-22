import threading
import types

import pytest

import main
from core.slash_commands.goal.state import STATUS_BLOCKED, GoalState, GoalStore
from core.slash_commands.goal_command import handle_goal


def _patch_main_print(monkeypatch):
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


def test_empty_args_returns_help_action():
    result = handle_goal("")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_help"


def test_status_keyword_maps_to_goal_status():
    result = handle_goal("status")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_status"


def test_goal_with_check_and_rounds():
    result = handle_goal("把测试跑绿 --check pytest -q --rounds 5")

    action = result["action"]
    assert action["action"] == "goal_start"
    assert action["goal"] == "把测试跑绿"
    assert action["check"] == "pytest -q"
    assert action["max_rounds"] == 5


def test_non_numeric_rounds_is_rejected():
    result = handle_goal("--rounds abc 目标")

    assert result["ok"] is False
    assert "rounds" in result["msg"]


def test_format_goal_status_renders_round_status_and_summary():
    state = GoalState(
        session="s",
        goal="把测试跑绿",
        max_rounds=5,
        round=2,
        status=STATUS_BLOCKED,
        history=[{"round": 1, "summary": "修了 A"}, {"round": 2, "summary": "修了 B"}],
    )

    text = main._format_goal_status(state)

    assert "第 2/5 轮" in text
    assert "受阻" in text
    assert "修了 B" in text
    assert "修了 A" not in text


def test_format_goal_status_without_state_is_friendly():
    assert "没有进行中的目标" in main._format_goal_status(None)


def test_goal_registered_in_command_completions():
    assert any(cmd == "/goal" for cmd, _desc in main.COMMAND_COMPLETIONS)


def test_goal_listed_in_help(monkeypatch):
    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/help")

    assert any("/goal" in line for line in printed)
    assert any("目标模式" in line for line in printed)


def test_goal_without_args_prints_usage(monkeypatch):
    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/goal")

    assert any("/goal" in line and "--check" in line for line in printed)


def test_goal_status_reads_store_and_prints_progress(monkeypatch, tmp_path):
    store = GoalStore(tmp_path)
    state = GoalState(
        session=main.current_session,
        goal="把测试跑绿",
        max_rounds=3,
        round=1,
        status=STATUS_BLOCKED,
        history=[{"round": 1, "summary": "改了 A 模块"}],
    )
    store.save(state)
    monkeypatch.setattr(main, "_goal_store", lambda: store)

    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/goal status")

    assert any("第 1/3 轮" in line for line in printed)
    assert any("改了 A 模块" in line for line in printed)


def test_goal_stop_without_running_goal_reports_clearly(monkeypatch):
    monkeypatch.setattr(main, "_GOAL_RUNS", {})
    printed = _patch_main_print(monkeypatch)

    main.handle_slash_command("/goal stop")

    assert any("没有运行中的目标" in line for line in printed)


def test_goal_resume_rejects_achieved_goal(monkeypatch, tmp_path):
    store = GoalStore(tmp_path)
    store.save(
        GoalState(
            session=main.current_session,
            goal="已完成的目标",
            max_rounds=3,
            round=2,
            status="achieved",
        )
    )
    monkeypatch.setattr(main, "_goal_store", lambda: store)

    called = []
    monkeypatch.setattr(main, "_goal_start_run", lambda state: called.append(state) or (True, ""))
    printed = _patch_main_print(monkeypatch)

    main.handle_slash_command("/goal resume")

    assert called == []
    assert any("不能恢复" in line for line in printed)


def test_cli_executor_returns_cancelled_when_already_cancelled():
    from core.slash_commands.goal.cli_executor import CliGoalExecutor

    cancel_event = threading.Event()
    cancel_event.set()
    executor = CliGoalExecutor(types.SimpleNamespace(), "s")

    outcome = executor.run_round("【目标】随便什么", cancel_event=cancel_event)

    assert outcome.status == "cancelled"


def test_cli_executor_falls_back_to_msg_queue(monkeypatch):
    import queue as queue_module

    from core.slash_commands.goal.cli_executor import CliGoalExecutor

    fake_queue = queue_module.Queue()
    host = types.SimpleNamespace(msg_queue=fake_queue, is_thinking=False)
    executor = CliGoalExecutor(host, "session_x")

    result = executor._submit("【目标】任意提示词")

    task = fake_queue.get_nowait()
    assert task["text"] == "【目标】任意提示词"
    # 会话快照必须带上，否则排队期间切换会话会把目标回合写进错误会话
    assert task["session"] == "session_x"
    assert task["was_queued"] is False
    assert result is task["result"]


def test_goal_confirm_dispatches_from_slash_command(monkeypatch, tmp_path):
    store = GoalStore(tmp_path)
    session = main.current_session
    store.save(
        GoalState(
            session=session,
            goal="修完前 100 章的病句与漏洞",
            max_rounds=10,
            round=5,
            status="awaiting_confirm",
        )
    )
    monkeypatch.setattr(main, "_goal_store", lambda: store)

    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/goal confirm")

    assert store.load(session).status == "achieved"
    assert any("已确认目标达成" in line for line in printed)


def test_goal_confirm_rejects_non_awaiting_state(monkeypatch, tmp_path):
    store = GoalStore(tmp_path)
    session = "sess_cli_confirm_running"
    store.save(GoalState(session=session, goal="继续跑", round=1, status="running"))
    monkeypatch.setattr(main, "_goal_store", lambda: store)

    printed = _patch_main_print(monkeypatch)
    main._goal_handle_confirm(session)

    assert store.load(session).status == "running"
    assert any("无需确认" in line for line in printed)
