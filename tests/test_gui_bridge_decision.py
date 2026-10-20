# -*- coding: utf-8 -*-
"""JEV 设置页「业务场景」三态桥测试（gui_bridge.get_decision_scenes / set_decision_scene）。

三态不新增 schema 字段，用两个布尔组合表达（与 core.decision_gateway 口径一致）：
- off    = scenes.X.enabled=false
- shadow = scenes.X.enabled=true 且（全局 shadow_mode=true 或 scenes.X.shadow=true）
- live   = scenes.X.enabled=true 且 全局 shadow_mode=false 且 scenes.X.shadow=false

隔离策略：``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；
本文件再把 ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰
真实 ``~/.ycode``，也不发任何网络请求（这两个桥方法只读写配置）。
"""
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


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """每个用例独占一份 ~/.ycode（config.YCODE_DIR 重定向到 tmp_path）。"""
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


def _make_bridge():
    """裸桥实例：两个场景桥方法只委托 decision_service，不依赖桥内部状态。"""
    return ApiBridge.__new__(ApiBridge)


def _scene_cfg(scene):
    return svc.load_decision_config()["scenes"][scene]


def _rows_by_scene(bridge):
    result = bridge.get_decision_scenes()
    assert result["ok"] is True
    return {row["scene"]: row for row in result["scenes"]}


# ---------------------------------------------------------------------------
# 1. get_decision_scenes：默认配置下四个场景全部 off
# ---------------------------------------------------------------------------

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
    assert result["shadow_mode"] is True  # 默认全局影子开
    assert result["enabled"] is False


def test_get_decision_scenes_default_thresholds(data_dir):
    """每个场景的阈值取自 DEFAULT_CONFIG（router 0.5 / secretary 0.6 / search 0.5 / safety 0.9）。"""
    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["threshold"] == pytest.approx(0.5)
    assert rows["secretary"]["threshold"] == pytest.approx(0.6)
    assert rows["search"]["threshold"] == pytest.approx(0.5)
    assert rows["safety"]["threshold"] == pytest.approx(0.9)


def test_get_decision_scenes_broken_config_falls_back_to_off(data_dir):
    """decision.json 损坏时回落默认值：四个场景仍全部 off，不抛错。"""
    (data_dir / "decision.json").write_text("{ 这不是 JSON", encoding="utf-8")

    result = _make_bridge().get_decision_scenes()

    assert result["ok"] is True
    assert [row["mode"] for row in result["scenes"]] == ["off"] * 4


# ---------------------------------------------------------------------------
# 2. set_decision_scene：三种 mode 写入后两个布尔与预期完全一致
# ---------------------------------------------------------------------------

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
    # 统一基线：场景关闭但自身 shadow=true（off 分支不改 shadow，便于断言"完全一致"）
    svc.save_decision_config({"scenes": {"router": {"enabled": False, "shadow": True}}})

    result = bridge.set_decision_scene("router", mode)

    assert result["ok"] is True
    assert result["requested_mode"] == mode
    # 全局 shadow_mode 默认仍为 true：live 会被折算成 shadow（口径如此）
    scene = svc.load_decision_config()["scenes"]["router"]
    assert scene["enabled"] is expect_enabled
    assert scene["shadow"] is expect_shadow


def test_set_decision_scene_live_when_global_shadow_off_is_live(data_dir):
    """关掉全局影子后切 live：配置为 (enabled=True, shadow=False) 且 effective_mode=live。"""
    bridge = _make_bridge()
    svc.save_decision_config({"shadow_mode": False})

    result = bridge.set_decision_scene("safety", "live")

    assert result["ok"] is True
    scene = svc.load_decision_config()["scenes"]["safety"]
    assert scene["enabled"] is True
    assert scene["shadow"] is False
    assert result["effective_mode"] == "live"
    assert result["global_shadow"] is False
    # 返回里带最新四场景快照
    assert len(result["scenes"]) == 4
    assert result["scene"]["mode"] == "live"


