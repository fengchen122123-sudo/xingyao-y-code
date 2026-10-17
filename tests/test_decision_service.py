# -*- coding: utf-8 -*-
"""JEV 决策服务核心测试：配置 / Key 策略 / 影子 / 审计 / 探针（全部 mock HTTP）。

隔离策略：``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；
本文件再把 ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰
真实 ~/.ycode。HTTP 层统一 monkeypatch ``decision_service._post`` / ``_get``，
不真实联网（对齐设计文档第 9 节 1~6）。
"""
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_service as svc
from gui_bridge import ApiBridge

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one\nsk_key_two",
}

NOUL_Q = {"q": {"type": "noul", "instructions": "该命题是否成立？"}}


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable(data_dir, scene="router", shadow=False, **extra):
    patch = {
        "enabled": True,
        "shadow_mode": shadow,
        "scenes": {scene: {"enabled": True, "shadow": shadow}},
    }
    patch.update(extra)
    return svc.save_decision_config(patch)


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds}
        )
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_get(monkeypatch, handler):
    calls = []

    def fake(url, timeout_seconds):
        calls.append({"url": url, "timeout": timeout_seconds})
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_get", fake)
    return calls


def _audit_records(data_dir):
    records = []
    directory = data_dir / "decision_audit"
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def _last_audit(data_dir):
    records = _audit_records(data_dir)
    assert records, "审计未落盘"
    return records[-1]


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


# ---------------------------------------------------------------------------
# 1. 配置读写：默认值 / 非法值拒绝 / 损坏回落 / 原子写
# ---------------------------------------------------------------------------

def test_default_config_full_table(data_dir):
    cfg = svc.load_decision_config()
    assert cfg["enabled"] is False
    assert cfg["provider_ref"] == "auto"
    assert cfg["base_url"] == ""
    assert cfg["model_id"] == "NeoHorse-Jev-4B"
    assert cfg["endpoint_style"] == "decision"
    assert cfg["key_mode"] == "auto"
    assert cfg["shadow_mode"] is True
    assert cfg["timeout_ms"] == 800
    assert cfg["audit_enabled"] is True
    assert set(cfg["scenes"]) == {"router", "secretary", "search", "safety"}
    for scene in cfg["scenes"].values():
        assert scene["enabled"] is False
        assert scene["shadow"] is True
        assert isinstance(scene["threshold"], float)


def test_load_returns_isolated_copy(data_dir):
    cfg = svc.load_decision_config()
    cfg["enabled"] = True
    cfg["scenes"]["router"]["enabled"] = True
    assert svc.DEFAULT_CONFIG["enabled"] is False
    assert svc.DEFAULT_CONFIG["scenes"]["router"]["enabled"] is False
    assert svc.load_decision_config()["enabled"] is False


def test_save_rejects_invalid_values(data_dir):
    with pytest.raises(ValueError):
        svc.save_decision_config({"enabled": True, "model_id": "   "})
    with pytest.raises(ValueError):
        svc.save_decision_config({"timeout_ms": 99})
    with pytest.raises(ValueError):
        svc.save_decision_config({"timeout_ms": 5001})
    with pytest.raises(ValueError):
        svc.save_decision_config({"timeout_ms": "800"})
    with pytest.raises(ValueError):
        svc.save_decision_config({"endpoint_style": "chat"})
    with pytest.raises(ValueError):
        svc.save_decision_config({"key_mode": "index:-1"})
    assert not (data_dir / "decision.json").exists()  # 非法值拒绝保存、不落盘


def test_save_valid_config_roundtrip_with_atomic_write(data_dir):
    saved = svc.save_decision_config({"enabled": True, "timeout_ms": 900})
    assert saved["enabled"] is True
    assert saved["timeout_ms"] == 900
    raw = json.loads((data_dir / "decision.json").read_text(encoding="utf-8"))
    assert raw["enabled"] is True
    assert raw["model_id"] == "NeoHorse-Jev-4B"
    assert svc.load_decision_config()["timeout_ms"] == 900
    assert list(data_dir.glob("*.tmp")) == []  # 原子写不残留临时文件


def test_load_falls_back_when_file_corrupted(data_dir):
    (data_dir / "decision.json").write_text("{ 这不是 JSON", encoding="utf-8")
    cfg = svc.load_decision_config()
    assert cfg["enabled"] is False
    assert cfg["model_id"] == "NeoHorse-Jev-4B"
    (data_dir / "decision.json").write_text("[1, 2]", encoding="utf-8")
    assert svc.load_decision_config()["enabled"] is False


