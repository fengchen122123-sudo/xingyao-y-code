from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SCENE = "router"

MAIN_OPTION = "main"
SECONDARY_OPTION = "secondary"
ROUTER_OPTIONS = (MAIN_OPTION, SECONDARY_OPTION)

ROUTER_TIMEOUT_MS = 1500

STATE_CHAR_LIMIT = 500

ROUTER_QUESTION = {
    "type": "choice",
    "instructions": (
        "判断这一回合该用哪个模型：main=能力更强但更贵，适合复杂推理/大范围改代码/"
        "多文件任务；secondary=更便宜，适合简单问答、单点修改、格式转换、闲聊。"
    ),
    "criteria": {
        MAIN_OPTION: "能力更强但更贵，适合复杂推理、大范围改代码、多文件任务",
        SECONDARY_OPTION: "更便宜，适合简单问答、单点修改、格式转换、闲聊",
    },
}


def _state_text(text) -> str:
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    # 用户粘贴的代码/命令里可能带密钥，发给决策模型前先过一遍脱敏
    from core.decision_gateway import redact_sensitive

    return redact_sensitive(collapsed[:STATE_CHAR_LIMIT])


def _positive_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def router_scene_live() -> bool:
    try:
        from core import decision_service

        cfg = decision_service.load_decision_config()
        if not isinstance(cfg, dict) or not bool(cfg.get("enabled")):
            return False
        if bool(cfg.get("shadow_mode")):
            return False
        scenes = cfg.get("scenes")
        if not isinstance(scenes, dict):
            return False
        scene_cfg = scenes.get(SCENE)
        if not isinstance(scene_cfg, dict) or not bool(scene_cfg.get("enabled")):
            return False
        return not bool(scene_cfg.get("shadow", True))
    except Exception:
        # 配置读不出来就当没开：宁可不用决策，也不能影响回合
        return False


def resolve_turn_model(
    active_model: dict,
    *,
    turn_source: str = "local",
    pending_model_id: str = "",
    baseline_tokens: int = 0,
    user_text: str = "",
    has_image: bool = False,
) -> dict | None:
    try:
        if str(turn_source or "") != "local":
            return None
        if str(pending_model_id or "").strip():
            return None
        if not isinstance(active_model, dict) or not active_model:
            return None
        main_litellm = str(active_model.get("litellm_model") or "").strip()
        if not main_litellm:
            return None
        state = _state_text(user_text)
        if not state:
            return None

        from core.model_manager import get_secondary_model

        secondary = get_secondary_model()
        if not isinstance(secondary, dict) or not secondary:
            return None
        secondary_litellm = str(secondary.get("litellm_model") or "").strip()
        secondary_id = str(secondary.get("id") or "").strip()
        if not secondary_litellm or not secondary_id:
            return None
        if secondary_litellm == main_litellm:
            return None
        # 关键字段缺失：宁可不覆盖，也不能拼出半套参数发请求
        secondary_key = str(secondary.get("api_key") or "").strip()
        secondary_base = str(secondary.get("base_url") or "").strip()
        if not secondary_key or not secondary_base:
            return None
        secondary_window = _positive_int(secondary.get("context_window"))
        if secondary_window <= 0:
            return None

        baseline = _positive_int(baseline_tokens)
        if baseline > 0 and secondary_window < baseline:
            logger.info(
                "router 场景跳过覆盖：会话上下文基线 %s tokens 超过副模型窗口 %s tokens",
                baseline,
                secondary_window,
            )
            return None

        # 能力硬护栏：本轮带图时，只有副模型确实支持视觉才允许覆盖。
        # 否则切过去等于把用户的图片丢掉（视觉链路会退化成「无法分析图片」的占位
        # 提示），省下的那点钱完全不值得。视觉能力判定失败一律保守放弃覆盖。
        if has_image:
            try:
                from core.llm_dialects import _supports_vision_llm_first

                secondary_can_see = bool(_supports_vision_llm_first(secondary_litellm))
            except Exception:
                secondary_can_see = False
            if not secondary_can_see:
                logger.info(
                    "router 场景跳过覆盖：本轮含图片，副模型 %s 不支持视觉输入",
                    secondary_id,
                )
                return None

        from core.decision_gateway import decide

        picked = decide(
            SCENE,
            state,
            dict(ROUTER_QUESTION),
            options=ROUTER_OPTIONS,
            timeout_ms=ROUTER_TIMEOUT_MS,
            audit_extra={
                "turn_source": "local",
                "baseline_tokens": baseline,
                "has_image": bool(has_image),
            },
        )
        if picked != SECONDARY_OPTION:
            return None

        return {
            "litellm_model": secondary_litellm,
            "api_key": secondary_key,
            "base_url": secondary_base,
            "context_window": secondary_window,
            "model_id": secondary_id,
            "name": str(secondary.get("name") or "").strip(),
        }
    except Exception as error:
        # 决策模型永远不能成为主流程的故障点：任何异常都退回「沿用主模型」
        logger.debug("router 场景模型档位判定失败，本回合沿用主模型: %s", error)
        return None
