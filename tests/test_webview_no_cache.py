import mimetypes
import threading
import time
import urllib.request
import wsgiref.simple_server

import bottle
import pytest

from core.webview_no_cache import apply


def _make_pywebview_like_app(dist_root):
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


def test_patched_server_overrides_windows_js_mime(dist_root, monkeypatch):
    apply()
    # Windows 注册表 HKCR\.js\Content Type=text/plain 会进入 mimetypes 映射。
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    apply()

    server = _serve_once(_make_pywebview_like_app(dist_root))
    try:
        status, headers, body = _get_headers(server, "/assets/index-ABC123.js")
        assert status == 200
        assert headers["Content-Type"] == "text/javascript; charset=UTF-8"
        assert body == b"console.log(1)"
    finally:
        server.shutdown()
