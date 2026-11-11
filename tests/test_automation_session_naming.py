"""星轨自动化任务：运行会话名生成（纯函数层）。"""

from datetime import datetime

from core.automation.models import MAX_SESSION_CHARS
from core.automation.session_naming import build_run_session_name

_NOW = datetime(2026, 11, 11, 9, 30)


def test_name_uses_iso_date_prefix():
    assert build_run_session_name("每日站会摘要", _NOW) == "2026-11-11 每日站会摘要"


def test_name_ignores_time_of_day():
    morning = build_run_session_name("每日站会摘要", datetime(2026, 11, 11, 9, 0))
    evening = build_run_session_name("每日站会摘要", datetime(2026, 11, 11, 21, 45))
    assert morning == evening == "2026-11-11 每日站会摘要"


def test_name_strips_surrounding_whitespace():
    assert build_run_session_name("  每日站会摘要 \t", _NOW) == "2026-11-11 每日站会摘要"


def test_name_is_stable_for_repeated_calls_on_same_day():
    first = build_run_session_name("每日站会摘要", _NOW)
    second = build_run_session_name("每日站会摘要", _NOW)
    assert first == second


def test_name_truncates_long_task_name_to_max_session_chars():
    name = build_run_session_name("长" * 200, _NOW)
    assert len(name) == MAX_SESSION_CHARS == 100
    assert name.startswith("2026-11-11 长")


def test_blank_task_name_returns_date_only():
    assert build_run_session_name("", _NOW) == "2026-11-11"
    assert build_run_session_name("   \t ", _NOW) == "2026-11-11"