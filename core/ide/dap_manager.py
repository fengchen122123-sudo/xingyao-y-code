"""DAP（Debug Adapter Protocol）帧编解码与适配器进程托管。

帧格式与 LSP 一致（ASCII 头 + Content-Length + CRLF CRLF + UTF-8 JSON 正文），
但载荷字段完全不同：DAP 用 seq / type(request|response|event) / command|event
/ arguments|body，LSP 用 jsonrpc / id / method / params。因此不复用 LSP 侧代码，
本模块自带解析器。

不过解析**语义**必须与 `core/ide/lsp_codec.py` 一致（S2-T3 踩过的坑）：stdio 是
字节流，一次 read 可能拿到半帧（分片）或多帧（粘包），解析器必须是有状态的累加器；
且一个 chunk 完全可能是 `[合法帧 A][坏帧 B]`，A 很可能正是上层正在等的响应——
坏帧抛错时不得把 A 一起丢掉，因此 `DapProtocolError.messages` 挂着本次 feed 里
已经成功解码的报文，调用方应当先派发它们，再决定是否放弃这条连接。

`Content-Length` 是外部输入，等于攻击面：巨大的数值会让读线程一直等，或者按声明
长度预分配缓冲直接吃爆内存。因此头部长度与单帧正文长度都有硬上限，越界立刻抛
`DapProtocolError`（既不等待也不分配）。

线程模型（本模块所有类都不触碰 WebView）：
- DapProcess 的读线程负责把字节流还原成报文，response 唤醒等待者，event 交给
  on_event 回调；on_event 的实现只允许 emit_event（内部 put_nowait 进 _js_queue）。
- 读线程是唯一的接收方，写侧由 `_write_lock` 串行化（DAP 的 stdio 是单写者模型：
  两个线程同时写同一个 Content-Length 帧会让适配器解析错位）。
- **读线程内绝不允许调用任何同步等待响应的请求**（`send_request`）——那会与自己
  的读循环死锁。需要「收到事件后回请求」（如 stopped 后取栈）时，必须投递到别的
  线程，或改用 `send_request_async` + 在别处 `await_response`（Task 4 的会话层）。

请求/响应配对契约（与 S2 的 LspProcess 同口径）：
- 配对只认 `request_seq`，不认到达顺序；
- 超时：把 waiter 从待答表 pop 掉再抛 `DapRequestError`，迟到的响应命中「没有
  waiter」时静默丢弃并留 debug 日志（排障要能区分「适配器没回」与「回了但已超时」）；
- 通道断开（EOF / 坏帧 / terminate）：等待者全部唤醒，`DapRequestError` 里带上
  断开原因，绝不留下永久阻塞的 `send_request`。
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional

from core.ide.debug_adapters import TCP
from core.process_io import decode_process_output

logger = logging.getLogger(__name__)

# 帧头分隔符：CRLF CRLF
_HEADER_SEPARATOR = b"\r\n\r\n"
# 帧头上限（16 KB）：迟迟等不到空行的「头」必然是畸形或攻击流量，无界缓冲会被撑爆
_MAX_HEADER_BYTES = 16 * 1024
# 单帧上限：variables 响应可能很大，给 32MB；超过即判协议错，
# 防止畸形 Content-Length 让内存被撑爆
MAX_FRAME_BYTES = 32 * 1024 * 1024


class DapProtocolError(Exception):
    """DAP 帧解析或通道状态异常（头不完整、Content-Length 非法、帧超限、通道关闭）。

    异常上还挂着 `messages`：**抛出本次异常之前，同一次 feed 里已经成功解码的消息**。
    调用方捕获后必须先把 `messages` 派发出去，再决定是否放弃这条连接——否则上层
    会一直等一个永远不会被投递的响应（挂起），而不是看到错误。
    """

    def __init__(self, message, messages=None):
        super().__init__(message)
        self.messages = list(messages or ())


def encode_message(message: Dict[str, Any]) -> bytes:
    """把一条 DAP 报文编码成 Content-Length 帧（UTF-8 载荷，紧分隔符省字节）。"""
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class DapFramer:
    """增量帧解析器：把任意切分的字节流还原成 DAP 报文列表。"""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buffer = bytearray()
        self._max_frame_bytes = max_frame_bytes

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        """喂入一段字节，返回本次新增的完整报文（可能为空列表）。

        坏帧抛 `DapProtocolError`，异常上的 `messages` 是本次 feed 里已经解出来的
        报文（见 `DapProtocolError` 说明）。
        """
        if data:
            self._buffer.extend(data)
        messages: List[Dict[str, Any]] = []
        try:
            while True:
                header_end = self._buffer.find(_HEADER_SEPARATOR)
                if header_end < 0:
                    if len(self._buffer) > _MAX_HEADER_BYTES:
                        raise DapProtocolError(
                            f"DAP 帧头超过 {_MAX_HEADER_BYTES} 字节仍未见空行，帧结构异常"
                        )
                    return messages
                if header_end > _MAX_HEADER_BYTES:
                    raise DapProtocolError(
                        f"DAP 帧头超过 {_MAX_HEADER_BYTES} 字节，帧结构异常"
                    )
                length = self._parse_content_length(bytes(self._buffer[:header_end]))
                body_start = header_end + len(_HEADER_SEPARATOR)
                if length > self._max_frame_bytes:
                    # 读到头就拒绝：不等正文、不预分配
                    raise DapProtocolError(f"DAP 帧超过上限：{length} 字节")
                if len(self._buffer) < body_start + length:
                    return messages  # 正文未到齐：保留缓冲，等下一次 feed
                body = bytes(self._buffer[body_start:body_start + length])
                del self._buffer[:body_start + length]
                messages.append(self._decode_body(body))
        except DapProtocolError as exc:
            # 坏帧之前已经解出来的报文不能随异常一起消失（见 DapProtocolError 说明）
            if not exc.messages:
                exc.messages = list(messages)
            raise

    @staticmethod
    def _parse_content_length(header: bytes) -> int:
        for line in header.split(b"\r\n"):
            if not line:
                continue
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                try:
                    length = int(value.strip())
                except ValueError as e:
                    raise DapProtocolError(f"Content-Length 不是整数：{value!r}") from e
                if length < 0:
                    raise DapProtocolError(f"Content-Length 为负：{length}")
                return length
        raise DapProtocolError(f"DAP 帧头缺少 Content-Length：{header[:64]!r}")

    @staticmethod
    def _decode_body(body: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise DapProtocolError(f"DAP 载荷不是合法 JSON：{e}") from e
        if not isinstance(payload, dict):
            raise DapProtocolError("DAP 载荷必须是 JSON 对象")
        return payload

    @property
    def pending_bytes(self) -> int:
        """缓冲中尚未成帧的字节数（诊断与测试断言用）。"""
        return len(self._buffer)

# =====================================================================================
# 适配器进程托管（S3-T3）
# =====================================================================================

DEFAULT_REQUEST_TIMEOUT = 10.0
# dlv dap 只支持 TCP：进程启动后把随机端口打进 stderr，等端口的超时
TCP_PORT_TIMEOUT = 15.0
# dlv 打印监听地址的措辞可能随版本变化，用宽松正则只抓端口
_TCP_PORT_RE = re.compile(rb"127\.0\.0\.1:(\d+)")
# stderr 尾部缓冲：端口那行可能被切在两次 read 之间，按尾部若干字节做正则匹配
_STDERR_TAIL_BYTES = 8192
# 「抢跑响应」暂存上限：读线程可能先处理到响应、请求登记随后才发生（见 await_response）。
# 这不是正常协议的产物，所以给一个硬上限，免得畸形适配器把内存堆满。
_MAX_EARLY_RESPONSES = 32
_READ_CHUNK_BYTES = 4096


class DapRequestError(DapProtocolError):
    """请求失败：超时、适配器返回 success=false、或通道已断开。"""


class DapReaderBlockingError(DapProtocolError):
    """在读线程内发起「同步等待响应」的请求（必然与自己的读循环死锁）。

    这是运行时护栏（S3-T4 补）：铁律「适配器读线程内禁止调用任何同步等待响应的 DAP
    请求」此前只有 docstring 约定，写出违规代码的后果是整条会话静默挂死（读线程在等
    自己写出去的那条响应），既没有报错也没有超时。现在 `send_request` / `await_response`
    只要在读线程里被调用就立刻抛本异常。

    事件回调（`on_event`）内需要「收到事件后回请求」（如 stopped 后取栈）时只能二选一：
    - 投递到别的线程执行（Task 4 的 `_handle_stopped` 走这条）；
    - `send_request_async` 发出去，在别处 `await_response` 配对。

    本异常从 `_dispatch` 的事件回调里冒出来时不会被「回调异常不打断读线程」的兜底吞掉
    （见 `_dispatch`）：它会结束读线程并唤醒所有等待者——宁可让会话明确失败，也不留一个
    「进程活着但没人读」的半死通道。
    """


def _spawn_adapter(command: List[str], cwd: str):
    """启动适配器进程：stdin/stdout/stderr 全接管（dlv 从 stderr 报端口）。"""
    return subprocess.Popen(
        command,
        cwd=cwd or None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _read_chunk(stream, size: int = _READ_CHUNK_BYTES) -> bytes:
    """从管道/套接字读「当前可用」的一段字节，返回空串只表示 EOF。

    优先 `read1`：`BufferedReader.read(n)` 会一直等攒满 n 字节或 EOF，而适配器的单条
    报文往往只有几百字节、进程又长期不退出，用 read(n) 会把整条通道卡死（S2 的
    lsp_manager._read_chunk 踩过同一个坑）。`_spawn_adapter` 用 bufsize=0，标准库给的
    本就是 raw 流（一次系统调用即返回），read1 只是对「调用方自建 spawn」的兜底。
    """
    read1 = getattr(stream, "read1", None)
    if read1 is not None:
        return read1(size)
    return stream.read(size)


def _write_all(stream, data: bytes) -> None:
    """整帧写净：管道/套接字都可能部分写，循环到写完（帧绝不可被截断）。"""
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if written is None:
            written = len(view)
        if written <= 0:
            raise DapProtocolError("调试适配器输入通道已关闭")
        view = view[written:]
    flush = getattr(stream, "flush", None)
    if flush is not None:
        flush()


def _kill_process_tree(proc) -> None:
    """强杀适配器进程树（范式同 core/git/runner.py 的 _kill_process_tree）。

    dlv 之类的适配器会再拉出被调试程序，只 kill 适配器本身会留下孤儿进程。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            import signal

            proc.kill()
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        # 关闭动作吞掉一切异常（与在飞流中断同一铁律）
        pass


