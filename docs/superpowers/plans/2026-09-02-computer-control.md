# Y-CODE Browser and Windows Computer Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Y-CODE 增加默认关闭、设置页显式开启、可随时暂停和紧急停止的 Edge/Chrome 专用浏览器操控及 Windows 完整电脑操控，并让 OpenAI 原生 Computer Use 与显式声明视觉和工具能力的 DeepSeek、Kimi、GLM 等模型使用各自正确协议。

**Architecture:** 使用“共享执行核心 + 双协议适配器”：`core/computer/` 独占设置、能力决策、动作协议、浏览器/Windows backend、单例 controller、预览服务和固定工具适配；generic 模型通过 `computer_action` function tool 与自动 user 多模态观察闭环，OpenAI 原生模型通过 Responses built-in computer tool 与 `previous_response_id` 闭环。GUI、CLI 和 LAN 只调用 controller 公共接口；默认关闭路径不得改变现有 Provider 请求、稳定 prompt、工具 schema 或 golden 字节。

**Tech Stack:** Python 3.12、pytest、Python Playwright（系统 Edge/Chrome channel）、Pillow、Win32 `ctypes`/`SendInput`、现有 Harness/SQLite/LiteLLM、Vue 3、Pinia、TypeScript、Vitest、PyWebView。

**Spec:** `docs/superpowers/specs/2026-09-02-computer-control-design.md`

## Global Constraints

- 开发和验证只在现有 `dev` 分支进行；不创建或切换分支，不覆盖工作树中用户已有修改。
- `browser_control_enabled`、`computer_control_enabled` 默认均为 `false`；`computer_browser` 默认 `edge`，只接受 `edge|chrome`。
- 开关与浏览器选择只在 GUI 系统设置页改变；CLI 不提供永久开关命令。
- 开启前必须通过对应 preflight；失败不得写入 `true`。关闭必须先持久化 `false` 并递增 generation，再停止活动 run。
- 用户开启后 Y-CODE 不逐动作询问权限；OpenAI Provider 强制 safety check 不得绕过。只有当前官方 GA schema/SDK 明确提供确认字段且已有 byte-golden 时才允许“继续”，否则只允许显示状态和停止。
- 浏览器始终使用 `%YCODE_DATA_DIR%/browser-profiles/{edge|chrome}` 的专用持久 profile；不得读取、复制或复用用户日常浏览器 profile。
- Playwright 只启动系统 `msedge` 或 `chrome` channel，窗口可见，viewport 固定 `1440×900`，`device_scale_factor=1`；不得下载或静默改用 Playwright Chromium。同步 Playwright、context 和 page 必须始终由同一个专用 worker 线程持有，所有调用经有界队列 marshal。
- Windows backend 只支持交互式 Windows Default desktop，必须确认 Per-Monitor V2 DPI awareness；锁屏、UAC secure desktop、截图失败或 `SendInput` 拒绝均立即失败。
- 模型能力必须显式配置：generic 要求 `supports_vision=true`、`supports_function_calling=true`、`computer_protocol=generic`；native 要求官方 OpenAI API、Responses 协议和 `computer_protocol=native_openai`。禁止按模型名猜测。
- LAN 发起的 Turn 永远不获得电脑操控能力；LAN 只能查看状态和紧急停止，不能开启、暂停、恢复或启动。
- 一个进程只有一个 `ComputerControlService` 活动租约；一个 run 固定一个 target；generic assistant Step 只允许一个电脑动作且不得与其他工具混用。OpenAI GA native 的 `actions[]` 按 Task 11 的可中断串行规则处理。
- 全局停止键固定为 `Ctrl+Alt+Esc`；任一开关开启期间持续注册，两个都关闭或进程退出才注销。注册失败时两个能力都不可用。停止必须释放所有按下的键和鼠标键。
- 截图失败不得返回旧截图；浏览器失败不得改走桌面；native 解析失败不得改走 generic；非法配置不得自动纠正。
- `computer_action` 是固定 Harness 保留工具，不进入普通 `ToolRegistry`，插件/MCP 同名必须明确拒绝。
- 默认关闭时以下测试不得修改任何 expected/hash：`tests/test_prompt_byte_stability.py`、`tests/test_provider_payload_golden.py`、DeepSeek Prompt Ledger、Kimi/GLM 缓存/推理参数、token 统计。
- `messages` 仍是正文唯一事实源；Harness Journal 只保存 ID、序号、时间、耗时、状态和哈希，不保存截图、输入文字、URL、Provider response id 或 safety 原文。
- `evaluate_js` 仍只允许 `gui-js-dispatcher` 单消费者线程调用；backend/hotkey 线程只入现有事件队列。
- 每个 Task 必须真实执行 RED → GREEN → 相关回归 → `git diff --check` → `git status --short`；不得先写实现后补测试。新建未跟踪文件还必须执行本计划“新增文件空白检查”，不能把 `git diff --check` 当成对未跟踪文件的覆盖。
- 本计划不授权 K3 执行 `git commit`、push、merge、发布或修改现有 golden；每个 Task 末尾只给出建议提交信息。

## File Map

### 新建文件

- `core/computer/__init__.py`：包级公共导出，不创建全局副作用。
- `core/computer/models.py`：枚举、错误码、动作、观察、能力、run 快照和 backend Protocol。
- `core/computer/actions.py`：严格动作 schema、解析和 Provider 投影。
- `core/computer/settings.py`：三个配置键、严格读取/保存、浏览器探测、profile 路径和 generation。
- `core/computer/capability.py`：Turn 起点的显式能力决策。
- `core/computer/hotkey.py`：可注入的 `Ctrl+Alt+Esc` 注册与注销。
- `core/computer/preview_server.py`：loopback 令牌预览服务与最近三图清理。
- `core/computer/browser_backend.py`：专用 worker 线程独占 Playwright persistent Edge/Chrome backend。
- `core/computer/win32_adapter.py`：DPI、desktop 检查、`SendInput` 和 foreground 摘要。
- `core/computer/windows_backend.py`：Pillow 截图、缩放和物理坐标映射。
- `core/computer/controller.py`：单租约状态机、串行动作、暂停/恢复/停止、事件与无进展判断。
- `core/computer/tool_adapter.py`：保留工具识别、generic schema、`ToolResult.followup_messages`。
- `core/computer/provider_generic.py`：generic 动态提示、工具注入和多模态观察。
- `core/computer/provider_openai.py`：原生 Responses 工具、事件、continuation 与脱敏投影。
- `core/computer/cli.py`：`/computer status|pause|resume|stop` 解析和无 Markdown 渲染。
- `frontend/src/components/ComputerControlBar.vue`：顶部安全条。
- `frontend/src/components/ComputerControlPane.vue`：截图、轨迹和控制按钮。
- `scripts/smoke_computer_control.py`：显式参数、人工确认后才执行真实输入的烟测程序。
- `tests/test_computer_models.py`
- `tests/test_computer_settings.py`
- `tests/test_computer_capability.py`
- `tests/test_computer_hotkey.py`
- `tests/test_computer_preview_server.py`
- `tests/test_computer_browser_backend.py`
- `tests/test_computer_windows_backend.py`
- `tests/test_computer_controller.py`
- `tests/test_computer_tool_adapter.py`
- `tests/test_computer_provider_generic.py`
- `tests/test_computer_provider_openai.py`
- `tests/test_computer_cli.py`
- `tests/test_computer_privacy.py`
- `tests/computer_fakes.py`：所有电脑控制测试共用的 fake hotkey、Playwright、Win32、截图和 Provider fixture；测试正文不得引用未定义 helper。
- `frontend/src/components/ComputerControlBar.test.ts`
- `frontend/src/components/ComputerControlPane.test.ts`

### 修改文件

- `requirements.txt`、`ycode.spec`：Playwright Python 包和系统浏览器 channel 的打包声明；不打包 Chromium。
- `config.py`：只复用现有原子配置写入，不增加第二配置存储。
- `core/model_catalog.py`、`core/model_manager.py`、`core/model_selector.py`：新增显式 function calling/computer protocol 元数据。
- `core/context_builder.py`、`core/agent_service.py`：仅 capability 开启时追加动态电脑说明。
- `core/tool_protocol.py`：给 `ToolResult` 增加默认空、不可变 `followup_messages`。
- `core/tool_registry.py`：拒绝插件/MCP 占用固定保留名，但默认 schema 不增加电脑工具。
- `core/harness/tool_pipeline.py`、`core/harness/request_envelope.py`、`core/harness/stream_assembler.py`：follow-up 消息、受控 built-in computer、原生事件。
- `core/llm.py`：Turn 冻结 capability、双协议接缝、continuation、取消和持久化投影。
- `core/command_shell.py`：扩展 system settings 聚合快照。
- `main.py`：沿用现有斜杠命令分派、帮助表、CLI 状态显示和 prompt_toolkit 主线程事件入口；不得创建平行 command handler。
- `core/memory.py`：让媒体检测/折叠同时支持 content 列表与带 `_computer_v1` 的完整消息 dict，并保持折叠幂等。
- `gui_bridge.py`、`gui_main.py`：严格 setters、controller API、生命周期与小型事件转发。
- `core/lan_server.py`：RPC 禁止列表和 `turn_source=lan` 后端标记。
- `frontend/src/types/pywebview.d.ts`、`frontend/src/stores/appStore.ts`：设置、run、trace 类型和 reducer。
- `frontend/src/components/SystemSettings.vue`、`frontend/src/components/SettingsPage.vue`：电脑操控设置和模型能力字段。
- `frontend/src/components/RightPanel.vue`、`frontend/src/App.vue`：动态操控页签与顶部安全条。
- 相关现有 Python/Vitest 测试：只增加行为覆盖，不改写 golden expected。

## Cross-Task Interface Contract

后续 Task 必须沿用以下名字，不得产生平行实现：

```python
resolve_computer_capability(model: dict, provider: dict, settings: ComputerSettingsSnapshot, turn_source: str) -> ComputerCapability
parse_computer_action(payload: Mapping[str, object]) -> ComputerAction
ComputerControlService.start_run(owner: ComputerOwner, capability: ComputerCapability, target: ComputerTarget | None = None) -> ComputerRunSnapshot
ComputerControlService.execute(run_id: str, owner: ComputerOwner, action: ComputerAction) -> ComputerObservation
ComputerControlService.complete_run(run_id: str, owner: ComputerOwner, retain_browser_session: bool = True) -> ComputerRunSnapshot
ComputerControlService.pause(run_id: str, reason: str) -> ComputerRunSnapshot
ComputerControlService.resume(run_id: str) -> ComputerRunSnapshot
ComputerControlService.stop(run_id: str, reason: str) -> ComputerRunSnapshot
ComputerControlService.snapshot() -> ComputerRunSnapshot
get_computer_control_service() -> ComputerControlService
```

所有事件使用小 JSON，并含 run 内严格递增 `event_seq`；`action_ordinal` 只表示动作顺序。`computer_observation` 只带 tokenized URL 和摘要。所有时间只存在 UI snapshot/event，不进入 Provider 投影。

### 共享测试 fixture 契约

