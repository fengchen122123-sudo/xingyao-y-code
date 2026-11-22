import pytest

import core.memory_compressor as mc
from core.memory import MemoryManager
from core.memory_compressor import compress_session_memory


class _Resp:
    def __init__(self, text):
        class _Msg:
            content = text
        class _Choice:
            message = _Msg()
        self.choices = [_Choice()]


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("chunk-session", workdir="GLOBAL", source="GUI")
    return m


def _seed_history(manager, count, filler=200):
    for i in range(count):
        manager.add_message(
            "chunk-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i} " + "内容填充 " * filler,
        )


@pytest.fixture(autouse=True)
def _no_secondary(monkeypatch):
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"compression": False}
    )
    fake_active = {
        "litellm_model": "fake/compress-model",
        "api_key": "k",
        "base_url": "",
        "context_window": 4096,
    }
    monkeypatch.setattr(
        "core.model_manager.get_active_model", lambda *a, **kw: fake_active
    )
    monkeypatch.setattr(
        "core.llm._provider_cache_affinity_options",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(mc, "_get_encoding", lambda *a, **kw: None)
    monkeypatch.setattr(mc, "COMPRESSION_INTER_CHUNK_DELAY_SECONDS", 0)


def test_long_history_is_compressed_in_window_sized_chunks(manager, monkeypatch):
    _seed_history(manager, 60)

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp(f"第 {len(prompts)} 段摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    ) is True

    assert len(prompts) >= 2
    budget = int(4096 * 0.25)
    for p in prompts:
        assert mc._estimate_tokens(p) <= budget + 1200

    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?",
        ("chunk-session",),
    )
    summary, boundary = cursor.fetchone()
    assert summary == f"第 {len(prompts)} 段摘要"
    assert boundary and boundary > 0


def test_chunked_merge_carries_summary_forward(manager, monkeypatch):
    _seed_history(manager, 60)

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp(f"摘要v{len(prompts)}")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    )

    assert len(prompts) >= 2
    assert "摘要v1" in prompts[1]
    assert "摘要v1" not in prompts[0]


def test_single_chunk_keeps_first_summary_length(manager, monkeypatch):
    _seed_history(manager, 11, filler=3)

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp("单一摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    ) is True
    assert len(prompts) == 1
    assert "前期历史记忆摘要" not in prompts[0]


def test_compression_request_uses_token_adaptive_timeout(manager, monkeypatch):
    _seed_history(manager, 11, filler=3)
    timeouts = []

    def fake_completion(**kwargs):
        timeouts.append(kwargs["timeout"])
        return _Resp("单一摘要")

    monkeypatch.setattr(mc, "_compression_request_timeout", lambda _tokens: 321)
    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager,
        "chunk-session",
        "fake/compress-model",
        "k",
        "",
        output_callback=lambda _message: None,
    ) is True
    assert timeouts == [321]


def test_empty_chunk_result_aborts_without_touching_db(manager, monkeypatch):
    _seed_history(manager, 60)
    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE session_name = ?",
        ("chunk-session",),
    )
    boundary_before = cursor.fetchone()[0]

    replies = iter(["第一段摘要", ""])

    def fake_completion(**kwargs):
        return _Resp(next(replies))

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    outcome = compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None, return_outcome=True,
    )

    assert outcome.status == "compressed"
    assert outcome.reason == "partial_empty_summary"

    cursor.execute(
        "SELECT summarized_until_msg_id, memory_summary FROM sessions WHERE session_name = ?",
        ("chunk-session",),
    )
    boundary_after, summary = cursor.fetchone()
    assert boundary_after > (boundary_before or 0)
    assert summary == "第一段摘要"
    first_two_ids = [
        row[0] for row in manager.conn.execute(
            "SELECT m.id FROM messages m JOIN sessions s ON m.session_id = s.id "
            "WHERE s.session_name = 'chunk-session' ORDER BY m.id LIMIT 2"
        )
    ]
    assert boundary_after == first_two_ids[0]


def test_chinese_long_message_truncated_by_token_not_chars(manager, monkeypatch):
    for i in range(12):
        manager.add_message("chunk-session", "user" if i % 2 == 0 else "assistant", f"短消息 {i}")
    manager.add_message("chunk-session", "assistant", "这是一段中文文本，用于模拟真实对话场景。" * 3000)
    manager.add_message("chunk-session", "user", "最后一条")

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp("摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    )

    assert len(prompts) >= 1
    budget = int(4096 * 0.25)
    for p in prompts:
        assert mc._estimate_tokens(p) <= budget + 1200


def test_compression_budget_capped_at_max_chunk_budget():
    assert mc._compression_chunk_budget(256000) == 24000
    assert mc._compression_chunk_budget(1048576) == 24000
    assert mc._compression_chunk_budget(4096) == 1024


def test_chunk_rate_limit_backoff_retry(manager, monkeypatch):
    _seed_history(manager, 12, filler=3)
    attempts = 0
    sleeps = []

    def fake_cancellable_sleep(duration, cancel_event=None):
        sleeps.append(duration)
        return True

    monkeypatch.setattr(mc, "_cancellable_sleep", fake_cancellable_sleep)

    def fake_completion(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise Exception("RateLimitError: 429 Too Many Requests - 请求过于频繁")
        return _Resp("重试成功摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    notices = []
    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: notices.append(m),
    ) is True

    assert attempts == 2
    assert len(sleeps) >= 1
    assert any("429" in n or "速率限制" in n for n in notices)


def test_chunk_rate_limit_respects_retry_after(manager, monkeypatch):
    _seed_history(manager, 12, filler=3)
    attempts = 0
    sleeps = []

    def fake_cancellable_sleep(duration, cancel_event=None):
        sleeps.append(duration)
        return True

    monkeypatch.setattr(mc, "_cancellable_sleep", fake_cancellable_sleep)

    class RateLimitWithHeader(Exception):
        def __init__(self):
            super().__init__("RateLimit 429")
            class _Resp:
                headers = {"retry-after": "12.5"}
            self.response = _Resp()
            self.status_code = 429

    def fake_completion(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitWithHeader()
        return _Resp("重试成功摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert attempts == 2
    assert 12.5 in sleeps


def test_cancellable_sleep_interrupts_on_cancel_event():
    import threading
    import time
    cancel_event = threading.Event()

    threading.Timer(0.05, cancel_event.set).start()

    start_t = time.monotonic()
    result = mc._cancellable_sleep(10.0, cancel_event)
    duration = time.monotonic() - start_t

    assert result is False
    assert duration < 1.0


def test_partial_progress_is_committed_when_budget_exceeded(manager, monkeypatch):
    import time as real_time
    _seed_history(manager, 40, filler=60)

    clock = {"t": 1000.0}
    monkeypatch.setattr(real_time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}
    def fake_completion(**kwargs):
        calls["n"] += 1
        clock["t"] += 90.0
        return _Resp("第 %d 段摘要内容" % calls["n"])
    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)
    monkeypatch.setattr(mc, "_cancellable_sleep", lambda *a, **kw: True)

    def read():
        row = manager.conn.execute(
            "SELECT summarized_until_msg_id, memory_summary FROM sessions WHERE session_name='chunk-session'"
        ).fetchone()
        return row[0] or 0, row[1]

    boundary_before, _ = read()
    outcome = compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None, max_timeout_seconds=180.0, return_outcome=True,
    )
    boundary_after, summary = read()

    assert calls["n"] < 40
    assert boundary_after > boundary_before
    assert summary
    assert outcome.status == "compressed"
