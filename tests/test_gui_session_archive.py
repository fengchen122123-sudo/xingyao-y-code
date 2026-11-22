import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


def _bare_bridge():
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._current_session = "当前会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    return bridge


def _seed(mem, name, workdir="GLOBAL", n=2):
    mem.create_session_explicit(name, workdir=workdir, source="GUI")
    for i in range(n):
        mem.add_message(name, "user" if i % 2 == 0 else "assistant", f"msg-{i}")


def _flatten_names(groups):
    names = []
    for g in groups:
        for s in g.get("sessions", []):
            names.append(s["name"])
    return names


class _EventRecorder:

    def __init__(self):
        self.events = []

    def __call__(self, event_type, data):
        self.events.append((event_type, data))

    def types(self):
        return [e[0] for e in self.events]

    def last(self):
        return self.events[-1]


def test_archive_session_hides_from_default_list():
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
    bridge = _bare_bridge()
    _seed(bridge._memory, "当前会话")

    res = bridge.archive_session("当前会话")

    assert res["ok"] is False
    assert "当前" in res["msg"]
    assert "当前会话" in _flatten_names(bridge.get_sessions())


def test_archive_session_missing_returns_false():
    bridge = _bare_bridge()

    res = bridge.archive_session("不存在的会话")

    assert res["ok"] is False
    assert "不存在的会话" in res["msg"]


def test_archive_session_idempotent_keeps_original_stamp():
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")

    first = bridge.archive_session("待归档会话")
    second = bridge.archive_session("待归档会话")

    assert first["ok"] is True and second["ok"] is True
    assert second["archived_at"] == first["archived_at"]
    names = [s["name"] for s in bridge.get_archived_sessions()]
    assert names.count("待归档会话") == 1


def test_unarchive_session_restores_to_main_list():
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")
    assert bridge.archive_session("待归档会话")["ok"] is True

    res = bridge.unarchive_session("待归档会话")

    assert res["ok"] is True
    assert "待归档会话" in _flatten_names(bridge.get_sessions())
    assert "待归档会话" not in [s["name"] for s in bridge.get_archived_sessions()]


def test_unarchive_session_missing_returns_false():
    bridge = _bare_bridge()

    res = bridge.unarchive_session("不存在的会话")

    assert res["ok"] is False
    assert "不存在的会话" in res["msg"]


def test_unarchive_session_idempotent():
    bridge = _bare_bridge()
    _seed(bridge._memory, "普通会话")

    res = bridge.unarchive_session("普通会话")

    assert res["ok"] is True
    assert "普通会话" in _flatten_names(bridge.get_sessions())


def test_get_archived_sessions_only_archived_sorted_desc():
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
    assert "未归档会话" not in [s["name"] for s in archived]


def test_get_archived_sessions_entry_shape_matches_get_sessions():
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


def test_archive_session_emits_event(monkeypatch):
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
    assert "待归档会话" not in _flatten_names(data["sessions"])
    assert "待归档会话" in [s["name"] for s in data["archived_sessions"]]


def test_unarchive_session_emits_event(monkeypatch):
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
    bridge = _bare_bridge()
    _seed(bridge._memory, "待归档会话")

    def _boom(event_type, data):
        raise RuntimeError("窗口已关闭")

    monkeypatch.setattr(bridge, "emit_event", _boom)

    res = bridge.archive_session("待归档会话")

    assert res["ok"] is True
    assert "待归档会话" not in _flatten_names(bridge.get_sessions())


def test_failed_archive_emits_no_event(monkeypatch):
    bridge = _bare_bridge()
    _seed(bridge._memory, "当前会话")
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.archive_session("当前会话")

    assert res["ok"] is False
    assert recorder.events == []


_SEARCH_KEYWORD = "归档检索唯一关键词"


def _seed_searchable(mem, name, keyword=_SEARCH_KEYWORD, workdir="GLOBAL"):
    mem.create_session_explicit(name, workdir=workdir, source="GUI")
    mem.add_message(name, "user", f"这是一条包含 {keyword} 的消息")


def test_search_sessions_full_excludes_archived_by_default():
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
    bridge = _bare_bridge()
    _seed_searchable(bridge._memory, "归档检索会话")
    assert bridge.archive_session("归档检索会话")["ok"] is True

    res = bridge.search_sessions_full(_SEARCH_KEYWORD, "all", "", 30, True)

    assert res["ok"] is True
    assert res["total"] >= 1
    assert "归档检索会话" in [r["session_name"] for r in res["results"]]


def test_search_sessions_full_legacy_four_args_still_ok():
    bridge = _bare_bridge()
    _seed_searchable(bridge._memory, "在库检索会话")

    res = bridge.search_sessions_full(_SEARCH_KEYWORD, "all", "", 30)

    assert res["ok"] is True
    assert res["total"] >= 1
    assert res["results"][0]["session_name"] == "在库检索会话"


def _seed_archived(bridge, name, workdir="GLOBAL"):
    _seed(bridge._memory, name, workdir=workdir)
    assert bridge._memory.archive_session(name) is True


def test_unarchive_sessions_restores_all_selected(monkeypatch):
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
    assert recorder.types() == ["session_unarchived"]
    _, data = recorder.last()
    assert sorted(data["names"]) == ["批量会话一", "批量会话二"]
    assert data["archived_sessions"] == []


def test_unarchive_sessions_partial_failure_not_blocking(monkeypatch):
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
    bridge = _bare_bridge()
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.unarchive_sessions([])

    assert res["ok"] is False
    assert res["msg"]
    assert recorder.events == []


def test_unarchive_sessions_dedupes_and_drops_blank_names(monkeypatch):
    bridge = _bare_bridge()
    _seed_archived(bridge, "去重会话")
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.unarchive_sessions(["去重会话", "去重会话", "", "   "])

    assert res["count"] == 1
    assert res["names"] == ["去重会话"]


def test_delete_archived_sessions_removes_selected(monkeypatch):
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
    left = [s["name"] for s in bridge._memory.get_all_sessions(include_archived=True)]
    assert "待删一" not in left and "待删二" not in left
    assert recorder.types() == ["session_deleted"]


def test_delete_archived_sessions_skips_state_drift(monkeypatch):
    bridge = _bare_bridge()
    _seed_archived(bridge, "漂移会话")
    assert bridge.unarchive_session("漂移会话")["ok"] is True
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.delete_archived_sessions(["漂移会话"])

    assert res["ok"] is False
    assert res["count"] == 0
    assert [f["name"] for f in res["failed"]] == ["漂移会话"]
    assert "漂移会话" in _flatten_names(bridge.get_sessions())


def test_delete_archived_sessions_rejects_running_session(monkeypatch):
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
    bridge = _bare_bridge()
    _seed_archived(bridge, "带排队会话")
    bridge._message_queues["带排队会话"] = ["排队消息"]
    monkeypatch.setattr(bridge, "emit_event", _EventRecorder())

    res = bridge.delete_archived_sessions(["带排队会话"])

    assert res["names"] == ["带排队会话"]
    assert "带排队会话" not in bridge._message_queues


def test_delete_archived_sessions_all_skipped_emits_no_event(monkeypatch):
    bridge = _bare_bridge()
    _seed_archived(bridge, "漂移会话")
    assert bridge.unarchive_session("漂移会话")["ok"] is True
    recorder = _EventRecorder()
    monkeypatch.setattr(bridge, "emit_event", recorder)

    res = bridge.delete_archived_sessions(["漂移会话"])

    assert res["ok"] is False
    assert recorder.events == []
