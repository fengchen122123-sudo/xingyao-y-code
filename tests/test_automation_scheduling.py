"""星轨自动化任务：触发器校验与下一次触发时间计算（纯函数层）。"""

from datetime import datetime

import pytest

from core.automation.scheduling import (
    TRIGGER_TYPES,
    compute_next_run,
    parse_hhmm,
    parse_run_at,
    validate_trigger,
)


def ts(**kwargs) -> float:
    return datetime(**kwargs).timestamp()


# ---------------------------------------------------------------------------
# parse_hhmm
# ---------------------------------------------------------------------------

def test_parse_hhmm_accepts_valid_values():
    assert parse_hhmm("00:00") == (0, 0)
    assert parse_hhmm("09:30") == (9, 30)
    assert parse_hhmm("23:59") == (23, 59)


@pytest.mark.parametrize("value", ["9:30", "09:3", "24:00", "12:60", "ab:cd", "", "12:30:00", " 9:30"])
def test_parse_hhmm_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_hhmm(value)


# ---------------------------------------------------------------------------
# validate_trigger
# ---------------------------------------------------------------------------

def test_validate_trigger_accepts_all_types():
    assert validate_trigger("interval", {"interval_minutes": 30}) == []
    assert validate_trigger("daily", {"time": "09:30"}) == []
    assert validate_trigger("weekly", {"time": "20:00", "weekdays": [1, 5]}) == []
    assert validate_trigger("startup", {}) == []


def test_validate_trigger_interval_bounds():
    errors = validate_trigger("interval", {})
    assert errors
    errors = validate_trigger("interval", {"interval_minutes": 0})
    assert any("interval_minutes" in e for e in errors)
    errors = validate_trigger("interval", {"interval_minutes": 10081})
    assert any("interval_minutes" in e for e in errors)
    errors = validate_trigger("interval", {"interval_minutes": 1.5})
    assert any("interval_minutes" in e for e in errors)
    # 上边界合法
    assert validate_trigger("interval", {"interval_minutes": 10080}) == []


def test_validate_trigger_daily_time():
    errors = validate_trigger("daily", {})
    assert errors
    errors = validate_trigger("daily", {"time": "25:00"})
    assert errors
    assert validate_trigger("daily", {"time": "00:05"}) == []


def test_validate_trigger_weekdays():
    errors = validate_trigger("weekly", {"time": "09:00"})
    assert errors  # weekdays 缺失
    errors = validate_trigger("weekly", {"time": "09:00", "weekdays": []})
    assert errors  # 空列表
    errors = validate_trigger("weekly", {"time": "09:00", "weekdays": [0, 8]})
    assert errors  # 越界（ISO 1..7）
    errors = validate_trigger("weekly", {"time": "09:00", "weekdays": ["1"]})
    assert errors  # 非整数
    assert validate_trigger("weekly", {"time": "09:00", "weekdays": [7]}) == []


def test_validate_trigger_unknown_type():
    assert validate_trigger("cron", {}) != []


# ---------------------------------------------------------------------------
# compute_next_run
# ---------------------------------------------------------------------------

def test_compute_next_run_interval_is_strictly_future():
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("interval", {"interval_minutes": 30}, now)
    assert nxt == pytest.approx(now + 30 * 60)


def test_compute_next_run_daily_same_day_future():
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("daily", {"time": "10:30"}, now)
    expected = ts(year=2026, month=9, day=12, hour=10, minute=30)
    assert nxt == pytest.approx(expected)


def test_compute_next_run_daily_past_goes_tomorrow():
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("daily", {"time": "09:30"}, now)
    expected = ts(year=2026, month=9, day=13, hour=9, minute=30)
    assert nxt == pytest.approx(expected)


def test_compute_next_run_daily_exact_minute_is_tomorrow():
    # 恰好等于当前时刻视为已过，取明天（严格递增，防止 0 秒死循环）
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("daily", {"time": "10:00"}, now)
    expected = ts(year=2026, month=9, day=13, hour=10, minute=0)
    assert nxt == pytest.approx(expected)
    assert nxt > now


def test_compute_next_run_weekly_same_day_later_time():
    # 2026-09-12 是周六（ISO weekday=6）
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("weekly", {"time": "20:00", "weekdays": [6]}, now)
    expected = ts(year=2026, month=9, day=12, hour=20, minute=0)
    assert nxt == pytest.approx(expected)


def test_compute_next_run_weekly_same_day_past_time_next_week():
    now = ts(year=2026, month=9, day=12, hour=20, minute=0)
    nxt = compute_next_run("weekly", {"time": "09:00", "weekdays": [6]}, now)
    expected = ts(year=2026, month=9, day=19, hour=9, minute=0)
    assert nxt == pytest.approx(expected)


