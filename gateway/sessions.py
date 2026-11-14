"""网关当前打开的 WebSocket。登出只关一条，失效矩阵关整个账号。"""

from __future__ import annotations


class SocketTable:
    def __init__(self):
        self._items: list[tuple[str, str, object]] = []

    def add(self, account_id: str, jti: str, close) -> None:
        self._items.append((account_id, jti, close))

    def close_jti(self, jti: str) -> int:
        kept = []
        closed = 0
        for account_id, item_jti, close in self._items:
            if item_jti == jti:
                close()
                closed += 1
            else:
                kept.append((account_id, item_jti, close))
        self._items = kept
        return closed

    def close_account(self, account_id: str) -> int:
        kept = []
        closed = 0
        for item_account, jti, close in self._items:
            if item_account == account_id:
                close()
                closed += 1
            else:
                kept.append((item_account, jti, close))
        self._items = kept
        return closed

    def pop(self, jti: str, default=None):
        self.close_jti(jti)
        return default
