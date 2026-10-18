"""工具结果的分层本地维护（Snip / Elide），服务于 DeepSeek 前缀缓存命中率。

设计来源（MIT 开源源码）：
- Waveloom pkg/compaction/compaction.go：分层水位线 + 决策单调
  （只允许 无→snip、无→elide、snip→elide，绝不降级或同级重复），尾部保护区。
- Reasonix internal/agent/prune.go：marker 前缀幂等（[snipped]/[elided] 开头的
  结果永不重复改写），错误结果不改写。

不变量：
1. 每条工具结果的字节最多被改写两次（snip 一次、elide 升级一次），之后永久冻结；
2. 改写只发生在 DB 层（core/memory.py 调用），请求路径绝不改写历史中间字节；
3. 尾部保护区内（最近 PROTECTION_ZONE_TOKENS 估算 token）的消息永不被改写；
4. 错误结果（🚨 前缀）不改写——错误文本是模型自我纠正的依据。
"""

import re
import secrets

SNIPPED_PREFIX = "[snipped tool result"
ELIDED_PREFIX = "[elided tool result"
ERROR_PREFIXES = ("🚨", "error:", "blocked:")

# 归档句柄：8 位小写十六进制（secrets.token_hex(4)），写在 marker 里形如
# "[snipped tool result #ab12cd34 — read_file, ...]"。模型据此用
# tool_result_read 按需取回改写前的原文，使 snip/elide 成为可逆折叠。
HANDLE_LENGTH = 8
_HANDLE_RE = re.compile(r"^\[(?:snipped|elided) tool result #([0-9a-f]{8})\b")
_HANDLE_FULL_RE = re.compile(r"^[0-9a-f]{8}$")

# 尾部保护区：最近这么多估算 token 内的消息永不被改写（Waveloom 同值）
PROTECTION_ZONE_TOKENS = 8000

# 小于该体积的结果不值得改写（省不了多少 token 还会破坏前缀）
MIN_SNIP_CHARS = 1024

# 每种工具的 snip 几何：(max_lines, head_lines, tail_lines, max_line_chars, max_total_chars)
_SNIP_STRATEGIES = {
    "read_file": (200, 150, 10, 4000, 30000),
    "fetch_url": (200, 150, 10, 4000, 30000),
    "search_code": (200, 150, 10, 4000, 30000),
    "glob_files": (200, 150, 10, 4000, 30000),
    "search_news": (200, 150, 10, 4000, 30000),
    "execute_command": (60, 20, 30, 2000, 12000),
}
_DEFAULT_STRATEGY = (60, 20, 30, 2000, 12000)


def new_handle():
    """生成一次性归档句柄：8 位小写十六进制。"""
    return secrets.token_hex(HANDLE_LENGTH // 2)


def is_valid_handle(handle):
    """句柄是否形如 8 位小写十六进制（工具入参校验用，防注入/防乱查）。"""
    return bool(handle) and bool(_HANDLE_FULL_RE.match(str(handle)))


def extract_handle(text):
    """从 marker 中解析归档句柄；不是 marker 或格式不符返回 None。"""
    if not text:
        return None
    match = _HANDLE_RE.match(str(text))
    return match.group(1) if match else None


def _marker(prefix, handle, tool_name, detail):
    """统一构造 marker 头：`<prefix> #<handle> — <tool>, <detail>]`。

    prefix 必须是 SNIPPED_PREFIX / ELIDED_PREFIX 原文，否则 is_snipped /
    is_elided 判定失效（marker 幂等靠前缀）。
    """
    token = handle or new_handle()
    return f"{prefix} #{token} — {tool_name}, {detail}]"


def estimate_text_tokens(text):
    """本地 token 估算：ASCII 按 bytes/4，CJK 按约 1 字符/token，取大者。

    只用于尾部保护区边界判断（触发判断用真实 prompt_tokens，见 agent_service），
    允许粗略但必须对中文会话不低估。
    """
    if not text:
        return 0
    cjk = sum(1 for c in text if ord(c) > 0x2E7F)
    return max(len(text.encode("utf-8")) // 4, cjk)


def is_snipped(text):
    return bool(text) and text.startswith(SNIPPED_PREFIX)


def is_elided(text):
    return bool(text) and text.startswith(ELIDED_PREFIX)


def is_frozen(text):
    return is_snipped(text) or is_elided(text)


def _is_error(text):
    return bool(text) and text.startswith(ERROR_PREFIXES)


def snip_tool_result(tool_name, text, handle=None):
    """Tier 1：head+tail 截断。返回新文本；不适用（已冻结/错误/太小）返回 None。

    handle：归档句柄，由调用方（改写前落库）传入；不传则内部新生成。
    """
    if not text or is_frozen(text) or _is_error(text):
        return None
    if len(text) < MIN_SNIP_CHARS:
        return None
    max_lines, head, tail, max_line_chars, max_total = _SNIP_STRATEGIES.get(
        tool_name, _DEFAULT_STRATEGY
    )
    lines = text.split("\n")
    # 1) 行数截断（优先，语义最友好）
    if len(lines) > max_lines and len(lines) > head + tail + 10:
        omitted = len(lines) - head - tail
        return (
            _marker(
                SNIPPED_PREFIX, handle, tool_name,
                f"已省略 {omitted} 行；完整原文可用 tool_result_read 按句柄取回",
            )
            + "\n"
            + "\n".join(lines[:head])
            + f"\n\n[... {omitted} lines omitted — full result already processed by Agent]\n\n"
            + "\n".join(lines[-tail:])
        )
    # 2) 总字符截断
    if len(text) > max_total:
        half = max_total // 2
        newline = text.find("\n", half)
        if half < newline < len(text) - half:
            head_text, tail_text = text[:newline], text[len(text) - half:]
        else:
            head_text, tail_text = text[:half], text[len(text) - half:]
        return (
            _marker(
                SNIPPED_PREFIX, handle, tool_name,
                f"已截断 {len(text)} → {max_total} 字符；"
                f"完整原文可用 tool_result_read 按句柄取回",
            )
            + "\n"
            + head_text
            + "\n[... content truncated — full result already processed by Agent]\n"
            + tail_text
        )
    # 3) 单行截断（base64 / minified JSON 类长行）
    new_lines, changed = [], False
    for line in lines:
        if len(line) > max_line_chars:
            new_lines.append(
                line[:max_line_chars]
                + f"... [line truncated: {len(line)} → {max_line_chars} chars]"
            )
            changed = True
        else:
            new_lines.append(line)
    if changed:
        return (
            _marker(
                SNIPPED_PREFIX, handle, tool_name,
                "已截断超长行；完整原文可用 tool_result_read 按句柄取回",
            )
            + "\n" + "\n".join(new_lines)
        )
    return None


def elide_tool_result(tool_name, text, handle=None):
    """Tier 2：整体占位符化。snipped 可升级一次；elided/错误/太小返回 None。

    handle：不传时优先复用 text 里已有的句柄（snip → elide 升级必须同一句柄，
    否则同一结果的原文会被重复归档），没有则新生成。
    """
    if not text or is_elided(text) or _is_error(text):
        return None
    if len(text) < MIN_SNIP_CHARS:
        return None
    token = handle or extract_handle(text)
    return _marker(
        ELIDED_PREFIX, token, tool_name,
        f"{len(text)} chars archived; full text retrievable via tool_result_read; "
        f"re-run the tool if the data is needed again",
    )
