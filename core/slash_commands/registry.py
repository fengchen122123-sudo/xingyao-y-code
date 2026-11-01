"""斜杠命令注册表：登记命令元数据并按名称/别名索引。

注册表只负责数据登记，不执行命令；执行分发由各入口按 spec.kind 自行完成，
这样 GUI 与 CLI 共享同一份命令清单，却各自保留适配层。
"""

from dataclasses import dataclass
from typing import Any, Callable

# 三种执行方式：local 本地直接处理；prompt 展开提示词后走普通回合；hybrid 交给编排器
VALID_KINDS = ("local", "prompt", "hybrid")


@dataclass(frozen=True)
class CommandSpec:
    """一条斜杠命令的静态描述。"""

    name: str  # 主名，含前导斜杠，例如 "/goal"
    aliases: tuple = ()  # 别名（同样含斜杠），用于兼容简写
    summary: str = ""  # 一句话说明，供补全列表展示
    usage: str = ""  # 用法示例，例如 "/goal <目标> [--check 命令]"
    kind: str = "local"  # 取值见 VALID_KINDS
    handler: Callable[..., Any] | None = None  # 执行函数，注册表不调用


# 唯一注册表实例：名称（含别名，统一小写）-> spec。
# 别名与主名指向同一个 spec 对象，因此 all_specs() 需要按对象去重。
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
