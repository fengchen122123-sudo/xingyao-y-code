"""`/goal` 共享核心的判定、循环与持久化测试（不发起任何真实模型请求）。"""

import json
import os
import threading

from core.slash_commands.goal import (
    GOAL_ACHIEVED_MARKER,
    GoalState,
    GoalStore,
    RoundOutcome,
    build_round_prompt,
    judge,
    run_goal_loop,
)


class FakeExecutor:
    """按预设脚本返回轮次结果的假执行器，并记录收到的提示词与取消信号。"""

    def __init__(self, replies=None, outcomes=None):
        self.replies = list(replies or [])
        self.outcomes = list(outcomes or [])
        self.prompts = []
        self.cancel_events = []

    def run_round(self, prompt, *, cancel_event=None):
        self.prompts.append(prompt)
        self.cancel_events.append(cancel_event)
        index = len(self.prompts) - 1
        if index < len(self.outcomes):
            return self.outcomes[index]
        reply = self.replies[index] if index < len(self.replies) else ""
        return RoundOutcome(status="completed", reply_text=reply)


def _make_state(**overrides):
    kwargs = {"session": "s1", "goal": "把 README 的安装步骤补全"}
    kwargs.update(overrides)
    return GoalState(**kwargs)


def _collect_events():
    events = []

    def on_event(name, state):
        events.append((name, state.round, state.status))

    return events, on_event


def test_check_success_marks_achieved_in_one_round(tmp_path):
    """check 退出码 0 → achieved，且只跑一轮。"""
    checks = []

    def run_check(command):
        checks.append(command)
        return 0

    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=["装好了，验证通过"])
    state = _make_state(check="pytest -q", max_rounds=5)
    events, on_event = _collect_events()

    result = run_goal_loop(
        state, executor, store, on_event, threading.Event(), run_check=run_check
    )

    assert result.status == "achieved"
    assert result.round == 1
    assert len(executor.prompts) == 1
    assert checks == ["pytest -q"]
    assert len(result.history) == 1
    assert result.history[0]["round"] == 1
    assert events == [("round_done", 1, "running"), ("finished", 1, "achieved")]
    # 循环结束后状态已落盘，适配层收到 finished 时就能读到终态
    assert store.load("s1").status == "achieved"


def test_check_failure_continues_until_blocked(tmp_path):
    """check 返回 1 → 继续下一轮，跑到 max_rounds 后 blocked。"""
    checks = []

    def run_check(command):
        checks.append(command)
        return 1

    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=["还差一点", "继续", "仍未通过"])
    state = _make_state(check="npm test", max_rounds=3)

    result = run_goal_loop(
        state, executor, store, None, threading.Event(), run_check=run_check
    )

    assert result.status == "blocked"
    assert result.round == 3
    assert len(executor.prompts) == 3
    assert checks == ["npm test"] * 3
    assert [item["round"] for item in result.history] == [1, 2, 3]


def test_self_report_marker_awaits_confirm(tmp_path):
    """无 check 且回复含达成标记 → awaiting_confirm（自评不可信，等用户确认）。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=[f"全部完成。\n{GOAL_ACHIEVED_MARKER}"])
    state = _make_state(max_rounds=5)

    result = run_goal_loop(state, executor, store, None, threading.Event())

    assert result.status == "awaiting_confirm"
    assert result.round == 1
    assert len(executor.prompts) == 1


def test_self_report_without_marker_continues(tmp_path):
    """无 check 且回复不含标记 → 继续下一轮，直到轮次耗尽。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=["做了一半", "又做了一点"])
    state = _make_state(max_rounds=2)

    result = run_goal_loop(state, executor, store, None, threading.Event())

    assert result.status == "blocked"
    assert result.round == 2
    assert len(executor.prompts) == 2


def test_cancel_before_start_stops_without_executor(tmp_path):
    """cancel_event 已置位 → stopped，且完全不调用执行器。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=["不该被调用"])
    state = _make_state(max_rounds=3)
    cancel_event = threading.Event()
    cancel_event.set()

    result = run_goal_loop(state, executor, store, None, cancel_event)

    assert result.status == "stopped"
    assert result.round == 0
    assert executor.prompts == []
    assert store.load("s1").status == "stopped"


def test_round_outcome_cancelled_marks_stopped(tmp_path):
    """执行器返回 cancelled（用户中途中断）→ stopped，不写历史。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(outcomes=[RoundOutcome(status="cancelled")])
    state = _make_state(max_rounds=3)

    result = run_goal_loop(state, executor, store, None, threading.Event())

    assert result.status == "stopped"
    assert result.round == 1
    assert result.history == []


