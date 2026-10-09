"""上下文水位基准（memory_compression.watermark_base_tokens）的校验 / 读取 / 保存。

键语义：会话上下文占用达到 min(模型窗口, watermark_base_tokens) 的比例线时触发维护
（60% snip / 80% elide / 85% 后台预压缩 / 95% LLM 摘要）；不设基准（None / 0 / 空串）
时按真实模型窗口的比例线计算。

读取语义（model_watermark_bases 按模型覆盖全局值）仍由
core/memory_compressor.get_watermark_base_tokens() 与 _load_compression_config() 提供，
本模块只负责该配置项的严格校验与安全写入，不改动那两个读取方的实现。
"""

from __future__ import annotations

import config


# config.json 中该配置项的点分路径（两层嵌套）
WATERMARK_BASE_CONFIG_PATH = "memory_compression.watermark_base_tokens"
# 合法区间（tokens）：低于下限没有意义（不如直接跟随真实窗口），高于上限则相当于永不触发
MIN_WATERMARK_BASE = 50000
MAX_WATERMARK_BASE = 2000000


class WatermarkBaseError(ValueError):
    """水位基准配置非法，或保存失败。"""


def validate_watermark_base(value) -> int | None:
    """校验水位基准：None / 0 / 空串 → None（不设基准、跟随真实窗口）。

    区间内整数（50000..2000000）→ 返回该整数；数字字符串按整数处理。
    bool、非整数、越界、非法字符串 → 抛 WatermarkBaseError。
    写法参照 core/harness/turn_limits.py 的 validate_turn_step_limit。
    """
    # bool 是 int 的子类，必须先挡掉，否则 True 会被当成 1 静默通过
    if isinstance(value, bool):
        raise WatermarkBaseError("上下文水位基准必须是整数")
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 只接受纯整数字面量："400000" 合法，"abc"/"1.5" 非法
        if not text.lstrip("+-").isdigit():
            raise WatermarkBaseError(f"上下文水位基准必须是整数，收到 {value!r}")
        value = int(text)
    elif not isinstance(value, int):
        raise WatermarkBaseError(f"上下文水位基准必须是整数，收到 {value!r}")
    if value == 0:
        # 0 与 None 同义：不设基准
        return None
    if not MIN_WATERMARK_BASE <= value <= MAX_WATERMARK_BASE:
        raise WatermarkBaseError(
            f"上下文水位基准必须在 {MIN_WATERMARK_BASE}–{MAX_WATERMARK_BASE} 之间，收到 {value}"
        )
    return value


def load_watermark_base() -> int | None:
    """读取已保存的水位基准；未配置或配置非法时返回 None。

    读取路径与 memory_compressor 的口径一致：非法值绝不抛错，只按「不设基准」处理
    并留下运行告警（配置写坏不能让压缩维护整体崩掉）。
    """
    try:
        data = config._load()
        section = data.get("memory_compression") if isinstance(data, dict) else None
        if not isinstance(section, dict) or "watermark_base_tokens" not in section:
            return None
        value = section["watermark_base_tokens"]
    except Exception as error:
        config._log_warning("读取 %s 失败（%s），按不设基准处理", WATERMARK_BASE_CONFIG_PATH, error)
        return None
    try:
        return validate_watermark_base(value)
    except WatermarkBaseError as error:
        config._log_warning(
            "config.json 中 %s 非法（%s），按不设基准处理",
            WATERMARK_BASE_CONFIG_PATH,
            error,
        )
        return None


def save_watermark_base(value) -> int | None:
    """校验并保存水位基准（写 config.json 的 memory_compression.watermark_base_tokens）。

    返回实际生效值（None 表示不设基准）；写盘失败抛 WatermarkBaseError。
    """
    resolved = validate_watermark_base(value)
    # 不设基准时写 0 而不是 null：配置文件里该键始终是数值，读取方对 0 与缺失
    # 都回落 None（见 memory_compressor._positive_int_or_none）
    stored = resolved if resolved is not None else 0
    try:
        saved = config.save_nested_config_value(WATERMARK_BASE_CONFIG_PATH, stored)
    except Exception as error:
        raise WatermarkBaseError(f"上下文水位基准保存失败: {error}") from error
    if not saved:
        raise WatermarkBaseError("上下文水位基准保存失败")
    return resolved
