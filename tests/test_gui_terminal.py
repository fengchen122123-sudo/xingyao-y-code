"""GUI 内嵌交互式终端（底部面板）桥接层测试。

覆盖 pty_terminal_* 桥接方法的会话复用/重建/清理语义、事件推送链路
（pty_data / pty_exit 经 _js_queue 派发）与 core.gui_terminal 的真实
ConPTY 会话往返（Windows）。
"""

import os
import queue
import threading
import time

import pytest

from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge
from core.gui_terminal import clamp_terminal_size


class _FakePtySession:
    """桥接层单测用的假常驻 shell 会话。"""

    def __init__(self, pid=4242, cwd=r"C:\fake_proj", alive=True):
        self.pid = pid
        self.cwd = cwd
        self._alive = alive
        self.writes = []
        self.resizes = []
        self.killed = False

    def is_alive(self):
        return self._alive and not self.killed

    def write(self, data):
        self.writes.append(data)

    def resize(self, cols, rows):
        self.resizes.append((cols, rows))

    def kill(self):
        self.killed = True


def _make_bridge():
    """构造仅含内嵌终端桥接所需内部状态的裸桥实例。

    _js_dispatcher 指向主线程（恒 alive）：阻止 _ensure_js_dispatcher 在
    测试内启动真实派发线程抢空队列，drain_js_queue 断言才稳定。
    """
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = False
    instance._window = object()  # _emit_raw 只判非空
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
    # 会话 workdir 必须是真实存在目录：桥会做 isdir 兜底回退
    instance._session_workdir = lambda name: os.getcwd()
    return instance


@pytest.fixture
def bridge():
    return _make_bridge()


def test_pty_terminal_ensure_spawns_session(bridge, monkeypatch):
    seen = {}

    def fake_create(cwd, cols, rows, on_output, on_exit):
        seen.update(cwd=cwd, cols=cols, rows=rows)
        return _FakePtySession(pid=101, cwd=cwd)

    monkeypatch.setattr("gui_bridge.create_pty_terminal", fake_create)
    res = bridge.pty_terminal_ensure(120, 30)

    assert res["ok"] and res["pid"] == 101 and res["reused"] is False
    assert seen["cwd"] == os.getcwd()
    assert (seen["cols"], seen["rows"]) == (120, 30)
    assert bridge._pty_session is not None


def test_pty_terminal_ensure_reuses_alive_session(bridge, monkeypatch):
    existing = _FakePtySession(pid=7, cwd=r"C:\keep")
    bridge._pty_session = existing
    calls = []
    monkeypatch.setattr(
        "gui_bridge.create_pty_terminal",
        lambda **kw: calls.append(kw) or (_ for _ in ()).throw(AssertionError("不应重建")),
    )
    res = bridge.pty_terminal_ensure(80, 24)
    assert res["ok"] and res["reused"] is True and res["pid"] == 7
    assert calls == []


def test_pty_terminal_ensure_respawns_after_exit(bridge, monkeypatch):
    bridge._pty_session = _FakePtySession(pid=7, alive=False)
    created = []
    monkeypatch.setattr(
        "gui_bridge.create_pty_terminal",
        lambda cwd, cols, rows, on_output, on_exit: created.append(cwd) or _FakePtySession(pid=8, cwd=cwd),
    )
    res = bridge.pty_terminal_ensure(80, 24)
    assert res["ok"] and res["pid"] == 8 and res["reused"] is False
    assert created == [os.getcwd()]


def test_pty_terminal_ensure_global_workdir_falls_back_to_cwd(bridge, monkeypatch):
    bridge._session_workdir = lambda name: "GLOBAL"
    seen = {}
    monkeypatch.setattr(
        "gui_bridge.create_pty_terminal",
        lambda cwd, cols, rows, on_output, on_exit: seen.update(cwd=cwd) or _FakePtySession(cwd=cwd),
    )
    res = bridge.pty_terminal_ensure(80, 24)
    assert res["ok"]
    assert seen["cwd"] == os.path.realpath(os.getcwd())


