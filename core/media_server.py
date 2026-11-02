"""core/media_server.py
=====================
受控媒体 HTTP 服务：向 GUI 前端提供媒体仓库 Blob 的只读访问。

设计见 docs/superpowers/specs/2026-08-30-media-display-design.md。
要点：
- 仅绑定 127.0.0.1，随机端口；每次启动生成随机能力令牌，错令牌返回 404
  （不区分 401/404，避免探测）。
- 只服务 ``<sha256hex>.<白名单扩展名>`` 形式的文件名（media_store.blob_name_is_safe），
  不存在任何路径成分，配合 blob_path_for 的 realpath 越界校验双保险。
- 内容寻址不可变：Cache-Control immutable；视频拖动依赖 Range → 206/416。
- 进程内单例惰性启动（首个媒体 URL 生成时拉起），随进程销毁。
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.media_store import (
    SUPPORTED_MIME,
    MediaStoreError,
    blob_name_is_safe,
    blob_path_for,
)

logger = logging.getLogger(__name__)


def _mime_for_ext(ext: str) -> str | None:
    """扩展名（不含点）反查白名单 MIME；未登记返回 None。"""
    return next((m for m, e in SUPPORTED_MIME.items() if e.lstrip(".") == ext), None)


class MediaServer:
    """127.0.0.1 单端口媒体只读服务；线程安全，进程内共享一个实例。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._token: str = ""

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def ensure_started(self) -> bool:
        """惰性启动；返回服务是否可用。启动失败只记日志并返回 False。"""
        with self._lock:
            if self._server is not None:
                return True
            token = secrets.token_urlsafe(32)
            try:
                server = ThreadingHTTPServer(("127.0.0.1", 0), _MediaRequestHandler)
                server.daemon_threads = True
            except OSError:
                logger.warning("媒体服务启动失败（端口绑定异常）", exc_info=True)
                return False
            server.media_server_ref = self  # type: ignore[attr-defined]
            thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="media-http-server",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._token = token
            logger.info("媒体服务已启动: %s", self.base_url())
            return True

    def base_url(self) -> str:
        if self._server is None:
            return ""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def check_token(self, token: str) -> bool:
        return bool(token) and hmac.compare_digest(token, self._token)

    def access_token(self) -> str:
        """只读访问器：把本进程的能力令牌交给宿主自己拼 URL 用。

        令牌本来就写在每个媒体 URL 的 query 里（public_url 一直在用），这里补一个
        不碰下划线的入口，免得 gui_canvas_mixin 去复制令牌逻辑或直接读 _token。
        服务未启动时返回空串，调用方按"服务不可用"处理。
        """
        return self._token if self._server is not None else ""

    def public_url(self, digest: str, ext: str) -> str:
        """生成带令牌的媒体访问 URL；服务不可用时返回空串。"""
        if not self.ensure_started():
            return ""
        return f"{self.base_url()}/m/{digest}.{ext}?t={self._token}"

    def local_path_for_url(self, url: str) -> str | None:
        """把本服务签发的媒体 URL 反解回本地 Blob 绝对路径。

        "另存为"需要把原文件复制给用户，因此必须先确认 URL 确实指向本进程
        仓库里的 Blob：只接受 127.0.0.1/localhost、端口与本服务一致、令牌
        校验通过、文件名过白名单且 Blob 实际存在的地址；其余一律返回 None
        （不抛异常，由调用方给出可读提示）。
        """
        if not url or self._server is None:
            return None
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError:
            return None
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            return None
        if port != self._server.server_address[1]:
            return None
        if not self.check_token((parse_qs(parsed.query).get("t") or [""])[0]):
            return None
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[1] != "m" or not blob_name_is_safe(parts[2]):
            return None
        digest, _, ext = parts[2].partition(".")
        mime = _mime_for_ext(ext)
        if mime is None:
            return None
        try:
            path = blob_path_for(digest, mime)
        except MediaStoreError:
            return None
        return path if os.path.isfile(path) else None


# 进程内单例；gui_bridge 直接复用，保证令牌与端口全进程一致
media_server = MediaServer()


class _MediaRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "YCodeMedia/1"

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: N802 (BaseHTTPRequestHandler 约定)
        logger.debug("media http: " + fmt, *args)

    def _send(self, status: int, headers: dict, body: bytes = b"") -> None:
        try:
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _not_found(self) -> None:
        self._send(404, {"Content-Type": "text/plain; charset=utf-8",
                         "Content-Length": "0"}, b"")

    def _do(self) -> None:
        server: MediaServer = self.server.media_server_ref  # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        parts = parsed.path.split("/")
        # 只接受 /m/<digest>.<ext>
        if len(parts) != 3 or parts[1] != "m" or not blob_name_is_safe(parts[2]):
            self._not_found()
            return
        token = (parse_qs(parsed.query).get("t") or [""])[0]
        if not server.check_token(token):
            self._not_found()
            return
        blob_name = parts[2]
        digest, _, ext = blob_name.partition(".")
        mime = _mime_for_ext(ext)
        if mime is None:
            self._not_found()
            return
        try:
            path = blob_path_for(digest, mime)
            size = os.path.getsize(path)
        except (MediaStoreError, OSError):
            self._not_found()
            return

        base_headers = {
            "Content-Type": mime,
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            # 前端"复制原图"需要 fetch Blob 再写入剪贴板，跨端口请求依赖 CORS。
            # 访问控制已由随机令牌承担（错令牌 404），CORS 在此不是安全边界；
            # <img>/<video> 嵌入本就不受同源策略限制，放行读请求不扩大暴露面。
            "Access-Control-Allow-Origin": "*",
        }

        range_header = self.headers.get("Range") or ""
        if range_header.startswith("bytes="):
            start, end = _parse_range(range_header[len("bytes="):], size)
            if start is None:
                self._send(416, {
                    "Content-Range": f"bytes */{size}",
                    "Content-Length": "0",
                })
                return
            length = end - start + 1
            headers = dict(base_headers)
            headers.update({
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(length),
            })
            self._send(206, headers)
            self._stream(path, start, length)
            return

        headers = dict(base_headers)
        headers["Content-Length"] = str(size)
        self._send(200, headers)
        self._stream(path, 0, size)

    def _stream(self, path: str, start: int, length: int) -> None:
        if self.command == "HEAD" or length <= 0:
            return
        chunk = 256 * 1024
        try:
            with open(path, "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining > 0:
                    data = handle.read(min(chunk, remaining))
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端拖动进度条会频繁中断旧 Range 连接，属正常现象
            pass

    do_GET = _do
    do_HEAD = _do


def _parse_range(spec: str, size: int):
    """解析 bytes=a-b 单区间；合法返回 (start, end) 闭区间，否则 (None, None)。"""
    try:
        first, _, last = spec.partition("-")
        first = first.strip()
        last = last.strip()
        if not first:
            # bytes=-N：末尾 N 字节
            suffix = int(last)
            if suffix <= 0 or size == 0:
                return None, None
            start = max(0, size - suffix)
            return start, size - 1
        start = int(first)
        if start >= size:
            return None, None
        if not last:
            return start, size - 1
        end = int(last)
        return start, min(end, size - 1)
    except (ValueError, TypeError):
        return None, None
