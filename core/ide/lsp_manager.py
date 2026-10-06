# -*- coding: utf-8 -*-
"""LSP 语言服务器进程与工作区生命周期管理。

职责边界：
- 只管进程与 JSON-RPC，不懂任何具体语言（语言配置在 language_registry）；
- 可执行文件定位交给 server_provision，本模块只接收完整 argv；
- 所有要送到前端的通知都通过 on_notification 回调交给 gui_bridge，由后者
  走 emit_event 进 _js_queue，本模块内不与 WebView 通信；
- 读线程是唯一的 recv 方，写侧由进程内锁串行化：LSP 的 stdio 是单写者模型，
  两个线程同时写同一个 Content-Length 帧会让服务器解析错位。
"""
from __future__ import annotations

import copy
import logging
import os
import subprocess
import threading
import time

from core.ide.lsp_codec import FrameDecoder, LspFrameError, encode_frame

logger = logging.getLogger(__name__)


class LspError(Exception):
    """LSP 层错误基类，消息可直接展示给用户。"""


class LspStartError(LspError):
    """进程启动失败（可执行文件不存在 / 权限不足）。"""


class LspHandshakeError(LspError):
    """initialize 握手失败或超时。"""


class LspNotRunning(LspError):
    """服务器进程已退出，无法处理请求。"""


class LspRequestError(LspError):
    """服务器返回了错误响应，或请求超时。"""

    def __init__(self, code, message, data=None):
        super().__init__(f"[{code}] {message}")
        self.code = int(code)
        self.message = str(message)
        self.data = data


DEFAULT_CLIENT_CAPABILITIES = {
    "textDocument": {
        "synchronization": {"didSave": True, "dynamicRegistration": False},
        "publishDiagnostics": {"relatedInformation": True},
        "completion": {
            "completionItem": {
                "snippetSupport": False,
                "documentationFormat": ["markdown", "plaintext"],
            }
        },
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "definition": {"linkSupport": False},
        "formatting": {"dynamicRegistration": False},
        "rename": {"prepareSupport": False},
    },
    "workspace": {"workspaceFolders": True, "configuration": True},
    "window": {"workDoneProgress": False},
}

# 服务器反向请求里我们真正实现的少数几个，其余一律回 method not found
_ACK_ONLY_METHODS = frozenset({
    "window/workDoneProgress/create",
    "client/registerCapability",
    "client/unregisterCapability",
})

# Windows 上 npm 全局命令的真实形态是 .cmd/.bat shim（PATHEXT 命中），
# 这类文件不能直接当 argv[0] 交给 CreateProcess：必须用 cmd.exe /c 包一层。
_WINDOWS_SCRIPT_SUFFIXES = (".cmd", ".bat")

_READ_CHUNK = 4096


def path_to_uri(path: str) -> str:
    """本地路径转 file:// URI；Windows 盘符写成 file:///E:/... 形式。"""
    from pathlib import Path

    return Path(os.path.abspath(path)).as_uri()


def uri_to_path(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri or "")
    if parsed.scheme != "file":
        return ""
    raw = unquote(parsed.path)
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return os.path.normpath(raw)


def wrap_command(command) -> list[str]:
    """把 argv 调整成真正能被 Popen 执行的形式。

    选择「启动时包装」而不是「把 .cmd/.bat 从候选里排除」：Windows 上
    `npm i -g typescript-language-server` 这类安装只会生成 .cmd shim（PATHEXT
    命中），排除掉就等于在 Windows 上永远发现不了这些服务器。所以候选照旧保留，
    在这里用 cmd.exe /c 包一层。
    """
    argv = [str(c) for c in command]
    if os.name == "nt" and argv and argv[0].lower().endswith(_WINDOWS_SCRIPT_SUFFIXES):
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/c", *argv]
    return argv


def _read_chunk(stream, size: int = _READ_CHUNK) -> bytes:
    """从管道读「当前可用」的一段字节。

    必须用 `read1`：`BufferedReader.read(n)` 会一直等到攒满 n 字节或 EOF 才返回，
    而语言服务器的单条响应往往只有几百字节、进程又长期不退出，用 read(n) 会让
    握手直接挂死（实测：子进程写 5 字节后存活 3 秒，`stdout.read(4096)` 1.5 秒内
    不返回）。read1 只发一次系统调用，拿到多少返回多少；返回空串只表示 EOF。
    """
    read1 = getattr(stream, "read1", None)
    if read1 is not None:
        return read1(size)
    return os.read(stream.fileno(), size)


