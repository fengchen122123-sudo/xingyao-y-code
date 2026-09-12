"""触发器校验与下一次触发时间计算。

纯函数层：不 import GUI、数据库或引擎模块，可穷举测试。
时间一律使用本地墙钟（datetime.fromtimestamp / .timestamp()），
不做夏令时/时区切换补偿（设计规格 8 节的明确简化）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

TRIGGER_TYPES = ("interval", "daily", "weekly", "startup")

# 单次间隔上限：7 天（10080 分钟）。
MAX_INTERVAL_MINUTES = 10080


def parse_hhmm(value) -> tuple[int, int]:
    """严格解析 "HH:MM"（两位数、零填充、范围合法），非法抛 ValueError。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是 HH:MM 字符串")
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式非法: {value!r}")
    hour_text, minute_text = parts
    if (
        len(hour_text) != 2
        or len(minute_text) != 2
        or not hour_text.isdigit()
        or not minute_text.isdigit()
    ):
        raise ValueError(f"时间格式非法: {value!r}")
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围: {value!r}")
    return hour, minute


def validate_trigger(trigger_type, config) -> list[str]:
    """校验触发器配置，返回全部错误（空列表 = 合法）。"""
    if trigger_type not in TRIGGER_TYPES:
        return [f"未知触发器类型: {trigger_type!r}"]
    if not isinstance(config, dict):
        return ["触发器配置必须是对象"]
    errors: list[str] = []
    if trigger_type == "interval":
        value = config.get("interval_minutes")
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append("触发间隔（interval_minutes）必须是整数分钟")
        elif not 1 <= value <= MAX_INTERVAL_MINUTES:
            errors.append(
                f"触发间隔（interval_minutes）必须在 1..{MAX_INTERVAL_MINUTES} 分钟之间"
            )
    elif trigger_type == "daily":
        try:
            parse_hhmm(config.get("time"))
        except ValueError as exc:
            errors.append(f"每天触发时间非法：{exc}")
    elif trigger_type == "weekly":
        try:
            parse_hhmm(config.get("time"))
        except ValueError as exc:
            errors.append(f"每周触发时间非法：{exc}")
        weekdays = config.get("weekdays")
        if not isinstance(weekdays, list) or not weekdays:
            errors.append("weekdays 必须是非空的星期列表（1=周一 … 7=周日）")
        else:
            for weekday in weekdays:
                if isinstance(weekday, bool) or not isinstance(weekday, int) or not 1 <= weekday <= 7:
                    errors.append("weekdays 每项必须是 1..7 的整数")
                    break
    return errors


def compute_next_run(trigger_type: str, config: dict, now_ts: float) -> float | None:
    """计算下一次触发时刻（unix 秒，本地墙钟）。

    返回值必须严格大于 now_ts（interval/daily/weekly）；startup 无计划时刻返回
    None。未知类型显式抛 ValueError（调用方保证入参已经 validate_trigger）。
    """
    if trigger_type == "startup":
        return None
    if trigger_type == "interval":
        minutes = int(config["interval_minutes"])
        return now_ts + minutes * 60
    if trigger_type == "daily":
        hour, minute = parse_hhmm(config["time"])
        now = datetime.fromtimestamp(now_ts)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= now_ts:
            candidate += timedelta(days=1)
        return candidate.timestamp()
    if trigger_type == "weekly":
        hour, minute = parse_hhmm(config["time"])
        weekdays = {int(weekday) for weekday in config["weekdays"]}
        now = datetime.fromtimestamp(now_ts)
        for offset in range(8):
            candidate = (now + timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate.weekday() + 1 in weekdays and candidate.timestamp() > now_ts:
                return candidate.timestamp()
        return None
    raise ValueError(f"未知触发器类型: {trigger_type!r}")
