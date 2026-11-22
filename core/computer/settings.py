from __future__ import annotations

import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import config
from core.computer import platforms
from core.computer.models import ComputerControlError

logger = logging.getLogger(__name__)

BROWSER_CONTROL_CONFIG_KEY = "browser_control_enabled"
COMPUTER_CONTROL_CONFIG_KEY = "computer_control_enabled"
COMPUTER_BROWSER_CONFIG_KEY = "computer_browser"
DEFAULT_COMPUTER_BROWSER = "edge"
# serve 档（容器/无窗口）的配置缺省：只认 Playwright 自带 headless chromium。
# 桌面默认 edge 在容器里没有安装位置，内嵌 WebView2 也不存在，因此 serve 的
# 缺省值必须是 chromium（设计 M07「从空 config.json 验收」）。
DEFAULT_SERVER_BROWSER = "chromium"

_GENERATION_LOCK = threading.Lock()
_settings_generation = 0

_BROWSER_DEFINITIONS = {
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "chromium": "Chromium",
    "embedded": "星瑶内置浏览器",
}


def get_browser_label(browser: object) -> str:
    key = str(browser or "").strip()
    return _BROWSER_DEFINITIONS.get(key, key or "未配置")


def is_external_browser(browser: object) -> bool:
    key = str(browser or "").strip()
    return key in ("edge", "chrome", "chromium")


def describe_runtime_browser_preference(
    settings: ComputerSettingsSnapshot,
    computer_capability=None,
    *,
    ui_profile: str = "",
) -> str:
    """生成给模型的浏览器系统设置优先说明；只反映当前设置事实。"""
    browser = str(getattr(settings, "computer_browser", "") or "").strip()
    if not browser:
        return ""
    label = get_browser_label(browser)
    targets = tuple(getattr(computer_capability, "targets", ()) or ()) if computer_capability else ()
    browser_cap_enabled = bool(
        computer_capability is not None
        and getattr(computer_capability, "enabled", False)
        and "browser" in targets
    )
    profile = str(ui_profile or "").strip().lower()
    gui_pane_available = profile not in ("cli", "coding", "server")

    if browser == "embedded":
        if browser_cap_enabled:
            return (
                f"默认浏览器：{label}（{browser}；「浏览器操控」已开启）\n"
                "浏览器网页打开、导航与交互操作必须优先使用 computer_action"
                "（target=browser），直接在 GUI 右侧内置浏览器中可交互呈现；"
                "仅关闭/查看/调整面板或打开本地 HTML 预览时才使用 browser_pane。"
            )
        if not gui_pane_available:
            return (
                f"默认浏览器：{label}（{browser}；当前运行模式不提供 GUI 浏览器操作）\n"
                "读取静态网页正文请使用 fetch_url。"
            )
        status_note = (
            "当前模型或运行通道未启用 computer_action 浏览器操控"
            if getattr(settings, "browser_control_enabled", False)
            else "「浏览器操控」开关未开启"
        )
        return (
            f"默认浏览器：{label}（{browser}；{status_note}）\n"
            "仅可使用 browser_pane 在右侧星瑶内置浏览器面板打开或导航网页"
            "（无法点击或输入页内元素）；需要完整交互操控时请提示用户检查「设置 → 电脑与浏览器操控」。"
        )

    if browser_cap_enabled:
        return (
            f"默认浏览器：{label}（{browser}，外部浏览器；「浏览器操控」已开启）\n"
            f"所有浏览器操作必须优先并严格使用 computer_action（target=browser）驱动 {label}，"
            "禁止改用 browser_pane 打开内置浏览器绕开系统设置。"
        )
    if getattr(settings, "browser_control_enabled", False):
        return (
            f"默认浏览器：{label}（{browser}；当前模型或运行通道未启用 computer_action 浏览器操控）\n"
            f"必须尊重系统设置选择的 {label}，禁止改用 browser_pane 打开内置浏览器代替；"
            "读取静态网页正文请使用 fetch_url，或提示用户切换到支持视觉能力的模型。"
        )
    return (
        f"默认浏览器：{label}（{browser}；「浏览器操控」开关未开启）\n"
        f"必须尊重系统设置选择的 {label}，禁止改用 browser_pane 打开内置浏览器代替；"
        f"读取静态网页正文请使用 fetch_url，如需操作 {label} 请提示用户在「设置 → 电脑与浏览器操控」开启「浏览器操控」。"
    )

