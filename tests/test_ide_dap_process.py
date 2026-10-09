"""DapProcess 测试：用注入的假进程覆盖请求配对、事件派发、反向请求与回收。

不依赖 debugpy/dlv 安装，也不启动真子进程——真进程端到端在 Task 4 由
tests/fake_dap_adapter.py 覆盖。

（S3-T3 追加段：本文件末尾还包含「按 seq 配对 / 迟到响应 / 真实子进程端到端」
三组用例——真实子进程用的是本文件内联生成到 tmp_path 的假适配器脚本，避免与
Task 4 的 tests/fake_dap_adapter.py 抢文件名。）
"""

import json
import logging
import sys
import threading
import time

import pytest

from core.ide import dap_manager as dm
from core.ide.dap_manager import DapProcess, DapProtocolError, DapRequestError, encode_message
from core.ide.debug_adapters import STDIO, TCP, AdapterSpec


class _FakeSink:
    """假 stdin：记录写入字节（DapProcess 用 memoryview 分片写，必须支持）。"""

    def __init__(self, drip=0):
        self.data = bytearray()
        self.closed = False
        self._drip = drip

    def write(self, data):
        chunk = bytes(data)
        if self._drip:
            chunk = chunk[: self._drip]
        self.data.extend(chunk)
        return len(chunk)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class _FakePipe:
    """假 stdout：read(n) 阻塞直到有数据或管道关闭。"""

    def __init__(self):
        self._pending = []
        self._closed = False
        self._cv = threading.Condition()

    def feed(self, data):
        with self._cv:
            self._pending.append(data)
            self._cv.notify_all()

    def read(self, size=-1):
        with self._cv:
            while not self._pending and not self._closed:
                self._cv.wait(0.5)
            if self._pending:
                return self._pending.pop(0)
            return b""

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()


class _FakeProc:
    def __init__(self, stdout, stderr=None, terminate_works=True):
        self.stdout = stdout
        self.stderr = stderr
        self.stdin = _FakeSink()
        self.pid = 4321
        self.killed = False
        self.waited = False
        self._terminate_works = terminate_works

    def poll(self):
        return 0 if self.killed else None

    def wait(self, timeout=None):
        self.waited = True
        return 0

    def terminate(self):
        if self._terminate_works:
            self.killed = True

    def kill(self):
        self.killed = True


def _spec(transport=STDIO):
    return AdapterSpec("debugpy", "python", ["python", "-m", "debugpy.adapter"], transport)


def _make_process(proc, events=None, stderr_texts=None, **kwargs):
    process = DapProcess(
        _spec(),
        cwd="",
        on_event=(lambda name, body: events.append((name, body))) if events is not None else None,
        on_stderr=(lambda text: stderr_texts.append(text)) if stderr_texts is not None else None,
        spawn=lambda command, cwd: proc,
        request_timeout=kwargs.pop("request_timeout", 1.0),
    )
    process.start()
    return process


def test_send_request_writes_frame_and_returns_body():
    out = _FakePipe()
    proc = _FakeProc(out)
    process = _make_process(proc)
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {"capabilities": {"a": 1}}}))

    body = process.send_request("initialize", {"clientID": "ycode"})

    assert body == {"capabilities": {"a": 1}}
    written = bytes(proc.stdin.data)
    assert written.startswith(b"Content-Length: ")
    assert b'"command":"initialize"' in written and b'"seq":1' in written
    process.terminate()


def test_fragmented_response_is_reassembled():
    out = _FakePipe()
    proc = _FakeProc(out)
    process = _make_process(proc)
    data = encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {"ok": True}})
    threading.Timer(0.05, lambda: [out.feed(data[i:i + 7]) for i in range(0, len(data), 7)]).start()

    assert process.send_request("threads", {}) == {"ok": True}
    process.terminate()


def test_events_are_dispatched_to_callback():
    out = _FakePipe()
    events = []
    process = _make_process(_FakeProc(out), events=events)
    out.feed(encode_message({"seq": 1, "type": "event", "event": "stopped", "body": {"reason": "breakpoint", "threadId": 1}}))

    for _ in range(100):
        if events:
            break
        threading.Event().wait(0.02)

    assert events == [("stopped", {"reason": "breakpoint", "threadId": 1})]
    process.terminate()


