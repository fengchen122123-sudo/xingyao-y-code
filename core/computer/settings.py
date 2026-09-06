"""电脑操控设置：严格配置读取、显式浏览器探测、专用 profile 路径与内存 generation。

本模块只提供静态设置快照（设计第 8.2 节）：配置值、generation、浏览器探测和
profile 展示路径。不得导入 controller 或 hotkey，不得执行任何运行时副作用。
错误码使用稳定英文 code（设计第 21 节 + brief 固定 computer_invalid_config）。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import config
from core.computer import platforms
from core.computer.models import ComputerControlError

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
_BROWSER_DEFINITIONS = {
    "edge": "Microsoft Edge",
    "chrome": "Google Chrome",
}


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
        return DEFAULT_COMPUTER_BROWSER
    value = data[COMPUTER_BROWSER_CONFIG_KEY]
    if type(value) is not str or value not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge 或 chrome",
        )
    return value


def _is_platform_supported(platform: str) -> bool:
    return platforms.get_platform_adapter(platform).supported


def _detect_browser(browser: str, platform: str) -> BrowserOption:
    """平台内单独探测所选浏览器，不可用平台不检查其他系统的安装路径。"""
    adapter = platforms.get_platform_adapter(platform)
    label = _BROWSER_DEFINITIONS[browser]
    executable = adapter.find_browser_executable(browser)
    available = bool(executable)
    return BrowserOption(
        browser=browser,
        label=label,
        available=available,
        executable=executable,
        unavailable_reason="" if available else (adapter.unavailable_reason or "未安装或不在 Y-CODE 的 PATH 中"),
    )


def list_browser_options(*, platform: str | None = None) -> tuple[BrowserOption, ...]:
    platform = platform or sys.platform
    return tuple(_detect_browser(browser, platform) for browser in _BROWSER_DEFINITIONS)


def get_browser_profile_dir(browser: str) -> Path:
    """专用 profile 目录：Path(config.YCODE_DIR) / "browser-profiles" / browser。

    config.YCODE_DIR 是字符串，必须先包 Path；resolve 后确认仍在
    browser-profiles 根之下，拒绝任何路径逃逸。
    """
    if type(browser) is not str or browser not in _BROWSER_DEFINITIONS:
        raise ComputerControlError(
            "computer_invalid_config",
            f"不支持的浏览器：{browser!r}，只支持 edge 或 chrome",
        )
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
            f"{COMPUTER_BROWSER_CONFIG_KEY} 只支持 edge 或 chrome",
        )
    if not config.save_config_value(COMPUTER_BROWSER_CONFIG_KEY, value):
        raise ComputerControlError(
            "computer_invalid_config",
            f"{COMPUTER_BROWSER_CONFIG_KEY} 配置保存失败",
        )
    _bump_settings_generation()
    return load_computer_settings()
