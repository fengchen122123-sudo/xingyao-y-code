import codecs

from core.process_io import decode_process_output


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
