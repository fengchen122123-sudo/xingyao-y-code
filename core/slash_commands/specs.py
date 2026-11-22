from core.slash_commands.goal_command import handle_goal
from core.slash_commands.local import handle_compact, handle_remember
from core.slash_commands.prompts import build_prompt
from core.slash_commands.tooling import (
    handle_agent,
    handle_commit,
    handle_forget,
    handle_skill,
    handle_workflow,
)
from core.slash_commands.registry import COMMANDS, CommandSpec, register

__all__ = ["COMMANDS", "register_builtin_commands"]


def _make_prompt_handler(name: str):

    def handler(args, ctx=None):
        return {"ok": True, "prompt": build_prompt(name, args)}

    return handler


# 元组而非列表：这是一张在导入期就该固定的静态表，避免被下游意外改动。
_PROMPT_COMMANDS = (
    CommandSpec(
        name="/fix",
        summary="定位根因并修复问题，改完跑验证",
        usage="/fix <报错原文或问题描述>",
        kind="prompt",
        handler=_make_prompt_handler("/fix"),
    ),
    CommandSpec(
        name="/review",
        summary="按严重程度审查改动，给出 file:line 与修复建议",
        usage="/review [审查范围，默认当前工作区改动]",
        kind="prompt",
        handler=_make_prompt_handler("/review"),
    ),
    CommandSpec(
        name="/init",
        summary="扫描项目并生成 AGENTS.md 草稿供确认",
        usage="/init [补充说明]",
        kind="prompt",
        handler=_make_prompt_handler("/init"),
    ),
    CommandSpec(
        name="/test",
        summary="跑测试、逐条分析失败并修复后重跑",
        usage="/test [关注的测试范围，默认相关]",
        kind="prompt",
        handler=_make_prompt_handler("/test"),
    ),
    CommandSpec(
        name="/explain",
        summary="读完调用方与被调用方后解释指定代码",
        usage="/explain <文件/函数/功能描述>",
        kind="prompt",
        handler=_make_prompt_handler("/explain"),
    ),
)


# 本地命令单独成表：kind="local" 命令不产生模型调用（见 local 模块 docstring），
# 与提示词命令的注册时机和幂等要求一致，只是执行入口不同，故这里仅分表便于区分。
_LOCAL_COMMANDS = (
    CommandSpec(
        name="/remember",
        summary="追加一条规则或偏好到长期记忆",
        usage="/remember <内容>",
        kind="local",
        handler=handle_remember,
    ),
    CommandSpec(
        name="/compact",
        summary="手动压缩当前会话上下文",
        usage="/compact [保留指令]",
        kind="local",
        handler=handle_compact,
    ),
)


_TOOLING_COMMANDS = (
    CommandSpec(
        name="/agent",
        summary="启动指定类型的子代理执行任务",
        usage="/agent <子代理类型> <任务描述>",
        kind="local",
        handler=handle_agent,
    ),
    CommandSpec(
        name="/skill",
        summary="装载指定技能，可附加补充要求",
        usage="/skill <技能名称> [补充要求]",
        kind="hybrid",
        handler=handle_skill,
    ),
    CommandSpec(
        name="/workflow",
        summary="按名称运行工作流，参数用 JSON 传入",
        usage='/workflow <工作流名称> [JSON参数，如 {"tag":"v1"}]',
        kind="local",
        handler=handle_workflow,
    ),
    CommandSpec(
        name="/commit",
        summary="提交当前改动，说明文字可留空由入口生成",
        usage="/commit [提交说明]",
        kind="hybrid",
        handler=handle_commit,
    ),
    CommandSpec(
        name="/forget",
        summary="按关键词删除长期记忆条目，删除前需确认",
        usage="/forget [--yes] <关键词>",
        kind="local",
        handler=handle_forget,
    ),
)


# 目标模式单独成表：/goal 的参数会被核心状态机（core.slash_commands.goal）消费，
# 但一轮目标是真实的模型回合 + 可能的多轮续跑，必须由入口层的编排器驱动，
# 所以 kind 标成 hybrid（与 /skill、/commit 同类，见上面 _TOOLING_COMMANDS 的说明）。
# 核心层仍只产出动作描述，不启动线程、不写状态文件。
_GOAL_COMMANDS = (
    CommandSpec(
        name="/goal",
        summary="设定目标并多轮推进，直到验证通过或轮次用尽",
        usage="/goal <目标> [--check 命令] [--rounds N]",
        kind="hybrid",
        handler=handle_goal,
    ),
)


def register_builtin_commands() -> None:
    # 四张表共用同一个 register()：注册表按名覆盖，重复调用只会重写同一批 key，
    # 因此追加目标模式命令后本函数仍保持幂等，无需额外的判重分支。
    for spec in _PROMPT_COMMANDS + _LOCAL_COMMANDS + _TOOLING_COMMANDS + _GOAL_COMMANDS:
        register(spec)
