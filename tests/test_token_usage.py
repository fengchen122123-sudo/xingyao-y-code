import sqlite3
import time
from datetime import date
from pathlib import Path

import pytest

import core.app_context as app_context
import main
from core.llm import _persist_token_usage
from core.memory import MemoryManager


def _ts_of(date_str):
    """把 YYYY-MM-DD 转成本地时区当天中午的秒级时间戳，避免凌晨/时区边界。"""
    return int(time.mktime(time.strptime(date_str + " 12:00:00", "%Y-%m-%d %H:%M:%S")))


def test_pytest_runtime_data_paths_are_isolated_from_real_ycode_home():
    import config
    from core.runtime_warnings import DEFAULT_WARNING_LOG

    isolated = Path(config.YCODE_DIR).resolve()
    real_home = (Path.home() / ".ycode").resolve()

    assert isolated != real_home
    assert Path(config.DB_FILE).resolve().parent == isolated
    assert Path(config.GUI_DB_FILE).resolve().parent == isolated
    assert Path(DEFAULT_WARNING_LOG).resolve().parent == isolated


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
    columns = {
        row[1] for row in manager.conn.execute("PRAGMA table_info(token_usage)")
    }
    assert {"api_base", "cache_scope"} <= columns


def test_token_usage_schema_migrates_legacy_rows_without_inventing_scope(tmp_path):
    db_path = tmp_path / "legacy-token-usage.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, date TEXT,
            provider TEXT, model TEXT, prompt_tokens INTEGER,
            completion_tokens INTEGER, cached_tokens INTEGER,
            total_tokens INTEGER, session_id TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO token_usage "
        "(ts,date,provider,model,prompt_tokens,completion_tokens,cached_tokens,total_tokens,session_id) "
        "VALUES (1,'2026-08-09','openai','legacy-model',100,10,90,110,'legacy-session')"
    )
    conn.commit()
    conn.close()

    manager = MemoryManager(str(db_path))
    row = manager.conn.execute(
        "SELECT api_base, cache_scope FROM token_usage WHERE session_id='legacy-session'"
    ).fetchone()

    assert row == ("", "legacy")


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
    assert rows[0]["cache_prompt"] == 4000
    assert rows[0]["total"] == 4600
    assert rows[1]["model"] == "kimi-k2"
    assert rows[1]["total"] == 550


def test_query_token_usage_by_day_and_model_keeps_days_and_models_separate():
    manager = MemoryManager(":memory:")
    manager.add_token_usage(
        "openai", "m1", 100, 10, 90, 110, ts=_ts_of("2026-08-08")
    )
    manager.add_token_usage(
        "openai", "m1", 200, 20, None, 220, ts=_ts_of("2026-08-08")
    )
    manager.add_token_usage(
        "moonshot", "m2", 300, 30, 250, 330, ts=_ts_of("2026-08-09")
    )

    rows = manager.query_token_usage_by_day_and_model(
        "2026-08-08", "2026-08-09"
    )

    assert rows == [
        {
            "date": "2026-08-08",
            "provider": "openai",
            "model": "m1",
            "prompt": 300,
            "completion": 30,
            "cached": 90,
            "cache_prompt": 100,
            "total": 330,
            "requests": 2,
        },
        {
            "date": "2026-08-09",
            "provider": "moonshot",
            "model": "m2",
            "prompt": 300,
            "completion": 30,
            "cached": 250,
            "cache_prompt": 300,
            "total": 330,
            "requests": 1,
        },
    ]


def test_token_usage_keeps_endpoint_and_scope_separate():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    common = {
        "provider": "openai", "model": "deepseek-v4-flash",
        "prompt_tokens": 1000, "completion_tokens": 10,
        "cached_tokens": 990, "total_tokens": 1010,
        "session_id": "s1", "ts": today,
    }
    manager.add_token_usage(
        **common, api_base="https://api.deepseek.com", cache_scope="main"
    )
    manager.add_token_usage(
        **common, api_base="HTTPS://API.DEEPSEEK.COM:443/", cache_scope="main"
    )
    manager.add_token_usage(
        **common, api_base="https://gateway.example/v1", cache_scope="main"
    )
    manager.add_token_usage(
        **common, api_base="https://api.deepseek.com", cache_scope="keepalive"
    )

    rows = manager.query_token_usage_by_model("2026-08-05", "2026-08-05")

    assert len(rows) == 3
    assert {
        (row["api_base"], row["cache_scope"]) for row in rows
    } == {
        ("https://api.deepseek.com", "main"),
        ("https://gateway.example/v1", "main"),
        ("https://api.deepseek.com", "keepalive"),
    }
    official_main = next(
        row for row in rows
        if row["api_base"] == "https://api.deepseek.com"
        and row["cache_scope"] == "main"
    )
    assert official_main["prompt"] == 2000


