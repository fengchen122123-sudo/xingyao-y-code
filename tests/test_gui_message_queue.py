"""会话消息排队（FIFO）核心逻辑回归。

覆盖：回合运行中入队、回合结束 FIFO 补发、取消排队、插队发送。
全部用裸实例 + 桩函数构造，不依赖真实 LLM / DB / 前端。
"""
import json
import threading

import config
import core.model_manager as model_manager

from gui_bridge import ApiBridge


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
    """构造仅含消息排队所需内部状态的裸桥实例。

    is_turn_active: 桩函数，控制 _is_turn_active 的判定结果
    返回 (bridge, spawned, events, stopped)，后三者分别记录
    回合启动调用 / 桥事件推送 / 停止回合调用，便于断言。
    """
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
    # 内部保存含 @路径的完整文本（补发用），展示用 display_text
    assert queue[0]["text"] == "第一条 @d:/a.txt"
    assert queue[0]["display_text"] == "第一条"
    # 快照事件已推送，且展示文本不含 @路径
    assert events and events[-1][0] == "message_queue"
    assert events[-1][1]["session_name"] == "s1"
    assert events[-1][1]["queue"][0]["text"] == "第一条"
    # 没有直接启动新回合（不打断当前任务）
    assert spawned == []


def test_send_user_message_direct_when_idle():
    bridge, spawned, events, _ = _make_bridge(lambda s: False)

    res = bridge.send_user_message("你好")

    # 空闲时保持原行为：直接启动回合，不入队
    assert res == {"ok": True}
    assert len(spawned) == 1
    assert spawned[0][0] == ("你好", "s1")
    assert "s1" not in bridge._message_queues
    assert events == []


def test_send_user_message_binds_known_model(tmp_path, monkeypatch):
    """前端携带的模型 id 在配置中存在时，随消息写入会话绑定。"""
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


def test_send_user_message_ignores_unknown_model_binding(tmp_path, monkeypatch):
    """前端携带的模型 id 不在配置中（陈旧/已删除）时不得写入会话绑定。

    回归防线：显示态一旦被陈旧全局值污染，不能让下一条消息把错误
    绑定落成真实的会话模型。
    """
    _use_tmp_config(tmp_path, monkeypatch, dict(_ONE_MODEL_CONFIG))
    bridge, spawned, _, _ = _make_bridge(lambda s: False)
    bound = []
    bridge._memory = type(
        "M", (), {"set_session_model_id": lambda self, s, m: bound.append((s, m))}
    )()

    res = bridge.send_user_message("你好", None, None, None, "m-deleted")

    assert res == {"ok": True}
    assert bound == []
    # 回合照常启动，仅忽略非法绑定
    assert len(spawned) == 1


def test_drain_message_queue_spawns_head_fifo():
    bridge, spawned, events, _ = _make_bridge(lambda s: False)
    bridge._message_queues["s1"] = [
        {"id": "q1", "text": "第一条", "display_text": "第一条", "attachments": [], "queued_at": 1.0},
        {"id": "q2", "text": "第二条", "display_text": "第二条", "attachments": [], "queued_at": 2.0},
    ]

    bridge._drain_message_queue("s1")

    # FIFO：只补发队首一条，其余顺位保留
    assert [a[0][0] for a in spawned] == ["第一条"]
    assert [m["id"] for m in bridge._message_queues["s1"]] == ["q2"]
    # 补发后队列快照已更新
    assert events and events[-1][0] == "message_queue"
    assert [m["id"] for m in events[-1][1]["queue"]] == ["q2"]


def test_drain_skips_while_turn_active():
    """并发竞态：新回合已建立时剩余队列留给那个回合自己的 finally 处理。"""
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

    # 队列清空后键整体移除（get_message_queue 返回空列表而非残留键）
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
    # 移到队首：回合结束后 drain 优先补发它
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
    # 无活跃回合时无需停止
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
    # 快照只含前端展示字段：display_text（不含 @路径）+ 附件列表
    assert res["queue"] == [
        {"id": "q1", "text": "完整", "attachments": ["d:/a.txt"]},
    ]
