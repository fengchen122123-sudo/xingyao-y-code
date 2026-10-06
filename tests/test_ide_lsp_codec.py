# -*- coding: utf-8 -*-
"""LSP Content-Length 帧编解码：分片到达、粘包、异常头。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide.lsp_codec import FrameDecoder, LspFrameError, encode_frame


def test_encode_decode_roundtrip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"x": "中文"}}
    assert FrameDecoder().feed(encode_frame(payload)) == [payload]


def test_length_counts_utf8_bytes_not_characters():
    payload = {"m": "中文"}
    frame = encode_frame(payload)
    length = int(frame.split(b"\r\n", 1)[0].split(b":")[1])
    # 中文一个字符三字节，按字符数算会让服务器少读一半正文
    assert length == len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def test_split_arrival_at_every_byte():
    payload = {"jsonrpc": "2.0", "method": "test/pushed", "params": {"n": 1}}
    frame = encode_frame(payload)
    decoder = FrameDecoder()
    out = []
    for i in range(len(frame)):
        out.extend(decoder.feed(frame[i:i + 1]))
    assert out == [payload]


def test_two_frames_in_one_chunk():
    a = {"id": 1}
    b = {"id": 2}
    assert FrameDecoder().feed(encode_frame(a) + encode_frame(b)) == [a, b]


def test_extra_headers_are_tolerated():
    body = json.dumps({"id": 3}).encode("utf-8")
    frame = (
        b"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n"
        b"Content-Length: %d\r\n\r\n" % len(body)
    ) + body
    assert FrameDecoder().feed(frame) == [{"id": 3}]


def test_missing_content_length_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"X-Test: 1\r\n\r\n{}")


def test_invalid_json_body_raises():
    body = b"{ not json"
    frame = b"Content-Length: %d\r\n\r\n" % len(body) + body
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(frame)


def test_negative_length_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"Content-Length: -5\r\n\r\n")


def test_oversized_header_raises():
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"X-Test: " + b"a" * (17 * 1024))


def test_partial_header_waits_instead_of_raising():
    decoder = FrameDecoder()
    assert decoder.feed(b"Content-Len") == []
    assert decoder.feed(b"gth: 2\r\n\r\n{}") == [{}]


# --- 单帧上限：Content-Length 是攻击面 -------------------------------------


def test_oversized_content_length_raises_before_body_arrives():
    """只给了头就必须报错：不能因为正文永远不来而无限等待。"""
    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"Content-Length: 999999999999\r\n\r\n")


def test_content_length_one_over_limit_raises():
    from core.ide.lsp_codec import MAX_PAYLOAD_BYTES

    with pytest.raises(LspFrameError):
        FrameDecoder().feed(b"Content-Length: %d\r\n\r\n" % (MAX_PAYLOAD_BYTES + 1))


def test_large_but_bounded_payload_still_decodes():
    """上限之下的大帧（含多字节字符）必须照常解出，避免上限误伤正常响应。"""
    payload = {"method": "textDocument/publishDiagnostics", "params": {"big": "中" * 20000}}
    assert FrameDecoder().feed(encode_frame(payload)) == [payload]


def test_oversized_length_does_not_allocate_buffer():
    """超大 Content-Length 不能触发按声明长度预分配。"""
    decoder = FrameDecoder()
    with pytest.raises(LspFrameError):
        decoder.feed(b"Content-Length: %d\r\n\r\n" % (200 * 1024 * 1024))
    assert decoder.pending_bytes < 64
