import json


def test_request_diagnostic_is_deterministic_and_contains_no_plaintext():
    from core.llm import _build_cache_request_diagnostic

    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "SECRET-USER-TEXT"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "secret_tool",
            "description": "SECRET-TOOL-ARG",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    first = _build_cache_request_diagnostic(
        "diag-session", "main", "OPENAI/DeepSeek-V4",
        "https://API.EXAMPLE/v1/", tools, messages,
    )
    second = _build_cache_request_diagnostic(
        "diag-session", "main", "OPENAI/DeepSeek-V4",
        "https://API.EXAMPLE/v1/", tools, messages,
    )

    assert first == second
    assert first["model"] == "openai/deepseek-v4"
    assert first["api_base"] == "https://api.example/v1"
    assert first["message_count"] == 2
    assert first["system_message_count"] == 1
    for key in ("messages_sha256", "system_sha256", "tools_sha256"):
        assert len(first[key]) == 64
    serialized = json.dumps(first, ensure_ascii=False)
    assert "SECRET-USER-TEXT" not in serialized
    assert "SECRET-TOOL-ARG" not in serialized


def test_request_details_measure_append_and_middle_rewrite_prefixes():
    from core.llm import _classify_cache_request_details

    session = "diag-prefix-session"
    base = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "first"},
    ]
    first = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", base,
    )
    appended = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", base + [{"role": "assistant", "content": "answer"}],
    )
    rewritten = _classify_cache_request_details(
        session, "main", "openai/deepseek-v4", "https://api.example/v1",
        "tools-v1", [
            {"role": "system", "content": "changed"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
        ],
    )

    assert first["request_reason"] == "冷启动"
    assert first["common_prefix_messages"] == 0
    assert appended["request_reason"] == ""
    assert appended["common_prefix_messages"] == len(base)
    assert rewritten["request_reason"] == "上下文压缩或历史重写"
    assert rewritten["common_prefix_messages"] == 0


def test_persist_diagnostic_distinguishes_valid_invalid_and_missing_usage(monkeypatch):
    import core.app_context as app_context
    from core.llm import _persist_cache_request_diagnostic

    captured = []

    class FakeManager:
        def add_cache_request_diagnostic(self, diagnostic):
            captured.append(diagnostic)

    monkeypatch.setattr(app_context, "get_manager", lambda: FakeManager())
    details = {
        "session_id": "persist-diag",
        "model": "openai/deepseek-v4",
        "api_base": "https://api.example/v1",
        "cache_scope": "main",
        "request_reason": "",
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 2,
        "system_message_count": 1,
        "common_prefix_messages": 1,
        "request_bytes": 100,
        "system_bytes": 50,
        "tools_bytes": 75,
    }
    valid_usage = {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 10,
    }
    invalid_usage = {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 9,
    }

    _persist_cache_request_diagnostic(details, valid_usage)
    _persist_cache_request_diagnostic(details, invalid_usage)
    _persist_cache_request_diagnostic(details, None)

    assert captured[0]["usage_valid"] is True
    assert captured[0]["prompt_tokens"] == 1000
    assert captured[0]["cached_tokens"] == 990
    assert captured[0]["cache_miss_tokens"] == 10
    assert captured[1]["usage_valid"] is False
    assert any("cache hit + miss" in err for err in captured[1]["usage_errors"])
    assert captured[2]["usage_valid"] is False
    assert captured[2]["usage_errors"] == ("usage missing",)


def test_diagnostic_records_route_override_sha256():
    from core.llm import _build_cache_request_diagnostic

    diag = _build_cache_request_diagnostic(
        "sess", "main", "openai/deepseek-flash", "https://tokenrhythm.studio/v1",
        [], [{"role": "system", "content": "x"}],
        route_override_fingerprint="deadbeef",
    )
    assert diag["route_override_sha256"] == "deadbeef"


def test_diagnostic_defaults_to_empty_without_override():
    from core.llm import _build_cache_request_diagnostic

    diag = _build_cache_request_diagnostic(
        "sess", "main", "openai/deepseek-flash", "https://api.deepseek.com",
        [], [{"role": "system", "content": "x"}],
    )
    assert diag["route_override_sha256"] == ""


def test_route_override_fingerprint_records_without_counting_as_rotation():
    """覆盖指纹变化只更新记录值，不得被换代判定误判成一次缓存换代。"""
    from core.llm import _classify_cache_request_details

    session = "diag-override-rotation"
    base = [{"role": "system", "content": "stable"}]
    first = _classify_cache_request_details(
        session, "main", "openai/deepseek-flash", "https://tokenrhythm.studio/v1",
        "tools-v1", base, route_override_fingerprint="aa",
    )
    second = _classify_cache_request_details(
        session, "main", "openai/deepseek-flash", "https://tokenrhythm.studio/v1",
        "tools-v1", base, route_override_fingerprint="bb",
    )

    assert first["request_reason"] == "冷启动"
    assert first["route_override_sha256"] == "aa"
    assert second["request_reason"] == ""
    assert second["route_override_sha256"] == "bb"


def test_legacy_cache_diagnostic_table_migrates_route_override_column(tmp_path):
    """老库升级：缺 route_override_sha256 列触发迁移；老调用不传该字段也能落库。"""
    import sqlite3

    from core.memory import MemoryManager

    db_path = tmp_path / "legacy-cache-diagnostics.db"
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
    conn.commit()
    conn.close()

    manager = MemoryManager(str(db_path))
    columns = {
        row[1]
        for row in manager.conn.execute(
            "PRAGMA table_info(cache_request_diagnostics)"
        )
    }
    assert "route_override_sha256" in columns

    manager.add_cache_request_diagnostic({
        "session_id": "legacy-path",
        "model": "openai/deepseek-v4",
        "api_base": "https://api.example/v1",
        "cache_scope": "main",
        "request_reason": "",
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 1,
        "system_message_count": 1,
        "common_prefix_messages": 0,
        "request_bytes": 10,
        "system_bytes": 5,
        "tools_bytes": 0,
        "cache_reported": False,
        "usage_valid": False,
        "usage_errors": ("usage missing",),
    })
    row = manager.conn.execute(
        "SELECT route_override_sha256 FROM cache_request_diagnostics "
        "WHERE session_id = 'legacy-path'"
    ).fetchone()
    assert row == ("",)