# 进程级默认浏览器：GUI 入口在启动时注入 "embedded"（未显式配置过的用户
# 默认走内置浏览器）；CLI 不注入，保持 "edge" 回落。只影响"配置缺省"，
# 绝不覆盖用户显式写入的值。
_process_default_browser: str | None = None


def set_process_default_browser(browser: str) -> None:
    global _process_default_browser
    if browser not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config", f"不支持的默认浏览器：{browser!r}"
        )
    _process_default_browser = browser


@dataclass(frozen=True)
class BrowserOption:
    """单个受支持浏览器的探测结果。"""

    browser: str
    label: str
    available: bool
    executable: str = ""
    unavailable_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "browser": self.browser,
            "label": self.label,
            "available": self.available,
            "executable": self.executable,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class ComputerSettingsSnapshot:
    """静态设置快照；不得包含 hotkey_available 或 run_summary（设计 8.2）。"""

    browser_control_enabled: bool
    computer_control_enabled: bool
    computer_browser: str
    browser_options: tuple[BrowserOption, ...]
    profile_display_path: str
    platform_supported: bool
    generation: int


def get_settings_generation() -> int:
    with _GENERATION_LOCK:
        return _settings_generation


def _bump_settings_generation() -> None:
    global _settings_generation
    with _GENERATION_LOCK:
        _settings_generation += 1


def _server_profile() -> bool:
    return os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "server"


def resolve_headless() -> bool:
    """浏览器是否无头：Windows 桌面恒 False；其他平台恒 True。"""
    return sys.platform != "win32"


def _read_bool(data: dict, key: str) -> bool:
    if key not in data:
        if key == BROWSER_CONTROL_CONFIG_KEY and _server_profile():
            return True
        return False
    value = data[key]
    if type(value) is not bool:
        raise ComputerControlError("computer_invalid_config", f"{key} 必须是布尔值")
    return value


def _read_browser(data: dict) -> str:
    if COMPUTER_BROWSER_CONFIG_KEY not in data:
        # serve 档优先于 GUI 注入的进程默认值：无窗口进程里 embedded/edge 都
        # 不可用，缺省必须是 chromium（M07「从空 config.json 验收」）。
        if _server_profile():
            return DEFAULT_SERVER_BROWSER
        # 配置缺省：GUI 进程注入的默认值优先（embedded）。
        # Windows 否则仍是 edge；Linux 回落到 chromium。
        if _process_default_browser:
            return _process_default_browser
        return DEFAULT_COMPUTER_BROWSER
    value = data[COMPUTER_BROWSER_CONFIG_KEY]
    if type(value) is not str or value not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge、chrome、chromium 或 embedded",
        )
    return value


def _is_platform_supported(platform: str) -> bool:
    return platforms.get_platform_adapter(platform).supported


def _detect_browser(browser: str, platform: str) -> BrowserOption:
    """平台内单独探测所选浏览器，不可用平台不检查其他系统的安装路径。"""
    if browser == "embedded":
        # 内置浏览器无可执行文件：可用性按「入参 platform + 内嵌模块可加载」
        # 判定。必须用入参而不是 sys.platform——测试/调用方会以
        # platform="darwin"/"linux" 模拟非 Windows 环境（此时 sys.platform
        # 仍是 win32），与其它浏览器同一裁决口径。
        from core.embedded_browser import is_supported

        available = platform == "win32" and is_supported()
        return BrowserOption(
            browser="embedded",
            label=_BROWSER_DEFINITIONS["embedded"],
            available=available,
            executable="",
            unavailable_reason="" if available else "内置浏览器仅在 Windows 桌面端可用",
        )
    adapter = platforms.get_platform_adapter(platform)
    label = _BROWSER_DEFINITIONS[browser]
    executable = adapter.find_browser_executable(browser)
    available = bool(executable)
    return BrowserOption(
        browser=browser,
        label=label,
        available=available,
        executable=executable,
        unavailable_reason="" if available else (adapter.unavailable_reason or "未安装或不在 星瑶Code 的 PATH 中"),
    )