def test_reverse_request_is_answered_with_failure():
    """适配器反向请求（runInTerminal 等）必须被明确回绝，不能让它死等。"""
    out = _FakePipe()
    proc = _FakeProc(out)
    process = _make_process(proc)
    out.feed(encode_message({"seq": 11, "type": "request", "command": "runInTerminal", "arguments": {"args": []}}))

    for _ in range(100):
        if proc.stdin.data:
            break
        threading.Event().wait(0.02)

    written = proc.stdin.data.decode("utf-8")
    assert '"type":"response"' in written and '"request_seq":11' in written
    assert '"success":false' in written and "runInTerminal" in written
    process.terminate()


def test_success_false_raises_request_error_with_adapter_message():
    out = _FakePipe()
    process = _make_process(_FakeProc(out))
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": False, "message": "no such thread"}))

    with pytest.raises(DapRequestError, match="no such thread"):
        process.send_request("threads", {})
    process.terminate()


def test_timeout_raises_and_drops_waiter():
    process = _make_process(_FakeProc(_FakePipe()), request_timeout=0.05)

    with pytest.raises(DapRequestError, match="超时"):
        process.send_request("threads", {})

    assert process._waiters == {}
    process.terminate()


def test_reader_eof_fails_pending_requests_with_reason():
    out = _FakePipe()
    process = _make_process(_FakeProc(out))
    out.close()

    with pytest.raises(DapRequestError, match="已断开"):
        process.send_request("threads", {})

    process.terminate()


def test_partial_writes_are_looped_until_frame_complete():
    """假 stdin 一次只收 1 字节，整帧仍必须写净（帧不可被截断）。"""
    out = _FakePipe()
    proc = _FakeProc(out)
    # 计划原用例把 proc.stdin 换在 start() 之后，而 start() 已把 proc.stdin 绑成写目标，
    # 于是整帧写进了被换掉的那个 sink、断言永远看不到字节。这里在 start() 之前接上。
    proc.stdin = _FakeSink(drip=1)
    process = _make_process(proc)
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {}}))

    process.send_request("threads", {})

    assert process._writer is proc.stdin
    frame = bytes(proc.stdin.data)
    header, _, payload = frame.partition(b"\r\n\r\n")
    # 声明长度 == 实际正文长度，才是「整帧写净」；只看尾部字节会把 arguments 的
    # 结尾 `}}` 误判成截断（计划原断言 endswith(b"{}") 就是这样写错的）。
    assert int(header.split(b":", 1)[1]) == len(payload)
    assert json.loads(payload.decode("utf-8"))["command"] == "threads"
    process.terminate()


def test_terminate_closes_stdin_and_reaps_process():
    out = _FakePipe()
    proc = _FakeProc(out)
    process = _make_process(proc)

    process.terminate()

    assert proc.stdin.closed is True
    assert proc.killed is True and proc.waited is True
    assert process.is_alive() is False


def test_terminate_falls_back_to_process_tree_kill(monkeypatch):
    out = _FakePipe()
    proc = _FakeProc(out, terminate_works=False)
    killed = []
    monkeypatch.setattr("core.ide.dap_manager._kill_process_tree", lambda p: killed.append(p.pid))
    process = _make_process(proc)

    process.terminate(timeout=0.05)

    assert killed == [4321]


def test_stderr_text_is_decoded_and_forwarded():
    out = _FakePipe()
    stderr = _FakePipe()
    seen = []
    process = _make_process(_FakeProc(out, stderr=stderr), stderr_texts=seen)
    stderr.feed("adapter 警告：缺少 debugpy\n".encode("utf-8"))

    for _ in range(100):
        if seen:
            break
        threading.Event().wait(0.02)

    assert any("缺少 debugpy" in text for text in seen)
    process.terminate()


def test_start_twice_is_rejected():
    out = _FakePipe()
    process = _make_process(_FakeProc(out))

    with pytest.raises(DapProtocolError, match="已启动"):
        process.start()
    process.terminate()


# =====================================================================================
# 以下为 S3-T3 期间追加的加强用例（评审要求：配对与超时必须闭合；真进程验证优先）
# =====================================================================================


def test_pairing_is_by_seq_not_fifo():
    """按 seq 配对：响应乱序到达也必须各归其主（不能按到达顺序先进先出）。"""
    out = _FakePipe()
    process = _make_process(_FakeProc(out), request_timeout=2.0)
    first = process.send_request_async("threads", {})
    second = process.send_request_async("variables", {"variablesReference": 1})

    out.feed(encode_message({"seq": 2, "type": "response", "request_seq": second, "success": True, "body": {"who": "second"}}))
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": first, "success": True, "body": {"who": "first"}}))

    assert process.await_response(second) == {"who": "second"}
    assert process.await_response(first) == {"who": "first"}
    assert process._waiters == {} and process._responses == {}
    process.terminate()


