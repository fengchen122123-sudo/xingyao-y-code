# -*- coding: utf-8 -*-
"""publishDiagnostics 的节流合批。

为什么必须合批：`evaluate_js` 只能在 gui-js-dispatcher 单线程调用，而语言
服务器在用户连续输入时会按击键频率推送整份文件的诊断（大文件一次几千条），
逐条直推会把前端主线程打满（设计规格 §6.1 的性能红线）。

策略：按文件路径累积「最后一次」的完整诊断列表，窗口期（默认 200 ms）结束后
一次性回调。同时做内容指纹去重：内容与上次已派发结果相同就不再派发，
这样连续击键只会在语义变化时才打扰前端。
"""
from __future__ import annotations

import threading

DEFAULT_THROTTLE_MS = 200


def _fingerprint(diagnostics) -> tuple:
    items = []
    for item in diagnostics or []:
        if not isinstance(item, dict):
            items.append(("?",))
            continue
        rng = item.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        items.append((
            start.get("line"),
            start.get("character"),
            end.get("line"),
            end.get("character"),
            item.get("severity"),
            str(item.get("message") or ""),
            str(item.get("source") or ""),
            str(item.get("code") or ""),
        ))
    return tuple(sorted(items, key=repr))


class DiagnosticsBatcher:
    """按路径合批诊断，窗口期结束或显式 flush 时回调。"""

    def __init__(self, *, interval_ms=None, on_flush=None, timer_factory=None):
        self._interval_ms = DEFAULT_THROTTLE_MS if interval_ms is None else int(interval_ms)
        self._on_flush = on_flush
        self._timer_factory = timer_factory or threading.Timer
        self._lock = threading.Lock()
        self._pending: dict[str, list] = {}
        self._last_flushed: dict[str, tuple] = {}
        self._timer = None
        self.flush_count = 0

    @property
    def interval_ms(self) -> int:
        return self._interval_ms

    def push(self, path: str, diagnostics) -> None:
        with self._lock:
            self._pending[str(path)] = list(diagnostics or [])
            if self._timer is None:
                timer = self._timer_factory(self._interval_ms / 1000.0, self.flush)
                timer.daemon = True
                self._timer = timer
                timer.start()

    def flush(self) -> list:
        """立即冲刷待派发批次（窗口到点自动调用，测试与关闭流程也会显式调用）。"""
        with self._lock:
            pending = self._pending
            self._pending = {}
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        batches = []
        for path, diagnostics in pending.items():
            fingerprint = _fingerprint(diagnostics)
            if self._last_flushed.get(path) == fingerprint:
                continue
            self._last_flushed[path] = fingerprint
            batches.append((path, diagnostics))
        for path, diagnostics in batches:
            self.flush_count += 1
            if self._on_flush is not None:
                self._on_flush(path, diagnostics)
        return batches

    def discard(self, path: str) -> None:
        """文档关闭时丢弃该路径的待派发内容与去重指纹。"""
        key = str(path)
        with self._lock:
            self._pending.pop(key, None)
        self._last_flushed.pop(key, None)
