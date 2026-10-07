"""DAP 调试会话编排：把一次「启动调试 → 断点 → 单步 → 变量 → 停止」收敛成一个对象。

时序以 debugpy 为准（lldb-dap 同序）：initialize → launch（异步发）→ 等 initialized
事件 → setBreakpoints（逐文件）→ configurationDone → 再收 launch 响应。launch 响应
故意延后收：真适配器要等被调试程序真正启动才回，早收会把「启动调试」卡死在桥方法里。

事件归一化后统一经注入的 emit(event_type, data) 外发；emit 的实现只允许
gui_bridge.emit_event（内部 put_nowait 进 _js_queue），本模块不触碰 WebView，
core 层不引入任何 webview 依赖。

线程模型（这边最容易踩死锁，先读这段）：
- `DapProcess.on_event` 是在 **DAP 读线程内同步调用** 的（`dap_manager._dispatch`）。
  所以 `_on_adapter_event` 只做两件事：`emit`（等价于 put_nowait 投递），或另起线程。
  **绝不在回调里调用任何同步等待响应的请求**（`send_request`）——那是等自己把响应读回来。
- 「stopped 后取顶部栈帧」必须发 stackTrace 请求，只能搬到别的线程做（`ide-dap-stopped`）。
  `DapProcess` 现在有运行时护栏（`DapReaderBlockingError`）兜住写错的代码，本模块不依赖
  「记得别写错」。
- 失败即回收：编排任何一步失败或超时，都要终止适配器进程——`DapProcess` 自己没有
  `__del__` / 上下文管理器（S3-T3 评审），忘了 `terminate()` 就是一个活着的调试器进程
  白白留在用户机器上。失败一律收敛成 `DebugSessionError`（中文原因，可直接展示）。
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.ide.dap_manager import DapProcess, DapProtocolError, DapRequestError

logger = logging.getLogger(__name__)

# 等 initialized 事件（适配器就绪）与 launch 响应（被调试程序真正启动）的超时
STOP_EVENT_TIMEOUT = 10.0
LAUNCH_TIMEOUT = 20.0
# disconnect 只求优雅收尾：卡住的适配器交给 terminate() 强杀，不在这里久等
_DISCONNECT_TIMEOUT = 5.0
# 补齐 stopped 栈帧的工作线程名（测试钉住它：必须不是 ide-dap-reader）
STOPPED_THREAD_NAME = "ide-dap-stopped"


class DebugSessionError(Exception):
    """调试会话失败（承载可直接展示给用户的中文消息）。"""


def build_launch_arguments(
    adapter_id: str,
    language_id: str,
    path: str,
    cwd: str,
    program: str = "",
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    python: str = "",
    stop_on_entry: bool = False,
) -> Dict[str, Any]:
    """按适配器构造 DAP launch 参数（program 缺省用当前文件）。"""
    arguments = list(args or [])
    target = program or path
    base: Dict[str, Any] = {
        "request": "launch",
        "program": target,
        "cwd": cwd,
        "args": arguments,
        "env": dict(env or {}),
        "stopOnEntry": bool(stop_on_entry),
    }
    if adapter_id == "debugpy":
        return {
            **base,
            "type": "python",
            "console": "internalConsole",
            "justMyCode": True,
            "redirectOutput": True,
            "python": [python] if python else [],
        }
    if adapter_id == "dlv":
        return {**base, "type": "go", "mode": "debug"}
    return {**base, "type": "lldb"}


class DebugSession:
    """一次调试会话：DAP 流程编排 + 事件归一化 + 失败即回收。"""

    def __init__(
        self,
        spec,
        program: str,
        cwd: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        spawn: Optional[Callable[..., Any]] = None,
        request_timeout: float = 10.0,
        stop_on_entry: bool = False,
        python: str = "",
        launch_timeout: float = LAUNCH_TIMEOUT,
        ready_timeout: float = STOP_EVENT_TIMEOUT,
    ):
        self.session_id = f"dap-{uuid.uuid4().hex[:8]}"
        self._spec = spec
        self._program = program
        self._cwd = cwd
        self._args = list(args or [])
        self._env = dict(env or {})
        self._emit = emit or (lambda event_type, data: None)
        self._stop_on_entry = stop_on_entry
        self._python = python
        self._launch_timeout = launch_timeout
        self._ready_timeout = ready_timeout
        self._initialized = threading.Event()
        self._stopped_lock = threading.Lock()
        self._capabilities: Dict[str, Any] = {}
        self._launch_seq = 0
        self._failure = ""
        # 会话已结束（stop()/失败回收/析构都会置位）：_handle_stopped 靠它决定要不要补发事件
        self._closed = False
        self._process = DapProcess(
            spec,
            cwd=cwd,
            on_event=self._on_adapter_event,
            on_stderr=lambda text: self._emit(
                "dap_output",
                {"session_id": self.session_id, "category": "stderr", "text": text},
            ),
            spawn=spawn,
            request_timeout=request_timeout,
        )

    # ------------------------------------------------------------------ 生命周期

    @property
    def capabilities(self) -> Dict[str, Any]:
        return dict(self._capabilities)

    def is_alive(self) -> bool:
        return self._process.is_alive()

    def start(self) -> Dict[str, Any]:
        """起适配器并走 initialize → launch（异步）→ 等 initialized。"""
        try:
            info = self._process.start()
        except Exception as e:
            raise self._fail("启动调试适配器", e) from e
        try:
            response = self._process.send_request("initialize", {
                "clientID": "ycode",
                "clientName": "星瑶",
                "adapterID": self._spec.adapter_id,
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariableType": True,
                "supportsVariablePaging": False,
                "supportsRunInTerminalRequest": False,
            })
            self._capabilities = dict(response.get("capabilities") or {})
            self._launch_seq = self._process.send_request_async("launch", self._launch_arguments())
            if not self._initialized.wait(self._ready_timeout):
                raise DebugSessionError(
                    f"调试适配器未在 {self._ready_timeout:.0f}s 内就绪（未收到 initialized 事件）"
                )
        except Exception as e:
            # 半启动的会话不许留下：_fail 会先回收进程再抛中文原因
            raise self._fail("初始化", e) from e
        return {
            "session_id": self.session_id,
            "adapter": self._spec.adapter_id,
            "pid": info.get("pid"),
            "capabilities": self._capabilities,
        }

    def _launch_arguments(self) -> Dict[str, Any]:
        return build_launch_arguments(
            self._spec.adapter_id,
            self._spec.language_id,
            self._program,
            self._cwd,
            args=self._args,
            env=self._env,
            python=self._python,
            stop_on_entry=self._stop_on_entry,
        )

    def set_breakpoints(self, path: str, lines: List[int]) -> List[Dict[str, Any]]:
        unique: List[int] = []
        for line in lines:
            try:
                value = int(line)
            except (TypeError, ValueError):
                continue  # 前端把行号传成字符串是常态；非法值直接忽略，不要炸掉会话
            if value > 0:
                unique.append(value)
        try:
            body = self._process.send_request("setBreakpoints", {
                "source": {"name": os.path.basename(path), "path": path},
                "breakpoints": [{"line": line} for line in sorted(set(unique))],
            })
        except Exception as e:
            raise self._fail("设置断点", e) from e
        return [bp for bp in (body.get("breakpoints") or []) if isinstance(bp, dict)]

    def configuration_done(self) -> None:
        """收尾配置并收 launch 响应（启动失败/程序路径写错都在这里暴露）。"""
        try:
            if self._capabilities.get("supportsConfigurationDoneRequest", True):
                self._process.send_request("configurationDone", {})
            seq, self._launch_seq = self._launch_seq, 0
            if seq:
                self._process.await_response(seq, timeout=self._launch_timeout)
        except Exception as e:
            raise self._fail("启动调试程序", e) from e

    def stop(self) -> None:
        """结束会话并回收适配器进程（幂等；异常路径上也应当调）。"""
        self._reap(graceful=True)

    def _reap(self, graceful: bool) -> None:
        process = self._process
        # 先置位、再关通道：正在补栈帧的 ide-dap-stopped 线程能立刻看到「会话已结束」，
        # 不会在 dap_terminated 之后又补发一条 frame_id=0 的停止事件（事件顺序倒置）
        self._closed = True
        try:
            if graceful and process.is_alive():
                process.send_request(
                    "disconnect", {"terminateDebuggee": True}, timeout=_DISCONNECT_TIMEOUT
                )
        except Exception as e:
            # disconnect 只是优雅收尾：失败也要走下去，terminate() 会强杀整棵树
            logger.debug("disconnect 未成功，直接回收适配器进程：%s", e)
        finally:
            try:
                process.terminate()
            except Exception:
                logger.debug("回收调试适配器进程失败", exc_info=True)

    def _fail(self, stage: str, exc: BaseException) -> DebugSessionError:
        """失败即回收：终止适配器进程，把原因包装成可直接展示的中文异常。"""
        reason = str(exc).strip() or exc.__class__.__name__
        logger.debug("调试会话 %s 在「%s」阶段失败：%s", self.session_id, stage, reason)
        self.stop()
        self._failure = f"{stage}失败：{reason}"
        return DebugSessionError(self._failure)

    def __enter__(self) -> "DebugSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False

    def __del__(self):
        """兜底回收（DapProcess 自己没有 `__del__`/上下文管理器，见 S3-T3 评审）。

        调用方在异常路径上忘了 `stop()` 时，GC 也保证不留活着的调试器进程。析构期不做
        disconnect：那时不允许再等任何响应（可能被 GC 收在任意线程、任意异常路径上）。
        """
        try:
            self._reap(graceful=False)
        except Exception:
            pass

    # ------------------------------------------------------------------ 执行控制

    def continue_(self, thread_id: int) -> Dict[str, Any]:
        return self._request("继续运行", "continue", {
            "threadId": int(thread_id), "singleThread": False,
        })

    def pause(self, thread_id: int) -> Dict[str, Any]:
        return self._request("暂停", "pause", {"threadId": int(thread_id)})

    def next_(self, thread_id: int) -> Dict[str, Any]:
        return self._request("单步跳过", "next", {"threadId": int(thread_id)})

    def step_in(self, thread_id: int) -> Dict[str, Any]:
        return self._request("单步进入", "stepIn", {"threadId": int(thread_id)})

    def step_out(self, thread_id: int) -> Dict[str, Any]:
        return self._request("单步跳出", "stepOut", {"threadId": int(thread_id)})

    def restart(self) -> Dict[str, Any]:
        if not self._capabilities.get("supportsRestartRequest"):
            raise DebugSessionError("当前调试适配器不支持重启，请停止后重新启动调试")
        return self._request("重启调试", "restart", {})

    # ------------------------------------------------------------------ 查询

    def threads(self) -> List[Dict[str, Any]]:
        body = self._request("获取线程列表", "threads", {})
        return [
            {"id": int(item.get("id") or 0), "name": str(item.get("name") or "")}
            for item in (body.get("threads") or []) if isinstance(item, dict)
        ]

    def stack_trace(self, thread_id: int, levels: int = 20) -> List[Dict[str, Any]]:
        body = self._request("获取调用栈", "stackTrace", {
            "threadId": int(thread_id), "startFrame": 0, "levels": int(levels),
        })
        frames: List[Dict[str, Any]] = []
        for raw in body.get("stackFrames") or []:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            frames.append({
                "id": int(raw.get("id") or 0),
                "name": str(raw.get("name") or ""),
                "path": str(source.get("path") or ""),
                "line": int(raw.get("line") or 0),
                "column": int(raw.get("column") or 0),
            })
        return frames

    def scopes(self, frame_id: int) -> List[Dict[str, Any]]:
        body = self._request("获取作用域", "scopes", {"frameId": int(frame_id)})
        return [
            {
                "name": str(item.get("name") or ""),
                "variablesReference": int(item.get("variablesReference") or 0),
                "expensive": bool(item.get("expensive")),
            }
            for item in (body.get("scopes") or []) if isinstance(item, dict)
        ]

    def variables(self, variables_reference: int) -> List[Dict[str, Any]]:
        body = self._request("获取变量", "variables", {
            "variablesReference": int(variables_reference),
        })
        return [
            {
                "name": str(item.get("name") or ""),
                "value": str(item.get("value") or ""),
                "type": str(item.get("type") or ""),
                "variablesReference": int(item.get("variablesReference") or 0),
            }
            for item in (body.get("variables") or []) if isinstance(item, dict)
        ]

    def evaluate(self, expression: str, frame_id: int = 0, context: str = "repl") -> Dict[str, Any]:
        body = self._request("求值", "evaluate", {
            "expression": str(expression), "frameId": int(frame_id), "context": context,
        })
        return {
            "result": str(body.get("result") or ""),
            "type": str(body.get("type") or ""),
            "variablesReference": int(body.get("variablesReference") or 0),
        }

    def _request(
        self,
        stage: str,
        command: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """运行期单条请求：失败只报当前动作，**不拆会话**（求值失败是调试常态）。

        编排阶段（initialize/launch/setBreakpoints/configurationDone）不走这里，
        那些步骤失败必须回收进程，见 `set_breakpoints` / `configuration_done` / `start`。
        """
        try:
            return self._process.send_request(command, arguments, timeout=timeout)
        except Exception as e:
            raise DebugSessionError(
                f"{stage}失败：{str(e).strip() or e.__class__.__name__}"
            ) from e

    # ------------------------------------------------------------------ 适配器事件

    def _on_adapter_event(self, name: str, body: Dict[str, Any]) -> None:
        """适配器事件回调。**运行在 DAP 读线程内**：只许投递或另起线程（见模块 docstring）。"""
        if name == "initialized":
            self._initialized.set()
            return
        if name == "output":
            self._emit("dap_output", {
                "session_id": self.session_id,
                "category": str(body.get("category") or "stdout"),
                "text": str(body.get("output") or ""),
            })
            return
        if name == "stopped":
            threading.Thread(
                target=self._handle_stopped,
                args=(body,),
                daemon=True,
                name=STOPPED_THREAD_NAME,
            ).start()
            return
        if name == "continued":
            self._emit("dap_continued", {
                "session_id": self.session_id, "thread_id": int(body.get("threadId") or 0),
            })
            return
        if name == "exited":
            self._emit("dap_exited", {
                "session_id": self.session_id, "exit_code": int(body.get("exitCode") or 0),
            })
            return
        if name == "terminated":
            self._emit("dap_terminated", {"session_id": self.session_id})
            return
        if name == "breakpoint":
            breakpoint = body.get("breakpoint") if isinstance(body.get("breakpoint"), dict) else {}
            source = breakpoint.get("source") if isinstance(breakpoint.get("source"), dict) else {}
            self._emit("dap_breakpoint_changed", {
                "session_id": self.session_id,
                "path": str(source.get("path") or ""),
                "line": int(breakpoint.get("line") or 0),
                "verified": bool(breakpoint.get("verified")),
            })

    def _handle_stopped(self, body: Dict[str, Any]) -> None:
        """在独立工作线程里补齐「停在哪」：取顶部栈帧要发 stackTrace 请求。

        本方法**绝不能**跑在读线程里（那种写法会与读循环自锁）：见模块 docstring 与
        `test_stopped_handling_never_runs_in_adapter_reader_thread`。
        """
        with self._stopped_lock:
            if self._closed:
                # 会话已经结束（stop()/失败回收）：这条 stopped 是残留事件，补发它只会让前端
                # 在 dap_terminated 之后又「跳回第 0 行」——事件顺序倒置，直接丢弃
                logger.debug("会话已结束，丢弃迟到的停止事件：%s", body.get("reason"))
                return
            thread_id = int(body.get("threadId") or 0)
            frame: Dict[str, Any] = {}
            try:
                frames = self.stack_trace(thread_id, levels=1)
                frame = frames[0] if frames else {}
            except Exception as e:
                # 口径必须是 Exception 而非 DebugSessionError：stack_trace 内部还有
                # int(raw.get("line") or 0) 这类转换，适配器推来畸形字段（"x"/dict）会抛
                # ValueError/TypeError；漏出去会让本线程直接死掉 → dap_stopped 一次都不发，
                # 前端永远不知道程序已经停下。取不到位置不影响「已停止」本身，事件照发。
                logger.debug("取停止位置失败：%s", e)
            if self._closed:
                # 取栈帧期间会话被结束了（栈帧请求可能等了一小会儿）：同样不强发
                return
            self._emit("dap_stopped", {
                "session_id": self.session_id,
                "reason": str(body.get("reason") or "breakpoint"),
                "thread_id": thread_id,
                "frame_id": int(frame.get("id") or 0),
                "path": str(frame.get("path") or ""),
                "line": int(frame.get("line") or 0),
            })