def test_failed_round_is_recorded_and_loop_continues(tmp_path):
    """failed 的一轮记入 history 后继续，下一轮达成。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(
        outcomes=[
            RoundOutcome(status="failed", error="网络中断"),
            RoundOutcome(status="completed", reply_text=GOAL_ACHIEVED_MARKER),
        ]
    )
    state = _make_state(max_rounds=5)

    result = run_goal_loop(state, executor, store, None, threading.Event())

    assert result.status == "awaiting_confirm"
    assert result.round == 2
    assert result.history[0]["summary"].startswith("失败")
    assert "网络中断" in result.history[0]["summary"]


def test_state_persists_and_reloads(tmp_path):
    """保存后 reload 能还原全部字段；会话名 slug 化且同一会话映射同一文件。"""
    root = tmp_path / "goals"
    store = GoalStore(root)
    session = "会话 A/测试:1?"
    state = GoalState(
        session=session,
        goal="修掉安装脚本在 Windows 上的报错",
        check="pytest tests/test_setup_engine.py -q",
        max_rounds=4,
        round=2,
        status="awaiting_confirm",
        history=[{"round": 1, "summary": "定位到编码问题"}],
    )

    store.save(state)
    reloaded = store.load(session)

    assert reloaded is not None
    assert reloaded.session == session
    assert reloaded.goal == state.goal
    assert reloaded.check == state.check
    assert reloaded.max_rounds == 4
    assert reloaded.round == 2
    assert reloaded.status == "awaiting_confirm"
    assert reloaded.history == [{"round": 1, "summary": "定位到编码问题"}]
    assert reloaded.created_at == state.created_at
    assert reloaded.updated_at == state.updated_at
    assert reloaded.owner_pid == os.getpid()

    path = store.path_for(session)
    assert path.exists()
    assert path.parent == root  # 非法字符没有把状态文件写到子目录里
    for char in '<>:"/\\|?*':
        assert char not in path.name
    # 同一会话稳定映射到同一文件（中文/空格/非法字符都被确定性地归一）
    assert store.path_for(session) == path
    assert store.load(session).session == session

    assert store.load("从未存在的会话") is None
    assert store.clear(session) is True
    assert store.clear(session) is False


def test_foreign_owner_pid_marks_interrupted(tmp_path):
    """owner_pid 不是当前进程的 running 状态，load 后转 interrupted 并落盘。"""
    store = GoalStore(tmp_path / "goals")
    session = "上次没跑完的会话"
    path = store.path_for(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session": session,
                "goal": "补测试",
                "check": "",
                "max_rounds": 5,
                "round": 2,
                "status": "running",
                "history": [{"round": 1, "summary": "写了一部分"}],
                "created_at": 111.0,
                "updated_at": 222.0,
                "owner_pid": os.getpid() + 1,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    state = store.load(session)

    assert state is not None
    assert state.status == "interrupted"
    assert state.round == 2  # 已有进展不能被清零，否则 resume 会从头重跑
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["status"] == "interrupted"
    assert on_disk["owner_pid"] == os.getpid()


def test_prompt_contains_goal_progress_and_marker_requirement(tmp_path):
    """每轮提示词必须包含目标原文、上一轮摘要、轮次/剩余轮次与达成标记要求。"""
    state = _make_state(max_rounds=5)
    state.history = [{"round": 1, "summary": "已经改好配置项"}]
    state.round = 2  # 模拟循环里已自增

    prompt = build_round_prompt(state)

    assert state.goal in prompt
    assert "已经改好配置项" in prompt
    assert "第 2 / 5 轮" in prompt
    assert "剩余 3 轮" in prompt
    assert GOAL_ACHIEVED_MARKER in prompt

    # 配了 check 时提示词要说明会自动执行验证命令，而不是要求输出标记
    state.check = "pytest -q"
    check_prompt = build_round_prompt(state)
    assert "pytest -q" in check_prompt
    assert GOAL_ACHIEVED_MARKER not in check_prompt


def test_judge_priority_and_error_tolerance(tmp_path):
    """judge 的三档优先级与容错。"""
    outcome = RoundOutcome(reply_text=GOAL_ACHIEVED_MARKER)

    with_check = _make_state(check="pytest -q")
    assert judge(with_check, outcome, lambda command: 0) == "achieved"
    assert judge(with_check, outcome, lambda command: 1) == "continue"
    # 有 check 但执行器缺失：不得把自评标记当成可信达成
    assert judge(with_check, outcome, None) == "continue"
    # 验证命令自身抛错 / 返回非整数：算未达成
    assert judge(with_check, outcome, lambda command: 1 / 0) == "continue"
    assert judge(with_check, outcome, lambda command: "oops") == "continue"

    without_check = _make_state()
    assert judge(without_check, outcome, None) == "pending_confirm"
    assert judge(without_check, RoundOutcome(reply_text="还在做"), None) == "continue"
    assert judge(without_check, RoundOutcome(reply_text=""), None) == "continue"


def test_executor_exception_fails_round_without_crashing(tmp_path):
    """执行器抛异常只折算为本轮失败，循环仍能收敛并落盘。"""

    class BoomExecutor:
        def __init__(self):
            self.calls = 0

        def run_round(self, prompt, *, cancel_event=None):
            self.calls += 1
            raise RuntimeError("回合启动失败")

    store = GoalStore(tmp_path / "goals")
    executor = BoomExecutor()
    state = _make_state(max_rounds=2)

    result = run_goal_loop(state, executor, store, None, threading.Event())

    assert executor.calls == 2
    assert result.status == "blocked"
    assert result.round == 2
    assert all(item["summary"].startswith("失败") for item in result.history)
    assert "RuntimeError" in result.history[0]["summary"]


def test_finished_callback_sees_persisted_state(tmp_path):
    """finished 事件回调时状态文件必须已经是终态（先 save 再回调）。"""
    store = GoalStore(tmp_path / "goals")
    executor = FakeExecutor(replies=[GOAL_ACHIEVED_MARKER])
    state = _make_state(max_rounds=3)
    seen = {}

    def on_event(name, current):
        if name == "finished":
            seen["status"] = store.load("s1").status
            seen["round"] = store.load("s1").round

    run_goal_loop(state, executor, store, on_event, threading.Event())

    assert seen == {"status": "awaiting_confirm", "round": 1}


def test_state_file_tracks_round_while_running(tmp_path):
    """每轮执行期间状态文件就要反映本轮轮次（GUI 状态条与 /goal status 都读文件）。

    回归背景：只在循环收尾保存时，界面整轮都停在"第 0/10 轮"，用户以为目标
    没推进；这里断言「轮次一开跑就落盘」与「round_done 时摘要已落盘」。
    """
    store = GoalStore(tmp_path / "goals")
    snapshots = []

    class PeekExecutor:
        """执行期间偷看状态文件，记录磁盘上的轮次/状态/历史条数。"""

        def __init__(self):
            self.calls = 0

        def run_round(self, prompt, *, cancel_event=None):
            self.calls += 1
            disk = store.load("s1")
            snapshots.append(
                (disk.round, disk.status, len(disk.history)) if disk else None
            )
            return RoundOutcome(
                status="completed", reply_text=f"第 {self.calls} 轮做完了"
            )

    events = []

    def on_event(name, state):
        if name == "round_done":
            disk = store.load("s1")
            events.append((disk.round, len(disk.history)))

    state = _make_state(max_rounds=3)
    run_goal_loop(state, PeekExecutor(), store, on_event, threading.Event())

    # 第 1、2、3 轮执行中，文件里分别已经是 1、2、3，且历史是已完成的前几轮
    assert snapshots == [
        (1, "running", 0),
        (2, "running", 1),
        (3, "running", 2),
    ]
    # round_done 广播时本轮摘要已经写进文件：状态条的"最近一轮进展"靠它
    assert events == [(1, 1), (2, 2), (3, 3)]
