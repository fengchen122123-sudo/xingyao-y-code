"""`/goal` 的 GUI 接入测试：命令动作、桥分支与执行器的失败路径。

刻意不启动任何真实回合：命令层是纯解析，桥的 `_run_slash_action` 只依赖
post_notice，执行器用 `types.SimpleNamespace` 替身顶住桥接口。这样整组用例
毫秒级完成，也不会因为某个用例真的跑起模型回合而污染开发者的会话数据。
"""

import types
import time

import core.llm
import gui_bridge
from core.slash_commands.goal import GOAL_ACHIEVED_MARKER
from core.slash_commands.goal.gui_executor import GuiGoalExecutor
from core.slash_commands.goal_command import handle_goal


def test_empty_args_returns_help_action():
    """不带目标 = 教用户怎么用，而不是报错（首次尝试 `/goal` 最常见）。"""
    result = handle_goal("")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_help"


def test_keyword_stop_maps_to_goal_stop():
    """控制关键词按整段完全相等匹配，不能当成一个叫 "stop" 的目标启动回合。"""
    result = handle_goal("stop")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_stop"


def test_goal_with_check_and_rounds():
    """目标正文、验证命令与轮次上限都要原样透传给适配层。"""
    result = handle_goal("把测试跑绿 --check pytest -q --rounds 5")

    action = result["action"]
    assert action["action"] == "goal_start"
    assert action["goal"] == "把测试跑绿"
    # 值段落按"下一个开关"为右边界：`pytest -q` 必须整条保留，否则验证命令永远通过
    assert action["check"] == "pytest -q"
    assert action["max_rounds"] == 5


def test_non_numeric_rounds_is_rejected():
    """非法轮次不能静默回落到默认值：那会让用户以为上限已生效。"""
    result = handle_goal("--rounds abc 目标")

    assert result["ok"] is False
    assert "rounds" in result["msg"]


def test_turn_sources_include_goal_and_match_llm():
    """桥与低层白名单必须一致，否则 goal 回合会在模型请求处直接抛错。"""
    assert "goal" in gui_bridge.TURN_SOURCES
    assert tuple(gui_bridge.TURN_SOURCES) == tuple(core.llm.COMPUTER_TURN_SOURCES)


def test_executor_reports_failed_when_send_rejected():
    """send_user_message 返回 ok=False → 本轮失败并带上桥给的错误文案。

    只做替身：真实 ApiBridge.__init__ 会建库、注册 GUI 全局与线程池，与本层无关。
    """
    bridge = types.SimpleNamespace(
        send_user_message=lambda *args, **kwargs: {"ok": False, "msg": "x"}
    )
    executor = GuiGoalExecutor(bridge, "session_a")

    outcome = executor.run_round("【目标】随便什么", cancel_event=None)

    assert outcome.status == "failed"
    assert outcome.error == "x"


class _NoticeCollector:
    """最小桥替身：只实现 _run_slash_action 会用到的 post_notice。"""

    def __init__(self):
        self.notices = []

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice"):
        self.notices.append(
            {"message": message, "session_name": session_name, "kind": kind}
        )
        return {"ok": True}


class _GoalBridgeStub(_NoticeCollector):
    """目标分支的最小桥替身：把 ApiBridge 的目标方法原样挂过来，不跑 __init__。

    为什么不直接 `ApiBridge._goal_stop(stub, ...)`：这些方法互相调用
    `self._goal_runs_map()` / `self._goal_store()`，SimpleNamespace 拿不到未绑定
    方法；在类体里按名字取过来，等于只替换掉 __init__ 建库/建线程池那部分。
    """

    _GOAL_STATUS_TEXT = gui_bridge.ApiBridge._GOAL_STATUS_TEXT
    _GOAL_RESUMABLE = gui_bridge.ApiBridge._GOAL_RESUMABLE
    _goal_runs_map = gui_bridge.ApiBridge._goal_runs_map
    _goal_store = gui_bridge.ApiBridge._goal_store
    _goal_status = gui_bridge.ApiBridge._goal_status
    _goal_stop = gui_bridge.ApiBridge._goal_stop
    _goal_resume = gui_bridge.ApiBridge._goal_resume
    _goal_start = gui_bridge.ApiBridge._goal_start
    _start_goal_run = gui_bridge.ApiBridge._start_goal_run

    def __init__(self):
        super().__init__()
        self.stopped = []

    def stop_current_turn(self, session_name: str = ""):
        self.stopped.append(session_name)
        return {"ok": True}

    def _materialize_goal_session(self, session_name, workdir, goal, turn_source="local"):
        """草稿落库/改名需要真实 DB，替身桥按“名字不变”处理。

        只有专门验证落库的用例会覆盖它（那几条用例自己注入 _memory 桩）。
        """
        return session_name


