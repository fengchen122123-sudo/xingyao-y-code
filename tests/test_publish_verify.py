"""
tests/test_publish_verify.py
============================
v1.2.16 发布事故回归测试：Gitee 附件管道按内容嗅探把 part4 误判为 UTF-16 文本，
注入 FF 拼成 BOM（体积 +1 字节）并把 Content-Type 标成 text/plain; charset=utf-16le，
导致全部旧客户端自动更新确定性失败。

覆盖两道长期防线：
  1. publish_release.verify_remote_attachment —— 上传后回读校验
     （Content-Type / Content-Length / 首 64 字节三重比对）
  2. publish_release.upload_and_verify —— 校验失败自动删附件重传一次
  3. build_release._safe_part_boundary —— 切分边界必须避开"看起来像
     UTF-16 文本"的起始字节（旧规则只查 BOM 与 0x00，不足以规避）
"""

import os
from unittest.mock import MagicMock, patch

import publish_release


def _make_local_file(tmp_path, body: bytes) -> str:
    p = tmp_path / "part.bin"
    p.write_bytes(body)
    return str(p)


def _fake_response(content_type: str, body: bytes):
    """模拟 urlopen 返回的响应：支持 with、headers.get、read(n)。"""
    resp = MagicMock()
    resp.headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    resp.read = lambda n=-1: body[:n] if n and n > 0 else body
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


# ============================================================================
# verify_remote_attachment
# ============================================================================

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
    """事故特征：Content-Type 被标成 text/plain; charset=utf-16le。"""
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
    """事故特征：远端比本地多 1 字节（被注入 BOM 首字节）。"""
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
    """同长度但首字节被改写（如 BOM 替换首字节）也必须检出。"""
    body = os.urandom(1024)
    tampered = b"\xff" + body[1:]
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


# ============================================================================
# upload_and_verify：校验失败自动删除坏附件并重传一次
# ============================================================================

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
    # 两片坏附件都必须被删掉，不能留毒在 Release 上
    assert m_del.call_count == 2


# ============================================================================
# build_release._safe_part_boundary：避开"看起来像 UTF-16 文本"的边界
# ============================================================================

def test_safe_part_boundary_rejects_utf16_like_head(tmp_path):
    """旧规则（查 BOM + 0x00）会接受 v1.2.16 事故边界：FE 91 FF 9D 开头、
    前 512 字节内有 0x00、但前 72 字节可连续按 UTF-16LE 解码。新规则必须跳过。"""
    import build_release

    # 构造危险头部：40 对可 UTF-16LE 解码的非控制码元（80 字节），
    # 末尾塞一个 0x00 骗过旧的 0x00 检查
    dangerous = b"\xfe\x91" + b"\x41\x29" * 40 + b"\x00"
    assert len(dangerous) <= 512
    # 安全后缀：以 0x00 开头（UTF-16 下是控制字符，立即排除文本嫌疑）
    suffix = b"\x00" * 64
    payload = dangerous + suffix + os.urandom(256)

    fpath = tmp_path / "big.bin"
    fpath.write_bytes(payload)
    off = 0  # 边界恰在危险头部起始处
    with open(fpath, "rb") as f:
        new_off = build_release._safe_part_boundary(f, off, len(payload))

    # 必须离开危险头部（旧规则会原样返回 0），且新边界的头部
    # 不能再被同一检测器判为 UTF-16 文本
    assert new_off != 0
    with open(fpath, "rb") as f:
        f.seek(new_off)
        assert not build_release._looks_like_utf16_text(f.read(512))
