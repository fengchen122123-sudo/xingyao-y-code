# 电脑操控提速与截图清理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把电脑操控典型任务（如微信发消息+读群消息）的模型往返次数与耗时大幅压缩（实测基线 111 次往返 / 37 分钟），并保证回合结束后所有观察截图被彻底清除。

**Architecture:** 三阶段。阶段一为协议扩展与可靠性止血：新增 `sequence` 批量动作（一次调用串行执行多个微动作、中途不截图、只返回最终截图，参考 microsoft/UFO 的 Speculative Multi-Action，官方数据减少约 51% LLM 调用）、type_text 非 ASCII 文本改走剪贴板粘贴（根治 IME 乱码）、RepeatCallGuard 豁免无副作用动作、回合收尾折叠全部观察截图与临时目录清扫。阶段二为结构升级（对齐 Windows-MCP/ZCode 的 a11y-first 架构）：新增 `list_elements`（UIA 控件树文本观察）与 `click_element`/`set_value_element`（元素引用动作），复用现有 comtypes UIAutomationCore 通道，不引入新依赖。阶段三为微信高级插件（参考 wxauto/wxauto-mcp）：把「按名找会话→发消息→读消息」收敛为一两次工具调用。

**Tech Stack:** Python 3 + 现有 core/computer 框架（controller 状态机、generic 协议投影、Win32 SendInput adapter）、comtypes UIAutomationCore（已有依赖）、pytest。

**背景数据（会话 147 实测，来自 ~/.ycode/ycode_gui.db）：** 114 个模型步骤共 2239 秒，其中 111 次 computer_action 工具执行合计仅 20.1 秒——98% 时间在等模型。往返次数黑洞：逐屏滚动读消息约 45 步、点击落空重试约 25 步、IME 乱码绕路 8 步、RepeatCallGuard 误拦 10 次。截图每张 ~0.6-1MB（1920x1080 PNG），占每步 ~900KB 请求的大头，但用户已决定**不压缩截图**（保持 1920 PNG）。

## Global Constraints

- 只在 `dev` 分支提交；不得合并到其他分支。
- Python 一律用 `venv\Scripts\python`；语法校验命令：`venv\Scripts\python -m py_compile <files>`。
- `core/computer/` 各模块不导入 GUI、CLI、LiteLLM、全局 config；重依赖（comtypes/UIA）必须函数内惰性导入。
- Harness Journal 只记录状态/ID/序号/哈希，严禁写入用户正文、截图字节、输入文本。
- 请求路径绝不改写历史中间字节；任何历史改写必须一次性、幂等、DB 层完成（折叠复用 `_fold_stale_media_locked` 的既有语义）。
- 插件单插件异常只记录日志并跳过，绝不影响 ycode 启动；插件兄弟模块用 `from core.plugin_loader import import_sibling` 加载。
- `tests/test_prompt_byte_stability.py` 黄金字节：tools schema 与动态提示的变更是本次**有意**的协议变更，按流程重新基线并在提交信息注明原因；其余 golden 不得动。
- 截图保持 1920 PNG 原样，**不做压缩**（用户决定）。
- provider 投影字节稳定性：相同画面/相同动作必须产生相同字节；投影不得含 png_path、时间戳、随机序号。
- 用户取消语义：cancelled 任务绝不自动重放副作用工具；sequence 执行中每步前复核 cancel_event。

---

## 阶段一：协议扩展与可靠性止血

### Task 1: `sequence` 动作 schema 与严格解析

**Files:**
- Modify: `core/computer/models.py`（ComputerActionKind、ComputerAction）
- Modify: `core/computer/actions.py`（_ALLOWED、schema、parse_computer_action）
- Test: `tests/test_computer_sequence.py`（新建）

