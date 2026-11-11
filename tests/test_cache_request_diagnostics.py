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


# ---------------------------------------------------------------------------
# Task 2（跨渠道缓存命中率修复）：诊断指纹统一为逻辑前缀口径 + 线上字节事实
# ---------------------------------------------------------------------------


def test_content_block_cache_control_is_stripped_from_logical_prefix():
    """Qwen 形态（content 块标记）不得进入逻辑前缀口径。

    同一批消息，未打标版与 `_apply_cache_control` 打标后的版本，per-message
    指纹与 messages_sha256 必须相等；否则「诊断显示字节没变」的归因不成立
    （设计 §1.5 / §五 Task 2）。
    """
    import copy

    from core.llm import (
        _apply_cache_control,
        _build_cache_request_diagnostic,
        _cache_message_fingerprints,
        _classify_cache_request_details,
    )

    model = "openai/qwen3.8-max"
    api_base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "round 1 question"},
        {"role": "assistant", "content": "round 1 answer"},
        {"role": "user", "content": "round 2 question"},
    ]
    marked = _apply_cache_control(copy.deepcopy(messages), model, api_base)
    # 前提：确实是 Qwen 形态——content 被改写成带块级 cache_control 的列表
    assert isinstance(marked[0]["content"], list)
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(marked[-1]["content"], list)
    assert marked[-1]["content"][0]["cache_control"] == {"type": "ephemeral"}

    assert _cache_message_fingerprints(marked) == _cache_message_fingerprints(messages)

    plain = _build_cache_request_diagnostic(
        "logical-prefix", "main", model, api_base, [], messages
    )
    marked_diag = _build_cache_request_diagnostic(
        "logical-prefix", "main", model, api_base, [], marked
    )
    assert marked_diag["messages_sha256"] == plain["messages_sha256"]
    assert marked_diag["system_sha256"] == plain["system_sha256"]
    assert marked_diag["request_bytes"] == plain["request_bytes"]

    # 会话内「带标记重发」不得被自己的标记误判成一次缓存换代
    session = "logical-prefix-session"
    first = _classify_cache_request_details(
        session, "main", model, api_base, "tools-v1", copy.deepcopy(messages)
    )
    second = _classify_cache_request_details(
        session, "main", model, api_base, "tools-v1",
        _apply_cache_control(copy.deepcopy(messages), model, api_base),
    )
    assert first["request_reason"] == "冷启动"
    assert second["request_reason"] == ""
    assert second["common_prefix_messages"] == len(messages)


def test_message_level_cache_control_is_stripped_for_anthropic_form():
    """Anthropic 形态（消息顶层标记）的回归保护：剥标记前后指纹必须相等。"""
    import copy

    from core.llm import _build_cache_request_diagnostic, _cache_message_fingerprints

    base = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    marked = [copy.deepcopy(message) for message in base]
    marked[0]["cache_control"] = {"type": "ephemeral"}
    marked[2]["cache_control"] = {"type": "ephemeral"}

    assert _cache_message_fingerprints(marked) == _cache_message_fingerprints(base)
    plain = _build_cache_request_diagnostic(
        "sess", "main", "anthropic/claude-sonnet", "https://api.anthropic.com", [], base
    )
    marked_diag = _build_cache_request_diagnostic(
        "sess", "main", "anthropic/claude-sonnet", "https://api.anthropic.com", [], marked
    )
    assert marked_diag["messages_sha256"] == plain["messages_sha256"]
    assert marked_diag["request_bytes"] == plain["request_bytes"]


