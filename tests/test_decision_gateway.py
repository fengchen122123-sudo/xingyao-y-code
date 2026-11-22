# -*- coding: utf-8 -*-
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one\nsk_key_two",
}

CHOICE_Q = {"type": "choice", "instructions": "该选哪个候选？", "criteria": ["a", "b"]}
NOUL_Q = {"type": "noul", "instructions": "该操作是否安全？"}


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable(scene="router", **scene_extra):
    scene_cfg = {"enabled": True}
    scene_cfg.update(scene_extra)
    patch = {
        "enabled": True,
        "scenes": {scene: scene_cfg},
    }
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


def _mock_no_network(monkeypatch):
    calls = []

    def fake(url, headers, payload, timeout_seconds):  # pragma: no cover - 不应被调用
        calls.append(payload)
        raise AssertionError("未启用的场景不允许发起网络请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _audit_records(data_dir):
    records = []
    directory = data_dir / "decision_audit"
    if not directory.is_dir():
        return records
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


def test_global_disabled_returns_none_without_http(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []
    assert _audit_records(data_dir) == []


def test_scene_disabled_returns_none_without_http(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True})
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("search", "上下文", NOUL_Q) is None
    assert calls == []
    assert _audit_records(data_dir) == []


def test_unknown_scene_returns_none_without_http(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("routr", "上下文", CHOICE_Q, options=["a"]) is None
    assert calls == []


def test_legacy_shadow_fields_do_not_block_decision(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    (data_dir / "decision.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "shadow_mode": True,
                "scenes": {"router": {"enabled": True, "shadow": True}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "a"
    assert len(calls) == 1
    assert _last_audit(data_dir)["adopted"] is True


def test_live_choice_returns_option_and_backfills_adopted(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "b"}}}))

    picked = gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])

    assert picked == "b"
    assert len(calls) == 1
    record = _last_audit(data_dir)
    assert "shadow" not in record
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_choice_accepts_bare_value_answer_shape(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": " a "}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "a"


def test_live_choice_falls_back_to_single_answer_key(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"anything": "b"}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "b"


def test_live_choice_keeps_option_type_for_non_string_options(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("search")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "8"}}}))

    assert gateway.decide("search", "上下文", CHOICE_Q, options=[3, 5, 8]) == 8


def test_choice_answer_not_in_options_returns_none(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "z"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert _last_audit(data_dir)["adopted"] is False


def test_choice_answer_missing_returns_none(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None


def test_choice_without_options_skips_candidate_check(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "任意值"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q) == "任意值"


def test_noul_below_threshold_returns_none(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.89}}}))

    assert gateway.decide("safety", "上下文", NOUL_Q) is None
    assert _last_audit(data_dir)["adopted"] is False


def test_noul_at_threshold_returns_probability(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.9}}}))

    result = gateway.decide("safety", "上下文", NOUL_Q)

    assert result == 0.9
    assert isinstance(result, float)
    assert _last_audit(data_dir)["adopted"] is True


def test_noul_scene_threshold_comes_from_config(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety", threshold=0.3)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.4}}}))

    assert gateway.decide("safety", "上下文", NOUL_Q) == 0.4


@pytest.mark.parametrize("bad", [1.5, -0.1, "不是数字", None, True])
def test_noul_illegal_probability_returns_none(data_dir, monkeypatch, bad):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", threshold=0.0)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": bad}}}))

    assert gateway.decide("router", "上下文", NOUL_Q) is None


@pytest.mark.parametrize(
    "handler",
    [
        lambda i, call: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
        lambda i, call: _resp(401, {"error": "unauthorized"}),
        lambda i, call: _resp(500, {"error": "boom"}),
        lambda i, call: _resp(429, {"error": "rate limited"}),
        lambda i, call: _resp(200, "不是对象"),
    ],
    ids=["timeout", "401", "500", "429", "bad_body"],
)
def test_upstream_failures_return_none_without_raising(data_dir, monkeypatch, handler):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, handler)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None


def test_mark_adopted_failure_is_swallowed_and_returns_picked(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    def boom(trace_id):
        raise RuntimeError("审计文件被锁")

    monkeypatch.setattr(svc, "mark_adopted", boom)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "a"


def test_broken_config_returns_none(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    (data_dir / "decision.json").write_text("{ 这不是 JSON", encoding="utf-8")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []


def test_no_key_returns_none(data_dir, monkeypatch):
    _enable("router")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []


def test_default_timeout_is_250ms(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", timeout_ms=250)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])

    assert calls[0]["timeout"] == pytest.approx(0.25)


def test_timeout_below_min_is_raised_to_min(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=10)

    assert calls[0]["timeout"] == pytest.approx(svc.MIN_TIMEOUT_MS / 1000.0)


def test_timeout_above_max_is_capped(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=99999)

    assert calls[0]["timeout"] == pytest.approx(svc.MAX_TIMEOUT_MS / 1000.0)


def test_scene_timeout_overrides_default_and_caller_wins(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", timeout_ms=1200)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])
    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=400)

    assert calls[0]["timeout"] == pytest.approx(1.2)
    assert calls[1]["timeout"] == pytest.approx(0.4)


@pytest.mark.parametrize(
    "question",
    [{"type": "open", "instructions": "随便说"}, {}, {"instructions": "缺 type"}, None, "不是字典"],
)
def test_unsupported_question_returns_none_without_http(data_dir, monkeypatch, question):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", question, options=["a"]) is None
    assert calls == []


def test_redact_sensitive_masks_common_secret_shapes():
    assert "sk-abc123456" not in gateway.redact_sensitive("export KEY=sk-abc123456")
    assert "abcdef123456" not in gateway.redact_sensitive("Authorization: Bearer abcdef123456")
    assert "xyz987654321" not in gateway.redact_sensitive("api_key=xyz987654321")
    # 正常文本逐字保留：脱敏不能影响决策模型理解任务
    assert gateway.redact_sensitive("给这个函数加个缓存") == "给这个函数加个缓存"
    assert gateway.redact_sensitive(None) == ""
    assert gateway.redact_sensitive(123) == ""
