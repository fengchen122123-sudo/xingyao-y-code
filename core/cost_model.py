"""成本账本：按模型价格把 token 用量换算成金额。

设计要点（见 docs/2026-11-12-pi-inspired-optimization-design.md 第 4 节）：

1. **价格用「微元/百万 token」存整数**：1 元/百万 token 记为 1_000_000，
   计费全程整数运算，避免浮点累计误差。1 元 = 1_000_000 微元。
2. **阶梯按整单命中**：用本次请求的输入总量（input + cache_read + cache_write）
   找到第一个 `up_to_input_tokens` 覆盖它的档位，整单按该档单价计。不做逐
   token 分段——厂商账单口径就是整单换价，分段反而对不上。
3. **没有价格就是没有价格**：找不到价格返回 None，调用方落 NULL。绝不按同系列
   推测，绝不把缺失当 0。
4. **纯函数**：本模块不读网络、不读数据库、不写文件，便于单测与复用。

术语：
- 输入 token（input）：未命中缓存的输入。
- 缓存读（cache_read）：命中提示缓存的输入。
- 缓存写（cache_write）：写入提示缓存的输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

MICROS_PER_UNIT = 1_000_000
CURRENCY_SYMBOLS = {"CNY": "¥", "USD": "$"}


@dataclass(frozen=True)
class PriceTier:
    """一档价格，单位：微元/百万 token。

    up_to_input_tokens 为 None 表示该档无上限（必须是最后一档）。
    """

    input: int
    output: int
    cache_read: int
    cache_write: int
    up_to_input_tokens: int | None = None

    def covers(self, input_tokens: int) -> bool:
        return self.up_to_input_tokens is None or input_tokens <= self.up_to_input_tokens


@dataclass(frozen=True)
class ModelPrice:
    """一个模型的价格表。tiers 必须按 up_to 升序，且只有末档可为 None。"""

    currency: str
    tiers: tuple[PriceTier, ...]
    source: str = ""
    verified_at: str = ""

    @property
    def symbol(self) -> str:
        return CURRENCY_SYMBOLS.get(self.currency, self.currency + " ")


@dataclass(frozen=True)
class CostBreakdown:
    """一次请求的费用拆解，全部为微元整数。"""

    total_micros: int
    input_micros: int
    output_micros: int
    cache_read_micros: int
    cache_write_micros: int
    currency: str
    tier_index: int
    incomplete: bool = False

    def format(self, digits: int = 4) -> str:
        """人类可读金额，如 ¥0.0123；currency 未知时带前缀。"""
        symbol = CURRENCY_SYMBOLS.get(self.currency, self.currency + " ")
        yuan = self.total_micros / MICROS_PER_UNIT
        return f"{symbol}{yuan:.{digits}f}"


def _non_negative_int(value: Any, field: str) -> int:
    """把价格字段规整成非负整数；非法即抛 ValueError（价格配置错要吵出来）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"价格字段 {field} 必须是整数（单位：微元/百万 token）")
    if value < 0:
        raise ValueError(f"价格字段 {field} 不能为负数")
    return value


def parse_price(raw: Mapping[str, Any] | ModelPrice | None) -> ModelPrice | None:
    """把配置里的价格块解析成 ModelPrice；空/非法返回 None（降级为「未计价」）。

    非法价格记 warning 由调用方负责——本函数只做纯解析，不记日志。
    """
    if raw is None:
        return None
    if isinstance(raw, ModelPrice):
        return raw
    if not isinstance(raw, Mapping):
        return None

    currency = str(raw.get("currency") or "CNY").upper()
    tiers_raw = raw.get("tiers")
    if not isinstance(tiers_raw, Iterable) or isinstance(tiers_raw, (str, bytes)):
        return None

    tiers: list[PriceTier] = []
    for item in tiers_raw:
        if not isinstance(item, Mapping):
            return None
        try:
            tier = PriceTier(
                input=_non_negative_int(item.get("input", 0), "input"),
                output=_non_negative_int(item.get("output", 0), "output"),
                cache_read=_non_negative_int(item.get("cache_read", 0), "cache_read"),
                cache_write=_non_negative_int(item.get("cache_write", 0), "cache_write"),
                up_to_input_tokens=(
                    None
                    if item.get("up_to_input_tokens") is None
                    else _non_negative_int(item["up_to_input_tokens"], "up_to_input_tokens")
                ),
            )
        except ValueError:
            return None
        tiers.append(tier)

    if not tiers:
        return None
    # 只有末档允许无上限，中间出现 None 视为配置错误：价格会静默错档，不如落空。
    for tier in tiers[:-1]:
        if tier.up_to_input_tokens is None:
            return None
    if tiers[-1].up_to_input_tokens is not None:
        # 末档必须无上限，否则超出最高档的请求无价可用
        return None

    return ModelPrice(
        currency=currency,
        tiers=tuple(tiers),
        source=str(raw.get("source") or ""),
        verified_at=str(raw.get("verified_at") or ""),
    )


