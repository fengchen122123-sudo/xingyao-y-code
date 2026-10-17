# tests/test_gui_session_archive.py — GUI 后端桥接层「对话归档」（TDD，先行失败）
import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge():
    """不经 __init__ 构造裸实例，挂一个内存版 MemoryManager（参考 test_gui_workspace_groups）。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._current_session = "当前会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    return bridge


def _seed(mem, name, workdir="GLOBAL", n=2):
    """建会话并写入 n 条消息（默认命名规则之外的名字，避免被空会话过滤逻辑跳过）。"""
    mem.create_session_explicit(name, workdir=workdir, source="GUI")
    for i in range(n):
        mem.add_message(name, "user" if i % 2 == 0 else "assistant", f"msg-{i}")


def _flatten_names(groups):
    """把 get_sessions 的分组树摊平成会话名列表。"""
    names = []
    for g in groups:
        for s in g.get("sessions", []):
            names.append(s["name"])
    return names


class _EventRecorder:
    """替代 emit_event 的事件记录器：返回 (event_type, data) 列表。"""

    def __init__(self):
        self.events = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))

    def types(self):
        return [e[0] for e in self.events]

    def last(self):
        return self.events[-1]


# ----------------------------------------------------------------------
# 归档
# ----------------------------------------------------------------------
def test_archive_session_hides_from_default_list():
    """归档成功返回 ok，且默认 get_sessions 不再返回该会话。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")
    _seed(bridge._memory, "当前会话")
    assert "待归档会话" in _flatten_names(bridge.get_sessions())

    res = bridge.archive_session("待归档会话")

    assert res["ok"] is True
    assert res.get("archived_at"), "归档成功必须回带归档时刻"
    assert "待归档会话" not in _flatten_names(bridge.get_sessions())
    assert "当前会话" in _flatten_names(bridge.get_sessions())


def test_archive_session_rejects_current_session():
    """当前活跃会话禁止归档：返回 ok=False 且带可读中文提示。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "当前会话")

    res = bridge.archive_session("当前会话")

    assert res["ok"] is False
    assert "当前" in res["msg"]
    # 拒绝后状态不变：会话仍在主列表
    assert "当前会话" in _flatten_names(bridge.get_sessions())


def test_archive_session_missing_returns_false():
    """会话不存在：返回 ok=False，提示含会话名。"""
    bridge = _bare_bridge()

    res = bridge.archive_session("不存在的会话")

    assert res["ok"] is False
    assert "不存在的会话" in res["msg"]


def test_archive_session_idempotent_keeps_original_stamp():
    """重复归档幂等：仍返回 ok，且不刷新 archived_at。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")

    first = bridge.archive_session("待归档会话")
    second = bridge.archive_session("待归档会话")

    assert first["ok"] is True and second["ok"] is True
    assert second["archived_at"] == first["archived_at"]
    names = [s["name"] for s in bridge.get_archived_sessions()]
    assert names.count("待归档会话") == 1


# ----------------------------------------------------------------------
# 取消归档
# ----------------------------------------------------------------------
def test_unarchive_session_restores_to_main_list():
    """取消归档后会话回到主列表，并离开归档列表。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")
    assert bridge.archive_session("待归档会话")["ok"] is True

    res = bridge.unarchive_session("待归档会话")

    assert res["ok"] is True
    assert "待归档会话" in _flatten_names(bridge.get_sessions())
    assert "待归档会话" not in [s["name"] for s in bridge.get_archived_sessions()]


def test_unarchive_session_missing_returns_false():
    """取消归档一个不存在的会话：返回 ok=False。"""
    bridge = _bare_bridge()

    res = bridge.unarchive_session("不存在的会话")

    assert res["ok"] is False
    assert "不存在的会话" in res["msg"]


def test_unarchive_session_idempotent():
    """对未归档会话取消归档不报错（幂等）。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "普通会话")

    res = bridge.unarchive_session("普通会话")

    assert res["ok"] is True
    assert "普通会话" in _flatten_names(bridge.get_sessions())


# ----------------------------------------------------------------------
# 归档列表
# ----------------------------------------------------------------------
def test_get_archived_sessions_only_archived_sorted_desc():
    """只返回归档会话，且按 archived_at 倒序。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "最早归档")
    _seed(bridge._memory, "中间归档")
    _seed(bridge._memory, "最近归档")
    _seed(bridge._memory, "未归档会话")
    bridge._memory.archive_session("最早归档", archived_at="2026-01-01 08:00:00")
    bridge._memory.archive_session("中间归档", archived_at="2026-02-01 08:00:00")
    bridge._memory.archive_session("最近归档", archived_at="2026-03-01 08:00:00")

    archived = bridge.get_archived_sessions()

    assert [s["name"] for s in archived] == ["最近归档", "中间归档", "最早归档"]
    assert all(s.get("archived_at") for s in archived)
    # 未归档会话不得出现在归档列表
    assert "未归档会话" not in [s["name"] for s in archived]


def test_get_archived_sessions_entry_shape_matches_get_sessions():
    """归档条目字段风格与 get_sessions 中的会话条目一致（另加 archived_at）。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "项目会话", workdir="C:/proj/alpha")
    bridge._memory.archive_session("项目会话")

    entry = bridge.get_archived_sessions()[0]

    for key in ("name", "session_id", "display_name", "count", "workdir",
                "is_global", "updated_at", "archived_at"):
        assert key in entry, f"归档条目缺少字段 {key}"
    assert entry["name"] == "项目会话"
    assert entry["is_global"] is False
    assert entry["count"] == 2
    assert entry["workdir"] == gui_bridge._resolve_real_path("C:/proj/alpha")


