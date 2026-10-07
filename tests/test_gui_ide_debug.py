"""调试桥测试：用假 DebugSession 覆盖桥层契约（真适配器由 Task 4 覆盖）。

裸桥构造范式同 tests/test_gui_terminal.py:32-68。

对照计划的三处事实性修正（最小探针见报告）：
1) 计划把 `probe_adapter` / `DebugSession` / `adapter_availability` 打在 `gui_bridge.*`
   上；实现按仓内既有口径把 core.ide 的导入放在方法内（同 `ide_run_file`：依赖链
   不该拖慢 GUI 启动），导入发生在调用时，因此正确的补丁目标是来源模块。
2) 计划写的 `core.ide.language_registry.detect_language_id` 在仓内不存在，
   实际函数是 `language_id_for_path`。
3) `ide_debug_evaluate` 平铺返回 result/type/variablesReference（计划断言即平铺）。

另补 4 条路径策略用例：S3-T4 评审要求 `program` / `cwd` 与 `path` 同口径
（先 `_resolve_real_path` 再 `gui_path_access_allowed`）。
"""

import inspect
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

import gui_bridge
from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge
from core.ide import dap_manager
from core.ide.debug_adapters import STDIO, AdapterProbe, AdapterSpec


class _FakeSession:
    def __init__(self, spec, program, cwd, **kwargs):
        self.session_id = "dap-fake"
        self.spec = spec
        self.program = program
        self.cwd = cwd
        self.emit = kwargs.get("emit")
        self.breakpoints = []
        self.commands = []
        self.configuration_done_called = False
        self.stopped = False
        self._alive = False

    def start(self):
        self._alive = True
        return {"session_id": self.session_id, "adapter": self.spec.adapter_id, "pid": 99, "capabilities": {}}

    def set_breakpoints(self, path, lines):
        self.breakpoints.append((path, list(lines)))
        return [{"verified": True, "line": line} for line in lines]

    def configuration_done(self):
        self.configuration_done_called = True

    def is_alive(self):
        return self._alive

    def threads(self):
        return [{"id": 1, "name": "MainThread"}]

    def stack_trace(self, thread_id, levels=20):
        return [{"id": 1001, "name": "main", "path": r"C:\p\app.py", "line": 7, "column": 1}]

    def scopes(self, frame_id):
        return [{"name": "Locals", "variablesReference": 2001, "expensive": False}]

    def variables(self, reference):
        return [{"name": "x", "value": "42", "type": "int", "variablesReference": 0}]

    def evaluate(self, expression, frame_id=0, context="repl"):
        return {"result": f"eval:{expression}", "type": "str", "variablesReference": 0}

    def continue_(self, thread_id):
        self.commands.append(("continue", thread_id))
        return {}

    def pause(self, thread_id):
        self.commands.append(("pause", thread_id))
        return {}

    def next_(self, thread_id):
        self.commands.append(("next", thread_id))
        return {}

    def step_in(self, thread_id):
        self.commands.append(("stepIn", thread_id))
        return {}

    def step_out(self, thread_id):
        self.commands.append(("stepOut", thread_id))
        return {}

    def restart(self):
        self.commands.append(("restart", 0))
        return {}

    def stop(self):
        self.stopped = True
        self._alive = False


def _make_bridge(workdir):
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = False
    instance._window = object()
    instance._js_queue = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
    instance._js_dispatcher = threading.current_thread()
    instance._js_dispatcher_lock = threading.Lock()
    instance._js_dropped = 0
    instance._delta_lock = threading.Lock()
    instance._delta_buffers = {}
    instance._delta_kinds = {}
    instance._delta_last_flush = 0.0
    instance._pty_session = None
    instance._pty_lock = threading.Lock()
    instance._debug_session = None
    instance._debug_lock = threading.Lock()
    instance._debug_last_thread_id = 0
    instance._debug_last_frame_id = 0
    instance._debug_reapers = []
    instance._debug_debuggee_exited = False
    # 当前会话 id（与 `_debug_session` 成对维护）：`_emit_dap_event` 靠它丢弃残留事件
    instance._debug_session_id = ""
    instance._current_session = "s1"
    instance._session_workdir = lambda name: workdir
    return instance


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


@pytest.fixture()
def source_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    return str(target)


