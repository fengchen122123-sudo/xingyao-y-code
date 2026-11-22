# -*- coding: utf-8 -*-
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_service as svc
from gui_bridge import ApiBridge

SCENES = ("router", "secretary", "search", "safety")
LABELS = {
    "router": "路由",
    "secretary": "秘书",
    "search": "搜索",
    "safety": "安全",
}


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


def _make_bridge():
    return ApiBridge.__new__(ApiBridge)


def _scene_cfg(scene):
    return svc.load_decision_config()["scenes"][scene]


def _rows_by_scene(bridge):
    result = bridge.get_decision_scenes()
    assert result["ok"] is True
    return {row["scene"]: row for row in result["scenes"]}


def test_get_decision_scenes_default_all_off(data_dir):
    bridge = _make_bridge()

    result = bridge.get_decision_scenes()

    assert result["ok"] is True
    assert [row["scene"] for row in result["scenes"]] == list(SCENES)
    for row in result["scenes"]:
        assert row["mode"] == "off"
        assert row["requested_mode"] == "off"
        assert row["enabled"] is False
        assert row["label"] == LABELS[row["scene"]]
        assert isinstance(row["description"], str) and row["description"]
        assert row["timeout_ms"] == 250
    assert result["shadow_mode"] is True
    assert result["enabled"] is False


def test_get_decision_scenes_default_thresholds(data_dir):
    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["threshold"] == pytest.approx(0.5)
    assert rows["secretary"]["threshold"] == pytest.approx(0.6)
    assert rows["search"]["threshold"] == pytest.approx(0.5)
    assert rows["safety"]["threshold"] == pytest.approx(0.9)


def test_get_decision_scenes_broken_config_falls_back_to_off(data_dir):
    (data_dir / "decision.json").write_text("{ 这不是 JSON", encoding="utf-8")

    result = _make_bridge().get_decision_scenes()

    assert result["ok"] is True
    assert [row["mode"] for row in result["scenes"]] == ["off"] * 4


@pytest.mark.parametrize(
    "mode, expect_enabled, expect_shadow",
    [
        ("shadow", True, True),
        ("live", True, False),
        ("off", False, True),
    ],
)
def test_set_decision_scene_writes_boolean_pair(data_dir, mode, expect_enabled, expect_shadow):
    bridge = _make_bridge()
    svc.save_decision_config({"scenes": {"router": {"enabled": False, "shadow": True}}})

    result = bridge.set_decision_scene("router", mode)

    assert result["ok"] is True
    assert result["requested_mode"] == mode
    scene = svc.load_decision_config()["scenes"]["router"]
    assert scene["enabled"] is expect_enabled
    assert scene["shadow"] is expect_shadow


def test_set_decision_scene_live_when_global_shadow_off_is_live(data_dir):
    bridge = _make_bridge()
    svc.save_decision_config({"shadow_mode": False})

    result = bridge.set_decision_scene("safety", "live")

    assert result["ok"] is True
    scene = svc.load_decision_config()["scenes"]["safety"]
    assert scene["enabled"] is True
    assert scene["shadow"] is False
    assert result["effective_mode"] == "live"
    assert result["global_shadow"] is False
    assert len(result["scenes"]) == 4
    assert result["scene"]["mode"] == "live"


def test_set_decision_scene_returns_fresh_snapshot(data_dir):
    bridge = _make_bridge()
    svc.save_decision_config({"shadow_mode": False})
    bridge.set_decision_scene("search", "shadow")

    result = bridge.set_decision_scene("secretary", "live")

    rows = {row["scene"]: row for row in result["scenes"]}
    assert rows["secretary"]["mode"] == "live"
    assert rows["search"]["mode"] == "shadow"
    assert rows["router"]["mode"] == "off"
    assert result["settings"]["scenes"]["secretary"]["enabled"] is True


def test_set_decision_scene_off_keeps_scene_shadow(data_dir):
    bridge = _make_bridge()
    svc.save_decision_config(
        {"shadow_mode": False, "scenes": {"router": {"enabled": True, "shadow": False}}}
    )

    result = bridge.set_decision_scene("router", "off")

    assert result["ok"] is True
    scene = svc.load_decision_config()["scenes"]["router"]
    assert scene["enabled"] is False
    assert scene["shadow"] is False
    assert result["effective_mode"] == "off"