# ---------------------------------------------------------------------------
# 2. ask_decision：请求体形状 / 题数限制 / 开关零请求
# ---------------------------------------------------------------------------

def test_ask_request_shape_and_result(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 1.0}}})
    )
    result = svc.ask_decision("上下文", NOUL_Q, scene="router")
    assert result is not None
    assert result["answers"] == {"q": {"noul": 1.0}}
    assert isinstance(result["trace_id"], str) and result["trace_id"]
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://tokenrhythm.studio/v1/decision"
    assert call["payload"] == {
        "model": "NeoHorse-Jev-4B",
        "state": "上下文",
        "questions": NOUL_Q,
    }
    assert call["headers"]["Authorization"] == "Bearer sk_key_one"
    assert call["timeout"] == pytest.approx(0.8)


def test_ask_packs_multiple_questions_into_one_call(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    questions = {
        f"q{i}": {"type": "choice", "instructions": "选一个", "criteria": ["a", "b"]}
        for i in range(16)
    }
    assert svc.ask_decision("s", questions, scene="router") is not None
    assert len(calls) == 1
    assert calls[0]["payload"]["questions"] == questions


def test_ask_rejects_more_than_16_questions(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200))
    questions = {f"q{i}": {"type": "noul", "instructions": "?"} for i in range(17)}
    with pytest.raises(ValueError):
        svc.ask_decision("s", questions, scene="router")
    assert calls == []


def test_ask_image_goes_top_level_and_limits_single_question(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    image = "data:image/png;base64,AAAA"
    result = svc.ask_decision("s", NOUL_Q, scene="router", image_b64=image)
    assert result is not None
    assert calls[0]["payload"]["image"] == image
    two = {
        "q1": {"type": "noul", "instructions": "?"},
        "q2": {"type": "noul", "instructions": "?"},
    }
    with pytest.raises(ValueError):
        svc.ask_decision("s", two, scene="router", image_b64=image)
    assert len(calls) == 1


def test_disabled_makes_zero_requests(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert calls == []


def test_scene_disabled_makes_zero_requests(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, scene="router")
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200))
    assert svc.ask_decision("s", NOUL_Q, scene="safety") is None
    assert calls == []


def test_missing_provider_audits_no_key(data_dir, monkeypatch):
    _write_user_config(
        data_dir,
        [{"id": "x", "name": "别的厂商", "base_url": "https://api.other.com/v1", "api_key": "sk_x"}],
    )
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert calls == []
    assert _last_audit(data_dir)["fallback_reason"] == "no_key"


# ---------------------------------------------------------------------------
# 3. Key 策略：403 轮换与记忆 / 401 / 429 冷却 / 超时 / 全失败
# ---------------------------------------------------------------------------

def test_403_switches_key_then_success_and_remembers(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)

    def handler(index, call):
        if index == 0:
            return _resp(403, {"code": "MODEL_ACCESS_DENIED"})
        return _resp(200, {"answers": {"q": {"noul": 1.0}}})

    calls = _mock_post(monkeypatch, handler)
    assert svc.ask_decision("s", NOUL_Q, scene="router") is not None
    assert [c["headers"]["Authorization"] for c in calls] == [
        "Bearer sk_key_one",
        "Bearer sk_key_two",
    ]
    # 记忆可用 Key：下一次粘性走 Key#2，不再碰 403 的 Key#1
    assert svc.ask_decision("s", NOUL_Q, scene="router") is not None
    assert len(calls) == 3
    assert calls[2]["headers"]["Authorization"] == "Bearer sk_key_two"
    snap = svc.get_decision_settings_snapshot()
    assert snap["runtime"]["active_key_index"] == 1


def test_all_keys_403_reports_no_permission(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(403))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 2
    assert _last_audit(data_dir)["fallback_reason"] == "no_permission"
    # 拒绝已记忆：下一次零请求直接回落
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 2


def test_all_keys_401_reports_unauthorized(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(401))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 2
    assert _last_audit(data_dir)["fallback_reason"] == "unauthorized"


