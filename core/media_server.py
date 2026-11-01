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
- 兼作画布子应用的静态站点服务：``/canvas/<token>/<relpath>``，同进程共用
  端口与令牌，省掉为 GUI 内嵌页面另起一个 HTTP 服务。
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

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


# 画布子应用（frontend-canvas/dist）静态资源的扩展名 → MIME。
# .js 必须给 text/javascript：浏览器对 <script type="module"> 会校验 MIME，
# 给成 application/octet-stream 或 text/plain 会被直接拒绝执行，现象是
# "页面白屏 + 控制台一条模块脚本加载失败"，排查成本极高。
# .map 是 sourcemap，按 JSON 给即可。
_CANVAS_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8",
}
# assets/ 下的文件名带内容哈希（如 index-B1_cBLz2.js），内容不会变，可以长缓存
_CANVAS_IMMUTABLE_CACHE = "private, max-age=31536000, immutable"


def canvas_dist_root() -> str:
    """frontend-canvas/dist 的绝对路径；入口 index.html 不存在时返回空串。

    两种运行形态各有一套定位方式：
    - PyInstaller 打包后资源被摊到 sys._MEIPASS 里，跟着可执行文件走；
    - 源码 / 开发形态下本文件在 <repo>/core/，dist 在 <repo>/frontend-canvas/dist。

    返回空串而不是抛异常：发布包可能还没带子应用，此时前端按"未构建"提示渲染
    即可，不该让整个媒体服务跟着启动失败。
    """
    try:
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            root = os.path.join(meipass, "frontend-canvas", "dist")
        else:
            here = os.path.dirname(os.path.abspath(__file__))
            root = os.path.join(os.path.dirname(here), "frontend-canvas", "dist")
        if os.path.isfile(os.path.join(root, "index.html")):
            return root
    except OSError:
        logger.debug("画布子应用目录定位失败", exc_info=True)
    return ""


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
        # 画布子应用的静态资源先分流：它是多级目录结构，与 /m/ 的单段
        # "sha256.ext" 完全不同，令牌位置也不同，各自走各自的校验
        if parsed.path.startswith("/canvas/"):
            self._serve_canvas(server, parsed)
            return
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

    def _serve_canvas(self, server: MediaServer, parsed) -> None:
        """服务画布子应用（Excalidraw）的构建产物，只读、只限本机令牌。

        支持两种地址形式：
        - /canvas/<token>/<relpath>：主用形式。子应用 index.html 用相对路径引
          资源（./assets/index-xxx.js），浏览器解析后会把 ?t= 查询串丢掉，只有把
          令牌放进路径段，二级请求才不会 404。
        - /canvas/index.html?t=<token>：备用形式，方便手敲地址排查。

        dist 目录不存在（发布包未带子应用）时一律 404——那是"功能未安装"，
        不是服务故障，不写 error 日志。
        """
        try:
            root = canvas_dist_root()
            if not root:
                self._not_found()
                return
            parts = parsed.path.split("/")  # ['', 'canvas', <token 或文件名>, ...]
            if len(parts) < 3:
                self._not_found()
                return
            # 主用形式：令牌占住第 3 段，其后全部是文件相对路径（为空 → 入口）
            if server.check_token(unquote(parts[2])):
                relpath = "/".join(parts[3:])
            else:
                # 备用形式：令牌走 query
                token = (parse_qs(parsed.query).get("t") or [""])[0]
                if not server.check_token(token):
                    self._not_found()
                    return
                relpath = "/".join(parts[2:])
            rel = unquote(relpath).replace("\\", "/").strip("/") or "index.html"
            if "\x00" in rel:
                self._not_found()
                return
            root_real = os.path.realpath(root)
            # 路径穿越防护：必须拼接后 realpath 归一化再比较——".." 只有归一化
            # 之后才显形，只比字符串前缀会放过 "/dist/../secret"
            real = os.path.realpath(
                os.path.join(root_real, *[seg for seg in rel.split("/") if seg])
            )
            if real != root_real and not real.startswith(root_real + os.sep):
                self._not_found()
                return
            if not os.path.isfile(real):
                self._not_found()
                return
            size = os.path.getsize(real)
            rel_from_root = os.path.relpath(real, root_real).replace("\\", "/")
            # 入口 html 每次重建都会换掉引用的哈希文件名，必须每回都来问一次；
            # assets/ 下的文件名自带内容哈希，可以放心长缓存
            if os.path.normcase(rel_from_root) == "index.html":
                cache = "no-cache"
            elif rel_from_root.startswith("assets/"):
                cache = _CANVAS_IMMUTABLE_CACHE
            else:
                cache = "no-cache"
            headers = {
                "Content-Type": _CANVAS_MIME.get(os.path.splitext(real)[1].lower(),
                                                 "application/octet-stream"),
                "Content-Length": str(size),
                "Cache-Control": cache,
                "Accept-Ranges": "bytes",
                "X-Content-Type-Options": "nosniff",
                # 主窗口由 pywebview 自己的随机端口提供服务，与子应用不同源；
                # 这里只放行读本地静态资源，访问控制仍由令牌承担
                "Access-Control-Allow-Origin": "*",
            }
            self._send(200, headers)
            self._stream(real, 0, size)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logger.debug("画布子应用静态资源服务失败: %s", parsed.path, exc_info=True)
            self._not_found()

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
