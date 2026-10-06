# -*- coding: utf-8 -*-
"""切换工作区必须回收全部语言服务器进程，不得残留。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core.gui_path_policy as gpp
from fake_lsp_server import make_fake_command
from gui_bridge import ApiBridge
from core.ide.lsp_manager import LspManager


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


def _manager(tmp_path):
    command = make_fake_command(tmp_path)

    def _resolve(server_id):
        return {
            "server": server_id,
            "executable": command[0],
            "extra_args": command[1:],
            "source": "path",
            "msg": "",
        }

    return LspManager(provision=_resolve, restart_backoff=0.0)


def _make_bridge(workdir, tmp_path, monkeypatch, manager):
    monkeypatch.setenv("FAKE_LSP_MODE", "normal")
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "s1"
    bridge._lsp_manager = manager
    return bridge


def test_lsp_sync_workspace_kills_all_servers(tmp_path, monkeypatch):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    f = ws1 / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws1), tmp_path, monkeypatch, manager)

    bridge.lsp_request(str(f), "test/echo", {})
    running = [row for row in manager.status(None) if row["state"] == "running"]
    assert len(running) == 1
    procs = [slot["proc"] for slot in manager._slots.values()]
    assert all(p is not None and p.is_alive() for p in procs)

    res = bridge.lsp_sync_workspace()
    assert res["ok"] is True
    assert res["workspace"] == str(ws1)
    assert manager.status(None) == []
    assert all(not p.is_alive() for p in procs)


def test_switching_session_workdir_recycles_on_next_request(tmp_path, monkeypatch):
    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    f1 = ws1 / "a.py"
    f1.write_text("x = 1\n", encoding="utf-8")
    f2 = ws2 / "b.py"
    f2.write_text("y = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws1), tmp_path, monkeypatch, manager)

    bridge.lsp_request(str(f1), "test/echo", {})
    first = [slot["proc"] for slot in manager._slots.values()][0]

    # 用户切到另一个工作区：即使前端没主动调 lsp_sync_workspace，
    # 下一次请求也必须先把旧进程收掉
    monkeypatch.setattr(bridge, "_session_workdir", lambda session_name="": str(ws2))
    bridge.lsp_request(str(f2), "test/echo", {})
    assert first.is_alive() is False
    rows = manager.status(None)
    assert len(rows) == 1
    assert rows[0]["workspace"] == os.path.normcase(os.path.abspath(str(ws2)))
    bridge.lsp_sync_workspace()
    assert manager.status(None) == []


def test_lsp_sync_workspace_without_manager_is_safe(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, None)
    res = bridge.lsp_sync_workspace()
    assert res["ok"] is True
    assert res["recycled"] == []


def test_lsp_sync_workspace_is_idempotent(tmp_path, monkeypatch):
    """回收必须幂等：连续两次同步不得报错，也不得留下任何进程。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws), tmp_path, monkeypatch, manager)
    bridge.lsp_request(str(f), "test/echo", {})
    pids = [slot["proc"].pid for slot in manager._slots.values()]

    first = bridge.lsp_sync_workspace()
    second = bridge.lsp_sync_workspace()
    assert first["ok"] is True and second["ok"] is True
    assert second["recycled"] == []
    assert manager.status(None) == []
    assert all(not _pid_alive(pid) for pid in pids)
