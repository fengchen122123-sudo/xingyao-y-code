from __future__ import annotations

import json
import os
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core.workflows.plan import PlanError, eval_condition, render_template

PublishFn = Callable[[str, dict], None]
AgentRunner = Callable[..., tuple[bool, str]]
CommandRunner = Callable[..., tuple[bool, dict]]

MAX_EVENTS = 2000


class StepFailure(Exception):
    """步骤失败（子代理失败/命令被拒/模板求值错误），fallback 可捕获。"""


class Cancelled(Exception):
    """回合取消——中断执行，不进入 fallback。"""


def _default_agent_runner(
    name: str,
    instructions: str,
    subagent_type: str,
    timeout_seconds: int,
    cancel_event=None,
) -> tuple[bool, str]:
    from core.tools import run_subagent

    try:
        raw = run_subagent(
            instructions,
            timeout_seconds=timeout_seconds,
            subagent_type=str(subagent_type or "general"),
            parent_cancel_event=cancel_event,
        )
    except Exception as exc:
        return False, f"子代理启动失败: {exc}"
    text = str(raw or "")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            status = str(data.get("status") or "")
            if status and status != "completed":
                return False, str(data.get("error") or stripped)[:2000]
        except ValueError:
            pass
    return True, text


def _default_command_runner(
    argv: list[str],
    timeout_seconds: int,
    cancel_event=None,
) -> tuple[bool, dict]:
    from core.tools import execute_command

    if os.name == "nt":
        command = subprocess.list2cmdline(argv)
    else:
        command = shlex.join(argv)
    try:
        text = execute_command(command, timeout_seconds=timeout_seconds, cancel_event=cancel_event)
    except Exception as exc:
        return False, {"output": f"命令执行异常: {exc}"}
    return True, {"output": str(text or "")}


def _scope_snapshot() -> dict:
    """并行分支的工作线程需要显式带走的线程级上下文（AGENTS.md 铁律）。"""
    snapshot: dict = {}
    try:
        from core import app_context

        snapshot["permission_mode"] = app_context.get_thread_permission_mode()
        snapshot["session"] = app_context.get_thread_session()
    except Exception:
        pass
    try:
        from core.workspace_context import get_workdir

        snapshot["workdir"] = str(get_workdir())
    except Exception:
        pass
    return snapshot


def _scope_restore(snapshot: dict) -> None:
    try:
        from core import app_context

        mode = snapshot.get("permission_mode")
        if mode:
            app_context.set_thread_permission_mode(mode)
        session = snapshot.get("session")
        if session and hasattr(app_context, "set_thread_session"):
            app_context.set_thread_session(session)
    except Exception:
        pass
    try:
        from core.workspace_context import set_workdir

        workdir = snapshot.get("workdir")
        if workdir:
            set_workdir(workdir)
    except Exception:
        pass


