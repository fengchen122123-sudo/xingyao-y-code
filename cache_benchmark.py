"""DeepSeek / Kimi 生产请求路径缓存命中率基准程序。"""

from __future__ import annotations

import argparse
import os
import uuid

from core.llm import (
    _apply_cache_control,
    _complete_thinking_blocks,
    _extract_cached_tokens,
    _provider_request_options,
    _strip_media_and_merge,
    _tool_contract_fingerprint,
    completion,
    get_tools_schema,
)


def weighted_hot_rate(rounds):
    """排除冷启动轮，按 prompt token 加权计算热启动命中率。"""
    hot_rounds = list(rounds)[1:]
    prompt = sum(int(item["prompt_tokens"]) for item in hot_rounds)
    cached = sum(int(item["cached_tokens"]) for item in hot_rounds)
    return (cached / prompt * 100) if prompt else 0.0


def _prompt_tokens(usage):
    if isinstance(usage, dict):
        return int(usage.get("prompt_tokens", 0) or 0)
    return int(getattr(usage, "prompt_tokens", 0) or 0)


def _run_one_request(*, model, api_base, api_key, messages, tools, cache_session_id):
    cleaned, _, _ = _strip_media_and_merge(messages, model)
    tool_contract = _tool_contract_fingerprint(tools)
    request = {
        "model": model,
        "api_key": api_key,
        "api_base": api_base,
        "messages": _apply_cache_control(cleaned, model),
        "tools": tools,
        # 基准程序验证生产工具契约，但禁止模型真的执行工具。
        "tool_choice": "none",
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
    # 足够长的确定性前缀，让 99% 目标衡量缓存能力而不是被几枚增量 token 稀释。
    stable_lines = [f"cache-benchmark-stable-line-{index:05d}" for index in range(6000)]
    return [{
        "role": "system",
        "content": (
            "这是 Y-CODE 缓存基准的合成稳定前缀，不包含用户文件或对话内容。\n"
            + "\n".join(stable_lines)
        ),
    }]


def run_benchmark(
    *,
    model,
    api_base,
    api_key,
    rounds=3,
    minimum_hot_rate=99.0,
    session_id=None,
    tools=None,
):
    if int(rounds) < 2:
        raise ValueError("rounds 必须至少为 2，第一轮用于冷启动")
    if not api_key:
        raise ValueError("API Key 不能为空")

    tools = get_tools_schema() if tools is None else list(tools)
    messages = _stable_messages()
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
        results.append({
            "prompt_tokens": _prompt_tokens(usage),
            "cached_tokens": _extract_cached_tokens(usage),
        })
        assistant = {"role": "assistant", "content": content or "OK"}
        if reasoning is not None:
            assistant["reasoning_content"] = reasoning
        if thinking_blocks is not None:
            assistant["thinking_blocks"] = thinking_blocks
        messages.append(assistant)

    hot_rate = weighted_hot_rate(results)
    return {
        "model": model,
        "rounds": results,
        "hot_rate": hot_rate,
        "minimum_hot_rate": float(minimum_hot_rate),
        "passed": hot_rate >= float(minimum_hot_rate),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--api-key-env", default="YCODE_BENCH_API_KEY")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--minimum-hot-rate", type=float, required=True)
    args = parser.parse_args()

    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"环境变量 {args.api_key_env} 未设置")
    result = run_benchmark(
        model=args.model,
        api_base=args.api_base,
        api_key=api_key,
        rounds=args.rounds,
        minimum_hot_rate=args.minimum_hot_rate,
    )
    for index, item in enumerate(result["rounds"], 1):
        prompt = item["prompt_tokens"]
        cached = item["cached_tokens"]
        rate = (cached / prompt * 100) if prompt else 0.0
        print(f"round={index} prompt={prompt} cached={cached} rate={rate:.2f}%")
    print(
        f"hot_rate={result['hot_rate']:.2f}% "
        f"threshold={result['minimum_hot_rate']:.2f}% passed={result['passed']}"
    )
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
