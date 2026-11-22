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

    rows = manager.query_token_usage_by_model("2026-08-03", "2026-08-09")
    assert len(rows) == 1
    assert rows[0]["total"] == 550

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
    assert main._term_display_width("模型") == 4
    assert main._term_display_width("缓存命中") == 8
    assert main._term_display_width("a中b") == 4


def test_pad_left_and_right_by_display_width():
    assert main._pad_left("缓存命中", 14) == "      缓存命中"
    assert main._pad_left("输入", 14) == "          输入"
    assert main._pad_right("模型", 12) == "模型        "
    assert main._pad_right("k3-256k", 12) == "k3-256k     "
    assert main._pad_left("123456789012345", 14) == "123456789012345"


def test_usage_model_label_shows_provider_and_model_only():
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
    manager.add_token_usage("openai", "m1", 50000, 100, 3000, 50100, ts=today, **main)
    manager.add_token_usage("openai", "m1", 80000, 100, 0, 80000, ts=today, **main)
    manager.add_token_usage("openai", "m1", 60000, 100, 59000, 60100, ts=today, **main)
    manager.add_token_usage("openai", "m1", 5000, 100, 0, 5100, ts=today, **main)
    manager.add_token_usage(
        "openai", "m1", 90000, 1, 0, 90001, ts=today,
        api_base="https://api.example/v1", cache_scope="keepalive",
    )

    inv = manager.query_cache_invalidation_events(
        "2026-08-05", "2026-08-05", cache_scope="main"
    )
    assert inv["count"] == 2
    assert inv["lost_tokens"] == (50000 - 3000) + (80000 - 0)

    inv2 = manager.query_cache_invalidation_events(
        "2026-09-01", "2026-09-30", cache_scope="main"
    )
    assert inv2 == {"count": 0, "lost_tokens": 0}


def test_get_last_token_usage_returns_latest_per_session():
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
    assert last["model_name"] == "openai/m1"

    other = manager.get_last_token_usage("s2")
    assert other["prompt_tokens"] == 200
    assert other["model_name"] == "openai/m1"

    manager.add_token_usage("", "bare-model", 5, 1, 0, 6, session_id="bare")
    assert manager.get_last_token_usage("bare")["model_name"] == "bare-model"


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


def test_status_cache_metrics_take_current_lane_hot_rate_not_process_mix():
    import core.llm as llm

    session, model = "task4-lane-switch", "openai/deepseek-v4.1-flash"
    aliyun = "https://aliyun-token-plan.example/v1"
    tokenrhythm = "https://tokenrhythm.example/v1"
    for _ in range(4):
        llm._record_cache_usage(session, "main", 1000, 950, "", model, aliyun)
    for _ in range(2):
        llm._record_cache_usage(session, "main", 1000, 100, "", model, tokenrhythm)

    aliyun_metrics = main._status_cache_metrics(session, model, aliyun)
    tokenrhythm_metrics = main._status_cache_metrics(session, model, tokenrhythm)

    assert aliyun_metrics["hot_rate"] == 95.0
    assert tokenrhythm_metrics["hot_rate"] == 10.0
    assert aliyun_metrics["hot_prompt"] == 4000
    assert tokenrhythm_metrics["hot_prompt"] == 2000


def test_status_cache_metrics_keep_keepalive_scope_out_of_main():
    import core.llm as llm

    session, model = "task4-keepalive-isolation", "openai/deepseek-v4.1-flash"
    base = "https://aliyun-token-plan.example/v1"
    for _ in range(4):
        llm._record_cache_usage(session, "main", 1000, 950, "", model, base)

    before = main._status_cache_metrics(session, model, base)
    for _ in range(6):
        llm._record_cache_usage(session, "keepalive", 10000, 0, "", model, base)
    after = main._status_cache_metrics(session, model, base)

    assert before["hot_rate"] == 95.0
    assert after["hot_rate"] == 95.0
    assert after["hot_prompt"] == 4000
    assert after["hot_cached"] == 3800
    assert after["keepalive_rate"] == 0.0
    assert after["keepalive_prompt"] == 60000
    assert after["keepalive_cached"] == 0