def test_set_decision_scene_live_with_global_shadow_reports_shadow(data_dir):
    bridge = _make_bridge()
    cfg = svc.load_decision_config()
    assert cfg["shadow_mode"] is True

    result = bridge.set_decision_scene("router", "live")

    assert result["ok"] is True
    assert result["requested_mode"] == "live"
    scene = svc.load_decision_config()["scenes"]["router"]
    assert scene["enabled"] is True
    assert scene["shadow"] is False
    assert result["effective_mode"] == "shadow"
    assert result["global_shadow"] is True
    assert result["scene"]["mode"] == "shadow"
    assert result["scene"]["requested_mode"] == "live"
    assert "影子" in result["msg"]
    assert isinstance(result["hint"], str) and "影子" in result["hint"]


def test_get_decision_scenes_exposes_requested_vs_effective(data_dir):
    svc.save_decision_config(
        {"shadow_mode": True, "scenes": {"router": {"enabled": True, "shadow": False}}}
    )

    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["mode"] == "shadow"
    assert rows["router"]["requested_mode"] == "live"
    assert rows["router"]["enabled"] is True
    assert rows["router"]["shadow"] is False
    assert rows["router"]["global_shadow"] is True


def test_set_decision_scene_rejects_unknown_scene(data_dir):
    bridge = _make_bridge()

    result = bridge.set_decision_scene("nope", "live")

    assert result["ok"] is False
    assert "场景" in result["msg"]
    assert not (data_dir / "decision.json").exists()


def test_set_decision_scene_rejects_empty_scene(data_dir):
    result = _make_bridge().set_decision_scene("", "shadow")

    assert result["ok"] is False
    assert result["msg"]
    assert not (data_dir / "decision.json").exists()


def test_set_decision_scene_rejects_unknown_mode(data_dir):
    bridge = _make_bridge()

    result = bridge.set_decision_scene("router", "turbo")

    assert result["ok"] is False
    assert "模式" in result["msg"]
    assert not (data_dir / "decision.json").exists()


def test_set_decision_scene_accepts_uppercase_mode(data_dir):
    result = _make_bridge().set_decision_scene("router", "LIVE")

    assert result["ok"] is True
    assert svc.load_decision_config()["scenes"]["router"]["enabled"] is True


@pytest.mark.parametrize(
    "raw, expected",
    [(-0.2, 0.0), (1.8, 1.0), (0.0, 0.0), (1.0, 1.0), (0.75, 0.75)],
)
def test_set_decision_scene_clamps_threshold(data_dir, raw, expected):
    bridge = _make_bridge()

    result = bridge.set_decision_scene("safety", "shadow", raw)

    assert result["ok"] is True
    assert svc.load_decision_config()["scenes"]["safety"]["threshold"] == pytest.approx(expected)
    assert result["scene"]["threshold"] == pytest.approx(expected)


def test_set_decision_scene_rejects_non_numeric_threshold(data_dir):
    result = _make_bridge().set_decision_scene("router", "shadow", "很高")

    assert result["ok"] is False
    assert "阈值" in result["msg"]
    assert svc.load_decision_config()["scenes"]["router"]["threshold"] == pytest.approx(0.5)


def test_get_decision_scenes_clamps_corrupted_threshold(data_dir):
    svc.save_decision_config({"scenes": {"router": {"threshold": 0.5}}})
    path = data_dir / "decision.json"
    payload = path.read_text(encoding="utf-8").replace('"threshold": 0.5', '"threshold": 3.0', 1)
    path.write_text(payload, encoding="utf-8")

    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["threshold"] == pytest.approx(1.0)


def test_set_decision_scene_swallows_save_exception(data_dir, monkeypatch):
    def boom(patch):
        raise OSError("磁盘只读")

    monkeypatch.setattr(svc, "save_decision_config", boom)

    result = _make_bridge().set_decision_scene("router", "live")

    assert result["ok"] is False
    assert "磁盘只读" in result["msg"]
    assert isinstance(result["scenes"], list)


def test_set_decision_scene_swallows_generic_exception(data_dir, monkeypatch):
    def boom():
        raise RuntimeError("配置损坏")

    monkeypatch.setattr(svc, "load_decision_config", boom)
    monkeypatch.setattr(svc, "save_decision_config", lambda patch: {})

    result = _make_bridge().set_decision_scene("search", "shadow")

    assert result["ok"] is False
    assert result["scenes"] == []