def test_token_usage_endpoint_identity_hashes_query_without_merging_routes():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    common = {
        "provider": "openai", "model": "m1", "prompt_tokens": 100,
        "completion_tokens": 1, "cached_tokens": 90, "total_tokens": 101,
        "session_id": "s1", "ts": today, "cache_scope": "main",
    }
    manager.add_token_usage(
        **common, api_base="https://gateway.example/v1?tenant=secret-a"
    )
    manager.add_token_usage(
        **common, api_base="https://gateway.example/v1?tenant=secret-b"
    )

    stored = [
        row[0] for row in manager.conn.execute(
            "SELECT api_base FROM token_usage ORDER BY id"
        )
    ]
    rows = manager.query_token_usage_by_model("2026-08-05", "2026-08-05")

    assert len(rows) == 2
    assert stored[0] != stored[1]
    assert all("tenant" not in value and "secret" not in value for value in stored)


def test_token_usage_endpoint_parse_failure_never_persists_url_plaintext():
    manager = MemoryManager(":memory:")
    raw_url = "https://user:secret@api.example:bad/v1?token=hidden"

    manager.add_token_usage(
        "openai", "m1", 100, 1, 90, 101,
        api_base=raw_url, cache_scope="main",
    )

    stored = manager.conn.execute(
        "SELECT api_base FROM token_usage"
    ).fetchone()[0]
    assert stored.startswith("invalid-endpoint#")
    for secret in ("user", "secret", "token", "hidden", "api.example"):
        assert secret not in stored


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


def test_unreported_cache_is_stored_as_null_and_excluded_from_cache_denominator():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    manager.add_token_usage(
        "openai", "m1", 100, 10, 0, 110, session_id="reported", ts=today
    )
    manager.add_token_usage(
        "openai", "m1", 200, 20, None, 220, session_id="unreported", ts=today
    )

    raw = manager.conn.execute(
        "SELECT cached_tokens FROM token_usage WHERE session_id = 'unreported'"
    ).fetchone()
    rows = manager.query_token_usage_by_model("2026-08-05", "2026-08-05")

    assert raw[0] is None
    assert rows[0]["prompt"] == 300
    assert rows[0]["cached"] == 0
    assert rows[0]["cache_prompt"] == 100
    assert manager.get_last_token_usage("unreported")["cached_tokens"] is None


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
        api_base="https://api.deepseek.com",
        cache_scope="main",
    )
    assert recorded["provider"] == "openai"
    assert recorded["model"] == "deepseek-v4-pro"
    assert recorded["prompt_tokens"] == 100
    assert recorded["total_tokens"] == 120
    assert recorded["session_id"] == "sess-x"
    assert recorded["api_base"] == "https://api.deepseek.com"
    assert recorded["cache_scope"] == "main"


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


def test_usage_model_label_shows_provider_and_model_only():
    """用量表标签只显示 provider/model，不带接口域名/path 哈希/作用域后缀。"""
    assert main._usage_model_label({
        "provider": "openai",
        "model": "deepseek-v4-flash",
        "api_base": "https://api.deepseek.com/v1",
        "cache_scope": "main",
    }) == "openai/deepseek-v4-flash"
    assert main._usage_model_label({
        "provider": "", "model": "legacy-model",
        "api_base": "", "cache_scope": "legacy"
    }) == "legacy-model"


def test_usage_section_does_not_compute_cross_route_subtotal_hit_rate(monkeypatch):
    rows = [
        {
            "provider": "openai", "model": "m1",
            "api_base": "https://api.example/v1", "cache_scope": "main",
            "prompt": 100, "completion": 10, "cached": 99,
            "cache_prompt": 100, "total": 110,
        },
        {
            "provider": "openai", "model": "m1",
            "api_base": "https://gateway.example/v1", "cache_scope": "keepalive",
            "prompt": 100, "completion": 1, "cached": 0,
            "cache_prompt": 100, "total": 101,
        },
    ]

    class Manager:
        def query_token_usage_by_model(self, start, end):
            return rows

        def query_cache_diagnostic_summary(self, *args, **kwargs):
            return {"count": 0, "lost_tokens": 0, "unreported": 0, "categories": {}}

    rate_calls = []
    monkeypatch.setattr(
        main, "_format_cache_hit_rate",
        lambda cached, prompt: rate_calls.append((cached, prompt)) or "rate",
    )
    monkeypatch.setattr(main, "print_formatted_text", lambda *args, **kwargs: None)

    main._render_usage_section(Manager(), "今日", "2026-08-09", "2026-08-09")

    assert rate_calls == [(99, 100), (0, 100)]


