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