def test_set_decision_scene_returns_fresh_snapshot(data_dir):
    """写完后重新读取配置返回最新快照：其他场景的状态一并回显。"""
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
    """off 只写 enabled=false，保留场景自身 shadow 档位（再次开启时语义不变）。"""
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


# ---------------------------------------------------------------------------
# 3. 全局 shadow_mode=true 时切 live：effective_mode 仍是 shadow（口径正确）
# ---------------------------------------------------------------------------

def test_set_decision_scene_live_with_global_shadow_reports_shadow(data_dir):
    bridge = _make_bridge()
    cfg = svc.load_decision_config()
    assert cfg["shadow_mode"] is True  # 默认全局影子开

    result = bridge.set_decision_scene("router", "live")

    assert result["ok"] is True
    assert result["requested_mode"] == "live"
    # 两个布尔已按 live 写入……
    scene = svc.load_decision_config()["scenes"]["router"]
    assert scene["enabled"] is True
    assert scene["shadow"] is False
    # ……但全局影子压着，真实生效仍是 shadow，并给出提示文本
    assert result["effective_mode"] == "shadow"
    assert result["global_shadow"] is True
    assert result["scene"]["mode"] == "shadow"
    assert result["scene"]["requested_mode"] == "live"
    assert "影子" in result["msg"]
    assert isinstance(result["hint"], str) and "影子" in result["hint"]


def test_get_decision_scenes_exposes_requested_vs_effective(data_dir):
    """全局影子开着 + 场景已按 live 落盘：mode=shadow 而 requested_mode=live（前端据此提示）。"""
    svc.save_decision_config(
        {"shadow_mode": True, "scenes": {"router": {"enabled": True, "shadow": False}}}
    )

    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["mode"] == "shadow"
    assert rows["router"]["requested_mode"] == "live"
    assert rows["router"]["enabled"] is True
    assert rows["router"]["shadow"] is False
    assert rows["router"]["global_shadow"] is True


# ---------------------------------------------------------------------------
# 4. 非法输入：scene / mode / threshold 越界
# ---------------------------------------------------------------------------

def test_set_decision_scene_rejects_unknown_scene(data_dir):
    bridge = _make_bridge()

    result = bridge.set_decision_scene("nope", "live")

    assert result["ok"] is False
    assert "场景" in result["msg"]  # 中文原因
    # 未写盘
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
    """模式大小写容错：LIVE 与 live 等价（前端固定传小写）。"""
    result = _make_bridge().set_decision_scene("router", "LIVE")

    assert result["ok"] is True
    assert svc.load_decision_config()["scenes"]["router"]["enabled"] is True


@pytest.mark.parametrize(
    "raw, expected",
    [(-0.2, 0.0), (1.8, 1.0), (0.0, 0.0), (1.0, 1.0), (0.75, 0.75)],
)
def test_set_decision_scene_clamps_threshold(data_dir, raw, expected):
    """越界阈值钳制到 0.0~1.0（不报错、不写非法值）。"""
    bridge = _make_bridge()

    result = bridge.set_decision_scene("safety", "shadow", raw)

    assert result["ok"] is True
    assert svc.load_decision_config()["scenes"]["safety"]["threshold"] == pytest.approx(expected)
    assert result["scene"]["threshold"] == pytest.approx(expected)


def test_set_decision_scene_rejects_non_numeric_threshold(data_dir):
    result = _make_bridge().set_decision_scene("router", "shadow", "很高")

    assert result["ok"] is False
    assert "阈值" in result["msg"]
    # 非法阈值不落盘（保持默认 0.5）
    assert svc.load_decision_config()["scenes"]["router"]["threshold"] == pytest.approx(0.5)


