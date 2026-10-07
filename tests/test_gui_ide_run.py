"""一键运行桥测试：命令必须写进常驻终端会话，输出继续走既有 pty_data 事件。

裸桥构造范式与 tests/test_gui_terminal.py:32-68 的 _make_bridge 一致：
_js_dispatcher 指向主线程，阻止真实派发线程抢空队列。
"""

import json
import os
import queue
import threading

import pytest

from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge


class _FakePtySession:
    def __init__(self, pid=4242, cwd=r"C:\fake_proj", alive=True, write_ok=True):
        self.pid = pid
        self.cwd = cwd
        self._alive = alive
        self.write_ok = write_ok
        self.writes = []
        self.killed = False

    def is_alive(self):
        return self._alive and not self.killed

    def write(self, data):
        self.writes.append(data)
        return self.write_ok

    def resize(self, cols, rows):
        return True

    def kill(self):
        self.killed = True


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
    instance._current_session = "s1"
    instance._session_workdir = lambda name: workdir
    return instance


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


def _write_python(workspace, name="app.py"):
    target = os.path.join(workspace, name)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    return target


def test_run_file_writes_command_into_terminal(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    created = []

    def fake_create(cwd, cols, rows, on_output, on_exit):
        created.append(cwd)
        return _FakePtySession(cwd=cwd)

    monkeypatch.setattr("gui_bridge.create_pty_terminal", fake_create)
    target = _write_python(workspace)

    res = bridge.ide_run_file(target)

    assert res["ok"] is True and res["source"] == "language"
    assert bridge._pty_session.writes == [res["command"] + "\r"]
    # 终端在会话工作区里拉起（realpath 归一化比较，避免短名/大小写差异误报）
    assert len(created) == 1
    assert os.path.realpath(created[0]) == os.path.realpath(workspace)


def test_run_file_reuses_existing_session(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    session = _FakePtySession(pid=7)
    bridge._pty_session = session
    monkeypatch.setattr(
        "gui_bridge.create_pty_terminal",
        lambda **kw: (_ for _ in ()).throw(AssertionError("不应重建终端")),
    )
    target = _write_python(workspace)

    res = bridge.ide_run_file(target)

    assert res["ok"] is True and res["pid"] == 7
    assert len(session.writes) == 1


def test_unknown_language_returns_message_without_touching_terminal(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    session = _FakePtySession()
    bridge._pty_session = session
    target = os.path.join(workspace, "data.xyz")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("x\n")

    res = bridge.ide_run_file(target)

    assert res["ok"] is False and ".xyz" in res["msg"]
    assert session.writes == []


def test_missing_file_rejected(workspace):
    bridge = _make_bridge(workspace)

    res = bridge.ide_run_file(os.path.join(workspace, "nope.py"))

    assert res["ok"] is False and "不存在" in res["msg"]


def test_credential_config_path_rejected(workspace, tmp_path, monkeypatch):
    from core import gui_path_policy

    bridge = _make_bridge(workspace)
    monkeypatch.setattr(gui_path_policy, "_ycode_data_dir", lambda: str(tmp_path / ".ycode"))
    credential = tmp_path / ".ycode" / "config.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}\n", encoding="utf-8")

    res = bridge.ide_run_file(str(credential))

    assert res["ok"] is False and "配置文件" in res["msg"]


def test_path_outside_workspace_rejected_when_sandbox_off(workspace, tmp_path, monkeypatch):
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    bridge = _make_bridge(workspace)
    outside = tmp_path.parent / "outside_app.py"
    outside.write_text("print('hi')\n", encoding="utf-8")

    res = bridge.ide_run_file(str(outside))

    assert res["ok"] is False and "工作区" in res["msg"]
    outside.unlink()


def test_terminal_spawn_failure_propagates_message(workspace, monkeypatch):
    from core.gui_terminal import PtyTerminalError

    def boom(cwd, cols, rows, on_output, on_exit):
        raise PtyTerminalError("未找到 PowerShell")

    monkeypatch.setattr("gui_bridge.create_pty_terminal", boom)
    bridge = _make_bridge(workspace)
    target = _write_python(workspace)

    res = bridge.ide_run_file(target)

    assert res["ok"] is False and "PowerShell" in res["msg"]


def test_terminal_dead_session_reports_unavailable(workspace, monkeypatch):
    """会话已死且重建失败：要告诉用户终端不可用，而不是静默"已运行"。"""
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession(alive=False)

    def boom(**kw):
        raise RuntimeError("ConPTY 初始化失败")

    monkeypatch.setattr("gui_bridge.create_pty_terminal", boom)
    target = _write_python(workspace)

    res = bridge.ide_run_file(target)

    assert res["ok"] is False and "ConPTY" in res["msg"]


def test_write_failure_reports_terminal_state(workspace):
    """写不进去（会话刚退出）必须如实报错，不能假装命令已送达。"""
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession(write_ok=False)

    res = bridge.ide_run_file(_write_python(workspace))

    assert res["ok"] is False and "终端" in res["msg"]


def test_explicit_command_bypasses_language_guess(workspace):
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()
    target = _write_python(workspace)

    res = bridge.ide_run_file(target, command="echo 自定义")

    assert res["ok"] is True and res["source"] == "explicit"
    assert bridge._pty_session.writes == ["echo 自定义\r"]


def test_extra_args_reach_the_terminal(workspace):
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()

    res = bridge.ide_run_file(_write_python(workspace), extra_args="--name 星瑶")

    assert res["ok"] is True
    assert res["command"].endswith("--name 星瑶")
    assert bridge._pty_session.writes == [res["command"] + "\r"]


def test_command_uses_absolute_paths(workspace, monkeypatch):
    """build_run_plan 按进程 cwd 解析相对路径：桥层必须先解析成绝对路径再传。"""
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()
    _write_python(workspace)
    monkeypatch.chdir(workspace)

    res = bridge.ide_run_file("app.py")

    assert res["ok"] is True
    assert os.path.abspath(workspace) in res["command"]


def test_launch_json_variable_command_reaches_terminal(workspace):
    """launch.json（含 ${file} 变量）解析出的命令同样必须真写进终端，且带 `& ` 调用符。"""
    vscode = os.path.join(workspace, ".vscode")
    os.makedirs(vscode, exist_ok=True)
    with open(os.path.join(vscode, "launch.json"), "w", encoding="utf-8") as handle:
        json.dump({"configurations": [{"name": "调试", "program": "${file}"}]}, handle)
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()
    target = _write_python(workspace)

    res = bridge.ide_run_file(target)

    assert res["ok"] is True and res["source"] == "launch.json"
    assert res["command"].startswith("& ")
    assert res["command"] == f"& '{target}'"
    assert bridge._pty_session.writes == [res["command"] + "\r"]


def test_fallback_note_is_surfaced_to_caller(workspace):
    """RunPlan.note（回退说明）必须回传给调用方，不能是死字段。"""
    vscode = os.path.join(workspace, ".vscode")
    os.makedirs(vscode, exist_ok=True)
    with open(os.path.join(vscode, "launch.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {"configurations": [{"name": "别的", "program": os.path.join(workspace, "other.py")}]},
            handle,
        )
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()

    res = bridge.ide_run_file(_write_python(workspace))

    assert res["ok"] is True and res["source"] == "language"
    assert "launch.json" in res["note"]
    assert res["msg"] == res["note"], "回退说明要放进 msg，前端渲染 msg 就能看到"
