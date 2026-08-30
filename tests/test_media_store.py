"""tests/test_media_store.py —— 永久媒体仓库层测试。"""

import os

import pytest

from core.media_store import (
    MEDIA_BLOBS_DIR,
    SUPPORTED_MIME,
    MediaStoreError,
    archive_media_file,
    blob_name_is_safe,
    blob_path_for,
    sniff_mime,
)


def _write(tmp_path, name: str, data: bytes) -> str:
    path = os.path.join(str(tmp_path), name)
    with open(path, "wb") as f:
        f.write(data)
    return path


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x00" * 64
GIF_BYTES = b"GIF89a" + b"\x00" * 64
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64
WEBM_BYTES = b"\x1a\x45\xdf\xa3" + b"\x00" * 64
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 32
AVIF_BYTES = b"\x00\x00\x00\x18ftypavif\x00\x00\x00\x00avifmif1" + b"\x00" * 32


def test_sniff_mime_all_supported_formats(tmp_path):
    cases = [
        ("a.png", PNG_BYTES, "image/png"),
        ("a.jpg", JPEG_BYTES, "image/jpeg"),
        ("a.gif", GIF_BYTES, "image/gif"),
        ("a.webp", WEBP_BYTES, "image/webp"),
        ("a.webm", WEBM_BYTES, "video/webm"),
        ("a.mp4", MP4_BYTES, "video/mp4"),
        ("a.avif", AVIF_BYTES, "image/avif"),
    ]
    for name, data, expected in cases:
        path = _write(tmp_path, name, data)
        assert sniff_mime(path) == expected, name


def test_sniff_mime_rejects_svg_and_unknown(tmp_path):
    svg = _write(tmp_path, "evil.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    txt = _write(tmp_path, "note.txt", b"hello world")
    assert sniff_mime(svg) is None
    assert sniff_mime(txt) is None


def test_archive_dedupes_by_content(tmp_path):
    src = _write(tmp_path, "pic.png", PNG_BYTES)
    item1 = archive_media_file(src)
    item2 = archive_media_file(_write(tmp_path, "copy.png", PNG_BYTES))
    assert item1["digest"] == item2["digest"]
    blob = os.path.join(MEDIA_BLOBS_DIR, item1["digest"] + ".png")
    assert os.path.isfile(blob)
    # 同内容两个源文件归档后仓库里只有一个 Blob
    blobs = [f for f in os.listdir(MEDIA_BLOBS_DIR) if f == item1["digest"] + ".png"]
    assert blobs == [item1["digest"] + ".png"]
    assert item1["kind"] == "image"
    assert item1["mime"] == "image/png"
    assert item1["ext"] == "png"
    assert item1["bytes"] == len(PNG_BYTES)
    assert item1["source_name"] == "pic.png"


def test_archive_video_kind(tmp_path):
    item = archive_media_file(_write(tmp_path, "demo.mp4", MP4_BYTES))
    assert item["kind"] == "video"
    assert item["mime"] == "video/mp4"


def test_archive_errors(tmp_path):
    with pytest.raises(MediaStoreError) as exc:
        archive_media_file(os.path.join(str(tmp_path), "missing.png"))
    assert exc.value.error_code == "not_found"

    svg = _write(tmp_path, "evil.svg", b"<svg/>")
    with pytest.raises(MediaStoreError) as exc:
        archive_media_file(svg)
    assert exc.value.error_code == "unsupported_type"

    # 扩展名伪装：.png 后缀但内容是文本 → 嗅探拒绝
    fake = _write(tmp_path, "fake.png", b"just text")
    with pytest.raises(MediaStoreError) as exc:
        archive_media_file(fake)
    assert exc.value.error_code == "unsupported_type"


def test_archive_relative_path_resolved_against_workdir(tmp_path):
    src = _write(tmp_path, "rel.png", PNG_BYTES)
    item = archive_media_file("rel.png", str(tmp_path))
    assert item["digest"]
    assert item["source_name"] == "rel.png"
    assert os.path.isfile(src)


def test_blob_path_for_rejects_traversal_and_bad_digest():
    with pytest.raises(MediaStoreError):
        blob_path_for("../etc/passwd".replace("/", "") .ljust(64, "a"), "image/png")
    with pytest.raises(MediaStoreError):
        blob_path_for("A" * 64, "image/png")
    with pytest.raises(MediaStoreError):
        blob_path_for("a" * 64, "image/svg+xml")
    path = blob_path_for("a" * 64, "image/png")
    assert path.endswith("a" * 64 + ".png")


def test_blob_name_is_safe():
    assert blob_name_is_safe("a" * 64 + ".png")
    assert blob_name_is_safe("f" * 64 + ".webm")
    assert not blob_name_is_safe("../x.png")
    assert not blob_name_is_safe("a" * 64 + ".exe")
    assert not blob_name_is_safe("a" * 63 + ".png")
    assert not blob_name_is_safe("a" * 64 + ".png/../evil")
    assert not blob_name_is_safe("")


def test_supported_mime_whitelist_has_no_svg():
    assert "image/svg+xml" not in SUPPORTED_MIME
