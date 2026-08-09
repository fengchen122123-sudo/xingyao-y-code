from types import SimpleNamespace

import cache_benchmark
import pytest


def test_weighted_hot_rate_excludes_cold_round():
    rounds = [
        {"prompt_tokens": 1000, "cached_tokens": 0},
        {"prompt_tokens": 1000, "cached_tokens": 990},
        {"prompt_tokens": 2000, "cached_tokens": 1990},
    ]

    assert cache_benchmark.weighted_hot_rate(rounds) == (2980 / 3000 * 100)


def test_round_summary_exposes_total_hot_minimum_and_catastrophic_misses():
    rounds = [
        {"prompt_tokens": 1000, "cached_tokens": 0},
        {"prompt_tokens": 1000, "cached_tokens": 990},
        {"prompt_tokens": 1000, "cached_tokens": 0},
        {"prompt_tokens": 1000, "cached_tokens": 995},
    ]

    summary = cache_benchmark.summarize_rounds(
        rounds, minimum_hot_rate=60.0, catastrophic_rate=50.0
    )

    assert summary["all_rate"] == pytest.approx(1985 / 4000 * 100)
    assert summary["hot_rate"] == pytest.approx(1985 / 3000 * 100)
    assert summary["minimum_hot_round_rate"] == 0.0
    assert summary["catastrophic_miss_count"] == 1
    assert summary["passed"] is False


@pytest.mark.parametrize(
    "rounds",
    [
        [{"prompt_tokens": -1, "cached_tokens": 0}],
        [{"prompt_tokens": 100, "cached_tokens": -1}],
        [{"prompt_tokens": 100, "cached_tokens": 101}],
        [{"prompt_tokens": 100}],
        [{"prompt_tokens": True, "cached_tokens": 1}],
        [{"prompt_tokens": 100.5, "cached_tokens": 99}],
    ],
)
def test_round_summary_rejects_invalid_provider_usage(rounds):
    with pytest.raises(ValueError):
        cache_benchmark.summarize_rounds(rounds, minimum_hot_rate=99.0)


def test_stable_messages_use_production_system_prefix(monkeypatch):
    expected = {"role": "system", "content": "真实生产稳定前缀"}
    monkeypatch.setattr(
        cache_benchmark, "build_stable_system_message", lambda: expected
    )

    messages = cache_benchmark._stable_messages()

    assert messages == [expected]
    assert "cache-benchmark-stable-line" not in messages[0]["content"]


def test_run_benchmark_uses_production_contract_and_checks_threshold(monkeypatch):
    captured = []
    usages = iter([
        SimpleNamespace(prompt_tokens=1000, completion_tokens=1, total_tokens=1001,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=0)),
        SimpleNamespace(prompt_tokens=1000, completion_tokens=1, total_tokens=1001,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=990)),
        SimpleNamespace(prompt_tokens=1000, completion_tokens=1, total_tokens=1001,
                        prompt_tokens_details=SimpleNamespace(cached_tokens=995)),
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
    assert result["all_rate"] == pytest.approx(1985 / 3000 * 100)
    assert result["minimum_hot_round_rate"] == 99.0
    assert result["catastrophic_miss_count"] == 0
    assert len(captured) == 3
    assert all(call["tools"] == [{"type": "function"}] for call in captured)
    assert all(call["cache_session_id"] == "fixed-session" for call in captured)


