from __future__ import annotations

import logging

from core.permission_policy import (
    PermissionAction,
    PermissionDecision,
    PermissionRisk,
)

logger = logging.getLogger(__name__)

DEFAULT_SAFETY_THRESHOLD = 0.9

# safety 场景单次决策超时（毫秒）：真实决策宁可不用，也不拖慢权限流程
SAFETY_DECISION_TIMEOUT_MS = 250

_STATE_COMMAND_LIMIT = 300

HIGH_RISK_LEVELS = frozenset({
    PermissionRisk.SENSITIVE_MUTATION,
    PermissionRisk.CATASTROPHIC,
})

# 降级后的 reason：让审计/UI 能区分「用户确认」与「JEV 降级」
DOWNGRADE_REASON = "safety_decision_downgraded"

_SAFETY_QUESTION = {
    "type": "noul",
    "instructions": "该命令在用户当前授权的权限模式下直接执行是否安全？给出安全的概率。",
}


def safety_threshold() -> float:
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        raw = cfg["scenes"]["safety"]["threshold"]
    except Exception:
        return DEFAULT_SAFETY_THRESHOLD
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_SAFETY_THRESHOLD
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        return DEFAULT_SAFETY_THRESHOLD
    return value


def is_high_risk(risk) -> bool:
    """是否属于需要发起 safety 决策的高风险档位。"""
    return risk in HIGH_RISK_LEVELS


# ---------------------------------------------------------------------------
# state 构造（最小必要信息，绝不带隐私）
# ---------------------------------------------------------------------------

def _build_state(command: str, risk, mode: str) -> str:
    # 命令原文可能夹带密钥（`export KEY=sk-...` / `--token=...`），发出去前先脱敏
    from core.decision_gateway import redact_sensitive

    text = redact_sensitive(str(command or "")).strip()
    if len(text) > _STATE_COMMAND_LIMIT:
        text = text[:_STATE_COMMAND_LIMIT]
    if not text:
        text = "（未提供命令文本）"
    risk_value = getattr(risk, "value", risk)
    return f"命令：{text}\n风险类型：{risk_value}\n权限模式：{mode}"


def _decide_safety_probability(command: str, risk, mode: str):
    from core.decision_gateway import decide

    probability = decide(
        "safety",
        _build_state(command, risk, mode),
        dict(_SAFETY_QUESTION),
        timeout_ms=SAFETY_DECISION_TIMEOUT_MS,
        threshold=0.0,
    )
    if isinstance(probability, bool) or not isinstance(probability, (int, float)):
        return None
    return float(probability)


def apply_decision_to_permission(facts, decision, *, command: str = ""):
    try:
        # 第一道闸（安全核心）：非 ALLOW 一律不进入决策比较。
        # 这里直接返回原对象，连概率读取都不做——从结构上保证「不可能放宽」。
        if decision is None or decision.action is not PermissionAction.ALLOW:
            return decision
        risk = getattr(facts, "risk", None)
        if not is_high_risk(risk):
            return decision
        mode = str(getattr(facts, "mode", "") or "")
        probability = _decide_safety_probability(command, risk, mode)
        if probability is None:
            return decision
        if probability >= safety_threshold():
            return decision
        # 概率偏低：只降一级到 ASK（不降到 DENY，避免卡死正常工作流）。
        return PermissionDecision(PermissionAction.ASK, DOWNGRADE_REASON)
    except Exception as error:  # pragma: no cover - 兜底路径，正常不触发
        # 决策链路永远不能成为权限判定的故障点；debug 级别避免噪音。
        logger.debug("safety 场景决策失败，保持原判定: %s", error)
        return decision