class DapProcess:
    """一个调试适配器进程的 DAP 通道（stdio 或 TCP）。

    生命周期：`start()` → `send_request*` / `await_response` → `terminate()`。
    `terminate()` 幂等、可从任意线程调用（包括读线程自己），负责关通道、回收进程树、
    join 掉读线程与 stderr 线程，并把所有等待者唤醒。
    """

    def __init__(
        self,
        spec,
        cwd: str,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_stderr: Optional[Callable[[str], None]] = None,
        spawn: Optional[Callable[..., Any]] = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ):
        self._spec = spec
        self._cwd = cwd
        self._on_event = on_event
        self._on_stderr = on_stderr
        self._spawn = spawn or _spawn_adapter
        self._request_timeout = request_timeout
        self._proc = None
        self._sock = None
        self._reader_source = None
        self._writer = None
        self._reader = None
        self._stderr_reader = None
        self._framer = DapFramer()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._seq = 0
        self._waiters: Dict[int, threading.Event] = {}
        self._responses: Dict[int, Dict[str, Any]] = {}
        self._early: Dict[int, Dict[str, Any]] = {}
        self._failure_reason = ""
        self._closed = False
        self._port = 0
        self._port_ready = threading.Event()

    @property
    def pid(self) -> Optional[int]:
        return getattr(self._proc, "pid", None)

    def is_alive(self) -> bool:
        if self._closed or self._proc is None:
            return False
        try:
            if self._proc.poll() is not None:
                return False
        except Exception:
            return False
        return self._reader is not None and self._reader.is_alive()

    # ------------------------------------------------------------------ 生命周期

    def start(self) -> Dict[str, Any]:
        if self._proc is not None:
            raise DapProtocolError("调试适配器已启动")
        self._proc = self._spawn(self._spec.command, self._cwd)
        if self._proc.stderr is not None:
            self._stderr_reader = threading.Thread(
                target=self._stderr_loop, daemon=True, name="ide-dap-stderr"
            )
            self._stderr_reader.start()
        if self._spec.transport == TCP:
            # dlv 只在 stderr 里报端口：先把 stderr 线程跑起来，再等端口
            if not self._port_ready.wait(TCP_PORT_TIMEOUT):
                self.terminate()
                raise DapProtocolError(
                    f"{self._spec.adapter_id} 未在 {TCP_PORT_TIMEOUT}s 内输出监听端口"
                )
            try:
                self._sock = socket.create_connection(("127.0.0.1", self._port), timeout=5.0)
            except OSError as exc:
                self.terminate()
                raise DapProtocolError(
                    f"连接 {self._spec.adapter_id} 的监听端口 127.0.0.1:{self._port} 失败：{exc}"
                ) from exc
            self._sock.settimeout(None)
            self._reader_source = self._sock.makefile("rb", buffering=0)
            self._writer = self._sock.makefile("wb", buffering=0)
        else:
            self._reader_source = self._proc.stdout
            self._writer = self._proc.stdin
        self._reader = threading.Thread(target=self._reader_loop, daemon=True, name="ide-dap-reader")
        self._reader.start()
        return {"pid": self.pid, "transport": self._spec.transport}

    def terminate(self, timeout: float = 5.0) -> None:
        """关通道 → 等自然退出 → 超时强杀进程树（幂等，可从任意线程调用）。"""
        with self._lock:
            already = self._closed
            self._closed = True
        if not already:
            try:
                if self._writer is not None:
                    self._writer.close()  # 关 stdin/套接字 → 适配器收到 EOF 自行退出
            except Exception:
                logger.debug("关闭适配器输入通道失败（忽略）", exc_info=True)
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=timeout)
            except Exception:
                logger.debug("适配器进程优雅退出失败，转强杀", exc_info=True)
            try:
                if proc.poll() is None:
                    # 优雅退出没生效（注入的假进程 wait 不抛错的场景也走这条）：强杀整棵树
                    _kill_process_tree(proc)
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
            except Exception:
                logger.debug("适配器进程回收状态检查失败（忽略）", exc_info=True)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        # 关掉读侧：否则读线程可能一直阻塞在 read 上（进程被杀后管道通常会 EOF，
        # 但注入的 spawn / 假流不保证），join 会白等到超时。
        reader_source = self._reader_source
        if reader_source is not None:
            try:
                reader_source.close()
            except Exception:
                logger.debug("关闭适配器输出通道失败（忽略）", exc_info=True)
        self._join_threads()
        self._fail_pending("调试会话已结束")

    def _join_threads(self) -> None:
        for thread, wait in ((self._reader, 2.0), (self._stderr_reader, 1.0)):
            if thread is not None and thread is not threading.current_thread():
                try:
                    thread.join(timeout=wait)
                except RuntimeError:
                    pass

    # ------------------------------------------------------------------ 请求与响应

    def send_request(
        self,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        # 护栏放在最前面：违规时连请求都不许发出去（发了就会留下一个没人等的 waiter）
        self._reject_reader_blocking()
        return self.await_response(self.send_request_async(command, arguments), timeout=timeout)

    def _reject_reader_blocking(self) -> None:
        """读线程内同步等响应＝等自己把响应读回来，必然死锁（见 DapReaderBlockingError）。"""
        if threading.current_thread() is self._reader:
            raise DapReaderBlockingError(
                "在 DAP 读线程内同步等待响应会造成死锁：请把请求投递到别的线程执行，"
                "或改用 send_request_async + await_response 配对"
            )

    def send_request_async(self, command: str, arguments: Optional[Dict[str, Any]] = None) -> int:
        """发出请求但不等响应，返回 seq。

        为什么需要它：launch 请求的响应要等 configurationDone 之后、被调试程序
        真正启动才回来，同步等待会把「启动调试」卡死在桥方法里。
        """
        if self._proc is None:
            raise DapProtocolError("调试适配器未启动")
        if self._closed:
            raise DapProtocolError("调试会话已结束")
        with self._lock:
            reason = self._failure_reason
        if reason:
            # 通道已经断了：立刻报原因，别让调用方白等一个完整的超时
            raise DapRequestError(reason)
        seq = self._take_seq()
        with self._lock:
            self._waiters[seq] = threading.Event()
        self._write_message({
            "seq": seq, "type": "request", "command": command, "arguments": arguments or {},
        })
        return seq

    def await_response(self, seq: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        # 运行时护栏（S3-T4 补）：直接 await 同样会死锁，违规在这里立刻变成异常，
        # 而不是静默挂死整条会话。
        self._reject_reader_blocking()
        with self._lock:
            waiter = self._waiters.get(seq)
            if waiter is not None:
                stored = self._early.pop(seq, None)
                if stored is not None:
                    # 响应比请求登记先到（读线程抢先处理）：按 seq 认领回来，别白等
                    self._responses[seq] = stored
            else:
                stored = None
        if waiter is None:
            raise DapProtocolError(f"请求 {seq} 不在等待列表中")
        if stored is not None:
            waiter.set()
        wait_time = self._request_timeout if timeout is None else timeout
        if not waiter.wait(wait_time):
            # 超时：把 waiter 与可能已经躺进 _responses 的条目一并摘掉再抛错。只摘 waiter
            # 会漏：响应完全可能在 wait() 返回 False 之后、pop 之前被 _dispatch 存入，而
            # seq 单调递增、永不复用，这条响应就永久滞留在 _responses 里（S3-T3 评审）。
            with self._lock:
                self._waiters.pop(seq, None)
                self._responses.pop(seq, None)
                reason = self._failure_reason
            if reason:
                raise DapRequestError(reason)
            raise DapRequestError(f"调试适配器响应超时（请求 seq={seq}）")
        with self._lock:
            response = self._responses.pop(seq, None)
            self._waiters.pop(seq, None)
            reason = self._failure_reason
        if response is None:
            raise DapRequestError(reason or f"请求 seq={seq} 未收到响应")
        if not response.get("success", False):
            detail = str(response.get("message") or "").strip() or "适配器未说明原因"
            raise DapRequestError(f"{response.get('command') or '请求'} 失败：{detail}")
        body = response.get("body")
        return body if isinstance(body, dict) else {}

    def _take_seq(self) -> int:
        """分配请求 seq（必须在锁内递增：seq 是配对键，重号等于串台）。"""
        with self._lock:
            self._seq += 1
            return self._seq

    def _remember_early(self, request_seq: int, message: Dict[str, Any]) -> None:
        """暂存「抢跑响应」。调用方必须已持有 `self._lock`。"""
        while len(self._early) >= _MAX_EARLY_RESPONSES:
            self._early.pop(next(iter(self._early)), None)
        self._early[request_seq] = message

    def _write_message(self, message: Dict[str, Any]) -> None:
        if self._writer is None:
            raise DapProtocolError("调试适配器输入通道不可用")
        data = encode_message(message)
        with self._write_lock:
            _write_all(self._writer, data)

    # ------------------------------------------------------------------ 读线程

    def _reader_loop(self) -> None:
        source = self._reader_source
        if source is None:
            return
        try:
            while True:
                chunk = _read_chunk(source)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                try:
                    messages = self._framer.feed(chunk)
                except DapProtocolError as exc:
                    # 坏帧之前同一次 feed 里已解出的报文必须先派发：其中很可能正是
                    # 上层在等的响应，直接丢掉会让它白等到超时（S3-T1 的契约）。
                    for message in exc.messages:
                        self._dispatch(message)
                    logger.warning("调试适配器输出解析失败，已放弃该通道：%s", exc)
                    break
                for message in messages:
                    self._dispatch(message)
        except DapReaderBlockingError as exc:
            # 事件回调（on_event）里同步等响应：护栏异常故意穿透回调兜底，这里要响亮地
            # 结束读线程——finally 的 _fail_pending 会唤醒所有等待者，绝不静默半死。
            logger.error("调试事件回调在读线程内同步等待响应（会死锁），已放弃该通道：%s", exc)
        except DapProtocolError:
            logger.debug("调试适配器读线程因协议错误结束", exc_info=True)
        except Exception:
            # 读线程绝不以 traceback 逃逸（否则会留下「进程活着但没人读」的通道）
            logger.debug("调试适配器读线程结束", exc_info=True)
        finally:
            self._fail_pending("调试适配器已断开")

    def _stderr_loop(self) -> None:
        stream = self._proc.stderr
        tail = bytearray()
        try:
            while True:
                chunk = _read_chunk(stream)
                if not chunk:
                    break
                raw = chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode("utf-8", "ignore")
                tail.extend(raw)
                del tail[:-_STDERR_TAIL_BYTES]
                if self._spec.transport == TCP and not self._port_ready.is_set():
                    # dlv 打印监听地址的措辞可能随版本变化，用宽松正则只抓端口；
                    # 在尾部累积缓冲上匹配，避免端口那行被切在两次 read 之间。
                    match = _TCP_PORT_RE.search(bytes(tail))
                    if match:
                        self._port = int(match.group(1))
                        self._port_ready.set()
                if self._on_stderr is not None:
                    try:
                        self._on_stderr(decode_process_output(raw, "utf-8"))
                    except Exception:
                        logger.debug("stderr 回调异常", exc_info=True)
        except Exception:
            logger.debug("调试适配器 stderr 读取结束", exc_info=True)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "response":
            request_seq = int(message.get("request_seq") or 0)
            late = False
            with self._lock:
                waiter = self._waiters.get(request_seq)
                if waiter is not None:
                    self._responses[request_seq] = message
                elif request_seq > self._seq:
                    # 这个 seq 还没发出去过，不可能是「迟到」，只能是读线程抢先处理到：
                    # 按 seq 暂存，等 await_response 认领（丢了会让调用方白等一个
                    # 已经到手的响应）。
                    self._remember_early(request_seq, message)
                else:
                    late = True
            if waiter is not None:
                waiter.set()
            elif late:
                # 迟到响应：请求已超时返回或已失效，这条响应无处可去。排障必须能区分
                # 「适配器没回」与「回了但已超时」——前者是适配器卡死，后者是超时太紧。
                logger.debug(
                    "调试适配器的响应 seq=%s 迟到（请求已超时或已失效），已丢弃", request_seq
                )
            return
        if kind == "event":
            if self._on_event is not None:
                body = message.get("body")
                try:
                    self._on_event(str(message.get("event") or ""), body if isinstance(body, dict) else {})
                except DapReaderBlockingError:
                    # 护栏异常（回调内同步等响应）不许被下面的兜底吞掉：吞掉＝静默半死，
                    # 必须让读线程结束并唤醒等待者（见 DapReaderBlockingError）。
                    raise
                except Exception:
                    # 事件回调异常不允许打断读线程，否则整条通道会静默死掉
                    logger.debug("调试事件回调异常", exc_info=True)
            return
        if kind == "request":
            # 适配器反向请求（runInTerminal / startDebugging 等）：星瑶不在终端里
            # 代跑，明确回绝，让适配器按「不支持」继续，避免它死等一个永不来的响应
            try:
                self._write_message({
                    "seq": self._take_seq(),
                    "type": "response",
                    "request_seq": int(message.get("seq") or 0),
                    "success": False,
                    "command": str(message.get("command") or ""),
                    "message": "星瑶暂不支持调试适配器的反向请求",
                })
            except DapProtocolError as exc:
                # 通道已关：回不了包是预期情况，不允许它冒泡出读线程
                logger.debug("无法回绝调试适配器的反向请求：%s", exc)

    def _fail_pending(self, reason: str) -> None:
        """唤醒所有等待者并记录原因：通道断了不能让 send_request 永久阻塞。"""
        with self._lock:
            waiters = list(self._waiters.values())
            self._failure_reason = reason
        for waiter in waiters:
            waiter.set()