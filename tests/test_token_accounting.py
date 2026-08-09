from types import SimpleNamespace

import pytest

from core.llm import _normalize_server_usage


def test_normalize_deepseek_usage_preserves_exact_server_tokens():
    record = _normalize_server_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "prompt_cache_hit_tokens": 990,
        "prompt_cache_miss_tokens": 10,
    })

    assert record == {
        "valid": True,
        "errors": (),
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "total_tokens": 1100,
        "cached_tokens": 990,
        "cache_miss_tokens": 10,
        "cache_reported": True,
    }


def test_normalize_openai_usage_derives_completion_only_from_exact_total():
    usage = SimpleNamespace(
        prompt_tokens=676,
        completion_tokens=None,
        total_tokens=692,
        prompt_tokens_details=SimpleNamespace(cached_tokens=512),
    )

    record = _normalize_server_usage(usage)

    assert record["valid"] is True
    assert record["completion_tokens"] == 16
    assert record["cached_tokens"] == 512
    assert record["cache_reported"] is True


def test_normalize_usage_keeps_unreported_cache_distinct_from_zero_hit():
    unreported = _normalize_server_usage({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
    })
    zero_hit = _normalize_server_usage({
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "total_tokens": 120,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 100,
    })

    assert unreported["valid"] is True
    assert unreported["cache_reported"] is False
    assert unreported["cached_tokens"] is None
    assert unreported["cache_miss_tokens"] is None
    assert zero_hit["valid"] is True
    assert zero_hit["cache_reported"] is True
    assert zero_hit["cached_tokens"] == 0


@pytest.mark.parametrize(
    ("usage", "expected_error"),
    [
        (
            {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
            "prompt_tokens",
        ),
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "prompt_cache_hit_tokens": 11,
            },
            "cached_tokens exceeds prompt_tokens",
        ),
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "total_tokens": 11,
                "prompt_cache_hit_tokens": 8,
                "prompt_cache_miss_tokens": 1,
            },
            "cache hit + miss",
        ),
        (
            {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 11},
            "prompt + completion",
        ),
        (
            {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1},
            "prompt_tokens",
        ),
        (
            {"prompt_tokens": "10.5", "completion_tokens": 1, "total_tokens": 11},
            "prompt_tokens",
        ),
    ],
)
def test_normalize_usage_rejects_inconsistent_server_values(usage, expected_error):
    record = _normalize_server_usage(usage)

    assert record["valid"] is False
    assert any(expected_error in error for error in record["errors"])


def test_cache_usage_isolated_by_session_model_endpoint_and_scope():
    from core.llm import _record_cache_usage, get_token_usage_snapshot

    _record_cache_usage(
        "accounting-isolation-s1", "main", 1000, 990, "",
        "openai/deepseek-v4", "https://a.example/v1",
    )
    _record_cache_usage(
        "accounting-isolation-s1", "main", 2000, 1000, "",
        "openai/deepseek-v4", "https://b.example/v1",
    )
    _record_cache_usage(
        "accounting-isolation-s1", "subagent:test", 3000, 0, "",
        "openai/deepseek-v4", "https://a.example/v1",
    )
    _record_cache_usage(
        "accounting-isolation-s2", "main", 4000, 1000, "",
        "openai/deepseek-v4", "https://a.example/v1",
    )
    _record_cache_usage(
        "accounting-isolation-s1", "main", 5000, 1000, "",
        "openai/deepseek-v4-pro", "https://a.example/v1",
    )

    first = get_token_usage_snapshot(
        "accounting-isolation-s1", "OPENAI/DEEPSEEK-V4",
        "https://a.example/v1/", "main",
    )
    second_endpoint = get_token_usage_snapshot(
        "accounting-isolation-s1", "openai/deepseek-v4",
        "https://b.example/v1", "main",
    )

    assert first["cache"]["session_all"] == {
        "prompt_tokens": 1000,
        "cached_tokens": 990,
        "rate": 99.0,
    }
    assert second_endpoint["cache"]["session_all"] == {
        "prompt_tokens": 2000,
        "cached_tokens": 1000,
        "rate": 50.0,
    }


def test_cache_snapshot_distinguishes_unavailable_from_real_zero_hit():
    from core.llm import _record_cache_usage, get_token_usage_snapshot

    missing = get_token_usage_snapshot(
        "accounting-missing", "openai/deepseek-v4", "https://a.example/v1"
    )
    assert missing["cache"]["available"] is False
    assert missing["cache"]["latest"]["rate"] is None

    _record_cache_usage(
        "accounting-zero", "main", 100, 0, "冷启动",
        "openai/deepseek-v4", "https://a.example/v1",
    )
    zero = get_token_usage_snapshot(
        "accounting-zero", "openai/deepseek-v4", "https://a.example/v1"
    )
    assert zero["cache"]["available"] is True
    assert zero["cache"]["latest"]["rate"] == 0.0
    assert zero["cache"]["session_all"]["rate"] == 0.0
    assert zero["cache"]["hot"]["rate"] is None
    assert zero["cache"]["invalidation_reason"] == "冷启动"


