# 星瑶 IDE 模式 S3「运行与调试」实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 IDE 模式下的用户可以一键运行当前文件（输出落在底部终端面板），并用 Debug Adapter Protocol 完成「设断点 → 启动调试 → 单步 → 看调用栈/变量/监视 → 在调试控制台求值 → 停止并回收适配器进程」。

**Architecture:** 后端新增 `core/ide/` 下的三个 S3 专属模块：`dap_manager.py`（DAP 帧编解码 + 适配器进程 + 请求响应配对）、`debug_session.py`（DAP 流程编排与事件归一化）、`debug_adapters.py`（debugpy / dlv / lldb-dap 探测与启动规格）、`run_command.py`（按语言或项目配置解析运行命令）。`gui_bridge.py` 只加薄桥方法：运行命令写进既有 PTY 会话，调试事件经 `emit_event` 进 `_js_queue` 由 `gui-js-dispatcher` 单消费者线程派发，适配器读线程绝不直接触碰 WebView。前端新增 `utils/dapBus.ts`（高频调试输出旁路总线）、`stores/ideDebugStore.ts`（调试会话状态）、`components/DebugToolbar.vue`、`components/DebugPane.vue`、`ide/cmBreakpoints.ts`（CodeMirror 6 断点 gutter 与执行行高亮）。

**Tech Stack:** Python 3（仅标准库：`subprocess` / `socket` / `json` / `threading`）/ pytest / Vue 3 + Pinia + Vitest + jsdom / CodeMirror 6 / pywebview 桥接

## Global Constraints

以下为设计规格（`docs/superpowers/specs/2026-10-06-ide-mode-design.md`）与仓库级铁律，每个任务都隐含包含：

- 分支：`ide`（长期特性分支，定期从 `dev` 合入）；每个任务独立提交，不混杂无关重构。
- **事件推送铁律**：`evaluate_js` 只允许在 `gui_bridge.py` 的 `gui-js-dispatcher` 单消费者线程调用（`gui_bridge.py:2035-2097`，线程名见 `gui_bridge.py:2094`）；事件源线程（DAP 读线程、stderr 读线程、断点回调）只允许 `_js_queue.put_nowait`（唯一入队点 `gui_bridge.py:2062-2071`），队列满丢弃新事件。**适配器读线程内禁止调用任何同步等待响应的 DAP 请求**（会与自己的读循环死锁，见 Task 4 的 `_handle_stopped` 实现）。
- **字节稳定红线**：不得改变 `build_stable_prompt` 的字节输出；`tests/test_prompt_byte_stability.py` 必须保持通过，且不得为本计划改动该文件（`AGENTS.md:106` 记录的回归守卫）。
- **Python 侧不引入第三方依赖**：调试适配器是外部可执行程序，星瑶只用标准库与它通信；不自动安装任何东西，缺失时给中文安装提示（规格 §2.3）。
- 不改动 `core/lsp.py`（那是 `find_definition` 工具的实现，见规格 §4）。
- 版本号只改 `core/version.py`（当前 `1.3.5`）；`dist/`、`frontend/dist` 永不提交（`CLAUDE.md:39-40`）。
- 前端 CSP 为 `default-src 'self'`，仅放行 `127.0.0.1:*` 的 img/media/connect（`frontend/index.html:7-11`）：调试 UI 不得引用外部域名资源。
- 运行与调试进程由用户显式触发，不进入 `permission_broker`（规格 §7.4）；但路径必须先过 `_resolve_real_path`（`gui_bridge.py:366`）再走 `core.gui_path_policy.gui_path_access_allowed`（`core/gui_path_policy.py:81`）。
- 文本编码：读 `.vscode/launch.json` / `package.json` 按 UTF-8 strict → GBK 回退（与 `read_file_preview` 口径一致）。
- 固定测试命令：后端 `venv\Scripts\python -m pytest tests/ -q`（`CLAUDE.md:23`）；前端 `cd frontend && npx vitest run <路径>`。

## 前置契约（S1 / S2 交付物，S3 只消费不修改）

**核实结论**：`ide` 分支当前只有一个提交 `c18004e`（设计规格），`core/ide/` 目录不存在，`frontend/src/stores/ideStore.ts`、`frontend/src/components/IdeLayout.vue`、`frontend/src/components/EditorPane.vue` 均不存在。因此本计划要求：**先完成 S1（IDE 骨架）与 S2（语言智能）后再执行本计划**。Task 1 的第一步就是核实这些前置文件已存在；若缺失，停止执行并回报，不要在本计划里补做 S1/S2 的内容。

S3 消费的 S1/S2 接口（签名须与 S1/S2 规格一致，S3 只读不改）：

- `frontend/src/stores/ideStore.ts`
  - `activeTab: { path: string; encoding: string; eol: string; dirty: boolean; viewState: unknown } | null`
  - `breakpoints: Set<string>`，键为 `${path}:${line}`，`path` 为归一化（正斜杠）绝对路径
  - `toggleBreakpoint(path: string, line: number): void`
  - `breakpointsFor(path: string): number[]`（升序，1-based 行号）
  - `bottomTab: 'terminal' | 'problems' | 'output' | 'debug'`，`setBottomTab(tab: string): void`
  - `bottomPanelOpen: boolean`，`toggleBottomPanel(): void`
- `frontend/src/components/IdeBottomPanel.vue`：页签表常量 `IDE_BOTTOM_TABS: { id: string; label: string }[]`，内容区用 `v-show` 常驻挂载（与 `App.vue:193` 的 `<TerminalPane v-show="store.terminalPanelOpen" />` 同范式，收起期间输出不丢）。
- `frontend/src/components/EditorPane.vue`：CodeMirror 6 `EditorView` 实例，扩展数组变量 `extraExtensions: Extension[]`（在创建 `EditorView` 时与基础扩展一起传入），文件路径来自 `props.path`。
- `frontend/package.json` 已含 `@codemirror/view` / `@codemirror/state`（S1 加入；当前 `package.json` 只有 `@xterm/xterm`、`highlight.js`、`mermaid`、`pinia`、`vue`）。
- 后端 `core/ide/__init__.py` 与 `core/ide/language_registry.py`（S2 交付）：`detect_language_id(path: str) -> str` 返回 `"python" / "javascript" / "go" / "rust" / "c" / "cpp"` 等，未知返回 `""`。

## File Structure

新增（后端）：

- `core/ide/dap_manager.py`：DAP 帧编解码（`encode_message` / `DapFramer`）+ 适配器进程与请求响应配对（`DapProcess`）。职责：只谈协议与进程，不谈"调试会话语义"。
- `core/ide/debug_adapters.py`：适配器探测与 `AdapterSpec`。职责：把"用哪个可执行文件、什么参数、什么传输方式"查出来，缺失时产出可执行的中文提示。
- `core/ide/debug_session.py`：`DebugSession`（DAP 流程编排：initialize/launch/setBreakpoints/configurationDone + 事件归一化 + launch 参数构造）。职责：把 DAP 协议时序封成一个对象，事件统一经注入的 `emit` 回调外发。
- `core/ide/run_command.py`：`build_run_plan` / `RunPlan` / `RunPlanError`。职责：文件路径 + 工作区 → 一条可直接写进 shell 的命令行。
- `tests/fake_dap_adapter.py`：测试用最小 DAP 适配器（仅标准库，不 import 项目代码），供 `debug_session` 端到端测试当真实子进程。

修改（后端）：

- `gui_bridge.py`：新增 `ide_run_file`（运行桥）与 `ide_debug_*`（调试桥）+ `_emit_dap_event` / `_shutdown_debug_session`；`__init__` 加两行调试状态；`shutdown` 加调试会话回收。

新增（前端）：

- `frontend/src/utils/dapBus.ts`：调试控制台输出的旁路总线（不经 Pinia 响应式状态）。
- `frontend/src/stores/ideDebugStore.ts`：调试会话状态、DAP 事件 reducer、调试动作。
- `frontend/src/components/DebugToolbar.vue`：运行 / 继续 / 单步 / 跳出 / 重启 / 停止。
- `frontend/src/components/DebugPane.vue`：调用栈 / 变量 / 监视 / 调试控制台。
- `frontend/src/ide/cmBreakpoints.ts`：CodeMirror 6 断点 gutter 标记与执行行高亮字段。

修改（前端）：

- `frontend/src/stores/appStore.ts`：`handleBridgeEvent` 分流 `dap_output` 与 `dap_*`（范式见 `appStore.ts:3739` 的 `useGitStore()` 调用、`appStore.ts:4507-4511` 的 `pty_data` / `pty_exit` 分支）。
- `frontend/src/components/IdeBottomPanel.vue`：新增「调试控制台」页签与 `DebugPane` 常驻挂载。
- `frontend/src/components/EditorPane.vue`：把断点扩展与执行行字段接进 `extraExtensions`。

---

### Task 1: DAP 帧编解码（Content-Length + 分片/粘包）

**Files:**
- Create: `core/ide/dap_manager.py`
- Test: `tests/test_ide_dap_framing.py`

**Interfaces:**
- Consumes: 无（本任务是包内第一个 S3 模块；`core/ide/__init__.py` 由 S2 交付）
- Produces:
  - `MAX_FRAME_BYTES: int = 32 * 1024 * 1024`
  - `class DapProtocolError(Exception)`
  - `encode_message(message: Dict[str, Any]) -> bytes`
  - `class DapFramer`：`feed(data: bytes) -> List[Dict[str, Any]]`、`pending_bytes: int`

- [ ] **Step 1: 核实 S1/S2 前置已落地**

Run: `venv\Scripts\python -c "import os;print(os.path.exists('core/ide/__init__.py'), os.path.exists('core/ide/language_registry.py'))"`
Expected: `True True`。若为 `False False`，停止执行本计划并回报「S1/S2 未落地」。

- [ ] **Step 2: 写失败测试** `tests/test_ide_dap_framing.py`

```python
"""DAP 帧编解码测试（规格：2026-10-06-ide-mode-design.md 6.2）。

DAP 与 LSP 同为 Content-Length 帧，但载荷字段不同（seq/type/command/event
对 jsonrpc/id/method）。适配器输出是管道字节流，一次 read 可能拿到半帧
（分片）或多帧（粘包），解析状态必须跨 read 保留。
"""

import pytest

from core.ide.dap_manager import DapFramer, DapProtocolError, encode_message


def _frame(payload: str) -> bytes:
    body = payload.encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


def test_encode_message_frames_utf8_body_with_length():
    data = encode_message({"seq": 1, "type": "request", "command": "initialize"})

    header, _, body = data.partition(b"\r\n\r\n")
    assert header == b"Content-Length: %d" % len(body)
    assert body.decode("utf-8") == '{"seq":1,"type":"request","command":"initialize"}'


def test_feed_parses_one_complete_frame():
    framer = DapFramer()

    messages = framer.feed(_frame('{"seq":1,"type":"response","request_seq":1,"success":true}'))

    assert len(messages) == 1
    assert messages[0]["type"] == "response" and messages[0]["request_seq"] == 1
    assert framer.pending_bytes == 0


def test_feed_parses_two_frames_in_one_chunk():
    """粘包：一次 read 拿到两条完整报文必须都解析出来。"""
    framer = DapFramer()

    messages = framer.feed(
        _frame('{"seq":1,"type":"event","event":"initialized"}')
        + _frame('{"seq":2,"type":"event","event":"terminated"}')
    )

    assert [m["event"] for m in messages] == ["initialized", "terminated"]


def test_feed_byte_by_byte_reassembles_frame():
    """分片：逐个字节喂入，最后一字节到齐才产出报文。"""
    framer = DapFramer()
    data = _frame('{"seq":7,"type":"event","event":"output","body":{"output":"hi\\n"}}')
    messages = []

    for index, byte in enumerate(data):
        messages.extend(framer.feed(bytes([byte])))
        if index < len(data) - 1:
            assert messages == [], "帧未收齐时不得产出报文"
            assert framer.pending_bytes > 0

    assert len(messages) == 1 and messages[0]["body"]["output"] == "hi\n"


def test_header_split_across_chunks():
    framer = DapFramer()

    assert framer.feed(b"Content-Length: 27\r") == []
    assert framer.feed(b"\n\r\n") == []

    messages = framer.feed(b'{"seq":1,"type":"event","e":1}')

    assert len(messages) == 1 and messages[0]["seq"] == 1


def test_body_split_across_chunks_keeps_remainder():
    framer = DapFramer()
    data = _frame('{"seq":3,"type":"response","request_seq":3,"success":true}')
    cut = 20

    assert framer.feed(data[:cut]) == []
    assert framer.pending_bytes == cut

    messages = framer.feed(data[cut:])

    assert len(messages) == 1 and messages[0]["request_seq"] == 3
    assert framer.pending_bytes == 0


def test_content_length_header_is_case_insensitive_and_extra_headers_ignored():
    framer = DapFramer()
    body = b'{"seq":1,"type":"event","event":"x"}'
    data = b"content-LENGTH: %d\r\nContent-Type: application/vscode-jsonrpc\r\n\r\n" % len(body) + body

    messages = framer.feed(data)

    assert len(messages) == 1 and messages[0]["event"] == "x"


def test_missing_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="Content-Length"):
        framer.feed(b"X-Trace: 1\r\n\r\n{}")


def test_non_integer_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="不是整数"):
        framer.feed(b"Content-Length: abc\r\n\r\n{}")


def test_negative_content_length_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="为负"):
        framer.feed(b"Content-Length: -5\r\n\r\n{}")


def test_oversized_frame_rejected_before_buffering_body():
    """畸形/超大 Content-Length 必须在读正文前拒绝，避免内存被撑爆。"""
    framer = DapFramer(max_frame_bytes=64)

    with pytest.raises(DapProtocolError, match="超过上限"):
        framer.feed(b"Content-Length: 65\r\n\r\n")


def test_invalid_json_body_raises():
    framer = DapFramer()

    with pytest.raises(DapProtocolError, match="合法 JSON"):
        framer.feed(_frame("{not json}"))
```

- [ ] **Step 3: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_ide_dap_framing.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.ide.dap_manager'`

- [ ] **Step 4: 写最小实现** `core/ide/dap_manager.py`

```python
"""DAP（Debug Adapter Protocol）帧编解码与适配器进程托管。

帧格式与 LSP 一致（ASCII 头 + Content-Length + CRLF CRLF + UTF-8 JSON 正文），
但载荷字段完全不同：DAP 用 seq / type(request|response|event) / command|event
/ arguments|body，LSP 用 jsonrpc / id / method / params。因此不复用 LSP 侧代码，
本模块自带解析器。

线程模型（本模块所有类都不触碰 WebView）：
- DapProcess 的读线程负责把字节流还原成报文，response 唤醒等待者，event 交给
  on_event 回调；on_event 的实现只允许 emit_event（内部 put_nowait 进 _js_queue）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 帧头分隔符：CRLF CRLF
_HEADER_SEPARATOR = b"\r\n\r\n"
# 单帧上限：variables 响应可能很大，给 32MB；超过即判协议错，
# 防止畸形 Content-Length 让内存被撑爆
MAX_FRAME_BYTES = 32 * 1024 * 1024


class DapProtocolError(Exception):
    """DAP 帧解析或通道状态异常（头不完整、Content-Length 非法、帧超限、通道关闭）。"""


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
        """喂入一段字节，返回本次新增的完整报文（可能为空列表）。"""
        if data:
            self._buffer.extend(data)
        messages: List[Dict[str, Any]] = []
        while True:
            header_end = self._buffer.find(_HEADER_SEPARATOR)
            if header_end < 0:
                return messages
            length = self._parse_content_length(bytes(self._buffer[:header_end]))
            body_start = header_end + len(_HEADER_SEPARATOR)
            if length > self._max_frame_bytes:
                raise DapProtocolError(f"DAP 帧超过上限：{length} 字节")
            if len(self._buffer) < body_start + length:
                return messages  # 正文未到齐：保留缓冲，等下一次 feed
            body = bytes(self._buffer[body_start:body_start + length])
            del self._buffer[:body_start + length]
            messages.append(self._decode_body(body))

    @staticmethod
    def _parse_content_length(header: bytes) -> int:
        for line in header.split(b"\r\n"):
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
```

- [ ] **Step 5: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_ide_dap_framing.py -q`
Expected: `12 passed`

- [ ] **Step 6: 提交**

```bash
git add core/ide/dap_manager.py tests/test_ide_dap_framing.py
git commit -m "feat(ide): DAP 帧编解码（Content-Length/分片/粘包）"
```

---

### Task 2: 调试适配器供给（debugpy / dlv / lldb-dap 探测）

**Files:**
- Create: `core/ide/debug_adapters.py`
- Test: `tests/test_ide_debug_adapters.py`

**Interfaces:**
- Consumes: `core/ide/language_registry.py` 的语言 id（S2 交付，本任务只接收字符串）
- Produces:
  - `STDIO: str = "stdio"`、`TCP: str = "tcp"`
  - `@dataclass(frozen=True) class AdapterSpec`：`adapter_id: str`、`language_id: str`、`command: List[str]`、`transport: str = STDIO`、`cwd: str = ""`
  - `@dataclass(frozen=True) class AdapterProbe`：`ok: bool`、`adapter_id: str`、`spec: Optional[AdapterSpec] = None`、`hint: str = ""`
  - `LANGUAGE_ADAPTERS: Dict[str, str]`
  - `probe_adapter(language_id: str) -> AdapterProbe`
  - `adapter_availability() -> List[Dict[str, object]]`

- [ ] **Step 1: 写失败测试** `tests/test_ide_debug_adapters.py`

```python
"""调试适配器探测测试（规格 6.2：Python 用 debugpy、Go 用 dlv、C/C++/Rust 用 lldb-dap）。"""

import sys

import pytest

from core.ide import debug_adapters as da


def test_python_without_debugpy_returns_pip_hint(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: False)

    probe = da.probe_adapter("python")

    assert probe.ok is False and probe.adapter_id == "debugpy"
    assert "pip install debugpy" in probe.hint
    # 提示里的解释器必须是当前解释器：debugpy 装在别处时 -m debugpy.adapter 找不到
    assert sys.executable in probe.hint


def test_python_with_debugpy_returns_stdio_spec(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: True)

    probe = da.probe_adapter("python")

    assert probe.ok is True
    assert probe.spec.command == [sys.executable, "-m", "debugpy.adapter"]
    assert probe.spec.transport == da.STDIO


def test_go_uses_dlv_dap_over_tcp(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: r"C:\tools\dlv.exe" if name == "dlv" else None)

    probe = da.probe_adapter("go")

    assert probe.ok is True
    assert probe.spec.command == [r"C:\tools\dlv.exe", "dap", "--listen=127.0.0.1:0"]
    assert probe.spec.transport == da.TCP


