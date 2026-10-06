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


# --- 跨帧切片：一个 chunk 同时含上一帧尾部与下一帧头部 ----------------------


def test_chunk_with_previous_frame_tail_and_next_frame_head():
    """缓冲区切片顺序错误（先取正文再切头、或按错偏移删字节）只在这条路径暴露。

    逐字节喂与「整块喂多帧」都覆盖不到：前者每帧都在自己的 chunk 里成帧，
    后者每次切分都落在帧边界上。这里切在 A 的正文中间，让同一个 chunk 同时携带
    A 的尾部与 B 的头部。
    """
    a = {"jsonrpc": "2.0", "id": 1, "result": {"v": "a"}}
    b = {"jsonrpc": "2.0", "method": "test/pushed", "params": {"n": 2}}
    frame_a = encode_frame(a)
    frame_b = encode_frame(b)
    cut = len(frame_a) - 7
    decoder = FrameDecoder()
    assert decoder.feed(frame_a[:cut]) == []
    assert decoder.pending_bytes == cut
    assert decoder.feed(frame_a[cut:] + frame_b[:6]) == [a]
    assert decoder.pending_bytes == 6
    assert decoder.feed(frame_b[6:]) == [b]
    assert decoder.pending_bytes == 0


# --- 坏帧不能连带丢掉同一次 feed 里已解码的合法消息 ------------------------


def test_messages_decoded_before_bad_frame_are_kept():
    """[合法帧 A][坏帧 B] 同一个 chunk：A 必须能从异常里取回来。

    A 很可能正是 initialize 响应，丢掉它管理器就会一直等一个永远不会被
    投递的响应（挂起），而不是报错。
    """
    good = {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {"hoverProvider": True}}}
    bad_body = b"{ not json"
    bad = b"Content-Length: %d\r\n\r\n" % len(bad_body) + bad_body
    decoder = FrameDecoder()
    with pytest.raises(LspFrameError) as exc:
        decoder.feed(encode_frame(good) + bad)
    assert exc.value.messages == [good]


def test_messages_kept_when_bad_frame_has_no_content_length():
    good = {"id": 7}
    decoder = FrameDecoder()
    with pytest.raises(LspFrameError) as exc:
        decoder.feed(encode_frame(good) + b"X-Test: 1\r\n\r\n{}")
    assert exc.value.messages == [good]


def test_frame_error_without_prior_messages_has_empty_list():
    err = LspFrameError("坏帧")
    assert err.messages == []
    with pytest.raises(LspFrameError) as exc:
        FrameDecoder().feed(b"Content-Length: -1\r\n\r\n")
    assert exc.value.messages == []
