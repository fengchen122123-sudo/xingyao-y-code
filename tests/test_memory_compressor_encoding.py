import sys
import threading
import time
import types

import pytest

import core.memory_compressor as mc


def _reset_encoding_state():
    mc._encoding_state.clear()
    mc._encoding_state.update({"status": "idle", "enc": None, "failed_at": None})


def _wait_for_status(expected):
    deadline = time.time() + 1
    while mc._encoding_state["status"] != expected and time.time() < deadline:
        threading.Event().wait(0.001)
    assert mc._encoding_state["status"] == expected


def test_failed_encoding_load_retries_after_cooldown(monkeypatch):
    """临时失败超过冷却期后必须重试；永久 failed 会让进程终身退回字符估算。"""
    _reset_encoding_state()
    attempts = []
    encoding = object()

    def get_encoding(_name):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OSError("temporary network failure")
        return encoding

    monkeypatch.setitem(
        sys.modules, "tiktoken", types.SimpleNamespace(get_encoding=get_encoding)
    )
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    assert mc._get_encoding(timeout=0.1) is None
    _wait_for_status("failed")
    assert attempts == [1]

    clock[0] = 399.0
    assert mc._get_encoding(timeout=0.1) is None
    assert attempts == [1]

    clock[0] = 401.0
    assert mc._get_encoding(timeout=0.1) is encoding
    assert attempts == [1, 2]
    assert mc._get_encoding(timeout=0.1) is encoding
    assert attempts == [1, 2]


def test_compression_budget_uses_quarter_of_secondary_window():
    """副模型分段必须留出足够处理余量，不能继续发送约 40% 窗口的大请求。"""
    assert mc._compression_chunk_budget(256000) == 64000
    assert mc._compression_chunk_budget(4096) == 1024
    assert mc._compression_chunk_budget(1000) == 512


@pytest.mark.parametrize(
    ("prompt_tokens", "expected_timeout"),
    [(0, 90), (64000, 132), (1000000, 600)],
)
def test_compression_timeout_scales_with_prompt_tokens(
    prompt_tokens, expected_timeout
):
    """约 64K token 的摘要请求不能沿用会稳定失败的固定 60 秒超时。"""
    assert mc._compression_request_timeout(prompt_tokens) == expected_timeout


def test_truncated_message_includes_suffix_within_token_budget(monkeypatch):
    """二分前缀必须为截断标记预留 token，拼接后的正文仍不得超过预算。"""
    monkeypatch.setattr(mc, "_estimate_tokens", lambda text: len(text))
    content = "长" * 5000

    truncated, token_count = mc._truncate_to_token_budget(content, 100)

    assert truncated.endswith(mc.TRUNCATION_SUFFIX)
    assert token_count == len(truncated)
    assert token_count <= 100