# ----------------------------------------------------------------------
# 事件推送
# ----------------------------------------------------------------------
def test_archive_session_emits_event(monkeypatch):
    """归档成功推送 session_archived，负载含会话名、归档时刻与刷新后的列表。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.archive_session("待归档会话")

    assert res["ok"] is True
    assert recorder.types() == ["session_archived"]
    _, data = recorder.last()
    assert data["name"] == "待归档会话"
    assert data["session_name"] == "待归档会话"
    assert data["archived_at"] == res["archived_at"]
    # 负载附带最新列表，前端无需自算
    assert "待归档会话" not in _flatten_names(data["sessions"])
    assert "待归档会话" in [s["name"] for s in data["archived_sessions"]]


def test_unarchive_session_emits_event(monkeypatch):
    """取消归档成功推送 session_unarchived，负载含会话名与刷新后的列表。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")
    assert bridge.archive_session("待归档会话")["ok"] is True
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.unarchive_session("待归档会话")

    assert res["ok"] is True
    assert recorder.types() == ["session_unarchived"]
    _, data = recorder.last()
    assert data["name"] == "待归档会话"
    assert data["archived_at"] is None
    assert "待归档会话" in _flatten_names(data["sessions"])
    assert "待归档会话" not in [s["name"] for s in data["archived_sessions"]]


def test_archive_event_failure_does_not_break_result(monkeypatch):
    """事件推送异常不得让归档本身报失败（照 session_renamed 的容错写法）。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")

    def _boom(event_type, data):
        raise RuntimeError("窗口已关闭")

    monkeypatch.setattr(bridge, "emit_event", _boom)

    res = bridge.archive_session("待归档会话")

    assert res["ok"] is True
    assert "待归档会话" not in _flatten_names(bridge.get_sessions())


def test_failed_archive_emits_no_event(monkeypatch):
    """归档当前会话被拒时不得推送任何事件。"""
    bridge = _bare_bridge()
    _seed(bridge._memory, "当前会话")
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.archive_session("当前会话")

    assert res["ok"] is False
    assert recorder.events == []


# ----------------------------------------------------------------------
# 归档会话检索（search_sessions_full 第 5 个位置参数 include_archived）
# ----------------------------------------------------------------------
_SEARCH_KEYWORD = "归档检索唯一关键词"


def _seed_searchable(mem, name, keyword=_SEARCH_KEYWORD, workdir="GLOBAL"):
    """建会话并写入一条能被全文检索命中的消息。"""
    mem.create_session_explicit(name, workdir=workdir, source="GUI")
    mem.add_message(name, "user", f"这是一条包含 {keyword} 的消息")


def test_search_sessions_full_excludes_archived_by_default():
    """默认 include_archived=False：归档会话不出现在检索结果里，未归档会话正常命中。"""
    bridge = _bare_bridge()
    _seed_searchable(bridge._memory, "归档检索会话")
    _seed_searchable(bridge._memory, "在库检索会话")
    assert bridge.archive_session("归档检索会话")["ok"] is True

    res = bridge.search_sessions_full(_SEARCH_KEYWORD, "all", "", 30)

    assert res["ok"] is True
    names = [r["session_name"] for r in res["results"]]
    assert "归档检索会话" not in names
    assert "在库检索会话" in names


def test_search_sessions_full_include_archived_true_finds_archived():
    """include_archived=True 时归档会话可被检索到（前端「包含已归档」开关）。"""
    bridge = _bare_bridge()
    _seed_searchable(bridge._memory, "归档检索会话")
    assert bridge.archive_session("归档检索会话")["ok"] is True

    res = bridge.search_sessions_full(_SEARCH_KEYWORD, "all", "", 30, True)

    assert res["ok"] is True
    assert res["total"] >= 1
    assert "归档检索会话" in [r["session_name"] for r in res["results"]]


def test_search_sessions_full_legacy_four_args_still_ok():
    """旧 4 参调用不报 TypeError，且行为不变（未归档会话照常命中）。"""
    bridge = _bare_bridge()
    _seed_searchable(bridge._memory, "在库检索会话")

    res = bridge.search_sessions_full(_SEARCH_KEYWORD, "all", "", 30)

    assert res["ok"] is True
    assert res["total"] >= 1
    assert res["results"][0]["session_name"] == "在库检索会话"


# ----------------------------------------------------------------------
# 批量取消归档 / 批量删除（归档区多选操作）
# ----------------------------------------------------------------------
def _seed_archived(bridge, name, workdir="GLOBAL"):
    """建一个已归档的会话（批量用例的前置状态）。"""
    _seed(bridge._memory, name, workdir=workdir)
    assert bridge._memory.archive_session(name) is True


def test_unarchive_sessions_restores_all_selected(monkeypatch):
    """批量取消归档：选中的会话全部回主列表，只推一次事件。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "批量会话一")
    _seed_archived(bridge, "批量会话二")
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.unarchive_sessions(["批量会话一", "批量会话二"])

    assert res["ok"] is True
    assert res["count"] == 2
    assert sorted(res["names"]) == ["批量会话一", "批量会话二"]
    assert res["failed"] == []
    assert bridge.get_archived_sessions() == []
    main = _flatten_names(res["sessions"])
    assert "批量会话一" in main and "批量会话二" in main
    # 逐条推送会让前端反复重算并互相覆盖快照，故批量只推一次
    assert recorder.types() == ["session_unarchived"]
    _, data = recorder.last()
    assert sorted(data["names"]) == ["批量会话一", "批量会话二"]
    assert data["archived_sessions"] == []


