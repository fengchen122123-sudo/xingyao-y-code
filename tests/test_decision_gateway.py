# -*- coding: utf-8 -*-
"""JEV 决策网关测试：三态分发 / 答案校验 / 阈值 / 超时钳制 / 异常兜底 / 采纳回填。

隔离策略：``tests/conftest.py`` 收集阶段已把 ``YCODE_DATA_DIR`` 指向临时目录；
本文件再把 ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，绝不触碰
真实 ``~/.ycode``。HTTP 层统一 monkeypatch ``decision_service._post``，不真实联网。
"""
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


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable(scene="router", *, shadow=False, global_shadow=None, **scene_extra):
    """写决策配置：全局 enabled + 指定场景 enabled。"""
    scene_cfg = {"enabled": True, "shadow": shadow}
    scene_cfg.update(scene_extra)
    patch = {
        "enabled": True,
        "shadow_mode": shadow if global_shadow is None else global_shadow,
        "scenes": {scene: scene_cfg},
    }
    return svc.save_decision_config(patch)


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    """打桩 decision_service._post；返回调用记录（网关链路同样走这里）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(
            {"url": url, "headers": headers, "payload": payload, "timeout": timeout_seconds}
        )
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _mock_no_network(monkeypatch):
    """未启用路径的护栏：任何 HTTP 调用都视为失败。"""
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


# ---------------------------------------------------------------------------
# 1. 三态闸门：off / shadow / live
# ---------------------------------------------------------------------------

def test_global_disabled_returns_none_without_http(data_dir, monkeypatch):
    """全局 enabled=false：返回 None，且 0 次 HTTP 调用、0 条审计。"""
    _write_user_config(data_dir, [PROVIDER])
    calls = _mock_no_network(monkeypatch)  # 默认配置即全关，无需再写决策配置

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []
    assert _audit_records(data_dir) == []


def test_scene_disabled_returns_none_without_http(data_dir, monkeypatch):
    """全局启用但场景未启用：同样零请求、返回 None。"""
    _write_user_config(data_dir, [PROVIDER])
    svc.save_decision_config({"enabled": True, "shadow_mode": False})
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("search", "上下文", NOUL_Q) is None
    assert calls == []
    assert _audit_records(data_dir) == []


def test_unknown_scene_returns_none_without_http(data_dir, monkeypatch):
    """非法场景名（含拼写错误）：直接 None，不发请求。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("routr", "上下文", CHOICE_Q, options=["a"]) is None
    assert calls == []


def test_scene_shadow_calls_upstream_writes_audit_but_returns_none(data_dir, monkeypatch):
    """场景影子：发出请求、写了审计，但网关返回 None。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=True)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert len(calls) == 1

    record = _last_audit(data_dir)
    assert record["scene"] == "router"
    assert record["shadow"] is True
    assert record["adopted"] is False  # 影子不采纳
    assert record["answers"] == {"q": {"choice": "a"}}


def test_global_shadow_mode_overrides_scene_shadow_false(data_dir, monkeypatch):
    """全局 shadow_mode=true 时，即使场景 shadow=false 也只观测不采纳。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False, global_shadow=True)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert len(calls) == 1
    assert _last_audit(data_dir)["shadow"] is True


def test_live_choice_returns_option_and_backfills_adopted(data_dir, monkeypatch):
    """live 模式 choice 合法答案：返回该值，审计 adopted 回填为 true。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "b"}}}))

    picked = gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])

    assert picked == "b"
    assert len(calls) == 1
    record = _last_audit(data_dir)
    assert record["shadow"] is False
    assert record["adopted"] is True
    assert record["trace_id"]


def test_live_choice_accepts_bare_value_answer_shape(data_dir, monkeypatch):
    """答案形状容错：裸值（answers={"q": "a"}）也能识别。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": " a "}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "a"


def test_live_choice_falls_back_to_single_answer_key(data_dir, monkeypatch):
    """answers 无 "q" 键时取唯一一项。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"anything": "b"}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "b"


def test_live_choice_keeps_option_type_for_non_string_options(data_dir, monkeypatch):
    """候选为数字时（如 num_results 档位）返回原始候选值，便于调用方直接用。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("search", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "8"}}}))

    assert gateway.decide("search", "上下文", CHOICE_Q, options=[3, 5, 8]) == 8


# ---------------------------------------------------------------------------
# 2. 答案校验：choice 非法 / noul 阈值
# ---------------------------------------------------------------------------

def test_choice_answer_not_in_options_returns_none(data_dir, monkeypatch):
    """choice 答案不在 options 内：无效 → None，且不回填 adopted。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "z"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert _last_audit(data_dir)["adopted"] is False


def test_choice_answer_missing_returns_none(data_dir, monkeypatch):
    """上游答案缺字段/为空：None。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {}}))

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None


def test_choice_without_options_skips_candidate_check(data_dir, monkeypatch):
    """options 为空：不做候选校验，直接返回答案文本。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "任意值"}}}))

    assert gateway.decide("router", "上下文", CHOICE_Q) == "任意值"


def test_noul_below_threshold_returns_none(data_dir, monkeypatch):
    """noul 概率低于 scene.threshold（safety 默认 0.9）：None。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.89}}}))

    assert gateway.decide("safety", "上下文", NOUL_Q) is None
    assert _last_audit(data_dir)["adopted"] is False


