# -*- coding: utf-8 -*-
"""编码 agent 与不同 LLM 模型适配度基准测试脚本。

用途：
    真实调用 litellm.completion（stream=False）测两个核心指标：
      1. 缓存命中率（cached_tokens / prompt_tokens，冷启动 vs 热启动）
      2. token 消耗（prompt / completion / total / cached 的逐轮与汇总统计）

场景：
    - 场景一【连续对话缓存测试】：固定大 system prompt（近似真实 agent 的
      大 prompt），连续多轮对话，观察第一轮冷启动（cached=0）与后续轮次
      热启动（前缀缓存命中）的差异。
    - 场景二【工具调用模拟】：system + user + assistant(tool_calls) + tool
      结果的多轮结构，模拟 agent 使用工具的场景。

运行示例（在项目 venv 下）：
    venv\\Scripts\\python.exe bench\\adapt_bench.py --api-key <KEY> \\
        --rounds 3 --tool-rounds 3 --context 2000

短测（仅 1 次真实 API 调用）：
    venv\\Scripts\\python.exe bench\\adapt_bench.py --api-key <KEY> \\
        --rounds 1 --tool-rounds 0 --context 100
"""

import argparse
import json
import os
import sys
from copy import deepcopy

# ---------------------------------------------------------------------------
# 路径与导入准备
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 与 core/llm.py 保持一致：使用本地模型价格表，避免启动时远程拉取超时
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_TELEMETRY", "False")

try:
    # 优先使用项目包装函数（可被测试 mocker.patch 拦截，且已配置懒加载）
    from core.llm import completion as _completion
    _IMPORT_SOURCE = "core.llm.completion"
except Exception as _import_err:  # 独立环境降级为直接使用 litellm
    import litellm
    litellm.suppress_debug_info = True
    _completion = litellm.completion
    _IMPORT_SOURCE = "litellm.completion (fallback: %s)" % _import_err

# 单次请求超时（秒）
_REQUEST_TIMEOUT = 120

# 场景一固定系统提示词（不含上下文填充部分）
_BASE_SYSTEM_PROMPT = (
    "你是一个专业的编码 agent，负责在大型代码仓库中完成代码阅读、搜索与修改任务。"
    "回答要求：简洁、准确、直接给出结论，不要多余寒暄。"
)

# 场景二工具指令：强制模型必须先调用 read_file 工具，保证 tool_calls 结构真实出现
_TOOL_INSTRUCTION = (
    "\n\n【工具使用规则】\n"
    "回答当前问题前，你必须先调用 read_file 工具读取相关文件，"
    "然后基于工具返回结果给出最终答复。禁止跳过工具直接回答。"
)

# 场景一每轮 user 消息模板
_CHAT_USER_MSG_TEMPLATE = "第 {n} 轮：请用一句话说明这个编码 agent 项目的核心功能。"

# 场景二每轮 user 消息模板（强调每轮都必须先调用工具）
_TOOL_USER_MSG_TEMPLATE = (
    "第 {n} 轮：请继续分析项目。本轮你仍必须先调用 read_file 读取文件，"
    "再基于工具结果简要总结，禁止跳过工具直接回答。"
)

# 工具结果（模拟）：随轮数变化，避免内容完全一致影响对比
_TOOL_RESULT_TEMPLATE = (
    "read_file 返回：文件 E:/fch/Y-code_tui/core/llm.py 第 1-120 行，"
    "包含 litellm 懒加载与 completion 包装函数（模拟结果，第 {n} 轮）。"
)

# 场景二传给模型的最小工具 schema（read_file），让模型能真实发起工具调用
_READ_FILE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取指定文件的文本内容",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
            "required": ["path"],
        },
    },
}


class BenchmarkAPIError(Exception):
    """封装 API 调用失败的异常，便于上层统一捕获并以非 0 退出码结束。"""


