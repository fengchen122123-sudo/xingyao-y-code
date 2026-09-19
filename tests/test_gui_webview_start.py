"""GUI 启动参数回归：必须关闭 pywebview 私有模式。

背景现象：侧栏会话分组收起后，重启应用又全部展开。
根因：pywebview 的 `private_mode` 默认 True —— WebView2 不保留 localStorage，
且内置 HTTP 服务会退化为随机端口（`server.port = http_port or _get_random_port()`），
每次启动前端 origin 都不同。两者叠加导致所有 localStorage 界面偏好
（侧栏宽度、输入框高度、分组折叠态）重启必丢。

2026-09-19 补充：前端 HTTP 端口还必须按启动形态隔离（安装版 42001 /
源码开发版 42002）。两形态共用同一 WebView2 数据目录，origin 相同则共享
HTTP 缓存——未输入 no-cache 头的旧安装版会把过期 index.html 留给后续启动，
开发版命中后轻则加载到安装版旧前端（新功能"消失"），重则旧哈希资产 404
整窗白屏（当日实测复现三次）。
"""
import sys

import gui_main


def test_webview_start_disables_private_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path))

    options = gui_main._webview_start_options()

    # 私有模式关闭后 WebView2 才会把 localStorage 落到磁盘
    assert options["private_mode"] is False
    # 网站数据落在应用自己的数据目录，不污染系统级 EBWebView 共享目录
    assert options["storage_path"] == str(tmp_path / "webview")
    # 开发者工具开关仍由环境变量控制，不能被顺手改掉
    assert isinstance(options["debug"], bool)
    # 启动参数必须携带按形态固定的前端端口
    assert options["http_port"] == gui_main._frontend_http_port()


def test_frontend_http_port_frozen_uses_default(monkeypatch):
    # 安装版（PyInstaller frozen）沿用 pywebview 默认端口，终端用户
    # localStorage 偏好保持同一 origin，不因升级丢失
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert gui_main._frontend_http_port() == gui_main._HTTP_PORT_FROZEN


def test_frontend_http_port_source_isolated_from_frozen(monkeypatch):
    # 源码开发版必须独占端口：与安装版前端 origin 隔离，缓存互不可见
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert gui_main._frontend_http_port() == gui_main._HTTP_PORT_SOURCE
    assert gui_main._HTTP_PORT_SOURCE != gui_main._HTTP_PORT_FROZEN
