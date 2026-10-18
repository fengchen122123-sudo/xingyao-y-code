"""JEV 决策网关：业务场景接入决策模型的**唯一入口**。

设计依据：docs/superpowers/specs/2026-10-18-jev-decision-integration.md。

职责边界：
- ``core.decision_service`` 只负责「发请求 + 写审计」；
- 本模块在其上补齐三态闸门（off / shadow / live）、答案校验（choice 候选、
  noul 阈值）、超时钳制与采纳回填（``mark_adopted``）；
- 调用方拿到 ``None`` 时必须走接入前的原逻辑——**未启用 / 影子 / 低置信 /
  答案非法 / 任何异常一律返回 None**，绝不向业务代码抛异常，也绝不拖慢主流程。

硬性约束：
- 不 import gui_bridge / webview，只依赖 core.decision_service 与标准库；
- 未启用时零网络请求、零审计副作用；
- 本模块自身不记录任何敏感内容（state / 答案交由 decision_service 审计层处理）。
"""

from __future__ import annotations

import logging
import re

from core import decision_service

# 模块级 logger（标准库，不违反「只依赖 decision_service + 标准库」的约束）
logger = logging.getLogger(__name__)

# 允许接入的四个业务场景（与 DEFAULT_CONFIG["scenes"] 对齐）
SCENES = ("router", "secretary", "search", "safety")

# 单题决策的超时兜底：真实决策宁可不用，也不能拖慢主流程
DEFAULT_TIMEOUT_MS = 250

# noul 题缺省阈值（配置缺失时使用）
DEFAULT_THRESHOLD = 0.5

# 网关内部的「没有合法答案」标记（区分于合法的 None/False 之类的答案值）
_MISSING = object()

# 上游答案里包裹答案值的候选字段名（形状容错用）
_VALUE_KEYS = ("choice", "noul", "answer", "value", "probability", "prob")


# ---------------------------------------------------------------------------
# 隐私脱敏：所有进入 state 的业务文本都必须先过这一层
# ---------------------------------------------------------------------------

# 常见密钥/口令形态：sk-xxx、Bearer xxx、api_key=xxx、password: xxx 等。
# 审计只记录 state 的 sha 摘要，但**请求体是真的发给决策模型的**，
# 所以脱敏必须发生在构造 state 时，而不是事后。
_REDACT_PATTERNS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{6,}"), "Bearer ***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|token)"
            r"([\"']?\s*[:=]\s*[\"']?)(\S{4,})"
        ),
        r"\1\2***",
    ),
)


def redact_sensitive(text) -> str:
    """把文本里的疑似密钥/口令替换成 ***；非字符串返回空串。

    ``router``（用户输入）与 ``safety``（命令原文）会把原始文本发给决策模型，
    其中可能夹带 ``export KEY=sk-...``、``Authorization: Bearer ...`` 之类的
    内容。这里做一次保守替换：命中常见的密钥形态就抹掉值，其余文本原样保留，
    保证决策模型仍能判断语义。
    """
    if not isinstance(text, str):
        return ""
    value = text
    for pattern, replacement in _REDACT_PATTERNS:
        value = pattern.sub(replacement, value)
    return value



# ---------------------------------------------------------------------------
# 配置读取（scene 级三态 + 阈值 + 超时）
# ---------------------------------------------------------------------------

def _scene_config(cfg: dict, scene: str) -> dict | None:
    """取 scene 配置字典；缺失或形状异常返回 None（=未启用）。"""
    scenes = cfg.get("scenes")
    if not isinstance(scenes, dict):
        return None
    scene_cfg = scenes.get(scene)
    if not isinstance(scene_cfg, dict):
        return None
    return scene_cfg


def _resolve_threshold(scene_cfg: dict, override=None) -> float:
    """读取答案门槛；优先级：调用方 override > scene.threshold > DEFAULT_THRESHOLD。

    ``override`` 是给「需要拿到原始概率自行判断」的调用方留的窄口子
    （如 safety 场景：它要把「安全概率偏低」解读为降级信号，而不是当作
    无效答案丢弃）。传 None 时行为与旧版完全一致。
    """
    raw = override if override is not None else scene_cfg.get("threshold", DEFAULT_THRESHOLD)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_THRESHOLD
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        return DEFAULT_THRESHOLD
    return value


def _resolve_timeout_ms(scene_cfg: dict, override) -> int:
    """超时优先级：调用方 override > scene.timeout_ms > 250ms，再钳制到服务边界。

    MIN/MAX 现取 ``decision_service`` 上的常量（便于测试替换与统一维护）。
    """
    value = override
    if value is None:
        value = scene_cfg.get("timeout_ms")
    if value is None or isinstance(value, bool):
        value = DEFAULT_TIMEOUT_MS
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_MS
    low = decision_service.MIN_TIMEOUT_MS
    high = decision_service.MAX_TIMEOUT_MS
    return max(low, min(high, timeout))


# ---------------------------------------------------------------------------
# 答案提取与校验（形状容错：{"choice": "x"} / {"noul": 0.9} / 裸值）
# ---------------------------------------------------------------------------