def test_429_switches_to_next_key(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)

    def handler(index, call):
        if call["headers"]["Authorization"] == "Bearer sk_key_one":
            return _resp(429)
        return _resp(200, {"answers": {"q": {"noul": 1.0}}})

    calls = _mock_post(monkeypatch, handler)
    assert svc.ask_decision("s", NOUL_Q, scene="router") is not None
    assert len(calls) == 2
    assert calls[1]["headers"]["Authorization"] == "Bearer sk_key_two"


def test_429_all_keys_cools_down_then_recovers(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    clock = {"now": 1000.0}
    monkeypatch.setattr(svc, "_now", lambda: clock["now"])
    all_429 = _mock_post(monkeypatch, lambda i, call: _resp(429))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(all_429) == 2
    assert _last_audit(data_dir)["fallback_reason"] == "rate_limited"
    # 冷却期内（60s）：零请求直接回落
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(all_429) == 2
    # 冷却期过后恢复尝试
    clock["now"] += 61.0
    ok_calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is not None
    assert len(ok_calls) == 1


def test_timeout_returns_none_without_retry(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(headers["Authorization"])
        raise httpx.TimeoutException("slow")

    monkeypatch.setattr(svc, "_post", fake)
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 1  # 超时不重试、不换 Key
    assert _last_audit(data_dir)["fallback_reason"] == "upstream_error"


def test_network_error_and_5xx_return_none_without_retry(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir)
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(headers["Authorization"])
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(svc, "_post", fake)
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 1
    assert _last_audit(data_dir)["fallback_reason"] == "upstream_error"

    calls2 = _mock_post(monkeypatch, lambda i, call: _resp(502))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls2) == 1  # 5xx 不换 Key、不重试
    assert _last_audit(data_dir)["fallback_reason"] == "upstream_error"


def test_key_mode_fixed_index_uses_only_that_key(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, key_mode="index:1")
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is not None
    assert calls[0]["headers"]["Authorization"] == "Bearer sk_key_two"


# ---------------------------------------------------------------------------
# 4. 影子与开关
# ---------------------------------------------------------------------------

def test_shadow_sends_request_writes_audit_and_returns_none(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=True)
    calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 1.0}}})
    )
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 1  # 影子仍发请求
    record = _last_audit(data_dir)
    assert record["shadow"] is True
    assert record["answers"] == {"q": {"noul": 1.0}}
    assert record["fallback_reason"] is None


def test_global_shadow_mode_overrides_scene_shadow_false(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=False)
    svc.save_decision_config({"shadow_mode": True})
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    assert svc.ask_decision("s", NOUL_Q, scene="router") is None
    assert len(calls) == 1
    assert _last_audit(data_dir)["shadow"] is True


def test_non_shadow_returns_answers(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.9}}}))
    result = svc.ask_decision("s", NOUL_Q, scene="router")
    assert result is not None
    assert result["answers"]["q"]["noul"] == 0.9


# ---------------------------------------------------------------------------
# 5. 审计：字段完整 / 按天切分 / 不含 Key
# ---------------------------------------------------------------------------

AUDIT_FIELDS = [
    "ts",
    "scene",
    "shadow",
    "model_id",
    "key_hint",
    "state_digest",
    "questions_digest",
    "answers",
    "latency_ms",
    "adopted",
    "fallback_reason",
    "trace_id",
]


def test_audit_record_fields_and_no_key_leak(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=True)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 1.0}}}))
    svc.ask_decision("很长的上下文" * 30, NOUL_Q, scene="router")
    record = _last_audit(data_dir)
    for field in AUDIT_FIELDS:
        assert field in record, field
    assert record["scene"] == "router"
    assert record["model_id"] == "NeoHorse-Jev-4B"
    assert record["key_hint"] == "key#0"
    assert isinstance(record["latency_ms"], int)
    assert isinstance(record["state_digest"], str) and len(record["state_digest"]) <= 80
    assert isinstance(record["questions_digest"], str) and record["questions_digest"]
    assert record["adopted"] is False
    assert record["trace_id"]
    content = "".join(
        path.read_text(encoding="utf-8")
        for path in (data_dir / "decision_audit").glob("*.jsonl")
    )
    assert "sk_key_one" not in content
    assert "sk_key_two" not in content
    for line in content.splitlines():
        assert len(line.encode("utf-8")) < 2048  # 单条 <2KB