class LspProcess:
    """单个语言服务器进程：stdio + Content-Length 帧 + 请求表。"""

    def __init__(
        self,
        command,
        *,
        cwd,
        name="",
        initialization_options=None,
        client_capabilities=None,
        on_notification=None,
        on_exit=None,
        initialize_timeout=20.0,
        request_timeout=15.0,
        shutdown_timeout=5.0,
        env=None,
    ):
        self.command = [str(c) for c in command]
        self.cwd = cwd
        self.name = name or (os.path.basename(self.command[0]) if self.command else "lsp")
        # 深拷贝：调用方通常直接传 language_registry 的 SPECS[*].initialization_options，
        # 那是跨工作区共享的可变单例（frozen=True 只挡字段重绑定，嵌套 dict 仍是同一个
        # 对象）。浅拷贝会让一个工作区注入的 extraPaths 泄漏到另一个工作区。
        self.initialization_options = copy.deepcopy(dict(initialization_options or {}))
        self.client_capabilities = client_capabilities or DEFAULT_CLIENT_CAPABILITIES
        self.initialize_timeout = initialize_timeout
        self.request_timeout = request_timeout
        self.shutdown_timeout = shutdown_timeout
        self._env = env
        self._on_notification = on_notification
        self._on_exit = on_exit

        self._proc: subprocess.Popen | None = None
        self._decoder = FrameDecoder()
        self._write_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = False
        self.server_capabilities: dict = {}
        self.exit_code: int | None = None
        self.stderr_tail: list[str] = []

    # ---------------- 生命周期 ----------------

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        if self._proc is not None:
            return
        argv = wrap_command(self.command)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            self._proc = subprocess.Popen(
                argv,
                cwd=self.cwd or None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
                env=self._env,
            )
        except OSError as exc:
            raise LspStartError(f"无法启动语言服务器 {self.name}：{exc}") from exc
        self._reader = threading.Thread(
            target=self._read_loop, name=f"lsp-read-{self.name}", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, name=f"lsp-stderr-{self.name}", daemon=True
        )
        self._stderr_reader.start()

    def update_initialization_options(self, overrides: dict) -> dict:
        """把工作区相关的初始化选项深合并进本进程自己的副本。

        只允许改 `self.initialization_options`（构造时已深拷贝），
        绝不允许就地写 language_registry 的共享单例。
        """
        def _merge(target, extra):
            for key, value in (extra or {}).items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    _merge(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)

        _merge(self.initialization_options, overrides)
        return self.initialization_options

    def initialize(self, root_path: str) -> dict:
        root_uri = path_to_uri(root_path)
        params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "ycode-ide", "version": "1"},
            "rootUri": root_uri,
            "rootPath": root_path,
            "workspaceFolders": [
                {"uri": root_uri, "name": os.path.basename(root_path.rstrip("\\/")) or root_path}
            ],
            "capabilities": self.client_capabilities,
            "initializationOptions": self.initialization_options,
        }
        try:
            result = self.request("initialize", params, timeout=self.initialize_timeout)
        except LspError as exc:
            raise LspHandshakeError(f"语言服务器 {self.name} 握手失败：{exc}") from exc
        self.server_capabilities = (result or {}).get("capabilities", {}) or {}
        self.notify("initialized", {})
        return result or {}

    def shutdown(self, *, grace=None) -> None:
        """优雅退出：shutdown 请求 + exit 通知，超时强杀。"""
        grace = self.shutdown_timeout if grace is None else grace
        proc = self._proc
        if proc is None:
            return
        # 先立旗标：读线程据此区分「主动关闭」与「崩溃」，避免触发重启
        self._closed = True
        if proc.poll() is None:
            try:
                self.request("shutdown", {}, timeout=grace)
            except LspError:
                pass
            try:
                self.notify("exit", {})
            except LspError:
                pass
        if not self._wait(grace):
            self._kill()
        self._join_threads()

    def kill(self) -> None:
        self._closed = True
        self._kill()
        self._join_threads()

    # ---------------- JSON-RPC ----------------

    def request(self, method, params=None, *, timeout=None):
        if not self.is_alive():
            raise LspNotRunning(f"语言服务器 {self.name} 未运行")
        with self._id_lock:
            req_id = self._next_id
            self._next_id += 1
        entry = {"event": threading.Event(), "result": None}
        with self._pending_lock:
            self._pending[req_id] = entry
        payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            self._send(payload)
        except Exception:
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise
        wait_time = self.request_timeout if timeout is None else timeout
        if not entry["event"].wait(wait_time):
            with self._pending_lock:
                self._pending.pop(req_id, None)
            raise LspRequestError(-32001, f"请求 {method} 超时（{wait_time}s）")
        response = entry["result"] or {}
        if "error" in response:
            err = response["error"] or {}
            raise LspRequestError(
                err.get("code", -32603), err.get("message", "未知错误"), err.get("data")
            )
        return response.get("result")

    def notify(self, method, params=None) -> None:
        if not self.is_alive():
            raise LspNotRunning(f"语言服务器 {self.name} 未运行")
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _send(self, payload) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise LspNotRunning(f"语言服务器 {self.name} 未启动")
        data = encode_frame(payload)
        with self._write_lock:
            if proc.poll() is not None:
                raise LspNotRunning(f"语言服务器 {self.name} 已退出（exit={proc.returncode}）")
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LspNotRunning(f"语言服务器 {self.name} 写入失败：{exc}") from exc

    # ---------------- 读线程 ----------------

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        broken_frame = False
        try:
            while True:
                chunk = _read_chunk(proc.stdout)
                if not chunk:
                    break
                try:
                    messages = self._decoder.feed(chunk)
                except LspFrameError as exc:
                    broken_frame = True
                    # 坏帧之前同一次 feed 里已经解出来的消息必须先派发：其中很可能
                    # 正是 initialize 响应，直接丢掉会让上层的等待永远得不到应答。
                    for msg in exc.messages:
                        self._dispatch(msg)
                    logger.warning("语言服务器 %s 帧解析失败：%s", self.name, exc)
                    break
                for msg in messages:
                    self._dispatch(msg)
        except (ValueError, OSError):
            pass
        finally:
            if broken_frame:
                # 帧结构坏了就无法重新对齐，这条连接不再可用：直接收掉进程，
                # 避免留下「进程活着但没人读」的僵尸服务器（上层会看到非预期退出）。
                self._kill()
            self._on_reader_end()

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self.stderr_tail.append(line)
                    del self.stderr_tail[:-20]
        except (ValueError, OSError):
            return

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        msg_id = msg.get("id")
        if msg_id is not None and ("result" in msg or "error" in msg):
            with self._pending_lock:
                entry = self._pending.pop(msg_id, None)
            if entry is not None:
                entry["result"] = msg
                entry["event"].set()
            return
        method = msg.get("method")
        if not isinstance(method, str):
            return
        if msg_id is not None:
            self._handle_server_request(msg_id, method, msg.get("params") or {})
            return
        if self._on_notification is not None:
            try:
                self._on_notification(self, method, msg.get("params") or {})
            except Exception:
                # 通知处理出问题不允许打断读线程，否则整条连接会静默死掉
                logger.exception("语言服务器 %s 通知处理失败", self.name)

    def _handle_server_request(self, msg_id, method, params) -> None:
        if method == "workspace/configuration":
            items = params.get("items") or []
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": [{} for _ in items]})
            return
        if method in _ACK_ONLY_METHODS:
            self._send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            return
        self._send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not supported: {method}"},
        })

    def _on_reader_end(self) -> None:
        self.exit_code = self._reap()
        with self._pending_lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for entry in pendings:
            entry["result"] = {
                "error": {"code": -32002, "message": f"语言服务器 {self.name} 已退出"}
            }
            entry["event"].set()
        if self._on_exit is not None:
            try:
                self._on_exit(self, self.exit_code, self._closed)
            except Exception:
                logger.exception("LSP 退出回调失败")

    def _reap(self, timeout=0.5):
        """回收进程并返回退出码。

        读线程看到 EOF 时子进程可能还没被 waitpid 收走（EOF 早于回收），
        此时 `poll()` 会返回 None、`returncode` 还是 None；不稍等一下就会把
        「正常退出 0」记成「退出码未知」，退出回调与崩溃判定都会失真。
        """
        proc = self._proc
        if proc is None:
            return None
        try:
            proc.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return proc.returncode

    # ---------------- 进程收尾 ----------------

    def _wait(self, timeout) -> bool:
        proc = self._proc
        if proc is None:
            return True
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        self.exit_code = proc.returncode
        return True

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.error("语言服务器 %s 强杀后仍未退出", self.name)
        self.exit_code = proc.returncode

    def _join_threads(self) -> None:
        for thread in (self._reader, self._stderr_reader):
            if thread is not None and thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)
