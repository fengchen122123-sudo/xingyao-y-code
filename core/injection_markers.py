from __future__ import annotations

import re

#: 零宽空格：中和时插在标记首字符之后，肉眼不可见、不改变语义。
ZERO_WIDTH = "\u200b"

# 4 个保留标记（值必须与各使用处逐字一致；本文件是唯一来源）
DYNAMIC_CONTEXT_PREFIX = "【当前运行上下文】"
TIME_NOTE_PREFIX = "【当前时间】"
BG_SUMMARY_PREFIX = "📦 后台子代理任务结果汇总"
REF_SESSIONS_PREFIX = "【引用的对话记录】"

RESERVED_MARKERS: tuple[str, ...] = (
    DYNAMIC_CONTEXT_PREFIX,
    TIME_NOTE_PREFIX,
    BG_SUMMARY_PREFIX,
    REF_SESSIONS_PREFIX,
)


def neutralize_reserved_markers(text):
    if not text or not isinstance(text, str):
        return text
    for marker in RESERVED_MARKERS:
        if marker in text:
            text = text.replace(marker, marker[0] + ZERO_WIDTH + marker[1:])
    return text


#: 编排提醒文案（多步未建计划的软提醒）。定义于此并由 core/llm.py 导入，
#: 保证"生成点文案"与"展示层过滤前缀"同源，从结构上杜绝二者漂移。
ORCHESTRATION_NUDGE_TEXT = (
    "本回合已连续执行多步操作，仍未建立任务计划。"
    "若任务拆解后超过 3 条子任务，请立即用 plan_write 建立计划并逐条执行；"
    "不超过 3 条可忽略本提醒，直接完成。"
)

ORCHESTRATION_NUDGE_PREFIX = "本回合已连续执行多步操作，仍未建立任务计划。"

BG_SUMMARY_INJECTION_PREFIX = "请汇总所有后台子代理任务的结果并告诉我"

BG_COMMAND_SUMMARY_INJECTION_PREFIX = "有一个后台命令任务执行完毕"

LAN_PEER_SUMMARY_INJECTION_PREFIX = "远程节点任务已返回结果"

SYSTEM_INJECTED_USER_PREFIXES: tuple[str, ...] = (
    "<task-notification>",
    "你在最近一步耗时过长，且既没有调用任何工具、也没有输出任何",
    "工具已经执行完毕。现在必须只输出最终中文检查结论",
    "你刚才只输出了思考过程，既没有调用工具执行任何操作",
    "你声称已完成验证，但本回合没有执行任何命令。",
    "计划尚未执行完毕，仍有可执行的计划项",
    ORCHESTRATION_NUDGE_PREFIX,
    BG_SUMMARY_INJECTION_PREFIX,
    BG_COMMAND_SUMMARY_INJECTION_PREFIX,
    LAN_PEER_SUMMARY_INJECTION_PREFIX,
)

_HOOK_CONTEXT_HEADER_RE = re.compile(r"^【\S+ hook 追加上下文】")


def is_system_injected_user_text(text) -> bool:
    if not isinstance(text, str) or not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    for prefix in SYSTEM_INJECTED_USER_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return bool(_HOOK_CONTEXT_HEADER_RE.match(stripped))
