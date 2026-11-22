# -*- coding: utf-8 -*-
import json
import logging
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import decision_gateway as gateway
from core import decision_service as svc
from core import permission_safety_gate as gate
from core.permission_policy import (
    PermissionAction,
    PermissionDecision,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)

PROVIDER = {
    "id": "tr-1",
    "name": "基元律动",
    "base_url": "https://tokenrhythm.studio/v1",
    "api_key": "sk_key_one",
}

_STRICTNESS = {
    PermissionAction.ALLOW: 0,
    PermissionAction.ASK: 1,
    PermissionAction.DENY: 2,
}


def _allow_facts():
    return PermissionFacts(mode="full", risk=PermissionRisk.SENSITIVE_MUTATION)


def _ask_facts():
    return PermissionFacts(mode="confirm", risk=PermissionRisk.SENSITIVE_MUTATION)


def _deny_facts():
    return PermissionFacts(mode="plan", risk=PermissionRisk.SENSITIVE_MUTATION)


def _catastrophic_deny_facts():
    return PermissionFacts(mode="full", risk=PermissionRisk.CATASTROPHIC)


ORIGINAL_CASES = {
    "allow": _allow_facts,
    "ask": _ask_facts,
    "deny": _deny_facts,
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


def _write_user_config(data_dir, providers):
    (data_dir / "config.json").write_text(
        json.dumps({"providers": providers}, ensure_ascii=False), encoding="utf-8"
    )


def _enable_safety(**scene_extra):
    scene_cfg = {"enabled": True}
    scene_cfg.update(scene_extra)
    return svc.save_decision_config({
        "enabled": True,
        "scenes": {"safety": scene_cfg},
    })


def _forbid_network(monkeypatch):
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(payload)
        raise AssertionError("该路径不允许发起决策网络请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _stub_decide(monkeypatch, value):
    calls = []

    def fake(scene, state, question, **kwargs):
        calls.append({"scene": scene, "state": state, "question": question, **kwargs})
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(gateway, "decide", fake)
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


def _resp(code, body=None):
    return httpx.Response(code, json=body if body is not None else {"answers": {}})


def _mock_post(monkeypatch, handler):
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(payload)
        return handler(len(calls) - 1, calls[-1])

    monkeypatch.setattr(svc, "_post", fake)
    return calls


@pytest.mark.parametrize("case", sorted(ORIGINAL_CASES))
def test_jev_disabled_keeps_original_decision(data_dir, monkeypatch, case):
    _write_user_config(data_dir, [PROVIDER])
    calls = _forbid_network(monkeypatch)

    facts = ORIGINAL_CASES[case]()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="git push origin main")

    assert result == baseline
    assert calls == []
    assert _audit_records(data_dir) == []


def test_low_risk_operation_skips_decision(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    calls = _forbid_network(monkeypatch)
    decide_calls = _stub_decide(monkeypatch, 0.1)

    for facts in (
        PermissionFacts(mode="full", risk=PermissionRisk.QUERY),
        PermissionFacts(mode="full", risk=PermissionRisk.NORMAL_MUTATION),
    ):
        baseline = decide_permission(facts)
        assert gate.apply_decision_to_permission(facts, baseline, command="ls") == baseline
    assert decide_calls == []
    assert calls == []


def test_low_probability_downgrades_allow_to_ask(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    decide_calls = _stub_decide(monkeypatch, 0.3)

    facts = _allow_facts()
    baseline = decide_permission(facts)
    assert baseline.action is PermissionAction.ALLOW

    result = gate.apply_decision_to_permission(facts, baseline, command="git push origin main")

    assert result.action is PermissionAction.ASK
    assert result.reason == gate.DOWNGRADE_REASON
    assert len(decide_calls) == 1
    assert decide_calls[0]["scene"] == "safety"
    assert decide_calls[0]["timeout_ms"] == gate.SAFETY_DECISION_TIMEOUT_MS
    assert decide_calls[0]["question"]["type"] == "noul"


def test_probability_just_below_threshold_downgrades(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    _stub_decide(monkeypatch, 0.899)
    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ASK

    _stub_decide(monkeypatch, 0.9)
    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ALLOW


def test_threshold_comes_from_config(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False, threshold=0.2)
    _stub_decide(monkeypatch, 0.3)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ALLOW


def test_high_probability_keeps_allow(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, 0.95)

    facts = _allow_facts()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="npm publish")

    assert result is baseline
    assert result.action is PermissionAction.ALLOW
    assert result.reason == "full_mode_allow"


@pytest.mark.parametrize(
    "factory",
    [_ask_facts, _deny_facts, _catastrophic_deny_facts],
    ids=["ask", "deny", "catastrophic_deny"],
)
def test_non_allow_never_relaxed_even_with_max_probability(data_dir, monkeypatch, factory):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    decide_calls = _stub_decide(monkeypatch, 0.99)

    facts = factory()
    baseline = decide_permission(facts)
    assert baseline.action is not PermissionAction.ALLOW

    result = gate.apply_decision_to_permission(facts, baseline, command="rm -rf build")

    assert result == baseline
    assert result.action is baseline.action
    assert result.reason == baseline.reason
    assert decide_calls == []


@pytest.mark.parametrize("case", sorted(ORIGINAL_CASES))
@pytest.mark.parametrize("probability", [None, 0.0, 0.3, 0.89, 0.9, 0.95, 0.99, 1.0])
def test_decision_never_makes_permission_looser(data_dir, monkeypatch, case, probability):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, probability)

    facts = ORIGINAL_CASES[case]()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="npm run release")

    assert _STRICTNESS[result.action] >= _STRICTNESS[baseline.action]
    if baseline.action is PermissionAction.ALLOW:
        assert result.action in (PermissionAction.ALLOW, PermissionAction.ASK)
    else:
        assert result == baseline


def test_decide_none_keeps_original_silently(data_dir, monkeypatch, caplog):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    calls = _stub_decide(monkeypatch, None)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    with caplog.at_level(logging.INFO):
        result = gate.apply_decision_to_permission(facts, baseline, command="git push")

    assert result == baseline
    assert len(calls) == 1
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == []


def test_decide_exception_keeps_original(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, RuntimeError("上游炸了"))

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


@pytest.mark.parametrize("bad", ["不是数字", True, [0.1], {"v": 0.1}])
def test_illegal_probability_keeps_original(data_dir, monkeypatch, bad):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, bad)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline) == baseline


