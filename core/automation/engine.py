"""星轨自动化任务：GUI 常驻调度引擎。

职责边界（设计规格 5.3 / 4.6）：
- 只做「读钟 → 原子认领 → 交给分发回调」，绝不发起 LLM 请求、绝不触碰 JS 桥；
- 单任务分发异常隔离：该运行记 error，引擎继续服务其他任务；
- 认领整体异常（如数据库抖动）只记日志，本轮跳过，不停摆；
- 线程 daemon、名 automation-engine；start/stop 幂等；notify_changed 即时唤醒。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from core.automation.scheduling import compute_next_run

logger = logging.getLogger(__name__)

DispatchCallback = Callable[[dict, str, str], None]


class AutomationEngine:
    """调度引擎：持有 storage，按 next_run_at 认领到期任务并交给 dispatch 回调。"""

    def __init__(
        self,
        storage,
        dispatch: DispatchCallback,
        *,
        time_fn: Callable[[], float] = time.time,
        poll_seconds: float = 30.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._storage = storage
        self._dispatch = dispatch
        self._time_fn = time_fn
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first_iteration = True

    # -- 生命周期 --

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._first_iteration = True
        self._thread = threading.Thread(
            target=self._loop, name="automation-engine", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                # join 超时：保留线程引用，start() 据此拒绝另起新线程（避免
                # 短暂双引擎）；旧线程观察到 _stop 后会自行退出（P2-5）
                logger.warning("自动化引擎线程 %s 退出超时，保留引用等待自然结束", thread.name)
                return
        self._thread = None

    def notify_changed(self) -> None:
        """任务增删改/启停/手动执行后调用，让变更绕过轮询间隔即时生效。"""
        self._wake.set()

    # -- 主循环 --

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self._time_fn()
            self._run_iteration()
            timeout = self._next_wake_timeout(now)
            self._wake.wait(timeout)
            self._wake.clear()

    def _run_iteration(self) -> None:
        """单轮认领+分发。独立方法便于测试同步调用，不依赖线程时序。"""
        include_first = self._first_iteration
        self._first_iteration = False
        try:
            claimed = self._storage.claim_due_tasks(
                self._time_fn(),
                compute_next_run,
                include_startup=include_first,
                catchup_enabled=include_first,
            )
        except Exception as exc:
            logger.error("自动化任务认领失败，本轮跳过: %s", exc)
            return
        for task, run_id, trigger_kind in claimed:
            try:
                self._dispatch(task, run_id, trigger_kind)
            except Exception as exc:
                logger.error(
                    "自动化任务「%s」(%s) 分发失败: %s", task.get("name"), run_id, exc
                )
                try:
                    self._storage.finish_run(run_id, "error", f"分发失败: {exc}")
                except Exception:
                    logger.exception("自动化运行 %s 错误终态写入失败", run_id)

    def _next_wake_timeout(self, now: float) -> float:
        """取「最近的 next_run_at」与轮询间隔的较小者，夹在 [0, poll_seconds]。"""
        try:
            earliest = self._storage.earliest_next_run()
        except Exception:
            return self._poll_seconds
        if earliest is None:
            return self._poll_seconds
        return max(0.0, min(earliest - now, self._poll_seconds))