# ---------------------------------------------------------------------------
# 通用工具函数
# ---------------------------------------------------------------------------
def _get_attr(obj, name, default):
    """同时兼容对象属性与 dict 键的取值器，取不到时返回 default。"""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _build_system_prompt(context_chars):
    """构造指定近似字符数的"系统提示词 + 项目上下文"。

    上下文通过重复拼接代码仓库描述文本近似模拟真实 agent 的大 prompt，
    实际返回的字符串长度约为 context_chars 字符。
    """
    base = _BASE_SYSTEM_PROMPT + "\n\n【项目上下文】\n"
    if len(base) >= context_chars:
        return base[:context_chars]
    chunk = (
        "module core/agent_service.py: 负责 agent 主循环、任务调度与权限管理；"
        "module core/llm.py: 封装 litellm 完成大模型调用与 token 估算；"
        "module core/tools.py: 注册文件读写、目录列举、命令执行等工具；"
        "module core/model_selector.py: 多模型选择、连通性与价格表管理；"
        "module core/mcp_client.py: MCP 客户端，扩展外部工具能力。"
    )
    padding = ""
    while len(base) + len(padding) < context_chars:
        padding += chunk
    return base + padding[: context_chars - len(base)]


def _extract_usage(resp):
    """从 litellm 响应中提取 usage 四元组（prompt/completion/total/cached）。

    健壮性：usage 字段缺失或结构不符时全部优雅降级为 0。
    兼容两套字段命名：
      - OpenAI 风格：prompt_tokens / completion_tokens / total_tokens
      - Anthropic 风格：input_tokens / output_tokens（Kimi 等协议端点）
    当 prompt_tokens/completion_tokens 均为 0 时改读 input_tokens/output_tokens。
    cached_tokens 依次尝试：
      prompt_tokens_details.cached_tokens → cache_read_input_tokens
      → prompt_cache_hit_tokens（DeepSeek 兼容字段）。
    """
    usage = _get_attr(resp, "usage", None)
    prompt = int(_get_attr(usage, "prompt_tokens", 0) or 0)
    completion = int(_get_attr(usage, "completion_tokens", 0) or 0)
    total = int(_get_attr(usage, "total_tokens", 0) or 0)
    if not prompt and not completion:  # Anthropic 风格 usage 字段
        prompt = int(_get_attr(usage, "input_tokens", 0) or 0)
        completion = int(_get_attr(usage, "output_tokens", 0) or 0)
    if not total:
        total = prompt + completion
    cached = 0
    details = _get_attr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = int(_get_attr(details, "cached_tokens", 0) or 0)
    if not cached:  # Anthropic 协议缓存字段
        cached = int(_get_attr(usage, "cache_read_input_tokens", 0) or 0)
    if not cached:  # DeepSeek 等厂商的兼容字段
        cached = int(_get_attr(usage, "prompt_cache_hit_tokens", 0) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "cached_tokens": cached}


def _apply_cache_control(messages, model):
    """给消息列表加 cache_control 标记，启用 Anthropic 协议 prompt caching。

    仅当 model 以 "anthropic/" 开头时生效（Kimi 等 Anthropic 协议端点必须显式
    标记 system 与最后一条 assistant 消息，否则缓存命中率恒为 0）；
    其他协议直接原样返回（OpenAI 兼容端点自动缓存，显式标记反而可能被网关拒绝）。

    标记策略（与 core/llm.py._apply_cache_control 一致）：
    - system 消息：必缓存（固定前缀）
    - 最后一条 assistant 消息：设置缓存边界，从后往前找第一条 role=="assistant"。

    原地修改并返回 messages；assistant 消息原有的 content/tool_calls/
    reasoning_content 字段保持不变（仅追加 cache_control）。
    """
    prefix = str(model or "").split("/")[0].lower()
    if prefix != "anthropic":
        return messages
    if not messages:
        return messages

    # 清除已有标记，避免重复累积（幂等重设）
    for msg in messages:
        if isinstance(msg, dict):
            msg.pop("cache_control", None)

    # 1. 标记 system 消息（如果有）
    if messages[0].get("role") == "system":
        messages[0]["cache_control"] = {"type": "ephemeral"}

    # 2. 标记最后一条 assistant 消息（缓存边界）
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            messages[i]["cache_control"] = {"type": "ephemeral"}
            break

    return messages


