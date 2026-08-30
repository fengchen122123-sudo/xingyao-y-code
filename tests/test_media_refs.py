"""tests/test_media_refs.py —— 媒体引用解析器测试。"""

import os

import pytest

from core.media_refs import (
    attach_media_manifest,
    build_media_manifest,
    extract_media_refs,
)
from core.media_store import MediaStoreError

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 32


@pytest.fixture()
def media_dir(tmp_path):
    def _make(name: str, data: bytes = PNG_BYTES) -> str:
        path = os.path.join(str(tmp_path), name)
        with open(path, "wb") as f:
            f.write(data)
        return path
    return _make


def test_extract_image_and_video_ordered(media_dir):
    text = (
        "开头\n"
        f"![江南水乡]({media_dir('a.png')})\n"
        f"[video: 演示]({media_dir('b.mp4')})\n"
        f"[视频：中文标记]({media_dir('c.mp4')})\n"
    )
    refs = extract_media_refs(text)
    assert [r["ordinal"] for r in refs] == [0, 1, 2]
    assert refs[0]["caption"] == "江南水乡"
    assert refs[1]["caption"] == "演示"
    assert refs[2]["caption"] == "中文标记"
    assert os.path.basename(refs[1]["path"]) == "b.mp4"


def test_extract_skips_code_blocks_and_remote_urls():
    text = (
        "```bash\n![代码块里的不是媒体](x.png)\n```\n"
        "`![行内代码里的也不算](y.png)`\n"
        "![远程不下载](https://example.com/pic.png)\n"
        "[video: 远程视频](https://example.com/v.mp4)\n"
        "[普通链接](file_z.txt)\n"
    )
    assert extract_media_refs(text) == []


def test_extract_relative_path_keeps_raw(media_dir):
    refs = extract_media_refs("![图](img/相对.png)", workdir=str(media_dir))
    assert refs[0]["path"] == "img/相对.png"


def test_build_manifest_success_and_error_items(media_dir):
    good = media_dir("ok.png", PNG_BYTES)
    text = f"![好图]({good})\n![坏图](不存在.png)\n[video: 视频]({media_dir('v.mp4', MP4_BYTES)})\n"
    manifest = build_media_manifest(text, workdir=None)
    items = manifest["items"]
    assert manifest["version"] == 1
    assert len(items) == 3
    assert items[0]["kind"] == "image"
    assert items[0]["caption"] == "好图"
    assert items[0]["digest"]
    assert items[0]["mime"] == "image/png"
    assert items[1]["kind"] == "error"
    assert items[1]["error_code"] == "not_found"
    assert items[2]["kind"] == "video"
    assert items[2]["mime"] == "video/mp4"


def test_build_manifest_none_without_media(media_dir):
    assert build_media_manifest("纯文本，没有媒体") is None
    assert build_media_manifest("") is None


def test_attach_media_manifest_to_persisted_copy(media_dir):
    good = media_dir("pic.png", PNG_BYTES)
    msg = {
        "role": "assistant",
        "content": f"图如下 ![图]({good})",
        "_display": {"model_id": "m", "model_name": "m"},
    }
    attach_media_manifest(msg)
    items = msg["_display"]["media_v1"]["items"]
    assert len(items) == 1 and items[0]["kind"] == "image"
    # 原 _display 字段保留
    assert msg["_display"]["model_id"] == "m"


def test_attach_media_manifest_skips_non_assistant(media_dir):
    msg = {"role": "user", "content": "![图](a.png)"}
    attach_media_manifest(msg)
    assert "_display" not in msg


def test_attach_propagates_infra_error(monkeypatch, tmp_path):
    # 仓库不可写 → 契约要求显式失败而非静默降级
    from core import media_store

    def boom(path, workdir=None):
        raise MediaStoreError("repo_unwritable", "仓库写入失败")

    monkeypatch.setattr(media_store, "archive_media_file", boom)
    monkeypatch.setattr("core.media_refs.archive_media_file", boom)
    msg = {"role": "assistant", "content": "![图](x.png)"}
    with pytest.raises(MediaStoreError):
        attach_media_manifest(msg, str(tmp_path))
