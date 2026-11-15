"""网关当前打开的 WebSocket。登出只关一条，失效矩阵关整个账号。

失效矩阵靠这里的 reaper 兑现「5 秒内主动关」：每条连接登记时记住
account_id / jti / auth_epoch / role，扫描时逐条回库比对，不依赖
下一条业务帧，也不依赖管理员请求本身。
"""

from __future__ import annotations

import asyncio
import inspect
import time

# 刷新间隔 1 秒，最迟 5 秒关完；规范里的「5 秒内」就是这条。
SWEEP_INTERVAL = 1.0
MAX_LATENCY = 5.0


class SocketTable:
    def __init__(self):
        self._items: list[dict] = []

    def add(self, account_id: str, jti: str, close, *, auth_epoch: int | None = None, role: str = "") -> None:
        self._items.append({
            "account_id": account_id,
            "jti": jti,
            "close": close,
            "auth_epoch": None if auth_epoch is None else int(auth_epoch),
            "role": role,
        })

    def __len__(self) -> int:
        return len(self._items)

    async def _close_matching(self, predicate) -> int:
        kept = []
        closed = []
        for item in self._items:
            if predicate(item):
                closed.append(item["close"])
            else:
                kept.append(item)
        self._items = kept
        for close in closed:
            result = close()
            if inspect.isawaitable(result):
                await result
        return len(closed)

    async def close_jti(self, jti: str) -> int:
        return await self._close_matching(lambda item: item["jti"] == jti)

    async def close_account(self, account_id: str) -> int:
        return await self._close_matching(lambda item: item["account_id"] == account_id)

    async def pop(self, jti: str, default=None):
        await self.close_jti(jti)
        return default

    async def reap(self, validate) -> int:
        """validate(item) 返回 False 表示这条连接已经失效，立刻关掉。"""
        return await self._close_matching(lambda item: not validate(item))


class SessionReaper:
    """按固定间隔扫描连接表。时钟由调用方推进，测试不必真等。"""

    def __init__(self, table: SocketTable, validate, interval: float = SWEEP_INTERVAL, max_latency: float = MAX_LATENCY):
        if interval <= 0:
            raise ValueError("扫描间隔必须大于 0")
        if interval > max_latency:
            raise ValueError("扫描间隔不能大于失效时延上限")
        self.table = table
        self.validate = validate
        self.interval = float(interval)
        self.max_latency = float(max_latency)

    async def run_once(self) -> int:
        return await self.table.reap(self.validate)

    async def run(self, stop: asyncio.Event | None = None, rounds: int | None = None) -> int:
        done = 0
        count = 0
        while True:
            if stop is not None and stop.is_set():
                return done
            done += await self.run_once()
            count += 1
            if rounds is not None and count >= rounds:
                return done
            await asyncio.sleep(self.interval)


def close_with_4401(websocket):
    """失效矩阵统一用 4401：前端先 refresh 再重连，refresh 失效才回登录页。"""

    async def close() -> None:
        try:
            await websocket.close(code=4401)
        except Exception:
            return

    return close


def elapsed_since(start: float | None, now: float | None = None) -> float:
    if not start:
        return 0.0
    return max(0.0, float(now if now is not None else time.time()) - float(start))
