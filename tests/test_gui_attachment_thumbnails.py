import pytest

import gui_bridge
from core.media_store import MediaStoreError
from gui_bridge import ApiBridge


@pytest.fixture
def bridge():
    return ApiBridge.__new__(ApiBridge)


def test_mixed_list(bridge, monkeypatch):
    png_path = "C:/tmp/pic.png"
    txt_path = "C:/tmp/note.txt"
    fake_item = {"digest": "a" * 64, "ext": "png", "width": 10, "height": 10}

    def fake_archive(path):
        if str(path).endswith(".png"):
            return dict(fake_item)
        raise MediaStoreError("unsupported_type", "不支持的媒体格式")

    monkeypatch.setattr(gui_bridge, "archive_media_file", fake_archive)
    monkeypatch.setattr(
        gui_bridge.media_server,
        "public_url",
        lambda digest, ext: "http://127.0.0.1:1/m/x?t=k",
    )

    res = bridge.get_attachment_thumbnails([png_path, txt_path])
    assert res["ok"] is True
    assert res["items"] == [{
        "path": png_path,
        "url": "http://127.0.0.1:1/m/x?t=k",
        "width": 10,
        "height": 10,
    }]


def test_empty(bridge, monkeypatch):
    monkeypatch.setattr(
        gui_bridge,
        "archive_media_file",
        lambda path: pytest.fail("空列表不应触发归档"),
    )
    res = bridge.get_attachment_thumbnails([])
    assert res == {"ok": True, "items": []}


def test_all_non_string(bridge):
    res = bridge.get_attachment_thumbnails([None, 123, {"a": 1}])
    assert res == {"ok": True, "items": []}


def test_archive_error_no_raise(bridge, monkeypatch):

    def boom(path):
        raise MediaStoreError("read_failed", "读取媒体文件失败")

    monkeypatch.setattr(gui_bridge, "archive_media_file", boom)
    monkeypatch.setattr(
        gui_bridge.media_server,
        "public_url",
        lambda digest, ext: "http://127.0.0.1:1/m/x?t=k",
    )

    res = bridge.get_attachment_thumbnails(["C:/tmp/gone.png", "C:/tmp/gone.jpg"])
    assert res == {"ok": True, "items": []}


def test_empty_url_skips_item(bridge, monkeypatch):
    fake_item = {"digest": "b" * 64, "ext": "png", "width": None, "height": None}
    monkeypatch.setattr(gui_bridge, "archive_media_file", lambda path: fake_item)
    monkeypatch.setattr(gui_bridge.media_server, "public_url", lambda digest, ext: "")

    res = bridge.get_attachment_thumbnails(["C:/tmp/pic.png"])
    assert res == {"ok": True, "items": []}
