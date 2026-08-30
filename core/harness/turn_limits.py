"""Strict per-Turn Provider Step limit configuration."""

from __future__ import annotations

import config


TURN_STEP_LIMIT_CONFIG_KEY = "agent_turn_step_limit"
DEFAULT_TURN_STEP_LIMIT = 200
MIN_TURN_STEP_LIMIT = 10
MAX_TURN_STEP_LIMIT = 1000


class TurnStepLimitError(ValueError):
    """Raised when the configured per-Turn Step limit is invalid."""


def validate_turn_step_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TurnStepLimitError("单任务最大模型轮次必须是整数")
    if not MIN_TURN_STEP_LIMIT <= value <= MAX_TURN_STEP_LIMIT:
        raise TurnStepLimitError(
            f"单任务最大模型轮次必须在 {MIN_TURN_STEP_LIMIT}–{MAX_TURN_STEP_LIMIT} 之间"
        )
    return value


def load_turn_step_limit() -> int:
    data = config._load()
    if TURN_STEP_LIMIT_CONFIG_KEY not in data:
        return DEFAULT_TURN_STEP_LIMIT
    return validate_turn_step_limit(data[TURN_STEP_LIMIT_CONFIG_KEY])


def save_turn_step_limit(value: object) -> int:
    resolved = validate_turn_step_limit(value)
    if not config.save_config_value(TURN_STEP_LIMIT_CONFIG_KEY, resolved):
        raise TurnStepLimitError("单任务最大模型轮次保存失败")
    return resolved
