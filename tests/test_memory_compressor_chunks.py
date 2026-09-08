"""后台记忆压缩分段回归。

长会话历史超过压缩模型上下文窗口时，压缩请求本身超窗失败并反复重试
（88 万 token 历史 vs 26 万窗口）。分段压缩把历史按窗口安全预算切成
多段，逐段与累积摘要合并，每次请求都必须落在窗口内。
"""

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
    """写入 count 条 user/assistant 交替消息，每条填充 filler 个内容词。"""
    for i in range(count):
        manager.add_message(
            "chunk-session",
            "user" if i % 2 == 0 else "assistant",
            f"消息 {i} " + "内容填充 " * filler,
        )


@pytest.fixture(autouse=True)
def _no_secondary(monkeypatch):
    """禁用副模型、固定主模型窗口、强制走字符估算（不联网加载 tiktoken）。"""
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
    _seed_history(manager, 60)  # 每条约 1.2k token，总远超 4096*0.25 预算

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp(f"第 {len(prompts)} 段摘要")

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)

    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    ) is True

    # 必须切成了多段，且每段 prompt 估算 token 都在窗口预算内
    assert len(prompts) >= 2
    budget = int(4096 * 0.25)
    for p in prompts:
        assert mc._estimate_tokens(p) <= budget + 1200  # 指令与累积摘要的固定开销

    # 摘要与边界已写库
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

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
    compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    )

    # 第二段起必须携带上一段的累积摘要（合并语义）
    assert len(prompts) >= 2
    assert "摘要v1" in prompts[1]
    assert "摘要v1" not in prompts[0]


def test_single_chunk_keeps_first_summary_length(manager, monkeypatch):
    """短历史不切段：单次调用且走首次总结（无前期摘要分支）。"""
    # 消息数须超过 retain_count(8)+2 才会真正压缩；内容极短使总量在单段预算内
    _seed_history(manager, 11, filler=3)

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp("单一摘要")

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    ) is True
    assert len(prompts) == 1
    assert "前期历史记忆摘要" not in prompts[0]


def test_compression_request_uses_token_adaptive_timeout(manager, monkeypatch):
    """压缩调用必须使用按本段 token 计算的超时，而不是固定 60 秒。"""
    _seed_history(manager, 11, filler=3)
    timeouts = []

    def fake_completion(**kwargs):
        timeouts.append(kwargs["timeout"])
        return _Resp("单一摘要")

    monkeypatch.setattr(mc, "_compression_request_timeout", lambda _tokens: 321)
    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)

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
    """某段返回空摘要时中止：空段不推进水位，已完成段落进度保留（不白烧）。"""
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

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
    outcome = compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None, return_outcome=True,
    )

    # 中止于空段但已有部分进度落库 → compressed（下游淘汰有摘要兜底）
    assert outcome.status == "compressed"
    assert outcome.reason == "partial_empty_summary"

    cursor.execute(
        "SELECT summarized_until_msg_id, memory_summary FROM sessions WHERE session_name = ?",
        ("chunk-session",),
    )
    boundary_after, summary = cursor.fetchone()
    # 第一段进度已落库
    assert boundary_after > (boundary_before or 0)
    assert summary == "第一段摘要"
    # 空段不推进水位：只推进到第一条消息（filler=200 时单段单条）
    first_two_ids = [
        row[0] for row in manager.conn.execute(
            "SELECT m.id FROM messages m JOIN sessions s ON m.session_id = s.id "
            "WHERE s.session_name = 'chunk-session' ORDER BY m.id LIMIT 2"
        )
    ]
    assert boundary_after == first_two_ids[0]


def test_chinese_long_message_truncated_by_token_not_chars(manager, monkeypatch):
    """中文长消息截断按 token 而非字符：避免 '2字符=1token' 导致超窗。

    中文 1 字符 ≈ 1~1.5 token，按字符截会把 budget*2 的字符量塞进去，
    实际 token 远超窗口。修复后按 token 二分截断，确保不超预算。
    """
    # 写入足够多消息触发压缩，其中一条超长中文（远超 4096 窗口）
    for i in range(12):
        manager.add_message("chunk-session", "user" if i % 2 == 0 else "assistant", f"短消息 {i}")
    manager.add_message("chunk-session", "assistant", "这是一段中文文本，用于模拟真实对话场景。" * 3000)
    manager.add_message("chunk-session", "user", "最后一条")

    prompts = []

    def fake_completion(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _Resp("摘要")

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
    compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    )

    # 压缩被触发且生成了请求
    assert len(prompts) >= 1
    budget = int(4096 * 0.25)
    for p in prompts:
        # 每段 prompt 估算 token 必须在预算内（含指令与累积摘要的固定开销 ~1200）
        assert mc._estimate_tokens(p) <= budget + 1200


def test_compression_budget_capped_at_max_chunk_budget():
    """超大窗口（如 256k / 1M）的预算必须受上限 24000 约束，避免打爆 TPM。"""
    # 256k * 0.25 = 64k -> 应被截断为 24000
    assert mc._compression_chunk_budget(256000) == 24000
    # 1M * 0.25 = 262k -> 应被截断为 24000
    assert mc._compression_chunk_budget(1048576) == 24000
    # 小窗口（4096 * 0.25 = 1024）保持真实 25%
    assert mc._compression_chunk_budget(4096) == 1024


def test_chunk_rate_limit_backoff_retry(manager, monkeypatch):
    """测试当单段遭遇 429 RateLimit 时能够指数退避重试并成功。"""
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

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)

    notices = []
    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: notices.append(m),
    ) is True

    assert attempts == 2
    assert len(sleeps) >= 1
    assert any("429" in n or "速率限制" in n for n in notices)


def test_chunk_rate_limit_respects_retry_after(manager, monkeypatch):
    """测试当 RateLimit 异常包含 Retry-After 时优先使用指定休眠时间。"""
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

    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)

    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "",
        output_callback=lambda m: None,
    ) is True

    assert attempts == 2
    assert len(sleeps) == 1
    assert sleeps[0] == 12.5


def test_cancellable_sleep_interrupts_on_cancel_event():
    """测试 _cancellable_sleep 在 cancel_event 触发时立即提前退出。"""
    import threading
    import time
    cancel_event = threading.Event()

    # 预设在 0.05 秒后触发取消
    threading.Timer(0.05, cancel_event.set).start()

    start_t = time.monotonic()
    # 期望休眠 10 秒，但应在 ~0.05 秒被打断并返回 False
    result = mc._cancellable_sleep(10.0, cancel_event)
    duration = time.monotonic() - start_t

    assert result is False
    assert duration < 1.0


def test_partial_progress_is_committed_when_budget_exceeded(manager, monkeypatch):
    """预算耗尽中止时，已完成段落的摘要与水位必须已落库（不白烧）。"""
    import time as real_time
    _seed_history(manager, 40, filler=60)          # 造出多段

    clock = {"t": 1000.0}
    monkeypatch.setattr(real_time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}
    def fake_completion(**kwargs):
        calls["n"] += 1
        clock["t"] += 90.0                          # 每段假装耗时 90s
        return _Resp("第 %d 段摘要内容" % calls["n"])
    monkeypatch.setattr("core.llm._litellm_completion", fake_completion)
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

    assert calls["n"] < 40                          # 确实中途停了
    assert boundary_after > boundary_before         # 已完成段落水位已推进
    assert summary                                   # 摘要已落库
    assert outcome.status == "compressed"            # 有兜底 → 下游可正常微调淘汰

