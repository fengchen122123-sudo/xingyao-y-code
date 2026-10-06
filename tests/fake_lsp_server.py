# -*- coding: utf-8 -*-
"""测试用的假语言服务器。

它只实现 LSP 的最小骨架（Content-Length 帧 + initialize / initialized /
shutdown / exit），再加若干 test/* 方法用来触发各种分支（超时、崩溃、
主动推送、拒绝退出、错误响应、坏帧）。

刻意用手写最小实现而非真实语言服务器：单测必须不依赖用户机器上装了什么，
也不能因为某个语言服务器的行为变化而变红。

额外能力（都只在环境变量/ mode 显式打开时才生效，不影响其它用例）：
- `FAKE_LSP_CAPTURE_INIT=<文件>`：把收到的 initialize params 落盘，用来断言
  真正发到线上的内容（例如 initializationOptions 里有没有串工作区的数据）；
- `mode="bad-frame"` / `test/badframe`：在**同一次 write** 里给出「合法帧 + 坏帧」，
  验证坏帧不会连带丢掉前面的合法消息。
"""
import os
import sys

FAKE_SERVER_SOURCE = r'''
import json
import os
import sys


def send(payload):
    data = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(data))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def send_bytes(raw):
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def frame(payload):
    data = json.dumps(payload).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(data) + data


def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def capture_initialize(params):
    path = os.environ.get("FAKE_LSP_CAPTURE_INIT")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(params, fh, ensure_ascii=False)
    except OSError:
        pass


# 坏帧：Content-Length 声明 3 字节，正文却不是合法 JSON
BAD_FRAME = b"Content-Length: 3\r\n\r\n{xx"


def main():
    mode = os.environ.get("FAKE_LSP_MODE", "normal")
    while True:
        msg = read_message()
        if msg is None:
            return 0
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            capture_initialize(msg.get("params") or {})
            send({"jsonrpc": "2.0", "id": msg_id,
                  "result": {"capabilities": {"hoverProvider": True}}})
        elif method == "initialized":
            if mode == "exit-after-init":
                sys.exit(3)
        elif method == "shutdown":
            if mode == "ignore-shutdown":
                continue
            send({"jsonrpc": "2.0", "id": msg_id, "result": None})
        elif method == "exit":
            if mode == "ignore-shutdown":
                continue
            return 0
        elif method == "test/echo":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"echo": msg.get("params")}})
        elif method == "test/slow":
            continue
        elif method == "test/notify":
            send({"jsonrpc": "2.0", "method": "test/pushed", "params": {"n": 1}})
            send({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
        elif method == "test/badframe":
            send_bytes(
                frame({"jsonrpc": "2.0", "id": msg_id, "result": "ok"}) + BAD_FRAME
            )
        elif method == "test/publishDiagnostics":
            params = msg.get("params") or {}
            send({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": params.get("uri", ""),
                    "diagnostics": [
                        {
                            "range": {"start": {"line": 0, "character": 0},
                                      "end": {"line": 0, "character": 1}},
                            "severity": 1,
                            "message": "undefined name x",
                            "source": "fake",
                        }
                    ],
                },
            })
            send({"jsonrpc": "2.0", "id": msg_id, "result": "ok"})
        elif method == "test/error":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32602, "message": "bad params"}})
        elif method == "test/crash":
            sys.exit(9)
        else:
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32601, "message": "unknown method"}})


if __name__ == "__main__":
    sys.exit(main())
'''


def write_fake_server(tmp_path) -> str:
    path = os.path.join(str(tmp_path), "fake_lsp.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FAKE_SERVER_SOURCE)
    return path


def make_fake_command(tmp_path, mode: str = "normal") -> list[str]:
    """返回可直接当 argv 用的命令；mode 通过环境变量传给子进程。"""
    return [sys.executable, "-u", write_fake_server(tmp_path)]


def fake_env(mode: str = "normal") -> dict:
    env = dict(os.environ)
    env["FAKE_LSP_MODE"] = mode
    return env


def write_cmd_shim(tmp_path, name: str = "fake-lsp.cmd") -> str:
    """生成一个 Windows 批处理 shim（npm 全局命令在 Windows 上的形态）。

    真实场景：`npm i -g typescript-language-server` 在 Windows 上只会生成
    `typescript-language-server.cmd`，它无法被 Popen([...]) 直接执行。

    文件必须按 cmd.exe 使用的 ANSI 代码页写：路径里出现非 ASCII（测试机的
    临时目录就带中文用户名）时，用 utf-8 写会让 cmd 解出乱码路径。
    """
    path = os.path.join(str(tmp_path), name)
    encoding = "mbcs" if os.name == "nt" else "utf-8"
    with open(path, "w", encoding=encoding, newline="\r\n") as fh:
        fh.write("@echo off\r\n")
        fh.write(f'"{sys.executable}" -u "{write_fake_server(tmp_path)}" %*\r\n')
    return path
