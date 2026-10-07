"""DebugSession 端到端测试：以 sys.executable 跑 tests/fake_dap_adapter.py 真进程。

覆盖（规格 9 的 dap_manager 条目）：断点与停止事件流转、调用栈/变量/求值、
单步与继续、重启能力探测、停止后进程回收。

S3-T4 追加的三组钉子：
- 失败即回收：编排在 initialize 之后任何一步失败或超时，适配器进程都必须真的退出
  （按 pid 做存活探测，不看对象字段），且报错是可直接展示的中文原因；
- 停止事件处理必须离开读线程（铁律：读线程内同步等响应＝自锁）；
- launch 响应延后到 configurationDone 才收（早收会把「启动调试」卡死）。
"""

import ctypes
import os
import sys
import threading
import time

import pytest

from core.ide import dap_manager as dm
from core.ide.debug_adapters import STDIO, AdapterSpec
from core.ide.debug_session import DebugSession, DebugSessionError, build_launch_arguments

STUB = os.path.join(os.path.dirname(__file__), "fake_dap_adapter.py")
_KERNEL32 = None
_STILL_ACTIVE = 259


def _spec(no_restart=False):
    return AdapterSpec("debugpy", "python", [sys.executable, STUB], STDIO)


def _spec_with(*flags):
    """带注入开关的适配器规格（--no-restart / --fail-command=X / --hang-command=X）。"""
    return AdapterSpec("debugpy", "python", [sys.executable, STUB, *flags], STDIO)


