"""core.gui_runtime_check 单元测试：WebView2 Runtime 探测与原生告警。

背景（Win10 黑屏反馈）：GUI 走 pywebview 的 edgechromium 后端，强依赖系统
Microsoft Edge WebView2 Runtime。该运行时缺失/损坏时 pywebview 只写一条日志
就 return（不导航、不弹窗），窗口本身已创建并显示，用户看到的是 WebView2 控件的
DefaultBackgroundColor（即窗口 background_color #1a1a18）铺满客户区——纯色、
无内容、无提示。本模块负责把这种"哑巴黑屏"变成可读结论，故只测探测分支与
告警边界；真实 Windows API 用替身，不测系统本身。
"""

import sys

import pytest

import core.gui_runtime_check as grc


def test_registry_locations_cover_machine_and_user_scope():
    """机器级（32/64 位视图）与用户级都要查：WebView2 允许按用户安装。"""
    scopes = {hive for hive, _ in grc.WEBVIEW2_REGISTRY_LOCATIONS}

    assert scopes == {"HKLM", "HKCU"}
    assert all(
        grc.WEBVIEW2_CLIENT_GUID in path for _, path in grc.WEBVIEW2_REGISTRY_LOCATIONS
    )
    # 32 位进程看不到 64 位视图，WOW6432Node 必须在候选里
    assert any("WOW6432Node" in path for _, path in grc.WEBVIEW2_REGISTRY_LOCATIONS)


def test_detect_webview2_runtime_returns_version_from_first_hit(monkeypatch):
    monkeypatch.setattr(
        grc,
        "read_registry_pv",
        lambda hive, path: "120.0.2210.91" if hive == "HKLM" else None,
    )

    assert grc.detect_webview2_runtime() == "120.0.2210.91"


def test_detect_webview2_runtime_falls_back_to_later_locations(monkeypatch):
    seen = []

    def _fake(hive, path):
        seen.append(hive)
        return "131.0.2903.86" if hive == "HKCU" else None

    monkeypatch.setattr(grc, "read_registry_pv", _fake)

    assert grc.detect_webview2_runtime() == "131.0.2903.86"
    assert seen.count("HKLM") == 2  # 两个机器级位置都试过才轮到用户级


def test_detect_webview2_runtime_returns_none_when_all_absent(monkeypatch):
    monkeypatch.setattr(grc, "read_registry_pv", lambda hive, path: None)

    assert grc.detect_webview2_runtime() is None


def test_detect_webview2_runtime_returns_none_off_windows(monkeypatch):
    """非 Windows 平台不得读注册表（winreg 不存在）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(grc, "read_registry_pv", lambda hive, path: "120.0")

    assert grc.detect_webview2_runtime() is None


def test_read_registry_pv_returns_none_when_key_missing(monkeypatch):
    """键不存在/无权限必须静默返回 None：探测失败不能影响启动。"""
    import winreg

    def _raise(*args, **kwargs):
        raise FileNotFoundError(2, "系统找不到指定的文件")

    monkeypatch.setattr(winreg, "OpenKey", _raise)

    assert grc.read_registry_pv("HKLM", r"SOFTWARE\NotExist") is None


def test_check_reports_version_without_warning(monkeypatch):
    monkeypatch.setattr(grc, "detect_webview2_runtime", lambda: "131.0.2903.86")
    warned = []
    monkeypatch.setattr(grc, "warn_missing_webview2_runtime", lambda: warned.append(1))

    assert grc.check_webview2_runtime() == "131.0.2903.86"
    assert warned == []


def test_check_warns_when_runtime_missing(monkeypatch):
    monkeypatch.setattr(grc, "detect_webview2_runtime", lambda: None)
    warned = []
    monkeypatch.setattr(grc, "warn_missing_webview2_runtime", lambda: warned.append(1))

    assert grc.check_webview2_runtime() is None
    assert warned == [1]


def test_warn_missing_opens_download_page_when_user_confirms(monkeypatch):
    captured = {}

    def _box(text, title, flags):
        captured["text"] = text
        captured["title"] = title
        captured["flags"] = flags
        return grc._IDYES

    opened = []
    monkeypatch.setattr(grc, "_show_message_box", _box)
    monkeypatch.setattr(grc.os, "startfile", lambda url: opened.append(url))

    grc.warn_missing_webview2_runtime()

    assert opened == [grc.WEBVIEW2_DOWNLOAD_URL]
    assert captured["flags"] & 0x04  # MB_YESNO：允许用户选择
    assert captured["flags"] & 0x00040000  # MB_TOPMOST：黑屏时不能被空白主窗口盖住
    assert "WebView2" in captured["text"]


def test_warn_missing_does_not_open_download_page_when_user_declines(monkeypatch):
    opened = []
    monkeypatch.setattr(grc, "_show_message_box", lambda text, title, flags: 7)  # IDNO
    monkeypatch.setattr(grc.os, "startfile", lambda url: opened.append(url))

    grc.warn_missing_webview2_runtime()

    assert opened == []


def test_warn_render_failure_points_to_log_file(monkeypatch):
    """页面加载失败的兜底提示必须给出日志路径，反馈者才拿得到证据。"""
    captured = {}
    monkeypatch.setattr(
        grc,
        "_show_message_box",
        lambda text, title, flags: captured.update(text=text, title=title, flags=flags) or 1,
    )

    grc.warn_render_failure(r"C:\Users\demo\.ycode\logs\gui-runtime.log")

    assert r"C:\Users\demo\.ycode\logs\gui-runtime.log" in captured["text"]
    assert captured["flags"] & 0x00040000  # MB_TOPMOST：黑屏时不能被空白主窗口盖住


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda: grc.warn_missing_webview2_runtime(), id="missing-runtime"),
        pytest.param(
            lambda: grc.warn_render_failure(r"C:\tmp\gui-runtime.log"),
            id="render-failure",
        ),
    ],
)
def test_warn_never_raises_when_message_box_fails(monkeypatch, invoke):
    """告警是 best-effort：弹窗失败绝不能反过来打断启动/退出路径。"""

    def _boom(*args, **kwargs):
        raise OSError("no desktop")

    monkeypatch.setattr(grc, "_show_message_box", _boom)

    invoke()


def test_show_message_box_returns_zero_off_windows(monkeypatch):
    """非 Windows 平台没有 user32，返回 0（等价用户未确认）而不是抛异常。"""
    monkeypatch.setattr(sys, "platform", "linux")

    assert grc._show_message_box("text", "title", 0x40) == 0
