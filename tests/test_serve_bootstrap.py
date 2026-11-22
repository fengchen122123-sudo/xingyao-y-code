from __future__ import annotations

import json
import os
import socket
import urllib.request

import pytest


def test_linux_imports_credentials_frameless_and_bridge():
    import core.credentials
    from core.frameless import _MINMAXINFO, _MONITORINFO

    _MONITORINFO()
    _MINMAXINFO()
    import gui_bridge

    assert gui_bridge.ApiBridge is not None
    assert core.credentials.DPAPI_PREFIX == "dpapi:"


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
