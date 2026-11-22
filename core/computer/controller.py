from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable

from core.computer.models import (
    BackendIdentity,
    ComputerBackend,
    ComputerCapability,
    ComputerControlError,
    ComputerObservation,
    ComputerOwner,
    ComputerRunSnapshot,
    ComputerRunStatus,
    ObservationFrame,
    resolve_browser_mode,
)
from core.computer.frames import coordinate_disabled_error, frame_from_observation
from core.computer import stop_notice as stop_notice

_LOGGER = logging.getLogger(__name__)


def _default_settings_loader():
    from core.computer.settings import load_computer_settings

    return load_computer_settings()


def _default_generation_provider() -> int:
    from core.computer.settings import get_settings_generation

    return get_settings_generation()

_TRANSITIONS = {
    "idle": {"starting"},
    "starting": {"running", "failed", "stopping"},
    "running": {"paused", "waiting_for_user", "provider_safety_required", "completing", "stopping", "failed"},
    "paused": {"running", "stopping", "failed"},
    "waiting_for_user": {"running", "stopping", "failed", "completing"},
    "provider_safety_required": {"running", "stopping", "failed"},
    "completing": {"completed", "stopping", "failed"},
    "completed": {"starting"},
    "stopping": {"stopped", "failed"},
    "stopped": {"starting"},
    "failed": {"starting", "stopping"},
}

_TERMINAL_STATUSES = frozenset(
    {
        ComputerRunStatus.COMPLETED.value,
        ComputerRunStatus.STOPPED.value,
        ComputerRunStatus.FAILED.value,
    }
)

# R6 无进展规则：move/observe/wait 不预期改变界面，不计入无进展；
# request_user_takeover 由 controller 处理、不进入 backend，也不计数（设计 14.4）。
# R10：list_surfaces/observe_region 是观察类动作，同样不计数；
# activate_surface 预期改变前台（参与计数，变化即归零）。
# wait_for：条件等待命中时画面本来就可能不变（等的是稳定态，如窗口标题
# 仍是「保存成功」），不产生任何输入，不能按无进展处理——否则连续
# wait_for 会被误停为 computer_no_progress。
_NO_PROGRESS_EXEMPT_KINDS = frozenset(
    {
        "move",
        "observe",
        "wait",
        "wait_for",
        "request_user_takeover",
        "list_surfaces",
        "observe_region",
    }
)
_NO_PROGRESS_LIMIT = 3

# R5：受控画面身份错误——出现后旧坐标作废，必须重新 observe 才能继续输入。
_SURFACE_ERROR_CODES = frozenset(
    {"computer_surface_changed", "computer_surface_ambiguous"}
)


def next_progress_state(
    previous_key: tuple[str, str] | None,
    current_key: tuple[str, str],
    count: int,
    kind: str,
) -> tuple[tuple[str, str], int]:
    if current_key != previous_key:
        return current_key, 0
    if kind in _NO_PROGRESS_EXEMPT_KINDS:
        return current_key, count
    return current_key, count + 1


class _Run:

    def __init__(
        self,
        owner: ComputerOwner,
        capability: ComputerCapability,
        requested_target: str | None,
        run_generation: int = 0,
    ):
        self.run_id = f"computer-run-{uuid.uuid4().hex[:12]}"
        self.run_generation = int(run_generation)
        self.owner = owner
        self.capability = capability
        self.requested_target = requested_target
        self.target: str | None = requested_target
        self.backend: ComputerBackend | None = None
        self.backend_identity: BackendIdentity | None = None
        self.backend_starting = requested_target is not None
        self.stop_cleanup_in_progress = False
        self.stop_cancel_owner = False
        self.cleanup_confirmed = False
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
        self.last_frame_identity: tuple = ()
        self.frames: dict[str, ObservationFrame] = {}
        self.current_frame: ObservationFrame | None = None
        self.last_progress_key: tuple[str, str] | None = None
        self.no_progress_count = 0


_COORDINATE_ACTION_KINDS = frozenset(
    {"click", "double_click", "move", "drag"}
)


def _action_uses_coordinates(action) -> bool:
    if action.kind == "sequence":
        return any(_action_uses_coordinates(step) for step in (action.steps or ()))
    if action.kind in _COORDINATE_ACTION_KINDS:
        return True
    if action.kind in ("scroll", "observe_region"):
        return action.x is not None or action.y is not None
    return False


def _observation_frame_identity(observation: ComputerObservation) -> tuple:
    return (
        str(observation.surface_id or ""),
        int(observation.surface_generation or 0),
        int(observation.viewport_generation or 0),
        int(observation.origin_x or 0),
        int(observation.origin_y or 0),
        int(observation.width or 0),
        int(observation.height or 0),
        str(observation.page_origin or ""),
    )