def test_unarchive_sessions_partial_failure_not_blocking(monkeypatch):
    """单项失败不阻断其它项：成功的照常恢复，失败项带会话名与原因返回。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "存在的归档会话")
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.unarchive_sessions(["存在的归档会话", "不存在会话"])

    assert res["ok"] is True
    assert res["names"] == ["存在的归档会话"]
    assert [f["name"] for f in res["failed"]] == ["不存在会话"]
    assert "不存在会话" in res["failed"][0]["msg"]
    assert "存在的归档会话" in _flatten_names(bridge.get_sessions())


def test_unarchive_sessions_empty_names_rejected(monkeypatch):
    """空名单直接拒绝，不落库也不推事件。"""
    bridge = _bare_bridge()
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.unarchive_sessions([])

    assert res["ok"] is False
    assert res["msg"]
    assert recorder.events == []


def test_unarchive_sessions_dedupes_and_drops_blank_names(monkeypatch):
    """重复名与空名归一：同一会话只处理一次。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "去重会话")
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.unarchive_sessions(["去重会话", "去重会话", "", "   "])

    assert res["count"] == 1
    assert res["names"] == ["去重会话"]


def test_delete_archived_sessions_removes_selected(monkeypatch):
    """批量删除：选中的归档会话物理删除，未选中的留在归档区。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "待删一")
    _seed_archived(bridge, "待删二")
    _seed_archived(bridge, "保留会话")
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.delete_archived_sessions(["待删一", "待删二"])

    assert res["ok"] is True
    assert res["count"] == 2
    assert sorted(res["names"]) == ["待删一", "待删二"]
    assert res["failed"] == []
    assert [s["name"] for s in bridge.get_archived_sessions()] == ["保留会话"]
    # 会话行与消息一并清掉：include_archived 全量里再查不到这两个名字
    left = [s["name"] for s in bridge._memory.get_all_sessions(include_archived=True)]
    assert "待删一" not in left and "待删二" not in left
    assert recorder.types() == ["session_deleted"]


def test_delete_archived_sessions_skips_state_drift(monkeypatch):
    """状态漂移保护：确认弹窗到落盘之间已被取消归档的会话不删，只跳过并说明。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "漂移会话")
    assert bridge.unarchive_session("漂移会话")["ok"] is True
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.delete_archived_sessions(["漂移会话"])

    assert res["ok"] is False
    assert res["count"] == 0
    assert [f["name"] for f in res["failed"]] == ["漂移会话"]
    # 会话完好无损，且已回到主列表
    assert "漂移会话" in _flatten_names(bridge.get_sessions())


def test_delete_archived_sessions_rejects_running_session(monkeypatch):
    """运行中的会话拒绝删除（与单条 delete_session 同源保护），其余照常删。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "运行中会话")
    _seed_archived(bridge, "可删会话")
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())
    monkeypatch.setattr(bridge, "_is_turn_active", lambda name: name == "运行中会话")

    res = bridge.delete_archived_sessions(["运行中会话", "可删会话"])

    assert res["names"] == ["可删会话"]
    assert [f["name"] for f in res["failed"]] == ["运行中会话"]
    assert "运行中会话" in [s["name"] for s in bridge.get_archived_sessions()]


def test_delete_archived_sessions_drops_queued_messages(monkeypatch):
    """删除归档会话时其排队消息一并丢弃，避免补发线程写进已删会话（幽灵数据）。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "带排队会话")
    bridge._message_queues["带排队会话"] = ["排队消息"]
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.delete_archived_sessions(["带排队会话"])

    assert res["names"] == ["带排队会话"]
    assert "带排队会话" not in bridge._message_queues


def test_delete_archived_sessions_all_skipped_emits_no_event(monkeypatch):
    """一项都没删掉时不推事件（避免前端为无变化的列表白跑一次刷新）。"""
    bridge = _bare_bridge()
    _seed_archived(bridge, "漂移会话")
    assert bridge.unarchive_session("漂移会话")["ok"] is True
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.delete_archived_sessions(["漂移会话"])

    assert res["ok"] is False
    assert recorder.events == []