def test_late_response_after_timeout_is_dropped_and_logged(caplog):
    """超时后迟到的响应：静默丢弃但留 debug 日志，且不得滞留在待答表里。"""
    out = _FakePipe()
    process = _make_process(_FakeProc(out), request_timeout=0.05)
    caplog.set_level(logging.DEBUG, logger="core.ide.dap_manager")

    with pytest.raises(DapRequestError, match="超时"):
        process.send_request("threads", {})

    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {"late": True}}))
    for _ in range(100):
        if "迟到" in caplog.text:
            break
        threading.Event().wait(0.02)

    assert "迟到" in caplog.text
    assert process._waiters == {} and process._responses == {} and process._early == {}
    process.terminate()


def test_response_arriving_before_request_registration_is_not_lost():
    """读线程可能先处理到响应、请求登记随后才发生：这种「抢跑响应」不能丢。

    丢了会让调用方白等一个已经到手的响应（计划里的多条用例正是先 feed 再发请求）。
    """
    out = _FakePipe()
    process = _make_process(_FakeProc(out), request_timeout=0.5)
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {"early": True}}))
    for _ in range(100):
        if process._early:
            break
        threading.Event().wait(0.02)

    assert process.send_request("threads", {}) == {"early": True}
    assert process._early == {}
    process.terminate()


def test_framing_error_still_dispatches_decoded_messages():
    """坏帧之前同一次 feed 里解出的响应必须先派发（S3-T1 的 messages 契约）。"""
    out = _FakePipe()
    process = _make_process(_FakeProc(out), request_timeout=1.0)
    good = encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {"ok": True}})
    bad = b"Content-Length: 4\r\n\r\n{xx}"
    out.feed(good + bad)

    assert process.send_request("threads", {}) == {"ok": True}
    process.terminate()


# ------------------------------------------------------------------ 真实子进程端到端

_FAKE_ADAPTER = r'''
"""内联假 DAP 适配器：stdio 或 TCP 两种传输，用于真子进程端到端验证。"""
import json
import socket
import sys
import threading
import time

_MODE = sys.argv[1] if len(sys.argv) > 1 else "stdio"
_REVERSE_SEQ = 900
_write_lock = threading.Lock()
_seq_lock = threading.Lock()
_seq = [0]


def _next_seq():
    with _seq_lock:
        _seq[0] += 1
        return _seq[0]


def encode(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def read_frames(stream):
    buffer = bytearray()
    while True:
        chunk = stream.read(1)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            index = buffer.find(b"\r\n\r\n")
            if index < 0:
                break
            length = 0
            for line in bytes(buffer[:index]).split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1].strip())
            if len(buffer) < index + 4 + length:
                break
            body = bytes(buffer[index + 4:index + 4 + length])
            del buffer[:index + 4 + length]
            yield json.loads(body.decode("utf-8"))


def main():
    if _MODE == "stdio":
        reader = sys.stdin.buffer
        writer = sys.stdout.buffer
    else:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        sys.stderr.write("dlv dap: DAP server listening at: 127.0.0.1:%d\n" % port)
        sys.stderr.flush()
        conn, _ = server.accept()
        reader = conn.makefile("rb")
        writer = conn.makefile("wb")

    def send(message):
        with _write_lock:
            writer.write(encode(message))
            writer.flush()

    def respond(request, body=None, success=True, message=""):
        payload = {
            "seq": _next_seq(),
            "type": "response",
            "request_seq": request.get("seq"),
            "success": success,
            "command": request.get("command"),
        }
        if success:
            payload["body"] = body or {}
        else:
            payload["message"] = message
        send(payload)

    for message in read_frames(reader):
        kind = message.get("type")
        if kind == "response":
            if message.get("request_seq") == _REVERSE_SEQ:
                send({
                    "seq": _next_seq(),
                    "type": "event",
                    "event": "reversal",
                    "body": {
                        "state": "answered",
                        "command": message.get("command", ""),
                        "message": message.get("message", ""),
                        "success": message.get("success"),
                    },
                })
            continue
        command = message.get("command")
        if command == "initialize":
            respond(message, {"capabilities": {"supportsConfigurationDoneRequest": True}})
            send({"seq": _next_seq(), "type": "event", "event": "initialized", "body": {}})
        elif command == "threads":
            respond(message, {"threads": [{"id": 1, "name": "main"}]})
        elif command == "reverse":
            respond(message, {})
            send({
                "seq": _REVERSE_SEQ,
                "type": "request",
                "command": "runInTerminal",
                "arguments": {"args": []},
            })
        elif command == "noisy":
            sys.stderr.write("fake adapter: noisy\n")
            sys.stderr.flush()
            respond(message, {})
        elif command == "boom":
            respond(message, success=False, message="no such thread")
        elif command == "never":
            holder = dict(message)
            threading.Timer(1.0, lambda: respond(holder, {"late": True})).start()
        else:
            respond(message, {})


main()
'''


