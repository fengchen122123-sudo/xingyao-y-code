"""流式尾部丢失修复的回归测试。

根因 1（已修）：回合收尾 finally 直接 pop delta 缓冲，回合末尾滞留在缓冲里
的最后一块文本（距上次冲刷不足 60ms、冲刷线程未轮到）被静默丢弃，前端靠
delta 累加渲染，尾部永久缺失。
根因 2 兜底（新增）：evaluate_js 投递可能静默失败，后端回合结束补发
message_final 全文事件，前端仅在全文更长时覆盖校正。
"""

import threading

from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge(db=":memory:"):
    """不经 __init__ 构造裸实例，挂载 delta 缓冲与内存版 MemoryManager。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._shutting_down = False
    bridge._window = None
    bridge._lan_server = None
    bridge._delta_lock = threading.Lock()
    bridge._delta_buffers = {}
    bridge._delta_kinds = {}
    bridge._delta_last_flush = 0.0
    return bridge


def test_finalize_session_deltas_flushes_tail_before_discard(monkeypatch):
    """回归保护：收尾必须先冲刷再清缓冲，滞留在缓冲的尾部文本不得丢失。"""
    bridge = _bare_bridge()
    sent = []
    monkeypatch.setattr(
        bridge, "_emit_raw", lambda kind, data: sent.append((kind, data))
    )
    bridge._delta_buffers["会话A"] = ["尾部文本"]
    bridge._delta_kinds["会话A"] = "token_delta"

    bridge._finalize_session_deltas("会话A")

    assert sent == [("token_delta", {"text": "尾部文本", "session_name": "会话A"})]
    assert bridge._delta_buffers == {}
    assert bridge._delta_kinds == {}


def test_finalize_session_deltas_tolerates_empty_buffers():
    """缓冲为空时收尾是无害 no-op，不得抛异常。"""
    bridge = _bare_bridge()
    bridge._finalize_session_deltas("不存在的会话")
    assert bridge._delta_buffers == {}


def test_emit_message_final_sends_last_assistant_full_text(monkeypatch):
    """message_final 携带最后一条 assistant 消息全文（content + reasoning）。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "问")
    bridge._memory.add_message("会话A", "assistant", "前半轮回复")
    bridge._memory.add_message("会话A", "assistant", "完整回复全文")

    bridge._emit_message_final("会话A")

    assert events == [(
        "message_final",
        {"session_name": "会话A", "content": "完整回复全文", "reasoning_content": ""},
    )]


def test_emit_message_final_noop_without_assistant_text(monkeypatch):
    """会话中没有 assistant 文本（如开场即工具调用失败）时不发事件。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "只有用户消息")

    bridge._emit_message_final("会话A")

    assert events == []


def test_emit_message_final_swallows_errors():
    """兜底事件绝不能阻断 turn_finish：内存读取抛异常时静默吞掉。"""
    bridge = _bare_bridge()

    class _BrokenMemory:
        def get_history(self, session_name):
            raise RuntimeError("磁盘 IO 失败")

    bridge._memory = _BrokenMemory()
    # 不应抛出
    bridge._emit_message_final("会话A")


# ---------------------------------------------------------------------------
# 回合边界过滤（after_msg_id）：异常结束的回合不得把上一轮助手全文当成本轮回复
# 补发。core/llm.rollback_turn_messages() 会回滚本轮内存消息，若本轮尚未落库
# 任何助手消息，history 末尾会退化成上一轮的助手消息；前端 message_final 分支
# 只做「全文更长就覆盖」（appStore.ts），不过滤就会把上一轮结果灌进本轮气泡。
# ---------------------------------------------------------------------------


def _bare_turn_bridge(db=":memory:"):
    """裸 bridge：在 _bare_bridge 之上补齐 _run_turn_worker 全流程所需属性。"""
    bridge = _bare_bridge(db)
    bridge._cancel_events = {"session": threading.Event()}
    bridge._active_turns = {}
    bridge._active_turn_ids = {}
    bridge._active_turn_lock = threading.Lock()
    bridge._active_turn_snapshots = {}
    bridge._task_status = "idle"
    bridge._bg_summary_pending_sessions = set()
    bridge._bg_summary_lock = threading.Lock()
    bridge._turn_start_lock = threading.Lock()
    bridge._turn_owner_sessions = {}
    bridge._turn_owner_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._delta_flusher = None
    bridge._current_permission_mode = "confirm"
    bridge._emit_token_usage = lambda: None
    # 按回合撤销：worker 生命周期依赖真实服务；服务必须与 bridge._memory
    # 共用同一连接（message_turn_links 的 FK 校验在同一个数据库里）
    from core.turn_undo import service as _tu_service
    import tempfile as _tempfile

    _tu_service.configure_default_manager(
        bridge._memory, blob_root=_tempfile.mkdtemp()
    )
    return bridge


def test_emit_message_final_skips_stale_assistant_before_boundary(monkeypatch):
    """边界之后没有助手消息：不得补发上一轮助手全文（用户报告的现象）。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "上一轮提问")
    bridge._memory.add_message("会话A", "assistant", "上一轮的助手全文")
    boundary = bridge._memory.get_last_message_id("会话A")

    bridge._emit_message_final("会话A", after_msg_id=boundary)

    assert events == []