def _patch_probe(monkeypatch, ok=True):
    spec = AdapterSpec("debugpy", "python", ["python", "-m", "debugpy.adapter"], STDIO)
    probe = AdapterProbe(ok, "debugpy", spec if ok else None, "" if ok else "未安装 debugpy（Python 调试适配器）")
    monkeypatch.setattr("core.ide.debug_adapters.probe_adapter", lambda language_id: probe)
    return spec


def _patch_session(monkeypatch):
    created = []
    monkeypatch.setattr(
        "core.ide.debug_session.DebugSession",
        lambda spec, **kwargs: created.append(_FakeSession(spec, **kwargs)) or created[-1],
    )
    return created


def _patch_session_with_distinct_ids(monkeypatch):
    """同 `_patch_session`，但每次构造给一个不同的 session_id（检验跨会话的事件归属）。"""
    created = []

    def factory(spec, **kwargs):
        session = _FakeSession(spec, **kwargs)
        session.session_id = f"dap-{len(created) + 1}"
        created.append(session)
        return session

    monkeypatch.setattr("core.ide.debug_session.DebugSession", factory)
    return created


def test_start_returns_hint_when_adapter_missing(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch, ok=False)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "debugpy" in res["msg"]


def test_start_creates_session_and_sends_breakpoints(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, breakpoints=[{"path": source_file, "lines": [7, 11]}])

    assert res["ok"] is True and res["session_id"] == "dap-fake" and res["adapter"] == "debugpy"
    session = created[0]
    assert session.breakpoints == [(source_file, [7, 11])]
    assert session.configuration_done_called is True
    assert bridge._debug_session is session


def test_second_start_rejected_while_session_alive(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "已有调试会话" in res["msg"]


def test_start_rejects_file_outside_workspace_when_sandbox_off(workspace, monkeypatch):
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    _patch_probe(monkeypatch)
    outside = os.path.join(os.path.dirname(workspace), "outside_app.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(outside)

    assert res["ok"] is False and "工作区" in res["msg"]
    os.remove(outside)


def test_start_passes_program_and_cwd_through_path_policy(workspace, source_file, monkeypatch):
    """program / cwd 必须与 path 同口径：先 realpath，再 gui_path_access_allowed。"""
    from core import gui_path_policy

    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    program = os.path.join(workspace, "runner.py")
    with open(program, "w", encoding="utf-8") as handle:
        handle.write("print('runner')\n")
    seen = []
    real_policy = gui_path_policy.gui_path_access_allowed

    def spy(path, ws, *, action="preview", session_id=None):
        seen.append((path, ws))
        return real_policy(path, ws, action=action, session_id=session_id)

    monkeypatch.setattr(gui_path_policy, "gui_path_access_allowed", spy)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, program=program)

    assert res["ok"] is True
    checked = [path for path, _ in seen]
    assert os.path.realpath(source_file) in checked
    assert os.path.realpath(program) in checked
    # cwd（目标文件所在目录）同样过策略
    assert os.path.realpath(workspace) in checked
    # 交给策略的一律是 realpath 归一化后的绝对路径（顺序不能反）
    assert all(os.path.isabs(path) for path in checked)
    assert created[0].program == os.path.realpath(program)
    assert created[0].cwd == os.path.dirname(os.path.realpath(source_file))


def test_start_rejects_program_outside_workspace_when_sandbox_off(workspace, source_file, monkeypatch):
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    outside = os.path.join(os.path.dirname(workspace), "outside_program.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, program=outside)

    assert res["ok"] is False and "工作区" in res["msg"]
    assert created == []  # 越权时不得起会话（不留下适配器进程）
    os.remove(outside)


def test_start_rejects_credential_program(workspace, source_file, tmp_path, monkeypatch):
    """program 走的是完整策略（凭据文件连沙盒开关都不受）。"""
    from core import gui_path_policy

    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    monkeypatch.setattr(gui_path_policy, "_ycode_data_dir", lambda: str(tmp_path / ".ycode"))
    credential = tmp_path / ".ycode" / "config.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}\n", encoding="utf-8")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, program=str(credential))

    assert res["ok"] is False and "配置文件" in res["msg"]
    assert created == []


