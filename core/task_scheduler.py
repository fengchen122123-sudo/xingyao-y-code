"""Dependency-aware execution scheduler for persisted plan items."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import subprocess
import time
import os
import signal
from copy import deepcopy
from typing import Any, Callable

from core.plan import PlanManager

_LOGGER = logging.getLogger(__name__)


TaskRunner = Callable[[dict[str, Any]], Any]
SchedulerCallback = Callable[[str, dict[str, Any]], None]
VerificationRunner = Callable[[str], tuple[bool, str]]


def takeover_handoff_payload(item: dict[str, Any]) -> dict[str, Any]:
    """构造 GUI/CLI 共用的接管事件载荷。"""
    event_item = deepcopy(item)
    return {
        "item": event_item,
        "source_task_id": str(event_item.get("handoff_task_id", "")),
        "mode": str(event_item.get("execution_mode", "")),
        "queue_position": int(event_item.get("queue_position", 0) or 0),
    }


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
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
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
        # 与 core/tools.py 的 _MAX_CONCURRENT_SUBAGENTS 对齐：调度上限低于
        # 子代理并发上限时，模型按并发上限启动的最后一批认领会永远失败
        max_workers: int = 5,
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
        # 按 item_id 记录认领时的文件快照：释放必须从同一快照进行，
        # 绝不读取可能已被修改的计划字段猜测应释放哪些文件
        self._in_flight_files_by_item: dict[str, frozenset[str]] = {}
        self._item_callbacks: dict[str, SchedulerCallback] = {}
        self._verification_processes: dict[str, subprocess.Popen] = {}
        self._paused = False
        self._custom_verification_runner = verification_runner
        # 记录创建时计划绑定的会话：shutdown 仅在会话未切换时回写在途项，
        # 避免误改 bind_session 后新会话的同名计划项
        self._plan_session_id = plan.session_id
        self._reset_orphaned_in_progress()

    def _reset_orphaned_in_progress(self) -> None:
        """新调度器接管计划时兜底复位遗留 in_progress 项。

        新实例的在途集合必然为空，此时任何 in_progress 项都是上一任调度器
        异常退出（未走 shutdown/cancel_all）留下的孤儿：ready_items 不会选中它，
        retry 也拒绝从 in_progress 恢复，不复位就会永久卡死。"""
        for item in self.plan.snapshot():
            if item.get("status") != "in_progress":
                continue
            mode = str(item.get("execution_mode", ""))
            takeover_required = bool(item.get("takeover_required", False)) or mode in {
                "main_takeover",
                "takeover_queued",
            }
            self.plan.transition_execution(
                item["id"],
                expected_status="in_progress",
                expected_mode=mode,
                status="pending",
                execution_mode="takeover_queued" if takeover_required else "",
                owner="",
                handoff_task_id=(
                    str(item.get("handoff_task_id", "")) if takeover_required else ""
                ),
                handoff_reason=(
                    str(item.get("handoff_reason", "")) if takeover_required else ""
                ),
                takeover_turn_id="",
                takeover_sequence=(
                    int(item.get("takeover_sequence", 0) or 0)
                    if takeover_required
                    else 0
                ),
                takeover_required=takeover_required,
                error="调度器重建后已恢复为待执行",
            )

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
            # ready_items 只取一次快照，整轮认领基于同一时刻判定，
            # 避免每个候选项重复 O(n) 查询且两次快照间被其他线程改写
            ready = self.plan.ready_items()
            ready_ids = {candidate["id"] for candidate in ready}
            for item in ready:
                files = set(item.get("files", []))
                if files & claimed:
                    continue
                if len(self._in_flight_ids) >= self.max_workers:
                    break
                current = self._start_item_locked(item, claimed=claimed, ready_ids=ready_ids)
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

    def _files_available_locked(self, item_id: str, files) -> bool:
        requested = frozenset(str(path) for path in files)
        current = self._in_flight_files_by_item.get(item_id)
        if current is not None:
            return current == requested
        return not bool(requested & self._in_flight_files)

    def _claim_files_locked(self, item_id: str, files) -> bool:
        requested = frozenset(str(path) for path in files)
        if not self._files_available_locked(item_id, requested):
            return False
        current = self._in_flight_files_by_item.get(item_id)
        if current is None:
            self._in_flight_files_by_item[item_id] = requested
            self._in_flight_ids.add(item_id)
            self._in_flight_files.update(requested)
        return True

    def _start_item_locked(
        self,
        item: dict[str, Any],
        *,
        claimed: set[str] | None = None,
        owner: str | None = None,
        ready_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        files = set(item.get("files", []))
        # 调用方未提供共享快照时才自行查询，且只查一次（旧实现连续调用两次
        # ready_items，两次快照之间可能被其他线程改写导致判定不一致）
        if ready_ids is None:
            ready_ids = {candidate["id"] for candidate in self.plan.ready_items()}
        if item["id"] not in ready_ids:
            return None
        if not self._files_available_locked(item["id"], files):
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
        if not self._claim_files_locked(item["id"], files):
            raise RuntimeError(
                f"计划项 {item['id']} 状态已推进但文件租约认领失败，调度不变量被破坏"
            )
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

    def _report_writeback_rejected(
        self, item_id: str, action: str, item: dict[str, Any] | None
    ) -> None:
        """完成/失败回写被拒时显式上报，不再静默 return False。

        回写只认 in_progress。子代理回调晚于回合结束（项已被降级为
        pending/takeover_queued）时，静默丢弃会让"任务已干完、状态却一直不动"
        没有任何可观测线索，因此这里同时写日志并发 plan_writeback_rejected 事件，
        由 CLI/GUI 提示用户。
        """
        if item is None:
            reason = f"{action} 回写被拒绝：计划项不存在。"
        else:
            reason = (
                f"{action} 回写被拒绝：计划项当前状态为"
                f" '{item.get('status')}'"
                f"（execution_mode='{item.get('execution_mode') or 'none'}'），"
                "只有 in_progress 状态才允许回写。"
            )
        _LOGGER.warning("计划项 %s %s", item_id, reason)
        # 事件名不带 plan_item_ 前缀：CLI 把该前缀一律按"计划项状态变化"渲染，
        # 复用会把该项显示成它原本的 pending 状态，反而误导用户
        self._emit("plan_writeback_rejected", {
            "item_id": str(item_id),
            "action": str(action),
            "reason": reason,
        })

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
            self._report_writeback_rejected(item_id, "complete", item)
            return False
        verification_type = str(item.get("verification_type", "result") or "result").strip().lower()
        command = str(item.get("verification") or "").strip()
        if verification_type == "command" and command:
            if verification_runner is not None:
                passed, detail = verification_runner(command)
            elif self._custom_verification_runner is not None:
                passed, detail = self._custom_verification_runner(command)
            else:
                passed, detail = False, "验证命令未获授权执行器"
            if not passed:
                self.fail(
                    item_id,
                    (
                        f"计划项验证命令执行失败。实际执行的命令：{command!r}。"
                        f"错误详情：{detail or '验证命令未通过'}。"
                        "如果这是中文验收说明，请使用 plan_update 将 verification_type 改为 'result'；"
                        "如果这是可执行命令但命令本身有误，请修正 verification 字段后再试。"
                    ),
                    result=result,
                )
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
            self._report_writeback_rejected(item_id, "fail", item)
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
        # 已经发生超时接手的项只能显式 resume_takeover；普通 retry 会把它
        # 重新下发给子代理，违反“超时一次后主 Agent 接手”的业务约束。
        if bool(item.get("takeover_required", False)):
            return False
        updated = self.plan.transition_execution(
            item_id,
            expected_status=item["status"],
            expected_mode=str(item.get("execution_mode", "")),
            expected_takeover_required=False,
            status="pending",
            execution_mode="",
            owner="",
            handoff_task_id="",
            handoff_reason="",
            takeover_turn_id="",
            takeover_sequence=0,
            takeover_required=False,
            error="",
            result="",
        )
        return updated is not None

    def start_delegated_item(
        self,
        item_id: str,
        source_task_id: str,
        owner: str,
        event_callback: SchedulerCallback | None,
    ) -> bool:
        """认领一个 pending 计划项交给真实子代理执行，并原子绑定执行租约字段。

        与 start_item 的差异：同一临界区内额外写入 execution_mode=delegated、
        delegation_count+1 与 handoff_task_id=source_task_id（真实子代理任务 id，
        不得用角色名或事后猜测值代替），供超时移交 handoff_to_main 校验执行租约。
        """
        pending_events: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            if self._paused:
                return False
            for blocked in self._mark_blocked_dependencies():
                pending_events.append(("plan_item_blocked", blocked))
            # 依赖项可能被 plan_update 直接重置为 pending，启动前同步自愈 blocked 项
            self._revive_blocked_items()
            item = self._item(item_id)
            started_item = None
            if (
                item is not None
                and item["status"] == "pending"
                and not bool(item.get("takeover_required", False))
                and len(self._in_flight_ids) < self.max_workers
                and self._files_available_locked(item_id, item.get("files", []))
                and item["id"] in {candidate["id"] for candidate in self.plan.ready_items()}
            ):
                if event_callback is not None:
                    self._item_callbacks[item_id] = event_callback
                started_item = self.plan.transition_execution(
                    item_id,
                    expected_status="pending",
                    expected_takeover_required=False,
                    status="in_progress",
                    execution_mode="delegated",
                    owner=owner,
                    attempts=int(item.get("attempts", 0)) + 1,
                    delegation_count=int(item.get("delegation_count", 0)) + 1,
                    handoff_task_id=str(source_task_id),
                    handoff_reason="",
                    takeover_turn_id="",
                    takeover_sequence=0,
                    takeover_required=False,
                    error="",
                )
                if started_item is None:
                    self._item_callbacks.pop(item_id, None)
                else:
                    if not self._claim_files_locked(item_id, item.get("files", [])):
                        raise RuntimeError(
                            f"计划项 {item_id} 已委派但文件租约认领失败，调度不变量被破坏"
                        )
                    pending_events.append(("plan_item_started", started_item))
        # 事件统一在锁外派发，避免回调在持锁期间重入
        for kind, event_item in pending_events:
            self._emit_item(kind, event_item)
        return started_item is not None

    def handoff_to_main(
        self,
        item_id: str,
        source_task_id: str,
        parent_turn_id: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """子代理首次超时后把计划项移交给主 Agent（同一次尝试的延续）。

        调度器锁内校验：项目存在、status=in_progress、execution_mode=delegated、
        source_task_id 与真实执行租约 handoff_task_id 完全一致。
        同一会话已有 active 接管项时进入 takeover_queued FIFO 队列；
        否则直接成为 main_takeover 并绑定 owner=main:<turn_id>。
        状态保持 in_progress 且不增加 attempts——接管属于同一次尝试的继续。
        校验失败返回 None，绝不把超时改写成普通 failed。
        """
        with self._lock:
            item = self._item(item_id)
            if (
                item is None
                or item.get("status") != "in_progress"
                or item.get("execution_mode") != "delegated"
                or str(item.get("handoff_task_id", "")) != str(source_task_id)
            ):
                return None
            snapshot = self.plan.snapshot()
            has_active = self._active_takeover_locked() is not None
            has_queued = any(
                candidate.get("execution_mode") == "takeover_queued"
                and candidate.get("status") in {"pending", "in_progress"}
                for candidate in snapshot
            )
            queued_count = sum(
                1
                for candidate in snapshot
                if candidate.get("execution_mode") == "takeover_queued"
                and candidate.get("status") in {"pending", "in_progress"}
            )
            sequence = self._next_takeover_sequence_locked()
            if has_active or has_queued:
                handed_off = self.plan.transition_execution(
                    item_id,
                    expected_status="in_progress",
                    expected_mode="delegated",
                    expected_handoff_task_id=str(source_task_id),
                    execution_mode="takeover_queued",
                    handoff_reason=str(reason),
                    takeover_turn_id="",
                    takeover_sequence=sequence,
                    takeover_required=True,
                )
                if handed_off is not None:
                    handed_off["queue_position"] = queued_count + 1
                return handed_off
            handed_off = self.plan.transition_execution(
                item_id,
                expected_status="in_progress",
                expected_mode="delegated",
                expected_handoff_task_id=str(source_task_id),
                execution_mode="main_takeover",
                owner=f"main:{parent_turn_id}",
                handoff_reason=str(reason),
                takeover_turn_id=str(parent_turn_id),
                takeover_sequence=sequence,
                takeover_required=True,
            )
            if handed_off is not None:
                handed_off["queue_position"] = 0
            return handed_off

    def active_takeover(self, parent_turn_id: str) -> dict[str, Any] | None:
        """返回当前会话内归属 parent_turn_id 的 active 接管项；无则 None。

        只读取本调度器持有的会话计划，绝不查询进程级全局状态，
        保证并行会话的同名计划项互不串扰。
        """
        with self._lock:
            item = self._active_takeover_locked()
            if (
                item is not None
                and str(item.get("takeover_turn_id", "")) == str(parent_turn_id)
            ):
                return item
            return None

    def claim_takeover_for_turn(self, parent_turn_id: str) -> dict[str, Any] | None:
        """为真实主回合认领最早的接管项，并回收已死亡回合的旧租约。

        同一 turn 重复调用幂等返回当前项；不同 turn 到来时先把旧 active
        原子降级回 FIFO 队列，再按 sequence 认领，保证崩溃恢复后仍可继续。
        """
        turn_id = str(parent_turn_id).strip()
        if not turn_id:
            return None
        with self._lock:
            active = self._active_takeover_locked()
            if active is not None:
                if str(active.get("takeover_turn_id", "")) == turn_id:
                    active["queue_position"] = 0
                    return active
                requeued = self.plan.transition_execution(
                    active["id"],
                    expected_status="in_progress",
                    expected_mode="main_takeover",
                    expected_takeover_turn_id=str(active.get("takeover_turn_id", "")),
                    expected_takeover_required=True,
                    status="pending",
                    execution_mode="takeover_queued",
                    owner="",
                    takeover_turn_id="",
                    takeover_required=True,
                    error="上一主回合结束前未完成，已等待新回合继续接手",
                )
                if requeued is None:
                    return None
                self._release_files(active["id"])

            queued = [
                item
                for item in self.plan.snapshot()
                if item.get("execution_mode") == "takeover_queued"
                and item.get("status") in {"pending", "in_progress"}
                and bool(item.get("takeover_required", False))
            ]
            if not queued:
                return None
            queued.sort(key=lambda candidate: (
                int(candidate.get("takeover_sequence", 0) or 0),
                candidate["id"],
            ))
            # FIFO 只决定优先级，不保证队首一定可执行：队首声明的 files 若与
            # 在途租约冲突，继续尝试后面的项，避免单个占位项堵死整条接管队列
            for candidate in queued:
                files = set(candidate.get("files", []))
                if not self._files_available_locked(candidate["id"], files):
                    continue
                claimed = self.plan.transition_execution(
                    candidate["id"],
                    expected_status={"pending", "in_progress"},
                    expected_mode="takeover_queued",
                    expected_takeover_required=True,
                    status="in_progress",
                    execution_mode="main_takeover",
                    owner=f"main:{turn_id}",
                    takeover_turn_id=turn_id,
                    takeover_required=True,
                    error="",
                )
                if claimed is None:
                    # CAS 失败只跳过本项，不放弃整条队列
                    continue
                if not self._claim_files_locked(candidate["id"], files):
                    raise RuntimeError(
                        f"接管项 {candidate['id']} 已认领但文件租约登记失败，调度不变量被破坏"
                    )
                claimed["queue_position"] = 0
                return claimed
            return None

    def release_takeover_for_turn(
        self,
        parent_turn_id: str,
        *,
        cancelled: bool,
        reason: str,
    ) -> dict[str, Any] | None:
        """释放当前主回合的接管租约；系统结束重排队，用户取消落终态。"""
        turn_id = str(parent_turn_id).strip()
        with self._lock:
            active = self._active_takeover_locked()
            if (
                active is None
                or str(active.get("takeover_turn_id", "")) != turn_id
                or not bool(active.get("takeover_required", False))
            ):
                return None
            released = self.plan.transition_execution(
                active["id"],
                expected_status="in_progress",
                expected_mode="main_takeover",
                expected_takeover_turn_id=turn_id,
                expected_takeover_required=True,
                status="cancelled" if cancelled else "pending",
                execution_mode="" if cancelled else "takeover_queued",
                owner="",
                takeover_turn_id="",
                takeover_required=True,
                error=str(reason),
            )
            if released is None:
                return None
            self._release_files(active["id"])
        if cancelled:
            self._emit_item("plan_item_cancelled", released)
        return released

    def resume_takeover(
        self,
        item_id: str,
        parent_turn_id: str,
    ) -> dict[str, Any] | None:
        """显式恢复一个曾经移交主 Agent 的项，不增加 attempts/delegation_count。"""
        turn_id = str(parent_turn_id).strip()
        if not turn_id:
            return None
        with self._lock:
            item = self._item(item_id)
            if (
                item is None
                or item.get("status") not in {"failed", "blocked", "cancelled", "pending"}
                or not bool(item.get("takeover_required", False))
                or self._active_takeover_locked() is not None
            ):
                return None
            files = set(item.get("files", []))
            if not self._files_available_locked(item_id, files):
                return None
            resumed = self.plan.transition_execution(
                item_id,
                expected_status=str(item.get("status", "")),
                expected_mode=str(item.get("execution_mode", "")),
                expected_takeover_required=True,
                status="in_progress",
                execution_mode="main_takeover",
                owner=f"main:{turn_id}",
                takeover_turn_id=turn_id,
                takeover_required=True,
                error="",
            )
            if resumed is not None:
                if not self._claim_files_locked(item_id, files):
                    raise RuntimeError(
                        f"接管项 {item_id} 已恢复但文件租约登记失败，调度不变量被破坏"
                    )
                resumed["queue_position"] = 0
            return resumed

    def promote_next_takeover(self, parent_turn_id: str) -> dict[str, Any] | None:
        """把 FIFO 队列中最早的 queued 项提升为 active 接管项并绑定新 turn。

        已有 active 接管项（无论归属哪个 turn）时不提升——一个会话同一时刻
        只允许一个 active 主 Agent 接管项。崩溃恢复后的 queued 项可能是
        pending 状态（load 已复位），提升时同时恢复 in_progress。
        """
        with self._lock:
            if self._active_takeover_locked() is not None:
                return None
            queued = [
                item
                for item in self.plan.snapshot()
                if item.get("execution_mode") == "takeover_queued"
                and item.get("status") in {"pending", "in_progress"}
                and bool(item.get("takeover_required", False))
            ]
            if not queued:
                return None
            queued.sort(key=lambda candidate: (
                int(candidate.get("takeover_sequence", 0) or 0),
                candidate["id"],
            ))
            # 崩溃恢复的 queued 接管必须先成功认领文件租约才能晋升，绝不无锁运行；
            # 队首冲突时顺延到后面的可执行项，而不是让整条队列一起等待
            for candidate in queued:
                files = set(candidate.get("files", []))
                if not self._files_available_locked(candidate["id"], files):
                    continue
                promoted = self.plan.transition_execution(
                    candidate["id"],
                    expected_status={"pending", "in_progress"},
                    expected_mode="takeover_queued",
                    expected_takeover_required=True,
                    status="in_progress",
                    execution_mode="main_takeover",
                    owner=f"main:{parent_turn_id}",
                    takeover_turn_id=str(parent_turn_id),
                    takeover_required=True,
                )
                if promoted is None:
                    # CAS 失败只跳过本项，不放弃整条队列
                    continue
                if not self._claim_files_locked(candidate["id"], files):
                    raise RuntimeError(
                        f"接管项 {candidate['id']} 已晋升但文件租约登记失败，调度不变量被破坏"
                    )
                promoted["queue_position"] = 0
                return promoted
            return None

    def _active_takeover_locked(self) -> dict[str, Any] | None:
        for item in self.plan.snapshot():
            if (
                item.get("status") == "in_progress"
                and item.get("execution_mode") == "main_takeover"
            ):
                return item
        return None

    def _next_takeover_sequence_locked(self) -> int:
        sequences = [
            int(item.get("takeover_sequence", 0) or 0)
            for item in self.plan.snapshot()
        ]
        return (max(sequences) if sequences else 0) + 1

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
        """会话被切换后调用：终止在途验证进程并清空在途记录。

        计划仍绑定原会话时把在途项回写 pending；计划已被新会话接管时
        （bind_session 已替换全部计划项）不回写，避免误改新会话同名计划项。"""
        with self._lock:
            processes = list(self._verification_processes.values())
            self._verification_processes.clear()
            in_flight_ids = set(self._in_flight_ids)
            self._in_flight_ids.clear()
            self._in_flight_files.clear()
            self._in_flight_files_by_item.clear()
            self._item_callbacks.clear()
            can_write_back = self.plan.session_id == self._plan_session_id
        # 不回写已失效的计划；会话未切换时在途项必须复位 pending，
        # 否则计划项永久卡在 in_progress（不会被重新调度也无法 retry）
        if can_write_back:
            for item_id in in_flight_ids:
                self.plan.update(
                    item_id,
                    status="pending",
                    error="调度器关闭时已中断，恢复为待执行",
                    expected_status="in_progress",
                )
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
            # 只按认领时的不可变快照释放，绝不读取可能已变化的计划字段
            claimed = self._in_flight_files_by_item.pop(item_id, frozenset())
            self._in_flight_files.difference_update(claimed)
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
        from core.command_policy import is_dangerous_command
        from core.command_shell import CommandShellError, build_command_invocation
        from core.process_io import decode_process_output, platform_console_encoding

        if is_dangerous_command(command):
            return False, "危险命令已被安全策略拒绝，未执行验证。"
        try:
            invocation = build_command_invocation(command)
        except CommandShellError as exc:
            return False, f"命令解释器不可用: {exc}"

        output_encoding = platform_console_encoding()
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = output_encoding
        # 与 execute_command 的执行环境保持一致
        env["PYTHONUTF8"] = "0"
        # 不设置 cwd：验证命令必须和 execute_command 一样在项目工作目录下执行。
        # 历史上这里会根据计划项 files 把 cwd 偷换成文件所在目录，导致模型按项目根
        # 目录编写的验证命令（如 `cd subdir && ...`）在错误目录下执行而永远失败。
        try:
            popen_kwargs = {
                "shell": invocation.use_system_shell,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": False,
                "env": env,
                # GUI（无控制台）下避免系统为子进程分配新控制台窗口
                "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                "start_new_session": os.name != "nt",
            }
            if invocation.executable:
                popen_kwargs["executable"] = invocation.executable
            process = subprocess.Popen(
                invocation.argv,
                **popen_kwargs,
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
# 全局单例的创建/切换锁：两线程并发首次调用必须只建一个实例，
# 否则同一计划项会被两个调度器重复认领执行
_DEFAULT_SCHEDULER_LOCK = threading.RLock()

# 多会话并行：线程绑定了计划会话时，调度器按会话分桶（互不取消、互不阻塞）。
# 原先"会话切换即 cancel_all"的单例语义只保留给未绑定线程（CLI 单会话主流程）
# ——CLI 的 _DEFAULT_PLAN_MANAGER 是就地重绑的同一个对象，不收尾旧调度器
# 会让在途计划项把结果写进切换后的新会话计划。
_PARALLEL_SCHEDULERS: dict[str, TaskScheduler] = {}


def get_default_scheduler(
    *,
    event_callback: SchedulerCallback | None = None,
) -> TaskScheduler:
    """Return the scheduler for the currently bound plan session.

    绑定线程（GUI 回合/子代理）：按会话取专属调度器，其他会话的调度器
    不受影响（并行回合永不互相取消）。未绑定线程（CLI/测试主线程）：
    维持进程级单例 + 切会话收尾的旧语义。
    """
    from core.plan import get_bound_plan_session, get_default_plan_manager

    bound_session = get_bound_plan_session()
    if bound_session is not None:
        plan = get_default_plan_manager()
        with _DEFAULT_SCHEDULER_LOCK:
            scheduler = _PARALLEL_SCHEDULERS.get(bound_session)
            if scheduler is None or scheduler.plan is not plan:
                scheduler = TaskScheduler(plan, event_callback=event_callback)
                _PARALLEL_SCHEDULERS[bound_session] = scheduler
            elif event_callback is not None:
                scheduler.set_event_callback(event_callback)
            return scheduler

    global _DEFAULT_SCHEDULER, _DEFAULT_SCHEDULER_SESSION
    plan = get_default_plan_manager()
    session_id = plan.session_id
    # 读取-判定-创建全程持锁（RLock 允许会话切换时 cancel_all 的事件回调重入）
    with _DEFAULT_SCHEDULER_LOCK:
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


def shutdown_all_schedulers() -> int:
    """退出时收尾全部调度器（并行分桶 + CLI 兜底单例），返回收尾数量。"""
    global _DEFAULT_SCHEDULER
    with _DEFAULT_SCHEDULER_LOCK:
        schedulers = list(_PARALLEL_SCHEDULERS.values())
        _PARALLEL_SCHEDULERS.clear()
        if _DEFAULT_SCHEDULER is not None:
            schedulers.append(_DEFAULT_SCHEDULER)
            _DEFAULT_SCHEDULER = None
    count = 0
    for scheduler in schedulers:
        try:
            scheduler.cancel_all()
            scheduler.shutdown()
            count += 1
        except Exception:
            pass
    return count
