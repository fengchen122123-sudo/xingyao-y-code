"""成本账本（core/cost_model.py）单元测试。

口径见 docs/2026-11-12-pi-inspired-optimization-design.md 第 4 节：
价格单位「微元/百万 token」，阶梯按整单命中，无价即 NULL（不是 0 元）。
"""

import pytest

from core.cost_model import (
    MICROS_PER_UNIT,
    ModelPrice,
    aggregate_costs,
    compute_cost,
    parse_price,
    resolve_price,
    PriceTier,
)

# 单档价格：输入 2 元/百万，输出 8 元/百万，缓存读 0.2 元/百万，缓存写 2.5 元/百万
SINGLE = ModelPrice(
    currency="CNY",
    tiers=(
        PriceTier(
            input=2 * MICROS_PER_UNIT,
            output=8 * MICROS_PER_UNIT,
            cache_read=200_000,
            cache_write=2_500_000,
            up_to_input_tokens=None,
        ),
    ),
)

# 两档：输入总量 ≤1000 用第一档（贵），超过用第二档（便宜）
TIERED = ModelPrice(
    currency="CNY",
    tiers=(
        PriceTier(input=10 * MICROS_PER_UNIT, output=20 * MICROS_PER_UNIT,
                  cache_read=0, cache_write=0, up_to_input_tokens=1000),
        PriceTier(input=1 * MICROS_PER_UNIT, output=2 * MICROS_PER_UNIT,
                  cache_read=0, cache_write=0, up_to_input_tokens=None),
    ),
)


def test_no_price_returns_none():
    assert compute_cost({"input_tokens": 100}, None) is None


