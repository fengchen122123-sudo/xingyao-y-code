"""云中转客户端：注册帧、有界出站队列、序号缓冲。

真正的套接字由调用方注入。本模块不在导入时拨号。
"""

from __future__ import annotations

import json
import queue
import secrets
from typing import Callable

from core.remote_transport import EventReplayBuffer

OUTBOUND_LIMIT = 500


class CloudRelayClient:
    def __init__(self, send: Callable[[str], None], boot_id: str | None = None):
        self._send = send
        self.boot_id = boot_id or secrets.token_hex(8)
        self.buffer = EventReplayBuffer(128)
        self.outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_LIMIT)
        self.dropped = 0
        self.generation = 0
        self.connected = False

    def register_frame(self, token: str, device_id: str) -> dict:
        return {
            "type": "relay_register",
            "token": token,
            "device_id": device_id,
            "boot_id": self.boot_id,
        }

    def submit(self, event_type: str, data) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        stamped = self.buffer.stamp(payload)
        try:
            self.outbound.put_nowait(stamped)
        except queue.Full:
            self.dropped += 1

    def flush(self) -> None:
        while True:
            try:
                item = self.outbound.get_nowait()
            except queue.Empty:
                return
            self._send(item)

    def handle_registered(self, frame: dict) -> None:
        data = frame.get("data") if isinstance(frame, dict) else None
        if isinstance(data, dict):
            self.generation = int(data.get("connection_generation") or 0)
        self.connected = True
        # 重连成功后先把窗口里还在的帧送出去。
        for _seq, payload in list(self.buffer.items):
            self._send(payload)
