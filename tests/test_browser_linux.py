"""P1a1：server 来源可以开浏览器，Linux 只用无头 Chromium。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.computer.capability import resolve_computer_capability
from core.computer.platforms import get_platform_adapter
from core.computer.settings import ComputerSettingsSnapshot


def _settings(**overrides):
    base = dict(
        browser_control_enabled=True,
        computer_control_enabled=True,
        computer_browser="chromium",
        browser_options=(),
        profile_display_path="",
        platform_supported=False,
        generation=1,
    )
    base.update(overrides)
    return ComputerSettingsSnapshot(**base)


def test_server_tool_table_keeps_browser_controls(monkeypatch):
    from core.tool_registry import SERVER_EXCLUDED_TOOLS, _absolute_path_example

    assert "browser_set_viewport" not in SERVER_EXCLUDED_TOOLS
    assert "computer_control" not in SERVER_EXCLUDED_TOOLS
    assert "skill_manage" in SERVER_EXCLUDED_TOOLS
    assert "plugin_manage" in SERVER_EXCLUDED_TOOLS
    assert "hook_manage" in SERVER_EXCLUDED_TOOLS
    monkeypatch.delenv("YCODE_TOOL_PROFILE", raising=False)
    assert "D:" in _absolute_path_example()
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    assert _absolute_path_example() == "/data/work/demo"


def test_linux_adapter_is_browser_only():
    adapter = get_platform_adapter("linux")
    assert adapter.supported is False
    assert adapter.browser_supported is True
    assert adapter.desktop_supported is False
    assert adapter.browser_window_hints(1) == ()
    assert adapter.find_browser_executable("edge") == ""


def test_linux_launch_is_headless_without_channel():
    from core.computer.browser_backend import BrowserComputerBackend

    backend = BrowserComputerBackend("chromium", Path("/tmp/ycode-chromium-profile"))
    kwargs = backend._persistent_context_kwargs("/tmp/ycode-chromium-profile")
    assert kwargs["headless"] is (sys.platform != "win32")
    if sys.platform != "win32":
        assert kwargs["args"] == ["--no-sandbox", "--disable-dev-shm-usage"]
        assert "channel" not in kwargs


def test_server_turn_allows_browser_and_drops_desktop():
    model = {
        "supports_vision": True,
        "supports_function_calling": True,
        "computer_protocol": "generic",
    }
    capability = resolve_computer_capability(model, {}, _settings(), "server")
    assert capability.enabled is True
    assert capability.targets == ("browser",)


def test_lan_turn_still_forbids_computer():
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, {}, _settings(platform_supported=True), "lan")
    assert capability.enabled is False
    assert capability.error_code == "computer_remote_forbidden"


def test_empty_server_config_enables_browser_only(monkeypatch):
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    import config

    monkeypatch.setattr(config, "_load", lambda managed=True: {})
    from core.computer.settings import load_computer_settings

    snapshot = load_computer_settings(platform="linux")
    assert snapshot.browser_control_enabled is True
    assert snapshot.computer_control_enabled is False
    assert snapshot.computer_browser == "chromium"


def test_server_connection_injects_server_turn_source(monkeypatch):
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    from core.lan_server import LanServer

    class Bridge:
        def send_user_message(self, text="", **kwargs):
            return {"ok": True, "turn_source": kwargs.get("turn_source"), "text": text}

    server = LanServer(bridge=Bridge())
    result = server.dispatch_rpc("send_user_message", {"text": "hi"})
    assert result["turn_source"] == "server"
    assert result["text"] == "hi"


def test_missing_boot_id_resets_without_replay():
    from core.lan_server import PROCESS_BOOT_ID, LanServer

    class Socket:
        def __init__(self, first):
            self._first = first
            self.sent = []

        def recv(self, timeout=None):
            message, self._first = self._first, None
            return message

        def __iter__(self):
            return iter(())

        def send(self, payload):
            self.sent.append(payload)

        def close(self, code=None, reason=None):
            pass

    server = LanServer(bridge=None)
    token = server._issue_token()
    server.broadcast(
        json.dumps({"type": "turn_error", "data": {"message": "old"}}),
        event_type="turn_error",
    )
    socket = Socket(json.dumps({"token": token, "last_seq": 0}))
    server._handle_ws(socket)
    frames = [json.loads(item) for item in socket.sent]
    assert frames[0]["type"] == "hello"
    assert frames[0]["data"]["boot_id"] == PROCESS_BOOT_ID
    assert "id" not in frames[0]
    assert frames[1]["type"] == "seq_reset"
    assert frames[1]["data"]["reason"] == "boot"
    assert frames[1]["data"]["resync_required"] is True
    assert all(frame.get("type") != "turn_error" for frame in frames)


def test_preview_requires_worker_token_and_hides_it_from_the_path(monkeypatch):
    monkeypatch.setenv("YCODE_TOOL_PROFILE", "server")
    from core.lan_server import LanServer

    server = LanServer(bridge=None)
    token = server._issue_token()
    payload = json.dumps({
        "type": "computer_observation",
        "data": {
            "preview_url": "http://127.0.0.1:9/computer-preview/preview-token/run1/1.png",
        },
    })
    rewritten = server._rewrite_preview_url(payload, "computer_observation")
    public = json.loads(rewritten)["data"]["preview_url"]
    assert public == "/computer-preview/preview-token/run1/1.png"
    assert token not in public

    class Request:
        def __init__(self, authorization):
            self.headers = {"Authorization": authorization}
            self.path = public

    denied = server._serve_computer_preview(public, Request("Bearer wrong"))
    assert denied.status_code == 401
