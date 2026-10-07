"""DAP 帧编解码测试（规格：2026-10-06-ide-mode-design.md 6.2）。

DAP 与 LSP 同为 Content-Length 帧，但载荷字段不同（seq/type/command/event
对 jsonrpc/id/method）。适配器输出是管道字节流，一次 read 可能拿到半帧
（分片）或多帧（粘包），解析状态必须跨 read 保留。

坏帧语义与 `core/ide/lsp_codec.py` 保持一致（S2-T3 踩过的坑）：异常上挂着
`messages`，同一次 feed 里**已经成功解码**的报文不得随异常一起消失。
"""

import pytest

from core.ide.dap_manager import MAX_FRAME_BYTES, DapFramer, DapProtocolError, encode_message


def _frame(payload: str) -> bytes:
    body = payload.encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def test_encode_message_frames_utf8_body_with_length():
    data = encode_message({"seq": 1, "type": "request", "command": "initialize"})

    header, _, body = data.partition(b"\r\n\r\n")
    assert header == b"Content-Length: %d" % len(body)
    assert body.decode("utf-8") == '{"seq":1,"type":"request","command":"initialize"}'


def test_max_frame_bytes_constant_is_32_mib():
    """常量按计划公开：DapProcess 与适配器侧都按这个上限做防护。"""
    assert MAX_FRAME_BYTES == 32 * 1024 * 1024


def test_feed_parses_one_complete_frame():
    framer = DapFramer()

    messages = framer.feed(_frame('{"seq":1,"type":"response","request_seq":1,"success":true}'))

    assert len(messages) == 1
    assert messages[0]["type"] == "response" and messages[0]["request_seq"] == 1
    assert framer.pending_bytes == 0


def test_feed_parses_two_frames_in_one_chunk():
    """粘包：一次 read 拿到两条完整报文必须都解析出来。"""
    framer = DapFramer()

    messages = framer.feed(
        _frame('{"seq":1,"type":"event","event":"initialized"}')
        + _frame('{"seq":2,"type":"event","event":"terminated"}')
    )

    assert [m["event"] for m in messages] == ["initialized", "terminated"]


def test_feed_byte_by_byte_reassembles_frame():
    """分片：逐个字节喂入，最后一字节到齐才产出报文。"""
    framer = DapFramer()
    data = _frame('{"seq":7,"type":"event","event":"output","body":{"output":"hi\\n"}}')
    messages = []

    for index, byte in enumerate(data):
        messages.extend(framer.feed(bytes([byte])))
        if index < len(data) - 1:
            assert messages == [], "帧未收齐时不得产出报文"
            assert framer.pending_bytes > 0

    assert len(messages) == 1 and messages[0]["body"]["output"] == "hi\n"


def test_header_split_across_chunks():
    framer = DapFramer()

    # 正文 `{"seq":1,"type":"event","e":1}` 是 30 字节：头里的长度必须与它一致，
    # 否则 27 字节会把正文截成非法 JSON（原计划写 27，实测 30，已修正）。
    assert framer.feed(b"Content-Length: 30\r") == []
    assert framer.feed(b"\n\r\n") == []

    messages = framer.feed(b'{"seq":1,"type":"event","e":1}')

    assert len(messages) == 1 and messages[0]["seq"] == 1


def test_body_split_across_chunks_keeps_remainder():
    framer = DapFramer()
    data = _frame('{"seq":3,"type":"response","request_seq":3,"success":true}')
    cut = 20

    assert framer.feed(data[:cut]) == []
    assert framer.pending_bytes == cut

    messages = framer.feed(data[cut:])

    assert len(messages) == 1 and messages[0]["request_seq"] == 3
    assert framer.pending_bytes == 0


