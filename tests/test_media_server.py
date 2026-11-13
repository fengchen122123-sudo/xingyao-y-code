"""tests/test_media_server.py —— 受控媒体 HTTP 服务测试。"""

import os
import urllib.error
import urllib.request

import pytest

from core.media_server import media_server
from core.media_store import MEDIA_BLOBS_DIR, SUPPORTED_MIME, blob_name_is_safe

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


@pytest.fixture(scope="module")
def server():
    assert media_server.ensure_started()
    yield media_server


@pytest.fixture()
def blob():
    import hashlib

    digest = hashlib.sha256(PNG_BYTES).hexdigest()
    os.makedirs(MEDIA_BLOBS_DIR, exist_ok=True)
    path = os.path.join(MEDIA_BLOBS_DIR, digest + ".png")
    with open(path, "wb") as f:
        f.write(PNG_BYTES)
    return digest


def _fetch(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, dict(resp.headers), resp.read()


def test_public_url_shape(server, blob):
    url = server.public_url(blob, "png")
    assert url.startswith(f"{server.base_url()}/m/{blob}.png?t=")


def test_200_serves_blob_with_immutable_cache(server, blob):
    url = server.public_url(blob, "png")
    status, headers, body = _fetch(url)
    assert status == 200
    assert body == PNG_BYTES
    assert headers["Content-Type"] == "image/png"
    assert "immutable" in headers.get("Cache-Control", "")
    assert headers.get("Accept-Ranges") == "bytes"
    # 前端"复制原图"fetch Blob 依赖 CORS 头（令牌才是访问控制边界）
    assert headers.get("Access-Control-Allow-Origin") == "*"


def test_wrong_token_returns_404(server, blob):
    url = server.public_url(blob, "png")
    bad = url.replace("t=", "t=wrong")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _fetch(bad)
    assert exc.value.code == 404
    # 缺 token 同样 404
    no_token = url.split("?")[0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        _fetch(no_token)
    assert exc.value.code == 404


def test_bad_blob_name_returns_404(server):
    base = server.base_url()
    token = server._token
    for name in ["../../etc/passwd", "x" * 64 + ".exe", "a.png"]:
        url = f"{base}/m/{name}?t={token}"
        with pytest.raises(urllib.error.HTTPError) as exc:
            _fetch(url)
        assert exc.value.code == 404


def test_range_requests(server, blob):
    url = server.public_url(blob, "png")
    status, headers, body = _fetch(url, headers={"Range": "bytes=0-9"})
    assert status == 206
    assert body == PNG_BYTES[:10]
    assert headers["Content-Range"] == f"bytes 0-9/{len(PNG_BYTES)}"
    # 开放尾区间
    status, headers, body = _fetch(url, headers={"Range": "bytes=90-"})
    assert status == 206
    assert body == PNG_BYTES[90:]
    # 越界起点 → 416
    with pytest.raises(urllib.error.HTTPError) as exc:
        _fetch(url, headers={"Range": f"bytes={len(PNG_BYTES)}-"})
    assert exc.value.code == 416


def test_head_request_no_body(server, blob):
    url = server.public_url(blob, "png")
    status, headers, body = _fetch(url, method="HEAD")
    assert status == 200
    assert body == b""
    assert int(headers["Content-Length"]) == len(PNG_BYTES)


def test_blob_name_whitelist():
    from core.media_store import SUPPORTED_MIME

    assert blob_name_is_safe("a" * 64 + ".png")
    for mime, ext in SUPPORTED_MIME.items():
        assert blob_name_is_safe("b" * 64 + ext), (mime, ext)


# ---- 媒体 URL → 本地 Blob 路径反解（"另存为"用） ----


def test_local_path_for_url_roundtrip(server, blob):
    """本服务签发的 URL 应反解回真实存在的 Blob 文件路径。"""
    url = server.public_url(blob, "png")
    resolved = server.local_path_for_url(url)
    assert resolved is not None
    assert os.path.isfile(resolved)
    assert os.path.basename(resolved) == blob + ".png"


def test_local_path_for_url_rejects_foreign_and_bad(server, blob):
    """错令牌、缺令牌、非本机地址、非本服务端口、非法文件名一律拒绝。"""
    url = server.public_url(blob, "png")
    assert server.local_path_for_url(url.replace("t=", "t=wrong")) is None
    assert server.local_path_for_url(url.split("?")[0]) is None
    assert server.local_path_for_url("") is None
    assert server.local_path_for_url("https://example.com/m/" + blob + ".png") is None
    assert server.local_path_for_url("file:///" + blob + ".png") is None
    # 同主机不同端口：不是本服务
    assert server.local_path_for_url(
        f"http://127.0.0.1:1/m/{blob}.png?t={server._token}"
    ) is None
    # 文件名不合白名单
    assert server.local_path_for_url(
        f"{server.base_url()}/m/not-a-blob.png?t={server._token}"
    ) is None


def test_local_path_for_url_missing_blob_returns_none(server):
    """格式合法但仓库里没有该 Blob → None（不抛异常）。"""
    missing = "0" * 64
    url = server.public_url(missing, "png")
    assert server.local_path_for_url(url) is None
