"""P1a0：无窗口导入、健康检查、无人在线时立刻拒绝权限。"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _restore_permission_handlers():
    from core.permission_broker import permission_broker

    permission_broker.cancel_all()
    permission_broker.set_prompt_handlers(lambda request: None, lambda: None)


def test_linux_imports_credentials_frameless_and_bridge():
    import core.credentials
    from core.frameless import _MINMAXINFO, _MONITORINFO

    _MONITORINFO()
    _MINMAXINFO()
    import gui_bridge

    assert gui_bridge.ApiBridge is not None
    assert core.credentials.DPAPI_PREFIX == "dpapi:"


def test_serve_defaults_are_loopback_8766():
    from core.serve import SERVE_BIND, SERVE_PORT

    assert SERVE_BIND == "127.0.0.1"
    assert SERVE_PORT == 8766


def test_lan_bind_defaults_to_all_interfaces():
    from core.lan_server import LanServer

    server = LanServer()
    assert server.bind == "0.0.0.0"


def test_health_returns_boot_id_json():
    from core.lan_server import PROCESS_BOOT_ID, LanServer

    server = LanServer(port=0, bind="127.0.0.1")
    info = server.start(strict_port=True)
    try:
        assert info["host"] == "127.0.0.1"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{info['port']}/health", timeout=3
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
            content_type = response.headers.get("Content-Type", "")
        assert payload == {"ok": True, "boot_id": PROCESS_BOOT_ID}
        assert content_type.startswith("application/json")
    finally:
        server.stop()


def test_strict_port_does_not_fall_back():
    from core.lan_server import LanServer

    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    port = holder.getsockname()[1]
    server = LanServer(port=port, bind="127.0.0.1")
    try:
        with pytest.raises(OSError):
            server.start(strict_port=True)
    finally:
        holder.close()
        server.stop()


def test_remote_public_url_replaces_prefix_only(monkeypatch):
    from core.lan_server import LanServer

    server = LanServer()
    server.port = 8766
    server.lan_ip = "10.0.0.8"
    monkeypatch.setattr(
        "config._load",
        lambda managed=True: {"remote_public_url": "https://agent.example/"},
    )
    assert server.bind_url("tok") == "https://agent.example/?token=tok"
    monkeypatch.setattr("config._load", lambda managed=True: {})
    assert server.bind_url("tok") == "http://10.0.0.8:8766/?token=tok"


def test_windowless_emit_still_broadcasts():
    from gui_bridge import ApiBridge

    sent = []

    class _Lan:
        def broadcast(self, payload, event_type=""):
            sent.append((payload, event_type))

    class _Feishu:
        def __init__(self):
            self.events = []

        def on_event(self, event_type, data):
            self.events.append((event_type, data))

    feishu = _Feishu()
    bridge = type("Bridge", (), {})()
    bridge._shutting_down = False
    bridge._window = None
    bridge._lan_server = _Lan()
    bridge._feishu_service = feishu

    def _reject_js(_payload):
        raise AssertionError("无窗口时不应该把事件送进 JS 队列")

    bridge._queue_js_payload = _reject_js
    ApiBridge._emit_raw(bridge, "assistant_delta", {"text": "hi"})
    assert len(sent) == 1
    assert sent[0][1] == "assistant_delta"
    assert "hi" in sent[0][0]
    assert feishu.events == [("assistant_delta", {"text": "hi"})]

    queued = []
    bridge._window = object()
    bridge._queue_js_payload = queued.append
    ApiBridge._emit_raw(bridge, "assistant_delta", {"text": "desk"})
    assert len(queued) == 1
    assert len(sent) == 2

    bridge._shutting_down = True
    ApiBridge._emit_raw(bridge, "assistant_delta", {"text": "gone"})
    assert len(sent) == 2
    assert len(queued) == 1


def test_no_client_denies_permission_once():
    from core.permission_broker import permission_broker
    from core.serve.permission_policy import install_serve_permission_policy

    class _Bridge:
        def __init__(self):
            self.shown = False

        def _activate_permission_request(self, request):
            self.shown = True

        def _dismiss_permission_prompt(self):
            return None

    bridge = _Bridge()
    install_serve_permission_policy(bridge, client_count=lambda: 0)
    try:
        request = permission_broker.enqueue(
            "execute_command", {}, "owner", "turn", "server"
        )
        assert request.future.result(timeout=1) == 1
        assert bridge.shown is False
        assert permission_broker.resolve(request.request_id, 1) is False
    finally:
        _restore_permission_handlers()


def test_auto_deny_skips_wait_even_with_clients(monkeypatch):
    monkeypatch.setenv("YCODE_SERVE_AUTO_DENY", "1")
    from core.permission_broker import permission_broker
    from core.serve.permission_policy import install_serve_permission_policy

    class _Bridge:
        def __init__(self):
            self.shown = False

        def _activate_permission_request(self, request):
            self.shown = True

        def _dismiss_permission_prompt(self):
            return None

    bridge = _Bridge()
    install_serve_permission_policy(bridge, client_count=lambda: 2, wait_seconds=30)
    try:
        request = permission_broker.enqueue(
            "execute_command", {}, "owner", "turn", "server"
        )
        assert request.future.result(timeout=1) == 1
        assert bridge.shown is False
        assert permission_broker.resolve(request.request_id, 1) is False
    finally:
        _restore_permission_handlers()


def test_online_client_denies_after_wait():
    from core.permission_broker import permission_broker
    from core.serve.permission_policy import install_serve_permission_policy

    class _Bridge:
        def __init__(self):
            self.shown = 0

        def _activate_permission_request(self, request):
            self.shown += 1

        def _dismiss_permission_prompt(self):
            return None

    bridge = _Bridge()
    install_serve_permission_policy(bridge, client_count=lambda: 1, wait_seconds=0.05)
    try:
        request = permission_broker.enqueue(
            "execute_command", {}, "owner", "turn", "server"
        )
        assert bridge.shown == 1
        assert request.future.result(timeout=2) == 1
        assert permission_broker.resolve(request.request_id, 0) is False
    finally:
        _restore_permission_handlers()


def test_serve_lock_is_exclusive(tmp_path):
    from core.serve.lock import ServeLock

    first = ServeLock(tmp_path / "serve.lock")
    second = ServeLock(tmp_path / "serve.lock")
    assert first.acquire() is True
    try:
        assert second.acquire() is False
        assert (tmp_path / "serve.lock").read_text(encoding="utf-8") == str(os.getpid())
    finally:
        first.release()
    assert second.acquire() is True
    second.release()


def test_server_profile_does_not_start_mcp(monkeypatch):
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    from gui_bridge import ApiBridge

    bridge = ApiBridge.__new__(ApiBridge)
    assert ApiBridge._ensure_mcp_server(bridge) is None


def test_server_profile_accepted(monkeypatch):
    from core.tool_registry import VALID_TOOL_PROFILES, _resolve_tool_profile

    assert "server" in VALID_TOOL_PROFILES
    assert _resolve_tool_profile("server") == "server"
    assert _resolve_tool_profile("full") == "full"
    assert _resolve_tool_profile("cli") == "cli"
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    assert _resolve_tool_profile() == "server"


def test_tokenrhythm_disabled_on_server(monkeypatch):
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    from core import tokenrhythm_auth as auth

    assert auth.binding_active() is False
    denied = auth.password_login("account", "secret")
    assert denied["ok"] is False
    assert denied["logged_in"] is False
    health = auth.session_health()
    assert health["logged_in"] is False


def test_debug_tools_dump_stays_off_by_default(monkeypatch, tmp_path):
    from core import llm

    monkeypatch.delenv("YCODE_DEBUG_TOOLS_DUMP", raising=False)
    monkeypatch.setattr(llm, "_DEBUG_TOOLS_DUMP_FLAG", str(tmp_path / "missing.on"))
    assert llm._debug_tools_dump_enabled() is False
    monkeypatch.setenv("YCODE_DEBUG_TOOLS_DUMP", "1")
    assert llm._debug_tools_dump_enabled() is True
    monkeypatch.setenv("YCODE_DEBUG_TOOLS_DUMP", "0")
    flag = tmp_path / "llm_tools_dump.on"
    flag.write_text("1", encoding="utf-8")
    monkeypatch.setattr(llm, "_DEBUG_TOOLS_DUMP_FLAG", str(flag))
    assert llm._debug_tools_dump_enabled() is True


def test_user_paths_follow_data_dir(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("YCODE_DATA_DIR", str(data))
    from core.hooks.zcode_import import target_hooks_path
    from core.workflows.skill_sync import skill_dir
    from core.workflows.store import workflows_dir

    assert workflows_dir("user") == data / "workflows"
    assert skill_dir("demo") == data / "skills" / "zcode-workflows" / "demo"
    assert target_hooks_path("user") == data / "hooks.json"
    home = tmp_path / "home"
    assert workflows_dir("user", home=home) == home / ".ycode" / "workflows"


def test_server_skips_agents_skills(monkeypatch, tmp_path):
    agents = tmp_path / ".agents" / "skills"
    agents.mkdir(parents=True)
    (agents / "SKILL.md").write_text("name: demo\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "ycode-data"))
    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)
    from core.skills import get_skill_dirs

    present = get_skill_dirs()
    assert any(str(agents) == path or path.endswith(f"{os.sep}.agents{os.sep}skills") for path in present)

    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    skipped = get_skill_dirs()
    assert all(".agents" not in path for path in skipped)


def test_serve_process_answers_health_and_skips_mcp_port(tmp_path):
    port = 8766
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        pytest.skip("127.0.0.1:8766 已被占用")
    finally:
        probe.close()

    env = os.environ.copy()
    env["YCODE_DATA_DIR"] = str(tmp_path)
    env["YCODE_TOOL_PROFILE"] = "server"
    env["YCODE_SERVE_BIND"] = "127.0.0.1"
    env["YCODE_SERVE_PORT"] = str(port)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "gui_main.py"), "--serve"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.time() + 90
        last_error: Exception | None = None
        while time.time() < deadline:
            if process.poll() is not None:
                _stderr = process.stderr.read().decode("utf-8", "replace")
                raise AssertionError(
                    f"serve 提前退出 code={process.returncode}\n{_stderr[-4000:]}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                assert payload["ok"] is True
                assert payload.get("boot_id")
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                last_error = exc
                time.sleep(0.4)
        else:
            raise AssertionError(f"健康检查没有就绪: {last_error}")

        mcp = socket.socket()
        mcp.settimeout(0.5)
        try:
            assert mcp.connect_ex(("127.0.0.1", 8765)) != 0
        finally:
            mcp.close()
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