def test_status_cache_metrics_unreported_lane_is_none_not_zero():
    import core.llm as llm

    session, model = "task4-unreported", "openai/local-vllm"
    base = "http://127.0.0.1:8000/v1"

    llm._record_cache_usage(session, "main", 1000, 950, "", model, base)
    llm._mark_cache_usage_unavailable(session, model, base, "main", "usage 不一致")

    metrics = main._status_cache_metrics(session, model, base)

    assert metrics["hot_rate"] is None
    assert metrics["session_rate"] is None
    assert metrics["hot_prompt"] == 1000


def test_usage_model_label_separates_routes_and_keepalive_scope():
    row = {
        "provider": "openai",
        "model": "deepseek-v4-flash",
        "api_base": "https://aliyun-token-plan.example/v1",
        "cache_scope": "main",
    }
    other = {**row, "api_base": "https://tokenrhythm.example/v1"}
    keepalive = {**row, "cache_scope": "keepalive"}

    aliyun_label = main._usage_model_label(row, row["api_base"], row["cache_scope"])
    tokenrhythm_label = main._usage_model_label(
        other, other["api_base"], other["cache_scope"]
    )
    keepalive_label = main._usage_model_label(
        keepalive, keepalive["api_base"], keepalive["cache_scope"]
    )

    assert aliyun_label == (
        "openai/deepseek-v4-flash @ https://aliyun-token-plan.example/v1"
    )
    assert tokenrhythm_label != aliyun_label
    assert keepalive_label != aliyun_label
    assert "[keepalive]" in keepalive_label
    assert main._usage_model_label(
        row, row["api_base"], row["cache_scope"]
    ) == aliyun_label


def test_token_usage_snapshot_exposes_per_scope_views_without_changing_cache_keys():
    import core.llm as llm

    session, model = "task4-scope-views", "openai/deepseek-v4.1-flash"
    base = "https://aliyun-token-plan.example/v1"
    llm._record_cache_usage(session, "main", 1000, 950, "", model, base)
    llm._record_cache_usage(session, "keepalive", 1000, 100, "", model, base)

    snapshot = llm.get_token_usage_snapshot(session, model, base, "main")

    assert snapshot["cache"]["hot"]["rate"] == 95.0
    assert snapshot["cache"]["hot"]["prompt_tokens"] == 1000
    assert snapshot["cache_scopes"]["main"]["hot"]["rate"] == 95.0
    assert snapshot["cache_scopes"]["keepalive"]["hot"]["rate"] == 10.0
    assert snapshot["cache_scopes"]["keepalive"]["hot"]["prompt_tokens"] == 1000


def test_persist_token_usage_forwards_cache_write_tokens(monkeypatch):
    recorded = {}

    class _FakeManager:
        def add_token_usage(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr(app_context, "get_manager", lambda: _FakeManager())
    _persist_token_usage(
        "anthropic/kimi-k3",
        prompt_tokens=1000,
        completion_tokens=20,
        cached_tokens=None,
        total_tokens=1020,
        session_id="sess-write",
        api_base="https://api.kimi.com/coding/v1",
        cache_scope="main",
        cache_write_tokens=800,
    )
    assert recorded["cache_write_tokens"] == 800

    recorded.clear()
    _persist_token_usage(
        "openai/m1",
        prompt_tokens=1000,
        completion_tokens=20,
        cached_tokens=100,
        total_tokens=1020,
        session_id="sess-write",
        api_base="https://relay.example/v1",
        cache_scope="main",
    )
    assert recorded.get("cache_write_tokens") is None


def test_token_usage_cache_write_tokens_column_keeps_null_vs_zero(tmp_path):
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "token-write.db"))
    try:
        manager.add_token_usage(
            "anthropic", "kimi-k3", 1000, 20, None, 1020,
            session_id="write-null", cache_write_tokens=None,
        )
        manager.add_token_usage(
            "anthropic", "kimi-k3", 1000, 20, 0, 1020,
            session_id="write-zero", cache_write_tokens=0,
        )
        manager.add_token_usage(
            "anthropic", "kimi-k3", 1000, 20, None, 1020,
            session_id="write-real", cache_write_tokens=800,
        )

        assert manager.get_last_token_usage("write-null")["cache_write_tokens"] is None
        assert manager.get_last_token_usage("write-zero")["cache_write_tokens"] == 0
        assert manager.get_last_token_usage("write-real")["cache_write_tokens"] == 800
    finally:
        manager.close()


