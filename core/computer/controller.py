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
- Task 16：stop(run_id=None) 是进程退出统一入口——先停活动 run 再关闭全部
  retained backend；set_preview_publisher 注册观察截图发布器（GUI 预览路径，
  发布失败只留痕不影响主链路）。
- complete_run 与 stop 幂等；Turn 终态 finally 必须二选一调用，不留 running 租约。
- 浏览器 backend 在 retain 时不 close，作为无执行权的 retained session 供下一
  run 显式复用；desktop backend 在 complete 时立即 release_inputs + close。
"""

from __future__ import annotations

import logging
import sys
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
)
from core.computer import stop_notice as stop_notice

_LOGGER = logging.getLogger(__name__)


def _default_settings_loader():
    """惰性 import 设置模块：它连带 platforms/config（约 48ms），
    CLI 轻量模式下不创建 controller，没必要在启动期付这笔成本。"""
    from core.computer.settings import load_computer_settings

    return load_computer_settings()


def _default_generation_provider() -> int:
    from core.computer.settings import get_settings_generation

    return get_settings_generation()

# 显式状态转移表（计划 Task 8 Step 3 逐字给定，非法转换抛领域不变量错误）。
# 唯一有意放宽项（2026-10-01 实机复现后新增）：waiting_for_user → completing。
# 用户接管态在回合正常收尾时按"完成"终结——本回合的操控工作正常收尾并交还
# 排他租约；不放行该转移会让 run 既无终态也无清理，租约永久留活，下一回合被
# computer_session_busy 永久挡死（日志：非法状态转换：waiting_for_user → completing）。
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
    "failed": {"starting"},
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
_NO_PROGRESS_EXEMPT_KINDS = frozenset(
    {"move", "observe", "wait", "request_user_takeover", "list_surfaces", "observe_region"}
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
    """无进展计数纯函数（R6）。

    1. 画面/有效目标状态改变：计数归零（不设为 1）并更新基线；
    2. move/observe/wait/request_user_takeover 不增加失败计数；
    3. 豁免动作观察到真实变化同样归零并更新基线（不留过期基线）；
    4. 豁免动作且画面不变：保留既有失败次数（不得靠插入 observe 掩盖
       重复失败）。

    该机制只作“重复不变”限制，不宣称等价于任务语义成功；光标闪烁/时钟
    造成的像素变化只更新基线，不声称业务完成。
    """
    if current_key != previous_key:
        return current_key, 0
    if kind in _NO_PROGRESS_EXEMPT_KINDS:
        return current_key, count
    return current_key, count + 1


class _Run:
    """活动 run 的可变内部状态；字段访问由 controller 的状态锁保护。"""

    def __init__(self, owner: ComputerOwner, capability: ComputerCapability, requested_target: str | None):
        self.run_id = f"computer-run-{uuid.uuid4().hex[:12]}"
        self.owner = owner
        self.capability = capability
        self.requested_target = requested_target
        self.target: str | None = requested_target  # generic run 首次 observe 后绑定
        self.backend: ComputerBackend | None = None
        self.backend_identity: BackendIdentity | None = None
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


def _default_backend_factory(target: str, identity: BackendIdentity) -> ComputerBackend:
    """生产 backend 构造：惰性导入，模块顶层不触碰 Playwright/PIL/Win32。

    browser/profile 一律取自 run 冻结的 ``identity``（R4），不再独立重读
    全局设置——避免与 Turn 冻结能力中的 browser 不一致。
    """
    from core.computer.platforms import get_platform_adapter

    if target not in ("browser", "desktop"):
        raise ComputerControlError("computer_invalid_action", f"未知电脑操控目标：{target!r}")
    adapter = get_platform_adapter()
    if not adapter.supported:
        raise ComputerControlError("computer_unsupported_os", adapter.unavailable_reason)
    if target == "browser":
        from core.computer.browser_backend import BrowserComputerBackend
        from core.computer.settings import (
            DEFAULT_BROWSER_VIEWPORT_HEIGHT,
            DEFAULT_BROWSER_VIEWPORT_WIDTH,
        )

        # R5：系统侧受控页裁决信号（窗口标题提示）由平台层提供；
        # 平台未实现时 getattr 回落 None，backend 退化为 DOM 可见性裁决。
        hint_provider = getattr(adapter, "browser_window_hints", None)
        if hint_provider is not None and not callable(hint_provider):
            hint_provider = None
        # 项目分辨率（逻辑视口）对两种浏览器通道一致生效：外部浏览器作为
        # 启动视口，内置浏览器作为缩放呈现的目标视口。
        viewport = (identity.viewport_width, identity.viewport_height)
        if viewport[0] <= 0 or viewport[1] <= 0:
            viewport = (DEFAULT_BROWSER_VIEWPORT_WIDTH, DEFAULT_BROWSER_VIEWPORT_HEIGHT)
        if identity.browser == "embedded":
            from core.computer.embedded_browser_backend import EmbeddedBrowserBackend

            if not identity.cdp_endpoint:
                raise ComputerControlError(
                    "computer_embedded_unavailable", "内置浏览器 CDP endpoint 缺失"
                )
            # 不传窗口标题提示：浏览器归 GUI 所有，PID/窗口裁决不适用，
            # backend 内部退化为 DOM 可见性裁决。
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
        # 内置浏览器（embedded）CDP endpoint 提供者：仅 GUI 进程注入——
        # 负责确保内嵌 WebView2 控件带调试端口存在并返回回环 endpoint；
        # CLI/无 GUI 不注入，embedded 目标一律明确失败（不静默回落外部浏览器）。
        self._embedded_endpoint_provider = embedded_endpoint_provider
        self._lock = threading.RLock()
        self._action_lock = threading.Lock()
        self._run: _Run | None = None
        self._retained: dict[str, tuple[BackendIdentity, ComputerBackend]] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._last_snapshot: ComputerRunSnapshot | None = None
        self._preview_publisher: Callable[[str, str], str] | None = None

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
        """注册/清除内置浏览器 CDP endpoint 提供者（仅 GUI 进程接线）。

        provider(session_id) 负责确保内嵌 WebView2 控件带调试端口存在并返回
        "http://127.0.0.1:<port>"；session_id 为发起操控的会话名（供前端把
        浏览器页签开进正确会话），抛出的异常由调用方归一为启动失败。
        """
        with self._lock:
            self._embedded_endpoint_provider = provider

    def _build_event_locked(self, run: _Run, event_type: str, **fields) -> dict:
        """状态锁内分配事件序号并构造事件（调用方必须持有状态锁）。

        每个事件都带 owner_session_id：前端靠首个事件建立 run 运行态，而
        「操控」页签按 run 归属会话登记——归属只存在于快照时，首帧页面就会被
        开进用户当前查看的会话（后台会话发起的操控串台，2026-10-30 反馈）。
        事件里带上是零成本的：字段是短字符串，不随观察截图变大。
        """
        run.event_seq += 1
        return {
            "type": event_type,
            "run_id": run.run_id,
            "event_seq": run.event_seq,
            "owner_session_id": run.owner.session_id,
            **fields,
        }

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
        self._emit(
            run,
            "computer_state",
            status=run.status,
            target=run.target or "",
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

    def _raise_if_action_aborted_locked(self, run: _Run) -> None:
        """动作执行中段/提交点复核（调用方必须持有状态锁）。

        取消（owner.cancel_event）或失去执行权（run 被停止/终结）立即抛
        computer_cancelled；暂停不撤销已开始的动作（设计 15.3：暂停只阻止
        下一动作开始），PAUSED 仍允许当前动作完成并提交。
        """
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

    def _resolve_backend_identity(
        self, target: str, capability: ComputerCapability, session_id: str = ""
    ) -> BackendIdentity:
        """按 run 冻结能力构造 backend 请求身份；profile 在本次领取时解析为规范绝对路径。

        embedded（内置 WebView2）在此处即调用 endpoint 提供者：确保控件带
        调试端口存在（GUI 线程 marshal），端点冻结进身份——端口变化即视为
        配置变更、retained 会话不复用。项目分辨率同点冻结（R4）。
        session_id 为发起会话名，透传给提供者供前端按会话归属展示浏览器页签。
        """
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
        """领取 backend：留存实例必须身份完全相同且可复用，否则显式处理。

        - 身份不匹配（配置变更生命周期）：显式关闭旧实例，确认清理后才创建
          新配置指定实例；清理失败则本次启动失败，绝不静默丢弃旧对象；
        - 身份相同但实例已不可复用（窗口被关闭/实例不健康/有未结束命令）：
          明确失败，不暗中自动复活——重新打开是新一轮显式启动行为。
        """
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
        try:
            backend = self._backend_factory(target, identity)
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
        run = _Run(owner, capability, target)
        with self._lock:
            if self._run is not None:
                # L3 可诊断性：占用错误带上被占 run 的当前状态，让"下一回合
                # 拿不到租约"一眼可诊断（该等它结束还是该手动停止），不必翻日志。
                raise ComputerControlError(
                    "computer_session_busy",
                    f"电脑操控正被会话「{self._run.owner.session_label}」占用"
                    f"（当前状态：{self._run.status}）",
                    details={
                        "owner_session_label": self._run.owner.session_label,
                        "owner_run_status": self._run.status,
                    },
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
                self._fail_run(run, error, "backend_start_failed")
                raise
            with self._lock:
                run_alive = (
                    self._run is run
                    and run.status == ComputerRunStatus.STARTING.value
                )
                if run_alive:
                    run.backend = backend
                    run.backend_identity = identity
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
                    identity = self._resolve_backend_identity(
                        action.target, run.capability, run.owner.session_id
                    )
                    backend = self._claim_backend(action.target, identity)
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
                            run.backend_identity = identity
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
                # Task D：本步进入前的画面指纹基线（激活吞击判定用）。
                previous_screen_sha256 = run.last_screen_sha256
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
                    # observe 的 fresh observation 即动作本身；
                    # request_user_takeover 由 controller 处理，不进入 backend。
                    pass
                else:
                    input_started = time.perf_counter()
                    try:
                        # sequence（2026-09-06 协议扩展）：子步骤串行执行，每步前
                        # 复核取消；中途绝不 observe（模型只拿最终一张截图），
                        # 整体计 1 个 ordinal 并参与无进展计数。
                        steps = action.steps if action.kind == "sequence" else (action,)
                        for step in steps:
                            with self._lock:
                                self._raise_if_action_aborted_locked(run)
                            backend.execute(run_id, step)
                    finally:
                        input_ms = round((time.perf_counter() - input_started) * 1000, 3)
                        if action.kind == "wait":
                            # 显式等待阶段单列；不把条件等待混进输入耗时。
                            wait_ms = input_ms
                with self._lock:
                    # backend 输入返回后、开始可能耗时的 observe 前复核取消。
                    self._raise_if_action_aborted_locked(run)
                observe_started = time.perf_counter()
                try:
                    observation = backend.observe(run_id)
                finally:
                    observe_ms = round((time.perf_counter() - observe_started) * 1000, 3)
                with self._lock:
                    # observe 返回后、预览发布与成功提交前复核取消。
                    self._raise_if_action_aborted_locked(run)
                if (
                    action.kind == "click"
                    and observation.input_activated
                    and previous_screen_sha256 == observation.screen_sha256
                ):
                    # Task D（P5）激活吞击自愈：click 提交后画面与点击前完全
                    # 一致、且本步发生了前台切换——判定第一击被焦点切换吞掉。
                    # 同一动作原样补击一次并重新观察，以第二次观察为准；仍计
                    # 1 个 ordinal，补击前后同样复核取消（最多补击一次）。
                    with self._lock:
                        self._raise_if_action_aborted_locked(run)
                    backend.execute(run_id, action)
                    with self._lock:
                        self._raise_if_action_aborted_locked(run)
                    reobserve_started = time.perf_counter()
                    try:
                        observation = backend.observe(run_id)
                    finally:
                        observe_ms = (observe_ms or 0) + round(
                            (time.perf_counter() - reobserve_started) * 1000, 3
                        )
                    with self._lock:
                        self._raise_if_action_aborted_locked(run)
            except ComputerControlError as error:
                if error.code in _SURFACE_ERROR_CODES:
                    # 受控画面已切换/无法唯一确定：旧坐标作废（R5），
                    # 后续动作必须先 observe，不得继续按旧坐标输入。
                    with self._lock:
                        if self._run is run:
                            run.require_observe = True
                self._emit(
                    run,
                    "computer_action_finished",
                    action_ordinal=ordinal,
                    action_kind=action.kind,
                    ok=False,
                    error_code=error.code,
                    **self._timing_fields(
                        received_at, queue_ms, input_ms, wait_ms, observe_ms
                    ),
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
                    **self._timing_fields(
                        received_at, queue_ms, input_ms, wait_ms, observe_ms
                    ),
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
                # 帧契约：与上一步截图字节完全相同时打标，Provider 投影据此
                # 省掉重复附图（画面没变，模型手里已有同一张图）。previous 为
                # 空表示本 run 的第一张图，必须照常附上。
                screen_unchanged=bool(
                    previous_screen_sha256
                    and previous_screen_sha256 == observation.screen_sha256
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
                # 成功提交点（R1）：取消/run 执行权/状态/generation 复核、状态
                # 更新与成功事件序号分配在同一临界区完成。取消先于提交则不发布
                # 任何成功结果；提交先完成、随后取消则保留此前已成功动作。
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
                    run.last_screen_sha256 = observation.screen_sha256
                    run.current_surface = (
                        observation.page_origin or observation.window_title
                    )
                    run.preview_url = observation.preview_url
                    run.require_observe = False
                    key = (observation.screen_sha256, after_fingerprint)
                    run.last_progress_key, run.no_progress_count = next_progress_state(
                        run.last_progress_key, key, run.no_progress_count, action.kind
                    )
                    no_progress_hit = run.no_progress_count >= _NO_PROGRESS_LIMIT
                    if no_progress_hit:
                        finished_event = self._build_event_locked(
                            run,
                            "computer_action_finished",
                            action_ordinal=ordinal,
                            action_kind=action.kind,
                            ok=False,
                            error_code="computer_no_progress",
                            **self._timing_fields(
                                received_at, queue_ms, input_ms, wait_ms, observe_ms
                            ),
                        )
                    else:
                        finished_event = self._build_event_locked(
                            run,
                            "computer_action_finished",
                            action_ordinal=ordinal,
                            action_kind=action.kind,
                            ok=True,
                            error_code="",
                            **self._timing_fields(
                                received_at, queue_ms, input_ms, wait_ms, observe_ms
                            ),
                        )
                        observation_event = self._build_event_locked(
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
                            surface_id=observation.surface_id,
                            surface_generation=observation.surface_generation,
                        )

            if commit_failure is not None:
                # 取消先于提交：恰好一条 finished(ok=False)，零成功事件、
                # 零新观察 follow-up（generic 结果回填走既有稳定错误结构）。
                self._emit(
                    run,
                    "computer_action_finished",
                    action_ordinal=ordinal,
                    action_kind=action.kind,
                    ok=False,
                    error_code=commit_failure.code,
                    **self._timing_fields(
                        received_at, queue_ms, input_ms, wait_ms, observe_ms
                    ),
                )
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
        """取消/故障/紧急停止终态：固定顺序清理并关闭 backend；幂等。

        顺序：状态置 stopping → owner.cancel_event.set() → abort 在飞 Provider 流
        → backend release_inputs → backend close → 状态终结 → 租约释放 →
        final event。每步独立 try/except 保证后续释放；首个清理错误记为 failed，
        绝不伪装成 stopped success。

        cancel_owner（默认 True）：为 False 时跳过 `owner.cancel_event.set()`
        与 `_abort_streams` 两步，其余清理顺序完全不变。回合正常收尾的兜底
        释放必须传 False——`gui_bridge.py:6220` 与 `:6377` 用该 cancel_event
        判定"回合是否被用户取消"，若兜底释放设置它，会把正常结束的回合误标
        成"已取消"。

        幂等：已处终态返回终态快照；已处 stopping（停止在途）直接返回当前
        快照，不重复释放/关闭 backend、不重复终结——否则 stopping → stopping
        非法会中断正在进行的清理链（日志 2026-09-10 有两条实例）。

        run_id 为 None（Task 16 进程退出统一入口）：先按上表停止当前活动
        run（若有），再关闭全部 retained browser session；无活动 run 时幂等
        返回当前快照。活动 run 停止竞态失败（已在收尾）只留痕，retained
        清理照常执行。
        """
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
            if run.status in _TERMINAL_STATUSES:
                return self._snapshot_locked(run)
            if run.status == ComputerRunStatus.STOPPING.value:
                # 停止在途：另一次 stop 已进入清理链，本次幂等返回当前快照，
                # 不重复释放/关闭 backend、不重复终结。放行会让 _transition
                # 抛"stopping → stopping"并中断那条清理链。
                return self._snapshot_locked(run)
            self._transition(run, ComputerRunStatus.STOPPING.value)
            run.stop_reason = reason
        self._emit_state(run, reason=reason)

        first_error = None
        if cancel_owner:
            # 回合正常收尾的兜底释放传 cancel_owner=False：不得把 cancel_event
            # 置位（gui_bridge.py:6220/:6377 用它判定回合是否被用户取消）。
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

    def stop_for_user(
        self, run_id: str, reason: str = "user_stop"
    ) -> ComputerRunSnapshot:
        """用户主动停止（GUI 停止按钮 / computer_control stop / 紧急热键）。

        为什么不能直接用 stop()：stop 默认 cancel_owner=True 会把回合级
        cancel_event 置位并中断在飞 Provider 流，效果等于"点停止 = 终止整个
        对话回合"，模型连一句解释都收不到。用户停止只应结束电脑操控 run，
        因此这里传 cancel_owner=False 复用同一条清理链（顺序、幂等、错误
        处理与 stop 完全一致），并另外记录会话级禁止态 + 排队一条模型可见
        通知，让模型下一步知道"用户停止"而不是被静默掐断。

        抑制状态只在设置重开或用户消息命中恢复关键词时解除（见
        core/computer/stop_notice.py），因此本方法同时是 start_run 的闸门来源。
        """
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