def _make_draft_stub():
    """目标启动链路的桥替身：记录草稿落库调用与每轮 send 的关键字参数。

    真实落库/改名需要完整 DB，而这几条用例验证的是「workdir 有没有透传进来、
    状态文件按哪个会话名存」，与斜杠派发同层的 post_notice/运行表沿用类体绑定。
    """
    stub = _GoalBridgeStub()
    stub.materialize_calls = []
    stub.sent = []

    def _materialize(session_name, workdir, goal, turn_source="local"):
        stub.materialize_calls.append(
            {"session": session_name, "workdir": workdir, "goal": goal}
        )
        # 落库不改名：命名交给首轮结束后的自动改名（那里会随迁目标状态）
        return session_name

    stub._materialize_goal_session = _materialize
    stub._is_turn_active = lambda name: False
    stub._session_workdir = lambda name: "GLOBAL"
    stub._memory = types.SimpleNamespace(
        get_last_message_id=lambda name: 0,
        get_history_with_ids=lambda name: [
            {
                "role": "assistant",
                "content": f"做完了 {GOAL_ACHIEVED_MARKER}",
                "_msg_id": 1,
            }
        ],
    )
    stub.send_user_message = (
        lambda text, **kwargs: stub.sent.append(kwargs) or {"ok": True}
    )
    return stub


