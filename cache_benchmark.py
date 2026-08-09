"""DeepSeek / Kimi 生产请求路径缓存命中率基准程序。"""

from __future__ import annotations

import argparse
from copy import deepcopy
import os
import uuid

from core.agent_service import build_stable_system_message
from core.llm import (
    _apply_cache_control,
    _complete_thinking_blocks,
    _provider_request_options,
    _strip_media_and_merge,
    _tool_contract_fingerprint,
    completion,
    get_tools_schema,
)


def _validated_round(item):
    if not isinstance(item, dict):
        raise ValueError("round usage 必须是字典")
    if "prompt_tokens" not in item or "cached_tokens" not in item:
        raise ValueError("round usage 缺少 prompt_tokens/cached_tokens")
    prompt = int(item["prompt_tokens"])
    cached = int(item["cached_tokens"])
    if prompt < 0 or cached < 0:
        raise ValueError("provider token usage 不能为负数")
    if cached > prompt:
        raise ValueError("provider cached_tokens 不能大于 prompt_tokens")
    return {"prompt_tokens": prompt, "cached_tokens": cached}


def _weighted_rate(rounds):
    validated = [_validated_round(item) for item in rounds]
    prompt = sum(item["prompt_tokens"] for item in validated)
    cached = sum(item["cached_tokens"] for item in validated)
    return (cached / prompt * 100) if prompt else 0.0


def weighted_hot_rate(rounds):
    """排除冷启动轮，按 prompt token 加权计算热启动命中率。"""
    return _weighted_rate(list(rounds)[1:])


def summarize_rounds(
    rounds, *, minimum_hot_rate, catastrophic_rate=50.0
):
    validated = [_validated_round(item) for item in rounds]
    hot_rounds = validated[1:]
    hot_rates = [
        (item["cached_tokens"] / item["prompt_tokens"] * 100)
        if item["prompt_tokens"] else 0.0
        for item in hot_rounds
    ]
    hot_rate = _weighted_rate(hot_rounds)
    catastrophic_miss_count = sum(
        rate < float(catastrophic_rate) for rate in hot_rates
    )
    return {
        "all_rate": _weighted_rate(validated),
        "hot_rate": hot_rate,
        "minimum_hot_round_rate": min(hot_rates) if hot_rates else 0.0,
        "catastrophic_miss_count": catastrophic_miss_count,
        "catastrophic_rate": float(catastrophic_rate),
        "minimum_hot_rate": float(minimum_hot_rate),
        "passed": (
            hot_rate >= float(minimum_hot_rate)
            and catastrophic_miss_count == 0
        ),
    }


def _prompt_tokens(usage):
    if isinstance(usage, dict):
        if "prompt_tokens" not in usage:
            raise ValueError("provider usage 缺少 prompt_tokens")
        value = usage["prompt_tokens"]
    else:
        if not hasattr(usage, "prompt_tokens"):
            raise ValueError("provider usage 缺少 prompt_tokens")
        value = getattr(usage, "prompt_tokens")
    if value is None:
        raise ValueError("provider usage 的 prompt_tokens 为空")
    return int(value)


def _cached_tokens(usage):
    missing = object()

    def field(container, name):
        if isinstance(container, dict):
            return container[name] if name in container else missing
        return getattr(container, name, missing)

    for name in ("prompt_cache_hit_tokens", "cached_tokens"):
        value = field(usage, name)
        if value is not missing and value is not None:
            return int(value)
    details = field(usage, "prompt_tokens_details")
    if details is not missing and details is not None:
        value = field(details, "cached_tokens")
        if value is not missing and value is not None:
            return int(value)
    raise ValueError("provider usage 缺少 cached token 字段")


def _run_one_request(*, model, api_base, api_key, messages, tools, cache_session_id):
    cleaned, _, _ = _strip_media_and_merge(messages, model, api_base)
    tool_contract = _tool_contract_fingerprint(tools)
    request = {
        "model": model,
        "api_key": api_key,
        "api_base": api_base,
        "messages": _apply_cache_control(cleaned, model),
        "tools": tools,
        "stream": True,
        "stream_options": {"include_usage": True},
        "num_retries": 2,
        "timeout": 300,
    }
    request.update(_provider_request_options(
        model,
        api_base,
        cache_scope="benchmark:main",
        cache_session_id=cache_session_id,
        tool_contract=tool_contract,
    ))
    deepseek_thinking_request = (
        request.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"
        and "deepseek" in str(model or "").lower()
    )
    if not deepseek_thinking_request:
        request["tool_choice"] = "auto"

    response_stream = completion(**request)
    usage = None
    content_parts = []
    reasoning_parts = []
    thinking_blocks = None
    for chunk in response_stream:
        if getattr(chunk, "usage", None) is not None:
            usage = chunk.usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = choices[0].delta
        tool_calls = getattr(delta, "tool_calls", None)
        if tool_calls:
            raise RuntimeError("基准模型尝试调用工具，已终止以避免产生副作用")
        content = getattr(delta, "content", None)
        reasoning = getattr(delta, "reasoning_content", None)
        if content:
            content_parts.append(content)
        if reasoning:
            reasoning_parts.append(reasoning)
        complete_blocks = _complete_thinking_blocks(delta)
        if complete_blocks is not None:
            thinking_blocks = complete_blocks

    tail_chunks = getattr(response_stream, "chunks", None)
    if tail_chunks:
        for tail_chunk in reversed(tail_chunks):
            if getattr(tail_chunk, "usage", None) is not None:
                usage = tail_chunk.usage
                break
    if usage is None:
        raise RuntimeError("provider 未返回 usage，无法验证缓存命中率")
    return "".join(content_parts), usage, "".join(reasoning_parts) or None, thinking_blocks


