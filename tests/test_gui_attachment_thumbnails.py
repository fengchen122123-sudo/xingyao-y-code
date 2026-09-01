"""tests/test_gui_attachment_thumbnails.py —— 桥接层附件缩略图方法测试。

缩略图是增强能力：归档/服务失败只影响单项，整体必须 ok=True 且不抛异常。
全部通过 monkeypatch 替换 gui_bridge 所用的 archive_media_file 与
media_server.public_url，不依赖真实 Pillow 归档与 HTTP 服务。
"""

import pytest

import gui_bridge
from core.media_store import MediaStoreError
from gui_bridge import ApiBridge


@pytest.fixture
def bridge():
    """不经 __init__ 构造裸实例；get_attachment_thumbnails 不依赖桥接状态。"""
    return ApiBridge.__new__(ApiBridge)


def test_mixed_list(bridge, monkeypatch):
    """png 项归档成功返回带 url 项；txt 项抛 MediaStoreError 被静默跳过。"""
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
    """空列表直接返回空 items；不应触达归档与 URL 生成。"""
    monkeypatch.setattr(
        gui_bridge,
        "archive_media_file",
        lambda path: pytest.fail("空列表不应触发归档"),
    )
    res = bridge.get_attachment_thumbnails([])
    assert res == {"ok": True, "items": []}


def test_all_non_string(bridge):
    """全非字符串输入同样返回空 items，不抛异常。"""
    res = bridge.get_attachment_thumbnails([None, 123, {"a": 1}])
    assert res == {"ok": True, "items": []}


def test_archive_error_no_raise(bridge, monkeypatch):
    """所有路径归档都抛错 → ok=True 且 items 为空，异常绝不冒泡。"""

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
    """media 服务不可用（public_url 返回空串）→ 该项被跳过而非报错。"""
    fake_item = {"digest": "b" * 64, "ext": "png", "width": None, "height": None}
    monkeypatch.setattr(gui_bridge, "archive_media_file", lambda path: fake_item)
    monkeypatch.setattr(gui_bridge.media_server, "public_url", lambda digest, ext: "")

    res = bridge.get_attachment_thumbnails(["C:/tmp/pic.png"])
    assert res == {"ok": True, "items": []}
