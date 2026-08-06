import time
from datetime import date

import pytest

import core.app_context as app_context
import main
from core.llm import _persist_token_usage
from core.memory import MemoryManager


def _ts_of(date_str):
    """把 YYYY-MM-DD 转成本地时区当天中午的秒级时间戳，避免凌晨/时区边界。"""
    return int(time.mktime(time.strptime(date_str + " 12:00:00", "%Y-%m-%d %H:%M:%S")))


def test_token_usage_table_created():
    manager = MemoryManager(":memory:")
    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='token_usage'"
    )
    assert cursor.fetchone() is not None, "token_usage 表没有被创建！"
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_token_usage_date'"
    )
    assert cursor.fetchone() is not None, "token_usage 日期索引没有被创建！"


def test_add_and_query_token_usage_grouped_by_model():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    manager.add_token_usage("openai", "deepseek-v4-pro", 1000, 200, 500, 1200,
                            session_id="s1", ts=today)
    manager.add_token_usage("openai", "deepseek-v4-pro", 3000, 400, 100, 3400,
                            session_id="s1", ts=today)
    manager.add_token_usage("moonshot", "kimi-k2", 500, 50, 0, 550,
                            session_id="s1", ts=today)

    rows = manager.query_token_usage_by_model("2026-08-05", "2026-08-05")
    assert len(rows) == 2
    # 按合计降序：deepseek 在前
    assert rows[0]["model"] == "deepseek-v4-pro"
    assert rows[0]["provider"] == "openai"
    assert rows[0]["prompt"] == 4000
    assert rows[0]["completion"] == 600
    assert rows[0]["cached"] == 600
    assert rows[0]["total"] == 4600
    assert rows[1]["model"] == "kimi-k2"
    assert rows[1]["total"] == 550


def test_query_token_usage_date_range_filter():
    manager = MemoryManager(":memory:")
    manager.add_token_usage("openai", "m1", 100, 10, 0, 110, ts=_ts_of("2026-08-01"))
    manager.add_token_usage("openai", "m1", 200, 20, 0, 220, ts=_ts_of("2026-08-05"))
    manager.add_token_usage("openai", "m1", 300, 30, 0, 330, ts=_ts_of("2026-08-09"))

    # 近 7 天口径：只含 08-05 与 08-09，不含 08-01
    rows = manager.query_token_usage_by_model("2026-08-03", "2026-08-09")
    assert len(rows) == 1
    assert rows[0]["total"] == 550

    # 区间外无数据
    assert manager.query_token_usage_by_model("2026-09-01", "2026-09-30") == []


