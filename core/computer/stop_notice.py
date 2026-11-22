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

_LOCK = threading.Lock()
_stop_active = False
_stop_reason = ""
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
