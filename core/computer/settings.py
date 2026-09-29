"""电脑操控设置：严格配置读取、显式浏览器探测、专用 profile 路径与内存 generation。

本模块只提供静态设置快照（设计第 8.2 节）：配置值、generation、浏览器探测和
profile 展示路径。不得导入 controller 或 hotkey，不得执行任何运行时副作用。
错误码使用稳定英文 code（设计第 21 节 + brief 固定 computer_invalid_config）。
"""

from __future__ import annotations

import logging
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

# 进程内内存设置 generation：任何成功写入的设置变更都递增；
# 关闭开关的路径固定为「先持久化 false → 递增 generation → 返回快照」，
# 通知 controller 停止由调用方负责（设计 8.1）。
_GENERATION_LOCK = threading.Lock()
_settings_generation = 0

# 公共设置只保存浏览器身份；可执行文件名与安装位置由平台层负责。
# "embedded" 是 GUI 内置 WebView2（CDP 直连），不是独立安装的浏览器：
# 无可执行文件探测，profile 即内嵌控件的 UserDataFolder。
_BROWSER_DEFINITIONS = {
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
    "embedded": "星瑶内置浏览器",
}

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


def _read_bool(data: dict, key: str) -> bool:
    if key not in data:
        return False
    value = data[key]
    if type(value) is not bool:
        raise ComputerControlError("computer_invalid_config", f"{key} 必须是布尔值")
    return value


def _read_browser(data: dict) -> str:
    if COMPUTER_BROWSER_CONFIG_KEY not in data:
        # 配置缺省：GUI 进程注入的默认值优先（embedded），否则 edge
        return _process_default_browser or DEFAULT_COMPUTER_BROWSER
    value = data[COMPUTER_BROWSER_CONFIG_KEY]
    if type(value) is not str or value not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge、chrome 或 embedded",
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
    return tuple(_detect_browser(browser, platform) for browser in _BROWSER_DEFINITIONS)


def get_browser_profile_dir(browser: str) -> Path:
    """浏览器 profile 目录。

    edge/chrome：Path(config.YCODE_DIR) / "browser-profiles" / browser，
    resolve 后确认仍在专用根之下，拒绝任何路径逃逸；
    embedded：内嵌 WebView2 控件自己的 UserDataFolder（单一事实来源在
    core.embedded_browser.profile_dir，两处路径必须恒等）。
    """
    if type(browser) is not str or browser not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"不支持的浏览器：{browser!r}，只支持 edge、chrome 或 embedded",
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
    return ComputerSettingsSnapshot(
        browser_control_enabled=_read_bool(data, BROWSER_CONTROL_CONFIG_KEY),
        computer_control_enabled=_read_bool(data, COMPUTER_CONTROL_CONFIG_KEY),
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
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge、chrome 或 embedded",
        )
    if not config.save_config_value(COMPUTER_BROWSER_CONFIG_KEY, value):
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 配置保存失败",
        )
    _bump_settings_generation()
    return load_computer_settings()


# ---------------- 项目级浏览器视口（每项目分辨率） ----------------

BROWSER_VIEWPORT_CONFIG_KEY = "browser_viewport"
BROWSER_VIEWPORT_MIN = 120
BROWSER_VIEWPORT_MAX = 8192
DEFAULT_BROWSER_VIEWPORT_WIDTH = 1440
DEFAULT_BROWSER_VIEWPORT_HEIGHT = 900


def normalize_browser_viewport(width: object, height: object) -> tuple[int, int]:
    """校验/夹取逻辑视口尺寸；非法输入抛稳定错误码。"""
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
    """读取项目级浏览器逻辑视口；未设置/非法一律回落默认（1440×900）。

    workdir 为 None（GLOBAL 自由会话）时同样回落默认——分辨率是"每项目"
    概念，无项目即无个性化。
    """
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
        # 相对路径 / 目录不存在：折算为稳定错误码，上层工具可据此自纠
        raise ComputerControlError(
            "computer_invalid_config",
            f"项目分辨率保存失败：{error}",
        ) from error
    _notify_browser_viewport_changed(workdir, viewport)
    return viewport


# 项目分辨率变更通知：GUI 入口注册（把事件推给前端面板即时应用）；
# 工具层触发，listener 异常只留痕不传播。
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
