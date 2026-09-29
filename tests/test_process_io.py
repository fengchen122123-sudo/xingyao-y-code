import codecs

from core.process_io import decode_process_output, strip_ansi_escapes


def test_decode_process_output_accepts_windows_oem_chinese():
    raw = "星期六".encode("gbk")

    assert decode_process_output(raw, expected_encoding="gbk") == "星期六"


def test_decode_process_output_prefers_valid_utf8():
    raw = "工具执行完成".encode("utf-8")

    assert decode_process_output(raw, expected_encoding="utf-8") == "工具执行完成"


def test_decode_process_output_uses_explicit_encoding_for_ambiguous_bytes():
    raw = "漏".encode("gbk")
    assert raw.decode("utf-8") == "©"

    assert decode_process_output(raw, expected_encoding="gbk") == "漏"


def test_decode_process_output_honors_unicode_bom():
    raw = codecs.BOM_UTF16_LE + "当前时间".encode("utf-16-le")

    assert decode_process_output(raw, expected_encoding="gbk") == "当前时间"


def test_strip_ansi_escapes_removes_vitest_style_color_codes():
    raw = "⯈ \x1b[0;39m\x1b[33mWARN\x1b[39m HTMLCanvasElement's getContext()\x1b[0m\n"

    assert strip_ansi_escapes(raw) == "⯈ WARN HTMLCanvasElement's getContext()\n"


def test_strip_ansi_escapes_handles_osc_and_two_byte_sequences():
    raw = "\x1b]0;窗口标题\x07正文\x1bM下一行"

    assert strip_ansi_escapes(raw) == "正文下一行"


def test_strip_ansi_escapes_neutralizes_lone_esc_byte():
    # 不完整序列（日志截断常见）：整段正则匹配不上，兜底删 ESC 使其失效
    assert strip_ansi_escapes("前缀\x1b[33") == "前缀[33"
    assert "\x1b" not in strip_ansi_escapes("a\x1bb")


def test_strip_ansi_escapes_keeps_plain_text_and_newlines():
    text = "星期六\n第二行 [0;39m 不是转义\n"

    assert strip_ansi_escapes(text) == text
    assert strip_ansi_escapes("") == ""
