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


FAKE_SNAPSHOT = {
    "model": "fake/compress-model",
    "api_key": "k",
    "api_base": "",
    "messages": [
        {"role": "system", "content": "稳定系统提示 " * 20},
        {"role": "user", "content": "用户消息 " * 50},
        {"role": "assistant", "content": "助手回复 " * 50},
    ],
    "tools": [{"type": "function", "function": {"name": "read_file"}}],
    "stream": True,
}


@pytest.fixture
def manager():
    m = MemoryManager(":memory:")
    m.create_session_explicit("insert-session", workdir="GLOBAL", source="GUI")
    return m


def _seed_history(manager, count, filler=200):
    for i in range(count):
        manager.add_message(
            "insert-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i} " + "内容填充 " * filler,
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(
        "core.model_manager.get_secondary_roles", lambda: {"compression": False}
    )
    fake_active = {
        "litellm_model": "fake/compress-model",
        "api_key": "k",
        "base_url": "",
        "context_window": 131072,
    }
    monkeypatch.setattr(
        "core.model_manager.get_active_model", lambda *a, **kw: fake_active
    )
    monkeypatch.setattr("core.llm._provider_cache_affinity_options", lambda *a, **kw: {})
    monkeypatch.setattr(mc, "_get_encoding", lambda *a, **kw: None)
    monkeypatch.setattr(mc, "COMPRESSION_INTER_CHUNK_DELAY_SECONDS", 0)


def test_insert_path_reuses_snapshot_bytes_and_commits(manager, monkeypatch):
    _seed_history(manager, 30)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: dict(FAKE_SNAPSHOT))
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return _Resp("<topics>测试,缓存</topics>\n合并后的完整记忆摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(calls) == 1
    req = calls[0]
    assert req["model"] == "fake/compress-model"
    assert req["tools"] == FAKE_SNAPSHOT["tools"]
    assert req["messages"][:-1] == FAKE_SNAPSHOT["messages"]
    assert req["messages"][-1]["role"] == "user"
    assert "记忆压缩" in req["messages"][-1]["content"]
    assert req["stream"] is False
    assert req["max_tokens"] == mc.INSERT_SUMMARY_MAX_TOKENS

    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT memory_summary, summarized_until_msg_id FROM sessions WHERE session_name = ?",
        ("insert-session",),
    )
    summary, boundary = cursor.fetchone()
    assert summary == "合并后的完整记忆摘要"
    cursor.execute(
        "SELECT topics FROM session_summary_generations "
        "WHERE session_name = ? ORDER BY gen DESC LIMIT 1",
        ("insert-session",),
    )
    assert cursor.fetchone()[0] == "测试,缓存"
    assert boundary and boundary > 0


def test_missing_snapshot_falls_back_to_chunked_path(manager, monkeypatch):
    _seed_history(manager, 60)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: None)
    seen = []

    def fake_completion(**kwargs):
        seen.append(kwargs)
        return _Resp(f"分段摘要v{len(seen)}")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(seen) >= 2
    assert all(len(kw["messages"]) == 1 for kw in seen)
    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT memory_summary FROM sessions WHERE session_name = ?", ("insert-session",)
    )
    assert cursor.fetchone()[0] == f"分段摘要v{len(seen)}"


def test_snapshot_model_mismatch_falls_back(manager, monkeypatch):
    _seed_history(manager, 30)
    stale = dict(FAKE_SNAPSHOT, model="other/model")
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: stale)
    seen = []

    def fake_completion(**kwargs):
        seen.append(kwargs)
        return _Resp("回退摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(seen) >= 1
    assert all(kw["model"] == "fake/compress-model" for kw in seen)
    assert all(len(kw["messages"]) == 1 for kw in seen)


def test_insert_request_failure_falls_back(manager, monkeypatch):
    _seed_history(manager, 30)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: dict(FAKE_SNAPSHOT))
    seen = []

    def fake_completion(**kwargs):
        seen.append(kwargs)
        if len(kwargs["messages"]) > 1:
            raise RuntimeError("insert boom")
        return _Resp("回退摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(seen) == 2
    assert len(seen[0]["messages"]) > 1 and len(seen[1]["messages"]) == 1
    cursor = manager.conn.cursor()
    cursor.execute(
        "SELECT memory_summary FROM sessions WHERE session_name = ?", ("insert-session",)
    )
    assert cursor.fetchone()[0] == "回退摘要"


def test_insert_mode_disabled_falls_back(manager, monkeypatch):
    _seed_history(manager, 30)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: dict(FAKE_SNAPSHOT))
    real_config = mc._load_compression_config
    cfg = {**real_config(), "insert_mode": False}
    monkeypatch.setattr(mc, "_load_compression_config", lambda: cfg)
    seen = []

    def fake_completion(**kwargs):
        seen.append(kwargs)
        return _Resp("回退摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(seen) >= 1
    assert all(len(kw["messages"]) == 1 for kw in seen)


def test_snapshot_over_window_budget_falls_back(manager, monkeypatch):
    _seed_history(manager, 30)
    monkeypatch.setattr(mc, "_get_keepalive_snapshot", lambda s: dict(FAKE_SNAPSHOT))
    tiny_window = {
        "litellm_model": "fake/compress-model",
        "api_key": "k",
        "base_url": "",
        "context_window": 512,
    }
    monkeypatch.setattr("core.model_manager.get_active_model", lambda *a, **kw: tiny_window)
    seen = []

    def fake_completion(**kwargs):
        seen.append(kwargs)
        return _Resp("回退摘要")

    monkeypatch.setattr("core.llm._dispatch_completion", fake_completion)

    assert compress_session_memory(
        manager, "insert-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert len(seen) >= 1
    assert all(len(kw["messages"]) == 1 for kw in seen)


def test_topics_splitter_strips_tag_and_keeps_body():
    topics, clean = mc._split_topics_from_summary("<topics>a, b</topics>\n正文内容")
    assert topics == "a, b"
    assert clean == "正文内容"
    topics2, clean2 = mc._split_topics_from_summary("没有标签的摘要")
    assert topics2 is None
    assert clean2 == "没有标签的摘要"
