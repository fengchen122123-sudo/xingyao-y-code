# -*- coding: utf-8 -*-
"""IDE 编辑器文件服务：读取路径的编码 / BOM / 换行 / 体积 / 二进制处理。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import file_service
from core.ide.file_service import FileServiceError, read_text_file


def test_read_utf8_plain(tmp_path):
    f = tmp_path / "a.py"
    # 用 write_bytes 落盘：write_text 会按 os.linesep 翻译换行（Windows 上变 \r\n），
    # 本用例要验证的是「纯 LF 文件原样读回」，不能让夹具自己引入 CRLF。
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))
    assert info["content"] == "x = 1\n"
    assert info["encoding"] == "utf-8"
    assert info["bom"] is False
    assert info["eol"] == "lf"
    assert len(info["hash"]) == 64


def test_read_utf8_with_bom_marks_bom(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    info = read_text_file(str(f))
    assert info["bom"] is True
    # BOM 不进正文，否则编辑器里会出现一个不可见字符
    assert info["content"] == "x = 1\n"
    assert info["encoding"] == "utf-8"


def test_read_gbk_file(tmp_path):
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文内容\n".encode("gbk"))
    info = read_text_file(str(f))
    assert info["content"] == "中文内容\n"
    assert info["encoding"] == "gbk"


def test_read_detects_crlf(tmp_path):
    f = tmp_path / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    info = read_text_file(str(f))
    assert info["eol"] == "crlf"


def test_read_detects_mixed_eol(tmp_path):
    f = tmp_path / "mixed.py"
    f.write_bytes(b"a = 1\r\nb = 2\n")
    info = read_text_file(str(f))
    assert info["eol"] == "mixed"


def test_read_rejects_binary(tmp_path):
    f = tmp_path / "app.exe"
    f.write_bytes(b"MZ\x90\x00" + b"\x00" * 32)
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "二进制" in str(exc.value)


def test_read_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(file_service, "IDE_EDIT_MAX_BYTES", 8)
    f = tmp_path / "big.txt"
    f.write_text("0123456789", encoding="utf-8")
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "过大" in str(exc.value)


def test_read_rejects_unknown_encoding(tmp_path):
    # 0x81 在 GBK 里是非法首字节，UTF-8 也解不开
    f = tmp_path / "weird.txt"
    f.write_bytes(b"\x81\x81\x81")
    with pytest.raises(FileServiceError) as exc:
        read_text_file(str(f))
    assert "编码" in str(exc.value)


def test_read_rejects_missing_file(tmp_path):
    with pytest.raises(FileServiceError):
        read_text_file(str(tmp_path / "nope.py"))
