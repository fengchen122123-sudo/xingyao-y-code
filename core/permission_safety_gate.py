"""safety 场景接入：把「高风险命令放行概率」叠到权限判定之上的**薄包装层**。

设计依据：docs/superpowers/specs/2026-10-18-jev-decision-integration.md 第 3/4 节。

为什么不写进 ``core.permission_policy.decide_permission``：
那个函数是**纯函数**（无 IO、可穷举单测），把网络决策塞进去会毁掉这个性质。
因此判定规则一行不改，网络 I/O 全部留在这里 + ``core.decision_gateway``。

安全原则（硬性，任何改动都不得放松）：

1. JEV 只能**加严**，不能放宽——
   原判定不是 ALLOW（ask / deny）时，JEV 即便给出 0.99 的放行概率，本模块
   也直接把原判定原样返回，不含任何比较逻辑；
2. 原判定是 ALLOW 时，只有 JEV 给出**低于**阈值的安全概率，才降级为 ASK
   （不是 DENY，避免把正常工作流直接卡死）；
3. 因此 JEV 唯一的权力是「allow → ask」，除此之外什么都不改：返回的
   ``PermissionDecision`` 只可能是原对象，或一个 reason 为
   ``safety_decision_downgraded`` 的 ASK。

此外：
- 只在**高风险**操作上发请求，低风险（如 QUERY / NORMAL_MUTATION）零请求、零延迟；
- 网关在 off / shadow / 失败 / 答案非法时一律返回 None，本模块对 None 直接
  静默返回原判定（连日志都不打，避免刷屏噪音）；
- 任何异常一律吞掉，保持原判定。
"""

from __future__ import annotations

import logging

from core.permission_policy import (
    PermissionAction,
    PermissionDecision,
    PermissionRisk,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量（与 decision_service.DEFAULT_CONFIG["scenes"]["safety"] 对齐）
# ---------------------------------------------------------------------------

# safety 场景的默认阈值：安全概率 >= 0.9 才认为「可以直接执行」
DEFAULT_SAFETY_THRESHOLD = 0.9

# safety 场景单次决策超时（毫秒）：真实决策宁可不用，也不拖慢权限流程
SAFETY_DECISION_TIMEOUT_MS = 250

# state 里命令文本的截断长度（隐私与体积双约束）
_STATE_COMMAND_LIMIT = 300

# 被判定为「高风险」的档位：只有这些才值得问一次决策模型。
# CATASTROPHIC 在纯函数里恒为 DENY，本模块天然不会放宽它（见 apply_... 的早退）。
HIGH_RISK_LEVELS = frozenset({
    PermissionRisk.SENSITIVE_MUTATION,
    PermissionRisk.CATASTROPHIC,
})

# 降级后的 reason：让审计/UI 能区分「用户确认」与「JEV 降级」
DOWNGRADE_REASON = "safety_decision_downgraded"

# 单题决策：noul（概率）题
_SAFETY_QUESTION = {
    "type": "noul",
    "instructions": "该命令在用户当前授权的权限模式下直接执行是否安全？给出安全的概率。",
}


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def safety_threshold() -> float:
    """读取 ``scenes.safety.threshold``；缺失/损坏/越界一律回落 0.9。"""
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
    """构造决策 state：**只有**命令文本（截断 300 字符）+ 风险类型 + 权限模式。

    刻意不拼接：会话历史、文件内容、模型输出、密钥、环境变量值——
    这段文本会真实发送给决策模型。
    """
    # 命令原文可能夹带密钥（`export KEY=sk-...` / `--token=...`），发出去前先脱敏
    from core.decision_gateway import redact_sensitive

    text = redact_sensitive(str(command or "")).strip()
    if len(text) > _STATE_COMMAND_LIMIT:
        text = text[:_STATE_COMMAND_LIMIT]
    if not text:
        text = "（未提供命令文本）"
    risk_value = getattr(risk, "value", risk)
    return f"命令：{text}\n风险类型：{risk_value}\n权限模式：{mode}"


# ---------------------------------------------------------------------------
# 决策请求
# ---------------------------------------------------------------------------

def _decide_safety_probability(command: str, risk, mode: str):
    """向网关要一次「安全概率」；返回 float 或 None（未启用/影子/失败/非法）。

    惰性导入 ``decide``，避免 permission 相关模块在导入期就拉起决策链路。

    ``threshold=0.0``：本场景要自己按 0.9 判断「概率偏低 → 降级」，因此必须
    拿到原始概率；若沿用网关默认门槛，低概率答案会被网关当成「无效答案」
    吞成 None，降级逻辑就永远走不到（等于接入无效）。
    """
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


# ---------------------------------------------------------------------------
# 对外唯一入口：把决策叠到纯函数判定结果上
# ---------------------------------------------------------------------------

def apply_decision_to_permission(facts, decision, *, command: str = ""):
    """在 ``decide_permission`` 的结果上叠加 safety 决策。

    参数：
    - ``facts``：``PermissionFacts``（只用 mode / risk）；
    - ``decision``：``decide_permission(facts)`` 的返回值；
    - ``command``：被授权的命令文本（非命令类工具可留空）。

    返回：原 ``decision`` 对象，或降级后的 ASK 判定。**永远不会**返回比原判定
    更宽松的结果（代码层面只有 ALLOW → ASK 一条转移边）。

    任何异常/None/非法值一律原样返回 ``decision``。
    """
    try:
        # 第一道闸（安全核心）：非 ALLOW 一律不进入决策比较。
        # 这里直接返回原对象，连概率读取都不做——从结构上保证「不可能放宽」。
        if decision is None or decision.action is not PermissionAction.ALLOW:
            return decision
        # 第二道闸：低风险操作不值得问，零请求、零延迟。
        risk = getattr(facts, "risk", None)
        if not is_high_risk(risk):
            return decision
        mode = str(getattr(facts, "mode", "") or "")
        probability = _decide_safety_probability(command, risk, mode)
        # None = JEV 未启用 / 影子 / 上游失败 / 答案非法：静默保持原判定，不打日志。
        if probability is None:
            return decision
        # 概率达到阈值：维持 allow。
        if probability >= safety_threshold():
            return decision
        # 概率偏低：只降一级到 ASK（不降到 DENY，避免卡死正常工作流）。
        return PermissionDecision(PermissionAction.ASK, DOWNGRADE_REASON)
    except Exception as error:  # pragma: no cover - 兜底路径，正常不触发
        # 决策链路永远不能成为权限判定的故障点；debug 级别避免噪音。
        logger.debug("safety 场景决策失败，保持原判定: %s", error)
        return decision
