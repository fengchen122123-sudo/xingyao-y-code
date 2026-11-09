"""`/goal` 的 CLI 接入测试：命令表注册、参数解析复用、状态渲染与执行器短路。

刻意不跑真实回合：命令层是纯解析、状态渲染是纯函数、执行器只测"已取消即返回"。
这样整组毫秒级完成，也不会因为某个用例真的发起模型回合而污染开发者的会话数据。

为什么可以直接 `import main`：tests/conftest.py 已在收集阶段把 YCODE_DATA_DIR
指到临时目录，main 里的 manager / 状态仓库都落在临时目录，不会读到真实的
~/.ycode（test_doctor.py / test_trace_export.py 同此做法）。
"""

import threading
import types

import pytest

import main
from core.slash_commands.goal.state import STATUS_BLOCKED, GoalState, GoalStore
from core.slash_commands.goal_command import handle_goal


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _patch_main_print(monkeypatch):
    """捕获 main.handle_slash_command 的 print_formatted_text 输出。"""
    printed = []
    monkeypatch.setattr(
        main, "print_formatted_text", lambda *args, **kwargs: printed.append(str(args[0]))
    )
    return printed


# ---------------------------------------------------------------------------
# 一、参数解析：CLI 与 GUI 复用同一套（handle_goal）
# ---------------------------------------------------------------------------

def test_empty_args_returns_help_action():
    """/goal 单打独斗是用户最可能的第一次尝试：应当回用法，而不是报错。"""
    result = handle_goal("")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_help"


def test_status_keyword_maps_to_goal_status():
    """控制关键词必须早于"目标"解析，否则 /goal status 会真的启动一个模型回合。"""
    result = handle_goal("status")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_status"


def test_goal_with_check_and_rounds():
    """--check 的值是一整条命令：按 token 截断会让验证命令永远"通过"。"""
    result = handle_goal("把测试跑绿 --check pytest -q --rounds 5")

    action = result["action"]
    assert action["action"] == "goal_start"
    assert action["goal"] == "把测试跑绿"
    assert action["check"] == "pytest -q"
    assert action["max_rounds"] == 5


def test_non_numeric_rounds_is_rejected():
    """非法轮次不能静默回落默认值：用户会以为上限已生效。"""
    result = handle_goal("--rounds abc 目标")

    assert result["ok"] is False
    assert "rounds" in result["msg"]


# ---------------------------------------------------------------------------
# 二、状态渲染（_format_goal_status 是 /goal status 唯一的分支逻辑）
# ---------------------------------------------------------------------------

def test_format_goal_status_renders_round_status_and_summary():
    """一行里必须同时给出轮次、状态与最近摘要——这三样是用户判断进展的全部依据。"""
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
    # 摘要取最近一轮，不能回显更早的"修了 A"
    assert "修了 B" in text
    assert "修了 A" not in text


def test_format_goal_status_without_state_is_friendly():
    """没有状态文件是正常情况（用户只是问问进度），不能渲染成一个空行。"""
    assert "没有进行中的目标" in main._format_goal_status(None)


# ---------------------------------------------------------------------------
# 三、CLI 注册与命令分派
# ---------------------------------------------------------------------------

def test_goal_registered_in_command_completions():
    assert any(cmd == "/goal" for cmd, _desc in main.COMMAND_COMPLETIONS)


def test_goal_listed_in_help(monkeypatch):
    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/help")

    assert any("/goal" in line for line in printed)
    assert any("目标模式" in line for line in printed)


def test_goal_without_args_prints_usage(monkeypatch):
    """/goal 空参走 goal_help 分支：输出用法，不触碰任何状态仓库。"""
    printed = _patch_main_print(monkeypatch)
    main.handle_slash_command("/goal")

    assert any("/goal" in line and "--check" in line for line in printed)


def test_goal_status_reads_store_and_prints_progress(monkeypatch, tmp_path):
    """status 分支要能读到落盘状态并渲染成"第 N/M 轮 · 状态 · 摘要"。"""
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
    """没有运行中的循环时要明确提示，不能静默成功让用户以为已经停了。"""
    monkeypatch.setattr(main, "_GOAL_RUNS", {})
    printed = _patch_main_print(monkeypatch)

    main.handle_slash_command("/goal stop")

    assert any("没有运行中的目标" in line for line in printed)


def test_goal_resume_rejects_achieved_goal(monkeypatch, tmp_path):
    """achieved 不该被"恢复"：再跑一轮只会把已完成的结论推翻。"""
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

    assert called == []  # 没有真的启动循环
    assert any("不能恢复" in line for line in printed)


# ---------------------------------------------------------------------------
# 四、CLI 执行器（只测不发起真实回合的短路路径）
# ---------------------------------------------------------------------------

def test_cli_executor_returns_cancelled_when_already_cancelled():
    """取消信号已置位时立刻返回 cancelled，绝不投递消息进队列。"""
    from core.slash_commands.goal.cli_executor import CliGoalExecutor

    cancel_event = threading.Event()
    cancel_event.set()
    executor = CliGoalExecutor(types.SimpleNamespace(), "s")

    outcome = executor.run_round("【目标】随便什么", cancel_event=cancel_event)

    assert outcome.status == "cancelled"


def test_cli_executor_falls_back_to_msg_queue(monkeypatch):
    """宿主没有提交函数时退化为直接入队，结构与 headless 投递一致。"""
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
    # 返回的是本轮共享结果字典：worker 收尾时回填 ok，执行器靠它判定轮次结束
    assert result is task["result"]


# ---------------------------------------------------------------------------
# 五、待确认状态的确认入口（CLI 侧回归：以前只有 status/stop/resume）
# ---------------------------------------------------------------------------

def test_goal_confirm_dispatches_from_slash_command(monkeypatch, tmp_path):
    """`/goal confirm` 从分派到落盘整条链路：把待确认收敛成 achieved。

    回归场景：自评档停在 awaiting_confirm 等用户点头，但 CLI 既没有 confirm
    子命令也没有对应分支，用户敲完 `/goal confirm` 只会得到"未知目标动作"。
    """
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
    """运行中不能确认：提前点头会把剩余轮次吞掉，状态必须原样不动。"""
    store = GoalStore(tmp_path)
    session = "sess_cli_confirm_running"
    store.save(GoalState(session=session, goal="继续跑", round=1, status="running"))
    monkeypatch.setattr(main, "_goal_store", lambda: store)

    printed = _patch_main_print(monkeypatch)
    main._goal_handle_confirm(session)

    assert store.load(session).status == "running"
    assert any("无需确认" in line for line in printed)