def list_browser_options(*, platform: str | None = None) -> tuple[BrowserOption, ...]:
    platform = platform or sys.platform
    names = ["edge", "chrome", "embedded"]
    if _server_profile() or platform != "win32":
        names.append("chromium")
    return tuple(_detect_browser(browser, platform) for browser in names)


def get_browser_profile_dir(browser: str) -> Path:
    if type(browser) is not str or browser not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"不支持的浏览器：{browser!r}，只支持 edge、chrome、chromium 或 embedded",
        )
    if browser == "embedded":
        from core.embedded_browser import profile_dir

        return profile_dir().resolve()
    root = Path(config.YCODE_DIR) / "browser-profiles"
    resolved_root = root.resolve()
    candidate = (root / browser).resolve()
    if not candidate.is_relative_to(resolved_root):
        raise ComputerControlError(
            "computer_invalid_config",
            f"浏览器 profile 路径越出专用目录：{candidate}",
        )
    return candidate


def load_computer_settings(*, platform: str | None = None) -> ComputerSettingsSnapshot:
    platform = platform or sys.platform
    data = config._load()
    browser = _read_browser(data)
    desktop_enabled = _read_bool(data, COMPUTER_CONTROL_CONFIG_KEY)
    if _server_profile():
        desktop_enabled = False
    return ComputerSettingsSnapshot(
        browser_control_enabled=_read_bool(data, BROWSER_CONTROL_CONFIG_KEY),
        computer_control_enabled=desktop_enabled,
        computer_browser=browser,
        browser_options=list_browser_options(platform=platform),
        profile_display_path=str(get_browser_profile_dir(browser)),
        platform_supported=_is_platform_supported(platform),
        generation=get_settings_generation(),
    )


def _set_enabled(
    key: str,
    value: object,
    preflight: Callable[[], None],
    label: str,
) -> ComputerSettingsSnapshot:
    if type(value) is not bool:
        raise ComputerControlError("computer_invalid_config", f"{key} 必须是布尔值")
    if value:
        # 开启前必须先跑 preflight；失败则不写入 true（设计 8.1）
        try:
            preflight()
        except ComputerControlError:
            raise
        except Exception as error:
            raise ComputerControlError(
                "computer_preflight_failed",
                f"{label}开启前检查失败：{error}",
            ) from error
    # 关闭顺序固定：先原子持久化 false，再递增内存 generation，最后返回快照；
    # 通知 controller 停止由调用方负责，本模块不 import controller。
    if not config.save_config_value(key, value):
        raise ComputerControlError("computer_invalid_config", f"{key} 配置保存失败")
    _bump_settings_generation()
    return load_computer_settings()


def set_browser_control_enabled(
    value: object, preflight: Callable[[], None]
) -> ComputerSettingsSnapshot:
    return _set_enabled(BROWSER_CONTROL_CONFIG_KEY, value, preflight, "浏览器操控")


def set_computer_control_enabled(
    value: object, preflight: Callable[[], None]
) -> ComputerSettingsSnapshot:
    return _set_enabled(COMPUTER_CONTROL_CONFIG_KEY, value, preflight, "电脑操控")


def set_computer_browser(value: object, active_run: bool) -> ComputerSettingsSnapshot:
    if active_run:
        raise ComputerControlError(
            "computer_session_busy",
            "当前有进行中的电脑操控会话，请先停止后再切换浏览器",
        )
    if type(value) is not str or value not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge、chrome、chromium 或 embedded",
        )
    if not config.save_config_value(COMPUTER_BROWSER_CONFIG_KEY, value):
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 配置保存失败",
        )
    _bump_settings_generation()
    return load_computer_settings()