def test_go_missing_dlv_hints_go_install(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    probe = da.probe_adapter("go")

    assert probe.ok is False and "go install" in probe.hint and "dlv" in probe.hint


def test_cpp_prefers_lldb_dap_stdio(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: r"C:\LLVM\bin\lldb-dap.exe" if name == "lldb-dap" else None)

    probe = da.probe_adapter("cpp")

    assert probe.ok is True
    assert probe.spec.command == [r"C:\LLVM\bin\lldb-dap.exe", "--stdio"]
    assert probe.spec.transport == da.STDIO


def test_cpp_falls_back_to_lldb_vscode(monkeypatch):
    mapping = {"lldb-vscode": r"C:\LLVM\bin\lldb-vscode.exe"}
    monkeypatch.setattr(da.shutil, "which", lambda name: mapping.get(name))

    probe = da.probe_adapter("c")

    assert probe.ok is True and probe.spec.command[0].endswith("lldb-vscode.exe")


def test_cpp_missing_lldb_hint_mentions_llvm(monkeypatch):
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    probe = da.probe_adapter("rust")

    assert probe.ok is False and "LLVM" in probe.hint


def test_unsupported_language_returns_hint():
    probe = da.probe_adapter("java")

    assert probe.ok is False and "java" in probe.hint and probe.spec is None


def test_availability_lists_all_supported_languages(monkeypatch):
    monkeypatch.setattr(da, "_run_probe", lambda command, timeout=10.0: False)
    monkeypatch.setattr(da.shutil, "which", lambda name: None)

    rows = da.adapter_availability()

    assert [row["language_id"] for row in rows] == ["python", "go", "c", "cpp", "rust"]
    assert all(row["available"] is False for row in rows)
    assert all(str(row["hint"]) for row in rows)


def test_run_probe_returns_false_when_executable_missing():
    """真探测路径：不存在的可执行文件必须返回 False 而不是抛异常。"""
    assert da._run_probe(["definitely-not-an-executable-ycode"], timeout=5.0) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_ide_debug_adapters.py -q`
Expected: FAIL，`ImportError: cannot import name 'debug_adapters'`

- [ ] **Step 3: 写实现** `core/ide/debug_adapters.py`

```python
"""调试适配器供给：定位 debugpy / dlv / lldb-dap 并给出缺失时的可执行提示。

只用标准库；不自动安装任何东西（Python 侧不引入第三方依赖）。缺失时返回
带安装命令的中文提示，由用户在终端自行安装——星瑶不替用户改环境。

传输方式差异（实测口径）：
- debugpy / lldb-dap 走 stdio：直接对适配器进程的 stdin/stdout 说 DAP。
- dlv dap 只支持 TCP：进程启动后监听随机端口并把端口打进 stderr，
  因此用 --listen=127.0.0.1:0，由 DapProcess 从 stderr 解析端口再连。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional

STDIO = "stdio"
TCP = "tcp"

# 语言 id -> 适配器 id（语言 id 由 S2 的 language_registry 提供）
LANGUAGE_ADAPTERS: Dict[str, str] = {
    "python": "debugpy",
    "go": "dlv",
    "c": "lldb-dap",
    "cpp": "lldb-dap",
    "rust": "lldb-dap",
}


@dataclass(frozen=True)
class AdapterSpec:
    """一个可用的调试适配器启动规格。"""

    adapter_id: str           # "debugpy" / "dlv" / "lldb-dap"
    language_id: str
    command: List[str]        # 完整启动命令
    transport: str = STDIO    # STDIO / TCP
    cwd: str = ""


@dataclass(frozen=True)
class AdapterProbe:
    """探测结果：ok=True 时 spec 有效，否则 hint 是给用户的中文说明。"""

    ok: bool
    adapter_id: str
    spec: Optional[AdapterSpec] = None
    hint: str = ""


def _run_probe(command: List[str], timeout: float = 10.0) -> bool:
    """跑一条探测命令，退出码 0 视为可用（输出丢弃，绝不弹控制台窗口）。"""
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _debugpy_python() -> str:
    """debugpy 必须装在与星瑶同一解释器里，否则 -m debugpy.adapter 找不到模块。"""
    return sys.executable or "python"


def probe_adapter(language_id: str) -> AdapterProbe:
    """探测某语言的调试适配器；未支持的语言也返回 probe（ok=False + 提示）。"""
    adapter_id = LANGUAGE_ADAPTERS.get(language_id, "")
    if not adapter_id:
        return AdapterProbe(False, "", hint=f"星瑶暂不支持调试该语言：{language_id or '未知'}")
    if adapter_id == "debugpy":
        python = _debugpy_python()
        if _run_probe([python, "-c", "import debugpy"]):
            return AdapterProbe(
                True, adapter_id,
                AdapterSpec(adapter_id, language_id, [python, "-m", "debugpy.adapter"], STDIO),
            )
        return AdapterProbe(
            False, adapter_id,
            hint=(
                "未安装 debugpy（Python 调试适配器）。请在本机终端执行："
                f'"{python}" -m pip install debugpy'
            ),
        )
    if adapter_id == "dlv":
        path = shutil.which("dlv")
        if path:
            return AdapterProbe(
                True, adapter_id,
                AdapterSpec(adapter_id, language_id, [path, "dap", "--listen=127.0.0.1:0"], TCP),
            )
        return AdapterProbe(
            False, adapter_id,
            hint="未找到 dlv（Go 调试器）。安装：go install github.com/go-delve/delve/cmd/dlv@latest",
        )
    lldb_dap = shutil.which("lldb-dap") or shutil.which("lldb-vscode")
    if lldb_dap:
        return AdapterProbe(
            True, adapter_id,
            AdapterSpec(adapter_id, language_id, [lldb_dap, "--stdio"], STDIO),
        )
    return AdapterProbe(
        False, adapter_id,
        hint=(
            "未找到 lldb-dap（LLVM 调试适配器）。安装 LLVM 后确认 lldb-dap 在 PATH 中"
            "（Windows 版 LLVM 自带 lldb-dap.exe）"
        ),
    )


def adapter_availability() -> List[Dict[str, object]]:
    """给调试面板与设置页用的全量探测摘要（每语言一条）。"""
    rows: List[Dict[str, object]] = []
    for language_id in ("python", "go", "c", "cpp", "rust"):
        probe = probe_adapter(language_id)
        rows.append({
            "language_id": language_id,
            "adapter_id": LANGUAGE_ADAPTERS[language_id],
            "available": probe.ok,
            "hint": probe.hint,
        })
    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_ide_debug_adapters.py -q`
Expected: `10 passed`

- [ ] **Step 5: 提交**

```bash
git add core/ide/debug_adapters.py tests/test_ide_debug_adapters.py
git commit -m "feat(ide): 调试适配器探测（debugpy/dlv/lldb-dap）与缺失提示"
```

---

### Task 3: DAP 适配器进程与请求响应配对

**Files:**
- Modify: `core/ide/dap_manager.py`（在 Task 1 的帧编解码之后追加进程部分）
- Test: `tests/test_ide_dap_process.py`

**Interfaces:**
- Consumes: `core/ide/dap_manager.py` 的 `DapFramer` / `encode_message` / `DapProtocolError`；`core/ide/debug_adapters.py` 的 `AdapterSpec` / `STDIO` / `TCP`；`core/process_io.py` 的 `decode_process_output(data, expected_encoding)`（`core/process_io.py:56`）
- Produces:
  - `class DapRequestError(DapProtocolError)`
  - `DEFAULT_REQUEST_TIMEOUT: float = 10.0`、`TCP_PORT_TIMEOUT: float = 15.0`
  - `class DapProcess`：`__init__(spec, cwd, on_event=None, on_stderr=None, spawn=None, request_timeout=DEFAULT_REQUEST_TIMEOUT)`、`start() -> Dict[str, Any]`、`pid: Optional[int]`、`is_alive() -> bool`、`send_request(command, arguments=None, timeout=None) -> Dict[str, Any]`、`send_request_async(command, arguments=None) -> int`、`await_response(seq, timeout=None) -> Dict[str, Any]`、`terminate(timeout=5.0) -> None`

- [ ] **Step 1: 写失败测试** `tests/test_ide_dap_process.py`

```python
"""DapProcess 测试：用注入的假进程覆盖请求配对、事件派发、反向请求与回收。

不依赖 debugpy/dlv 安装，也不启动真子进程——真进程端到端在 Task 4 由
tests/fake_dap_adapter.py 覆盖。
"""

import threading

import pytest

from core.ide.dap_manager import DapProcess, DapProtocolError, DapRequestError, encode_message
from core.ide.debug_adapters import STDIO, AdapterSpec


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
    proc.stdin = _FakeSink(drip=1)
    process = _make_process(proc)
    out.feed(encode_message({"seq": 1, "type": "response", "request_seq": 1, "success": True, "body": {}}))

    process.send_request("threads", {})

    assert bytes(proc.stdin.data).endswith(b"{}")
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_ide_dap_process.py -q`
Expected: FAIL，`ImportError: cannot import name 'DapProcess'`

- [ ] **Step 3: 追加实现**（`core/ide/dap_manager.py` 末尾）

先把文件顶部的导入补齐：

```python
import os
import re
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from core.process_io import decode_process_output
```

再追加：

```python
DEFAULT_REQUEST_TIMEOUT = 10.0
# dlv dap 只支持 TCP：进程启动后把随机端口打进 stderr，等端口的超时
TCP_PORT_TIMEOUT = 15.0
_TCP_PORT_RE = re.compile(rb"127\.0\.0\.1:(\d+)")


class DapRequestError(DapProtocolError):
    """请求失败：超时、适配器返回 success=false、或通道已断开。"""


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
    """强杀适配器进程树（范式同 core/git/runner.py:104 的 _kill_process_tree）。"""
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
    """一个调试适配器进程的 DAP 通道（stdio 或 TCP）。"""

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
            if not self._port_ready.wait(TCP_PORT_TIMEOUT):
                self.terminate()
                raise DapProtocolError(
                    f"{self._spec.adapter_id} 未在 {TCP_PORT_TIMEOUT}s 内输出监听端口"
                )
            self._sock = socket.create_connection(("127.0.0.1", self._port), timeout=5.0)
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
                _kill_process_tree(proc)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=2.0)
        if self._stderr_reader is not None and self._stderr_reader is not threading.current_thread():
            self._stderr_reader.join(timeout=1.0)
        self._fail_pending("调试会话已结束")

    # ------------------------------------------------------------------ 请求与响应
    def send_request(
        self,
        command: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        return self.await_response(self.send_request_async(command, arguments), timeout=timeout)

    def send_request_async(self, command: str, arguments: Optional[Dict[str, Any]] = None) -> int:
        """发出请求但不等响应，返回 seq。

        为什么需要它：launch 请求的响应要等 configurationDone 之后、被调试程序
        真正启动才回来，同步等待会把"启动调试"卡死在桥方法里。
        """
        if self._proc is None:
            raise DapProtocolError("调试适配器未启动")
        if self._closed:
            raise DapProtocolError("调试会话已结束")
        seq = self._take_seq()
        with self._lock:
            self._waiters[seq] = threading.Event()
        self._write_message({
            "seq": seq, "type": "request", "command": command, "arguments": arguments or {},
        })
        return seq

    def await_response(self, seq: int, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            waiter = self._waiters.get(seq)
        if waiter is None:
            raise DapProtocolError(f"请求 {seq} 不在等待列表中")
        if not waiter.wait(timeout if timeout is not None else self._request_timeout):
            with self._lock:
                self._waiters.pop(seq, None)
            raise DapRequestError(f"调试适配器响应超时（请求 seq={seq}）")
        with self._lock:
            response = self._responses.pop(seq, None)
            self._waiters.pop(seq, None)
        if response is None:
            raise DapRequestError(self._failure_reason or f"请求 seq={seq} 未收到响应")
        if not response.get("success", False):
            detail = str(response.get("message") or "").strip() or "适配器未说明原因"
            raise DapRequestError(f"{response.get('command') or '请求'} 失败：{detail}")
        body = response.get("body")
        return body if isinstance(body, dict) else {}

    def _take_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def _write_message(self, message: Dict[str, Any]) -> None:
        if self._writer is None:
            raise DapProtocolError("调试适配器输入通道不可用")
        data = encode_message(message)
        with self._write_lock:
            _write_all(self._writer, data)

    # ------------------------------------------------------------------ 读线程
    def _reader_loop(self) -> None:
        try:
            while True:
                chunk = self._reader_source.read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                for message in self._framer.feed(chunk):
                    self._dispatch(message)
        except DapProtocolError:
            logger.exception("调试适配器输出解析失败")
        except Exception:
            logger.debug("调试适配器读线程结束", exc_info=True)
        finally:
            self._fail_pending("调试适配器已断开")

    def _stderr_loop(self) -> None:
        stream = self._proc.stderr
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                text = chunk if isinstance(chunk, str) else decode_process_output(chunk, "utf-8")
                if self._spec.transport == TCP and not self._port_ready.is_set():
                    # dlv 打印监听地址的措辞可能随版本变化，用宽松正则只抓端口
                    match = _TCP_PORT_RE.search(text.encode("utf-8", "ignore"))
                    if match:
                        self._port = int(match.group(1))
                        self._port_ready.set()
                if self._on_stderr is not None:
                    try:
                        self._on_stderr(text)
                    except Exception:
                        logger.debug("stderr 回调异常", exc_info=True)
        except Exception:
            logger.debug("调试适配器 stderr 读取结束", exc_info=True)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "response":
            request_seq = int(message.get("request_seq") or 0)
            with self._lock:
                waiter = self._waiters.get(request_seq)
                if waiter is not None:
                    self._responses[request_seq] = message
            if waiter is not None:
                waiter.set()
            return
        if kind == "event":
            if self._on_event is not None:
                body = message.get("body")
                try:
                    self._on_event(str(message.get("event") or ""), body if isinstance(body, dict) else {})
                except Exception:
                    logger.debug("调试事件回调异常", exc_info=True)
            return
        if kind == "request":
            # 适配器反向请求（runInTerminal / startDebugging 等）：星瑶不在终端里
            # 代跑，明确回绝，让适配器按"不支持"继续，避免它死等一个永不来的响应
            self._write_message({
                "seq": self._take_seq(),
                "type": "response",
                "request_seq": int(message.get("seq") or 0),
                "success": False,
                "command": str(message.get("command") or ""),
                "message": "星瑶暂不支持调试适配器的反向请求",
            })

    def _fail_pending(self, reason: str) -> None:
        """唤醒所有等待者并记录原因：通道断了不能让 send_request 永久阻塞。"""
        with self._lock:
            waiters = list(self._waiters.values())
            self._failure_reason = reason
        for waiter in waiters:
            waiter.set()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_ide_dap_process.py -q`
Expected: `13 passed`

- [ ] **Step 5: 回归帧编解码**

Run: `venv\Scripts\python -m pytest tests/test_ide_dap_framing.py tests/test_ide_dap_process.py -q`
Expected: `25 passed`

- [ ] **Step 6: 提交**

```bash
git add core/ide/dap_manager.py tests/test_ide_dap_process.py
git commit -m "feat(ide): DAP 适配器进程托管（请求配对/事件派发/进程回收）"
```

---

### Task 4: 调试会话编排（initialize → launch → 断点 → configurationDone）

**Files:**
- Create: `core/ide/debug_session.py`
- Create: `tests/fake_dap_adapter.py`
- Test: `tests/test_ide_debug_session.py`

**Interfaces:**
- Consumes: `core/ide/dap_manager.py` 的 `DapProcess` / `DapRequestError` / `DapProtocolError`；`core/ide/debug_adapters.py` 的 `AdapterSpec`
- Produces:
  - `STOP_EVENT_TIMEOUT: float = 10.0`、`LAUNCH_TIMEOUT: float = 20.0`
  - `class DebugSessionError(Exception)`
  - `build_launch_arguments(adapter_id, language_id, path, cwd, program="", args=None, env=None, python="", stop_on_entry=False) -> Dict[str, Any]`
  - `class DebugSession`：`session_id: str`、`start() -> Dict[str, Any]`、`set_breakpoints(path, lines) -> List[Dict[str, Any]]`、`configuration_done() -> None`、`continue_(thread_id)`、`pause(thread_id)`、`next_(thread_id)`、`step_in(thread_id)`、`step_out(thread_id)`、`restart()`、`threads()`、`stack_trace(thread_id, levels=20)`、`scopes(frame_id)`、`variables(variables_reference)`、`evaluate(expression, frame_id=0, context="repl")`、`is_alive() -> bool`、`stop() -> None`

- [ ] **Step 1: 写测试用假适配器** `tests/fake_dap_adapter.py`

```python
"""测试用最小 DAP 适配器（仅标准库，不 import 项目代码）。

不是真调试器：只复现 DAP 时序与事件流，让 core.ide.debug_session 的端到端
测试不依赖 debugpy 安装。用环境变量 FAKE_DAP_NO_RESTART=1 模拟不支持重启。

时序（与 debugpy 一致）：
  initialize -> 响应 + initialized 事件
  launch     -> 先不回，等 configurationDone 后再回（真适配器行为）
  setBreakpoints -> 响应 verified 列表
  configurationDone -> 响应，补发 launch 响应 + output 事件 + stopped 事件
"""

import json
import os
import sys
import threading

STOP_EVENT_SENT = threading.Event()


def encode(message):
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n" % len(body) + body


class Stub:
    def __init__(self):
        self.seq = 0
        self.launch = None
        self.breakpoints = {}

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
        if command == "initialize":
            capabilities = {"supportsConfigurationDoneRequest": True}
            if not os.environ.get("FAKE_DAP_NO_RESTART"):
                capabilities["supportsRestartRequest"] = True
            self.ok(request, {"capabilities": capabilities})
            self.event("initialized")
            return
        if command == "launch":
            self.launch = request
            return  # 故意延后到 configurationDone 再回
        if command == "setBreakpoints":
            source = (request.get("arguments") or {}).get("source") or {}
            lines = [int(bp.get("line") or 0) for bp in ((request.get("arguments") or {}).get("breakpoints") or [])]
            self.breakpoints[source.get("path", "")] = lines
            self.ok(request, {"breakpoints": [{"verified": True, "line": line} for line in lines]})
            return
        if command == "configurationDone":
            self.ok(request)
            if self.launch is not None:
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
            stop = True
            os._exit(0) if stop else None  # 适配器被 disconnect 后自行退出，模拟真适配器
            return
        self.fail(request, f"stub 不认识命令：{command}")


def main():
    stub = Stub()
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
```

- [ ] **Step 2: 写失败测试** `tests/test_ide_debug_session.py`

```python
"""DebugSession 端到端测试：以 sys.executable 跑 tests/fake_dap_adapter.py 真进程。

覆盖（规格 9 的 dap_manager 条目）：断点与停止事件流转、调用栈/变量/求值、
单步与继续、重启能力探测、停止后进程回收。
"""

import os
import sys
import threading
import time

import pytest

from core.ide.debug_adapters import STDIO, AdapterSpec
from core.ide.debug_session import DebugSession, DebugSessionError, build_launch_arguments

STUB = os.path.join(os.path.dirname(__file__), "fake_dap_adapter.py")


def _spec(no_restart=False):
    return AdapterSpec("debugpy", "python", [sys.executable, STUB], STDIO)


def _wait(event, timeout=10.0):
    assert event.wait(timeout), "事件未在超时内到达"


@pytest.fixture()
def collected():
    return {"events": [], "stopped": threading.Event(), "output": threading.Event()}


def _session(collected, no_restart=False, **kwargs):
    def emit(event_type, data):
        collected["events"].append((event_type, data))
        if event_type == "dap_stopped":
            collected["stopped"].set()
        if event_type == "dap_output":
            collected["output"].set()

    if no_restart:
        os.environ["FAKE_DAP_NO_RESTART"] = "1"
    session = DebugSession(
        _spec(no_restart), program=r"C:\proj\app.py", cwd=os.getcwd(), emit=emit, **kwargs
    )
    return session


def _types(collected):
    return [event_type for event_type, _ in collected["events"]]


def test_build_launch_arguments_per_adapter():
    python_payload = build_launch_arguments(
        "debugpy", "python", r"C:\proj\app.py", r"C:\proj", args=["--x"], python=sys.executable
    )
    assert python_payload["program"] == r"C:\proj\app.py"
    assert python_payload["python"] == [sys.executable]
    assert python_payload["request"] == "launch" and python_payload["console"] == "internalConsole"
    assert python_payload["args"] == ["--x"] and python_payload["justMyCode"] is True

    go_payload = build_launch_arguments("dlv", "go", r"C:\proj\main.go", r"C:\proj")
    assert go_payload["mode"] == "debug" and go_payload["type"] == "go"

    lldb_payload = build_launch_arguments(
        "lldb-dap", "cpp", r"C:\proj\build\app.exe", r"C:\proj", program=r"C:\proj\build\app.exe"
    )
    assert lldb_payload["type"] == "lldb" and lldb_payload["program"] == r"C:\proj\build\app.exe"


def test_start_returns_capabilities_and_emits_stopped_with_top_frame(collected):
    session = _session(collected)
    try:
        info = session.start()
        assert info["adapter"] == "debugpy" and info["capabilities"]["supportsConfigurationDoneRequest"] is True

        verified = session.set_breakpoints(r"C:\proj\app.py", [7, 11])
        assert [bp["line"] for bp in verified] == [7, 11]

        session.configuration_done()

        _wait(collected["stopped"])
        stopped = [data for kind, data in collected["events"] if kind == "dap_stopped"][0]
        assert stopped["reason"] == "breakpoint" and stopped["thread_id"] == 1
        assert stopped["path"] == r"C:\proj\app.py" and stopped["line"] == 7
        assert stopped["frame_id"] == 1001
    finally:
        session.stop()


def test_output_event_carries_category_and_text(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["output"])
        outputs = [data for kind, data in collected["events"] if kind == "dap_output"]
        assert any("stub adapter ready" in data["text"] for data in outputs)
        assert all(data["category"] in {"stdout", "stderr", "console"} for data in outputs)
    finally:
        session.stop()


def test_inspect_requests_return_normalized_shapes(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["stopped"])

        assert session.threads() == [{"id": 1, "name": "MainThread"}]
        frames = session.stack_trace(1, levels=1)
        assert frames[0]["id"] == 1001 and frames[0]["line"] == 7
        scopes = session.scopes(1001)
        assert scopes[0]["variablesReference"] == 2001
        variables = session.variables(2001)
        assert variables[0]["name"] == "x" and variables[0]["value"] == "42"
        result = session.evaluate("1+1", frame_id=1001)
        assert result["result"] == "eval:1+1" and result["type"] == "str"
    finally:
        session.stop()


def test_continue_and_step_emit_expected_events(collected):
    session = _session(collected)
    try:
        session.start()
        session.set_breakpoints(r"C:\proj\app.py", [7])
        session.configuration_done()
        _wait(collected["stopped"])

        session.continue_(1)
        assert "dap_continued" in _types(collected)

        collected["stopped"].clear()
        session.next_(1)
        _wait(collected["stopped"])
    finally:
        session.stop()


def test_restart_without_capability_raises(collected):
    session = _session(collected, no_restart=True)
    try:
        session.start()
        with pytest.raises(DebugSessionError, match="不支持重启"):
            session.restart()
    finally:
        os.environ.pop("FAKE_DAP_NO_RESTART", None)
        session.stop()


def test_stop_reaps_adapter_process(collected):
    session = _session(collected)
    session.start()
    session.set_breakpoints(r"C:\proj\app.py", [7])
    session.configuration_done()
    _wait(collected["stopped"])
    assert session.is_alive() is True

    session.stop()

    assert session.is_alive() is False


def test_stop_is_idempotent(collected):
    session = _session(collected)
    session.start()
    session.stop()
    session.stop()
    assert session.is_alive() is False


def test_start_fails_when_adapter_exits_before_initialized(collected):
    """适配器起来就死（命令写错）时必须报错，不能永久等待。"""
    session = DebugSession(
        AdapterSpec("debugpy", "python", [sys.executable, "-c", "raise SystemExit(1)"], STDIO),
        program=r"C:\proj\app.py",
        cwd=os.getcwd(),
        emit=lambda event_type, data: None,
        request_timeout=1.0,
    )
    started = time.monotonic()
    with pytest.raises(DebugSessionError):
        session.start()
    assert time.monotonic() - started < 15.0
    session.stop()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_ide_debug_session.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'core.ide.debug_session'`

- [ ] **Step 4: 写实现** `core/ide/debug_session.py`

```python
"""DAP 调试会话编排：把一次「启动调试 → 断点 → 单步 → 变量 → 停止」收敛成一个对象。

时序以 debugpy 为准（lldb-dap 同序）：initialize → launch（异步发）→ 等
initialized 事件 → setBreakpoints（逐文件）→ configurationDone → 再收 launch
响应。launch 响应故意延后收：真适配器要等被调试程序真正启动才回，早收会卡死。

事件归一化后统一经注入的 emit(event_type, data) 外发；emit 的实现只允许
gui_bridge.emit_event（内部 put_nowait 进 _js_queue），本模块不触碰 WebView。
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.ide.dap_manager import DapProcess, DapProtocolError, DapRequestError

logger = logging.getLogger(__name__)

STOP_EVENT_TIMEOUT = 10.0
LAUNCH_TIMEOUT = 20.0


class DebugSessionError(Exception):
    """调试会话失败（承载可直接展示给用户的中文消息）。"""


def build_launch_arguments(
    adapter_id: str,
    language_id: str,
    path: str,
    cwd: str,
    program: str = "",
    args: Optional[List[str]] = None,
    env: Optional[Dict[str, str]] = None,
    python: str = "",
    stop_on_entry: bool = False,
) -> Dict[str, Any]:
    """按适配器构造 DAP launch 参数（program 缺省用当前文件）。"""
    arguments = list(args or [])
    target = program or path
    base: Dict[str, Any] = {
        "request": "launch",
        "program": target,
        "cwd": cwd,
        "args": arguments,
        "env": dict(env or {}),
        "stopOnEntry": bool(stop_on_entry),
    }
    if adapter_id == "debugpy":
        return {
            **base,
            "type": "python",
            "console": "internalConsole",
            "justMyCode": True,
            "redirectOutput": True,
            "python": [python] if python else [],
        }
    if adapter_id == "dlv":
        return {**base, "type": "go", "mode": "debug"}
    return {**base, "type": "lldb"}


class DebugSession:
    """一次调试会话：DAP 流程编排 + 事件归一化。"""

    def __init__(
        self,
        spec,
        program: str,
        cwd: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        emit: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        spawn: Optional[Callable[..., Any]] = None,
        request_timeout: float = 10.0,
        stop_on_entry: bool = False,
        python: str = "",
    ):
        self.session_id = f"dap-{uuid.uuid4().hex[:8]}"
        self._spec = spec
        self._program = program
        self._cwd = cwd
        self._args = list(args or [])
        self._env = dict(env or {})
        self._emit = emit or (lambda event_type, data: None)
        self._stop_on_entry = stop_on_entry
        self._python = python
        self._initialized = threading.Event()
        self._stopped_lock = threading.Lock()
        self._capabilities: Dict[str, Any] = {}
        self._launch_seq = 0
        self._process = DapProcess(
            spec,
            cwd=cwd,
            on_event=self._on_adapter_event,
            on_stderr=lambda text: self._emit(
                "dap_output",
                {"session_id": self.session_id, "category": "stderr", "text": text},
            ),
            spawn=spawn,
            request_timeout=request_timeout,
        )

    # ------------------------------------------------------------------ 生命周期
    def is_alive(self) -> bool:
        return self._process.is_alive()

    def start(self) -> Dict[str, Any]:
        info = self._process.start()
        response = self._process.send_request("initialize", {
            "clientID": "ycode",
            "clientName": "星瑶",
            "adapterID": self._spec.adapter_id,
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsVariableType": True,
            "supportsVariablePaging": False,
            "supportsRunInTerminalRequest": False,
        })
        self._capabilities = dict(response.get("capabilities") or {})
        self._launch_seq = self._process.send_request_async("launch", self._launch_arguments())
        if not self._initialized.wait(STOP_EVENT_TIMEOUT):
            self.stop()
            raise DebugSessionError("调试适配器未在超时内就绪（未收到 initialized 事件）")
        return {
            "session_id": self.session_id,
            "adapter": self._spec.adapter_id,
            "pid": info.get("pid"),
            "capabilities": self._capabilities,
        }

    def _launch_arguments(self) -> Dict[str, Any]:
        return build_launch_arguments(
            self._spec.adapter_id,
            self._spec.language_id,
            self._program,
            self._cwd,
            args=self._args,
            env=self._env,
            python=self._python,
            stop_on_entry=self._stop_on_entry,
        )

    def set_breakpoints(self, path: str, lines: List[int]) -> List[Dict[str, Any]]:
        unique = sorted({int(line) for line in lines if int(line) > 0})
        body = self._process.send_request("setBreakpoints", {
            "source": {"name": os.path.basename(path), "path": path},
            "breakpoints": [{"line": line} for line in unique],
        })
        return [bp for bp in (body.get("breakpoints") or []) if isinstance(bp, dict)]

    def configuration_done(self) -> None:
        if self._capabilities.get("supportsConfigurationDoneRequest", True):
            self._process.send_request("configurationDone", {})
        if self._launch_seq:
            # 启动失败（解释器缺失、程序路径错）在这里才暴露，必须收这次响应
            self._process.await_response(self._launch_seq, timeout=LAUNCH_TIMEOUT)

    def stop(self) -> None:
        try:
            if self._process.is_alive():
                self._process.send_request("disconnect", {"terminateDebuggee": True}, timeout=5.0)
        except (DapProtocolError, DapRequestError) as e:
            logger.debug("disconnect 未成功，直接回收进程：%s", e)
        finally:
            self._process.terminate()

    # ------------------------------------------------------------------ 执行控制
    def continue_(self, thread_id: int) -> Dict[str, Any]:
        return self._process.send_request("continue", {"threadId": int(thread_id), "singleThread": False})

    def pause(self, thread_id: int) -> Dict[str, Any]:
        return self._process.send_request("pause", {"threadId": int(thread_id)})

    def next_(self, thread_id: int) -> Dict[str, Any]:
        return self._process.send_request("next", {"threadId": int(thread_id)})

    def step_in(self, thread_id: int) -> Dict[str, Any]:
        return self._process.send_request("stepIn", {"threadId": int(thread_id)})

    def step_out(self, thread_id: int) -> Dict[str, Any]:
        return self._process.send_request("stepOut", {"threadId": int(thread_id)})

    def restart(self) -> Dict[str, Any]:
        if not self._capabilities.get("supportsRestartRequest"):
            raise DebugSessionError("当前调试适配器不支持重启，请停止后重新启动调试")
        return self._process.send_request("restart", {})

    # ------------------------------------------------------------------ 查询
    def threads(self) -> List[Dict[str, Any]]:
        body = self._process.send_request("threads", {})
        return [
            {"id": int(item.get("id") or 0), "name": str(item.get("name") or "")}
            for item in (body.get("threads") or []) if isinstance(item, dict)
        ]

    def stack_trace(self, thread_id: int, levels: int = 20) -> List[Dict[str, Any]]:
        body = self._process.send_request("stackTrace", {
            "threadId": int(thread_id), "startFrame": 0, "levels": int(levels),
        })
        frames: List[Dict[str, Any]] = []
        for raw in body.get("stackFrames") or []:
            if not isinstance(raw, dict):
                continue
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            frames.append({
                "id": int(raw.get("id") or 0),
                "name": str(raw.get("name") or ""),
                "path": str(source.get("path") or ""),
                "line": int(raw.get("line") or 0),
                "column": int(raw.get("column") or 0),
            })
        return frames

    def scopes(self, frame_id: int) -> List[Dict[str, Any]]:
        body = self._process.send_request("scopes", {"frameId": int(frame_id)})
        return [
            {
                "name": str(item.get("name") or ""),
                "variablesReference": int(item.get("variablesReference") or 0),
                "expensive": bool(item.get("expensive")),
            }
            for item in (body.get("scopes") or []) if isinstance(item, dict)
        ]

    def variables(self, variables_reference: int) -> List[Dict[str, Any]]:
        body = self._process.send_request("variables", {"variablesReference": int(variables_reference)})
        return [
            {
                "name": str(item.get("name") or ""),
                "value": str(item.get("value") or ""),
                "type": str(item.get("type") or ""),
                "variablesReference": int(item.get("variablesReference") or 0),
            }
            for item in (body.get("variables") or []) if isinstance(item, dict)
        ]

    def evaluate(self, expression: str, frame_id: int = 0, context: str = "repl") -> Dict[str, Any]:
        body = self._process.send_request("evaluate", {
            "expression": str(expression), "frameId": int(frame_id), "context": context,
        })
        return {
            "result": str(body.get("result") or ""),
            "type": str(body.get("type") or ""),
            "variablesReference": int(body.get("variablesReference") or 0),
        }

    # ------------------------------------------------------------------ 适配器事件
    def _on_adapter_event(self, name: str, body: Dict[str, Any]) -> None:
        if name == "initialized":
            self._initialized.set()
            return
        if name == "output":
            self._emit("dap_output", {
                "session_id": self.session_id,
                "category": str(body.get("category") or "stdout"),
                "text": str(body.get("output") or ""),
            })
            return
        if name == "stopped":
            # 关键：本方法运行在 DAP 读线程里，而取顶部栈帧要发 stackTrace 请求、
            # 等读线程自己把响应读回来——同步做必然死锁。因此另起短线程处理。
            threading.Thread(
                target=self._handle_stopped, args=(body,), daemon=True, name="ide-dap-stopped"
            ).start()
            return
        if name == "continued":
            self._emit("dap_continued", {
                "session_id": self.session_id, "thread_id": int(body.get("threadId") or 0),
            })
            return
        if name == "exited":
            self._emit("dap_exited", {
                "session_id": self.session_id, "exit_code": int(body.get("exitCode") or 0),
            })
            return
        if name == "terminated":
            self._emit("dap_terminated", {"session_id": self.session_id})
            return
        if name == "breakpoint":
            breakpoint = body.get("breakpoint") if isinstance(body.get("breakpoint"), dict) else {}
            source = breakpoint.get("source") if isinstance(breakpoint.get("source"), dict) else {}
            self._emit("dap_breakpoint_changed", {
                "session_id": self.session_id,
                "path": str(source.get("path") or ""),
                "line": int(breakpoint.get("line") or 0),
                "verified": bool(breakpoint.get("verified")),
            })

    def _handle_stopped(self, body: Dict[str, Any]) -> None:
        with self._stopped_lock:
            thread_id = int(body.get("threadId") or 0)
            frame: Dict[str, Any] = {}
            try:
                frames = self.stack_trace(thread_id, levels=1)
                frame = frames[0] if frames else {}
            except (DapProtocolError, DapRequestError) as e:
                logger.debug("取停止位置失败：%s", e)
            self._emit("dap_stopped", {
                "session_id": self.session_id,
                "reason": str(body.get("reason") or "breakpoint"),
                "thread_id": thread_id,
                "frame_id": int(frame.get("id") or 0),
                "path": str(frame.get("path") or ""),
                "line": int(frame.get("line") or 0),
            })
```

- [ ] **Step 5: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_ide_debug_session.py -q`
Expected: `8 passed`

- [ ] **Step 6: 提交**

```bash
git add core/ide/debug_session.py tests/fake_dap_adapter.py tests/test_ide_debug_session.py
git commit -m "feat(ide): 调试会话编排（DAP 时序/断点/变量/进程回收）"
```

---

### Task 5: 运行命令解析（按语言 / 项目配置猜命令）

**Files:**
- Create: `core/ide/run_command.py`
- Test: `tests/test_ide_run_command.py`

**Interfaces:**
- Consumes: `core/workspace_context.get_workdir()`（`core/workspace_context.py:21`）的语义（工作区路径由调用方传入，本模块不自己取）
- Produces:
  - `@dataclass(frozen=True) class RunPlan`：`command: str`、`source: str`、`language_id: str`
  - `class RunPlanError(Exception)`
  - `build_run_plan(path: str, workspace: str, explicit_command: str = "", extra_args: str = "") -> RunPlan`

- [ ] **Step 1: 写失败测试** `tests/test_ide_run_command.py`

```python
"""一键运行命令解析测试（规格 6.2：按语言猜命令或读取项目配置；未知语言要有明确处理）。"""

import json
import os
import sys

import pytest

from core.ide import run_command as rc


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


def _write(path, text, encoding="utf-8"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def test_python_plan_uses_current_interpreter(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.language_id == "python" and plan.source == "language"
    assert plan.command.startswith("& ")
    assert sys.executable in plan.command
    assert "-u" in plan.command
    assert plan.command.endswith("'" + str(target) + "'")


def test_extra_args_are_appended(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace, extra_args="--name 星瑶")

    assert plan.command.endswith("--name 星瑶")


def test_explicit_command_wins(workspace, tmp_path):
    target = tmp_path / "app.py"
    _write(target, "print('hi')\n")

    plan = rc.build_run_plan(str(target), workspace, explicit_command="echo 自定义")

    assert plan.command == "echo 自定义" and plan.source == "explicit"


def test_javascript_plan_uses_node(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\node\node.exe" if name == "node" else None)
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.language_id == "javascript"
    assert plan.command == f"& 'C:\\node\\node.exe' '{target}'"


def test_javascript_without_node_raises_hint(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: None)
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    with pytest.raises(rc.RunPlanError, match="Node"):
        rc.build_run_plan(str(target), workspace)


def test_go_plan_uses_go_run(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr(rc.shutil, "which", lambda name: r"C:\Go\bin\go.exe" if name == "go" else None)
    target = tmp_path / "main.go"
    _write(target, "package main\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.command == f"& 'C:\\Go\\bin\\go.exe' run '{target}'"
    assert plan.language_id == "go"


def test_package_json_start_script_preferred(workspace, tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"start": "node index.js", "dev": "vite"}}))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "package.json" and "run start" in plan.command


def test_package_json_dev_script_fallback(workspace, tmp_path):
    _write(tmp_path / "package.json", json.dumps({"scripts": {"dev": "vite"}}))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert "run dev" in plan.command and plan.source == "package.json"


def test_launch_json_program_wins_for_compiled_output(workspace, tmp_path):
    exe = tmp_path / "build" / "app.exe"
    _write(tmp_path / ".vscode" / "launch.json", json.dumps({
        "version": "0.2.0",
        "configurations": [{"name": "dbg", "program": str(exe), "args": ["--verbose"]}],
    }))
    source_file = tmp_path / "src" / "main.cpp"
    _write(source_file, "int main(){return 0;}\n")

    plan = rc.build_run_plan(str(source_file), workspace)

    assert plan.source == "launch.json"
    assert str(exe) in plan.command and "--verbose" in plan.command


def test_typescript_without_package_json_raises(workspace, tmp_path):
    target = tmp_path / "app.ts"
    _write(target, "export const x = 1\n")

    with pytest.raises(rc.RunPlanError, match="package.json"):
        rc.build_run_plan(str(target), workspace)


def test_markdown_file_is_not_runnable(workspace, tmp_path):
    target = tmp_path / "notes.md"
    _write(target, "# hi\n")

    with pytest.raises(rc.RunPlanError, match="不可运行"):
        rc.build_run_plan(str(target), workspace)


def test_unknown_extension_raises_with_hint(workspace, tmp_path):
    target = tmp_path / "data.xyz"
    _write(target, "x\n")

    with pytest.raises(rc.RunPlanError) as excinfo:
        rc.build_run_plan(str(target), workspace)

    assert ".xyz" in str(excinfo.value) and "launch.json" in str(excinfo.value)


def test_missing_file_raises(workspace, tmp_path):
    with pytest.raises(rc.RunPlanError, match="不存在"):
        rc.build_run_plan(str(tmp_path / "nope.py"), workspace)


def test_gbk_package_json_is_still_readable(workspace, tmp_path):
    """项目里 GBK 编码的 package.json 不得让一键运行直接失败。"""
    payload = {"scripts": {"start": "node 主程序.js"}, "name": "中文项目"}
    (tmp_path / "package.json").write_bytes(json.dumps(payload, ensure_ascii=False).encode("gbk"))
    target = tmp_path / "index.js"
    _write(target, "console.log(1)\n")

    plan = rc.build_run_plan(str(target), workspace)

    assert plan.source == "package.json" and "run start" in plan.command
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_ide_run_command.py -q`
Expected: FAIL，`ImportError: cannot import name 'run_command'`

- [ ] **Step 3: 写实现** `core/ide/run_command.py`

```python
"""一键运行的命令解析：按语言猜命令，优先读项目配置，未知语言给明确提示。

输出是「可以直接写进常驻 shell 的一行命令行」。常驻 shell 由
core/gui_terminal.py:62 resolve_shell_command 决定：Windows 上是 PowerShell，
因此统一用 PowerShell 语法（& 调用 + 单引号引用），不拼 cmd 风格字符串。

项目配置读取顺序：.vscode/launch.json 的 program（编译型语言唯一可运行入口）
→ package.json 的 scripts.start / scripts.dev → 语言默认命令。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# 扩展名 -> 语言 id（与 S2 language_registry 的 id 口径一致）
LANGUAGE_BY_EXTENSION: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".ps1": "powershell",
    ".bat": "batch",
    ".cmd": "batch",
    ".sh": "shell",
    ".c": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
}

# 明确"不可运行"的扩展名：给出比"未识别语言"更准确的提示
NOT_RUNNABLE_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv", ".log",
    ".html", ".css", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".pdf",
}


@dataclass(frozen=True)
class RunPlan:
    """一条可运行的命令：command 未含末尾回车，由桥层补 \\r 写进 PTY。"""

    command: str
    source: str        # "explicit" / "launch.json" / "package.json" / "language"
    language_id: str


class RunPlanError(Exception):
    """无法解析出可运行命令（携带可直接展示给用户的中文消息）。"""


def _quote(path: str) -> str:
    """PowerShell 单引号引用：路径含空格/中文都不用转义（'' 表示一个单引号）。"""
    return "'" + str(path).replace("'", "''") + "'"


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """读 JSON 配置：UTF-8 strict 优先，失败回退 GBK（与 read_file_preview 口径一致）。"""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    for encoding in ("utf-8-sig", "gbk"):
        try:
            data = json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        return data if isinstance(data, dict) else None
    return None


def _nearest_config(name: str, start_dir: str, workspace: str) -> str:
    """从文件目录向上找最近的配置文件，最远到工作区根（不在工作区则只找文件目录）。"""
    current = os.path.abspath(start_dir)
    stop = os.path.abspath(workspace) if workspace and workspace != "GLOBAL" else current
    while True:
        candidate = os.path.join(current, name)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if current == stop or parent == current:
            return ""
        current = parent


def _launch_json_plan(path: str, workspace: str) -> Optional[RunPlan]:
    """.vscode/launch.json 里匹配当前文件的 configuration -> 运行计划。"""
    config_path = _nearest_config(os.path.join(".vscode", "launch.json"), os.path.dirname(path), workspace)
    if not config_path:
        return None
    data = _read_json(config_path)
    if not data:
        return None
    base = os.path.basename(path)
    for item in data.get("configurations") or []:
        if not isinstance(item, dict):
            continue
        program = str(item.get("program") or "")
        if not program:
            continue
        # 匹配口径：program 与当前文件同路径，或 program 是同名 basename（
        # launch.json 常写相对路径/变量，这里只做宽松匹配，匹配不到就退回语言默认）
        normalized = os.path.normcase(os.path.abspath(program)) if os.path.isabs(program) else ""
        if normalized and normalized == os.path.normcase(os.path.abspath(path)):
            matched = item
            break
        if os.path.basename(program) == base:
            matched = item
            break
    else:
        return None
    program_path = str(matched.get("program") or "")
    runtime = str(matched.get("runtimeExecutable") or "")
    extra = [str(arg) for arg in (matched.get("args") or [])]
    command = " ".join(
        [_quote(runtime) if runtime else "", _quote(program_path), *[_quote(arg) for arg in extra]]
    ).strip()
    return RunPlan(command=command, source="launch.json", language_id=LANGUAGE_BY_EXTENSION.get(os.path.splitext(path)[1].lower(), ""))


def _package_json_script(path: str, workspace: str) -> str:
    """package.json 的 scripts.start / scripts.dev（找不到返回空串）。"""
    config_path = _nearest_config("package.json", os.path.dirname(path), workspace)
    if not config_path:
        return ""
    data = _read_json(config_path)
    if not data:
        return ""
    scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
    for name in ("start", "dev"):
        if scripts.get(name):
            return name
    return ""


def build_run_plan(
    path: str,
    workspace: str,
    explicit_command: str = "",
    extra_args: str = "",
) -> RunPlan:
    """解析运行计划；解析不出来时抛 RunPlanError（不返回"半截命令"）。"""
    explicit = str(explicit_command or "").strip()
    if explicit:
        return RunPlan(command=_append_args(explicit, extra_args), source="explicit", language_id="")

    real = os.path.abspath(str(path or ""))
    if not os.path.isfile(real):
        raise RunPlanError("文件不存在或不是普通文件，无法运行")

    extension = os.path.splitext(real)[1].lower()
    if extension in NOT_RUNNABLE_EXTENSIONS:
        raise RunPlanError(f"该文件类型不可运行：{extension or '无扩展名'}")
    language_id = LANGUAGE_BY_EXTENSION.get(extension, "")
    if not language_id:
        raise RunPlanError(
            f"未识别的语言：{extension or '无扩展名'}；请在底部终端手动运行，"
            "或在 .vscode/launch.json 里为本文件配置 program"
        )

    launch_plan = _launch_json_plan(real, workspace)
    if launch_plan is not None:
        return RunPlan(
            command=_append_args(launch_plan.command, extra_args),
            source="launch.json",
            language_id=language_id,
        )

    if language_id in ("javascript", "typescript"):
        script = _package_json_script(real, workspace)
        if script:
            return RunPlan(command=_append_args(f"& npm run {script}", extra_args), source="package.json", language_id=language_id)

    if language_id == "python":
        interpreter = sys.executable or "python"
        return RunPlan(command=_append_args(f"& {_quote(interpreter)} -u {_quote(real)}", extra_args), source="language", language_id=language_id)
    if language_id == "javascript":
        node = _require_executable("node", "Node.js")
        return RunPlan(command=_append_args(f"& {_quote(node)} {_quote(real)}", extra_args), source="language", language_id=language_id)
    if language_id == "typescript":
        raise RunPlanError(
            "TypeScript 需要 package.json 的 scripts.start / scripts.dev 才能在终端运行；"
            "请先补齐 package.json，或在底部终端手动运行"
        )
    if language_id == "go":
        go = _require_executable("go", "Go")
        return RunPlan(command=_append_args(f"& {_quote(go)} run {_quote(real)}", extra_args), source="language", language_id=language_id)
    if language_id == "rust":
        cargo = _require_executable("cargo", "Rust")
        # 用 Cargo.toml 所在目录作为 cargo 的运行目录不可行（终端 cwd 不能逐条改），
        # 因此显式指定 manifest-path：文件向上最近的 Cargo.toml
        manifest = _nearest_config("Cargo.toml", os.path.dirname(real), workspace)
        if not manifest:
            raise RunPlanError("未找到 Cargo.toml，无法用 cargo run 运行；请在终端手动运行")
        return RunPlan(command=_append_args(f"& {_quote(cargo)} run --manifest-path {_quote(manifest)}", extra_args), source="language", language_id=language_id)
    if language_id in ("powershell", "batch"):
        return RunPlan(command=_append_args(f"& {_quote(real)}", extra_args), source="language", language_id=language_id)
    if language_id == "shell":
        bash = _require_executable("bash", "bash")
        return RunPlan(command=_append_args(f"& {_quote(bash)} {_quote(real)}", extra_args), source="language", language_id=language_id)

    raise RunPlanError(
        f"{extension} 是编译型语言，需要先编译出可执行文件并在 .vscode/launch.json 里配置 program"
    )


def _append_args(command: str, extra_args: str) -> str:
    extra = str(extra_args or "").strip()
    return f"{command} {extra}" if extra else command


def _require_executable(name: str, display: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RunPlanError(f"未找到 {display}（{name}），请先安装并确保它在 PATH 中")
    return path
```

- [ ] **Step 4: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_ide_run_command.py -q`
Expected: `14 passed`

- [ ] **Step 5: 提交**

```bash
git add core/ide/run_command.py tests/test_ide_run_command.py
git commit -m "feat(ide): 一键运行命令解析（语言/package.json/launch.json）"
```

---

### Task 6: 桥方法 `ide_run_file`（复用常驻 PTY 会话执行）

**Files:**
- Modify: `gui_bridge.py:7885-7893`（在 `pty_terminal_kill` 之后插入 IDE 运行桥）
- Test: `tests/test_gui_ide_run.py`

**Interfaces:**
- Consumes: `gui_bridge.py:7816` `pty_terminal_ensure(cols=80, rows=24)`、`gui_bridge.py:7846` `pty_terminal_write(data)`、`gui_bridge.py:6381` `_session_workdir(session_name)`、`gui_bridge.py:366` `_resolve_real_path`；`core.ide.run_command.build_run_plan` / `RunPlanError`
- Produces: `ApiBridge.ide_run_file(path: str, extra_args: str = "", command: str = "") -> Dict[str, Any]`（返回 `{ok, command?, source?, pid?, msg?}`）

- [ ] **Step 1: 写失败测试** `tests/test_gui_ide_run.py`

```python
"""一键运行桥测试：命令必须写进常驻终端会话，输出继续走既有 pty_data 事件。

裸桥构造范式与 tests/test_gui_terminal.py:32-68 的 _make_bridge 一致：
_js_dispatcher 指向主线程，阻止真实派发线程抢空队列。
"""

import os
import queue
import threading

import pytest

from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge


class _FakePtySession:
    def __init__(self, pid=4242, cwd=r"C:\fake_proj", alive=True):
        self.pid = pid
        self.cwd = cwd
        self._alive = alive
        self.writes = []
        self.killed = False

    def is_alive(self):
        return self._alive and not self.killed

    def write(self, data):
        self.writes.append(data)
        return True

    def resize(self, cols, rows):
        return True

    def kill(self):
        self.killed = True


def _make_bridge(workdir):
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = False
    instance._window = object()
    instance._js_queue = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
    instance._js_dispatcher = threading.current_thread()
    instance._js_dispatcher_lock = threading.Lock()
    instance._js_dropped = 0
    instance._delta_lock = threading.Lock()
    instance._delta_buffers = {}
    instance._delta_kinds = {}
    instance._delta_last_flush = 0.0
    instance._pty_session = None
    instance._pty_lock = threading.Lock()
    instance._current_session = "s1"
    instance._session_workdir = lambda name: workdir
    return instance


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


def test_run_file_writes_command_into_terminal(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    created = []

    def fake_create(cwd, cols, rows, on_output, on_exit):
        created.append(cwd)
        return _FakePtySession(cwd=cwd)

    monkeypatch.setattr("gui_bridge.create_pty_terminal", fake_create)
    target = os.path.join(workspace, "app.py")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")

    res = bridge.ide_run_file(target)

    assert res["ok"] is True and res["source"] == "language"
    assert bridge._pty_session.writes == [res["command"] + "\r"]
    assert created == [os.path.realpath(workspace)]


def test_run_file_reuses_existing_session(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    session = _FakePtySession(pid=7)
    bridge._pty_session = session
    monkeypatch.setattr(
        "gui_bridge.create_pty_terminal",
        lambda **kw: (_ for _ in ()).throw(AssertionError("不应重建终端")),
    )
    target = os.path.join(workspace, "app.py")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")

    res = bridge.ide_run_file(target)

    assert res["ok"] is True and res["pid"] == 7
    assert len(session.writes) == 1


def test_unknown_language_returns_message_without_touching_terminal(workspace, monkeypatch):
    bridge = _make_bridge(workspace)
    session = _FakePtySession()
    bridge._pty_session = session
    target = os.path.join(workspace, "data.xyz")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("x\n")

    res = bridge.ide_run_file(target)

    assert res["ok"] is False and ".xyz" in res["msg"]
    assert session.writes == []


def test_missing_file_rejected(workspace):
    bridge = _make_bridge(workspace)

    res = bridge.ide_run_file(os.path.join(workspace, "nope.py"))

    assert res["ok"] is False and "不存在" in res["msg"]


def test_credential_config_path_rejected(workspace, tmp_path, monkeypatch):
    from core import gui_path_policy

    bridge = _make_bridge(workspace)
    monkeypatch.setattr(gui_path_policy, "_ycode_data_dir", lambda: str(tmp_path / ".ycode"))
    credential = tmp_path / ".ycode" / "config.json"
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text("{}\n", encoding="utf-8")

    res = bridge.ide_run_file(str(credential))

    assert res["ok"] is False and "配置文件" in res["msg"]


def test_path_outside_workspace_rejected_when_sandbox_off(workspace, tmp_path, monkeypatch):
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    bridge = _make_bridge(workspace)
    outside = tmp_path.parent / "outside_app.py"
    outside.write_text("print('hi')\n", encoding="utf-8")

    res = bridge.ide_run_file(str(outside))

    assert res["ok"] is False and "工作区" in res["msg"]
    outside.unlink()


def test_terminal_spawn_failure_propagates_message(workspace, monkeypatch):
    from core.gui_terminal import PtyTerminalError

    def boom(cwd, cols, rows, on_output, on_exit):
        raise PtyTerminalError("未找到 PowerShell")

    monkeypatch.setattr("gui_bridge.create_pty_terminal", boom)
    bridge = _make_bridge(workspace)
    target = os.path.join(workspace, "app.py")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")

    res = bridge.ide_run_file(target)

    assert res["ok"] is False and "PowerShell" in res["msg"]


def test_explicit_command_bypasses_language_guess(workspace):
    bridge = _make_bridge(workspace)
    bridge._pty_session = _FakePtySession()
    target = os.path.join(workspace, "app.py")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")

    res = bridge.ide_run_file(target, command="echo 自定义")

    assert res["ok"] is True and res["source"] == "explicit"
    assert bridge._pty_session.writes == ["echo 自定义\r"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_gui_ide_run.py -q`
Expected: FAIL，`AttributeError: 'ApiBridge' object has no attribute 'ide_run_file'`

- [ ] **Step 3: 插入桥方法**（`gui_bridge.py:7893` 之后，紧接 `pty_terminal_kill`）

```python
    # ------------------------------------------------------------------
    # IDE 一键运行：命令写进既有常驻终端会话（输出继续走 pty_data 事件）
    # core.ide 的导入放在方法内：与 gui_path_access_allowed 的懒导入同口径
    # （gui_bridge.py:7395 / :7447），不让 IDE 模块拖慢 GUI 启动
    # ------------------------------------------------------------------
    def ide_run_file(self, path: str, extra_args: str = "", command: str = "") -> Dict[str, Any]:
        """解析运行命令并写进常驻终端；不新开进程，复用 pty_terminal_ensure 的会话语义。"""
        from core.gui_path_policy import gui_path_access_allowed
        from core.ide.run_command import RunPlanError, build_run_plan

        real = _resolve_real_path(path)
        if not os.path.isfile(real):
            return {"ok": False, "msg": "文件不存在或不是普通文件，无法运行"}
        workspace = self._session_workdir(self._current_session)
        allowed, reason = gui_path_access_allowed(real, workspace, action="open")
        if not allowed:
            return {"ok": False, "msg": reason}
        try:
            plan = build_run_plan(real, workspace, explicit_command=command, extra_args=extra_args)
        except RunPlanError as e:
            return {"ok": False, "msg": str(e)}
        ensured = self.pty_terminal_ensure()
        if ensured.get("ok") is not True:
            return {"ok": False, "msg": ensured.get("msg") or "终端启动失败"}
        written = self.pty_terminal_write(plan.command + "\r")
        if written.get("ok") is not True:
            return {"ok": False, "msg": written.get("msg") or "命令未送达终端"}
        return {
            "ok": True,
            "command": plan.command,
            "source": plan.source,
            "pid": ensured.get("pid"),
        }
```

- [ ] **Step 4: 运行测试确认通过（含终端既有用例）**

Run: `venv\Scripts\python -m pytest tests/test_gui_ide_run.py tests/test_gui_terminal.py -q`
Expected: `8 passed` + `13 passed`（终端既有用例无回归；`test_real_pty_session_roundtrip` 在 Windows 上会跑真 ConPTY，需数秒）

- [ ] **Step 5: 提交**

```bash
git add gui_bridge.py tests/test_gui_ide_run.py
git commit -m "feat(ide): 一键运行桥方法（复用常驻终端会话）"
```

---

### Task 7: 桥方法 `ide_debug_*`（调试会话与事件）

**Files:**
- Modify: `gui_bridge.py:599-604`（`__init__` 里 `_pty_session` 一带新增调试状态）
- Modify: `gui_bridge.py:1111-1118`（`shutdown` 的终断清理片段之后新增调试会话回收）
- Modify: `gui_bridge.py:7894` 起（Task 6 新增段落之后，追加调试桥）
- Test: `tests/test_gui_ide_debug.py`

**Interfaces:**
- Consumes: Task 3/4/5 的 `DapProtocolError` / `DapRequestError`、`DebugSession` / `DebugSessionError`、`probe_adapter` / `adapter_availability`；S2 的 `core.ide.language_registry.detect_language_id(path)`；`core.gui_path_policy.gui_path_access_allowed`
- Produces（均为 `ApiBridge` 方法）：
  - `ide_debug_availability() -> Dict[str, Any]`
  - `ide_debug_start(path: str = "", breakpoints: Optional[List[Dict[str, Any]]] = None, program: str = "", args: Optional[List[str]] = None, stop_on_entry: bool = False) -> Dict[str, Any]`
  - `ide_debug_set_breakpoints(path: str, lines: Optional[List[int]] = None) -> Dict[str, Any]`
  - `ide_debug_command(command: str) -> Dict[str, Any]`
  - `ide_debug_threads() -> Dict[str, Any]`
  - `ide_debug_stack_trace(thread_id: int = 0, levels: int = 20) -> Dict[str, Any]`
  - `ide_debug_scopes(frame_id: int = 0) -> Dict[str, Any]`
  - `ide_debug_variables(variables_reference: int = 0) -> Dict[str, Any]`
  - `ide_debug_evaluate(expression: str, frame_id: int = 0) -> Dict[str, Any]`
  - `ide_debug_stop() -> Dict[str, Any]`
  - `_emit_dap_event(event_type: str, data: Dict[str, Any]) -> None`、`_shutdown_debug_session() -> None`
  - 事件：`dap_status`、`dap_stopped`、`dap_output`、`dap_continued`、`dap_exited`、`dap_terminated`、`dap_breakpoint_changed`

- [ ] **Step 1: 写失败测试** `tests/test_gui_ide_debug.py`

```python
"""调试桥测试：用假 DebugSession 覆盖桥层契约（真适配器由 Task 4 覆盖）。

裸桥构造范式同 tests/test_gui_terminal.py:32-68。
"""

import os
import queue
import threading

import pytest

from gui_bridge import JS_QUEUE_MAXSIZE, ApiBridge
from core.ide.debug_adapters import STDIO, AdapterProbe, AdapterSpec


class _FakeSession:
    def __init__(self, spec, program, cwd, **kwargs):
        self.session_id = "dap-fake"
        self.spec = spec
        self.program = program
        self.cwd = cwd
        self.emit = kwargs.get("emit")
        self.breakpoints = []
        self.commands = []
        self.configuration_done_called = False
        self.stopped = False
        self._alive = False

    def start(self):
        self._alive = True
        return {"session_id": self.session_id, "adapter": self.spec.adapter_id, "pid": 99, "capabilities": {}}

    def set_breakpoints(self, path, lines):
        self.breakpoints.append((path, list(lines)))
        return [{"verified": True, "line": line} for line in lines]

    def configuration_done(self):
        self.configuration_done_called = True

    def is_alive(self):
        return self._alive

    def threads(self):
        return [{"id": 1, "name": "MainThread"}]

    def stack_trace(self, thread_id, levels=20):
        return [{"id": 1001, "name": "main", "path": r"C:\p\app.py", "line": 7, "column": 1}]

    def scopes(self, frame_id):
        return [{"name": "Locals", "variablesReference": 2001, "expensive": False}]

    def variables(self, reference):
        return [{"name": "x", "value": "42", "type": "int", "variablesReference": 0}]

    def evaluate(self, expression, frame_id=0, context="repl"):
        return {"result": f"eval:{expression}", "type": "str", "variablesReference": 0}

    def continue_(self, thread_id):
        self.commands.append(("continue", thread_id))
        return {}

    def pause(self, thread_id):
        self.commands.append(("pause", thread_id))
        return {}

    def next_(self, thread_id):
        self.commands.append(("next", thread_id))
        return {}

    def step_in(self, thread_id):
        self.commands.append(("stepIn", thread_id))
        return {}

    def step_out(self, thread_id):
        self.commands.append(("stepOut", thread_id))
        return {}

    def restart(self):
        self.commands.append(("restart", 0))
        return {}

    def stop(self):
        self.stopped = True
        self._alive = False


def _make_bridge(workdir):
    instance = ApiBridge.__new__(ApiBridge)
    instance._shutting_down = False
    instance._window = object()
    instance._js_queue = queue.Queue(maxsize=JS_QUEUE_MAXSIZE)
    instance._js_dispatcher = threading.current_thread()
    instance._js_dispatcher_lock = threading.Lock()
    instance._js_dropped = 0
    instance._delta_lock = threading.Lock()
    instance._delta_buffers = {}
    instance._delta_kinds = {}
    instance._delta_last_flush = 0.0
    instance._pty_session = None
    instance._pty_lock = threading.Lock()
    instance._debug_session = None
    instance._debug_lock = threading.Lock()
    instance._current_session = "s1"
    instance._session_workdir = lambda name: workdir
    return instance


@pytest.fixture()
def workspace(tmp_path):
    return str(tmp_path)


@pytest.fixture()
def source_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('hi')\n", encoding="utf-8")
    return str(target)


def _patch_probe(monkeypatch, ok=True):
    spec = AdapterSpec("debugpy", "python", ["python", "-m", "debugpy.adapter"], STDIO)
    probe = AdapterProbe(ok, "debugpy", spec if ok else None, "" if ok else "未安装 debugpy（Python 调试适配器）")
    monkeypatch.setattr("gui_bridge.probe_adapter", lambda language_id: probe)
    return spec


def _patch_session(monkeypatch):
    created = []
    monkeypatch.setattr("gui_bridge.DebugSession", lambda spec, **kwargs: created.append(_FakeSession(spec, **kwargs)) or created[-1])
    return created


def test_start_returns_hint_when_adapter_missing(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch, ok=False)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "debugpy" in res["msg"]


def test_start_creates_session_and_sends_breakpoints(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(source_file, breakpoints=[{"path": source_file, "lines": [7, 11]}])

    assert res["ok"] is True and res["session_id"] == "dap-fake" and res["adapter"] == "debugpy"
    session = created[0]
    assert session.breakpoints == [(source_file, [7, 11])]
    assert session.configuration_done_called is True
    assert bridge._debug_session is session


def test_second_start_rejected_while_session_alive(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_start(source_file)

    assert res["ok"] is False and "已有调试会话" in res["msg"]


def test_start_rejects_file_outside_workspace_when_sandbox_off(workspace, monkeypatch):
    from core import gui_path_policy

    monkeypatch.setattr(gui_path_policy, "_allow_outside_workspace", lambda: False)
    _patch_probe(monkeypatch)
    outside = os.path.join(os.path.dirname(workspace), "outside_app.py")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("print('hi')\n")
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_start(outside)

    assert res["ok"] is False and "工作区" in res["msg"]
    os.remove(outside)


def test_command_requires_running_session(workspace):
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_command("continue")

    assert res["ok"] is False and "未启动" in res["msg"]


def test_unknown_command_rejected(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_command("jump")

    assert res["ok"] is False and "jump" in res["msg"]


def test_step_commands_call_session_methods(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)
    bridge._debug_session.emit("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "frame_id": 1001})

    assert bridge.ide_debug_command("continue")["ok"] is True
    assert bridge.ide_debug_command("next")["ok"] is True
    assert bridge.ide_debug_command("stepIn")["ok"] is True
    assert bridge.ide_debug_command("stepOut")["ok"] is True
    assert bridge.ide_debug_command("pause")["ok"] is True
    assert created[0].commands == [("continue", 1), ("next", 1), ("stepIn", 1), ("stepOut", 1), ("pause", 1)]


def test_inspect_methods_return_session_payloads(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    assert bridge.ide_debug_threads()["threads"][0]["id"] == 1
    frames = bridge.ide_debug_stack_trace(1, 5)["frames"]
    assert frames[0]["line"] == 7 and frames[0]["path"] == r"C:\p\app.py"
    assert bridge.ide_debug_scopes(1001)["scopes"][0]["variablesReference"] == 2001
    assert bridge.ide_debug_variables(2001)["variables"][0]["value"] == "42"
    assert bridge.ide_debug_evaluate("1+1", 1001)["result"] == "eval:1+1"


def test_set_breakpoints_pending_without_session(workspace, source_file):
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_set_breakpoints(source_file, [3])

    assert res["ok"] is True and res["pending"] is True


def test_set_breakpoints_forwards_to_session(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_set_breakpoints(source_file, [3, 9])

    assert res["ok"] is True and res["pending"] is False
    assert created[0].breakpoints[-1] == (source_file, [3, 9])


def test_debug_events_reach_js_queue(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)

    bridge._emit_dap_event("dap_stopped", {"session_id": "dap-fake", "thread_id": 1, "line": 7})
    payload = bridge.drain_js_queue()

    assert payload["type"] == "dap_stopped"
    assert payload["data"]["line"] == 7


def test_stop_clears_session_and_terminates(workspace, source_file, monkeypatch):
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    res = bridge.ide_debug_stop()

    assert res["ok"] is True and created[0].stopped is True
    assert bridge._debug_session is None
    assert bridge.ide_debug_stop()["ok"] is True  # 幂等


def test_shutdown_debug_session_helper_reaps(workspace, source_file, monkeypatch):
    """shutdown 里的清理片段单独可测（同 tests/test_gui_terminal.py 的 shutdown 片段用例）。"""
    _patch_probe(monkeypatch)
    created = _patch_session(monkeypatch)
    bridge = _make_bridge(workspace)
    bridge.ide_debug_start(source_file)

    bridge._shutdown_debug_session()

    assert created[0].stopped is True and bridge._debug_session is None


def test_availability_lists_adapters(workspace, monkeypatch):
    monkeypatch.setattr(
        "gui_bridge.adapter_availability",
        lambda: [{"language_id": "python", "adapter_id": "debugpy", "available": True, "hint": ""}],
    )
    bridge = _make_bridge(workspace)

    res = bridge.ide_debug_availability()

    assert res["ok"] is True and res["adapters"][0]["adapter_id"] == "debugpy"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_gui_ide_debug.py -q`
Expected: FAIL，`AttributeError: 'ApiBridge' object has no attribute 'ide_debug_start'`

- [ ] **Step 3: 加调试状态字段**（`gui_bridge.py:601` 的 `self._pty_session = None` 之后）

```python
        # IDE 调试（DAP 适配器进程）：单会话、用户显式启动；读线程只 put_nowait 事件
        self._debug_session = None
        self._debug_lock = threading.Lock()
```

- [ ] **Step 4: shutdown 里回收调试会话**（`gui_bridge.py:1118` 的 `logger.exception("退出时终止内嵌交互式终端失败")` 之后）

```python
        try:
            self._shutdown_debug_session()
        except Exception:
            logger.exception("退出时终止调试适配器失败")
```

- [ ] **Step 5: 追加调试桥实现**（Task 6 新增段落之后）

```python
    # ------------------------------------------------------------------
    # IDE 调试：适配器进程由后端托管，事件一律经 emit_event（put_nowait 入队）
    # ------------------------------------------------------------------
    def _emit_dap_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """调试事件唯一出口：绝不在这里 evaluate_js（读线程不是派发线程）。"""
        if self._shutting_down:
            return
        try:
            self.emit_event(event_type, data)
        except Exception:
            logger.debug("推送调试事件失败", exc_info=True)

    def _shutdown_debug_session(self) -> None:
        with self._debug_lock:
            session = self._debug_session
            self._debug_session = None
        if session is None:
            return
        session.stop()

    def ide_debug_availability(self) -> Dict[str, Any]:
        """探测各语言调试适配器（给调试面板与设置页展示安装状态）。"""
        from core.ide.debug_adapters import adapter_availability

        return {"ok": True, "adapters": adapter_availability()}

    def ide_debug_start(
        self,
        path: str = "",
        breakpoints: Optional[List[Dict[str, Any]]] = None,
        program: str = "",
        args: Optional[List[str]] = None,
        stop_on_entry: bool = False,
    ) -> Dict[str, Any]:
        """启动调试：探测适配器 → 起会话 → 下发断点 → configurationDone。"""
        from core.gui_path_policy import gui_path_access_allowed
        from core.ide.dap_manager import DapProtocolError, DapRequestError
        from core.ide.debug_adapters import probe_adapter
        from core.ide.debug_session import DebugSession, DebugSessionError
        from core.ide.language_registry import detect_language_id

        with self._debug_lock:
            if self._debug_session is not None:
                return {"ok": False, "msg": "已有调试会话在运行，请先停止"}
        real = _resolve_real_path(path)
        if not os.path.isfile(real):
            return {"ok": False, "msg": "文件不存在或不是普通文件，无法调试"}
        workspace = self._session_workdir(self._current_session)
        allowed, reason = gui_path_access_allowed(real, workspace, action="open")
        if not allowed:
            return {"ok": False, "msg": reason}
        language_id = detect_language_id(real)
        probe = probe_adapter(language_id)
        if not probe.ok or probe.spec is None:
            return {"ok": False, "msg": probe.hint or f"星瑶暂不支持调试该语言：{language_id or '未知'}"}
        session = DebugSession(
            probe.spec,
            program=program or real,
            cwd=os.path.dirname(real),
            args=list(args or []),
            stop_on_entry=bool(stop_on_entry),
            emit=self._emit_dap_event,
            # debugpy 的适配器必须用启动它自己的解释器，否则 -m debugpy.adapter 找不到模块
            python=probe.spec.command[0] if language_id == "python" else "",
        )
        try:
            info = session.start()
        except DebugSessionError as e:
            session.stop()
            return {"ok": False, "msg": str(e)}
        except (DapProtocolError, DapRequestError) as e:
            session.stop()
            return {"ok": False, "msg": f"启动调试适配器失败：{e}"}
        try:
            for item in breakpoints or []:
                if not isinstance(item, dict):
                    continue
                entry_path = str(item.get("path") or real)
                lines = [int(line) for line in (item.get("lines") or [])]
                session.set_breakpoints(entry_path, lines)
            session.configuration_done()
        except (DapProtocolError, DapRequestError, DebugSessionError) as e:
            session.stop()
            return {"ok": False, "msg": f"调试初始化失败：{e}"}
        with self._debug_lock:
            self._debug_session = session
        self._emit_dap_event("dap_status", {"session_id": session.session_id, "state": "running", "msg": ""})
        return {
            "ok": True,
            "session_id": session.session_id,
            "adapter": probe.spec.adapter_id,
            "pid": info.get("pid"),
        }

    def ide_debug_set_breakpoints(self, path: str, lines: Optional[List[int]] = None) -> Dict[str, Any]:
        """会话未启动时断点只存在前端，这里返回 pending 由启动时统一下发。"""
        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": True, "pending": True, "verified": []}
        try:
            verified = session.set_breakpoints(_resolve_real_path(path), [int(line) for line in (lines or [])])
        except DapProtocolError as e:
            return {"ok": False, "msg": f"设置断点失败：{e}"}
        except DapRequestError as e:
            return {"ok": False, "msg": f"设置断点失败：{e}"}
        return {"ok": True, "pending": False, "verified": verified}

    def ide_debug_command(self, command: str) -> Dict[str, Any]:
        """调试控制：continue / pause / next / stepIn / stepOut / restart。"""
        from core.ide.debug_session import DebugSessionError

        handlers = {
            "continue": lambda session, thread_id: session.continue_(thread_id),
            "pause": lambda session, thread_id: session.pause(thread_id),
            "next": lambda session, thread_id: session.next_(thread_id),
            "stepIn": lambda session, thread_id: session.step_in(thread_id),
            "stepOut": lambda session, thread_id: session.step_out(thread_id),
            "restart": lambda session, thread_id: session.restart(),
        }
        handler = handlers.get(str(command))
        if handler is None:
            return {"ok": False, "msg": f"不支持的调试命令：{command}"}
        with self._debug_lock:
            session = self._debug_session
            thread_id = getattr(session, "_active_thread_id", 0) if session is not None else 0
        if session is None:
            return {"ok": False, "msg": "调试未启动"}
        try:
            handler(session, int(thread_id or 0))
        except DapProtocolError as e:
            return {"ok": False, "msg": f"{command} 失败：{e}"}
        except DapRequestError as e:
            return {"ok": False, "msg": f"{command} 失败：{e}"}
        except DebugSessionError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True}

    def ide_debug_threads(self) -> Dict[str, Any]:
        return self._debug_query("threads", lambda session: session.threads(), field="threads")

    def ide_debug_stack_trace(self, thread_id: int = 0, levels: int = 20) -> Dict[str, Any]:
        return self._debug_query(
            "stackTrace",
            lambda session: session.stack_trace(int(thread_id) or self._debug_thread_id(), int(levels)),
            field="frames",
        )

    def ide_debug_scopes(self, frame_id: int = 0) -> Dict[str, Any]:
        return self._debug_query("scopes", lambda session: session.scopes(int(frame_id)), field="scopes")

    def ide_debug_variables(self, variables_reference: int = 0) -> Dict[str, Any]:
        return self._debug_query(
            "variables", lambda session: session.variables(int(variables_reference)), field="variables"
        )

    def ide_debug_evaluate(self, expression: str, frame_id: int = 0) -> Dict[str, Any]:
        return self._debug_query(
            "evaluate",
            lambda session: session.evaluate(str(expression), int(frame_id) or self._debug_frame_id()),
            field="result",
        )

    def ide_debug_stop(self) -> Dict[str, Any]:
        """停止调试并回收适配器进程（幂等）。"""
        self._shutdown_debug_session()
        self._emit_dap_event("dap_status", {"session_id": "", "state": "stopped", "msg": ""})
        return {"ok": True}

    def _debug_query(self, label: str, action, field: str) -> Dict[str, Any]:
        """调试查询的统一出口：无会话 / 适配器报错的返回结构在这里收敛。"""
        from core.ide.dap_manager import DapProtocolError, DapRequestError

        with self._debug_lock:
            session = self._debug_session
        if session is None:
            return {"ok": False, "msg": "调试未启动", field: None}
        try:
            return {"ok": True, field: action(session)}
        except (DapProtocolError, DapRequestError) as e:
            return {"ok": False, "msg": f"{label} 失败：{e}", field: None}

    def _debug_thread_id(self) -> int:
        return int(getattr(self, "_debug_last_thread_id", 0) or 0)

    def _debug_frame_id(self) -> int:
        return int(getattr(self, "_debug_last_frame_id", 0) or 0)
```

同时补上线程/帧记忆（调试控制条需要"当前线程/当前帧"）：在 `__init__` 与 `_emit_dap_event` 上各加一行。

- [ ] **Step 6: 记住当前线程与帧**

在 `__init__`（`self._debug_lock = threading.Lock()` 之后）加：

```python
        # 当前暂停线程/帧：单步与求值都作用在它上面（由 dap_stopped 事件刷新）
        self._debug_last_thread_id = 0
        self._debug_last_frame_id = 0
```

把 `_emit_dap_event` 改为：

```python
    def _emit_dap_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """调试事件唯一出口：绝不在这里 evaluate_js（读线程不是派发线程）。"""
        if event_type == "dap_stopped":
            self._debug_last_thread_id = int(data.get("thread_id") or 0)
            self._debug_last_frame_id = int(data.get("frame_id") or 0)
        if self._shutting_down:
            return
        try:
            self.emit_event(event_type, data)
        except Exception:
            logger.debug("推送调试事件失败", exc_info=True)
```

- [ ] **Step 7: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_gui_ide_debug.py -q`
Expected: `15 passed`

- [ ] **Step 8: 提交**

```bash
git add gui_bridge.py tests/test_gui_ide_debug.py
git commit -m "feat(ide): 调试桥方法（启动/断点/单步/变量/求值/停止）与事件"
```

---

### Task 8: 前端调试输出总线、调试状态仓与事件分流

**Files:**
- Create: `frontend/src/utils/dapBus.ts`
- Create: `frontend/src/utils/dapBus.test.ts`
- Create: `frontend/src/stores/ideDebugStore.ts`
- Create: `frontend/src/stores/ideDebugStore.test.ts`
- Modify: `frontend/src/stores/appStore.ts:4507-4511`（`pty_data` / `pty_exit` 分支之后追加 `dap_*` 分流）
- Test: `frontend/src/stores/appStore.ideDebugEvents.test.ts`

**Interfaces:**
- Consumes: `ideStore`（`breakpoints: Set<string>`、`breakpointsFor(path)`、`toggleBreakpoint(path, line)`、`activeTab`）；桥方法 `ide_debug_start` / `ide_debug_set_breakpoints` / `ide_debug_command` / `ide_debug_stack_trace` / `ide_debug_scopes` / `ide_debug_variables` / `ide_debug_evaluate` / `ide_debug_stop` / `ide_run_file`（Task 6/7）
- Produces:
  - `dapBus.ts`：`DapOutputPayload`、`emitDapOutput(payload)`、`onDapOutput(handler) -> () => void`
  - `ideDebugStore`（`defineStore('ideDebug')`）：state `sessionId` / `adapter` / `debugState` / `stopReason` / `threadId` / `stopPath` / `stopLine` / `frames` / `selectedFrameId` / `scopes` / `variables` / `watchExpressions` / `watchResults` / `breakpointStates` / `lastError`；getter `active` / `paused` / `breakpointsByPath`；action `reduceDapEvent` / `startDebug` / `stopDebug` / `sendCommand` / `setBreakpointsFor` / `toggleBreakpoint` / `refreshFrames` / `selectFrame` / `loadScopes` / `expandVariables` / `evaluate` / `addWatch` / `removeWatch` / `runFile` / `reset`

- [ ] **Step 1: 写失败测试** `frontend/src/utils/dapBus.test.ts`

```ts
import { describe, expect, it, vi } from 'vitest';
import { emitDapOutput, onDapOutput } from './dapBus';

describe('dapBus', () => {
  it('fans debug adapter output out to every subscriber', () => {
    const a = vi.fn();
    const b = vi.fn();
    const offA = onDapOutput(a);
    const offB = onDapOutput(b);

    emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'hello\n' });

    expect(a).toHaveBeenCalledWith({ session_id: 'dap-1', category: 'stdout', text: 'hello\n' });
    expect(b).toHaveBeenCalledTimes(1);
    offA();
    offB();
  });

  it('stops delivering after unsubscribe', () => {
    const handler = vi.fn();
    const off = onDapOutput(handler);
    off();

    emitDapOutput({ session_id: 'dap-1', category: 'stderr', text: 'x' });

    expect(handler).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/utils/dapBus.test.ts`
Expected: FAIL，`Failed to resolve import "./dapBus"`

- [ ] **Step 3: 写实现** `frontend/src/utils/dapBus.ts`

```ts
// DAP 调试输出总线：调试控制台的原始输出通道。
//
// 与 ptyBus 同一思路（frontend/src/utils/ptyBus.ts 文件头注释）：适配器输出是
// 高频原始文本，进 Pinia 响应式状态会白白触发大量组件更新，因此绕开 store，
// 由 DebugPane 订阅后直接追加到本地行缓冲。appStore 只做事件分流，不缓存内容。

export interface DapOutputPayload {
  session_id: string;
  category: string;
  text: string;
}

type DapOutputHandler = (payload: DapOutputPayload) => void;

const outputHandlers = new Set<DapOutputHandler>();

export function emitDapOutput(payload: DapOutputPayload): void {
  for (const handler of [...outputHandlers]) handler(payload);
}

/** 订阅调试输出，返回取消订阅函数 */
export function onDapOutput(handler: DapOutputHandler): () => void {
  outputHandlers.add(handler);
  return () => outputHandlers.delete(handler);
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/utils/dapBus.test.ts`
Expected: `2 passed`

- [ ] **Step 5: 写失败测试** `frontend/src/stores/ideDebugStore.test.ts`

```ts
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useIdeDebugStore } from './ideDebugStore';
import { useIdeStore } from './ideStore';
import { onDapOutput } from '../utils/dapBus';

const api = {
  ide_debug_start: vi.fn(),
  ide_debug_set_breakpoints: vi.fn(),
  ide_debug_command: vi.fn(),
  ide_debug_stack_trace: vi.fn(),
  ide_debug_scopes: vi.fn(),
  ide_debug_variables: vi.fn(),
  ide_debug_evaluate: vi.fn(),
  ide_debug_stop: vi.fn(),
  ide_run_file: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

describe('ideDebugStore 事件归约', () => {
  it('marks paused state and remembers the stop position', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_stopped', {
      session_id: 'dap-1', reason: 'breakpoint', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7,
    });

    expect(store.debugState).toBe('paused');
    expect(store.paused).toBe(true);
    expect(store.threadId).toBe(1);
    expect(store.selectedFrameId).toBe(1001);
    expect(store.stopPath).toBe('E:/p/app.py');
    expect(store.stopLine).toBe(7);
  });

  it('clears pause info on continue and terminated', () => {
    const store = useIdeDebugStore();
    store.reduceDapEvent('dap_stopped', { thread_id: 1, frame_id: 1001, line: 7 });
    store.reduceDapEvent('dap_continued', { thread_id: 1 });
    expect(store.debugState).toBe('running');
    expect(store.frames).toEqual([]);

    store.reduceDapEvent('dap_terminated', { session_id: 'dap-1' });
    expect(store.debugState).toBe('stopped');
    expect(store.active).toBe(false);
  });

  it('records breakpoint verification per path and line', () => {
    const store = useIdeDebugStore();

    store.reduceDapEvent('dap_breakpoint_changed', { path: 'E:/p/app.py', line: 7, verified: true });

    expect(store.breakpointStates['E:/p/app.py:7']).toBe(true);
  });
});

describe('ideDebugStore 与桥的协作', () => {
  it('groups stored breakpoints by file when starting a debug session', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: true, session_id: 'dap-9', adapter: 'debugpy' });
    const ide = useIdeStore();
    const store = useIdeDebugStore();
    ide.toggleBreakpoint('E:/p/app.py', 7);
    ide.toggleBreakpoint('E:/p/app.py', 11);
    ide.toggleBreakpoint('E:/p/other.py', 3);

    await store.startDebug('E:/p/app.py');

    expect(api.ide_debug_start).toHaveBeenCalledWith('E:/p/app.py', [
      { path: 'E:/p/app.py', lines: [7, 11] },
      { path: 'E:/p/other.py', lines: [3] },
    ], '', []);
    expect(store.sessionId).toBe('dap-9');
    expect(store.adapter).toBe('debugpy');
  });

  it('surfaces adapter hints from a failed start', async () => {
    api.ide_debug_start.mockResolvedValue({ ok: false, msg: '未安装 debugpy（Python 调试适配器）' });
    const store = useIdeDebugStore();

    const res = await store.startDebug('E:/p/app.py');

    expect(res.ok).toBe(false);
    expect(store.lastError).toContain('debugpy');
  });

  it('forwards breakpoint toggles to the adapter when a session is running', async () => {
    api.ide_debug_set_breakpoints.mockResolvedValue({ ok: true, pending: false, verified: [{ line: 7, verified: true }] });
    const ide = useIdeStore();
    const store = useIdeDebugStore();
    store.sessionId = 'dap-9';
    store.debugState = 'paused';

    await store.toggleBreakpoint('E:/p/app.py', 7);

    expect(api.ide_debug_set_breakpoints).toHaveBeenCalledWith('E:/p/app.py', [7]);
    expect(store.breakpointStates['E:/p/app.py:7']).toBe(true);
  });

  it('evaluates a watch expression on the selected frame', async () => {
    api.ide_debug_evaluate.mockResolvedValue({ ok: true, result: 'eval:x+1', type: 'int' });
    const store = useIdeDebugStore();
    store.sessionId = 'dap-9';
    store.selectedFrameId = 1001;

    await store.addWatch('x+1');

    expect(api.ide_debug_evaluate).toHaveBeenCalledWith('x+1', 1001);
    expect(store.watchResults).toEqual([{ expression: 'x+1', result: 'eval:x+1', type: 'int', error: '' }]);
  });

  it('routes debug adapter output through the bus, not reactive state', () => {
    const store = useIdeDebugStore();
    const seen: string[] = [];
    const off = onDapOutput((payload) => seen.push(payload.text));

    store.reduceDapEvent('dap_output', { session_id: 'dap-1', category: 'stdout', text: 'line\n' });

    expect(seen).toEqual(['line\n']);
    off();
  });
});
```

- [ ] **Step 6: 写实现** `frontend/src/stores/ideDebugStore.ts`

```ts
import { defineStore } from 'pinia';

import { useIdeStore } from './ideStore';
import { emitDapOutput } from '../utils/dapBus';

export type DebugState = 'idle' | 'starting' | 'running' | 'paused' | 'stopped';

export interface DapStackFrame {
  id: number;
  name: string;
  path: string;
  line: number;
  column: number;
}

export interface DapVariable {
  name: string;
  value: string;
  type: string;
  variablesReference: number;
}

export interface DapScopeItem {
  name: string;
  variablesReference: number;
  expensive: boolean;
}

export interface DapWatchResult {
  expression: string;
  result: string;
  type: string;
  error: string;
}

export interface DapEventPayload {
  session_id?: string;
  state?: string;
  msg?: string;
  reason?: string;
  thread_id?: number;
  frame_id?: number;
  path?: string;
  line?: number;
  exit_code?: number;
  verified?: boolean;
  category?: string;
  text?: string;
}

/** 调试桥句柄：与 appStore._ptyApi 同口径，桥未就绪时返回 null。 */
function debugApi(): any {
  return (window as any).pywebview?.api ?? null;
}

function breakpointKey(path: string, line: number): string {
  return `${path}:${line}`;
}

export const useIdeDebugStore = defineStore('ideDebug', {
  state: () => ({
    sessionId: '',
    adapter: '',
    debugState: 'idle' as DebugState,
    stopReason: '',
    threadId: 0,
    stopPath: '',
    stopLine: 0,
    frames: [] as DapStackFrame[],
    selectedFrameId: 0,
    scopes: [] as DapScopeItem[],
    variables: [] as DapVariable[],
    watchExpressions: [] as string[],
    watchResults: [] as DapWatchResult[],
    breakpointStates: {} as Record<string, boolean>,
    lastError: '',
  }),

  getters: {
    active(state): boolean {
      return state.sessionId !== '' && state.debugState !== 'idle' && state.debugState !== 'stopped';
    },
    paused(state): boolean {
      return state.debugState === 'paused';
    },
    /** 断点按文件分组：启动调试时一次性下发（后端 DAP 用 setBreakpoints 逐文件设置）。 */
    breakpointsByPath(): { path: string; lines: number[] }[] {
      const ide = useIdeStore();
      const groups = new Map<string, number[]>();
      for (const key of ide.breakpoints) {
        const index = key.lastIndexOf(':');
        if (index <= 0) continue;
        const path = key.slice(0, index);
        const line = Number(key.slice(index + 1));
        if (!path || !Number.isFinite(line)) continue;
        groups.set(path, [...(groups.get(path) ?? []), line]);
      }
      return [...groups.entries()].map(([path, lines]) => ({ path, lines: lines.sort((a, b) => a - b) }));
    },
  },

  actions: {
    /** DAP 事件统一归约入口：由 appStore.handleBridgeEvent 分流调用。 */
    reduceDapEvent(type: string, data: DapEventPayload = {}) {
      if (type === 'dap_output') {
        emitDapOutput({
          session_id: String(data.session_id ?? ''),
          category: String(data.category ?? 'stdout'),
          text: String(data.text ?? ''),
        });
        return;
      }
      if (type === 'dap_status') {
        const state = String(data.state ?? '');
        if (state) this.debugState = state as DebugState;
        if (data.msg) this.lastError = String(data.msg);
        return;
      }
      if (type === 'dap_stopped') {
        this.debugState = 'paused';
        this.stopReason = String(data.reason ?? 'breakpoint');
        this.threadId = Number(data.thread_id ?? 0);
        this.selectedFrameId = Number(data.frame_id ?? 0);
        this.stopPath = String(data.path ?? '');
        this.stopLine = Number(data.line ?? 0);
        this.frames = [];
        this.scopes = [];
        this.variables = [];
        return;
      }
      if (type === 'dap_continued') {
        this.debugState = 'running';
        this.frames = [];
        this.scopes = [];
        this.variables = [];
        this.selectedFrameId = 0;
        return;
      }
      if (type === 'dap_exited' || type === 'dap_terminated') {
        this.debugState = 'stopped';
        this.sessionId = '';
        this.frames = [];
        this.scopes = [];
        this.variables = [];
        this.selectedFrameId = 0;
        return;
      }
      if (type === 'dap_breakpoint_changed') {
        const path = String(data.path ?? '');
        const line = Number(data.line ?? 0);
        if (path && line > 0) this.breakpointStates[breakpointKey(path, line)] = Boolean(data.verified);
      }
    },

    reset() {
      this.sessionId = '';
      this.adapter = '';
      this.debugState = 'idle';
      this.stopReason = '';
      this.threadId = 0;
      this.stopPath = '';
      this.stopLine = 0;
      this.frames = [];
      this.selectedFrameId = 0;
      this.scopes = [];
      this.variables = [];
      this.watchResults = [];
      this.breakpointStates = {};
      this.lastError = '';
    },

    async runFile(path: string, extraArgs = '') {
      const api = debugApi();
      if (!api?.ide_run_file) return { ok: false, msg: '后端桥未就绪' };
      const res = await api.ide_run_file(path, extraArgs);
      if (!res?.ok) this.lastError = String(res?.msg ?? '运行失败');
      return res ?? { ok: false };
    },

    async startDebug(path: string, program = '', args: string[] = []) {
      const api = debugApi();
      if (!api?.ide_debug_start) return { ok: false, msg: '后端桥未就绪' };
      this.debugState = 'starting';
      this.lastError = '';
      const res = await api.ide_debug_start(path, this.breakpointsByPath, program, args);
      if (res?.ok) {
        this.sessionId = String(res.session_id ?? '');
        this.adapter = String(res.adapter ?? '');
        this.debugState = 'running';
      } else {
        this.debugState = 'idle';
        this.lastError = String(res?.msg ?? '启动调试失败');
      }
      return res ?? { ok: false };
    },

    async stopDebug() {
      const api = debugApi();
      if (api?.ide_debug_stop) await api.ide_debug_stop();
      this.reset();
      this.debugState = 'stopped';
    },

    async sendCommand(command: string) {
      const api = debugApi();
      if (!api?.ide_debug_command) return { ok: false, msg: '后端桥未就绪' };
      const res = await api.ide_debug_command(command);
      if (!res?.ok) this.lastError = String(res?.msg ?? '调试命令失败');
      return res ?? { ok: false };
    },

    async setBreakpointsFor(path: string) {
      const api = debugApi();
      if (!api?.ide_debug_set_breakpoints) return { ok: false };
      const ide = useIdeStore();
      const lines = ide.breakpointsFor(path);
      const res = await api.ide_debug_set_breakpoints(path, lines);
      for (const item of res?.verified ?? []) {
        if (item?.line) this.breakpointStates[breakpointKey(path, Number(item.line))] = Boolean(item.verified);
      }
      return res ?? { ok: false };
    },

    /** 断点切换：先落前端状态（无会话时也能设），有会话时同步给适配器。 */
    async toggleBreakpoint(path: string, line: number) {
      const ide = useIdeStore();
      ide.toggleBreakpoint(path, line);
      const key = breakpointKey(path, line);
      if (!ide.breakpoints.has(key)) {
        delete this.breakpointStates[key];
      }
      if (this.sessionId) await this.setBreakpointsFor(path);
    },

    async refreshFrames() {
      const api = debugApi();
      if (!api?.ide_debug_stack_trace || !this.threadId) return [];
      const res = await api.ide_debug_stack_trace(this.threadId, 20);
      this.frames = (res?.frames ?? []) as DapStackFrame[];
      return this.frames;
    },

    async selectFrame(frameId: number) {
      this.selectedFrameId = frameId;
      await this.loadScopes(frameId);
    },

    async loadScopes(frameId: number) {
      const api = debugApi();
      if (!api?.ide_debug_scopes) return [];
      const res = await api.ide_debug_scopes(frameId);
      this.scopes = (res?.scopes ?? []) as DapScopeItem[];
      const first = this.scopes[0];
      this.variables = first ? await this.expandVariables(first.variablesReference) : [];
      return this.scopes;
    },

    async expandVariables(variablesReference: number) {
      const api = debugApi();
      if (!api?.ide_debug_variables) return [];
      const res = await api.ide_debug_variables(variablesReference);
      this.variables = (res?.variables ?? []) as DapVariable[];
      return this.variables;
    },

    async evaluate(expression: string, frameId = 0) {
      const api = debugApi();
      if (!api?.ide_debug_evaluate) return { ok: false, msg: '后端桥未就绪' };
      const target = frameId || this.selectedFrameId;
      const res = await api.ide_debug_evaluate(expression, target);
      return res ?? { ok: false };
    },

    async addWatch(expression: string) {
      const text = expression.trim();
      if (!text) return;
      if (!this.watchExpressions.includes(text)) this.watchExpressions.push(text);
      const res = await this.evaluate(text);
      this.watchResults = [
        ...this.watchResults.filter((item) => item.expression !== text),
        {
          expression: text,
          result: String(res?.result ?? ''),
          type: String(res?.type ?? ''),
          error: res?.ok ? '' : String(res?.msg ?? '求值失败'),
        },
      ];
    },

    removeWatch(expression: string) {
      this.watchExpressions = this.watchExpressions.filter((item) => item !== expression);
      this.watchResults = this.watchResults.filter((item) => item.expression !== expression);
    },

    async refreshWatch() {
      const expressions = [...this.watchExpressions];
      this.watchResults = [];
      for (const expression of expressions) await this.addWatch(expression);
    },
  },
});
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/stores/ideDebugStore.test.ts src/utils/dapBus.test.ts`
Expected: `8 passed`（2 个 bus 用例 + 6 个 store 用例）

- [ ] **Step 8: 写失败测试** `frontend/src/stores/appStore.ideDebugEvents.test.ts`

```ts
import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

import { useAppStore } from './appStore';
import { useIdeDebugStore } from './ideDebugStore';
import { onDapOutput } from '../utils/dapBus';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api: {} };
});

it('routes dap_output to the bus without touching reactive state', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();
  const seen: string[] = [];
  const off = onDapOutput((payload) => seen.push(payload.text));

  app.handleBridgeEvent({ type: 'dap_output', data: { session_id: 'dap-1', category: 'stdout', text: 'hi\n' } });

  expect(seen).toEqual(['hi\n']);
  expect(debug.frames).toEqual([]);
  off();
});

it('routes dap_stopped into the debug store', () => {
  const app = useAppStore();
  const debug = useIdeDebugStore();

  app.handleBridgeEvent({
    type: 'dap_stopped',
    data: { session_id: 'dap-1', reason: 'breakpoint', thread_id: 1, frame_id: 1001, path: 'E:/p/app.py', line: 7 },
  });

  expect(debug.debugState).toBe('paused');
  expect(debug.stopLine).toBe(7);
});

it('keeps pty routing intact after adding dap branches', () => {
  const app = useAppStore();
  const seen: number[] = [];
  const store2 = useAppStore();
  vi.spyOn(store2, 'ptyTerminalWrite').mockResolvedValue(undefined);

  app.handleBridgeEvent({ type: 'pty_exit', data: { code: 3 } });

  expect(seen).toEqual([]);
  expect(app.terminalPanelOpen).toBe(false);
});
```

- [ ] **Step 9: 改动 `appStore.ts` 事件分流**（`appStore.ts:4511` 的 `pty_exit` 分支之后）

先在导入区（`appStore.ts:46` 的 `import { emitPtyData, emitPtyExit } from '../utils/ptyBus';` 之后）加：

```ts
import { emitDapOutput } from '../utils/dapBus';
import { useIdeDebugStore } from './ideDebugStore';
```

再把分支改为：

```ts
      } else if (type === 'pty_data') {
        emitPtyData(String(data.text ?? ''));
      } else if (type === 'pty_exit') {
        emitPtyExit(Number(data.code ?? 0));
      } else if (type === 'dap_output') {
        // 调试适配器输出：高频原始文本走总线，不进响应式状态
        emitDapOutput({
          session_id: String(data.session_id ?? ''),
          category: String(data.category ?? 'stdout'),
          text: String(data.text ?? ''),
        });
      } else if (type.startsWith('dap_')) {
        // 调试会话状态事件统一进 ideDebugStore（与 useGitStore 同范式，appStore.ts:3739）
        useIdeDebugStore().reduceDapEvent(type, data);
      } else if (type === 'update_download_progress') {
```

- [ ] **Step 10: 运行测试确认通过（含既有 pty 分流用例）**

Run: `cd frontend && npx vitest run src/stores/appStore.ideDebugEvents.test.ts src/stores/appStore.terminalPanel.test.ts src/stores/ideDebugStore.test.ts src/utils/dapBus.test.ts`
Expected: `8 passed` + `5 passed` + `8 passed` + `2 passed`

- [ ] **Step 11: 提交**

```bash
git add frontend/src/utils/dapBus.ts frontend/src/utils/dapBus.test.ts frontend/src/stores/ideDebugStore.ts frontend/src/stores/ideDebugStore.test.ts frontend/src/stores/appStore.ts frontend/src/stores/appStore.ideDebugEvents.test.ts
git commit -m "feat(ide): 调试事件分流与 ideDebugStore（输出走总线）"
```

---

### Task 9: 调试工具条 `DebugToolbar.vue`

**Files:**
- Create: `frontend/src/components/DebugToolbar.vue`
- Test: `frontend/src/components/DebugToolbar.test.ts`

**Interfaces:**
- Consumes: Task 8 的 `useIdeDebugStore`（`active` / `paused` / `debugState` / `runFile` / `startDebug` / `stopDebug` / `sendCommand`）；`useIdeStore`（`activeTab.path` / `bottomTab` / `setBottomTab` / `bottomPanelOpen` / `toggleBottomPanel`）；`AppIcon`（`frontend/src/components/iconPaths.ts` 现有图标：`play` / `pause` / `square` / `refresh` / `chevron-right` / `chevron-down` / `chevron-up` / `ban`）
- Produces: `DebugToolbar.vue`（无 props、无 emit；交互全部经 store）；`data-test` 钩子：`debug-toolbar`、`debug-run`、`debug-continue`、`debug-pause`、`debug-step-over`、`debug-step-in`、`debug-step-out`、`debug-restart`、`debug-stop`、`debug-error`

- [ ] **Step 1: 写失败测试** `frontend/src/components/DebugToolbar.test.ts`

```ts
import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import DebugToolbar from './DebugToolbar.vue';
import { useIdeStore } from '../stores/ideStore';
import { useIdeDebugStore } from '../stores/ideDebugStore';

const api = {
  ide_run_file: vi.fn().mockResolvedValue({ ok: true, command: 'python -u app.py' }),
  ide_debug_start: vi.fn().mockResolvedValue({ ok: true, session_id: 'dap-1', adapter: 'debugpy' }),
  ide_debug_command: vi.fn().mockResolvedValue({ ok: true }),
  ide_debug_stop: vi.fn().mockResolvedValue({ ok: true }),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function mountBar() {
  return mount(DebugToolbar);
}

it('runs the active file after expanding the bottom terminal panel', async () => {
  const ide = useIdeStore();
  ide.activeTab = { path: 'E:/p/app.py', encoding: 'utf-8', eol: 'lf', dirty: false, viewState: null };
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-run"]').trigger('click');

  expect(api.ide_run_file).toHaveBeenCalledWith('E:/p/app.py', '');
  expect(ide.bottomPanelOpen).toBe(true);
  expect(ide.bottomTab).toBe('terminal');
  wrapper.unmount();
});

it('disables run without an active editor tab', () => {
  const wrapper = mountBar();

  expect(wrapper.get('[data-test="debug-run"]').attributes('disabled')).toBeDefined();
  wrapper.unmount();
});

it('shows step controls only while paused', async () => {
  const debug = useIdeDebugStore();
  const wrapper = mountBar();
  expect(wrapper.find('[data-test="debug-step-over"]').exists()).toBe(false);

  debug.debugState = 'paused';
  await wrapper.vm.$nextTick();

  expect(wrapper.find('[data-test="debug-step-over"]').exists()).toBe(true);
  await wrapper.get('[data-test="debug-step-over"]').trigger('click');
  expect(api.ide_debug_command).toHaveBeenCalledWith('next');
  wrapper.unmount();
});

it('starts a debug session for the active file', async () => {
  const ide = useIdeStore();
  const debug = useIdeDebugStore();
  ide.activeTab = { path: 'E:/p/app.py', encoding: 'utf-8', eol: 'lf', dirty: false, viewState: null };
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-start"]').trigger('click');

  expect(api.ide_debug_start).toHaveBeenCalledWith('E:/p/app.py', [], '', []);
  expect(debug.sessionId).toBe('dap-1');
  wrapper.unmount();
});

it('surfaces adapter hints when starting fails', async () => {
  api.ide_debug_start.mockResolvedValue({ ok: false, msg: '未安装 debugpy（Python 调试适配器）' });
  const ide = useIdeStore();
  ide.activeTab = { path: 'E:/p/app.py', encoding: 'utf-8', eol: 'lf', dirty: false, viewState: null };
  const wrapper = mountBar();

  await wrapper.get('[data-test="debug-start"]').trigger('click');

  expect(wrapper.get('[data-test="debug-error"]').text()).toContain('debugpy');
  wrapper.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/DebugToolbar.test.ts`
Expected: FAIL，`Failed to resolve import "./DebugToolbar.vue"`

- [ ] **Step 3: 写实现** `frontend/src/components/DebugToolbar.vue`

```vue
<template>
  <div class="debug-toolbar" data-test="debug-toolbar">
    <button
      class="dt-btn"
      title="运行当前文件（输出在底部终端）"
      data-test="debug-run"
      :disabled="!hasActiveFile"
      @click="runActiveFile"
    >
      <AppIcon name="play" :size="13" />
      <span>运行</span>
    </button>

    <span class="dt-sep"></span>

    <button
      class="dt-btn"
      title="启动调试"
      data-test="debug-start"
      :disabled="!hasActiveFile || debug.active"
      @click="startDebugging"
    >
      <AppIcon name="chevron-right" :size="13" />
      <span>调试</span>
    </button>

    <template v-if="debug.active">
      <button
        v-if="debug.paused"
        class="dt-btn"
        title="继续"
        data-test="debug-continue"
        @click="command('continue')"
      >
        <AppIcon name="play" :size="13" />
      </button>
      <button
        v-else
        class="dt-btn"
        title="暂停"
        data-test="debug-pause"
        @click="command('pause')"
      >
        <AppIcon name="pause" :size="13" />
      </button>

      <button
        v-if="debug.paused"
        class="dt-btn"
        title="单步跳过"
        data-test="debug-step-over"
        @click="command('next')"
      >
        <AppIcon name="chevron-right" :size="13" />
      </button>
      <button
        v-if="debug.paused"
        class="dt-btn"
        title="单步进入"
        data-test="debug-step-in"
        @click="command('stepIn')"
      >
        <AppIcon name="chevron-down" :size="13" />
      </button>
      <button
        v-if="debug.paused"
        class="dt-btn"
        title="单步跳出"
        data-test="debug-step-out"
        @click="command('stepOut')"
      >
        <AppIcon name="chevron-up" :size="13" />
      </button>

      <button class="dt-btn" title="重启调试" data-test="debug-restart" @click="command('restart')">
        <AppIcon name="refresh" :size="13" />
      </button>
      <button class="dt-btn dt-stop" title="停止调试" data-test="debug-stop" @click="debug.stopDebug()">
        <AppIcon name="square" :size="13" />
      </button>
    </template>

    <span class="dt-spacer"></span>
    <span v-if="debug.lastError" class="dt-error" data-test="debug-error">{{ debug.lastError }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue';

import AppIcon from './AppIcon.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { useIdeStore } from '../stores/ideStore';

const ide = useIdeStore();
const debug = useIdeDebugStore();

const hasActiveFile = computed(() => Boolean(ide.activeTab?.path));

function revealTerminal() {
  // 输出落在底部终端页签：先把面板切过去再发命令，否则用户看不到输出
  ide.setBottomTab('terminal');
  if (!ide.bottomPanelOpen) ide.toggleBottomPanel();
}

async function runActiveFile() {
  const path = ide.activeTab?.path;
  if (!path) return;
  revealTerminal();
  await debug.runFile(path);
}

async function startDebugging() {
  const path = ide.activeTab?.path;
  if (!path) return;
  await debug.startDebug(path);
}

async function command(name: string) {
  await debug.sendCommand(name);
}
</script>

<style scoped>
.debug-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 30px;
  padding: 0 8px;
  border-bottom: 1px solid var(--border);
  background: var(--bg0);
  font-size: 11.5px;
  color: var(--text-dim);
  user-select: none;
}
.dt-btn {
  display: flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 7px;
  border: none;
  background: transparent;
  color: var(--text-dim);
  border-radius: 5px;
  cursor: pointer;
}
.dt-btn:hover:not(:disabled) {
  background: var(--bg2);
  color: var(--text);
}
.dt-btn:disabled {
  opacity: 0.45;
  cursor: default;
}
.dt-stop:hover {
  color: var(--red, #e5534b);
}
.dt-sep {
  width: 1px;
  height: 14px;
  background: var(--border);
  margin: 0 3px;
}
.dt-spacer {
  flex: 1;
}
.dt-error {
  color: var(--orange);
  max-width: 46%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/DebugToolbar.test.ts`
Expected: `5 passed`

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/DebugToolbar.vue frontend/src/components/DebugToolbar.test.ts
git commit -m "feat(ide): 调试工具条（运行/继续/单步/重启/停止）"
```

---

### Task 10: 调试面板 `DebugPane.vue`（调用栈 / 变量 / 监视 / 调试控制台）

**Files:**
- Create: `frontend/src/components/DebugPane.vue`
- Test: `frontend/src/components/DebugPane.test.ts`

**Interfaces:**
- Consumes: Task 8 的 `useIdeDebugStore`（`frames` / `scopes` / `variables` / `selectedFrameId` / `watchExpressions` / `watchResults` / `paused` / `lastError` / `refreshFrames` / `selectFrame` / `expandVariables` / `addWatch` / `removeWatch` / `evaluate` / `stopDebug`）与 `onDapOutput`（`frontend/src/utils/dapBus.ts`）；`ideStore.bottomTab`（用于展示"当前文件没有调试会话"时的提示）
- Produces: `DebugPane.vue`；`data-test` 钩子：`debug-pane`、`debug-frames`、`debug-frame`、`debug-scope`、`debug-variable`、`debug-watch-input`、`debug-watch-add`、`debug-watch-row`、`debug-console-output`、`debug-console-input`、`debug-console-run`、`debug-pane-error`

- [ ] **Step 1: 写失败测试** `frontend/src/components/DebugPane.test.ts`

```ts
import { beforeEach, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import DebugPane from './DebugPane.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { emitDapOutput } from '../utils/dapBus';

const api = {
  ide_debug_stack_trace: vi.fn().mockResolvedValue({
    ok: true,
    frames: [
      { id: 1001, name: 'main', path: 'E:/p/app.py', line: 7, column: 1 },
      { id: 1002, name: 'helper', path: 'E:/p/app.py', line: 21, column: 3 },
    ],
  }),
  ide_debug_scopes: vi.fn().mockResolvedValue({
    ok: true,
    scopes: [{ name: 'Locals', variablesReference: 2001, expensive: false }],
  }),
  ide_debug_variables: vi.fn().mockResolvedValue({
    ok: true,
    variables: [{ name: 'x', value: '42', type: 'int', variablesReference: 0 }],
  }),
  ide_debug_evaluate: vi.fn().mockResolvedValue({ ok: true, result: 'eval:1+1', type: 'str' }),
};

beforeEach(() => {
  vi.clearAllMocks();
  setActivePinia(createPinia());
  (window as any).pywebview = { api };
});

function mountPane() {
  return mount(DebugPane);
}

it('renders the stopped stack and selects a frame on click', async () => {
  const debug = useIdeDebugStore();
  debug.threadId = 1;
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await debug.refreshFrames();
  await wrapper.vm.$nextTick();

  const rows = wrapper.findAll('[data-test="debug-frame"]');
  expect(rows.length).toBe(2);
  expect(rows[0].text()).toContain('main');

  await rows[1].trigger('click');

  expect(api.ide_debug_scopes).toHaveBeenCalledWith(1002);
  expect(debug.selectedFrameId).toBe(1002);
  wrapper.unmount();
});

it('renders variables of the first scope', async () => {
  const debug = useIdeDebugStore();
  const wrapper = mountPane();

  await debug.loadScopes(1001);
  await wrapper.vm.$nextTick();

  expect(wrapper.get('[data-test="debug-scope"]').text()).toContain('Locals');
  expect(wrapper.get('[data-test="debug-variable"]').text()).toContain('42');
  wrapper.unmount();
});

it('appends adapter output into the console buffer', async () => {
  const wrapper = mountPane();

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'breakpoint hit\n' });
  await wrapper.vm.$nextTick();

  expect(wrapper.get('[data-test="debug-console-output"]').text()).toContain('breakpoint hit');
  wrapper.unmount();
});

it('evaluates an expression typed into the console on the selected frame', async () => {
  const debug = useIdeDebugStore();
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-console-input"]').setValue('1+1');

  await wrapper.get('[data-test="debug-console-run"]').trigger('click');

  expect(api.ide_debug_evaluate).toHaveBeenCalledWith('1+1', 1001);
  expect(wrapper.get('[data-test="debug-console-output"]').text()).toContain('eval:1+1');
  wrapper.unmount();
});

it('adds and removes watch expressions', async () => {
  const debug = useIdeDebugStore();
  debug.selectedFrameId = 1001;
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-watch-input"]').setValue('x+1');

  await wrapper.get('[data-test="debug-watch-add"]').trigger('click');

  expect(api.ide_debug_evaluate).toHaveBeenCalledWith('x+1', 1001);
  expect(wrapper.get('[data-test="debug-watch-row"]').text()).toContain('eval:x+1');

  await wrapper.get('[data-test="debug-watch-remove"]').trigger('click');
  expect(wrapper.find('[data-test="debug-watch-row"]').exists()).toBe(false);
  wrapper.unmount();
});

it('shows the bridge error message when a debug call fails', async () => {
  api.ide_debug_evaluate.mockResolvedValue({ ok: false, msg: '调试未启动' });
  const wrapper = mountPane();
  await wrapper.get('[data-test="debug-console-input"]').setValue('x');
  await wrapper.get('[data-test="debug-console-run"]').trigger('click');

  expect(wrapper.get('[data-test="debug-pane-error"]').text()).toContain('调试未启动');
  wrapper.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/DebugPane.test.ts`
Expected: FAIL，`Failed to resolve import "./DebugPane.vue"`

- [ ] **Step 3: 写实现** `frontend/src/components/DebugPane.vue`

```vue
<template>
  <div class="debug-pane" data-test="debug-pane">
    <div class="dp-left">
      <section class="dp-section">
        <header class="dp-head">
          <span>调用栈</span>
          <button class="dp-icon" title="刷新调用栈" data-test="debug-refresh" @click="debug.refreshFrames()">
            <AppIcon name="refresh" :size="12" />
          </button>
        </header>
        <ul class="dp-list" data-test="debug-frames">
          <li
            v-for="frame in debug.frames"
            :key="frame.id"
            class="dp-frame"
            :class="{ active: frame.id === debug.selectedFrameId }"
            data-test="debug-frame"
            @click="debug.selectFrame(frame.id)"
          >
            <span class="dp-frame-name">{{ frame.name }}</span>
            <span class="dp-frame-loc">{{ baseName(frame.path) }}:{{ frame.line }}</span>
          </li>
          <li v-if="!debug.frames.length" class="dp-empty">未暂停或调用栈为空</li>
        </ul>
      </section>

      <section class="dp-section">
        <header class="dp-head"><span>变量</span></header>
        <div class="dp-vars">
          <div v-for="scope in debug.scopes" :key="scope.variablesReference" class="dp-scope" data-test="debug-scope">
            <div class="dp-scope-name">{{ scope.name }}</div>
            <div
              v-for="item in debug.variables"
              :key="item.name"
              class="dp-var"
              data-test="debug-variable"
              :class="{ clickable: item.variablesReference > 0 }"
              @click="item.variablesReference > 0 && debug.expandVariables(item.variablesReference)"
            >
              <span class="dp-var-name">{{ item.name }}</span>
              <span class="dp-var-value">{{ item.value }}</span>
              <span class="dp-var-type">{{ item.type }}</span>
            </div>
          </div>
          <div v-if="!debug.scopes.length" class="dp-empty">暂停后可查看变量</div>
        </div>
      </section>
    </div>

    <div class="dp-right">
      <section class="dp-section dp-watch">
        <header class="dp-head"><span>监视</span></header>
        <div class="dp-watch-add">
          <input
            v-model="watchInput"
            class="dp-input"
            placeholder="输入表达式后回车"
            data-test="debug-watch-input"
            @keyup.enter="addWatch"
          />
          <button class="dp-btn" data-test="debug-watch-add" @click="addWatch">添加</button>
        </div>
        <ul class="dp-list">
          <li v-for="row in debug.watchResults" :key="row.expression" class="dp-watch-row" data-test="debug-watch-row">
            <span class="dp-var-name">{{ row.expression }}</span>
            <span class="dp-var-value">{{ row.error || row.result }}</span>
            <button class="dp-icon" title="移除" data-test="debug-watch-remove" @click="debug.removeWatch(row.expression)">
              <AppIcon name="x" :size="12" />
            </button>
          </li>
          <li v-if="!debug.watchResults.length" class="dp-empty">添加表达式以在暂停时求值</li>
        </ul>
      </section>

      <section class="dp-section dp-console">
        <header class="dp-head"><span>调试控制台</span></header>
        <pre class="dp-console-out" data-test="debug-console-output">{{ consoleText }}</pre>
        <div class="dp-console-in">
          <input
            v-model="consoleInput"
            class="dp-input"
            placeholder="在此求值（如 print(x)）后回车"
            data-test="debug-console-input"
            @keyup.enter="runExpression"
          />
          <button class="dp-btn" data-test="debug-console-run" @click="runExpression">求值</button>
        </div>
      </section>

      <div v-if="debug.lastError" class="dp-error" data-test="debug-pane-error">{{ debug.lastError }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

import AppIcon from './AppIcon.vue';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { onDapOutput } from '../utils/dapBus';

const debug = useIdeDebugStore();
const watchInput = ref('');
const consoleInput = ref('');
// 控制台行缓冲：高频原始输出不进 Pinia（同 ptyBus 的理由），上限 2000 行防内存膨胀
const consoleLines = ref<string[]>([]);
const unsubs: Array<() => void> = [];

const consoleText = computed(() => consoleLines.value.join('\n'));

function appendConsole(text: string) {
  for (const line of text.split(/\r?\n/)) {
    consoleLines.value.push(line.replace(/\x1b\[[0-9;]*m/g, ''));
  }
  if (consoleLines.value.length > 2000) {
    consoleLines.value.splice(0, consoleLines.value.length - 2000);
  }
}

function baseName(path: string): string {
  const parts = String(path || '').split(/[\\/]/);
  return parts[parts.length - 1] || '';
}

async function addWatch() {
  const expression = watchInput.value.trim();
  if (!expression) return;
  await debug.addWatch(expression);
  watchInput.value = '';
}

async function runExpression() {
  const expression = consoleInput.value.trim();
  if (!expression) return;
  consoleInput.value = '';
  appendConsole(`> ${expression}`);
  const res = await debug.evaluate(expression);
  if (res?.ok) appendConsole(String(res.result ?? ''));
}

onMounted(() => {
  unsubs.push(onDapOutput((payload) => appendConsole(payload.text)));
});

onBeforeUnmount(() => {
  for (const unsub of unsubs) unsub();
  unsubs.length = 0;
});
</script>

<style scoped>
.debug-pane {
  display: flex;
  gap: 10px;
  height: 100%;
  min-height: 0;
  padding: 6px 8px;
  font-size: 11.5px;
  color: var(--text);
  overflow: hidden;
}
.dp-left,
.dp-right {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  min-height: 0;
}
.dp-left {
  flex: 0 0 42%;
}
.dp-right {
  flex: 1;
}
.dp-section {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.dp-console {
  flex: 1;
}
.dp-head {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 24px;
  padding: 0 7px;
  background: var(--bg2);
  color: var(--text-dim);
  font-weight: 600;
  flex-shrink: 0;
}
.dp-icon,
.dp-btn {
  border: none;
  background: transparent;
  color: var(--text-dim);
  cursor: pointer;
  border-radius: 4px;
}
.dp-btn {
  height: 20px;
  padding: 0 7px;
}
.dp-icon:hover,
.dp-btn:hover {
  background: var(--bg2);
  color: var(--text);
}
.dp-list {
  list-style: none;
  margin: 0;
  padding: 2px 0;
  overflow: auto;
  min-height: 0;
}
.dp-frame,
.dp-var,
.dp-watch-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 2px 8px;
  white-space: nowrap;
}
.dp-frame.active {
  background: var(--accent-soft);
}
.dp-frame-name,
.dp-var-name {
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
}
.dp-frame-loc,
.dp-var-type {
  color: var(--text-faint);
  font-size: 11px;
}
.dp-var-value {
  color: var(--accent);
  overflow: hidden;
  text-overflow: ellipsis;
}
.dp-vars {
  overflow: auto;
  min-height: 0;
}
.dp-scope-name {
  padding: 2px 8px;
  color: var(--text-dim);
}
.dp-empty {
  padding: 4px 8px;
  color: var(--text-faint);
}
.dp-watch-add,
.dp-console-in {
  display: flex;
  gap: 6px;
  padding: 4px 6px;
}
.dp-input {
  flex: 1;
  min-width: 0;
  height: 22px;
  padding: 0 6px;
  border: 1px solid var(--border);
  border-radius: 5px;
  background: var(--bg0);
  color: var(--text);
  font-family: Consolas, "Courier New", monospace;
  font-size: 11.5px;
}
.dp-console-out {
  flex: 1;
  margin: 0;
  padding: 6px 8px;
  overflow: auto;
  min-height: 0;
  background: var(--bg0);
  color: var(--text);
  font-family: Consolas, "Courier New", monospace;
  font-size: 11.5px;
  white-space: pre-wrap;
}
.dp-error {
  color: var(--orange);
  padding: 2px 4px;
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/DebugPane.test.ts`
Expected: `6 passed`

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/DebugPane.vue frontend/src/components/DebugPane.test.ts
git commit -m "feat(ide): 调试面板（调用栈/变量/监视/调试控制台）"
```

---

### Task 11: 编辑器断点 gutter 与当前执行行高亮

**Files:**
- Create: `frontend/src/ide/cmBreakpoints.ts`
- Create: `frontend/src/ide/cmBreakpoints.test.ts`
- Modify: `frontend/src/components/EditorPane.vue`（S1 交付物：把两个扩展接进 `extraExtensions`）

**Interfaces:**
- Consumes: `@codemirror/view`（`gutter` / `GutterMarker` / `Decoration` / `EditorView`）、`@codemirror/state`（`StateEffect` / `StateField` / `Extension`）——由 S1 加入 `frontend/package.json`；`ideStore.breakpointsFor(path)` / `toggleBreakpoint(path, line)`；`ideDebugStore.stopPath` / `stopLine` / `paused`
- Produces:
  - `breakpointKey(path: string, line: number): string`
  - `class BreakpointMarker extends GutterMarker`（`toDOM(): HTMLElement`，类名 `cm-ide-breakpoint`）
  - `breakpointMarker: BreakpointMarker`
  - `execLineDecoration: Decoration`
  - `setExecLineEffect: StateEffect<number | null>`
  - `execLineField: StateField<DecorationSet>`（`provide` 为 `EditorView.decorations`）
  - `breakpointGutter(options: { lines: () => number[]; onToggle: (line: number) => void }): Extension`

- [ ] **Step 1: 写失败测试** `frontend/src/ide/cmBreakpoints.test.ts`

```ts
import { describe, expect, it } from 'vitest';
import { EditorState } from '@codemirror/state';

import {
  BreakpointMarker,
  breakpointKey,
  execLineField,
  setExecLineEffect,
} from './cmBreakpoints';

const DOC = 'a\nb\nc\nd\n';

function makeState() {
  return EditorState.create({ doc: DOC, extensions: [execLineField] });
}

describe('breakpointKey', () => {
  it('builds the same key shape the store uses', () => {
    expect(breakpointKey('E:/p/app.py', 7)).toBe('E:/p/app.py:7');
  });
});

describe('BreakpointMarker', () => {
  it('renders a dot element with the breakpoint class', () => {
    const marker = new BreakpointMarker();

    expect(marker.toDOM().className).toBe('cm-ide-breakpoint');
  });

  it('treats markers of the same class as equal so gutters can diff them', () => {
    expect(new BreakpointMarker().eq(new BreakpointMarker())).toBe(true);
  });
});

describe('execLineField', () => {
  it('starts empty', () => {
    expect(makeState().field(execLineField).size).toBe(0);
  });

  it('decorates the line set by setExecLineEffect', () => {
    const state = makeState();

    const updated = state.update({ effects: setExecLineEffect.of(3) }).state;
    const decorations = updated.field(execLineField);
    let found: any = null;
    decorations.between(0, updated.doc.length, (from, _to, value) => {
      found = { from, value };
    });

    expect(decorations.size).toBe(1);
    expect(found.from).toBe(updated.doc.line(3).from);
    expect(found.value.spec.class).toBe('cm-ide-exec-line');
  });

  it('clears the decoration when the line is null or out of range', () => {
    const state = makeState();

    const cleared = state.update({ effects: setExecLineEffect.of(null) }).state;
    expect(cleared.field(execLineField).size).toBe(0);

    const outOfRange = state.update({ effects: setExecLineEffect.of(999) }).state;
    expect(outOfRange.field(execLineField).size).toBe(0);
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/ide/cmBreakpoints.test.ts`
Expected: FAIL，`Failed to resolve import "./cmBreakpoints"`

- [ ] **Step 3: 写实现** `frontend/src/ide/cmBreakpoints.ts`

```ts
// CodeMirror 6 断点 gutter 与当前执行行高亮。
//
// 只做"显示 + 点击切换"两件事：断点数据是唯一真源在 ideStore（规格 5.2），
// 调试会话的停止位置来自 ideDebugStore；本模块不持有业务状态。
// 点击切换的行号用 doc.lineAt 还原为 1-based 行号（gutter 回调给的是位置）。

import { EditorView, Decoration, GutterMarker, gutter, type DecorationSet, type Extension } from '@codemirror/view';
import { StateEffect, StateField } from '@codemirror/state';

export function breakpointKey(path: string, line: number): string {
  return `${path}:${line}`;
}

/** 断点标记：一个实心圆点，样式类供主题变量着色。 */
export class BreakpointMarker extends GutterMarker {
  toDOM(): HTMLElement {
    const dot = document.createElement('span');
    dot.className = 'cm-ide-breakpoint';
    return dot;
  }
}

export const breakpointMarker = new BreakpointMarker();

export const execLineDecoration = Decoration.line({ class: 'cm-ide-exec-line' });

export const setExecLineEffect = StateEffect.define<number | null>();

export const execLineField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(decorations, transaction) {
    let next = decorations.map(transaction.changes);
    for (const effect of transaction.effects) {
      if (!effect.is(setExecLineEffect)) continue;
      const line = effect.value;
      if (line === null || line < 1 || line > transaction.state.doc.lines) {
        next = Decoration.none;
        continue;
      }
      next = Decoration.set([execLineDecoration.range(transaction.state.doc.line(line).from)]);
    }
    return next;
  },
  provide: (field) => EditorView.decorations.from(field),
});

export function breakpointGutter(options: {
  lines: () => number[];
  onToggle: (line: number) => void;
}): Extension {
  return gutter({
    class: 'cm-ide-breakpoint-gutter',
    lineMarker: (view, line) => {
      const number = view.state.doc.lineAt(line.from).number;
      return options.lines().includes(number) ? breakpointMarker : null;
    },
    domEventHandlers: {
      mousedown: (view, line) => {
        options.onToggle(view.state.doc.lineAt(line.from).number);
        return true;
      },
    },
  });
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/ide/cmBreakpoints.test.ts`
Expected: `6 passed`

（gutter 的鼠标点击路径不在 vitest 里覆盖：jsdom 没有布局实现，CodeMirror 的 `EditorView` 初始化需要真实 `getClientRects`。该路径由 Task 13 的手工验收步骤覆盖。）

- [ ] **Step 5: 接进 EditorPane**（S1 交付的 `frontend/src/components/EditorPane.vue`）

在 imports 区加：

```ts
import { breakpointGutter, execLineField, setExecLineEffect } from '../ide/cmBreakpoints';
import { useIdeDebugStore } from '../stores/ideDebugStore';
```

在 `extraExtensions` 构造处（S1 已定义的扩展数组）追加：

```ts
const debug = useIdeDebugStore();
extraExtensions.push(
  breakpointGutter({
    lines: () => (props.path ? ide.breakpointsFor(props.path) : []),
    onToggle: (line) => {
      if (props.path) void debug.toggleBreakpoint(props.path, line);
    },
  }),
  execLineField,
);
```

并在调试停止位置变化时把执行行推给编辑器（放在 EditorPane 的同一个 `watch` 块里）：

```ts
watch(
  () => [debug.paused, debug.stopPath, debug.stopLine] as const,
  ([paused, stopPath, stopLine]) => {
    if (!view || !props.path) return;
    const sameFile = stopPath === props.path;
    view.dispatch({
      effects: setExecLineEffect.of(paused && sameFile ? Number(stopLine) || null : null),
    });
  },
);
```

- [ ] **Step 6: 运行相关测试**

Run: `cd frontend && npx vitest run src/ide/cmBreakpoints.test.ts src/stores/ideDebugStore.test.ts`
Expected: `6 passed` + `8 passed`

- [ ] **Step 7: 提交**

```bash
git add frontend/src/ide/cmBreakpoints.ts frontend/src/ide/cmBreakpoints.test.ts frontend/src/components/EditorPane.vue
git commit -m "feat(ide): 编辑器断点 gutter 与当前执行行高亮"
```

---

### Task 12: 底部面板组新增「调试控制台」页签

**Files:**
- Modify: `frontend/src/components/IdeBottomPanel.vue`（S1 交付物：页签表与内容区）
- Test: `frontend/src/components/IdeBottomPanel.debugTab.test.ts`

**Interfaces:**
- Consumes: Task 10 的 `DebugPane.vue`；`ideStore.bottomTab` / `setBottomTab` / `bottomPanelOpen`；S1 的 `IDE_BOTTOM_TABS` 页签表（`{ id: string; label: string }[]`，规格 5.1 已定标签文案：终端 / 问题 / 输出）
- Produces: 页签表新增 `{ id: 'debug', label: '调试控制台' }`；`DebugPane` 以 `v-show` 常驻挂载（与 `App.vue:193` 的 TerminalPane 同范式，收起期间调试输出不丢）

- [ ] **Step 1: 写失败测试** `frontend/src/components/IdeBottomPanel.debugTab.test.ts`

```ts
import { beforeEach, expect, it } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';
import { mount } from '@vue/test-utils';

import IdeBottomPanel from './IdeBottomPanel.vue';
import { useIdeStore } from '../stores/ideStore';
import { useIdeDebugStore } from '../stores/ideDebugStore';
import { emitDapOutput } from '../utils/dapBus';

beforeEach(() => {
  setActivePinia(createPinia());
  (window as any).pywebview = { api: {} };
});

it('lists the debug console tab and switches to it', async () => {
  const ide = useIdeStore();
  const wrapper = mount(IdeBottomPanel);

  const tab = wrapper.get('[data-test="bottom-tab-debug"]');
  expect(tab.text()).toContain('调试控制台');

  await tab.trigger('click');

  expect(ide.bottomTab).toBe('debug');
  wrapper.unmount();
});

it('keeps the debug pane mounted while another tab is active', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('terminal');
  const wrapper = mount(IdeBottomPanel);

  emitDapOutput({ session_id: 'dap-1', category: 'stdout', text: 'kept while hidden\n' });

  expect(wrapper.findComponent({ name: 'DebugPane' }).exists()).toBe(true);
  wrapper.unmount();
});

it('shows the debug pane when the tab is active', async () => {
  const ide = useIdeStore();
  ide.setBottomTab('debug');
  const wrapper = mount(IdeBottomPanel);

  expect(wrapper.find('[data-test="debug-pane"]').isVisible()).toBe(true);
  wrapper.unmount();
});
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run src/components/IdeBottomPanel.debugTab.test.ts`
Expected: FAIL，找不到 `[data-test="bottom-tab-debug"]`

- [ ] **Step 3: 在 IdeBottomPanel 里加页签与内容**

页签表常量改为（只加一行，其余保持 S1 原样）：

```ts
export const IDE_BOTTOM_TABS: { id: string; label: string }[] = [
  { id: 'terminal', label: '终端' },
  { id: 'problems', label: '问题' },
  { id: 'output', label: '输出' },
  { id: 'debug', label: '调试控制台' },
];
```

页签渲染加 `data-test`（其它页签同规则）：

```vue
<button
  v-for="tab in IDE_BOTTOM_TABS"
  :key="tab.id"
  class="bottom-tab"
  :class="{ active: store.bottomTab === tab.id }"
  :data-test="`bottom-tab-${tab.id}`"
  @click="store.setBottomTab(tab.id)"
>
  {{ tab.label }}
</button>
```

内容区加（与既有 TerminalPane 的 `v-show` 常驻写法一致，收起期间输出不丢）：

```vue
<DebugPane v-show="store.bottomTab === 'debug'" />
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run src/components/IdeBottomPanel.debugTab.test.ts src/components/DebugPane.test.ts`
Expected: `3 passed` + `6 passed`

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/IdeBottomPanel.vue frontend/src/components/IdeBottomPanel.debugTab.test.ts
git commit -m "feat(ide): 底部面板组新增调试控制台页签"
```

---

### Task 13: 回归验证与 S3 验收

**Files:**
- 只读验证，不新增文件（若发现必须改动的代码，另开任务，不在本任务里顺手改）

**Interfaces:**
- Consumes: 全部前序任务
- Produces: 验收结论（写进本次提交的 commit message 正文）

- [ ] **Step 1: 后端回归（含终端既有用例与字节稳定守卫）**

Run: `venv\Scripts\python -m pytest tests/test_gui_terminal.py tests/test_prompt_byte_stability.py tests/test_ide_dap_framing.py tests/test_ide_dap_process.py tests/test_ide_debug_adapters.py tests/test_ide_debug_session.py tests/test_ide_run_command.py tests/test_gui_ide_run.py tests/test_gui_ide_debug.py -q`
Expected: 全绿（`test_real_pty_session_roundtrip` 在 Windows 上执行真实 ConPTY，约 5 秒），0 failed

- [ ] **Step 2: 前端回归（终端既有用例必须不动）**

Run: `cd frontend && npx vitest run src/utils/ptyBus.test.ts src/utils/dapBus.test.ts src/components/TerminalPane.bridge.test.ts src/stores/appStore.terminalPanel.test.ts src/components/DebugToolbar.test.ts src/components/DebugPane.test.ts src/stores/ideDebugStore.test.ts src/ide/cmBreakpoints.test.ts src/components/IdeBottomPanel.debugTab.test.ts src/stores/appStore.ideDebugEvents.test.ts`
Expected: 全绿，0 failed

- [ ] **Step 3: 类型检查与构建**

Run: `cd frontend && npm run build`
Expected: `vue-tsc -b` 与 `vite build` 均成功；构建产物落在 `frontend/dist`（该目录永不提交）

- [ ] **Step 4: 全量后端测试（确认无新增失败）**

Run: `venv\Scripts\python -m pytest tests/ -q -m "not slow"`
Expected: 无新增失败（与改动前基线对比；`-m "not slow"` 跳过依赖真实 git 子进程的慢用例，标记定义见 `pytest.ini`）

- [ ] **Step 5: 手工验收（S3 验收标准逐条）**

在 GUI 里进入 IDE 模式，打开 `tests/fake_dap_adapter.py` 之外的任一本机 `.py` 文件：

1. 点调试工具条「运行」→ 底部终端页签自动展开并出现 `& "…python.exe" -u "…"` 命令与程序输出（对应验收标准 S3-1）。
2. 在编辑器行号左侧 gutter 点击第 7 行 → 出现红点；点工具条「调试」→ 底部切到调试控制台，调用栈出现 `main`，变量出现局部变量（对应 S3-2 前半）。
3. 点「单步跳过」→ 编辑器当前执行行高亮移动到下一行；在监视输入框加 `x` 能看到值；在调试控制台输入 `1+1` 显示求值结果（对应 S3-2 后半）。
4. 点「停止」→ 用任务管理器或 `Get-Process python` 确认没有遗留 `python.exe`（debugpy 子进程已回收）（对应验收标准 S3-3）。

Run: `powershell -NoProfile -Command "Get-Process python -ErrorAction SilentlyContinue | Select-Object Id,StartTime"`
Expected: 停止调试后不出现新增的 debugpy 常驻进程

- [ ] **Step 6: 提交验收记录**

```bash
git add --allow-empty
git commit -m "test(ide): S3 运行与调试回归验收通过（终端/字节稳定守卫均无回归）"
```

---

## 实现决策与规格偏差（供评审确认）

1. 新增三个 `core/ide/` 模块（`dap_manager.py` 之外还有 `debug_adapters.py` / `debug_session.py` / `run_command.py`）：规格 §4 的文件清单只列了 `dap_manager.py`，但把"适配器探测"与"DAP 流程编排"塞进同一文件会让它同时承担进程、协议、调试语义三类职责（违反 SKILL 的单一职责与小文件偏好）。
2. dlv 走 TCP：`dlv dap` 只提供 TCP 监听（无 stdio 模式），因此 `DapProcess` 必须支持两种传输；端口从 stderr 用宽松正则解析，避免绑定 dlv 的输出措辞。
3. 断点的 verified 状态放在 `ideDebugStore.breakpointStates`，断点本身仍按规格 5.2 放在 `ideStore.breakpoints`。
4. launch 响应的收取时机是**互操作假设**（以 debugpy 为准：launch 响应要等 configurationDone 之后才回）。若实测某适配器在 launch 后立即回响应，`await_response` 会先拿到已缓存响应，逻辑仍成立；若它永不回响应，会由 `LAUNCH_TIMEOUT` 报超时而不是静默卡死。
5. 前端 gutter 点击路径不做 vitest 覆盖（jsdom 无布局），由 Task 13 Step 5 的手工验收覆盖；`execLineField` 的状态级行为有单测。
6. 「调试」活动栏图标未新增：`frontend/src/components/iconPaths.ts` 现有图标集里没有 bug/step 类图标，本计划复用 `play` / `pause` / `square` / `refresh` / `chevron-*` 表达调试控制，不凭记忆新造 lucide path 数据。

## Self-Review

**1. Spec coverage（对照设计规格）**

| 规格条目 | 落点任务 |
|---|---|
| §6.2 运行：按语言猜命令 / 读项目配置 / 一键在现有终端面板执行 / 复用 `pty_*` | Task 5（解析）+ Task 6（`pty_terminal_ensure` + `pty_terminal_write`）+ Task 9（前端先展开终端页签） |
| §6.2 调试：`core/ide/dap_manager.py` 托管适配器 | Task 1（帧）+ Task 3（`DapProcess`）+ Task 4（`DebugSession`） |
| §6.2 适配器供给：Python=debugpy / Go=dlv / C-C++-Rust=lldb-dap，缺失要有提示 | Task 2；缺失提示经 Task 7 的 `ide_debug_start` 返回 `msg`，Task 9 用 `debug-error` 展示 |
| §6.2 DAP 帧与 LSP 同为 Content-Length、载荷字段不同 | Task 1（载荷校验 `type`/`event`/`request_seq`，不复用 LSP 代码） |
| §6.2 前端 `DebugPane.vue`（调用栈/变量/监视/调试控制台） | Task 10 |
| §6.2 编辑器侧断点 gutter、当前执行行高亮 | Task 11 |
| §6.2 调试工具条（继续/单步/跳出/重启/停止） | Task 9 |
| §5.1 / §6.2 底部面板组预留并填充「调试控制台」 | Task 12 |
| §5.5 事件 `dap_stopped` / `dap_output`（经 `emit_event` → `_js_queue`） | Task 4 归一化 + Task 7 `_emit_dap_event` + Task 8 分流 |
| §7.2 读线程只 `put_nowait`、不 `evaluate_js` | Task 3 `on_event` 回调约束 + Task 7 `_emit_dap_event` 注释与实现；Task 4 `_handle_stopped` 另起线程避免读线程死锁 |
| §7.4 运行/调试进程不进 `permission_broker`，但路径策略照旧 | Task 6 / Task 7 的 `_resolve_real_path` + `gui_path_access_allowed`，用例覆盖凭据文件与工作区外拒绝 |
| §8 错误处理：适配器启动失败 / 崩溃 / 工作区切换清理 | Task 2 提示、Task 3 通道断开唤醒等待者、Task 4 `start` 失败即回收、Task 7 `ide_debug_stop` 与 `shutdown` 回收 |
| §9 测试策略：`dap_manager` 帧编解码（分片/粘包）、断点与停止事件流转 | Task 1 用例、Task 3 用例、Task 4 端到端用例 |
| §10 S3-1 一键运行，输出落底部终端 | Task 5 / 6 / 9；Task 13 Step 5-1 验收 |
| §10 S3-2 断点、启动调试、单步、调用栈与变量、控制台求值 | Task 1-4 / 7 / 8-11；Task 13 Step 5-2、5-3 验收 |
| §10 S3-3 调试结束后适配器进程被回收 | Task 4 `test_stop_reaps_adapter_process` + Task 3 `test_terminate_*` + Task 7 `test_shutdown_debug_session_helper_reaps`；Task 13 Step 5-4 验收 |
| §9 回归红线：`test_prompt_byte_stability.py` 必须通过，本计划对请求路径零影响 | Task 13 Step 1 |

**2. Placeholder scan**：全文无 TBD / TODO / "稍后补充" / "类似上文"；每个代码步骤都给出可直接粘贴的完整代码；所有测试步骤都给出真实命令与预期输出。

**3. Type consistency**：跨任务复用的名字逐一核对过——`AdapterSpec.adapter_id / command / transport`（Task 2 定义，Task 3/4/7 使用）、`DapProcess.send_request / send_request_async / await_response / terminate`（Task 3 定义，Task 4 使用）、`DebugSession.session_id / set_breakpoints / configuration_done / continue_ / next_ / step_in / step_out / restart / threads / stack_trace / scopes / variables / evaluate / is_alive / stop`（Task 4 定义，Task 7 使用）、`RunPlan.command / source`（Task 5 定义，Task 6 使用）、`DapOutputPayload.session_id / category / text`（Task 8 定义，Task 8/10 使用）、`breakpointKey(path, line)`（Task 11 定义，与 `ideStore.breakpoints` 的 `${path}:${line}` 键一致）、`debugState` 取值集合 `idle|starting|running|paused|stopped`（Task 8 定义，Task 9/10 使用）。

**4. 与 S1 的接口对齐**：S3 前置契约里消费的 `ideStore` 成员（`activeTab`、`breakpoints`、`breakpointsFor`、`toggleBreakpoint`、`bottomTab`、`bottomPanelOpen`、`setBottomTab`、`toggleBottomPanel`）已全部回写进 S1 计划的 Task 8 冻结契约，命名与形态一致，实现时以 S1 的定义为准。
