"""画布媒体两方法（M2-T03）的入口测试。

覆盖五段契约：
1) dataURL 落盘往返：png 落盘 → 文件字节原样、path 为项目内相对路径（正斜杠）、
   归档 digest 回填 media_id、预览 URL 与 resolve 结果一致、width/height 取真实尺寸；
2) 非图片 / 无归档能力格式（mp3）：文件照样落盘，但 media_id / url 留空、width/height
   为 None —— 归档失败不推翻"文件已落好"这一事实；
3) 坏 base64、未知 mime（image/svg+xml）、超上限（monkeypatch 小上限，不真造 32MB）
   一律 ok=False，且拒绝路径上不写任何文件；
4) resolve 未知 digest / 非法 media_id 报错，不抛异常；
5) 路径全部限定在 .ycode/canvas/media/（含 hint_name 带 "../" 的注入探测）。

宿主构造照抄 tests/test_canvas_persistence.py：假桥只补齐画布混入依赖的一件
事——宿主工作目录（``_canvas_workdir``），其余全部走真实实现（真实 media_store
归档 + 真实 media_server 预览 URL）。
"""

import base64
import struct
import zlib
from pathlib import Path

import gui_canvas_mixin
from gui_canvas_mixin import CanvasBridgeMixin

# 归档仓库（conftest 已把 YCODE_DATA_DIR 指到临时目录，不会污染真实 ~/.ycode）
from core.media_store import MEDIA_BLOBS_DIR


class _FakeBridge(CanvasBridgeMixin):
    """最小假桥：只补齐画布混入依赖的宿主工作目录。"""

    def __init__(self, workdir):
        self._workdir = str(workdir)

    def _canvas_workdir(self):
        return self._workdir


def _png_bytes(width=3, height=2):
    """纯 Python 造一张最小合法 PNG（不依赖 Pillow 生成，只要魔数 + IHDR 可读）。"""

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _data_url(payload, mime):
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def _media_dir(bridge):
    return Path(bridge._canvas_workdir()) / ".ycode" / "canvas" / "media"


def _media_files(bridge):
    root = _media_dir(bridge)
    return sorted(p.name for p in root.glob("*")) if root.is_dir() else []


def _new_bridge(tmp_path):
    workdir = tmp_path / "proj"
    workdir.mkdir()
    return _FakeBridge(workdir), workdir


# ---- 1) png 落盘往返 ----


def test_save_png_data_url_roundtrip(tmp_path):
    bridge, workdir = _new_bridge(tmp_path)
    payload = _png_bytes(3, 2)

    result = bridge.canvas_save_media(_data_url(payload, "image/png"))

    assert result["ok"] is True, result
    assert result["bytes"] == len(payload)
    # width/height 取真实尺寸（走 media_store 的 Pillow 探测）
    assert (result["width"], result["height"]) == (3, 2)
    # path 是项目内相对路径，正斜杠，且落在 .ycode/canvas/media/ 下
    assert result["path"].startswith(".ycode/canvas/media/")
    assert "\\" not in result["path"]
    assert result["path"].endswith(".png")
    on_disk = workdir / Path(result["path"])
    assert on_disk.is_file()
    assert on_disk.read_bytes() == payload
    # 落盘文件在当前工作目录内（没跑到 tmp_path 之外）
    assert on_disk.resolve().is_relative_to(workdir.resolve())
    # media_id 为 sha256 digest，归档 blob 真实存在
    assert len(result["media_id"]) == 64
    assert (Path(MEDIA_BLOBS_DIR) / f"{result['media_id']}.png").is_file()
    # 预览 URL 为 media_server 签发的带令牌地址，且与 resolve 同口径
    assert result["url"].startswith("http://127.0.0.1:")
    assert result["media_id"] in result["url"]

    resolved = bridge.canvas_resolve_media(result["media_id"])
    assert resolved["ok"] is True, resolved
    assert resolved["url"] == result["url"]


def test_save_plain_base64_uses_hint_name_ext(tmp_path):
    """无 dataURL 前缀的纯 base64：扩展名从 hint_name 推断。"""
    bridge, workdir = _new_bridge(tmp_path)
    payload = _png_bytes(1, 1)

    result = bridge.canvas_save_media(
        base64.b64encode(payload).decode("ascii"), hint_name="paste.PNG"
    )

    assert result["ok"] is True, result
    assert result["path"].endswith(".png")
    assert (workdir / Path(result["path"])).read_bytes() == payload
    assert (result["width"], result["height"]) == (1, 1)


# ---- 2) mp3：落盘成功但归档失败 ----