def test_noul_at_threshold_returns_probability(data_dir, monkeypatch):
    """noul 概率达到阈值：返回 float 概率并回填 adopted。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.9}}}))

    result = gateway.decide("safety", "上下文", NOUL_Q)

    assert result == 0.9
    assert isinstance(result, float)
    assert _last_audit(data_dir)["adopted"] is True


def test_noul_scene_threshold_comes_from_config(data_dir, monkeypatch):
    """阈值读配置：把 safety 阈值降到 0.3 后 0.4 的答案也能通过。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("safety", shadow=False, threshold=0.3)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.4}}}))

    assert gateway.decide("safety", "上下文", NOUL_Q) == 0.4


@pytest.mark.parametrize("bad", [1.5, -0.1, "不是数字", None, True])
def test_noul_illegal_probability_returns_none(data_dir, monkeypatch, bad):
    """概率非法（越界 / 非数字 / 布尔）：None。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False, threshold=0.0)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": bad}}}))

    assert gateway.decide("router", "上下文", NOUL_Q) is None


# ---------------------------------------------------------------------------
# 3. 上游故障兜底（不抛异常）
# ---------------------------------------------------------------------------

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
    """上游超时/401/500/429/坏响应：返回 None，不向上抛异常。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, handler)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None


def test_mark_adopted_failure_is_swallowed_and_returns_picked(data_dir, monkeypatch):
    """mark_adopted 抛异常：只记日志，决策值照常返回，绝不外抛。

    语义：回填 adopted 是审计旁路动作，失败不代表决策无效。此前实现把它放在
    decide 的总 try 里，会导致一次已校验合法的决策因审计回填失败而整体丢失
    （业务侧退回原有逻辑），属于错误降级。
    """
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    def boom(trace_id):
        raise RuntimeError("审计文件被锁")

    monkeypatch.setattr(svc, "mark_adopted", boom)

    # 不抛异常是硬要求；决策值必须照常返回
    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) == "a"


def test_broken_config_returns_none(data_dir, monkeypatch):
    """配置损坏（非法 JSON）：回落默认全关 → None，不发请求。"""
    _write_user_config(data_dir, [PROVIDER])
    (data_dir / "decision.json").write_text("{ 这不是 JSON", encoding="utf-8")
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []


def test_no_key_returns_none(data_dir, monkeypatch):
    """无可用 Key（上游链路未配置）：None，不炸。"""
    _enable("router", shadow=False)
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"]) is None
    assert calls == []


# ---------------------------------------------------------------------------
# 4. 超时：默认 250ms + 双向钳制 + scene 级覆盖
# ---------------------------------------------------------------------------

def test_default_timeout_is_250ms(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False, timeout_ms=250)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])

    assert calls[0]["timeout"] == pytest.approx(0.25)


def test_timeout_below_min_is_raised_to_min(data_dir, monkeypatch):
    """传 10ms：抬到 MIN_TIMEOUT_MS。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=10)

    assert calls[0]["timeout"] == pytest.approx(svc.MIN_TIMEOUT_MS / 1000.0)


def test_timeout_above_max_is_capped(data_dir, monkeypatch):
    """传 99999ms：压到 MAX_TIMEOUT_MS。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=99999)

    assert calls[0]["timeout"] == pytest.approx(svc.MAX_TIMEOUT_MS / 1000.0)


def test_scene_timeout_overrides_default_and_caller_wins(data_dir, monkeypatch):
    """优先级：调用方 timeout_ms > scene.timeout_ms > 250ms。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False, timeout_ms=1200)
    calls = _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"choice": "a"}}}))

    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"])
    gateway.decide("router", "上下文", CHOICE_Q, options=["a", "b"], timeout_ms=400)

    assert calls[0]["timeout"] == pytest.approx(1.2)
    assert calls[1]["timeout"] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# 5. 题目形状：非 choice/noul 不接入
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "question",
    [{"type": "open", "instructions": "随便说"}, {}, {"instructions": "缺 type"}, None, "不是字典"],
)
def test_unsupported_question_returns_none_without_http(data_dir, monkeypatch, question):
    _write_user_config(data_dir, [PROVIDER])
    _enable("router", shadow=False)
    calls = _mock_no_network(monkeypatch)

    assert gateway.decide("router", "上下文", question, options=["a"]) is None
    assert calls == []


# ---------------------------------------------------------------------------
# 6. 隐私脱敏：进入 state 的文本可能夹带密钥
# ---------------------------------------------------------------------------

def test_redact_sensitive_masks_common_secret_shapes():
    """router / safety 的 state 会真实发给决策模型，密钥形态必须先抹掉。"""
    # sk- 形态
    assert "sk-abc123456" not in gateway.redact_sensitive("export KEY=sk-abc123456")
    # Bearer 形态
    assert "abcdef123456" not in gateway.redact_sensitive("Authorization: Bearer abcdef123456")
    # key=value 形态（键名保留，值抹掉，语义仍可判断）
    assert "xyz987654321" not in gateway.redact_sensitive("api_key=xyz987654321")
    # 正常文本逐字保留：脱敏不能影响决策模型理解任务
    assert gateway.redact_sensitive("给这个函数加个缓存") == "给这个函数加个缓存"
    # 非字符串不抛异常
    assert gateway.redact_sensitive(None) == ""
    assert gateway.redact_sensitive(123) == ""

