from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

from core.computer.models import ComputerControlError

_LOGGER = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({"completed", "stopped", "failed"})

_PREVIEW_ROOT_DIRNAME = "ycode-computer-preview"

_RUNTIME_LOCK = threading.Lock()
_RUNTIME: "ComputerRuntime | None" = None


class ComputerRuntime:
    """进程级轻量运行时容器：全局热键持有者 + 惰性预览发布器。

    不持有 controller/backend：默认关闭路径只读 settings/hotkey，绝不导入
    Playwright、绝不启动浏览器/截图。预览服务在第一次 publish_observation
    时才创建并启动（loopback + 随机 token，见 preview_server.py）。
    """

    def __init__(self, *, hotkey=None, preview_root=None) -> None:
        self._hotkey = hotkey
        self._preview_root = (
            Path(preview_root)
            if preview_root is not None
            else _default_preview_root()
        )
        self._preview = None
        self._preview_lock = threading.Lock()

    @property
    def hotkey(self):
        """已注册的 EmergencyStopHotkey；未注册/注册失败为 None。"""
        return self._hotkey

    @property
    def preview(self):
        """惰性创建的预览服务实例；从未发布过观察为 None。"""
        with self._preview_lock:
            return self._preview

    def publish_observation(self, run_id: str, png_path: str) -> str:
        """把 observation PNG 发布到惰性预览服务，返回可访问 URL。

        发布失败（IO/HTTP 启动失败）只留痕返回空串——预览是增强能力，
        绝不影响操控主链路；URL 仅供 GUI 事件消费（含随机 token，设计 16.3）。
        """
        with self._preview_lock:
            if self._preview is None:
                from core.computer.preview_server import ComputerPreviewServer

                self._preview = ComputerPreviewServer(self._preview_root)
                self._preview.start()
            preview = self._preview
        try:
            with open(png_path, "rb") as handle:
                png = handle.read()
        except OSError:
            _LOGGER.exception("读取观察截图失败，跳过预览发布")
            return ""
        try:
            return preview.publish(run_id, png)
        except Exception:
            _LOGGER.exception("发布观察截图到预览服务失败")
            return ""


def _default_preview_root() -> Path:
    return Path(tempfile.gettempdir()) / _PREVIEW_ROOT_DIRNAME


# 临时目录清扫（2026-09-06 提速计划 Task 7）：崩溃/断电可能残留 backend 截图
# 目录与预览目录。mtime 超过 24h 才删——时间窗防止多实例场景误删正在使用的目录。
_STALE_TEMP_DIR_PREFIXES = (
    "ycode-computer-desktop-",
    "ycode-computer-browser-",
    "ycode-computer-preview",
)
_STALE_TEMP_MAX_AGE_S = 24 * 3600


def sweep_stale_computer_temp_dirs(
    *, root=None, max_age_s: int = _STALE_TEMP_MAX_AGE_S, now: float | None = None
) -> int:
    """清扫崩溃残留的电脑操控临时目录；失败只留痕，绝不影响启动。"""
    import shutil
    import time as _time

    root = Path(root) if root is not None else Path(tempfile.gettempdir())
    now = _time.time() if now is None else now
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_dir() or not any(
                entry.name.startswith(prefix) for prefix in _STALE_TEMP_DIR_PREFIXES
            ):
                continue
            if entry.name == "ycode-computer-preview":
                # 预览根目录只是容器，绝不整体 rmtree：根自身 mtime 只随子目录
                # 增删变化，长驻实例 24h 无子目录变动时根 mtime 过老，后启动
                # 实例会把存活实例的 preview 目录一并删掉。逐子目录
                # （preview-* 按进程隔离）判 mtime。
                for child in entry.iterdir():
                    try:
                        if not child.name.startswith("preview-"):
                            continue
                        if now - child.stat().st_mtime < max_age_s:
                            continue
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
                    except OSError:
                        _LOGGER.exception("清扫预览进程目录失败：%s", child)
                continue
            if now - entry.stat().st_mtime < max_age_s:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            _LOGGER.exception("清扫电脑操控临时目录失败：%s", entry)
    return removed


