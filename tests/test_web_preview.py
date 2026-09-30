"""core.web_preview 的纯函数测试：预览页落盘、文件名稳定性与体积上限。"""

import pytest

from core import web_preview as wp


@pytest.fixture()
def preview_dir(tmp_path, monkeypatch):
    target = tmp_path / "preview"
    monkeypatch.setattr(wp, "get_preview_dir", lambda: target)
    return target


def test_write_preview_page_creates_file(preview_dir):
    path = wp.write_preview_page("<html>hi</html>", r"D:\proj\a.md")

    assert path.is_file()
    assert path.parent == preview_dir
    assert path.read_text(encoding="utf-8") == "<html>hi</html>"


def test_same_source_reuses_same_file(preview_dir):
    first = wp.write_preview_page("<html>v1</html>", r"D:\proj\a.md")
    second = wp.write_preview_page("<html>v2</html>", r"D:\proj\a.md")

    # 同一源文件重复点击只覆盖同一份预览页，不在磁盘上堆垃圾
    assert first == second
    assert first.read_text(encoding="utf-8") == "<html>v2</html>"


def test_different_sources_get_different_files(preview_dir):
    one = wp.write_preview_page("<html>1</html>", r"D:\proj\a.md")
    two = wp.write_preview_page("<html>2</html>", r"D:\proj\b.md")

    assert one != two
    assert len(list(preview_dir.glob("preview_*.html"))) == 2


def test_source_path_case_and_space_insensitive(preview_dir):
    # 大小写与首尾空白不影响命名：Windows 路径大小写不敏感，避免生成两份
    one = wp.write_preview_page("<html>1</html>", r"  D:\Proj\A.md  ")
    two = wp.write_preview_page("<html>2</html>", r"d:\proj\a.md")

    assert one == two


def test_rejects_empty_and_non_string(preview_dir):
    with pytest.raises(ValueError):
        wp.write_preview_page("")
    with pytest.raises(ValueError):
        wp.write_preview_page("   ")
    with pytest.raises(ValueError):
        wp.write_preview_page(None)  # type: ignore[arg-type]


def test_rejects_oversize_payload(preview_dir, monkeypatch):
    monkeypatch.setattr(wp, "MAX_PREVIEW_BYTES", 8)

    with pytest.raises(ValueError):
        wp.write_preview_page("<html>0123456789</html>", r"D:\proj\a.md")
