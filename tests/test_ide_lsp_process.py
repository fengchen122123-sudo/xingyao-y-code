# -*- coding: utf-8 -*-
"""LspProcess：握手、请求转发、通知回调、优雅退出与超时强杀。"""
import copy
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_lsp_server import fake_env, make_fake_command, write_cmd_shim
from core.ide.lsp_manager import (
    LspProcess,
    LspRequestError,
    LspStartError,
    wrap_command,
)


def _spawn(tmp_path, mode="normal", **kwargs):
    proc = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(tmp_path),
        name="fake",
        env=fake_env(mode),
        **kwargs,
    )
    proc.start()
    return proc


def test_initialize_returns_server_capabilities(tmp_path):
    proc = _spawn(tmp_path)
    try:
        result = proc.initialize(str(tmp_path))
        assert result["capabilities"]["hoverProvider"] is True
        assert proc.is_alive() is True
    finally:
        proc.shutdown()


def test_request_returns_result(tmp_path):
    proc = _spawn(tmp_path)
    try:
        assert proc.request("test/echo", {"v": 1}) == {"echo": {"v": 1}}
    finally:
        proc.shutdown()


def test_request_error_is_raised_with_code(tmp_path):
    proc = _spawn(tmp_path)
    try:
        with pytest.raises(LspRequestError) as exc:
            proc.request("test/error", {})
        assert exc.value.code == -32602
        assert exc.value.message == "bad params"
    finally:
        proc.shutdown()


def test_request_timeout_does_not_hang(tmp_path):
    proc = _spawn(tmp_path)
    try:
        started = time.time()
        with pytest.raises(LspRequestError) as exc:
            proc.request("test/slow", {}, timeout=0.5)
        assert "超时" in exc.value.message
        assert time.time() - started < 5
    finally:
        proc.shutdown()


def test_notification_reaches_callback(tmp_path):
    seen = []
    proc = _spawn(tmp_path, on_notification=lambda p, m, params: seen.append((m, params)))
    try:
        proc.request("test/notify", {})
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen[0][0] == "test/pushed"
        assert seen[0][1] == {"n": 1}
    finally:
        proc.shutdown()


def test_shutdown_is_graceful(tmp_path):
    proc = _spawn(tmp_path)
    proc.initialize(str(tmp_path))
    proc.shutdown()
    assert proc.is_alive() is False
    assert proc.exit_code == 0


def test_shutdown_kills_server_that_ignores_exit(tmp_path):
    proc = _spawn(tmp_path, mode="ignore-shutdown", shutdown_timeout=1.0)
    proc.initialize(str(tmp_path))
    started = time.time()
    proc.shutdown()
    assert proc.is_alive() is False
    # shutdown + exit 都无响应，必须在超时后强杀，总耗时不超过 grace*2 + 余量
    assert time.time() - started < 8


def test_exit_callback_receives_intentional_flag(tmp_path):
    exits = []
    proc = _spawn(tmp_path, on_exit=lambda p, code, intentional: exits.append((code, intentional)))
    proc.initialize(str(tmp_path))
    proc.shutdown()
    deadline = time.time() + 5
    while not exits and time.time() < deadline:
        time.sleep(0.02)
    assert exits == [(0, True)]


def test_start_failure_raises_lsp_start_error(tmp_path):
    proc = LspProcess(["definitely-not-a-real-binary-9f3a"], cwd=str(tmp_path), name="bad")
    with pytest.raises(LspStartError):
        proc.start()


def test_request_before_start_raises(tmp_path):
    proc = LspProcess(make_fake_command(tmp_path), cwd=str(tmp_path), name="fake")
    with pytest.raises(Exception):
        proc.request("test/echo", {})


def test_uri_roundtrip():
    from core.ide.lsp_manager import path_to_uri, uri_to_path

    path = os.path.join(str(os.path.abspath(os.sep)), "tmp", "子目录", "a.py")
    assert uri_to_path(path_to_uri(path)) == os.path.normpath(path)


# --- 读循环：一次 read 里的短消息必须立刻派发 -------------------------------


def test_small_response_is_delivered_without_filling_the_buffer(tmp_path):
    """读循环不能等「攒满缓冲区」：`stdout.read(n)` 会一直等到 n 字节或 EOF，
    真实语言服务器的响应远小于缓冲区，等满就等于永久挂起（已用探针实测）。"""
    proc = _spawn(tmp_path, request_timeout=3.0)
    try:
        started = time.time()
        assert proc.request("test/echo", {"v": 1}) == {"echo": {"v": 1}}
        assert time.time() - started < 2
    finally:
        proc.shutdown()


# --- 评审发现 D：坏帧不能吞掉同一 chunk 里的合法帧 ---------------------------


def test_valid_frame_before_bad_frame_is_not_lost(tmp_path):
    """假服务器把 [合法响应帧][坏帧] 写进同一次 write（<4096 字节的管道写是原子的），
    读端一次 read 就会同时拿到两者。合法响应必须仍然被派发，请求不能挂起。"""
    proc = _spawn(tmp_path, request_timeout=5.0)
    try:
        assert proc.request("test/badframe", {}) == "ok"
        deadline = time.time() + 5
        while proc.is_alive() and time.time() < deadline:
            time.sleep(0.02)
        # 帧结构已损坏、无法重新对齐：不能留下「进程活着但没人读」的僵尸服务器
        assert proc.is_alive() is False
    finally:
        proc.kill()