def test_one_request_matches_deepseek_and_regular_tool_choice_contract(monkeypatch):
    captured = []

    class FakeStream:
        chunks = []

        def __iter__(self):
            return iter([SimpleNamespace(
                usage=SimpleNamespace(
                    prompt_tokens=100,
                    completion_tokens=1,
                    total_tokens=101,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
                choices=[],
            )])

    monkeypatch.setattr(
        cache_benchmark,
        "completion",
        lambda **kwargs: captured.append(kwargs) or FakeStream(),
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda model, base_url=None: None,
    )
    common = {
        "api_key": "secret",
        "messages": [{"role": "system", "content": "stable"}],
        "tools": [],
        "cache_session_id": "suite-session",
    }

    cache_benchmark._run_one_request(
        model="openai/deepseek-v4-pro",
        api_base="https://api.deepseek.com",
        **common,
    )
    cache_benchmark._run_one_request(
        model="openai/gpt-4o",
        api_base="https://api.openai.com/v1",
        **common,
    )

    assert "tool_choice" not in captured[0]
    assert captured[0]["extra_body"]["thinking"] == {"type": "enabled"}
    assert captured[0]["max_tokens"] == 8192
    assert captured[1]["tool_choice"] == "auto"
    assert captured[1]["max_tokens"] == 8192


def test_benchmark_rejects_usage_that_fails_production_invariants(monkeypatch):
    invalid = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=2,
        total_tokens=101,
        prompt_tokens_details=SimpleNamespace(cached_tokens=90),
    )
    monkeypatch.setattr(
        cache_benchmark,
        "_run_one_request",
        lambda **kwargs: ("ok", invalid, None, None),
    )

    with pytest.raises(ValueError, match=r"prompt \+ completion"):
        cache_benchmark.run_benchmark(
            model="openai/deepseek-v4-pro",
            api_base="https://api.deepseek.com",
            api_key="secret",
            rounds=2,
            minimum_hot_rate=99.0,
            tools=[],
            stable_messages=[{"role": "system", "content": "stable"}],
        )


def test_ab_benchmark_shares_contract_and_suite_seed(monkeypatch):
    captured = []
    tools = [{"type": "function"}]

    def fake_run(**kwargs):
        captured.append(kwargs)
        return {"model": kwargs["model"], "hot_rate": 99.0, "passed": True}

    monkeypatch.setattr(cache_benchmark, "run_benchmark", fake_run)
    monkeypatch.setattr(cache_benchmark, "get_tools_schema", lambda: tools)
    targets = [
        {
            "label": "official",
            "model": "openai/deepseek-v4-pro",
            "api_base": "https://api.deepseek.com",
            "api_key": "official-key",
        },
        {
            "label": "aggregator",
            "model": "openai/deepseek-v4-pro",
            "api_base": "https://aggregator.example/v1",
            "api_key": "aggregator-key",
        },
    ]

    result = cache_benchmark.run_ab_benchmark(
        targets, rounds=4, minimum_hot_rate=99.0, suite_id="fixed-suite"
    )

    assert [item["label"] for item in result["targets"]] == [
        "official", "aggregator"
    ]
    assert len(captured) == 2
    assert all(call["tools"] is tools for call in captured)
    assert all(call["rounds"] == 4 for call in captured)
    assert all(call["session_id"] == "fixed-suite" for call in captured)


def test_compare_cli_runs_ab_and_reports_winner(monkeypatch, capsys):
    captured = []
    monkeypatch.setenv("PRIMARY_KEY", "primary-secret")
    monkeypatch.setenv("COMPARE_KEY", "compare-secret")
    monkeypatch.setattr(
        "sys.argv",
        [
            "cache_benchmark.py",
            "--model", "openai/deepseek-v4-pro",
            "--api-base", "https://api.deepseek.com",
            "--api-key-env", "PRIMARY_KEY",
            "--compare-model", "openai/deepseek-v4-pro",
            "--compare-api-base", "https://aggregator.example/v1",
            "--compare-api-key-env", "COMPARE_KEY",
            "--minimum-hot-rate", "99",
        ],
    )

    def fake_ab(targets, **kwargs):
        captured.extend(targets)
        return {
            "winner": "official",
            "comparable_models": True,
            "targets": [
                {
                    "label": "official",
                    "rounds": [{"prompt_tokens": 100, "cached_tokens": 99}],
                    "all_rate": 99.0,
                    "hot_rate": 99.0,
                    "minimum_hot_round_rate": 99.0,
                    "catastrophic_miss_count": 0,
                    "minimum_hot_rate": 99.0,
                    "passed": True,
                },
                {
                    "label": "compare",
                    "rounds": [{"prompt_tokens": 100, "cached_tokens": 95}],
                    "all_rate": 95.0,
                    "hot_rate": 95.0,
                    "minimum_hot_round_rate": 95.0,
                    "catastrophic_miss_count": 0,
                    "minimum_hot_rate": 99.0,
                    "passed": False,
                },
            ],
        }

    monkeypatch.setattr(cache_benchmark, "run_ab_benchmark", fake_ab)

    with pytest.raises(SystemExit) as exc:
        cache_benchmark.main()

    assert exc.value.code == 1
    assert captured[0]["api_key"] == "primary-secret"
    assert captured[1]["api_key"] == "compare-secret"
    assert "winner=official" in capsys.readouterr().out
