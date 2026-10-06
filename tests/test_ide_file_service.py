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


# ---------------------------------------------------------------------------
# 写入：编码与换行原样保持、原子写、冲突检测
#
# 注意：夹具一律用 write_bytes。Windows 上 Path.write_text 会把 "\n" 翻译成
# os.linesep（实测 3.12：write_text("x = 1\n") 落盘为 b"x = 1\r\n"），会让
# 「纯 LF 文件」的用例悄悄变成 CRLF 用例，断言随之失真。
# ---------------------------------------------------------------------------
from core.ide.file_service import write_text_file


def test_write_keeps_utf8_and_lf(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))
    res = write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is True
    assert f.read_bytes() == b"x = 2\n"


def test_write_keeps_gbk_encoding(tmp_path):
    """GBK 文件写回必须还是 GBK，否则中文变乱码。"""
    f = tmp_path / "gbk.txt"
    f.write_bytes("中文\n".encode("gbk"))
    info = read_text_file(str(f))
    res = write_text_file(
        str(f), "中文改\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is True
    assert f.read_bytes() == "中文改\n".encode("gbk")


def test_write_keeps_bom(tmp_path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfx = 1\n")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert f.read_bytes() == b"\xef\xbb\xbfx = 2\n"


def test_write_keeps_crlf(tmp_path):
    """编辑器内部统一用 LF，写回时必须还原成原文件的 CRLF。"""
    f = tmp_path / "win.py"
    f.write_bytes(b"a = 1\r\nb = 2\r\n")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "a = 1\nb = 3\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert f.read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_write_mixed_eol_unified_to_lf(tmp_path):
    """mixed 换行无法逐行还原：编辑器内部已归一为 LF，保存后统一成 LF。

    这是架构取舍而非「不转换」——CodeMirror 6 的 Text 构造时就把 \r\n / \r
    归一为 \n，原始的行级换行分布传到后端时已丢失。
    """
    f = tmp_path / "mixed.py"
    f.write_bytes(b"a = 1\r\nb = 2\n")
    info = read_text_file(str(f))
    assert info["eol"] == "mixed"
    write_text_file(
        str(f), "a = 9\nb = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    # mixed 不做逐行还原，正文按 LF 统一落盘
    assert f.read_bytes() == b"a = 9\nb = 2\n"


def test_write_detects_conflict(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))
    # 模拟星瑶在用户编辑期间改了同一个文件
    f.write_bytes(b"x = 1  # changed by agent\n")
    res = write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is False
    assert res["conflict"]["disk_hash"] != info["hash"]
    # 冲突时绝不落盘，磁盘内容保持星瑶写的版本
    assert f.read_bytes() == b"x = 1  # changed by agent\n"


def test_write_skips_conflict_check_when_hash_empty(tmp_path):
    """expected_hash 为空表示调用方明确要求覆盖（用户选择了「用我的覆盖」）。"""
    f = tmp_path / "a.py"
    f.write_bytes(b"old\n")
    res = write_text_file(
        str(f), "new\n", "",
        encoding="utf-8", bom=False, eol="lf",
    )
    assert res["ok"] is True
    assert f.read_bytes() == b"new\n"


def test_write_is_atomic_no_temp_left(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))
    write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".ycode-ide-")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# 保存返回的 hash 必须自洽：能直接作为下一次保存的 expected_hash
