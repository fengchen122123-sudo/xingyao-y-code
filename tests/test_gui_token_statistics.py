from datetime import date, datetime, time, timedelta

import pytest

import gui_bridge
from core.memory import MemoryManager


def _local_noon(day):
    return int(datetime.combine(day, time(hour=12)).timestamp())


def test_gui_token_statistics_fills_dates_and_aggregates_models():
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    today = date.today()
    bridge._memory.add_token_usage(
        "openai", "m1", 400, 40, 340, 440,
        session_id="older", ts=_local_noon(today - timedelta(days=1)),
    )
    bridge._memory.add_token_usage(
        "openai", "m1", 200, 20, 170, 220,
        session_id="today", ts=_local_noon(today),
    )
    bridge._memory.add_token_usage(
        "moonshot", "m2", 100, 10, 50, 110,
        session_id="today-m2", ts=_local_noon(today),
    )

    result = bridge.get_token_usage_statistics(7)

    assert result["ok"] is True
    assert result["range"] == {
        "days": 7,
        "start_date": (today - timedelta(days=6)).isoformat(),
        "end_date": today.isoformat(),
    }
    assert len(result["days"]) == 7
    assert result["days"][-1]["date"] == today.isoformat()
    assert result["days"][0]["total"] == 0
    assert result["summary"] == {
        "prompt": 700,
        "completion": 70,
        "cached": 560,
        "cache_prompt": 700,
        "total": 770,
        "requests": 3,
        "cache_hit_rate": 80.0,
        # 成本账本：全部未配价 → 未计价计数 3，金额为空（不是 0 元）
        "cost_micros": 0,
        "unpriced": 3,
        "cost_text": None,
    }
    assert result["models"] == [
        {
            "key": "openai/m1",
            "provider": "openai",
            "model": "m1",
            "prompt": 600,
            "completion": 60,
            "cached": 510,
            "cache_prompt": 600,
            "total": 660,
            "requests": 2,
            "cache_hit_rate": 85.0,
            "cost_micros": 0,
            "unpriced": 2,
            "cost_text": None,
        },
        {
            "key": "moonshot/m2",
            "provider": "moonshot",
            "model": "m2",
            "prompt": 100,
            "completion": 10,
            "cached": 50,
            "cache_prompt": 100,
            "total": 110,
            "requests": 1,
            "cache_hit_rate": 50.0,
            "cost_micros": 0,
            "unpriced": 1,
            "cost_text": None,
        },
    ]
    assert [item["key"] for item in result["days"][-1]["models"]] == [
        "openai/m1", "moonshot/m2",
    ]


def test_gui_token_statistics_uses_stable_key_order_for_equal_totals():
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    today = _local_noon(date.today())
    bridge._memory.add_token_usage("zeta", "m", 90, 10, 0, 100, ts=today)
    bridge._memory.add_token_usage("alpha", "m", 90, 10, 0, 100, ts=today)

    result = bridge.get_token_usage_statistics(1)

    assert [item["key"] for item in result["models"]] == ["alpha/m", "zeta/m"]
    assert [item["key"] for item in result["days"][0]["models"]] == [
        "alpha/m", "zeta/m",
    ]


def test_gui_token_statistics_returns_query_failures():
    bridge = object.__new__(gui_bridge.ApiBridge)

    class BrokenMemory:
        def query_token_usage_by_day_and_model(self, *_args):
            raise RuntimeError("database unavailable")

    bridge._memory = BrokenMemory()

    result = bridge.get_token_usage_statistics(7)

    assert result == {"ok": False, "msg": "database unavailable"}


@pytest.mark.parametrize("days", [14, 1.5, "7.0", True])
def test_gui_token_statistics_rejects_unsupported_range_without_querying(days):
    bridge = object.__new__(gui_bridge.ApiBridge)

    class ExplodingMemory:
        def query_token_usage_by_day_and_model(self, *_args):
            raise AssertionError("invalid ranges must not query SQLite")

    bridge._memory = ExplodingMemory()

    result = bridge.get_token_usage_statistics(days)

    assert result == {"ok": False, "msg": "统计范围仅支持 0、1、7、30、365 天"}


def test_gui_token_statistics_supports_full_year_range():
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    bridge._memory.add_token_usage(
        "openai", "m1", 400, 40, 340, 440,
        session_id="today", ts=_local_noon(date.today()),
    )

    result = bridge.get_token_usage_statistics(365)

    assert result["ok"] is True
    assert result["range"]["days"] == 365
    assert result["range"]["start_date"] == (
        date.today() - timedelta(days=364)
    ).isoformat()
    assert len(result["days"]) == 365
    assert result["days"][-1]["total"] == 440
    assert result["days"][0]["total"] == 0


def test_gui_token_statistics_supports_lifetime_range():
    """days=0 累计全量：区间起点取最早一条用量记录，不受 365 天限制。"""
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    today = date.today()
    first_day = today - timedelta(days=400)
    bridge._memory.add_token_usage(
        "openai", "m1", 400, 40, 340, 440,
        session_id="old", ts=_local_noon(first_day),
    )
    bridge._memory.add_token_usage(
        "openai", "m1", 200, 20, 170, 220,
        session_id="today", ts=_local_noon(today),
    )

    result = bridge.get_token_usage_statistics(0)

    assert result["ok"] is True
    assert result["range"] == {
        "days": 401,
        "start_date": first_day.isoformat(),
        "end_date": today.isoformat(),
    }
    assert len(result["days"]) == 401
    assert result["days"][0]["total"] == 440
    assert result["days"][-1]["total"] == 220
    assert result["summary"]["total"] == 660


def test_gui_token_statistics_lifetime_without_records_covers_today_only():
    """累计范围没有历史记录时只返回今天一格，不生成空日期。"""
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")

    result = bridge.get_token_usage_statistics(0)

    assert result["ok"] is True
    assert result["range"] == {
        "days": 1,
        "start_date": date.today().isoformat(),
        "end_date": date.today().isoformat(),
    }
    assert len(result["days"]) == 1
    assert result["days"][0]["total"] == 0

def test_gui_token_statistics_sums_cost_and_marks_unpriced():
    """有价的行累加金额，无价的行只计 unpriced；两者不混。"""
    bridge = object.__new__(gui_bridge.ApiBridge)
    bridge._memory = MemoryManager(":memory:")
    today = _local_noon(date.today())
    bridge._memory.add_token_usage(
        "openai", "m1", 100, 10, 0, 110, ts=today,
        cost_micros=1500, cost_currency="CNY", price_source="config",
    )
    bridge._memory.add_token_usage(
        "openai", "m1", 200, 20, 0, 220, ts=today,
        cost_micros=2500, cost_currency="CNY", price_source="config",
    )
    bridge._memory.add_token_usage("openai", "m1", 300, 30, 0, 330, ts=today)

    result = bridge.get_token_usage_statistics(1)

    summary = result["summary"]
    assert summary["cost_micros"] == 4000
    assert summary["unpriced"] == 1
    assert summary["cost_text"] == "¥0.0040"
    # 日明细的模型行同样带成本
    day_model = result["days"][0]["models"][0]
    assert day_model["cost_micros"] == 4000
    assert day_model["unpriced"] == 1
