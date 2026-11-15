"""云中转客户端：注册帧、有界出站队列、序号缓冲。

真正的套接字由调用方注入。本模块不在导入时拨号。

补发只承诺「窗口内去重」：本地 128 帧窗口或 500 条出站队列一旦丢帧，
注册成功后立刻发 relay_lost 告诉网关，让网关给 App 发 resync_required
整态自愈——不能无条件声称不重不漏。
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
from typing import Callable

from core.remote_transport import EventReplayBuffer

OUTBOUND_LIMIT = 500
MIRROR_LIMIT = 128


class CloudRelayClient:
    def __init__(self, send: Callable[[str], None], boot_id: str | None = None):
        self._send = send
        self.boot_id = boot_id or secrets.token_hex(8)
        self.buffer = EventReplayBuffer(MIRROR_LIMIT)
        self.outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_LIMIT)
        self.dropped = 0
        self.lost = 0
        self._loss_reported = 0
        self.generation = 0
        self.connected = False
        self.device_id = ""
        self._replayed_seqs = set()
        self.closed = False
        self.on_rpc = None
        self.on_registered = None
        self._socket = None

    def register_frame(self, token: str, device_id: str) -> dict:
        return {
            "type": "relay_register",
            "token": token,
            "device_id": device_id,
            "boot_id": self.boot_id,
        }

    def submit(self, event_type: str, data) -> None:
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        if len(self.buffer.items) >= MIRROR_LIMIT:
            # 本地窗口已满：再记一帧就会挤掉最旧的，网关侧必然看到 seq 断裂。
            self.lost += 1
        stamped = self.buffer.stamp(payload)
        try:
            self.outbound.put_nowait(stamped)
        except queue.Full:
            self.dropped += 1
            self.lost += 1
            return
        if self.connected:
            self.flush()

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "generation": self.generation,
            "boot_id": self.boot_id,
            "device_id": self.device_id,
            "queued": self.outbound.qsize(),
            "dropped": self.dropped,
            "lost": self.lost,
            "closed": self.closed,
        }

    def report_loss(self) -> bool:
        """丢过帧就上报一次：网关据此让 App 走 resync_required，而不是假装补齐。"""
        if self.lost <= self._loss_reported:
            return False
        dropped = self.lost - self._loss_reported
        self._loss_reported = self.lost
        self._send(json.dumps(
            {
                "type": "relay_lost",
                "data": {
                    "device_id": self.device_id,
                    "boot_id": self.boot_id,
                    "dropped": dropped,
                },
            },
            ensure_ascii=False,
        ))
        return True

    def flush(self) -> None:
        sent = False
        while True:
            try:
                item = self.outbound.get_nowait()
            except queue.Empty:
                if sent and self.buffer.items:
                    # 已发送的旧帧无需在重复注册时重放；保留最新帧做状态对齐。
                    self._replayed_seqs.update(seq for seq, _payload in list(self.buffer.items)[:-1])
                return
            self._send(item)
            sent = True

    def close(self) -> None:
        self.closed = True
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                socket.close()
            except Exception:
                return

    def pump(self, socket, token: str, device_id: str) -> None:
        """在一条已经接通的套接字上注册并读到断开。测试注入套接字，不在这里拨号。"""
        self._socket = socket
        self.device_id = device_id
        stop_ping = threading.Event()

        def heartbeat() -> None:
            while not stop_ping.wait(15):
                try:
                    socket.send(json.dumps({"type": "ping"}))
                except Exception:
                    return

        threading.Thread(target=heartbeat, daemon=True, name="cloud-relay-ping").start()
        try:
            socket.send(json.dumps(self.register_frame(token, device_id), ensure_ascii=False))
            while not self.closed:
                try:
                    raw = socket.recv()
                except Exception:
                    self.connected = False
                    return
                if not raw:
                    self.connected = False
                    return
                frame = json.loads(raw)
                if frame.get("type") == "relay_registered":
                    self.handle_registered(frame)
                    # 丢过帧必须先告诉网关：App 收到 resync_required 才能整态自愈。
                    self.report_loss()
                    self.flush()
                elif frame.get("type") == "ping":
                    socket.send(json.dumps({"type": "pong"}))
                elif frame.get("method") and self.on_rpc is not None:
                    result = self.on_rpc(frame.get("method"), frame.get("args") or {})
                    error = result.get("error") if isinstance(result, dict) else None
                    if isinstance(error, dict):
                        reply = {"id": frame.get("id"), "error": "method not allowed"}
                    else:
                        reply = {"id": frame.get("id"), "result": result}
                    socket.send(json.dumps(reply, ensure_ascii=False))
        finally:
            stop_ping.set()
            self._socket = None
            self.connected = False

    def handle_registered(self, frame: dict) -> None:
        data = frame.get("data") if isinstance(frame, dict) else None
        if isinstance(data, dict):
            self.generation = int(data.get("connection_generation") or 0)
        self.connected = True
        callback = self.on_registered
        if callable(callback):
            try:
                callback()
            except Exception:
                # 状态回调只给界面看，不能因为它打断补发。
                pass
        # 每条序号最多补发一次，避免重复注册时反复重放同一窗口。
        for seq, payload in list(self.buffer.items):
            if seq in self._replayed_seqs:
                continue
            self._send(payload)
            self._replayed_seqs.add(seq)
