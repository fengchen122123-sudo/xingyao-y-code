"""斜杠命令分发层：把一段用户文本判定成"命令"并执行出统一结果。

本模块只做纯逻辑：不 import GUI、不 import CLI、不读环境、不弹窗，因此可以被
单测完整覆盖，也能被两个入口同时复用。三种执行方式（见 registry.VALID_KINDS）
在这里被收敛成同一份 DispatchResult，入口层不必再按 kind 写分支：

  - prompt：handler 把参数展开成提示词，入口拿 prompt 替换本回合正文（走模型）；
  - action：handler 只产出动作描述（/commit /agent /workflow ...），副作用由
    入口适配层执行，核心层不碰 git、不写记忆；
  - notice / error：本地命令就地完成（写记忆、压缩上下文），给一句提示即可。

之所以要这一层而不是让入口直接调 spec.handler：handler 的返回结构是 dict 契约，
失败有两种表达（ok=False、抛异常），不同命令的字段也各不相同；若不在这里统一，
GUI 与 CLI 各写一遍解析逻辑，两边就会对同一命令给出不同行为。
"""

from dataclasses import dataclass, field

from core.slash_commands.parser import parse_slash_command
from core.slash_commands.registry import COMMANDS, all_specs, get
from core.slash_commands.specs import register_builtin_commands

__all__ = ["DispatchResult", "dispatch_command"]


@dataclass
class DispatchResult:
    """一条命令的执行结果，入口层只需读 kind 与对应字段。

    所有字段都给默认值，是为了让每个分支只填自己关心的部分（例如命中 prompt
    时不必构造 error=""），构造点更短也更不容易漏字段。
    action 用 field(default_factory=dict) 而不是默认 {}：字典默认可变，
    被多个实例共享后一旦有人写入就会串味。
    """

    handled: bool = False  # 是否被当作命令处理（False 表示按普通消息继续）
    kind: str = ""  # prompt | notice | action | error
    prompt: str = ""  # kind=prompt：要替换本回合正文的提示词
    notice: str = ""  # kind=notice：给用户的一句提示
    action: dict = field(default_factory=dict)  # kind=action：动作描述（整个 handler 结果）
    error: str = ""  # kind=error：可直接展示给用户的错误文案


def _known_names() -> tuple:
    """取当前白名单（含别名）。

    顺手补齐内置命令注册：注册函数目前只在入口启动时被调用，而分发层可能先于
    那次调用被触达（测试里也常直接调 dispatch_command）。register 是幂等的，
    "空表才补注册"既保证可用，又不会覆盖测试里自行登记的命令。
    """
    if not all_specs():
        register_builtin_commands()
    return tuple(COMMANDS.keys())


def dispatch_command(text, ctx=None) -> DispatchResult:
    """解析并执行一条斜杠命令；不是命令时返回 handled=False。

    ctx 是给本地命令的可选能力包（当前只有 /compact 需要的 compress），
    形状由入口决定（dict 或对象都行，见 local._resolve_compress），
    核心分发层只做透传、不解释它的内容。

    判定顺序刻意与入口的取值顺序一致（先 ok 再 prompt/action/notice），
    这样"handler 说失败"永远优先于它顺手带的其它字段，不会被误当成成功。
    """
    parsed = parse_slash_command(text, _known_names())
    if parsed is None:
        # 未命中白名单：/usr/bin 这类路径提问、// 转义文本都走这里，交回普通消息
        return DispatchResult(handled=False)

    spec = get(parsed.name)
    if spec is None or not callable(spec.handler):
        # 解析层已保证名字在白名单内，这里是注册表被并发重置时的兜底；
        # 保守按"不是命令"处理，让入口继续走普通回合而不是丢消息。
        return DispatchResult(handled=False)

    try:
        result = spec.handler(parsed.args, ctx)
    except Exception as exc:  # 单条命令的实现缺陷不应让整个入口崩掉
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
        # 失败文案统一从 msg 取：local/tooling 两条实现路径都按这个键报错。
        message = str(result.get("msg") or "").strip() or "命令执行失败"
        return DispatchResult(handled=True, kind="error", error=message)

    # 用 in 而不是真值判断：空提示词同样算命中 prompt（入口会按原样提交），
    # 若按真值判断，一个空 prompt 会掉进最后的"命令已执行"兜底，语义更离谱。
    if "prompt" in result:
        return DispatchResult(handled=True, kind="prompt", prompt=str(result.get("prompt") or ""))

    if "action" in result:
        # 整个 dict 一并交出：confirm_required、subagent_type 等字段是适配层
        # 执行动作时的判断依据，抽走字段会让适配层只能按命令名硬编码。
        return DispatchResult(handled=True, kind="action", action=result)

    if "notice" in result:
        return DispatchResult(handled=True, kind="notice", notice=str(result.get("notice") or ""))

    # handler 只说成功、没给任何内容（例如未来的静默命令）：给一句通用回执，
    # 否则入口拿不到任何可展示信息，用户会以为命令没生效。
    return DispatchResult(handled=True, kind="notice", notice="命令已执行")
