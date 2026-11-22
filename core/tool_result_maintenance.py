import re
import secrets

SNIPPED_PREFIX = "[snipped tool result"
ELIDED_PREFIX = "[elided tool result"
ERROR_PREFIXES = ("🚨", "error:", "blocked:")

HANDLE_LENGTH = 8
_HANDLE_RE = re.compile(r"^\[(?:snipped|elided) tool result #([0-9a-f]{8})\b")
_HANDLE_FULL_RE = re.compile(r"^[0-9a-f]{8}$")

PROTECTION_ZONE_TOKENS = 8000

MIN_SNIP_CHARS = 1024

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
    if not text or is_frozen(text) or _is_error(text):
        return None
    if len(text) < MIN_SNIP_CHARS:
        return None
    max_lines, head, tail, max_line_chars, max_total = _SNIP_STRATEGIES.get(
        tool_name, _DEFAULT_STRATEGY
    )
    lines = text.split("\n")
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