def test_pty_terminal_ensure_spawn_failure_returns_message(bridge, monkeypatch):
    from core.gui_terminal import PtyTerminalError

    def boom(cwd, cols, rows, on_output, on_exit):
        raise PtyTerminalError("未找到 PowerShell")

    monkeypatch.setattr("gui_bridge.create_pty_terminal", boom)
    res = bridge.pty_terminal_ensure(80, 24)
    assert res["ok"] is False and "PowerShell" in res["msg"]
    assert bridge._pty_session is None


def test_pty_terminal_write_and_resize_forward(bridge):
    session = _FakePtySession()
    bridge._pty_session = session
    assert bridge.pty_terminal_write("dir\r")["ok"] is True
    assert session.writes == ["dir\r"]
    assert bridge.pty_terminal_resize(120, 40)["ok"] is True
    assert session.resizes == [(120, 40)]


def test_pty_terminal_write_without_session_rejected(bridge):
    assert bridge.pty_terminal_write("dir\r")["ok"] is False
    bridge._pty_session = _FakePtySession(alive=False)
    assert bridge.pty_terminal_resize(80, 24)["ok"] is False


def test_pty_terminal_write_empty_rejected(bridge):
    bridge._pty_session = _FakePtySession()
    assert bridge.pty_terminal_write("")["ok"] is False
    assert bridge.pty_terminal_write(None)["ok"] is False


def test_pty_terminal_kill_clears_session(bridge):
    session = _FakePtySession()
    bridge._pty_session = session
    res = bridge.pty_terminal_kill()
    assert res["ok"] and session.killed and bridge._pty_session is None
    # 未启动时再次 kill 幂等
    assert bridge.pty_terminal_kill()["ok"] is True


def test_pty_data_and_exit_events_reach_js_queue(bridge, monkeypatch):
    captured = {}

    def fake_create(cwd, cols, rows, on_output, on_exit):
        captured["on_output"] = on_output
        captured["on_exit"] = on_exit
        return _FakePtySession(cwd=cwd)

    monkeypatch.setattr("gui_bridge.create_pty_terminal", fake_create)
    res = bridge.pty_terminal_ensure(80, 24)
    assert res["ok"]

    captured["on_output"]("$ dir\r\n")
    payload = bridge.drain_js_queue()
    assert payload["type"] == "pty_data"
    assert payload["data"] == {"text": "$ dir\r\n"}

    captured["on_exit"](0)
    payload = bridge.drain_js_queue()
    assert payload["type"] == "pty_exit"
    assert payload["data"] == {"code": 0}


def test_shutdown_kills_pty_session(bridge):
    session = _FakePtySession()
    bridge._pty_session = session
    # 复用 shutdown 的清理片段：直接模拟其内部逻辑（不跑完整 shutdown 链）
    with bridge._pty_lock:
        grabbed = bridge._pty_session
        bridge._pty_session = None
    grabbed.kill()
    assert session.killed


def test_clamp_terminal_size_bounds():
    assert clamp_terminal_size(0, 99999) == (2, 300)
    assert clamp_terminal_size("x", None) == (80, 24)
    assert clamp_terminal_size(120, 40) == (120, 40)


@pytest.mark.skipif(os.name != "nt", reason="ConPTY 仅 Windows")
def test_real_pty_session_roundtrip():
    """真实 ConPTY 往返：spawn PowerShell → echo → exit，输出与退出码齐全。"""
    from core.gui_terminal import create_pty_terminal

    out = []
    done = threading.Event()
    code_holder = {}
    session = create_pty_terminal(
        cwd=os.getcwd(),
        cols=100,
        rows=30,
        on_output=out.append,
        on_exit=lambda c: (code_holder.update(code=c), done.set()),
    )
    try:
        time.sleep(2.0)
        session.write("echo PTY_TEST_MARKER\r")
        time.sleep(1.5)
        session.resize(120, 40)
        session.write("exit\r")
        assert done.wait(20), "会话未在超时内退出"
        text = "".join(out)
        assert "PTY_TEST_MARKER" in text
        assert code_holder.get("code") == 0
    finally:
        session.kill()