def _default_hotkey_factory():
    from core.computer.platforms import get_platform_adapter

    return get_platform_adapter().create_hotkey_api()


def _default_controller_provider():
    from core.computer.controller import get_computer_control_service

    return get_computer_control_service()


def _emergency_stop(controller_provider):
    """Ctrl+Alt+Esc 回调：停止当前活动 run。

    走 stop_for_user：紧急热键同样是"用户"的停止意图，只应结束电脑操控 run，
    不能置位回合 cancel_event 把对话回合整条掐掉（模型会毫不知情地被打断）；
    抑制态与通知由 stop_for_user 统一记账。run 已收尾（竞态）无事发生。
    """

    def _on_hotkey_stop() -> None:
        try:
            controller = controller_provider()
        except Exception:
            _LOGGER.exception("紧急停止热键获取 controller 失败")
            return
        try:
            snapshot = controller.snapshot()
            run_id = str(getattr(snapshot, "run_id", "") or "")
            status = str(getattr(snapshot, "status", "") or "")
        except Exception:
            _LOGGER.exception("紧急停止热键读取 run 快照失败")
            return
        if not run_id or status in _TERMINAL_RUN_STATUSES:
            return
        try:
            controller.stop_for_user(run_id, "emergency_hotkey")
        except ComputerControlError:
            # run 已在停止收尾（竞态）：紧急停止的目标已达成
            _LOGGER.info("紧急停止热键触发时 run 已在收尾（忽略）")
        except Exception:
            _LOGGER.exception("紧急停止热键停止电脑操控 run 失败")

    return _on_hotkey_stop


def _build_runtime(*, settings_loader, hotkey_factory, controller_provider):
    settings = None
    try:
        settings = settings_loader()
    except Exception:
        _LOGGER.exception("读取电脑操控设置失败，跳过热键注册")
        settings = None
    hotkey = None
    if (
        settings is not None
        and (settings.browser_control_enabled or settings.computer_control_enabled)
    ):
        hotkey = _make_hotkey(hotkey_factory, controller_provider)
    return ComputerRuntime(hotkey=hotkey)


def _make_hotkey(hotkey_factory, controller_provider):
    from core.computer.hotkey import EmergencyStopHotkey

    try:
        hotkey = EmergencyStopHotkey(
            hotkey_factory(),
            on_stop=_emergency_stop(controller_provider),
        )
        hotkey.start()
        return hotkey
    except ComputerControlError as error:
        # 注册失败是可预期降级（热键被别的进程占用 / 平台不支持），不是故障。
        # 只在 INFO 留痕、堆栈只在 DEBUG：CLI 没有自定义 handler，WARNING 及以上
        # 会经 logging.lastResort 直打控制台，启动界面必须保持零提示
        # （能力状态另有快照供设置页展示，信息不会丢）。
        _LOGGER.info("全局紧急停止热键不可用：%s", error)
        _LOGGER.debug("紧急停止热键注册失败详情", exc_info=True)
    except Exception:
        _LOGGER.exception("全局紧急停止热键注册异常，紧急停止能力不可用")
    return None


def _controller_has_active_run(controller_provider) -> bool:
    try:
        snapshot = controller_provider().snapshot()
    except Exception:
        _LOGGER.exception("读取电脑操控 run 状态失败（按活动处理，保持热键）")
        return True
    run_id = str(getattr(snapshot, "run_id", "") or "")
    status = str(getattr(snapshot, "status", "") or "")
    return bool(run_id) and status not in _TERMINAL_RUN_STATUSES


