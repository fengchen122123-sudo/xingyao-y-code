import gui_bridge
from gui_bridge import ApiBridge, _media_save_file_types, _media_save_filename

MEDIA_URL = "http://127.0.0.1:5000/m/" + "a" * 64 + ".png?t=token"


class FakeWindow:

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


def test_filename_keeps_chinese_stem_and_real_ext():
    assert _media_save_filename("微信群二维码", ".png") == "微信群二维码.png"


def test_filename_strips_path_and_illegal_chars():
    assert _media_save_filename(r"C:\tmp\a:b*c?.jpg", ".png") == "a_b_c_.png"


def test_filename_falls_back_when_empty():
    assert _media_save_filename("   ", ".jpg") == "ycode-image.jpg"
    assert _media_save_filename("", ".jpg") == "ycode-image.jpg"


def test_file_types_by_ext():
    assert _media_save_file_types(".mp4") == ("视频文件 (*.mp4;*.webm)",)
    assert _media_save_file_types(".webm") == ("视频文件 (*.mp4;*.webm)",)
    assert _media_save_file_types(".MP4") == ("视频文件 (*.mp4;*.webm)",)
    assert _media_save_file_types(".png") == ("图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.avif)",)
    assert _media_save_file_types("") == ("图片文件 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.avif)",)


def test_filename_video_fallback():
    assert _media_save_filename("", ".mp4") == "ycode-video.mp4"
    assert _media_save_filename("   ", ".webm") == "ycode-video.webm"


def test_rejects_non_media_url(monkeypatch):
    patch_resolve(monkeypatch, None)
    window = FakeWindow(None)
    bridge = make_bridge(window)
    res = bridge.save_media_image("https://example.com/a.png", "a.png")
    assert res["ok"] is False
    assert res["msg"]
    assert window.calls == []


def test_save_success_copies_bytes(monkeypatch, tmp_path):
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
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    bridge = make_bridge(FakeWindow(None))

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["cancelled"] is True


def test_write_failure_returns_message(monkeypatch, tmp_path):
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    dest = tmp_path / "missing-dir" / "out.png"
    bridge = make_bridge(FakeWindow([str(dest)]))

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["msg"]


def test_window_not_ready(monkeypatch, tmp_path):
    src = tmp_path / "blob.png"
    src.write_bytes(b"x")
    patch_resolve(monkeypatch, str(src))
    bridge = make_bridge(None)

    res = bridge.save_media_image(MEDIA_URL, "图")

    assert res["ok"] is False
    assert res["msg"]


def test_save_video_uses_video_filter(monkeypatch, tmp_path):
    src = tmp_path / "blob.mp4"
    src.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    dest = tmp_path / "out.mp4"
    patch_resolve(monkeypatch, str(src))
    window = FakeWindow([str(dest)])
    bridge = make_bridge(window)

    res = bridge.save_media_image(MEDIA_URL.replace(".png", ".mp4"), "演示视频")

    assert res == {"ok": True, "path": str(dest)}
    assert dest.read_bytes() == src.read_bytes()
    _, kwargs = window.calls[0]
    assert kwargs["file_types"] == ("视频文件 (*.mp4;*.webm)",)
    assert kwargs["save_filename"] == "演示视频.mp4"
