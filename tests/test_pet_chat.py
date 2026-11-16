"""core/pet_chat 的离线单测。

刻意的约束：不联网、不启动调度线程、不碰真实桌宠进程、不碰真实模型配置。
所有触发条件都用 monkeypatch 把「配置读取」和「桌宠是否在跑」换成本地桩，
这样 `_should_speak` 的判断逻辑可以逐条单独验证，任何一条写错都会立刻暴露。
"""

from __future__ import annotations

import time

import pytest

from core import pet_chat as m


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch):
    """每个用例都从「没说过话、今天 0 条、最近列表为空」的干净状态开始。"""
    monkeypatch.setattr(m, "_last_spoken_at", 0.0)
    # 下次开口时间戳也要归零：它是 _record_spoken 直接写模块级变量的，不归零会漏到
    # 下一个用例，表现就是「明明什么都没说却被判定为还在等下一次」
    monkeypatch.setattr(m, "_next_speak_at", 0.0)
    monkeypatch.setattr(m, "_today_count", 0)
    monkeypatch.setattr(m, "_today_day", m._today_stamp())
    m._recent.clear()
    yield
    m._recent.clear()


def _stub_conditions(monkeypatch, *, enabled=True, idle=600, running=True, busy=False):
    """把 `_should_speak` 依赖的外部世界全部换成可控桩。"""
    monkeypatch.setattr(m, "_setting_enabled", lambda: enabled)
    monkeypatch.setattr(m, "_setting_idle_seconds", lambda: idle)
    monkeypatch.setattr(m, "_setting_model_id", lambda: "")
    monkeypatch.setattr(m, "_agent_busy", lambda: busy)

    from core import pet_manager

    monkeypatch.setattr(pet_manager, "get_status", lambda: {"running": running})


# ---------------------------------------------------------------- _clean_line


def test_clean_line_strips_thinking_tags():
    raw = "<think>\n这里是想事情的过程\n</think>\n今天风有点凉"
    assert m._clean_line(raw) == "今天风有点凉"


def test_clean_line_strips_wrapping_quotes():
    assert m._clean_line("「今天有点安静」") == "今天有点安静"
    assert m._clean_line('"今天有点安静"') == "今天有点安静"
    assert m._clean_line("“今天有点安静”") == "今天有点安静"
    assert m._clean_line("'今天有点安静'") == "今天有点安静"


def test_clean_line_keeps_first_line_only():
    assert m._clean_line("先歇一会儿\n第二行不该出现") == "先歇一会儿"


def test_clean_line_truncates_long_text():
    cleaned = m._clean_line("啊" * 80)
    assert cleaned is not None
    assert len(cleaned) == m._MAX_CHARS


def test_clean_line_returns_none_for_blank():
    assert m._clean_line("   \n\t ") is None
    assert m._clean_line("") is None
    assert m._clean_line(None) is None


def test_clean_line_rejects_recent_duplicate():
    m._recent.append("今天有点安静")
    assert m._clean_line("今天有点安静") is None
    # 只是「包含」不算重复：去重按完全相等判断，避免把相似台词一起误杀
    assert m._clean_line("今天有点安静呀") == "今天有点安静呀"


# --------------------------------------------------------------- _should_speak


def test_should_speak_false_when_disabled(monkeypatch):
    _stub_conditions(monkeypatch, enabled=False)
    assert m._should_speak(3600) is False


def test_should_speak_false_when_pet_not_running(monkeypatch):
    _stub_conditions(monkeypatch, running=False)
    assert m._should_speak(3600) is False


def test_should_speak_false_when_not_idle_enough(monkeypatch):
    _stub_conditions(monkeypatch, idle=600)
    assert m._should_speak(599) is False


def test_should_speak_false_when_daily_limit_reached(monkeypatch):
    _stub_conditions(monkeypatch, idle=600)
    monkeypatch.setattr(m, "_today_count", m._DAILY_LIMIT)
    assert m._should_speak(3600) is False


def test_should_speak_false_when_waiting_for_next_slot(monkeypatch):
    """刚说完、抽定的下一次还没到：哪怕人一直不在，也不能马上再说。"""
    _stub_conditions(monkeypatch, idle=600)
    now = time.time()
    monkeypatch.setattr(m, "_last_spoken_at", now)
    monkeypatch.setattr(m, "_next_speak_at", now + 300)
    assert m._should_speak(3600) is False


def test_should_speak_true_once_next_slot_reached(monkeypatch):
    """抽定的时间点一过就允许开口——「间隔随机」正是靠这个时间戳生效的。"""
    _stub_conditions(monkeypatch, idle=600)
    now = time.time()
    monkeypatch.setattr(m, "_last_spoken_at", now - 400)
    monkeypatch.setattr(m, "_next_speak_at", now - 100)
    assert m._should_speak(3600) is True


def test_should_speak_false_when_agent_busy(monkeypatch):
    _stub_conditions(monkeypatch, idle=600, busy=True)
    assert m._should_speak(3600) is False


def test_should_speak_true_when_all_conditions_met(monkeypatch):
    _stub_conditions(monkeypatch, idle=600)
    assert m._should_speak(601) is True


# --------------------------------------------------------------- _roll_next_gap


def test_roll_next_gap_is_random_but_bounded():
    """间隔必须落在 0.4~2.6 倍阈值内，而且要真的在变，不能退化成固定值。"""
    gaps = [m._roll_next_gap(600) for _ in range(200)]
    assert all(0.4 * 600 <= gap <= 2.6 * 600 for gap in gaps)
    # 固定值 + 小抖动时极差会很小；三角分布应该轻松拉开上百秒
    assert max(gaps) - min(gaps) > 300
    # 均值落在合理中段（三角分布 (0.4+2.6+0.7)/3 ≈ 1.23 倍）
    avg = sum(gaps) / len(gaps)
    assert 0.9 * 600 < avg < 1.6 * 600


def test_roll_next_gap_falls_back_for_bad_base():
    """阈值传进来是脏值时退回默认值算，不能抛异常（配置可能被手改坏）。"""
    assert m._roll_next_gap(None) > 0
    assert m._roll_next_gap("abc") > 0


def test_record_spoken_schedules_next_slot(monkeypatch):
    """说完立刻把下一次时间点定下来，间隔落在随机区间内。"""
    monkeypatch.setattr(m, "_setting_idle_seconds", lambda: 600)
    m._record_spoken("今天有点安静")
    assert m._today_count == 1
    assert m._recent[-1] == "今天有点安静"
    gap = m._next_speak_at - m._last_spoken_at
    assert 240 <= gap <= 1560


# --------------------------------------------------------- _system_idle_seconds


def test_system_idle_seconds_is_non_negative_float():
    value = m._system_idle_seconds()
    assert isinstance(value, float)
    assert value >= 0.0
