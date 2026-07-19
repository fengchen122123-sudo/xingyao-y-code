import os

import core.input_parser as input_parser
from core.input_parser import MAX_TEXT_BYTES, parse_user_input


def test_at_file_text_attachment(tmp_path, monkeypatch):
    """@文件 语法附加文本内容，并从正文中移除 @引用"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "note.txt").write_text("hello world", encoding="utf-8")

    content, msgs = parse_user_input("看看 @note.txt")

    assert isinstance(content, str)
    assert "hello world" in content
    assert "@note.txt" not in content
    assert any("note.txt" in m for m in msgs)


def test_backslash_absolute_path_is_extracted(tmp_path):
    """反斜杠盘符绝对路径 D:\\... 仍可识别"""
    p = tmp_path / "back.txt"
    p.write_text("backslash-data", encoding="utf-8")

    content, msgs = parse_user_input(f"分析 {str(p)}")

    assert "backslash-data" in content


def test_forward_slash_absolute_path_is_extracted(tmp_path):
    """正斜杠盘符绝对路径 D:/... 也必须识别"""
    p = tmp_path / "fwd.txt"
    p.write_text("forward-data", encoding="utf-8")
    fwd = str(p).replace("\\", "/")

    content, msgs = parse_user_input(f"分析 {fwd} 谢谢")

    assert "forward-data" in content


def test_binary_file_is_skipped(tmp_path, monkeypatch):
    """含 NUL 字节的二进制文件直接跳过并提示"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")

    content, msgs = parse_user_input("@bin.dat")

    assert any("二进制" in m for m in msgs)
    assert "binary" not in content


def test_oversized_image_is_skipped(tmp_path, monkeypatch):
    """图片超过大小上限跳过"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.png").write_bytes(b"\x89PNG" + b"\x00" * 100)
    monkeypatch.setattr(input_parser, "MAX_IMAGE_BYTES", 10)

    content, msgs = parse_user_input("@big.png")

    assert any("超过 5MB" in m for m in msgs)


def test_text_file_over_size_limit_is_truncated(tmp_path, monkeypatch):
    """文本超过 200KB 截断并附截断标记"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_bytes(b"a" * (MAX_TEXT_BYTES + 100))

    content, msgs = parse_user_input("@big.txt")

    assert "已截断" in content
    # 截断后的正文不应包含完整的超长内容
    assert content.count("a") < MAX_TEXT_BYTES + 100


def test_truncated_utf8_multibyte_not_misjudged_as_binary(tmp_path, monkeypatch):
    """截断切在 UTF-8 多字节序列中间时，用 replace 解码而不是误判二进制跳过"""
    monkeypatch.chdir(tmp_path)
    # 让 raw[:MAX_TEXT_BYTES] 的最后一个字节恰为 emoji 的首字节 0xF0，
    # utf-8 与 gbk 严格解码都会在尾部失败
    payload = b"a" * (MAX_TEXT_BYTES - 1) + "\U0001F600".encode("utf-8") + b"tail"
    (tmp_path / "big.txt").write_bytes(payload)

    content, msgs = parse_user_input("@big.txt")

    assert any("已附加代码/文本" in m for m in msgs)
    assert not any("无法解码" in m for m in msgs)
    assert "已截断" in content


def test_duplicate_attachments_deduped_keep_order(tmp_path, monkeypatch):
    """重复引用保序去重"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.txt").write_text("X", encoding="utf-8")
    (tmp_path / "y.txt").write_text("Y", encoding="utf-8")

    content, msgs = parse_user_input("@x.txt @y.txt @x.txt")

    assert content.count("[附带上下文: x.txt]") == 1
    assert content.index("x.txt") < content.index("y.txt")


def test_at_removal_replaces_only_first_occurrence(tmp_path, monkeypatch):
    """@xxx 只替换首个匹配，正文中相同的字串不被误删"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("Z", encoding="utf-8")

    content, msgs = parse_user_input("@f.txt 正文提到 f.txt 字符")

    assert "正文提到 f.txt 字符" in content