def _wait_until(predicate, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _write_fake_adapter(tmp_path, mode="stdio", transport=STDIO, adapter_id="debugpy"):
    script = tmp_path / "fake_dap_adapter.py"
    script.write_text(_FAKE_ADAPTER, encoding="utf-8")
    return AdapterSpec(
        adapter_id,
        "python",
        [sys.executable, "-u", str(script), mode],
        transport,
    )


def _real_process(spec, tmp_path, events=None, stderr_texts=None, request_timeout=5.0):
    """用真 `_spawn_adapter` 起真子进程，并留下 Popen 句柄供「无僵尸」断言。"""
    spawned = []

    def spawn(command, cwd):
        proc = dm._spawn_adapter(command, cwd)
        spawned.append(proc)
        return proc

    process = DapProcess(
        spec,
        cwd=str(tmp_path),
        on_event=(lambda name, body: events.append((name, body))) if events is not None else None,
        on_stderr=(lambda text: stderr_texts.append(text)) if stderr_texts is not None else None,
        spawn=spawn,
        request_timeout=request_timeout,
    )
    return process, spawned


def test_real_stdio_adapter_end_to_end(tmp_path):
    """真子进程 + 真管道：请求/响应配对、事件、反向请求回绝、stderr 转发、回收。"""
    events, stderr_texts = [], []
    process, spawned = _real_process(_write_fake_adapter(tmp_path), tmp_path, events, stderr_texts)
    info = process.start()

    assert info["transport"] == STDIO and info["pid"] == spawned[0].pid and process.is_alive()

    body = process.send_request("initialize", {"clientID": "ycode"})
    assert body == {"capabilities": {"supportsConfigurationDoneRequest": True}}
    assert _wait_until(lambda: any(name == "initialized" for name, _ in events))

    process.send_request("reverse", {})
    assert _wait_until(lambda: any(name == "reversal" for name, _ in events))
    reversal = [body for name, body in events if name == "reversal"][0]
    assert reversal["state"] == "answered" and reversal["command"] == "runInTerminal"
    assert '"success":false' in reversal["message"] or reversal["success"] is False

    process.send_request("noisy", {})
    assert _wait_until(lambda: any("fake adapter: noisy" in text for text in stderr_texts))

    with pytest.raises(DapRequestError, match="no such thread"):
        process.send_request("boom", {})

    process.terminate()
    process.terminate()  # 幂等

    assert process.is_alive() is False
    assert spawned[0].poll() is not None, "适配器进程必须已被回收（不能留僵尸）"
    assert not process._reader.is_alive() and not process._stderr_reader.is_alive()


def test_real_adapter_over_tcp_end_to_end(tmp_path):
    """dlv 走 TCP：从 stderr 解析随机端口、连上去、配对、回收。"""
    stderr_texts = []
    spec = _write_fake_adapter(tmp_path, mode="tcp", transport=TCP, adapter_id="dlv")
    process, spawned = _real_process(spec, tmp_path, None, stderr_texts)

    info = process.start()

    assert info["transport"] == TCP and process._port > 0
    assert any("listening at" in text for text in stderr_texts)
    assert process.send_request("threads", {})["threads"] == [{"id": 1, "name": "main"}]

    process.terminate()

    assert process.is_alive() is False
    assert spawned[0].poll() is not None
    assert not process._reader.is_alive()


def test_real_adapter_timeout_then_late_response_keeps_channel_usable(tmp_path):
    """真适配器超时后：迟到响应被丢弃，通道仍可继续用，待答表不留垃圾。"""
    process, spawned = _real_process(
        _write_fake_adapter(tmp_path), tmp_path, request_timeout=0.3
    )
    process.start()

    with pytest.raises(DapRequestError, match="超时"):
        process.send_request("never", {})

    time.sleep(1.5)  # 让适配器那条迟到响应真的到达读线程

    assert process.send_request("threads", {})["threads"] == [{"id": 1, "name": "main"}]
    assert process._waiters == {} and process._responses == {} and process._early == {}

    process.terminate()

    assert spawned[0].poll() is not None


# =====================================================================================
# S3-T4 追加段：读线程内同步等待的运行时护栏 + 超时分支清 _responses
# =====================================================================================


def test_sync_request_inside_event_callback_is_rejected_not_deadlocked():
    """读线程内同步等响应 = 自锁：护栏必须立刻抛 DapReaderBlockingError。

    Global Constraints 的铁律：`on_event` 是在读线程内被同步调用的，回调里只能
    `put_nowait` 投递事件，或改用 `send_request_async` + `await_response` 配对。
    这条此前只有 docstring 约定、没有运行时护栏——写出违规代码的后果是整条会话静默
    挂死（读线程在等自己的响应），因此这里把它变成「立刻可诊断的异常」。
    """
    out = _FakePipe()
    proc = _FakeProc(out)
    callback_threads, violations = [], []

    def on_event(name, body):
        callback_threads.append(threading.current_thread().name)
        try:
            process.send_request("threads", {}, timeout=5.0)
        except Exception as exc:  # 违规的一方自己接住：护栏异常不该顺带毁掉通道
            violations.append(exc)

    process = DapProcess(
        _spec(), cwd="", on_event=on_event, spawn=lambda command, cwd: proc, request_timeout=1.0
    )
    process.start()
    try:
        out.feed(encode_message({
            "seq": 1, "type": "event", "event": "stopped", "body": {"threadId": 1},
        }))
        assert _wait_until(lambda: len(violations) == 1)

        assert callback_threads == ["ide-dap-reader"], "回调必须在读线程内同步执行（铁律前提）"
        assert isinstance(violations[0], dm.DapReaderBlockingError)
        assert "死锁" in str(violations[0])
        assert process._waiters == {}, "被拦下的请求不许留下孤儿 waiter"

        # 护栏只拦违规请求，不牵连通道：读线程仍在读，后续请求照常配对
        seq = process.send_request_async("threads", {})
        out.feed(encode_message({
            "seq": 2, "type": "response", "request_seq": seq, "success": True,
            "body": {"threads": [{"id": 1, "name": "MainThread"}]},
        }))
        assert process.await_response(seq) == {"threads": [{"id": 1, "name": "MainThread"}]}
        assert process.is_alive()
    finally:
        process.terminate()


def test_uncaught_reader_violation_fails_channel_instead_of_hanging():
    """回调没接住护栏异常时：必须让会话明确失败，而不是留下永久阻塞的等待者。"""
    out = _FakePipe()
    proc = _FakeProc(out)

    def on_event(name, body):
        process.send_request("threads", {}, timeout=60.0)  # 故意不接住护栏异常

    process = DapProcess(
        _spec(), cwd="", on_event=on_event, spawn=lambda command, cwd: proc, request_timeout=10.0
    )
    process.start()
    try:
        seq = process.send_request_async("launch", {})
        started = time.time()
        out.feed(encode_message({
            "seq": 1, "type": "event", "event": "stopped", "body": {"threadId": 1},
        }))

        with pytest.raises(DapRequestError):
            process.await_response(seq, timeout=10.0)

        assert time.time() - started < 5.0, "违规后必须立刻唤醒等待者，不能白等到超时"
        assert _wait_until(lambda: not process._reader.is_alive())
        assert process._waiters == {} and process._responses == {}
    finally:
        process.terminate()


def test_timeout_drops_response_stored_just_after_wait_returned():
    """超时分支必须连 `_responses` 一起清：迟到响应可能卡在 wait 与 pop 之间。

    竞态：`waiter.wait()` 返回 False（超时）之后、`pop` 之前，读线程完全可能刚把这条
    响应 `_responses[seq] = ...` 存下。seq 单调递增、永不复用，只摘 waiter 会让该条目
    永久滞留（内存只涨不降）。这里用受控交错复现：在「wait 返回 False 的同一瞬间」让
    读线程真的把响应存进来。
    """
    out = _FakePipe()
    proc = _FakeProc(out)
    process = _make_process(proc, request_timeout=0.05)
    seq = process.send_request_async("threads", {})

    class _LateStoreEvent:
        """假 waiter：wait() 里先让读线程存下响应，再返回 False（等价于超时那一瞬）。"""

        def set(self):
            pass

        def is_set(self):
            return False

        def wait(self, timeout=None):
            process._dispatch({
                "seq": 99, "type": "response", "request_seq": seq, "success": True,
                "command": "threads", "body": {"threads": []},
            })
            return False

    process._waiters[seq] = _LateStoreEvent()
    try:
        with pytest.raises(DapRequestError, match="超时"):
            process.await_response(seq)

        assert process._responses == {}, "超时后不许在 _responses 里留下永不回收的响应"
        assert process._waiters == {}
    finally:
        process.terminate()
