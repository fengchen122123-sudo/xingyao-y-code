import json
import queue
import threading

import pytest

import config
import core.model_manager as model_manager
from core.computer.models import ComputerRunSnapshot

from gui_bridge import ApiBridge, JS_QUEUE_MAXSIZE
from tests.computer_fakes import FakeBridgeController


def _use_tmp_config(tmp_path, monkeypatch, data):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    return cfg


_ONE_MODEL_CONFIG = {
    "active_model_id": "m1",
    "providers": [
        {
            "id": "p1",
            "name": "测试厂商",
            "base_url": "https://api.example.com",
            "api_key": "",
            "protocol": "openai",
            "models": [
                {
                    "id": "m1",
                    "name": "model-one",
                    "litellm_model": "openai/model-one",
                    "context_window": 200000,
                }
            ],
        }
    ],
}


def _make_bridge(is_turn_active):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "s1"
    bridge._turn_start_lock = threading.Lock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._cancel_events = {}
    bridge._is_turn_active = is_turn_active
    spawned = []
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append((a, k))
    events = []
    bridge.emit_event = lambda etype, payload: events.append((etype, payload))
    stopped = []
    bridge.stop_current_turn = lambda session="": stopped.append(session)
    return bridge, spawned, events, stopped


def test_send_user_message_queues_while_turn_active():
    bridge, spawned, events, _ = _make_bridge(lambda s: True)

    res = bridge.send_user_message("第一条", None, ["d:/a.txt"])

    assert res == {"ok": True, "queued": True}
    queue = bridge._message_queues["s1"]
    assert len(queue) == 1
    assert queue[0]["text"] == "第一条 @d:/a.txt"
    assert queue[0]["display_text"] == "第一条"
    assert events and events[-1][0] == "message_queue"
    assert events[-1][1]["session_name"] == "s1"
    assert events[-1][1]["queue"][0]["text"] == "第一条"
    assert spawned == []


def test_send_user_message_direct_when_idle():
    bridge, spawned, events, _ = _make_bridge(lambda s: False)

    res = bridge.send_user_message("你好")

    assert res == {"ok": True}
    assert len(spawned) == 1
    assert spawned[0][0] == ("你好", "s1")
    assert "s1" not in bridge._message_queues
    assert events == []


def test_send_user_message_binds_known_model(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, dict(_ONE_MODEL_CONFIG))
    bridge, spawned, _, _ = _make_bridge(lambda s: False)
    bound = []
    bridge._memory = type(
        "M", (), {"set_session_model_id": lambda self, s, m: bound.append((s, m))}
    )()

    res = bridge.send_user_message("你好", None, None, None, "m1")

    assert res == {"ok": True}
    assert bound == [("s1", "m1")]
    assert len(spawned) == 1


def test_pending_draft_first_message_switches_current_session(tmp_path):
    from core.memory import MemoryManager

    mem = MemoryManager(db_path=str(tmp_path / "t.db"))
    mem.ensure_session("我这个自然语言交互如果给", workdir="D:/iMOM/SHMK/publish", source="GUI")
    mem.add_message("我这个自然语言交互如果给", "user", "旧会话的历史消息")

    bridge, spawned, _, _ = _make_bridge(lambda s: False)
    bridge._memory = mem
    bridge._current_session = "我这个自然语言交互如果给"
    bridge.get_sessions = lambda: {}

    res = bridge.send_user_message(
        "为什么我输入@无法找到红", "session_abc123", None, "D:/proj"
    )

    assert res["ok"] is True
    assert bridge._current_session == "为什么我输入@无法找到红"
    assert spawned[0][0][1] == "为什么我输入@无法找到红"
    names = [s["name"] for s in bridge.search_sessions("")["sessions"]]
    assert "我这个自然语言交互如果给" in names
    assert "为什么我输入@无法找到红" not in names
    mem.conn.close()


def test_pending_draft_first_message_lan_does_not_steal_current_session(tmp_path):
    from core.memory import MemoryManager

    mem = MemoryManager(db_path=str(tmp_path / "t.db"))
    mem.ensure_session("本地会话", workdir="D:/proj", source="GUI")
    mem.add_message("本地会话", "user", "本地消息")

    bridge, spawned, _, _ = _make_bridge(lambda s: False)
    bridge._memory = mem
    bridge._current_session = "本地会话"
    bridge.get_sessions = lambda: {}

    res = bridge.send_user_message(
        "远程消息", "session_def456", None, "D:/proj", None, None, "lan"
    )

    assert res["ok"] is True
    assert bridge._current_session == "本地会话"
    mem.conn.close()


def test_send_user_message_ignores_unknown_model_binding(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, dict(_ONE_MODEL_CONFIG))
    bridge, spawned, _, _ = _make_bridge(lambda s: False)
    bound = []
    bridge._memory = type(
        "M", (), {"set_session_model_id": lambda self, s, m: bound.append((s, m))}
    )()

    res = bridge.send_user_message("你好", None, None, None, "m-deleted")

    assert res == {"ok": True}
    assert bound == []
    assert len(spawned) == 1


def test_drain_message_queue_spawns_head_fifo():
    bridge, spawned, events, _ = _make_bridge(lambda s: False)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "第一条", "display_text": "第一条", "attachments": [], "queued_at": 1.0},
        {"id": "q2", "text": "第二条", "display_text": "第二条", "attachments": [], "queued_at": 2.0},
    ]

    bridge._drain_message_queue("s1")

    assert [a[0][0] for a in spawned] == ["第一条"]
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q2"]
    assert events and events[-1][0] == "message_queue"
    assert [m["id"] for m in events[-1][1]["queue"]] == ["q2"]