`tests/computer_fakes.py` 必须在首次引用前创建，并由各测试显式 import。它统一导出 `FakeHotkeyApi`、`FakePlaywrightRuntime`、`FakeWin32Adapter`、`FakeComputerBackend`、`FakeController`、`fake_png_bytes(seed)`、`http_status(url)`、`use_temp_config(tmp_path, monkeypatch, values)`、`capture_responses_request(...)` 和 `read_text_surfaces(...)`。fixture 只能实现对应 Protocol 的最小确定性行为，禁止访问真实键鼠、真实浏览器、网络 Provider 或用户配置。后文旧式 `_png_bytes`、`_http_status`、`_use_config` 名称一律视为上述公开 helper 的笔误并在实现时不得出现；测试收集阶段必须证明没有 `NameError` 或缺失 fixture。

---

### Task 1: 冻结基线并验证 Playwright 系统 channel 前提

**Files:**
- Modify: `requirements.txt`
- Create: `tests/test_computer_dependency.py`
- Create: `scripts/smoke_computer_control.py`
- Test: `tests/test_prompt_byte_stability.py`
- Test: `tests/test_provider_payload_golden.py`

**Interfaces:**
- Produces: 可导入的 `playwright.sync_api`
- Produces: `scripts/smoke_computer_control.py --probe-browser edge|chrome`
- Does not produce: 浏览器下载、真实键鼠输入或默认工具 schema 变化

- [ ] **Step 1: 记录干净基线与用户已有变更**

Run:

```powershell
git branch --show-current
git status --short
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: branch 为 `dev`；现有用户修改被记录但不清理；所有门禁退出码为 0。若基线失败，停止实施并用 `superpowers:systematic-debugging` 定位，不能把既有失败算作本功能 RED。

- [ ] **Step 2: 写依赖与探测失败测试**

创建 `tests/test_computer_dependency.py`：

```python
from pathlib import Path


def test_playwright_is_declared_without_browser_download_hook():
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "playwright>=" in requirements
    assert "playwright install" not in requirements


def test_playwright_python_api_is_importable():
    from playwright.sync_api import sync_playwright

    assert callable(sync_playwright)
```

- [ ] **Step 3: 运行并确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py -q`

Expected: 至少因 `requirements.txt` 尚未声明 Playwright 或模块未安装而失败。

- [ ] **Step 4: 声明并安装 Python 依赖，不下载浏览器**

在 `requirements.txt` 增加一行：

```text
playwright>=1.55,<2
```

Run: `venv\Scripts\python.exe -m pip install "playwright>=1.55,<2"`

禁止运行 `playwright install`。若安装需要网络授权，按运行环境正常申请；不得改用 Selenium 或 CDP 临时代替。

- [ ] **Step 5: 写只读系统 channel 探针**

`scripts/smoke_computer_control.py` 的探测入口必须要求显式参数并只打开 `about:blank`：

```python
def probe_browser(browser: str) -> int:
    from playwright.sync_api import sync_playwright

    channel = {"edge": "msedge", "chrome": "chrome"}[browser]
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch(channel=channel, headless=False)
        try:
            page = instance.new_page(viewport={"width": 1440, "height": 900})
            page.goto("about:blank")
            assert page.viewport_size == {"width": 1440, "height": 900}
        finally:
            instance.close()
    return 0
```

`argparse` 只接受 `--probe-browser edge|chrome` 或后续 Task 增加的显式烟测模式；无参数时打印帮助并退出 2。

- [ ] **Step 6: 确认 GREEN 与默认字节不变**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
git diff --check -- requirements.txt tests/test_computer_dependency.py scripts/smoke_computer_control.py
```

Expected: 全部通过；golden 文件本身无 diff。系统 channel GUI 探针只在用户明确允许弹出浏览器时人工运行，不计入自动 pytest。

Suggested commit after explicit authorization: `build: declare system-browser Playwright dependency`

---

### Task 2: 建立严格领域模型和动作解析

**Files:**
- Create: `core/computer/__init__.py`
- Create: `core/computer/models.py`
- Create: `core/computer/actions.py`
- Create: `tests/test_computer_models.py`

**Interfaces:**
- Produces: `ComputerProtocol`, `ComputerTarget`, `BrowserKind`, `ComputerRunStatus`, `ComputerActionKind`
- Produces: `ComputerControlError(code: str, message: str)`
- Produces: `ComputerOwner`, `ComputerAction`, `ComputerObservation`, `ComputerCapability`, `ComputerRunSnapshot`
- Produces: `ComputerBackend` Protocol
- Produces: `COMPUTER_ACTION_TOOL_SCHEMA`, `parse_computer_action()`, `observation_provider_projection()`

- [ ] **Step 1: 写严格解析失败测试**

创建覆盖合法动作、未知字段、`bool` 坐标、字段互斥、8000 字符、100–10000 ms、最多 8 键、URL scheme 和 browser-only 导航的参数化测试：

```python
import pytest

from core.computer.actions import parse_computer_action
from core.computer.models import ComputerControlError


