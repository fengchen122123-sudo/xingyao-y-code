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


def test_long_history_is_compressed_in_window_sized_chunks(manager, monkeypatch):
    _seed_history(manager, 60)  # 每条约 1.2k token，总远超 4096*0.4 预算

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
    budget = int(4096 * 0.4)
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


def test_empty_chunk_result_aborts_without_touching_db(manager, monkeypatch):
    """某段返回空摘要时中止且不推进边界，避免静默丢失该段事实。"""
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
    assert compress_session_memory(
        manager, "chunk-session", "fake/compress-model", "k", "", output_callback=lambda m: None
    ) is False

    cursor.execute(
        "SELECT summarized_until_msg_id FROM sessions WHERE session_name = ?",
        ("chunk-session",),
    )
    assert cursor.fetchone()[0] == boundary_before


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
    budget = int(4096 * 0.4)
    for p in prompts:
        # 每段 prompt 估算 token 必须在预算内（含指令与累积摘要的固定开销 ~1200）
        assert mc._estimate_tokens(p) <= budget + 1200
