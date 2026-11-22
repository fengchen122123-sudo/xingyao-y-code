import re
from urllib.error import URLError

from core.tools import fetch_image


class _FakeImageResponse:

    def __init__(self, body, content_type="image/png"):
        self._body = body
        self._pos = 0
        self.status = 200
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit=-1):
        if limit < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk


def _patch_http(monkeypatch, body=b"", content_type="image/png"):
    def fake_urlopen(request, timeout):
        return _FakeImageResponse(body, content_type)

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)


def test_fetch_image_downloads_png_and_returns_local_path(monkeypatch, tmp_path):
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 128
    _patch_http(monkeypatch, payload, "image/png")
    monkeypatch.setattr(
        "core.ai_output_paths._read_setting", lambda: str(tmp_path)
    )

    result = fetch_image("https://example.com/pic.png")

    assert not result.startswith("🚨"), f"下载应成功，实际: {result}"
    assert "HTTP 200" in result
    files = list(tmp_path.glob("download_*.png"))
    assert len(files) == 1, f"应落盘一个 .png 文件，实际: {files}"
    saved = files[0]
    assert re.fullmatch(r"download_\d{8}_\d{6}\.png", saved.name), saved.name
    assert saved.read_bytes() == payload
    assert str(saved) in result


def test_fetch_image_rejects_svg(monkeypatch, tmp_path):
    _patch_http(monkeypatch, b"<svg><script>alert(1)</script></svg>", "image/svg+xml")
    monkeypatch.chdir(tmp_path)

    result = fetch_image("https://example.com/icon.svg")

    assert result.startswith("🚨")
    assert "svg" in result.lower() or "不支持" in result
    assert not list(tmp_path.glob("download_*")), "拒绝时不应落盘任何文件"


def test_fetch_image_rejects_html(monkeypatch, tmp_path):
    _patch_http(monkeypatch, b"<html><body>not an image</body></html>", "text/html; charset=utf-8")
    monkeypatch.chdir(tmp_path)

    result = fetch_image("https://example.com/page")

    assert result.startswith("🚨")
    assert not list(tmp_path.glob("download_*"))


def test_fetch_image_rejects_non_http_schemes():
    for url in ("file:///C:/secret.png", "ftp://example.com/x.png"):
        result = fetch_image(url)
        assert result.startswith("🚨"), url
        assert "http" in result.lower()


def test_fetch_image_aborts_when_body_exceeds_10mb(monkeypatch, tmp_path):
    body = b"\xff" * (10 * 1024 * 1024 + 1)
    _patch_http(monkeypatch, body, "image/png")
    monkeypatch.chdir(tmp_path)

    result = fetch_image("https://example.com/huge.png")

    assert result.startswith("🚨")
    assert "超过" in result or "限制" in result
    assert not list(tmp_path.glob("download_*")), "中止时不应留下半成品文件"


def test_fetch_image_network_error_returns_message(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)
    monkeypatch.chdir(tmp_path)

    result = fetch_image("https://example.com/pic.png")

    assert result.startswith("🚨")
    assert "connection refused" in result
    assert not list(tmp_path.glob("download_*"))