def test_parse_click_requires_strict_integer_coordinates():
    action = parse_computer_action({"kind": "click", "target": "desktop", "x": 12, "y": 34})
    assert (action.kind, action.target, action.x, action.y) == ("click", "desktop", 12, 34)


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": True, "y": 2},
    {"kind": "observe", "target": "browser", "x": 1},
    {"kind": "wait", "target": "desktop", "duration_ms": 99},
    {"kind": "keypress", "target": "desktop", "keys": ["CTRL"] * 9},
    {"kind": "navigate", "target": "desktop", "url": "https://example.com"},
    {"kind": "navigate", "target": "browser", "url": "file:///c:/secret"},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "mystery": 3},
])
def test_parse_action_rejects_invalid_payload(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q`

Expected: collection 因 `core.computer` 不存在而失败。

- [ ] **Step 3: 实现不可变值对象和 backend Protocol**

`models.py` 使用 `str, Enum` 和 `@dataclass(frozen=True)`；核心形状固定为：

```python
@dataclass(frozen=True)
class ComputerOwner:
    session_id: str
    turn_id: str
    session_label: str
    cancel_event: threading.Event = field(compare=False, repr=False)


@dataclass(frozen=True)
class ComputerAction:
    kind: str
    target: str
    x: int | None = None
    y: int | None = None
    end_x: int | None = None
    end_y: int | None = None
    button: str = "left"
    delta_x: int = 0
    delta_y: int = 0
    text: str | None = field(default=None, repr=False)
    keys: tuple[str, ...] = ()
    url: str | None = None
    duration_ms: int | None = None


class ComputerBackend(Protocol):
    target: str
    def start(self) -> None: ...
    def observe(self, run_id: str) -> "ComputerObservation": ...
    def execute(self, run_id: str, action: ComputerAction) -> None: ...
    def release_inputs(self) -> None: ...
    def close(self) -> None: ...
```

`ComputerControlError.__str__` 返回中文 message，`code` 保持设计规格的稳定英文错误码。快照和观察的 `repr` 不得暴露 `text` 或本地截图绝对路径。

- [ ] **Step 4: 实现白名单动作解析**

`actions.py` 必须使用每种动作的允许字段集合，不得通过删除未知字段继续：

```python
_COMMON = {"kind", "target"}
_ALLOWED = {
    "observe": _COMMON,
    "click": _COMMON | {"x", "y", "button"},
    "double_click": _COMMON | {"x", "y", "button"},
    "move": _COMMON | {"x", "y"},
    "drag": _COMMON | {"x", "y", "end_x", "end_y", "button"},
    "scroll": _COMMON | {"x", "y", "delta_x", "delta_y"},
    "type_text": _COMMON | {"text"},
    "keypress": _COMMON | {"keys"},
    "wait": _COMMON | {"duration_ms"},
    "navigate": _COMMON | {"url"},
    "back": _COMMON,
    "forward": _COMMON,
    "reload": _COMMON,
}
```

实现 `_strict_int()` 显式拒绝 `bool`；`urllib.parse.urlsplit` 只允许 `http|https` 且必须有 hostname；组合键只允许 `CTRL, ALT, SHIFT, META, ENTER, TAB, ESC, SPACE, BACKSPACE, DELETE, HOME, END, PAGEUP, PAGEDOWN, UP, DOWN, LEFT, RIGHT, A-Z, 0-9, F1-F12`。

- [ ] **Step 5: GREEN、序列化与隐私回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q
venv\Scripts\python.exe -m py_compile core/computer/models.py core/computer/actions.py
git diff --check -- core/computer tests/test_computer_models.py
```

Expected: 所有输入边界通过；Provider 投影不含 `png_path`、时间戳、完整 URL query、输入正文。

Suggested commit after explicit authorization: `feat(computer): add strict action domain`

---

### Task 3: 设置、浏览器探测、profile 路径与 generation

**Files:**
- Create: `core/computer/settings.py`
- Create: `tests/test_computer_settings.py`
- Modify: `core/command_shell.py`
- Modify: `tests/test_gui_system_settings.py`

**Interfaces:**
- Consumes: `BrowserKind`, `ComputerControlError`
- Produces: `ComputerSettingsSnapshot`
- Produces: `load_computer_settings() -> ComputerSettingsSnapshot`
- Produces: `set_browser_control_enabled(value: object, preflight: Callable) -> ComputerSettingsSnapshot`
- Produces: `set_computer_control_enabled(value: object, preflight: Callable) -> ComputerSettingsSnapshot`
- Produces: `set_computer_browser(value: object, active_run: bool) -> ComputerSettingsSnapshot`
- Produces: `list_browser_options() -> tuple[BrowserOption, ...]`
- Produces: `get_browser_profile_dir(browser: str) -> Path`
- Produces: `get_settings_generation() -> int`

- [ ] **Step 1: 写默认值、损坏配置、preflight 和关闭顺序测试**

```python
def test_missing_computer_settings_are_safe_defaults(tmp_path, monkeypatch):
    _use_config(tmp_path, monkeypatch, {})
    snapshot = load_computer_settings()
    assert snapshot.browser_control_enabled is False
    assert snapshot.computer_control_enabled is False
    assert snapshot.computer_browser == "edge"


@pytest.mark.parametrize("key,value", [
    ("browser_control_enabled", 1),
    ("computer_control_enabled", "false"),
    ("computer_browser", "chromium"),
])
def test_explicit_invalid_config_is_rejected(tmp_path, monkeypatch, key, value):
    _use_config(tmp_path, monkeypatch, {key: value})
    with pytest.raises(ComputerControlError):
        load_computer_settings()


def test_enable_does_not_persist_when_preflight_fails(tmp_path, monkeypatch):
    path = use_temp_config(tmp_path, monkeypatch, {})
    with pytest.raises(ComputerControlError, match="热键"):
        set_browser_control_enabled(True, preflight=lambda: (_ for _ in ()).throw(RuntimeError("热键不可用")))
    assert "browser_control_enabled" not in json.loads(path.read_text(encoding="utf-8"))


def test_disable_persists_and_increments_generation_before_stop(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {"computer_control_enabled": True})
    before = get_settings_generation()
    snapshot = set_computer_control_enabled(False, preflight=lambda: None)
    assert snapshot.computer_control_enabled is False
    assert get_settings_generation() == before + 1
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py -q`

Expected: 新模块或新增快照字段不存在。

- [ ] **Step 3: 实现严格设置与显式浏览器发现**

固定常量：

```python
BROWSER_CONTROL_CONFIG_KEY = "browser_control_enabled"
COMPUTER_CONTROL_CONFIG_KEY = "computer_control_enabled"
COMPUTER_BROWSER_CONFIG_KEY = "computer_browser"
DEFAULT_COMPUTER_BROWSER = "edge"
```

使用 `shutil.which()` 和 Windows 明确安装路径探测 `msedge.exe`/`chrome.exe`，返回每个选项自己的 `available/executable/unavailable_reason`；所选浏览器不可用时禁止切换到另一个。profile 路径通过 `Path(config.YCODE_DIR) / "browser-profiles" / browser` 计算并 `resolve()` 后确认仍在 `browser-profiles` 下。

布尔读取逻辑必须精确：

```python
def _read_bool(data: dict, key: str) -> bool:
    if key not in data:
        return False
    value = data[key]
    if type(value) is not bool:
        raise ComputerControlError("computer_invalid_config", f"{key} 必须是布尔值")
    return value
```

三个 setter 使用现有 `config.save_config_value()`；任何返回 `False` 都抛 `computer_invalid_config`。活动 run 时修改 browser 抛 `computer_session_busy`。

- [ ] **Step 4: 扩展统一系统设置快照**

`core.command_shell.get_system_settings()` 合并：

```python
computer = load_computer_settings()
hotkey = get_emergency_hotkey_snapshot()  # core.computer.hotkey，惰性导入
run = get_computer_control_service().snapshot()  # core.computer.controller，惰性导入
return {
    # 保留全部现有键和值
    "browser_control_enabled": computer.browser_control_enabled,
    "computer_control_enabled": computer.computer_control_enabled,
    "computer_browser": computer.computer_browser,
    "computer_browser_options": [option.to_dict() for option in computer.browser_options],
    "computer_profile_path": computer.profile_display_path,
    "computer_platform_supported": computer.platform_supported,
    "computer_hotkey_available": hotkey.available,
    "computer_hotkey": "Ctrl+Alt+Esc",
    "computer_run": run.to_public_dict(),
}
```

`ComputerSettingsSnapshot` 本身不得包含 `hotkey_available` 或 `run_summary`，`settings.py` 不导入 controller/hotkey。不要把 browser profile 文件列表或真实 Cookie 状态放进快照。

- [ ] **Step 5: GREEN 与现有设置回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py tests/test_config.py tests/test_config_toml_overlay.py -q
git diff --check -- core/computer/settings.py core/command_shell.py tests/test_computer_settings.py tests/test_gui_system_settings.py
```

Expected: 缺失键安全关闭，显式损坏值失败，preflight 失败不落盘，关闭 generation 先变化。

Suggested commit after explicit authorization: `feat(computer): add strict control settings`

---

### Task 4: 显式模型能力元数据与 Turn 能力决策

**Files:**
- Create: `core/computer/capability.py`
- Create: `tests/test_computer_capability.py`
- Modify: `core/model_catalog.py`
- Modify: `core/model_manager.py`
- Modify: `core/model_selector.py`
- Modify: `tests/test_model_catalog.py`
- Modify: `tests/test_model_selector.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Consumes: `ComputerSettingsSnapshot`
- Produces: `resolve_computer_capability(model, provider, settings, turn_source) -> ComputerCapability`
- Produces model fields: `supports_function_calling: bool`, `computer_protocol: "none"|"generic"|"native_openai"`

- [ ] **Step 1: 写能力矩阵失败测试**

```python
@pytest.mark.parametrize("model", [
    {"supports_vision": True, "supports_function_calling": False, "computer_protocol": "generic"},
    {"supports_vision": False, "supports_function_calling": True, "computer_protocol": "generic"},
    {"supports_vision": True, "supports_function_calling": True},
])
def test_generic_requires_three_explicit_capability_fields(model, enabled_settings):
    capability = resolve_computer_capability(model, _openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


def test_generic_capable_model_receives_enabled_targets(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, _openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is True
    assert capability.targets == ("browser", "desktop")


def test_lan_turn_is_always_disabled(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    assert resolve_computer_capability(model, {}, enabled_settings, "lan").error_code == "computer_remote_forbidden"


def test_native_requires_official_openai_responses(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "native_openai"}
    provider = {"protocol": "openai", "base_url": "https://proxy.example/v1"}
    capability = resolve_computer_capability(model, provider, enabled_settings, "local")
    assert capability.error_code == "computer_protocol_mismatch"
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py -q`

Expected: resolver 和新配置字段不存在。

- [ ] **Step 3: 实现纯函数 resolver**

resolver 不读全局 config、不做网络探测、不按模型名称分支。官方 OpenAI base 只允许规范化 host `api.openai.com`，协议必须是项目现有 Responses 标识；目标按两个设置开关组成固定 tuple。返回对象至少含：

```python
ComputerCapability(
    enabled=True,
    protocol="generic",
    targets=("browser", "desktop"),
    browser="edge",
    settings_generation=settings.generation,
    error_code="",
    reason="",
)
```

未开启任何 target 返回 `computer_disabled`；非 Windows 返回 `computer_unsupported_os`；LAN 返回 `computer_remote_forbidden`。

- [ ] **Step 4: 严格持久化与预置元数据**

`model_manager` 的 add/update/export/import 路径都显式保存两个新字段。规则：`type(value) is bool`；protocol 必须在固定集合内；`generic` 不满足视觉与工具能力时保存即拒绝，不能保存一个运行时才失败的半有效组合。

`model_catalog.py` 只给已确认条目写字面量元数据，例如：

```python
{"name": "k3", "context_window": 1000000, "supports_vision": True,
 "supports_function_calling": True, "computer_protocol": "generic"}
```

对 OpenAI native 型号，只依据实施当时官方 API 文档确认的准确型号和 Responses 支持写入；没有证据的预置保持 `computer_protocol="none"`。不得用 family name 批量推断。

截至 2026-09-02，官方模型目录/单模型页已明确列出 Computer use 的首批 GA 预置至少包括 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.4`、`gpt-5.4-mini`；这些条目可显式标记 `native_openai`。`computer-use-preview` 是仅限 Responses 的旧 preview 模型，但官方新实现指南要求使用 GA `computer` tool，因此本计划不实现 preview 请求变体，也不把该旧模型标为首版可用。实施当天仍需逐个打开官方模型页复核，不能从此清单推断其他型号。官方来源：<https://developers.openai.com/api/docs/models>、<https://developers.openai.com/api/docs/models/gpt-5.4>、<https://developers.openai.com/api/docs/models/gpt-5.4-mini>、<https://developers.openai.com/api/docs/guides/tools-computer-use>。

- [ ] **Step 5: GREEN 与旧配置兼容**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py tests/test_config.py tests/test_gui_model_settings.py -q
git diff --check -- core/computer/capability.py core/model_catalog.py core/model_manager.py core/model_selector.py tests
```

Expected: 旧模型缺字段仍可普通对话和普通工具，但 computer capability 明确 disabled；无 substring 猜测。

Suggested commit after explicit authorization: `feat(models): declare explicit computer capabilities`

---

### Task 5: 全局紧急停止热键与令牌化预览服务

**Files:**
- Create: `core/computer/hotkey.py`
- Create: `core/computer/preview_server.py`
- Create: `tests/test_computer_hotkey.py`
- Create: `tests/test_computer_preview_server.py`

**Interfaces:**
- Produces: `EmergencyStopHotkey(api: HotkeyApi, on_stop: Callable[[], None])` with `start()`, `close()`, `available`
- Produces: `ComputerPreviewServer(root: Path)` with `start() -> str`, `publish(run_id: str, png: bytes) -> str`, `stop() -> None`
- Produces invariant: only `127.0.0.1`, random per-process token, newest three PNG per current run

- [ ] **Step 1: 写热键和预览边界失败测试**

热键用 fake Win32 API，不得在 pytest 注册真实系统热键：

```python
def test_hotkey_registration_failure_is_not_reported_available():
    api = FakeHotkeyApi(register_result=False)
    hotkey = EmergencyStopHotkey(api, on_stop=lambda: None)
    with pytest.raises(ComputerControlError) as raised:
        hotkey.start()
    assert raised.value.code == "computer_hotkey_unavailable"
    assert hotkey.available is False


def test_hotkey_callback_only_requests_controller_stop():
    requested = []
    hotkey = EmergencyStopHotkey(FakeHotkeyApi(), on_stop=lambda: requested.append("stop"))
    hotkey._dispatch_for_test("Ctrl+Alt+Esc")
    assert requested == ["stop"]
```

预览服务测试：

```python
def test_preview_server_requires_token_and_keeps_only_three(tmp_path):
    server = ComputerPreviewServer(tmp_path)
    base = server.start()
    try:
        urls = [server.publish("run-a", fake_png_bytes(i)) for i in range(4)]
        assert http_status(urls[-1]) == 200
        assert http_status(urls[-1].replace(server.token, "wrong")) == 404
        assert http_status(urls[0]) == 404
        assert len(list(tmp_path.rglob("*.png"))) == 3
    finally:
        server.stop()
    assert not list(tmp_path.rglob("*.png"))
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q`

Expected: 两个模块不存在。

- [ ] **Step 3: 实现热键生命周期**

Windows 实现使用独立消息线程注册 `MOD_CONTROL|MOD_ALT` + `VK_ESCAPE`，只在收到对应 `WM_HOTKEY` 后调用 `on_stop`。任一设置开关为 true 时由应用生命周期持续持有该实例；两个开关均为 false 或应用退出时才 `close()`。应用启动读到已开启配置时立即注册，失败则 capability preflight 返回 `computer_hotkey_unavailable`，但不静默改写配置。`close()` 发送退出消息并 `UnregisterHotKey`，允许重复调用。回调只发 stop 请求，不直接碰 GUI、prompt_toolkit 或 Provider 流。

必须提供可注入 API；默认 API 只在 `sys.platform == "win32"` 构造，其他平台抛 `computer_unsupported_os`。

- [ ] **Step 4: 实现 preview HTTP 服务**

使用标准库 `ThreadingHTTPServer`：

```python
self.token = secrets.token_urlsafe(32)
self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
```

请求路径格式固定为 `/computer-preview/{token}/{run_id}/{ordinal}.png`。`run_id` 只允许 `^[a-zA-Z0-9_-]{1,80}$`，文件名由服务内部生成；handler 不接受 `..`、反斜线、URL 编码路径穿越或目录索引。响应设置 `Cache-Control: no-store` 和 `Content-Type: image/png`。publish 使用临时文件 + `os.replace()`，切换 run 立即删除旧 run 图片；启动时删除 24 小时前目录，停止时删除本进程目录。

- [ ] **Step 5: GREEN 与资源释放回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q
venv\Scripts\python.exe -m py_compile core/computer/hotkey.py core/computer/preview_server.py
git diff --check -- core/computer/hotkey.py core/computer/preview_server.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py
```

Expected: 端口在 stop 后可释放；错误 token、旧 run、第四旧图和路径穿越均为 404。

Suggested commit after explicit authorization: `feat(computer): add emergency stop and preview server`

---

### Task 6: Edge/Chrome 专用 Playwright backend

**Files:**
- Create: `core/computer/browser_backend.py`
- Create: `tests/test_computer_browser_backend.py`
- Modify: `scripts/smoke_computer_control.py`

**Interfaces:**
- Consumes: `ComputerAction`, `ComputerObservation`, `get_browser_profile_dir()`
- Produces: `BrowserComputerBackend(browser: str, profile_dir: Path, playwright_factory=sync_playwright)`，内部专用 worker 线程和有界命令队列
- Produces: active page lease and browser-only action execution

- [ ] **Step 1: 写 fake Playwright 合约测试**

fake 需要记录 launch 参数和 page 调用，不启动真实浏览器：

```python
def test_browser_backend_uses_visible_persistent_system_channel(tmp_path):
    runtime = FakePlaywrightRuntime()
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    backend.start()
    assert runtime.chromium.launch_persistent_context_calls == [{
        "user_data_dir": str((tmp_path / "edge").resolve()),
        "channel": "msedge",
        "headless": False,
        "viewport": {"width": 1440, "height": 900},
        "device_scale_factor": 1,
    }]


def test_selected_browser_failure_does_not_fallback(tmp_path):
    runtime = FakePlaywrightRuntime(error=RuntimeError("msedge missing"))
    backend = BrowserComputerBackend("edge", tmp_path / "edge", playwright_factory=lambda: runtime)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_browser_unavailable"
    assert runtime.requested_channels == ["msedge"]


def test_closed_active_page_fails_instead_of_reopening(backend):
    backend.active_page.closed = True
    with pytest.raises(ComputerControlError) as raised:
        backend.observe("run-a")
    assert raised.value.code == "computer_browser_closed"
```

另测所有 fake Playwright 调用的 thread id 与创建 runtime 的 thread id 完全相同、并发调用仍由 worker 串行、worker 启动失败/命令超时明确失败、零 page 时只创建一个 `about:blank`、popup 成为 active page、PNG bytes/viewport、navigate 只接受已校验 URL、back/forward/reload、坐标边界和 `close()` 幂等。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py -q`

Expected: backend 模块不存在。

- [ ] **Step 3: 实现懒加载、profile 锁与页面生命周期**

构造函数不得导入或启动 Playwright；`start()` 创建专用 worker 线程，worker 内创建 profile、获取进程内 profile 锁并调用 `sync_playwright().start()` 与 `launch_persistent_context`。外部 start/observe/execute/complete/close 全部向有界队列提交命令，只有 worker 接触 runtime/context/page。browser/channel 映射固定：

```python
_CHANNELS = {"edge": "msedge", "chrome": "chrome"}
```

context 的 `page` 事件也在 worker 内处理，把最后获得焦点的可见 page 设为 active；初始 pages 为空时只创建一个 `about:blank`。context/page close 立即把 backend 标记 closed，后续动作抛 `computer_browser_closed`。profile 锁冲突抛 `computer_profile_locked`，不杀其他进程。任何调用线程都不得直接重建或关闭 Playwright 对象。

- [ ] **Step 4: 实现动作与稳定观察**

浏览器坐标只落在 `0 <= x < 1440`、`0 <= y < 900`。动作映射：`mouse.click/dblclick/move/down/up/wheel`、`keyboard.insert_text`、`keyboard.press`、`page.goto/go_back/go_forward/reload`。拖拽用 `try/finally` 确保 mouse up。

观察调用 `page.screenshot(type="png")`，SHA-256 对 PNG bytes；Provider 只见 `urlsplit(page.url).scheme + "://" + netloc` 与截断 200 字符 title，不见 path/query/fragment。不得读取 DOM 输入值、Cookie、localStorage 或密码库。

- [ ] **Step 5: GREEN 与受控人工探针**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py tests/test_computer_models.py -q
git diff --check -- core/computer/browser_backend.py tests/test_computer_browser_backend.py scripts/smoke_computer_control.py
```

自动测试全绿后，人工探针只允许：

```powershell
venv\Scripts\python.exe scripts/smoke_computer_control.py --probe-browser edge
venv\Scripts\python.exe scripts/smoke_computer_control.py --probe-browser chrome
```

仅对已安装浏览器执行；不存在的选项应明确报告 unavailable，不构成本轮 fallback。

Suggested commit after explicit authorization: `feat(computer): add dedicated browser backend`

---

### Task 7: Windows 截图、DPI、坐标映射与 SendInput backend

**Files:**
- Create: `core/computer/win32_adapter.py`
- Create: `core/computer/windows_backend.py`
- Create: `tests/test_computer_windows_backend.py`
- Modify: `scripts/smoke_computer_control.py`

**Interfaces:**
- Consumes: `ComputerAction`, `ComputerObservation`
- Produces: `Win32InputAdapter` with `preflight()`, `send_mouse()`, `send_text()`, `send_keys()`, `release_all()`
- Produces: `WindowsComputerBackend(image_grab, win32_adapter)`
- Produces: exact `screenshot_to_physical(x, y, observation) -> tuple[int, int]`

- [ ] **Step 1: 写纯 fake 的截图和输入测试**

```python
def test_virtual_desktop_is_scaled_to_1920_and_coordinates_round_trip():
    image = FakeImage(width=3840, height=2160, origin_x=-1920, origin_y=0)
    adapter = FakeWin32Adapter()
    backend = WindowsComputerBackend(image_grab=lambda: image, win32_adapter=adapter)
    observation = backend.observe("run-a")
    assert (observation.width, observation.height) == (1920, 1080)
    assert (observation.origin_x, observation.origin_y) == (-1920, 0)
    backend.execute("run-a", ComputerAction(kind="click", target="desktop", x=960, y=540))
    assert adapter.clicks == [(0, 1080, "left")]


def test_sendinput_partial_result_is_error():
    adapter = Win32InputAdapter(send_input=lambda events: len(events) - 1)
    with pytest.raises(ComputerControlError) as raised:
        adapter.send_text("秘密")
    assert raised.value.code == "computer_input_rejected"


def test_pressed_keys_are_released_when_keypress_fails():
    adapter = FakeWin32Adapter(fail_after=2)
    with pytest.raises(ComputerControlError):
        adapter.send_keys(("CTRL", "SHIFT", "A"))
    assert adapter.keys_down == set()
```

另测负坐标、多屏边界、`bool` 不透传、secure desktop、无 foreground、截图异常、wheel/hwheel、Unicode、double click 和 drag finally release。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_windows_backend.py -q`

Expected: Windows 模块不存在。

- [ ] **Step 3: 实现 Win32 adapter 的 fail-closed preflight**

`preflight()` 顺序固定：平台为 win32；设置/确认 Per-Monitor V2；input desktop 名称为 `Default`；存在 foreground window；热键已可注册。任何失败映射到稳定错误码，不请求管理员、不重启应用。

`SendInput` 返回值必须等于事件数量；组合键和 drag 在 `finally` 中释放已按下状态。鼠标绝对坐标使用虚拟桌面 metrics 和 `MOUSEEVENTF_VIRTUALDESK|MOUSEEVENTF_ABSOLUTE`，Unicode 使用 `KEYEVENTF_UNICODE`。

- [ ] **Step 4: 实现截图和坐标换算**

使用 `ImageGrab.grab(all_screens=True)`；测试通过构造注入替代。最长边大于 1920 时按同一比例缩小，PNG 使用确定参数编码。坐标换算只依赖最新 observation：

```python
physical_x = origin_x + round(x * original_width / model_width)
physical_y = origin_y + round(y * original_height / model_height)
```

换算后再次检查虚拟桌面半开区间边界。动作开始前重新检查 interactive desktop；截图失败抛 `computer_backend_start_failed` 或 `computer_secure_desktop`，绝不复用 `_last_observation`。

- [ ] **Step 5: GREEN 与禁止真实输入验证**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_windows_backend.py -q
venv\Scripts\python.exe -m py_compile core/computer/win32_adapter.py core/computer/windows_backend.py
git diff --check -- core/computer/win32_adapter.py core/computer/windows_backend.py tests/test_computer_windows_backend.py
```

Expected: pytest 全程只使用 fake adapter，测试运行时真实鼠标和键盘完全不动。`scripts/smoke_computer_control.py --desktop-input` 必须额外要求 `--confirm-real-input I_UNDERSTAND`，否则退出 2。

Suggested commit after explicit authorization: `feat(computer): add fail-closed Windows backend`

---

### Task 8: 单租约 Controller、状态机、暂停恢复和停止顺序

**Files:**
- Create: `core/computer/controller.py`
- Create: `tests/test_computer_controller.py`

**Interfaces:**
- Consumes: `ComputerBackend`, `ComputerCapability`, `ComputerOwner`, `ComputerAction`
- Produces: `ComputerControlService` 全部 Cross-Task 公共方法
- Produces: `get_computer_control_service()` 惰性单例
- Produces events: `computer_state`, `computer_action_started`, `computer_action_finished`, `computer_observation`

- [ ] **Step 1: 写状态机和租约失败测试**

```python
def test_second_owner_cannot_take_active_lease(service, owner_a, owner_b, capability):
    run = service.start_run(owner_a, capability, target="desktop")
    with pytest.raises(ComputerControlError) as raised:
        service.start_run(owner_b, capability, target="desktop")
    assert raised.value.code == "computer_session_busy"
    assert raised.value.details == {"owner_session_label": owner_a.session_label}
    assert service.snapshot().run_id == run.run_id


def test_generic_two_target_run_leases_on_first_observe(service, owner_a, capability):
    run = service.start_run(owner_a, capability, target=None)
    service.execute(run.run_id, owner_a, ComputerAction(kind="observe", target="browser"))
    with pytest.raises(ComputerControlError):
        service.execute(run.run_id, owner_a, ComputerAction(kind="click", target="desktop", x=1, y=1))


def test_resume_requires_fresh_observation_before_action(service, running_run, owner_a):
    service.pause(running_run.run_id, "用户暂停")
    service.resume(running_run.run_id)
    with pytest.raises(ComputerControlError, match="先观察"):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="desktop", x=1, y=1))