**Interfaces:**
- Produces: `parse_computer_action` 支持 kind=`sequence`，产出 `ComputerAction(kind="sequence", target=..., steps=(ComputerAction, ...))`；常量 `MAX_SEQUENCE_STEPS = 10`。后续 Task 2/3 依赖此形状。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_computer_sequence.py"""
import pytest

from core.computer.actions import parse_computer_action
from core.computer.models import ComputerControlError


def test_parse_sequence_ok():
    action = parse_computer_action({
        "kind": "sequence",
        "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 100, "y": 200},
            {"kind": "type_text", "target": "desktop", "text": "你好"},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    assert action.kind == "sequence"
    assert [s.kind for s in action.steps] == ["click", "type_text", "keypress"]


def test_parse_sequence_rejects_nested_sequence():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence", "target": "desktop",
            "steps": [{"kind": "sequence", "target": "desktop", "steps": []}],
        })
    assert exc.value.code == "computer_invalid_action"


def test_parse_sequence_rejects_observe_and_semantic():
    for nested in (
        {"kind": "observe", "target": "desktop"},
        {"kind": "request_user_takeover", "target": "desktop", "reason_code": "login_required"},
        {"kind": "list_surfaces", "target": "desktop"},
    ):
        with pytest.raises(ComputerControlError):
            parse_computer_action({"kind": "sequence", "target": "desktop", "steps": [nested]})


def test_parse_sequence_rejects_target_mismatch():
    with pytest.raises(ComputerControlError) as exc:
        parse_computer_action({
            "kind": "sequence", "target": "desktop",
            "steps": [{"kind": "click", "target": "browser", "x": 1, "y": 2}],
        })
    assert "target" in exc.value.message


def test_parse_sequence_rejects_step_count():
    steps = [{"kind": "wait", "target": "desktop", "duration_ms": 100}] * 11
    with pytest.raises(ComputerControlError):
        parse_computer_action({"kind": "sequence", "target": "desktop", "steps": steps})


def test_parse_sequence_rejects_empty_steps():
    with pytest.raises(ComputerControlError):
        parse_computer_action({"kind": "sequence", "target": "desktop", "steps": []})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_sequence.py -v`
Expected: FAIL（未知动作类型 'sequence'）

- [ ] **Step 3: 实现**

`core/computer/models.py`：
```python
class ComputerActionKind(str, Enum):
    # ...现有项之后追加：
    SEQUENCE = "sequence"  # 批量微动作（2026-09-06 协议扩展）
```
```python
@dataclass(frozen=True)
class ComputerAction:
    # ...现有字段之后追加：
    # sequence 批量动作的子步骤（仅 kind="sequence" 非空；子项本身也是
    # 不可变 ComputerAction，且 kind 限定为无副作用定位的八种基本动作）。
    steps: tuple["ComputerAction", ...] = ()
```

`core/computer/actions.py`：
```python
MAX_SEQUENCE_STEPS = 10
# sequence 子步骤白名单：无副作用定位的基本输入动作。observe/request_user_takeover
# 改变操控状态、语义定位依赖观察候选、navigate 是 browser 专属跳转，均不允许打包。
_SEQUENCE_STEP_KINDS = frozenset(
    {"click", "double_click", "move", "drag", "scroll", "type_text", "keypress", "wait"}
)
```
`_ALLOWED` 追加：`"sequence": _COMMON | {"steps"}`；`COMPUTER_ACTION_TOOL_SCHEMA` 的 kind enum 会随 `list(_ALLOWED)` 自动扩展，`parameters.properties` 追加：
```python
"steps": {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_SEQUENCE_STEPS,
    "items": {"type": "object"},
    "description": "sequence 打包的子动作列表；每项是与 computer_action 相同格式的单个动作对象，kind 限 click/double_click/move/drag/scroll/type_text/keypress/wait，target 必须与本动作一致，按顺序执行，中途不截图",
},
```
（schema 层 items 故意宽松——嵌套完整动作 schema 会成倍膨胀 tools 字节；严格校验在 parse 层完成。）

`parse_computer_action` 在 `kind == "wait"` 分支之后追加：
```python
if kind == "sequence":
    steps_raw = payload.get("steps")
    if not isinstance(steps_raw, (list, tuple)) or not 1 <= len(steps_raw) <= MAX_SEQUENCE_STEPS:
        raise _invalid(f"sequence 需要 1-{MAX_SEQUENCE_STEPS} 个 steps")
    steps = []
    for index, item in enumerate(steps_raw):
        if not isinstance(item, Mapping):
            raise _invalid(f"steps[{index}] 必须是动作对象")
        step_kind = item.get("kind")
        if step_kind not in _SEQUENCE_STEP_KINDS:
            raise _invalid(
                f"steps[{index}] 的 kind {step_kind!r} 不允许在 sequence 内执行"
                "（observe/语义定位/request_user_takeover/navigate 必须单独调用）"
            )
        if item.get("target") != target:
            raise _invalid(f"steps[{index}] 的 target 必须与本动作一致：{target!r}")
        steps.append(parse_computer_action(dict(item)))
    kwargs["steps"] = tuple(steps)
```
注意：`sequence` 不加入 `_BROWSER_ONLY_KINDS`/`_DESKTOP_ONLY_KINDS`（两个 target 均可用）。

- [ ] **Step 4: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_computer_sequence.py tests/test_computer_actions.py -v`
（若不存在 test_computer_actions.py 则跑 `tests/` 下现有 actions 相关测试文件；用 `venv\Scripts\python -m pytest tests -k computer -v` 兜底。）
Expected: PASS（新旧全部）

- [ ] **Step 5: 语法校验 + 提交**

```bash
venv\Scripts\python -m py_compile core/computer/models.py core/computer/actions.py
git add core/computer/models.py core/computer/actions.py tests/test_computer_sequence.py
git commit -m "feat(computer): sequence 批量动作 schema 与严格解析（提速阶段一）"
```

### Task 2: `sequence` 执行路径（controller）

**Files:**
- Modify: `core/computer/controller.py:705-730`（execute 的输入分发段）
- Test: `tests/test_computer_sequence.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `ComputerAction.steps`。
- Produces: controller 对 `kind="sequence"` 串行执行全部子动作（每步前复核取消），整段计入 `input_ms`，最后一次 fresh observe 作为唯一观察；sequence 计 1 个 action ordinal，参与无进展计数（不在豁免清单）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_computer_sequence.py` 追加（复用现有 fake；先读 `tests/computer_fakes.py` 与 `tests/test_computer_windows_backend.py:47` 的 fake backend 构造方式，把 `backend.execute` 换成记录调用序列的记录器）：

```python
def test_controller_sequence_executes_all_steps_then_single_observe():
    """sequence：backend.execute 按序收到全部子动作，observe 恰一次。"""
    from core.computer.controller import ComputerControlService
    from core.computer.models import (
        BackendIdentity, ComputerAction, ComputerCapability, ComputerOwner,
    )
    from core.computer.models import ComputerObservation
    import threading

    calls = []

    class FakeBackend:
        target = "desktop"
        def start(self): pass
        def execute(self, run_id, action):
            calls.append(("execute", action.kind))
        def observe(self, run_id):
            calls.append(("observe", None))
            return ComputerObservation(run_id=run_id, target="desktop", action="sequence")
        def release_inputs(self): pass
        def close(self): pass
        def is_reusable(self): return False

    service = ComputerControlService(
        backend_factory=lambda target, identity: FakeBackend(),
    )
    owner = ComputerOwner(session_id="s1", turn_id="t1", session_label="测试",
                          cancel_event=threading.Event())
    capability = ComputerCapability(enabled=True, protocol="generic",
                                    targets=("desktop",), settings_generation=0)
    service.start_run(owner, capability, target="desktop")
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "click", "target": "desktop", "x": 1, "y": 2},
            {"kind": "keypress", "target": "desktop", "keys": ["ENTER"]},
        ],
    })
    observation = service.execute(service.snapshot().run_id, owner, action)
    assert [("execute", "click"), ("execute", "keypress"), ("observe", None)] == calls
    assert observation.ok
```

（若 fake 构造与现有测试设施冲突，以 `tests/computer_fakes.py` 的既有 owner/capability fixture 为准改写，保持断言语义不变。）

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_sequence.py -v`
Expected: 新测试 FAIL（sequence 进了 `_dispatch_locked` 分支或报不支持动作）

- [ ] **Step 3: 实现**

`core/computer/controller.py` 的 `execute()` 中（现约 705-730 行）把：

```python
if action.kind in ("observe", "request_user_takeover"):
    pass
else:
    input_started = time.perf_counter()
    try:
        backend.execute(run_id, action)
    finally:
        input_ms = round((time.perf_counter() - input_started) * 1000, 3)
        if action.kind == "wait":
            wait_ms = input_ms
```

改为：

```python
if action.kind in ("observe", "request_user_takeover"):
    pass
else:
    input_started = time.perf_counter()
    try:
        # sequence（2026-09-06 协议扩展）：子步骤串行执行，每步前复核取消；
        # 中途绝不 observe（模型只拿最终一张截图），整体计 1 个 ordinal。
        steps = action.steps if action.kind == "sequence" else (action,)
        for step in steps:
            with self._lock:
                self._raise_if_action_aborted_locked(run)
            backend.execute(run_id, step)
    finally:
        input_ms = round((time.perf_counter() - input_started) * 1000, 3)
        if action.kind == "wait":
            wait_ms = input_ms
```

另外：`controller.py` 模块 docstring 的动作顺序说明处补一行 sequence 语义；`_NO_PROGRESS_EXEMPT_KINDS` **不变**（sequence 预期改变画面，参与计数）。

- [ ] **Step 4: 运行测试确认通过**

Run: `venv\Scripts\python -m pytest tests/test_computer_sequence.py tests/test_computer_controller.py -v`（controller 测试文件名以实际为准，`-k controller` 兜底）
Expected: PASS

- [ ] **Step 5: 提交**

```bash
venv\Scripts\python -m py_compile core/computer/controller.py
git add core/computer/controller.py tests/test_computer_sequence.py
git commit -m "feat(computer): controller 串行执行 sequence 子动作并单次观察"
```

### Task 3: `sequence` 脱敏投影与动态提示

**Files:**
- Modify: `core/computer/actions.py`（sanitized_action_summary）
- Modify: `core/computer/provider_generic.py`（_GENERIC_DYNAMIC_PROMPT）
- Test: `tests/test_computer_sequence.py`（追加）

**Interfaces:**
- Produces: `sanitized_action_summary(sequence_action)` 返回含 `steps_count` 与逐步摘要数组 `steps`；动态提示第 2 条改为 sequence 语义。Task 8 的 golden 更新依赖此字节。

- [ ] **Step 1: 写失败测试**

```python
def test_sequence_summary_is_sanitized():
    action = parse_computer_action({
        "kind": "sequence", "target": "desktop",
        "steps": [
            {"kind": "type_text", "target": "desktop", "text": "机密正文"},
            {"kind": "click", "target": "desktop", "x": 5, "y": 6},
        ],
    })
    summary = sanitized_action_summary(action)
    assert summary["steps_count"] == 2
    assert summary["steps"][0]["text_code_points"] == 4
    assert "机密正文" not in json.dumps(summary, ensure_ascii=False)
    assert summary["steps"][1]["x"] == 5


def test_generic_prompt_mentions_sequence():
    from core.computer.provider_generic import build_generic_dynamic_prompt
    from core.computer.models import ComputerCapability
    cap = ComputerCapability(enabled=True, protocol="generic", targets=("desktop",))
    prompt = build_generic_dynamic_prompt(cap)
    assert "sequence" in prompt
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_sequence.py -v`
Expected: FAIL（summary 无 steps_count；提示词未提 sequence）

- [ ] **Step 3: 实现**

`core/computer/actions.py` 的 `sanitized_action_summary` 末尾追加：
```python
if action.steps:
    summary["steps_count"] = len(action.steps)
    summary["steps"] = [sanitized_action_summary(step) for step in action.steps]
```

`core/computer/provider_generic.py` 的 `_GENERIC_DYNAMIC_PROMPT` 第 2 条替换为：
```python
"2. 每次只调用一个 computer_action，不得与其他工具混用；有把握的连续微动作"
"（点击→输入→按键等）用 kind=sequence 打包一次执行（中途不截图，完成后只返回"
"最终截图）；没把握时单独执行动作并逐次验证。\n"
```

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_sequence.py -v
venv\Scripts\python -m py_compile core/computer/actions.py core/computer/provider_generic.py
git add core/computer/actions.py core/computer/provider_generic.py tests/test_computer_sequence.py
git commit -m "feat(computer): sequence 脱敏投影与动态提示"
```

### Task 4: type_text 非 ASCII 走剪贴板粘贴

**Files:**
- Modify: `core/computer/win32_adapter.py`（Win32InputAdapter 增加 set_clipboard_text）
- Modify: `core/computer/windows_backend.py:613-618`（type_text 分发分支）
- Test: `tests/test_computer_clipboard_type.py`（新建）

**Interfaces:**
- Produces: `Win32InputAdapter.set_clipboard_text(text: str) -> None`（失败抛 `ComputerControlError("computer_backend_error", ...)`）；desktop backend 对含非 ASCII 字符的 type_text 自动改为「写剪贴板 → Ctrl+V」，纯 ASCII 保持逐键注入。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_computer_clipboard_type.py"""
from core.computer.models import ComputerAction, ComputerControlError


class RecordingAdapter:
    """记录调用序列的假 adapter（仅暴露 backend 用到的接口）。"""
    def __init__(self):
        self.calls = []
        self.clipboard = None
    def ensure_interactive(self): pass
    def foreground_window_title(self): return "测试"
    def foreground_window_id(self): return 0
    def send_text(self, text): self.calls.append(("send_text", text))
    def send_keys(self, keys): self.calls.append(("send_keys", tuple(keys)))
    def set_clipboard_text(self, text):
        self.calls.append(("set_clipboard", text))
        self.clipboard = text
    # 其余方法按 tests/test_computer_windows_backend.py 的 fake adapter 签名补齐