def test_extract_cache_write_tokens_candidates_and_unreported():
    from core.llm_diagnostics import _extract_cache_write_tokens

    assert _extract_cache_write_tokens(None) is None
    assert _extract_cache_write_tokens({"prompt_tokens": 1000}) is None
    assert _extract_cache_write_tokens({"cache_creation_input_tokens": 700}) == 700
    assert _extract_cache_write_tokens({"cache_write_tokens": 300}) == 300
    assert _extract_cache_write_tokens(
        {"prompt_tokens_details": {"cache_creation_tokens": 400}}
    ) == 400
    assert _extract_cache_write_tokens({
        "prompt_tokens_details": {"cache_creation_tokens": 400},
        "cache_write_tokens": 300,
        "cache_creation_input_tokens": 700,
    }) == 400
    assert _extract_cache_write_tokens({"cache_write_tokens": 0}) == 0
    assert _extract_cache_write_tokens({"cache_write_tokens": -1}) is None


def test_cache_diagnostic_table_keeps_null_vs_zero_for_cache_write(tmp_path):
    from core.memory import MemoryManager

    manager = MemoryManager(str(tmp_path / "diag-write.db"))
    try:
        base = {
            "model": "anthropic/kimi-k3",
            "api_base": "https://api.kimi.com/coding/v1",
            "cache_scope": "main",
            "request_reason": "",
            "messages_sha256": "a" * 64,
            "system_sha256": "b" * 64,
            "tools_sha256": "c" * 64,
            "message_count": 2,
            "system_message_count": 1,
            "common_prefix_messages": 0,
            "request_bytes": 100,
            "system_bytes": 40,
            "tools_bytes": 0,
            "cache_reported": True,
            "usage_valid": True,
            "usage_errors": (),
        }
        manager.add_cache_request_diagnostic(dict(base, session_id="dw-null"))
        manager.add_cache_request_diagnostic(
            dict(base, session_id="dw-zero", cache_write_tokens=0)
        )
        manager.add_cache_request_diagnostic(
            dict(base, session_id="dw-real", cache_write_tokens=800)
        )

        rows = {
            row["session_id"]: row
            for row in manager.query_cache_request_diagnostics()
        }
        assert rows["dw-null"]["cache_write_tokens"] is None
        assert rows["dw-zero"]["cache_write_tokens"] == 0
        assert rows["dw-real"]["cache_write_tokens"] == 800
    finally:
        manager.close()


def test_legacy_diagnostic_table_migrates_cache_write_column(tmp_path):
    import sqlite3

    from core.memory import MemoryManager

    db_path = tmp_path / "legacy-write.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE cache_request_diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            api_base TEXT NOT NULL,
            cache_scope TEXT NOT NULL,
            request_reason TEXT NOT NULL,
            messages_sha256 TEXT NOT NULL,
            system_sha256 TEXT NOT NULL,
            tools_sha256 TEXT NOT NULL,
            message_count INTEGER NOT NULL,
            system_message_count INTEGER NOT NULL,
            common_prefix_messages INTEGER NOT NULL,
            request_bytes INTEGER NOT NULL,
            system_bytes INTEGER NOT NULL,
            tools_bytes INTEGER NOT NULL,
            prompt_tokens INTEGER,
            cached_tokens INTEGER,
            cache_miss_tokens INTEGER,
            cache_reported INTEGER NOT NULL,
            usage_valid INTEGER NOT NULL,
            usage_errors TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO cache_request_diagnostics ("
        "ts, session_id, model, api_base, cache_scope, request_reason, "
        "messages_sha256, system_sha256, tools_sha256, message_count, "
        "system_message_count, common_prefix_messages, request_bytes, "
        "system_bytes, tools_bytes, cache_reported, usage_valid, usage_errors"
        ") VALUES (1, 'legacy-write-row', 'openai/qwen3.8-max', "
        "'https://dashscope.aliyuncs.com/compatible-mode/v1', 'main', '', "
        "'a', 'b', 'c', 1, 1, 0, 188, 90, 0, 0, 0, '[]')"
    )
    conn.commit()
    conn.close()

    manager = MemoryManager(str(db_path))
    try:
        columns = {
            row[1]
            for row in manager.conn.execute(
                "PRAGMA table_info(cache_request_diagnostics)"
            )
        }
        assert "cache_write_tokens" in columns
        legacy = manager.conn.execute(
            "SELECT cache_write_tokens FROM cache_request_diagnostics "
            "WHERE session_id = 'legacy-write-row'"
        ).fetchone()
        assert legacy == (None,)
    finally:
        manager.close()

    reopened = MemoryManager(str(db_path))
    try:
        assert "cache_write_tokens" in {
            row[1]
            for row in reopened.conn.execute(
                "PRAGMA table_info(cache_request_diagnostics)"
            )
        }
    finally:
        reopened.close()


