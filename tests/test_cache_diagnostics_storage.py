import json
import time

import pytest

from core.memory import MemoryManager


def _ts(date_text, clock="12:00:00"):
    return int(time.mktime(time.strptime(
        f"{date_text} {clock}", "%Y-%m-%d %H:%M:%S"
    )))


def _diagnostic(**overrides):
    row = {
        "ts": _ts("2026-08-09"),
        "session_id": "diag-session",
        "model": "openai/deepseek-v4",
        "api_base": "https://api.example/v1",
        "cache_scope": "main",
        "request_reason": "",
        "messages_sha256": "a" * 64,
        "system_sha256": "b" * 64,
        "tools_sha256": "c" * 64,
        "message_count": 3,
        "system_message_count": 1,
        "common_prefix_messages": 2,
        "request_bytes": 1000,
        "system_bytes": 300,
        "tools_bytes": 500,
        "prompt_tokens": 30000,
        "cached_tokens": 1000,
        "cache_miss_tokens": 29000,
        "cache_reported": True,
        "usage_valid": True,
        "usage_errors": (),
    }
    row.update(overrides)
    return row


def test_cache_diagnostic_round_trip_preserves_zero_null_and_errors():
    manager = MemoryManager(":memory:")
    manager.add_cache_request_diagnostic(_diagnostic(
        session_id="zero-hit", cached_tokens=0, cache_miss_tokens=30000,
    ))
    manager.add_cache_request_diagnostic(_diagnostic(
        session_id="unreported", cached_tokens=None, cache_miss_tokens=None,
        cache_reported=False, usage_errors=("usage missing cache details",),
    ))

    rows = manager.query_cache_request_diagnostics()
    zero = next(row for row in rows if row["session_id"] == "zero-hit")
    unknown = next(row for row in rows if row["session_id"] == "unreported")

    assert zero["cached_tokens"] == 0
    assert zero["cache_reported"] is True
    assert unknown["cached_tokens"] is None
    assert unknown["cache_miss_tokens"] is None
    assert unknown["cache_reported"] is False
    assert unknown["usage_errors"] == ["usage missing cache details"]
    assert "SECRET" not in json.dumps(rows, ensure_ascii=False)


def test_cache_diagnostic_commit_failure_rolls_back_and_connection_recovers():
    manager = MemoryManager(":memory:")
    real_conn = manager.conn

    class FailCommitOnce:
        def __init__(self, connection):
            self.connection = connection
            self.fail = True
            self.rollbacks = 0

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self):
            if self.fail:
                self.fail = False
                raise RuntimeError("commit failed")
            return self.connection.commit()

        def rollback(self):
            self.rollbacks += 1
            return self.connection.rollback()

    proxy = FailCommitOnce(real_conn)
    manager.conn = proxy

    with pytest.raises(RuntimeError, match="commit failed"):
        manager.add_cache_request_diagnostic(_diagnostic(session_id="failed"))
    assert proxy.rollbacks == 1

    manager.add_cache_request_diagnostic(_diagnostic(session_id="recovered"))
    assert [row["session_id"] for row in manager.query_cache_request_diagnostics()] == [
        "recovered"
    ]


def test_cache_diagnostic_summary_classifies_evidence_without_guessing_provider():
    manager = MemoryManager(":memory:")
    base_ts = _ts("2026-08-09", "12:00:00")
    reasons = [
        ("cold", "冷启动"),
        ("identity", "模型或接口变化"),
        ("tools", "工具契约变化"),
        ("rewrite", "上下文压缩或历史重写"),
    ]
    for offset, (session, reason) in enumerate(reasons):
        manager.add_cache_request_diagnostic(_diagnostic(
            ts=base_ts + offset,
            session_id=session,
            request_reason=reason,
        ))

    # prompt_drop：同身份上一条 100000，本条 97000（缩短 3%）。
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 10, session_id="drop", prompt_tokens=100000,
        cached_tokens=99000, cache_miss_tokens=1000,
    ))
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 11, session_id="drop", prompt_tokens=97000,
        cached_tokens=1000, cache_miss_tokens=96000,
    ))

    # idle_gap：相同身份间隔 121 秒，且没有更强客户端原因。
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 20, session_id="idle", prompt_tokens=30000,
        cached_tokens=29000, cache_miss_tokens=1000,
    ))
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 141, session_id="idle",
    ))

    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 30, session_id="unknown",
    ))
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 31, session_id="unreported", cached_tokens=None,
        cache_miss_tokens=None, cache_reported=False,
    ))
    # 后台 scope 的低命中与未报告不得污染主链路摘要。
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 32, session_id="background-low",
        cache_scope="keepalive", cached_tokens=0, cache_miss_tokens=30000,
    ))
    manager.add_cache_request_diagnostic(_diagnostic(
        ts=base_ts + 33, session_id="background-unreported",
        cache_scope="subagent:explore", cached_tokens=None,
        cache_miss_tokens=None, cache_reported=False,
    ))

    summary = manager.query_cache_diagnostic_summary(
        "2026-08-09", "2026-08-09", idle_seconds=120,
        cache_scope="main",
    )

    assert summary["count"] == 7
    assert summary["unreported"] == 1
    assert summary["categories"] == {
        "cold_start": 1,
        "identity_change": 1,
        "tool_contract_change": 1,
        "client_history_rewrite": 1,
        "prompt_drop": 1,
        "idle_gap": 1,
        "provider_or_unknown": 1,
    }