def test_stop_is_idempotent_and_releases_inputs_once(service, running_run):
    first = service.stop(running_run.run_id, "hotkey")
    second = service.stop(running_run.run_id, "hotkey")
    assert first.status == second.status == "stopped"
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1
```

另测非法状态转换、owner/cancel/generation 检查、动作串行、错误时不发成功、三次无变化、observe/wait 不计 no-progress、停止事件只发一次。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q`

Expected: controller 模块不存在。

- [ ] **Step 3: 实现显式转移表和锁**

```python
_TRANSITIONS = {
    "idle": {"starting"},
    "starting": {"running", "failed", "stopping"},
    "running": {"paused", "waiting_for_user", "provider_safety_required", "stopping", "failed"},
    "paused": {"running", "stopping", "failed"},
    "waiting_for_user": {"running", "stopping", "failed"},
    "provider_safety_required": {"running", "stopping", "failed"},
    "stopping": {"stopped", "failed"},
    "stopped": {"starting"},
    "failed": {"starting"},
}
```

租约和状态由 `threading.RLock` 保护；backend I/O 不在持锁期间长时间执行，但每个动作由单一 `action_lock` 串行。动作前后用 run id + generation 二次确认，防止关闭设置后旧动作提交成功。

- [ ] **Step 4: 实现固定动作与停止语义**

动作顺序严格为 owner/state/cancel/generation → before → execute → fresh after → no-progress → events → Provider observation。预期改变界面的动作只有在 `screen_sha256` 和 active fingerprint 都连续三次相同才失败为 `computer_no_progress`。

