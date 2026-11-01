"""内置斜杠命令清单。

本模块只负责把内置命令登记进注册表，命令实现分散在 local / prompts / tooling 等
模块，避免元数据与执行逻辑纠缠。当前登记四类：提示词类命令（/fix /review /init
/test /explain）不做本地动作，只把参数展开成一次普通回合的提示词；本地类命令
（/remember /compact）在进程内直接执行，不经过模型、不消耗 token；工具类命令
（/agent /skill /workflow /commit /forget）只解析参数并产出动作描述，真正的副作用
由 GUI/CLI 适配层执行，核心层不启动子代理、不调用 git；目标模式命令（/goal）
同样是纯解析，多轮循环与状态落盘由适配层驱动 core.slash_commands.goal。
"""

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
    """为指定命令生成 handler，把 args 展开成提示词。

    之所以用工厂而不是给每条命令写一个函数，是因为五条命令的 handler 逻辑完全
    相同，只差命令名；差异一旦下沉到数据里，改模板就不必碰 handler。

    ctx 参数当前不使用，但保留在签名里：入口统一按 handler(args, ctx) 调用，
    提示词类命令保持同一形状，日后需要注入工作区信息时不必改所有调用点。
    """

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


# 工具类命令单独成表：它们的共同点是「核心层不执行副作用」，只返回带 action 的
# 结构体交给适配层。kind 区分的是执行路径而不是副作用位置：
#   - local：适配层拿到动作后直接在本进程完成（/agent /workflow /forget）；
#   - hybrid：需要会话/编排器参与（/skill 装载技能、/commit 可能要先跑生成说明的
#     回合），所以标成 hybrid，由编排器决定是否插入一次模型调用。
# 这样 kind 仍严格取自 VALID_KINDS，入口层不必为工具类命令新增分支。
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
        # usage 带 [--yes] 是因为现在是两阶段删除：裸 /forget 只统计命中行，
        # 带上 --yes 才真删（见 core/slash_commands/tooling.handle_forget）。
        # 帮助文案不写这个旗标，用户会以为一句命令就删掉了。
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
    """登记全部内置命令。

    做成幂等无参函数，是因为 GUI 与 CLI 两个入口都会在启动时各调用一次。
    注册表实例定义在 registry 模块，这里只做再导出：让 register/get 与
    specs 看到同一个 dict，避免出现两份互不同步的命令表。
    幂等性由 register 的“同名覆盖”语义保证，重复调用只是重写同一批 key，
    不会出现重复条目，因此这里无需额外的判重分支。
    """
    # 四张表共用同一个 register()：注册表按名覆盖，重复调用只会重写同一批 key，
    # 因此追加目标模式命令后本函数仍保持幂等，无需额外的判重分支。
    for spec in _PROMPT_COMMANDS + _LOCAL_COMMANDS + _TOOLING_COMMANDS + _GOAL_COMMANDS:
        register(spec)