def test_cache_usage_rejects_invalid_values_instead_of_clamping():
    from core.llm import _record_cache_usage

    with pytest.raises(ValueError, match="prompt_tokens"):
        _record_cache_usage(
            "accounting-invalid", "main", -1, 0, "",
            "openai/deepseek-v4", "https://a.example/v1",
        )
    with pytest.raises(ValueError, match="cached_tokens"):
        _record_cache_usage(
            "accounting-invalid", "main", 10, 11, "",
            "openai/deepseek-v4", "https://a.example/v1",
        )


def test_cache_usage_unavailable_hides_stale_rates_until_next_valid_usage():
    from core.llm import (
        _mark_cache_usage_unavailable,
        _record_cache_usage,
        get_token_usage_snapshot,
    )

    args = (
        "accounting-unavailable", "openai/deepseek-v4",
        "https://a.example/v1", "main",
    )
    _record_cache_usage(args[0], args[3], 1000, 990, "", args[1], args[2])
    _mark_cache_usage_unavailable(*args, "usage 不一致")

    unavailable = get_token_usage_snapshot(*args)
    assert unavailable["cache"]["available"] is False
    assert unavailable["cache"]["latest"]["rate"] is None
    assert unavailable["cache"]["hot"]["rate"] is None
    assert unavailable["cache"]["session_all"]["rate"] is None
    assert unavailable["cache"]["invalidation_reason"] == "usage 不一致"

    _record_cache_usage(args[0], args[3], 1000, 980, "", args[1], args[2])
    recovered = get_token_usage_snapshot(*args)
    assert recovered["cache"]["available"] is True
    assert recovered["cache"]["latest"]["rate"] == 98.0
    assert recovered["cache"]["session_all"]["rate"] == 98.5


def test_cache_usage_lifecycle_renames_clears_and_returns_detached_snapshots():
    from core.llm import (
        _record_cache_usage,
        clear_cache_usage_session,
        get_token_usage_snapshot,
        rename_cache_usage_session,
    )

    model = "openai/deepseek-v4"
    base = "https://a.example/v1"
    _record_cache_usage(
        "accounting-old", "main", 1000, 900, "", model, base
    )
    rename_cache_usage_session("accounting-old", "accounting-new")

    assert get_token_usage_snapshot(
        "accounting-old", model, base
    )["cache"]["available"] is False
    moved = get_token_usage_snapshot("accounting-new", model, base)
    assert moved["cache"]["available"] is True
    moved["cache"]["session_all"]["prompt_tokens"] = 1
    assert get_token_usage_snapshot(
        "accounting-new", model, base
    )["cache"]["session_all"]["prompt_tokens"] == 1000

    clear_cache_usage_session("accounting-new")
    assert get_token_usage_snapshot(
        "accounting-new", model, base
    )["cache"]["available"] is False


def test_account_server_usage_updates_exact_counters_and_persistence(monkeypatch):
    import core.llm as llm

    persisted = []
    monkeypatch.setattr(llm, "_persist_token_usage", lambda *args, **kwargs: persisted.append(kwargs))
    with llm.token_usage_lock:
        before = {
            key: llm.session_token_usage.get(key, 0)
            for key in (
                "cumulative_prompt",
                "cumulative_completion",
                "cumulative_total",
                "cumulative_cached",
                "cumulative_cache_prompt",
            )
        }

    result = llm._account_server_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 990,
            "prompt_cache_miss_tokens": 10,
        },
        model_name="openai/deepseek-v4",
        api_base="https://account.example/v1",
        session_id="accounting-stream-valid",
        cache_scope="main",
        invalidation_reason="冷启动",
    )

    assert result["valid"] is True
    with llm.token_usage_lock:
        assert llm.session_token_usage["cumulative_prompt"] == before["cumulative_prompt"] + 1000
        assert llm.session_token_usage["cumulative_completion"] == before["cumulative_completion"] + 100
        assert llm.session_token_usage["cumulative_total"] == before["cumulative_total"] + 1100
        assert llm.session_token_usage["cumulative_cached"] == before["cumulative_cached"] + 990
        assert llm.session_token_usage["cumulative_cache_prompt"] == before["cumulative_cache_prompt"] + 1000
        assert llm.session_token_usage["latest_prompt"] == 1000
        assert llm.session_token_usage["latest_total"] == 1100

    snapshot = llm.get_token_usage_snapshot(
        "accounting-stream-valid", "openai/deepseek-v4",
        "https://account.example/v1",
    )
    assert snapshot["cache"]["latest"]["rate"] == 99.0
    assert snapshot["cache"]["session_all"]["rate"] == 99.0
    assert persisted == [{
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "cached_tokens": 990,
        "total_tokens": 1100,
        "session_id": "accounting-stream-valid",
    }]