def test_persist_token_usage_splits_provider_and_model(monkeypatch):
    recorded = {}

    class _FakeManager:
        def add_token_usage(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(app_context, "get_manager", lambda: _FakeManager())
    _persist_token_usage(
        "openai/deepseek-v4-pro",
        prompt_tokens=100,
        completion_tokens=20,
        cached_tokens=5,
        total_tokens=120,
        session_id="sess-x",
    )
    assert recorded["provider"] == "openai"
    assert recorded["model"] == "deepseek-v4-pro"
    assert recorded["prompt_tokens"] == 100
    assert recorded["total_tokens"] == 120
    assert recorded["session_id"] == "sess-x"


def test_persist_token_usage_without_manager_or_on_error(monkeypatch):
    # 无 manager 时静默返回，不抛异常
    monkeypatch.setattr(app_context, "get_manager", lambda: None)
    _persist_token_usage("openai/m1", 1, 1, 0, 2)

    # manager 写库抛异常也必须被吞掉，绝不影响主流程
    class _BrokenManager:
        def add_token_usage(self, **kwargs):
            raise RuntimeError("db locked")

    monkeypatch.setattr(app_context, "get_manager", lambda: _BrokenManager())
    _persist_token_usage("openai/m1", 1, 1, 0, 2)


def test_parse_usage_date_args_default():
    assert main._parse_usage_date_args([]) is None


def test_parse_usage_date_args_single_day():
    assert main._parse_usage_date_args(["2026-08-01"]) == (
        date(2026, 8, 1), date(2026, 8, 1)
    )


def test_parse_usage_date_args_range():
    assert main._parse_usage_date_args(["2026-08-01", "2026-08-05"]) == (
        date(2026, 8, 1), date(2026, 8, 5)
    )


def test_parse_usage_date_args_tilde_separator():
    assert main._parse_usage_date_args(["2026-08-01", "~", "2026-08-05"]) == (
        date(2026, 8, 1), date(2026, 8, 5)
    )


def test_parse_usage_date_args_reversed_range_auto_fixed():
    assert main._parse_usage_date_args(["2026-08-05", "2026-08-01"]) == (
        date(2026, 8, 1), date(2026, 8, 5)
    )


def test_parse_usage_date_args_invalid_format():
    with pytest.raises(ValueError):
        main._parse_usage_date_args(["2026/08/01"])
    with pytest.raises(ValueError):
        main._parse_usage_date_args(["abc"])


def test_parse_usage_date_args_too_many():
    with pytest.raises(ValueError):
        main._parse_usage_date_args(["2026-08-01", "2026-08-02", "2026-08-03"])


def test_term_display_width():
    assert main._term_display_width("k3-256k") == 7
    assert main._term_display_width("模型") == 4   # 2 个中文字符 = 4 格
    assert main._term_display_width("缓存命中") == 8
    assert main._term_display_width("a中b") == 4


def test_pad_left_and_right_by_display_width():
    # 中文表头右对齐到 14 格：8 格内容 + 6 格前导空格
    assert main._pad_left("缓存命中", 14) == "      缓存命中"
    assert main._pad_left("输入", 14) == "          输入"
    # 左对齐补齐
    assert main._pad_right("模型", 12) == "模型        "
    assert main._pad_right("k3-256k", 12) == "k3-256k     "
    # 超出目标宽度时不截断、不加空格
    assert main._pad_left("123456789012345", 14) == "123456789012345"


def test_query_cache_invalidation_events():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    # 大输入低命中 -> 失效事件
    manager.add_token_usage("openai", "m1", 50000, 100, 3000, 50100, ts=today)
    manager.add_token_usage("openai", "m1", 80000, 100, 0, 80000, ts=today)
    # 大输入高命中 -> 不算
    manager.add_token_usage("openai", "m1", 60000, 100, 59000, 60100, ts=today)
    # 小输入低命中 -> 不算（低于 min_prompt_tokens 阈值）
    manager.add_token_usage("openai", "m1", 5000, 100, 0, 5100, ts=today)

    inv = manager.query_cache_invalidation_events("2026-08-05", "2026-08-05")
    assert inv["count"] == 2
    assert inv["lost_tokens"] == (50000 - 3000) + (80000 - 0)

    # 区间外无事件
    inv2 = manager.query_cache_invalidation_events("2026-09-01", "2026-09-30")
    assert inv2 == {"count": 0, "lost_tokens": 0}


def test_get_last_token_usage_returns_latest_per_session():
    """取指定会话最近一次真实用量；无记录返回 None（供真实基线口径）。"""
    manager = MemoryManager(":memory:")
    assert manager.get_last_token_usage("s1") is None

    manager.add_token_usage("openai", "m1", 100, 10, 0, 110, session_id="s1")
    manager.add_token_usage("openai", "m1", 200, 20, 0, 220, session_id="s2")
    manager.add_token_usage("openai", "m1", 300, 30, 5, 330, session_id="s1")

    last = manager.get_last_token_usage("s1")
    assert last["prompt_tokens"] == 300
    assert last["completion_tokens"] == 30
    assert last["total_tokens"] == 330
    assert last["cached_tokens"] == 5

    other = manager.get_last_token_usage("s2")
    assert other["prompt_tokens"] == 200