`stop()` 顺序：controller flag → owner cancel event → 注入的 `abort_provider(owner.turn_id)` → 禁止新动作 → backend cancel/`release_inputs()` → backend `close()` → 状态终结 → 租约释放 → final event。每步用 `try/finally` 保证后续安全释放，但不把清理错误伪装成 stopped success；首个清理错误记录为 run failure。

- [ ] **Step 5: GREEN 与并发回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_controller.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py -q
git diff --check -- core/computer/controller.py tests/test_computer_controller.py
```

Expected: 同时提交的第二动作在第一动作结束前不进入 backend；关闭 generation 后旧动作不能提交 observation。

Suggested commit after explicit authorization: `feat(computer): add exclusive control state machine`

---

### Task 9: 固定 generic 工具、follow-up 多模态消息与批处理约束

**Files:**
- Modify: `core/tool_protocol.py`
- Modify: `core/harness/tool_pipeline.py`
- Modify: `core/tool_registry.py`
- Create: `core/computer/tool_adapter.py`
- Create: `core/computer/provider_generic.py`
- Create: `tests/test_computer_tool_adapter.py`
- Create: `tests/test_computer_provider_generic.py`
- Modify: `tests/test_tool_pipeline.py`
- Modify: `tests/test_tool_registry.py`

**Interfaces:**
- Consumes: controller public API and `COMPUTER_ACTION_TOOL_SCHEMA`
- Produces: `ToolResult.followup_messages: tuple[dict[str, Any], ...] = ()`
- Produces: `build_generic_computer_tool(capability) -> dict | None`
- Produces: `build_generic_dynamic_prompt(capability) -> str`
- Produces: `validate_computer_tool_batch(tool_calls) -> None`
- Produces: `execute_reserved_computer_tool(decoded, context) -> ToolResult | None`

- [ ] **Step 1: 写默认兼容和电脑 follow-up 失败测试**

```python
def test_existing_tool_result_default_is_byte_compatible():
    result = ToolResult("ok")
    assert result.to_message_content() == "ok"
    assert result.followup_messages == ()


def test_computer_result_adds_tool_then_user_observation(fake_controller, context):
    record = execute_one_computer_action(fake_controller, context, target="browser")
    assert record.message == {
        "role": "tool", "tool_call_id": "call-1", "name": "computer_action",
        "content": record.message["content"],
    }
    assert record.followup_messages == ({
        "role": "user",
        "content": [
            {"type": "text", "text": "【电脑观察】请只依据这张最新截图决定下一步。"},
            {"type": "image_url", "image_url": {"url": record.observation_data_url}},
        ],
        "_computer_v1": {"run_id": "run-a", "ordinal": 1, "target": "browser"},
    },)


@pytest.mark.parametrize("calls", [
    [_call("computer_action"), _call("computer_action")],
    [_call("computer_action"), _call("read_file")],
])
def test_computer_action_cannot_be_batched_or_mixed(calls):
    with pytest.raises(ComputerControlError) as raised:
        validate_computer_tool_batch(calls)
    assert raised.value.code == "computer_invalid_action"
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py -q`

Expected: `followup_messages` 和固定工具适配不存在。

- [ ] **Step 3: 扩展 ToolResult 和 ToolExecutionRecord，不污染 tool message**

`core/tool_protocol.py`：

```python
@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    changed_files: list[str] = field(default_factory=list)
    diff: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    followup_messages: tuple[dict[str, Any], ...] = ()
```

`ToolExecutionRecord` 同样增加默认空 tuple。`ToolPipeline.execute_decoded()` 在固定 computer adapter 返回结果时把 follow-up 暂存在内部 outcome；`ToolBatchExecutor` 构造 record 后从普通 message 完全剥离该私有载荷。外部 Provider/tool message 仍只有 `role/tool_call_id/name/content`。

在 `core/llm.py` 现有提交点，先按 ordinal 持久化全部 tool results，再按相同 ordinal 追加 follow-up user messages，然后才创建下一 Provider Step。Journal event 继续只哈希 tool content，不哈希/保存图片正文。

- [ ] **Step 4: 实现保留名与 capability token 校验**

`ToolPipeline.execute_decoded()` 在 `resolve_tool()` 之前调用固定适配：只有 `decoded.name == "computer_action"` 才进入；context 必须携带 Turn 冻结的 capability token、owner 和 run id。设置关闭、owner 不符或伪造名称分别返回稳定错误。

`ToolRegistry.register()`、插件加载和 MCP schema 合并发现 `computer_action` 或 `computer_select_target` 时明确拒绝/跳过该扩展并记录失败状态；默认 `build_default_registry().schemas()` 完全不含这两个名字。

- [ ] **Step 5: 实现 generic schema、提示和观察消息**

generic tool 只在 capability protocol 为 `generic` 时追加到现有 tools 尾部。说明文本必须逐字包含：首次 observe、一次一个动作、动作后看新截图、坐标原点、登录/验证码用户接管、完成后停止调用。

Provider tool result 使用 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，字段只含 `ok,target,action,screen_sha256,width,height,origin_or_window,error_code,error_message`。PNG 转 `data:image/png;base64,...` 只存在 follow-up 多模态消息；输入正文不进入 JSON/metadata/log。

- [ ] **Step 6: GREEN、顺序和现有工具回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py tests/test_tool_adapters.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
git diff --check -- core/tool_protocol.py core/harness/tool_pipeline.py core/tool_registry.py core/computer tests
```

Expected: 默认工具序列和 golden 不变；电脑 call 的 tool result 总在观察 user message 前；混合批次零副作用。

Suggested commit after explicit authorization: `feat(harness): add generic computer tool observations`

---

### Task 10: 冻结 Turn capability 并接入 generic Provider 请求

**Files:**
- Modify: `core/context_builder.py`
- Modify: `core/agent_service.py`
- Modify: `core/llm.py`
- Modify: `core/harness/request_envelope.py`
- Create: `tests/test_computer_llm_generic.py`
- Modify: `tests/test_prompt_byte_stability.py`
- Modify: `tests/test_provider_payload_golden.py`
- Modify: `tests/test_deepseek_prompt_ledger.py`
- Modify: `tests/test_cache_keepalive.py`

**Interfaces:**
- Consumes: `resolve_computer_capability()`, generic schema/prompt, fixed tool adapter
- Produces: `RuntimeContext.computer_capability: ComputerCapability | None`
- Produces: one immutable capability snapshot per Turn, reused by every Step
- Produces: default-off request bytes identical to pre-feature baseline

- [ ] **Step 1: 写 Turn 冻结和默认关闭字节测试**

```python
def test_computer_capability_is_resolved_once_per_turn(monkeypatch, generic_model):
    calls = []
    monkeypatch.setattr(llm_mod, "resolve_computer_capability", lambda *args: calls.append(args) or GENERIC_CAPABILITY)
    run_two_step_tool_turn(model=generic_model)
    assert len(calls) == 1


def test_generic_tool_is_appended_without_reordering_existing_tools(monkeypatch):
    request = capture_first_provider_request(capability=GENERIC_CAPABILITY)
    names = [tool["function"]["name"] for tool in request["tools"]]
    assert names[:-1] == get_existing_tool_names()
    assert names[-1] == "computer_action"


def test_default_off_request_matches_existing_golden(monkeypatch):
    request = capture_first_provider_request(capability=DISABLED_CAPABILITY)
    assert canonical_bytes(request) == EXISTING_DEFAULT_REQUEST_BYTES
```

另测 capability 在中途开启时当前 Turn 不变；中途关闭时 controller 取消但 tools contract 不被换成另一个 schema；动态 prompt 只在 enabled 时出现；generic 图片经过现有媒体折叠链。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q`

Expected: 新 capability 上下文和工具注入不存在；原有 golden 测试仍应单独通过。

- [ ] **Step 3: 在 Turn 起点解析一次能力**

在 `core/agent_service.py` 已确定当前 model/provider、turn source、settings 和 turn id 后立即解析，存入本次调用的局部不可变 `ComputerCapability`，并传给 `core.llm.chat_with_model_events(...)` 和 `ToolExecutionContext`。不要写模块级“当前 capability”。

来源必须由入口层给出受控值 `local|lan`；不得从用户消息或模型参数读取。关闭设置时通过 generation/controller cancel 终止活动动作，不重算该 Turn 的 request contract。

- [ ] **Step 4: 只向动态 prompt 追加电脑说明**

扩展 `RuntimeContext` 默认字段：

```python
computer_capability: ComputerCapability | None = None
```

`build_dynamic_prompt()` 末尾仅在 `capability and capability.enabled` 时追加 `build_generic_dynamic_prompt()` 或原生短说明。`build_stable_prompt()`、`build_stable_system_message()` 和 `_DECISION_POLICY` 一字不改。默认 `None/disabled` 返回值必须与修改前完全相同，包括换行。

- [ ] **Step 5: 在 route tool selection 后追加 generic tool**

在现有 tools 已按 provider/权限/plan 路由完成之后复制列表：

```python
request_tools = list(existing_tools)
computer_tool = build_generic_computer_tool(turn_capability)
if computer_tool is not None:
    request_tools.append(computer_tool)
```

然后才交给 RequestEnvelope 指纹和 Provider 转换。不得缓存到全局 `get_tools_schema()`。DeepSeek Ledger 看到新 contract 时走现有切代；不要专门改 Ledger。Kimi/GLM metadata、cache-control、reasoning、`extra_body` 保持原逻辑。

- [ ] **Step 6: 提交工具结果和观察消息的同 Step 边界**

现有 `ToolBatchExecutor` 返回 records 后：

```python
tool_messages = [record.message for record in records]
followups = [message for record in records for message in record.followup_messages]
messages.extend(tool_messages)
persist_messages(tool_messages)
messages.extend(followups)
persist_messages(followups)
```

实际代码必须复用现有单事务/持久化 API，不新增独立 SQLite 连接。若观察构造失败，当前工具 result 为 error 且不得附旧图；取消打捞为悬空 computer call 生成 `computer_cancelled` tool result。

- [ ] **Step 7: GREEN 与缓存/ledger 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_llm_responses.py tests/test_llm_events_fixes.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q
git diff --check -- core/context_builder.py core/agent_service.py core/llm.py core/harness/request_envelope.py tests
```

Expected: 现有 hash/expected 文件内容无变化；enabled generic 只在 tools 尾部多一个 schema；同 Turn 多 Step schema 稳定。

Suggested commit after explicit authorization: `feat(llm): route generic computer turns`

---

### Task 11: OpenAI Responses 原生 Computer Use 与 continuation

**Files:**
- Create: `core/computer/provider_openai.py`
- Create: `tests/test_computer_provider_openai.py`
- Modify: `core/harness/request_envelope.py`
- Modify: `core/harness/stream_assembler.py`
- Modify: `core/llm.py`
- Modify: `tests/test_llm_responses.py`
- Modify: `tests/test_provider_payload_golden.py`
- Modify: `tests/test_harness_runtime.py`

**Interfaces:**
- Consumes: native `ComputerCapability`, controller and target selector
- Produces: `build_openai_computer_tools(capability, target_selected) -> tuple[dict, ...]`
- Produces: `NativeComputerCall(response_id, call_id, actions, pending_safety_checks)`
- Produces: `parse_openai_computer_events(events) -> NativeComputerCall | NativeFinalText`
- Produces: `build_computer_call_output(call, observation, acknowledged_checks=()) -> dict`
- Produces: deterministic `_computer_v1` persistence projection

