"""pywebview 内置静态服务器必须真正发出 no-cache 响应头。

背景现象（2026-09-19）：双击桌面快捷方式启动打包版后整窗白屏，无任何 JS 报错。
根因链：
  1. pywebview 6.2.1 的 webview/http.py 在 asset 路由里先
     ``bottle.response.set_header('Cache-Control', 'no-cache, ...')`` 再
     ``return bottle.static_file(...)``；
  2. bottle 的 static_file 返回全新 HTTPResponse，路由返回 HTTPResponse 时，
     此前设在 thread-local response 上的头被整体丢弃——no-cache 从未真正发出
     （实测响应只有 Last-Modified/Etag）；
  3. WebView2 按启发式缓存 index.html，而 pywebview 每次启动随机分配端口、
     网站数据目录固定（gui_main._webview_start_options 的 storage_path），
     同一端口被旧前端构建用过就会命中过期 index.html；
  4. 浏览器拿旧哈希资产名请求新 dist → 404 → 主 bundle 不加载 →
     #app 为空 → 白屏。
修复：在 pywebview 服务器开始服务前用 core.webview_no_cache.apply() 包一层
bottle.static_file，把缓存控制头直接补到返回的 HTTPResponse 上。
"""
import threading
import time
import urllib.request
import wsgiref.simple_server

import bottle
import pytest

from core.webview_no_cache import apply


def _make_pywebview_like_app(dist_root):
    """1:1 复刻 pywebview webview/http.py 的 asset 路由写法。"""
    app = bottle.Bottle()

    @app.route("/")
    @app.route("/<file:path>")
    def asset(file):
        bottle.response.set_header("Cache-Control", "no-cache, no-store, must-revalidate")
        bottle.response.set_header("Pragma", "no-cache")
        bottle.response.set_header("Expires", 0)
        return bottle.static_file(file, root=dist_root)

    return app


def _serve_once(app):
    server = wsgiref.simple_server.make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    return server


@pytest.fixture()
def dist_root(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(
        '<!doctype html><div id="app"></div>'
        '<script src="/assets/index-ABC123.js"></script>',
        encoding="utf-8",
    )
    (root / "assets" / "index-ABC123.js").write_text("console.log(1)", encoding="utf-8")
    return str(root)


def _get_headers(server, path):
    port = server.server_address[1]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
        return resp.status, dict(resp.headers), resp.read()


def test_patched_server_sends_no_cache_headers(dist_root):
    server = _serve_once(_make_pywebview_like_app(dist_root))
    try:
        apply()

        status, headers, body = _get_headers(server, "/index.html")
        assert status == 200
        # 修复点：no-cache 真正到达响应（pywebview 原写法会被 bottle 丢弃）
        assert headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
        assert b"index-ABC123.js" in body

        _, asset_headers, asset_body = _get_headers(server, "/assets/index-ABC123.js")
        assert asset_headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
        assert asset_body == b"console.log(1)"
    finally:
        server.shutdown()


def test_apply_is_idempotent(dist_root):
    apply()
    apply()

    server = _serve_once(_make_pywebview_like_app(dist_root))
    try:
        _, headers, _ = _get_headers(server, "/index.html")
        assert headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
    finally:
        server.shutdown()