def _surface_fingerprint(observation: ComputerObservation) -> str:
    if observation.target == "browser":
        return f"{observation.page_origin}|{observation.page_title}"
    return observation.window_title


def _default_backend_factory(target: str, identity: BackendIdentity) -> ComputerBackend:
    from core.computer.platforms import get_platform_adapter

    if target not in ("browser", "desktop"):
        raise ComputerControlError("computer_invalid_action", f"未知电脑操控目标：{target!r}")
    adapter = get_platform_adapter()
    browser_ok = getattr(adapter, "browser_supported", adapter.supported)
    desktop_ok = getattr(adapter, "desktop_supported", adapter.supported)
    if target == "browser" and not browser_ok:
        raise ComputerControlError("computer_unsupported_os", adapter.unavailable_reason)
    if target == "desktop" and not desktop_ok:
        raise ComputerControlError("computer_unsupported_os", adapter.unavailable_reason)
    if target == "browser":
        from core.computer.browser_backend import BrowserComputerBackend
        from core.computer.settings import (
            DEFAULT_BROWSER_VIEWPORT_HEIGHT,
            DEFAULT_BROWSER_VIEWPORT_WIDTH,
        )

        hint_provider = getattr(adapter, "browser_window_hints", None)
        if hint_provider is not None and not callable(hint_provider):
            hint_provider = None
        viewport = (identity.viewport_width, identity.viewport_height)
        if viewport[0] <= 0 or viewport[1] <= 0:
            viewport = (DEFAULT_BROWSER_VIEWPORT_WIDTH, DEFAULT_BROWSER_VIEWPORT_HEIGHT)
        if identity.browser == "embedded":
            from core.computer.embedded_browser_backend import EmbeddedBrowserBackend

            if not identity.cdp_endpoint:
                raise ComputerControlError(
                    "computer_embedded_unavailable", "内置浏览器 CDP endpoint 缺失"
                )
            return EmbeddedBrowserBackend(
                identity.cdp_endpoint,
                Path(identity.profile_path),
                viewport=viewport,
            )
        return BrowserComputerBackend(
            identity.browser,
            Path(identity.profile_path),
            window_hint_provider=hint_provider,
            use_process_worker=True,
            viewport=viewport,
        )
    return adapter.create_desktop_backend()


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
        backend_factory: Callable[[str, BackendIdentity], ComputerBackend] | None = None,
        settings_loader: Callable[[], object] | None = None,
        generation_provider: Callable[[], int] | None = None,
        abort_streams: Callable[[threading.Event], None] | None = None,
        embedded_endpoint_provider: Callable[[str], str] | None = None,
    ) -> None:
        self._backend_factory = backend_factory or _default_backend_factory
        self._settings_loader = settings_loader or _default_settings_loader
        self._generation_provider = generation_provider or _default_generation_provider
        self._abort_streams = abort_streams or _default_abort_streams
        self._embedded_endpoint_provider = embedded_endpoint_provider
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._run: _Run | None = None
        self._run_generation = 0
        self._retained: dict[str, tuple[BackendIdentity, ComputerBackend]] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._last_snapshot: ComputerRunSnapshot | None = None
        self._preview_publisher: Callable[[str, str], str] | None = None

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

    def set_preview_publisher(
        self, publisher: Callable[[str, str], str] | None
    ) -> None:
        """注册/清除观察截图预览发布器（GUI 预览路径，Task 16）。

        publisher(run_id, png_path) 返回预览 URL；发布失败由发布器自行留痕
        返回空串，绝不影响操控主链路。仅 GUI 路径接线（CLI 无预览 UI）；
        注册幂等，传 None 清除。
        """
        with self._lock:
            self._preview_publisher = publisher

    def set_embedded_endpoint_provider(
        self, provider: Callable[[str], str] | None
    ) -> None:
        with self._lock:
            self._embedded_endpoint_provider = provider

    def _build_event_locked(self, run: _Run, event_type: str, **fields) -> dict:
        run.event_seq += 1
        return {
            "type": event_type,
            "run_id": run.run_id,
            "run_generation": run.run_generation,
            "event_seq": run.event_seq,
            "owner_session_id": run.owner.session_id,
            **fields,
        }

    def _finish_action_event_locked(
        self,
        run: _Run,
        ordinal: int,
        action_kind: str,
        *,
        ok: bool,
        error_code: str,
        received_at: float,
        queue_ms: float,
        input_ms: float | None,
        wait_ms: float | None,
        observe_ms: float | None,
    ) -> dict:
        if self._run is run:
            run.current_action = ""
        return self._build_event_locked(
            run,
            "computer_action_finished",
            action_ordinal=ordinal,
            action_kind=action_kind,
            ok=ok,
            error_code=error_code,
            **self._timing_fields(received_at, queue_ms, input_ms, wait_ms, observe_ms),
        )

    def _dispatch(self, event: dict) -> None:
        """向 listener 派发事件；绝不在 Controller 状态锁内调用外部 listener。"""
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event)
            except Exception:
                _LOGGER.exception("电脑操控事件 listener 异常（已忽略）")

    def _emit(self, run: _Run, event_type: str, **fields) -> None:
        with self._lock:
            event = self._build_event_locked(run, event_type, **fields)
        self._dispatch(event)

    def _emit_state(self, run: _Run, reason: str = "") -> None:
        target = run.target or ""
        browser = run.capability.browser
        self._emit(
            run,
            "computer_state",
            status=run.status,
            target=target,
            browser=browser,
            browser_mode=resolve_browser_mode(target, browser),
            reason=reason,
        )

    @staticmethod
    def _timing_fields(
        received_at: float,
        queue_ms: float,
        input_ms: float | None,
        wait_ms: float | None,
        observe_ms: float | None,
    ) -> dict:
        """动作耗时字段（R9）：未发生的阶段不出现，绝不默认 0ms 伪装已测量。"""
        fields: dict = {
            "duration_ms": round((time.perf_counter() - received_at) * 1000, 3),
            "queue_ms": queue_ms,
        }
        if input_ms is not None:
            fields["input_ms"] = input_ms
        if wait_ms is not None:
            fields["wait_ms"] = wait_ms
        if observe_ms is not None:
            fields["observe_ms"] = observe_ms
        return fields

    def _transition(self, run: _Run, new_status: str) -> None:
        """显式转移表校验（调用方必须持有状态锁）；非法转换抛领域不变量错误。"""
        allowed = _TRANSITIONS.get(run.status, set())
        if new_status not in allowed:
            raise ComputerControlError(
                "computer_invalid_state",
                f"非法状态转换：{run.status} → {new_status}",
            )
        run.status = new_status

    def _snapshot_locked(self, run: _Run) -> ComputerRunSnapshot:
        target = run.target or ""
        browser = run.capability.browser
        return ComputerRunSnapshot(
            run_id=run.run_id,
            run_generation=run.run_generation,
            event_seq=run.event_seq,
            owner_session_id=run.owner.session_id,
            owner_turn_id=run.owner.turn_id,
            status=run.status,
            target=target,
            browser=browser,
            browser_mode=resolve_browser_mode(target, browser),
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

    def sync_observation_frame(self, run_id: str, frame: ObservationFrame) -> None:
        """同步 Provider 重缩放或超限禁用状态到当前 run 及 backend 的同一 frame 记录。"""
        if not frame.frame_id:
            return
        with self._lock:
            run = self._run
            if run is None or run.run_id != run_id:
                return
            if run.status not in (
                ComputerRunStatus.RUNNING.value,
                ComputerRunStatus.PAUSED.value,
            ):
                return
            run.frames[frame.frame_id] = frame
            while len(run.frames) > 32:
                run.frames.pop(next(iter(run.frames)))
            if run.current_frame is None or run.current_frame.frame_id == frame.frame_id:
                run.current_frame = frame
            backend = run.backend
        if backend is not None:
            sync_backend = getattr(backend, "sync_observation_frame", None)
            if callable(sync_backend):
                try:
                    sync_backend(frame)
                except ComputerControlError:
                    raise
                except Exception:
                    _LOGGER.warning("同步视觉帧逆变换到 backend 失败", exc_info=True)

    def get_observation_frame(self, run_id: str, frame_id: str) -> ObservationFrame | None:
        """读取指定 run/frame_id 的当前不可变视觉帧记录。"""
        with self._lock:
            run = self._run
            if run is None or run.run_id != run_id:
                return None
            return run.frames.get(frame_id)

    def _finalize_locked(self, run: _Run) -> None:
        run.current_action = ""
        self._last_snapshot = self._snapshot_locked(run)
        self._run = None

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
            prefix = "首次操控或" if run.action_count == 0 else ""
            raise ComputerControlError(
                "computer_invalid_action",
                f"{prefix}暂停/接管恢复后旧坐标已作废，必须先观察（observe）刷新画面",
            )
        if (
            _action_uses_coordinates(action)
            and run.current_frame is not None
            and not run.current_frame.coordinate_input_enabled
        ):
            raise coordinate_disabled_error(run.current_frame.disabled_reason)

    def _raise_if_action_aborted_locked(self, run: _Run) -> None:
        if run.owner.cancel_event.is_set():
            raise ComputerControlError(
                "computer_cancelled", "操控回合已取消"
            )
        if self._run is not run or run.status not in (
            ComputerRunStatus.RUNNING.value,
            ComputerRunStatus.PAUSED.value,
        ):
            raise ComputerControlError(
                "computer_cancelled", "run 在动作执行期间已停止"
            )

    def _generation_allowed_locked(
        self, run: _Run, effective_target: str | None = None
    ) -> tuple[bool, int]:
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

    def _resolve_backend_identity(
        self, target: str, capability: ComputerCapability, session_id: str = ""
    ) -> BackendIdentity:
        from core.computer.platforms import get_platform_adapter

        if target == "browser":
            from core.computer.settings import (
                get_browser_profile_dir,
                get_project_browser_viewport,
            )
            from core.workspace_context import get_workdir

            browser = capability.browser
            cdp_endpoint = ""
            if browser == "embedded":
                provider = self._embedded_endpoint_provider
                if provider is None:
                    raise ComputerControlError(
                        "computer_embedded_unavailable",
                        "当前进程没有内置浏览器（GUI 专属）；"
                        "请在设置中把浏览器切换为 Edge 或 Chrome",
                    )
                try:
                    cdp_endpoint = provider(session_id)
                except ComputerControlError:
                    raise
                except Exception as error:
                    raise ComputerControlError(
                        "computer_embedded_unavailable",
                        f"内置浏览器自动化准备失败：{error}",
                    ) from error
                if not cdp_endpoint:
                    raise ComputerControlError(
                        "computer_embedded_unavailable",
                        "内置浏览器 CDP endpoint 无效",
                    )
            elif browser not in ("edge", "chrome"):
                raise ComputerControlError(
                    "computer_invalid_config",
                    f"能力快照缺少有效浏览器选择：{browser!r}",
                )
            profile_path = str(get_browser_profile_dir(browser))
            viewport_width, viewport_height = get_project_browser_viewport(
                get_workdir()
            )
        else:
            browser = ""
            profile_path = ""
            cdp_endpoint = ""
            viewport_width = 0
            viewport_height = 0
        return BackendIdentity(
            platform=get_platform_adapter().name,
            target=target,
            browser=browser,
            profile_path=profile_path,
            cdp_endpoint=cdp_endpoint,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )

    def _claim_backend(self, target: str, identity: BackendIdentity) -> ComputerBackend:
        with self._lock:
            retained_entry = self._retained.pop(target, None)
        if retained_entry is not None:
            retained_identity, retained = retained_entry
            if retained_identity != identity:
                try:
                    retained.close()
                except Exception as error:
                    raise ComputerControlError(
                        "computer_cleanup_failed",
                        f"关闭旧配置电脑操控实例失败：{error}",
                    ) from error
            elif retained.is_reusable():
                return retained
            else:
                _close_backend_quietly(retained)
                raise ComputerControlError(
                    "computer_browser_closed",
                    "留存的浏览器会话已不可复用（窗口已关闭或实例不健康），"
                    "请停止后重新开始",
                )
        backend = None
        try:
            backend = self._backend_factory(target, identity)
            backend.start()
        except ComputerControlError:
            if backend is not None:
                _close_backend_quietly(backend)
            raise
        except Exception as error:
            if backend is not None:
                _close_backend_quietly(backend)
            raise ComputerControlError(
                "computer_backend_start_failed", f"电脑操控 backend 启动失败：{error}"
            ) from error
        return backend

    @staticmethod
    def _cleanup_backend_for_stop(
        backend: ComputerBackend | None, existing_error: Exception | None = None
    ) -> Exception | None:
        first_error = existing_error
        if backend is None:
            return first_error
        try:
            backend.release_inputs()
        except Exception as error:
            if first_error is None:
                first_error = error
            _LOGGER.exception("停止时释放电脑操控输入失败")
        try:
            backend.close()
        except Exception as error:
            if getattr(error, "code", "") == "computer_stop_unconfirmed":
                first_error = error
            elif first_error is None:
                first_error = error
            _LOGGER.exception("停止时关闭电脑操控 backend 失败")
        return first_error

    def _apply_stop_cleanup_outcome_locked(
        self, run: _Run, cleanup_error: Exception | None
    ) -> str:
        if cleanup_error is None:
            self._transition(run, ComputerRunStatus.STOPPED.value)
            run.stop_cleanup_in_progress = False
            run.cleanup_confirmed = True
            self._finalize_locked(run)
            return run.stop_reason
        code = str(getattr(cleanup_error, "code", "") or "computer_cleanup_failed")
        run.error_code = code
        run.current_action = ""
        run.stop_cleanup_in_progress = False
        if code != "computer_stop_unconfirmed":
            self._transition(run, ComputerRunStatus.FAILED.value)
        return "stop_cleanup_pending"

    def _finish_deferred_stop(
        self, run: _Run, backend: ComputerBackend, identity: BackendIdentity
    ) -> None:
        with self._lock:
            owns_cleanup = (
                self._run is run
                and run.status == ComputerRunStatus.STOPPING.value
            )
            if owns_cleanup:
                run.backend = backend
                run.backend_identity = identity
                run.backend_starting = False
                run.stop_cleanup_in_progress = True
        if not owns_cleanup:
            _close_backend_quietly(backend)
            return
        first_error: Exception | None = None
        request_stop = getattr(backend, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception as error:
                first_error = error
                _LOGGER.exception("关闭初始化中的电脑操控准入失败")
        cleanup_error = self._cleanup_backend_for_stop(
            backend, existing_error=first_error
        )
        emit_reason = ""
        with self._lock:
            if self._run is not run or run.status != ComputerRunStatus.STOPPING.value:
                return
            emit_reason = self._apply_stop_cleanup_outcome_locked(run, cleanup_error)
        self._emit_state(run, reason=emit_reason)

    def _fail_run(self, run: _Run, error: BaseException, reason: str) -> None:
        with self._lock:
            if self._run is run and run.status not in _TERMINAL_STATUSES:
                run.error_code = getattr(error, "code", "computer_backend_start_failed")
                try:
                    self._transition(run, ComputerRunStatus.FAILED.value)
                except ComputerControlError:
                    run.status = ComputerRunStatus.FAILED.value
                self._finalize_locked(run)
        self._emit_state(run, reason=reason)

    def start_run(
        self,
        owner: ComputerOwner,
        capability: ComputerCapability,
        target: str | None = None,
    ) -> ComputerRunSnapshot:
        """创建活动 run 并获取排他租约；target=None 时首次合法 observe 建立租约。"""
        # 用户已停止操控期间一律不准新开 run：否则模型可以换个 target（或换
        # 协议走 native）绕开 computer_turn_context_error 的抑制拦截。
        if stop_notice.is_user_stop_active():
            raise ComputerControlError(
                "computer_user_stopped",
                "用户已停止电脑操控：本任务后续不要调用任何电脑操控工具，"
                "请改用其它方式或直接向用户说明",
            )
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
        with self._lock:
            if self._run is not None:
                raise ComputerControlError(
                    "computer_session_busy",
                    f"电脑操控正被会话「{self._run.owner.session_label}」占用"
                    f"（当前状态：{self._run.status}）",
                    details={
                        "owner_session_label": self._run.owner.session_label,
                        "owner_run_status": self._run.status,
                    },
                )
            self._run_generation += 1
            run = _Run(
                owner,
                capability,
                target,
                run_generation=self._run_generation,
            )
            self._run = run
            self._transition(run, ComputerRunStatus.STARTING.value)
        self._emit_state(run, reason="start")
        if target is not None:
            try:
                identity = self._resolve_backend_identity(
                    target, capability, run.owner.session_id
                )
                backend = self._claim_backend(target, identity)
            except Exception as error:
                with self._lock:
                    run.backend_starting = False
                self._fail_run(run, error, "backend_start_failed")
                raise
            with self._lock:
                run.backend_starting = False
                run_alive = (
                    self._run is run
                    and run.status == ComputerRunStatus.STARTING.value
                )
                if run_alive:
                    run.backend = backend
                    run.backend_identity = identity
                    self._transition(run, ComputerRunStatus.RUNNING.value)
            if not run_alive:
                with self._lock:
                    deferred_stop = (
                        self._run is run
                        and run.status == ComputerRunStatus.STOPPING.value
                    )
                if deferred_stop:
                    self._finish_deferred_stop(run, backend, identity)
                else:
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
        """执行一个动作并返回全新 observation；每个动作由单一 action_lock 串行。

        R9：动作计时全部为本地事件数据（monotonic/perf_counter），成功、失败、
        取消都携带 duration_ms；queue/input/wait/observe 分段只在该阶段真实
        发生时才出现（绝不默认 0ms 暗示瞬间完成），不进入 Provider 观察投影。
        """
        received_at = time.perf_counter()
        with self._action_lock:
            queue_ms = round((time.perf_counter() - received_at) * 1000, 3)
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
                    run.backend_starting = True
                elif action.target != run.target:
                    raise ComputerControlError(
                        "computer_invalid_action",
                        f"本 run 已绑定 target={run.target!r}，"
                        f"拒绝 target={action.target!r} 的动作",
                    )
            if establishing:
                try:
                    identity = self._resolve_backend_identity(
                        action.target, run.capability, run.owner.session_id
                    )
                    backend = self._claim_backend(action.target, identity)
                except Exception as error:
                    with self._lock:
                        run.backend_starting = False
                    self._fail_run(run, error, "backend_start_failed")
                    raise
                backend_attached = False
                with self._lock:
                    run.backend_starting = False
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
                            run.backend_identity = identity
                            run.target = action.target
                            backend_attached = True
                if not backend_attached:
                    with self._lock:
                        deferred_stop = (
                            self._run is run
                            and run.status == ComputerRunStatus.STOPPING.value
                        )
                    if deferred_stop:
                        self._finish_deferred_stop(run, backend, identity)
                    else:
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
                previous_screen_sha256 = run.last_screen_sha256
                previous_frame_identity = run.last_frame_identity
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

            input_ms: float | None = None
            wait_ms: float | None = None
            observe_ms: float | None = None
            try:
                if action.kind in ("observe", "request_user_takeover"):
                    pass
                else:
                    input_started = time.perf_counter()
                    try:
                        steps = action.steps if action.kind == "sequence" else (action,)
                        completed_steps = 0
                        step_submitted = False
                        try:
                            for step_idx, step in enumerate(steps):
                                step_submitted = False
                                with self._lock:
                                    self._raise_if_action_aborted_locked(run)
                                step_submitted = True
                                backend.execute(run_id, step)
                                completed_steps = step_idx + 1
                        except ComputerControlError as seq_error:
                            if action.kind == "sequence":
                                from core.computer.actions import annotate_sequence_step_error

                                failed_idx = min(completed_steps, len(steps) - 1)
                                raise annotate_sequence_step_error(
                                    seq_error,
                                    step_index=failed_idx,
                                    step_kind=steps[failed_idx].kind,
                                    total_steps=len(steps),
                                    submitted_before_call=step_submitted,
                                ) from seq_error
                            raise
                    finally:
                        elapsed_ms = round((time.perf_counter() - input_started) * 1000, 3)
                        if action.kind == "wait":
                            input_ms = elapsed_ms
                            wait_ms = elapsed_ms
                        elif action.kind == "wait_for":
                            # 条件等待单列（Global：不改变既有 wait 语义）：
                            # wait_for 只轮询界面、不产生任何输入，故 input_ms 记
                            # 0（真正的零输入），轮询耗时全部计入 wait_ms，绝不
                            # 把被动等待混进输入耗时。
                            input_ms = 0
                            wait_ms = elapsed_ms
                        else:
                            input_ms = elapsed_ms
                with self._lock:
                    self._raise_if_action_aborted_locked(run)
                observe_started = time.perf_counter()
                try:
                    observation = backend.observe(run_id)
                except ComputerControlError as obs_error:
                    if action.kind == "sequence":
                        details = dict(obs_error.details) if obs_error.details else {}
                        details["failed_step_index"] = None
                        details["submission_state"] = (
                            "outcome_unknown"
                            if obs_error.code == "computer_action_timeout"
                            else "submitted"
                        )
                        details["completed_steps"] = len(action.steps or ())
                        details["total_steps"] = len(action.steps or ())
                        raise ComputerControlError(
                            obs_error.code, obs_error.message, details=details
                        ) from obs_error
                    raise
                finally:
                    observe_ms = round((time.perf_counter() - observe_started) * 1000, 3)
                with self._lock:
                    self._raise_if_action_aborted_locked(run)
            except ComputerControlError as error:
                with self._lock:
                    if error.code in _SURFACE_ERROR_CODES and self._run is run:
                        # 受控画面已切换/无法唯一确定：旧坐标作废（R5），
                        # 后续动作必须先 observe，不得继续按旧坐标输入。
                        run.require_observe = True
                    error_event = self._finish_action_event_locked(
                        run,
                        ordinal,
                        action.kind,
                        ok=False,
                        error_code=error.code,
                        received_at=received_at,
                        queue_ms=queue_ms,
                        input_ms=input_ms,
                        wait_ms=wait_ms,
                        observe_ms=observe_ms,
                    )
                self._dispatch(error_event)
                raise
            except Exception as error:
                with self._lock:
                    error_event = self._finish_action_event_locked(
                        run,
                        ordinal,
                        action.kind,
                        ok=False,
                        error_code="computer_backend_error",
                        received_at=received_at,
                        queue_ms=queue_ms,
                        input_ms=input_ms,
                        wait_ms=wait_ms,
                        observe_ms=observe_ms,
                    )
                self._dispatch(error_event)
                raise ComputerControlError(
                    "computer_backend_error", f"电脑操控 backend 异常：{error}"
                ) from error

            after_fingerprint = _surface_fingerprint(observation)
            cur_frame_identity = _observation_frame_identity(observation)
            has_screenshot = bool(observation.png_path or (not observation.frame_id and not observation.screenshot_error))
            initial_frame = (
                frame_from_observation(
                    observation,
                    run_id=run.run_id,
                    coordinate_input_enabled=has_screenshot,
                    disabled_reason="" if has_screenshot else "no_screenshot",
                )
                if observation.ok
                else None
            )
            observation = replace(
                observation,
                action=action.kind,
                before_fingerprint=before_fingerprint,
                after_fingerprint=after_fingerprint,
                # 帧契约：仅当视觉像素、surface、视口、裁剪与页面来源全一致时才判定为未变；
                # 绝不能仅靠 screen_sha256 相同就掩盖切页/缩放/文档重建。
                screen_unchanged=bool(
                    previous_screen_sha256
                    and previous_screen_sha256 == observation.screen_sha256
                    and previous_frame_identity == cur_frame_identity
                ),
            )
            publisher = self._preview_publisher
            if publisher is not None and observation.png_path:
                # Task 16：observation 产生时发布 PNG 到惰性预览服务（GUI
                # 路径）；发布失败只留痕返回空串，绝不影响动作主链路。
                try:
                    published_url = publisher(run.run_id, observation.png_path)
                except Exception:
                    _LOGGER.exception("发布电脑操控观察截图到预览服务失败")
                    published_url = ""
                if published_url:
                    observation = replace(observation, preview_url=published_url)
            no_progress_hit = False
            commit_failure: ComputerControlError | None = None
            finished_event: dict | None = None
            observation_event: dict | None = None
            with self._lock:
                try:
                    self._raise_if_action_aborted_locked(run)
                except ComputerControlError as error:
                    commit_failure = error
                if commit_failure is None:
                    allowed, current = self._generation_allowed_locked(run)
                    if allowed:
                        run.generation = current
                    else:
                        stop_for_settings = True
                if commit_failure is None and not stop_for_settings:
                    run.last_fingerprint = after_fingerprint
                    if observation.ok:
                        run.last_screen_sha256 = observation.screen_sha256
                        run.last_frame_identity = cur_frame_identity
                    if initial_frame is not None:
                        if initial_frame.frame_id:
                            run.frames[initial_frame.frame_id] = initial_frame
                            while len(run.frames) > 32:
                                run.frames.pop(next(iter(run.frames)))
                        run.current_frame = initial_frame
                    run.current_surface = (
                        observation.page_origin or observation.window_title
                    )
                    run.preview_url = observation.preview_url
                    run.require_observe = bool(
                        not observation.ok
                        or observation.error_code in _SURFACE_ERROR_CODES
                    )
                    key = (observation.screen_sha256, after_fingerprint)
                    run.last_progress_key, run.no_progress_count = next_progress_state(
                        run.last_progress_key, key, run.no_progress_count, action.kind
                    )
                    no_progress_hit = run.no_progress_count >= _NO_PROGRESS_LIMIT
                    finished_event = self._finish_action_event_locked(
                        run,
                        ordinal,
                        action.kind,
                        ok=not no_progress_hit,
                        error_code="computer_no_progress" if no_progress_hit else "",
                        received_at=received_at,
                        queue_ms=queue_ms,
                        input_ms=input_ms,
                        wait_ms=wait_ms,
                        observe_ms=observe_ms,
                    )
                    if not no_progress_hit:
                        obs_target = observation.target or run.target or ""
                        observation_event = self._build_event_locked(
                            run,
                            "computer_observation",
                            action_ordinal=ordinal,
                            target=obs_target,
                            browser=run.capability.browser,
                            browser_mode=resolve_browser_mode(
                                obs_target, run.capability.browser
                            ),
                            frame_id=observation.frame_id,
                            observation_error=observation.screenshot_error,
                            observed_at_ms=int(time.time() * 1000),
                            screen_sha256=observation.screen_sha256,
                            width=observation.width,
                            height=observation.height,
                            origin_or_window=(
                                observation.page_origin or observation.window_title
                            ),
                            preview_url=observation.preview_url,
                            surface_id=observation.surface_id,
                            surface_generation=observation.surface_generation,
                        )
                elif commit_failure is not None:
                    finished_event = self._finish_action_event_locked(
                        run,
                        ordinal,
                        action.kind,
                        ok=False,
                        error_code=commit_failure.code,
                        received_at=received_at,
                        queue_ms=queue_ms,
                        input_ms=input_ms,
                        wait_ms=wait_ms,
                        observe_ms=observe_ms,
                    )

            if commit_failure is not None:
                if finished_event is not None:
                    self._dispatch(finished_event)
                raise commit_failure
            if stop_for_settings:
                self.stop(run_id, "settings_disabled")
                raise ComputerControlError(
                    "computer_disabled", "电脑操控开关已关闭，run 已停止"
                )
            if no_progress_hit:
                self._dispatch(finished_event)
                self.stop(run_id, "no_progress")
                raise ComputerControlError(
                    "computer_no_progress",
                    f"连续 {_NO_PROGRESS_LIMIT} 次动作后截图与活动指纹完全不变，已停止电脑操控",
                )

            self._dispatch(finished_event)
            self._dispatch(observation_event)

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
        # 有未结束 worker/不健康/已封闭的实例不得进入 retained（R2/R4）：
        # 不可复用时按不留存路径关闭。
        if retain and not backend.is_reusable():
            retain = False
        first_error = None
        if backend is not None:
            # 留存复用也必须先释放按下状态（R3）：retain 不等于残留修饰键/
            # 鼠标键进入下一 run；仅 desktop/不留存路径额外 close。
            try:
                backend.release_inputs()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("complete_run 释放电脑操控输入失败")
            if not retain:
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
                        self._retained[run.target] = (run.backend_identity, backend)
                self._finalize_locked(run)
        self._emit_state(
            run, reason="complete" if first_error is None else "cleanup_failed"
        )
        return self.snapshot()

    def stop(
        self,
        run_id: str | None = None,
        reason: str = "application_exit",
        *,
        cancel_owner: bool = True,
    ) -> ComputerRunSnapshot:
        if run_id is None:
            with self._lock:
                active = self._run
                active_run_id = active.run_id if active is not None else ""
            if active_run_id:
                try:
                    self.stop(active_run_id, reason)
                except ComputerControlError:
                    _LOGGER.warning(
                        "退出时停止活动电脑操控 run 失败（已在收尾，忽略）",
                        exc_info=True,
                    )
            with self._lock:
                retained = [entry[1] for entry in self._retained.values()]
                self._retained.clear()
            for backend in retained:
                _close_backend_quietly(backend)
            return self.snapshot()
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
            if run.status in _TERMINAL_STATUSES and (
                run.status != ComputerRunStatus.FAILED.value
                or run.cleanup_confirmed
            ):
                return self._snapshot_locked(run)
            if run.status == ComputerRunStatus.STOPPING.value:
                if run.stop_cleanup_in_progress:
                    return self._snapshot_locked(run)
                retrying_stop = True
                run.stop_cleanup_in_progress = True
            elif run.status == ComputerRunStatus.FAILED.value:
                retrying_stop = True
                self._transition(run, ComputerRunStatus.STOPPING.value)
                run.error_code = ""
                run.stop_cleanup_in_progress = True
            else:
                retrying_stop = False
                self._transition(run, ComputerRunStatus.STOPPING.value)
                run.stop_reason = reason
                run.stop_cancel_owner = bool(cancel_owner)
                run.stop_cleanup_in_progress = not run.backend_starting
            deferred_backend_start = run.backend_starting
            backend = run.backend
            reason = run.stop_reason
            cancel_owner = run.stop_cancel_owner
        if deferred_backend_start:
            with self._lock:
                if self._run is run:
                    run.stop_cleanup_in_progress = False
        first_error = None
        request_stop = getattr(backend, "request_stop", None)
        if callable(request_stop):
            try:
                request_stop()
            except Exception as error:
                first_error = error
                _LOGGER.exception("关闭电脑操控 backend 输入准入失败")
        self._emit_state(run, reason=reason)
        if cancel_owner and not retrying_stop:
            # 回合正常收尾的兜底释放传 cancel_owner=False：不得把 cancel_event
            # 置位（gui_bridge.py:6220/:6377 用它判定回合是否被用户取消）。
            try:
                run.owner.cancel_event.set()
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("设置电脑操控 owner cancel event 失败")
            try:
                self._abort_streams(run.owner.cancel_event)
            except Exception as error:
                first_error = first_error or error
                _LOGGER.exception("中断在飞 Provider 流失败")
        if deferred_backend_start:
            return self.snapshot()
        cleanup_error = self._cleanup_backend_for_stop(
            backend, existing_error=first_error
        )
        emit_reason = reason
        with self._lock:
            if self._run is run:
                emit_reason = self._apply_stop_cleanup_outcome_locked(
                    run, cleanup_error
                )
        self._emit_state(run, reason=emit_reason)
        return self.snapshot()

    def stop_for_user(
        self, run_id: str, reason: str = "user_stop"
    ) -> ComputerRunSnapshot:
        snapshot = self.stop(run_id, reason, cancel_owner=False)
        # 顺序：先真正停住，再立禁止态。stop 抛错（未知 run / 非法状态）时
        # 不会走到这里，避免"没停成却已经宣告用户停止"。
        stop_notice.activate_user_stop(reason)
        stop_notice.queue_stop_notice(reason)
        return snapshot


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