def test_broken_decision_config_keeps_original(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    (data_dir / "decision.json").write_text("{ 不是 JSON", encoding="utf-8")
    calls = _forbid_network(monkeypatch)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline) == baseline
    assert calls == []


def test_state_contains_only_command_risk_and_mode(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    decide_calls = _stub_decide(monkeypatch, 0.95)

    facts = _allow_facts()
    gate.apply_decision_to_permission(
        facts, decide_permission(facts), command="git push origin main"
    )

    state = decide_calls[0]["state"]
    assert state == "命令：git push origin main\n风险类型：sensitive_mutation\n权限模式：full"
    assert decide_calls[0]["question"]["instructions"] == (
        "该命令在用户当前授权的权限模式下直接执行是否安全？给出安全的概率。"
    )
    for forbidden in ("会话历史", "SECRET", "环境变量", "api_key", "sk_key_one"):
        assert forbidden not in state


def test_state_truncates_long_command_to_300_chars(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    decide_calls = _stub_decide(monkeypatch, 0.95)

    facts = _allow_facts()
    command = "echo " + ("A" * 500) + "TAIL_MARK"
    gate.apply_decision_to_permission(facts, decide_permission(facts), command=command)

    state = decide_calls[0]["state"]
    command_part = state.split("\n")[0]
    assert "TAIL_MARK" not in state
    assert len(command_part) == len("命令：") + gate._STATE_COMMAND_LIMIT


def test_live_low_probability_downgrades_via_gateway(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.3}}})
    )

    facts = _allow_facts()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="git push --force")

    assert len(calls) == 1
    assert result.action is PermissionAction.ASK
    assert result.reason == gate.DOWNGRADE_REASON


def test_live_high_probability_keeps_allow_via_gateway(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety()
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.95}}}))

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


def test_upstream_timeout_keeps_original(data_dir, monkeypatch):
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety()
    _mock_post(
        monkeypatch,
        lambda i, call: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
    )

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


def test_permission_policy_stays_pure():
    source = (
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        + os.sep
        + os.path.join("core", "permission_policy.py")
    )
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("httpx", "decision_service", "decision_gateway", "requests."):
        assert forbidden not in text