def _extract_content(resp):
    """提取助手回复文本，content 为 None 时降级为空字符串。"""
    try:
        return _get_attr(resp.choices[0].message, "content", "") or ""
    except Exception:
        return ""


def _extract_reasoning_content(resp):
    """提取 thinking 模式模型的推理内容，返回 dict。

    返回 {"reasoning_content": <str|None>, "thinking_blocks": <list|None>}：
      - reasoning_content：OpenAI 形状（deepseek-v4-flash 等 thinking 模型使用），
        读 resp.choices[0].message.reasoning_content；后续请求必须原样回传。
      - thinking_blocks：Anthropic 协议形状（Kimi 等），读
        resp.provider_specific_fields.thinking_blocks；仅保留带 signature 的
        完整块（与 core/llm.py._complete_thinking_blocks 一致），保留原始 dict 结构。
    两者都取不到时返回 {"reasoning_content": None, "thinking_blocks": None}。
    """
    reasoning_content = None
    try:
        reasoning_content = _get_attr(
            resp.choices[0].message, "reasoning_content", None)
    except Exception:
        reasoning_content = None
    thinking_blocks = None
    try:
        # Kimi (anthropic 协议)：thinking 块在 message.thinking_blocks
        # （litellm 1.91.2 非流式响应已暴露该字段，且必须原样回传）
        msg = resp.choices[0].message
        blocks = getattr(msg, "thinking_blocks", None)
        if isinstance(blocks, list):
            complete = [
                dict(b) for b in blocks
                if isinstance(b, dict)
                and (
                    bool(b.get("signature"))
                    or (
                        b.get("type") == "redacted_thinking"
                        and bool(b.get("data"))
                    )
                )
            ]
            thinking_blocks = complete or None
    except Exception:
        thinking_blocks = None
    return {"reasoning_content": reasoning_content,
            "thinking_blocks": thinking_blocks}

def _extract_tool_calls(resp):
    """提取模型返回的 tool_calls 列表，返回 [(id, name, arguments), ...]。
    兼容 resp.tool_calls 与 resp.choices[0].message.tool_calls 两种位置。
    """
    raw = None
    try:
        raw = _get_attr(resp, "tool_calls", None) or _get_attr(
            resp.choices[0].message, "tool_calls", None)
    except Exception:
        raw = None
    result = []
    for tc in raw or []:
        tc_id = _get_attr(tc, "id", "")
        func = _get_attr(tc, "function", None)
        name = _get_attr(func, "name", "")
        arguments = _get_attr(func, "arguments", "")
        result.append((tc_id, name, arguments))
    return result


def _call_completion(fn, model, api_key, api_base, messages, max_tokens,
                     tools=None):
    """发起一次真实 API 调用（stream=False）。

    tools 可选：传入工具 schema 列表时模型可发起真实 tool_calls。
    失败时抛出 BenchmarkAPIError，由上层统一打印错误并以非 0 退出码结束。
    """
    kwargs = {
        "model": model,
        "api_key": api_key,
        "api_base": api_base,
        "messages": messages,
        "max_tokens": max_tokens,
        "stream": False,
        "timeout": _REQUEST_TIMEOUT,
    }
    if tools:
        kwargs["tools"] = tools
    try:
        return fn(**kwargs)
    except Exception as exc:
        raise BenchmarkAPIError("API 调用失败：%r" % (exc,))


