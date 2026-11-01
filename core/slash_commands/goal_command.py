"""`/goal` 目标模式命令的入口解析：只把用户输入翻译成动作描述。

与 tooling.py 里的工具类命令同一份契约：核心层零副作用，不读配置、不起线程、
不写状态文件，只返回带 action 的纯数据。状态机的读写与后台多轮循环由适配层
（gui_bridge / 未来的 CLI）按 action 自行执行，这样这条命令可以被单测完整覆盖，
也不会出现「输入一行命令就偷偷跑起一堆模型回合」的情况。

为什么动作再包一层（{"action": {"action": "goal_start", ...}}）：外层是
dispatch 约定的搬运字段（整份 handler 结果交给适配层），内层才是本命令的动作
描述——/goal 需要携带 goal/check/max_rounds 等参数，不能像 /commit 那样把
动作名直接写在外层字符串里。适配层读 inner.action 决定分支。
"""

import re

from core.slash_commands.goal.state import GoalState

__all__ = ["handle_goal", "GOAL_USAGE", "DEFAULT_MAX_ROUNDS"]

GOAL_USAGE = "用法: /goal <目标> [--check 命令] [--rounds N]"

# 轮次上限默认值直接取 GoalState 的默认值：两处各写一个 10 迟早漂移，
# 而适配层真正启动循环时用的是 GoalState，命令层只是把同一个值透传出去。
DEFAULT_MAX_ROUNDS = GoalState.__dataclass_fields__["max_rounds"].default

# 只认这两个开关，且必须落在空白边界上（前面是开头或空白，后面是空白或结尾）。
# 为什么要限定边界：目标正文里出现 "--" 是常事（"修复 --help 输出的排版"），
# 若按子串匹配，"--help" 会被当参数、目标被截成"修复"，用户看到的是跑偏的目标。
_FLAG_RE = re.compile(r"(?:^|\s)--(check|rounds)(?=\s|$)")

# 控制类关键词：只有整段参数完全等于关键词时才生效。用完全相等而不是前缀匹配，
# 是因为"stop 掉旧服务并重启"这种目标以 stop 开头，误判成停止指令会让用户
# 的目标永远起不来；多打一个字就救不回来，代价远大于少一个简写。
_KEYWORD_ACTIONS = {
    "help": "goal_help",
    "status": "goal_status",
    "stop": "goal_stop",
    "resume": "goal_resume",
}


def _strip_quotes(value: str) -> str:
    """去掉整段值外侧成对的引号（`--check "pytest -q"` 这种写法）。"""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _split_flags(text: str):
    """把整段参数切成（目标原文, {开关名: 值段})。

    值段的右边界是「下一个开关的起点」或文本末尾：`--check` 的值是一整条命令，
    里面本来就含空格（`pytest -q`），按单个 token 截断会把命令截成 `pytest`，
    用户拿到的是"验证命令总是通过"的假达成。
    """
    matches = list(_FLAG_RE.finditer(text))
    # 第一个开关之前的内容就是目标原文；开关之后的内容一律归开关的值，
    # 免得 "目标 --check cmd 补充说明" 里的补充说明被当成目标的一部分
    goal = text[: matches[0].start()].strip() if matches else text.strip()

    values = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        values[match.group(1)] = text[match.end():end].strip()
    return goal, values


def handle_goal(args, ctx=None) -> dict:
    """解析 /goal 参数，返回 {"ok": True, "action": {...}} 或带 msg 的失败。

    分支顺序刻意是「空参数 → 关键词 → 开关 → 目标」：关键词判断必须早于开关解析，
    否则 `/goal status` 会被当成一个叫 "status" 的目标启动一轮模型回合，白白烧 token。
    """
    text = str(args or "").strip()
    if not text:
        # 不带目标时的正确行为是教用户怎么用，而不是报错：/goal 单打独斗是
        # 用户最可能的第一次尝试，回一句用法比回一个红色错误卡片友好得多。
        return {"ok": True, "action": {"action": "goal_help"}}

    keyword_action = _KEYWORD_ACTIONS.get(text.lower())
    if keyword_action:
        return {"ok": True, "action": {"action": keyword_action}}

    goal, flags = _split_flags(text)

    max_rounds = DEFAULT_MAX_ROUNDS
    if "rounds" in flags:
        # --rounds 只取值段的第一个 token：`--rounds 5 额外说明` 中多写的部分
        # 不参与解析，也不报错（用户可能顺手在末尾补了备注）
        raw_rounds = flags["rounds"].split(None, 1)[0] if flags["rounds"] else ""
        try:
            parsed_rounds = int(raw_rounds)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "rounds 必须是正整数"}
        if parsed_rounds <= 0:
            # 0 或负数会让 run_goal_loop 一轮都不跑却立刻标 blocked，
            # 用户会以为目标"跑完了"；这里直接拦住更诚实
            return {"ok": False, "msg": "rounds 必须是正整数"}
        max_rounds = parsed_rounds

    if not goal:
        # 只有开关没有目标（"/goal --rounds 5"）：循环没有可推进的目标，
        # 起线程也是空转，直接报用法
        return {"ok": False, "msg": GOAL_USAGE}

    return {
        "ok": True,
        "action": {
            "action": "goal_start",
            "goal": goal,
            "check": _strip_quotes(flags.get("check", "")),
            "max_rounds": max_rounds,
        },
    }