def _backend_with(adapter):
    from core.computer.windows_backend import WindowsComputerBackend
    from PIL import Image
    return WindowsComputerBackend(
        image_grab=lambda: Image.new("RGB", (100, 100), "white"),
        win32_adapter=adapter,
    )


def test_ascii_text_keeps_send_text():
    adapter = RecordingAdapter()
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-test"
    backend.observe(run_id)
    backend.execute(run_id, ComputerAction(kind="type_text", target="desktop", text="hello"))
    assert ("send_text", "hello") in adapter.calls
    backend.close()


def test_chinese_text_goes_through_clipboard():
    adapter = RecordingAdapter()
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-test"
    backend.observe(run_id)
    backend.execute(run_id, ComputerAction(kind="type_text", target="desktop", text="大家好，我是星瑶"))
    assert ("set_clipboard", "大家好，我是星瑶") in adapter.calls
    assert ("send_keys", ("CTRL", "V")) in adapter.calls
    assert not any(c[0] == "send_text" for c in adapter.calls)
    backend.close()


def test_clipboard_failure_raises_stable_error():
    adapter = RecordingAdapter()
    def _boom(text): raise ComputerControlError("computer_backend_error", "剪贴板被占用")
    adapter.set_clipboard_text = _boom
    backend = _backend_with(adapter)
    backend.start()
    run_id = "run-test"
    backend.observe(run_id)
    try:
        backend.execute(run_id, ComputerAction(kind="type_text", target="desktop", text="中文"))
        assert False, "应当抛错"
    except ComputerControlError as e:
        assert e.code == "computer_backend_error"
    finally:
        backend.close()
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_clipboard_type.py -v`
Expected: FAIL（`RecordingAdapter` 无 set_clipboard_text 被 backend 调用 / 中文仍走 send_text）

- [ ] **Step 3: 实现**

`core/computer/win32_adapter.py` 的 `Win32InputAdapter` 增加（放在 `send_text` 之后）：

```python
def set_clipboard_text(self, text: str) -> None:
    """把 Unicode 文本写入系统剪贴板（CF_UNICODETEXT），供 type_text 中文路径使用。

    剪贴板可能被其他进程短暂占用：OpenClipboard 带重试；全部失败抛
    computer_backend_error。不恢复用户原剪贴板内容（与 wxauto 等业界实现
    一致，恢复竞态复杂度高且无可靠事务语义）。
    """
    if not isinstance(text, str) or not text:
        raise ComputerControlError("computer_invalid_action", "剪贴板文本必须非空")
    import time as _time

    data = text.encode("utf-16-le") + b"\x00\x00"
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    for attempt in range(10):
        if _ctypes_user32().OpenClipboard(0):
            try:
                _ctypes_kernel32().EmptyClipboard()
                handle = _ctypes_kernel32().GlobalAlloc(GMEM_MOVEABLE, len(data))
                if not handle:
                    raise OSError("GlobalAlloc 失败")
                pointer = _ctypes_kernel32().GlobalLock(handle)
                if not pointer:
                    raise OSError("GlobalLock 失败")
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    _ctypes_kernel32().GlobalUnlock(handle)
                if not _ctypes_kernel32().SetClipboardData(CF_UNICODETEXT, handle):
                    raise OSError("SetClipboardData 失败")
                return
            except OSError as error:
                raise ComputerControlError(
                    "computer_backend_error", f"写入剪贴板失败：{error}"
                )
            finally:
                _ctypes_user32().CloseClipboard()
        _time.sleep(0.03)
    raise ComputerControlError(
        "computer_backend_error", "打开剪贴板失败（被其他进程长期占用）"
    )
```

说明：`_ctypes_user32()`/`_ctypes_kernel32()` 指模块内**已有的** ctypes user32/kernel32 句柄获取方式——实现时先读 win32_adapter.py 顶部现有句柄常量（该文件已有 `KEYEVENTF_UNICODE` 等定义与 `_ctypes_send_input` 等函数），复用同一来源，不要新造第二套。若句柄是模块级常量（如 `user32 = ctypes.windll.user32`），直接引用常量即可。

`core/computer/windows_backend.py:613-618` 的 type_text 分支改为：

```python
elif kind == "type_text":
    if not isinstance(action.text, str) or not action.text:
        raise ComputerControlError(
            "computer_invalid_action", "type_text 需要非空 text"
        )
    if any(ord(ch) > 127 for ch in action.text):
        # 中文等非 ASCII 文本经 KEYEVENTF_UNICODE 注入会被 IME/目标应用
        # 输入处理扰动（会话 147 实测乱码），改走剪贴板 + Ctrl+V。
        self._adapter.set_clipboard_text(action.text)
        time.sleep(_CLIPBOARD_PASTE_DELAY_S)
        self._adapter.send_keys(("CTRL", "V"))
    else:
        self._adapter.send_text(action.text)
```

模块常量：`_CLIPBOARD_PASTE_DELAY_S = 0.05`（写剪贴板到粘贴的小间隔，给目标窗口焦点线程让路）；确认 windows_backend.py 顶部已 import time（若无则补）。

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_clipboard_type.py tests/test_computer_windows_backend.py -v
venv\Scripts\python -m py_compile core/computer/win32_adapter.py core/computer/windows_backend.py
git add core/computer/win32_adapter.py core/computer/windows_backend.py tests/test_computer_clipboard_type.py
git commit -m "feat(computer): type_text 非 ASCII 走剪贴板粘贴，根治 IME 乱码"
```

### Task 5: RepeatCallGuard 豁免无副作用电脑动作

**Files:**
- Modify: `core/harness/tool_pipeline.py:97-103`（repeat_guard_exempt）
- Test: `tests/test_computer_repeat_guard.py`（新建）

