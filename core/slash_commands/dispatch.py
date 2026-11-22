from dataclasses import dataclass, field

from core.slash_commands.parser import parse_slash_command
from core.slash_commands.registry import COMMANDS, all_specs, get
from core.slash_commands.specs import register_builtin_commands

__all__ = ["DispatchResult", "dispatch_command"]


@dataclass
class DispatchResult:

    handled: bool = False
    kind: str = ""
    prompt: str = ""
    notice: str = ""
    action: dict = field(default_factory=dict)
    error: str = ""


def _known_names() -> tuple:
    if not all_specs():
        register_builtin_commands()
    return tuple(COMMANDS.keys())


def dispatch_command(text, ctx=None) -> DispatchResult:
    parsed = parse_slash_command(text, _known_names())
    if parsed is None:
        return DispatchResult(handled=False)

    spec = get(parsed.name)
    if spec is None or not callable(spec.handler):
        # 解析层已保证名字在白名单内，这里是注册表被并发重置时的兜底；
        # 保守按"不是命令"处理，让入口继续走普通回合而不是丢消息。
        return DispatchResult(handled=False)

    try:
        result = spec.handler(parsed.args, ctx)
    except Exception as exc:
        # 异常类型名兜底：有些异常（如裸 Exception()）str() 是空串，
        # 空错误文案在提示卡片上等于什么都没说。
        message = str(exc).strip() or exc.__class__.__name__
        return DispatchResult(handled=True, kind="error", error=message)

    if not isinstance(result, dict):
        # handler 契约是 dict；返回别的类型说明实现有 bug，但仍要给出可展示的
        # 错误而不是抛 TypeError 穿透到入口。
        return DispatchResult(
            handled=True,
            kind="error",
            error=f"命令 {parsed.name} 返回了非字典结果: {type(result).__name__}",
        )

    if result.get("ok") is False:
        message = str(result.get("msg") or "").strip() or "命令执行失败"
        return DispatchResult(handled=True, kind="error", error=message)

    # 用 in 而不是真值判断：空提示词同样算命中 prompt（入口会按原样提交），
    # 若按真值判断，一个空 prompt 会掉进最后的"命令已执行"兜底，语义更离谱。
    if "prompt" in result:
        return DispatchResult(handled=True, kind="prompt", prompt=str(result.get("prompt") or ""))

    if "action" in result:
        return DispatchResult(handled=True, kind="action", action=result)

    if "notice" in result:
        return DispatchResult(handled=True, kind="notice", notice=str(result.get("notice") or ""))

    # handler 只说成功、没给任何内容（例如未来的静默命令）：给一句通用回执，
    # 否则入口拿不到任何可展示信息，用户会以为命令没生效。
    return DispatchResult(handled=True, kind="notice", notice="命令已执行")