def test_save_mp3_lands_but_archive_failure_keeps_placeholder(tmp_path):
    """mp3 不在 media_store 白名单：文件照落，media_id/url 留空，不推翻落盘事实。"""
    bridge, workdir = _new_bridge(tmp_path)
    payload = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 40

    result = bridge.canvas_save_media(_data_url(payload, "audio/mpeg"))

    assert result["ok"] is True, result
    assert result["path"].startswith(".ycode/canvas/media/")
    assert result["path"].endswith(".mp3")
    assert (workdir / Path(result["path"])).read_bytes() == payload
    assert result["bytes"] == len(payload)
    assert result["media_id"] == ""
    assert result["url"] == ""
    # 非图片：width/height 为 None
    assert result["width"] is None
    assert result["height"] is None


# ---- 3) 各类拒绝路径 ----


def test_save_rejects_bad_base64_without_writing(tmp_path):
    bridge, _workdir = _new_bridge(tmp_path)

    result = bridge.canvas_save_media("data:image/png;base64,@@@not-base64@@@")

    assert result["ok"] is False
    assert "base64" in result["msg"]
    assert _media_files(bridge) == []


def test_save_rejects_empty_payload(tmp_path):
    bridge, _workdir = _new_bridge(tmp_path)

    for raw in ("", "data:image/png;base64,", "data:image/png"):
        result = bridge.canvas_save_media(raw)
        assert result["ok"] is False, raw
        assert result["msg"]
    assert _media_files(bridge) == []


def test_save_rejects_unknown_mime(tmp_path):
    """白名单外的 mime（svg 可带脚本）必须拒绝，不按 png 静默落盘。"""
    bridge, _workdir = _new_bridge(tmp_path)

    result = bridge.canvas_save_media(
        _data_url(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml")
    )

    assert result["ok"] is False
    assert "不支持" in result["msg"]
    assert _media_files(bridge) == []


def test_save_rejects_oversize_without_writing(tmp_path, monkeypatch):
    """上限用 monkeypatch 缩小来验证（不真造 32MB 数据）。"""
    bridge, _workdir = _new_bridge(tmp_path)
    monkeypatch.setattr(gui_canvas_mixin, "CANVAS_MEDIA_MAX_BYTES", 32)

    result = bridge.canvas_save_media(_data_url(_png_bytes(8, 8), "image/png"))

    assert result["ok"] is False
    assert "上限" in result["msg"]
    assert _media_files(bridge) == []


def test_save_rejects_raw_bytes_over_limit(tmp_path, monkeypatch):
    """bytes 入参没有 base64 长度可预判，只能按真实字节数复核上限。"""
    bridge, _workdir = _new_bridge(tmp_path)
    payload = _png_bytes(2, 2)
    monkeypatch.setattr(gui_canvas_mixin, "CANVAS_MEDIA_MAX_BYTES", len(payload) - 1)

    result = bridge.canvas_save_media(payload)

    assert result["ok"] is False
    assert "上限" in result["msg"]
    assert _media_files(bridge) == []


# ---- 4) resolve ----


def test_resolve_unknown_digest_fails(tmp_path):
    bridge, _workdir = _new_bridge(tmp_path)

    result = bridge.canvas_resolve_media("0" * 64)

    assert result["ok"] is False
    assert "不存在" in result["msg"]


def test_resolve_rejects_illegal_media_id(tmp_path):
    bridge, _workdir = _new_bridge(tmp_path)

    for bad in ("", "   ", None, "../x", "a" * 63, "g" * 64, "0" * 64 + "/../x.png"):
        result = bridge.canvas_resolve_media(bad)
        assert result["ok"] is False, bad
        assert result["msg"]


# ---- 5) 路径限定 ----


def test_media_paths_stay_inside_media_dir(tmp_path):
    """hint_name 里塞 ../ 也只影响扩展名，落盘路径始终在 media 目录内。"""
    bridge, workdir = _new_bridge(tmp_path)
    payload = _png_bytes(2, 2)

    result = bridge.canvas_save_media(
        base64.b64encode(payload).decode("ascii"), hint_name="../../evil.png"
    )

    assert result["ok"] is True, result
    assert result["path"].startswith(".ycode/canvas/media/")
    on_disk = (workdir / Path(result["path"])).resolve()
    assert on_disk.is_relative_to((workdir / ".ycode" / "canvas" / "media").resolve())
    # 注入目标位置一处都没被创建
    assert not (tmp_path / "evil.png").exists()
    assert not (workdir / "evil.png").exists()
    assert not (workdir / ".ycode" / "evil.png").exists()
    # 工作目录里除 media 目录之外没有多出任何文件
    created = [
        p
        for p in workdir.rglob("*")
        if p.is_file() and ".ycode" not in p.relative_to(workdir).parts
    ]
    assert created == []
