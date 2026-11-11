# -*- coding: utf-8 -*-
"""safety 场景（危险命令放行判定）接入测试。

被测对象：``core.permission_safety_gate.apply_decision_to_permission``。

核心安全不变量：**JEV 只能加严，不能放宽**。
- 原判定不是 allow（ask / deny）→ 任何概率都不改变它；
- 原判定是 allow → 只有安全概率 < 阈值（默认 0.9）才降级为 ask；
- 因此本模块唯一的力量是「allow → ask」，其余一概不动。

隔离策略：``tests/conftest.py`` 已把 ``YCODE_DATA_DIR`` 指向临时目录；
本文件再把 ``config.YCODE_DIR`` monkeypatch 到每个用例的 tmp_path，
并统一打桩 ``decision_service._post``，绝不触碰真实 ``~/.ycode``、绝不真实联网。
"""
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

# 判定动作的「严格程度」序：值越大越严。用于断言 JEV 永不让判定变松。
_STRICTNESS = {
    PermissionAction.ALLOW: 0,
    PermissionAction.ASK: 1,
    PermissionAction.DENY: 2,
}


# ---------------------------------------------------------------------------
# facts 工厂：三种原判定（均为高风险档，确保决策链路会被触发）
# ---------------------------------------------------------------------------

def _allow_facts():
    """完全访问模式 + 敏感变更 → 原判定 allow（full_mode_allow）。"""
    return PermissionFacts(mode="full", risk=PermissionRisk.SENSITIVE_MUTATION)


def _ask_facts():
    """变更前确认模式 + 敏感变更 → 原判定 ask（confirm_requires_confirmation）。"""
    return PermissionFacts(mode="confirm", risk=PermissionRisk.SENSITIVE_MUTATION)


def _deny_facts():
    """计划模式 + 敏感变更 → 原判定 deny（plan_mutation_denied）。"""
    return PermissionFacts(mode="plan", risk=PermissionRisk.SENSITIVE_MUTATION)


def _catastrophic_deny_facts():
    """灾难级操作 → 原判定恒为 deny（catastrophic_denied）。"""
    return PermissionFacts(mode="full", risk=PermissionRisk.CATASTROPHIC)


ORIGINAL_CASES = {
    "allow": _allow_facts,
    "ask": _ask_facts,
    "deny": _deny_facts,
}


# ---------------------------------------------------------------------------
# 测试基础设施
# ---------------------------------------------------------------------------

@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """把 config.YCODE_DIR 指到 tmp_path，彻底隔离真实 ~/.ycode。"""
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


def _enable_safety(*, shadow=False, global_shadow=None, **scene_extra):
    """写决策配置：全局 enabled + safety 场景 enabled。"""
    scene_cfg = {"enabled": True, "shadow": shadow}
    scene_cfg.update(scene_extra)
    return svc.save_decision_config({
        "enabled": True,
        "shadow_mode": shadow if global_shadow is None else global_shadow,
        "scenes": {"safety": scene_cfg},
    })


def _forbid_network(monkeypatch):
    """护栏：任何 HTTP 调用都直接失败（未启用/低风险路径必须零请求）。"""
    calls = []

    def fake(url, headers, payload, timeout_seconds):
        calls.append(payload)
        raise AssertionError("该路径不允许发起决策网络请求")

    monkeypatch.setattr(svc, "_post", fake)
    return calls


def _stub_decide(monkeypatch, value):
    """打桩网关 decide；返回调用记录（args/kwargs）。"""
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


# ---------------------------------------------------------------------------
# 1. JEV 关闭：判定结果与接入前完全一致
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", sorted(ORIGINAL_CASES))
def test_jev_disabled_keeps_original_decision(data_dir, monkeypatch, case):
    """JEV 全关（默认配置）：allow/ask/deny 三种原判定都零变化、零请求、零审计。"""
    _write_user_config(data_dir, [PROVIDER])  # 有 Key，但场景全关
    calls = _forbid_network(monkeypatch)

    facts = ORIGINAL_CASES[case]()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="git push origin main")

    assert result == baseline
    assert calls == []
    assert _audit_records(data_dir) == []


def test_low_risk_operation_skips_decision(data_dir, monkeypatch):
    """低风险操作（QUERY / NORMAL_MUTATION）完全不发决策请求。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)  # 即使是 live 也不该问
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


# ---------------------------------------------------------------------------
# 2. 概率低于阈值 → allow 降级为 ask
# ---------------------------------------------------------------------------

def test_low_probability_downgrades_allow_to_ask(data_dir, monkeypatch):
    """原判定 allow + 概率 0.3（< 阈值 0.9）→ 降级为 ask（不是 deny）。"""
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
    """边界：0.899 < 0.9 → 降级；0.9 恰好达标 → 保持 allow。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    _stub_decide(monkeypatch, 0.899)
    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ASK

    _stub_decide(monkeypatch, 0.9)
    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ALLOW


