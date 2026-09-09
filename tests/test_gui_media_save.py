"""tests/test_gui_media_save.py —— 桥接层「图片另存为」测试。

URL → 本地 Blob 的反解由 media_server.local_path_for_url 负责（其单测见
test_media_server.py）。本文件只验证桥接层四条路径：非法 URL 拒绝、用户
取消、复制成功、写盘失败，全部用假窗口替身，不弹真实对话框。
"""

import gui_bridge
from gui_bridge import ApiBridge, _media_save_filename

MEDIA_URL = "http://127.0.0.1:5000/m/" + "a" * 64 + ".png?t=token"


class FakeWindow:
    """create_file_dialog 替身：记录调用参数，返回预设结果。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def make_bridge(window):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._window = window
    return bridge


def patch_resolve(monkeypatch, path):
    monkeypatch.setattr(gui_bridge.media_server, "local_path_for_url", lambda url: path)


# ---- 文件名清理 ----


def test_filename_keeps_chinese_stem_and_real_ext():
    assert _media_save_filename("微信群二维码", ".png") == "微信群二维码.png"


def test_filename_strips_path_and_illegal_chars():
    assert _media_save_filename(r"C:\tmp\a:b*c?.jpg", ".png") == "a_b_c_.png"


def test_filename_falls_back_when_empty():
    assert _media_save_filename("   ", ".jpg") == "ycode-image.jpg"
    assert _media_save_filename("", ".jpg") == "ycode-image.jpg"


# ---- 桥接层 ----


def test_rejects_non_media_url(monkeypatch):
    """解析不出本地文件（远程图片/伪造 URL）时明确失败，不弹对话框。"""
    patch_resolve(monkeypatch, None)
    window = FakeWindow(None)
    bridge = make_bridge(window)
    res = bridge.save_media_image("https://example.com/a.png", "a.png")
    assert res["ok"] is False
    assert res["msg"]
    assert window.calls == []


def test_save_success_copies_bytes(monkeypatch, tmp_path):
    """成功路径：按建议名生成默认文件名，原样复制字节并返回目标路径。"""
    src = tmp_path / "blob.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\npayload")
    dest = tmp_path / "out.png"
    patch_resolve(monkeypatch, str(src))
    window = FakeWindow([str(dest)])
    bridge = make_bridge(window)

    res = bridge.save_media_image(MEDIA_URL, "微信群二维码")

    assert res == {"ok": True, "path": str(dest)}
    assert dest.read_bytes() == src.read_bytes()
    _, kwargs = window.calls[0]
    assert kwargs["save_filename"] == "微信群二维码.png"


def test_user_cancel_returns_cancelled(monkeypatch, tmp_path):
    """用户取消对话框：ok=False 且带 cancelled 标记，前端据此不报错。"""
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    bridge = make_bridge(FakeWindow(None))

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["cancelled"] is True


def test_write_failure_returns_message(monkeypatch, tmp_path):
    """目标目录不存在等写盘失败：返回可读错误，不抛异常。"""
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    dest = tmp_path / "missing-dir" / "out.png"
    bridge = make_bridge(FakeWindow([str(dest)]))

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["msg"]


def test_window_not_ready(monkeypatch, tmp_path):
    """窗口尚未就绪时不崩，返回失败说明。"""
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    bridge = make_bridge(None)

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["msg"]
