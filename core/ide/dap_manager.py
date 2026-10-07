"""DAP（Debug Adapter Protocol）帧编解码与适配器进程托管。

帧格式与 LSP 一致（ASCII 头 + Content-Length + CRLF CRLF + UTF-8 JSON 正文），
但载荷字段完全不同：DAP 用 seq / type(request|response|event) / command|event
/ arguments|body，LSP 用 jsonrpc / id / method / params。因此不复用 LSP 侧代码，
本模块自带解析器。

不过解析**语义**必须与 `core/ide/lsp_codec.py` 一致（S2-T3 踩过的坑）：stdio 是
字节流，一次 read 可能拿到半帧（分片）或多帧（粘包），解析器必须是有状态的累加器；
且一个 chunk 完全可能是 `[合法帧 A][坏帧 B]`，A 很可能正是上层正在等的响应——
坏帧抛错时不得把 A 一起丢掉，因此 `DapProtocolError.messages` 挂着本次 feed 里
已经成功解码的报文，调用方应当先派发它们，再决定是否放弃这条连接。

`Content-Length` 是外部输入，等于攻击面：巨大的数值会让读线程一直等，或者按声明
长度预分配缓冲直接吃爆内存。因此头部长度与单帧正文长度都有硬上限，越界立刻抛
`DapProtocolError`（既不等待也不分配）。

线程模型（本模块所有类都不触碰 WebView）：
- DapProcess 的读线程负责把字节流还原成报文，response 唤醒等待者，event 交给
  on_event 回调；on_event 的实现只允许 emit_event（内部 put_nowait 进 _js_queue）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 帧头分隔符：CRLF CRLF
_HEADER_SEPARATOR = b"\r\n\r\n"
# 帧头上限（16 KB）：迟迟等不到空行的「头」必然是畸形或攻击流量，无界缓冲会被撑爆
_MAX_HEADER_BYTES = 16 * 1024
# 单帧上限：variables 响应可能很大，给 32MB；超过即判协议错，
# 防止畸形 Content-Length 让内存被撑爆
MAX_FRAME_BYTES = 32 * 1024 * 1024


class DapProtocolError(Exception):
    """DAP 帧解析或通道状态异常（头不完整、Content-Length 非法、帧超限、通道关闭）。

    异常上还挂着 `messages`：**抛出本次异常之前，同一次 feed 里已经成功解码的消息**。
    调用方捕获后必须先把 `messages` 派发出去，再决定是否放弃这条连接——否则上层
    会一直等一个永远不会被投递的响应（挂起），而不是看到错误。
    """

    def __init__(self, message, messages=None):
        super().__init__(message)
        self.messages = list(messages or ())


def encode_message(message: Dict[str, Any]) -> bytes:
    """把一条 DAP 报文编码成 Content-Length 帧（UTF-8 载荷，紧分隔符省字节）。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class DapFramer:
    """增量帧解析器：把任意切分的字节流还原成 DAP 报文列表。"""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buffer = bytearray()
        self._max_frame_bytes = max_frame_bytes

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """喂入一段字节，返回本次新增的完整报文（可能为空列表）。

        坏帧抛 `DapProtocolError`，异常上的 `messages` 是本次 feed 里已经解出来的
        报文（见 `DapProtocolError` 说明）。
        """
        if data:
            self._buffer.extend(data)
        messages: List[Dict[str, Any]] = []
        try:
            while True:
                header_end = self._buffer.find(_HEADER_SEPARATOR)
                if header_end < 0:
                    if len(self._buffer) > _MAX_HEADER_BYTES:
                        raise DapProtocolError(
                            f"DAP 帧头超过 {_MAX_HEADER_BYTES} 字节仍未见空行，帧结构异常"
                        )
                    return messages
                if header_end > _MAX_HEADER_BYTES:
                    raise DapProtocolError(
                        f"DAP 帧头超过 {_MAX_HEADER_BYTES} 字节，帧结构异常"
                    )
                length = self._parse_content_length(bytes(self._buffer[:header_end]))
                body_start = header_end + len(_HEADER_SEPARATOR)
                if length > self._max_frame_bytes:
                    # 读到头就拒绝：不等正文、不预分配
                    raise DapProtocolError(f"DAP 帧超过上限：{length} 字节")
                if len(self._buffer) < body_start + length:
                    return messages  # 正文未到齐：保留缓冲，等下一次 feed
                body = bytes(self._buffer[body_start:body_start + length])
                del self._buffer[:body_start + length]
                messages.append(self._decode_body(body))
        except DapProtocolError as exc:
            # 坏帧之前已经解出来的报文不能随异常一起消失（见 DapProtocolError 说明）
            if not exc.messages:
                exc.messages = list(messages)
            raise

    @staticmethod
    def _parse_content_length(header: bytes) -> int:
        for line in header.split(b"\r\n"):
            if not line:
                continue
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError as e:
                    raise DapProtocolError(f"Content-Length 不是整数：{value!r}") from e
                if length < 0:
                    raise DapProtocolError(f"Content-Length 为负：{length}")
                return length
        raise DapProtocolError(f"DAP 帧头缺少 Content-Length：{header[:64]!r}")

    @staticmethod
    def _decode_body(body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DapProtocolError(f"DAP 载荷不是合法 JSON：{e}") from e
        if not isinstance(payload, dict):
            raise DapProtocolError("DAP 载荷必须是 JSON 对象")
        return payload

    @property
    def pending_bytes(self) -> int:
        """缓冲中尚未成帧的字节数（诊断与测试断言用）。"""
        return len(self._buffer)
