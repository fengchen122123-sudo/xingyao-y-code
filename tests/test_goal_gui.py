import types
import time

import core.llm
import gui_bridge
from core.slash_commands.goal import GOAL_ACHIEVED_MARKER
from core.slash_commands.goal.gui_executor import GuiGoalExecutor
from core.slash_commands.goal_command import handle_goal


def test_empty_args_returns_help_action():
    result = handle_goal("")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_help"


def test_keyword_stop_maps_to_goal_stop():
    result = handle_goal("stop")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_stop"


def test_goal_with_check_and_rounds():
    result = handle_goal("把测试跑绿 --check pytest -q --rounds 5")

    action = result["action"]
    assert action["action"] == "goal_start"
    assert action["goal"] == "把测试跑绿"
    # 值段落按"下一个开关"为右边界：`pytest -q` 必须整条保留，否则验证命令永远通过
    assert action["check"] == "pytest -q"
    assert action["max_rounds"] == 5


def test_non_numeric_rounds_is_rejected():
    result = handle_goal("--rounds abc 目标")

    assert result["ok"] is False
    assert "rounds" in result["msg"]


def test_turn_sources_include_goal_and_match_llm():
    assert "goal" in gui_bridge.TURN_SOURCES
    assert tuple(gui_bridge.TURN_SOURCES) == tuple(core.llm.COMPUTER_TURN_SOURCES)


def test_executor_reports_failed_when_send_rejected():
    bridge = types.SimpleNamespace(
        send_user_message=lambda *args, **kwargs: {"ok": False, "msg": "x"}
    )
    executor = GuiGoalExecutor(bridge, "session_a")

    outcome = executor.run_round("【目标】随便什么", cancel_event=None)

    assert outcome.status == "failed"
    assert outcome.error == "x"


class _NoticeCollector:

    def __init__(self):
        self.notices = []

    def post_notice(self, message: str, session_name: str = "", kind: str = "notice"):
        self.notices.append(
            {"message": message, "session_name": session_name, "kind": kind}
        )
        return {"ok": True}


class _GoalBridgeStub(_NoticeCollector):

    _GOAL_STATUS_TEXT = gui_bridge.ApiBridge._GOAL_STATUS_TEXT
    _GOAL_RESUMABLE = gui_bridge.ApiBridge._GOAL_RESUMABLE
    _goal_runs_map = gui_bridge.ApiBridge._goal_runs_map
    _goal_store = gui_bridge.ApiBridge._goal_store
    _goal_status = gui_bridge.ApiBridge._goal_status
    _goal_stop = gui_bridge.ApiBridge._goal_stop
    _goal_resume = gui_bridge.ApiBridge._goal_resume
    _goal_confirm = gui_bridge.ApiBridge._goal_confirm
    _goal_start = gui_bridge.ApiBridge._goal_start
    _start_goal_run = gui_bridge.ApiBridge._start_goal_run
    _goal_target_model_id = gui_bridge.ApiBridge._goal_target_model_id
    _known_model_ids = gui_bridge.ApiBridge._known_model_ids
    _inherit_goal_session_model = gui_bridge.ApiBridge._inherit_goal_session_model

    def __init__(self):
        super().__init__()
        self.stopped = []

    def stop_current_turn(self, session_name: str = ""):
        self.stopped.append(session_name)
        return {"ok": True}

    def _materialize_goal_session(self, session_name, workdir, goal, turn_source="local"):
        return session_name


def _make_draft_stub():
    stub = _GoalBridgeStub()
    stub.materialize_calls = []
    stub.sent = []

    def _materialize(session_name, workdir, goal, turn_source="local"):
        stub.materialize_calls.append(
            {"session": session_name, "workdir": workdir, "goal": goal}
        )
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
    assert result["session"] == "session_a1b2c3"

    _wait_goal_runs_empty(stub, "session_a1b2c3")
    # 目标循环每一轮都必须跑在同一个会话名上，否则状态条/停止/续跑查不到目标
    assert stub.sent and stub.sent[0]["session_name"] == "session_a1b2c3"
    assert stub._goal_store().load("session_a1b2c3") is not None