# ---------------------------------------------------------------------------
# 场景一：连续对话缓存测试
# ---------------------------------------------------------------------------
def run_chat_scenario(fn, model, api_key, api_base, rounds, system_prompt):
    """连续多轮对话，每轮追加一条 user 消息与 assistant 回复。

    返回 (messages, details)：details 为每轮的 usage 记录列表。
    第一轮为冷启动（cached_tokens 预期为 0），后续轮次前缀缓存命中。
    """
    messages = [{"role": "system", "content": system_prompt}]
    details = []
    for i in range(1, rounds + 1):
        messages.append({"role": "user",
                         "content": _CHAT_USER_MSG_TEMPLATE.format(n=i)})
        # anthropic 协议（Kimi 等）须在每次请求前打 cache_control 标记
        _apply_cache_control(messages, model)
        resp = _call_completion(fn, model, api_key, api_base,
                                messages, max_tokens=128)
        usage = _extract_usage(resp)
        details.append(_make_detail("chat", i, "reply", usage))
        # 追加 assistant 回复，让后续轮次的 messages 前缀保持真实增长
        # thinking 模型需回传 reasoning_content，否则后续请求会被 API 拒绝
        asst_msg = {"role": "assistant", "content": _extract_content(resp)}
        rc_fields = _extract_reasoning_content(resp)
        if rc_fields["reasoning_content"]:
            asst_msg["reasoning_content"] = rc_fields["reasoning_content"]
        if rc_fields["thinking_blocks"]:
            asst_msg["thinking_blocks"] = rc_fields["thinking_blocks"]
        messages.append(asst_msg)
    return messages, details


# ---------------------------------------------------------------------------
# 场景二：工具调用模拟
# ---------------------------------------------------------------------------
def run_tool_scenario(fn, model, api_key, api_base, tool_rounds, system_prompt):
    """模拟 agent 工具调用：每轮 = 模型生成 tool_calls + tool 结果 + 模型总结。

    每轮产生两次 API 调用（step=tool_call 与 step=tool_result），
    返回 (messages, details)。若模型未按指令生成 tool_calls，
    则降级构造一条合成 tool_call 以维持消息结构，并在记录中标出。
    """
    messages = [
        {"role": "system", "content": system_prompt + _TOOL_INSTRUCTION},
        {"role": "user", "content": "请先调用 read_file 工具读取项目核心文件，然后总结。"},
    ]
    details = []
    synthetic_seq = 0
    for i in range(1, tool_rounds + 1):
        # --- 步骤 1：模型生成 tool_calls（传入工具 schema，保证真实 tool_calls）---
        # anthropic 协议（Kimi 等）须在每次请求前打 cache_control 标记
        _apply_cache_control(messages, model)
        resp = _call_completion(fn, model, api_key, api_base,
                                messages, max_tokens=4096,
                                tools=[_READ_FILE_TOOL_SCHEMA])
        usage1 = _extract_usage(resp)
        tool_calls = _extract_tool_calls(resp)
        emitted = bool(tool_calls)
        if emitted:
            # 回传全部 tool_calls（Kimi 可能一次返回多个），不能只取第一个
            all_tool_calls = [
                {
                    "id": tc[0] or "call_bench_%d" % i,
                    "type": "function",
                    "function": {"name": tc[1], "arguments": tc[2]},
                }
                for tc in tool_calls
            ]
            call_id = all_tool_calls[0]["id"]
            call_name = all_tool_calls[0]["function"]["name"]
            call_args = all_tool_calls[0]["function"]["arguments"]
        else:
            # 降级：构造合成 tool_call 维持结构，保证后续轮次可继续
            synthetic_seq += 1
            call_id = "call_synth_%d" % synthetic_seq
            call_name = "read_file"
            call_args = '{"path": "E:/fch/Y-code_tui/core/llm.py"}'
            all_tool_calls = [{
                "id": call_id,
                "type": "function",
                "function": {"name": call_name, "arguments": call_args},
            }]
            print("  [警告] 第 %d 轮模型未返回 tool_calls，已构造合成调用继续。" % i)
        details.append(_make_detail("tool", i, "tool_call", usage1,
                                    tool_calls_emitted=emitted))
        # 追加 assistant(tool_calls) 与 tool 结果消息
        # thinking 模型的 reasoning_content / thinking_blocks 必须原样回传
        asst_tool_msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": all_tool_calls,
        }
        rc_fields = _extract_reasoning_content(resp)
        if rc_fields["reasoning_content"]:
            asst_tool_msg["reasoning_content"] = rc_fields["reasoning_content"]
        if rc_fields["thinking_blocks"]:
            asst_tool_msg["thinking_blocks"] = rc_fields["thinking_blocks"]
        messages.append(asst_tool_msg)
        # 为每个 tool_call 都追加对应的 tool 结果（Kimi 可能一次返回多个）
        for tc_entry in all_tool_calls:
            messages.append({
                "role": "tool",
                "tool_call_id": tc_entry["id"],
                "content": _TOOL_RESULT_TEMPLATE.format(n=i),
            })
        # --- 步骤 2：携带 tool 结果再次调用，模型给出最终答复 ---
        _apply_cache_control(messages, model)
        resp2 = _call_completion(fn, model, api_key, api_base,
                                 messages, max_tokens=2048)
        usage2 = _extract_usage(resp2)
        details.append(_make_detail("tool", i, "tool_result", usage2))
        asst_msg2 = {"role": "assistant", "content": _extract_content(resp2)}
        rc2_fields = _extract_reasoning_content(resp2)
        if rc2_fields["reasoning_content"]:
            asst_msg2["reasoning_content"] = rc2_fields["reasoning_content"]
        if rc2_fields["thinking_blocks"]:
            asst_msg2["thinking_blocks"] = rc2_fields["thinking_blocks"]
        messages.append(asst_msg2)
    return messages, details

