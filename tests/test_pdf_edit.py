# -*- coding: utf-8 -*-
"""core/pdf_edit.apply_edits 与 gui_bridge.save_pdf_edits 的测试。

坐标口径：PDF point，原点在页面左上角，y 向下，page 为 1-based（与前端 pdf.js
getViewport({scale:1}) 一致）。测试用的源 PDF 由 reportlab 现造，带中文文字，
便于断言「原文保留 + 叠加层可见」。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

import core.pdf_edit as pdf_edit

PAGE_W, PAGE_H = 595.28, 841.89
FIXTURE_FONT = "PdfEditTestCJK"
_FONT_CANDIDATES = (
    (r"C:\Windows\Fonts\msyh.ttc", 0),
    (r"C:\Windows\Fonts\msyhbd.ttc", 0),
    (r"C:\Windows\Fonts\simhei.ttf", None),
    (r"C:\Windows\Fonts\simsun.ttc", 0),
)


def _fixture_font() -> str:
    """注册测试用中文字体；系统没有任何中文字体时跳过（否则断言中文提取无意义）。"""
    for path, subfont_index in _FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            if subfont_index is None:
                pdfmetrics.registerFont(TTFont(FIXTURE_FONT, path))
            else:
                pdfmetrics.registerFont(TTFont(FIXTURE_FONT, path, subfontIndex=subfont_index))
            return FIXTURE_FONT
        except Exception:
            continue
    pytest.skip("系统中没有可用的中文字体，无法构造中文 fixture")


def _make_source_pdf(path, texts=("第一页 正文内容", "第二页 正文内容", "第三页 正文内容")):
    """造一份 pages=len(texts) 的源 PDF，每页写一行中文。"""
    font = _fixture_font()
    pdf = canvas.Canvas(str(path), pagesize=(PAGE_W, PAGE_H))
    for text in texts:
        pdf.setFont(font, 16)
        pdf.drawString(72, 760, text)
        pdf.showPage()
    pdf.save()
    return str(path)


@pytest.fixture()
def source_pdf(tmp_path):
    return _make_source_pdf(tmp_path / "source.pdf")


# ---------------------------------------------------------------------------
# 用例 1：页面级操作（删除 / 旋转 / 插空白页）
# ---------------------------------------------------------------------------


def test_page_ops_delete_rotate_and_insert_blank(source_pdf, tmp_path):
    out_path = str(tmp_path / "out.pdf")
    ops = [
        {"kind": "page", "op": "delete", "page": 2},
        {"kind": "page", "op": "rotate", "page": 1, "degrees": 90},
        {"kind": "page", "op": "insertBlank", "after": 1, "width": PAGE_W, "height": PAGE_H},
    ]

    result = pdf_edit.apply_edits(source_pdf, ops, out_path)

    assert result["pages"] == 3
    assert os.path.isfile(out_path)
    reader = PdfReader(out_path)
    assert len(reader.pages) == 3
    # 页 1：原第 1 页被旋转 90°（/Rotate 写在页面字典上）
    assert int(reader.pages[0].get("/Rotate", 0)) == 90
    # 页 2：新插入的空白页（插在原第 1 页之后）
    assert reader.pages[1].extract_text().strip() == ""
    # 页 3：原来的第 3 页，第 2 页已被删除
    assert "第三页" in reader.pages[2].extract_text()
    assert "第二页" not in "".join(page.extract_text() for page in reader.pages)


def test_move_page_reorders(source_pdf, tmp_path):
    out_path = str(tmp_path / "moved.pdf")
    ops = [{"kind": "page", "op": "move", "page": 3, "to": 1}]

    pdf_edit.apply_edits(source_pdf, ops, out_path)

    reader = PdfReader(out_path)
    assert len(reader.pages) == 3
    assert "第三页" in reader.pages[0].extract_text()
    assert "第一页" in reader.pages[1].extract_text()


# ---------------------------------------------------------------------------
# 用例 2：批注 / 遮盖 / 文本框叠加，不改页面尺寸与原文
# ---------------------------------------------------------------------------


def test_annotations_preserve_page_and_add_visible_text(source_pdf, tmp_path):
    out_path = str(tmp_path / "annotated.pdf")
    src_reader = PdfReader(source_pdf)
    src_mediabox = [float(v) for v in src_reader.pages[0].mediabox]
    ops = [
        {
            "kind": "annot", "type": "highlight", "page": 1,
            "rects": [{"x": 70, "y": 100, "w": 200, "h": 14}],
            "color": "#ffd400", "opacity": 0.35,
        },
        {"kind": "annot", "type": "rect", "page": 1, "rect": {"x": 60, "y": 300, "w": 120, "h": 60},
         "color": "#e5484d", "opacity": 1},
        {
            "kind": "annot", "type": "ink", "page": 1,
            "points": [{"x": 70, "y": 400}, {"x": 200, "y": 450}, {"x": 260, "y": 400}],
            "color": "#e5484d", "width": 1.5,
        },
        {"kind": "redact", "page": 1, "rect": {"x": 70, "y": 500, "w": 100, "h": 20}, "fill": "#ffffff"},
        {
            "kind": "text", "page": 1, "rect": {"x": 70, "y": 600, "w": 300, "h": 60},
            "text": "改写后的文字测试", "fontSize": 12, "color": "#111111", "bg": "#ffffff",
        },
    ]

    result = pdf_edit.apply_edits(source_pdf, ops, out_path)

    assert result["pages"] == 3
    assert os.path.isfile(out_path)
    reader = PdfReader(out_path)
    assert len(reader.pages) == 3  # 批注不增删页
    assert [float(v) for v in reader.pages[0].mediabox] == src_mediabox
    text = reader.pages[0].extract_text()
    assert "改写后的文字测试" in text  # 文本框内容可提取（中文未丢）
    assert "第一页" in text  # 原文内容仍保留


def test_text_block_wrapping_warns_when_too_many_lines(source_pdf, tmp_path):
    out_path = str(tmp_path / "wrapped.pdf")
    ops = [
        {
            "kind": "text", "page": 1, "rect": {"x": 70, "y": 300, "w": 80, "h": 30},
            "text": "这是一段很长很长的中文内容需要换行并且一定会超出框体高度" * 3,
            "fontSize": 12, "color": "#111111", "bg": None,
        },
    ]

    result = pdf_edit.apply_edits(source_pdf, ops, out_path)

    assert os.path.isfile(out_path)
    assert any("丢弃" in item for item in result["warnings"])
    assert "warning" in result


# ---------------------------------------------------------------------------
# 用例 3：非法输入一律 ValueError
# ---------------------------------------------------------------------------


def test_page_out_of_range_raises(source_pdf, tmp_path):
    ops = [{"kind": "annot", "type": "rect", "page": 5, "rect": {"x": 10, "y": 10, "w": 10, "h": 10},
            "color": "#e5484d"}]
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, ops, str(tmp_path / "bad.pdf"))


def test_invalid_color_raises(source_pdf, tmp_path):
    ops = [{"kind": "annot", "type": "rect", "page": 1, "rect": {"x": 10, "y": 10, "w": 10, "h": 10},
            "color": "red"}]
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, ops, str(tmp_path / "bad.pdf"))


def test_too_many_ops_raises(source_pdf, tmp_path):
    ops = [{"kind": "page", "op": "rotate", "page": 1, "degrees": 90}] * (pdf_edit.MAX_OPS + 1)
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, ops, str(tmp_path / "bad.pdf"))


def test_invalid_rect_size_raises(source_pdf, tmp_path):
    ops = [{"kind": "redact", "page": 1, "rect": {"x": 10, "y": 10, "w": 0, "h": 10}}]
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, ops, str(tmp_path / "bad.pdf"))


def test_invalid_rotate_degrees_raises(source_pdf, tmp_path):
    ops = [{"kind": "page", "op": "rotate", "page": 1, "degrees": 45}]
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, ops, str(tmp_path / "bad.pdf"))


# ---------------------------------------------------------------------------
# 用例 4：桥方法 save_pdf_edits（copy / overwrite 两条保存路径）
# ---------------------------------------------------------------------------


def _make_bridge(workspace):
    """参照 tests/test_read_file_preview.py 的桥构造写法（不启动 MCP / GUI）。"""
    from gui_bridge import ApiBridge

    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": str(workspace)  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    return bridge


_COPY_OPS = [
    {"kind": "page", "op": "delete", "page": 2},
    {"kind": "annot", "type": "rect", "page": 1, "rect": {"x": 60, "y": 300, "w": 120, "h": 60},
     "color": "#e5484d", "opacity": 1},
]


def test_bridge_copy_mode_writes_new_file_without_touching_source(tmp_path, monkeypatch):
    import core.gui_path_policy as gpp

    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    source = _make_source_pdf(tmp_path / "doc.pdf")
    original = open(source, "rb").read()
    bridge = _make_bridge(tmp_path)

    result = bridge.save_pdf_edits(source, _COPY_OPS, mode="copy")

    assert result["ok"] is True, result
    assert result["pages"] == 2
    assert result["backup"] == ""
    assert os.path.abspath(result["path"]) != os.path.abspath(source)
    assert os.path.isfile(result["path"])
    assert "已编辑" in os.path.basename(result["path"])
    assert open(source, "rb").read() == original  # 源文件一个字节都没动
    assert len(PdfReader(result["path"]).pages) == 2


def test_bridge_overwrite_mode_backs_up_and_updates_source(tmp_path, monkeypatch):
    import core.gui_path_policy as gpp

    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    source = _make_source_pdf(tmp_path / "doc2.pdf")
    original = open(source, "rb").read()
    bridge = _make_bridge(tmp_path)

    result = bridge.save_pdf_edits(source, _COPY_OPS, mode="overwrite")

    assert result["ok"] is True, result
    assert os.path.abspath(result["path"]) == os.path.abspath(source)
    backup = result["backup"]
    assert backup and os.path.isfile(backup)
    assert ".bak_" in os.path.basename(backup)
    assert open(backup, "rb").read() == original  # 备份就是原文件
    assert open(source, "rb").read() != original  # 源文件已被更新
    assert len(PdfReader(source).pages) == 2


def test_bridge_reports_invalid_ops_as_failure(tmp_path, monkeypatch):
    import core.gui_path_policy as gpp

    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    source = _make_source_pdf(tmp_path / "doc3.pdf")
    bridge = _make_bridge(tmp_path)

    result = bridge.save_pdf_edits(source, [{"kind": "annot", "type": "rect", "page": 99,
                                             "rect": {"x": 1, "y": 1, "w": 5, "h": 5}}], mode="copy")

    assert result["ok"] is False
    assert isinstance(result["msg"], str) and result["msg"]


def test_bridge_denies_path_outside_workspace_with_sandbox_off(tmp_path, monkeypatch):
    import core.gui_path_policy as gpp

    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    source = _make_source_pdf(elsewhere / "outside.pdf")
    bridge = _make_bridge(workspace)

    result = bridge.save_pdf_edits(source, [], mode="copy")

    assert result["ok"] is False
    assert "工作区" in result["msg"]

def test_rect_stroke_width_takes_effect(source_pdf, tmp_path):
    """前端一直下发 width（默认 1.5），后端必须真的用上，不能静默忽略。"""
    out_default = tmp_path / "default.pdf"
    out_wide = tmp_path / "wide.pdf"
    base = {
        "kind": "annot", "type": "rect", "page": 1,
        "rect": {"x": 60, "y": 300, "w": 120, "h": 60}, "color": "#e5484d",
    }

    pdf_edit.apply_edits(source_pdf, [base], str(out_default))
    pdf_edit.apply_edits(source_pdf, [{**base, "width": 6}], str(out_wide))

    default_stream = PdfReader(str(out_default)).pages[0].get_contents().get_data()
    wide_stream = PdfReader(str(out_wide)).pages[0].get_contents().get_data()
    assert b"6 w" in wide_stream
    assert b"6 w" not in default_stream
    assert default_stream != wide_stream


def test_invalid_stroke_width_raises(source_pdf, tmp_path):
    bad = {
        "kind": "annot", "type": "rect", "page": 1,
        "rect": {"x": 60, "y": 300, "w": 120, "h": 60}, "color": "#e5484d",
        "width": 999,
    }
    with pytest.raises(ValueError):
        pdf_edit.apply_edits(source_pdf, [bad], str(tmp_path / "bad.pdf"))


def test_bridge_copy_mode_denies_out_path_outside_workspace(tmp_path, monkeypatch):
    """copy 模式下，输出路径同样要过路径策略（桥层可能被非常规调用）。"""
    import core.gui_path_policy as gpp

    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    source = _make_source_pdf(workspace / "doc.pdf")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "escaped.pdf"
    bridge = _make_bridge(workspace)

    result = bridge.save_pdf_edits(source, _COPY_OPS, mode="copy", out_path=str(target))

    assert result["ok"] is False, result
    assert "输出路径不允许" in result["msg"]
    assert not target.exists()