def test_chunk_holds_tail_of_a_and_head_of_b():
    """跨帧切片：一个 chunk = A 帧尾部 + B 帧头部，A 必须产出、B 必须留到下一次。"""
    framer = DapFramer()
    a = _frame('{"seq":1,"type":"event","event":"initialized"}')
    b = _frame('{"seq":2,"type":"response","request_seq":1,"success":true}')
    data = a + b
    cut = len(a) - 9  # 落在 A 的正文里

    assert framer.feed(data[:cut]) == []

    # 这一段同时含 A 的尾部（9 字节）与 B 的头部（12 字节）
    messages = framer.feed(data[cut : len(a) + 12])

    assert [m["event"] for m in messages] == ["initialized"]
    assert framer.pending_bytes == 12

    rest = framer.feed(data[len(a) + 12 :])

    assert [m["request_seq"] for m in rest] == [1]
    assert framer.pending_bytes == 0


def test_chunk_holds_tail_of_a_and_complete_b():
    """同一 chunk 里 A 收尾 + B 完整：两条都要产出（顺序不得颠倒）。"""
    framer = DapFramer()
    a = _frame('{"seq":1,"type":"event","event":"initialized"}')
    b = _frame('{"seq":2,"type":"event","event":"terminated"}')
    cut = len(a) - 5

    assert framer.feed(a[:cut]) == []
    messages = framer.feed(a[cut:] + b)

    assert [m["event"] for m in messages] == ["initialized", "terminated"]
    assert framer.pending_bytes == 0


def test_content_length_header_is_case_insensitive_and_extra_headers_ignored():
    framer = DapFramer()
    body = b'{"seq":1,"type":"event","event":"x"}'
    data = b"content-LENGTH: %d\r\nContent-Type: application/vscode-jsonrpc\r\n\r\n" % len(body) + body

    messages = framer.feed(data)

    assert len(messages) == 1 and messages[0]["event"] == "x"


def test_missing_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="Content-Length"):
        framer.feed(b"X-Trace: 1\r\n\r\n{}")


def test_non_integer_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="不是整数"):
        framer.feed(b"Content-Length: abc\r\n\r\n{}")


def test_negative_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="为负"):
        framer.feed(b"Content-Length: -5\r\n\r\n{}")


def test_oversized_frame_rejected_before_buffering_body():
    """畸形/超大 Content-Length 必须在读正文前拒绝，避免内存被撑爆。"""
    framer = DapFramer(max_frame_bytes=64)

    with pytest.raises(DapProtocolError, match="超过上限"):
        framer.feed(b"Content-Length: 65\r\n\r\n")


def test_oversized_header_without_blank_line_rejected():
    """迟迟等不到空行的头同样要拒绝：否则无界缓冲会被一段垃圾头撑爆。"""
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="头"):
        framer.feed(b"X-Pad: " + b"a" * (17 * 1024))


def test_invalid_json_body_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="合法 JSON"):
        framer.feed(_frame("{not json}"))


# --- 坏帧不得连带丢弃同一次 feed 里已解出的报文（S2-T3 语义） ---------------


def test_bad_json_body_keeps_messages_decoded_earlier_in_same_feed():
    """`[合法帧 A][坏帧 B]`：异常必须带上 A，否则上层会一直等一个永不投递的响应。"""
    framer = DapFramer()
    good = _frame('{"seq":1,"type":"response","request_seq":1,"success":true}')

    with pytest.raises(DapProtocolError) as excinfo:
        framer.feed(good + _frame("{oops}"))

    assert [m["request_seq"] for m in excinfo.value.messages] == [1]


def test_bad_header_keeps_messages_decoded_earlier_in_same_feed():
    framer = DapFramer()
    good = _frame('{"seq":1,"type":"event","event":"initialized"}')

    with pytest.raises(DapProtocolError) as excinfo:
        framer.feed(good + b"X-Trace: 1\r\n\r\n{}")

    assert [m["event"] for m in excinfo.value.messages] == ["initialized"]


def test_bad_frame_is_consumed_so_later_frames_still_parse():
    """坏帧本身已从缓冲里摘掉：其余字节流仍可继续解析（调用方自行决定是否放弃连接）。"""
    framer = DapFramer()

    with pytest.raises(DapProtocolError):
        framer.feed(_frame("{oops}") + _frame('{"seq":9,"type":"event","event":"later"}'))

    messages = framer.feed(b"")

    assert [m["event"] for m in messages] == ["later"]
    assert framer.pending_bytes == 0
