"""星轨自动化任务：触发器校验与下一次触发时间计算（纯函数层）。"""

from datetime import datetime

import pytest

from core.automation.scheduling import (
    compute_next_run,
    parse_hhmm,
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