def test_query_cache_invalidation_events():
    manager = MemoryManager(":memory:")
    today = _ts_of("2026-08-05")
    main = {"api_base": "https://api.example/v1", "cache_scope": "main"}
    # 大输入低命中 -> 失效事件
    manager.add_token_usage("openai", "m1", 50000, 100, 3000, 50100, ts=today, **main)
    manager.add_token_usage("openai", "m1", 80000, 100, 0, 80000, ts=today, **main)
    # 大输入高命中 -> 不算
    manager.add_token_usage("openai", "m1", 60000, 100, 59000, 60100, ts=today, **main)
    # 小输入低命中 -> 不算（低于 min_prompt_tokens 阈值）
    manager.add_token_usage("openai", "m1", 5000, 100, 0, 5100, ts=today, **main)
    # 后台大输入低命中不得污染主链路失效数。
    manager.add_token_usage(
        "openai", "m1", 90000, 1, 0, 90001, ts=today,
        api_base="https://api.example/v1", cache_scope="keepalive",
    )

    inv = manager.query_cache_invalidation_events(
        "2026-08-05", "2026-08-05", cache_scope="main"
    )
    assert inv["count"] == 2
    assert inv["lost_tokens"] == (50000 - 3000) + (80000 - 0)

    # 区间外无事件
    inv2 = manager.query_cache_invalidation_events(
        "2026-09-01", "2026-09-30", cache_scope="main"
    )
    assert inv2 == {"count": 0, "lost_tokens": 0}


def test_get_last_token_usage_returns_latest_per_session():
    """取指定会话最近一次真实用量；无记录返回 None（供真实基线口径）。"""
    manager = MemoryManager(":memory:")
    assert manager.get_last_token_usage("s1") is None

    manager.add_token_usage("openai", "m1", 100, 10, 0, 110, session_id="s1")
    manager.add_token_usage("openai", "m1", 200, 20, 0, 220, session_id="s2")
    manager.add_token_usage(
        "openai", "m1", 300, 30, 5, 330, session_id="s1",
        api_base="https://main.example/v1", cache_scope="main",
    )
    manager.add_token_usage(
        "openai", "m1", 10, 1, 10, 11, session_id="s1",
        api_base="https://main.example/v1", cache_scope="keepalive",
    )

    last = manager.get_last_token_usage("s1")
    assert last["prompt_tokens"] == 300
    assert last["completion_tokens"] == 30
    assert last["total_tokens"] == 330
    assert last["cached_tokens"] == 5

    other = manager.get_last_token_usage("s2")
    assert other["prompt_tokens"] == 200


def test_memory_session_lifecycle_keeps_runtime_cache_state_in_sync():
    import core.llm as llm

    manager = MemoryManager(":memory:")
    old_name = "memory-cache-old"
    new_name = "memory-cache-new"
    model = "openai/deepseek-v4"
    base = "https://memory-lifecycle.example/v1"
    manager.create_session_explicit(old_name)
    llm._record_cache_usage(old_name, "main", 100, 90, "", model, base)

    success, final_name = manager.rename_session(old_name, new_name)
    assert success is True
    assert final_name == new_name
    assert llm.get_token_usage_snapshot(old_name, model, base)["cache"]["available"] is False
    assert llm.get_token_usage_snapshot(new_name, model, base)["cache"]["available"] is True

    manager.clear_session_messages(new_name)
    assert llm.get_token_usage_snapshot(new_name, model, base)["cache"]["available"] is False

    llm._record_cache_usage(new_name, "main", 100, 80, "", model, base)
    manager.delete_session(new_name)
    assert llm.get_token_usage_snapshot(new_name, model, base)["cache"]["available"] is False


def test_delete_workspace_sessions_clears_each_runtime_cache_bucket(tmp_path):
    import core.llm as llm

    manager = MemoryManager(":memory:")
    workdir = str(tmp_path)
    model = "openai/deepseek-v4"
    base = "https://workspace-delete.example/v1"
    for name in ("workspace-cache-a", "workspace-cache-b"):
        manager.create_session_explicit(name, workdir=workdir)
        llm._record_cache_usage(name, "main", 100, 90, "", model, base)

    manager.delete_sessions_by_workdir(workdir)

    for name in ("workspace-cache-a", "workspace-cache-b"):
        assert llm.get_token_usage_snapshot(name, model, base)["cache"]["available"] is False


def test_format_cache_diagnostic_summary_is_evidence_based():
    summary = {
        "count": 7,
        "lost_tokens": 123456,
        "unreported": 2,
        "categories": {
            "cold_start": 1,
            "identity_change": 1,
            "tool_contract_change": 1,
            "client_history_rewrite": 1,
            "prompt_drop": 1,
            "idle_gap": 1,
            "provider_or_unknown": 1,
        },
    }

    text = main._format_cache_diagnostic_summary(summary)

    assert "低命中诊断 7 次" in text
    assert "冷启动 1" in text
    assert "历史重写 1" in text
    assert "prompt 缩短 1" in text
    assert "未确定 1" in text
    assert "未报告缓存明细 2" in text
    assert "缺少足够证据" in text
    assert "客户端无法避免" not in text
    assert "属 provider" not in text


def test_format_cache_diagnostic_summary_omits_empty_summary():
    assert main._format_cache_diagnostic_summary({
        "count": 0, "lost_tokens": 0, "unreported": 0, "categories": {}
    }) == ""