def test_goal_start_without_workdir_skips_materialize():
    stub = _make_draft_stub()

    result = stub._goal_start(
        {"goal": "把测试跑绿", "check": "", "max_rounds": 1}, "sess_existing"
    )

    assert result["ok"] is True
    assert stub.materialize_calls[0]["workdir"] == ""
    assert result["session"] == "sess_existing"
    _wait_goal_runs_empty(stub, "sess_existing")
    assert stub.sent and stub.sent[0]["session_name"] == "sess_existing"


def test_materialize_goal_session_persists_draft_with_workdir():
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
    assert (
        stub._materialize_goal_session("session_a1b2c3", "D:\\x", "目标")
        == "session_a1b2c3"
    )


def test_dispatch_slash_passes_workdir_and_model_into_goal_start():
    stub = _make_draft_stub()
    stub._dispatch_slash = gui_bridge.ApiBridge._dispatch_slash.__get__(stub)
    stub._run_slash_action = gui_bridge.ApiBridge._run_slash_action.__get__(stub)
    calls = []
    stub._goal_start = (
        lambda action, session_name, workdir="", turn_source="local", model_id="":
        calls.append((action, session_name, workdir, model_id))
        or {"ok": True, "slash": True}
    )

    result = stub._dispatch_slash(
        "/goal 把测试跑绿",
        "session_ff00aa",
        "/goal 把测试跑绿",
        workdir="D:\\x",
        model_id="m-deepseek",
    )

    assert result is not None and result.get("slash") is True
    assert calls, "goal_start 应被调用"
    assert calls[0][1] == "session_ff00aa"
    assert calls[0][2] == "D:\\x"
    assert calls[0][3] == "m-deepseek"


def test_goal_start_inherits_triggering_model_on_draft_session():
    stub = _make_draft_stub()
    stub._known_model_ids = lambda: {"m-deepseek"}
    bound = []
    stub._memory = types.SimpleNamespace(
        get_last_message_id=lambda name: 0,
        get_history_with_ids=lambda name: [
            {
                "role": "assistant",
                "content": f"做完了 {GOAL_ACHIEVED_MARKER}",
                "_msg_id": 1,
            }
        ],
        get_session_model_id=lambda name: None,
        set_session_model_id=lambda name, mid: bound.append((name, mid)) or True,
    )

    result = stub._goal_start(
        {"goal": "写完第7章", "check": "", "max_rounds": 1},
        "session_a1b2c3",
        workdir="D:\\fengchenhao\\小说",
        model_id="m-deepseek",
    )

    assert result["ok"] is True
    assert bound == [("session_a1b2c3", "m-deepseek")]
    _wait_goal_runs_empty(stub, "session_a1b2c3")


def test_goal_start_keeps_existing_session_model():
    stub = _make_draft_stub()
    stub._known_model_ids = lambda: {"m-deepseek", "m-other"}
    bound = []
    stub._memory = types.SimpleNamespace(
        get_last_message_id=lambda name: 0,
        get_history_with_ids=lambda name: [
            {
                "role": "assistant",
                "content": f"做完了 {GOAL_ACHIEVED_MARKER}",
                "_msg_id": 1,
            }
        ],
        get_session_model_id=lambda name: "m-other",
        set_session_model_id=lambda name, mid: bound.append((name, mid)) or True,
    )

    result = stub._goal_start(
        {"goal": "把测试跑绿", "check": "", "max_rounds": 1},
        "sess_existing",
        model_id="m-deepseek",
    )

    assert result["ok"] is True
    assert bound == []
    _wait_goal_runs_empty(stub, "sess_existing")


