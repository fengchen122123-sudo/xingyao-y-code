"""斜杠命令共享核心（GUI 与 CLI 共用）。

当前只含解析器与注册表骨架：解析决定"这段文本是不是命令"，注册表提供命令元数据；
执行分发与各条命令的实现由后续模块补齐，两端只保留各自的适配层。
"""

from core.slash_commands.parser import ParsedCommand, parse_slash_command
from core.slash_commands.registry import (
    COMMANDS,
    CommandSpec,
    all_specs,
    get,
    register,
    reset,
)
from core.slash_commands.specs import register_builtin_commands

__all__ = [
    "parse_slash_command",
    "ParsedCommand",
    "CommandSpec",
    "COMMANDS",
    "register",
    "get",
    "all_specs",
    "reset",
    "register_builtin_commands",
]