BROWSER_VIEWPORT_CONFIG_KEY = "browser_viewport"
BROWSER_VIEWPORT_MIN = 120
BROWSER_VIEWPORT_MAX = 8192
DEFAULT_BROWSER_VIEWPORT_WIDTH = 1440
DEFAULT_BROWSER_VIEWPORT_HEIGHT = 900


def normalize_browser_viewport(width: object, height: object) -> tuple[int, int]:
    def _clamp(value: object, minimum: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ComputerControlError(
                "computer_invalid_config", f"浏览器视口{label}必须是整数"
            )
        try:
            ivalue = int(value)
        except (ValueError, OverflowError, TypeError):
            # float('nan')/float('inf') 等非有限值不能穿透读路径：
            # 折算成模块内稳定错误码，而不是裸 ValueError/OverflowError
            raise ComputerControlError(
                "computer_invalid_config", f"浏览器视口{label}必须是整数"
            ) from None
        if ivalue != value:
            raise ComputerControlError(
                "computer_invalid_config", f"浏览器视口{label}必须是整数"
            )
        return max(minimum, min(BROWSER_VIEWPORT_MAX, ivalue))

    return (
        _clamp(width, BROWSER_VIEWPORT_MIN, "宽度"),
        _clamp(height, BROWSER_VIEWPORT_MIN, "高度"),
    )


def get_project_browser_viewport(workdir: str | None) -> tuple[int, int]:
    if not workdir:
        return (DEFAULT_BROWSER_VIEWPORT_WIDTH, DEFAULT_BROWSER_VIEWPORT_HEIGHT)
    from core import workspace_settings

    try:
        raw = workspace_settings.get_workspace_setting(
            workdir, BROWSER_VIEWPORT_CONFIG_KEY
        )
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            return (DEFAULT_BROWSER_VIEWPORT_WIDTH, DEFAULT_BROWSER_VIEWPORT_HEIGHT)
        return normalize_browser_viewport(raw[0], raw[1])
    except Exception:
        # 读路径铁律：任何损坏（NaN/Inf、相对 workdir、文件不可读等）
        # 都静默回落默认视口，绝不外抛打断回合
        logger.debug("项目浏览器视口读取失败，回落默认", exc_info=True)
        return (DEFAULT_BROWSER_VIEWPORT_WIDTH, DEFAULT_BROWSER_VIEWPORT_HEIGHT)


def set_project_browser_viewport(workdir: str | None, width: object, height: object) -> tuple[int, int]:
    """写入项目级浏览器逻辑视口（星瑶经 browser_set_viewport 工具调用）。"""
    if not workdir:
        raise ComputerControlError(
            "computer_invalid_config",
            "当前会话未关联项目，无法设置项目分辨率；请先进入项目工作区",
        )
    from core import workspace_settings

    viewport = normalize_browser_viewport(width, height)
    try:
        workspace_settings.set_workspace_setting(
            workdir, BROWSER_VIEWPORT_CONFIG_KEY, list(viewport)
        )
    except ValueError as error:
        raise ComputerControlError(
            "computer_invalid_config",
            f"项目分辨率保存失败：{error}",
        ) from error
    _notify_browser_viewport_changed(workdir, viewport)
    return viewport


_VIEWPORT_LISTENERS: list[Callable[[str | None, tuple[int, int]], None]] = []


def register_browser_viewport_listener(
    listener: Callable[[str | None, tuple[int, int]], None],
) -> None:
    with _GENERATION_LOCK:
        if listener not in _VIEWPORT_LISTENERS:
            _VIEWPORT_LISTENERS.append(listener)


def _notify_browser_viewport_changed(
    workdir: str | None, viewport: tuple[int, int]
) -> None:
    with _GENERATION_LOCK:
        listeners = list(_VIEWPORT_LISTENERS)
    for listener in listeners:
        try:
            listener(workdir, viewport)
        except Exception:
            logger.debug("浏览器视口变更通知失败", exc_info=True)
