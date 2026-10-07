"""测试用最小 DAP 适配器（仅标准库，不 import 项目代码）。

不是真调试器：只复现 DAP 时序与事件流，让 core.ide.debug_session 的端到端
测试不依赖 debugpy 安装（本机没有 debugpy/dlv/lldb-dap 真二进制）。

时序（与 debugpy 一致）：
  initialize -> 响应 + initialized 事件
  launch     -> 先不回，等 configurationDone 后再回（真适配器行为）
  setBreakpoints -> 响应 verified 列表
  configurationDone -> 响应，补发 launch 响应 + output 事件 + stopped 事件

注入开关（都是给「失败即回收」这组用例用的，正常路径不带）：
  --no-restart（或环境变量 FAKE_DAP_NO_RESTART=1）-> 不上报 supportsRestartRequest
  --fail-command=X -> X 返回 success=false（launch 的失败仍在 configurationDone 时回）
  --hang-command=X -> X 收到后故意不回响应（模拟适配器卡住 → 客户端超时）
"""

import json
import os
import sys


def encode(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class Stub:
    def __init__(self, no_restart=False, fail_command="", hang_command=""):
        self.seq = 0
        self.launch = None
        self.breakpoints = {}
        self.no_restart = no_restart
        self.fail_command = fail_command
        self.hang_command = hang_command

    def next_seq(self):
        self.seq += 1
        return self.seq

    def write(self, message):
        message.setdefault("seq", self.next_seq())
        sys.stdout.buffer.write(encode(message))
        sys.stdout.buffer.flush()

    def ok(self, request, body=None):
        self.write({
            "type": "response",
            "request_seq": request["seq"],
            "success": True,
            "command": request.get("command", ""),
            "body": body or {},
        })

    def fail(self, request, message):
        self.write({
            "type": "response",
            "request_seq": request["seq"],
            "success": False,
            "command": request.get("command", ""),
            "message": message,
        })

    def event(self, name, body=None):
        self.write({"type": "event", "event": name, "body": body or {}})

    def handle(self, request):
        command = request.get("command")
        if command == "launch":
            self.launch = request
            return  # 故意延后到 configurationDone 再回（真适配器行为）
        if self.hang_command and command == self.hang_command:
            return  # 故意不回：模拟适配器卡住，客户端应当超时
        if self.fail_command and command == self.fail_command:
            self.fail(request, f"注入的失败：{command}")
            return
        if command == "initialize":
            capabilities = {"supportsConfigurationDoneRequest": True}
            if not self.no_restart:
                capabilities["supportsRestartRequest"] = True
            self.ok(request, {"capabilities": capabilities})
            self.event("initialized")
            return
        if command == "setBreakpoints":
            source = (request.get("arguments") or {}).get("source") or {}
            lines = [int(bp.get("line") or 0) for bp in ((request.get("arguments") or {}).get("breakpoints") or [])]
            self.breakpoints[source.get("path", "")] = lines
            self.ok(request, {"breakpoints": [{"verified": True, "line": line} for line in lines]})
            return
        if command == "configurationDone":
            self.ok(request)
            if self.launch is not None and self.hang_command != "launch":
                if self.fail_command == "launch":
                    self.fail(self.launch, "注入的启动失败")
                else:
                    self.ok(self.launch, {})
            self.event("output", {"category": "stdout", "output": "stub adapter ready\n"})
            self.event("stopped", {"reason": "breakpoint", "threadId": 1, "allThreadsStopped": True})
            return
        if command == "threads":
            self.ok(request, {"threads": [{"id": 1, "name": "MainThread"}]})
            return
        if command == "stackTrace":
            source = next(iter(self.breakpoints), r"C:\proj\app.py")
            line = (self.breakpoints.get(source) or [7])[0]
            self.ok(request, {"stackFrames": [{"id": 1001, "name": "main", "line": line, "column": 1, "source": {"path": source}}]})
            return
        if command == "scopes":
            self.ok(request, {"scopes": [{"name": "Locals", "variablesReference": 2001, "expensive": False}]})
            return
        if command == "variables":
            self.ok(request, {"variables": [{"name": "x", "value": "42", "type": "int", "variablesReference": 0}]})
            return
        if command == "evaluate":
            expression = (request.get("arguments") or {}).get("expression", "")
            self.ok(request, {"result": f"eval:{expression}", "type": "str", "variablesReference": 0})
            return
        if command in ("continue", "next", "stepIn", "stepOut", "pause"):
            self.ok(request, {})
            if command == "continue":
                self.event("continued", {"threadId": 1, "allThreadsContinued": True})
            else:
                self.event("stopped", {"reason": "step", "threadId": 1})
            return
        if command == "restart":
            self.ok(request)
            self.event("stopped", {"reason": "restart", "threadId": 1})
            return
        if command == "disconnect":
            self.ok(request)
            self.event("terminated")
            self.write({"type": "event", "event": "exited", "body": {"exitCode": 0}})
            sys.stdout.buffer.flush()
            os._exit(0)  # 适配器被 disconnect 后自行退出，模拟真适配器
            return
        self.fail(request, f"stub 不认识命令：{command}")


def parse_argv(argv):
    no_restart = bool(os.environ.get("FAKE_DAP_NO_RESTART")) or "--no-restart" in argv
    fail_command, hang_command = "", ""
    for arg in argv:
        if arg.startswith("--fail-command="):
            fail_command = arg.split("=", 1)[1]
        elif arg.startswith("--hang-command="):
            hang_command = arg.split("=", 1)[1]
    return no_restart, fail_command, hang_command


def main():
    no_restart, fail_command, hang_command = parse_argv(sys.argv[1:])
    stub = Stub(no_restart=no_restart, fail_command=fail_command, hang_command=hang_command)
    buffer = b""
    stream = sys.stdin.buffer
    while True:
        chunk = stream.read(1)
        if not chunk:
            return
        buffer += chunk
        while True:
            header_end = buffer.find(b"\r\n\r\n")
            if header_end < 0:
                break
            length = 0
            for line in buffer[:header_end].split(b"\r\n"):
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            body_start = header_end + 4
            if len(buffer) < body_start + length:
                break
            payload = json.loads(buffer[body_start:body_start + length].decode("utf-8"))
            buffer = buffer[body_start + length:]
            stub.handle(payload)


if __name__ == "__main__":
    main()