- [ ] **Step 1: 写受控 built-in、事件完整性和 continuation 失败测试**

```python
def test_native_first_request_appends_controlled_computer_tool():
    request = capture_responses_request(capability=NATIVE_BROWSER_CAPABILITY)
    assert request["tools"][-1] == {"type": "computer"}
    assert "previous_response_id" not in request


def test_native_continuation_sends_only_previous_id_and_call_output():
    request = capture_continuation_request(response_id="resp_1", call_id="call_1", png=b"png")
    assert request["previous_response_id"] == "resp_1"
    assert request["input"][0]["type"] == "computer_call_output"
    assert request["input"][0]["call_id"] == "call_1"
    assert request["input"][0]["output"]["type"] == "computer_screenshot"
    assert "messages" not in request


@pytest.mark.parametrize("event", [
    {"type": "response.output_item.done", "item": {"type": "computer_call", "call_id": "c", "actions": [{"type": "click", "x": 1, "y": 1}]}},
    {"type": "response.completed", "response": {"id": "resp_1", "output": [{"type": "computer_call", "actions": [{"type": "click", "x": 1, "y": 1}]}]}},
])
def test_missing_response_or_call_id_is_protocol_error(event):
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events([event])
    assert raised.value.code == "computer_provider_protocol_error"


def test_pending_safety_blocks_before_execution(controller):
    call = NativeComputerCall("resp_1", "call_1", (CLICK,), (SafetyCheck("sc_1", "reason"),))
    result = handle_native_call(call, controller)
    assert result.status == "provider_safety_required"
    assert controller.execute_count == 0


def test_native_actions_are_serial_and_stop_after_first_failure(controller):
    controller.fail_on_ordinal = 2
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK, TYPE_TEXT), ())
    with pytest.raises(ComputerControlError):
        handle_native_call(call, controller)
    assert controller.executed_actions == [MOVE, CLICK]
    assert controller.max_concurrent_actions == 1
```

另测 native 双 target 时 `computer_select_target` 从首请求存在；未选 target 的 computer call 被拒；response failed/incomplete；unknown built-in 仍拒绝；final text 退出 continuation。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py -q`

Expected: 原生 computer built-in 和事件类型不被当前转换器支持。

- [ ] **Step 3: 实现精确原生工具与 target selector**

built-in 使用当前官方 GA 定义 `{"type":"computer"}`。官方 migration 表明确 `computer_use_preview` 是旧路径，新实现不得采用或自动回退。实施时再次打开官方指南复核；若官方 GA 形状未来变化，先更新设计附录并说明证据。`_responses_tools_from_chat_tools()` 仍拒绝任意 built-in，只允许 capability 分支直接传入的精确 computer definition。

两个 target 同时启用时，首请求在现有 function tools 后依次追加 `computer_select_target` 和 native computer tool。selector schema：

```python
{
    "type": "function",
    "name": "computer_select_target",
    "description": "为本次电脑操控选择唯一目标；选择后不可更改。",
    "parameters": {
        "type": "object",
        "properties": {"target": {"type": "string", "enum": ["browser", "desktop"]}},
        "required": ["target"],
        "additionalProperties": False,
    },
}
```

重复选择、未开启 target、选择后变更均明确失败。

- [ ] **Step 4: 严格标准化 Responses 事件**

`NativeComputerCall` 只有 response id、call id、非空完整 `actions[]` 和 safety checks 解析成功才构造。流式 delta 先由 assembler 合并，禁止基于半截 JSON 执行动作。provider 的每个 computer action 映射到 Task 2 统一动作后再次校验坐标/字段；官方 `screenshot` 映射为 `observe`。

GA 原生 `actions[]` 是 generic 单动作规则的明确例外：按原序逐个调用 controller，每个动作前都检查 cancel/pause/owner/generation，每个动作都发 trace；失败或停止后不执行余项。整批成功后只捕获一次截图并构造一个 `computer_call_output`。禁止并行、禁止忽略中间失败、禁止在后台把整批做成不可中断操作。

completed/incomplete/failed 三种终态显式区分。缺字段、重复冲突字段或未知动作抛 `computer_provider_protocol_error`，当前 Step/Turn 失败，绝不进入 generic 分支。

- [ ] **Step 5: 实现 continuation 和 Provider safety 确认**

活动 native continuation 的下一 Step 请求仅携带 `previous_response_id` 与一个 `computer_call_output`。`previous_response_id` 仅存 Turn 内存对象，不写 message、Journal、event、runtime log。

pending safety check 时 controller 进入 `provider_safety_required`，UI/CLI 的继续 API 必须传当前 run id 和精确 check ids；只有用户操作调用后才把这些 ids 放入官方要求的 acknowledged 字段。普通聊天文本、“完全访问”模式或自动重试不能确认。

- [ ] **Step 6: 实现脱敏持久化投影与崩溃语义**

原生中间行写 `messages` 时使用：

```python
{
    "role": "assistant",
    "content": "",
    "_computer_v1": {
        "kind": "native_call",
        "run_id": run_id,
        "call_id_hash": sha256(call_id),
        "action": sanitized_action_summary,
    },
}
```

输入文字只保存 code point 数和 SHA-256；截图只保存 screen hash/尺寸；不保存 base64、profile path、完整 URL、response id。后续普通 Provider 请求确定性过滤 `_computer_v1` 中间行，但最终 assistant 文本正常保留。崩溃恢复只标 interrupted，不自动继续旧 response id 或重放动作。

- [ ] **Step 7: GREEN 与默认/原生 golden 门禁**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py tests/test_harness_runtime.py -q
venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q
git diff --check -- core/computer/provider_openai.py core/harness/request_envelope.py core/harness/stream_assembler.py core/llm.py tests
```

Expected: 默认所有现有 golden 原样通过；新 native fixture 精确断言 first request 和 continuation bytes；日志/DB/Journals 无 response id 与图片 base64。

Suggested commit after explicit authorization: `feat(openai): add native computer continuation`

---

### Task 12: GUI 系统设置与模型能力编辑

**Files:**
- Modify: `gui_bridge.py`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/components/SystemSettings.vue`
- Modify: `frontend/src/components/SettingsPage.vue`
- Modify: `frontend/src/components/SystemSettings.test.ts`
- Modify: `frontend/src/components/SettingsPage.test.ts`
- Modify: `frontend/src/stores/appStore.systemSettings.test.ts`
- Modify: `frontend/src/stores/appStore.modelSettings.test.ts`
- Modify: `tests/test_gui_system_settings.py`
- Modify: `tests/test_gui_model_settings.py`

**Interfaces:**
- Consumes: settings strict setters, model_manager capability fields
- Produces bridge: `set_browser_control_enabled(value: bool)`, `set_computer_control_enabled(value: bool)`, `set_computer_browser(value: str)`
- Produces UI: “电脑操控”设置区和模型 `supports_function_calling/computer_protocol` 字段

- [ ] **Step 1: 写 Bridge 和 Store 回滚失败测试**

```python
def test_bridge_enable_failure_returns_unchanged_snapshot(monkeypatch):
    bridge = _bare_bridge()
    monkeypatch.setattr("core.computer.settings.set_browser_control_enabled", Mock(side_effect=ComputerControlError("computer_hotkey_unavailable", "热键不可用")))
    result = bridge.set_browser_control_enabled(True)
    assert result["ok"] is False
    assert result["settings"]["browser_control_enabled"] is False
    assert result["error_code"] == "computer_hotkey_unavailable"
```

```ts
it('rolls back to backend snapshot when browser enable preflight fails', async () => {
  api.set_browser_control_enabled.mockResolvedValue({
    ok: false,
    error_code: 'computer_browser_unavailable',
    msg: 'Edge 未安装',
    settings: disabledSettings,
  });
  await store.setBrowserControlEnabled(true);
  expect(store.systemSettings.browser_control_enabled).toBe(false);
  expect(store.modal.message).toContain('Edge 未安装');
});
```

另测活动 run 时 browser select 失败；关闭调用后快照立即 false；model generic 的三字段校验；旧模型默认 none。

- [ ] **Step 2: 运行确认 RED**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q
npm --prefix frontend test -- --run frontend/src/components/SystemSettings.test.ts frontend/src/components/SettingsPage.test.ts frontend/src/stores/appStore.systemSettings.test.ts frontend/src/stores/appStore.modelSettings.test.ts
```

Expected: 新 Bridge、类型和控件不存在。

- [ ] **Step 3: 实现 Bridge 严格 API**

每个 setter 只调用领域设置函数，返回：

```python
{"ok": True, "settings": self.get_system_settings()}
```

或：

```python
{"ok": False, "error_code": error.code, "msg": str(error), "settings": self.get_system_settings()}
```

Bridge 不直接写 config、不操作 Vue、不吞异常为 success。开启 preflight 必须组合 selected browser/Windows backend 和全局热键检查。关闭成功后调用 controller.stop，且即使清理尚在进行，返回快照中的开关已为 false。

- [ ] **Step 4: 扩展 TypeScript 类型和 Store actions**

`SystemSettingsSnapshot` 增加精确字段和 `ComputerBrowserOption`；model 类型增加两个能力字段。Store setter 只以后端返回的 `settings` 覆盖本地状态，不做持久乐观值：

```ts
async setBrowserControlEnabled(enabled: boolean) {
  const result = await window.pywebview.api.set_browser_control_enabled(enabled);
  this.systemSettings = result.settings;
  if (!result.ok) this.showError(result.msg || '浏览器操控设置失败');
  return result.ok;
}
```

- [ ] **Step 5: 增量扩展设置页面**

在现有紧凑列表加“电脑操控”区，顺序固定：浏览器操控开关、Edge/Chrome select、完整电脑操控开关、专用 profile 只读路径、隐私提示、平台/浏览器/热键 preflight。两个 switch 初始 false；option 不可用时 disabled 并显示原因；活动 run 时 browser select disabled。

提示文本必须明确：“开启后，当前页面或桌面截图会发送给当前模型 Provider；Y-CODE 不会逐动作询问。登录、验证码和凭据请在专用浏览器中由你手动完成。紧急停止：Ctrl+Alt+Esc。”

模型 modal 使用 checkbox `supports_function_calling` 与 select `computer_protocol`；选 generic 时视觉或工具能力未开启则前端阻止保存并显示同一校验原因，后端仍再次校验。

- [ ] **Step 6: GREEN 与前端构建**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py tests/test_computer_settings.py tests/test_computer_capability.py -q
npm --prefix frontend test -- --run frontend/src/components/SystemSettings.test.ts frontend/src/components/SettingsPage.test.ts frontend/src/stores/appStore.systemSettings.test.ts frontend/src/stores/appStore.modelSettings.test.ts
npm --prefix frontend run build
git diff --check -- gui_bridge.py frontend/src tests/test_gui_system_settings.py tests/test_gui_model_settings.py
```

Expected: 失败回滚到后端真实值；手机 viewport 不显示可点击启用控件；既有设置无视觉回归。

Suggested commit after explicit authorization: `feat(gui): add explicit computer settings`

---

### Task 13: GUI 顶部安全条、操控页签、事件 reducer 与 Provider safety 操作

**Files:**
- Create: `frontend/src/components/ComputerControlBar.vue`
- Create: `frontend/src/components/ComputerControlBar.test.ts`
- Create: `frontend/src/components/ComputerControlPane.vue`
- Create: `frontend/src/components/ComputerControlPane.test.ts`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/components/RightPanel.vue`
- Modify: `frontend/src/App.vue`
- Modify: `gui_bridge.py`
- Modify: `tests/test_gui_bridge_events.py`
- Modify: relevant existing `RightPanel` and `App` Vitest files

