"""Deterministic decoding for subprocess output across terminal platforms."""

from __future__ import annotations

import codecs
import locale
import os
import re

# ANSI/VT 转义序列整体匹配：CSI（颜色/光标控制）、OSC（窗口标题等）、两字节转义。
# 子进程写管道/日志时可能携带这些序列（vitest 在 CI/FORCE_COLOR 下非 TTY 也上色），
# 而日志文本既要给 GUI/CLI 面板显示，也会经任务快照进入模型上下文，只认纯文本。
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;:?]*[ -/]*[@-~]"           # CSI：\x1b[0;39m / \x1b[2K 等
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC：\x1b]0;title\x07 等
    r"|\x1b[@-Z\\-_]"                        # 两字节转义：\x1b7 / \x1bM 等
)


def strip_ansi_escapes(text: str) -> str:
    """剥离 ANSI/VT 终端转义序列，返回可安全显示与入模的纯文本。

    只删 ESC 字符会留下 ``[33m`` 之类的碎码，必须整段匹配后再兜底删除
    残余 ESC 字节——不完整序列删掉 ESC 即失效，防止劫持终端渲染
    （与 history_renderer._strip_escapes 的防注入立场一致）。
    """
    if not text:
        return text
    cleaned = _ANSI_ESCAPE_RE.sub("", text)
    return cleaned.replace("\x1b", "")


def platform_console_encoding() -> str:
    """Return the encoding used by piped console programs on this platform."""
    if os.name == "nt":
        import ctypes

        code_page = int(ctypes.windll.kernel32.GetConsoleOutputCP())
        if code_page <= 0:
            code_page = int(ctypes.windll.kernel32.GetOEMCP())
        if code_page > 0:
            return f"cp{code_page}"
    return locale.getpreferredencoding(False) or "utf-8"


def decode_process_output(
    data: bytes | bytearray | memoryview | str | None,
    expected_encoding: str | None = None,
) -> str:
    """Decode raw process output without corrupting UTF or OEM text."""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    raw = bytes(data)
    if not raw:
        return ""
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return raw.decode("utf-32")
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    # 当调用方显式指定了预期编码时，先严格按该编码解码；失败则回退 UTF-8。
    # 这同时满足两个场景：
    # 1) 旧版/控制台程序按 gbk/cp936 输出时得到正确中文；
    # 2) 现代工具输出 UTF-8 但 expected_encoding 是 cp936 且 strict 解码失败时，
    #    能自动回退到 UTF-8，避免乱码。
    if expected_encoding:
        try:
            return raw.decode(expected_encoding)
        except UnicodeDecodeError:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(platform_console_encoding(), errors="replace")
