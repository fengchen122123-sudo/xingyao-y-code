# -*- coding: utf-8 -*-
"""LSP / DAP 共用的 Content-Length 帧编解码。

帧格式：`Content-Length: <十进制字节数>\r\n\r\n<UTF-8 JSON 正文>`，头部允许
携带其它行（如 Content-Type）。

stdio 是字节流，一次 read 可能只拿到半个帧（分片），也可能一次拿到多个帧
（粘包），因此解码器必须是有状态的累加器：绝不能在缺正文时提前消费缓冲区，
否则下一个帧的头部会被当成正文解析。

`Content-Length` 是外部输入，等于攻击面：一个巨大的数值会让解码器无限等待，
或者按声明长度预分配缓冲区直接吃爆内存。因此头部长度与单帧正文长度都有硬上限，
一旦越界立刻抛 `LspFrameError`（协议错），既不等待也不分配。

本模块是纯状态机，不涉及进程与线程，便于用各种切分方式单测覆盖。
"""
from __future__ import annotations

import json

_HEADER_SEP = b"\r\n\r\n"
_MAX_HEADER_BYTES = 16 * 1024
# 单帧正文上限（8 MiB）。依据：8 MiB 正文约合 838 万个 ASCII 字符或约 270 万个
# 中文字符，比任何真实语言服务器消息都高出两个数量级（jedi-language-server 的
# initialize 能力树、补全列表、悬停内容、批量诊断均远小于 1 MiB），既不会误伤
# 正常大响应，又能让 "Content-Length: 999999999999" 这类恶意头在读到头的瞬间
# 就被判为协议错，而不是让读线程一直等下去。
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


class LspFrameError(Exception):
    """帧结构非法（缺 Content-Length、长度非整数、头部超长、正文非 JSON）。

    异常上还挂着 `messages`：**抛出本次异常之前，同一次 feed 里已经成功解码的
    消息**。一个 chunk 完全可能是 `[合法帧 A][坏帧 B]`，而 A 很可能正是
    `initialize` 响应；读循环若因为 B 把 A 一起丢掉，上层就会一直等一个永远
    不会被投递的响应（挂起），而不是看到错误。调用方捕获异常后必须先把
    `messages` 派发出去，再决定是否放弃这条连接。
    """

    def __init__(self, message, messages=None):
        super().__init__(message)
        self.messages = list(messages or ())


def encode_frame(payload: dict, *, encoding: str = "utf-8") -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode(encoding)
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


def _parse_content_length(header: bytes) -> int:
    for line in header.split(b"\r\n"):
        if not line:
            continue
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                length = int(value.strip())
            except ValueError as exc:
                raise LspFrameError("Content-Length 不是整数") from exc
            if length < 0:
                raise LspFrameError("Content-Length 不能为负")
            if length > MAX_PAYLOAD_BYTES:
                raise LspFrameError(
                    f"Content-Length {length} 超过单帧上限 {MAX_PAYLOAD_BYTES}"
                )
            return length
    raise LspFrameError("缺少 Content-Length 头")


class FrameDecoder:
    """有状态字节流解码器：喂入任意切分的字节，产出完整消息。"""

    def __init__(self, *, encoding: str = "utf-8"):
        self._buffer = bytearray()
        self._encoding = encoding

    @property
    def pending_bytes(self) -> int:
        """缓冲区里尚未成帧的字节数，用于排查半截帧。"""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[dict]:
        if chunk:
            self._buffer.extend(chunk)
        messages: list[dict] = []
        try:
            while True:
                sep = self._buffer.find(_HEADER_SEP)
                if sep < 0:
                    if len(self._buffer) > _MAX_HEADER_BYTES:
                        raise LspFrameError("头部超过 16 KB 仍未见空行，帧结构异常")
                    break
                if sep > _MAX_HEADER_BYTES:
                    raise LspFrameError("头部超过 16 KB，帧结构异常")
                length = _parse_content_length(bytes(self._buffer[:sep]))
                body_start = sep + len(_HEADER_SEP)
                total = body_start + length
                if len(self._buffer) < total:
                    # 正文还没到齐，保持原样等待下一次 feed
                    break
                body = bytes(self._buffer[body_start:total])
                del self._buffer[:total]
                try:
                    messages.append(json.loads(body.decode(self._encoding)))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise LspFrameError(f"正文不是合法 JSON：{exc}") from exc
        except LspFrameError as exc:
            # 坏帧之前已经解出来的消息不能随异常一起消失（见 LspFrameError 说明）
            if not exc.messages:
                exc.messages = list(messages)
            raise
        return messages