def test_emit_message_final_sends_only_message_after_boundary(monkeypatch):
    """边界之后新增了助手消息：只补发该条，绝不能补发上一轮那条。"""
    bridge = _bare_bridge()
    events = []
    monkeypatch.setattr(
        bridge, "emit_event", lambda t, d: events.append((t, d))
    )
    bridge._memory.create_session_explicit("会话A")
    bridge._memory.add_message("会话A", "user", "上一轮提问")
    bridge._memory.add_message("会话A", "assistant", "上一轮的助手全文")
    boundary = bridge._memory.get_last_message_id("会话A")
    bridge._memory.add_message("会话A", "user", "本轮提问")
    bridge._memory.add_message("会话A", "assistant", "本轮的助手全文")

    bridge._emit_message_final("会话A", after_msg_id=boundary)

    assert events == [(
        "message_final",
        {"session_name": "会话A", "content": "本轮的助手全文", "reasoning_content": ""},
    )]


def test_worker_exception_does_not_replay_previous_assistant_message(monkeypatch):
    """端到端回归：回合异常结束时不得把上一轮助手全文当本轮回复补发。

    预置上一轮 user+assistant，本回合在 get_active_model 处抛异常（等价于
    provider 断连），turn_error 必须发出，而 message_final 一条都不能有。
    """
    bridge = _bare_turn_bridge()
    bridge._memory.create_session_explicit("session")
    bridge._memory.add_message("session", "user", "上一轮提问")
    bridge._memory.add_message("session", "assistant", "上一轮的助手全文" * 3)
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr(
        "gui_bridge.get_active_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider disconnected")),
    )

    bridge._run_turn_worker("你好", "session")

    kinds = [kind for kind, _ in emitted]
    assert "turn_error" in kinds
    assert "message_final" not in kinds
    assert [data for kind, data in emitted if kind == "message_final"] == []


def test_worker_emits_message_final_for_current_turn_message(monkeypatch, tmp_path):
    """端到端正向保护：本回合落库了助手消息时，仍要补发该条（不是上一轮）。"""
    bridge = _bare_turn_bridge()
    bridge._memory.create_session_explicit("session")
    bridge._memory.add_message("session", "user", "上一轮提问")
    bridge._memory.add_message("session", "assistant", "上一轮的助手全文")
    emitted = []
    bridge.emit_event = lambda kind, data: emitted.append((kind, data))
    monkeypatch.setattr("gui_bridge.get_active_model", lambda *_a, **_k: {
        "litellm_model": "model",
        "api_key": "key",
        "base_url": None,
        "context_window": 128000,
    })
    monkeypatch.setattr(
        "core.input_parser.parse_user_input", lambda text, **_kwargs: (text, [])
    )
    monkeypatch.setattr("core.agent_service.prepare_turn_messages", lambda *_a, **_k: [])
    bridge._session_workdir = lambda _name: str(tmp_path)
    bridge._sync_active_model_context = lambda *_a: None
    bridge._recalc_token_usage = lambda *_a, **_k: None
    bridge._subagent_progress_poller = lambda *_a, **_k: None
    bridge._maybe_auto_rename = lambda _name: None

    def _fake_consume(*_args, **_kwargs):
        # 模拟本回合内助手消息正常落库（未取消、未回滚）
        bridge._memory.add_message("session", "assistant", "本轮的完整回复")
        return False

    bridge._consume_turn_events = _fake_consume

    bridge._run_turn_worker("你好", "session")

    finals = [data for kind, data in emitted if kind == "message_final"]
    assert len(finals) == 1
    assert finals[0]["content"] == "本轮的完整回复"