def test_single_tier_exact_amounts():
    cost = compute_cost(
        None,
        SINGLE,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    assert cost.total_micros == 2 * MICROS_PER_UNIT + 8 * MICROS_PER_UNIT
    assert cost.input_micros == 2 * MICROS_PER_UNIT
    assert cost.output_micros == 8 * MICROS_PER_UNIT
    assert cost.tier_index == 0
    assert cost.incomplete is False


def test_cache_read_and_write_billed_separately():
    cost = compute_cost(
        None,
        SINGLE,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    assert cost.cache_read_micros == 200_000
    assert cost.cache_write_micros == 2_500_000
    assert cost.input_micros == 0


def test_tier_boundary_hits_current_tier():
    # 输入总量正好 1000 → 命中第一档
    cost = compute_cost(None, TIERED, input_tokens=1000, output_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0)
    assert cost.tier_index == 0
    # 1000 token × 10 元/百万 token = 0.01 元 = 10000 微元
    assert cost.input_micros == 10_000


def test_tier_boundary_plus_one_hits_next_tier():
    cost = compute_cost(None, TIERED, input_tokens=1001, output_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0)
    assert cost.tier_index == 1
    assert cost.input_micros == (1001 * MICROS_PER_UNIT + 500_000) // MICROS_PER_UNIT


def test_cache_tokens_count_toward_tier_input_total():
    # input 600 + cache_read 500 = 1100 > 1000，应落第二档
    cost = compute_cost(None, TIERED, input_tokens=600, output_tokens=0,
                        cache_read_tokens=500, cache_write_tokens=0)
    assert cost.tier_index == 1


def test_missing_fields_marked_incomplete_but_calculated():
    cost = compute_cost({"input_tokens": 1000, "output_tokens": 0}, SINGLE)
    assert cost.incomplete is True
    assert cost.total_micros > 0


def test_all_fields_missing_returns_none():
    assert compute_cost({}, SINGLE) is None


def test_usage_dict_and_object_both_supported():
    class Usage:
        input_tokens = 1000
        output_tokens = 2000
        cache_read_tokens = 0
        cache_write_tokens = 0

    from_obj = compute_cost(Usage(), SINGLE)
    from_dict = compute_cost(
        {"input_tokens": 1000, "output_tokens": 2000,
         "cache_read_tokens": 0, "cache_write_tokens": 0},
        SINGLE,
    )
    assert from_obj.total_micros == from_dict.total_micros


def test_prompt_and_completion_aliases():
    """兼容 OpenAI 命名的 prompt_tokens/completion_tokens/cached_tokens。"""
    cost = compute_cost(
        {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "cached_tokens": 0},
        SINGLE,
    )
    assert cost.input_micros == 2 * MICROS_PER_UNIT
    assert cost.output_micros == 800_000


def test_parse_price_rejects_open_ended_middle_tier():
    raw = {
        "currency": "CNY",
        "tiers": [
            {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
             "up_to_input_tokens": None},
            {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
             "up_to_input_tokens": None},
        ],
    }
    assert parse_price(raw) is None


def test_parse_price_rejects_missing_last_open_ended():
    raw = {
        "tiers": [
            {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
             "up_to_input_tokens": 1000},
        ],
    }
    assert parse_price(raw) is None


def test_parse_price_rejects_non_integer():
    raw = {"tiers": [{"input": 1.5, "output": 1, "cache_read": 0,
                      "cache_write": 0, "up_to_input_tokens": None}]}
    assert parse_price(raw) is None


def test_parse_price_accepts_valid_block():
    raw = {
        "currency": "USD",
        "tiers": [{"input": 3_000_000, "output": 15_000_000,
                   "cache_read": 300_000, "cache_write": 3_750_000,
                   "up_to_input_tokens": None}],
        "source": "https://example.com/pricing",
        "verified_at": "2026-11-12",
    }
    price = parse_price(raw)
    assert isinstance(price, ModelPrice)
    assert price.currency == "USD"
    assert price.symbol == "$"
    assert price.tiers[0].input == 3_000_000


def test_resolve_price_prefers_config(monkeypatch):
    price = resolve_price("openai/some-model", {"tiers": [
        {"input": 1, "output": 2, "cache_read": 0, "cache_write": 0,
         "up_to_input_tokens": None}
    ]})
    assert price is not None
    assert price.tiers[0].output == 2


def test_resolve_price_builtin_lookup_by_leaf(monkeypatch):
    import core.model_spec_table as spec

    monkeypatch.setattr(spec, "MODEL_PRICES", {
        "demo-model": {"tiers": [
            {"input": 7, "output": 9, "cache_read": 0, "cache_write": 0,
             "up_to_input_tokens": None}
        ]},
    })
    price = resolve_price("openai/Demo-Model")
    assert price is not None
    assert price.tiers[0].input == 7


def test_resolve_price_unknown_returns_none(monkeypatch):
    import core.model_spec_table as spec

    monkeypatch.setattr(spec, "MODEL_PRICES", {"only-this": {"tiers": [
        {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0,
         "up_to_input_tokens": None}
    ]}})
    # 同系列不同型号绝不套价
    assert resolve_price("openai/only-this-mini") is None


def test_aggregate_splits_currency_and_counts_unpriced():
    result = aggregate_costs([
        {"cost_micros": 1_000_000, "cost_currency": "CNY"},
        {"cost_micros": 500_000, "cost_currency": "CNY"},
        {"cost_micros": 2_000_000, "cost_currency": "USD"},
        {"cost_micros": None, "cost_currency": None},
    ])
    assert result["by_currency"]["CNY"]["micros"] == 1_500_000
    assert result["by_currency"]["CNY"]["rows"] == 2
    assert result["by_currency"]["USD"]["micros"] == 2_000_000
    assert result["unpriced"] == 1


def test_format_cost_renders_currency_symbol():
    cost = compute_cost(None, SINGLE, input_tokens=1_000_000, output_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0)
    assert cost.format() == "¥2.0000"


def test_zero_tokens_costs_zero_not_none():
    cost = compute_cost(None, SINGLE, input_tokens=0, output_tokens=0,
                        cache_read_tokens=0, cache_write_tokens=0)
    assert cost is not None
    assert cost.total_micros == 0
def test_persist_token_usage_writes_cost_fields(monkeypatch):
    """价格可解析时，记账过程要带上成本三字段。"""
    from core import cost_model, llm_diagnostics
    from core.app_context import set_manager  # noqa: F401  (仅探测导入可用性)

    recorded = {}

    class _Manager:
        def add_token_usage(self, **kwargs):
            recorded.update(kwargs)

    price = cost_model.ModelPrice(currency="CNY", tiers=(
        cost_model.PriceTier(input=1_000_000, output=2_000_000,
                             cache_read=100_000, cache_write=0,
                             up_to_input_tokens=None),))
    monkeypatch.setattr(cost_model, "resolve_price", lambda *a, **k: price)
    monkeypatch.setattr(llm_diagnostics, "_resolve_model_price", lambda name: price)
    monkeypatch.setattr("core.llm._persist_token_usage", llm_diagnostics._persist_token_usage)
    monkeypatch.setattr("core.app_context.get_manager", lambda: _Manager())

    llm_diagnostics._persist_token_usage("openai/demo", 1000, 500, 400, 1500)

    assert recorded["cost_currency"] == "CNY"
    # input=1000-400=600 → 600 微元；output 500 → 1000 微元；cache_read 400 → 40 微元
    assert recorded["cost_micros"] == 1640


def test_persist_token_usage_omits_cost_when_unpriced(monkeypatch):
    """没有价格时不写成本键，保持旧契约（老测试对 kwargs 做精确断言）。"""
    from core import llm_diagnostics

    recorded = {}

    class _Manager:
        def add_token_usage(self, **kwargs):
            recorded.update(kwargs)

    monkeypatch.setattr("core.app_context.get_manager", lambda: _Manager())

    llm_diagnostics._persist_token_usage("openai/demo", 1000, 500, 400, 1500)

    assert "cost_micros" not in recorded
    assert "cost_currency" not in recorded