class PlanExecutor:
    """解释执行 plan 步骤图；一个实例对应一次运行。"""

    def __init__(
        self,
        plan: dict,
        *,
        run_id: str,
        publish: PublishFn,
        cancel_event=None,
        permission_mode: str = "confirm",
        agent_runner: AgentRunner | None = None,
        command_runner: CommandRunner | None = None,
        initial_scope: dict | None = None,
    ) -> None:
        self._plan = plan
        self._run_id = run_id
        self._publish = publish
        self._cancel_event = cancel_event
        self._permission_mode = str(permission_mode or "confirm")
        self._agent_runner = agent_runner or _default_agent_runner
        self._command_runner = command_runner or _default_command_runner
        self._scope: dict = dict(initial_scope or {})
        self._phase_total = 0
        self._phase_index = 0
        self._report_count = 0
        self._event_count = 0
        self._reports: list[str] = []
        for step in plan.get("steps") or []:
            if isinstance(step, dict) and step.get("op") == "phase":
                self._phase_total += 1

    def run(self) -> dict:
        """执行全部步骤；返回摘要。任何未捕获失败 → run_failed 事件 + 摘要。"""
        try:
            self._run_steps(self._plan.get("steps") or [])
            self._publish("run_completed", {"reports": self._report_count})
            return {
                "ok": True,
                "summary": f"工作流执行完成：{self._phase_index}/{self._phase_total} 个阶段，{self._report_count} 条产物",
                "reports": self._reports,
            }
        except Cancelled:
            self._publish("run_failed", {"error": "回合已取消，工作流中止"})
            return {"ok": False, "summary": "回合已取消，工作流中止", "reports": self._reports}
        except StepFailure as exc:
            self._publish("run_failed", {"error": str(exc)})
            return {"ok": False, "summary": f"工作流执行失败：{exc}", "reports": self._reports}
        except Exception as exc:  # 执行器自身异常也只记失败，绝不抛出工具调用
            self._publish("run_failed", {"error": f"执行器异常: {exc}"})
            return {"ok": False, "summary": f"工作流执行失败：执行器异常 {exc}", "reports": self._reports}

    def _emit(self, kind: str, payload: dict) -> None:
        self._event_count += 1
        if self._event_count > MAX_EVENTS:
            raise StepFailure(f"事件数超过 {MAX_EVENTS} 上限（plan 放大效应异常）")
        self._publish(kind, payload)

    def _check_cancel(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise Cancelled()

    def _run_steps(self, steps) -> None:
        for step in steps or []:
            self._check_cancel()
            if not isinstance(step, dict):
                raise StepFailure("步骤必须是对象")
            op = step.get("op")
            handler = getattr(self, f"_step_{op}", None)
            if handler is None:
                raise StepFailure(f"未知步骤 op: {op!r}")
            handler(step)

    def _step_phase(self, step: dict) -> None:
        self._phase_index += 1
        name = str(step.get("name") or "")
        self._emit("phase_started", {"index": self._phase_index, "total": self._phase_total, "name": name})

    def _step_agent(self, step: dict) -> None:
        name = str(step.get("name") or f"agent-{self._event_count}")
        instructions = render_template(str(step.get("instructions") or ""), self._scope)
        subagent_type = str(step.get("subagent_type") or "general")
        timeout = int(step.get("timeout_seconds") or 600)
        self._emit("agent_started", {"name": name, "subagent_type": subagent_type, "instructions": instructions[:500]})
        ok, output = self._agent_runner(
            name, instructions, subagent_type, timeout, self._cancel_event
        )
        self._emit(
            "agent_completed" if ok else "agent_failed",
            {"name": name, "output": str(output)[:2000]},
        )
        if not ok:
            raise StepFailure(f"子代理「{name}」失败: {str(output)[:300]}")
        bind = step.get("bind")
        if bind:
            self._scope[str(bind)] = output

    def _step_command(self, step: dict) -> None:
        argv = [str(item) for item in (step.get("argv") or [])]
        timeout = int(step.get("timeout_seconds") or 30)
        if self._permission_mode not in ("accept_edits", "full"):
            reason = (
                f"命令步骤需要 accept_edits/full 权限档（当前 {self._permission_mode}），"
                "已跳过；如需运行命令请切换权限档位后重跑"
            )
            self._emit("command_skipped", {"argv": argv, "reason": reason})
            raise StepFailure(reason)
        self._emit("command_started", {"argv": argv, "timeout_seconds": timeout})
        ok, result = self._command_runner(argv, timeout, self._cancel_event)
        self._emit("command_completed" if ok else "command_failed", {"argv": argv, **result})
        bind = step.get("bind")
        if bind:
            self._scope[str(bind)] = result

    def _step_report(self, step: dict) -> None:
        self._report_count += 1
        value = render_template(str(step.get("value") or ""), self._scope)
        label = str(step.get("label") or f"#{self._report_count}")
        self._emit("report_item", {"label": label, "value": value[:4000]})
        self._reports.append(f"{label}: {value}")

    def _step_log(self, step: dict) -> None:
        message = render_template(str(step.get("message") or ""), self._scope)
        self._emit("log_line", {"message": message[:1000]})

    def _step_parallel(self, step: dict) -> None:
        branches = step.get("steps") or []
        snapshot = _scope_snapshot()
        failures: list[str] = []

        def _run_branch(branch) -> None:
            _scope_restore(snapshot)
            try:
                self._run_steps([branch] if isinstance(branch, dict) else branch)
            except StepFailure as exc:
                failures.append(str(exc))

        if len(branches) == 1:
            _run_branch(branches[0])
        else:
            with ThreadPoolExecutor(max_workers=min(len(branches), 8)) as pool:
                list(pool.map(_run_branch, branches))
        if failures:
            raise StepFailure(f"并行分支失败: {'; '.join(failures[:3])}")

    def _step_loop(self, step: dict) -> None:
        max_iterations = int(step.get("max") or 1)
        while_expr = step.get("while")
        for iteration in range(1, max_iterations + 1):
            self._check_cancel()
            if while_expr is not None:
                try:
                    should_run = eval_condition(str(while_expr), self._scope)
                except PlanError:
                    should_run = True
                if not should_run:
                    break
            self._emit("loop_iteration", {"iteration": iteration, "max": max_iterations})
            try:
                self._run_steps(step.get("steps") or [])
            except StepFailure as exc:
                if while_expr is not None:
                    raise
                # 无条件循环（固定次数重试型）：失败即终止循环，后续步骤可用
                # fallback 兜底——与「跑到成功为止」的意图一致
                self._emit("log_line", {"message": f"第 {iteration} 轮失败退出循环: {exc}"})
                break

    def _step_fallback(self, step: dict) -> None:
        try:
            self._run_steps(step.get("steps") or [])
        except StepFailure as exc:
            self._check_cancel()
            self._emit("fallback_used", {"error": str(exc)[:500]})
            self._run_steps(step.get("fallback") or [])


def execute_plan(
    plan: dict,
    *,
    run_id: str = "",
    publish: PublishFn | None = None,
    cancel_event=None,
    permission_mode: str = "confirm",
    agent_runner: AgentRunner | None = None,
    command_runner: CommandRunner | None = None,
    initial_scope: dict | None = None,
) -> dict:
    executor = PlanExecutor(
        plan,
        run_id=run_id,
        publish=publish or (lambda kind, payload: None),
        cancel_event=cancel_event,
        permission_mode=permission_mode,
        agent_runner=agent_runner,
        command_runner=command_runner,
        initial_scope=initial_scope,
    )
    return executor.run()