def test_audit_splits_by_day(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=True)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    days = iter(["20261015", "20261016"])
    monkeypatch.setattr(svc, "_today", lambda: next(days))
    svc.ask_decision("s", NOUL_Q, scene="router")
    svc.ask_decision("s", NOUL_Q, scene="router")
    names = sorted(path.name for path in (data_dir / "decision_audit").glob("*.jsonl"))
    assert names == ["20261015.jsonl", "20261016.jsonl"]


def test_mark_adopted_updates_record(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable(data_dir, shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))
    result = svc.ask_decision("s", NOUL_Q, scene="router")
    assert result is not None
    svc.mark_adopted(result["trace_id"])
    assert _last_audit(data_dir)["adopted"] is True
    svc.mark_adopted("不存在的trace")  # 不存在时静默无副作用


# ---------------------------------------------------------------------------
# 6. 探针分类：200 / 403 / 401 / 超时 / 网络失败
# ---------------------------------------------------------------------------

def test_probe_classifies_each_key_once(data_dir, monkeypatch):
    provider = dict(PROVIDER, api_key="k0\nk1\nk2\nk3\nk4")
    _write_user_config(data_dir, [provider])
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append({"headers": headers, "payload": payload})
        key = headers["Authorization"].split(" ", 1)[1]
        if key == "k0":
            return _resp(200, {"answers": {"probe": {"noul": 1.0}}})
        if key == "k1":
            return _resp(403)
        if key == "k2":
            return _resp(401)
        if key == "k3":
            raise httpx.TimeoutException("slow")
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(svc, "_post", fake)
    result = svc.probe_decision()
    assert result["ok"] is True
    assert result["model_id"] == "NeoHorse-Jev-4B"
    statuses = [item["status"] for item in result["results"]]
    assert statuses == [
        "available",
        "no_permission",
        "invalid_key",
        "timeout",
        "network_error",
    ]
    assert [item["key_index"] for item in result["results"]] == [0, 1, 2, 3, 4]
    assert result["results"][0]["latency_ms"] is not None
    assert result["results"][0]["error"] == ""
    assert len(calls) == 5  # 每把 Key 至多 1 次
    assert calls[0]["payload"]["questions"]["probe"]["type"] == "noul"
    assert calls[0]["payload"]["model"] == "NeoHorse-Jev-4B"
    # 探针结果进入运行时快照，可用 Key 被记忆
    snap = svc.get_decision_settings_snapshot()
    assert snap["runtime"]["last_probe"]["results"][0]["status"] == "available"
    assert snap["runtime"]["active_key_index"] == 0
    assert 1 in snap["runtime"]["denied_key_indexes"]


def test_probe_without_keys_reports_message(data_dir, monkeypatch):
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200))
    result = svc.probe_decision()
    assert result["results"] == []
    assert result["ok"] is False
    assert result["msg"]
    assert calls == []


# ---------------------------------------------------------------------------
# 7. 上游模型列表：过滤 type=="decision" / 失败路径
# ---------------------------------------------------------------------------