#
# 前端的用法是「保存成功后用返回的 hash 更新标签状态」，若返回的 hash 与磁盘
# 现状不一致，第二次保存会立刻误报「文件已被外部修改」。这里覆盖纯 LF、CRLF、
# 带 BOM 三种落盘形态；夹具一律用 write_bytes 以避免 Windows 换行翻译。
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,eol,bom",
    [
        (b"x = 1\n", "lf", False),
        (b"a = 1\r\nb = 2\r\n", "crlf", False),
        (b"\xef\xbb\xbfx = 1\n", "lf", True),
    ],
    ids=["lf", "crlf", "bom"],
)
def test_write_returned_hash_round_trips(tmp_path, raw, eol, bom):
    f = tmp_path / "rt.py"
    f.write_bytes(raw)
    info = read_text_file(str(f))
    assert info["eol"] == eol
    assert info["bom"] is bom

    first = write_text_file(
        str(f), "x = 1\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert first["ok"] is True
    # 返回值必须等于磁盘现状的 hash（含 BOM 的原始字节口径）
    assert read_text_file(str(f))["hash"] == first["hash"]

    # 闭环：用返回的 hash 再保存一次，必须成功且不报冲突
    second = write_text_file(
        str(f), "x = 2\n", first["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert second["ok"] is True
    assert "conflict" not in second
    assert second["hash"] == read_text_file(str(f))["hash"]


# ---------------------------------------------------------------------------
# 编码失败与写入失败：必须「磁盘内容一字不变且不留临时文件」
# ---------------------------------------------------------------------------
def test_write_gbk_unrepresentable_raises_and_keeps_disk(tmp_path):
    f = tmp_path / "gbk.txt"
    original = "中文\n".encode("gbk")
    f.write_bytes(original)
    info = read_text_file(str(f))

    # 「🚀」（U+1F680）在 GBK 里没有对应码位
    with pytest.raises(FileServiceError) as exc:
        write_text_file(
            str(f), "中文🚀\n", info["hash"],
            encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
        )
    assert "gbk" in str(exc.value).lower()

    # 编码失败发生在落盘之前：磁盘一字不变，也不留下临时文件
    assert f.read_bytes() == original
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".ycode-ide-")]
    assert leftovers == []


def test_write_expected_hash_on_deleted_file_is_conflict(tmp_path):
    """目标文件已不存在时，带 expected_hash 的保存必须走冲突分支。

    若把「读不到文件」当成「无冲突」直接覆盖，用户会静默地重新创建一个已被
    删除（或被改名）的文件，这是数据事故。
    """
    f = tmp_path / "gone.py"
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))
    os.unlink(str(f))

    res = write_text_file(
        str(f), "x = 2\n", info["hash"],
        encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
    )
    assert res["ok"] is False
    # 哨兵语义：disk_hash 为空串表示目标文件已不存在
    assert res["conflict"]["disk_hash"] == ""
    assert "磁盘" in res["msg"]
    # 绝不能把文件重新创建出来
    assert not f.exists()


def test_write_failure_cleans_temp_and_keeps_original(tmp_path, monkeypatch):
    """os.replace 失败时不得残留 .ycode-ide-* 临时文件，也不得破坏原文件。"""
    f = tmp_path / "a.py"
    f.write_bytes(b"x = 1\n")
    info = read_text_file(str(f))

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_text_file(
            str(f), "x = 2\n", info["hash"],
            encoding=info["encoding"], bom=info["bom"], eol=info["eol"],
        )

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".ycode-ide-")]
    assert leftovers == []
    assert f.read_bytes() == b"x = 1\n"


# ---------------------------------------------------------------------------
# 外部改动轮询：只盯已打开文件，用显式 utime 保证结果确定
# ---------------------------------------------------------------------------
from core.ide.file_service import OpenFileWatcher


def test_watcher_reports_modified_file_once(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    base = os.stat(str(f)).st_mtime
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(f)])

    os.utime(str(f), (base + 10, base + 10))
    assert watcher.poll() == [str(f)]
    # 第二次轮询不应重复上报同一个变更
    assert watcher.poll() == []


def test_watcher_reports_deleted_file_once(tmp_path):
    f = tmp_path / "gone.py"
    f.write_text("x = 1\n", encoding="utf-8")
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(f)])
    os.unlink(str(f))
    assert watcher.poll() == [str(f)]
    assert watcher.poll() == []


def test_watcher_ignores_unopened_files(tmp_path):
    opened = tmp_path / "open.py"
    opened.write_text("x = 1\n", encoding="utf-8")
    other = tmp_path / "other.py"
    other.write_text("y = 1\n", encoding="utf-8")
    base = os.stat(str(other)).st_mtime
    watcher = OpenFileWatcher()
    watcher.set_open_files([str(opened)])

    os.utime(str(other), (base + 10, base + 10))
    assert watcher.poll() == []


def test_watcher_set_open_files_resets_baseline(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    watcher = OpenFileWatcher()
    base = os.stat(str(f)).st_mtime
    os.utime(str(f), (base + 10, base + 10))
    # 重新登记时才建立基线，不会把登记前的改动当作变更上报
    watcher.set_open_files([str(f)])
    assert watcher.poll() == []
