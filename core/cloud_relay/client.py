from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core.local_image_inline import inline_local_images_in_payload
from core.remote_transport import EventReplayBuffer

logger = logging.getLogger(__name__)

OUTBOUND_LIMIT = 500
MIRROR_LIMIT = 128
RPC_WORKERS = 8

EPHEMERAL_EVENT_TYPES = frozenset({
    "token_delta", "thinking_delta", "step_metrics", "round_started",
    "subagent_progress", "tool_start", "tool_finish",
    "pty_data", "terminal_output",
    "token_usage", "token_usage_updated",
    "file_tree_loaded", "skills_loaded", "mcp_status",
})

DESKTOP_ONLY_EVENT_TYPES = frozenset({
    "window_maximize_state", "close_requested",
    "bg_convert_progress", "bg_convert_done", "bg_convert_error",
    "update_download_progress", "update_download_complete", "update_download_error",
    "playwright_component_progress", "ffmpeg_component_progress", "search_component_progress",
})


class CloudRelayClient:
    def __init__(self, send: Callable[[str], None], boot_id: str | None = None):
        self._send = send
        self.boot_id = boot_id or secrets.token_hex(8)
        self.buffer = EventReplayBuffer(MIRROR_LIMIT)
        self.outbound: queue.Queue = queue.Queue(maxsize=OUTBOUND_LIMIT)
        self.dropped = 0
        self.lost = 0
        self._loss_reported = 0
        self._last_sent_seq = 0
        self.generation = 0
        self.connected = False
        self.device_id = ""
        self._replayed_seqs = set()
        self.closed = False
        self.on_rpc = None
        self.on_registered = None
        self._socket = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._outbound_wake = threading.Event()
        self._sender_active = False

    def register_frame(self, token: str, device_id: str) -> dict:
        return {
            "type": "relay_register",
            "token": token,
            "device_id": device_id,
            "boot_id": self.boot_id,
        }

    def _emit_wire(self, payload: str) -> bool:
        """线程安全调用底层 send；发送失败时标离线但不把异常抛回事件生产者。"""
        with self._send_lock:
            sender = self._send
            if sender is None:
                return False
            try:
                sender(payload)
                return True
            except Exception:
                logger.debug("云中转出站发送失败", exc_info=True)
                self.connected = False
                return False

    def _socket_send(self, sock, payload: str) -> bool:
        """线程安全写入当前 WebSocket（与 _emit_wire 共用发送锁避免并发交错）。"""
        with self._send_lock:
            try:
                sock.send(payload)
                return True
            except Exception:
                logger.debug("云中转套接字写入失败", exc_info=True)
                return False

    @staticmethod
    def _extract_seq(payload: str) -> int | None:
        if '"seq"' not in payload:
            return None
        try:
            parsed = json.loads(payload)
        except Exception:
            return None
        seq = parsed.get("seq") if isinstance(parsed, dict) else None
        return seq if isinstance(seq, int) else None

    def submit(self, event_type: str, data) -> None:
        if event_type in DESKTOP_ONLY_EVENT_TYPES:
            return
        payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False)
        payload = inline_local_images_in_payload(payload)
        replayable = event_type not in EPHEMERAL_EVENT_TYPES
        with self._state_lock:
            if replayable:
                if len(self.buffer.items) >= MIRROR_LIMIT:
                    oldest_seq = self.buffer.items[0][0]
                    if oldest_seq > self._last_sent_seq:
                        self.lost += 1
                wire_payload = self.buffer.stamp(payload)
            else:
                wire_payload = payload
            try:
                self.outbound.put_nowait(wire_payload)
            except queue.Full:
                self.dropped += 1
                if replayable:
                    self.lost += 1
                return
        if self._sender_active:
            if self.connected:
                self._outbound_wake.set()
        elif self.connected:
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
        return self._emit_wire(json.dumps(
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

    def flush(self) -> None:
        sent_seqs: list[int] = []
        failed = False
        while True:
            try:
                item = self.outbound.get_nowait()
            except queue.Empty:
                break
            if not self._emit_wire(item):
                failed = True
                break
            seq = self._extract_seq(item)
            if seq is not None:
                sent_seqs.append(seq)
        with self._state_lock:
            if sent_seqs:
                self._last_sent_seq = max(self._last_sent_seq, sent_seqs[-1])
            if not failed and self.buffer.items and (sent_seqs or self._last_sent_seq > 0):
                self._replayed_seqs.update(
                    seq for seq, _payload in list(self.buffer.items)[:-1]
                    if seq <= self._last_sent_seq
                )

    def close(self) -> None:
        self.closed = True
        self._outbound_wake.set()
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
        stop_bg = threading.Event()
        self._outbound_wake.clear()
        self._sender_active = True
        rpc_pool = ThreadPoolExecutor(max_workers=RPC_WORKERS, thread_name_prefix="cloud-relay-rpc")

        def heartbeat() -> None:
            while not stop_bg.wait(15):
                if not self._socket_send(socket, json.dumps({"type": "ping"})):
                    return

        def outbound_sender() -> None:
            while not stop_bg.is_set() and not self.closed:
                self._outbound_wake.wait(0.5)
                self._outbound_wake.clear()
                if stop_bg.is_set() or self.closed:
                    break
                if self.connected:
                    self.flush()

        def handle_rpc(rpc_frame: dict) -> None:
            rpc_id = rpc_frame.get("id")
            try:
                result = self.on_rpc(rpc_frame.get("method"), rpc_frame.get("args") or {})
                error = result.get("error") if isinstance(result, dict) else None
                if isinstance(error, dict) and error.get("code") == "method_not_allowed":
                    reply = {"id": rpc_id, "error": "method not allowed"}
                else:
                    reply = {"id": rpc_id, "result": result}
            except Exception:
                logger.exception("云中转 RPC 处理异常: method=%s", rpc_frame.get("method"))
                reply = {"id": rpc_id, "error": "internal error"}
            encoded = inline_local_images_in_payload(json.dumps(reply, ensure_ascii=False))
            self._socket_send(socket, encoded)

        threading.Thread(target=heartbeat, daemon=True, name="cloud-relay-ping").start()
        threading.Thread(target=outbound_sender, daemon=True, name="cloud-relay-outbound").start()
        try:
            if not self._socket_send(
                socket, json.dumps(self.register_frame(token, device_id), ensure_ascii=False)
            ):
                return
            while not self.closed:
                try:
                    raw = socket.recv()
                except Exception:
                    self.connected = False
                    return
                if not raw:
                    self.connected = False
                    return
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "relay_registered":
                    self.handle_registered(frame)
                    # 丢过帧必须先告诉网关：App 收到 resync_required 才能整态自愈。
                    self.report_loss()
                    self.flush()
                    self._outbound_wake.set()
                elif frame.get("type") == "ping":
                    self._socket_send(socket, json.dumps({"type": "pong"}))
                elif frame.get("method") and self.on_rpc is not None:
                    rpc_pool.submit(handle_rpc, frame)
        finally:
            stop_bg.set()
            self._sender_active = False
            self._outbound_wake.set()
            rpc_pool.shutdown(wait=False, cancel_futures=True)
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
        with self._state_lock:
            pending_items = [
                (seq, payload)
                for seq, payload in list(self.buffer.items)
                if seq not in self._replayed_seqs
            ]
        for seq, payload in pending_items:
            if not self._emit_wire(payload):
                break
            with self._state_lock:
                self._replayed_seqs.add(seq)
                self._last_sent_seq = max(self._last_sent_seq, seq)
