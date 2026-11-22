import sys

import gui_main


def test_webview_start_disables_private_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path))

    options = gui_main._webview_start_options()

    assert options["private_mode"] is False
    assert options["storage_path"] == str(tmp_path / "webview")
    assert isinstance(options["debug"], bool)
    assert options["http_port"] == gui_main._frontend_http_port()


def test_frontend_http_port_frozen_uses_default(monkeypatch):
    # 安装版（PyInstaller frozen）沿用 pywebview 默认端口，终端用户
    # localStorage 偏好保持同一 origin，不因升级丢失
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert gui_main._frontend_http_port() == gui_main._HTTP_PORT_FROZEN


def test_frontend_http_port_source_isolated_from_frozen(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert gui_main._frontend_http_port() == gui_main._HTTP_PORT_SOURCE
    assert gui_main._HTTP_PORT_SOURCE != gui_main._HTTP_PORT_FROZEN
