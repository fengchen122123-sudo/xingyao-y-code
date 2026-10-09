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


def test_ensure_server_rejects_stale_epoch(tmp_path):
    """过期令牌直接拒绝启动，绝不静默建槽（S2 验收欠账 1，管理器级）。"""
    import pytest

    from core.ide.lsp_manager import LspStartError

    ws = tmp_path / "ws"
    ws.mkdir()
    manager = _manager(tmp_path)

    stale = manager.current_epoch()
    manager.recycle_all()  # 取完令牌之后发生了一次回收
    assert manager.current_epoch() != stale

    with pytest.raises(LspStartError, match="过期"):
        manager.ensure_server(str(ws), "python", epoch=stale)

    assert manager.status(None) == []  # 没建槽，也就没有进程
    # 不带令牌（内部重启等既有调用方）保持向后兼容
    manager.recycle_all()


def test_stale_epoch_request_does_not_respawn_old_workspace_server(tmp_path, monkeypatch):
    """取到旧 (workspace, epoch) → 回收 → 带着旧令牌的请求不得给旧工作区重拉进程。

    竞态窗口：`_lsp_workspace_token()` 取 workspace/epoch 与 `ensure_server` 真正
    建槽之间会穿插路径校验、可执行文件探测等动作。若这中间工作区被回收，旧的
    `ensure_server` 会给**旧工作区**重新建槽并拉起一个全新进程（新世代号，
    世代护栏拦不住），而且此后 `_lsp_workspace` 已是新工作区、下一次 guard 判定
    「无变化」不再回收——那个进程会一直活到下次切换或 close()。
    """
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    f1 = ws1 / "a.py"
    f1.write_text("x = 1\n", encoding="utf-8")
    manager = _manager(tmp_path)
    bridge = _make_bridge(str(ws1), tmp_path, monkeypatch, manager)

    assert bridge.lsp_request(str(f1), "test/echo", {})["ok"] is True
    procs = [slot["proc"] for slot in manager._slots.values()]
    assert len(procs) == 1
    old_pid = procs[0].pid
    assert _pid_alive(old_pid) is True

    # 精确复现竞态：guard 已经返回 (ws1, epoch)，此时回收插进来，然后才建槽
    original_request = manager.request

    def racing_request(workspace, language_id, method, params=None, *, timeout=None, epoch=None):
        manager.recycle_all()
        return original_request(
            workspace, language_id, method, params, timeout=timeout, epoch=epoch
        )

    monkeypatch.setattr(manager, "request", racing_request)

    res = bridge.lsp_request(str(f1), "test/echo", {})

    assert res["ok"] is False
    assert "过期" in res["msg"]
    # 旧进程确实被回收，且**没有**被重新拉起：用 pid 向操作系统求证，不看对象字段
    assert _pid_alive(old_pid) is False
    assert manager.status(None) == []
    assert manager._started == []
