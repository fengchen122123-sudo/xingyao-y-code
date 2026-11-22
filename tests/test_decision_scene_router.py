# -*- coding: utf-8 -*-
import json
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import model_manager
from core import turn_model_override as tmo

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_main_key",
}

MAIN_MODEL = {
    "litellm_model": "openai/gpt-5-main",
    "api_key": "sk_main_key",
    "base_url": "https://main.example.com/v1",
    "context_window": 400000,
    "id": "model-main",
    "name": "主模型",
}

SECONDARY_MODEL = {
    "litellm_model": "openai/gpt-5-mini",
    "api_key": "sk_secondary_key",
    "base_url": "https://secondary.example.com/v1",
    "context_window": 128000,
    "supports_vision": False,
    "supports_function_calling": True,
    "computer_protocol": None,
    "system_prompt_extra": "",
    "name": "副模型",
    "protocol": "openai",
    "provider_name": "便宜厂商",
    "id": "model-secondary",
}

USER_TEXT = "帮我解释一下这段报错是什么意思"


def _mock_secondary(monkeypatch, secondary):
    monkeypatch.setattr(model_manager, "get_secondary_model", lambda: secondary)


def _mock_decide(monkeypatch, value):
    calls = []

    def fake(scene, state, question, **kwargs):
        calls.append(
            {"scene": scene, "state": state, "question": question, "kwargs": kwargs}
        )
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(gateway, "decide", fake)
    return calls


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
        raise AssertionError("JEV 关闭时不允许发起决策请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _write_user_config(data_dir):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": [PROVIDER]}, ensure_ascii=False), encoding="utf-8"
    )


def _enable_router(*, shadow=False):
    return svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": shadow,
            "scenes": {"router": {"enabled": True, "shadow": shadow, "timeout_ms": 250}},
        }
    )


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


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    import config

    directory = tmp_path / "ycode"
    directory.mkdir()
    monkeypatch.setattr(config, "YCODE_DIR", str(directory), raising=False)
    svc._reset_runtime_state()
    yield directory
    svc._reset_runtime_state()


