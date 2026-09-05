"""令牌化截图预览服务（core/computer/preview_server.py）测试（计划 Task 5 / 设计 16.3）。

只打 127.0.0.1 loopback；错 token、旧 run、第四旧图与路径穿越一律 404。
"""

import os
import socket
import time
import urllib.error
import urllib.request

import pytest

from core.computer.preview_server import ComputerPreviewServer
from tests.computer_fakes import fake_png_bytes, http_status


def _http_get(url: str):
    """GET 并返回 (status, headers, body)；HTTPError 同样返回其状态与头。"""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


# ---------------- brief 样例（逐字） ----------------


def test_preview_server_requires_token_and_keeps_only_three(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    try:
        urls = [server.publish("run-a", fake_png_bytes(i)) for i in range(4)]
        assert http_status(urls[-1]) == 200
        assert http_status(urls[-1].replace(server.token, "wrong")) == 404
        assert http_status(urls[0]) == 404
        assert len(list(tmp_path.rglob("*.png"))) == 3
    finally:
        server.stop()
    assert not list(tmp_path.rglob("*.png"))


# ---------------- 绑定、响应头与内容 ----------------


def test_preview_server_binds_loopback_with_random_port(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    try:
        assert base.startswith("http://127.0.0.1:")
        assert int(base.rsplit(":", 1)[1]) > 0
        assert server.token and len(server.token) >= 32
    finally:
        server.stop()


def test_preview_server_serves_png_with_no_store_headers(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        url = server.publish("run-a", fake_png_bytes(7))
        status, headers, body = _http_get(url)
        assert status == 200
        assert headers.get("Content-Type") == "image/png"
        assert headers.get("Cache-Control") == "no-store"
        assert body == fake_png_bytes(7)
    finally:
        server.stop()


def test_preview_server_tokens_differ_between_instances(tmp_path):
    first = ComputerPreviewServer(tmp_path / "a")
    second = ComputerPreviewServer(tmp_path / "b")
    first.start()
    second.start()
    try:
        assert first.token != second.token
    finally:
        first.stop()
        second.stop()


# ---------------- 路径穿越与 run_id 边界（一律 404） ----------------


def test_preview_server_rejects_traversal_and_directory_index(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    try:
        url = server.publish("run-a", fake_png_bytes(1))
        prefix = f"{base}/computer-preview/{server.token}"
        assert http_status(f"{prefix}/../run-a/1.png") == 404
        assert http_status(f"{prefix}/%2e%2e/run-a/1.png") == 404
        assert http_status(f"{prefix}/run-a%2f..%2frun-a/1.png") == 404
        assert http_status(f"{prefix}/run-a\\../1.png") == 404
        assert http_status(f"{prefix}/run-a/") == 404
        assert http_status(f"{prefix}/") == 404
        assert http_status(f"{base}/computer-preview/") == 404
        assert http_status(f"{base}/") == 404
        assert http_status(url) == 200
    finally:
        server.stop()


def test_preview_server_rejects_invalid_run_id_and_filename_in_url(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    try:
        assert http_status(server.publish("run-a", fake_png_bytes(1))) == 200
        prefix = f"{base}/computer-preview/{server.token}"
        assert http_status(f"{prefix}/run.a/1.png") == 404
        assert http_status(f"{prefix}/run%20a/1.png") == 404
        assert http_status(f"{prefix}/{'a' * 81}/1.png") == 404
        assert http_status(f"{prefix}/run-a/index.png") == 404
        assert http_status(f"{prefix}/run-a/1.PNG") == 404
        assert http_status(f"{prefix}/run-a/1.png/extra") == 404
        assert http_status(f"{prefix}/run-b/1.png") == 404  # 未发布的 run
        assert http_status(f"{prefix}/run-a/2.png") == 404  # 未发布的序号
    finally:
        server.stop()


@pytest.mark.parametrize("run_id", ["", "run.a", "run a", "a" * 81, "run/a", "run\\a", ".."])
def test_publish_rejects_invalid_run_id(tmp_path, run_id):
    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        with pytest.raises(ValueError):
            server.publish(run_id, fake_png_bytes(1))
    finally:
        server.stop()


# ---------------- run 切换、残留清理与资源释放 ----------------


def test_switching_run_deletes_old_run_images_immediately(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        old_urls = [server.publish("run-a", fake_png_bytes(i)) for i in range(2)]
        new_url = server.publish("run-b", fake_png_bytes(9))
        assert http_status(new_url) == 200
        for url in old_urls:
            assert http_status(url) == 404
        run_dirs = [p for p in tmp_path.rglob("*") if p.is_dir() and p.name.startswith("run-")]
        assert [p.name for p in run_dirs] == ["run-b"]
    finally:
        server.stop()


def test_start_cleans_stale_directories_older_than_24_hours(tmp_path):
    stale = tmp_path / "preview-9999-deadbeef"  # 本服务进程目录命名模式的残留
    stale.mkdir()
    (stale / "leftover.png").write_bytes(fake_png_bytes(1))
    fresh = tmp_path / "preview-8888-cafef00d"  # 同模式但未超龄
    fresh.mkdir()
    old = time.time() - 25 * 60 * 60
    os.utime(stale, (old, old))
    os.utime(stale / "leftover.png", (old, old))

    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        assert not stale.exists()
        assert fresh.exists()
    finally:
        server.stop()


def test_start_keeps_stale_directories_with_foreign_names(tmp_path):
    """超过 24h 但非 preview-* 命名的目录绝不删除，避免误删并发进程/其他数据。"""
    foreign = tmp_path / "other-app-data"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("keep", encoding="utf-8")
    old = time.time() - 25 * 60 * 60
    os.utime(foreign, (old, old))
    os.utime(foreign / "keep.txt", (old, old))

    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        assert foreign.exists()
        assert (foreign / "keep.txt").read_text(encoding="utf-8") == "keep"
    finally:
        server.stop()


def test_stop_releases_port_and_is_idempotent(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    port = int(base.rsplit(":", 1)[1])
    server.stop()
    server.stop()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))  # stop 后端口必须可重新绑定


def test_publish_requires_started_server(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    with pytest.raises(RuntimeError):
        server.publish("run-a", fake_png_bytes(1))


# ---------------- token 比较加固（常量时间 + 非 ASCII 候选） ----------------


def test_preview_server_compares_token_in_constant_time(tmp_path, monkeypatch):
    """token 校验必须走 hmac.compare_digest（常量时间），而非 != 短路比较。

    预览服务绑定 loopback，时序侧信道风险低，但 token 是唯一鉴权材料，
    比较方式按加固基线钉死：用 spy 包裹 hmac.compare_digest 断言被调用。
    """
    import hmac as hmac_mod

    calls = []
    original = hmac_mod.compare_digest

    def _spy(left, right):
        calls.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac_mod, "compare_digest", _spy)
    server = ComputerPreviewServer(tmp_path)
    server.start()
    try:
        url = server.publish("run-a", fake_png_bytes(1))
        assert http_status(url.replace(server.token, "wrong-token")) == 404
        assert http_status(url) == 200
    finally:
        server.stop()
    assert (server.token, "wrong-token") in calls


def test_preview_server_rejects_non_ascii_token_with_clean_404(tmp_path):
    """原始非 ASCII 字节 token（请求行按 latin-1 解码后为非 ASCII str）必须干净 404。

    hmac.compare_digest 对非 ASCII str 抛 TypeError（会变成 500/断连），
    此测试钉住 isascii 前置守卫：坏 token 永远只表现为 404，不区分原因。
    """
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    port = int(base.rsplit(":", 1)[1])
    try:
        url = server.publish("run-a", fake_png_bytes(1))
        assert http_status(url) == 200
        request = (
            b"GET /computer-preview/\xe4\xf6/run-a/1.png HTTP/1.0\r\n"
            b"Host: 127.0.0.1\r\n\r\n"
        )
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            sock.sendall(request)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        first_line = data.split(b"\r\n", 1)[0]
        assert b" 404 " in first_line
    finally:
        server.stop()