def test_compute_next_run_weekly_wraps_to_next_week():
    # 周六 20:00，目标周一（weekday=1）
    now = ts(year=2026, month=9, day=12, hour=20, minute=0)
    nxt = compute_next_run("weekly", {"time": "09:00", "weekdays": [1]}, now)
    expected = ts(year=2026, month=9, day=14, hour=9, minute=0)
    assert nxt == pytest.approx(expected)


def test_compute_next_run_always_strictly_after_now():
    now = ts(year=2026, month=9, day=12, hour=23, minute=59, second=59)
    for trigger, config in (
        ("interval", {"interval_minutes": 1}),
        ("daily", {"time": "23:59"}),
        ("weekly", {"time": "23:59", "weekdays": [6]}),
    ):
        nxt = compute_next_run(trigger, config, now)
        assert nxt is not None and nxt > now, (trigger, nxt)


def test_compute_next_run_startup_has_no_schedule():
    assert compute_next_run("startup", {}, ts(year=2026, month=9, day=12, hour=10, minute=0)) is None


# ---------------------------------------------------------------------------
# once：类型注册与 parse_run_at
# ---------------------------------------------------------------------------

def test_once_is_registered_trigger_type():
    assert "once" in TRIGGER_TYPES
    assert isinstance(TRIGGER_TYPES, tuple)
    assert TRIGGER_TYPES[-1] == "once"


def test_parse_run_at_accepts_strict_format():
    parsed = parse_run_at("2026-10-25 09:05")
    assert parsed == pytest.approx(ts(year=2026, month=10, day=25, hour=9, minute=5))
    assert isinstance(parsed, float)
    # 闰日（2028 是闰年）合法
    leap = parse_run_at("2028-02-29 00:00")
    assert leap == pytest.approx(ts(year=2028, month=2, day=29, hour=0, minute=0))


@pytest.mark.parametrize(
    "value",
    [
        "2026-2-29 09:00",       # 月日未零填充
        "2026-02-29 9:00",       # 时未零填充
        "2026-02-28 09:0",       # 分未零填充
        "2026/02/28 09:00",      # 分隔符错误
        "2026-02-28T09:00",      # 缺空格
        "2026-02-28 09:00:00",   # 多余秒
        "2026-02-30 09:00",      # 非法日期（2 月 30 日）
        "2027-02-29 09:00",      # 非闰年闰日
        "2026-13-01 09:00",      # 月份越界
        "2026-00-10 09:00",      # 月份为 0
        "2026-04-31 09:00",      # 4 月没有 31 日
        "2026-01-01 24:00",      # 时越界
        "2026-01-01 09:60",      # 分越界
        "",
        "  2026-01-01 09:00 ",
        None,
        1234,
    ],
)
def test_parse_run_at_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_run_at(value)


# ---------------------------------------------------------------------------
# once：validate_trigger
# ---------------------------------------------------------------------------

def test_validate_trigger_once_accepts_each_form():
    assert validate_trigger("once", {"delay_minutes": 8}) == []
    assert validate_trigger("once", {"run_at": "2026-10-25 09:05"}) == []
    # 边界值合法
    assert validate_trigger("once", {"delay_minutes": 1}) == []
    assert validate_trigger("once", {"delay_minutes": 10080}) == []


def test_validate_trigger_once_requires_exactly_one_form():
    missing = validate_trigger("once", {})
    assert missing
    assert any("必须且只能提供 delay_minutes 或 run_at 之一" in e for e in missing)

    both = validate_trigger("once", {"delay_minutes": 5, "run_at": "2026-10-25 09:05"})
    assert both
    assert any("必须且只能提供 delay_minutes 或 run_at 之一" in e for e in both)


@pytest.mark.parametrize("value", [0, 10081, -3, 1.5, True, False, "8", None])
def test_validate_trigger_once_rejects_bad_delay(value):
    errors = validate_trigger("once", {"delay_minutes": value})
    assert errors
    assert any("delay_minutes" in e for e in errors)


def test_validate_trigger_once_rejects_bad_run_at():
    for value in ("2026-02-30 09:00", "2026-1-1 09:00", "09:00", 20261025):
        errors = validate_trigger("once", {"run_at": value})
        assert errors, value
        assert any("一次性触发时刻非法" in e for e in errors), value


# ---------------------------------------------------------------------------
# once：compute_next_run
# ---------------------------------------------------------------------------

def test_compute_next_run_once_uses_run_at():
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("once", {"run_at": "2026-09-12 10:30"}, now)
    assert nxt == pytest.approx(ts(year=2026, month=9, day=12, hour=10, minute=30))


def test_compute_next_run_once_uses_delay_minutes():
    now = ts(year=2026, month=9, day=12, hour=10, minute=0)
    nxt = compute_next_run("once", {"delay_minutes": 8}, now)
    assert nxt == pytest.approx(now + 8 * 60)
    # 上限
    nxt = compute_next_run("once", {"delay_minutes": 10080}, now)
    assert nxt == pytest.approx(now + 10080 * 60)