**Interfaces:**
- Consumes: 无。
- Produces: `repeat_guard_exempt("computer_action", {"kind": "observe"})` 返回 True；kind ∈ {observe, scroll, wait, move, list_surfaces} 豁免；click/type_text/keypress/sequence 等有副作用动作**不**豁免。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_computer_repeat_guard.py"""
from core.harness.tool_pipeline import repeat_guard_exempt


def test_observe_scroll_wait_move_exempt():
    for kind in ("observe", "scroll", "wait", "move", "list_surfaces"):
        assert repeat_guard_exempt("computer_action", {"kind": kind, "target": "desktop"})


def test_side_effect_kinds_not_exempt():
    for kind in ("click", "double_click", "type_text", "keypress", "drag", "sequence"):
        assert not repeat_guard_exempt("computer_action", {"kind": kind, "target": "desktop"})


def test_other_tools_unaffected():
    assert not repeat_guard_exempt("read_file", {"path": "x"})
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_repeat_guard.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`core/harness/tool_pipeline.py` 在 `_REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS` 之后追加：

```python
# 电脑操控无副作用/纯观察类 action 不消耗重复额度：逐屏滚动阅读、重新观察
# 与等待是合法重复（会话 147 实测被误拦 10 次，每次白烧一轮模型往返）。
# 点击/输入/sequence 等有副作用动作不豁免：同参数重复点击可能造成重复发送；
# 真死循环由 controller 的 no-progress 熔断（3 次画面不变停止）兜底。
_REPEAT_GUARD_EXEMPT_COMPUTER_KINDS = frozenset(
    {"observe", "scroll", "wait", "move", "list_surfaces"}
)
```

`repeat_guard_exempt` 改为：

```python
def repeat_guard_exempt(name: str, arguments: dict) -> bool:
    if name in _REPEAT_GUARD_EXEMPT_TOOLS:
        return True
    if name == "manage_task":
        action = str((arguments or {}).get("action", "")).strip().lower()
        return action in _REPEAT_GUARD_EXEMPT_MANAGE_TASK_ACTIONS
    if name == "computer_action":
        kind = str((arguments or {}).get("kind", "") or "").strip().lower()
        return kind in _REPEAT_GUARD_EXEMPT_COMPUTER_KINDS
    return False
```

（阶段二 Task 13 会把 `list_elements` 补进豁免集合。）

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_repeat_guard.py tests/test_tool_pipeline.py -v
git add core/harness/tool_pipeline.py tests/test_computer_repeat_guard.py
git commit -m "fix(harness): RepeatCallGuard 豁免电脑操控无副作用动作"
```

### Task 6: 回合收尾折叠全部观察截图（DB 层）

**Files:**
- Modify: `core/memory.py`（MemoryManager 新增 fold_session_computer_media，放在 `_fold_stale_media_locked` 附近）
- Modify: `core/llm.py:4513-4556`（_finalize_computer_run 收尾追加折叠调用）
- Test: `tests/test_memory_computer_fold.py`（新建）

**Interfaces:**
- Consumes: memory.py 模块级 `_folded_media_text`、`_try_parse_json`（已存在）。
- Produces: `MemoryManager.fold_session_computer_media(session_id: str) -> int`（返回折叠条数，幂等）；回合 complete/stop 后该会话全部 `_computer_v1` 观察消息的图片块被替换为占位文本。用户要求「截图最后都要清除」的 DB 层落点。

- [ ] **Step 1: 写失败测试**

参考 `tests/` 现有 memory 测试的临时 DB fixture 写法（conftest.py 已提供隔离的 YCODE_DIR fixture，见 test_token_usage.py:28 的用法）：

```python
"""tests/test_memory_computer_fold.py"""
import json


def _insert_observation(mm, session_id, with_image=True):
    content = [
        {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
    ]
    if with_image:
        content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}})
    mm.conn.execute(
        "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
        (session_id, json.dumps({"role": "user", "content": content,
                                  "_computer_v1": {"run_id": "r", "ordinal": 1, "target": "desktop"}},
                                 ensure_ascii=False)),
    )
    mm.conn.commit()


def test_fold_removes_all_computer_images_including_latest(tmp_memory):
    mm = tmp_memory
    _insert_observation(mm, "s-fold", with_image=True)
    _insert_observation(mm, "s-fold", with_image=True)
    folded = mm.fold_session_computer_media("s-fold")
    assert folded == 2
    rows = mm.conn.execute(
        "SELECT content FROM messages WHERE session_id=?", ("s-fold",)
    ).fetchall()
    for (raw,) in rows:
        assert "image_url" not in raw
        assert "已折叠" in raw
        # _computer_v1 私有元数据原样保留
        assert "_computer_v1" in raw


def test_fold_is_idempotent(tmp_memory):
    mm = tmp_memory
    _insert_observation(mm, "s-fold2")
    assert mm.fold_session_computer_media("s-fold2") == 1
    assert mm.fold_session_computer_media("s-fold2") == 0


def test_fold_never_touches_plain_media_messages(tmp_memory):
    mm = tmp_memory
    mm.conn.execute(
        "INSERT INTO messages(session_id, role, content) VALUES (?, 'user', ?)",
        ("s-fold3", json.dumps([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])),
    )
    mm.conn.commit()
    assert mm.fold_session_computer_media("s-fold3") == 0
    raw = mm.conn.execute(
        "SELECT content FROM messages WHERE session_id=?", ("s-fold3",)
    ).fetchone()[0]
    assert "image_url" in raw
```

（`tmp_memory` fixture：若 conftest 无现成的，按 `MemoryManager(db_path=str(tmp_path/"t.db"))` 自建并保证建表。）

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_memory_computer_fold.py -v`
Expected: FAIL（无 fold_session_computer_media 属性）

- [ ] **Step 3: 实现**

`core/memory.py` 的 `MemoryManager` 中、`_fold_stale_media_locked` 方法之后新增：

```python
def fold_session_computer_media(self, session_id):
    """回合收尾后折叠本会话全部电脑操控观察图（含最新一条）；幂等。

    与 _fold_stale_media_locked 的区别：只处理带 _computer_v1 私有元数据的
    user 消息 dict 形态，折叠其 content 内全部图片块（普通多模态消息与
    _fold_stale_media_locked 的「保留最新一条」语义均不触碰——两者职责
    互斥：前者保运行中滚动上下文，后者清回合结束后的全部截图）。
    铁律：一次性、幂等、DB 层完成，改写后字节永久冻结。
    """
    with self._lock:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "SELECT id, content FROM messages WHERE session_id = ? AND role = 'user' "
                "ORDER BY id ASC",
                (session_id,),
            )
            folded = 0
            for msg_id, raw in cursor.fetchall():
                parsed, ok = _try_parse_json(raw)
                if not ok or not isinstance(parsed, dict) or "_computer_v1" not in parsed:
                    continue
                blocks = parsed.get("content")
                if not isinstance(blocks, list) or not any(
                    isinstance(b, dict) and b.get("type") in ("image", "image_url")
                    for b in blocks
                ):
                    continue  # 已折叠或本就无图：幂等跳过
                parsed["content"] = [
                    {"type": "text", "text": _folded_media_text(b)}
                    if isinstance(b, dict) and b.get("type") in ("image", "image_url")
                    else b
                    for b in blocks
                ]
                cursor.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (json.dumps(parsed, ensure_ascii=False), msg_id),
                )
                folded += 1
            self.conn.commit()
            return folded
        except Exception:
            self.conn.rollback()
            raise
```

`core/llm.py` 的 `_finalize_computer_run()`（4513 行起）：在 `if computer_exit_kind == "completed": ... else: ... stop(...)` 分支之后、函数收尾（except 之前的主 try 内末尾）追加：

```python
        # 截图清除（用户要求）：回合终结后该会话所有观察图立即折叠为占位符。
        # provider_safety_required 的提前 return 不会走到这里（run 仍存活，
        # 模型恢复后还需要最后一张图）。
        try:
            manager = app_context.get_manager()
            fold_session = parent_session or app_context.get_current_session()
            if manager is not None and fold_session:
                folded = manager.fold_session_computer_media(str(fold_session))
                if folded:
                    logger.info(
                        "已折叠会话 %s 的 %d 张电脑操控观察截图", fold_session, folded
                    )
        except Exception:
            logger.warning("折叠电脑操控观察截图失败（已忽略）", exc_info=True)
```

注意：这段必须在 `computer_controller is None or not computer_run_id` 的提前 return **之后**（没有电脑操控回合就不需要折叠）；确认 `app_context` 已在 llm.py 模块级导入（5147 行已有 `app_context.get_manager()` 用法，应已导入）。

- [ ] **Step 4: 运行确认通过**

Run: `venv\Scripts\python -m pytest tests/test_memory_computer_fold.py tests/test_memory.py tests/test_llm.py -v`（后两个以实际存在为准，`-k "memory or fold" -v` 兜底）
Expected: PASS

- [ ] **Step 5: 提交**

```bash
venv\Scripts\python -m py_compile core/memory.py core/llm.py
git add core/memory.py core/llm.py tests/test_memory_computer_fold.py
git commit -m "feat(computer): 回合收尾折叠会话全部观察截图（DB 层幂等）"
```

### Task 7: 启动时清扫陈旧临时目录

**Files:**
- Modify: `core/computer/runtime.py`（新增 sweep_stale_computer_temp_dirs，start_computer_runtime 里调用）
- Test: `tests/test_computer_temp_sweep.py`（新建）

**Interfaces:**
- Produces: `sweep_stale_computer_temp_dirs(*, max_age_s=86400, now=None) -> int`；启动路径 `start_computer_runtime()` 自动执行。覆盖前缀：`ycode-computer-desktop-`、`ycode-computer-browser-`、`ycode-computer-preview`。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_computer_temp_sweep.py"""
import time


def test_sweep_removes_only_stale_matching_dirs(tmp_path, monkeypatch):
    from core.computer import runtime

    stale = tmp_path / "ycode-computer-desktop-abc"
    fresh = tmp_path / "ycode-computer-desktop-xyz"
    unrelated = tmp_path / "ycode-computer-unrelated"
    for d in (stale, fresh, unrelated):
        d.mkdir()
    old = time.time() - 25 * 3600
    import os
    os.utime(stale, (old, old))

    removed = runtime.sweep_stale_computer_temp_dirs(root=tmp_path)

    assert removed == 1
    assert not stale.exists()
    assert fresh.exists()      # 24h 内：在用，不删
    assert unrelated.exists()  # 前缀不匹配，不删


def test_sweep_never_raises(tmp_path):
    from core.computer import runtime
    (tmp_path / "ycode-computer-preview").mkdir()
    # now 传入未来时间制造 stat 竞态也不崩
    assert runtime.sweep_stale_computer_temp_dirs(root=tmp_path) >= 0
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_temp_sweep.py -v`
Expected: FAIL（无该函数）

- [ ] **Step 3: 实现**

`core/computer/runtime.py` 模块级（`_default_preview_root` 附近）追加：

```python
# 临时目录清扫（2026-09-06）：崩溃/断电可能残留 backend 截图目录与预览目录。
# mtime 超过 24h 才删——时间窗防止多实例场景误删正在使用的目录。
_STALE_TEMP_DIR_PREFIXES = (
    "ycode-computer-desktop-",
    "ycode-computer-browser-",
    "ycode-computer-preview",
)
_STALE_TEMP_MAX_AGE_S = 24 * 3600


def sweep_stale_computer_temp_dirs(
    *, root=None, max_age_s: int = _STALE_TEMP_MAX_AGE_S, now: float | None = None
) -> int:
    """清扫崩溃残留的电脑操控临时目录；失败只留痕，绝不影响启动。"""
    import shutil
    import time as _time

    root = Path(root) if root is not None else Path(tempfile.gettempdir())
    now = _time.time() if now is None else now
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_dir() or not any(
                entry.name.startswith(prefix) for prefix in _STALE_TEMP_DIR_PREFIXES
            ):
                continue
            if now - entry.stat().st_mtime < max_age_s:
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
        except OSError:
            _LOGGER.exception("清扫电脑操控临时目录失败：%s", entry)
    return removed
