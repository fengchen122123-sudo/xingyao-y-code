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

import os
import queue
import threading

import pytest

from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge
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


def test_debug_events_reach_js_queue(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)

    bridge._emit_dap_event("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "line": 7})
    payload = bridge.drain_js_queue()

    assert payload["type"] == "dap_stopped"
    assert payload["data"]["line"] == 7


def test_stop_clears_session_and_terminates(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_stop()

    assert res["ok"] is True and created[0].stopped is True
    assert bridge._debug_session is None
    assert bridge.ide_debug_stop()["ok"] is True  # 幂等


def test_shutdown_debug_session_helper_reaps(workspace, source_file, monkeypatch):
    """shutdown 里的清理片段单独可测（同 tests/test_gui_terminal.py 的 shutdown 片段用例）。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    bridge._shutdown_debug_session()

    assert created[0].stopped is True and bridge._debug_session is None


def test_availability_lists_adapters(workspace, monkeypatch):
    monkeypatch.setattr(
        "core.ide.debug_adapters.adapter_availability",
        lambda: [{"language_id": "python", "adapter_id": "debugpy", "available": True, "hint": ""}],
    )
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_availability()

    assert res["ok"] is True and res["adapters"][0]["adapter_id"] == "debugpy"