**Interfaces:**
- Consumes: controller snapshots/events and preview URL
- Produces Store: `computerRun`, `computerTrace`, `computerLastAutoOpenedRunId`
- Produces actions: `pauseComputerControl()`, `resumeComputerControl()`, `stopComputerControl()`, `acknowledgeComputerSafety(checkIds)`
- Produces UI: persistent top bar and dynamic right tab with max 200 trace records

- [ ] **Step 1: 写 reducer 顺序和安全按钮失败测试**

```ts
it('rejects late events from old runs and older ordinals', () => {
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', ordinal: 1, status: 'running' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', ordinal: 99, preview_url: '/old' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-2', ordinal: 0, preview_url: '/stale' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.preview_url).not.toBe('/old');
  expect(store.computerRun.preview_url).not.toBe('/stale');
});


it('keeps only the newest 200 sanitized trace rows', () => {
  for (let ordinal = 1; ordinal <= 205; ordinal++) {
    store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', ordinal, summary: `输入 ${ordinal} 个字符` });
  }
  expect(store.computerTrace).toHaveLength(200);
  expect(JSON.stringify(store.computerTrace)).not.toContain('secret text');
});


it('always shows stop and pause controls on the compact bar', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningComputer() });
  expect(wrapper.get('[data-test="computer-pause"]').isVisible()).toBe(true);
  expect(wrapper.get('[data-test="computer-stop"]').isVisible()).toBe(true);
});
```

另测 run 首次开始自动开操控 tab 一次、用户切走后不抢回、结束保留 trace、下一 run 清空、provider safety 只显示继续/停止。

- [ ] **Step 2: 运行确认 RED**

Run:

```powershell
npm --prefix frontend test -- --run frontend/src/components/ComputerControlBar.test.ts frontend/src/components/ComputerControlPane.test.ts
venv\Scripts\python.exe -m pytest tests/test_gui_bridge_events.py -q
```

Expected: 组件、Store 字段和 Bridge 方法不存在。

- [ ] **Step 3: 实现纯事件 reducer**

事件共同类型：

```ts
interface ComputerEventBase { run_id: string; ordinal: number; type: string }
interface ComputerRunSnapshot {
  run_id: string; status: ComputerRunStatus; target: 'browser'|'desktop'|'';
  browser: 'edge'|'chrome'|''; model_name: string; protocol: ComputerProtocol;
  action_count: number; current_action: string; context_summary: string;
  preview_url: string; pause_reason: string; stop_reason: string; error_code: string;
  hotkey_available: boolean; pending_safety_checks: Array<{ id: string; message: string }>;
}
```

reducer 只接受当前 run 且 ordinal 不小于已记录值；新 run 的 `computer_state/starting` 清 trace 并设置一次自动开页签标志。trace 行只含 ordinal、动作脱敏摘要、状态、耗时、错误码；`type_text` 显示长度，不存正文。

- [ ] **Step 4: 实现顶部条和右侧页签**

`ComputerControlBar` 插在现有聊天标题栏下方，只对 `starting|running|paused|waiting_for_user|provider_safety_required|stopping|failed` 显示。窄屏 CSS 可以隐藏 model/context，但 `[data-test=computer-stop]` 和 pause/resume 永不隐藏。

`RightPanel.tabs` 动态追加 `{ id: 'computer', label: '操控' }`；`ComputerControlPane` 显示 `<img :src="preview_url">`、run 摘要、最多 200 行新到旧 trace。不要通过 `evaluate_js` 传 base64。

- [ ] **Step 5: Bridge 只转发小事件和领域控制**

Bridge API：

```python
get_computer_control_status()
pause_computer_control(run_id)
resume_computer_control(run_id)
stop_computer_control(run_id)
acknowledge_computer_safety(run_id, check_ids)
```

每个 mutation 都要求当前 run id，防止旧 UI 事件停止新 run。controller listener 只把小 JSON `put_nowait` 到 `_js_queue`；队列满按现有策略丢新事件，不在 backend/action/delta lock 内调用 `evaluate_js`。

- [ ] **Step 6: GREEN、构建和 GUI 线程边界**

Run:

```powershell
npm --prefix frontend test -- --run frontend/src/components/ComputerControlBar.test.ts frontend/src/components/ComputerControlPane.test.ts frontend/src/components/RightPanel.test.ts frontend/src/stores/appStore.systemSettings.test.ts
npm --prefix frontend run build
venv\Scripts\python.exe -m pytest tests/test_gui_bridge_events.py tests/test_computer_controller.py -q
git diff --check -- frontend/src gui_bridge.py tests/test_gui_bridge_events.py
```

Expected: event thread 测试断言没有直接 `evaluate_js`；preview src 为 loopback token URL；旧事件不能覆盖新 run。

Suggested commit after explicit authorization: `feat(gui): show controllable computer runtime`

---

### Task 14: CLI 运行控制、Ctrl+C 和线程安全显示

**Files:**
- Create: `core/computer/cli.py`
- Create: `tests/test_computer_cli.py`
- Modify: `core/command_handler.py`
- Modify: `core/ui.py`
- Modify: relevant CLI command tests

**Interfaces:**
- Consumes: controller public API
- Produces commands: `/computer status|pause|resume|stop`
- Does not produce: `/computer on`, `/computer off`, 永久设置修改或截图字符画

- [ ] **Step 1: 写命令白名单和脱敏渲染失败测试**

```python
@pytest.mark.parametrize("command", ["/computer on", "/computer off", "/computer browser chrome", "/computer"])
def test_cli_rejects_permanent_or_incomplete_computer_commands(command):
    result = handle_computer_command(command, FakeController())
    assert result.ok is False
    assert "设置页" in result.text or "用法" in result.text


def test_cli_status_when_disabled_is_plain_text():
    result = render_computer_snapshot(disabled_snapshot())
    assert result == "电脑操控: 已关闭（请在 GUI 系统设置页开启）"
    assert "**" not in result


def test_cli_trace_never_prints_typed_text():
    text = render_computer_event({"action": "type_text", "text_length": 6, "text": "secret"})
    assert text == "电脑操控: 输入 6 个字符"
    assert "secret" not in text
```

另测 pause/resume/stop 传当前 run id，provider safety/error code，Ctrl+C 先 controller stop 再走现有 Turn cancel。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_cli.py tests/test_command_handler.py -q`

Expected: CLI computer handler 不存在。

- [ ] **Step 3: 实现严格 parser 和纯文本 renderer**

```python
_COMMANDS = {"status", "pause", "resume", "stop"}
```

使用 `shlex.split` 后必须恰好两个 token；不接受别名或额外参数。status 不开启能力；其他命令要求活动 run。显示格式固定为 `电脑操控: 运行中 · Edge · 12 步`，动作一行，输入只显示长度，URL 只显示 host。

- [ ] **Step 4: 接入主线程安全事件和取消顺序**

hotkey/backend/controller listener 不直接 `print_formatted_text` 或改 layout；通过现有事件队列，最终用 `app_instance.loop.call_soon_threadsafe(callback)` 更新。CLI Ctrl+C 检测到 active run 时先 `controller.stop(run_id, "cli_ctrl_c")`，其内部触发 owner cancel/Provider stream abort，再复用现有 KeyboardInterrupt 收尾。

- [ ] **Step 5: GREEN 与 CLI 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_cli.py tests/test_command_handler.py tests/test_ui.py tests/test_llm_cancellation.py -q
git diff --check -- core/computer/cli.py core/command_handler.py core/ui.py tests/test_computer_cli.py
```

Expected: 原始终端无 Markdown 强调；任何 CLI 命令都不能改三个永久配置键。

Suggested commit after explicit authorization: `feat(cli): add safe computer run controls`

---

### Task 15: LAN 后端权限边界和不可伪造 Turn source

**Files:**
- Modify: `core/lan_server.py`
- Modify: `gui_bridge.py`
- Modify: `core/agent_service.py`
- Modify: `tests/test_lan_server.py`
- Modify: `tests/test_computer_capability.py`

**Interfaces:**
- Consumes: `turn_source` capability input and Bridge methods
- Produces: LAN RPC may call status/stop only
- Produces: LAN-created turn enters backend as `turn_source="lan"`

- [ ] **Step 1: 写 RPC 后端强制边界失败测试**

```python
@pytest.mark.parametrize("method", [
    "set_browser_control_enabled",
    "set_computer_control_enabled",
    "set_computer_browser",
    "pause_computer_control",
    "resume_computer_control",
    "acknowledge_computer_safety",
])
def test_lan_forbids_computer_permission_expansion(server, method):
    response = server.dispatch_rpc(method, [])
    assert response["error"]["code"] == "method_not_allowed"


@pytest.mark.parametrize("method", ["get_computer_control_status", "stop_computer_control"])
def test_lan_allows_status_and_emergency_stop(server, method):
    assert server.dispatch_rpc(method, ["run-a"] if method.startswith("stop") else [])["ok"] is True


def test_lan_user_message_cannot_override_turn_source(server, bridge):
    server.dispatch_rpc("send_user_message", [{"text": "use computer", "turn_source": "local"}])
    assert bridge.last_turn_source == "lan"
```

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py -q`

Expected: 新 mutation 还未加入禁止列表或 turn source 尚未由 transport 注入。

- [ ] **Step 3: 更新后端禁止列表并注入来源**

把六个 mutation 方法加入 `LanServer.FORBIDDEN_METHODS`；保留 status/stop。LAN dispatch 在调用 Bridge 的消息入口时由服务器内部参数注入 `turn_source="lan"`，并忽略/覆盖客户端 payload 的同名字段。Bridge/local GUI 调用固定传 `local`，模型/用户正文无法设置。

前端隐藏移动端设置控件只作为 UX；安全性以 RPC 黑名单和 resolver 双层测试为准。

- [ ] **Step 4: GREEN 与现有 LAN 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py tests/test_gui_system_settings.py -q
git diff --check -- core/lan_server.py gui_bridge.py core/agent_service.py tests/test_lan_server.py tests/test_computer_capability.py
```

Expected: 手机可看到并停止本机已有 run；远程消息即使本机开关开启也拿不到 computer tool/native built-in。

Suggested commit after explicit authorization: `fix(lan): prevent remote computer privilege expansion`

---

### Task 16: 生命周期、打包、隐私审计与受控烟测

**Files:**
- Modify: `gui_main.py`
- Modify: `main.py`
- Modify: `ycode.spec`
- Modify: `requirements.txt`
- Modify: `scripts/smoke_computer_control.py`
- Create: `tests/test_computer_privacy.py`
- Modify: application lifecycle/package tests
- Modify: `README.md` only if the current user-owned README changes can be preserved with a non-overlapping minimal section

**Interfaces:**
- Consumes: singleton controller, preview server, hotkey
- Produces: startup registration, deterministic shutdown and packaged Python Playwright support
- Produces: explicit manual smoke modes with no implicit real input

