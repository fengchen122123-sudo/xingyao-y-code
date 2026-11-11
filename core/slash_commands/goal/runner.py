"""`/goal` 的完成判定与轮次循环：纯逻辑，不依赖 GUI / CLI。

设计要点（设计文档 6.3 / 6.4）：
  - `judge` 只做"这轮算不算达成"的三档判断，不含任何 IO 之外的知识；
  - `run_goal_loop` 只编排"起一轮 → 判定 → 记历史"，具体怎么发起一轮由
    `RoundExecutor` 适配层决定（GUI 走回合事件、CLI 走 msg_queue）。
这样两端共享同一套循环语义，行为差异只可能出在适配层，便于定位。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Protocol

from core.slash_commands.goal.state import (
    STATUS_ACHIEVED,
    STATUS_AWAITING_CONFIRM,
    STATUS_BLOCKED,
    STATUS_RUNNING,
    STATUS_STOPPED,
    GoalState,
    GoalStore,
)

logger = logging.getLogger(__name__)

# 自评档的达成标记。用独立标签而不是"完成/已完成"这类词：模型正常叙述里随时
# 可能出现那些词，标签是唯一的机器可判定信号。
GOAL_ACHIEVED_MARKER = "<goal-status>achieved</goal-status>"

# 历史摘要的字符上限：history 会随状态一起落盘并回显给用户，整段回复存进去会
# 让 JSON 迅速膨胀（一轮可能上万字），摘要够用即可。
_SUMMARY_LIMIT = 300


@dataclass
class RoundOutcome:
    """一轮执行的结果，由适配层（GUI/CLI executor）填充。"""

    status: str = "completed"  # completed | cancelled | failed
    reply_text: str = ""
    error: str = ""
    usage: dict = field(default_factory=dict)


class RoundExecutor(Protocol):
    """适配层需要实现的单轮执行协议。

    只要求一个同步方法：循环本身跑在调用方（GUI 后台线程 / CLI daemon 线程）里，
    核心层不引入线程，避免把线程模型绑进共享代码。
    """

    def run_round(self, prompt: str, *, cancel_event) -> RoundOutcome:
        """执行一轮并把结果返回；取消时返回 status="cancelled"。"""
        ...


class _NeverCancelled:
    """永不置位的取消信号。

    让不关心取消的调用方（单测、一次性脚本）可以省略 cancel_event，同时循环里
    不必到处判空。
    """

    def is_set(self) -> bool:
        return False


def _is_cancelled(cancel_event) -> bool:
    """兼容任意"类 Event"对象；没有 is_set 的视为未取消。"""
    if cancel_event is None:
        return False
    checker = getattr(cancel_event, "is_set", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        # 取消信号自身出错时按"未取消"处理：继续下一轮，由 status 兜底收敛
        return False


def summarize(text: str, limit: int = _SUMMARY_LIMIT) -> str:
    """把一轮回复压成单行摘要（供 history 与回显使用）。"""
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)] + "…"


def build_round_prompt(state: GoalState) -> str:
    """构造本轮注入的提示词。

    必须包含四件事：目标原文（模型每轮独立起回合，不依赖上文记忆）、上一轮进展
    （否则容易重复劳动）、轮次与剩余额度（让模型知道该收敛）、达成判定要求
    （决定它要不要输出标记 / 该让哪条验证命令通过）。
    """
    last_summary = ""
    if state.history:
        last_summary = str(state.history[-1].get("summary") or "")
    remaining = max(0, state.max_rounds - state.round)
    lines = [
        f"【目标】{state.goal}",
        f"【轮次】第 {state.round} / {state.max_rounds} 轮，剩余 {remaining} 轮",
        f"【上一轮进展】{last_summary or '（无，这是第一轮）'}",
        "【要求】围绕目标继续推进并直接动手；本轮结束时用一两句话说明做了什么、"
        "离目标还差什么，不要重复上一轮已完成的工作。",
    ]
    if state.check:
        lines.append(
            f"【达成判定】本轮结束后会自动执行验证命令 `{state.check}`，退出码 0 视为达成；"
            "请优先让该命令通过。"
        )
    else:
        lines.append(
            f"【达成判定】当目标已完全达成时，在回复最后单独输出一行 "
            f"{GOAL_ACHIEVED_MARKER}；尚未达成时不要输出该标记。"
        )
    return "\n".join(lines)


def judge(state: GoalState, outcome: RoundOutcome, run_check=None) -> str:
    """判断本轮结果，返回 "achieved" | "pending_confirm" | "continue"。

    优先级按设计文档 6.3：有 check 就以退出码为准（可信），没有 check 才看自评
    标记（不可信，需要用户确认）。两档都不成立就继续下一轮。
    """
    if state.check:
        if run_check is None:
            # 配了 check 却没有执行器：判定不了，继续跑；绝不能拿自评标记冒充可信达成
            return "continue"
        try:
            code = run_check(state.check)
        except Exception:
            # 验证命令本身起不来（解释器缺失、超时）算本轮未达成，不能让循环崩掉
            return "continue"
        try:
            return "achieved" if int(code) == 0 else "continue"
        except (TypeError, ValueError):
            return "continue"
    if GOAL_ACHIEVED_MARKER in str(outcome.reply_text or ""):
        return "pending_confirm"
    return "continue"


def _persist(store: GoalStore, state: GoalState) -> None:
    """把当前内存状态落盘；写失败只记日志，绝不打断循环。

    为什么每轮都要落盘：GUI 的 GoalBar（每 5 秒轮询 get_goal_state）与 CLI 的
    `/goal status` 都读状态文件，而循环只在收尾时保存——一整轮跑几十秒到几分钟
    期间，界面会一直停在创建时的「第 0/10 轮」，用户以为目标没推进。
    """
    try:
        store.save(state)
    except Exception:  # noqa: BLE001 - 落盘失败不该让多轮循环终止
        logger.warning("目标状态落盘失败", exc_info=True)


def _notify(on_event, event: str, state: GoalState) -> None:
    """发送进度事件，吞掉回调异常。

    为什么吞掉：GUI 的 post_notice 依赖 webview 仍然存活，用户关窗时可能抛异常；
    此时循环仍必须走到 store.save，否则下次启动看到的是过期状态。
    """
    if on_event is None:
        return
    try:
        on_event(event, state)
    except Exception:
        pass


def run_goal_loop(
    state: GoalState,
    executor: RoundExecutor,
    store: GoalStore,
    on_event=None,
    cancel_event=None,
    *,
    run_check=None,
) -> GoalState:
    """驱动目标模式的多轮循环，返回收敛后的状态（同时已落盘）。

    循环只依赖 `state.status` 与 `state.round` 两个量：所有退出路径都先改状态再
    break，末尾统一判定 blocked，保证"转出循环"与"状态语义"一对一，不会出现
    循环停了状态还写着 running。
    """
    if cancel_event is None:
        cancel_event = _NeverCancelled()

    while state.round < state.max_rounds and state.status == STATUS_RUNNING:
        if _is_cancelled(cancel_event):
            state.status = STATUS_STOPPED
            break

        state.round += 1
        prompt = build_round_prompt(state)
        # 本轮一开跑就写盘：GUI 状态条与 /goal status 读的是文件，若等收尾那次
        # save，整个回合期间界面会一直停在上一轮的轮次（用户看到「第 0/10 轮」）。
        _persist(store, state)

        try:
            outcome = executor.run_round(prompt, cancel_event=cancel_event)
        except Exception as exc:  # noqa: BLE001 - 适配层任何异常都折算成本轮失败
            # 不向上冒泡：目标循环多数跑在后台线程里，异常冒泡等于静默终止且状态
            # 停在 running，用户既看不到错误也不知道要 resume。
            outcome = RoundOutcome(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )
        if outcome is None:
            outcome = RoundOutcome(status="failed", error="执行器未返回结果")

        outcome_status = str(outcome.status or "").strip().lower()
        if outcome_status == "cancelled":
            # 用户中断：本轮不算进展，也不写 history，状态直接转 stopped
            state.status = STATUS_STOPPED
            break

        if outcome_status == "failed":
            state.history.append(
                {
                    "round": state.round,
                    "summary": f"失败: {outcome.error or '未知错误'}",
                }
            )
            # 先落盘再广播：状态条收到进度后会立刻重拉，文件里必须已含本轮摘要
            _persist(store, state)
            _notify(on_event, "round_done", state)
            continue

        verdict = judge(state, outcome, run_check)
        state.history.append(
            {"round": state.round, "summary": summarize(outcome.reply_text)}
        )
        _persist(store, state)
        _notify(on_event, "round_done", state)

        if verdict == "achieved":
            state.status = STATUS_ACHIEVED
            break
        if verdict == "pending_confirm":
            # 自评不可信：停在 awaiting_confirm 等用户确认，绝不自动收尾
            state.status = STATUS_AWAITING_CONFIRM
            break
        # verdict == "continue"：跑完最后一轮时由下方 blocked 兜底

    if state.status == STATUS_RUNNING and state.round >= state.max_rounds:
        # 轮次耗尽仍未达成：转 blocked（可 resume），而不是假装还在跑
        state.status = STATUS_BLOCKED

    # 先落盘再发 finished：适配层收到 finished 后立刻查询状态/关面板，此时文件
    # 里必须已是最终状态，否则会读到中间态。
    store.save(state)
    _notify(on_event, "finished", state)
    return state
