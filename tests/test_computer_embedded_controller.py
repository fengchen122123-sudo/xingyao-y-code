"""内置 WebView2（embedded）通道的 controller 接线测试：
endpoint 提供者注入、identity 冻结（endpoint + 项目视口）、factory 构造。"""

from pathlib import Path

import pytest

from core.computer.controller import ComputerControlService, _default_backend_factory
from core.computer.embedded_browser_backend import EmbeddedBrowserBackend
from core.computer.models import (
    BackendIdentity,
    ComputerCapability,
    ComputerControlError,
    ComputerProtocol,
)


def _capability(browser: str = "embedded") -> ComputerCapability:
    return ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser=browser,
        settings_generation=0,
    )


def _service(provider=None) -> ComputerControlService:
    return ComputerControlService(embedded_endpoint_provider=provider)


def test_identity_without_provider_fails_explicitly():
    """CLI/无 GUI 不注入 provider：embedded 明确失败，不静默回落外部浏览器。"""
    service = ComputerControlService()
    with pytest.raises(ComputerControlError) as excinfo:
        service._resolve_backend_identity("browser", _capability())
    assert excinfo.value.code == "computer_embedded_unavailable"


def test_identity_carries_endpoint_and_project_viewport(tmp_path, monkeypatch):
    from core.computer.settings import set_project_browser_viewport
    from core.workspace_context import workspace_scope

    workdir = tmp_path / "proj"
    workdir.mkdir()
    set_project_browser_viewport(str(workdir), 1024, 768)
    service = _service(provider=lambda session_id: "http://127.0.0.1:9333")
    with workspace_scope(str(workdir)):
        identity = service._resolve_backend_identity("browser", _capability())
    assert identity.browser == "embedded"
    assert identity.cdp_endpoint == "http://127.0.0.1:9333"
    assert (identity.viewport_width, identity.viewport_height) == (1024, 768)
    # profile 即面板控件（自动化接管的就是用户看到的那个浏览器）的
    # UserDataFolder：两者共用 browser_profile，不再有独立自动化目录
    assert identity.profile_path.replace("\\", "/").endswith("browser_profile")


def test_provider_receives_owner_session_id():
    """embedded 分支把发起会话名透传给 endpoint 提供者：GUI 侧据此把浏览器
    页签开进发起会话的右面板（会话级隔离，不在其他会话抢屏）。"""
    seen: list[str] = []
    service = _service(provider=lambda session_id: (seen.append(session_id), "http://127.0.0.1:9334")[1])
    service._resolve_backend_identity("browser", _capability(), "sess-A")
    assert seen == ["sess-A"]
    # 缺省（如直接以身份解析调试）传空串，不炸
    service._resolve_backend_identity("browser", _capability())
    assert seen == ["sess-A", ""]


def test_provider_failure_maps_to_embedded_unavailable():
    def _boom(_session_id: str = ""):
        raise RuntimeError("form not ready")

    service = _service(provider=_boom)
    with pytest.raises(ComputerControlError) as excinfo:
        service._resolve_backend_identity("browser", _capability())
    assert excinfo.value.code == "computer_embedded_unavailable"


def test_factory_builds_embedded_backend_from_identity():
    identity = BackendIdentity(
        platform="win32",
        target="browser",
        browser="embedded",
        profile_path=str(Path(".")),
        cdp_endpoint="http://127.0.0.1:9444",
        viewport_width=800,
        viewport_height=600,
    )
    backend = _default_backend_factory("browser", identity)
    assert isinstance(backend, EmbeddedBrowserBackend)
    assert backend.cdp_endpoint == "http://127.0.0.1:9444"
    assert (backend._viewport_width, backend._viewport_height) == (800, 600)


def test_factory_embedded_requires_endpoint():
    identity = BackendIdentity(
        platform="win32",
        target="browser",
        browser="embedded",
        profile_path=str(Path(".")),
    )
    with pytest.raises(ComputerControlError) as excinfo:
        _default_backend_factory("browser", identity)
    assert excinfo.value.code == "computer_embedded_unavailable"


def test_factory_external_browser_honors_project_viewport():
    """项目分辨率对外部浏览器同样生效（作为启动视口），行为一致。"""
    identity = BackendIdentity(
        platform="win32",
        target="browser",
        browser="edge",
        profile_path=str(Path(".")),
        viewport_width=1024,
        viewport_height=768,
    )
    backend = _default_backend_factory("browser", identity)
    assert (backend._viewport_width, backend._viewport_height) == (1024, 768)
    # 外部浏览器仍是进程模式 + 拥有 context（进程树终止语义不变）
    assert backend._use_process_worker is True
    assert backend._owns_context is True
