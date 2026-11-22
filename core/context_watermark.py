from __future__ import annotations

import config


WATERMARK_BASE_CONFIG_PATH = "memory_compression.watermark_base_tokens"
MIN_WATERMARK_BASE = 50000
MAX_WATERMARK_BASE = 2000000


class WatermarkBaseError(ValueError):
    """水位基准配置非法，或保存失败。"""


def validate_watermark_base(value) -> int | None:
    # bool 是 int 的子类，必须先挡掉，否则 True 会被当成 1 静默通过
    if isinstance(value, bool):
        raise WatermarkBaseError("上下文水位基准必须是整数")
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if not text.lstrip("+-").isdigit():
            raise WatermarkBaseError(f"上下文水位基准必须是整数，收到 {value!r}")
        value = int(text)
    elif not isinstance(value, int):
        raise WatermarkBaseError(f"上下文水位基准必须是整数，收到 {value!r}")
    if value == 0:
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
    resolved = validate_watermark_base(value)
    stored = resolved if resolved is not None else 0
    try:
        saved = config.save_nested_config_value(WATERMARK_BASE_CONFIG_PATH, stored)
    except Exception as error:
        raise WatermarkBaseError(f"上下文水位基准保存失败: {error}") from error
    if not saved:
        raise WatermarkBaseError("上下文水位基准保存失败")
    return resolved
