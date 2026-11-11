"""`/goal` 的 CLI 适配层：把「一轮目标」投递进 CLI 既有回合链路并等它跑完。

为什么不复用 gui_executor：GUI 的一轮是"桥启动一个回合线程 + 前端轮询事件"，CLI
的一轮是"消息投进 msg_queue、由唯一的后台 worker 消费"。两者"怎么发起一轮"完全
不同，而这恰好是 RoundExecutor 协议里唯一需要适配的部分——判定与循环仍由 runner
负责，git/测试里看到的行为差异只可能出在这一层。

线程模型：本模块自己不建线程，`run_round` / `run_check` 都由调用方（main.py 里的
goal-loop daemon 线程）同步调用，与 GuiGoalExecutor 保持同一契约，避免执行器内部
再套一层线程导致取消信号难以传递。

为什么宿主（host）以参数注入而不是 `import main`：CLI 用 `python main.py` 启动时，
入口模块在 sys.modules 里的名字是 "__main__"；`import main` 会把 main.py 再执行
一遍，得到**第二份** msg_queue / is_thinking 等全局——目标回合就被投进一个没有
消费者的队列，表现为"启动了但永远没动静"。因此只按名字从宿主取有限的几个能力，
取不到就按"没有该能力"降级，本模块也就能用 SimpleNamespace 替身直接单测。
"""

from __future__ import annotations

import logging
import os
import subprocess
import time

from core.slash_commands.goal.runner import RoundOutcome
from core.slash_commands.goal.state import GoalStore

logger = logging.getLogger(__name__)

# 轮询回合状态的间隔（与 GUI 端一致 0.5 秒）：一轮通常几十秒到几分钟，
# 0.5 秒的 CPU 代价可忽略，又能让 `/goal stop` 在半秒内生效。
POLL_INTERVAL = 0.5

# 等 CLI 空闲的上限：用户可能正在同一会话里手动跑别的回合，目标轮次若强行插队
# 只会与用户消息在队列里交错；这里等它结束，等太久就按本轮失败上抛。
IDLE_WAIT_SECONDS = 600.0

# 投递后"迟迟没有开始执行"的判定窗口：消息可能被 Ctrl+C 或 /queue clear 从队列里
# 抽走（抽走不会调 task_done，也不会回填 result），这种等待永远不会收敛，
# 必须主动放弃并记为本轮失败，否则目标循环会永久挂住。
START_WAIT_SECONDS = 60.0

# 单轮总超时（30 分钟）：比 headless 的硬超时同量级，用于兜底 worker 异常退出。
TURN_TIMEOUT_SECONDS = 1800.0

# 验证命令超时：与任务约定一致（600 秒）。跑测试类命令常要几分钟，再短会误判未达成。
CHECK_TIMEOUT_SECONDS = 600

# 回合结束后消息可能还差最后一步落库：最多再等这么久读回复，
# 避免把"还没落库"误读成空回复（空回复会让自评判定永远拿不到达成标记）。
REPLY_WAIT_SECONDS = 2.0


def open_goal_store() -> GoalStore:
    """打开目标状态仓库（`<AI 生成内容目录>/goals`）。

    目录约定直接复用 GUI 端的 goal_store_root：两端各自拼一遍路径迟早分叉，
    一旦分叉，GUI 里 start 的目标在 CLI 里 `/goal status` 就看不到（反之亦然），
    而"跨进程可恢复"正是这套状态机的存在意义。
    """
    from core.slash_commands.goal.gui_executor import goal_store_root

    return GoalStore(goal_store_root())