```

`start_computer_runtime()` 在 `global _RUNTIME` 之后、`with _RUNTIME_LOCK:` 之前加一行：

```python
    sweep_stale_computer_temp_dirs()
```

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_temp_sweep.py tests/test_computer_runtime.py -v
venv\Scripts\python -m py_compile core/computer/runtime.py
git add core/computer/runtime.py tests/test_computer_temp_sweep.py
git commit -m "feat(computer): 启动清扫崩溃残留的操控临时目录"
```

### Task 8: 阶段一回归 + prompt 黄金字节重基线 + 设计文档补遗

**Files:**
- Modify: `tests/test_prompt_byte_stability.py`（仅 tools schema 与动态提示两个 golden）
- Create: `docs/superpowers/specs/2026-09-06-computer-speedup-design.md`（简短补遗：sequence 协议 + 剪贴板输入 + 截图清除语义）

**Interfaces:**
- Consumes: Task 1-5 全部字节变更。

- [ ] **Step 1: 跑字节稳定性测试，收集 diff**

Run: `venv\Scripts\python -m pytest tests/test_prompt_byte_stability.py -v`
Expected: FAIL——diff 应**只**包含：(a) tools schema 的 kind enum 新增 "sequence" 与 steps 属性；(b) 动态提示第 2 条新文案。任何其他 diff 都说明实现误改了别的字节，回去修。

- [ ] **Step 2: 重新基线 golden**

按该测试文件现有的 golden 更新方式（读取文件内注释/`python tests/request_payload_snapshot.py` 之类的再生成机制），用新字节更新，并在更新处注明：`# 2026-09-06 sequence 协议扩展有意变更（提速计划 Task 8）`。

- [ ] **Step 3: 全量回归**

Run: `venv\Scripts\python -m pytest tests -k "computer or prompt or memory or pipeline" -v`
Expected: 全部 PASS

- [ ] **Step 4: 写设计补遗文档并提交**

`docs/superpowers/specs/2026-09-06-computer-speedup-design.md` 记录：sequence 协议（子步骤白名单、单观察、无进展计数语义）、剪贴板 type_text（不恢复剪贴板的取舍）、截图清除（DB 折叠 + 临时目录清扫 + 24h 窗口）、以及调研依据（UFO Speculative Multi-Action ~51%、Windows-MCP 双轨观察、wxauto UIA 路线，附仓库 URL）。

```bash
git add tests/test_prompt_byte_stability.py docs/superpowers/specs/2026-09-06-computer-speedup-design.md
git commit -m "test(computer): 阶段一协议变更重基线 prompt 黄金字节；补设计文档"
```

---

## 阶段二：UIA 文本观察 + 元素引用动作

> 仿做对象：Windows-MCP 的 `Snapshot`（控件树文本观察 + 元素 ID）与 ZCode 的 a11y-first。底层复用现有 comtypes UIAutomationCore 通道（`requirements.txt:27` 已有 `comtypes>=1.4`），**不引入 uiautomation 新依赖**。

### Task 9: WindowsElementLocator（UIA 元素收集与操作）

**Files:**
- Create: `core/computer/platforms/windows_elements.py`
- Test: `tests/test_computer_elements.py`（新建）

**Interfaces:**
- Produces: `WindowsElementLocator`，接口（供 backend 注入 fake 测试）：
  - `collect(hwnd: int) -> list[dict]`：返回 `[{"ref": "el-1", "kind": "ButtonControl", "name": "发送", "bounds": [l,t,r,b], "enabled": True}, ...]`（BFS、最多 `MAX_ELEMENTS=200`、深度 `MAX_TREE_DEPTH=14`、name 截 80 字符、只收可交互 ControlType）。
  - `click(ref: str) -> None`：对已收集元素执行 InvokePattern（不支持则抛稳定错误）。
  - `set_value(ref: str, text: str) -> None`：ValuePattern.SetValue（不支持则抛稳定错误）。
  - `close() -> None`。
  - ref 在下一次 collect 后整体失效，旧 ref 抛 `ComputerControlError("computer_element_stale", ...)`。
  - 真实实现全部 UIA 调用走**专用 COM 线程**——照抄 `windows_surfaces.py` 的 `_ComThread` 模式（160-203 行：CoInitialize → GetModule("UIAutomationCore.dll") → CreateObject(IUIAutomation)，job 队列分发）。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_computer_elements.py"""
import pytest

from core.computer.models import ComputerControlError
from core.computer.platforms.windows_elements import WindowsElementLocator


class FakeUIAElement:
    def __init__(self, name):
        self.name = name
        self.invoked = False
        self.value_set = None
        self.invoke_supported = True
        self.value_supported = True


class FakeComHost:
    """绕过真实 COM：直接以 dict 模拟元素表。"""
    def __init__(self):
        self._elements = {}
        self._generation = 0
    def collect_impl(self, hwnd):
        self._generation += 1
        self._elements = {
            f"el-{n}": FakeUIAElement(f"控件{n}") for n in range(1, 4)
        }
        return [
            {"ref": f"el-{n}", "kind": "ButtonControl", "name": f"控件{n}",
             "bounds": [0, 0, 10, 10], "enabled": True}
            for n in range(1, 4)
        ], dict(self._elements)
    def click_impl(self, ref):
        element = self._require(ref)
        element.invoked = True
    def set_value_impl(self, ref, text):
        element = self._require(ref)
        element.value_set = text
    def _require(self, ref):
        if ref not in self._elements:
            raise ComputerControlError(
                "computer_element_stale", "元素引用已失效，请重新 list_elements")
        return self._elements[ref]


def _locator_with_fake(monkeypatch):
    host = FakeComHost()
    locator = WindowsElementLocator(com_host=host,
                                    collect_fn=host.collect_impl,
                                    click_fn=host.click_impl,
                                    set_value_fn=host.set_value_impl)
    return locator, host


def test_collect_and_click_roundtrip(monkeypatch):
    locator, host = _locator_with_fake(monkeypatch)
    elements = locator.collect(hwnd=0)
    assert elements[0]["ref"] == "el-1"
    locator.click("el-2")
    assert host._elements["el-2"].invoked


def test_stale_ref_after_recollect():
    locator, host = _locator_with_fake(monkeypatch)
    locator.collect(hwnd=0)
    locator.collect(hwnd=0)  # 重新收集：旧代元素表整体作废
    with pytest.raises(ComputerControlError) as exc:
        locator.click("el-1")
    assert exc.value.code == "computer_element_stale"
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_elements.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`core/computer/platforms/windows_elements.py`（骨架——真实 COM 部分照抄 `windows_surfaces.py` 的 `_ComThread` 与 `_DefaultSurfaceHost` 模式，把「枚举窗口」换成「IUIAutomation.ElementFromHandle(hwnd) → TreeWalker BFS」；`GetPatternProvider` 用 `GetCurrentPattern(IUIAutomationInvokePatternId)` / `IUIAutomationValuePatternId`，常量 id：Invoke=10000、Value=10002）：

```python
"""Windows UIA 元素定位器（阶段二，a11y-first 仿做）。

