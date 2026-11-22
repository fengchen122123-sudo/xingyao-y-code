from __future__ import annotations

import logging
import re

from core import decision_service

logger = logging.getLogger(__name__)

SCENES = ("router", "secretary", "search", "safety")

TASK_SCENES = (
    "loop_guard",
    "subagent_review",
    "plan_recovery",
    "memory_filter",
    "auto_permission",
    "auto_question",
)
ALL_SCENES = SCENES + TASK_SCENES

# 单题决策的超时兜底：真实决策宁可不用，也不能拖慢主流程
DEFAULT_TIMEOUT_MS = 250
# 任务执行期决策超时：实测 JEV 单次约 600~900ms，留足余量以免每次超时回退
TASK_DEFAULT_TIMEOUT_MS = 1500

DEFAULT_THRESHOLD = 0.5

_MISSING = object()

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
    if not isinstance(text, str):
        return ""
    value = text
    for pattern, replacement in _REDACT_PATTERNS:
        value = pattern.sub(replacement, value)
    return value



def _scene_config(cfg: dict, scene: str) -> dict | None:
    scenes = cfg.get("scenes")
    if not isinstance(scenes, dict):
        return None
    scene_cfg = scenes.get(scene)
    if not isinstance(scene_cfg, dict):
        return None
    return scene_cfg


def _resolve_threshold(scene_cfg: dict, override=None) -> float:
    raw = override if override is not None else scene_cfg.get("threshold", DEFAULT_THRESHOLD)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return DEFAULT_THRESHOLD
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        return DEFAULT_THRESHOLD
    return value


def _resolve_timeout_ms(scene_cfg: dict, override, default_timeout_ms: int = DEFAULT_TIMEOUT_MS) -> int:
    value = override
    if value is None:
        value = scene_cfg.get("timeout_ms")
    if value is None or isinstance(value, bool):
        value = default_timeout_ms
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = default_timeout_ms
    low = decision_service.MIN_TIMEOUT_MS
    high = decision_service.MAX_TIMEOUT_MS
    return max(low, min(high, timeout))


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
        return text
    for option in candidates:
        if _as_text(option) == text:
            return option
    return _MISSING


def _criteria_object(criteria, options) -> dict:
    if isinstance(criteria, dict) and criteria:
        return {str(name): str(desc) for name, desc in criteria.items()}
    items = criteria if isinstance(criteria, (list, tuple)) else []
    if not items:
        items = list(options) if isinstance(options, (list, tuple)) else []
    normalized: dict = {}
    for item in items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("id") or item.get("choice")
            if name is None:
                continue
            description = item.get("description") or item.get("desc") or name
            normalized[str(name)] = str(description)
        else:
            normalized[str(item)] = str(item)
    return normalized


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


def decide(scene, state, question, *, options=None, timeout_ms=None, threshold=None,
           audit_extra=None, session_id=None):
    try:
        if not isinstance(scene, str) or scene not in ALL_SCENES:
            return None
        if not isinstance(question, dict):
            return None
        question_type = question.get("type")
        if question_type not in ("choice", "noul"):
            return None

        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return None
        is_task_scene = scene in TASK_SCENES
        scene_cfg = _scene_config(cfg, scene)
        if is_task_scene:
            if scene_cfg is not None and not bool(scene_cfg.get("enabled", True)):
                return None
            effective_scene_cfg = scene_cfg if scene_cfg is not None else {}
        else:
            if scene_cfg is None or not bool(scene_cfg.get("enabled")):
                return None
            effective_scene_cfg = scene_cfg

        if question_type == "choice":
            question = dict(question)
            question["criteria"] = _criteria_object(question.get("criteria"), options)
            if not question["criteria"]:
                return None

        if is_task_scene:
            shadow = bool(cfg.get("shadow_mode")) or bool(effective_scene_cfg.get("shadow", False))
            raw_global_timeout = cfg.get("timeout_ms")
            if (
                not isinstance(raw_global_timeout, bool)
                and isinstance(raw_global_timeout, (int, float))
                and int(raw_global_timeout) > TASK_DEFAULT_TIMEOUT_MS
            ):
                fallback_timeout = int(raw_global_timeout)
            else:
                fallback_timeout = TASK_DEFAULT_TIMEOUT_MS
            effective_timeout = _resolve_timeout_ms(
                effective_scene_cfg, timeout_ms, default_timeout_ms=fallback_timeout
            )
        else:
            shadow = bool(cfg.get("shadow_mode")) or bool(effective_scene_cfg.get("shadow", True))
            effective_timeout = _resolve_timeout_ms(effective_scene_cfg, timeout_ms)
        ask_kwargs = {
            "scene": scene,
            "state": state,
            "questions": {"q": question},
            "timeout_ms": effective_timeout,
            "audit_extra": audit_extra,
        }
        if session_id is not None:
            ask_kwargs["session_id"] = session_id
        result = decision_service.ask_decision(**ask_kwargs)
        if shadow:
            return None
        if not isinstance(result, dict):
            return None

        answer = _extract_answer(result)
        if question_type == "choice":
            picked = _pick_choice(answer, options)
        else:
            picked = _pick_noul(answer, _resolve_threshold(effective_scene_cfg, threshold))
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
