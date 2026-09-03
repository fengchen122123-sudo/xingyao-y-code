"""loopback 令牌化截图预览服务（设计 16.3）。

不变量：只绑定 127.0.0.1；每进程随机 token；只服务当前 run 最近 3 张 PNG；
URL 路径固定 /computer-preview/{token}/{run_id}/{ordinal}.png；错 token、
路径穿越、目录索引一律 404（不区分原因）。publish 用临时文件 + os.replace；
切换 run 立即删除旧 run 图片；start 只清理 root 下 24 小时前的本服务进程目录
（preview-* 命名），绝不误删其他数据；stop 幂等，停止后删除本进程目录并释放端口。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_FILENAME_RE = re.compile(r"^[0-9]+\.png$")
_KEEP_PNG_COUNT = 3
_STALE_DIR_MAX_AGE_SECONDS = 24 * 60 * 60
_URL_PREFIX = "computer-preview"
_PROCESS_DIR_PREFIX = "preview-"


class ComputerPreviewServer:
    """单进程预览服务；root 下按进程隔离目录，stop 时删除本进程目录。"""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._process_dir = self._root / f"{_PROCESS_DIR_PREFIX}{os.getpid()}-{secrets.token_hex(4)}"
        self._process_dir_resolved: Path | None = None
        self.token = ""
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._current_run_id = ""
        self._ordinal = 0

    def start(self) -> str:
        with self._lock:
            if self._server is not None:
                return self._base_url_locked()
            self._root.mkdir(parents=True, exist_ok=True)
            self._cleanup_stale_dirs_locked()
            self._process_dir.mkdir(parents=True, exist_ok=True)
            self._process_dir_resolved = self._process_dir.resolve()
            self.token = secrets.token_urlsafe(32)
            handler_type = self._build_handler()
            self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
            self._server.preview_server = self  # type: ignore[attr-defined]
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                name="computer-preview-http",
                daemon=True,
            )
            self._thread.start()
            return self._base_url_locked()

    def publish(self, run_id: str, png: bytes) -> str:
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError(f"非法 run_id：{run_id!r}")
        with self._lock:
            if self._server is None:
                raise RuntimeError("预览服务未启动")
            if run_id != self._current_run_id:
                if self._current_run_id:
                    shutil.rmtree(self._run_dir(self._current_run_id), ignore_errors=True)
                self._current_run_id = run_id
                self._ordinal = 0
            self._ordinal += 1
            ordinal = self._ordinal
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            temp_path = run_dir / f".{ordinal}.png.tmp"
            final_path = run_dir / f"{ordinal}.png"
            temp_path.write_bytes(png)
            os.replace(temp_path, final_path)
            self._prune_run_locked(run_dir)
            return f"{self._base_url_locked()}/{_URL_PREFIX}/{self.token}/{run_id}/{ordinal}.png"

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._current_run_id = ""
            self._ordinal = 0
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=10)
        shutil.rmtree(self._process_dir, ignore_errors=True)

    # ---------------- 内部 ----------------

    def _base_url_locked(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def _run_dir(self, run_id: str) -> Path:
        return self._process_dir / run_id

    def _prune_run_locked(self, run_dir: Path) -> None:
        ordinals = sorted(
            (int(path.stem) for path in run_dir.glob("*.png") if path.stem.isdigit()),
            reverse=True,
        )
        for ordinal in ordinals[_KEEP_PNG_COUNT:]:
            (run_dir / f"{ordinal}.png").unlink(missing_ok=True)

    def _cleanup_stale_dirs_locked(self) -> None:
        cutoff = time.time() - _STALE_DIR_MAX_AGE_SECONDS
        for child in self._root.iterdir():
            if not child.is_dir() or child == self._process_dir:
                continue
            if not child.name.startswith(_PROCESS_DIR_PREFIX):
                continue  # 只清理本服务的进程目录，绝不误删并发进程或其他数据
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)

    def _resolve_get(self, raw_path: str) -> bytes | None:
        """严格匹配固定 URL 形状；任何不匹配（含错 token/穿越/索引）返回 None→404。"""
        segments = urlsplit(raw_path).path.split("/")
        if len(segments) != 5 or segments[0] != "" or segments[1] != _URL_PREFIX:
            return None
        token, run_id, filename = segments[2], segments[3], segments[4]
        if not self.token or token != self.token:
            return None
        if not _RUN_ID_RE.fullmatch(run_id):
            return None
        if not _FILENAME_RE.fullmatch(filename):
            return None
        root = self._process_dir_resolved
        if root is None:
            return None
        file_path = self._run_dir(run_id) / filename
        try:
            resolved = file_path.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(root):
            return None
        try:
            if not resolved.is_file():
                return None
            return resolved.read_bytes()
        except OSError:
            return None

    def _build_handler(self):
        class _PreviewHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                preview = self.server.preview_server
                payload = preview._resolve_get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib 签名
                return  # 预览服务不往 stderr 写访问日志

        return _PreviewHandler