def test_set_decision_scene_value_error_returns_message(data_dir, monkeypatch):

    def reject(patch):
        raise ValueError("scenes 必须是字典")

    monkeypatch.setattr(svc, "save_decision_config", reject)

    result = _make_bridge().set_decision_scene("router", "shadow")

    assert result["ok"] is False
    assert "scenes" in result["msg"]
    assert [row["scene"] for row in result["scenes"]] == list(SCENES)


def test_get_decision_scenes_swallows_exception(data_dir, monkeypatch):
    def boom():
        raise RuntimeError("读配置失败")

    monkeypatch.setattr(svc, "load_decision_config", boom)

    result = _make_bridge().get_decision_scenes()

    assert result["ok"] is False
    assert result["scenes"] == []
    assert "读配置失败" in result["msg"]


def test_existing_bridge_methods_untouched(data_dir):
    bridge = _make_bridge()

    saved = bridge.set_decision_settings({"enabled": True, "model_id": "NeoHorse-Jev-4B"})
    assert saved["ok"] is True
    assert saved["settings"]["enabled"] is True

    snapshot = bridge.get_decision_settings()
    assert snapshot["ok"] is True
    assert snapshot["settings"]["model_id"] == "NeoHorse-Jev-4B"
    assert "runtime" in snapshot

    assert [row["mode"] for row in bridge.get_decision_scenes()["scenes"]] == ["off"] * 4


SUMMARY_KEYS = {
    "ok",
    "today_count",
    "today_shadow_count",
    "today_adopted_count",
    "today_fallback_count",
    "recent",
    "has_business_records",
    "session_scoped",
}


def _fake_summary(has_business_records=True, session_scoped=False):
    return {
        "ok": True,
        "today_count": 2,
        "today_shadow_count": 1,
        "today_adopted_count": 1,
        "today_fallback_count": 0,
        "recent": [],
        "has_business_records": has_business_records,
        "session_scoped": session_scoped,
    }


def test_get_decision_audit_summary_passes_session_id(data_dir, monkeypatch):
    calls = []

    def fake(limit=20, session_id=None, session_only=False):
        calls.append((limit, session_id, session_only))
        return _fake_summary(True)

    monkeypatch.setattr(svc, "get_decision_audit_summary", fake)

    result = _make_bridge().get_decision_audit_summary(5, "sess-A")

    assert calls == [(5, "sess-A", False)]
    assert result["ok"] is True
    assert result["has_business_records"] is True
    assert SUMMARY_KEYS <= set(result)


def test_get_decision_audit_summary_passes_session_only(data_dir, monkeypatch):
    calls = []

    def fake(limit=20, session_id=None, session_only=False):
        calls.append((limit, session_id, session_only))
        return _fake_summary(True, session_scoped=True)

    monkeypatch.setattr(svc, "get_decision_audit_summary", fake)

    result = _make_bridge().get_decision_audit_summary(10, "sess-A", True)

    assert calls == [(10, "sess-A", True)]
    assert result["session_scoped"] is True


def test_get_decision_audit_summary_defaults_to_global(data_dir, monkeypatch):
    calls = []

    def fake(limit=20, session_id=None, session_only=False):
        calls.append((limit, session_id))
        return _fake_summary(True)

    monkeypatch.setattr(svc, "get_decision_audit_summary", fake)

    result = _make_bridge().get_decision_audit_summary()

    assert calls == [(20, None)]
    assert result["ok"] is True
    assert SUMMARY_KEYS <= set(result)
    assert result["today_count"] == 2
    assert result["recent"] == []


def test_get_decision_audit_summary_non_dict_result_falls_back(data_dir, monkeypatch):
    monkeypatch.setattr(
        svc,
        "get_decision_audit_summary",
        lambda limit=20, session_id=None, session_only=False: [],
    )

    result = _make_bridge().get_decision_audit_summary(3, "sess-A")

    assert result["ok"] is False
    assert result["msg"] == "汇总返回异常"


def test_get_decision_audit_summary_session_scope_end_to_end(data_dir):
    audit = data_dir / "decision_audit"
    audit.mkdir()
    (audit / f"{svc._today()}.jsonl").write_text(
        json.dumps(
            {"ts": "2026-10-18T10:00:00", "scene": "router", "shadow": False,
             "session_id": "别的会话"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    bridge = _make_bridge()

    scoped = bridge.get_decision_audit_summary(10, "本会话")
    assert scoped["ok"] is True
    assert scoped["has_business_records"] is False

    glob = bridge.get_decision_audit_summary(10)
    assert glob["ok"] is True
    assert glob["has_business_records"] is True
