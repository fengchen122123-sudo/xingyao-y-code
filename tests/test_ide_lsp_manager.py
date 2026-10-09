# -*- coding: utf-8 -*-
"""LspManager：按工作区按需启动、通知转发、崩溃限次重启、回收。

文件尾两段是评审发现：
- A（Task 4 评审）：管理器层必须兜底回收所有启动过的子进程，断言用 pid 存活探测
  （直接问操作系统），不看对象字段；
- D（Task 4 评审）：回包时进程已退出（`_send` 抛 LspNotRunning）不能让读线程
  带 traceback 终止。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lsp_server import fake_env, make_fake_command
from core.ide.lsp_manager import LspManager, LspNotRunning, LspProcess, LspStartError


def _provision_for(command):
    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }
    return _resolve


def _manager(tmp_path, **kwargs):
    kwargs.setdefault("restart_backoff", 0.0)
    kwargs.setdefault("provision", _provision_for(make_fake_command(tmp_path)))
    return LspManager(**kwargs)


def _wait_for(getter, predicate, timeout=10.0, interval=0.05):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = getter()
        if predicate(last):
            return last
        time.sleep(interval)
    return last


def test_ensure_server_starts_once_per_workspace_language(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    try:
        first = manager.ensure_server(str(tmp_path), "python")
        second = manager.ensure_server(str(tmp_path), "python")
        assert first is second
        assert first.is_alive() is True
        assert first.server_capabilities == {"hoverProvider": True}
    finally:
        manager.recycle_all()


def test_request_and_notify_route_through_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    seen = []
    manager = _manager(tmp_path, on_notification=lambda ws, lang, m, p: seen.append((ws, lang, m, p)))
    try:
        assert manager.request(str(tmp_path), "python", "test/echo", {"v": 2}) == {"echo": {"v": 2}}
        manager.notify(str(tmp_path), "python", "test/notify", {})
        _wait_for(lambda: seen, lambda s: len(s) >= 1)
        assert seen[0][1] == "python"
        assert seen[0][2] == "test/pushed"
    finally:
        manager.recycle_all()


def test_status_reports_starting_then_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    states = []
    manager = _manager(tmp_path, on_status=lambda s: states.append(s["state"]))
    try:
        manager.ensure_server(str(tmp_path), "python")
        assert states[0] == "starting"
        assert states[-1] == "running"
        row = manager.status(str(tmp_path))[0]
        assert row["state"] == "running"
        assert row["server"] == "jedi"
        assert row["pid"] == manager.ensure_server(str(tmp_path), "python").pid
    finally:
        manager.recycle_all()


def test_missing_executable_raises_start_error(tmp_path, monkeypatch):
    manager = LspManager(
        provision=lambda server_id: {
            "server": server_id, "executable": "", "extra_args": [], "source": "missing",
            "msg": "未找到 jedi-language-server",
        },
        restart_backoff=0.0,
    )
    with pytest.raises(LspStartError):
        manager.ensure_server(str(tmp_path), "python")


def test_unknown_language_raises_start_error(tmp_path):
    manager = _manager(tmp_path)
    with pytest.raises(LspStartError):
        manager.ensure_server(str(tmp_path), "cobol")


def test_two_workspaces_get_separate_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    manager = _manager(tmp_path)
    try:
        p1 = manager.ensure_server(str(ws1), "python")
        p2 = manager.ensure_server(str(ws2), "python")
        assert p1 is not p2
        assert p1.pid != p2.pid
        assert len(manager.status(None)) == 2
    finally:
        manager.recycle_all()


def test_recycle_workspace_only_touches_that_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    manager = _manager(tmp_path)
    p1 = manager.ensure_server(str(ws1), "python")
    p2 = manager.ensure_server(str(ws2), "python")
    manager.recycle_workspace(str(ws1))
    assert p1.is_alive() is False
    assert p2.is_alive() is True
    remaining = manager.status(None)
    assert len(remaining) == 1
    assert remaining[0]["workspace"] == os.path.normcase(os.path.abspath(str(ws2)))
    manager.recycle_all()
    assert p2.is_alive() is False
    assert manager.status(None) == []


def test_crash_restarts_up_to_limit_then_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "exit-after-init")
    manager = LspManager(
        provision=_provision_for(make_fake_command(tmp_path)),
        restart_backoff=0.0,
        max_restarts=2,
    )
    try:
        manager.ensure_server(str(tmp_path), "python")
        row = _wait_for(
            lambda: (manager.status(str(tmp_path)) or [{}])[0],
            lambda r: r.get("state") == "failed",
            timeout=15,
        )
        assert row["state"] == "failed"
        assert row["restarts"] == 2
        assert "重启" in row["msg"]
    finally:
        manager.recycle_all()


def test_crash_does_not_restart_after_intentional_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    statuses = []
    manager = _manager(tmp_path, on_status=lambda s: statuses.append(s["state"]), max_restarts=3)
    manager.ensure_server(str(tmp_path), "python")
    manager.recycle_all()
    time.sleep(0.3)
    assert "restarting" not in statuses


# --- 评审发现 A：管理器层兜底回收 -------------------------------------------


def _pid_alive(pid) -> bool:
    """真实存活探测：直接问操作系统这个 pid 还在不在，不看对象字段。"""
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION：查询退出码所需的最小权限
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid_dead(pid, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_close_reclaims_every_started_process(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    manager = _manager(tmp_path)
    p1 = manager.ensure_server(str(ws1), "python")
    p2 = manager.ensure_server(str(ws2), "python")
    pids = [p1.pid, p2.pid]
    assert all(_pid_alive(pid) for pid in pids)
    manager.close()
    for pid in pids:
        assert _wait_pid_dead(pid), "pid %s 在管理器关闭后仍然存活" % pid


def test_close_reclaims_process_even_if_slot_was_lost(tmp_path, monkeypatch):
    """异常路径模拟：槽位丢了（调用方漏调 shutdown）也不能泄漏子进程。"""
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    pid = manager.ensure_server(str(tmp_path), "python").pid
    assert _pid_alive(pid)
    manager._slots.clear()
    manager.close()
    assert _wait_pid_dead(pid), "pid %s 在管理器关闭后仍然存活" % pid


def test_close_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    manager.ensure_server(str(tmp_path), "python")
    first = manager.close()
    assert len(first) == 1
    assert manager.close() == []
    assert manager.status(None) == []


def test_context_manager_reclaims_on_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    with manager as opened:
        pid = opened.ensure_server(str(tmp_path), "python").pid
        assert _pid_alive(pid)
    assert _wait_pid_dead(pid), "pid %s 在退出 with 后仍然存活" % pid


def test_del_reclaims_processes(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    pid = manager.ensure_server(str(tmp_path), "python").pid
    assert _pid_alive(pid)
    manager.__del__()
    assert _wait_pid_dead(pid), "pid %s 在管理器析构后仍然存活" % pid
    manager.close()


def test_closed_manager_refuses_to_start_more_processes(tmp_path, monkeypatch):
    """关闭必须是终态：否则崩溃重启线程能在关闭后新起一个没人回收的进程。"""
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    manager = _manager(tmp_path)
    manager.ensure_server(str(tmp_path), "python")
    manager.close()
    with pytest.raises(LspStartError):
        manager.ensure_server(str(tmp_path), "python")


def test_exit_after_close_does_not_restart(tmp_path, monkeypatch):
    """关闭之后才到的崩溃回调不允许再拉起进程（否则直接泄漏）。"""
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    statuses = []
    manager = _manager(tmp_path, on_status=lambda s: statuses.append(s["state"]))
    proc = manager.ensure_server(str(tmp_path), "python")
    key = (manager._norm(str(tmp_path)), "python")
    manager.close()
    # 关闭后手工放回槽位，模拟「回调晚于关闭到达」：必须只标记停止、不再重启
    manager._slots[key] = {
        "proc": proc, "restarts": 0, "state": "running",
        "server_id": "jedi", "msg": "", "started_at": time.time(),
    }
    manager._handle_exit(key, str(tmp_path), "python", proc, 1, False)
    assert "restarting" not in statuses
    assert manager.status(str(tmp_path))[0]["state"] == "stopped"


class _RecordingProcess(LspProcess):
    """把每次真正 spawn 出来的 pid 记到测试提供的列表里。

    断言必须落在 pid + 操作系统存活探测上：只看 `_slots` 这类对象字段，
    漏掉的正是「槽位没了但进程还在跑」这一种残留。
    """

    def __init__(self, *args, spawned=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._spawned = spawned

    def start(self):
        super().start()
        if self._spawned is not None:
            self._spawned.append(self.pid)


def test_exit_race_with_recycle_workspace_does_not_resurrect(tmp_path, monkeypatch):
    """评审发现 A：崩溃退避期间工作区被回收，退避结束后不得重建进程。

    交错时序：`_handle_exit` 持锁判定为崩溃并释放锁 → 退避中
    → `recycle_workspace` pop 掉槽位（此时 `slot["proc"]` 已是 None，不会 shutdown）
    → 退避结束。没有世代护栏时，最后那步 `ensure_server` 会给一个已经切走的
    工作区重新拉起语言服务器，留下跨工作区残留。
    """
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    ws = tmp_path / "ws"
    ws.mkdir()
    spawned = []
    manager = LspManager(
        provision=_provision_for(make_fake_command(tmp_path)),
        restart_backoff=1.0,
        process_factory=lambda *a, **kw: _RecordingProcess(*a, spawned=spawned, **kw),
    )
    try:
        proc = manager.ensure_server(str(ws), "python")
        pid = proc.pid
        assert _pid_alive(pid)
        # 让服务器自己崩溃（test/crash → sys.exit(9)），进入崩溃重启流程
        manager.notify(str(ws), "python", "test/crash", {})
        state = _wait_for(
            lambda: (manager.status(str(ws)) or [{}])[0].get("state"),
            lambda s: s == "restarting",
        )
        assert state == "restarting", "崩溃回调没有进入退避等待，用例前提不成立"

        # 退避期间用户切走工作区：槽位被 pop，进程已死
        recycled = manager.recycle_workspace(str(ws))
        assert recycled == [(os.path.normcase(os.path.abspath(str(ws))), "python")]

        # 等退避窗口过去（+ 余量），观察是否有人把进程重新拉起来
        _wait_for(lambda: manager.status(None), lambda rows: bool(rows), timeout=1.5)
        assert manager.status(None) == [], "已回收的工作区被重建了槽位"
        assert len(spawned) == 1, "已回收的工作区被重建了进程：spawned=%s" % spawned
        assert all(not _pid_alive(p) for p in spawned), "回收后仍有语言服务器进程存活"
    finally:
        manager.close()


# --- 评审发现 D：读线程的兜底 -------------------------------------------------


_REVERSE_SERVER_SOURCE = r'''
import json
import sys


def send(payload):
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.strip().lower()] = value.strip()
    body = sys.stdin.buffer.read(int(headers.get("content-length", "0")))
    return json.loads(body.decode("utf-8"))


def main():
    asked = False
    while True:
        msg = read_message()
        if msg is None:
            return 0
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {}}})
        elif method == "initialized":
            if not asked:
                asked = True
                # 反向请求：制造「回包时进程已退出」的分支
                send({"jsonrpc": "2.0", "id": 9001,
                      "method": "workspace/configuration",
                      "params": {"items": [{"section": "jedi"}]}})
        elif method == "shutdown":
            send({"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "exit":
            return 0
        elif method == "test/echo":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"echo": msg.get("params")}})


if __name__ == "__main__":
    sys.exit(main())
'''


def _reverse_command(tmp_path) -> list:
    path = os.path.join(str(tmp_path), "reverse_lsp.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_REVERSE_SERVER_SOURCE)
    return [sys.executable, "-u", path]


def test_reader_thread_survives_failed_reply_to_server_request(tmp_path, monkeypatch):
    """D：`_handle_server_request` 里的 `_send` 抛 LspNotRunning 逃出读线程。

    断言「连接仍然可用」：同一个进程随后还能应答 test/echo，而且没有任何
    未捕获的线程异常（threading.excepthook 记录为空）。
    """
    thread_errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: thread_errors.append(args))
    raised = []

    def _boom(self, msg_id, method, params):
        raised.append(method)
        raise LspNotRunning("语言服务器 %s 已退出" % self.name)

    monkeypatch.setattr(LspProcess, "_handle_server_request", _boom)
    proc = LspProcess(
        _reverse_command(tmp_path), cwd=str(tmp_path), name="reverse", env=fake_env("normal")
    )
    proc.start()
    try:
        proc.initialize(str(tmp_path))
        _wait_for(lambda: raised, lambda v: len(v) >= 1, timeout=10)
        assert raised == ["workspace/configuration"]
        assert proc.request("test/echo", {"v": 3}, timeout=5) == {"echo": {"v": 3}}
        time.sleep(0.2)
        assert thread_errors == []
    finally:
        proc.shutdown()