def _make_detail(scenario, round_no, step, usage, tool_calls_emitted=None):
    """构造一条逐轮明细记录（含缓存命中率）。"""
    prompt = usage["prompt_tokens"]
    rate = usage["cached_tokens"] / prompt if prompt else 0.0
    entry = {
        "scenario": scenario,
        "round": round_no,
        "step": step,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "cached_tokens": usage["cached_tokens"],
        "cache_hit_rate": round(rate, 4),
    }
    if tool_calls_emitted is not None:
        entry["tool_calls_emitted"] = tool_calls_emitted
    return entry


# ---------------------------------------------------------------------------
# 统计、汇总与报告
# ---------------------------------------------------------------------------
def _summarize_scenario(scenario, details):
    """按场景汇总：总 prompt/completion/cached、缓存命中率、预估节省 token。"""
    rows = [d for d in details if d["scenario"] == scenario]
    if not rows:
        return None
    total_prompt = sum(r["prompt_tokens"] for r in rows)
    total_completion = sum(r["completion_tokens"] for r in rows)
    total_cached = sum(r["cached_tokens"] for r in rows)
    return {
        "calls": len(rows),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cached_tokens": total_cached,
        "cache_hit_rate": round(total_cached / total_prompt, 4) if total_prompt else 0.0,
        "estimated_saved_tokens": total_cached,
    }


def _build_summary(details):
    """构建整体汇总 dict（含两个场景的分项与总计）。"""
    chat = _summarize_scenario("chat", details)
    tool = _summarize_scenario("tool", details)
    total_prompt = sum(r["prompt_tokens"] for r in details)
    total_completion = sum(r["completion_tokens"] for r in details)
    total_cached = sum(r["cached_tokens"] for r in details)
    return {
        "calls": len(details),
        "total_prompt_tokens": total_prompt,
        "total_completion_tokens": total_completion,
        "total_tokens": total_prompt + total_completion,
        "total_cached_tokens": total_cached,
        "overall_cache_hit_rate": round(total_cached / total_prompt, 4) if total_prompt else 0.0,
        "estimated_saved_tokens": total_cached,
        "chat": chat,
        "tool": tool,
    }