def test_threshold_comes_from_config(data_dir, monkeypatch):
    """阈值读 scenes.safety.threshold：降到 0.2 后 0.3 不再触发降级。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False, threshold=0.2)
    _stub_decide(monkeypatch, 0.3)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline).action is PermissionAction.ALLOW


# ---------------------------------------------------------------------------
# 3. 概率达标 → 保持 allow
# ---------------------------------------------------------------------------

def test_high_probability_keeps_allow(data_dir, monkeypatch):
    """原判定 allow + 概率 0.95 → 保持 allow，reason 也不变。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, 0.95)

    facts = _allow_facts()
    baseline = decide_permission(facts)
    result = gate.apply_decision_to_permission(facts, baseline, command="npm publish")

    assert result is baseline
    assert result.action is PermissionAction.ALLOW
    assert result.reason == "full_mode_allow"


# ---------------------------------------------------------------------------
# 4. 【最重要】只有加严：ask / deny 永不被放宽
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "factory",
    [_ask_facts, _deny_facts, _catastrophic_deny_facts],
    ids=["ask", "deny", "catastrophic_deny"],
)
def test_non_allow_never_relaxed_even_with_max_probability(data_dir, monkeypatch, factory):
    """原判定 ask / deny + 概率 0.99：原判定必须原封不动。

    实现上靠「非 ALLOW 直接早退」保证——连概率读取都不会发生，
    因此这里同时断言决策请求数为 0（结构上的保证，强于数值比较）。
    """
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
    """不变量（穷举组合）：结果严格程度永不小于原判定，且只在 allow 上可能变成 ask。"""
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


# ---------------------------------------------------------------------------
# 5. None / 异常兜底：原判定不变，且不产生日志噪音
# ---------------------------------------------------------------------------

def test_decide_none_keeps_original_silently(data_dir, monkeypatch, caplog):
    """decide 返回 None（off/shadow/失败/非法答案）→ 原判定不变、无警告级日志。"""
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
    """decide 抛异常 → 吞掉，原判定不变。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, RuntimeError("上游炸了"))

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


@pytest.mark.parametrize("bad", ["不是数字", True, [0.1], {"v": 0.1}])
def test_illegal_probability_keeps_original(data_dir, monkeypatch, bad):
    """网关返回非概率值（字符串/布尔/容器）→ 当作 None，原判定不变。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _stub_decide(monkeypatch, bad)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline) == baseline


def test_broken_decision_config_keeps_original(data_dir, monkeypatch):
    """decision.json 损坏 → 回落全关，原判定不变、零请求。"""
    _write_user_config(data_dir, [PROVIDER])
    (data_dir / "decision.json").write_text("{ 不是 JSON", encoding="utf-8")
    calls = _forbid_network(monkeypatch)

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline) == baseline
    assert calls == []


# ---------------------------------------------------------------------------
# 6. state 隐私约束：只有命令 + 风险类型 + 权限模式
# ---------------------------------------------------------------------------

def test_state_contains_only_command_risk_and_mode(data_dir, monkeypatch):
    """state 只含命令文本、风险类型、权限模式，不含历史/文件内容/密钥。"""
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
    """命令文本超过 300 字符时截断，尾部内容不会外发。"""
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


# ---------------------------------------------------------------------------
# 7. 端到端（真实网关 + 打桩 HTTP）：live / shadow 三态
# ---------------------------------------------------------------------------

def test_live_low_probability_downgrades_via_gateway(data_dir, monkeypatch):
    """live 模式：上游给 0.3（低于 0.9）→ 真实链路下 allow 降级为 ask。

    这条用例同时钉住网关的 threshold 覆盖：若沿用网关默认门槛，
    0.3 会被当成「无效答案」吞成 None，降级逻辑就永远走不到。
    """
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
    """live 模式：上游给 0.95 → 保持 allow。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _mock_post(monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.95}}}))

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


def test_shadow_mode_keeps_original(data_dir, monkeypatch):
    """影子模式：真发请求、真写审计，但判定零变化。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=True)
    calls = _mock_post(
        monkeypatch, lambda i, call: _resp(200, {"answers": {"q": {"noul": 0.1}}})
    )

    facts = _allow_facts()
    baseline = decide_permission(facts)

    result = gate.apply_decision_to_permission(facts, baseline, command="git push")

    assert len(calls) == 1
    assert result == baseline
    records = _audit_records(data_dir)
    assert records and records[-1]["shadow"] is True
    assert records[-1]["adopted"] is False


def test_upstream_timeout_keeps_original(data_dir, monkeypatch):
    """上游超时：原判定不变，不抛异常。"""
    _write_user_config(data_dir, [PROVIDER])
    _enable_safety(shadow=False)
    _mock_post(
        monkeypatch,
        lambda i, call: (_ for _ in ()).throw(httpx.TimeoutException("timeout")),
    )

    facts = _allow_facts()
    baseline = decide_permission(facts)

    assert gate.apply_decision_to_permission(facts, baseline, command="git push") == baseline


# ---------------------------------------------------------------------------
# 8. 纯函数性质护栏：permission_policy 不得引入网络依赖
# ---------------------------------------------------------------------------

def test_permission_policy_stays_pure():
    """硬性约束：permission_policy 不 import httpx / decision 链路（保持纯函数）。"""
    source = (
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        + os.sep
        + os.path.join("core", "permission_policy.py")
    )
    with open(source, "r", encoding="utf-8") as handle:
        text = handle.read()
    for forbidden in ("httpx", "decision_service", "decision_gateway", "requests."):
        assert forbidden not in text
