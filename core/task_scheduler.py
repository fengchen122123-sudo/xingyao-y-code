"""Dependency-aware execution scheduler for persisted plan items."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import subprocess
import time
import os
from copy import deepcopy
from typing import Any, Callable

from core.plan import PlanManager


TaskRunner = Callable[[dict[str, Any]], Any]
SchedulerCallback = Callable[[str, dict[str, Any]], None]
VerificationRunner = Callable[[str], tuple[bool, str]]


def _reap_process_output(process: subprocess.Popen) -> None:
    """进程树已终止后兜底回收输出；detached 孙进程继承管道句柄时 communicate
    可能永久阻塞，超时后关闭管道放弃读取剩余输出，避免验证线程悬挂。"""
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
    except Exception:
        pass


def _stop_process_tree(process: subprocess.Popen) -> None:
    """先 terminate 整棵进程树，超时后强制 kill 并 wait 回收，避免验证进程泄露。"""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=5)
        except Exception:
            pass


class TaskScheduler:
    def __init__(
        self,
        plan: PlanManager,
        *,
        max_workers: int = 4,
        event_callback: SchedulerCallback | None = None,
        verification_runner: VerificationRunner | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.plan = plan
        self.max_workers = max_workers
        self._event_callback = event_callback
        self._lock = threading.RLock()
        self._in_flight_files: set[str] = set()
        self._in_flight_ids: set[str] = set()
        self._item_callbacks: dict[str, SchedulerCallback] = {}
        self._verification_processes: dict[str, subprocess.Popen] = {}
        self._paused = False
        self._custom_verification_runner = verification_runner
        self._verification_runner = verification_runner or self._run_verification

    @property
    def paused(self) -> bool:
        return self._paused

    def set_event_callback(self, event_callback: SchedulerCallback | None) -> None:
        with self._lock:
            self._event_callback = event_callback

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        self._emit("plan_paused", {})

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._emit("plan_resumed", {})

    def start_ready(self) -> list[dict[str, Any]]:
        pending_events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            if self._paused:
                return []
            for blocked in self._mark_blocked_dependencies():
                pending_events.append(("plan_item_blocked", blocked))
            # 依赖项可能被 plan_update 直接重置为 pending，启动前同步自愈 blocked 项
            self._revive_blocked_items()
            selected: list[dict[str, Any]] = []
            claimed = set(self._in_flight_files)
            for item in self.plan.ready_items():
                files = set(item.get("files", []))
                if files & claimed:
                    continue
                if len(self._in_flight_ids) >= self.max_workers:
                    break
                current = self._start_item_locked(item, claimed=claimed)
                if current is None:
                    continue
                selected.append(current)
                pending_events.append(("plan_item_started", current))
        # 事件统一在锁外派发，避免回调在持锁期间重入
        for kind, event_item in pending_events:
            self._emit_item(kind, event_item)
        return deepcopy(selected)

    def start_item(
        self,
        item_id: str,
        *,
        owner: str | None = None,
        event_callback: SchedulerCallback | None = None,
    ) -> bool:
        """Atomically claim one plan item for a real child-agent execution."""
        pending_events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            if self._paused:
                return False
            for blocked in self._mark_blocked_dependencies():
                pending_events.append(("plan_item_blocked", blocked))
            # 依赖项可能被 plan_update 直接重置为 pending，启动前同步自愈 blocked 项
            self._revive_blocked_items()
            item = next((candidate for candidate in self.plan.snapshot() if candidate["id"] == item_id), None)
            started_item = None
            if (
                item is not None
                and item["status"] == "pending"
                and len(self._in_flight_ids) < self.max_workers
                and not (set(item.get("files", [])) & self._in_flight_files)
            ):
                if event_callback is not None:
                    self._item_callbacks[item_id] = event_callback
                started_item = self._start_item_locked(item, owner=owner)
                if started_item is None:
                    self._item_callbacks.pop(item_id, None)
                else:
                    pending_events.append(("plan_item_started", started_item))
        # 事件统一在锁外派发，避免回调在持锁期间重入
        for kind, event_item in pending_events:
            self._emit_item(kind, event_item)
        return started_item is not None

    def _start_item_locked(
        self,
        item: dict[str, Any],
        *,
        claimed: set[str] | None = None,
        owner: str | None = None,
    ) -> dict[str, Any] | None:
        files = set(item.get("files", []))
        if not self.plan.ready_items() or item["id"] not in {candidate["id"] for candidate in self.plan.ready_items()}:
            return None
        if files & self._in_flight_files:
            return None
        attempts = int(item.get("attempts", 0)) + 1
        if not self.plan.update(
            item["id"],
            status="in_progress",
            owner=owner if owner is not None else item.get("owner", ""),
            attempts=attempts,
            error="",
        ):
            return None
        self._in_flight_ids.add(item["id"])
        self._in_flight_files.update(files)
        if claimed is not None:
            claimed.update(files)
        return next(
            (candidate for candidate in self.plan.snapshot() if candidate["id"] == item["id"]),
            None,
        )

    def run_ready(self, runner: TaskRunner) -> list[str]:
        selected = self.start_ready()
        if not selected:
            return []

        def execute(item: dict[str, Any]) -> tuple[dict[str, Any], Any, BaseException | None]:
            try:
                return item, runner(deepcopy(item)), None
            except BaseException as error:
                return item, None, error

        with ThreadPoolExecutor(max_workers=min(self.max_workers, len(selected))) as pool:
            results = list(pool.map(execute, selected))

        for item, result, error in results:
            if error is not None:
                self.fail(item["id"], str(error))
                continue
            verification_passed = True
            public_result = result
            if isinstance(result, tuple) and len(result) == 2:
                public_result, verification_passed = result
            if verification_passed:
                self.complete(item["id"], str(public_result or ""))
            else:
                self.fail(item["id"], "验证命令未通过")
        return [item["id"] for item in selected]

    def complete(
        self,
        item_id: str,
        result: str = "",
        *,
        verification_passed: bool = True,
        verification_runner: VerificationRunner | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        if not verification_passed:
            self.fail(item_id, "验证命令未通过")
            return False
        item = self._item(item_id)
        if item is None or item.get("status") != "in_progress":
            return False
        verification_type = str(item.get("verification_type", "result") or "result").strip().lower()
        command = str(item.get("verification") or "").strip()
        if verification_type == "command" and command:
            if verification_runner is not None:
                passed, detail = verification_runner(command)
            elif self._custom_verification_runner is not None:
                passed, detail = self._custom_verification_runner(command)
            else:
                passed, detail = self._run_verification(
                    command,
                    item_id=item_id,
                    cancel_event=cancel_event,
                )
            if not passed:
                self.fail(item_id, detail or "验证命令未通过", result=result)
                return False
        updated = self.plan.mark_completed(item_id, result)
        if updated:
            self._release_files(item_id)
            item = self._item(item_id)
            if item is not None:
                self._emit_item("plan_item_completed", item)
            self._revive_blocked_items()
        return updated

    def fail(self, item_id: str, error: str, *, result: str = "") -> bool:
        item = self._item(item_id)
        if item is None or item.get("status") != "in_progress":
            return False
        updated = self.plan.mark_failed(item_id, error, result=result)
        if updated:
            self._release_files(item_id)
            item = self._item(item_id)
            if item is not None:
                self._emit_item("plan_item_failed", item)
        return updated

    def cancel(self, item_id: str, error: str = "任务已取消") -> bool:
        with self._lock:
            process = self._verification_processes.get(item_id)
        if process is not None and process.poll() is None:
            _stop_process_tree(process)
        item = self._item(item_id)
        if item is None or item.get("status") not in {"pending", "in_progress", "blocked"}:
            return False
        updated = self.plan.mark_cancelled(item_id, error)
        if updated:
            self._release_files(item_id)
            current = self._item(item_id)
            if current is not None:
                self._emit_item("plan_item_cancelled", current)
        return updated

    def retry(self, item_id: str) -> bool:
        item = self._item(item_id)
        if item is None:
            return False
        if item["status"] not in {"failed", "blocked", "cancelled"}:
            return False
        return self.plan.update(
            item_id,
            status="pending",
            attempts=int(item.get("attempts", 0)) + 1,
            error="",
            result="",
        )

    def cancel_all(self) -> int:
        cancelled = 0
        for item in self.plan.snapshot():
            if item["status"] in {"pending", "in_progress", "blocked"} and self.cancel(
                item["id"],
                "任务计划已取消",
            ):
                cancelled += 1
        return cancelled

    def shutdown(self) -> None:
        """会话被切换后调用：终止在途验证进程并清空在途记录，不回写已失效的计划。"""
        with self._lock:
            processes = list(self._verification_processes.values())
            self._verification_processes.clear()
            self._in_flight_ids.clear()
            self._in_flight_files.clear()
            self._item_callbacks.clear()
        for process in processes:
            if process.poll() is None:
                _stop_process_tree(process)

    def _mark_blocked_dependencies(self) -> list[dict[str, Any]]:
        newly_blocked: list[dict[str, Any]] = []
        for item in self.plan.blocked_items():
            if self.plan.update(
                item["id"],
                status="blocked",
                error="依赖任务未完成",
            ):
                current = self._item(item["id"])
                if current is not None:
                    newly_blocked.append(current)
        return newly_blocked

    def _revive_blocked_items(self) -> None:
        """依赖转为 completed 后，把仅因失败依赖而 blocked 的项自动降级回 pending。"""
        # 只做一次 snapshot，保证 statuses 字典与迭代的 items 来自同一时刻，
        # 避免两次 snapshot() 之间的竞态导致状态不一致。
        snapshot = self.plan.snapshot()
        statuses = {item["id"]: item["status"] for item in snapshot}
        revived: list[dict[str, Any]] = []
        for item in snapshot:
            if item["status"] != "blocked":
                continue
            if any(
                statuses.get(dependency) in {"failed", "blocked", "cancelled"}
                for dependency in item["depends_on"]
            ):
                continue
            if self.plan.update(item["id"], status="pending", error=""):
                current = self._item(item["id"])
                if current is not None:
                    revived.append(current)
        for current in revived:
            self._emit_item("plan_item_revived", current)

    def _release_files(self, item_id: str) -> None:
        with self._lock:
            item = self._item(item_id)
            if item is not None:
                self._in_flight_files.difference_update(item.get("files", []))
            self._in_flight_ids.discard(item_id)

    def _item(self, item_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.plan.snapshot() if item["id"] == item_id),
            None,
        )

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        callback = self._event_callback
        if callback is not None:
            callback(kind, payload)

    def _emit_item(self, kind: str, item: dict[str, Any]) -> None:
        with self._lock:
            callback = self._item_callbacks.get(item["id"], self._event_callback)
            if kind in {"plan_item_completed", "plan_item_failed", "plan_item_blocked", "plan_item_cancelled"}:
                self._item_callbacks.pop(item["id"], None)
        if callback is not None:
            callback(kind, {"item": deepcopy(item), "items": self.plan.snapshot()})

    def _run_verification(
        self,
        command: str,
        *,
        item_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        from core.process_io import decode_process_output, platform_console_encoding

        output_encoding = platform_console_encoding()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = output_encoding
        # 与 execute_command 的执行环境保持一致
        env["PYTHONUTF8"] = "0"
        try:
            process = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                env=env,
            )
            if item_id:
                with self._lock:
                    self._verification_processes[item_id] = process
            started_at = time.monotonic()
            # 用 communicate 持续排空管道，避免子进程输出超过管道缓冲区后互相死锁
            while True:
                try:
                    stdout_raw, stderr_raw = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        _stop_process_tree(process)
                        _reap_process_output(process)
                        return False, "验证命令已取消"
                    if time.monotonic() - started_at > 120:
                        _stop_process_tree(process)
                        _reap_process_output(process)
                        return False, "验证命令超时（120 秒）"
            stdout = decode_process_output(stdout_raw, output_encoding)
            stderr = decode_process_output(stderr_raw, output_encoding)
        except OSError as error:
            return False, f"验证命令无法启动: {error}"
        finally:
            if item_id:
                with self._lock:
                    self._verification_processes.pop(item_id, None)
        if process.returncode == 0:
            return True, stdout.strip() or "验证通过"
        detail = (stderr or stdout or "验证命令返回非零状态").strip()
        return False, f"验证失败: {detail[-800:]}"


_DEFAULT_SCHEDULER: TaskScheduler | None = None
_DEFAULT_SCHEDULER_SESSION: str | None = None


def get_default_scheduler(
    *,
    event_callback: SchedulerCallback | None = None,
) -> TaskScheduler:
    """Return the process-wide scheduler for the currently bound plan session."""
    from core.plan import get_default_plan_manager

    global _DEFAULT_SCHEDULER, _DEFAULT_SCHEDULER_SESSION
    plan = get_default_plan_manager()
    session_id = plan.session_id
    if _DEFAULT_SCHEDULER is not None and _DEFAULT_SCHEDULER_SESSION != session_id:
        # 会话切换时收尾旧调度器，避免 in-flight 项回调失联；
        # 若旧计划仍有效则 cancel_all，否则只清理在途资源（计划可能已被新会话替换）
        if _DEFAULT_SCHEDULER.plan.session_id == _DEFAULT_SCHEDULER_SESSION:
            _DEFAULT_SCHEDULER.cancel_all()
        else:
            _DEFAULT_SCHEDULER.shutdown()
        _DEFAULT_SCHEDULER = None
    if _DEFAULT_SCHEDULER is None:
        _DEFAULT_SCHEDULER = TaskScheduler(plan, event_callback=event_callback)
        _DEFAULT_SCHEDULER_SESSION = session_id
    elif event_callback is not None:
        _DEFAULT_SCHEDULER.set_event_callback(event_callback)
    return _DEFAULT_SCHEDULER
