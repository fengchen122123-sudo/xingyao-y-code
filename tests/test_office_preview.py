# -*- coding: utf-8 -*-
"""core.office_preview 的单元测试：全部 mock，不依赖本机真实 Office。

隔离策略：与 tests/test_web_preview.py 一致，monkeypatch
``core.office_preview.get_preview_dir`` 指向 tmp_path，绝不碰用户真实
~/.ycode；子进程一律用 ``subprocess.run`` 的假实现替换，断言的是"父进程如何
调用子进程、如何解释结果"，COM 行为由真实冒烟验证覆盖。
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from core import office_preview as op


@pytest.fixture()
def preview_dir(tmp_path, monkeypatch):
    """把缓存目录换到临时目录（不触碰 ~/.ycode）。"""
    target = tmp_path / "preview"
    monkeypatch.setattr(op, "get_preview_dir", lambda: target)
    return target


@pytest.fixture()
def source_file(tmp_path):
    """一个存在的 docx 源文件，mtime 压到过去，便于构造"缓存新鲜"。"""
    path = tmp_path / "报告.docx"
    path.write_bytes(b"fake-docx")
    old = 1_600_000_000.0
    os.utime(path, (old, old))
    return path


@pytest.fixture()
def no_subprocess(monkeypatch):
    """subprocess.run 一旦被调用就失败——用来验证"缓存命中不启动子进程"。"""

    def _fail(*args, **kwargs):  # pragma: no cover - 命中即说明代码走错分支
        raise AssertionError("缓存命中时不应启动转换子进程")

    monkeypatch.setattr(op.subprocess, "run", _fail)
    return _fail


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["python"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------- 扩展名判定


@pytest.mark.parametrize(
    "path",
    [
        "a.doc",
        "a.docx",
        "a.rtf",
        "a.odt",
        "a.xls",
        "a.xlsx",
        "a.ods",
        "a.ppt",
        "a.pptx",
        "a.odp",
        # 大小写、带目录的绝对路径、首尾空白都应判定为 Office 文档
        r"C:\proj\sub\A.DOCX",
        r"D:/proj/B.XLSX",
        "  C:\\proj\\c.PPTX  ",
    ],
)
def test_is_office_path_accepts_office_extensions(path):
    assert op.is_office_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "a.pdf",
        "a.txt",
        "a.md",
        "a.docx.bak",
        "docx",
        "",
        None,
        r"C:\proj\noext",
    ],
)
def test_is_office_path_rejects_others(path):
    assert op.is_office_path(path) is False


def test_office_extensions_mapping():
    # 调用方（gui_bridge）会按这张表决定"能不能点预览"，键必须是含点小写扩展名
    assert op.OFFICE_EXTENSIONS[".docx"] == "word"
    assert op.OFFICE_EXTENSIONS[".rtf"] == "word"
    assert op.OFFICE_EXTENSIONS[".xlsx"] == "excel"
    assert op.OFFICE_EXTENSIONS[".ods"] == "excel"
    assert op.OFFICE_EXTENSIONS[".pptx"] == "powerpoint"
    assert op.OFFICE_EXTENSIONS[".odp"] == "powerpoint"
    for suffix in op.OFFICE_EXTENSIONS:
        assert suffix == suffix.lower() and suffix.startswith(".")
    assert set(op.OFFICE_EXTENSIONS.values()) == {"word", "excel", "powerpoint"}


# ---------------------------------------------------------------- 缓存路径


def test_cached_pdf_path_is_stable_and_in_preview_dir(preview_dir, source_file):
    first = op.cached_pdf_path(str(source_file))
    second = op.cached_pdf_path(str(source_file))

    assert first == second
    assert first.parent == preview_dir
    # 命名约定：office_<sha1 前 12 位>.pdf，调用方与清理脚本都按这个模式找文件
    assert re.fullmatch(r"office_[0-9a-f]{12}\.pdf", first.name)


def test_cached_pdf_path_differs_for_different_sources(preview_dir, tmp_path):
    one = op.cached_pdf_path(str(tmp_path / "a.docx"))
    two = op.cached_pdf_path(str(tmp_path / "b.docx"))

    assert one != two


def test_cached_pdf_path_case_insensitive(preview_dir, tmp_path):
    # Windows 路径大小写不敏感：同一文件不同大小写必须落到同一份缓存
    lower = op.cached_pdf_path(str(tmp_path / "Case" / "A.docx").lower())
    upper = op.cached_pdf_path(str(tmp_path / "Case" / "A.docx").upper())

    assert lower == upper


def test_cached_pdf_path_ignores_surrounding_whitespace(preview_dir, tmp_path):
    plain = op.cached_pdf_path(str(tmp_path / "a.docx"))

    assert op.cached_pdf_path(f"  {tmp_path / 'a.docx'}  ") == plain


# ---------------------------------------------------------------- 缓存命中


def test_convert_returns_cached_pdf_without_subprocess(preview_dir, source_file, no_subprocess):
    cached = op.cached_pdf_path(str(source_file))
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"%PDF-1.4 cached")

    result = op.convert_to_pdf(str(source_file))

    assert result == cached
    assert result.read_bytes() == b"%PDF-1.4 cached"


def test_stale_cache_triggers_conversion(preview_dir, source_file, monkeypatch):
    cached = op.cached_pdf_path(str(source_file))
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"%PDF-1.4 stale")
    # 让缓存 PDF 比源文件旧 → 必须重新转换
    os.utime(cached, (1_500_000_000.0, 1_500_000_000.0))

    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"%PDF-1.4 fresh")
        return _completed()

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.convert_to_pdf(str(source_file))

    assert len(calls) == 1
    assert result == cached
    assert cached.read_bytes() == b"%PDF-1.4 fresh"


# ---------------------------------------------------------------- 失败分支


def test_convert_missing_source_raises(preview_dir, tmp_path):
    with pytest.raises(op.OfficeConvertError) as excinfo:
        op.convert_to_pdf(str(tmp_path / "nope.docx"))

    assert "源文件不存在" in str(excinfo.value)


def test_convert_empty_path_raises(preview_dir):
    with pytest.raises(op.OfficeConvertError):
        op.convert_to_pdf("")


def test_convert_unsupported_extension_raises(preview_dir, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("hi", encoding="utf-8")

    with pytest.raises(op.OfficeConvertError):
        op.convert_to_pdf(str(target))


def test_convert_nonzero_returncode_raises(preview_dir, source_file, monkeypatch):
    monkeypatch.setattr(
        op.subprocess,
        "run",
        lambda *a, **k: _completed(returncode=1, stderr="Word 转换失败：文档已损坏"),
    )

    with pytest.raises(op.OfficeConvertError) as excinfo:
        op.convert_to_pdf(str(source_file))

    assert "文档已损坏" in str(excinfo.value)


def test_nonzero_returncode_gbk_stderr_decoded(preview_dir, source_file, monkeypatch):
    # 中文 Windows 子进程往管道写 GBK，父进程要先试 UTF-8 再回落本地编码，
    # 否则 GUI 里看到的是乱码错误信息
    monkeypatch.setattr(
        op.subprocess,
        "run",
        lambda *a, **k: _completed(
            returncode=1, stderr="Word 转换失败：文档已损坏".encode("gbk")
        ),
    )

    with pytest.raises(op.OfficeConvertError) as excinfo:
        op.convert_to_pdf(str(source_file))

    assert "文档已损坏" in str(excinfo.value)


def test_convert_timeout_raises(preview_dir, source_file, monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 180))

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    with pytest.raises(op.OfficeConvertError) as excinfo:
        op.convert_to_pdf(str(source_file), timeout=180.0)

    assert "超时" in str(excinfo.value)
    assert "180s" in str(excinfo.value)


def test_convert_success_but_no_pdf_raises(preview_dir, source_file, monkeypatch):
    # 子进程返回 0 却没落盘：不能当成成功（用户会看到空白预览）
    monkeypatch.setattr(op.subprocess, "run", lambda *a, **k: _completed())

    with pytest.raises(op.OfficeConvertError) as excinfo:
        op.convert_to_pdf(str(source_file))

    assert "没有生成 PDF" in str(excinfo.value)


# ---------------------------------------------------------------- 子进程约定


def test_child_command_shape(preview_dir, source_file, monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        Path(command[-1]).write_bytes(b"%PDF-1.4")
        return _completed()

    monkeypatch.setattr(op.subprocess, "run", fake_run)

    result = op.convert_to_pdf(str(source_file), timeout=42.0)

    command = captured["command"]
    assert command[0] == op.sys.executable
    assert command[1:3] == ["-m", "core.office_preview"]
    assert command[3] == "--convert"
    assert command[4] == str(source_file)
    assert command[5] == str(result)
    assert captured["kwargs"]["timeout"] == 42.0
    assert captured["kwargs"]["cwd"] == op._PROJECT_ROOT
    assert result.is_file() and result.stat().st_size > 0


def test_module_has_no_gui_dependencies():
    # 本模块会被子进程与单测直接导入，必须保持轻量（见模块 docstring 取舍 4）
    source = Path(op.__file__).read_text(encoding="utf-8")
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert import_lines
    for line in import_lines:
        assert "webview" not in line
        assert "PIL" not in line
        assert "gui_bridge" not in line