def _as_text(value):
    """把候选值转为去空白文本；不可转（None/容器等）返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _unwrap(payload):
    """剥掉答案外层包装：{"choice": "x"} → "x"；裸值原样返回。"""
    if isinstance(payload, dict):
        for key in _VALUE_KEYS:
            if key in payload:
                return payload[key]
        if len(payload) == 1:
            return next(iter(payload.values()))
        return _MISSING
    if isinstance(payload, (list, tuple)):
        if len(payload) == 1:
            return _unwrap(payload[0])
        return _MISSING
    return payload


def _extract_answer(result: dict):
    """从 ask_decision 结果里提取本题答案；键名优先 "q"，否则取唯一一项。"""
    answers = result.get("answers")
    if answers is None:
        return _MISSING
    if not isinstance(answers, dict):
        # 上游直接返回裸值（无 answers 包装）
        return _unwrap(answers)
    if "q" in answers:
        payload = answers["q"]
    elif len(answers) == 1:
        payload = next(iter(answers.values()))
    else:
        return _MISSING
    return _unwrap(payload)


def _pick_choice(answer, options):
    """choice 题：答案必须（去空白后）严格等于 options 中某一项。"""
    if answer is _MISSING:
        return _MISSING
    text = _as_text(answer)
    if text is None:
        return _MISSING
    candidates = (
        list(options) if isinstance(options, (list, tuple, set, frozenset)) else []
    )
    if not candidates:
        # 未提供候选：不做校验，按文本返回
        return text
    for option in candidates:
        if _as_text(option) == text:
            # 选中项原样返回（保留调用方给的类型，如 int 档位/条数）
            return option
    return _MISSING


def _pick_noul(answer, threshold: float):
    """noul 题：概率必须是 0~1 的数字且 >= threshold。"""
    if answer is _MISSING:
        return _MISSING
    if isinstance(answer, bool):
        return _MISSING
    value = answer
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except (TypeError, ValueError):
            return _MISSING
    if not isinstance(value, (int, float)):
        return _MISSING
    probability = float(value)
    if not 0.0 <= probability <= 1.0:
        return _MISSING
    if probability < threshold:
        return _MISSING
    return probability


# ---------------------------------------------------------------------------
# 对外唯一入口
# ---------------------------------------------------------------------------

def decide(scene, state, question, *, options=None, timeout_ms=None, threshold=None,
           audit_extra=None):
    """单题决策。

    参数：
    - ``scene``：``"router"`` / ``"secretary"`` / ``"search"`` / ``"safety"``；
    - ``state``：交给模型的上下文文本；
    - ``question``：单题 dict，``{"type": "choice", ...}`` 或 ``{"type": "noul", ...}``；
    - ``options``：choice 题的合法候选列表（为空则不做候选校验）；
    - ``timeout_ms``：调用方建议超时（会被钳制到 MIN_TIMEOUT_MS..MAX_TIMEOUT_MS）；
    - ``threshold``：noul 答案门槛覆盖（默认 None = 读 ``scenes.X.threshold``）。
      仅给「需要原始概率自行裁决」的调用方使用（safety）：它必须能看到低于
      门槛的概率值才能做出降级判断。传 None 时行为与旧版完全一致；
    - ``audit_extra``：附带的审计上下文（如启发式基线），不含敏感内容。

    返回：choice 题返回选中的候选（原样），noul 题返回 ``float`` 概率；
    未启用 / 影子 / 上游失败 / 答案非法 / 任何异常一律返回 ``None``。
    """
    try:
        # 1) 场景与题目形状先判（不做任何 IO）
        if not isinstance(scene, str) or scene not in SCENES:
            return None
        if not isinstance(question, dict):
            return None
        question_type = question.get("type")
        if question_type not in ("choice", "noul"):
            return None

        # 2) 三态闸门：off 直接返回（零请求、零副作用）
        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return None
        scene_cfg = _scene_config(cfg, scene)
        if scene_cfg is None or not bool(scene_cfg.get("enabled")):
            return None

        # 3) shadow：照旧发请求并写审计，但结果丢弃（影子只做观测）
        shadow = bool(cfg.get("shadow_mode")) or bool(scene_cfg.get("shadow", True))
        effective_timeout = _resolve_timeout_ms(scene_cfg, timeout_ms)
        result = decision_service.ask_decision(
            scene=scene,
            state=state,
            questions={"q": question},
            timeout_ms=effective_timeout,
            audit_extra=audit_extra,
        )
        if shadow:
            return None
        if not isinstance(result, dict):
            return None

        # 4) 答案校验：非法答案一律视为未采纳
        answer = _extract_answer(result)
        if question_type == "choice":
            picked = _pick_choice(answer, options)
        else:
            picked = _pick_noul(answer, _resolve_threshold(scene_cfg, threshold))
        if picked is _MISSING:
            return None

        # 5) 返回非 None 即视为采纳：回填审计。
        # 回填是旁路动作（审计 adopted 标记），失败只记日志——一次已经校验合法的
        # 决策不能因为审计文件被锁/磁盘异常而整条丢失，否则业务侧会莫名退回原逻辑。
        try:
            decision_service.mark_adopted(result.get("trace_id"))
        except Exception as exc:
            logger.warning("mark_adopted 回填审计失败（决策照常返回）: %s", exc)
        return picked
    except Exception:
        # 决策模型永远不能成为主流程的故障点
        return None
