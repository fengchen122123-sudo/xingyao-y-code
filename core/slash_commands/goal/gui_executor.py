"""`/goal` 的 GUI 适配层：把「一轮目标」翻译成一次真实回合并等它跑完。

为什么单独成文件而不是塞进 gui_bridge：桥已经有上万行，目标模式真正需要的只有
四件事——发一条消息、等这个回合结束、读回最后一条助手回复、跑验证命令；把它们
放这里既能用 `types.SimpleNamespace` 替身直接单测（不必构造完整 ApiBridge），
也不再让桥继续膨胀。

线程模型：本模块自己不建线程，`run_round` / `run_check` 都由调用方（桥里的
GoalRunner 线程）同步调用。这样核心循环 `run_goal_loop` 保持"不带线程"的契约，
也不会出现执行器内部再套一层线程、取消信号难以传递的问题。

界面：本模块不碰任何界面对象，需要让用户看见的信息一律交回给桥（post_notice
由桥的 on_event 回调负责），保证后台线程只做广播。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from core.slash_commands.goal.runner import RoundOutcome
from core.slash_commands.goal.state import GoalStore

logger = logging.getLogger(__name__)

# 轮询回合状态的间隔（任务约定 0.5 秒）。GUI 回合通常几十秒到几分钟，
# 0.5 秒对 CPU 可忽略，又能让 `/goal stop` 在半秒内生效。
POLL_INTERVAL = 0.5

# 等会话空闲的上限：用户可能正在同一会话里手动跑别的回合，目标轮次若强行插进去
# 只会被排进消息队列、与目标顺序错位；这里等它结束，等太久就按本轮失败上抛。
IDLE_WAIT_SECONDS = 600.0

# 验证命令超时：与任务约定一致（600 秒）。跑测试类命令常常要几分钟，
# 再短会误判"未达成"。
CHECK_TIMEOUT_SECONDS = 600

# 回合线程退出后，消息可能还差最后一步落库：最多再等这么久读回复，
# 避免把"还没落库"误读成空回复（空回复会让自评判定永远拿不到达成标记）。
REPLY_WAIT_SECONDS = 2.0


def goal_store_root() -> Path:
    """目标状态落盘目录：`<AI 生成内容目录>/goals`。

    为什么复用 core.ai_output_paths：打包版与"用户自定义 AI 生成目录"都要生效，
    各调用点自行读 config 拼路径迟早分叉；也不用临时目录——目标状态要跨进程恢复，
    临时目录一清理就丢掉了"上次跑到第几轮"。
    """
    from core.ai_output_paths import resolve_ai_output_dir

    return Path(resolve_ai_output_dir()) / "goals"


def open_goal_store() -> GoalStore:
    """按既有约定打开目标状态仓库（目录由 GoalStore.save 按需创建）。"""
    return GoalStore(goal_store_root())


def _cancelled(cancel_event) -> bool:
    """兼容任意"类 Event"对象；没有 is_set 的视为未取消。

    与 runner._is_cancelled 同语义，但那个是私有函数，这里不跨模块引用私有名，
    免得日后谁把它改名导致适配层静默失效。
    """
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

    # —— 桥能力查询（软依赖，缺失时按"没有该能力"处理） ——

    def _is_turn_active(self) -> bool:
        """该会话是否有正在运行的回合。

        桥里的权威标志是 `_active_turns[session]` 线程是否存活，封装在
        ApiBridge._is_turn_active 里；没有这个方法的替身按"永远空闲"处理，
        以便单测只覆盖发送失败/成功两条主路径。
        """
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
        """读该会话在 after_msg_id 之后最后一条助手消息的正文。

        为什么要边界：回合异常/取消时本轮可能一条助手消息都没落库，不过滤就会
        读到**上一轮**的全文，把上一轮的达成标记当成这一轮的结论。
        """
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

    # —— 单轮执行 ——

    def _wait_until_idle(self, cancel_event) -> str:
        """等本会话空闲；返回 "idle" | "cancelled" | "timeout"。"""
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

        # 会话里已经在跑别的回合时先等它结束：目标消息若被排进消息队列，
        # "第 N 轮"的轮次与实际执行顺序就会错位（排队消息可能在目标轮次之后才跑）。
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
        except Exception as exc:  # 桥自身异常折算成本轮失败，由循环记入 history
            return RoundOutcome(
                status="failed", error=f"{type(exc).__name__}: {exc}"
            )

        if not isinstance(result, dict) or not result.get("ok"):
            message = ""
            if isinstance(result, dict):
                message = str(result.get("msg") or "")
            return RoundOutcome(status="failed", error=message or "回合启动失败")
        if result.get("queued"):
            # 排队说明这一轮没有立刻开跑：不确定何时执行，直接算本轮失败，
            # 而不是把"排队等待"记录成一轮已完成。
            return RoundOutcome(status="failed", error="会话正在运行其他回合，本轮未执行")

        while self._is_turn_active():
            if _cancelled(cancel_event):
                # 只返回 cancelled，不在这里调 stop_current_turn：终止真实回合
                # 由 `/goal stop` 的调用方负责，执行器保持最小职责。
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