import re

from core.slash_commands.goal.state import GoalState

__all__ = ["handle_goal", "GOAL_USAGE", "DEFAULT_MAX_ROUNDS"]

GOAL_USAGE = "用法: /goal <目标> [--check 命令] [--rounds N]"

DEFAULT_MAX_ROUNDS = GoalState.__dataclass_fields__["max_rounds"].default

# 只认这两个开关，且必须落在空白边界上（前面是开头或空白，后面是空白或结尾）。
# 为什么要限定边界：目标正文里出现 "--" 是常事（"修复 --help 输出的排版"），
# 若按子串匹配，"--help" 会被当参数、目标被截成"修复"，用户看到的是跑偏的目标。
_FLAG_RE = re.compile(r"(?:^|\s)--(check|rounds)(?=\s|$)")

_KEYWORD_ACTIONS = {
    "help": "goal_help",
    "status": "goal_status",
    "stop": "goal_stop",
    "resume": "goal_resume",
    # 自评达成（模型自己打标记）不能由循环直接收尾，必须由用户点头；没有这个
    # 关键词时 awaiting_confirm 是个死胡同——状态条上只剩"停止"，用户无从确认。
    "confirm": "goal_confirm",
}


def _strip_quotes(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _split_flags(text: str):
    matches = list(_FLAG_RE.finditer(text))
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
        return {"ok": True, "action": {"action": "goal_help"}}

    keyword_action = _KEYWORD_ACTIONS.get(text.lower())
    if keyword_action:
        return {"ok": True, "action": {"action": keyword_action}}

    goal, flags = _split_flags(text)

    max_rounds = DEFAULT_MAX_ROUNDS
    if "rounds" in flags:
        raw_rounds = flags["rounds"].split(None, 1)[0] if flags["rounds"] else ""
        try:
            parsed_rounds = int(raw_rounds)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "rounds 必须是正整数"}
        if parsed_rounds <= 0:
            return {"ok": False, "msg": "rounds 必须是正整数"}
        max_rounds = parsed_rounds

    if not goal:
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
