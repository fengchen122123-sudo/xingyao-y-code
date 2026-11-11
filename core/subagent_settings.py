"""全局子代理开关（设置页「子代理」）的读写与统一判定。

用途：给「子代理」一个进程级单点开关，关闭后核心链路（运行上下文注入、
工具注册、编排入口）统一按关闭处理，而不是各处自己拼条件判断。

设计约束：
- 配置键固定 ``subagent_enabled``，值必须是布尔：缺省 = True（保持既有行为），
  读到非布尔（手改 config.json 写成 "false"/0 之类）也按 True 处理——
  非法值不得静默改变既有行为；
- 读取路径绝不抛异常：配置文件损坏/读失败一律兜底为 True；
- 写入路径真正落盘（config.save_config_value），失败抛 SubagentSettingError，
  由设置页/工具层转成可见错误；
- 本模块不得 import gui_bridge / main / pywebview（与 core 层其它设置模块一致）。
"""

from __future__ import annotations

from typing import Any

CONFIG_KEY = "subagent_enabled"
DEFAULT_SUBAGENT_ENABLED = True

# 写入时接受的字符串写法（大小写不敏感）；其它任何取值都视为非法入参。
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on", "enable", "enabled"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off", "disable", "disabled"})


class SubagentSettingError(Exception):
    """子代理开关保存失败或入参非法（消息为中文原因）。"""


def _normalize_bool(value: Any) -> bool:
    """把入参规范化为布尔；无法确定时抛 SubagentSettingError。

    只接受：布尔、整数 0/1、常见真/假字符串。不做真值语义猜测
    （None、空串、2、任意对象都算非法），避免"看起来成功了却写反"。
    """
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
    """写入全局子代理开关并持久化到 config.json。

    成功返回写入的布尔值；入参非法或写入失败抛 SubagentSettingError（中文原因）。
    """
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
    """子代理与计划编排是否可用：base_allowed 且全局开关开启。

    核心链路的统一判定入口（禁止各处自己拼 ``x and subagent_enabled()``）。
    """
    return bool(base_allowed) and subagent_enabled()