def test_list_decision_models_filters_decision_type(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    payload = {
        "data": [
            {
                "id": "NeoHorse-Jev-4B",
                "name": "Jev 决策",
                "type": "decision",
                "contextWindow": 33000,
                "effectiveInputPrice": 0,
                "effectiveOutputPrice": 0,
            },
            {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek",
                "type": "chat",
                "contextWindow": 100000,
            },
        ]
    }
    calls = _mock_get(monkeypatch, lambda i, call: _resp(200, payload))
    result = svc.list_decision_models()
    assert result["ok"] is True
    assert [item["id"] for item in result["models"]] == ["NeoHorse-Jev-4B"]
    model = result["models"][0]
    assert model["name"] == "Jev 决策"
    assert model["context"] == 33000
    assert isinstance(model["price_text"], str)
    assert calls[0]["url"] == "https://tokenrhythm.studio/api/models"


def test_list_decision_models_failure_paths(data_dir, monkeypatch):
    result = svc.list_decision_models()  # 无 provider
    assert result["ok"] is False
    assert result["models"] == []
    assert result["msg"]

    _write_user_config(data_dir, [PROVIDER])

    def boom(url, timeout_seconds):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(svc, "_get", boom)
    result = svc.list_decision_models()
    assert result["ok"] is False
    assert result["msg"]

    monkeypatch.setattr(svc, "_get", lambda url, timeout_seconds: _resp(500))
    result = svc.list_decision_models()
    assert result["ok"] is False
    assert "500" in result["msg"]


# ---------------------------------------------------------------------------
# 8. 桥层（gui_bridge.ApiBridge）：校验 / 委托 / 审计尾部
# ---------------------------------------------------------------------------

def _make_bridge():
    """裸桥实例：五个决策桥方法只委托 decision_service，不依赖桥内部状态。"""
    return ApiBridge.__new__(ApiBridge)


def _has_chinese(text):
    """断言文本含中文字符（错误原因须为中文）。"""
    return any("一" <= ch <= "鿿" for ch in text)


def test_bridge_set_decision_settings_rejects_invalid_without_writing(data_dir):
    bridge = _make_bridge()

    result = bridge.set_decision_settings({"timeout_ms": 99999})
    assert result["ok"] is False
    assert "timeout_ms" in result["msg"]
    assert _has_chinese(result["msg"])
    assert not (data_dir / "decision.json").exists()

    # 显式清空 model_id：默认值本就非空，只有置空才能触发"启用时必须给模型"的校验
    result = bridge.set_decision_settings({"enabled": True, "model_id": ""})
    assert result["ok"] is False
    assert "model_id" in result["msg"]
    assert not (data_dir / "decision.json").exists()


def test_bridge_set_decision_settings_persists_and_reads_back(data_dir):
    bridge = _make_bridge()
    result = bridge.set_decision_settings(
        {"enabled": True, "model_id": "NeoHorse-Jev-4B", "timeout_ms": 800}
    )
    assert result["ok"] is True
    assert result["msg"]
    assert result["settings"]["enabled"] is True
    assert result["settings"]["model_id"] == "NeoHorse-Jev-4B"
    assert (data_dir / "decision.json").exists()
    assert svc.load_decision_config()["model_id"] == "NeoHorse-Jev-4B"


def test_bridge_set_decision_settings_passes_patch_through(data_dir, monkeypatch):
    seen = {}

    def fake_save(patch):
        seen["patch"] = patch
        return {"enabled": True, "model_id": "m"}

    monkeypatch.setattr(svc, "save_decision_config", fake_save)
    patch = {"enabled": True, "model_id": "m"}
    result = _make_bridge().set_decision_settings(patch)
    assert seen["patch"] is patch
    assert result["ok"] is True
    assert result["settings"] == {"enabled": True, "model_id": "m"}


def test_bridge_get_decision_settings_delegates(data_dir, monkeypatch):
    calls = []

    def fake_snapshot():
        calls.append(True)
        return {
            "config": {"enabled": False},
            "runtime": {"key_count": 2, "active_key_index": None},
        }

    monkeypatch.setattr(svc, "get_decision_settings_snapshot", fake_snapshot)
    result = _make_bridge().get_decision_settings()
    assert calls == [True]
    assert result["ok"] is True
    assert result["settings"] == {"enabled": False}
    assert result["runtime"] == {"key_count": 2, "active_key_index": None}


def test_bridge_probe_decision_delegates(data_dir, monkeypatch):
    calls = []

    def fake_probe():
        calls.append(True)
        return {
            "ok": True,
            "results": [
                {"key_index": 0, "status": "available", "latency_ms": 12, "error": ""}
            ],
            "model_id": "NeoHorse-Jev-4B",
            "msg": "",
        }

    monkeypatch.setattr(svc, "probe_decision", fake_probe)
    result = _make_bridge().probe_decision()
    assert calls == [True]
    assert result["ok"] is True
    assert result["model_id"] == "NeoHorse-Jev-4B"
    assert result["results"][0]["status"] == "available"


def test_bridge_list_decision_models_delegates(data_dir, monkeypatch):
    monkeypatch.setattr(
        svc,
        "list_decision_models",
        lambda: {"ok": True, "models": [{"id": "m1"}], "msg": ""},
    )
    result = _make_bridge().list_decision_models()
    assert result["ok"] is True
    assert result["models"] == [{"id": "m1"}]

    monkeypatch.setattr(
        svc,
        "list_decision_models",
        lambda: {"ok": False, "models": [], "msg": "拉取失败"},
    )
    result = _make_bridge().list_decision_models()
    assert result["ok"] is False
    assert result["msg"] == "拉取失败"
    assert result["models"] == []


def test_bridge_get_decision_audit_tail_missing_dir_returns_empty(data_dir):
    result = _make_bridge().get_decision_audit_tail()
    assert result == {"ok": True, "lines": []}


def test_bridge_get_decision_audit_tail_reads_latest_file_tail(data_dir, monkeypatch):
    directory = data_dir / "decision_audit"
    directory.mkdir()
    (directory / "20261014.jsonl").write_text('{"old": true}\n', encoding="utf-8")
    (directory / "20261015.jsonl").write_text('{"a": 1}\n{"b": 2}\n{"c": 3}\n', encoding="utf-8")
    delegated = []
    real_audit_dir = svc._audit_dir

    def fake_audit_dir():
        delegated.append(True)
        return real_audit_dir()

    monkeypatch.setattr(svc, "_audit_dir", fake_audit_dir)
    result = _make_bridge().get_decision_audit_tail(limit=2)
    assert delegated == [True]
    assert result["ok"] is True
    assert result["lines"] == ['{"b": 2}', '{"c": 3}']


def test_bridge_get_decision_audit_tail_default_limit(data_dir):
    directory = data_dir / "decision_audit"
    directory.mkdir()
    rows = [json.dumps({"i": i}) for i in range(25)]
    (directory / "20261015.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")
    result = _make_bridge().get_decision_audit_tail()
    assert result["ok"] is True
    assert len(result["lines"]) == 20
    assert result["lines"][-1] == '{"i": 24}'


def test_bridge_decision_methods_never_raise(data_dir, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("模拟内部错误")

    monkeypatch.setattr(svc, "get_decision_settings_snapshot", boom)
    monkeypatch.setattr(svc, "save_decision_config", boom)
    monkeypatch.setattr(svc, "probe_decision", boom)
    monkeypatch.setattr(svc, "list_decision_models", boom)
    monkeypatch.setattr(svc, "_audit_dir", boom)

    bridge = _make_bridge()
    for result in (
        bridge.get_decision_settings(),
        bridge.set_decision_settings({"enabled": False}),
        bridge.probe_decision(),
        bridge.list_decision_models(),
        bridge.get_decision_audit_tail(),
    ):
        assert result["ok"] is False
        assert "模拟内部错误" in result["msg"]
    assert bridge.get_decision_audit_tail()["lines"] == []


def test_list_decision_providers_lists_channels_without_key_text(data_dir, monkeypatch):
    """通道列表：报 Key 数量但不含 Key 内容；tokenrhythm 通道标记 auto_match。"""
    monkeypatch.setattr(
        svc,
        "_load_user_config",
        lambda: {
            "providers": [
                {
                    "id": "prov-token",
                    "name": "基元律动",
                    "base_url": "https://tokenrhythm.studio/v1",
                    "api_key": "sk_a\nsk_b",
                },
                {
                    "id": "prov-zen",
                    "name": "OpenCode Zen 免费通道",
                    "base_url": "http://127.0.0.1:17863/v1",
                    "api_key": "sk_zen",
                },
            ]
        },
    )
    result = svc.list_decision_providers()
    assert result["ok"] is True
    items = result["providers"]
    assert [item["ref"] for item in items] == ["prov-token", "prov-zen"]
    token, zen = items
    assert token["auto_match"] is True
    assert token["key_count"] == 2
    assert zen["auto_match"] is False
    assert zen["key_count"] == 1
    # Key 内容绝不外泄：整个响应序列化后不得出现任何 Key 明文
    assert "sk_" not in json.dumps(result, ensure_ascii=False)


def test_bridge_list_decision_providers_delegates(data_dir, monkeypatch):
    monkeypatch.setattr(
        svc,
        "list_decision_providers",
        lambda: {
            "ok": True,
            "providers": [
                {
                    "ref": "p1",
                    "name": "P1",
                    "base_url": "u",
                    "key_count": 1,
                    "auto_match": True,
                }
            ],
        },
    )
    result = _make_bridge().list_decision_providers()
    assert result["ok"] is True
    assert result["providers"][0]["ref"] == "p1"


def test_audit_summary_counts_and_recent(data_dir):
    """审计汇总：今日计数（含影子/采纳/回退）+ 最近记录按时间升序。"""
    audit = data_dir / "decision_audit"
    audit.mkdir()
    rows = [
        {"ts": "2026-10-16T10:00:00", "scene": "probe", "shadow": False, "adopted": True,
         "latency_ms": 326, "fallback_reason": None, "model_id": "NeoHorse-Jev-4B",
         "answers": {"probe": 1.0}},
        {"ts": "2026-10-16T10:01:00", "scene": "router", "shadow": True, "adopted": False,
         "latency_ms": 210, "fallback_reason": None, "model_id": "NeoHorse-Jev-4B",
         "answers": {"tier": "c2"}},
        {"ts": "2026-10-16T10:02:00", "scene": "router", "shadow": False, "adopted": False,
         "latency_ms": None, "fallback_reason": "upstream_error", "model_id": "NeoHorse-Jev-4B",
         "answers": None},
    ]
    (audit / f"{svc._today()}.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    result = svc.get_decision_audit_summary(limit=10)
    assert result["ok"] is True
    assert result["today_count"] == 3
    assert result["today_shadow_count"] == 1
    assert result["today_adopted_count"] == 1
    assert result["today_fallback_count"] == 1
    assert [item["scene"] for item in result["recent"]] == ["probe", "router", "router"]
    assert result["recent"][-1]["fallback_reason"] == "upstream_error"


def test_audit_summary_missing_dir_returns_zeros(data_dir):
    result = svc.get_decision_audit_summary()
    assert result["ok"] is True
    assert result["today_count"] == 0
    assert result["recent"] == []


def test_probe_writes_audit_record(data_dir, monkeypatch):
    """探针也是真实调用：必须落一条 scene=probe 审计（面板可见性的基础）。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "audit_enabled": True})
    _mock_post(monkeypatch, lambda index, call: _resp(200, {"answers": {"probe": 1.0}}))

    result = svc.probe_decision()
    assert result["ok"] is True
    record = _last_audit(data_dir)
    assert record["scene"] == "probe"
    assert record["adopted"] is True
    assert record["latency_ms"] is not None


def test_bridge_get_decision_audit_summary_delegates(data_dir, monkeypatch):
    monkeypatch.setattr(
        svc,
        "get_decision_audit_summary",
        lambda limit=20: {"ok": True, "today_count": 5, "recent": []},
    )
    result = _make_bridge().get_decision_audit_summary()
    assert result["ok"] is True
    assert result["today_count"] == 5


def test_audit_listener_receives_brief(data_dir):
    """审计写入成功 → 监听器收到精简条目（前端实时徽章/面板的数据源）。"""
    received = []
    svc.add_audit_listener(received.append)
    try:
        svc._append_audit(
            {
                "ts": "2026-10-16T10:00:00",
                "scene": "probe",
                "shadow": False,
                "adopted": True,
                "latency_ms": 120,
                "fallback_reason": None,
                "model_id": "NeoHorse-Jev-4B",
                "answers": {"a": 1},
            },
            True,
        )
    finally:
        with svc._AUDIT_LISTENERS_LOCK:
            svc._AUDIT_LISTENERS.clear()
    assert len(received) == 1
    assert received[0]["scene"] == "probe"
    assert received[0]["adopted"] is True
    assert received[0]["latency_ms"] == 120


def test_audit_listener_exception_is_isolated(data_dir):
    """监听器崩溃不得影响审计落盘（异常隔离是实时转发的底线）。"""

    def boom(brief):
        raise RuntimeError("listener 崩溃")

    svc.add_audit_listener(boom)
    try:
        svc._append_audit({"ts": "x", "scene": "router"}, True)
    finally:
        with svc._AUDIT_LISTENERS_LOCK:
            svc._AUDIT_LISTENERS.clear()
    assert _audit_records(data_dir), "监听器异常不应中断审计写入"


def test_ask_decision_writes_audit_extra(data_dir, monkeypatch):
    """audit_extra 落进审计原始记录（影子模式一致性对比的基线数据）。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({
        "enabled": True,
        "audit_enabled": True,
        "shadow_mode": True,
        "scenes": {"router": {"enabled": True, "shadow": True, "threshold": 0.5}},
    })
    _mock_post(monkeypatch, lambda index, call: _resp(200, {"answers": {"tier": "c1"}}))

    result = svc.ask_decision(
        "帮我重构一下代码",
        NOUL_Q,
        scene="router",
        audit_extra={"baseline_tier": "c0", "message_chars": 8},
    )
    assert result is None  # 影子模式：只记录不返回
    record = _last_audit(data_dir)
    assert record["extra"] == {"baseline_tier": "c0", "message_chars": 8}
    assert record["shadow"] is True
    assert record["answers"] == {"tier": "c1"}