def test_secondary_picked_returns_five_tuple(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert result == {
        "litellm_model": "openai/gpt-5-mini",
        "api_key": "sk_secondary_key",
        "base_url": "https://secondary.example.com/v1",
        "context_window": 128000,
        "model_id": "model-secondary",
        "name": "副模型",
    }
    assert result["model_id"] != MAIN_MODEL["id"]


def test_secondary_picked_passes_protocol_expectations(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    call = calls[0]
    assert call["scene"] == "router"
    assert call["kwargs"]["options"] == ("main", "secondary")
    assert call["kwargs"]["timeout_ms"] == tmo.ROUTER_TIMEOUT_MS
    assert call["question"]["type"] == "choice"
    assert set(call["question"]["criteria"]) == {"main", "secondary"}
    assert all(call["question"]["criteria"].values())
    assert "main=能力更强但更贵" in call["question"]["instructions"]
    assert "secondary=更便宜" in call["question"]["instructions"]
    assert "sk_secondary_key" not in json.dumps(call, ensure_ascii=False)
    assert "sk_main_key" not in json.dumps(call, ensure_ascii=False)


def test_baseline_equal_to_window_still_allowed(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(
        MAIN_MODEL, baseline_tokens=SECONDARY_MODEL["context_window"], user_text=USER_TEXT
    )

    assert result is not None


def test_baseline_zero_skips_window_guard(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(MAIN_MODEL, baseline_tokens=0, user_text=USER_TEXT)

    assert result is not None


def test_decide_none_keeps_main_model(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, None)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_decide_main_keeps_main_model(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "main")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


@pytest.mark.parametrize(
    "bad_answer",
    ["Secondary", "SECONDARY", " secondary ", "", "unknown", 1, 0.9, True],
    ids=["case", "upper", "padded", "empty", "unknown", "int", "float", "bool"],
)
def test_illegal_answer_keeps_main_model(monkeypatch, bad_answer):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, bad_answer)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_decide_exception_keeps_main_model(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, RuntimeError("决策服务炸了"))

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_get_secondary_model_exception_keeps_main_model(monkeypatch):
    def _boom():
        raise RuntimeError("配置损坏")

    monkeypatch.setattr(model_manager, "get_secondary_model", _boom)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


@pytest.mark.parametrize(
    "secondary",
    [None, {}, {"litellm_model": "", "id": "model-secondary"}],
    ids=["none", "empty", "no-litellm"],
)
def test_secondary_unusable_keeps_main_model(monkeypatch, secondary):
    _mock_secondary(monkeypatch, secondary)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert calls == [], "副模型不可用时不该浪费一次决策"


def test_secondary_same_as_main_keeps_main_model(monkeypatch):
    same = dict(SECONDARY_MODEL, litellm_model=MAIN_MODEL["litellm_model"])
    _mock_secondary(monkeypatch, same)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert calls == []


@pytest.mark.parametrize("field", ["api_key", "base_url", "id", "context_window"])
def test_secondary_missing_critical_field_keeps_main_model(monkeypatch, field):
    broken = dict(SECONDARY_MODEL)
    broken[field] = "" if field != "context_window" else 0
    _mock_secondary(monkeypatch, broken)
    _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None


def test_baseline_exceeds_secondary_window_keeps_main_model(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    result = tmo.resolve_turn_model(
        MAIN_MODEL, baseline_tokens=200000, user_text=USER_TEXT
    )

    assert result is None
    assert calls == [], "护栏在决策之前就该拦下，不该再发决策请求"


@pytest.mark.parametrize("source", ["lan", "automation", "local ", ""])
def test_non_local_turn_source_keeps_main_model(monkeypatch, source):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, turn_source=source, user_text=USER_TEXT) is None
    assert calls == []


@pytest.mark.parametrize("pending", ["model-explicit", " model-explicit "])
def test_pending_model_id_keeps_main_model(monkeypatch, pending):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert (
        tmo.resolve_turn_model(
            MAIN_MODEL, pending_model_id=pending, user_text=USER_TEXT
        )
        is None
    )
    assert calls == []


@pytest.mark.parametrize(
    "active",
    [None, {}, {"api_key": "k"}, {"litellm_model": ""}, "not-a-dict"],
    ids=["none", "empty", "no-litellm", "blank-litellm", "str"],
)
def test_active_model_unusable_keeps_main_model(monkeypatch, active):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(active, user_text=USER_TEXT) is None


@pytest.mark.parametrize("text", ["", "   ", None, [{"type": "image_url"}]])
def test_empty_user_text_keeps_main_model(monkeypatch, text):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, "secondary")

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=text) is None
    assert calls == []


def test_pending_empty_string_allows_override(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    _mock_decide(monkeypatch, "secondary")

    assert (
        tmo.resolve_turn_model(MAIN_MODEL, pending_model_id="", user_text=USER_TEXT)
        is not None
    )


def test_state_is_user_text_only(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert calls[0]["state"] == USER_TEXT


def test_state_truncated_to_500_chars(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    long_text = "把这段文档逐字解释一遍：" + "内容" * 1000
    tmo.resolve_turn_model(MAIN_MODEL, user_text=long_text)

    state = calls[0]["state"]
    assert len(state) <= 500
    assert state.startswith("把这段文档逐字解释一遍：")
    assert state == " ".join(long_text.split())[:500]


def test_state_has_no_file_content_marker(monkeypatch):
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    calls = _mock_decide(monkeypatch, None)

    tmo.resolve_turn_model(MAIN_MODEL, user_text="看看 README 写的是什么")

    state = calls[0]["state"]
    for leaked in ("<file_content", "工具返回", "tool_result", "系统提示词"):
        assert leaked not in state


def test_router_scene_live_false_when_off(data_dir, monkeypatch):
    _write_user_config(data_dir)

    assert tmo.router_scene_live() is False


def test_router_scene_live_false_when_scene_shadow(data_dir):
    _write_user_config(data_dir)
    _enable_router(shadow=True)

    assert tmo.router_scene_live() is False


def test_router_scene_live_false_when_global_shadow(data_dir):
    _write_user_config(data_dir)
    svc.save_decision_config(
        {
            "enabled": True,
            "shadow_mode": True,
            "scenes": {"router": {"enabled": True, "shadow": False}},
        }
    )

    assert tmo.router_scene_live() is False


def test_router_scene_live_true_when_live(data_dir):
    _write_user_config(data_dir)
    _enable_router(shadow=False)

    assert tmo.router_scene_live() is True


def test_router_scene_live_zero_network_when_off(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _mock_no_network(monkeypatch)

    assert tmo.router_scene_live() is False


def test_live_mode_end_to_end_picks_secondary(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "secondary"}}})
    )

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT)

    assert result is not None
    assert result["litellm_model"] == "openai/gpt-5-mini"
    assert len(post_calls) == 1, "live 模式必须真发一次决策请求"
    records = _audit_records(data_dir)
    assert records, "live 决策必须留审计"
    record = records[-1]
    assert record["scene"] == "router"
    assert record["shadow"] is False
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_mode_end_to_end_main_keeps_main_model(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "main"}}})
    )

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert len(post_calls) == 1
    record = _audit_records(data_dir)[-1]
    assert record["scene"] == "router"
    assert record["adopted"] is True, "网关采纳了合法答案 main（结论是「不用副模型」）"


def test_shadow_mode_keeps_main_model(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _enable_router(shadow=True)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "secondary"}}})
    )

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert len(post_calls) == 1
    record = _audit_records(data_dir)[-1]
    assert record["shadow"] is True
    assert record["adopted"] is False


def test_jev_disabled_zero_request_and_no_override(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_no_network(monkeypatch)

    assert tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT) is None
    assert post_calls == []
    assert _audit_records(data_dir) == []


def test_image_turn_never_falls_back_to_text_only_secondary(data_dir, monkeypatch):
    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, SECONDARY_MODEL)
    post_calls = _mock_no_network(monkeypatch)

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT, has_image=True)

    assert result is None, "带图回合不能被切到不识图的副模型"
    assert post_calls == [], "能力护栏必须在问决策模型之前就短路（零请求）"


def test_image_turn_allowed_when_secondary_can_see(data_dir, monkeypatch):
    from core import llm_dialects

    _write_user_config(data_dir)
    _enable_router(shadow=False)
    _mock_secondary(monkeypatch, {**SECONDARY_MODEL, "supports_vision": True})
    calls = _mock_decide(monkeypatch, "secondary")
    monkeypatch.setattr(llm_dialects, "_supports_vision_llm_first", lambda name: True)

    result = tmo.resolve_turn_model(MAIN_MODEL, user_text=USER_TEXT, has_image=True)

    assert result is not None
    assert result["litellm_model"] == SECONDARY_MODEL["litellm_model"]
    assert len(calls) == 1
    assert calls[0]["kwargs"]["audit_extra"]["has_image"] is True