def _print_report(args, details, summary):
    """以易读文本打印基准报告（模型、参数、逐轮表格、汇总）。"""
    line = "=" * 78
    print(line)
    print("编码 Agent × LLM 适配度基准测试报告")
    print(line)
    print("模型: %s" % args.model)
    print("API Base: %s" % args.api_base)
    print("导入方式: %s" % _IMPORT_SOURCE)
    print("参数: rounds=%d, tool_rounds=%d, context≈%d 字符"
          % (args.rounds, args.tool_rounds, args.context))
    print(line)

    # 逐轮明细表格
    header = ("%-7s %-5s %-11s %8s %11s %8s %8s %9s"
              % ("场景", "轮次", "步骤", "prompt", "completion", "total",
                 "cached", "命中率"))
    print("逐轮明细：")
    print(header)
    print("-" * 78)
    for d in details:
        print("%-7s %-5d %-11s %8d %11d %8d %8d %8.1f%%"
              % (d["scenario"], d["round"], d["step"],
                 d["prompt_tokens"], d["completion_tokens"],
                 d["total_tokens"], d["cached_tokens"],
                 d["cache_hit_rate"] * 100))
    print("-" * 78)

    # 汇总
    print("汇总：")
    s = summary
    print("  总调用次数            : %d" % s["calls"])
    print("  总 prompt_tokens      : %d" % s["total_prompt_tokens"])
    print("  总 completion_tokens  : %d" % s["total_completion_tokens"])
    print("  总 total_tokens       : %d" % s["total_tokens"])
    print("  总 cached_tokens      : %d" % s["total_cached_tokens"])
    print("  总体缓存命中率        : %.2f%%" % (s["overall_cache_hit_rate"] * 100))
    print("  预估节省 token 数     : %d" % s["estimated_saved_tokens"])
    for label, key in (("连续对话场景", "chat"), ("工具调用场景", "tool")):
        sub = s.get(key)
        if not sub:
            continue
        print("  %s              : %d 次调用，命中率 %.2f%%，节省 %d tokens"
              % (label, sub["calls"], sub["cache_hit_rate"] * 100,
                 sub["estimated_saved_tokens"]))
    print(line)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main():
    """命令行入口：解析参数、依次跑两个场景、输出报告与可选 JSON。"""
    parser = argparse.ArgumentParser(
        description="编码 agent 与不同 LLM 模型的适配度基准测试（缓存命中率与 token 消耗）")
    parser.add_argument("--model", default="openai/deepseek-v4-flash",
                        help="litellm 模型名（默认 openai/deepseek-v4-flash）")
    parser.add_argument("--api-base", default="https://api.deepseek.com",
                        help="API 基础地址（默认 https://api.deepseek.com）")
    parser.add_argument("--api-key", required=True,
                        help="API Key（必填，通过命令行传入）")
    parser.add_argument("--rounds", type=int, default=3,
                        help="连续对话轮数（默认 3）")
    parser.add_argument("--tool-rounds", type=int, default=3,
                        help="工具调用模拟轮数（默认 3）")
    parser.add_argument("--context", type=int, default=2000,
                        help="系统提示词+任务上下文的近似字符数（默认 2000）")
    parser.add_argument("--out", default=None,
                        help="可选：输出 JSON 结果保存路径")
    args = parser.parse_args()

    if args.rounds < 0 or args.tool_rounds < 0 or args.context < 0:
        print("错误：rounds / tool_rounds / context 不能为负数。")
        sys.exit(2)

    details = []
    system_prompt = _build_system_prompt(args.context)
    try:
        # 场景一：连续对话缓存测试
        if args.rounds > 0:
            print("[场景一] 连续对话缓存测试：%d 轮 ..." % args.rounds)
            _, chat_details = run_chat_scenario(
                _completion, args.model, args.api_key, args.api_base,
                args.rounds, system_prompt)
            details.extend(chat_details)
            print("[场景一] 完成。")
        # 场景二：工具调用模拟
        if args.tool_rounds > 0:
            print("[场景二] 工具调用模拟：%d 轮（每轮 2 次调用）..." % args.tool_rounds)
            _, tool_details = run_tool_scenario(
                _completion, args.model, args.api_key, args.api_base,
                args.tool_rounds, system_prompt)
            details.extend(tool_details)
            print("[场景二] 完成。")
    except BenchmarkAPIError as exc:
        print("❌ %s" % exc)
        sys.exit(1)

    if not details:
        print("警告：没有任何 API 调用发生（rounds 与 tool_rounds 均为 0）。")

    summary = _build_summary(details)
    _print_report(args, details, summary)

    # 可选：输出结构化 JSON
    if args.out:
        result = {
            "model": args.model,
            "api_base": args.api_base,
            "params": {
                "rounds": args.rounds,
                "tool_rounds": args.tool_rounds,
                "context_chars": args.context,
            },
            "details": details,
            "summary": summary,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print("JSON 结果已写入: %s" % args.out)


if __name__ == "__main__":
    main()
