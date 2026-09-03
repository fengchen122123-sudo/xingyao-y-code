"""单租约电脑操控 Controller：显式状态机、排他租约、暂停恢复与固定停止顺序。

设计依据 docs/superpowers/specs/2026-09-02-computer-control-design.md 第 14、15 节：

- 进程内只有一个 ComputerControlService，活动 run 持有排他租约（owner session /
  turn id / cancel event / capability 快照 / target+backend / settings generation）；
  第二 owner 请求操控返回 computer_session_busy（details 只含会话显示名）。
- 状态转换只走 _TRANSITIONS 显式转移表，非法转换抛 computer_invalid_state。
- 动作顺序严格为 owner/state/cancel/generation → before fingerprint → execute →
  fresh after observation → no-progress 计数 → events → 返回 observation；
  任何一步失败都不发 action_finished(ok=True)；截图失败直接传播，绝不复用旧图。
- generation 语义（设计 8.1 + Task 3 审查裁决）：generation 失配时重读当前有效
  设置，仅当本 run target 对应能力已被削弱/撤销（或设置读取失败按关闭处理）才
  停止 run；只是“另一个开关被开启”导致的变化更新 generation 快照后继续。
- stop() 顺序固定：controller 状态置 stopping → owner.cancel_event.set() →
  abort_inflight_streams(owner.cancel_event)（惰性导入 core.llm，模块顶层绝不
  import core.llm）→ backend release_inputs → backend close → 状态终结 →
  租约释放 → final event。每步独立 try/except 保证后续释放；首个清理错误记为
  run failure（failed），绝不伪装成 stopped success。
- complete_run 与 stop 幂等；Turn 终态 finally 必须二选一调用，不留 running 租约。
- 浏览器 backend 在 retain 时不 close，作为无执行权的 retained session 供下一
  run 显式复用；desktop backend 在 complete 时立即 release_inputs + close。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from typing import Callable

from core.computer.models import (
    ComputerBackend,
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
    ComputerRunSnapshot,
    ComputerRunStatus,
)
from core.computer.settings import get_settings_generation, load_computer_settings

_LOGGER = logging.getLogger(__name__)

# 显式状态转移表（计划 Task 8 Step 3 逐字给定，非法转换抛领域不变量错误）。
_TRANSITIONS = {
    "idle": {"starting"},
    "starting": {"running", "failed", "stopping"},
    "running": {"paused", "waiting_for_user", "provider_safety_required", "completing", "stopping", "failed"},
    "paused": {"running", "stopping", "failed"},
    "waiting_for_user": {"running", "stopping", "failed"},
    "provider_safety_required": {"running", "stopping", "failed"},
    "completing": {"completed", "stopping", "failed"},
    "completed": {"starting"},
    "stopping": {"stopped", "failed"},
    "stopped": {"starting"},
    "failed": {"starting"},
}

_TERMINAL_STATUSES = frozenset(
    {
        ComputerRunStatus.COMPLETED.value,
        ComputerRunStatus.STOPPED.value,
        ComputerRunStatus.FAILED.value,
    }
)

# observe/wait 不预期改变界面，不计入无进展；request_user_takeover 由 controller
# 处理、不进入 backend，也不计数（设计 14.4）。
_NO_PROGRESS_EXEMPT_KINDS = frozenset({"observe", "wait", "request_user_takeover"})
_NO_PROGRESS_LIMIT = 3


class _Run:
    """活动 run 的可变内部状态；字段访问由 controller 的状态锁保护。"""

    def __init__(self, owner: ComputerOwner, capability: ComputerCapability, requested_target: str | None):
        self.run_id = f"computer-run-{uuid.uuid4().hex[:12]}"
        self.owner = owner
        self.capability = capability
        self.requested_target = requested_target
        self.target: str | None = requested_target  # generic run 首次 observe 后绑定
        self.backend: ComputerBackend | None = None
        self.status = ComputerRunStatus.IDLE.value
        self.generation = capability.settings_generation
        self.started_at = time.time()
        self.action_count = 0
        self.event_seq = 0
        self.current_action = ""
        self.current_surface = ""
        self.preview_url = ""
        self.pause_reason = ""
        self.stop_reason = ""
        self.error_code = ""
        self.require_observe = True  # 第一次必须先 observe（设计 10.1）
        self.last_fingerprint = ""
        self.last_screen_sha256 = ""
        self.last_progress_key: tuple[str, str] | None = None
        self.no_progress_count = 0


def _surface_fingerprint(observation: ComputerObservation) -> str:
    """active window/page 指纹：browser 为 origin+title，desktop 为窗口标题。"""
    if observation.target == "browser":
        return f"{observation.page_origin}|{observation.page_title}"
    return observation.window_title


def _default_backend_factory(target: str) -> ComputerBackend:
    """生产 backend 构造：惰性导入，模块顶层不触碰 Playwright/PIL/Win32。"""
    if target == "browser":
        from core.computer.browser_backend import BrowserComputerBackend
        from core.computer.settings import get_browser_profile_dir

        settings = load_computer_settings()
        profile_dir = get_browser_profile_dir(settings.computer_browser)
        return BrowserComputerBackend(settings.computer_browser, profile_dir)
    from core.computer.windows_backend import WindowsComputerBackend

    return WindowsComputerBackend.create_default()


def _default_abort_streams(cancel_event) -> None:
    # 惰性导入铁律：本模块顶层绝不 import core.llm（避免循环与冷启动成本）。
    from core.llm import abort_inflight_streams

    abort_inflight_streams(cancel_event)


def _hotkey_available() -> bool:
    try:
        from core.computer.hotkey import get_emergency_hotkey_snapshot

        return bool(get_emergency_hotkey_snapshot().available)
    except Exception:
        return False


def _close_backend_quietly(backend: ComputerBackend) -> None:
    """best-effort 关闭孤儿 backend（租约建立失败时刚启动的实例）。

    清理异常只留痕，绝不向上传播、绝不掩盖原始错误。
    """
    try:
        backend.close()
    except Exception:
        _LOGGER.exception("关闭孤儿电脑操控 backend 失败（已忽略）")


class ComputerControlService:
    """进程内唯一电脑操控控制器（Cross-Task 公共方法见计划 Interface Contract）。"""

    def __init__(
        self,
        *,
        backend_factory: Callable[[str], ComputerBackend] | None = None,
        settings_loader: Callable[[], object] | None = None,
        generation_provider: Callable[[], int] | None = None,
        abort_streams: Callable[[threading.Event], None] | None = None,
    ) -> None:
        self._backend_factory = backend_factory or _default_backend_factory
        self._settings_loader = settings_loader or load_computer_settings
        self._generation_provider = generation_provider or get_settings_generation
        self._abort_streams = abort_streams or _default_abort_streams
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._run: _Run | None = None
        self._retained: dict[str, ComputerBackend] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._last_snapshot: ComputerRunSnapshot | None = None

    # ---------------- 事件 ----------------

    def add_listener(self, listener: Callable[[dict], None]) -> None:
        """注册事件 listener（供 GUI Bridge 等消费）；listener 异常只留痕不传播。"""
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[dict], None]) -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    def _emit(self, run: _Run, event_type: str, **fields) -> None:
        with self._lock:
            run.event_seq += 1
            event = {
                "type": event_type,
                "run_id": run.run_id,
                "event_seq": run.event_seq,
                **fields,
            }
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("电脑操控事件 listener 异常（已忽略）")

    def _emit_state(self, run: _Run, reason: str = "") -> None:
        self._emit(
            run,
            "computer_state",
            status=run.status,
            target=run.target or "",
            reason=reason,
        )

    # ---------------- 状态机 ----------------

    def _transition(self, run: _Run, new_status: str) -> None:
        """显式转移表校验（调用方必须持有状态锁）；非法转换抛领域不变量错误。"""
        allowed = _TRANSITIONS.get(run.status, set())
        if new_status not in allowed:
            raise ComputerControlError(
                "computer_invalid_state",
                f"非法状态转换：{run.status} → {new_status}",
            )
        run.status = new_status

    # ---------------- 快照 ----------------

    def _snapshot_locked(self, run: _Run) -> ComputerRunSnapshot:
        return ComputerRunSnapshot(
            run_id=run.run_id,
            owner_session_id=run.owner.session_id,
            owner_turn_id=run.owner.turn_id,
            status=run.status,
            target=run.target or "",
            browser=run.capability.browser,
            protocol=run.capability.protocol,
            started_at=run.started_at,
            action_count=run.action_count,
            current_action=run.current_action,
            current_surface=run.current_surface,
            preview_url=run.preview_url,
            pause_reason=run.pause_reason,
            stop_reason=run.stop_reason,
            error_code=run.error_code,
            hotkey_available=_hotkey_available(),
        )

    def snapshot(self) -> ComputerRunSnapshot:
        """当前活动 run 快照；无活动 run 时返回最近终态快照，否则 idle 默认值。"""
        with self._lock:
            if self._run is not None:
                return self._snapshot_locked(self._run)
            if self._last_snapshot is not None:
                return self._last_snapshot
            return ComputerRunSnapshot()

    def _finalize_locked(self, run: _Run) -> None:
        """终态收尾：释放排他租约，保留终态快照供 UI 查询。"""
        self._last_snapshot = self._snapshot_locked(run)
        self._run = None

    # ---------------- 检查 ----------------

    def _require_active_run_locked(self, run_id: str) -> _Run:
        run = self._run
        if run is None or run.run_id != run_id:
            raise ComputerControlError(
                "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
            )
        return run

    @staticmethod
    def _check_owner_locked(run: _Run, owner: ComputerOwner) -> None:
        if (
            owner.session_id != run.owner.session_id
            or owner.turn_id != run.owner.turn_id
        ):
            raise ComputerControlError(
                "computer_owner_mismatch",
                "当前电脑操控 run 属于其他会话/Turn，拒绝操作",
            )

    @staticmethod
    def _check_action_allowed_locked(run: _Run, action) -> None:
        if run.status == ComputerRunStatus.PAUSED.value:
            raise ComputerControlError(
                "computer_invalid_state", "电脑操控已暂停，请先 resume"
            )
        if run.status != ComputerRunStatus.RUNNING.value:
            raise ComputerControlError(
                "computer_invalid_state",
                f"当前状态 {run.status} 不允许执行动作",
            )
        if run.owner.cancel_event.is_set():
            raise ComputerControlError(
                "computer_cancelled", "owner Turn 已取消，拒绝执行电脑动作"
            )
        if run.require_observe and action.kind != "observe":
            raise ComputerControlError(
                "computer_invalid_action",
                "暂停/接管恢复后旧坐标已作废，必须先观察（observe）刷新画面",
            )

    def _generation_allowed_locked(
        self, run: _Run, effective_target: str | None = None
    ) -> tuple[bool, int]:
        """generation 失配时重读有效设置；仅 target 能力被削弱/撤销才判 False。

        未绑定 target 的 run（generic 首次 observe 之前）传入本次动作的
        ``effective_target``——设计 8.1 要求开关关闭后"新动作从此立即拒绝"，
        不能因另一个开关仍开启就放行对已关闭 target 的动作。
        设置读取失败（损坏配置）按关闭处理（Task 3 审查裁决）。
        """
        current = self._generation_provider()
        if current == run.generation:
            return True, current
        try:
            settings = self._settings_loader()
        except Exception:
            _LOGGER.warning("重读电脑操控设置失败，按关闭处理", exc_info=True)
            return False, current
        platform_ok = bool(settings.platform_supported)
        target = effective_target if effective_target is not None else run.target
        if target == "browser":
            allowed = platform_ok and bool(settings.browser_control_enabled)
        elif target == "desktop":
            allowed = platform_ok and bool(settings.computer_control_enabled)
        else:
            allowed = platform_ok and (
                bool(settings.browser_control_enabled)
                or bool(settings.computer_control_enabled)
            )
        return allowed, current

    # ---------------- backend 获取（含 retained 复用） ----------------

    def _claim_backend(self, target: str) -> ComputerBackend:
        with self._lock:
            retained = self._retained.pop(target, None)
        if retained is not None:
            return retained
        try:
            backend = self._backend_factory(target)
            backend.start()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_backend_start_failed", f"电脑操控 backend 启动失败：{error}"
            ) from error
        return backend

    def _fail_run(self, run: _Run, error: BaseException, reason: str) -> None:
        """backend 启动失败路径：run → failed，释放租约并发最终事件。"""
        with self._lock:
            if self._run is run and run.status not in _TERMINAL_STATUSES:
                run.error_code = getattr(error, "code", "computer_backend_start_failed")
                try:
                    self._transition(run, ComputerRunStatus.FAILED.value)
                except ComputerControlError:
                    run.status = ComputerRunStatus.FAILED.value
                self._finalize_locked(run)
        self._emit_state(run, reason=reason)

    # ---------------- 公共 API ----------------

    def start_run(
        self,
        owner: ComputerOwner,
        capability: ComputerCapability,
        target: str | None = None,
    ) -> ComputerRunSnapshot:
        """创建活动 run 并获取排他租约；target=None 时首次合法 observe 建立租约。"""
        if owner.cancel_event.is_set():
            raise ComputerControlError(
                "computer_cancelled", "owner Turn 已取消，不能开启电脑操控 run"
            )
        if not capability.enabled:
            raise ComputerControlError(
                capability.error_code or "computer_disabled",
                capability.reason or "电脑操控不可用",
            )
        if target is not None and target not in capability.targets:
            raise ComputerControlError(
                "computer_disabled", f"操控目标 {target!r} 未在设置中开启"
            )
        run = _Run(owner, capability, target)
        with self._lock:
            if self._run is not None:
                raise ComputerControlError(
                    "computer_session_busy",
                    f"电脑操控正被会话「{self._run.owner.session_label}」占用",
                    details={"owner_session_label": self._run.owner.session_label},
                )
            self._run = run
            self._transition(run, ComputerRunStatus.STARTING.value)
        self._emit_state(run, reason="start")
        if target is not None:
            try:
                backend = self._claim_backend(target)
            except Exception as error:
                self._fail_run(run, error, "backend_start_failed")
                raise
            with self._lock:
                run_alive = (
                    self._run is run
                    and run.status == ComputerRunStatus.STARTING.value
                )
                if run_alive:
                    run.backend = backend
                    self._transition(run, ComputerRunStatus.RUNNING.value)
            if not run_alive:
                # run 在 backend 启动期间被停止：backend 从未挂到活动 run 上，
                # stop() 关不到它；不关闭会遗留浏览器进程与被锁 profile。
                _close_backend_quietly(backend)
                raise ComputerControlError(
                    "computer_cancelled", "run 在 backend 启动期间已结束"
                )
        else:
            with self._lock:
                self._transition(run, ComputerRunStatus.RUNNING.value)
        self._emit_state(run, reason="running")
        return self.snapshot()

    def execute(
        self, run_id: str, owner: ComputerOwner, action
    ) -> ComputerObservation:
        """执行一个动作并返回全新 observation；每个动作由单一 action_lock 串行。"""
        with self._action_lock:
            stop_for_settings = False
            with self._lock:
                run = self._require_active_run_locked(run_id)
                self._check_owner_locked(run, owner)
                self._check_action_allowed_locked(run, action)
                # 未绑定 target 的 run 用本次动作的 target 判定能力：
                # 开关关闭后新动作立即拒绝（设计 8.1），不能因另一开关
                # 仍开启就放行对已关闭 target 的动作。
                allowed, current = self._generation_allowed_locked(
                    run, run.target or action.target
                )
                if allowed:
                    run.generation = current
                else:
                    stop_for_settings = True
            if stop_for_settings:
                self.stop(run_id, "settings_disabled")
                raise ComputerControlError(
                    "computer_disabled", "电脑操控开关已关闭，run 已停止"
                )

            establishing = False
            with self._lock:
                backend = run.backend
                if backend is None:
                    # generic 双 target：第一次合法 observe 建立 target 租约
                    if action.kind != "observe":
                        raise ComputerControlError(
                            "computer_invalid_action",
                            "尚未建立观察基线，必须先观察（observe）后再执行其他动作",
                        )
                    if action.target not in run.capability.targets:
                        raise ComputerControlError(
                            "computer_invalid_action",
                            f"操控目标 {action.target!r} 不在本 Turn 能力范围内",
                        )
                    establishing = True
                elif action.target != run.target:
                    raise ComputerControlError(
                        "computer_invalid_action",
                        f"本 run 已绑定 target={run.target!r}，"
                        f"拒绝 target={action.target!r} 的动作",
                    )
            if establishing:
                try:
                    backend = self._claim_backend(action.target)
                except Exception as error:
                    self._fail_run(run, error, "backend_start_failed")
                    raise
                backend_attached = False
                with self._lock:
                    # 二次确认：run 仍是活动 run 且 generation 未失效
                    run_alive = (
                        self._run is run
                        and run.status == ComputerRunStatus.RUNNING.value
                    )
                    if run_alive:
                        allowed, current = self._generation_allowed_locked(
                            run, action.target
                        )
                        if not allowed:
                            stop_for_settings = True
                        else:
                            run.generation = current
                            run.backend = backend
                            run.target = action.target
                            backend_attached = True
                if not backend_attached:
                    # 刚启动的 backend 尚未挂到活动 run 上，stop()/调用方都
                    # 关不到它；不关闭会遗留浏览器进程与被锁 profile。
                    _close_backend_quietly(backend)
                if not run_alive:
                    raise ComputerControlError(
                        "computer_cancelled", "run 在建立租约期间已结束"
                    )
            if stop_for_settings:
                self.stop(run_id, "settings_disabled")
                raise ComputerControlError(
                    "computer_disabled", "电脑操控开关已关闭，run 已停止"
                )

            with self._lock:
                before_fingerprint = run.last_fingerprint
                run.action_count += 1
                ordinal = run.action_count
                run.current_action = action.kind
            self._emit(
                run,
                "computer_action_started",
                action_ordinal=ordinal,
                action_kind=action.kind,
                target=action.target,
            )

            try:
                if action.kind in ("observe", "request_user_takeover"):
                    # observe 的 fresh observation 即动作本身；
                    # request_user_takeover 由 controller 处理，不进入 backend。
                    pass
                else:
                    backend.execute(run_id, action)
                observation = backend.observe(run_id)
            except ComputerControlError as error:
                self._emit(
                    run,
                    "computer_action_finished",
                    action_ordinal=ordinal,
                    action_kind=action.kind,
                    ok=False,
                    error_code=error.code,
                )
                raise
            except Exception as error:
                self._emit(
                    run,
                    "computer_action_finished",
                    action_ordinal=ordinal,
                    action_kind=action.kind,
                    ok=False,
                    error_code="computer_backend_error",
                )
                raise ComputerControlError(
                    "computer_backend_error", f"电脑操控 backend 异常：{error}"
                ) from error

            after_fingerprint = _surface_fingerprint(observation)
            observation = replace(
                observation,
                action=action.kind,
                before_fingerprint=before_fingerprint,
                after_fingerprint=after_fingerprint,
            )
            no_progress_hit = False
            with self._lock:
                # 动作后二次确认：关闭设置后旧动作不得提交成功
                if self._run is not run or run.status != ComputerRunStatus.RUNNING.value:
                    raise ComputerControlError(
                        "computer_cancelled", "run 在动作执行期间已停止"
                    )
                allowed, current = self._generation_allowed_locked(run)
                if allowed:
                    run.generation = current
                else:
                    stop_for_settings = True
            if stop_for_settings:
                self.stop(run_id, "settings_disabled")
                raise ComputerControlError(
                    "computer_disabled", "电脑操控开关已关闭，run 已停止"
                )
            with self._lock:
                run.last_fingerprint = after_fingerprint
                run.last_screen_sha256 = observation.screen_sha256
                run.current_surface = (
                    observation.page_origin or observation.window_title
                )
                run.preview_url = observation.preview_url
                run.require_observe = False
                key = (observation.screen_sha256, after_fingerprint)
                if action.kind in _NO_PROGRESS_EXEMPT_KINDS:
                    if run.last_progress_key is None:
                        run.last_progress_key = key
                elif key == run.last_progress_key:
                    run.no_progress_count += 1
                    run.last_progress_key = key
                else:
                    run.no_progress_count = 1
                    run.last_progress_key = key
                no_progress_hit = run.no_progress_count >= _NO_PROGRESS_LIMIT

            if no_progress_hit:
                self._emit(
                    run,
                    "computer_action_finished",
                    action_ordinal=ordinal,
                    action_kind=action.kind,
                    ok=False,
                    error_code="computer_no_progress",
                )
                self.stop(run_id, "no_progress")
                raise ComputerControlError(
                    "computer_no_progress",
                    f"连续 {_NO_PROGRESS_LIMIT} 次动作后截图与活动指纹完全不变，已停止电脑操控",
                )

            self._emit(
                run,
                "computer_action_finished",
                action_ordinal=ordinal,
                action_kind=action.kind,
                ok=True,
                error_code="",
            )
            self._emit(
                run,
                "computer_observation",
                action_ordinal=ordinal,
                target=observation.target,
                screen_sha256=observation.screen_sha256,
                width=observation.width,
                height=observation.height,
                origin_or_window=(
                    observation.page_origin or observation.window_title
                ),
                preview_url=observation.preview_url,
            )

            if action.kind == "request_user_takeover":
                with self._lock:
                    if self._run is run and run.status == ComputerRunStatus.RUNNING.value:
                        self._transition(run, ComputerRunStatus.WAITING_FOR_USER.value)
                        run.pause_reason = action.reason_code or ""
                        run.require_observe = True
                self._emit_state(run, reason=action.reason_code or "")
            return observation

    def pause(self, run_id: str, reason: str) -> ComputerRunSnapshot:
        """暂停只阻止下一动作开始，不回滚当前动作（设计 15.3）。"""
        with self._lock:
            run = self._require_active_run_locked(run_id)
            self._transition(run, ComputerRunStatus.PAUSED.value)
            run.pause_reason = reason
        self._emit_state(run, reason=reason)
        return self.snapshot()

    def resume(self, run_id: str) -> ComputerRunSnapshot:
        """恢复后强制下一动作是 observe（旧坐标作废）；owner Turn 已结束不得恢复。"""
        with self._lock:
            run = self._require_active_run_locked(run_id)
            if run.owner.cancel_event.is_set():
                raise ComputerControlError(
                    "computer_cancelled", "owner Turn 已结束，不得恢复电脑操控 run"
                )
            if run.status == ComputerRunStatus.PROVIDER_SAFETY_REQUIRED.value:
                raise ComputerControlError(
                    "computer_provider_safety_required",
                    "Provider 强制安全检查未确认，不能恢复，只允许停止",
                )
            self._transition(run, ComputerRunStatus.RUNNING.value)
            run.pause_reason = ""
            run.require_observe = True
        self._emit_state(run, reason="resume")
        return self.snapshot()

    def enter_provider_safety_required(self, run_id: str) -> ComputerRunSnapshot:
        """Provider 返回强制安全检查且无已验证确认契约时进入（设计 11.5）。"""
        with self._lock:
            run = self._require_active_run_locked(run_id)
            self._transition(run, ComputerRunStatus.PROVIDER_SAFETY_REQUIRED.value)
        self._emit_state(run, reason="provider_safety")
        return self.snapshot()

    def complete_run(
        self,
        run_id: str,
        owner: ComputerOwner,
        retain_browser_session: bool = True,
    ) -> ComputerRunSnapshot:
        """正常终态：completing→completed，释放 owner/租约；幂等。

        浏览器 backend retain 时不 close，作为 retained session 供下一 run 显式
        复用；desktop backend 立即 release_inputs + close。清理错误记为 failed。
        """
        with self._lock:
            run = self._run
            if run is None or run.run_id != run_id:
                last = self._last_snapshot
                if (
                    last is not None
                    and last.run_id == run_id
                    and last.status == ComputerRunStatus.COMPLETED.value
                ):
                    return last
                raise ComputerControlError(
                    "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
                )
            self._check_owner_locked(run, owner)
            self._transition(run, ComputerRunStatus.COMPLETING.value)
        backend = run.backend
        retain = (
            retain_browser_session and run.target == "browser" and backend is not None
        )
        first_error = None
        if backend is not None and not retain:
            try:
                backend.release_inputs()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("complete_run 释放电脑操控输入失败")
            try:
                backend.close()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("complete_run 关闭电脑操控 backend 失败")
        with self._lock:
            if self._run is run:
                if first_error is not None:
                    run.error_code = getattr(
                        first_error, "code", "computer_cleanup_failed"
                    )
                    self._transition(run, ComputerRunStatus.FAILED.value)
                else:
                    self._transition(run, ComputerRunStatus.COMPLETED.value)
                    if retain:
                        self._retained[run.target] = backend
                self._finalize_locked(run)
        self._emit_state(
            run, reason="complete" if first_error is None else "cleanup_failed"
        )
        return self.snapshot()

    def stop(self, run_id: str, reason: str) -> ComputerRunSnapshot:
        """取消/故障/紧急停止终态：固定顺序清理并关闭 backend；幂等。

        顺序：状态置 stopping → owner.cancel_event.set() → abort 在飞 Provider 流
        → backend release_inputs → backend close → 状态终结 → 租约释放 →
        final event。每步独立 try/except 保证后续释放；首个清理错误记为 failed，
        绝不伪装成 stopped success。
        """
        with self._lock:
            run = self._run
            if run is None or run.run_id != run_id:
                last = self._last_snapshot
                if (
                    last is not None
                    and last.run_id == run_id
                    and last.status in _TERMINAL_STATUSES
                ):
                    return last
                raise ComputerControlError(
                    "computer_invalid_state", f"未知的电脑操控 run：{run_id}"
                )
            if run.status in _TERMINAL_STATUSES:
                return self._snapshot_locked(run)
            self._transition(run, ComputerRunStatus.STOPPING.value)
            run.stop_reason = reason
        self._emit_state(run, reason=reason)

        first_error = None
        try:
            run.owner.cancel_event.set()
        except Exception as error:  # 防御性：threading.Event.set 正常不会失败
            first_error = first_error or error
            _LOGGER.exception("设置电脑操控 owner cancel event 失败")
        try:
            self._abort_streams(run.owner.cancel_event)
        except Exception as error:
            first_error = first_error or error
            _LOGGER.exception("中断在飞 Provider 流失败")
        backend = run.backend
        if backend is not None:
            try:
                backend.release_inputs()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("停止时释放电脑操控输入失败")
            try:
                backend.close()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("停止时关闭电脑操控 backend 失败")
        with self._lock:
            if self._run is run:
                if first_error is not None:
                    run.error_code = getattr(
                        first_error, "code", "computer_cleanup_failed"
                    )
                    self._transition(run, ComputerRunStatus.FAILED.value)
                else:
                    self._transition(run, ComputerRunStatus.STOPPED.value)
                self._finalize_locked(run)
        self._emit_state(run, reason=reason)
        return self.snapshot()


_SERVICE_LOCK = threading.Lock()
_SERVICE: ComputerControlService | None = None


def get_computer_control_service() -> ComputerControlService:
    """进程内惰性单例（设计 14.1）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = ComputerControlService()
    return _SERVICE
