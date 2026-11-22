from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from core.slash_commands.goal.runner import RoundOutcome
from core.slash_commands.goal.state import GoalStore

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5

IDLE_WAIT_SECONDS = 600.0

CHECK_TIMEOUT_SECONDS = 600

# 回合线程退出后，消息可能还差最后一步落库：最多再等这么久读回复，
# 避免把"还没落库"误读成空回复（空回复会让自评判定永远拿不到达成标记）。
REPLY_WAIT_SECONDS = 2.0


def goal_store_root() -> Path:
    from core.ai_output_paths import resolve_ai_output_dir

    return Path(resolve_ai_output_dir()) / "goals"


def open_goal_store() -> GoalStore:
    """按既有约定打开目标状态仓库（目录由 GoalStore.save 按需创建）。"""
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


class GuiGoalExecutor:
    """把一轮目标落到 GUI 桥上：启动真实回合 → 等结束 → 读回复 / 跑验证命令。

    全部桥能力都用 getattr 兜底：单测替身与裸实例不需要实现整套桥接口。
    """

    def __init__(self, bridge, session_name: str):
        self._bridge = bridge
        self._session_name = str(session_name or "")

    def _is_turn_active(self) -> bool:
        checker = getattr(self._bridge, "_is_turn_active", None)
        if checker is None:
            return False
        try:
            return bool(checker(self._session_name))
        except Exception:
            return False

    def _last_message_id(self) -> int:
        memory = getattr(self._bridge, "_memory", None)
        getter = getattr(memory, "get_last_message_id", None)
        if getter is None:
            return 0
        try:
            return int(getter(self._session_name) or 0)
        except Exception:
            return 0

    def _last_assistant_text(self, after_msg_id: int = 0) -> str:
        memory = getattr(self._bridge, "_memory", None)
        if memory is None:
            return ""
        rows = []
        getter = getattr(memory, "get_history_with_ids", None)
        if getter is not None:
            try:
                rows = getter(self._session_name) or []
            except Exception:
                rows = []
        if not rows:
            fallback = getattr(memory, "get_history", None)
            if fallback is None:
                return ""
            try:
                rows = fallback(self._session_name) or []
            except Exception:
                return ""
        for msg in reversed(rows):
            if msg.get("role") != "assistant":
                continue
            if after_msg_id:
                try:
                    if int(msg.get("_msg_id") or 0) <= after_msg_id:
                        # 边界之后没有助手消息：本轮没有产出，不能拿旧回复冒充
                        return ""
                except (TypeError, ValueError):
                    return ""
            content = msg.get("content")
            return content if isinstance(content, str) else ""
        return ""

    def _wait_until_idle(self, cancel_event) -> str:
        deadline = time.monotonic() + IDLE_WAIT_SECONDS
        while self._is_turn_active():
            if _cancelled(cancel_event):
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_INTERVAL)
        return "idle"

    def run_round(self, prompt: str, *, cancel_event=None) -> RoundOutcome:
        """发一轮目标提示并等回合结束，返回本轮结果。"""
        if _cancelled(cancel_event):
            return RoundOutcome(status="cancelled")

        wait_state = self._wait_until_idle(cancel_event)
        if wait_state == "cancelled":
            return RoundOutcome(status="cancelled")
        if wait_state == "timeout":
            return RoundOutcome(status="failed", error="等待会话空闲超时，本轮未执行")

        boundary = self._last_message_id()
        try:
            result = self._bridge.send_user_message(
                prompt, session_name=self._session_name, turn_source="goal"
            )
        except Exception as exc:
            return RoundOutcome(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )

        if not isinstance(result, dict) or not result.get("ok"):
            message = ""
            if isinstance(result, dict):
                message = str(result.get("msg") or "")
            return RoundOutcome(status="failed", error=message or "回合启动失败")
        if result.get("queued"):
            return RoundOutcome(status="failed", error="会话正在运行其他回合，本轮未执行")

        while self._is_turn_active():
            if _cancelled(cancel_event):
                return RoundOutcome(status="cancelled")
            time.sleep(POLL_INTERVAL)

        if _cancelled(cancel_event):
            return RoundOutcome(status="cancelled")

        reply = self._last_assistant_text(boundary)
        deadline = time.monotonic() + REPLY_WAIT_SECONDS
        while not reply and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            reply = self._last_assistant_text(boundary)
        return RoundOutcome(status="completed", reply_text=reply)

    def run_check(self, command: str) -> int:
        """执行验证命令并返回退出码；任何异常一律返回 1（视为未达成）。

        为什么用 shell=True：check 是用户写的一整条命令（`pytest -q`），不经过
        shell 就没法带参数与管道；为什么 cwd 用会话工作目录：验证命令要在项目
        目录里跑，否则相对路径全错、结论恒为"未达成"。
        """
        workdir = ""
        resolve = getattr(self._bridge, "_session_workdir", None)
        if resolve is not None:
            try:
                workdir = str(resolve(self._session_name) or "")
            except Exception:
                workdir = ""
        if not workdir or workdir.upper() == "GLOBAL":
            # 自由对话没有项目目录：回落到进程 cwd（与回合的 workdir 兜底一致）
            workdir = os.getcwd()
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