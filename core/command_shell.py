"""Default command-interpreter discovery, persistence, and invocation."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any

import config
from core.computer.settings import load_computer_settings
from core.harness.turn_limits import (
    MAX_TURN_STEP_LIMIT,
    MIN_TURN_STEP_LIMIT,
    load_turn_step_limit,
)


COMMAND_SHELL_CONFIG_KEY = "command_shell"
DEFAULT_COMMAND_SHELL = "auto"


class CommandShellError(RuntimeError):
    """Base error for invalid or unusable command-interpreter settings."""


class CommandShellUnavailableError(CommandShellError):
    """The selected interpreter is known but unavailable in this process."""


@dataclass(frozen=True)
class CommandInvocation:
    argv: str | list[str]
    use_system_shell: bool
    shell_id: str
    executable: str = ""


def _shell_definitions(platform: str) -> list[dict[str, str | None]]:
    if platform == "win32":
        return [
            {
                "id": "auto",
                "label": "自动（系统默认）",
                "description": "保持兼容旧行为，当前 Windows 使用 CMD。",
                "command": None,
            },
            {
                "id": "pwsh",
                "label": "PowerShell 7+",
                "description": "现代 PowerShell；需要单独安装。",
                "command": "pwsh.exe",
            },
            {
                "id": "powershell",
                "label": "Windows PowerShell 5.1",
                "description": "Windows 自带，兼容旧脚本和旧管理模块。",
                "command": "powershell.exe",
            },
            {
                "id": "cmd",
                "label": "CMD",
                "description": "Windows 传统命令解释器，适合 BAT/CMD 脚本。",
                "command": "cmd.exe",
            },
        ]
    return [
        {
            "id": "auto",
            "label": "自动（系统默认）",
            "description": "保持当前平台的默认 Shell 行为。",
            "command": None,
        },
        {
            "id": "bash",
            "label": "Bash",
            "description": "使用 Bash 解释命令。",
            "command": "bash",
        },
        {
            "id": "zsh",
            "label": "Zsh",
            "description": "使用 Zsh 解释命令。",
            "command": "zsh",
        },
        {
            "id": "sh",
            "label": "POSIX sh",
            "description": "使用系统 POSIX Shell 解释命令。",
            "command": "sh",
        },
        {
            "id": "pwsh",
            "label": "PowerShell 7+",
            "description": "跨平台 PowerShell；需要单独安装。",
            "command": "pwsh",
        },
    ]


def _discover_executable(command: str) -> str:
    executable = shutil.which(command) or ""
    normalized = executable.replace("/", "\\").lower()
    # Codex Desktop 会把自身私有运行时临时加入 PATH。该路径不属于 Y-CODE
    # 或系统安装，独立启动 Y-CODE 时不可依赖，不能据此把解释器标成可选。
    if "\\.cache\\codex-runtimes\\" in normalized:
        return ""
    return executable


def list_command_shell_options(*, platform: str | None = None) -> list[dict[str, Any]]:
    platform = platform or sys.platform
    options: list[dict[str, Any]] = []
    for definition in _shell_definitions(platform):
        command = definition["command"]
        executable = ""
        if command is None:
            available = True
            if platform == "win32":
                executable = os.environ.get("COMSPEC", "")
        else:
            executable = _discover_executable(command)
            available = bool(executable)
        options.append({
            "id": definition["id"],
            "label": definition["label"],
            "description": definition["description"],
            "available": available,
            "executable": executable,
            "unavailable_reason": "" if available else "未安装",
        })
    return options


def get_selected_command_shell() -> str:
    value = str(config._load().get(COMMAND_SHELL_CONFIG_KEY) or DEFAULT_COMMAND_SHELL).strip()
    return value or DEFAULT_COMMAND_SHELL


def _computer_active_model_capability(computer) -> dict[str, Any]:
    """当前活动模型的电脑操控资格摘要（设置面板展示用，只读）。

    与 Turn 起点走同一个判定函数（resolve_computer_capability，local 来源），
    保证设置页提示与真实执行门槛一致；模型未配置/读取失败时不影响设置页。
    """
    try:
        from core.computer.capability import resolve_computer_capability

        # 经 config._load() 读取（与设置读取同一路径，可被测试隔离）；
        # 不走 model_manager.load_config（其模块级 _CONFIG_FILE 快照与设置
        # 路径可能不一致）。
        data = config._load()
        active_id = str(data.get("active_model_id") or "")
        model = None
        provider = {}
        for candidate in data.get("providers", []):
            for entry in candidate.get("models", []):
                if entry.get("id") == active_id:
                    model = dict(entry)
                    provider = {
                        "protocol": candidate.get("protocol", "openai"),
                        "base_url": candidate.get("base_url", ""),
                    }
                    break
            if model is not None:
                break
        if not model:
            return {
                "model": "",
                "eligible": False,
                "error_code": "computer_model_not_capable",
                "reason": "当前未选择模型",
            }
        capability = resolve_computer_capability(model, provider, computer, "local")
        return {
            "model": str(model.get("name") or model.get("litellm_model") or ""),
            "eligible": bool(capability.enabled),
            "protocol": capability.protocol,
            "error_code": capability.error_code,
            "reason": capability.reason,
        }
    except Exception:  # 设置页展示失败绝不拖垮系统设置
        return {
            "model": "",
            "eligible": False,
            "error_code": "computer_model_not_capable",
            "reason": "活动模型资格判定失败（详见日志）",
        }


def get_system_settings(*, platform: str | None = None) -> dict[str, Any]:
    selected = get_selected_command_shell()
    options = list_command_shell_options(platform=platform)
    selected_option = next((item for item in options if item["id"] == selected), None)
    if selected_option is None:
        error = f"未知的命令解释器配置：{selected}"
        selected_available = False
    elif not selected_option["available"]:
        error = f"{selected_option['label']} 未安装或不在 星瑶Code 的 PATH 中"
        selected_available = False
    else:
        error = ""
        selected_available = True
    try:
        remote_token_ttl = int(
            config._load().get(config.REMOTE_TOKEN_TTL_KEY, config.REMOTE_TOKEN_TTL_DEFAULT)
        )
    except (TypeError, ValueError):
        remote_token_ttl = config.REMOTE_TOKEN_TTL_DEFAULT
    # 电脑操控静态设置（core.computer.settings，无运行时依赖）；
    # hotkey 运行态键（Task 5）与 controller run 键（Task 8）经函数内惰性导入接入。
    computer = load_computer_settings(platform=platform)
    from core.computer.hotkey import get_emergency_hotkey_snapshot
    from core.computer.controller import get_computer_control_service

    hotkey = get_emergency_hotkey_snapshot()
    computer_run = get_computer_control_service().snapshot()
    # AI 生成内容目录（设置项 ai_output_dir）：同样走函数内惰性导入，
    # 避免模块级 import 造成 command_shell ↔ ai_output_paths 循环依赖。
    from core.ai_output_paths import (
        default_ai_output_dir as _default_ai_output_dir,
        get_ai_output_dir_setting as _get_ai_output_dir_setting,
        resolve_ai_output_dir as _resolve_ai_output_dir,
    )

    return {
        "command_shell": selected,
        "command_shell_options": options,
        "command_shell_available": selected_available,
        "command_shell_error": error,
        "remote_token_ttl": remote_token_ttl,
        "agent_turn_step_limit": load_turn_step_limit(),
        "agent_turn_step_limit_min": MIN_TURN_STEP_LIMIT,
        "agent_turn_step_limit_max": MAX_TURN_STEP_LIMIT,
        "browser_control_enabled": computer.browser_control_enabled,
        "computer_control_enabled": computer.computer_control_enabled,
        "computer_browser": computer.computer_browser,
        "computer_browser_options": [option.to_dict() for option in computer.browser_options],
        "computer_profile_path": computer.profile_display_path,
        "computer_platform_supported": computer.platform_supported,
        "computer_hotkey_available": hotkey.available,
        "computer_hotkey": "Ctrl+Alt+Esc",
        "computer_run": computer_run.to_public_dict(),
        "computer_active_model_capability": _computer_active_model_capability(computer),
        # AI 生成内容目录：生效绝对路径 / 原始配置值（设置页回显）/ 默认目录
        "ai_output_dir": str(_resolve_ai_output_dir()),
        "ai_output_dir_setting": _get_ai_output_dir_setting(),
        "ai_output_dir_default": str(_default_ai_output_dir()),
    }


def set_selected_command_shell(shell_id: str, *, platform: str | None = None) -> dict[str, Any]:
    shell_id = str(shell_id or "").strip()
    options = list_command_shell_options(platform=platform)
    selected = next((item for item in options if item["id"] == shell_id), None)
    if selected is None:
        raise ValueError(f"不支持的命令解释器：{shell_id or '空值'}")
    if not selected["available"]:
        raise CommandShellUnavailableError(f"{selected['label']} 未安装或不在 星瑶Code 的 PATH 中")
    if not config.save_config_value(COMMAND_SHELL_CONFIG_KEY, shell_id):
        raise CommandShellError("命令解释器配置保存失败")
    return get_system_settings(platform=platform)


def _selected_option(platform: str, shell_id: str | None = None) -> dict[str, Any]:
    selected_id = str(shell_id or "").strip() or get_selected_command_shell()
    options = list_command_shell_options(platform=platform)
    selected = next((item for item in options if item["id"] == selected_id), None)
    if selected is None:
        raise CommandShellError(f"未知的命令解释器配置：{selected_id}")
    if not selected["available"]:
        raise CommandShellUnavailableError(
            f"{selected['label']} 未安装或不在 星瑶Code 的 PATH 中，请在系统设置中重新选择"
        )
    return selected


def describe_selected_command_shell(*, platform: str | None = None) -> str:
    platform = platform or sys.platform
    settings = get_system_settings(platform=platform)
    selected = next(
        (item for item in settings["command_shell_options"] if item["id"] == settings["command_shell"]),
        None,
    )
    if selected is None:
        return f"未知（{settings['command_shell']}，配置无效）"
    suffix = "" if selected["available"] else "（不可用：未安装）"
    executable = os.path.basename(selected["executable"]) if selected["executable"] else "系统默认"
    return f"{selected['label']} ({executable}){suffix}"


def build_command_invocation(
    command: str,
    *,
    platform: str | None = None,
    shell_id: str | None = None,
) -> CommandInvocation:
    platform = platform or sys.platform
    selected = _selected_option(platform, shell_id)
    shell_id = str(selected["id"])
    if shell_id == "auto":
        return CommandInvocation(command, True, shell_id)

    executable = str(selected["executable"])
    if shell_id == "cmd":
        # Windows 的 list2cmdline 采用 C 运行时转义规则，而 cmd.exe 不把 \"
        # 视为引号转义。交给 Popen 的 shell 路径承载原始命令，避免破坏路径
        # 和嵌套引号。
        return CommandInvocation(command, True, shell_id, executable)
    elif shell_id in {"powershell", "pwsh"}:
        argv = [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ]
    elif shell_id in {"bash", "zsh", "sh"}:
        argv = [executable, "-c", command]
    else:
        raise CommandShellError(f"不支持的命令解释器：{shell_id}")
    return CommandInvocation(argv, False, shell_id)
