import os
from unittest.mock import MagicMock, patch

import publish_release


def _make_local_file(tmp_path, body: bytes) -> str:
    p = tmp_path / "part.bin"
    p.write_bytes(body)
    return str(p)


def _fake_response(content_type: str, body: bytes):
    resp = MagicMock()
    resp.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    resp.read = lambda n=-1: body[:n] if n and n > 0 else body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def test_verify_ok_when_binary_and_bytes_match(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    with patch("urllib.request.urlopen") as m:
        m.return_value = _fake_response("application/octet-stream", body)
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is True
    assert reason == ""


def test_verify_rejects_text_plain_utf16le(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    with patch("urllib.request.urlopen") as m:
        m.return_value = _fake_response("text/plain; charset=utf-16le", body)
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is False
    assert "text/plain" in reason


def test_verify_rejects_html(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    with patch("urllib.request.urlopen") as m:
        m.return_value = _fake_response("text/html; charset=utf-8", body)
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is False
    assert "text/html" in reason


def test_verify_rejects_bom_injected_size_plus_one(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    with patch("urllib.request.urlopen") as m:
        m.return_value = _fake_response(
            "application/octet-stream", b"\xff" + body
        )
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is False
    assert "大小" in reason or "字节" in reason


def test_verify_rejects_head_mismatch_same_size(tmp_path):
    body = os.urandom(1024)
    tampered = bytes([body[0] ^ 0xFF]) + body[1:]
    local = _make_local_file(tmp_path, body)
    with patch("urllib.request.urlopen") as m:
        m.return_value = _fake_response("application/octet-stream", tampered)
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is False
    assert "首" in reason or "不一致" in reason


def test_verify_network_error_is_failure(tmp_path):
    local = _make_local_file(tmp_path, b"x" * 128)
    with patch("urllib.request.urlopen", side_effect=OSError("boom")):
        ok, reason = publish_release.verify_remote_attachment(
            "https://gitee.com/x/y/releases/download/v1/part.bin", local
        )
    assert ok is False
    assert reason


def test_upload_and_verify_retries_once_then_succeeds(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    good = {"id": 102, "browser_download_url": "https://x/good"}
    bad = {"id": 101, "browser_download_url": "https://x/bad"}

    with patch.object(publish_release, "upload_attachment",
                      side_effect=[bad, good]) as m_up, \
         patch.object(publish_release, "verify_remote_attachment",
                      side_effect=[(False, "text/plain"), (True, "")]) as m_ver, \
         patch.object(publish_release, "delete_attachment") as m_del:
        result = publish_release.upload_and_verify("tok", 918054, local)

    assert result is good
    assert m_up.call_count == 2
    m_del.assert_called_once_with("tok", 918054, 101)
    assert m_ver.call_count == 2


def test_upload_and_verify_raises_when_persistent(tmp_path):
    body = os.urandom(1024)
    local = _make_local_file(tmp_path, body)
    bad1 = {"id": 101, "browser_download_url": "https://x/bad1"}
    bad2 = {"id": 102, "browser_download_url": "https://x/bad2"}

    with patch.object(publish_release, "upload_attachment",
                      side_effect=[bad1, bad2]), \
         patch.object(publish_release, "verify_remote_attachment",
                      return_value=(False, "text/plain")), \
         patch.object(publish_release, "delete_attachment") as m_del:
        try:
            publish_release.upload_and_verify("tok", 918054, local)
        except RuntimeError as e:
            assert "text/plain" in str(e)
        else:
            raise AssertionError("应当抛 RuntimeError")
    assert m_del.call_count == 2


def test_safe_part_boundary_rejects_utf16_like_head(tmp_path):
    import build_release

    dangerous = b"\xfe\x91" + b"\x41\x29" * 40 + b"\x00"
    assert len(dangerous) <= 512
    suffix = b"\x00" * 64
    payload = dangerous + suffix + os.urandom(256)

    fpath = tmp_path / "big.bin"
    fpath.write_bytes(payload)
    off = 0
    with open(fpath, "rb") as f:
        new_off = build_release._safe_part_boundary(f, off, len(payload))

    assert new_off != 0
    with open(fpath, "rb") as f:
        f.seek(new_off)
        assert not build_release._looks_like_utf16_text(f.read(512))


def _load_verify_vendor():
    import importlib.util

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts",
        "verify_vendor.py",
    )
    spec = importlib.util.spec_from_file_location("verify_vendor_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_vendor_tree(root, *, record_digest, extra_file=None):
    import hashlib
    import json as _json

    vendor = root / "vendor"
    vendor.mkdir(exist_ok=True)
    body = b"hello"
    (vendor / "x.bin").write_bytes(body)
    if extra_file:
        (vendor / extra_file).write_bytes(b"unlisted")
    manifest = {
        "files": [
            {
                "path": "vendor/x.bin",
                "sha256": record_digest or hashlib.sha256(body).hexdigest(),
                "hash_mode": "binary",
            }
        ]
    }
    (vendor / "VENDOR_MANIFEST.json").write_text(
        _json.dumps(manifest), encoding="utf-8"
    )


def test_verify_vendor_detects_tamper_and_gaps(tmp_path):
    import hashlib

    mod = _load_verify_vendor()
    good = hashlib.sha256(b"hello").hexdigest()

    _write_vendor_tree(tmp_path, record_digest=good)
    assert mod.verify_vendor(str(tmp_path), printer=lambda *a, **k: None) == []

    _write_vendor_tree(tmp_path, record_digest="0" * 64)
    problems = mod.verify_vendor(str(tmp_path), printer=lambda *a, **k: None)
    assert any("SHA256 不一致" in p for p in problems)

    _write_vendor_tree(tmp_path, record_digest=good, extra_file="y.bin")
    problems = mod.verify_vendor(str(tmp_path), printer=lambda *a, **k: None)
    assert any("未登记" in p for p in problems)

    (tmp_path / "vendor" / "VENDOR_MANIFEST.json").unlink()
    problems = mod.verify_vendor(str(tmp_path), printer=lambda *a, **k: None)
    assert any("记录文件不存在" in p for p in problems)