def start_computer_runtime(
    *,
    settings_loader=None,
    hotkey_factory=None,
    controller_provider=None,
) -> ComputerRuntime:
    """进程级轻量容器（幂等单例）：开关开启时注册热键，默认关闭零副作用。"""
    global _RUNTIME
    sweep_stale_computer_temp_dirs()
    with _RUNTIME_LOCK:
        if _RUNTIME is not None:
            return _RUNTIME
        if settings_loader is None:
            from core.computer.settings import load_computer_settings

            settings_loader = load_computer_settings
        if hotkey_factory is None:
            hotkey_factory = _default_hotkey_factory
        if controller_provider is None:
            controller_provider = _default_controller_provider
        _RUNTIME = _build_runtime(
            settings_loader=settings_loader,
            hotkey_factory=hotkey_factory,
            controller_provider=controller_provider,
        )
        return _RUNTIME


def get_computer_runtime() -> "ComputerRuntime | None":
    """当前进程运行时容器；未启动（或已退出清理）为 None。"""
    with _RUNTIME_LOCK:
        return _RUNTIME


def refresh_computer_runtime_hotkey(
    *,
    settings_loader=None,
    hotkey_factory=None,
    controller_provider=None,
) -> None:
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
    if runtime is None:
        return
    if settings_loader is None:
        from core.computer.settings import load_computer_settings

        settings_loader = load_computer_settings
    if hotkey_factory is None:
        hotkey_factory = _default_hotkey_factory
    if controller_provider is None:
        controller_provider = _default_controller_provider
    try:
        settings = settings_loader()
    except Exception:
        _LOGGER.exception("读取电脑操控设置失败，紧急停止热键保持现状")
        return
    needed = bool(
        settings.browser_control_enabled or settings.computer_control_enabled
    )
    with _RUNTIME_LOCK:
        if _RUNTIME is not runtime:
            return  # 竞态：运行时已被退出清理替换，本次同步作废
        current = runtime._hotkey
        if needed and current is None:
            runtime._hotkey = _make_hotkey(hotkey_factory, controller_provider)
        elif not needed and current is not None:
            if _controller_has_active_run(controller_provider):
                return
            try:
                current.close()
            except Exception:
                _LOGGER.exception("注销全局紧急停止热键失败")
            runtime._hotkey = None


def attach_preview_publisher(runtime: "ComputerRuntime | None" = None) -> None:
    if runtime is None:
        runtime = get_computer_runtime()
    if runtime is None:
        return
    from core.computer.controller import get_computer_control_service

    get_computer_control_service().set_preview_publisher(
        runtime.publish_observation
    )


def shutdown_computer_runtime(
    *,
    controller=None,
    preview=None,
    hotkey=None,
    reason: str = "application_exit",
) -> None:
    if controller is not None:
        try:
            controller.stop(reason=reason)
        except Exception:
            _LOGGER.exception("退出时停止电脑操控 controller 失败")
    if preview is not None:
        try:
            preview.stop()
        except Exception:
            _LOGGER.exception("退出时停止电脑操控预览服务失败")
    if hotkey is not None:
        try:
            hotkey.close()
        except Exception:
            _LOGGER.exception("退出时注销紧急停止热键失败")


def shutdown_process_computer_runtime(reason: str = "application_exit") -> None:
    """进程级退出入口：收集单例（controller / runtime 的 preview 与 hotkey）
    统一关闭并清空运行时单例；幂等，任何一步失败只留痕。"""
    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
    controller = None
    try:
        controller = _default_controller_provider()
    except Exception:
        _LOGGER.exception("退出时获取电脑操控 controller 单例失败")
    preview = runtime.preview if runtime is not None else None
    hotkey = runtime.hotkey if runtime is not None else None
    shutdown_computer_runtime(
        controller=controller, preview=preview, hotkey=hotkey, reason=reason
    )
    with _RUNTIME_LOCK:
        if _RUNTIME is runtime:
            _RUNTIME = None


def _reset_runtime_for_tests() -> None:
    """测试专用：注销残留热键并清空进程单例（绝不在运行时调用）。"""
    global _RUNTIME
    with _RUNTIME_LOCK:
        runtime = _RUNTIME
        _RUNTIME = None
    if runtime is not None and runtime.hotkey is not None:
        try:
            runtime.hotkey.close()
        except Exception:
            _LOGGER.exception("测试清理紧急停止热键失败")
