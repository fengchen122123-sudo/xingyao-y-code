"""设备注册表。在线状态只在内存里，库里只留设备和 token 哈希。

本模块同时承担 P4 的协议职责（设计 4.6）：
- 单连接 generation：新注册令旧连接 close 4401，之后的帧一律作废；
- 128 帧镜像 + replay/resync_required：只承诺窗口内去重，溢出/丢帧必须显式要求整态；
- App 名额（MAX_APPS）：按 app_id 占位，断开/登出必须释放，不能泄漏后误报超限；
- RPC id 命名空间：网关只做传输级的 id 前缀化和回程路由，不解包 method 语义。
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
import uuid
from collections import OrderedDict, deque


MAX_APPS = 4
MIRROR_LIMIT = 128
OUTBOUND_LIMIT = 500
OFFLINE_AFTER = 45
# 回程路由表上限：无人应答的 RPC 不能把网关内存吃掉，超出按最旧淘汰。
RPC_ROUTE_LIMIT = 512


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class RelayRegistry:
    def __init__(self):
        self.devices = {}
        self.mirrors = {}
        # device_id -> [{"app_id": <jti>, "key": <连接唯一键>}]
        self.apps = {}
        # 桌面出站队列丢帧的次数（App 取走一次 resync_required 后清零）
        self.dropped = {}
        # 设备侧序号断裂的起点 seq：这一段帧永远补不回来
        self.holes = {}
        # 镜像因 MIRROR_LIMIT 挤出去的最大 seq
        self.overflowed = {}
        # device_id -> OrderedDict{命名空间化后的 id: (app 键, 原 id)}
        self.rpc_routes = {}
        self.listeners = []

    # ---- 设备生命周期 -------------------------------------------------

    def create_device(self, database, account_id: str, name: str) -> tuple[str, str]:
        device_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        database.execute(
            """
            INSERT INTO devices(id, account_id, name, token_hash, revoked, created_at)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
            (device_id, account_id, name, hash_token(token), int(time.time())),
        )
        database.commit()
        return device_id, token

    def _forget(self, device_id: str) -> dict | None:
        """清掉一台设备在内存里的全部痕迹（镜像/名额/丢帧标记/路由表）。"""
        current = self.devices.pop(device_id, None)
        self.mirrors.pop(device_id, None)
        self.apps.pop(device_id, None)
        self.dropped.pop(device_id, None)
        self.holes.pop(device_id, None)
        self.overflowed.pop(device_id, None)
        self.rpc_routes.pop(device_id, None)
        return current

    @staticmethod
    def _close(current: dict | None) -> None:
        closer = current.get("connection") if current else None
        if callable(closer):
            closer()

    def register(self, database, device_id: str, token: str, boot_id: str) -> dict:
        row = database.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        if row is None or int(row["revoked"]) or row["token_hash"] != hash_token(token):
            return {"ok": False, "close": 4401}
        account = database.execute(
            "SELECT disabled FROM accounts WHERE id = ?",
            (row["account_id"],),
        ).fetchone()
        if account is None or int(account["disabled"]):
            # 禁用期间设备连不上来；重新启用后同一枚 token 还能用（矩阵只关不吊销）。
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
        """只断线，不动库里的 token：禁用账号时用这个，不是吊销。"""
        current = self._forget(device_id)
        self._close(current)
        return current is not None

    def revoke(self, database, device_id: str) -> dict | None:
        database.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))
        database.commit()
        current = self._forget(device_id)
        self._close(current)
        return current

    # ---- 在线状态 -----------------------------------------------------

    def mark_offline(self, device_id: str) -> dict | None:
        """套接字自己断了：立刻标离线（比 45 秒扫描更早），并给出要推给 App 的帧。"""
        device = self.devices.get(device_id)
        if device is None or not device.get("online"):
            return None
        device["online"] = False
        device["connection"] = None
        return {"type": "device_status", "data": {"device_id": device_id, "online": False}}

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

    # ---- App 名额 -----------------------------------------------------

    def attach_app(self, device_id: str, app_id: str) -> dict:
        """占一个 App 名额。同一 app_id 重连只换连接键，不重复计数。"""
        bucket = self.apps.setdefault(device_id, [])
        key = uuid.uuid4().hex
        for item in bucket:
            if item["app_id"] == app_id:
                item["key"] = key
                return {"ok": True, "key": key}
        if len(bucket) >= MAX_APPS:
            if not bucket:
                self.apps.pop(device_id, None)
            return {"ok": False, "error": "too_many_apps"}
        bucket.append({"app_id": app_id, "key": key})
        return {"ok": True, "key": key}

    def detach_app(self, device_id: str, key: str) -> bool:
        """断开/登出必须走这里，否则名额泄漏会把第 5 个 App 误判成超限。"""
        bucket = self.apps.get(device_id)
        if not bucket:
            return False
        kept = [item for item in bucket if item["key"] != key]
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

    # ---- 镜像与补发 ---------------------------------------------------

    def remember(self, device_id: str, seq: int, payload: str) -> None:
        """记账设备上行的帧。重复帧丢弃；序号断裂和镜像挤出都留下痕迹供 resync 用。"""
        mirror = self.mirrors.setdefault(device_id, deque(maxlen=MIRROR_LIMIT))
        if mirror:
            last = mirror[-1][0]
            if seq <= last:
                return
            if seq != last + 1:
                # 桌面出站队列满丢过帧：中间这段永远补不回来，只能让 App 整态自愈。
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
            "data": {"device_id": device_id, "boot_id": device.get("boot_id"), "reason": "queue_drop"},
        }

    def _resync(self, device_id: str, boot_id, reason: str) -> dict:
        return {
            "type": "resync_required",
            "data": {"device_id": device_id, "boot_id": boot_id, "reason": reason},
        }

    def frames_for(self, device_id: str, last_seq, last_boot_id: str | None) -> dict:
        """给刚连上的 App 决定「补窗口」还是「要求整态」。

        只承诺窗口内去重：镜像溢出、设备侧序号断裂、出站队列丢帧都必须如实
        要求 resync_required，不能无条件声称不重不漏。
        """
        device = self.devices.get(device_id)
        if device is None:
            return self._resync(device_id, None, "boot")
        boot_id = device["boot_id"]
        # 首连（last_boot_id 为空）只代表「我没见过任何帧」，不是 GUI 重启。
        if last_boot_id and last_boot_id != boot_id:
            return self._resync(device_id, boot_id, "boot")
        if last_seq is not None and self.dropped.get(device_id):
            self.dropped.pop(device_id, None)
            return self._resync(device_id, boot_id, "queue_drop")
        if last_seq is not None and self.holes.get(device_id):
            self.holes.pop(device_id, None)
            return self._resync(device_id, boot_id, "queue_drop")
        mirror = self.mirrors.get(device_id) or deque()
        if not mirror:
            if last_seq is None:
                return {"type": "replay", "frames": []}
            return self._resync(device_id, boot_id, "gap")
        oldest = mirror[0][0]
        if last_seq is not None and oldest > int(last_seq) + 1:
            reason = "overflow" if self.overflowed.get(device_id, -1) > int(last_seq) else "gap"
            return self._resync(device_id, boot_id, reason)
        frames = [payload for seq, payload in mirror if last_seq is None or seq > int(last_seq)]
        return {"type": "replay", "frames": frames}

    # ---- RPC id 命名空间 ----------------------------------------------

    def namespace_rpc(self, device_id: str, app_key: str, rpc_id) -> str:
        """两个 App 各自的 id=1 不能串：网关在传输层加前缀，回程按前缀还原。"""
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

    # ---- 广播 ---------------------------------------------------------

    def listen(self, account_id: str, device_id: str | None, send, app_key: str | None = None) -> tuple:
        item = (account_id, device_id, send, app_key)
        self.listeners.append(item)
        return item

    def unlisten(self, item) -> None:
        if item in self.listeners:
            self.listeners.remove(item)

    def publish(self, account_id: str, device_id: str | None, frame: dict, app_key: str | None = None) -> None:
        """app_key 非空表示定向给某个 App（RPC 回程），否则广播给该设备的所有订阅者。"""
        for listener_account, listener_device, send, listener_key in list(self.listeners):
            if listener_account != account_id:
                continue
            if listener_device not in (None, device_id):
                continue
            if app_key is not None and listener_key != app_key:
                continue
            send(frame)


class DeviceSweeper:
    """按固定间隔扫在线状态。时钟与间隔由调用方给，测试不必真等 45 秒。"""

    def __init__(self, registry: RelayRegistry, publish, interval: float = 5.0, clock=time.time):
        self.registry = registry
        self.publish = publish
        self.interval = float(interval)
        self.clock = clock

    def run_once(self) -> list[dict]:
        events = self.registry.sweep(self.clock())
        for frame in events:
            device = self.registry.devices.get(frame["data"]["device_id"]) or {}
            self.publish(device.get("account_id"), frame["data"]["device_id"], frame)
        return events

    async def run(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            self.run_once()
            await asyncio.sleep(self.interval)
