"""设备注册表。在线状态只在内存里，库里只留设备和 token 哈希。"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections import deque


MAX_APPS = 4
MIRROR_LIMIT = 128
OUTBOUND_LIMIT = 500
OFFLINE_AFTER = 45


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RelayRegistry:
    def __init__(self):
        self.devices = {}
        self.mirrors = {}
        self.apps = {}
        self.dropped = {}
        self.listeners = []

    def create_device(self, database, account_id: str, name: str) -> tuple[str, str]:
        device_id = str(uuid.uuid4())
        token = __import__("secrets").token_urlsafe(32)
        database.execute(
            """
            INSERT INTO devices(id, account_id, name, token_hash, revoked, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (device_id, account_id, name, hash_token(token), int(time.time())),
        )
        database.commit()
        return device_id, token

    def register(self, database, device_id: str, token: str, boot_id: str) -> dict:
        row = database.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None or int(row["revoked"]) or row["token_hash"] != hash_token(token):
            return {"ok": False, "close": 4401}
        previous = self.devices.get(device_id)
        generation = 1 if previous is None else int(previous["generation"]) + 1
        if previous and previous.get("boot_id") != boot_id:
            self.mirrors[device_id] = deque(maxlen=MIRROR_LIMIT)
            reset = {
                "type": "device_reset",
                "data": {"device_id": device_id, "boot_id": boot_id},
            }
        else:
            self.mirrors.setdefault(device_id, deque(maxlen=MIRROR_LIMIT))
            reset = None
        kicked = previous.get("connection") if previous else None
        self.devices[device_id] = {
            "account_id": row["account_id"],
            "boot_id": boot_id,
            "generation": generation,
            "connection": object(),
            "online": True,
            "last_ping": time.time(),
        }
        database.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ?",
            (int(time.time()), device_id),
        )
        database.commit()
        mirror = self.mirrors[device_id]
        oldest = mirror[0][0] if mirror else None
        newest = mirror[-1][0] if mirror else None
        online = {
            "type": "device_online",
            "data": {
                "device_id": device_id,
                "boot_id": boot_id,
                "connection_generation": generation,
                "oldest_seq": oldest,
                "newest_seq": newest,
            },
        }
        return {
            "ok": True,
            "generation": generation,
            "kicked": kicked,
            "reset": reset,
            "online": online,
            "registered": {
                "type": "relay_registered",
                "data": {"device_id": device_id, "name": row["name"], "connection_generation": generation},
            },
        }

    def revoke(self, database, device_id: str) -> dict | None:
        database.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))
        database.commit()
        current = self.devices.pop(device_id, None)
        self.mirrors.pop(device_id, None)
        closer = current.get("connection") if current else None
        if callable(closer):
            closer()
        return current

    def listen(self, account_id: str, device_id: str | None, send) -> tuple:
        item = (account_id, device_id, send)
        self.listeners.append(item)
        return item

    def unlisten(self, item) -> None:
        if item in self.listeners:
            self.listeners.remove(item)

    def publish(self, account_id: str, device_id: str | None, frame: dict) -> None:
        for listener_account, listener_device, send in list(self.listeners):
            if listener_account != account_id:
                continue
            if listener_device not in (None, device_id):
                continue
            send(frame)

    def attach_app(self, device_id: str, app_id: str) -> dict:
        bucket = self.apps.setdefault(device_id, [])
        if len(bucket) >= MAX_APPS:
            return {"ok": False, "error": "too_many_apps"}
        bucket.append(app_id)
        return {"ok": True}

    def remember(self, device_id: str, seq: int, payload: str) -> None:
        mirror = self.mirrors.setdefault(device_id, deque(maxlen=MIRROR_LIMIT))
        mirror.append((seq, payload))

    def frames_for(self, device_id: str, last_seq, last_boot_id: str | None) -> dict:
        device = self.devices.get(device_id)
        if device is None:
            return {"type": "resync_required", "data": {"device_id": device_id, "boot_id": None, "reason": "boot"}}
        if last_boot_id != device["boot_id"]:
            return {
                "type": "resync_required",
                "data": {"device_id": device_id, "boot_id": device["boot_id"], "reason": "boot"},
            }
        if self.dropped.get(device_id):
            return {
                "type": "resync_required",
                "data": {"device_id": device_id, "boot_id": device["boot_id"], "reason": "queue_drop"},
            }
        mirror = self.mirrors.get(device_id) or deque()
        if not mirror:
            if last_seq is None:
                return {"type": "replay", "frames": []}
            return {
                "type": "resync_required",
                "data": {"device_id": device_id, "boot_id": device["boot_id"], "reason": "gap"},
            }
        oldest = mirror[0][0]
        if last_seq is not None and oldest > int(last_seq) + 1:
            return {
                "type": "resync_required",
                "data": {"device_id": device_id, "boot_id": device["boot_id"], "reason": "gap"},
            }
        frames = [payload for seq, payload in mirror if last_seq is None or seq > int(last_seq)]
        return {"type": "replay", "frames": frames}

    def touch(self, device_id: str, now: float) -> None:
        device = self.devices.get(device_id)
        if device is not None:
            device["last_ping"] = now
            device["online"] = True

    def sweep(self, now: float) -> list[dict]:
        """45 秒没有心跳就标离线。15 秒是客户端的 ping 间隔，不在这里轮询。"""
        events = []
        for device_id, device in self.devices.items():
            if not device.get("online"):
                continue
            if now - float(device.get("last_ping") or 0) > OFFLINE_AFTER:
                device["online"] = False
                events.append({
                    "type": "device_status",
                    "data": {"device_id": device_id, "online": False},
                })
        return events

    def enqueue(self, device_id: str) -> bool:
        count = self.dropped.get(device_id, 0)
        # 调用方在放不进 500 队列时走这里。
        self.dropped[device_id] = count + 1
        return False
