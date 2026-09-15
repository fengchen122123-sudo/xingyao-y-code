import threading
import time
from gui_bridge import ApiBridge


def _make_test_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._cancel_events = {}
    bridge._active_turn_snapshots = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._delta_lock = threading.Lock()
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_last_flush = 0.0
    bridge._delta_flusher = None
    bridge._delta_flusher_lock = threading.Lock()
    bridge._shutting_down = False
    bridge._window = None
    bridge._current_session = "s1"
    return bridge


def test_active_turn_snapshot_crud_and_delta_updates():
    bridge = _make_test_bridge()

    # 1. 初始为空
    assert bridge.get_active_turn_snapshot("s1") is None
    assert bridge.get_all_active_turn_snapshots() == {}

    # 2. 模拟回合开始存入快照
    with bridge._active_turn_lock:
        bridge._active_turn_snapshots["s1"] = {
            "session_name": "s1",
            "user_text": "hello",
            "display_text": "hello",
            "started_at_ms": 1700000000000,
            "thinking_text": "",
            "streaming_text": "",
            "tool_calls": [],
            "internal_kind": "",
        }

    # 3. 验证通过 helper 查询
    snap = bridge.get_active_turn_snapshot("s1")
    assert snap is not None
    assert snap["user_text"] == "hello"
    assert snap["streaming_text"] == ""

    # 4. 模拟 _emit_delta 累积 thinking_delta 与 token_delta
    bridge._emit_delta("thinking_delta", "思考第1步...", session_name="s1")
    bridge._emit_delta("token_delta", "你好，", session_name="s1")
    bridge._emit_delta("token_delta", "这是助手回答。", session_name="s1")

    snap = bridge.get_active_turn_snapshot("s1")
    assert snap["thinking_text"] == "思考第1步..."
    assert snap["streaming_text"] == "你好，这是助手回答。"

    # 5. 返回的快照为独立拷贝，修改不污染内部
    snap["tool_calls"].append({"id": "dummy"})
    snap_after = bridge.get_active_turn_snapshot("s1")
    assert snap_after["tool_calls"] == []

    # 6. 模拟回合结束清理
    with bridge._active_turn_lock:
        bridge._active_turn_snapshots.pop("s1", None)

    assert bridge.get_active_turn_snapshot("s1") is None
    assert bridge.get_all_active_turn_snapshots() == {}


def test_session_last_error_and_message_loading():
    bridge = _make_test_bridge()
    bridge._session_last_errors = {}
    bridge._session_last_errors_lock = threading.Lock()

    # 1. 记录会话错误
    bridge._record_session_last_error(
        "s_err",
        "litellm.APIError: APIError: OpenAIException - 余额不足",
        model_name="deepseek-chat",
        model_id="deepseek-v3",
    )

    err = bridge._get_session_last_error("s_err")
    assert err is not None
    assert "余额不足" in err["message"]
    assert err["model_name"] == "deepseek-chat"

    # 2. 模拟 _load_session_messages 将错误附加到末尾
    class FakeMemory:
        def get_history_paged(self, s, before_id=None, limit=50):
            return ([{"role": "user", "content": "你好", "_display": {}, "_msg_id": 1}], False)
        def set_session_last_error(self, *args, **kwargs):
            pass
        def get_session_last_error(self, *args, **kwargs):
            return None

    bridge._memory = FakeMemory()
    bridge._media_items_for_frontend = lambda d: []

    msgs, has_more, oldest_id = bridge._load_session_messages("s_err")
    assert has_more is False and oldest_id == 1
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["execution_state"] == "error"
    assert "余额不足" in msgs[1]["error_note"]
    assert msgs[1]["model_name"] == "deepseek-chat"

    # 3. 清理错误
    bridge._clear_session_last_error("s_err")
    assert bridge._get_session_last_error("s_err") is None

