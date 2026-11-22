from __future__ import annotations

from typing import Any

CONFIG_KEY = "subagent_enabled"
DEFAULT_SUBAGENT_ENABLED = True

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on", "enable", "enabled"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", "disable", "disabled"})


class SubagentSettingError(Exception):
    """子代理开关保存失败或入参非法（消息为中文原因）。"""


def _normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):  # 注意 bool 是 int 子类，已在上一行拦下
        if value in (0, 1):
            return bool(value)
        raise SubagentSettingError(f"子代理开关只接受布尔值，收到整数 {value}")
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE_STRINGS:
            return True
        if text in _FALSE_STRINGS:
            return False
        raise SubagentSettingError(f"子代理开关只接受布尔值，收到字符串 {value!r}")
    raise SubagentSettingError(f"子代理开关只接受布尔值，收到 {type(value).__name__}")


def subagent_enabled() -> bool:
    """读取全局子代理开关。

    配置缺失返回 True；值不是布尔时返回 True（非法值不改变既有行为）。
    读取异常（文件损坏、依赖缺失）兜底为 True，绝不在读路径抛错。
    """
    try:
        import config as _config

        data = _config._load()
        if not isinstance(data, dict):
            return DEFAULT_SUBAGENT_ENABLED
        value = data.get(CONFIG_KEY)
        if isinstance(value, bool):
            return value
        return DEFAULT_SUBAGENT_ENABLED
    except Exception:  # noqa: BLE001 - 读路径不允许影响主流程
        return DEFAULT_SUBAGENT_ENABLED


def set_subagent_enabled(value: bool) -> bool:
    bool_value = _normalize_bool(value)
    try:
        import config as _config

        saved = _config.save_config_value(CONFIG_KEY, bool_value)
    except SubagentSettingError:
        raise
    except Exception as error:  # noqa: BLE001 - 统一转成中文可见错误
        raise SubagentSettingError(f"子代理开关保存失败（{error}）") from error
    if not saved:
        raise SubagentSettingError("子代理开关保存失败（config.json 写入失败）")
    return bool_value


def orchestration_allowed(base_allowed: bool = True) -> bool:
    return bool(base_allowed) and subagent_enabled()