def test_account_server_usage_rejects_inconsistent_record_without_pollution(
    monkeypatch, caplog
):
    import core.llm as llm

    persisted = []
    monkeypatch.setattr(llm, "_persist_token_usage", lambda *args, **kwargs: persisted.append(kwargs))
    with llm.token_usage_lock:
        before = {
            key: llm.session_token_usage.get(key, 0)
            for key in (
                "cumulative_prompt",
                "cumulative_completion",
                "cumulative_total",
                "cumulative_cached",
                "cumulative_cache_prompt",
            )
        }

    result = llm._account_server_usage(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 100,
            "total_tokens": 1100,
            "prompt_cache_hit_tokens": 990,
            "prompt_cache_miss_tokens": 9,
        },
        model_name="openai/deepseek-v4",
        api_base="https://invalid.example/v1",
        session_id="accounting-stream-invalid",
        cache_scope="main",
        invalidation_reason="",
    )

    assert result is None
    assert persisted == []
    with llm.token_usage_lock:
        assert {
            key: llm.session_token_usage.get(key, 0)
            for key in before
        } == before
    snapshot = llm.get_token_usage_snapshot(
        "accounting-stream-invalid", "openai/deepseek-v4",
        "https://invalid.example/v1",
    )
    assert snapshot["cache"]["available"] is False
    assert "cache hit + miss" in snapshot["cache"]["invalidation_reason"]
    assert "accounting-stream-invalid" in caplog.text
    assert "openai/deepseek-v4" in caplog.text


def test_account_server_usage_persists_unknown_cache_as_null(monkeypatch):
    import core.llm as llm

    persisted = []
    monkeypatch.setattr(llm, "_persist_token_usage", lambda *args, **kwargs: persisted.append(kwargs))
    with llm.token_usage_lock:
        before_cache_prompt = llm.session_token_usage.get("cumulative_cache_prompt", 0)

    result = llm._account_server_usage(
        {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        model_name="openai/no-cache-details",
        api_base="https://unknown.example/v1",
        session_id="accounting-cache-unknown",
        cache_scope="main",
        invalidation_reason="",
    )

    assert result["valid"] is True
    assert result["cache_reported"] is False
    assert persisted[0]["cached_tokens"] is None
    with llm.token_usage_lock:
        assert llm.session_token_usage["cumulative_cache_prompt"] == before_cache_prompt
    snapshot = llm.get_token_usage_snapshot(
        "accounting-cache-unknown", "openai/no-cache-details",
        "https://unknown.example/v1",
    )
    assert snapshot["cache"]["available"] is False
    assert "未报告缓存" in snapshot["cache"]["invalidation_reason"]


def test_non_main_usage_increases_process_cost_without_overwriting_main(monkeypatch):
    import core.llm as llm

    monkeypatch.setattr(llm, "_persist_token_usage", lambda *args, **kwargs: None)
    common = {
        "model_name": "openai/deepseek-v4",
        "api_base": "https://scope.example/v1",
        "session_id": "accounting-scope-main",
    }
    llm._account_server_usage(
        {
            "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
            "prompt_cache_hit_tokens": 990, "prompt_cache_miss_tokens": 10,
        },
        cache_scope="main", invalidation_reason="", **common,
    )
    before_snapshot = llm.get_token_usage_snapshot(
        common["session_id"], common["model_name"], common["api_base"]
    )
    with llm.token_usage_lock:
        before_process_total = llm.session_token_usage["cumulative_total"]

    llm._account_server_usage(
        {
            "prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 500,
        },
        cache_scope="secondary:test", invalidation_reason="", **common,
    )

    after_snapshot = llm.get_token_usage_snapshot(
        common["session_id"], common["model_name"], common["api_base"]
    )
    assert after_snapshot["window"] == before_snapshot["window"]
    assert after_snapshot["cache"] == before_snapshot["cache"]
    with llm.token_usage_lock:
        assert llm.session_token_usage["cumulative_total"] == before_process_total + 550