def test_bad_frame_does_not_lose_the_pending_request(tmp_path):
    """握手之后出现的坏帧同样不能把已解码的响应吞掉（否则管理器会以为服务器卡死）。"""
    proc = _spawn(tmp_path, request_timeout=5.0)
    try:
        proc.initialize(str(tmp_path))
        assert proc.request("test/badframe", {}) == "ok"
    finally:
        proc.kill()


# --- 评审发现 C：握手用的初始化选项必须是本进程私有副本 ---------------------


def _capture_env(tmp_path, filename, mode="normal"):
    env = fake_env(mode)
    env["FAKE_LSP_CAPTURE_INIT"] = str(tmp_path / filename)
    return env


def test_workspace_initialization_options_do_not_leak_between_workspaces(tmp_path):
    """SPECS[*].initialization_options 是跨工作区共享的可变单例：
    只有深拷贝之后在新对象上改，第二个工作区才不会看到第一个工作区注入的内容。"""
    from core.ide import language_registry

    spec = language_registry.get_spec("jedi")
    before = copy.deepcopy(spec.initialization_options)
    # frozen=True 只挡字段重绑定，嵌套 dict 依旧是同一个对象（这正是危险之处）
    assert copy.copy(spec).initialization_options is spec.initialization_options

    ws1 = tmp_path / "ws1"
    ws2 = tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()

    first = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(ws1),
        name="fake",
        initialization_options=spec.initialization_options,
        env=_capture_env(tmp_path, "init-1.json"),
    )
    first.start()
    try:
        # 工作区相关配置只允许注入到本进程自己的副本上
        first.update_initialization_options({"workspace": {"extraPaths": [str(ws1)]}})
        first.initialize(str(ws1))
    finally:
        first.shutdown()

    second = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(ws2),
        name="fake",
        initialization_options=spec.initialization_options,
        env=_capture_env(tmp_path, "init-2.json"),
    )
    second.start()
    try:
        second.initialize(str(ws2))
    finally:
        second.shutdown()

    sent_first = json.loads((tmp_path / "init-1.json").read_text(encoding="utf-8"))
    sent_second = json.loads((tmp_path / "init-2.json").read_text(encoding="utf-8"))
    assert sent_first["initializationOptions"]["workspace"]["extraPaths"] == [str(ws1)]
    # 第二个工作区不能看到第一个工作区注入的内容（同一条路径都没出现过）
    assert sent_second["initializationOptions"]["workspace"]["extraPaths"] == []
    assert str(ws1) not in json.dumps(sent_second)
    # 注册表里的共享单例也不能被任何一次注入污染
    assert spec.initialization_options == before
    assert spec.initialization_options["workspace"]["extraPaths"] == []


def test_initialization_options_accept_spec_singleton_without_aliasing(tmp_path):
    """直接把单例交给 LspProcess 时，嵌套 dict 也不能被共享。"""
    from core.ide import language_registry

    spec = language_registry.get_spec("jedi")
    proc = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(tmp_path),
        name="fake",
        initialization_options=spec.initialization_options,
    )
    assert proc.initialization_options is not spec.initialization_options
    assert proc.initialization_options["workspace"] is not spec.initialization_options["workspace"]
    proc.initialization_options["workspace"]["extraPaths"].append("污染")
    assert spec.initialization_options["workspace"]["extraPaths"] == []


def test_update_initialization_options_deep_merges(tmp_path):
    proc = LspProcess(
        make_fake_command(tmp_path),
        cwd=str(tmp_path),
        name="fake",
        initialization_options={"a": {"b": 1, "c": 2}, "d": 3},
    )
    proc.update_initialization_options({"a": {"b": 9}, "e": 5})
    assert proc.initialization_options == {"a": {"b": 9, "c": 2}, "d": 3, "e": 5}


# --- 评审发现 F：Windows 的 .cmd/.bat shim 不能直接 Popen -------------------


def test_wrap_command_leaves_plain_argv_untouched():
    assert wrap_command(["python", "-u", "server.py", "--stdio"]) == [
        "python",
        "-u",
        "server.py",
        "--stdio",
    ]


@pytest.mark.skipif(os.name != "nt", reason="只有 Windows 的 .cmd/.bat 需要 cmd.exe /c")
def test_wrap_command_wraps_windows_script_shims(tmp_path, monkeypatch):
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    assert wrap_command([r"C:\npm\ts.cmd", "--stdio"]) == [
        r"C:\Windows\System32\cmd.exe",
        "/c",
        r"C:\npm\ts.cmd",
        "--stdio",
    ]
    assert wrap_command([r"C:\npm\ts.BAT"])[1:3] == ["/c", r"C:\npm\ts.BAT"]
    # 真正的 exe 不做任何包装
    assert wrap_command([r"C:\npm\ts.exe", "--stdio"]) == [r"C:\npm\ts.exe", "--stdio"]
    assert wrap_command([str(tmp_path / "ts.cmd")])[0].lower().endswith("cmd.exe")


@pytest.mark.skipif(os.name != "nt", reason="npm 全局命令的 .cmd shim 只存在于 Windows")
def test_windows_cmd_shim_can_be_started_and_handshake(tmp_path):
    shim = write_cmd_shim(tmp_path)
    proc = LspProcess([shim], cwd=str(tmp_path), name="fake-cmd", env=fake_env())
    proc.start()
    try:
        result = proc.initialize(str(tmp_path))
        assert result["capabilities"]["hoverProvider"] is True
        assert proc.request("test/echo", {"v": 2}) == {"echo": {"v": 2}}
    finally:
        proc.shutdown()