def _cancelled(cancel_event) -> bool:
    """兼容任意"类 Event"对象；没有 is_set 的视为未取消。

    与 runner._is_cancelled 同语义，但那个是私有函数，不去跨模块引用私有名。
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


class CliGoalExecutor:
    """把一轮目标落到 CLI 回合链路：投递消息 → 等结束 → 读回复 / 跑验证命令。

    宿主能力全部用 getattr 软依赖：单测与裸替身不必实现整套入口层接口。
    可选的注入参数（submit_turn / is_busy / history_reader）只为测试服务，
    生产路径一律读宿主模块的真实状态。
    """

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

    # —— 宿主能力查询（软依赖，缺失按"没有该能力"处理） ——

    def _is_busy(self) -> bool:
        """CLI 当前是否有正在执行的回合。

        三个标志任一非空都说明 worker 正在处理某个回合。这里必须每次 getattr 读
        **宿主模块属性**（即当前值），不能 `from main import is_thinking` 那样在
        导入时拷一份快照——快照永远是 False，轮询就变成"永远空闲"。
        active_cancel_event 由 worker 线程在取到任务后立刻建立（不经 schedule_ui），
        所以即使没有 prompt_toolkit App（headless）也能准确判定"回合已开始"。
        """
        if self._is_busy_override is not None:
            return bool(self._is_busy_override())
        if bool(getattr(self._host, "is_thinking", False)):
            return True
        return (
            getattr(self._host, "active_cancel_event", None) is not None
            or getattr(self._host, "active_turn_id", None) is not None
        )

    def _submit(self, prompt: str) -> dict:
        """把 prompt 作为一次普通用户消息投递进 CLI 回合链路。

        返回的是**本轮共享的结果字典**：worker 收尾时会把 ok/error 回填进同一个
        对象（main._report_task_result），所以"dict 里出现 ok 键"就是精确的本轮
        结束信号——比轮询 is_thinking 更可靠（is_thinking 在准备阶段之前还是
        False，会让我们误判"回合已经结束"而读到上一轮的回复）。
        """
        submitter = self._submit_turn
        if submitter is None:
            submitter = getattr(self._host, "_submit_goal_turn", None)
        if submitter is not None:
            try:
                result = submitter(prompt, self._session_name)
            except Exception as exc:  # 投递本身失败折算成本轮失败，不上抛给循环
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
        """读当前会话的历史消息（含 _msg_id 的实现优先）。"""
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
        """按顺序取出所有助手消息正文。"""
        replies = []
        for row in self._history_rows():
            if row.get("role") != "assistant":
                continue
            content = row.get("content")
            replies.append(content if isinstance(content, str) else "")
        return replies

    def _last_assistant_text(self, after_count: int = 0) -> str:
        """读投递之后新增的最后一条助手回复。

        为什么要有 after_count 边界：回合被取消/失败时本条可能一条助手消息都
        没落库，不过滤就会读到**上一轮**的全文，把上一轮的达成标记当成这一轮的
        结论——自评档会因此提前判定"已达成"。
        """
        replies = self._assistant_replies()
        if after_count and len(replies) <= after_count:
            return ""
        return replies[-1] if replies else ""

    def _wait_until_idle(self, cancel_event) -> str:
        """等 CLI 空闲；返回 "idle" | "cancelled" | "timeout"。"""
        deadline = time.monotonic() + IDLE_WAIT_SECONDS
        while self._is_busy():
            if _cancelled(cancel_event):
                return "cancelled"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_INTERVAL)
        return "idle"

    def _wait_turn_finished(self, result: dict, cancel_event) -> str:
        """轮询本轮结果字典直到 worker 回填；返回 done/cancelled/timeout/dropped。"""
        deadline = time.monotonic() + TURN_TIMEOUT_SECONDS
        start_deadline = time.monotonic() + START_WAIT_SECONDS
        seen_busy = False
        while "ok" not in result:
            if _cancelled(cancel_event):
                return "cancelled"
            if self._is_busy():
                seen_busy = True
            elif not seen_busy and time.monotonic() >= start_deadline:
                # 投递后一直既不忙也没有结果：消息多半已被从队列里抽走
                # （Ctrl+C 清队列 / /queue clear），继续等只会永久挂住
                return "dropped"
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(POLL_INTERVAL)
        return "done"

    # —— 单轮执行 ——

    def run_round(self, prompt: str, *, cancel_event=None) -> RoundOutcome:
        """投递一轮目标提示并等回合结束，返回本轮结果。"""
        if _cancelled(cancel_event):
            return RoundOutcome(status="cancelled")

        # 会话里已经在跑别的回合时先等它结束：目标消息若排进队列，会与用户消息
        # 交错执行，"第 N 轮"的轮次与实际执行顺序就错位了。
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
            # 只返回 cancelled，不在这里调 _cancel_active_turn：终止真实回合由
            # `/goal stop` 的调用方负责，执行器保持最小职责。
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