def test_get_decision_scenes_clamps_corrupted_threshold(data_dir):
    """配置里被手改成越界阈值时，桥层展示仍钳制在 0~1（界面不会出现非法值）。"""
    svc.save_decision_config({"scenes": {"router": {"threshold": 0.5}}})
    path = data_dir / "decision.json"
    payload = path.read_text(encoding="utf-8").replace('"threshold": 0.5', '"threshold": 3.0', 1)
    path.write_text(payload, encoding="utf-8")

    rows = _rows_by_scene(_make_bridge())

    assert rows["router"]["threshold"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 5. 写配置异常：返回 ok=false 且不抛
# ---------------------------------------------------------------------------

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
    """save_decision_config 抛 ValueError（校验拒绝）时转成 ok=false + 中文原因。"""

    def reject(patch):
        raise ValueError("scenes 必须是字典")

    monkeypatch.setattr(svc, "save_decision_config", reject)

    result = _make_bridge().set_decision_scene("router", "shadow")

    assert result["ok"] is False
    assert "scenes" in result["msg"]
    # 失败时也回显当前（未改动）的四场景快照，界面不会空掉
    assert [row["scene"] for row in result["scenes"]] == list(SCENES)


def test_get_decision_scenes_swallows_exception(data_dir, monkeypatch):
    def boom():
        raise RuntimeError("读配置失败")

    monkeypatch.setattr(svc, "load_decision_config", boom)

    result = _make_bridge().get_decision_scenes()

    assert result["ok"] is False
    assert result["scenes"] == []
    assert "读配置失败" in result["msg"]


# ---------------------------------------------------------------------------
# 6. 不回归：现有五个桥方法行为不变（三态桥不接管它们）
# ---------------------------------------------------------------------------

def test_existing_bridge_methods_untouched(data_dir):
    bridge = _make_bridge()

    saved = bridge.set_decision_settings({"enabled": True, "model_id": "NeoHorse-Jev-4B"})
    assert saved["ok"] is True
    assert saved["settings"]["enabled"] is True

    snapshot = bridge.get_decision_settings()
    assert snapshot["ok"] is True
    assert snapshot["settings"]["model_id"] == "NeoHorse-Jev-4B"
    assert "runtime" in snapshot

    # 现有保存不会写 scenes：四个场景保持默认全关
    assert [row["mode"] for row in bridge.get_decision_scenes()["scenes"]] == ["off"] * 4


# ---------------------------------------------------------------------------
# 7. get_decision_audit_summary：session_id 透传到 decision_service
# ---------------------------------------------------------------------------

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
    """桥层把 session_id 原样透传给 decision_service（头部决策入口按会话判定）。"""
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
    """session_only=True 必须透传到下游：否则面板拿到全局 recent（含别会话记录）。"""
    calls = []

    def fake(limit=20, session_id=None, session_only=False):
        calls.append((limit, session_id, session_only))
        return _fake_summary(True, session_scoped=True)

    monkeypatch.setattr(svc, "get_decision_audit_summary", fake)

    result = _make_bridge().get_decision_audit_summary(10, "sess-A", True)

    assert calls == [(10, "sess-A", True)]
    assert result["session_scoped"] is True


def test_get_decision_audit_summary_defaults_to_global(data_dir, monkeypatch):
    """不传 session_id 时下游仍然只拿到 limit（session_id 有效值为 None），返回结构不变。"""
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
    """底层返回非字典时仍走既有兜底：ok=false + 中文原因，不抛错。"""
    monkeypatch.setattr(
        svc,
        "get_decision_audit_summary",
        lambda limit=20, session_id=None, session_only=False: [],
    )

    result = _make_bridge().get_decision_audit_summary(3, "sess-A")

    assert result["ok"] is False
    assert result["msg"] == "汇总返回异常"


def test_get_decision_audit_summary_session_scope_end_to_end(data_dir):
    """真链路：别的会话做过业务决策时，本会话 has_business_records 仍为 False。"""
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

    # 全局口径（不传 session_id）：任何会话的业务记录都算
    glob = bridge.get_decision_audit_summary(10)
    assert glob["ok"] is True
    assert glob["has_business_records"] is True