def test_drain_skips_while_turn_active():
    bridge, spawned, _, _ = _make_bridge(lambda s: True)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "x", "display_text": "x", "attachments": []},
    ]

    bridge._drain_message_queue("s1")

    assert spawned == []
    assert len(bridge._message_queues["s1"]) == 1


def test_cancel_queued_message_removes_only_target():
    bridge, _, _, _ = _make_bridge(lambda s: True)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "a", "display_text": "a", "attachments": []},
        {"id": "q2", "text": "b", "display_text": "b", "attachments": []},
    ]

    res = bridge.cancel_queued_message("s1", "q1")
    assert res["ok"] is True and res["removed"] == 1
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q2"]

    res2 = bridge.cancel_queued_message("s1", "q2")
    assert res2["removed"] == 1
    assert "s1" not in bridge._message_queues


def test_jump_queue_moves_to_front_and_stops_turn():
    bridge, _, events, stopped = _make_bridge(lambda s: True)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "a", "display_text": "a", "attachments": []},
        {"id": "q2", "text": "b", "display_text": "b", "attachments": []},
    ]

    res = bridge.jump_queue("s1", "q2")

    assert res["ok"] is True
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q2", "q1"]
    assert stopped == ["s1"]
    assert events and events[-1][0] == "message_queue"


def test_jump_queue_without_active_turn_only_reorders():
    bridge, _, _, stopped = _make_bridge(lambda s: False)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "a", "display_text": "a", "attachments": []},
        {"id": "q2", "text": "b", "display_text": "b", "attachments": []},
    ]

    res = bridge.jump_queue("s1", "q2")

    assert res["ok"] is True
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q2", "q1"]
    assert stopped == []


def test_jump_queue_unknown_id_fails_gracefully():
    bridge, _, _, stopped = _make_bridge(lambda s: True)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "a", "display_text": "a", "attachments": []},
    ]

    res = bridge.jump_queue("s1", "q_missing")

    assert res["ok"] is False
    assert stopped == []
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q1"]


def test_get_message_queue_returns_snapshot():
    bridge, _, _, _ = _make_bridge(lambda s: True)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "完整 @d:/a.txt", "display_text": "完整", "attachments": ["d:/a.txt"]},
    ]

    res = bridge.get_message_queue("s1")

    assert res["ok"] is True
    assert res["session_name"] == "s1"
    assert res["queue"] == [
        {"id": "q1", "text": "完整", "attachments": ["d:/a.txt"]},
    ]


def _make_computer_bridge():
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = False
    instance._window = None
    instance._js_queue = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
    instance._js_dispatcher = threading.current_thread()
    instance._js_dispatcher_lock = threading.Lock()
    instance._js_dropped = 0
    instance._computer_event_listener = None
    return instance


@pytest.fixture
def fake_controller():
    return FakeBridgeController()


@pytest.fixture
def bridge(fake_controller, monkeypatch):
    monkeypatch.setattr(
        "core.computer.controller.get_computer_control_service",
        lambda: fake_controller,
    )
    instance = _make_computer_bridge()
    instance.get_computer_control_status()
    return instance


def test_bridge_mutation_requires_current_run_id(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")
    result = bridge.stop_computer_control("run-b")
    assert result["ok"] is False
    assert fake_controller.snapshot().run_id == "run-a"


def test_safety_acknowledge_unavailable_without_verified_contract(fake_controller, bridge):
    result = bridge.acknowledge_computer_safety("run-a", ["sc_1"])
    assert result["ok"] is False
    assert result["error_code"] == "computer_provider_safety_required"


def test_bridge_pause_resume_pass_through_to_controller(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")

    paused = bridge.pause_computer_control("run-a")
    assert paused["ok"] is True
    assert paused["status"] == "paused"
    assert fake_controller.pause_calls == [("run-a", "user_pause")]

    resumed = bridge.resume_computer_control("run-a")
    assert resumed["ok"] is True
    assert resumed["status"] == "running"
    assert fake_controller.resume_calls == ["run-a"]


def test_bridge_stop_with_current_run_id_stops_run(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")

    result = bridge.stop_computer_control("run-a")

    assert result["ok"] is True
    assert result["status"] == "stopped"
    assert fake_controller.stop_calls == [("run-a", "user_stop")]
    assert fake_controller.snapshot().run_id == "run-a"


def test_every_control_operation_returns_full_snapshot(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")
    fake_controller.action_count = 12
    snapshot_keys = set(ComputerRunSnapshot().to_public_dict())

    results = [
        bridge.get_computer_control_status(),
        bridge.pause_computer_control("run-a"),
        bridge.resume_computer_control("run-a"),
        bridge.stop_computer_control("run-a"),
    ]
    assert [result["status"] for result in results] == [
        "running", "paused", "running", "stopped",
    ]
    for result in results:
        assert result["ok"] is True
        assert snapshot_keys <= set(result)
        assert result["run_id"] == "run-a"
        assert result["action_count"] == 12
        assert result["target"] == "browser"


def test_failed_mutation_returns_current_snapshot_for_recalibration(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")

    result = bridge.stop_computer_control("run-b")

    assert result["ok"] is False
    assert result["error_code"] == "computer_invalid_state"
    assert result["run_id"] == "run-a"
    assert result["status"] == "running"
    assert fake_controller.stop_calls == []
