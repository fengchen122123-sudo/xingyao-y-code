# -*- coding: utf-8 -*-
"""read_file_preview 对目录 / 不存在路径的错误信息测试。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gui_bridge import ApiBridge


def _make_bridge_with_workdir(workdir: str) -> ApiBridge:
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    return bridge


def test_preview_directory_returns_folder_message():
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "subdir")
        os.makedirs(sub)
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.read_file_preview(sub)
        assert res["ok"] is False
        # 目录要有专属、准确的提示，而不是误导性的「文件不存在」
        assert "文件夹" in res["msg"] or "目录" in res["msg"]


def test_preview_missing_file_keeps_missing_message():
    with tempfile.TemporaryDirectory() as tmp:
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.read_file_preview(os.path.join(tmp, "no_such.md"))
        assert res["ok"] is False


def test_preview_trailing_slash_directory():
    with tempfile.TemporaryDirectory() as tmp:
        sub = os.path.join(tmp, "dist")
        os.makedirs(sub)
        bridge = _make_bridge_with_workdir(tmp)
        # 模型常输出带尾反斜杠的目录路径：E:\xxx\dist\
        res = bridge.read_file_preview(sub.rstrip("\\/") + "\\")
        assert res["ok"] is False
        assert "文件夹" in res["msg"] or "目录" in res["msg"]


def test_check_paths_kind_classifies_file_dir_missing():
    with tempfile.TemporaryDirectory() as tmp:
        f = os.path.join(tmp, "a.md")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x")
        d = os.path.join(tmp, "sub")
        os.makedirs(d)
        bridge = _make_bridge_with_workdir(tmp)
        res = bridge.check_paths_kind([f, d, os.path.join(tmp, "nope.md")])
        assert [r["kind"] for r in res] == ["file", "dir", "missing"]


# ---------------------------------------------------------------------------
# 预览工作区限制放宽：GLOBAL 放行 + 沙盒开关跟随 + 凭据文件永久拒绝
# 通过 core.gui_path_policy 的注入口控制状态，绝不触碰用户真实 ~/.ycode
# ---------------------------------------------------------------------------
import core.gui_path_policy as gpp


def _inject_ycode_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))


def test_preview_global_session_allows_file(tmp_path, monkeypatch):
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    f = tmp_path / "hello.txt"
    f.write_text("hello", encoding="utf-8")
    bridge = _make_bridge_with_workdir("GLOBAL")
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert "hello" in res["content"]


def test_preview_outside_workspace_allowed_when_sandbox_on(tmp_path, monkeypatch):
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    f = elsewhere / "note.txt"
    f.write_text("outside", encoding="utf-8")
    bridge = _make_bridge_with_workdir(str(workspace))
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True


def test_preview_outside_workspace_denied_when_sandbox_off(tmp_path, monkeypatch):
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    f = elsewhere / "note.txt"
    f.write_text("outside", encoding="utf-8")
    bridge = _make_bridge_with_workdir(str(workspace))
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "工作区" in res["msg"]


def test_preview_credential_config_always_denied(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    bridge = _make_bridge_with_workdir("GLOBAL")
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False


# ---------------------------------------------------------------------------
# 图片文件应用内预览：按扩展名走 base64 data URL 分支
# ---------------------------------------------------------------------------

# 最小 PNG 头（后端不校验图片内容，只要二进制字节即可）
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _make_global_bridge(tmp_path, monkeypatch):
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    return _make_bridge_with_workdir("GLOBAL")


def test_preview_png_returns_image_data_url(tmp_path, monkeypatch):
    f = tmp_path / "pic.png"
    f.write_bytes(_PNG_BYTES)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["kind"] == "image"
    assert res["data_url"].startswith("data:image/png;base64,")
    # data URL 可逆解码回原始字节
    import base64
    decoded = base64.b64decode(res["data_url"].split(",", 1)[1])
    assert decoded == _PNG_BYTES


def test_preview_jpg_uses_jpeg_mime(tmp_path, monkeypatch):
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["data_url"].startswith("data:image/jpeg;base64,")


def test_preview_svg_rendered_as_image(tmp_path, monkeypatch):
    f = tmp_path / "icon.svg"
    f.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["kind"] == "image"
    assert res["data_url"].startswith("data:image/svg+xml;base64,")


def test_preview_oversized_image_rejected(tmp_path, monkeypatch):
    import gui_bridge as gb
    f = tmp_path / "big.png"
    f.write_bytes(_PNG_BYTES)
    # 把图片上限压到 1 字节，避免真的写 20MB 文件
    monkeypatch.setattr(gb, "IMAGE_PREVIEW_MAX_BYTES", 1)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "过大" in res["msg"]


def test_preview_non_image_binary_still_rejected(tmp_path, monkeypatch):
    f = tmp_path / "app.exe"
    f.write_bytes(b"MZ\x90\x00" + b"\x00" * 16)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "系统中打开" in res["msg"]


# ---------------------------------------------------------------------------
# PDF 应用内预览：右栏「文档」页签由前端 pdf.js 渲染，字节按 base64 data URL 过桥
# ---------------------------------------------------------------------------

_PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


def test_preview_pdf_returns_pdf_data_url(tmp_path, monkeypatch):
    f = tmp_path / "plan.pdf"
    f.write_bytes(_PDF_BYTES)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["kind"] == "pdf"
    assert res["data_url"].startswith("data:application/pdf;base64,")
    assert res["size"] == len(_PDF_BYTES)
    import base64
    assert base64.b64decode(res["data_url"].split(",", 1)[1]) == _PDF_BYTES


def test_preview_oversized_pdf_rejected(tmp_path, monkeypatch):
    import gui_bridge as gb
    f = tmp_path / "huge.pdf"
    f.write_bytes(_PDF_BYTES)
    monkeypatch.setattr(gb, "PDF_PREVIEW_MAX_BYTES", 1)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "过大" in res["msg"]


# ---------------------------------------------------------------------------
# 预览产物豁免：Office 转出的 PDF / Markdown 预览页落在数据目录 exports/preview，
# 不在会话工作区里。沙盒未解除时若不豁免，转换完的 PDF 会被自己的路径策略拦下
# （用户白等 20 秒），所以只对这一层目录开口子，数据目录其它位置照旧拒绝。
# ---------------------------------------------------------------------------


def test_office_cached_pdf_allowed_when_sandbox_off(tmp_path, monkeypatch):
    from core import web_preview
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    preview_dir = tmp_path / "ydata" / "exports" / "preview"
    preview_dir.mkdir(parents=True)
    monkeypatch.setattr(web_preview, "get_preview_dir", lambda: preview_dir)
    cached = preview_dir / "office_abc123.pdf"
    cached.write_bytes(_PDF_BYTES)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    bridge = _make_bridge_with_workdir(str(workspace))
    res = bridge.read_file_preview(str(cached))

    assert res["ok"] is True
    assert res["kind"] == "pdf"


def test_pdf_outside_preview_dir_still_denied_when_sandbox_off(tmp_path, monkeypatch):
    from core import web_preview
    _inject_ycode_data_dir(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    preview_dir = tmp_path / "ydata" / "exports" / "preview"
    preview_dir.mkdir(parents=True)
    monkeypatch.setattr(web_preview, "get_preview_dir", lambda: preview_dir)
    # 数据目录根部的文件不在豁免范围内（只有预览产物那一层放行）
    outside = tmp_path / "ydata" / "loose.pdf"
    outside.write_bytes(_PDF_BYTES)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    bridge = _make_bridge_with_workdir(str(workspace))
    res = bridge.read_file_preview(str(outside))

    assert res["ok"] is False
    assert "工作区" in res["msg"]
# ---------------------------------------------------------------------------
# docx 应用内预览：不再由 Word COM 转 PDF，直接把 OOXML 字节按 base64 交前端
# docx-preview 渲染，所以这里回 kind=docx（不是 pdf），字节要能原样还原。
# ---------------------------------------------------------------------------

_DOCX_BYTES = b"PK\x03\x04" + b"\x00" * 24


def test_preview_docx_returns_docx_data_url(tmp_path, monkeypatch):
    f = tmp_path / "report.docx"
    f.write_bytes(_DOCX_BYTES)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["kind"] == "docx"
    assert res["data_url"].startswith(
        "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,"
    )
    assert res["size"] == len(_DOCX_BYTES)
    import base64
    assert base64.b64decode(res["data_url"].split(",", 1)[1]) == _DOCX_BYTES


def test_preview_oversized_docx_rejected(tmp_path, monkeypatch):
    import gui_bridge as gb
    f = tmp_path / "huge.docx"
    f.write_bytes(_DOCX_BYTES)
    monkeypatch.setattr(gb, "DOCX_PREVIEW_MAX_BYTES", 1)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "过大" in res["msg"]

def test_preview_docx_extension_but_pdf_content_falls_back_to_pdf(tmp_path, monkeypatch):
    # 扩展名不可信：把 PDF 改名成 .docx 时，不能让 JSZip 去解 zip 而报英文错
    f = tmp_path / "renamed.docx"
    f.write_bytes(_PDF_BYTES)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is True
    assert res["kind"] == "pdf"
    assert res["data_url"].startswith("data:application/pdf;base64,")


def test_preview_broken_docx_reports_chinese_message(tmp_path, monkeypatch):
    # 「PDF 转 Word」失败留下的空壳：内容是随机字节，既非 ZIP 也非 PDF
    f = tmp_path / "broken.docx"
    f.write_bytes(bytes(range(256)) * 4)
    sibling = tmp_path / "broken.pdf"
    sibling.write_bytes(_PDF_BYTES)
    bridge = _make_global_bridge(tmp_path, monkeypatch)
    res = bridge.read_file_preview(str(f))
    assert res["ok"] is False
    assert "不是有效的 Word 文档" in res["msg"]
    # 同目录有源 PDF 时应把它指出来，省得用户自己找
    assert "broken.pdf" in res["msg"]