def test_strip_helper_preserves_every_field_except_cache_control():
    """铁律：剥离只动 cache_control 键；content 块的 type/text 原样保留，
    且不修改调用方传入的消息对象。"""
    import copy

    from core.llm import _strip_cache_control_markers

    messages = [
        {
            "role": "system",
            "content": [{
                "type": "text",
                "text": "stable system",
                "cache_control": {"type": "ephemeral"},
            }],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看看这张图"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
    ]
    snapshot = copy.deepcopy(messages)

    stripped = _strip_cache_control_markers(messages)

    assert messages == snapshot  # 原对象一个字节都没动
    # 打标引入的单文本块包装还原为客户端原字符串（Qwen 打标前形态）
    assert stripped[0]["content"] == "stable system"
    # 多块 content（含非文本块）保持列表形态，只剥 cache_control
    assert stripped[1]["content"] == [
        {"type": "text", "text": "看看这张图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


def test_legacy_cache_diagnostic_table_migrates_online_byte_columns(tmp_path):
    """老库升级：缺 cache_control_markers / request_bytes_final 两列时自动补列，
    老行保持 NULL（未观测 ≠ 0），重复打开幂等不报错。"""
    import sqlite3

    from core.memory import MemoryManager

    db_path = tmp_path / "legacy-online-byte-facts.db"
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
            usage_errors TEXT NOT NULL,
            route_override_sha256 TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute(
        "INSERT INTO cache_request_diagnostics ("
        "ts, session_id, model, api_base, cache_scope, request_reason, "
        "messages_sha256, system_sha256, tools_sha256, message_count, "
        "system_message_count, common_prefix_messages, request_bytes, "
        "system_bytes, tools_bytes, cache_reported, usage_valid, usage_errors"
        ") VALUES (1, 'legacy-row', 'openai/qwen3.8-max', "
        "'https://dashscope.aliyuncs.com/compatible-mode/v1', 'main', '', "
        "'a', 'b', 'c', 1, 1, 0, 188, 90, 0, 0, 0, '[]')"
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
    assert "cache_control_markers" in columns
    assert "request_bytes_final" in columns
    legacy = manager.conn.execute(
        "SELECT cache_control_markers, request_bytes_final "
        "FROM cache_request_diagnostics WHERE session_id = 'legacy-row'"
    ).fetchone()
    assert legacy == (None, None)

    base_row = {
        "model": "openai/qwen3.8-max",
        "api_base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "cache_scope": "main",
        "request_reason": "",
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 1,
        "system_message_count": 1,
        "common_prefix_messages": 0,
        "request_bytes": 188,
        "system_bytes": 90,
        "tools_bytes": 0,
        "cache_reported": False,
        "usage_valid": False,
        "usage_errors": (),
    }
    # 新记录写真实观测值：真的没标记写 0，未打标字节数等于 request_bytes
    manager.add_cache_request_diagnostic(dict(
        base_row, session_id="new-row", cache_control_markers=0,
        request_bytes_final=188,
    ))
    # 未观测（老调用方不传新字段）→ NULL，不冒充 0
    manager.add_cache_request_diagnostic(dict(base_row, session_id="unobserved-row"))

    new_rows = manager.query_cache_request_diagnostics(session_id="new-row")
    assert new_rows[0]["cache_control_markers"] == 0
    assert new_rows[0]["request_bytes_final"] == 188
    unobserved = manager.query_cache_request_diagnostics(session_id="unobserved-row")
    assert unobserved[0]["cache_control_markers"] is None
    assert unobserved[0]["request_bytes_final"] is None
    manager.close()

    # 重复打开幂等不报错，列仍在
    reopened = MemoryManager(str(db_path))
    try:
        columns = {
            row[1]
            for row in reopened.conn.execute(
                "PRAGMA table_info(cache_request_diagnostics)"
            )
        }
        assert {"cache_control_markers", "request_bytes_final"} <= columns
    finally:
        reopened.close()


def _final_usage_object():
    from types import SimpleNamespace

    return SimpleNamespace(
        total_tokens=1100,
        prompt_tokens=1000,
        completion_tokens=100,
        prompt_cache_hit_tokens=990,
        prompt_cache_miss_tokens=10,
        prompt_tokens_details=None,
    )


def _stream_chunk(content=""):
    from types import SimpleNamespace

    delta = SimpleNamespace(
        content=content,
        reasoning_content="",
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _run_one_diagnostic_turn(monkeypatch, *, model, api_base, messages, session_id):
    """跑一个最小流式回合，返回（线上 provider messages, 捕获的诊断对象）。"""
    from core.llm import chat_with_model_events

    monkeypatch.setattr("core.model_manager.load_config", lambda: {})

    class FakeStream:
        chunks = [_final_usage_object()]

        def __iter__(self):
            return iter([_stream_chunk("完成")])

    payloads = []
    diagnostics = []

    def fake_completion(**kwargs):
        payloads.append(kwargs)
        return FakeStream()

    monkeypatch.setattr("core.llm.completion", fake_completion)
    monkeypatch.setattr(
        "core.llm._persist_cache_request_diagnostic",
        lambda details, usage: diagnostics.append(details),
    )

    events = list(chat_with_model_events(
        model,
        "fake-key",
        messages,
        api_base,
        persist=False,
        turn_id=f"turn-{session_id}",
        consume_background_notifications=False,
        cache_scope="main",
        cache_session_id=session_id,
    ))
    assert events[-1].kind == "turn_finished"
    assert len(payloads) == 1
    assert len(diagnostics) == 1
    return payloads[0]["messages"], diagnostics[0]


def test_online_byte_facts_recorded_for_marked_request(monkeypatch):
    """带显式标记的请求：记下真实标记数与打标后规范化字节数（同一行），
    且逻辑前缀 sha256 等于剥离标记后的线上字节。"""
    import hashlib

    from core.llm import _canonical_diagnostic_json, _strip_cache_control_markers

    online_messages, details = _run_one_diagnostic_turn(
        monkeypatch,
        model="openai/qwen3.8-max",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        messages=[
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "hello"},
        ],
        session_id="online-byte-facts-marked",
    )

    # 前提：线上报文确实带显式标记（Qwen 形态：system + 尾部 user 各 1 个）
    assert isinstance(online_messages[0]["content"], list)
    assert online_messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert isinstance(online_messages[-1]["content"], list)

    assert details["cache_control_markers"] == 2
    assert details["request_bytes_final"] == len(
        _canonical_diagnostic_json(online_messages).encode("utf-8")
    )
    assert details["request_bytes_final"] > details["request_bytes"]

    # 逻辑前缀口径：sha256 / request_bytes 等于剥离标记后的线上字节
    stripped = _strip_cache_control_markers(online_messages)
    assert details["messages_sha256"] == hashlib.sha256(
        _canonical_diagnostic_json(stripped).encode("utf-8")
    ).hexdigest()
    assert details["request_bytes"] == len(
        _canonical_diagnostic_json(stripped).encode("utf-8")
    )
    # 中间态绝不漏进持久化字段
    assert "_message_fingerprints" not in details


def test_online_byte_facts_record_zero_for_unmarked_request(monkeypatch):
    """未打标车道：零标记，打标后字节数等于 request_bytes（字节零变化）。"""
    from core.llm import _canonical_diagnostic_json

    messages = [
        {"role": "system", "content": "stable system"},
        {"role": "user", "content": "hello"},
    ]
    online_messages, details = _run_one_diagnostic_turn(
        monkeypatch,
        model="openai/glm-5.3",
        api_base="https://online-byte-facts.example/v1",
        messages=messages,
        session_id="online-byte-facts-unmarked",
    )

    # 注意：chat_with_model_events 会注入系统提示词（含系统级段落），
    # 线上 messages 不等于入参原样；这里断言的是「零标记」这一事实本身。
    assert all("cache_control" not in message for message in online_messages)
    assert all(
        not isinstance(message.get("content"), list)
        or all("cache_control" not in block for block in message["content"])
        for message in online_messages
    )
    assert details["cache_control_markers"] == 0
    assert details["request_bytes_final"] == details["request_bytes"]
    assert details["request_bytes_final"] == len(
        _canonical_diagnostic_json(online_messages).encode("utf-8")
    )
