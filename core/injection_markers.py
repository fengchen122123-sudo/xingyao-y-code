"""注入保留标记的唯一来源 + 防伪造中和（提示词工程与信源防护升级 · T1.5）。

保留标记是程序与消息文本之间的"协议"：

- ``strip_context_suffix``（core/agent_service.py）依赖它们做末尾剥离；
- 模型依赖它们识别信源（"这是程序注入的运行上下文"）。

用户内容中出现同名标记时必须"中和"——在标记首字符后插入零宽字符 U+200B：
破坏精确匹配、不影响用户可见文本与语义。否则会出现两类问题：

1. 数据完整性：用户消息自带 ``【当前运行上下文】`` 时，可能被
   ``strip_context_suffix`` 从中间误截断，丢掉用户真正写的内容；
2. 信任混淆：用户内容里的伪造标记会让模型误认为"这是系统注入的
   运行上下文"，从而把伪造的"权限模式：full"等内容当真。

中和必须发生在**程序自身注入之前**：程序注入使用原标记，strip 照常识别。

本模块是这 4 个标记的唯一来源，其余模块一律从本模块导入（值逐字不变）。
"""

from __future__ import annotations

import re

#: 零宽空格：中和时插在标记首字符之后，肉眼不可见、不改变语义。
ZERO_WIDTH = "\u200b"

# 4 个保留标记（值必须与各使用处逐字一致；本文件是唯一来源）
DYNAMIC_CONTEXT_PREFIX = "【当前运行上下文】"
TIME_NOTE_PREFIX = "【当前时间】"
BG_SUMMARY_PREFIX = "📦 后台子代理任务结果汇总"
REF_SESSIONS_PREFIX = "【引用的对话记录】"

#: 参与防伪造中和的全部保留标记。
RESERVED_MARKERS: tuple[str, ...] = (
    DYNAMIC_CONTEXT_PREFIX,
    TIME_NOTE_PREFIX,
    BG_SUMMARY_PREFIX,
    REF_SESSIONS_PREFIX,
)


def neutralize_reserved_markers(text):
    """中和文本中的保留标记（幂等；仅破坏精确匹配，不改变可见文本）。

    中和手段是在标记首字符后插入一个零宽空格（U+200B）。因此：

    - 中和后的文本仍**包含**标记的可见字符序列，但不再是
      ``"\\n\\n" + marker`` 的精确匹配，``strip_context_suffix`` 不会命中；
    - ``text.replace(ZERO_WIDTH, "")`` 可还原出与原文完全一致的可见文本；
    - 幂等：已中和的文本不含原标记子串，重复调用不会叠加零宽字符。
    """
    if not text or not isinstance(text, str):
        return text
    for marker in RESERVED_MARKERS:
        if marker in text:
            text = text.replace(marker, marker[0] + ZERO_WIDTH + marker[1:])
    return text


# ---------------------------------------------------------------------------
# 系统注入 user 消息识别（展示层过滤：红点导航 / GUI 气泡 / CLI 历史回放）
#
# core/llm.py 会以 role="user" 持久化一批"系统替任务/护栏生成的提示词"：
# 后台子代理回报（task-notification）、慢步催促、总结催促、验证催促、
# 计划续推催促，以及 hook 追加上下文（core/hooks/lifecycle.py 的
# contexts_to_messages 头部）。它们不是用户发送的内容，展示层必须过滤
# （否则冒充用户发言、污染红点导航）；模型上下文（get_history）不受影响。
#
# 同步铁律：前缀与各生成点文案逐字一致，改文案必须同步本表（对账测试在
# tests/test_injection_markers.py::test_system_injected_prefixes_frozen）：
#   - llm.py 后台通知注入（文案生成于 core/tools.py:_notification）
#   - llm.py slow_step_nudge
#   - llm.py summary_request（"工具已经执行完毕…" / "你刚才只输出了思考过程…"）
#   - llm_execution.py VERIFY_CLAIM_NUDGE_TEXT
#   - llm.py 计划续推 nudge_request
SYSTEM_INJECTED_USER_PREFIXES: tuple[str, ...] = (
    "<task-notification>",
    "你在最近一步耗时过长，且既没有调用任何工具、也没有输出任何",
    "工具已经执行完毕。现在必须只输出最终中文检查结论",
    "你刚才只输出了思考过程，既没有调用工具执行任何操作",
    "你声称已完成验证，但本回合没有执行任何命令。",
    "计划尚未执行完毕，仍有可执行的计划项",
)

# hook 追加上下文头部：【{事件名} hook 追加上下文】（事件名为无空白 ASCII 标识）
_HOOK_CONTEXT_HEADER_RE = re.compile(r"^【\S+ hook 追加上下文】")


def is_system_injected_user_text(text) -> bool:
    """判定 user 消息文本是否为系统注入的提示词（展示层过滤用）。

    前缀精确匹配（容忍行首空白）；hook 头部按事件名正则。真实用户消息
    不会以这些系统文案开头；本判定只用于红点导航 / 气泡 / CLI 历史回放
    等展示路径，模型上下文（get_history）不调用它。
    """
    if not isinstance(text, str) or not text:
        return False
    stripped = text.lstrip()
    if not stripped:
        return False
    for prefix in SYSTEM_INJECTED_USER_PREFIXES:
        if stripped.startswith(prefix):
            return True
    return bool(_HOOK_CONTEXT_HEADER_RE.match(stripped))
