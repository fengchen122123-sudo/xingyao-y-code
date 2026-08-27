import base64
import os

import pytest

from gui_bridge import ApiBridge


@pytest.fixture
def bridge():
    return ApiBridge()


def test_upload_file_ok(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))  # tempfile.gettempdir 走 TEMP
    data = base64.b64encode(b"hello-image-bytes").decode()
    res = bridge.upload_file("photo.jpg", data)
    assert res["ok"] is True
    assert os.path.isfile(res["path"])
    assert res["path"].endswith(".jpg")
    with open(res["path"], "rb") as f:
        assert f.read() == b"hello-image-bytes"


def test_upload_file_over_50mb_rejected(bridge):
    big = base64.b64encode(b"x" * (50 * 1024 * 1024 + 1)).decode()
    res = bridge.upload_file("big.bin", big)
    assert res["ok"] is False
    assert "50MB" in res["msg"]


def test_upload_file_invalid_base64(bridge):
    res = bridge.upload_file("x.png", "!!!not-base64!!!")
    assert res["ok"] is False
