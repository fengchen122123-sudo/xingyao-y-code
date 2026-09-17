"""GUI 启动参数回归：必须关闭 pywebview 私有模式。

背景现象：侧栏会话分组收起后，重启应用又全部展开。
根因：pywebview 的 `private_mode` 默认 True —— WebView2 不保留 localStorage，
且内置 HTTP 服务会退化为随机端口（`server.port = http_port or _get_random_port()`），
每次启动前端 origin 都不同。两者叠加导致所有 localStorage 界面偏好
（侧栏宽度、输入框高度、分组折叠态）重启必丢。
"""
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
