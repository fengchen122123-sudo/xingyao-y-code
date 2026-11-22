from dataclasses import dataclass
from typing import Any, Callable

VALID_KINDS = ("local", "prompt", "hybrid")


@dataclass(frozen=True)
class CommandSpec:
    """一条斜杠命令的静态描述。"""

    name: str
    aliases: tuple = ()  # 别名（同样含斜杠），用于兼容简写
    summary: str = ""
    usage: str = ""
    kind: str = "local"
    handler: Callable[..., Any] | None = None


COMMANDS: dict = {}


def register(spec: CommandSpec) -> CommandSpec:
    """登记一条命令，主名与别名都可被 get() 命中（重复登记按后者覆盖）。"""
    if not spec.name or not spec.name.startswith("/"):
        raise ValueError(f"命令名必须以 / 开头: {spec.name!r}")
    if spec.kind not in VALID_KINDS:
        raise ValueError(f"未知命令类型 {spec.kind!r}，可选: {VALID_KINDS}")

    COMMANDS[spec.name.lower()] = spec
    for alias in spec.aliases or ():
        alias_name = str(alias).strip().lower()
        if alias_name:
            COMMANDS[alias_name] = spec
    return spec


def get(name):
    """按主名或别名查找命令，大小写不敏感；查不到返回 None。"""
    if not name:
        return None
    return COMMANDS.get(str(name).strip().lower())


def all_specs() -> list:
    """按主名排序返回全部命令，别名不会让同一条命令重复出现。"""
    unique = {id(spec): spec for spec in COMMANDS.values()}
    return sorted(unique.values(), key=lambda spec: spec.name)


def reset() -> None:
    """清空注册表，供测试隔离使用。"""
    COMMANDS.clear()