只读无障碍树 + 语义 Invoke/Value，绝不注入内存或 hook。所有真实 UIA 调用
在专用 COM 线程执行（复用 windows_surfaces._ComThread 模式）；测试注入
com_host seam 绕过 COM。收集失败抛 computer_backend_error，ref 过期抛
computer_element_stable —— 错误码稳定英文（设计 21 节约定）。
"""

from __future__ import annotations

import threading

from core.computer.models import ComputerControlError

MAX_ELEMENTS = 200
MAX_TREE_DEPTH = 14
_MAX_NAME_CHARS = 80
# 只收集可交互控件（参照 Windows-MCP Snapshot 的交互元素过滤）
_INTERACTIVE_TYPES = frozenset({
    "ButtonControl", "EditControl", "DocumentControl", "HyperlinkControl",
    "ListItemControl", "TabItemControl", "MenuItemControl", "ComboBoxControl",
    "CheckBoxControl", "RadioButtonControl", "TreeItemControl", "DataItemControl",
})


class WindowsElementLocator:
    """前台窗口元素收集 + 元素引用操作；线程安全，关闭幂等。"""

    def __init__(self, com_host=None, collect_fn=None, click_fn=None, set_value_fn=None):
        self._lock = threading.Lock()
        self._elements: dict[str, object] = {}
        if collect_fn is not None:
            # 测试注入路径：com_host 仅为兼容签名占位
            self._collect_fn = collect_fn
            self._click_fn = click_fn
            self._set_value_fn = set_value_fn
        else:
            from core.computer.platforms.windows_elements_uia import (
                RealUIAHost,
            )

            self._real = RealUIAHost()  # 惰性创建 COM 线程
            self._collect_fn = self._real.collect
            self._click_fn = self._real.click
            self._set_value_fn = self._real.set_value

    def collect(self, hwnd: int) -> list[dict]:
        with self._lock:
            listing, elements = self._collect_fn(int(hwnd))
            self._elements = elements
            return listing

    def click(self, ref: str) -> None:
        with self._lock:
            self._click_fn(ref)

    def set_value(self, ref: str, text: str) -> None:
        with self._lock:
            self._set_value_fn(ref, text)

    def close(self) -> None:
        with self._lock:
            real = getattr(self, "_real", None)
            if real is not None:
                real.close()
```

同文件或同目录 `windows_elements_uia.py` 放 `RealUIAHost`：复制 `windows_surfaces.py:126-203` 的 `_ComThread`（逐字复用，含 CoInitialize/GetModule/CreateObject 与 job 队列），实现：

```python
def collect(self, hwnd):
    """在 COM 线程内 ElementFromHandle → BFS 收集；返回 (listing, ref→IUIAutomationElement 映射)。"""
def click(self, ref):
    """InvokePattern.Invoke；pattern 不支持抛 computer_element_unsupported。"""
def set_value(self, ref, text):
    """ValuePattern.SetValue(text)；不支持抛 computer_element_unsupported。"""
def _require(self, elements, ref):
    """ref 不存在抛 computer_element_stale（统一文案：请重新 list_elements）。"""
```

BFS 细节：`walker = uia.GetRawViewWalker()`（或 ControlViewWalker，优先 ControlView 减噪），从 `uia.ElementFromHandle(hwnd)` 出发逐层 `GetFirstChildElement/GetNextSiblingElement`，`CurrentControlType` 经 `uia.GetProgrammaticName`（或从 comtypes.gen 枚举映射）得到 `*Control` 名称，`CurrentName`、`CurrentIsEnabled`、`CurrentBoundingRectangle`（physical 像素）逐元素记录；超 MAX_ELEMENTS 或 MAX_TREE_DEPTH 截断；`CurrentName` 为空的交互元素保留（ref 仍可 Invoke），name 列空串。

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_elements.py -v
venv\Scripts\python -m py_compile core/computer/platforms/windows_elements.py core/computer/platforms/windows_elements_uia.py
git add core/computer/platforms/windows_elements.py core/computer/platforms/windows_elements_uia.py tests/test_computer_elements.py
git commit -m "feat(computer): WindowsElementLocator UIA 元素收集与语义操作"
```

### Task 10: 三种元素动作的 schema/解析/模型字段

**Files:**
- Modify: `core/computer/models.py`（ActionKind + ComputerObservation.elements）
- Modify: `core/computer/actions.py`（_ALLOWED、_DESKTOP_ONLY_KINDS、schema、summary）
- Test: `tests/test_computer_elements.py`（追加）

**Interfaces:**
- Consumes: Task 9 的 locator 接口。
- Produces: kind `list_elements` / `click_element` / `set_value_element`（desktop only）；`ComputerAction.element_ref: str | None`；`ComputerObservation.elements: tuple = ()`；`sanitized_action_summary` 含 `element_ref`。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_computer_elements.py
def test_parse_element_actions():
    from core.computer.actions import parse_computer_action
    a = parse_computer_action({"kind": "click_element", "target": "desktop", "element_ref": "el-3"})
    assert a.element_ref == "el-3"
    b = parse_computer_action({"kind": "set_value_element", "target": "desktop",
                               "element_ref": "el-1", "text": "中文内容"})
    assert b.text == "中文内容"
    from core.computer.actions import observation_provider_projection  # 确认 schema 校验
    from core.computer.actions import COMPUTER_ACTION_TOOL_SCHEMA
    assert "list_elements" in COMPUTER_ACTION_TOOL_SCHEMA["function"]["parameters"]["properties"]["kind"]["enum"]


def test_element_actions_reject_browser_target():
    from core.computer.actions import parse_computer_action
    from core.computer.models import ComputerControlError
    import pytest
    with pytest.raises(ComputerControlError):
        parse_computer_action({"kind": "click_element", "target": "browser", "element_ref": "el-1"})


def test_summary_includes_element_ref():
    from core.computer.actions import parse_computer_action, sanitized_action_summary
    a = parse_computer_action({"kind": "click_element", "target": "desktop", "element_ref": "el-3"})
    assert sanitized_action_summary(a)["element_ref"] == "el-3"
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_elements.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`core/computer/models.py`：
```python
class ComputerActionKind(str, Enum):
    # R10 之后追加：
    LIST_ELEMENTS = "list_elements"        # 阶段二：UIA 控件树文本观察
    CLICK_ELEMENT = "click_element"        # 阶段二：元素引用点击
    SET_VALUE_ELEMENT = "set_value_element"  # 阶段二：元素引用直接写值
```
`ComputerAction` 追加字段 `element_ref: str | None = None`；`ComputerObservation` 追加字段：
```python
    # 阶段二：list_elements 观察携带的控件元素列表（dict：ref/kind/name/
    # bounds/enabled）；仅本次观察有效，普通观察恒为空 tuple。与
    # surface_candidates 同一生命周期语义。
    elements: tuple = ()
```

`core/computer/actions.py`：
```python
_ALLOWED 追加：
    "list_elements": _COMMON,
    "click_element": _COMMON | {"element_ref"},
    "set_value_element": _COMMON | {"element_ref", "text"},
_DESKTOP_ONLY_KINDS 追加 {"list_elements", "click_element", "set_value_element"}
```
schema properties 追加 `"element_ref": {"type": "string", "minLength": 1}`。
parse 分支（在 surface 三动作分支后）：
```python
    if kind in ("click_element", "set_value_element"):
        ref = payload.get("element_ref")
        if not isinstance(ref, str) or not ref:
            raise _invalid(f"{kind} 需要非空 element_ref（来自最近一次 list_elements）")
        kwargs["element_ref"] = ref
```
`sanitized_action_summary` 追加：
```python
    if action.element_ref:
        summary["element_ref"] = action.element_ref
```

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_elements.py -v
venv\Scripts\python -m py_compile core/computer/models.py core/computer/actions.py
git add core/computer/models.py core/computer/actions.py tests/test_computer_elements.py
git commit -m "feat(computer): 元素动作 schema 与解析（list_elements/click_element/set_value_element）"
```

### Task 11: backend 接线三种元素动作

**Files:**
- Modify: `core/computer/windows_backend.py`（execute 分发 + observe 附加 elements）
- Test: `tests/test_computer_elements.py`（追加，用 fake locator 注入 backend）

**Interfaces:**
- Consumes: Task 9/10。
- Produces: backend 构造参数 `element_locator=None`（None 时三种元素动作返回 `computer_elements_unavailable` 稳定错误）；`list_elements` 执行后紧随的 observe 携带 `elements`；click_element/set_value_element 走 locator 后照常截图观察。

- [ ] **Step 1: 写失败测试**

```python
# 追加到 tests/test_computer_elements.py（复用 Task 4 的 RecordingAdapter 模式）
def _backend_with_locator(locator):
    from core.computer.windows_backend import WindowsComputerBackend
    from PIL import Image
    return WindowsComputerBackend(
        image_grab=lambda: Image.new("RGB", (100, 100), "white"),
        win32_adapter=_recording_adapter(),
        element_locator=locator,
    )


def test_backend_list_elements_attaches_to_observation():
    locator, host = _locator_with_fake(None)
    backend = _backend_with_locator(locator)
    backend.start()
    run_id = "run-el"
    backend.execute(run_id, ComputerAction(kind="list_elements", target="desktop"))
    obs = backend.observe(run_id)
    assert obs.elements and obs.elements[0]["ref"] == "el-1"
    backend.close()


def test_backend_click_element_without_locator_is_stable_error():
    backend = _backend_with_locator(None)
    backend.start()
    run_id = "run-el2"
    backend.observe(run_id)
    try:
        backend.execute(run_id, ComputerAction(kind="click_element", target="desktop", element_ref="el-1"))
        assert False
    except ComputerControlError as e:
        assert e.code == "computer_elements_unavailable"
    finally:
        backend.close()
```

（`_recording_adapter` 从 tests/test_computer_clipboard_type.py 的 RecordingAdapter 提炼为公共 fixture 或直接复制。）

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_elements.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`core/computer/windows_backend.py`：

1. `__init__` 追加参数 `element_locator=None`；`self._element_locator = element_locator`、`self._pending_elements: tuple = ()`（锁内初始化）。
2. `execute()` 的 `elif kind in _SEMANTIC_DESKTOP_KINDS:` 分支**之前**插入元素动作分支：
```python
            elif kind in _ELEMENT_KINDS:
                locator = self._element_locator
                if locator is None:
                    raise ComputerControlError(
                        "computer_elements_unavailable",
                        "当前桌面 backend 未提供 UIA 元素定位能力",
                    )
                if kind == "list_elements":
                    hwnd = self._adapter.foreground_window_id()
                    self._pending_elements = tuple(locator.collect(hwnd))
                    return
                # 点击/写值与普通输入动作一样要求存在当前画面基线（R5）。
                self._require_current_surface_locked()
                if kind == "click_element":
                    locator.click(action.element_ref)
                else:
                    if not isinstance(action.text, str) or not action.text:
                        raise ComputerControlError("computer_invalid_action", "set_value_element 需要非空 text")
                    locator.set_value(action.element_ref, action.text)
                return
```
（`_ELEMENT_KINDS = frozenset({"list_elements", "click_element", "set_value_element"})` 模块常量；`_require_current_surface_locked` 是 backend 现有方法，list_elements 属纯观察不要求基线，click/set_value 要求。）
3. `observe()` 构造 ComputerObservation 的两处（全屏 observe 与 observe_region）追加 `elements=self._pending_elements`，并在取用后清空：`self._pending_elements = ()`（与 `_pending_surface_candidates` 的取用-清空模式保持一致；若现有代码是在 observe 里读 pending candidates，把 elements 挂同一位置）。

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_elements.py tests/test_computer_windows_backend.py tests/test_computer_surfaces.py -v
venv\Scripts\python -m py_compile core/computer/windows_backend.py
git add core/computer/windows_backend.py tests/test_computer_elements.py
git commit -m "feat(computer): desktop backend 接线元素观察与元素引用动作"
```

### Task 12: 文本观察 followup 消息 + 提示词 + 豁免补充

**Files:**
- Modify: `core/computer/provider_generic.py`（_observation_followup_message 双路径、_GENERIC_DYNAMIC_PROMPT）
- Modify: `core/harness/tool_pipeline.py`（豁免集合补 list_elements）
- Test: `tests/test_computer_elements.py`（追加）

**Interfaces:**
- Consumes: Task 11 的 `observation.elements`。
- Produces: 有元素时 followup user 消息为纯文本控件列表（无图片字节、请求从 ~900KB 降到几 KB）；普通观察的字节完全不变。

- [ ] **Step 1: 写失败测试**

```python
def test_followup_text_observation_when_elements_present():
    from core.computer.provider_generic import _observation_followup_message
    from core.computer.models import ComputerObservation
    obs = ComputerObservation(run_id="r", target="desktop", elements=(
        {"ref": "el-1", "kind": "EditControl", "name": "输入", "bounds": [0, 0, 5, 5], "enabled": True},
    ))
    msg = _observation_followup_message(obs, run_id="r", ordinal=1)
    assert msg["_computer_v1"]["ordinal"] == 1
    assert all(block["type"] == "text" for block in msg["content"])
    assert "el-1" in msg["content"][-1]["text"]


def test_followup_image_path_unchanged_when_no_elements(tmp_path):
    """无元素时维持既有截图路径字节（回归保护）。"""
    import base64 as _b64
    from io import BytesIO
    from PIL import Image
    from core.computer.provider_generic import _observation_followup_message
    from core.computer.models import ComputerObservation

    buffer = BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    png_path = tmp_path / "shot.png"
    png_path.write_bytes(buffer.getvalue())
    obs = ComputerObservation(run_id="r", target="desktop", png_path=str(png_path))
    msg = _observation_followup_message(obs, run_id="r", ordinal=1)
    image_blocks = [b for b in msg["content"] if b.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_repeat_guard_exempts_list_elements():
    from core.harness.tool_pipeline import repeat_guard_exempt
    assert repeat_guard_exempt("computer_action", {"kind": "list_elements", "target": "desktop"})
```

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_computer_elements.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`core/computer/provider_generic.py` 的 `_observation_followup_message` 改为双路径：

```python
_OBSERVATION_ELEMENTS_TEXT_PREFIX = (
    "【电脑观察·控件列表】请优先用元素引用动作操作"
    "（click_element 点击 / set_value_element 写值，目标输入框优先 set_value_element）；"
    "元素都不可用时再用截图坐标。元素列表："
)


def _observation_followup_message(observation, *, run_id: str, ordinal: int) -> dict:
    """观察 follow-up user 消息：有元素列表时为纯文本（a11y-first，省截图字节），
    否则维持截图 data URL 路径；_computer_v1 私有元数据两种路径一致。"""
    meta = {"run_id": run_id, "ordinal": int(ordinal), "target": observation.target}
    if observation.elements:
        lines = [
            f"- {e.get('ref', '')} {e.get('kind', '')} name={e.get('name', '')!r} "
            f"bounds={tuple(e.get('bounds', ()))} enabled={bool(e.get('enabled', True))}"
            for e in observation.elements
        ]
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": _OBSERVATION_ELEMENTS_TEXT_PREFIX},
                {"type": "text", "text": "\n".join(lines)},
            ],
            "_computer_v1": meta,
        }
    png_bytes = _read_observation_png(observation.png_path)
    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": OBSERVATION_FOLLOWUP_TEXT},
            {"type": "image_url", "image_url": {"url": data_url}},
        ],
        "_computer_v1": meta,
    }
```

`_GENERIC_DYNAMIC_PROMPT` 第 1 条后追加一条说明（重排序号为 7）：
```
"7. 原生应用优先用 list_elements 拿控件列表，再以 click_element/set_value_element"
"按元素引用操作（中文写值不过输入法）；坐标点击只用于元素不可达的场景。"
```

`core/harness/tool_pipeline.py` 的 `_REPEAT_GUARD_EXEMPT_COMPUTER_KINDS` 追加 `"list_elements"`。

- [ ] **Step 4: 运行确认通过 + 提交**

```bash
venv\Scripts\python -m pytest tests/test_computer_elements.py tests/test_computer_provider_generic.py -v
venv\Scripts\python -m py_compile core/computer/provider_generic.py core/harness/tool_pipeline.py
git add core/computer/provider_generic.py core/harness/tool_pipeline.py tests/test_computer_elements.py
git commit -m "feat(computer): 元素列表文本观察 followup（a11y-first）"
```

### Task 13: 阶段二回归 + golden 重基线

- [ ] **Step 1: 字节稳定性测试**——同 Task 8 流程，预期 diff 仅限：kind enum 三新增、element_ref/steps 属性、动态提示第 7 条与第 2 条文案。重新基线并注明 `# 2026-09-06 元素动作协议扩展（提速计划 Task 13）`。

- [ ] **Step 2: 全量回归**

Run: `venv\Scripts\python -m pytest tests -k "computer or prompt" -v`
Expected: 全部 PASS

- [ ] **Step 3: 提交**

```bash
git add tests/test_prompt_byte_stability.py
git commit -m "test(computer): 阶段二元素动作协议重基线 prompt 黄金字节"
```

---

## 阶段三：微信高级插件（仿 wxauto-mcp）

### Task 14: plugins/wechat_tool 插件

**Files:**
- Create: `plugins/wechat_tool/plugin.json`
- Create: `plugins/wechat_tool/plugin.py`
- Create: `plugins/wechat_tool/wechat_ui.py`（兄弟模块，UIA 交互层）
- Test: `tests/test_wechat_plugin.py`（新建）

**Interfaces:**
- Consumes: comtypes UIA（独立 COM 线程，照抄 windows_surfaces 模式）；插件加载器 `register(registry)` + `ToolSpec`（照抄 `plugins/hunyuan_image/plugin.py:18` 的形态）；兄弟模块 `import_sibling`。
- Produces: 工具 `wechat_send_message(who: str, message: str, exact: bool = False)` 与 `wechat_read_messages(who: str, max_items: int = 50)`，返回 `ToolResult`（JSON 文本）。**声明 disclaimers**：仅 UIA 界面自动化（无内存读取/hook），微信版本升级可能失效，工具描述注明学习用途与账号风险。

**核心流程（wxauto 验证过的 UIA 路线，自研实现）：**
1. 找微信主窗口：EnumWindows 按 pid 进程名 ∈ {"WeChat.exe", "Weixin.exe"} 或窗口类名 ∈ {"WeChatMainWndForPC"}（3.x）/标题"微信"（4.x）。
2. 切换会话：激活窗口 → 点击/聚焦搜索框（4.x 顶栏 EditControl）→ `set_clipboard_text(who)` + Ctrl+V（中文不过输入法，复用 Task 4 同款 ctypes 实现）→ 等 0.3s → ENTER → 再 ENTER 打开会话。
3. 发消息：聚焦消息输入框（4.x 为 DocumentControl 下的可编辑区域，按 ControlType+IsKeyboardFocusable 探测；3.x 为 EditControl）→ 写剪贴板 + Ctrl+V → 校验输入框 ValuePattern.Value 与 message 相似度 ≥0.9（wxauto SEND_CONTENT_RATIO 同思路，防错发）→ ENTER 发送。
4. 读消息：定位消息列表 ListControl → 收集当前可见 item 的 Name（含 "发送者：内容" 与时间头启发式解析）→ 向上滚动（SGWheel/PageUp）加载更早消息直到 max_items 或两轮无新增 → 返回 `[{"time": "...", "sender": "...", "text": "..."}]`。注意微信 4.x 虚拟化：只有可见区消息在 UIA 树内，必须真滚动。

- [ ] **Step 1: 写失败测试**

```python
"""tests/test_wechat_plugin.py"""
import json


def _load_module():
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "plugins" / "wechat_tool"
    spec = importlib.util.spec_from_file_location("wechat_tool_plugin", root / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, root


def test_plugin_registers_two_tools():
    module, _ = _load_module()
    registered = {}

    class FakeRegistry:
        def register(self, spec):
            registered[spec.name] = spec

    module.register(FakeRegistry())
    assert set(registered) == {"wechat_send_message", "wechat_read_messages"}
    assert registered["wechat_send_message"].requires_permission is True


def test_parse_message_item_heuristics():
    module, _ = _load_module()
    assert module.parse_message_item("张三：你好呀") == {"sender": "张三", "text": "你好呀"}
    assert module.parse_message_item("昨天 22:20") is None       # 时间头
    assert module.parse_message_item("大家好") == {"sender": "", "text": "大家好"}


def test_similarity_check_blocks_wrong_content():
    module, _ = _module_with_helpers()
    assert module.content_matches("大家好，我是星瑶", "大家好，我是星瑶", ratio=0.9)
    assert not module.content_matches("大家好，我是星瑶", "完全不同的内容", ratio=0.9)


def _module_with_helpers():
    return _load_module()
```

（`parse_message_item`/`content_matches` 是 plugin.py 里的纯函数：前者做 "sender：text" 拆分与时间头过滤，后者用 `difflib.SequenceMatcher.ratio()`。）

- [ ] **Step 2: 运行确认失败**

Run: `venv\Scripts\python -m pytest tests/test_wechat_plugin.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`plugins/wechat_tool/plugin.json`：
```json
{
    "name": "wechat_tool",
    "version": "0.1.0",
    "description": "微信桌面客户端 UIA 自动化：按名称给联系人/群发消息、读取会话消息。仅界面自动化（无内存读取/hook）；微信版本升级可能失效，仅供学习使用。",
    "entry": "plugin.py",
    "enabled": false
}
```
（默认 `enabled: false`——有账号风险的外围能力由用户显式开启，符合插件信任机制。）

`plugins/wechat_tool/wechat_ui.py`：COM 线程 UIA 主机（`_ComThread` 模式照抄 `windows_surfaces.py:126-203`）+ 上述四步流程的实现，函数签名：
```python
def send_message(who: str, message: str, *, exact: bool = False) -> dict
def read_messages(who: str, max_items: int, *, exact: bool = False) -> dict
def _find_wechat_window() -> int          # 返回 hwnd；找不到抛 ComputerControlError("wechat_not_running", ...)
def _open_chat(host, who: str, exact: bool) -> None
def _input_similarity(a: str, b: str) -> float
```
剪贴板写入复用 Task 4 的 ctypes 实现思路（插件内自带一份，不跨层导入 core.computer，保持插件自包含）。

`plugins/wechat_tool/plugin.py`：
```python
"""wechat_tool：微信桌面客户端 UIA 自动化插件。

