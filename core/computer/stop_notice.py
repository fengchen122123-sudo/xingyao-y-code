"""会话级「用户已停止电脑操控」抑制状态与模型可见通知。

为什么要这个模块：用户点击停止只应结束电脑操控 run，不该终止整个对话回合
（cancel_event 是回合级信号，置位后本轮直接结束，模型毫不知情）。控制器与
工具适配层都需要一个不依赖 LLM 的轻量状态源，所以这里只用标准库、模块顶层
绝不 import core.llm，避免把循环导入拉进电脑操控链路。

抑制状态是进程级模块变量（单进程 GUI 足够，不需要落盘/跨进程同步），所有
读写都在同一把锁内完成，因为停止入口可能来自 GUI 线程，而检查来自回合线程。

解除抑制只有三条明确路径（除此之外一律保持抑制，防止模型换个 target 绕开）：
① GUI 设置里重新开启电脑操控 → deactivate_user_stop("settings_reenabled")；
② 用户新消息命中恢复关键词 → 回合入口调用 user_message_resumes_control 判定后
   调 deactivate_user_stop；
③ 进程重启（内存态自然清零）。
"""

from __future__ import annotations

import threading

# 模型侧可见的固定通知文案：必须是"用户停止了操控"这个事实，而不是"回合被
# 取消"，否则模型会把它当成外部故障并尝试重试同一动作。文案里刻意不含下方
# 任何恢复关键词，避免这条通知自身在下一回合被误判成"用户要求恢复"。
STOP_NOTICE_TEXT = (
    "[系统通知] 用户已通过界面停止了电脑操控。本任务后续不要再调用任何电脑操控工具"
    "（computer_action / computer_control / computer_select_target），也不要再尝试"
    "操作鼠标键盘或浏览器；请改用普通工具或直接向用户说明现状并等待新指令。"
    "用户明确要求恢复后才可继续使用。"
)

# 恢复关键词：命中任一即视为用户主动要求恢复电脑操控。宁可宽一点（多一两个
# 词），因为误判代价只是解除抑制、用户可再次点停止；漏判代价是模型被永久挡住。
# 例外：裸的"继续操作"过宽——"继续操作数据库"这类纯对话也会命中并顺手丢弃
# 未投递的停止通知，必须带上电脑/操控语境；"再帮我操作"是明确祈使（操作什么
# 由宾语决定，测试语义要求保留），不在此列。
_RESUME_KEYWORDS = (
    "恢复操控",
    "继续操作电脑",
    "继续操控",
    "重新操作电脑",
    "再帮我操作",
    "操作电脑",
    "操控电脑",
    "控制电脑",
    "用鼠标点",
    "帮我点一下",
)

# 单把锁保护下面两个状态：写入方是 GUI/热键线程，读取方是回合线程与工具线程。
_LOCK = threading.Lock()
_stop_active = False
_stop_reason = ""
# 待投递通知按 reason 去重：用户连点几次停止不该在模型上下文里堆出多条同样文案。
_pending_reasons: list[str] = []


def activate_user_stop(reason: str) -> None:
    """记账「用户已停止电脑操控」；幂等，后到的 reason 覆盖先到的（便于诊断）。"""
    global _stop_active, _stop_reason
    with _LOCK:
        _stop_active = True
        _stop_reason = str(reason or "user_stop")


def is_user_stop_active() -> bool:
    """当前是否处于用户停止抑制态：工具适配层与 start_run 用它硬拦截。"""
    with _LOCK:
        return _stop_active


def deactivate_user_stop(reason: str = "user_resume") -> None:
    """解除抑制（设置重开 / 用户消息命中恢复关键词）；同时清空未投递通知。

    未投递通知必须一起丢掉：用户已经要求恢复，再让模型收到"别再操作"的通知
    会与最新指令冲突，模型可能反过来拒绝执行。
    """
    global _stop_active, _stop_reason
    with _LOCK:
        _stop_active = False
        _stop_reason = ""
        _pending_reasons.clear()


def queue_stop_notice(reason: str) -> None:
    """把一条「用户已停止」通知排进待投递队列，等回合的 Step 边界 drain。"""
    with _LOCK:
        normalized = str(reason or "user_stop")
        if normalized in _pending_reasons:
            return
        # 防御性上限：正常流程一两次停止就会 drain，异常情况下也不无限增长。
        if len(_pending_reasons) < 4:
            _pending_reasons.append(normalized)


def drain_stop_notices() -> list[str]:
    """取出并清空待投递通知，返回模型侧可见的文案列表（固定文案，可能多条）。"""
    with _LOCK:
        reasons = list(_pending_reasons)
        _pending_reasons.clear()
    return [STOP_NOTICE_TEXT for _ in reasons]


def user_message_resumes_control(text: str) -> bool:
    """用户新消息是否明确要求恢复电脑操控（回合入口据此解除抑制）。"""
    content = str(text or "")
    if not content:
        return False
    return any(keyword in content for keyword in _RESUME_KEYWORDS)