def test_account_server_usage_forwards_cache_write_from_anthropic_shape(monkeypatch):
    import core.llm as llm

    persisted = []
    monkeypatch.setattr(
        llm, "_persist_token_usage",
        lambda *args, **kwargs: persisted.append(kwargs),
    )
    try:
        llm._account_server_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "total_tokens": 1020,
                "cache_read_input_tokens": 200,
                "cache_creation_input_tokens": 700,
            },
            model_name="anthropic/kimi-k3",
            api_base="https://api.kimi.com/coding/v1",
            session_id="accounting-write",
            cache_scope="main",
            invalidation_reason="",
        )
        assert persisted and persisted[0]["cache_write_tokens"] == 700

        persisted.clear()
        llm._account_server_usage(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 20,
                "total_tokens": 1020,
                "prompt_cache_hit_tokens": 200,
            },
            model_name="openai/deepseek-v4",
            api_base="https://relay.example/v1",
            session_id="accounting-write-none",
            cache_scope="main",
            invalidation_reason="",
        )
        assert persisted and "cache_write_tokens" not in persisted[0]
    finally:
        with llm.token_usage_lock:
            for key in [
                k for k in llm._cache_usage_states
                if str(k[0]).startswith("accounting-write")
            ]:
                del llm._cache_usage_states[key]


def test_usage_table_keeps_numeric_columns_aligned_for_long_route_labels(monkeypatch):
    import re

    rows = [
        {
            "provider": "openai", "model": "deepseek-v4.1-flash",
            "api_base": "https://cn-beijing.maas.aliyuncs.com/api/v1",
            "cache_scope": "main",
            "prompt": 100000, "completion": 500, "cached": 88000,
            "cache_prompt": 100000, "total": 100500,
        },
        {
            "provider": "openai", "model": "deepseek-v4.1-flash",
            "api_base": "https://cn-beijing.maas.aliyuncs.com/api/v1",
            "cache_scope": "keepalive",
            "prompt": 50000, "completion": 20, "cached": 2000,
            "cache_prompt": 50000, "total": 50020,
        },
    ]

    class Manager:
        def query_token_usage_by_model(self, start, end):
            return rows

        def query_cache_diagnostic_summary(self, *args, **kwargs):
            return {"count": 0, "lost_tokens": 0, "unreported": 0, "categories": {}}

    captured = []
    monkeypatch.setattr(
        main, "print_formatted_text",
        lambda html, *args, **kwargs: captured.append(str(html)),
    )

    main._render_usage_section(Manager(), "今日", "2026-08-09", "2026-08-09")

    lines = [re.sub(r"<[^>]+>", "", line) for line in captured]
    table_lines = lines[1:]
    assert len(table_lines) >= 4, lines
    widths = {main._term_display_width(line) for line in table_lines}
    assert len(widths) == 1, (widths, table_lines)


