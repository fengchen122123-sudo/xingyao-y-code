from __future__ import annotations

import logging
import os
import subprocess
import time

from core.slash_commands.goal.runner import RoundOutcome
from core.slash_commands.goal.state import GoalStore

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5

IDLE_WAIT_SECONDS = 600.0

# 投递后"迟迟没有开始执行"的判定窗口：消息可能被 Ctrl+C 或 /queue clear 从队列里
# 抽走（抽走不会调 task_done，也不会回填 result），这种等待永远不会收敛，
# 必须主动放弃并记为本轮失败，否则目标循环会永久挂住。
START_WAIT_SECONDS = 60.0

# 单轮总超时（30 分钟）：比 headless 的硬超时同量级，用于兜底 worker 异常退出。
TURN_TIMEOUT_SECONDS = 1800.0

CHECK_TIMEOUT_SECONDS = 600

# 回合结束后消息可能还差最后一步落库：最多再等这么久读回复，
# 避免把"还没落库"误读成空回复（空回复会让自评判定永远拿不到达成标记）。
REPLY_WAIT_SECONDS = 2.0


def open_goal_store() -> GoalStore:
    from core.slash_commands.goal.gui_executor import goal_store_root

    return GoalStore(goal_store_root())


def _cancelled(cancel_event) -> bool:
    if cancel_event is None:
        return False
    checker = getattr(cancel_event, "is_set", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        return False


class CliGoalExecutor:

    def __init__(
        self,
        host,
        session_name: str,
        *,
        submit_turn=None,
        is_busy=None,
        history_reader=None,
        workdir: str = "",
    ):
        self._host = host
        self._session_name = str(session_name or "")
        self._submit_turn = submit_turn
        self._is_busy_override = is_busy
        self._history_reader = history_reader
        self._workdir = str(workdir or "")

    def _is_busy(self) -> bool:
        if self._is_busy_override is not None:
            return bool(self._is_busy_override())
        if bool(getattr(self._host, "is_thinking", False)):
            return True
        return (
            getattr(self._host, "active_cancel_event", None) is not None
            or getattr(self._host, "active_turn_id", None) is not None
        )

    def _submit(self, prompt: str) -> dict:
        submitter = self._submit_turn
        if submitter is None:
            submitter = getattr(self._host, "_submit_goal_turn", None)
        if submitter is not None:
            try:
                result = submitter(prompt, self._session_name)
            except Exception as exc:
                logger.warning("目标轮次投递失败", exc_info=True)
                return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
            return result if isinstance(result, dict) else {"ok": False}

        # 宿主没提供提交函数时退化为直接入队：结构必须与 headless 投递一致，
        # 否则 worker 取不到 session 快照会把消息写进"当前"会话（切换后即错位）。
        queue = getattr(self._host, "msg_queue", None)
        if queue is None:
            return {"ok": False, "msg": "CLI 消息队列不可用"}
        task = {
            "text": prompt,
            "was_queued": False,
            "is_skill_call": False,
            "session": self._session_name,
            "result": {},
        }
        try:
            queue.put(task)
        except Exception as exc:
            logger.warning("目标轮次入队失败", exc_info=True)
            return {"ok": False, "msg": f"{type(exc).__name__}: {exc}"}
        return task["result"]

    def _history_rows(self) -> list:
        reader = self._history_reader
        if reader is None:
            manager = getattr(self._host, "manager", None)
            if manager is None:
                return []
            getter = getattr(manager, "get_history_with_ids", None) or getattr(
                manager, "get_history", None
            )
            if getter is None:
                return []
            reader = lambda: getter(self._session_name)  # noqa: E731 - 局部适配层
        try:
            rows = reader() or []
        except Exception:
            logger.warning("读取会话历史失败", exc_info=True)
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _assistant_replies(self) -> list:
        replies = []
        for row in self._history_rows():
            if row.get("role") != "assistant":
                continue
            content = row.get("content")
            replies.append(content if isinstance(content, str) else "")
        return replies

    def _last_assistant_text(self, after_count: int = 0) -> str:
        replies = self._assistant_replies()
        if after_count and len(replies) <= after_count:
            return ""
        return replies[-1] if replies else ""

    def _wait_until_idle(self, cancel_event) -> str:
        deadline = time.monotonic() + IDLE_WAIT_SECONDS
        while self._is_busy():
            if _cancelled(cancel_event):
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_INTERVAL)
        return "idle"

    def _wait_turn_finished(self, result: dict, cancel_event) -> str:
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        start_deadline = time.monotonic() + START_WAIT_SECONDS
        seen_busy = False
        while "ok" not in result:
            if _cancelled(cancel_event):
                return "cancelled"
            if self._is_busy():
                seen_busy = True
            elif not seen_busy and time.monotonic() >= start_deadline:
                return "dropped"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_INTERVAL)
        return "done"

    def run_round(self, prompt: str, *, cancel_event=None) -> RoundOutcome:
        """投递一轮目标提示并等回合结束，返回本轮结果。"""
        if _cancelled(cancel_event):
            return RoundOutcome(status="cancelled")

        idle = self._wait_until_idle(cancel_event)
        if idle == "cancelled":
            return RoundOutcome(status="cancelled")
        if idle == "timeout":
            return RoundOutcome(status="failed", error="等待 CLI 空闲超时，本轮未执行")

        boundary = len(self._assistant_replies())
        result = self._submit(prompt)
        if not isinstance(result, dict):
            return RoundOutcome(status="failed", error="执行器未返回结果")

        wait = self._wait_turn_finished(result, cancel_event)
        if wait == "cancelled":
            return RoundOutcome(status="cancelled")
        if wait == "timeout":
            return RoundOutcome(status="failed", error="等待本轮回合结束超时")
        if wait == "dropped":
            return RoundOutcome(
                status="failed", error="目标消息未能进入回合链路（可能被中断或清队列丢弃）"
            )
        if result.get("ok") is False:
            message = str(result.get("error") or result.get("msg") or "")
            return RoundOutcome(status="failed", error=message or "回合执行失败")

        reply = self._last_assistant_text(boundary)
        deadline = time.monotonic() + REPLY_WAIT_SECONDS
        while not reply and time.monotonic() < deadline:
            if _cancelled(cancel_event):
                return RoundOutcome(status="cancelled")
            time.sleep(POLL_INTERVAL)
            reply = self._last_assistant_text(boundary)
        return RoundOutcome(status="completed", reply_text=reply)

    def run_check(self, command: str) -> int:
        """执行验证命令并返回退出码；任何异常一律返回 1（视为未达成）。

        为什么 shell=True：check 是用户写的一整条命令（`pytest -q`），不走 shell
        就没法带参数与管道；为什么 cwd 用当前工作目录：CLI 的项目目录就是进程
        cwd，相对路径的验证命令必须在那儿跑，否则结论恒为"未达成"。
        """
        workdir = self._workdir or os.getcwd()
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=workdir,
                timeout=CHECK_TIMEOUT_SECONDS,
                capture_output=True,
            )
        except Exception:
            logger.warning("目标验证命令执行失败: %s", command, exc_info=True)
            return 1
        try:
            return int(completed.returncode)
        except (TypeError, ValueError):
            return 1


# 任务书里写作 `_CliGoalExecutor`；真实类名与 GuiGoalExecutor 对齐（不带前导下划线，
# 因为它本来就是适配层的公开入口）。保留别名让两种写法导入到同一个类。
_CliGoalExecutor = CliGoalExecutor