def test_start_rejects_debug_cwd_when_policy_denies(workspace, source_file, monkeypatch):
    """cwd 也在策略里：只放行 cwd 之外的路径时仍必须拒绝启动。"""
    from core import gui_path_policy

    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    cwd = os.path.dirname(os.path.realpath(source_file))

    def deny_cwd(path, ws, *, action="preview", session_id=None):
        if os.path.normcase(path) == os.path.normcase(cwd):
            return False, "调试工作目录不在当前工作区内"
        return True, ""

    monkeypatch.setattr(gui_path_policy, "gui_path_access_allowed", deny_cwd)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "工作区" in res["msg"]
    assert created == []


def test_start_rejects_breakpoint_outside_workspace_when_sandbox_off(workspace, source_file, monkeypatch):
    """断点路径与 program / cwd 同口径：工作区外的断点在起会话前就被拒（不留适配器进程）。"""
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    outside = os.path.join(os.path.dirname(workspace), "outside_bp.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, breakpoints=[{"path": outside, "lines": [3]}])

    assert res["ok"] is False
    # 措辞与 program / cwd 越权一致：标签 + 策略原因，用户能直接看出该去改什么
    assert "断点文件不可访问" in res["msg"] and "工作区" in res["msg"]
    assert created == []
    os.remove(outside)


def test_start_passes_breakpoints_through_path_policy(workspace, source_file, monkeypatch):
    """断点路径必须过策略；path 缺省时回落成已校验的目标文件，不误判成越权。"""
    from core import gui_path_policy

    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    seen = []
    real_policy = gui_path_policy.gui_path_access_allowed

    def spy(path, ws, *, action="preview", session_id=None):
        seen.append(path)
        return real_policy(path, ws, action=action, session_id=session_id)

    monkeypatch.setattr(gui_path_policy, "gui_path_access_allowed", spy)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(
        source_file,
        breakpoints=[{"path": source_file, "lines": [7]}, {"lines": [11]}],
    )

    assert res["ok"] is True
    assert os.path.realpath(source_file) in seen  # 断点路径确实进了策略
    # 下发前统一 realpath 归一化；两条（带 path / 只有行号）都落在目标文件上
    assert created[0].breakpoints == [
        (os.path.realpath(source_file), [7]),
        (os.path.realpath(source_file), [11]),
    ]


def test_restart_allowed_after_program_terminated(workspace, source_file, monkeypatch):
    """程序自然结束（terminated）后必须能重新开始调试，不需要先手动点一次停止。

    S3-T7 评审：`ide_debug_start` 原来只判 `_debug_session is not None`，而 terminated
    事件到达时不清会话引用；前端 `reduceDapEvent('dap_terminated')` 只把 debugState
    置 stopped、不调 ide_debug_stop，于是「再点开始调试」永远拿到「已有调试会话在运行」。
    """
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    # 被调试程序跑完：DebugSession 从适配器读线程推 terminated
    created[0].emit("dap_terminated", {"session_id": "dap-fake"})

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is True
    assert len(created) == 2


def test_restart_reaps_dead_session_without_terminated_event(workspace, source_file, monkeypatch):
    """适配器进程意外退出（连 terminated 都没送到）：同样要放行并回收旧会话。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    created[0]._alive = False

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is True
    assert created[0].stopped is True  # 旧会话被回收（不留适配器进程）
    assert len(created) == 2


def test_stop_resets_thread_and_frame_memory(workspace, source_file, monkeypatch):
    """会话回收时必须复位线程/帧记忆，否则上一会话的失效 id 会泄漏给下一次调试。"""
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    bridge._emit_dap_event("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "frame_id": 1001})
    assert bridge._debug_last_thread_id == 1 and bridge._debug_last_frame_id == 1001

    bridge.ide_debug_stop()

    assert bridge._debug_last_thread_id == 0 and bridge._debug_last_frame_id == 0


def test_new_session_does_not_inherit_stale_thread_id(workspace, source_file, monkeypatch):
    """新会话在第一条 dap_stopped 之前不得沿用上一会话的线程号（会被适配器拒成 thread not found）。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    bridge._emit_dap_event("dap_stopped", {"session_id": "dap-fake", "thread_id": 7, "frame_id": 7001})

    bridge.ide_debug_stop()
    bridge.ide_debug_start(source_file)  # 新会话

    assert bridge._debug_last_thread_id == 0 and bridge._debug_last_frame_id == 0
    assert bridge.ide_debug_command("continue")["ok"] is True
    assert created[-1].commands == [("continue", 0)]


def test_command_requires_running_session(workspace):
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_command("continue")

    assert res["ok"] is False and "未启动" in res["msg"]


def test_unknown_command_rejected(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_command("jump")

    assert res["ok"] is False and "jump" in res["msg"]


def test_step_commands_call_session_methods(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    bridge._debug_session.emit("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "frame_id": 1001})

    assert bridge.ide_debug_command("continue")["ok"] is True
    assert bridge.ide_debug_command("next")["ok"] is True
    assert bridge.ide_debug_command("stepIn")["ok"] is True
    assert bridge.ide_debug_command("stepOut")["ok"] is True
    assert bridge.ide_debug_command("pause")["ok"] is True
    assert created[0].commands == [("continue", 1), ("next", 1), ("stepIn", 1), ("stepOut", 1), ("pause", 1)]


def test_inspect_methods_return_session_payloads(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    assert bridge.ide_debug_threads()["threads"][0]["id"] == 1
    frames = bridge.ide_debug_stack_trace(1, 5)["frames"]
    assert frames[0]["line"] == 7 and frames[0]["path"] == r"C:\p\app.py"
    assert bridge.ide_debug_scopes(1001)["scopes"][0]["variablesReference"] == 2001
    assert bridge.ide_debug_variables(2001)["variables"][0]["value"] == "42"
    assert bridge.ide_debug_evaluate("1+1", 1001)["result"] == "eval:1+1"


def test_inspect_methods_report_session_missing(workspace):
    """无会话时查询类方法给中文原因而不是抛异常（前端据此提示先启动调试）。"""
    bridge = _make_bridge(workspace)

    for payload in (
        bridge.ide_debug_threads(),
        bridge.ide_debug_stack_trace(1),
        bridge.ide_debug_scopes(1),
        bridge.ide_debug_variables(1),
        bridge.ide_debug_evaluate("1+1"),
    ):
        assert payload["ok"] is False and "未启动" in payload["msg"]


def test_set_breakpoints_pending_without_session(workspace, source_file):
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_set_breakpoints(source_file, [3])

    assert res["ok"] is True and res["pending"] is True


def test_set_breakpoints_forwards_to_session(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_set_breakpoints(source_file, [3, 9])

    assert res["ok"] is True and res["pending"] is False
    # 桥层下发前先 realpath（与启动路径同口径）；realpath 归一化比较避免短名误报
    forwarded = created[0].breakpoints[-1]
    assert os.path.realpath(forwarded[0]) == os.path.realpath(source_file)
    assert forwarded[1] == [3, 9]


def test_set_breakpoints_rejects_path_outside_workspace(workspace, monkeypatch):
    """工作区外的断点路径必须被拒，并给出与 program / cwd 同口径的中文原因。"""
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    outside = os.path.join(os.path.dirname(workspace), "outside_bp.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_set_breakpoints(outside, [3])

    assert res["ok"] is False
    assert "断点文件不可访问" in res["msg"] and "工作区" in res["msg"]
    assert res.get("pending") is None  # 越权时不得退化成"先挂前端"的 pending
    os.remove(outside)


def test_set_breakpoints_rejects_outside_path_with_running_session(workspace, source_file, monkeypatch):
    """会话在跑时同样拦截：越权路径不得下发给适配器。"""
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    outside = os.path.join(os.path.dirname(workspace), "outside_bp.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_set_breakpoints(outside, [3])

    assert res["ok"] is False and "工作区" in res["msg"]
    assert created[0].breakpoints == []
    os.remove(outside)


def test_set_breakpoints_allows_path_inside_workspace(workspace, source_file, monkeypatch):
    """防"顺手封死"：工作区内的断点路径照旧放行（无会话时仍返回 pending）。"""
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_set_breakpoints(source_file, [3])

    assert res["ok"] is True and res["pending"] is True


def test_set_breakpoints_rejects_credential_file_even_with_sandbox_on(workspace, tmp_path, monkeypatch):
    """断点走的是完整策略：凭据文件连沙盒开关都不受（与 program 同口径）。"""
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_ycode_data_dir", lambda: str(tmp_path / ".ycode"))
    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: True)
    credential = tmp_path / ".ycode" / "config.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}\n", encoding="utf-8")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_set_breakpoints(str(credential), [1])

    assert res["ok"] is False and "配置文件" in res["msg"]


def test_debug_events_reach_js_queue(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    # 会话必须先起来：`_emit_dap_event` 现在按事件携带的 session_id 与当前会话比对，
    # 不属于当前会话的事件一律丢弃（S3-T10 评审），裸发会被当成残留事件
    bridge.ide_debug_start(source_file)
    bridge.drain_js_queue()

    bridge._emit_dap_event("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "line": 7})
    payload = bridge.drain_js_queue()

    assert payload["type"] == "dap_stopped"
    assert payload["data"]["line"] == 7


def test_stale_session_events_are_dropped(workspace, source_file, monkeypatch):
    """S3-T10 评审：旧会话迟到的事件不得被当成当前会话的事件处理。

    `ide_debug_stop` 只丢引用 + 立即回报，适配器进程最坏 ≈10s 后才死；这段时间里旧会话
    仍会推 `dap_continued` / `dap_exited` / `dap_terminated`。若放行它们，用户几秒内重启
    调试时，旧会话的 `dap_exited` 会把 `_debug_debuggee_exited` 写脏，`ide_debug_start`
    随即把**正在跑的新会话**当「已结束」回收掉。
    """
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    bridge.drain_js_queue()
    bridge.ide_debug_stop()
    bridge.drain_js_queue()

    # 停止后桥层的会话 id 必须与会话引用一起放掉，否则残留事件的校验等于没有
    assert bridge._debug_session_id == ""

    for event_type, payload in (
        ("dap_continued", {"session_id": "dap-fake", "thread_id": 1}),
        ("dap_exited", {"session_id": "dap-fake", "exit_code": 0}),
        ("dap_terminated", {"session_id": "dap-fake"}),
        ("dap_output", {"session_id": "dap-fake", "category": "stderr", "text": "残留\n"}),
    ):
        bridge._emit_dap_event(event_type, payload)

    assert bridge.drain_js_queue() is None
    # 旧会话的 exited 不得写脏「被调试程序已退出」标记
    assert bridge._debug_debuggee_exited is False
    # 无当前会话时 dap_status 仍放行（停止/失败提示就是靠它送到前端的）
    bridge._emit_dap_event("dap_status", {"session_id": "", "state": "stopped", "msg": ""})
    assert bridge.drain_js_queue()["type"] == "dap_status"


def test_stale_exited_does_not_reclaim_running_new_session(workspace, source_file, monkeypatch):
    """更糟的那条后果：新会话正在跑时，旧会话的 dap_exited 不能让重启判重把它回收掉。"""
    _patch_probe(monkeypatch)
    created = _patch_session_with_distinct_ids(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    first = created[0]
    bridge.ide_debug_stop()
    assert bridge._debug_session is None

    bridge.ide_debug_start(source_file)
    bridge.drain_js_queue()
    running = bridge._debug_session
    assert running is created[1]
    assert running.session_id != first.session_id

    # 旧适配器临死前推 exited：必须被会话校验丢掉
    bridge._emit_dap_event("dap_exited", {"session_id": first.session_id, "exit_code": 0})

    assert bridge._debug_debuggee_exited is False
    # 新会话照旧是「在跑」：再来一次 start 会被那句「已有调试会话在运行」挡掉，
    # 而不是被当作「已结束」回收
    res = bridge.ide_debug_start(source_file)
    assert res["ok"] is False and "已有调试会话" in res["msg"]
    assert bridge._debug_session is running


def test_stop_clears_session_and_terminates(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_stop()

    assert res["ok"] is True
    # 会话引用当场放掉（同步），回收本身是异步投递的：有界等待回收线程跑完
    # （`ide_debug_stop` 绝不阻塞唯一派发线程，见 test_stop_returns_without_waiting_for_reap）
    assert bridge._debug_session is None
    deadline = time.monotonic() + 5.0
    while not created[0].stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert created[0].stopped is True
    assert bridge.ide_debug_stop()["ok"] is True  # 幂等


def test_shutdown_debug_session_helper_reaps(workspace, source_file, monkeypatch):
    """shutdown 里的清理片段单独可测（同 tests/test_gui_terminal.py 的 shutdown 片段用例）。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    bridge._shutdown_debug_session()

    assert created[0].stopped is True and bridge._debug_session is None


def test_pause_accepts_explicit_thread_id(workspace, source_file, monkeypatch):
    """S3-T9 评审：暂停的典型时机是「程序正在运行」，此时还没有 dap_stopped 记录，
    桥层记忆是 0，发 threadId:0 的 pause 会被适配器拒成 thread not found。前端先取线程表
    拿到真实线程号再传进来，桥层必须优先用它（传 0/不传时仍走既有回退逻辑）。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    assert bridge.ide_debug_command("pause", 42)["ok"] is True
    # 显式线程号优先于（这里为 0 的）桥层记忆
    assert created[0].commands == [("pause", 42)]
    # 不传时仍走回退逻辑：没有 dap_stopped → 0
    assert bridge.ide_debug_command("pause")["ok"] is True
    assert created[0].commands[-1] == ("pause", 0)


def test_restart_allowed_after_debuggee_exited_without_terminated(workspace, source_file, monkeypatch):
    """适配器只发 exited（不发 terminated）且进程仍活：重新开始调试不能撞上「已有会话」判重。

    S3-T9 评审：前端 `reduceDapEvent('dap_exited')` 会清空 sessionId、界面放行「再次开始
    调试」，而后端只有 terminated 才清会话 → 两边打架。这里按「exited 只表示被调试程序
    退出、会话未必结束」的 DAP 语义记事实，只在**用户重新开始调试**时回收旧会话。
    """
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    # 被调试程序退出，但适配器进程还活着，且没有 terminated
    created[0].emit("dap_exited", {"session_id": "dap-fake", "exit_code": 0})
    assert created[0].is_alive() is True

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is True
    assert created[0].stopped is True  # 旧会话被回收（否则适配器变孤儿）
    assert len(created) == 2


def test_start_still_rejected_while_debuggee_running_after_exited_then_continued(
    workspace, source_file, monkeypatch
):
    """exited 只是「被调试程序结束」的标记，不能变成绕过判重的后门。

    restart 之后被调试程序又跑起来了（dap_continued / dap_stopped），此时再开新调试
    必须照旧被拒——否则会把运行中的会话连人带进程收掉。
    """
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    created[0].emit("dap_exited", {"session_id": "dap-fake", "exit_code": 0})
    created[0].emit("dap_continued", {"session_id": "dap-fake", "thread_id": 1})

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "已有调试会话" in res["msg"]
    assert created[0].stopped is False
    assert len(created) == 1


def test_availability_lists_adapters(workspace, monkeypatch):
    monkeypatch.setattr(
        "core.ide.debug_adapters.adapter_availability",
        lambda: [{"language_id": "python", "adapter_id": "debugpy", "available": True, "hint": ""}],
    )
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_availability()

    assert res["ok"] is True and res["adapters"][0]["adapter_id"] == "debugpy"


# ---------------------------------------------------------------------------
# S3-T8 评审：dap_terminated 后适配器进程可能变孤儿 —— 必须真的回收
# ---------------------------------------------------------------------------


def _sleeping_child():
    """一个真实活着的子进程（充当「不自行退出的适配器」，如 dlv / lldb-dap）。"""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class _ReapingSession:
    """持有**真实子进程**的会话罐头：stop() 真的回收它。

    回收断言一律走 pid + `Popen.poll()` 存活探测，不看对象字段。若 stop() 在读线程内被
    同步调用，就模拟 `DapProcess._reject_reader_blocking` 的 `DapReaderBlockingError`
    护栏抛错——旧实现（在 `_discard_debug_session` 里同步等）会因此收不掉进程。
    """

    def __init__(self, process, reader_thread=None):
        self._process = process
        self._reader_thread = reader_thread
        self.stopped = False
        self.stop_thread = ""

    def is_alive(self):
        return self._process.poll() is None

    def stop(self):
        if self._reader_thread is not None and threading.current_thread() is self._reader_thread:
            raise RuntimeError("DapReaderBlockingError: 读线程内禁止同步等响应")
        self.stopped = True
        self.stop_thread = threading.current_thread().name
        self._process.kill()
        self._process.wait(timeout=5)


def _wait_dead(process, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return True
        time.sleep(0.05)
    return process.poll() is not None


def test_terminated_reaps_adapter_process_off_the_reader_thread(workspace):
    """收到 dap_terminated → 适配器进程必须真的被回收，且回收发生在读线程之外。

    旧实现只把 `_debug_session` 置 None、指望 `__del__` 兜底：存在引用环
    （DapProcess.on_event 是 DebugSession 的绑定方法）时只能靠分代 GC 非确定回收，
    且读线程活着时环根本收不了 → 进程与线程一路泄漏（S3-T8 评审）。
    """
    child = _sleeping_child()
    assert child.poll() is None  # 起点：进程真实存活
    try:
        bridge = _make_bridge(workspace)
        holder = {}

        # 从名为 ide-dap-reader 的线程里推 terminated，忠实模拟「事件来自 DAP 读线程」
        def emit_on_reader_thread():
            bridge._emit_dap_event("dap_terminated", {"session_id": "dap-fake"})

        reader_thread = threading.Thread(
            target=emit_on_reader_thread, daemon=True, name="ide-dap-reader"
        )
        session = _ReapingSession(child, reader_thread=reader_thread)
        bridge._debug_session = session
        # 会话 id 与会话引用成对存在（`_emit_dap_event` 按它做归属校验，S3-T10 评审）：
        # 这里手工搭桥内状态，必须把 id 一起放上，否则 terminated 会被当成残留事件丢掉
        bridge._debug_session_id = "dap-fake"
        holder["session"] = session

        reader_thread.start()
        reader_thread.join(5)
        assert not reader_thread.is_alive()

        # 会话引用当场放掉（S3-T7 语义不变）
        assert bridge._debug_session is None
        # 临时回收线程真的把子进程收掉了（pid 存活探测）
        assert _wait_dead(child), "适配器子进程未被回收（成了孤儿进程）"
        assert session.stopped is True
        # 回收必须由临时线程执行，绝不能是读线程自己同步 stop()
        assert session.stop_thread == "ide-dap-reap"
        # 回收完成后登记表清空
        assert bridge._debug_reapers == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_shutdown_after_terminated_still_reaps(workspace):
    """terminated 之后关停路径仍要兜底：join 掉投递出去的回收线程，不留孤儿。

    旧实现里 terminated 已把 `_debug_session` 置 None，`_shutdown_debug_session`
    的 `if session is None: return` 直接短路 → 关停什么都没做，适配器子进程不随父
    进程退出被杀（S3-T8 评审理由③）。
    """
    child = _sleeping_child()
    assert child.poll() is None
    try:
        bridge = _make_bridge(workspace)
        session = _ReapingSession(child)
        bridge._debug_session = session
        # 同上一个用例：手工搭桥内状态时 id 要与会话引用成对放上
        bridge._debug_session_id = "dap-fake"

        bridge._emit_dap_event("dap_terminated", {"session_id": "dap-fake"})
        assert bridge._debug_session is None

        # 关停时当前会话已是 None：仍必须兜底收掉投递出去的回收
        bridge._shutdown_debug_session()

        assert child.poll() is not None, "关停后适配器子进程仍存活（孤儿进程）"
        assert session.stopped is True
        assert bridge._debug_session is None
        assert bridge._debug_reapers == []
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


# ---------------------------------------------------------------------------
# S3-T9 评审（第 0 步）：停止调试不得冻结唯一派发线程；关停兜底窗口要够；
# 回收线程起不来也不能丢会话；关停超时后补一刀强杀进程树
# ---------------------------------------------------------------------------


class _RecordingSession:
    """记录「回收参数 + 回收发生在哪条线程」的会话替身（不带真实进程）。"""

    def __init__(self):
        self.graceful = None
        self.reap_thread = ""

    def is_alive(self):
        return True

    def _reap(self, graceful):
        self.graceful = graceful
        self.reap_thread = threading.current_thread().name

    def stop(self):
        # 有 _reap 时必须优先走 _reap：graceful=False 不做同步 disconnect
        raise AssertionError("丢弃/关停路径不该调用 stop()（会发同步 disconnect 请求）")


class _BlockingReapSession:
    """回收会一直阻塞的会话替身：用来证明 `ide_debug_stop` 不同步等回收。"""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.graceful = None

    def is_alive(self):
        return True

    def _reap(self, graceful):
        self.graceful = graceful
        self.entered.set()
        self.release.wait(10.0)


def test_stop_returns_without_waiting_for_reap(workspace):
    """`ide_debug_stop` 跑在唯一的 `gui-js-dispatcher` 线程上，绝不同步等回收。

    适配器卡住不回 disconnect 时同步 `stop()` 最坏 ≈15s，会把唯一派发线程占住 =
    整个前端 UI 冻结，退出星瑶的链路也被拖慢（S3-T9 评审 Important）。
    """
    bridge = _make_bridge(workspace)
    session = _BlockingReapSession()
    bridge._debug_session = session

    started = time.monotonic()
    res = bridge.ide_debug_stop()
    elapsed = time.monotonic() - started

    try:
        assert res["ok"] is True
        # 同步部分只做「丢引用 + 投递」：必须远快于回收本身
        assert bridge._debug_session is None
        assert elapsed < 1.0, f"ide_debug_stop 同步等了回收（{elapsed:.2f}s）"
        # 非阻塞 ≠ 不回收：回收确实被投递到另一条线程，且用 graceful=False
        assert session.entered.wait(5.0) is True, "回收没有被投递出去"
        assert session.graceful is False
    finally:
        session.release.set()


def test_shutdown_reaps_without_graceful_disconnect(workspace):
    """关停路径的回收走 `_reap(graceful=False)`：不发同步 disconnect，最坏耗时砍 5s。"""
    bridge = _make_bridge(workspace)
    session = _RecordingSession()
    bridge._debug_session = session

    bridge._shutdown_debug_session()

    assert session.graceful is False
    assert session.reap_thread == threading.current_thread().name


def test_shutdown_join_window_covers_worst_case_reap(workspace):
    """join 窗口必须 ≥ 一次 `_reap(graceful=False)` 的最坏耗时，否则适配器卡住时父进程
    先退出、子进程成孤儿（S3-T9 评审 Important）。

    最坏构成：`terminate()` 等适配器优雅退出 + 强杀进程树后 `wait(2.0)` +
    `_join_threads()` 的 2.0 + 1.0。旧实现 join_timeout=5.0 既小于这个值，走的还是
    `stop()`（额外再等 `_DISCONNECT_TIMEOUT=5.0`）→ 最坏 ≈15s。
    """
    terminate_default = inspect.signature(dap_manager.DapProcess.terminate).parameters["timeout"].default
    worst_case = terminate_default + 2.0 + 2.0 + 1.0

    default = inspect.signature(ApiBridge._shutdown_debug_session).parameters["join_timeout"].default

    assert default == gui_bridge.DEBUG_REAP_JOIN_TIMEOUT
    assert default >= worst_case, f"join 窗口 {default}s 小于最坏回收耗时 {worst_case}s"


def test_reaper_start_failure_falls_back_to_inline_reap(workspace, monkeypatch):
    """`Thread.start()` 抛 RuntimeError（线程资源耗尽）时不许丢会话、也不许留一条永远
    不会启动的登记线程。

    旧实现：锁内先 `self._debug_session = None` 并 append 线程，锁外才 `start()`——
    start() 失败即会话引用已丢、无人回收，随后 `_shutdown_debug_session` 对未启动线程
    `join()` 抛 "cannot join thread before it is started"，异常经 `ide_debug_start`
    冒到前端，界面停在「运行中」而进程无人回收（S3-T9 评审 Important）。
    """
    bridge = _make_bridge(workspace)
    session = _RecordingSession()
    bridge._debug_session = session

    real_thread = threading.Thread

    class _BoomThread(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(gui_bridge.threading, "Thread", _BoomThread)

    bridge._discard_debug_session()  # 不许抛

    assert bridge._debug_session is None
    assert bridge._debug_reapers == []  # 未启动的登记被摘掉
    # 退化为就地回收：graceful=False 不发同步请求，因此在派发/读线程上调用也安全
    assert session.graceful is False
    assert session.reap_thread == threading.current_thread().name
    # 关停链路不会再炸（旧实现在这里抛 cannot join thread before it is started）
    bridge._shutdown_debug_session()


def test_shutdown_force_kills_after_join_timeout(workspace):
    """join 到点仍未收完 → 补一刀强杀进程树，保证退出后不留调试器进程（S3-3）。"""
    child = _sleeping_child()
    assert child.poll() is None
    try:
        bridge = _make_bridge(workspace)

        class _ProcessHolder:
            _proc = child

        session = _RecordingSession()
        session._process = _ProcessHolder()  # `_force_kill_session_tree` 的取值口径
        bridge._debug_session = session

        bridge._shutdown_debug_session(join_timeout=0.0)

        assert _wait_dead(child), "关停超时后没有补强杀，适配器子进程成了孤儿"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