def test_status_token_info_reports_process_scope_only():
    text = main._compose_status_token_info(
        prompt="40.0M", completion="2.1M", cached="700.4K", global_rate="71%",
        ctx_pct="3%", ctx_used="4K", ctx_max="128K",
    )

    assert "本车道" not in text
    assert text.startswith("↑ 40.0M · ↓ 2.1M · ⚡ 700.4K (71%)")
    assert "上下文 3% (4K/128K)" in text
    assert "全局" not in text
    assert "保活" not in text


def test_usage_model_label_cell_is_bounded_but_keeps_channel_and_scope():
    label = (
        "openai/deepseek-v4.1-flash @ "
        "https://cn-beijing.maas.aliyuncs.com/api/v1 [keepalive]"
    )
    cell = main._truncate_visible(label, 60)

    assert main._term_display_width(cell) <= 60
    assert cell.startswith("openai/deepseek-v4.1-flash @ h")
    assert cell.endswith("iyuncs.com/api/v1 [keepalive]")
    assert "…" in cell
    assert main._truncate_visible("a/b", 60) == "a/b"


def test_get_token_usage_by_session_aggregates_conversation_and_keepalive_separately():
    manager = MemoryManager(":memory:")
    assert manager.get_token_usage_by_session("s1")["prompt"] == 0

    manager.add_token_usage(
        "openai", "m1", 300, 30, 5, 335, session_id="s1",
        api_base="https://a.example/v1", cache_scope="main", ts=1000,
    )
    manager.add_token_usage(
        "openai", "m1", 200, 20, None, 220, session_id="s1",
        api_base="https://a.example/v1", cache_scope="main", ts=2000,
    )
    manager.add_token_usage(
        "openai", "m1", 100, 10, 50, 160, session_id="s1", cache_scope="legacy", ts=2500,
    )
    # 保活单列，绝不混进对话口径
    manager.add_token_usage(
        "openai", "m1", 10, 1, 10, 11, session_id="s1",
        api_base="https://a.example/v1", cache_scope="keepalive", ts=2600,
    )
    manager.add_token_usage("openai", "m1", 999, 99, 0, 1098, session_id="s2")

    totals = manager.get_token_usage_by_session("s1")
    assert totals["prompt"] == 600
    assert totals["completion"] == 60
    assert totals["cached"] == 55
    assert totals["cache_prompt"] == 400
    assert totals["total"] == 715
    assert totals["requests"] == 3
    assert totals["last_ts"] == 2500
    assert totals["keepalive_prompt"] == 10
    assert totals["keepalive_requests"] == 1

    assert manager.get_token_usage_by_session("missing")["requests"] == 0


def test_list_token_usage_by_sessions_orders_by_total_and_skips_deleted():
    manager = MemoryManager(":memory:")
    manager.create_session_explicit("live-a")
    manager.create_session_explicit("live-b")
    manager.add_token_usage("openai", "m1", 10000, 100, 0, 10100, session_id="deleted")

    manager.add_token_usage("openai", "m1", 300, 30, 5, 335, session_id="live-a")
    manager.add_token_usage("openai", "m1", 100, 10, 0, 110, session_id="live-b")
    manager.add_token_usage(
        "openai", "m1", 10, 1, 10, 11, session_id="live-a", cache_scope="keepalive",
    )

    rows = manager.list_token_usage_by_sessions(limit=10)
    assert [row["session_id"] for row in rows] == ["live-a", "live-b"]
    assert rows[0]["total"] == 335
    assert rows[0]["cache_prompt"] == 300
    assert rows[1]["total"] == 110
    totals = manager.get_token_usage_totals()
    assert totals["prompt"] == 300 + 100 + 10000 + 10
    assert totals["requests"] == 4


def test_rename_session_migrates_token_usage_rows_in_same_transaction():
    manager = MemoryManager(":memory:")
    manager.create_session_explicit("old-name")
    manager.add_token_usage("openai", "m1", 300, 30, 5, 335, session_id="old-name")

    success, final_name = manager.rename_session("old-name", "new-name")
    assert success is True

    moved = manager.get_token_usage_by_session("new-name")
    assert moved["prompt"] == 300
    assert moved["requests"] == 1
    assert manager.get_token_usage_by_session("old-name")["requests"] == 0