def _wait_goal_runs_empty(stub, session_name, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline and session_name in stub._goal_runs_map():
        time.sleep(0.05)


def test_goal_start_on_draft_session_materializes_with_workdir():
    """草稿会话（未落库）里直接 /goal：必须按前端携带的目录落库，而不是丢掉 workdir。

    回归场景：用户在小说项目分组点「新会话」后输入 /goal …，斜杠命令在首条消息
    落库之前就地执行且提前返回，workdir 没人接——目标循环首轮把会话 ensure 成
    进程启动目录（星瑶自己），于是会话归组、每轮 cwd、小说项目检测全部错位。
    """
    stub = _make_draft_stub()

    result = stub._goal_start(
        {"goal": "写完第7章正文", "check": "", "max_rounds": 1},
        "session_a1b2c3",
        workdir="D:\\fengchenhao\\小说",
    )

    assert result["ok"] is True
    assert stub.materialize_calls, "草稿 /goal 必须先把会话按 workdir 落库"
    call = stub.materialize_calls[0]
    assert call["workdir"] == "D:\\fengchenhao\\小说"
    assert call["session"] == "session_a1b2c3"
    # 落库不改名：命名交给首轮结束后的自动改名（那里会随迁目标状态）
    assert result["session"] == "session_a1b2c3"

    _wait_goal_runs_empty(stub, "session_a1b2c3")
    # 目标循环每一轮都必须跑在同一个会话名上，否则状态条/停止/续跑查不到目标
    assert stub.sent and stub.sent[0]["session_name"] == "session_a1b2c3"
    assert stub._goal_store().load("session_a1b2c3") is not None


def test_goal_start_without_workdir_skips_materialize():
    """已落库会话里 /goal（不带 workdir）：不落库、不改名，行为与修复前一致。"""
    stub = _make_draft_stub()

    result = stub._goal_start(
        {"goal": "把测试跑绿", "check": "", "max_rounds": 1}, "sess_existing"
    )

    assert result["ok"] is True
    # 没带目录 → 原样返回（不落库、不改名），与修复前行为一致
    assert stub.materialize_calls[0]["workdir"] == ""
    assert result["session"] == "sess_existing"
    _wait_goal_runs_empty(stub, "sess_existing")
    assert stub.sent and stub.sent[0]["session_name"] == "sess_existing"


def test_materialize_goal_session_persists_draft_with_workdir():
    """真实 _materialize_goal_session：按 workdir 落库并切当前会话，不改名。

    改名留给首轮结束后的自动命名：此刻前端还在等 send_user_message 返回，
    在这里改名会让会话名与状态刷新抢跑（GoalBar 用旧名查空后不再轮询）。
    """
    stub = _GoalBridgeStub()
    stub._materialize_goal_session = (
        gui_bridge.ApiBridge._materialize_goal_session.__get__(stub)
    )
    ensured = []
    stub._memory = types.SimpleNamespace(
        get_all_sessions=lambda workdir=None, include_empty=False: [
            {"name": "session_old", "workdir": "GLOBAL"}
        ],
        ensure_session=lambda n, workdir=None, source="GUI": ensured.append(
            (n, workdir)
        )
        or True,
    )

    def _no_rename(*args, **kwargs):
        raise AssertionError("草稿 /goal 不该在这里改名")

    stub._rename_session_to_title = _no_rename
    stub._current_session = "session_old"
    stub._set_current_session = lambda name: setattr(stub, "_current_session", name)

    final = stub._materialize_goal_session(
        "session_a1b2c3", "D:\\novel", "写完第7章正文，继续推进后面的章节"
    )

    assert ensured == [("session_a1b2c3", "D:\\novel")]
    assert final == "session_a1b2c3"
    assert stub._current_session == "session_a1b2c3"


def test_materialize_goal_session_keeps_current_for_lan():
    """LAN 分发同样带 workdir，但不能抢本地当前会话（与普通消息落库同口径）。"""
    stub = _GoalBridgeStub()
    stub._materialize_goal_session = (
        gui_bridge.ApiBridge._materialize_goal_session.__get__(stub)
    )
    stub._memory = types.SimpleNamespace(
        get_all_sessions=lambda workdir=None, include_empty=False: [],
        ensure_session=lambda n, workdir=None, source="GUI": True,
    )
    stub._rename_session_to_title = lambda *a, **k: "不该被调用"
    stub._current_session = "session_local"
    stub._set_current_session = lambda name: setattr(stub, "_current_session", name)

    final = stub._materialize_goal_session(
        "session_lan01", "D:\\novel", "目标", turn_source="lan"
    )

    assert final == "session_lan01"
    assert stub._current_session == "session_local"


def test_materialize_goal_session_guards():
    """不该动的场景一律原样返回：非草稿名、无 workdir、库里已存在同名会话。"""
    stub = _GoalBridgeStub()
    stub._materialize_goal_session = (
        gui_bridge.ApiBridge._materialize_goal_session.__get__(stub)
    )

    def _boom(*args, **kwargs):
        raise AssertionError("守卫场景不该触发落库/改名")

    stub._memory = types.SimpleNamespace(
        get_all_sessions=lambda workdir=None, include_empty=False: [
            {"name": "session_a1b2c3", "workdir": "D:\\x"}
        ],
        ensure_session=_boom,
    )
    stub._rename_session_to_title = _boom
    stub._set_current_session = _boom

    assert stub._materialize_goal_session("real_name", "D:\\x", "目标") == "real_name"
    assert stub._materialize_goal_session("session_a1b2c3", "", "目标") == "session_a1b2c3"
    # 已存在的草稿名会话（极端：库里已有行）也不重复落库
    assert (
        stub._materialize_goal_session("session_a1b2c3", "D:\\x", "目标")
        == "session_a1b2c3"
    )


def test_dispatch_slash_passes_workdir_into_goal_start():
    """workdir 必须一路透传到 goal 分支：斜杠层丢参数就是本次 bug 的根因。"""
    stub = _make_draft_stub()
    stub._dispatch_slash = gui_bridge.ApiBridge._dispatch_slash.__get__(stub)
    stub._run_slash_action = gui_bridge.ApiBridge._run_slash_action.__get__(stub)
    calls = []
    stub._goal_start = (
        lambda action, session_name, workdir="", turn_source="local": calls.append(
            (action, session_name, workdir)
        )
        or {"ok": True, "slash": True}
    )

    result = stub._dispatch_slash(
        "/goal 把测试跑绿", "session_ff00aa", "/goal 把测试跑绿", workdir="D:\\x"
    )

    assert result is not None and result.get("slash") is True
    assert calls, "goal_start 应被调用"
    assert calls[0][1] == "session_ff00aa"
    assert calls[0][2] == "D:\\x"


def test_run_slash_action_unwraps_goal_action_dict():
    """/goal 的动作是"字典套字典"：桥必须解包内层，否则会落到未知动作分支。

    这条用例守的是 gui_bridge._run_slash_action 顶部的解包逻辑——命令层改了
    包装形状时，这里会第一时间红。
    """
    collector = _NoticeCollector()
    stub = types.SimpleNamespace(post_notice=collector.post_notice)

    result = gui_bridge.ApiBridge._run_slash_action(
        stub, {"ok": True, "action": {"action": "goal_help"}}, "session_a"
    )

    assert result == {"ok": True, "slash": True}
    assert collector.notices, "goal_help 必须给用户一句用法提示"
    assert collector.notices[0]["session_name"] == "session_a"


def test_goal_status_without_state_says_no_active_goal():
    """没有状态文件是正常情况（用户只是问问），应当是一句提示而不是错误卡片。

    conftest 把 YCODE_DATA_DIR 指到临时目录，所以这里读到的是"干净仓库"。
    """
    stub = _GoalBridgeStub()

    result = stub._goal_status("session_none")

    assert result == {"ok": True, "slash": True}
    assert "没有进行中的目标" in stub.notices[0]["message"]
    assert stub.notices[0]["kind"] == "notice"


def test_goal_stop_without_running_goal_is_a_notice():
    """没有运行中的目标时 stop 不该报错，只说明当前没有可停止的循环。"""
    stub = _GoalBridgeStub()

    result = stub._goal_stop("session_none")

    assert result == {"ok": True, "slash": True}
    assert "没有运行中的目标" in stub.notices[0]["message"]
    # 没在跑时不该顺手调用回合取消，否则可能误停用户手动发的回合
    assert stub.stopped == []


def test_goal_stop_sets_cancel_event_and_stops_turn():
    """运行表里有事件时必须置位，并把取消转给真实回合（否则要等整轮跑完）。"""
    import threading

    stub = _GoalBridgeStub()
    event = threading.Event()
    stub._goal_runs = {"session_run": event}

    result = stub._goal_stop("session_run")

    assert result == {"ok": True, "slash": True}
    assert event.is_set()
    assert stub.stopped == ["session_run"]


def test_goal_start_drives_loop_through_stub_bridge():
    """`_goal_start` → 后台线程 → run_goal_loop 全链路（替身桥，无真实回合）。

    替身桥的回合"瞬间结束"（_is_turn_active 恒 False），记忆里固定回一条带达成
    标记的助手消息，于是第 1 轮就该判成待确认。这条用例守住三件容易写错的事：
    turn_source 必须是 goal、进度提示必须发到用户会话、线程结束后运行表要自清。
    """
    stub = _GoalBridgeStub()
    stub._memory = types.SimpleNamespace(
        get_last_message_id=lambda name: 0,
        get_history_with_ids=lambda name: [
            {
                "role": "assistant",
                "content": f"做完了 {GOAL_ACHIEVED_MARKER}",
                "_msg_id": 1,
            }
        ],
    )
    stub._session_workdir = lambda name: "GLOBAL"
    sent = []
    stub.send_user_message = lambda text, **kwargs: sent.append(kwargs) or {"ok": True}
    stub._is_turn_active = lambda name: False

    result = stub._goal_start(
        {"goal": "把测试跑绿", "check": "", "max_rounds": 1}, "sess_loop"
    )

    # 返回值多带 session：草稿落库改名后前端要靠它对齐当前会话
    assert result == {"ok": True, "slash": True, "session": "sess_loop"}
    # 等后台线程收尾（daemon 线程没有句柄，只能按运行表收敛等待）
    deadline = time.time() + 10
    while time.time() < deadline and "sess_loop" in stub._goal_runs_map():
        time.sleep(0.05)

    state = stub._goal_store().load("sess_loop")
    assert state is not None
    assert state.round == 1
    # 自评不可信：带达成标记只到"待确认"，绝不能自动收尾成 achieved
    assert state.status == "awaiting_confirm"
    assert sent and sent[0]["turn_source"] == "goal"
    messages = [item["message"] for item in stub.notices]
    assert any("已启动目标" in text for text in messages)
    assert any("目标第 1/1 轮" in text for text in messages)
    assert any("目标流程结束" in text for text in messages)
    # 线程退出后运行表必须自清，否则用户再也启动不了新目标
    assert "sess_loop" not in stub._goal_runs_map()


def test_migrate_goal_state_follows_session_rename():
    """会话改名后目标状态必须跟着改键：否则状态条/停止/继续全都查不到目标。"""
    import threading

    from core.slash_commands.goal.state import GoalState

    stub = _GoalBridgeStub()
    stub._migrate_goal_state = gui_bridge.ApiBridge._migrate_goal_state.__get__(stub)
    store = stub._goal_store()
    old_name, new_name = "session_rename_old", "改名后的会话"
    store.clear(old_name)
    store.clear(new_name)
    store.save(GoalState(session=old_name, goal="继续写正文"))
    event = threading.Event()
    stub._goal_runs = {old_name: event}

    stub._migrate_goal_state(old_name, new_name)

    migrated = store.load(new_name)
    assert migrated is not None
    assert migrated.goal == "继续写正文"
    assert migrated.session == new_name
    assert store.load(old_name) is None
    # 运行表键也要换：否则 /goal stop 按新名停不到正在跑的循环
    assert stub._goal_runs_map().get(new_name) is event


def test_migrate_goal_state_is_noop_without_goal():
    """没有目标的会话改名是空操作：不能因为改名就往 goals/ 写空文件。"""
    stub = _GoalBridgeStub()
    stub._migrate_goal_state = gui_bridge.ApiBridge._migrate_goal_state.__get__(stub)
    store = stub._goal_store()
    store.clear("session_no_goal")
    store.clear("新名字")

    stub._migrate_goal_state("session_no_goal", "新名字")

    assert store.load("新名字") is None


def test_auto_rename_skips_session_with_running_goal():
    """目标循环在跑时不许自动改名：循环闭包记的是旧名，改名后后续轮次会
    写进旧名的幽灵会话（用户看到"目标还在跑，消息却进了另一个会话"）。"""
    import threading

    stub = _GoalBridgeStub()
    stub._maybe_auto_rename = gui_bridge.ApiBridge._maybe_auto_rename.__get__(stub)

    def _boom(*args, **kwargs):
        raise AssertionError("有目标在跑时不该走到取名/改名")

    stub._derive_title_from_first_user_msg = _boom
    stub._goal_runs = {"session_aaa111": threading.Event()}

    assert stub._maybe_auto_rename("session_aaa111") == "session_aaa111"


def test_manual_rename_rejected_while_goal_running():
    """手动改名同样要拦：理由与自动改名一致，且失败文案要说清怎么解开。"""
    import threading

    stub = _GoalBridgeStub()
    stub._is_turn_active = lambda name: False

    def _boom(*args, **kwargs):
        raise AssertionError("有目标在跑时不该动数据库")

    stub._memory = types.SimpleNamespace(rename_session=_boom)
    stub._goal_runs = {"session_bbb222": threading.Event()}

    result = gui_bridge.ApiBridge.rename_session(stub, "session_bbb222", "新名字")

    assert result["ok"] is False
    assert "/goal stop" in result["msg"]
    assert result["name"] == "session_bbb222"

