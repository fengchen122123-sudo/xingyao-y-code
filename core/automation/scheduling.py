from __future__ import annotations

from datetime import datetime, timedelta

TRIGGER_TYPES = ("interval", "daily", "weekly", "startup", "once")

MAX_INTERVAL_MINUTES = 10080

RUN_AT_FORMAT = "%Y-%m-%d %H:%M"


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


def parse_run_at(value) -> float:
    if not isinstance(value, str):
        raise ValueError("时刻必须是 YYYY-MM-DD HH:MM 字符串")
    text = value
    if (
        len(text) != 16
        or text[4] != "-"
        or text[7] != "-"
        or text[10] != " "
        or text[13] != ":"
    ):
        raise ValueError(f"时刻格式非法: {value!r}（应为 YYYY-MM-DD HH:MM）")
    digits = text[:4] + text[5:7] + text[8:10] + text[11:13] + text[14:16]
    if not digits.isascii() or not digits.isdigit():
        raise ValueError(f"时刻格式非法: {value!r}（应为 YYYY-MM-DD HH:MM）")
    try:
        parsed = datetime.strptime(text, RUN_AT_FORMAT)
    except ValueError as exc:
        raise ValueError(f"时刻非法: {value!r}（{exc}）") from exc
    return parsed.timestamp()


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
    elif trigger_type == "once":
        has_delay = config.get("delay_minutes") is not None
        has_run_at = config.get("run_at") is not None
        if has_delay == has_run_at:
            errors.append("once 触发器必须且只能提供 delay_minutes 或 run_at 之一")
        elif has_delay:
            value = config.get("delay_minutes")
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append("一次性触发延迟（delay_minutes）必须是整数分钟")
            elif not 1 <= value <= MAX_INTERVAL_MINUTES:
                errors.append(
                    f"一次性触发延迟（delay_minutes）必须在 1..{MAX_INTERVAL_MINUTES} 分钟之间"
                )
        else:
            try:
                parse_run_at(config.get("run_at"))
            except ValueError as exc:
                errors.append(f"一次性触发时刻非法：{exc}")
    return errors


def compute_next_run(trigger_type: str, config: dict, now_ts: float) -> float | None:
    if trigger_type == "startup":
        return None
    if trigger_type == "once":
        if config.get("run_at") is not None:
            return parse_run_at(config["run_at"])
        delay_minutes = config.get("delay_minutes")
        if isinstance(delay_minutes, bool) or not isinstance(delay_minutes, int):
            raise ValueError("once 触发器缺少合法的 run_at 或 delay_minutes")
        return now_ts + delay_minutes * 60
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