def _stable_messages():
    return [deepcopy(build_stable_system_message())]


def run_benchmark(
    *,
    model,
    api_base,
    api_key,
    rounds=3,
    minimum_hot_rate=99.0,
    session_id=None,
    tools=None,
    stable_messages=None,
):
    if int(rounds) < 2:
        raise ValueError("rounds 必须至少为 2，第一轮用于冷启动")
    if not api_key:
        raise ValueError("API Key 不能为空")

    tools = get_tools_schema() if tools is None else list(tools)
    messages = deepcopy(
        _stable_messages() if stable_messages is None else stable_messages
    )
    cache_session_id = session_id or f"benchmark-{uuid.uuid4().hex}"
    results = []

    for index in range(int(rounds)):
        messages.append({
            "role": "user",
            "content": f"缓存验证第 {index + 1} 轮：只回复 OK，不要调用工具。",
        })
        content, usage, reasoning, thinking_blocks = _run_one_request(
            model=model,
            api_base=api_base,
            api_key=api_key,
            messages=messages,
            tools=tools,
            cache_session_id=cache_session_id,
        )
        round_usage = {
            "prompt_tokens": _prompt_tokens(usage),
            "cached_tokens": _cached_tokens(usage),
        }
        results.append(_validated_round(round_usage))
        assistant = {"role": "assistant", "content": content or "OK"}
        if reasoning is not None:
            assistant["reasoning_content"] = reasoning
        if thinking_blocks is not None:
            assistant["thinking_blocks"] = thinking_blocks
        messages.append(assistant)

    summary = summarize_rounds(
        results, minimum_hot_rate=minimum_hot_rate
    )
    return {
        "model": model,
        "api_base": api_base,
        "rounds": results,
        **summary,
    }


def run_ab_benchmark(
    targets,
    *,
    rounds=3,
    minimum_hot_rate=99.0,
    suite_id=None,
    tools=None,
):
    targets = list(targets)
    if len(targets) < 2:
        raise ValueError("A/B 基准至少需要两个 target")
    shared_tools = get_tools_schema() if tools is None else tools
    shared_stable_messages = _stable_messages()
    resolved_suite_id = suite_id or f"benchmark-suite-{uuid.uuid4().hex}"
    results = []
    for target in targets:
        label = str(target.get("label") or "").strip()
        if not label:
            raise ValueError("A/B target 缺少 label")
        result = run_benchmark(
            model=target["model"],
            api_base=target["api_base"],
            api_key=target["api_key"],
            rounds=rounds,
            minimum_hot_rate=minimum_hot_rate,
            session_id=resolved_suite_id,
            tools=shared_tools,
            stable_messages=shared_stable_messages,
        )
        results.append({"label": label, **result})
    comparable_models = len({item["model"] for item in results}) == 1
    winner = max(results, key=lambda item: item["hot_rate"])["label"]
    return {
        "suite_id": resolved_suite_id,
        "comparable_models": comparable_models,
        "winner": winner if comparable_models else None,
        "targets": results,
    }


def _print_benchmark_result(result, label=None):
    prefix = f"target={label} " if label else ""
    for index, item in enumerate(result["rounds"], 1):
        prompt = item["prompt_tokens"]
        cached = item["cached_tokens"]
        rate = (cached / prompt * 100) if prompt else 0.0
        print(
            f"{prefix}round={index} prompt={prompt} "
            f"cached={cached} rate={rate:.2f}%"
        )
    print(
        f"{prefix}all_rate={result['all_rate']:.2f}% "
        f"hot_rate={result['hot_rate']:.2f}% "
        f"minimum_hot_round_rate={result['minimum_hot_round_rate']:.2f}% "
        f"catastrophic_miss_count={result['catastrophic_miss_count']} "
        f"threshold={result['minimum_hot_rate']:.2f}% "
        f"passed={result['passed']}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--api-key-env", default="YCODE_BENCH_API_KEY")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--minimum-hot-rate", type=float, required=True)
    parser.add_argument("--compare-model")
    parser.add_argument("--compare-api-base")
    parser.add_argument("--compare-api-key-env")
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"环境变量 {args.api_key_env} 未设置")
    compare_values = (
        args.compare_model,
        args.compare_api_base,
        args.compare_api_key_env,
    )
    if any(compare_values) and not all(compare_values):
        parser.error(
            "--compare-model、--compare-api-base、--compare-api-key-env 必须同时提供"
        )
    if all(compare_values):
        compare_key = os.environ.get(args.compare_api_key_env, "")
        if not compare_key:
            raise SystemExit(f"环境变量 {args.compare_api_key_env} 未设置")
        result = run_ab_benchmark(
            [
                {
                    "label": "official",
                    "model": args.model,
                    "api_base": args.api_base,
                    "api_key": api_key,
                },
                {
                    "label": "compare",
                    "model": args.compare_model,
                    "api_base": args.compare_api_base,
                    "api_key": compare_key,
                },
            ],
            rounds=args.rounds,
            minimum_hot_rate=args.minimum_hot_rate,
        )
        for target in result["targets"]:
            _print_benchmark_result(target, target["label"])
        winner = result["winner"] or "not-comparable"
        print(
            f"comparable_models={result['comparable_models']} winner={winner}"
        )
        raise SystemExit(
            0 if all(target["passed"] for target in result["targets"]) else 1
        )

    result = run_benchmark(
        model=args.model,
        api_base=args.api_base,
        api_key=api_key,
        rounds=args.rounds,
        minimum_hot_rate=args.minimum_hot_rate,
    )
    _print_benchmark_result(result)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
