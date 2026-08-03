from types import SimpleNamespace

import cache_benchmark


def test_weighted_hot_rate_excludes_cold_round():
    rounds = [
        {"prompt_tokens": 1000, "cached_tokens": 0},
        {"prompt_tokens": 1000, "cached_tokens": 990},
        {"prompt_tokens": 2000, "cached_tokens": 1990},
    ]

    assert cache_benchmark.weighted_hot_rate(rounds) == (2980 / 3000 * 100)


def test_run_benchmark_uses_production_contract_and_checks_threshold(monkeypatch):
    captured = []
    usages = iter([
        SimpleNamespace(prompt_tokens=1000, prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        SimpleNamespace(prompt_tokens=1000, prompt_tokens_details=SimpleNamespace(cached_tokens=990)),
        SimpleNamespace(prompt_tokens=1000, prompt_tokens_details=SimpleNamespace(cached_tokens=995)),
    ])

    def fake_one_request(**kwargs):
        captured.append(kwargs)
        return "ok", next(usages), None, None

    monkeypatch.setattr(cache_benchmark, "_run_one_request", fake_one_request)
    monkeypatch.setattr(cache_benchmark, "get_tools_schema", lambda: [{"type": "function"}])

    result = cache_benchmark.run_benchmark(
        model="openai/kimi-for-coding",
        api_base="https://api.kimi.com/coding/v1",
        api_key="secret",
        rounds=3,
        minimum_hot_rate=97.0,
        session_id="fixed-session",
    )

    assert result["passed"] is True
    assert result["hot_rate"] == 99.25
    assert len(captured) == 3
    assert all(call["tools"] == [{"type": "function"}] for call in captured)
    assert all(call["cache_session_id"] == "fixed-session" for call in captured)
