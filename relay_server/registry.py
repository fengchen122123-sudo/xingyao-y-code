from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, deque


MAX_APPS = 4
MIRROR_LIMIT = 128
OUTBOUND_LIMIT = 500
OFFLINE_AFTER = 45
# 回程路由表上限：无人应答的 RPC 不能把服务器内存吃掉，超出按最旧淘汰。
RPC_ROUTE_LIMIT = 512


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DeviceAlreadyEnrolledError(ValueError):
    """同一设备编码重复申请令牌时抛出，避免未授权覆盖已有设备凭据。"""


class RelayRegistry:
    def __init__(self):
        self.devices = {}
        self.mirrors = {}
        self.apps = {}
        self.dropped = {}
        self.holes = {}
        self.overflowed = {}
        self.rpc_routes = {}
        self.listeners = []
        self.observers = []
        self._db_lock = threading.Lock()
        # (device_id, app_key) -> closer：签发新票时要能主动断开已建立的远端连接
        self.app_closers = {}

    def create_device(self, database, name: str) -> tuple[str, str]:
        device_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        database.execute(
            """
            INSERT INTO devices(id, name, token_hash, revoked, created_at)
            VALUES (?, ?, ?, 0, ?)
            """,
            (device_id, name, hash_token(token), int(time.time())),
        )
        database.commit()
        return device_id, token

    def create_mcp_token(
        self, database, device_id: str, name: str = "default"
    ) -> tuple[str, str]:
        """为已注册设备签发长期有效的 MCP / Skill 访问令牌，明文只返回一次。"""
        row = database.execute("SELECT id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            raise ValueError(f"设备不存在: {device_id}")
        token_id = uuid.uuid4().hex[:16]
        raw_token = "xym_" + secrets.token_urlsafe(32)
        display_name = str(name or "").strip()[:64] or "default"
        database.execute(
            """
            INSERT INTO mcp_tokens(id, device_id, name, token_hash, revoked, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (token_id, device_id, display_name, hash_token(raw_token), int(time.time())),
        )
        database.commit()
        return token_id, raw_token

    def revoke_mcp_token(self, database, token_id: str) -> bool:
        cur = database.execute(
            "UPDATE mcp_tokens SET revoked = 1 WHERE id = ?",
            (str(token_id or "").strip(),),
        )
        database.commit()
        return cur.rowcount > 0

    def authenticate_mcp_token(
        self, database, raw_token: str, expected_device_id: str | None = None
    ) -> str | None:
        """校验 MCP / Skill 访问令牌并解析归属 device_id（设备或令牌被吊销均拒绝）。"""
        token = str(raw_token or "").strip()
        if not token:
            return None
        digest = hash_token(token)
        with self._db_lock:
            row = database.execute(
                """
                SELECT m.id AS token_id, m.device_id AS device_id, m.token_hash AS token_hash,
                       m.revoked AS token_revoked, m.last_used AS last_used,
                       d.revoked AS device_revoked
                FROM mcp_tokens m
                JOIN devices d ON d.id = m.device_id
                WHERE m.token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if row is None:
                return None
            if int(row["token_revoked"]) or int(row["device_revoked"]):
                return None
            if not secrets.compare_digest(str(row["token_hash"]), digest):
                return None
            device_id = str(row["device_id"])
            if expected_device_id is not None and device_id != expected_device_id:
                return None
            now = int(time.time())
            last_used = int(row["last_used"] or 0)
            if now - last_used >= 60:
                database.execute(
                    "UPDATE mcp_tokens SET last_used = ? WHERE id = ?",
                    (now, str(row["token_id"])),
                )
                database.commit()
            return device_id

    def enroll_device(
        self, database, device_code: str, name: str | None = None
    ) -> tuple[str, str]:
        """按客户端设备编码签发独立设备凭据（不同设备不同 device_id 与 token）。"""
        code = str(device_code or "").strip()
        if not code or len(code) > 128:
            raise ValueError("device_code 不能为空且长度不能超过 128")
        existing = database.execute(
            "SELECT id FROM devices WHERE device_code = ?", (code,)
        ).fetchone()
        if existing is not None:
            raise DeviceAlreadyEnrolledError("该设备编码已注册")
        display_name = str(name or "").strip()[:64] or code[:64]
        device_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        try:
            database.execute(
                """
                INSERT INTO devices(id, name, token_hash, revoked, created_at, device_code)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (device_id, display_name, hash_token(token), int(time.time()), code),
            )
            database.commit()
        except sqlite3.IntegrityError as exc:
            database.rollback()
            raise DeviceAlreadyEnrolledError("该设备编码已注册") from exc
        return device_id, token

    def _forget(self, device_id: str) -> dict | None:
        current = self.devices.pop(device_id, None)
        self.mirrors.pop(device_id, None)
        self.apps.pop(device_id, None)
        self.dropped.pop(device_id, None)
        self.holes.pop(device_id, None)
        self.overflowed.pop(device_id, None)
        self.rpc_routes.pop(device_id, None)
        self._close_apps(device_id)
        return current

    def _close_apps(self, device_id: str) -> int:
        keys = [key for (dev, key) in list(self.app_closers) if dev == device_id]
        for key in keys:
            closer = self.app_closers.pop((device_id, key), None)
            if callable(closer):
                closer()
        return len(keys)

    def kick_apps(self, device_id: str) -> int:
        """踢掉该设备所有已建立的远端连接，并回收 App 名额。

        刷新会换新 jti，旧 jti 占的名额若不回收，连刷几次就会把 MAX_APPS 占满，
        新二维码反而连不上。
        """
        self.apps.pop(device_id, None)
        return self._close_apps(device_id)

    def bind_app_closer(self, device_id: str, app_key: str, closer) -> None:
        self.app_closers[(device_id, app_key)] = closer

    def unbind_app_closer(self, device_id: str, app_key: str) -> None:
        self.app_closers.pop((device_id, app_key), None)

    @staticmethod
    def _close(current: dict | None) -> None:
        closer = current.get("connection") if current else None
        if callable(closer):
            closer()

    def register(self, database, device_id: str, token: str, boot_id: str) -> dict:
        row = database.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None or int(row["revoked"]) or not secrets.compare_digest(
            str(row["token_hash"]), hash_token(token)
        ):
            return {"ok": False, "close": 4401}
        previous = self.devices.get(device_id)
        generation = 1 if previous is None else int(previous["generation"]) + 1
        boot_changed = bool(previous) and previous.get("boot_id") != boot_id
        if boot_changed:
            # GUI 重启换了 boot_id：旧镜像里的 seq 与新进程毫无关系，必须整态重来。
            self.mirrors[device_id] = deque(maxlen=MIRROR_LIMIT)
            self.holes.pop(device_id, None)
            self.overflowed.pop(device_id, None)
            self.dropped.pop(device_id, None)
            reset_frame = {
                "type": "device_reset",
                "data": {"device_id": device_id, "boot_id": boot_id},
            }
        else:
            self.mirrors.setdefault(device_id, deque(maxlen=MIRROR_LIMIT))
            reset_frame = None
        kicked_closer = previous.get("connection") if previous else None
        was_online = bool(previous) and bool(previous.get("online"))
        self.devices[device_id] = {
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
        online_transition = (not was_online) or boot_changed
        online_frame = (
            {
                "type": "device_online",
                "data": {
                    "device_id": device_id,
                    "boot_id": boot_id,
                    "connection_generation": generation,
                    "oldest_seq": oldest,
                    "newest_seq": newest,
                },
            }
            if online_transition
            else None
        )
        return {
            "ok": True,
            "generation": generation,
            "kicked": bool(kicked_closer),
            "kicked_closer": kicked_closer,
            "reset": boot_changed,
            "reset_frame": reset_frame,
            "online": online_transition,
            "online_frame": online_frame,
            "registered": {
                "type": "relay_registered",
                "data": {
                    "device_id": device_id,
                    "name": row["name"],
                    "connection_generation": generation,
                },
            },
        }

    def rotate(self, database, device_id: str) -> str | None:
        row = database.execute("SELECT id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None:
            return None
        token = secrets.token_urlsafe(32)
        database.execute(
            "UPDATE devices SET token_hash = ?, revoked = 0 WHERE id = ?",
            (hash_token(token), device_id),
        )
        database.commit()
        self._close(self._forget(device_id))
        return token

    def disconnect(self, device_id: str) -> bool:
        """只断线，不动库里的 token。"""
        current = self._forget(device_id)
        self._close(current)
        return current is not None

    def revoke(self, database, device_id: str) -> dict | None:
        database.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))
        database.commit()
        current = self._forget(device_id)
        self._close(current)
        return current

    def mark_offline(self, device_id: str) -> dict | None:
        """套接字自己断了：立刻标离线（比 45 秒扫描更早）并广播 device_status。"""
        device = self.devices.get(device_id)
        if device is None or not device.get("online"):
            return None
        device["online"] = False
        device["connection"] = None
        device["send"] = None
        frame = {"type": "device_status", "data": {"device_id": device_id, "online": False}}
        self.publish(device_id, frame)
        return frame

    def touch(self, device_id: str, now: float | None = None) -> None:
        device = self.devices.get(device_id)
        if device is not None:
            device["last_ping"] = time.time() if now is None else now
            device["online"] = True

    def sweep(self, now: float) -> list[dict]:
        """45 秒没有心跳就标离线并广播。15 秒是客户端的 ping 间隔，不在这里轮询。"""
        events = []
        for device_id, device in self.devices.items():
            if not device.get("online"):
                continue
            if now - float(device.get("last_ping") or 0) > OFFLINE_AFTER:
                device["online"] = False
                closer = device.get("connection")
                device["connection"] = None
                device["send"] = None
                if callable(closer):
                    closer()
                frame = {
                    "type": "device_status",
                    "data": {"device_id": device_id, "online": False},
                }
                events.append(frame)
                self.publish(device_id, frame)
        return events

    def attach_app(self, device_id: str, app_id: str) -> dict:
        """占一个 App 名额，每个连接拿独立 key。

        单活 token 下同一张票可能同时开多个页面（手机 + 平板），它们共享
        app_id 但必须各自持有自己的 key，否则 RPC 回程会串到另一个连接上。
        """
        bucket = self.apps.setdefault(device_id, [])
        if len(bucket) >= MAX_APPS:
            if not bucket:
                self.apps.pop(device_id, None)
            return {"ok": False, "error": "too_many_apps"}
        key = uuid.uuid4().hex
        bucket.append({"app_id": app_id, "key": key})
        return {"ok": True, "key": key}

    def detach_app(self, device_id: str, key: str) -> bool:
        """断开/登出必须走这里，否则名额泄漏会把第 5 个 App 误判成超限。"""
        bucket = self.apps.get(device_id)
        if not bucket:
            return False
        kept = [
            item for item in bucket
            if item["key"] != key and key not in item.get("retired_keys", ())
        ]
        removed = len(kept) != len(bucket)
        if kept:
            self.apps[device_id] = kept
        else:
            self.apps.pop(device_id, None)
        return removed

    def app_count(self, device_id: str) -> int:
        return len(self.apps.get(device_id) or [])

    def app_keys(self, device_id: str) -> list[str]:
        return [item["key"] for item in (self.apps.get(device_id) or [])]

    def remember(self, device_id: str, seq: int, payload: str) -> None:
        """记账设备上行的帧。重复帧丢弃；序号断裂和镜像挤出都留下痕迹供 resync 用。"""
        mirror = self.mirrors.setdefault(device_id, deque(maxlen=MIRROR_LIMIT))
        if mirror:
            last = mirror[-1][0]
            if seq <= last:
                return
            if seq != last + 1:
                self.holes[device_id] = max(self.holes.get(device_id, last), last)
        if len(mirror) == mirror.maxlen:
            evicted = mirror[0][0]
            self.overflowed[device_id] = max(self.overflowed.get(device_id, evicted), evicted)
        mirror.append((seq, payload))

    def enqueue(self, device_id: str) -> dict | None:
        """出站 500 队列放不进帧时调用：计数并给出要让 App 整态自愈的那一帧。"""
        self.dropped[device_id] = self.dropped.get(device_id, 0) + 1
        device = self.devices.get(device_id)
        if device is None:
            return None
        return {
            "type": "resync_required",
            "data": {
                "device_id": device_id,
                "boot_id": device.get("boot_id"),
                "reason": "queue_drop",
            },
        }

    def _resync(self, device_id: str, boot_id, reason: str) -> dict:
        return {
            "type": "resync_required",
            "data": {"device_id": device_id, "boot_id": boot_id, "reason": reason},
        }

    def frames_for(self, device_id: str, last_seq, last_boot_id: str | None) -> dict:
        device = self.devices.get(device_id)
        first = last_seq is None or int(last_seq) <= 0
        if device is None:
            if first and not last_boot_id:
                return {"type": "replay", "frames": []}
            return self._resync(device_id, None, "boot")
        boot_id = device["boot_id"]
        if first and not last_boot_id:
            return {"type": "replay", "frames": []}
        if last_boot_id and last_boot_id != boot_id:
            return self._resync(device_id, boot_id, "boot")
        if not first and self.dropped.get(device_id):
            self.dropped.pop(device_id, None)
            return self._resync(device_id, boot_id, "queue_drop")
        if not first and self.holes.get(device_id):
            self.holes.pop(device_id, None)
            return self._resync(device_id, boot_id, "gap")
        mirror = self.mirrors.get(device_id) or deque()
        if not mirror:
            if first:
                return {"type": "replay", "frames": []}
            return self._resync(device_id, boot_id, "gap")
        oldest = mirror[0][0]
        if not first and oldest > int(last_seq) + 1:
            reason = "overflow" if self.overflowed.get(device_id, -1) > int(last_seq) else "gap"
            return self._resync(device_id, boot_id, reason)
        frames = [payload for seq, payload in mirror if first or seq > int(last_seq)]
        return {"type": "replay", "frames": frames}

    def namespace_rpc(self, device_id: str, app_key: str, rpc_id) -> str:
        """两个 App 各自的 id=1 不能串：在传输层加前缀，回程按前缀还原。"""
        namespaced = f"{app_key}:{rpc_id}"
        routes = self.rpc_routes.setdefault(device_id, OrderedDict())
        routes[namespaced] = (app_key, rpc_id)
        while len(routes) > RPC_ROUTE_LIMIT:
            routes.popitem(last=False)
        return namespaced

    def resolve_rpc(self, device_id: str, namespaced) -> tuple | None:
        """回程路由一次性：应答到达即释放，不会无限长。"""
        routes = self.rpc_routes.get(device_id)
        if not routes or not isinstance(namespaced, str):
            return None
        return routes.pop(namespaced, None)

    def listen(self, device_id: str, send, app_key: str | None = None) -> tuple:
        item = (device_id, send, app_key)
        self.listeners.append(item)
        return item

    def unlisten(self, item) -> None:
        if item in self.listeners:
            self.listeners.remove(item)

    def add_observer(self, fn) -> None:
        if fn not in self.observers:
            self.observers.append(fn)

    def remove_observer(self, fn) -> None:
        if fn in self.observers:
            self.observers.remove(fn)

    def publish(self, device_id: str, frame: dict, app_key: str | None = None) -> None:
        """app_key 非空表示定向给某个 App（RPC 回程），否则广播给该设备的所有订阅者。"""
        if app_key is None:
            for obs in list(self.observers):
                try:
                    obs(device_id, frame)
                except Exception:
                    pass
        for listener_device, send, listener_key in list(self.listeners):
            if listener_device != device_id:
                continue
            if app_key is not None and listener_key != app_key:
                continue
            send(frame)


class DeviceSweeper:
    """按固定间隔扫在线状态。时钟与间隔由调用方给，测试不必真等 45 秒。

    sweep 自身已向订阅者广播 device_status；publish 参数保留构造兼容。
    """

    def __init__(self, registry: RelayRegistry, publish, interval: float = 5.0, clock=time.time):
        self.registry = registry
        self.publish = publish
        self.interval = float(interval)
        self.clock = clock

    def run_once(self) -> list[dict]:
        return self.registry.sweep(self.clock())

    async def run(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            self.run_once()
            await asyncio.sleep(self.interval)