def _usage_value(usage: Any, *names: str) -> int | None:
    """按字段名列表取值，dict 与 SDK 对象都支持；缺失/None 返回 None。"""
    for name in names:
        if isinstance(usage, Mapping):
            if name in usage and usage[name] is not None:
                try:
                    return int(usage[name])
                except (TypeError, ValueError):
                    return None
        else:
            value = getattr(usage, name, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
    return None


def _micros(tokens: int, price_per_million: int) -> int:
    """微元整数计费，四舍五入（半上）。"""
    if tokens <= 0 or price_per_million <= 0:
        return 0
    return (tokens * price_per_million + MICROS_PER_UNIT // 2) // MICROS_PER_UNIT


def compute_cost(
    usage: Any,
    price: ModelPrice | None,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> CostBreakdown | None:
    """按价格表计算一次请求的费用。

    usage 可以是 dict 或任意带属性的对象；显式关键字参数优先于 usage
    （调用方已经拆好字段时用它，避免重复猜字段名）。
    无价格、或四个 token 字段全缺失 → 返回 None（未计价，不等价于 0 元）。
    """
    if price is None:
        return None

    values = {
        "input": input_tokens if input_tokens is not None else _usage_value(usage, "input_tokens", "prompt_tokens"),
        "output": output_tokens if output_tokens is not None else _usage_value(usage, "output_tokens", "completion_tokens"),
        "cache_read": (
            cache_read_tokens
            if cache_read_tokens is not None
            else _usage_value(usage, "cache_read_tokens", "cached_tokens")
        ),
        "cache_write": (
            cache_write_tokens
            if cache_write_tokens is not None
            else _usage_value(usage, "cache_write_tokens", "cache_creation_input_tokens")
        ),
    }
    if all(value is None for value in values.values()):
        return None

    incomplete = any(value is None for value in values.values())
    counts = {key: max(0, value or 0) for key, value in values.items()}
    total_input = counts["input"] + counts["cache_read"] + counts["cache_write"]

    tier_index = next(
        (index for index, tier in enumerate(price.tiers) if tier.covers(total_input)),
        None,
    )
    if tier_index is None:
        return None
    tier = price.tiers[tier_index]

    input_micros = _micros(counts["input"], tier.input)
    output_micros = _micros(counts["output"], tier.output)
    cache_read_micros = _micros(counts["cache_read"], tier.cache_read)
    cache_write_micros = _micros(counts["cache_write"], tier.cache_write)

    return CostBreakdown(
        total_micros=input_micros + output_micros + cache_read_micros + cache_write_micros,
        input_micros=input_micros,
        output_micros=output_micros,
        cache_read_micros=cache_read_micros,
        cache_write_micros=cache_write_micros,
        currency=price.currency,
        tier_index=tier_index,
        incomplete=incomplete,
    )


def resolve_price(model_name: str, config_price: Mapping[str, Any] | ModelPrice | None = None) -> ModelPrice | None:
    """按「模型配置 → 内置价格表」顺序取价，取不到返回 None。

    model_name 可以是 "provider/model" 形式；内置表按叶子名（小写）匹配。
    """
    parsed = parse_price(config_price)
    if parsed is not None:
        return parsed

    try:
        from core.model_spec_table import MODEL_PRICES
    except Exception:  # pragma: no cover - 内置表缺失不应影响主流程
        return None

    full = str(model_name or "").strip().lower()
    leaf = full.split("/", 1)[1] if "/" in full else full
    for key in (full, leaf):
        if key and key in MODEL_PRICES:
            return parse_price(MODEL_PRICES[key])
    return None


def aggregate_costs(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """把多行用量聚合成总费用。

    跨币种不求和：返回 {currency: {...}} 的字典，由展示层分行；
    任何一行 cost_micros 为 NULL 都计入 unpriced，不当作 0 元。
    """
    totals: dict[str, dict[str, Any]] = {}
    unpriced = 0
    for row in rows:
        micros = row.get("cost_micros")
        currency = str(row.get("cost_currency") or "")
        if micros is None or not currency:
            unpriced += 1
            continue
        slot = totals.setdefault(
            currency, {"micros": 0, "currency": currency, "rows": 0}
        )
        slot["micros"] += int(micros)
        slot["rows"] += 1
    return {"by_currency": totals, "unpriced": unpriced}