def _process_exists(pid):
    """按 pid 真实探测进程是否还在（不依赖 Popen 对象字段）。

    `Popen.poll()` 也是 OS 级探针，但它只回答「我这次等的就是它」；这里再独立按 pid
    查一次，避免「对象字段说死了、进程其实还活着」这类漏判（评审第 1 条的验收口径）。
    """
    global _KERNEL32
    if not pid:
        return False
    if os.name == "nt":
        if _KERNEL32 is None:
            _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
            _KERNEL32.OpenProcess.restype = ctypes.c_void_p
            _KERNEL32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            _KERNEL32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)
            ]
            _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]
        # PROCESS_QUERY_LIMITED_INFORMATION：只查状态，不要任何写权限
        handle = _KERNEL32.OpenProcess(0x1000, 0, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_uint32()
            if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == _STILL_ACTIVE
        finally:
            _KERNEL32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _spawn_recorder(spawned):
    """真 spawn（走 DapProcess 默认的 _spawn_adapter）+ 留 Popen 句柄供回收断言。"""

    def spawn(command, cwd):
        proc = dm._spawn_adapter(command, cwd)
        spawned.append(proc)
        return proc

    return spawn


def _wait(event, timeout=10.0):
    assert event.wait(timeout), "事件未在超时内到达"


def _wait_until(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture()
def collected():
    return {
        "events": [],
        "emits": [],
        "stopped": threading.Event(),
        "output": threading.Event(),
    }


def _session(collected, no_restart=False, spec=None, spawn=None, **kwargs):
    def emit(event_type, data):
        collected["events"].append((event_type, data))
        collected["emits"].append((event_type, threading.current_thread().name))
        if event_type == "dap_stopped":
            collected["stopped"].set()
        if event_type == "dap_output":
            collected["output"].set()

    if no_restart:
        os.environ["FAKE_DAP_NO_RESTART"] = "1"
    session = DebugSession(
        spec or _spec(no_restart),
        program=r"C:\proj\app.py",
        cwd=os.getcwd(),
        emit=emit,
        spawn=spawn,
        **kwargs
    )
    return session


def _types(collected):
    return [event_type for event_type, _ in collected["events"]]


# ------------------------------------------------------------------ launch 参数构造


def test_build_launch_arguments_per_adapter():
    python_payload = build_launch_arguments(
        "debugpy", "python", r"C:\proj\app.py", r"C:\proj", args=["--x"], python=sys.executable
    )
    assert python_payload["program"] == r"C:\proj\app.py"
    assert python_payload["python"] == [sys.executable]
    assert python_payload["request"] == "launch" and python_payload["console"] == "internalConsole"
    assert python_payload["args"] == ["--x"] and python_payload["justMyCode"] is True

    go_payload = build_launch_arguments("dlv", "go", r"C:\proj\main.go", r"C:\proj")
    assert go_payload["mode"] == "debug" and go_payload["type"] == "go"

    lldb_payload = build_launch_arguments(
        "lldb-dap", "cpp", r"C:\proj\build\app.exe", r"C:\proj", program=r"C:\proj\build\app.exe"
    )
    assert lldb_payload["type"] == "lldb" and lldb_payload["program"] == r"C:\proj\build\app.exe"


# ------------------------------------------------------------------ 编排主路径


def test_start_returns_capabilities_and_emits_stopped_with_top_frame(collected):
    session = _session(collected)
    try:
        info = session.start()
        assert info["adapter"] == "debugpy" and info["capabilities"]["supportsConfigurationDoneRequest"] is True

        verified = session.set_breakpoints(r"C:\proj\app.py", [7, 11])
        assert [bp["line"] for bp in verified] == [7, 11]

        session.configuration_done()

        _wait(collected["stopped"])
        stopped = [data for kind, data in collected["events"] if kind == "dap_stopped"][0]
        assert stopped["reason"] == "breakpoint" and stopped["thread_id"] == 1
        assert stopped["path"] == r"C:\proj\app.py" and stopped["line"] == 7
        assert stopped["frame_id"] == 1001
    finally:
        session.stop()


def test_output_event_carries_category_and_text(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["output"])
        outputs = [data for kind, data in collected["events"] if kind == "dap_output"]
        assert any("stub adapter ready" in data["text"] for data in outputs)
        assert all(data["category"] in {"stdout", "stderr", "console"} for data in outputs)
    finally:
        session.stop()


def test_inspect_requests_return_normalized_shapes(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["stopped"])

        assert session.threads() == [{"id": 1, "name": "MainThread"}]
        frames = session.stack_trace(1, levels=1)
        assert frames[0]["id"] == 1001 and frames[0]["line"] == 7
        scopes = session.scopes(1001)
        assert scopes[0]["variablesReference"] == 2001
        variables = session.variables(2001)
        assert variables[0]["name"] == "x" and variables[0]["value"] == "42"
        result = session.evaluate("1+1", frame_id=1001)
        assert result["result"] == "eval:1+1" and result["type"] == "str"
    finally:
        session.stop()


def test_continue_and_step_emit_expected_events(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["stopped"])

        session.continue_(1)
        assert "dap_continued" in _types(collected)

        collected["stopped"].clear()
        session.next_(1)
        _wait(collected["stopped"])
    finally:
        session.stop()


def test_restart_without_capability_raises(collected):
    session = _session(collected, no_restart=True)
    try:
        session.start()
        with pytest.raises(DebugSessionError, match="不支持重启"):
            session.restart()
    finally:
        os.environ.pop("FAKE_DAP_NO_RESTART", None)
        session.stop()


def test_stop_reaps_adapter_process(collected):
    spawned = []
    session = _session(collected, spawn=_spawn_recorder(spawned))
    info = session.start()
    session.set_breakpoints(r"C:\proj\app.py", [7])
    session.configuration_done()
    _wait(collected["stopped"])
    assert session.is_alive() is True

    session.stop()

    assert session.is_alive() is False
    assert _process_exists(info["pid"]) is False


def test_stop_is_idempotent(collected):
    session = _session(collected)
    session.start()
    session.stop()
    session.stop()
    assert session.is_alive() is False


def test_start_fails_when_adapter_exits_before_initialized(collected):
    """适配器起来就死（命令写错）时必须报错，不能永久等待。"""
    session = DebugSession(
        AdapterSpec("debugpy", "python", [sys.executable, "-c", "raise SystemExit(1)"], STDIO),
        program=r"C:\proj\app.py",
        cwd=os.getcwd(),
        emit=lambda event_type, data: None,
        request_timeout=1.0,
    )
    started = time.monotonic()
    with pytest.raises(DebugSessionError):
        session.start()
    assert time.monotonic() - started < 15.0
    session.stop()


# ------------------------------------------------------------------ 失败即回收（评审第 1 条）


def test_breakpoint_failure_reaps_adapter_process(collected):
    """initialize 之后某一步失败：适配器进程必须真的退出，不留半启动会话。"""
    spawned = []
    session = _session(
        collected,
        spec=_spec_with("--fail-command=setBreakpoints"),
        spawn=_spawn_recorder(spawned),
        request_timeout=5.0,
    )
    info = session.start()
    assert _process_exists(info["pid"]) is True

    with pytest.raises(DebugSessionError, match="设置断点失败"):
        session.set_breakpoints(r"C:\proj\app.py", [7])

    assert session.is_alive() is False
    assert _wait_until(lambda: spawned[0].poll() is not None)
    assert _process_exists(info["pid"]) is False, "失败路径上必须回收适配器进程"

    session.stop()  # 失败之后再 stop 也必须安全（幂等）
    with pytest.raises(DebugSessionError, match="调试会话已结束"):
        session.set_breakpoints(r"C:\proj\app.py", [7])


def test_breakpoint_timeout_reaps_adapter_process(collected):
    """setBreakpoints 无响应（超时）：同样要回收进程，并给出可诊断的中文原因。"""
    spawned = []
    session = _session(
        collected,
        spec=_spec_with("--hang-command=setBreakpoints"),
        spawn=_spawn_recorder(spawned),
        request_timeout=0.5,
    )
    info = session.start()

    started = time.monotonic()
    with pytest.raises(DebugSessionError, match="设置断点失败"):
        session.set_breakpoints(r"C:\proj\app.py", [7])

    assert time.monotonic() - started < 8.0, "超时必须收敛，不能挂在这里"
    assert session.is_alive() is False
    assert _process_exists(info["pid"]) is False


def test_launch_failure_reaps_adapter_process(collected):
    """launch 的失败要等 configurationDone 收响应才暴露：暴露了也必须回收。"""
    spawned = []
    session = _session(
        collected,
        spec=_spec_with("--fail-command=launch"),
        spawn=_spawn_recorder(spawned),
        request_timeout=5.0,
    )
    info = session.start()
    session.set_breakpoints(r"C:\proj\app.py", [7])

    with pytest.raises(DebugSessionError, match="启动调试程序失败"):
        session.configuration_done()

    assert session.is_alive() is False
    assert _process_exists(info["pid"]) is False


def test_launch_timeout_reaps_adapter_process(collected):
    """launch 响应永不到（适配器卡住）：LAUNCH_TIMEOUT 后报错并回收。"""
    spawned = []
    session = _session(
        collected,
        spec=_spec_with("--hang-command=launch"),
        spawn=_spawn_recorder(spawned),
        request_timeout=5.0,
        launch_timeout=0.5,
    )
    info = session.start()

    with pytest.raises(DebugSessionError, match="启动调试程序失败"):
        session.configuration_done()

    assert session.is_alive() is False
    assert _process_exists(info["pid"]) is False


def test_context_manager_reaps_adapter_process(collected):
    """适配器进程在异常路径上也要被回收：会话支持 with（DapProcess 自己没有 __del__）。"""
    spawned = []
    info = None
    with pytest.raises(RuntimeError):
        with _session(collected, spawn=_spawn_recorder(spawned)) as session:
            info = session.start()
            session.set_breakpoints(r"C:\proj\app.py", [7])
            raise RuntimeError("模拟编排中途炸掉")

    assert _process_exists(info["pid"]) is False
    assert "dap_terminated" in _types(collected)


# ------------------------------------------------------------------ 线程铁律（读线程禁止同步等待）


def test_stopped_handling_never_runs_in_adapter_reader_thread(collected):
    """停止事件里要发 stackTrace 请求：只能在读线程之外做，否则与读循环自锁。"""
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["stopped"])
    finally:
        session.stop()

    stopped_threads = [name for kind, name in collected["emits"] if kind == "dap_stopped"]
    assert stopped_threads == ["ide-dap-stopped"], (
        "stopped 事件必须由独立工作线程补齐栈帧，绝不能蹲在读线程里等响应"
    )