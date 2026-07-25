"""Deterministic decoding for subprocess output across terminal platforms."""

from __future__ import annotations

import codecs
import locale
import os


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