- [ ] **Step 1: 写退出顺序和敏感信息扫描失败测试**

```python
def test_application_shutdown_stops_controller_before_preview_and_hotkey():
    calls = []
    shutdown_computer_runtime(
        controller=Recorder(calls, "controller"),
        preview=Recorder(calls, "preview"),
        hotkey=Recorder(calls, "hotkey"),
    )
    assert calls == ["controller.stop", "preview.stop", "hotkey.close"]


def test_persisted_and_logged_computer_records_exclude_secrets(tmp_path):
    secret = "S3CRET-TYPED-TEXT"
    response_id = "resp_secret_123"
    exercise_generic_and_native_projection(tmp_path, secret, response_id)
    corpus = read_text_database_journal_events_and_logs(tmp_path)
    assert secret not in corpus
    assert response_id not in corpus
    assert "data:image/png;base64" not in corpus.native_rows
```

另测 URL query/token、Cookie、API key、profile 文件内容、preview token 不进入日志/Journals；generic DB 图片只走现有媒体字段/折叠，native DB 无 base64。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_app_lifecycle.py -q`

Expected: lifecycle glue 和隐私 fixture 未完成。

- [ ] **Step 3: 接入启动和 deterministic shutdown**

启动只创建轻量 runtime 容器；开关默认 false 时不得导入 Playwright、启动浏览器、启动截图或注册 computer tools。全局热键 preflight 可在用户开启设置时首次注册；活动能力存在时保持注册。

GUI close、CLI 退出和异常 finally 统一调用：active run stop → Provider stream abort 由 controller 执行 → backend inputs release/close → preview stop/delete → hotkey close。不得终止 LiteLLM 共享连接池，不自动恢复 interrupted run。

- [ ] **Step 4: 更新 PyInstaller 打包但不打包浏览器**

`ycode.spec` 只收集 Playwright Python 包需要的模块/driver 资源；不得运行或包含 Playwright Chromium 下载目录。打包 smoke 需要验证系统 Edge/Chrome channel 能被找到，缺失时设置页显示 unavailable。

Run:

```powershell
venv\Scripts\python.exe -m PyInstaller --clean ycode.spec
```

如果仓库现有正式打包命令不同，以 `ycode.spec` 和现有发布脚本的真实命令为准，并把准确命令记录到执行证据；不得发明第二套构建流程。

- [ ] **Step 5: 完成显式人工烟测矩阵**

`scripts/smoke_computer_control.py` 支持：

```text
--probe-browser edge|chrome
--browser-observe edge|chrome
--desktop-observe
--desktop-input --confirm-real-input I_UNDERSTAND
```

observe 模式只截图，不输入。真实输入模式固定执行可见测试区域的一次 move/click/type，并要求用户先打开无敏感数据的测试窗口；每一步前在终端打印将执行的脱敏动作并要求单次 Enter。测试后必须调用 release/stop。

人工验收矩阵：Edge 专用 profile 首次登录/重启保留；Chrome 同样且 profile 隔离；浏览器关闭明确失败；desktop 多屏/缩放；pause 后无新动作；resume 首动作 observe；GUI stop、CLI stop、Ctrl+C、Ctrl+Alt+Esc；锁屏/UAC fail-closed；退出后无残留 Playwright 子进程和临时 PNG。

- [ ] **Step 6: GREEN、打包和隐私回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_app_lifecycle.py tests/test_computer_preview_server.py tests/test_computer_hotkey.py -q
venv\Scripts\python.exe -m py_compile main.py gui_main.py scripts/smoke_computer_control.py
venv\Scripts\python.exe -m compileall -q core/computer
git diff --check -- gui_main.py main.py ycode.spec requirements.txt scripts/smoke_computer_control.py tests/test_computer_privacy.py
```

Expected: 自动测试不产生真实输入；打包产物只依赖系统浏览器；退出清理完整。若 `README.md` 当前存在用户未提交修改且内容重叠，本 Task 不修改 README，并在最终报告明确记录文档冲突，不覆盖用户内容。

Suggested commit after explicit authorization: `build(computer): package and clean control runtime`

---

### Task 17: 全量门禁、规格逐条验收和交付审计

**Files:**
- Modify only: 本计划执行过程中已列出的实现/测试文件中的缺陷修复
- Do not modify: existing golden expected/hash files, unrelated user files, generated screenshots, browser profiles

**Interfaces:**
- Consumes: Tasks 1–16 全部产物
- Produces: 可审查的 RED/GREEN/回归/人工烟测证据和无 Critical/Important 遗留结论

- [ ] **Step 1: 运行电脑操控全套定向测试**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_computer_models.py tests/test_computer_settings.py tests/test_computer_capability.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py tests/test_computer_controller.py tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_computer_provider_openai.py tests/test_computer_llm_generic.py tests/test_computer_cli.py tests/test_computer_privacy.py -q
npm --prefix frontend test -- --run frontend/src/components/ComputerControlBar.test.ts frontend/src/components/ComputerControlPane.test.ts frontend/src/components/SystemSettings.test.ts frontend/src/components/SettingsPage.test.ts frontend/src/stores/appStore.systemSettings.test.ts frontend/src/stores/appStore.modelSettings.test.ts
```

Expected: 全部退出码 0，无 skipped 的核心安全测试；平台特定真实 I/O 由 fake 单测 + Task 16 人工烟测覆盖。

- [ ] **Step 2: 运行 Harness、Provider、缓存和取消门禁**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_llm_responses.py tests/test_harness_runtime.py tests/test_tool_pipeline.py tests/test_tool_adapters.py tests/test_tool_registry.py tests/test_cache_keepalive.py tests/test_token_accounting.py tests/test_llm_cancellation.py tests/test_llm_events_fixes.py -q
```

Expected: 所有现有 expected/hash 原样通过；若失败，定位实现导致的字节漂移，禁止更新 golden。

- [ ] **Step 3: 运行完整 Python、前端和构建门禁**

Run:

```powershell
venv\Scripts\python.exe -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py
venv\Scripts\python.exe -m compileall -q core/computer
venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
git diff --check
```

Expected: 全部退出码 0；记录每条命令实际测试数、持续时间、exit code 和失败重跑原因。

- [ ] **Step 4: 做规格覆盖和敏感信息审计**

Run:

```powershell
rg -n "browser_control_enabled|computer_control_enabled|computer_browser|computer_protocol|supports_function_calling" core frontend tests
rg -n "response_id|data:image/png;base64|Cookie|S3CRET-TYPED-TEXT" core/computer core/harness gui_bridge.py
git diff --name-only
git status --short --branch
```

人工检查：两个开关默认关闭；选定 browser 不 fallback；generic/native 双协议；单 run/单 target；热键；Provider safety；GUI/CLI/LAN 边界；最近三图；200 trace；输入脱敏；默认请求字节不变；关闭/退出无复活。

- [ ] **Step 5: 审查变更边界**

将 `git diff --name-only` 与 File Map 比较。任何 File Map 外变更必须能证明是用户原有修改并保持未触碰，或属于测试执行产生且已安全清理的临时产物。不得删除用户已有 `README.md`、插件、skill、图片或其他未跟踪文件。

- [ ] **Step 6: 输出最终实施报告并停止**

报告必须包含：

- 变更摘要和完整文件列表；
- Tasks 1–16 每个 Task 的真实 RED 命令/失败原因、GREEN 命令/测试数量/时长/exit code；
- generic 与 native 的真实请求形状证据；
- 默认关闭 golden/hash 未改且通过的证据；
- Edge/Chrome/desktop、pause/resume/stop/hotkey、LAN、Provider safety、隐私和退出清理证据；
- 完整 pytest、Vitest、frontend build、PyInstaller（若环境可运行）和人工烟测结果；
- 用户原有工作树修改清单及未触碰说明；
- 剩余风险或未完成项；若有任何 Critical/Important 或未跑门禁，明确结论为“尚不可提交”。

达到停止条件后不要开展开放式重构或新功能，不执行 commit/push/merge/发布。

Suggested final commit after separate explicit authorization: `feat: add controllable browser and Windows computer use`

## Execution Record Protocol

执行者在每个 Task 完成后，把真实证据追加到该 Task 末尾。标题使用 `Execution Record — Task N`；随后依次记录 RED 的精确命令、实际非零 exit code、失败测试名、目标失败原因和时长，GREEN 与 Regression 的精确命令、实际 passed 数、时长和 exit 0，diff check 命令及 exit 0，本 Task 实际触碰文件，以及 `spec/privacy/default-byte/unresolved` 四项审查结论。不得留下模板文字。若 RED 不是因目标缺失而失败，先修正测试或用 systematic-debugging 取证，不能继续 GREEN。

## Spec Coverage Matrix

- 设计 7、10、21（领域模型、统一动作、错误码）：Tasks 2、8、9。
- 设计 8（设置、默认关闭、独立开关）：Tasks 3、12、15。
- 设计 9（显式模型能力）：Tasks 4、10、11、12。
- 设计 11（OpenAI native、continuation、safety、投影）：Task 11，Task 13 提供用户确认 UI。
- 设计 12（Edge/Chrome 专用 profile）：Tasks 1、3、6、16。
- 设计 13（Windows 截图/DPI/SendInput/secure desktop）：Tasks 7、16。
- 设计 14–15（单租约、状态机、暂停/停止/取消/退出）：Tasks 8、13、14、16。
- 设计 16（顶部条、右栏、预览、Store/Bridge）：Tasks 5、12、13。
- 设计 17（CLI）：Task 14。
- 设计 18（LAN）：Tasks 4、15。
- 设计 19（Harness/tool follow-up/消息边界）：Tasks 9、10、11。
- 设计 20（默认字节、Ledger、Kimi/GLM 缓存）：Tasks 1、10、11、17。
- 设计 22（隐私与数据处理）：Tasks 2、5、6、7、9、11、13、14、16、17。
- 设计 23（依赖、打包、生命周期）：Tasks 1、16。
- 设计 24、27–29（测试、验收、风险、K3 约束）：贯穿每个 Task，并由 Task 17 和 companion K3 prompt 收口。

## Stop Conditions

只有同时满足以下条件才可报告“当前可以提交”：

1. Tasks 1–16 全部有真实 RED → GREEN 证据；
2. Edge 和 Chrome 至少完成可见系统 channel observe 探针，未安装选项明确显示 unavailable；
3. desktop 自动测试全 fake，真实输入烟测只在用户明确确认后运行且成功释放输入；
4. generic 模型能在每个动作后看到新截图；native OpenAI 能使用 response id continuation，且二者没有互相 fallback；
5. Provider safety 未确认时零动作，确认只来自显式 GUI/CLI 用户操作；
6. 默认关闭的 Provider/prompt/tools golden 全部原样通过；
7. DB、Journal、runtime log、GUI trace、CLI trace 均无输入正文、response id、Cookie、profile 内容或 native screenshot base64；
8. GUI、CLI、Ctrl+C、Ctrl+Alt+Esc、设置关闭和应用退出都汇聚到同一 stop 路径；
9. LAN 无法开启/暂停/恢复/确认 safety/启动电脑操控，只能 status/stop；
10. 完整 pytest、Vitest、frontend build 和 `git diff --check` 退出码均为 0；
11. 没有 Critical/Important 遗留，没有为通过测试改写 existing golden；
12. 所有用户原有工作树修改都被保留且未被覆盖。
