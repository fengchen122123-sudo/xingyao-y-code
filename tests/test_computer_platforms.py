"""平台扩展边界：不使用真实系统输入、浏览器或热键。"""

import builtins
from types import SimpleNamespace

import pytest

from core.computer.models import ComputerControlError


@pytest.mark.parametrize("platform,name,supported", [
    ("win32", "windows", True), ("darwin", "macos", False),
    ("linux", "linux", False), ("freebsd14", "unsupported", False),
])
def test_platform_resolution(platform, name, supported):
    from core.computer.platforms import get_platform_adapter
    from core.computer.platforms.base import ComputerPlatformAdapter

    adapter = get_platform_adapter(platform)
    assert isinstance(adapter, ComputerPlatformAdapter)
    assert adapter.name == name
    assert adapter.supported is supported


@pytest.mark.parametrize("platform", ["darwin", "linux", "freebsd14"])
@pytest.mark.parametrize("method", ["create_desktop_backend", "create_hotkey_api", "preflight_desktop", "probe_hotkey"])
def test_reserved_platform_rejects_execution_without_windows_import(platform, method, monkeypatch):
    from core.computer.platforms import get_platform_adapter

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name not in {"core.computer.win32_adapter", "core.computer.windows_backend", "core.computer.hotkey"}
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    adapter = get_platform_adapter(platform)
    with pytest.raises(ComputerControlError) as raised:
        getattr(adapter, method)()
    assert raised.value.code == "computer_unsupported_os"
    assert adapter.unavailable_reason
    assert adapter.find_browser_executable("edge") == ""


def test_windows_adapter_delegates_to_existing_implementations(monkeypatch):
    from core.computer.platforms import get_platform_adapter
    from core.computer.windows_backend import WindowsComputerBackend
    from core.computer import hotkey, win32_adapter

    sentinel = object()
    calls = []
    monkeypatch.setattr(WindowsComputerBackend, "create_default", lambda: sentinel)
    monkeypatch.setattr(hotkey, "WindowsHotkeyApi", lambda: sentinel)
    monkeypatch.setattr(win32_adapter, "Win32InputAdapter", lambda: SimpleNamespace(preflight=lambda: calls.append("preflight")))
    monkeypatch.setattr(win32_adapter, "_default_hotkey_probe", lambda: True)
    adapter = get_platform_adapter("win32")
    assert adapter.create_desktop_backend() is sentinel
    assert adapter.create_hotkey_api() is sentinel
    adapter.preflight_desktop()
    assert calls == ["preflight"]
    assert adapter.probe_hotkey() is True


def test_settings_controller_and_runtime_share_platform_entry(monkeypatch):
    from core.computer import controller, platforms, runtime, settings
    from core.computer.models import BackendIdentity

    sentinel = object()
    adapter = SimpleNamespace(
        supported=True, unavailable_reason="",
        find_browser_executable=lambda browser: f"/test/{browser}",
        create_desktop_backend=lambda: sentinel,
        create_hotkey_api=lambda: sentinel,
    )
    monkeypatch.setattr(platforms, "get_platform_adapter", lambda platform=None: adapter)
    assert settings._is_platform_supported("test-platform") is True
    assert settings.list_browser_options(platform="test-platform")[0].executable == "/test/edge"
    desktop_identity = BackendIdentity(platform="windows", target="desktop")
    assert controller._default_backend_factory("desktop", desktop_identity) is sentinel
    assert runtime._default_hotkey_factory() is sentinel


def test_gui_preflight_uses_platform_adapter(monkeypatch):
    from core.computer import platforms, settings
    from gui_bridge import ApiBridge

    calls = []
    adapter = SimpleNamespace(
        supported=True, unavailable_reason="",
        preflight_desktop=lambda: calls.append("desktop"),
        probe_hotkey=lambda: calls.append("hotkey") or True,
    )
    monkeypatch.setattr(platforms, "get_platform_adapter", lambda platform=None: adapter)
    monkeypatch.setattr(settings, "load_computer_settings", lambda: SimpleNamespace(
        platform_supported=True, computer_browser="edge",
        browser_options=[SimpleNamespace(browser="edge", available=True)],
    ))
    bridge = object.__new__(ApiBridge)
    bridge._computer_control_preflight()
    bridge._browser_control_preflight()
    assert calls == ["desktop", "hotkey"]


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_reserved_settings_never_probe_windows_executables(platform, monkeypatch):
    from core.computer.settings import list_browser_options

    monkeypatch.setattr("shutil.which", lambda name: pytest.fail("unimplemented platform must not inspect Windows PATH"))
    options = list_browser_options(platform=platform)
    assert all(not option.available and option.unavailable_reason for option in options)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("target", ["browser", "desktop"])
def test_default_factory_rejects_unimplemented_platform_before_construction(platform, target, monkeypatch):
    from core.computer import controller, platforms
    from core.computer.models import BackendIdentity

    adapter = platforms.get_platform_adapter(platform)
    monkeypatch.setattr(platforms, "get_platform_adapter", lambda platform=None: adapter)
    identity = BackendIdentity(platform=adapter.name, target=target, browser="edge")
    with pytest.raises(ComputerControlError) as raised:
        controller._default_backend_factory(target, identity)
    assert raised.value.code == "computer_unsupported_os"