仅做界面自动化（UIA Invoke/Value/剪贴板粘贴），不做内存读取、hook、协议
破解；微信版本升级可能失效。风险声明见 plugin.json 与工具描述。
"""

from __future__ import annotations

import json

from core.plugin_loader import import_sibling
from core.tool_protocol import ToolResult, ToolSpec

_wechat_ui = import_sibling(__file__, "wechat_ui")


def parse_message_item(raw: str):
    """消息条目启发式解析：'发送者：内容' 拆分；时间头返回 None。"""
    import re

    text = (raw or "").strip()
    if not text:
        return None
    # 时间头形态：'昨天 22:20' / '10:05' / '星期四 20:11' / '2026/9/6' 等：
    # 短且整体匹配时间模式
    if re.fullmatch(
        r"(昨天|今天|前天|星期[一二三四五六日天]|\d{1,2}:\d{2}|\d{4}/\d{1,2}/\d{1,2})"
        r"[ ]?(\d{1,2}:\d{2})?",
        text,
    ):
        return None
    if "：" in text:
        sender, _, body = text.partition("：")
        return {"sender": sender.strip(), "text": body.strip()}
    return {"sender": "", "text": text}


def content_matches(expected: str, actual: str, *, ratio: float = 0.9) -> bool:
    import difflib
    if not expected or not actual:
        return False
    return difflib.SequenceMatcher(None, expected, actual).ratio() >= ratio


def _send_message_execute(**kwargs):
    result = _wechat_ui.send_message(
        kwargs["who"], kwargs["message"], exact=bool(kwargs.get("exact"))
    )
    return ToolResult(content=json.dumps(result, ensure_ascii=False))


def _read_messages_execute(**kwargs):
    result = _wechat_ui.read_messages(
        kwargs["who"], int(kwargs.get("max_items") or 50),
        exact=bool(kwargs.get("exact")),
    )
    return ToolResult(content=json.dumps(result, ensure_ascii=False))


def register(registry):
    registry.register(ToolSpec(
        name="wechat_send_message",
        description=(
            "给 Windows 微信桌面客户端的指定联系人/群发送文本消息。参数 who 为会话名称"
            "（群名或联系人备注名），message 为要发送的文本。仅 UIA 界面自动化；"
            "需要微信已登录。仅供学习使用，注意账号风险。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "who": {"type": "string", "description": "会话名称（群名或联系人名）"},
                "message": {"type": "string", "description": "要发送的消息文本"},
                "exact": {"type": "boolean", "description": "是否精确匹配会话名，默认 False"},
            },
            "required": ["who", "message"],
        },
        execute=_send_message_execute,
        read_only=False,
        mutates_files=False,
        requires_permission=True,
        cancellable=True,
        parallel_safe=False,
        activity_description="发送微信消息",
    ))
    registry.register(ToolSpec(
        name="wechat_read_messages",
        description=(
            "读取 Windows 微信桌面客户端指定会话的最近消息（自动向上滚动加载，"
            "最多 max_items 条），返回 [{time, sender, text}]。仅 UIA 界面自动化；"
            "微信 4.x 只有可见区消息存在于界面树，读取靠真实滚动。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "who": {"type": "string", "description": "会话名称（群名或联系人名）"},
                "max_items": {"type": "integer", "description": "最多读取条数，默认 50", "minimum": 1, "maximum": 500},
                "exact": {"type": "boolean", "description": "是否精确匹配会话名，默认 False"},
            },
            "required": ["who"],
        },
        execute=_read_messages_execute,
        read_only=True,
        mutates_files=False,
        requires_permission=False,
        cancellable=True,
        parallel_safe=False,
        activity_description="读取微信消息",
    ))
```

（`parse_message_item` 与 `content_matches` 是 plugin.py 里的纯函数，UI 交互全部隔离在 wechat_ui.py，纯函数可在无微信环境直接单测。）

- [ ] **Step 4: 运行确认通过**

Run: `venv\Scripts\python -m pytest tests/test_wechat_plugin.py -v`
Expected: PASS
（真实微信链路不在 CI 验证；提供 `venv\Scripts\python -m plugins.wechat_tool.wechat_ui --check` 手工自检入口可选实现。）

- [ ] **Step 5: 全量回归 + 提交**

```bash
venv\Scripts\python -m pytest tests -v
venv\Scripts\python -m py_compile plugins/wechat_tool/plugin.py plugins/wechat_tool/wechat_ui.py
git add plugins/wechat_tool tests/test_wechat_plugin.py
git commit -m "feat(plugins): 微信桌面 UIA 自动化插件（发消息/读消息，默认停用）"
```

---

## 验收与效果度量

- 阶段一完成后，用「打开微信→给群发固定消息」场景实测一次：目标往返从 111 步降到 ~70 步以内（sequence 打包点击链 + 无 IME 绕路 + 无守卫误拦）。
- 阶段二完成后同场景复测：微信走 `list_elements` + `click_element`/`set_value_element`，预期 ≤30 步、每步请求 <50KB；对照 ZCode 的 2 分钟基线。
- 阶段三完成后同场景复测：发消息应 ≤5 步；读群消息 1~3 步。
- 截图清除验收：任务结束后 `~/.ycode/ycode_gui.db` 中该会话无任何 `image_url` 块（SQL 检查）；`%TEMP%` 无超过 24h 的 `ycode-computer-*` 目录。

## 明确不做（YAGNI）

- 截图压缩/降分辨率（用户明确保持 1920 PNG）。
- 滚动多帧打包（一次返回 N 张截图）——阶段二的文本观察使滚动读屏场景大概率被 wechat_read_messages 覆盖，如有残留需求再议。
- 剪贴板内容恢复（写入后不还原用户剪贴板，与业界实现一致）。
- 微信内存/数据库/hook 路线（封号与安全风险，只走 UIA 界面自动化）。