def test_run_slash_action_unwraps_goal_action_dict():
    collector = _NoticeCollector()
    stub = types.SimpleNamespace(post_notice=collector.post_notice)

    result = gui_bridge.ApiBridge._run_slash_action(
        stub, {"ok": True, "action": {"action": "goal_help"}}, "session_a"
    )

    assert result == {"ok": True, "slash": True}
    assert collector.notices, "goal_help 必须给用户一句用法提示"
    assert collector.notices[0]["session_name"] == "session_a"


def test_goal_status_without_state_says_no_active_goal():
    stub = _GoalBridgeStub()

    result = stub._goal_status("session_none")

    assert result == {"ok": True, "slash": True}
    assert "没有进行中的目标" in stub.notices[0]["message"]
    assert stub.notices[0]["kind"] == "notice"


def test_goal_stop_without_running_goal_is_a_notice():
    stub = _GoalBridgeStub()

    result = stub._goal_stop("session_none")

    assert result == {"ok": True, "slash": True}
    assert "没有运行中的目标" in stub.notices[0]["message"]
    # 没在跑时不该顺手调用回合取消，否则可能误停用户手动发的回合
    assert stub.stopped == []


def test_goal_stop_sets_cancel_event_and_stops_turn():
    import threading

    stub = _GoalBridgeStub()
    event = threading.Event()
    stub._goal_runs = {"session_run": event}

    result = stub._goal_stop("session_run")

    assert result == {"ok": True, "slash": True}
    assert event.is_set()
    assert stub.stopped == ["session_run"]


def test_goal_start_drives_loop_through_stub_bridge():
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

    assert result == {"ok": True, "slash": True, "session": "sess_loop"}
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
    stub = _GoalBridgeStub()
    stub._migrate_goal_state = gui_bridge.ApiBridge._migrate_goal_state.__get__(stub)
    store = stub._goal_store()
    store.clear("session_no_goal")
    store.clear("新名字")

    stub._migrate_goal_state("session_no_goal", "新名字")

    assert store.load("新名字") is None


def test_auto_rename_skips_session_with_running_goal():
    import threading

    stub = _GoalBridgeStub()
    stub._maybe_auto_rename = gui_bridge.ApiBridge._maybe_auto_rename.__get__(stub)

    def _boom(*args, **kwargs):
        raise AssertionError("有目标在跑时不该走到取名/改名")

    stub._derive_title_from_first_user_msg = _boom
    stub._goal_runs = {"session_aaa111": threading.Event()}

    assert stub._maybe_auto_rename("session_aaa111") == "session_aaa111"


def test_manual_rename_rejected_while_goal_running():
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


def test_confirm_keyword_maps_to_goal_confirm():
    result = handle_goal("confirm")

    assert result["ok"] is True
    assert result["action"]["action"] == "goal_confirm"


def test_goal_confirm_marks_awaiting_state_achieved():
    from core.slash_commands.goal.state import (
        STATUS_ACHIEVED,
        STATUS_AWAITING_CONFIRM,
        GoalState,
    )

    stub = _GoalBridgeStub()
    store = stub._goal_store()
    session = "sess_confirm_ok"
    store.clear(session)
    store.save(
        GoalState(
            session=session,
            goal="修完前 100 章的病句与漏洞",
            round=5,
            max_rounds=10,
            status=STATUS_AWAITING_CONFIRM,
        )
    )

    result = stub._goal_confirm(session)

    assert result == {"ok": True, "slash": True}
    assert store.load(session).status == STATUS_ACHIEVED
    assert any("已确认目标达成" in item["message"] for item in stub.notices)
    store.clear(session)


def test_goal_confirm_rejects_running_state():
    from core.slash_commands.goal.state import STATUS_RUNNING, GoalState

    stub = _GoalBridgeStub()
    store = stub._goal_store()
    session = "sess_confirm_running"
    store.clear(session)
    store.save(GoalState(session=session, goal="继续推进", round=1, status=STATUS_RUNNING))

    result = stub._goal_confirm(session)

    assert result == {"ok": True, "slash": True}
    assert store.load(session).status == STATUS_RUNNING
    assert any("无需确认" in item["message"] for item in stub.notices)
    store.clear(session)
