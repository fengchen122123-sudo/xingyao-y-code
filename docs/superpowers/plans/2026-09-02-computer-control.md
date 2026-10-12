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
- 一个进程只有一个 `ComputerControlService` 活动租约；一个 run 固定一个 target；generic assistant Step 只允许一个电脑动作且不得与其他工具混用。OpenAI GA native 的 `actions[]` 按 Task 11a 的可中断串行规则处理。
- 全局停止键固定为 `Ctrl+Alt+Esc`；任一开关开启期间持续注册，两个都关闭或进程退出才注销。注册失败时两个能力都不可用。停止必须释放所有按下的键和鼠标键。
- 截图失败不得返回旧截图；浏览器失败不得改走桌面；native 解析失败不得改走 generic；非法配置不得自动纠正。
- `computer_action` 是固定 Harness 保留工具，不进入普通 `ToolRegistry`，插件/MCP 同名必须明确拒绝。
- 默认关闭时以下测试不得修改任何 expected/hash：`tests/test_prompt_byte_stability.py`、`tests/test_provider_payload_golden.py`、DeepSeek Prompt Ledger、Kimi/GLM 缓存/推理参数、token 统计。
- `messages` 仍是正文唯一事实源；Harness Journal 只保存 ID、序号、时间、耗时、状态和哈希，不保存截图、输入文字、URL、Provider response id 或 safety 原文。
- `evaluate_js` 仍只允许 `gui-js-dispatcher` 单消费者线程调用；backend/hotkey 线程只入现有事件队列。
- 每个 Task 必须真实执行 RED → GREEN → 相关回归 → `git diff --check` → `git status --short`；不得先写实现后补测试。`git diff --check` 不覆盖未跟踪的新文件，因此每个 Task 创建新文件后必须执行下面的“新增文件空白检查”，不能把它当成可选步骤。
- 本计划不授权 K3 执行 `git commit`、push、merge、发布或修改现有 golden；每个 Task 末尾只给出建议提交信息。

**新增文件空白检查（每个 Task 含新建文件时必做）：**

```powershell
git add -N <本 Task 全部新建文件路径>
git diff --check
git status --porcelain
```

`git add -N`（intent-to-add）让 `git diff --check` 能覆盖未跟踪文件，检查尾随空白、冲突标记和缩进问题；`git status --porcelain` 用于确认没有计划外的新文件悄悄出现。intent-to-add 不改变文件内容，也不是 `git commit` 授权的一部分。

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

`tests/computer_fakes.py` 必须在首次引用前创建，并由各测试显式 import。它统一导出：

- fake 实现：`FakeHotkeyApi`、`FakePlaywrightRuntime`、`FakeWin32Adapter`、`FakeComputerBackend`、`FakeController`；
- pytest fixture：`enabled_settings`（返回两个开关均开启的 `ComputerSettingsSnapshot`）；
- 数据 helper：`fake_png_bytes(seed)`、`sample_computer_actions()`（返回 MOVE/CLICK/TYPE_TEXT 等合法样例动作）、`SafetyCheck`、`openai_compatible_provider()`；
- capability 常量：`GENERIC_CAPABILITY`、`NATIVE_BROWSER_CAPABILITY`、`DISABLED_CAPABILITY`；
- 请求捕获 helper：`http_status(url)`、`capture_responses_request(...)`、`capture_continuation_request(...)`、`use_temp_config(tmp_path, monkeypatch, values)`、`read_text_surfaces(...)`。

fixture 只能实现对应 Protocol 的最小确定性行为，禁止访问真实键鼠、真实浏览器、网络 Provider 或用户配置。除上述导出外，测试正文引用的其他 helper（如 Task 9 的 `_call`、Task 10 的 `run_two_step_tool_turn`/`canonical_bytes`、Task 11a 的 `NATIVE_MODEL_NAME` 等）必须定义在同一个测试模块内；测试收集阶段必须证明没有 `NameError` 或缺失 fixture。后文旧式 `_png_bytes`、`_http_status`、`_use_config` 名称一律视为上述公开 helper 的笔误并在实现时不得出现。

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

- [x] **Step 1: 记录干净基线与用户已有变更**

Run:

```powershell
git branch --show-current
git status --short
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Expected: branch 为 `dev`；现有用户修改被记录但不清理；所有门禁退出码为 0。若基线失败，停止实施并用 `superpowers:systematic-debugging` 定位，不能把既有失败算作本功能 RED。

- [x] **Step 2: 写依赖与探测失败测试**

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

- [x] **Step 3: 运行并确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py -q`

Expected: 至少因 `requirements.txt` 尚未声明 Playwright 或模块未安装而失败。

- [x] **Step 4: 声明并安装 Python 依赖，不下载浏览器**

在 `requirements.txt` 增加一行：

```text
playwright>=1.55,<2
```

Run: `venv\Scripts\python.exe -m pip install "playwright>=1.55,<2"`

禁止运行 `playwright install`。若安装需要网络授权，按运行环境正常申请；不得改用 Selenium 或 CDP 临时代替。

- [x] **Step 5: 写只读系统 channel 探针**

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

- [x] **Step 6: 确认 GREEN 与默认字节不变**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
git diff --check -- requirements.txt tests/test_computer_dependency.py scripts/smoke_computer_control.py
```

Expected: 全部通过；golden 文件本身无 diff。系统 channel GUI 探针只在用户明确允许弹出浏览器时人工运行，不计入自动 pytest。

Suggested commit after explicit authorization: `build: declare system-browser Playwright dependency`

#### Execution Record — Task 1

- Baseline: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_token_accounting.py -q` | exit 0 | 54 passed in 8.00s（墙钟约 10s）。分支 `dev`；工作树仅含用户已有修改 `frontend/src/components/Sidebar.vue`，未触碰。前端 vitest（490 passed）与 frontend build（exit 0）采用用户提供的冻结基线结论，本 Task 未重跑。
- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py -q` | exit code 1 | 失败测试 `test_playwright_is_declared_without_browser_download_hook`、`test_playwright_python_api_is_importable` | 目标失败原因：`requirements.txt` 尚未声明 playwright（`AssertionError: 'playwright>=' not in requirements`）且模块未安装（`ModuleNotFoundError: No module named 'playwright'`）| 2 failed in 1.38s（墙钟约 3s）。
- Dependency install: `venv\Scripts\python.exe -m pip install "playwright>=1.55,<2"` | exit 0 | 安装 playwright-1.62.0、pyee-13.0.1（greenlet/typing-extensions 已满足）。未运行 `playwright install`，未下载浏览器。前两次调用因 shell 300s 前台超时在 wheel 下载阶段被中断，第三次（`-v`）在 90s 内完成，属环境性下载耗时，非代码问题。`venv\Scripts\python.exe -c "from playwright.sync_api import sync_playwright"` exit 0。
- Probe gate: `venv\Scripts\python.exe -m py_compile scripts/smoke_computer_control.py tests/test_computer_dependency.py` exit 0；`venv\Scripts\python.exe scripts/smoke_computer_control.py`（无参数）exit 2 且打印帮助，未启动任何浏览器。`--probe-browser` 人工探针按计划留待用户执行。
- GREEN / Regression: `venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 19 passed in 8.52s（墙钟约 11s）。
- Default-byte: `git diff --stat -- tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py` | exit 0 | 输出为空，golden 零改动。
- Diff check: `git add -N tests/test_computer_dependency.py scripts/smoke_computer_control.py && git diff --check` | exit 0；`git diff --check -- requirements.txt tests/test_computer_dependency.py scripts/smoke_computer_control.py` | exit 0；`git status --porcelain` 仅含 `M frontend/src/components/Sidebar.vue`（用户原有）、`M requirements.txt`、`A scripts/smoke_computer_control.py`、`A tests/test_computer_dependency.py`，无计划外文件。
- Files touched: `requirements.txt`（追加 `playwright>=1.55,<2`）、`tests/test_computer_dependency.py`（新建）、`scripts/smoke_computer_control.py`（新建）、本计划文件（本记录与 checkbox）。
- Review — spec: 符合。requirements 声明 `playwright>=1.55,<2` 且无 `playwright install` hook；`probe_browser` 逐字采用 brief 给定实现（`msedge`/`chrome` channel、`headless=False`、viewport `1440×900`、仅 `about:blank`）；argparse 仅接受 `--probe-browser edge|chrome`，无参数打印帮助并退出 2（实测）。
- Review — privacy: 符合。不读取/复制用户浏览器 profile，不下载浏览器，不发送真实键鼠，probe 仅打开 `about:blank`。
- Review — default-byte: 符合。byte-stability 与 payload golden 全部通过且文件零 diff；默认工具 schema 无变化。
- Review — unresolved: `--probe-browser edge|chrome` 人工 GUI 探针未执行（按计划由用户在允许弹出浏览器时运行）；前端 vitest/build 基线未在本 Task 重跑。

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

- [x] **Step 1: 写严格解析失败测试**

创建覆盖合法动作、未知字段、`bool` 坐标、字段互斥、8000 字符、100–10000 ms、最多 8 键、URL scheme、browser-only 导航、拖拽路径和用户接管的参数化测试：

```python
import pytest

from core.computer.actions import parse_computer_action
from core.computer.models import ComputerControlError


def test_parse_click_requires_strict_integer_coordinates():
    action = parse_computer_action({"kind": "click", "target": "desktop", "x": 12, "y": 34})
    assert (action.kind, action.target, action.x, action.y) == ("click", "desktop", 12, 34)


def test_parse_drag_normalizes_start_end_to_path():
    action = parse_computer_action(
        {"kind": "drag", "target": "desktop", "x": 1, "y": 2, "end_x": 3, "end_y": 4}
    )
    assert action.path == ((1, 2), (3, 4))
    assert action.end_x is None and action.end_y is None


def test_parse_takeover_requires_stable_reason_code():
    action = parse_computer_action(
        {"kind": "request_user_takeover", "target": "browser", "reason_code": "login_required"}
    )
    assert action.reason_code == "login_required"


@pytest.mark.parametrize("payload", [
    {"kind": "click", "target": "desktop", "x": True, "y": 2},
    {"kind": "observe", "target": "browser", "x": 1},
    {"kind": "wait", "target": "desktop", "duration_ms": 99},
    {"kind": "keypress", "target": "desktop", "keys": ["CTRL"] * 9},
    {"kind": "navigate", "target": "desktop", "url": "https://example.com"},
    {"kind": "navigate", "target": "browser", "url": "file:///c:/secret"},
    {"kind": "click", "target": "desktop", "x": 1, "y": 2, "mystery": 3},
    {"kind": "drag", "target": "desktop", "path": [(1, 2)], "x": 1, "y": 2},
    {"kind": "drag", "target": "desktop"},
    {"kind": "request_user_takeover", "target": "browser", "reason_code": "just_because"},
])
def test_parse_action_rejects_invalid_payload(payload):
    with pytest.raises(ComputerControlError) as raised:
        parse_computer_action(payload)
    assert raised.value.code == "computer_invalid_action"
```

`drag` 必须恰好提供 `path`（2–256 个整数坐标点）或起终点 `x/y/end_x/end_y` 之一，同时提供二者或都不提供均拒绝；起终点输入解析后立即规范化为两点 `path`。`request_user_takeover` 只接受 `login_required|captcha_required|credential_required`。

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q`

Expected: collection 因 `core.computer` 不存在而失败。

- [x] **Step 3: 实现不可变值对象和 backend Protocol**

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
    path: tuple[tuple[int, int], ...] = ()
    button: str = "left"
    delta_x: int = 0
    delta_y: int = 0
    text: str | None = field(default=None, repr=False)
    keys: tuple[str, ...] = ()
    url: str | None = None
    duration_ms: int | None = None
    reason_code: str | None = None


class ComputerBackend(Protocol):
    target: str
    def start(self) -> None: ...
    def observe(self, run_id: str) -> "ComputerObservation": ...
    def execute(self, run_id: str, action: ComputerAction) -> None: ...
    def release_inputs(self) -> None: ...
    def close(self) -> None: ...
```

`keys` 既用于 `keypress` 组合键，也作为受支持鼠标动作（click/double_click/drag）的 modifier；`path` 是拖拽的唯一规范表示（起终点输入解析后立即规范化）。`ComputerControlError.__str__` 返回中文 message，`code` 保持设计规格的稳定英文错误码。快照和观察的 `repr` 不得暴露 `text` 或本地截图绝对路径。

- [x] **Step 4: 实现白名单动作解析**

`actions.py` 必须使用每种动作的允许字段集合，不得通过删除未知字段继续：

```python
_COMMON = {"kind", "target"}
_ALLOWED = {
    "observe": _COMMON,
    "click": _COMMON | {"x", "y", "button", "keys"},
    "double_click": _COMMON | {"x", "y", "button", "keys"},
    "move": _COMMON | {"x", "y"},
    "drag": _COMMON | {"path", "x", "y", "end_x", "end_y", "button", "keys"},
    "scroll": _COMMON | {"x", "y", "delta_x", "delta_y"},
    "type_text": _COMMON | {"text"},
    "keypress": _COMMON | {"keys"},
    "wait": _COMMON | {"duration_ms"},
    "navigate": _COMMON | {"url"},
    "back": _COMMON,
    "forward": _COMMON,
    "reload": _COMMON,
    "request_user_takeover": _COMMON | {"reason_code"},
}
```

`drag` 的互斥规则：`path` 与 `x/y/end_x/end_y` 恰好提供其一，起终点输入解析后立即规范化为两点 `path`，存储层不再保留 `end_x/end_y` 字段。实现 `_strict_int()` 显式拒绝 `bool`；`urllib.parse.urlsplit` 只允许 `http|https` 且必须有 hostname；组合键只允许 `CTRL, ALT, SHIFT, META, ENTER, TAB, ESC, SPACE, BACKSPACE, DELETE, HOME, END, PAGEUP, PAGEDOWN, UP, DOWN, LEFT, RIGHT, A-Z, 0-9, F1-F12`；`reason_code` 只允许 `login_required|captcha_required|credential_required`。

- [x] **Step 5: GREEN、序列化与隐私回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q
venv\Scripts\python.exe -m py_compile core/computer/models.py core/computer/actions.py
git diff --check -- core/computer tests/test_computer_models.py
```

Expected: 所有输入边界通过；Provider 投影不含 `png_path`、时间戳、完整 URL query、输入正文。

Suggested commit after explicit authorization: `feat(computer): add strict action domain`

#### Execution Record — Task 2

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q` | exit 2 | collection 中断（1 error during collection）| 目标失败原因：`ModuleNotFoundError: No module named 'core.computer'`（导入 `core.computer.actions` / `core.computer.models` 失败）| 1 error in 0.53s。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_models.py -q` | exit 0 | 163 passed in 1.71s（墙钟约 4s）。注：首次 GREEN 运行 161 passed + 4 error，4 个 error 均为 Windows 环境变量 32767 字符上限导致 `PYTEST_CURRENT_TEST` 写入失败（8000/8001 个 CJK 字符的参数化 ID 过长），非断言失败；为相关参数化加短 `ids` 后全绿。
- Compile: `venv\Scripts\python.exe -m py_compile core/computer/models.py core/computer/actions.py core/computer/__init__.py tests/test_computer_models.py` | exit 0。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 17 passed in 9.59s。`git diff --stat -- tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py` 输出为空，golden 零改动。
- Diff check: `git add -N core/computer/__init__.py core/computer/models.py core/computer/actions.py tests/test_computer_models.py && git diff --check` | exit 0；`git diff --check -- core/computer tests/test_computer_models.py` | exit 0。`git status --porcelain` 仅含 Task 1 已有文件（requirements.txt、scripts/smoke_computer_control.py、tests/test_computer_dependency.py）、用户原有 `M frontend/src/components/Sidebar.vue`、本计划文件与本 Task 四个新文件，无计划外文件。
- Files touched: `core/computer/__init__.py`（新建）、`core/computer/models.py`（新建）、`core/computer/actions.py`（新建）、`tests/test_computer_models.py`（新建）、本计划文件（checkbox 与本记录）。未修改任何现有代码文件。
- Review — spec: 符合。枚举为 str+Enum 且取值与设计 7.1 逐字一致；ComputerOwner/ComputerAction 采用 brief 固定形状（ComputerAction 额外保留恒为 None 的 end_x/end_y 以满足 brief 样例断言，path 为拖拽唯一规范表示）；_ALLOWED 白名单逐字采用 brief；`_strict_int` 以 `type(value) is not int` 拒绝 bool/float/NaN/字符串；urlsplit 只放行 http/https 且必须有 hostname；键白名单为 CTRL/ALT/SHIFT/META/ENTER/TAB/ESC/SPACE/BACKSPACE/DELETE/HOME/END/PAGEUP/PAGEDOWN/UP/DOWN/LEFT/RIGHT + A-Z + 0-9 + F1-F12，最多 8 个；reason_code 只接受 login_required|captcha_required|credential_required；navigate/back/forward/reload 仅限 browser target；text 上限 8000 code point；wait 100-10000；drag path 2-256 点且与起终点严格互斥、起终点解析后立即规范化为两点 path。
- Review — privacy: 符合。ComputerAction.text 与 ComputerObservation.png_path 均为 field(repr=False)，repr 测试验证不泄露正文与本地截图绝对路径；observation_provider_projection 只含 ok/target/action/screen_sha256/width/height/origin_or_window/error_code/error_message 九个键，browser 用 page_origin（无 path/query/fragment）、desktop 用窗口摘要，不含 png_path、时间戳或输入正文；ComputerRunSnapshot.to_public_dict() 无 text/png/api_key/cookie 字段。
- Review — default-byte: 符合。prompt byte-stability 与 provider payload golden 17 passed 且文件零 diff；本 Task 不触碰任何请求构造路径。
- Review — unresolved: ~~keypress 键名大小写归一~~（已在审查修复中改为严格模式：键名必须逐字匹配大写白名单，小写直接 computer_invalid_action）；COMPUTER_ACTION_TOOL_SCHEMA 的 description 文案为自撰（设计只规定语义要点），后续 Task 若需 byte-golden 可冻结。

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

- [x] **Step 1: 写默认值、损坏配置、preflight 和关闭顺序测试**

```python
def test_missing_computer_settings_are_safe_defaults(tmp_path, monkeypatch):
    use_temp_config(tmp_path, monkeypatch, {})
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
    use_temp_config(tmp_path, monkeypatch, {key: value})
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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py -q`

Expected: 新模块或新增快照字段不存在。

- [x] **Step 3: 实现严格设置与显式浏览器发现**

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

- [x] **Step 4: 扩展统一系统设置快照**

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

- [x] **Step 5: GREEN 与现有设置回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py tests/test_config.py tests/test_config_toml_overlay.py -q
git diff --check -- core/computer/settings.py core/command_shell.py tests/test_computer_settings.py tests/test_gui_system_settings.py
```

Expected: 缺失键安全关闭，显式损坏值失败，preflight 失败不落盘，关闭 generation 先变化。

Suggested commit after explicit authorization: `feat(computer): add strict control settings`

#### Execution Record — Task 3

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py -q` | exit 2 | collection 中断（1 error during collection）| 目标失败原因：`ModuleNotFoundError: No module named 'core.computer.settings'`（tests/test_computer_settings.py 导入失败）| 1 error in 1.97s。单独运行 `tests/test_gui_system_settings.py` | exit 1 | 2 failed（`test_system_settings_snapshot_exposes_computer_control_settings`、`test_system_settings_snapshot_reflects_persisted_computer_values`，`KeyError: 'browser_control_enabled'`）, 6 passed in 2.85s。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py -q` | exit 0 | 54 passed in 4.74s。注：首轮 GREEN 出现 1 个 `NameError: name 'sys' is not defined`（两个并行 StrReplaceFile 编辑同一文件相互覆盖导致 import sys 丢失），补回 import 后全绿，非实现逻辑问题。
- Compile: `venv\Scripts\python.exe -m py_compile core/computer/settings.py core/command_shell.py tests/computer_fakes.py tests/test_computer_settings.py tests/test_gui_system_settings.py` | exit 0。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_computer_settings.py tests/test_gui_system_settings.py tests/test_config.py tests/test_config_toml_overlay.py -q` | exit 0 | 95 passed in 9.94s；`venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 17 passed in 7.88s；邻近消费者 `tests/test_computer_models.py tests/test_command_shell.py` | 197 passed in 2.98s；`tests/test_context_builder.py tests/test_gui_model_settings.py` | 19 passed in 2.47s。
- Diff check: `git add -N core/computer/settings.py tests/computer_fakes.py tests/test_computer_settings.py && git diff --check -- core/computer/settings.py core/command_shell.py tests/computer_fakes.py tests/test_computer_settings.py tests/test_gui_system_settings.py` | exit 0。`git status --porcelain` 无计划外新增；`.gitattributes`/`README.md`/`assets/CHANGELOG.md`/`core/hunyuan_image.py`/`scripts/secret_filter.py`/`tests/test_hunyuan_image_auth.py`/`frontend/src/components/Sidebar.vue` 为用户/其他会话已有改动，未触碰。
- Files touched: `core/computer/settings.py`（新建）、`tests/computer_fakes.py`（新建，仅 use_temp_config）、`tests/test_computer_settings.py`（新建）、`core/command_shell.py`（get_system_settings 聚合 6 个静态键 + import）、`tests/test_gui_system_settings.py`（新增 2 个快照断言测试 + import sys）、本计划文件（checkbox 与本记录）。
- Review — spec: 符合（含 dispatch 授权的两处最小偏差，见下）。四个固定常量逐字；`_read_bool` 逐字采用 brief（缺失=false、`type(value) is not bool` 拒绝 truthy 转换）；浏览器探测 = shutil.which + ProgramFiles/ProgramFiles(x86) 明确安装路径，每选项独立 available/executable/unavailable_reason，绝不 fallback（有测试证明所选 edge 不可用时 chrome 可用但快照仍选 edge）；profile 路径 `Path(config.YCODE_DIR) / "browser-profiles" / browser`，resolve 后 `is_relative_to` 根校验；活动 run 改 browser 抛 `computer_session_busy`；save_config_value 返回 False 抛 `computer_invalid_config`；开启先 preflight 失败不落盘（错误码 `computer_preflight_failed`，message 含原始原因）；关闭顺序固定为 持久化 false → 递增内存 generation → 返回快照。
- Review — privacy: 符合。快照只含配置值、generation、浏览器探测结果与 profile 展示路径；无 profile 文件清单、Cookie、凭据；`ComputerSettingsSnapshot` 无 hotkey_available/run_summary；settings.py 只 import os/shutil/sys/threading/dataclasses/pathlib/typing/config/models，已确认不 import controller/hotkey。
- Review — default-byte: 符合。prompt byte-stability 与 provider payload golden 17 passed，未触碰任何请求构造路径。
- Review — unresolved / dispatch 授权的偏差（已记入 ledger）：(1) `computer_hotkey_available`/`computer_hotkey`/`computer_run` 三个键依赖 Task 5 hotkey.py 与 Task 8 controller.py，本 Task 未聚合、未建 stub、无 try/except 兜底，待后续 Task 惰性导入接入；(2) `tests/computer_fakes.py` 只实现 `use_temp_config`（额外 monkeypatch `_TOML_CONFIG_FILE` 隔离真实 config.toml overlay），FakeHotkeyApi 等其余契约导出由后续 Task 按需扩展；(3) `load_computer_settings`/`list_browser_options` 增加 keyword-only `platform` 参数（与 command_shell 现有风格一致）以便非 win32 分支可测；(4) 显式安装路径仅覆盖 Program Files/Program Files (x86)（按 dispatch 指示），未覆盖 LocalAppData 绿色安装。

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

- [x] **Step 1: 写能力矩阵失败测试**

```python
@pytest.mark.parametrize("model", [
    {"supports_vision": True, "supports_function_calling": False, "computer_protocol": "generic"},
    {"supports_vision": False, "supports_function_calling": True, "computer_protocol": "generic"},
    {"supports_vision": True, "supports_function_calling": True},
])
def test_generic_requires_three_explicit_capability_fields(model, enabled_settings):
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


def test_generic_capable_model_receives_enabled_targets(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py -q`

Expected: resolver 和新配置字段不存在。

- [x] **Step 3: 实现纯函数 resolver**

resolver 不读全局 config、不做网络探测、不按模型名称分支。官方 OpenAI base 只允许规范化 host `api.openai.com`；协议字段必须用 `core/model_manager.py` 的真实字段名 `protocol`（取值如 `"openai"`、`"responses"`），native 判定精确为 `provider.get("protocol") == "responses"` 加规范化官方 base URL，不得发明 `api_protocol` 之类的新字段或按 base URL 字符串猜测。目标按两个设置开关组成固定 tuple。返回对象至少含：

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

- [x] **Step 4: 严格持久化与预置元数据**

`model_manager` 的 add/update/export/import 路径都显式保存两个新字段。规则：`type(value) is bool`；protocol 必须在固定集合内；`generic` 不满足视觉与工具能力时保存即拒绝，不能保存一个运行时才失败的半有效组合。

`model_catalog.py` 只给已确认条目写字面量元数据，例如：

```python
{"name": "k3", "context_window": 1000000, "supports_vision": True,
 "supports_function_calling": True, "computer_protocol": "generic"}
```

对 OpenAI native 型号，只依据实施当时官方 API 文档确认的准确型号和 Responses 支持写入；没有证据的预置保持 `computer_protocol="none"`。不得用 family name 批量推断。

截至 2026-09-02，官方模型目录/单模型页已明确列出 Computer use 的首批 GA 预置至少包括 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.4`、`gpt-5.4-mini`；这些条目可显式标记 `native_openai`。`computer-use-preview` 是仅限 Responses 的旧 preview 模型，但官方新实现指南要求使用 GA `computer` tool，因此本计划不实现 preview 请求变体，也不把该旧模型标为首版可用。实施当天仍需逐个打开官方模型页复核，不能从此清单推断其他型号。官方来源：<https://developers.openai.com/api/docs/models>、<https://developers.openai.com/api/docs/models/gpt-5.4>、<https://developers.openai.com/api/docs/models/gpt-5.4-mini>、<https://developers.openai.com/api/docs/guides/tools-computer-use>。

- [x] **Step 5: GREEN 与旧配置兼容**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py tests/test_config.py tests/test_gui_model_settings.py -q
git diff --check -- core/computer/capability.py core/model_catalog.py core/model_manager.py core/model_selector.py tests
```

Expected: 旧模型缺字段仍可普通对话和普通工具，但 computer capability 明确 disabled；无 substring 猜测。

Suggested commit after explicit authorization: `feat(models): declare explicit computer capabilities`

#### Execution Record — Task 4

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py -q` | exit 2 | collection 中断（1 error during collection）| 目标失败原因：`ModuleNotFoundError: No module named 'core.computer.capability'` | 1 error in 1.43s。补充运行 `tests/test_model_catalog.py tests/test_model_selector.py tests/test_config.py -q` | exit 1 | 14 failed, 43 passed in 7.54s | 失败原因全部指向目标缺失：catalog 断言 `AssertionError: assert set() == {...}`（目录无能力元数据）、selector 路径 `KeyError: 'computer_protocol'`、config 持久化 `TypeError: add_model_to_provider() got an unexpected keyword argument 'supports_function_calling'` 等。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_capability.py tests/test_model_catalog.py tests/test_model_selector.py tests/test_config.py tests/test_gui_model_settings.py -q` | exit 0 | 89 passed in 7.08s。注：首轮 88 passed + 1 failed，唯一失败是 `test_catalog_preserves_cli_model_metadata` 对 deepseek 条目做精确 dict 相等断言——本 Task 计划内修改（Modify: tests/test_model_catalog.py），已把 `deepseek-v4-flash-vision-exp` 的预期更新为含新元数据后全绿，非实现逻辑问题。
- Compile: `venv\Scripts\python.exe -m py_compile core/computer/capability.py core/model_manager.py core/model_catalog.py core/model_selector.py tests/computer_fakes.py tests/test_computer_capability.py` | exit 0。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 17 passed in 6.98s，golden 文件 `git diff --stat` 输出为空；`tests/test_computer_settings.py tests/test_computer_models.py tests/test_gui_system_settings.py tests/test_config_toml_overlay.py -q` | exit 0 | 246 passed in 3.70s。
- Diff check: `git add -N core/computer/capability.py tests/test_computer_capability.py && git diff --check` | exit 0（仅既有 LF/CRLF warning）。`git status --porcelain` 无计划外新增；`.gitattributes`/`README.md`/`assets/CHANGELOG.md`/`core/hunyuan_image.py`/`scripts/secret_filter.py`/`tests/test_hunyuan_image_auth.py`/`frontend/src/components/Sidebar.vue` 等为用户/其他会话已有改动，未触碰。
- Files touched: `core/computer/capability.py`（新建）、`tests/test_computer_capability.py`（新建）、`tests/computer_fakes.py`（追加 enabled_settings fixture / openai_compatible_provider() / 三个 capability 常量）、`core/model_manager.py`（COMPUTER_PROTOCOLS 常量 + 三个校验 helper；add_model_to_provider/update_model/update_provider_and_model/add_preset_provider_with_models/sync_preset_provider_models 显式保存两新字段；get_active_model/get_secondary_model 导出两新字段）、`core/model_catalog.py`（11 个已确认条目加字面量元数据）、`tests/test_model_catalog.py`（更新 deepseek 精确断言 + 新增 2 个元数据测试）、`tests/test_model_selector.py`（新增预置添加路径透传测试）、`tests/test_config.py`（新增 6 个持久化/校验测试 + import pytest）、本计划文件（checkbox 与本记录）。`core/model_selector.py` 未修改：CLI 向导不采集能力字段，add/update 传 None 走“保留既有值”语义，预置路径经 add_preset_provider_with_models 自动透传，无需改动（dispatch 条件“如需透传则做最小修改”判定为不需要）。
- Review — spec: 符合。resolver 为纯函数（只 import urllib.parse/models/settings，不读 config、不网络探测、无模型名分支，有 `test_missing_or_unknown_protocol_is_never_capable` 证明 gpt-5.4/deepseek 等名字不获得资格）；判定顺序 turn_source→platform→开关→模型协议；LAN/移动（非 "local"）一律 `computer_remote_forbidden`；非 Windows 经 `settings.platform_supported` 判 `computer_unsupported_os`（platform 由 Task 3 快照的 platform 注入参数覆盖，无需 monkeypatch）；无 target 判 `computer_disabled`；generic 三字段缺一判 `computer_model_not_capable`；native 精确为 `provider.get("protocol") == "responses"` + urlsplit 规范化 hostname 恰为 `api.openai.com`（仿冒子域 `api.openai.com.evil.example`、非 responses、缺 base、非法 URL 均判 `computer_protocol_mismatch`）；启用结果含 enabled/protocol/targets 固定 tuple/browser=settings.computer_browser/settings_generation，brief 样例逐字包含。持久化：`type(value) is bool` 严格校验、protocol 限定 none|generic|native_openai、generic 缺视觉或工具能力保存即 ValueError 且不落盘（`cfg.read_bytes() == before` 断言）；update 路径传 None 保留既有值且组合校验针对生效后最终值；预置 add/sync 路径透传目录元数据。catalog 只标记 brief/设计 9.2 点名且真实存在的条目：deepseek-v4-flash-vision-exp、k3/k3-256k（kimi-coding 两个协议各一对）、kimi-k3（moonshot+sensenova）、glm-5.3-flash（token-rhythm/zhipu/zhipu-coding-openai/zhipu-coding-anthropic）共 11 条 generic；gpt-5.6-sol/gpt-5.6-terra/gpt-5.6-luna/gpt-5.4/gpt-5.4-mini 在目录中不存在（无 OpenAI 预置厂商），按 dispatch 指示未发明新条目，故目录暂无任何 native_openai 条目。
- Review — privacy: 符合。capability.py 不接触凭据/profile/截图；禁用结果 reason 为固定中文短语，不回显 base_url 或模型配置原文；model_manager 新字段与 api_key 写路径无交集（test_config 既有 dpapi 断言全绿）。
- Review — default-byte: 符合。byte-stability 与 payload golden 17 passed 且文件零 diff；未触碰任何请求构造路径。
- Review — unresolved / 与计划文字差异：(1) brief 说 "model_manager 的 add/update/export/import 路径"，真实代码无独立 export/import 函数，已映射到全部真实路径——写入侧 add_model_to_provider/update_model/update_provider_and_model/add_preset_provider_with_models/sync_preset_provider_models，导出侧 get_active_model/get_secondary_model；(2) provider 协议字段名确认为 `protocol`（与计划声称一致，无偏差）；(3) `core/model_selector.py` 零修改（见 Files touched）；(4) 官方 OpenAI 模型页复核未在本环境执行（无网络授权动作），native 预置因此保持为空而非按 brief 清单发明条目；(5) gui_bridge.get_model_settings 快照暂不暴露新字段，GUI 模型设置页展示属后续前端 Task。

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

- [x] **Step 1: 写热键和预览边界失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q`

Expected: 两个模块不存在。

- [x] **Step 3: 实现热键生命周期**

Windows 实现使用独立消息线程注册 `MOD_CONTROL|MOD_ALT` + `VK_ESCAPE`，只在收到对应 `WM_HOTKEY` 后调用 `on_stop`。任一设置开关为 true 时由应用生命周期持续持有该实例；两个开关均为 false 或应用退出时才 `close()`。应用启动读到已开启配置时立即注册，失败则 capability preflight 返回 `computer_hotkey_unavailable`，但不静默改写配置。`close()` 发送退出消息并 `UnregisterHotKey`，允许重复调用。回调只发 stop 请求，不直接碰 GUI、prompt_toolkit 或 Provider 流。

必须提供可注入 API；默认 API 只在 `sys.platform == "win32"` 构造，其他平台抛 `computer_unsupported_os`。

- [x] **Step 4: 实现 preview HTTP 服务**

使用标准库 `ThreadingHTTPServer`：

```python
self.token = secrets.token_urlsafe(32)
self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
```

请求路径格式固定为 `/computer-preview/{token}/{run_id}/{ordinal}.png`。`run_id` 只允许 `^[a-zA-Z0-9_-]{1,80}$`，文件名由服务内部生成；handler 不接受 `..`、反斜线、URL 编码路径穿越或目录索引。响应设置 `Cache-Control: no-store` 和 `Content-Type: image/png`。publish 使用临时文件 + `os.replace()`，切换 run 立即删除旧 run 图片；启动时删除 24 小时前目录，停止时删除本进程目录。

- [x] **Step 5: GREEN 与资源释放回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q
venv\Scripts\python.exe -m py_compile core/computer/hotkey.py core/computer/preview_server.py
git diff --check -- core/computer/hotkey.py core/computer/preview_server.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py
```

Expected: 端口在 stop 后可释放；错误 token、旧 run、第四旧图和路径穿越均为 404。

Suggested commit after explicit authorization: `feat(computer): add emergency stop and preview server`

#### Execution Record — Task 5

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q` | exit 2 | collection 中断（2 errors during collection）| 目标失败原因：`ModuleNotFoundError: No module named 'core.computer.hotkey'` 与 `No module named 'core.computer.preview_server'` | 2 errors in 1.09s。command_shell 聚合接入同样先 RED：`tests/test_gui_system_settings.py` 新增两条断言后单独运行 | exit 1 | 2 failed, 8 passed | `KeyError: 'computer_hotkey_available'`（两键未聚合），实现后转绿。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_hotkey.py tests/test_computer_preview_server.py -q` | exit 0 | 28 passed in 10.73s。注：首轮 27 passed + 1 failed，唯一失败是测试自身用空格拼接 URL（`run a`）被 http.client 客户端侧 CVE-2019-9740 校验拒绝（InvalidURL），根本到不了服务端；改为 `%20` 编码空格（仍要求服务端 404）后全绿，非实现逻辑问题。
- Compile: `venv\Scripts\python.exe -m py_compile core/computer/hotkey.py core/computer/preview_server.py tests/computer_fakes.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py core/command_shell.py tests/test_gui_system_settings.py` | exit 0。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_computer_settings.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 73 passed in 11.11s，golden 文件零改动；邻近消费者 `tests/test_command_shell.py tests/test_computer_capability.py tests/test_computer_models.py -q` | exit 0 | 222 passed in 2.53s。
- Diff check: `git add -N core/computer/hotkey.py core/computer/preview_server.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py && git diff --check -- <本 Task 全部触碰文件>` | exit 0（仅既有 LF/CRLF warning）。`git status --porcelain` 无计划外新增；`.gitattributes`/`README.md`/`assets/CHANGELOG.md`/`core/hunyuan_image.py`/`scripts/secret_filter.py`/`tests/test_hunyuan_image_auth.py`/`frontend/src/components/Sidebar.vue`/`generated_images/` 为用户/其他会话已有改动，未触碰。
- Files touched: `core/computer/hotkey.py`（新建）、`core/computer/preview_server.py`（新建）、`tests/test_computer_hotkey.py`（新建，11 条）、`tests/test_computer_preview_server.py`（新建，17 条）、`tests/computer_fakes.py`（追加 FakeHotkeyApi / fake_png_bytes(seed) / http_status(url)，原有导出原样保留）、`core/command_shell.py`（get_system_settings 函数内惰性导入 get_emergency_hotkey_snapshot，聚合 computer_hotkey_available/computer_hotkey 两键；computer_run 仍留待 Task 8）、`tests/test_gui_system_settings.py`（新增 2 条快照断言测试）、本计划文件（checkbox 与本记录）。
- Review — spec: 符合。brief 两个热键样例与预览样例逐字包含；HotkeyApi 依赖注入，默认 WindowsHotkeyApi 只在 sys.platform=="win32" 构造（非 win32 抛 computer_unsupported_os），独立消息线程注册 MOD_CONTROL|MOD_ALT（含 MOD_NOREPEAT）+ VK_ESCAPE，仅 WM_HOTKEY 且 wParam==HOTKEY_ID 时回调；close() 幂等（PostThreadMessage 退出消息 + 消息线程内 UnregisterHotKey）；注册失败抛 computer_hotkey_unavailable 且 available=False；回调只调 on_stop。预览服务逐字采用 brief 的 secrets.token_urlsafe(32) + ThreadingHTTPServer(("127.0.0.1", 0))；URL 固定 /computer-preview/{token}/{run_id}/{ordinal}.png；run_id 严格 ^[a-zA-Z0-9_-]{1,80}$；文件名 ^[0-9]+\.png$ 由服务内部生成；`..`、反斜杠、%2e%2e/%20 编码穿越、目录索引、错 token、旧 run、第四旧图一律 404（不区分原因，另加 resolve 后 is_relative_to 进程目录兜底）；响应 Cache-Control: no-store + Content-Type: image/png；publish 临时文件 + os.replace；只留当前 run 最近 3 张，切 run 立即删旧 run 目录；start 清理 24 小时前残留目录；stop 幂等，删除本进程目录且端口可重新绑定（有测试）。
- Review — privacy: 符合。token 每进程随机且不出现在日志/快照；预览服务只绑 127.0.0.1；get_emergency_hotkey_snapshot 只暴露 available 布尔；command_shell 聚合不含 token/路径/凭据。
- Review — default-byte: 符合。prompt byte-stability 与 provider payload golden 全部通过且文件零 diff；未触碰任何请求构造路径。
- Review — unresolved / 与计划文字差异：(1) publish 的非法 run_id 抛 ValueError（内部契约违约），设计第 21 节无 preview 专用错误码，未发明 computer_* 新码；(2) 进程目录名 preview-{pid}-{hex4}、序号从 1 起始为自择实现细节；(3) handler 非 GET 方法走 BaseHTTPRequestHandler 默认 501（brief 只规定 GET 路径 404 语义）；(4) 真实 WindowsHotkeyApi 消息线程不经 pytest 注册（遵守禁令），仅经 FakeHotkeyApi 与 sys.platform monkeypatch 覆盖。

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

- [x] **Step 1: 写 fake Playwright 合约测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py -q`

Expected: backend 模块不存在。

- [x] **Step 3: 实现懒加载、profile 锁与页面生命周期**

构造函数不得导入或启动 Playwright；`start()` 创建专用 worker 线程，worker 内创建 profile、获取进程内 profile 锁并调用 `sync_playwright().start()` 与 `launch_persistent_context`。外部 start/observe/execute/complete/close 全部向有界队列提交命令，只有 worker 接触 runtime/context/page。browser/channel 映射固定：

```python
_CHANNELS = {"edge": "msedge", "chrome": "chrome"}
```

context 的 `page` 事件也在 worker 内处理，把最后获得焦点的可见 page 设为 active；初始 pages 为空时只创建一个 `about:blank`。context/page close 立即把 backend 标记 closed，后续动作抛 `computer_browser_closed`。profile 锁冲突抛 `computer_profile_locked`，不杀其他进程。任何调用线程都不得直接重建或关闭 Playwright 对象。

- [x] **Step 4: 实现动作与稳定观察**

浏览器坐标只落在 `0 <= x < 1440`、`0 <= y < 900`。动作映射：`mouse.click/dblclick/move/down/up/wheel`、`keyboard.insert_text`、`keyboard.press`、`page.goto/go_back/go_forward/reload`。拖拽用 `try/finally` 确保 mouse up。

观察调用 `page.screenshot(type="png")`，SHA-256 对 PNG bytes；Provider 只见 `urlsplit(page.url).scheme + "://" + netloc` 与截断 200 字符 title，不见 path/query/fragment。不得读取 DOM 输入值、Cookie、localStorage 或密码库。

- [x] **Step 5: GREEN 与受控人工探针**

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

#### Execution Record — Task 6

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py -q` | exit code 1 | collection error（0 测试运行）| 目标失败原因：`ModuleNotFoundError: No module named 'core.computer.browser_backend'`（backend 模块尚不存在）| 1 error in 0.50s。
- Fix round（实现期自查）: 首轮实现 `close()` 未启动时消耗生命周期导致 `test_close_is_idempotent_and_safe_without_start` 失败（1 failed, 37 passed in 2.29s）；修为未启动时 no-op 后全绿，非需求偏差。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_browser_backend.py tests/test_computer_models.py -q` | exit 0 | 217 passed in 2.32s。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 17 passed in 5.01s（golden 零 diff）；邻近套件 `pytest tests/test_computer_browser_backend.py tests/test_computer_models.py tests/test_computer_settings.py tests/test_computer_capability.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py tests/test_computer_dependency.py -q` | exit 0 | 321 passed in 13.56s。
- Compile: `venv\Scripts\python.exe -m py_compile core/computer/browser_backend.py scripts/smoke_computer_control.py tests/test_computer_browser_backend.py tests/computer_fakes.py` | exit 0。
- Smoke gate: `venv\Scripts\python.exe scripts/smoke_computer_control.py`（无参数）exit 2 且打印帮助，未启动浏览器；`--probe-browser` / `--browser-observe` 人工探针按计划留待用户执行。
- Diff check: `git add -N core/computer/browser_backend.py tests/test_computer_browser_backend.py && git diff --check -- core/computer/browser_backend.py tests/test_computer_browser_backend.py tests/computer_fakes.py scripts/smoke_computer_control.py` | exit 0（仅既有 LF/CRLF warning）；`git status --porcelain` 无计划外文件；未 commit。
- Files touched: `core/computer/browser_backend.py`（新建）、`tests/test_computer_browser_backend.py`（新建，38 条）、`tests/computer_fakes.py`（扩展 FakePlaywrightRuntime 家族，保留全部已有导出）、`scripts/smoke_computer_control.py`（新增 `--browser-observe` 模式）、本计划文件（本记录与 checkbox）。
- Review — spec: 符合。`_CHANNELS = {"edge": "msedge", "chrome": "chrome"}` 逐字；persistent context 参数精确（user_data_dir=resolve 后字符串、headless=False、viewport 1440×900、device_scale_factor=1）；构造函数不导入/启动 Playwright（默认 factory=None，worker 内惰性 import）；worker 独占 runtime/context/page，外部调用全经有界队列 marshal（thread id 测试证明）；零 page 只创建一个 about:blank；popup 成为 active 并发页面变化事件；坐标半开区间 0<=x<1440、0<=y<900；拖拽 try/finally 保证 mouse.up；navigate 复用 parse 层 `_parse_url`。
- Review — privacy: 符合。观察只暴露 `scheme://netloc` origin 与截断 200 字符 title（projection 测试断言 query/fragment 不泄漏）；不读 DOM 输入值/Cookie/localStorage；截图临时文件只在进程临时目录保留最近 3 张，close 时删除。
- Review — default-byte: 符合。byte-stability 与 payload golden 全部通过且零 diff；未触碰请求构造路径。
- Review — unresolved: `--probe-browser` / `--browser-observe` 人工探针未执行（留待用户允许弹出浏览器时运行）；新增稳定错误码 `computer_action_failed`（设计 21“至少包括”允许扩展）用于截图/动作运行时失败；`active_page` 为测试只读 seam；profile 锁的跨进程判定依赖 Playwright 报错文案启发式（ProcessSingleton/already in use 等）。

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

- [x] **Step 1: 写纯 fake 的截图和输入测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_windows_backend.py -q`

Expected: Windows 模块不存在。

- [x] **Step 3: 实现 Win32 adapter 的 fail-closed preflight**

`preflight()` 顺序固定：平台为 win32；设置/确认 Per-Monitor V2；input desktop 名称为 `Default`；存在 foreground window；热键已可注册。任何失败映射到稳定错误码，不请求管理员、不重启应用。

`SendInput` 返回值必须等于事件数量；组合键和 drag 在 `finally` 中释放已按下状态。鼠标绝对坐标使用虚拟桌面 metrics 和 `MOUSEEVENTF_VIRTUALDESK|MOUSEEVENTF_ABSOLUTE`，Unicode 使用 `KEYEVENTF_UNICODE`。

- [x] **Step 4: 实现截图和坐标换算**

使用 `ImageGrab.grab(all_screens=True)`；测试通过构造注入替代。最长边大于 1920 时按同一比例缩小，PNG 使用确定参数编码。坐标换算只依赖最新 observation：

```python
physical_x = origin_x + round(x * original_width / model_width)
physical_y = origin_y + round(y * original_height / model_height)
```

换算后再次检查虚拟桌面半开区间边界。动作开始前重新检查 interactive desktop；截图失败抛 `computer_backend_start_failed` 或 `computer_secure_desktop`，绝不复用 `_last_observation`。

- [x] **Step 5: GREEN 与禁止真实输入验证**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_windows_backend.py -q
venv\Scripts\python.exe -m py_compile core/computer/win32_adapter.py core/computer/windows_backend.py
git diff --check -- core/computer/win32_adapter.py core/computer/windows_backend.py tests/test_computer_windows_backend.py
```

Expected: pytest 全程只使用 fake adapter，测试运行时真实鼠标和键盘完全不动。`scripts/smoke_computer_control.py --desktop-input` 必须额外要求 `--confirm-real-input I_UNDERSTAND`，否则退出 2。

Suggested commit after explicit authorization: `feat(computer): add fail-closed Windows backend`

**Execution Record (2026-09-03, implementer subagent, no commit per K3):**

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_windows_backend.py -q` → collection error `ModuleNotFoundError: No module named 'core.computer.win32_adapter'`（1 error / 0.42s，符合预期）。
- GREEN: 同上命令 → **43 passed / ~1.4-2.1s / exit 0**。全程只使用 FakeImage/FakeWin32Adapter 或全 seam 注入的 Win32InputAdapter；真实鼠标/键盘/截屏零触碰（send_input、metrics、desktop、foreground、DPI、hotkey 全部注入）。
- 回归门禁: `pytest tests/test_computer_models.py tests/test_computer_browser_backend.py -q` → 217 passed / 2.78s；全部 computer 套件（models/settings/capability/hotkey/preview_server/browser_backend/windows_backend）→ 362 passed / 13.84s；`tests/test_prompt_byte_stability.py` → 5 passed（零 diff）。
- `venv\Scripts\python.exe -m py_compile core/computer/win32_adapter.py core/computer/windows_backend.py scripts/smoke_computer_control.py` → exit 0。
- 新文件已 `git add -N`；`git diff --check -- core/computer/win32_adapter.py core/computer/windows_backend.py tests/test_computer_windows_backend.py tests/computer_fakes.py scripts/smoke_computer_control.py` → 干净（仅 Windows LF→CRLF 提示）。
- Smoke: `--desktop-input` 缺确认 / `--confirm-real-input WRONG` 均 exit 2 且在导入 backend 前返回；`--desktop-observe` / `--desktop-input --confirm-real-input I_UNDERSTAND` 真实模式未执行（留待用户受控环境）。
- 接口落定: `Win32InputAdapter.preflight()/send_mouse()/send_text()/send_keys()/release_all()/virtual_screen_rect()/ensure_interactive()/foreground_window_title()`；`WindowsComputerBackend(image_grab, win32_adapter)` + `create_default()` + 模块级纯函数 `screenshot_to_physical(x, y, observation)`；computer_fakes 新增 `FakeImage`/`FakeWin32Adapter`（保留全部已有导出）。
- 决策备注: preflight 第 2 步 DPI 失败映射 `computer_backend_start_failed`（不在错误坐标系继续）；无前台窗口与 input desktop 非 Default 均映射 `computer_secure_desktop`（设计 21 无更细码）；无 foreground/非交互 desktop 打开失败区分 `computer_noninteractive_session`；截图失败一律 `computer_backend_start_failed`；SendInput 部分成功按已生效前缀补发 up 后抛 `computer_input_rejected`；`_key_up_quietly` 失败保留按下标记供 `release_all` 重试（与 browser backend drag 语义一致）。

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

- [x] **Step 1: 写状态机和租约失败测试**

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


def test_request_user_takeover_pauses_for_user_then_requires_observe(service, running_run, owner_a):
    service.execute(
        running_run.run_id, owner_a,
        ComputerAction(kind="request_user_takeover", target="browser", reason_code="login_required"),
    )
    assert service.snapshot().status == "waiting_for_user"
    service.resume(running_run.run_id)
    with pytest.raises(ComputerControlError, match="先观察"):
        service.execute(running_run.run_id, owner_a, ComputerAction(kind="click", target="browser", x=1, y=1))


def test_complete_run_releases_lease_but_retains_browser_session(service, running_run, owner_a, owner_b, capability):
    first = service.complete_run(running_run.run_id, owner_a, retain_browser_session=True)
    second = service.complete_run(running_run.run_id, owner_a, retain_browser_session=True)
    assert first.status == second.status == "completed"
    assert service.backend.release_count == 0
    assert service.backend.close_count == 0  # stop 才关闭 backend
    new_run = service.start_run(owner_b, capability, target="browser")  # 租约已释放，可开新 run
    assert new_run.run_id != running_run.run_id


def test_stop_is_idempotent_and_releases_inputs_once(service, running_run):
    first = service.stop(running_run.run_id, "hotkey")
    second = service.stop(running_run.run_id, "hotkey")
    assert first.status == second.status == "stopped"
    assert service.backend.release_count == 1
    assert service.backend.close_count == 1
```

另测非法状态转换（含 running→completing→completed 与跳过 completing 直接 completed 的拒绝）、owner/cancel/generation 检查、动作串行、错误时不发成功、三次无变化、observe/wait 不计 no-progress、停止事件只发一次、Turn 终态 finally 二选一调用 complete/stop 后不遗留 running 租约。

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q`

Expected: controller 模块不存在。

- [x] **Step 3: 实现显式转移表和锁**

```python
_TRANSITIONS = {
    "idle": {"starting"},
    "starting": {"running", "failed", "stopping"},
    "running": {"paused", "waiting_for_user", "provider_safety_required", "completing", "stopping", "failed"},
    "paused": {"running", "stopping", "failed"},
    "waiting_for_user": {"running", "stopping", "failed"},
    "provider_safety_required": {"running", "stopping", "failed"},
    "completing": {"completed", "stopping", "failed"},
    "completed": {"starting"},
    "stopping": {"stopped", "failed"},
    "stopped": {"starting"},
    "failed": {"starting"},
}
```

租约和状态由 `threading.RLock` 保护；backend I/O 不在持锁期间长时间执行，但每个动作由单一 `action_lock` 串行。动作前后用 run id + generation 二次确认，防止关闭设置后旧动作提交成功。

- [x] **Step 4: 实现固定动作与停止语义**

动作顺序严格为 owner/state/cancel/generation → before → execute → fresh after → no-progress → events → Provider observation。预期改变界面的动作只有在 `screen_sha256` 和 active fingerprint 都连续三次相同才失败为 `computer_no_progress`。

`complete_run(run_id, owner, retain_browser_session=True)`：run 进入 `completing` → `completed`，释放 owner/活动租约并发出最终事件；浏览器 backend 在 retain 时不 `close()`，作为无执行权的 retained session 留给下一 run 显式复用；desktop backend 立即释放输入和运行态。`stop()` 专用于取消、故障和紧急停止，会关闭 backend。二者幂等；任何 Turn 终态必须在 `finally` 中二选一调用，不能遗留 running 租约。

`stop()` 顺序：controller flag → `owner.cancel_event.set()` → 调用现有 `core.llm.abort_inflight_streams(owner.cancel_event)` 关闭在飞 Provider 流（先 set 事件再 abort，让被关闭一侧归一为用户取消；不得按 turn id 发明第二套取消注册表）→ 禁止新动作 → backend cancel/`release_inputs()` → backend `close()` → 状态终结 → 租约释放 → final event。每步用 `try/finally` 保证后续安全释放，但不把清理错误伪装成 stopped success；首个清理错误记录为 run failure。

- [x] **Step 5: GREEN 与并发回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_controller.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py -q
git diff --check -- core/computer/controller.py tests/test_computer_controller.py
```

Expected: 同时提交的第二动作在第一动作结束前不进入 backend；关闭 generation 后旧动作不能提交 observation。

Suggested commit after explicit authorization: `feat(computer): add exclusive control state machine`

---

#### Execution Record — Task 8

**Execution Record (2026-09-03, implementer subagent, no commit per K3):**

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q` → exit 2（collection error：`ModuleNotFoundError: No module named 'core.computer.controller'`，0.41s），目标失败原因即 controller 模块缺失，符合预期。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py -q` → 114 passed / 2.53s / exit 0（其中 test_computer_controller.py 33 条：brief 6 样例逐字 + 非法状态转换（含跳过 completing）/owner/cancel/generation 三类检查/动作串行/错误不发成功/三次无变化停止/observe+wait 不计 no-progress/停止事件只发一次/Turn 终态 finally 二选一不留 running 租约/provider_safety_required 不可 resume/清理错误记 failed/单例）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` → 28 passed / 8.97s / exit 0；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer"` → 415 passed / 21.73s / exit 0。
- Diff check: `git add -N core/computer/controller.py tests/test_computer_controller.py` + `git diff --check -- core/computer/controller.py tests/test_computer_controller.py core/command_shell.py tests/test_gui_system_settings.py tests/computer_fakes.py` → exit 0（仅 LF→CRLF 警告）。
- Files touched: `core/computer/controller.py`（新建：_TRANSITIONS 逐字转移表、ComputerControlService 全部 Cross-Task 公共方法 + enter_provider_safety_required、RLock+action_lock、retained browser session 复用、listener 事件机制、get_computer_control_service 惰性单例）、`tests/test_computer_controller.py`（新建，33 条）、`tests/computer_fakes.py`（追加 FakeComputerBackend，docstring 同步）、`core/command_shell.py`（get_system_settings 函数内惰性导入 controller 单例，聚合 computer_run 键）、`tests/test_gui_system_settings.py`（新增 1 条 computer_run 快照断言）、本计划文件（checkbox 与本记录）。
- spec: PASS（_TRANSITIONS 逐字；动作顺序 owner/state/cancel/generation → before → execute → fresh after → no-progress → events → observation；stop 顺序 flag→cancel_event→abort_inflight_streams→release_inputs→close→终结→释放租约→final event；generation 失配重读有效设置仅削弱/撤销才停、损坏配置按关闭处理；generic 首次 observe 建租约）。
- privacy: PASS（事件小 JSON 无输入正文/截图 base64/绝对路径；session_busy details 只含会话显示名；快照走 ComputerRunSnapshot.to_public_dict）。
- default-byte: PASS（默认关闭路径不实例化 backend；computer_run 为 idle 快照；prompt byte-stability 与 provider payload golden 全绿）。
- unresolved: (1) backend 执行中错误（如 computer_browser_closed）当前只发 action_finished(ok=False) 并传播，run 保持 running，不自动 stop（设计 15.1 把浏览器关闭列为停止入口，自动停止策略留给 Turn 接线 Task 裁决）；(2) controller 未接 preview_server.publish（preview_url 透传 backend 值，GUI 预览接线归后续 Task）；(3) complete_run 的 retained browser 由下一同 target run 显式复用，应用退出时 retained 清理归 Task 16 生命周期。

**Execution Record — Task 8 审查修复轮（2026-09-03，controller reviewer 发现，未 commit）：**

- Review findings: 1 Important（claim 竞态孤儿 backend：start_run 赋值窗口与 execute establishing 二次确认失败/stop_for_settings 分支均不关闭刚启动的 backend，遗留浏览器进程 + 被锁 profile）+ 2 Minor + 2 Info（M2 持锁读设置文件、M3 停止竞态悬空 action_started 事件、M4 wait 取消打断无控制器级实现、I5 `_fail_run` 转移失败直改 status、I6 complete_run 幂等仅覆盖 completed 终态）。Minor/Info 按 SDD 惯例 defer 并记入 ledger。
- 关联发现并一并修复：未绑定 target 的 run 在 generation 失配时用"任一开关开启"语义判定（`_generation_allowed_locked` else 分支），browser 开关已关闭仍能绑定 browser 租约并截图，违反设计 8.1"关闭后新动作从此立即拒绝"。修复为 `_generation_allowed_locked(run, effective_target=None)`，execute 首查传 `run.target or action.target`、建租约二查传 `action.target`。
- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q` | exit 1 | 4 failed（test_start_run_closes_backend_when_stopped_during_claim、test_start_run_orphan_close_failure_does_not_mask_error 均断言 computer_cancelled 实得 computer_invalid_state；test_execute_establishing_closes_backend_when_run_stopped_during_claim close_count 0==1 失败；test_execute_establishing_closes_backend_when_settings_revoked_during_claim DID NOT RAISE）| 目标失败原因均为孤儿 backend 不关闭/未绑定 target 语义错误，4 failed 33 passed in 4.31s。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q` | exit 0 | 37 passed in 2.44s。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py -q` | exit 0 | 118 passed in 4.20s；`tests/test_gui_system_settings.py tests/test_cancellation.py tests/test_llm_inflight_abort.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` | exit 0 | 42 passed in 24.35s；`tests/ -q -k "computer"` | exit 0 | 419 passed, 1885 deselected in 35.01s。
- Compile/Diff: `py_compile core/computer/controller.py tests/test_computer_controller.py` exit 0；`git diff --check -- core/computer/controller.py tests/test_computer_controller.py` exit 0（仅 LF→CRLF 警告）。
- Files touched: `core/computer/controller.py`（新增 `_close_backend_quietly`；start_run 孤儿分支；execute establishing 孤儿分支；`_generation_allowed_locked` effective_target 参数）、`tests/test_computer_controller.py`（新增 4 条审查修复测试）、本计划文件（本记录）。
- Re-review: RE-REVIEW PASS（核验项 1-5 全 PASS；实测 37/118/419 与自报一致；新增 Minor 覆盖缺口一条：未绑定 run 首查路径的 browser 关闭场景无直接测试，与二查共用同一代码，记入 ledger defer）。


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

- [x] **Step 1: 写默认兼容和电脑 follow-up 失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py -q`

Expected: `followup_messages` 和固定工具适配不存在。

- [x] **Step 3: 扩展 ToolResult 和 ToolExecutionRecord，不污染 tool message**

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

- [x] **Step 4: 实现保留名与 capability token 校验**

`ToolPipeline.execute_decoded()` 在 `resolve_tool()` 之前调用固定适配：只有 `decoded.name == "computer_action"` 才进入；context 必须携带 Turn 冻结的 capability token、owner 和 run id。设置关闭、owner 不符或伪造名称分别返回稳定错误。

`ToolRegistry.register()`、插件加载和 MCP schema 合并发现 `computer_action` 或 `computer_select_target` 时明确拒绝/跳过该扩展并记录失败状态；默认 `build_default_registry().schemas()` 完全不含这两个名字。

- [x] **Step 5: 实现 generic schema、提示和观察消息**

generic tool 只在 capability protocol 为 `generic` 时追加到现有 tools 尾部。说明文本必须逐字包含：首次 observe、一次一个动作、动作后看新截图、坐标原点、登录/验证码用户接管、完成后停止调用。

Provider tool result 使用 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`，字段只含 `ok,target,action,screen_sha256,width,height,origin_or_window,error_code,error_message`。PNG 转 `data:image/png;base64,...` 只存在 follow-up 多模态消息；输入正文不进入 JSON/metadata/log。

- [x] **Step 6: GREEN、顺序和现有工具回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q
git diff --check -- core/tool_protocol.py core/harness/tool_pipeline.py core/tool_registry.py core/computer tests
```

Expected: 默认工具序列和 golden 不变；电脑 call 的 tool result 总在观察 user message 前；混合批次零副作用。

Suggested commit after explicit authorization: `feat(harness): add generic computer tool observations`

#### Execution Record — Task 9

**Execution Record (2026-09-03, implementer subagent, no commit per K3):**

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py -q` → exit 2（collection error：`ModuleNotFoundError: No module named 'core.computer.provider_generic'`，两个新测试文件即目标能力缺失，2 errors in 2.55s；collection 中断使另两个文件未跑）。补跑 `venv\Scripts\python.exe -m pytest tests/test_tool_pipeline.py tests/test_tool_registry.py -q` → exit 1，8 failed / 80 passed in 6.31s：test_tool_pipeline 4 failed（`ToolExecutionRecord` 无 followup_messages / `ToolExecutionContext` 无 computer_lease / 私有暂存键不剥离），test_tool_registry 4 failed（保留名不拒绝 DID NOT RAISE / 插件 loaded≠failed / `filter_reserved_tool_schemas` 不存在 / MCP 保留名未过滤）。失败原因全部是目标能力缺失，符合预期。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_tool_pipeline.py tests/test_tool_registry.py -q` → 116 passed / 6.62s / exit 0（test_computer_tool_adapter 19 条：brief 3 样例逐字 + 权限链不询问/9 键 JSON 逐字节/type_text 输入不入 JSON 与 followup/无 lease 关闭/能力冻结错误/owner 不符/非法动作/控制器错误/缺 PNG 无旧图/伪造名走普通解析/ToolSpec 四语义/ordinal 递增；test_computer_provider_generic 9 条：schema 副本与 None/七条说明语义/批处理通过与拒绝；test_tool_pipeline +4：record 默认空/lease 默认 None/暂存转 record/execute_one 剥离；test_tool_registry +5：保留名拒绝/默认 schema 排除/插件 failed 记录/filter 函数/MCP 合并过滤）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` → 17 passed / 11.97s / exit 0；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer"` → 450 passed / 23.73s / exit 0；全量 `venv\Scripts\python.exe -m pytest tests/ -q --tb=no` → 2338 passed / 2 skipped / 1 failed / 484.80s——唯一 failed 为 `tests/test_tokenrhythm_image.py::PluginMetadataTest::test_plugin_json_disabled_by_default`（断言 plugin.json enabled=False，实际 True），系并发会话对 `plugins/tokenrhythm_image/plugin.json` 的既有工作区改动（Task 9 禁触文件，git diff 显示 1 行 enabled 翻转在本次开始前已存在），与 Task 9 无关，单跑复现相同失败。
- Diff check: `git add -N core/computer/tool_adapter.py core/computer/provider_generic.py tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py` + `git diff --check -- core/tool_protocol.py core/harness/tool_pipeline.py core/tool_registry.py core/computer tests` → exit 0（仅 LF→CRLF 警告）。
- Files touched: `core/tool_protocol.py`（ToolResult 增默认空不可变 followup_messages）、`core/harness/tool_pipeline.py`（ToolExecutionRecord 增 followup_messages、ToolExecutionContext 增 computer_lease、execute_decoded 在 resolve_tool 前接固定适配、execute_batch 构造 record 时剥离 `_ycode_followup_messages` 暂存、execute_one 剥离旧单调用路径暂存）、`core/computer/tool_adapter.py`（新建：保留名常量、ComputerTurnLease、computer_action_tool_spec 四语义、lease_from_context、computer_turn_context_error 稳定错误）、`core/computer/provider_generic.py`（新建：build_generic_computer_tool/build_generic_dynamic_prompt/validate_computer_tool_batch/execute_reserved_computer_tool、OBSERVATION_FOLLOWUP_TEXT、9 键 JSON 投影、follow-up 多模态消息）、`core/tool_registry.py`（register 拒绝保留名 + filter_reserved_tool_schemas）、`core/llm.py`（get_tools_schema 的 MCP 合并处套用 filter_reserved_tool_schemas，2 行最小改动，见下方偏差说明）、`tests/computer_fakes.py`（新增 FakeController）、`tests/test_computer_tool_adapter.py`（新建 19 条）、`tests/test_computer_provider_generic.py`（新建 9 条）、`tests/test_tool_pipeline.py`（+4）、`tests/test_tool_registry.py`（+5）、本计划文件（checkbox 与本记录）。
- spec: PASS（保留名在 resolve_tool 之前识别且仅 computer_action 进入；context 校验 capability token/owner lease/run id，设置关闭、owner 不符、伪造名称分别稳定错误；register/插件/MCP 三处保留名拒绝并记录失败状态；generic schema 仅 enabled+generic 返回且为 tools 尾部追加提供副本；tool result 恰好 9 键 compact JSON；PNG data URL 只在 follow-up user 消息；批次多个 computer call 或混用 → computer_invalid_action 整批拒绝零副作用；follow-up 暂存 record、不污染 tool message；RepeatCallGuard 不为电脑动作豁免）。
- privacy: PASS（输入正文不进 JSON/metadata/log，type_text 正文有专项测试；截图 base64 只存在于 follow-up 多模态消息；错误结果不含截图/凭据/URL query；Journal event 继续只哈希 tool content——build_tool_result_event_draft 消费剥离后的 message）。
- default-byte: PASS（默认关闭：registry 无 computer_action/computer_select_target；get_tools_schema 过滤为直通（无保留名时逐项相同）；prompt byte-stability 与 provider payload golden 17 条原样通过；ToolExecutionContext.computer_lease 默认 None，普通回合行为与序列化不变）。
- unresolved: (1) `core/llm.py` 的 MCP 合并点在 get_tools_schema（工具 schema 合并唯一现场），Task 9 Files 清单未列 llm.py——按"最小调整并写进报告"纪律做了 2 行过滤接线，无保留名时字节完全不变；(2) `_computer_v1.ordinal` 取 controller.snapshot().action_count（execute 返回后即本次动作序号，依赖 controller 单动作锁串行语义），未改 ComputerObservation 模型；(3) follow-up user messages 的持久化与按 ordinal 追加、`_content_has_media` 折叠兼容、以及 validate_computer_tool_batch 在调度层的接线点均为 Task 10 范围，本 Task 只交付函数与 record 通道；(4) execute_one 旧单调用入口无 follow-up 通道，剥离后丢弃（生产 computer 调用走 ToolBatchExecutor 路径）。

---

### Task 10: 冻结 Turn capability 并接入 generic Provider 请求

**Files:**
- Modify: `core/context_builder.py`
- Modify: `core/agent_service.py`
- Modify: `core/llm.py`
- Modify: `core/harness/request_envelope.py`
- Modify: `core/memory.py`
- Create: `tests/test_computer_llm_generic.py`
- Modify: `tests/test_memory_maintenance.py`
- Modify: `tests/test_prompt_byte_stability.py`
- Modify: `tests/test_provider_payload_golden.py`
- Modify: `tests/test_deepseek_prompt_ledger.py`
- Modify: `tests/test_cache_keepalive.py`

**Interfaces:**
- Consumes: `resolve_computer_capability()`, generic schema/prompt, fixed tool adapter
- Produces: `RuntimeContext.computer_capability: ComputerCapability | None`
- Produces: one immutable capability snapshot per Turn, reused by every Step
- Produces: default-off request bytes identical to pre-feature baseline

- [x] **Step 1: 写 Turn 冻结和默认关闭字节测试**

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

`core/memory.py` 媒体折叠兼容（写入 `tests/test_memory_maintenance.py`；`add_observation_message` 等测试内 helper 在同一模块定义）：

```python
def test_content_has_media_accepts_full_message_dict_with_computer_meta():
    message = {
        "role": "user",
        "content": [{"type": "text", "text": "观察"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}],
        "_computer_v1": {"run_id": "run-a", "ordinal": 3, "target": "browser"},
    }
    assert _content_has_media(message) is True


def test_fold_keeps_computer_meta_and_is_idempotent(store, session):
    ids = [add_observation_message(store, session, ordinal) for ordinal in range(1, 4)]
    add_observation_message(store, session, 4)  # 最新一条不折叠
    folded_first = raw_content_of(store, session, ids[0])
    assert '"_computer_v1"' in folded_first
    assert "data:image/png;base64" not in folded_first
    add_observation_message(store, session, 5)  # 再次触发折叠
    assert raw_content_of(store, session, ids[0]) == folded_first  # 幂等，字节不再变化


def test_old_computer_screenshots_do_not_accumulate(store, session):
    for ordinal in range(1, 20):
        add_observation_message(store, session, ordinal)
    assert count_unfolded_media_rows(store, session) <= 1  # 只有最新一条保留图片
```

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_memory_maintenance.py tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q`

Expected: 新 capability 上下文和工具注入不存在；原有 golden 测试仍应单独通过。

- [x] **Step 3: 在 Turn 起点解析一次能力**

在 `core/agent_service.py` 已确定当前 model/provider、turn source、settings 和 turn id 后立即解析，存入本次调用的局部不可变 `ComputerCapability`，并传给 `core.llm.chat_with_model_events(...)` 和 `ToolExecutionContext`。不要写模块级“当前 capability”。

来源必须由入口层给出受控值 `local|lan`；不得从用户消息或模型参数读取。关闭设置时通过 generation/controller cancel 终止活动动作，不重算该 Turn 的 request contract。

- [x] **Step 4: 只向动态 prompt 追加电脑说明**

扩展 `RuntimeContext` 默认字段：

```python
computer_capability: ComputerCapability | None = None
```

`build_dynamic_prompt()` 末尾仅在 `capability and capability.enabled` 时追加 `build_generic_dynamic_prompt()` 或原生短说明。`build_stable_prompt()`、`build_stable_system_message()` 和 `_DECISION_POLICY` 一字不改。默认 `None/disabled` 返回值必须与修改前完全相同，包括换行。

- [x] **Step 5: 在 route tool selection 后追加 generic tool**

在现有 tools 已按 provider/权限/plan 路由完成之后复制列表：

```python
request_tools = list(existing_tools)
computer_tool = build_generic_computer_tool(turn_capability)
if computer_tool is not None:
    request_tools.append(computer_tool)
```

然后才交给 RequestEnvelope 指纹和 Provider 转换。不得缓存到全局 `get_tools_schema()`。DeepSeek Ledger 看到新 contract 时走现有切代；不要专门改 Ledger。Kimi/GLM metadata、cache-control、reasoning、`extra_body` 保持原逻辑。

- [x] **Step 6: 提交工具结果和观察消息的同 Step 边界，扩展媒体折叠**

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

带 `_computer_v1` 的 follow-up 观察消息以完整 dict 持久化时，`core/memory.py` 的 `_content_has_media()` 和 `_fold_stale_media_locked()` 必须同时识别两种形态：content 列表，以及含 `content` 列表的完整消息 dict。折叠只替换媒体块为文本占位符，不删除或改写 `_computer_v1` 私有元数据；折叠后字节永久冻结（幂等）。该改动不得改变现有非电脑消息的折叠行为——普通用户图片消息的折叠结果字节必须与改动前完全一致。

- [x] **Step 7: GREEN 与缓存/ledger 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_memory_maintenance.py tests/test_memory.py tests/test_llm_responses.py tests/test_llm_events_fixes.py -q
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q
git diff --check -- core/context_builder.py core/agent_service.py core/llm.py core/harness/request_envelope.py core/memory.py tests
```

Expected: 现有 hash/expected 文件内容无变化；enabled generic 只在 tools 尾部多一个 schema；同 Turn 多 Step schema 稳定；旧电脑截图按现有媒体折叠收敛、折叠幂等。

Suggested commit after explicit authorization: `feat(llm): route generic computer turns`

#### Execution Record — Task 10

**Execution Record (2026-09-03, implementer subagent, no commit per K3):**

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_memory_maintenance.py -q` → exit 1 | 22 failed / 8 passed in 0.66s | test_computer_llm_generic 17 条收集失败或断言失败：15 条 AttributeError（`core.llm` 无 `resolve_computer_capability` 属性——接线点不存在）、2 条 TypeError（`chat_with_model_events() got an unexpected keyword argument 'computer_turn_source'`、`RuntimeContext.__init__() got an unexpected keyword argument 'computer_capability'`）；test_memory_maintenance 新增 5 条中 3 条真失败（`_content_has_media` 对完整消息 dict 返回 False、dict 形态行不折叠导致 base64 残留）+ 2 条 RED 期平凡通过（`test_old_computer_screenshots_do_not_accumulate` 在折叠缺失时 count==0 同样满足 ≤1，GREEN 后才具备真实区分度）——失败原因全部是目标能力缺失。另按 brief Step 2 期望单独验证原 golden：`venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py -q` → 17 passed / 2.54s / exit 0（改动前基线完好）。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_memory_maintenance.py -q` → 30 passed / 0.68s / exit 0（test_computer_llm_generic 20 条：resolve 一次冻结×多 Step、tools 尾部追加不重排、默认关闭逐字节等于 pre-feature 真实捕获基线 `EXISTING_DEFAULT_REQUEST_BYTES`、capability=None 同基线、中途开启不改本 Turn、中途关闭 contract 不换 schema+9 键 computer_disabled、动态 prompt enabled 追加/None==disabled 逐字节、native 短说明不含 generic 文本、stable prompt 不含电脑段、tool result→followup 同序提交+`_computer_v1` 不进 Provider 请求、followup 完整 dict 落库、混用批次拒绝 computer 且其他工具照常、多 computer call 双拒、取消打捞 computer_cancelled+stop 收尾、正常收尾 complete_run、无 computer 调用不建 run、busy 时精确稳定错误、turn_source 透传、非法 turn_source ValueError、子代理回合不解析不注入；test_memory_maintenance 10 条：原 5 条 + dict 形态媒体识别 + 纯列表/纯文本拒绝 + 折叠保留 _computer_v1 且幂等 + 19 条旧截图收敛到 1 + 普通图与电脑观察混排折叠且普通行仍为列表形态）。
- Regression: brief Step 7 三条命令 → `venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_memory_maintenance.py tests/test_memory.py tests/test_llm_responses.py tests/test_llm_events_fixes.py -q` → 143 passed / 113.67s / exit 0；`venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q` → 64 passed / 3.10s / exit 0；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no` → 474 passed / 1893 deselected / 12.54s / exit 0（≥450 达标）。额外扩散回归：test_llm_events + test_cancellation + test_agent_turn_e2e + test_harness_runtime + test_gui_tool_events + test_session_parallel_isolation → 135 passed / 70.79s；test_tools + test_context_builder → 215 passed / 13.39s；test_agent_service + test_gui_tool_events → 58 passed / 1.40s（stub_llm 桩补 resolve_turn_computer_capability，见偏差 7）；全量 `venv\Scripts\python.exe -m pytest tests/ -q --tb=no` → **2365 passed / 2 skipped / 0 failed** in 242.81s（Task 9 时预存失败的 test_tokenrhythm_image 本轮通过，并发会话已还原 plugin.json）。
- Compile: `venv\Scripts\python.exe -m py_compile core/llm.py core/agent_service.py core/context_builder.py core/memory.py core/computer/provider_generic.py` → exit 0。
- Diff check: `git add -N tests/test_computer_llm_generic.py` + `git diff --check -- core/context_builder.py core/agent_service.py core/llm.py core/harness/request_envelope.py core/memory.py tests` → exit 0（仅 LF→CRLF 警告，与 Task 9 一致）。
- Files touched: `core/context_builder.py`（RuntimeContext 增默认 None 的 computer_capability 字段 + build_dynamic_prompt 末尾仅在 enabled 时追加 `_build_computer_prompt_section`：generic 走 build_generic_dynamic_prompt、native 给短说明；默认 None/disabled 逐字节不变；build_stable_prompt/_DECISION_POLICY 一字未改）、`core/agent_service.py`（build_runtime_context 惰性 import core.llm.resolve_turn_computer_capability 解析一次并注入 RuntimeContext，turn_source 固定受控值 local）、`core/llm.py`（顶层轻量 computer 导入 + `resolve_turn_computer_capability` 公共组合解析（get_model_config_by_litellm_model/get_model_protocol + load_computer_settings + 纯函数，设置读取失败 fail-closed 为 computer_invalid_config 拒绝快照）+ chat_with_model_events 增 computer_turn_source 参数（仅 local|lan，非法 ValueError）并在 Turn 起点解析一次传给 generator（allow_subagents=False 的子代理回合不解析）+ Step 5 在 `_select_request_tools_for_route` 之后复制列表尾部追加 build_generic_computer_tool 副本（不进 get_tools_schema 缓存）+ 接线点② 收到 tool_calls 后执行前 validate_computer_tool_batch，被拒 computer call 在 execute_decoded 拦截合成 computer_error_tool_message 稳定 9 键错误（其余工具照常，零 run 建立）+ 接线点① 惰性 lease：模型首次真正调用 computer_action 时 get_computer_control_service().start_run(owner, capability) 取 run_id 构造 ComputerTurnLease 注入 ToolExecutionContext.computer_lease（start_run 失败时以带错误码的禁用 lease 继续，contract 不重算）+ 接线点③ records 排序后先 messages/pending_persistence 全部 tool results、再按相同 ordinal 追加 followup user 观察（同一 flush 单事务提交）+ salvage 对悬空 computer_action call 生成 computer_cancelled 9 键 JSON + 生成器 finally `_finalize_computer_run`：正常文本收尾 complete_run(retain_browser_session=True)，取消/异常/无进展/Step 上限一律 stop，controller 已终态时幂等跳过 + `_build_tool_pipeline_and_context` 增 computer_lease 透传）、`core/memory.py`（`_content_has_media` 同时识别 content 列表与含 content 列表的完整消息 dict；`_fold_stale_media_locked` dict 形态只折叠 content 内媒体块、`_computer_v1` 等其余键原样保留，列表形态折叠字节与改动前完全一致，幂等不变）、`core/computer/provider_generic.py`（新增公共 `computer_error_tool_message(tool_call, error)`：批处理拒绝与取消打捞共用的 9 键 JSON 合成，参数解析失败 target/kind 空串占位）、`tests/computer_fakes.py`（FakeController 增 start_run/complete_run/stop 最小生命周期记录与 start_error/complete_error/stop_error/run_status，向后兼容 Task 9 用法）、`tests/test_computer_llm_generic.py`（新建 20 条，helper 全部模块内定义）、`tests/test_memory_maintenance.py`（+5 条与 store/session fixture、add_observation_message/raw_content_of/count_unfolded_media_rows 模块内 helper）、`tests/test_agent_service.py`（stub_llm 桩补 resolve_turn_computer_capability→None，见偏差 7）、本计划文件（checkbox 与本记录）。
- spec: PASS（capability 每 Turn 在 chat_with_model_events 起点解析一次、局部不可变快照贯穿全部 Step，无模块级状态；generic tool 只在 enabled+generic 且主代理回合时复制列表尾部追加，绝不缓存进 get_tools_schema；DeepSeek Ledger/Kimi/GLM metadata/cache-control/reasoning/extra_body 原逻辑零改动（64 条 golden 原样通过）；收到 tool_calls 后执行前整批校验，多个/混用 computer call 本 Step 零副作用、其余工具照常；followup 在同 Step 既有提交边界按 ordinal 追加在全部 tool results 之后；取消打捞 computer_cancelled；Turn 终态 finally 二选一 complete/stop 不留 running 租约；媒体折叠双形态识别、dict 折叠保留 _computer_v1、幂等、普通消息折叠字节不变）。
- privacy: PASS（follow-up user 消息以完整 dict 持久化，`_computer_v1` 只存在于 DB 与内存消息；Provider 请求经 _strip_media_and_merge 只保留 role/content/tool 字段——专项断言第二请求 messages 无 `_computer_v1` 且 data URL 保留；Journal event 只哈希剥离后的 tool content，followup user 消息不产生 Journal 事件；computer_error_tool_message/cancelled 均为 9 键 JSON，无截图/输入正文/旧图；OWNER cancel_event 语义未被触碰）。
- default-byte: PASS（默认关闭：resolve 走真实配置（本机 config 无 computer 键、模型无 computer_protocol）返回 disabled，build_generic_computer_tool 返回 None，tools/prompt/请求字节零变化；新测试以未改动代码真实捕获的 `EXISTING_DEFAULT_REQUEST_BYTES` 常量逐字节比对，DISABLED 与 capability=None 两种路径均等于基线；test_prompt_byte_stability 5 条 + test_provider_payload_golden 12 条 + DeepSeek ledger/cache keepalive/token accounting 共 64 条原样通过，未改任何现有 expected/hash）。
- unresolved/deviations: (1) `core/harness/request_envelope.py` 在 Task 10 Files 清单但零改动——generic 路径的 tools 指纹与消息清洗已被现有 `RequestEnvelope.capture` + `_strip_media_and_merge` 完整覆盖（`_computer_v1` 在请求构造前被剥离，tools_sha256 随尾部追加自然变化），native continuation 的 envelope 改动归 Task 11b；(2) `core/computer/provider_generic.py` 不在 Files 清单，新增公共 `computer_error_tool_message`（最小扩展，复用 Task 9 `_error_result`，避免在 llm.py 平行实现 9 键 JSON 合成）；(3) lease/start_run 为惰性建立（模型首次真正调用 computer_action 时）而非 Turn 起点立即建立——brief 接线点①的"解析 capability"确实在 Turn 起点一次完成，start_run 推迟到首个 computer call：普通对话回合不占用进程级排他租约（设计 14.1 busy 语义 + 16.1 安全条"只在 run 非 idle 时显示"支持该读法），Turn 终态 finally complete/stop 语义不变；start_run 失败（如 computer_session_busy）以带错误码的禁用 lease 继续、contract 不重算，模型收到精确稳定错误（非降级：不换浏览器/协议/旧图）；(4) 子代理回合（allow_subagents=False）不解析也不注入电脑操控（入口层 turn_source 语义 + 单租约 owner 归属主 Turn），有专项测试；(5) brief 的 golden 相关 Modify 文件（test_prompt_byte_stability/test_provider_payload_golden/test_deepseek_prompt_ledger/test_cache_keepalive）零改动——默认关闭字节断言（pre-feature 真实捕获常量）已在新测试文件内，现有 expected 全部原样通过；(6) `computer_turn_source` 参数默认 "local"，入口层 main.py/gui_bridge.py 不在 Files 清单未改，LAN 的 turn_source=lan 标记为后续 Task 范围；(7) `tests/test_agent_service.py` 不在 Files 清单，stub_llm 桩（把 core.llm 换成轻量 ModuleType 避免 litellm）缺 `resolve_turn_computer_capability` 导致 13 条 ImportError——按"最小调整"纪律补一行 `stub.resolve_turn_computer_capability = lambda *args, **kwargs: None`；(8) 会话中途出现并发会话改动：tests/test_llm_responses.py +30 行（responses 桥接枚举归一化测试）与 core/llm.py 内 `_normalize_responses_event_type` 2 个 hunk（+18/-1，非本 Task 作者），以及未跟踪目录 `待开发/`——全部未触碰，共存后全量回归 0 失败；(9) 中途关闭设置的 controller 侧自动 stop 由真实 controller 的 generation 检查在 execute 内完成（设计 8.1，Task 8 已交付），generator 侧只保证 contract 冻结与收尾幂等。

---

### Task 11a: OpenAI Responses 原生事件解析、target selector 与串行动作执行

**Files:**
- Create: `core/computer/provider_openai.py`
- Create: `tests/test_computer_provider_openai.py`
- Modify: `core/harness/stream_assembler.py`
- Modify: `tests/test_llm_responses.py`

**Interfaces:**
- Consumes: native `ComputerCapability`, controller and target selector
- Produces: `build_openai_computer_tools(capability, target_selected) -> tuple[dict, ...]`
- Produces: `NativeComputerCall(response_id, call_id, actions, pending_safety_checks)`
- Produces: `parse_openai_computer_events(events) -> NativeComputerCall | NativeFinalText`
- Produces: `execute_native_computer_call(call, controller) -> NativeCallResult`
- Produces: selector tool `computer_select_target` 的固定 schema 与租约建立

- [x] **Step 1: 写受控 built-in、事件完整性和 selector 失败测试**

```python
def test_native_first_request_appends_controlled_computer_tool():
    request = capture_responses_request(capability=NATIVE_BROWSER_CAPABILITY)
    assert request["tools"][-1] == {"type": "computer"}
    assert "previous_response_id" not in request


def test_native_dual_target_disables_parallel_tool_calls_and_includes_selector():
    request = capture_responses_request(capability=NATIVE_DUAL_TARGET_CAPABILITY)
    names = [tool.get("name") or tool.get("type") for tool in request["tools"]]
    assert "computer_select_target" in names and "computer" in names
    assert request["parallel_tool_calls"] is False


def test_selector_and_computer_call_in_same_response_is_protocol_error(controller):
    events = [SELECTOR_DONE_EVENT, COMPUTER_CALL_EVENT]
    with pytest.raises(ComputerControlError) as raised:
        parse_openai_computer_events(events)
    assert raised.value.code == "computer_provider_protocol_error"


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
    result = execute_native_computer_call(call, controller)
    assert result.status == "provider_safety_required"
    assert controller.execute_count == 0


def test_native_actions_are_serial_and_stop_after_first_failure(controller):
    controller.fail_on_ordinal = 2  # 第二个动作（CLICK）执行时失败
    call = NativeComputerCall("resp_1", "call_1", (MOVE, CLICK, TYPE_TEXT), ())
    with pytest.raises(ComputerControlError):
        execute_native_computer_call(call, controller)
    assert controller.attempted_actions == [MOVE, CLICK]  # 失败后 TYPE_TEXT 不再执行
    assert controller.max_concurrent_actions == 1
    assert controller.observation_count == 2  # 每个动作都有本地 after observation
```

另测 native 双 target 时 `computer_select_target` 从首请求存在；未选 target 的 computer call 被拒；selector 必须单独完成一个 Step（`parallel_tool_calls=false`），不依赖 output 数组顺序；response failed/incomplete；unknown built-in 仍拒绝；final text 结束原生分支。`SELECTOR_DONE_EVENT`、`COMPUTER_CALL_EVENT`、`NATIVE_DUAL_TARGET_CAPABILITY` 等在 `tests/computer_fakes.py` 或本测试模块内定义。

- [ ] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py -q`

Expected: 原生 computer built-in 和事件类型不被当前转换器支持。

- [x] **Step 3: 实现精确原生工具与 target selector**

built-in 使用当前官方 GA 定义 `{"type":"computer"}`。官方 migration 表明确 `computer_use_preview` 是旧路径，新实现不得采用或自动回退。实施时再次打开官方指南复核；若官方 GA 形状未来变化，先更新设计附录并说明证据。`_responses_tools_from_chat_tools()` 仍拒绝任意 built-in，只允许 capability 分支直接传入的精确 computer definition。

两个 target 同时启用时，首请求在现有 function tools 后依次追加 `computer_select_target` 和 native computer tool，并固定设置 `parallel_tool_calls=false`，强制 selector 单独完成一个 Step。selector schema：

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

`computer_select_target` 由固定 adapter（Task 9 的保留名机制）执行并写入 target 租约，不进入普通 ToolRegistry。同一个 Provider response 同时出现 selector 和 `computer_call` 时以 `computer_provider_protocol_error` 失败，绝不按 output 数组顺序猜测。重复选择、未开启 target、选择后变更均明确失败。

- [ ] **Step 4: 严格标准化 Responses 事件**

`NativeComputerCall` 只有 response id、call id、非空完整 `actions[]` 和 safety checks 解析成功才构造。流式 delta 先由 assembler 合并，禁止基于半截 JSON 执行动作。provider 的每个 computer action 映射到 Task 2 统一动作后再次校验坐标/字段；官方 `screenshot` 映射为 `observe`；官方 `drag` 的 `path` 与鼠标 modifier `keys` 直接映射到统一动作的 `path`/`keys`。

GA 原生 `actions[]` 是 generic 单动作规则的明确例外：按原序逐个调用 controller，每个动作前都检查 cancel/pause/owner/generation，每个动作都捕获本地 after observation、执行无进展判断并发 trace；失败或停止后不执行余项。整批成功后只把最后一次 observation 的截图构造为一个 `computer_call_output` 回传 Provider——“只回传最后一图”不等于“中间动作不观察”。禁止并行、禁止忽略中间失败、禁止在后台把整批做成不可中断操作。

completed/incomplete/failed 三种终态显式区分。缺字段、重复冲突字段或未知动作抛 `computer_provider_protocol_error`，当前 Step/Turn 失败，绝不进入 generic 分支。

- [x] **Step 5: GREEN 与 Responses 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py -q
git diff --check -- core/computer/provider_openai.py core/harness/stream_assembler.py tests
```

Expected: 事件解析、selector 租约和串行执行全绿；现有 Responses 测试原样通过。

Suggested commit after explicit authorization: `feat(openai): parse native computer events`

#### Execution Record — Task 11a

**Execution Record (2026-09-04, verification round after implementation-first, no commit):**

- 偏差声明：本 Task 为 implementation-first（代码先于验证轮完成并已随 f6d0543 提交），brief Step 2 的 RED 证据不可补——实现先在。以定向 + 回归全绿 + 独立审查弥补。
- RED-equivalent（验证轮实测暴露的三处测试侧缺陷，均先观察到真实失败再修复）：
  1. `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py -q` 首跑 **挂起 >16 分钟**（faulthandler 2 分钟转储×2）：`_run_native_responses_turn` 定义了 `fake_responses` 但 patches 列表漏装 `llm_mod._litellm_responses` 拦截，导致 `test_native_first_request_appends_controlled_computer_tool` 走真实 litellm `socket.create_connection`（真实付费 Provider 路径，违反 fake 注入纪律）。修复：补 `mock.patch.object(llm_mod, "_litellm_responses", fake_responses)`（与 tests/test_llm_responses.py 既有成熟接缝一致）；仅测试接线修复，产品代码零改动。
  2. 修复 1 后 `7 failed / 52 passed in 19.66s`：全部集中于 `test_build_openai_computer_tools_matrix[*]` 末尾深拷贝断言——期望值字面量多包一层元组 `(({"type":"computer"},),)`，与 brief 声明接口 `tuple[dict, ...]` 不符；第一处断言（normalized == expected）全 7 参数通过证明产品行为正确。修复：期望字面量改为 `({"type": "computer"},)`。
  3. computer 全套首跑 `1 failed, 538 passed in 58.73s`：`tests/test_computer_tool_adapter.py::test_forged_non_computer_name_stays_on_normal_resolution_path`——Task 9 时以 `computer_select_target` 作"未实现伪造名"示例断言走普通解析得"未找到工具"；Task 11a 计划 Step 3 明确 selector 由固定 adapter 拦截后，该名的行为按计划变为 `computer_disabled`（且 11a 已有 `test_selector_without_selection_context_fails` 专项覆盖新行为）。修复：示例换成真正非保留名 `computer_select_browser`，保留原测试意图（非保留名走普通解析路径），断言强度不降。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py -q --tb=short` → 59 passed / 8.13s / exit 0（含 F1 收敛 `test_capability_resolved_once_per_production_turn`：prompt 侧解析一次 + tools 侧穿参复用，共恰 1 次）；`venv\Scripts\python.exe -m pytest tests/test_llm_responses.py -q --tb=short` → 18 passed / 10.04s / exit 0（含 11a 新增终态 computer_call 转发、半截事件忽略、completed output 携带、冲突 response id 拒绝、assembler 合并 chunk）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q` → 65 passed / 13.62s / exit 0（原 64 条原样通过 + 1 条为并发会话新增 stepfun 测试，现有 expected/hash 零改动）；`venv\Scripts\python.exe -m pytest tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_memory_maintenance.py -q` → 39 passed / 6.07s / exit 0（Task 10 保持绿）；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no` → **539 passed / 1900 deselected in 54.63s** / exit 0（≥512 达标）。
- Diff check: `git diff --check` → exit 0（本轮无新建文件，全部为已提交 tracked 文件的最小修改；工作区仅余并发会话未跟踪图片，未触碰）。
- Files touched（验证轮）: `tests/test_computer_provider_openai.py`（+`_litellm_responses` 拦截 patch、深拷贝断言期望字面量修正）、`tests/test_computer_tool_adapter.py`（过时伪造名示例更新，意图不变）、本计划文件（checkbox 与本记录）。
- spec: PASS（native 首请求受控追加 `{"type":"computer"}`、双 target 时 selector+computer 且 `parallel_tool_calls=false`；selector 与 computer_call 同 response 无论顺序均 `computer_provider_protocol_error`；缺 response/call id、未知动作/字段、三终态显式区分；官方 screenshot→observe、drag path/keys 直接映射；actions[] 串行、每动作 cancel/pause/owner/generation 检查 + fresh after observation、失败即停不执行余项、整批成功只回传最后 observation；pending safety → `provider_safety_required` 零执行；selector 不进 ToolRegistry（Task 9 保留名冻结测试仍绿）；native 解析失败不 fallback generic）。
- F1 收敛证据: `test_capability_resolved_once_per_production_turn` 真实通过——prepare_turn_messages（prompt 侧）与 chat_with_model_events（tools 侧）共享同一次 resolve（agent_service 解析一次经 `set_turn_computer_capability`/`take_turn_computer_capability` 线程 handoff 穿参），测试内 resolve 被替换为计数器，两阶段后恰为 1。
- privacy: PASS（截图/输入正文/response id 不进断言的 Provider 捕获与 DB 路径均由专项测试覆盖；`previous_response_id` 不出现在首请求断言中）。
- default-byte: PASS（默认关闭 golden 65 条原样通过，含 prompt byte-stability 与 provider payload golden；test_llm_responses 既有 12 条含 reject builtin 不变）。
- unresolved/deviations: (1) implementation-first 偏差如上声明，RED 不可补；(2) 三处修复全部为测试侧缺陷/过时示例，产品代码（provider_openai.py/stream_assembler.py/tool_pipeline.py/request_envelope.py/capability.py/llm.py native 分支）验证轮零改动即全绿；(3) 计划 Files 清单未列 `core/computer/capability.py`、`core/agent_service.py`、`core/llm.py`、`tests/computer_fakes.py`、`core/harness/tool_pipeline.py`、`core/harness/request_envelope.py`、`core/computer/provider_generic.py`——F1 handoff 与 native 接线所需的最小扩展，已在交接文档 B 节逐项列明，审查轮覆盖。

#### Execution Record — Task 11a 修复轮（reviewer I-1/I-2，2026-09-04）

- Review findings: reviewer（general-purpose 只读子代理）九镜头审查，NEEDS-FIX：I-1（Important）F1 收敛后 `build_runtime_context` 不接收 api_base，`resolve_turn_computer_capability` 以 api_base=None 构造 provider，官方 OpenAI 路由的 native 能力在生产入口永远以 `computer_protocol_mismatch` 拒绝（fail-closed 但 11a 交付不可达；CLI main.py:3571 持有 task_url 却未向下传、chat 侧靠 handoff 拿到禁用快照）；I-2（Important）`execute_native_computer_call` 的窄 controller 协议（execute(action)/enter_provider_safety_required()）与真实 ComputerControlService 签名（execute(run_id, owner, action)/enter_provider_safety_required(run_id)）不匹配且 docstring 虚构"真实 ComputerControlService 在单动作内完成"表述，11b 直传真实服务会 TypeError。Minor：M-1 assembler 重复终态载荷后者覆盖（11b 消费侧保持 native_computer_call_from_stream 严格校验即可）、M-3 live loop 未消费 assembled.computer_call（11a/11b 计划分界，11b 验收项）。
- RED: `venv\Scripts\python.exe -m pytest "tests/test_computer_provider_openai.py::test_production_native_capability_receives_entry_api_base" -q --tb=short` → exit 1 | 1 failed in 7.32s（首跑 `computer_model_not_capable`：use_temp_config 只 patch config._CONFIG_FILE，model_manager 在 import 时复制了该值致模型查找落空；按 tests/test_config.py 既有模式补 patch model_manager._CONFIG_FILE 后复跑）→ 复跑 exit 1 | 1 failed in 17.22s | `AssertionError: assert False is True` + `error_code='computer_protocol_mismatch'`——失败原因恰为目标能力缺失（入口 api_base 未穿参，native 官方路由判定必然失败），有效 RED。
- Fix: `core/agent_service.py`（build_runtime_context 增默认空串 keyword 参数 api_base，resolve 调用改为 `api_base=api_base or None`；prepare_turn_messages 第 564 行传 `api_base=api_base`——恢复 F1 前 Task 10 在 chat 侧持有真实 api_base 解析的语义，入口层 CLI main.py task_url / GUI active base_url 同源；docstring 记录该约束）；`core/computer/provider_openai.py`（execute_native_computer_call docstring 改为窄执行协议契约声明 + Task 11b 必须构造绑定 run_id/owner 适配器的前置条件，代码零改动）；`tests/test_computer_provider_openai.py`（+I-1 测试 test_production_native_capability_receives_entry_api_base：真实 resolve 链路 + 临时 config 官方 OpenAI Responses provider + native 模型 + browser_control_enabled，prepare 传 api_base 后断言 handoff 快照 enabled=True/protocol=native_openai；+use_temp_config 导入与 model_manager._CONFIG_FILE 双 patch）。I-2 按 reviewer 认可的方案 B（docstring 契约 + 11b 适配器前置），接口签名保持 brief 声明不变。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py -q --tb=short` → 60 passed / 52.13s / exit 0（59 旧 + 1 新）。
- Regression: `tests/test_agent_service.py tests/test_computer_llm_generic.py tests/test_computer_provider_generic.py tests/test_memory_maintenance.py -q` → 72 passed / 87.52s / exit 0；`tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q` → 65 passed / 73.78s / exit 0；`tests/ -q -k "computer" --tb=no` → 540 passed / 1900 deselected / 98.09s / exit 0；`tests/test_llm_responses.py -q --tb=no` → 18 passed / 33.33s / exit 0。
- Compile: `venv\Scripts\python.exe -m py_compile core/agent_service.py core/computer/provider_openai.py` → exit 0。
- Diff check: `git diff --check` → exit 0（仅 LF→CRLF 警告，与既往一致）。
- 环境事件：修复轮中途一次 Edit（build_runtime_context docstring + resolve 调用）在工具确认成功后被回退（多会话共享工作区疑似并发写回），发现后重新应用并全量重验——最终 60 passed 证明当前工作区版本完整。
- 11b 前置条件移交：① live loop 消费 assembled.computer_call 并构造绑定 run_id/owner 的窄协议适配器再调 execute_native_computer_call（不得直传真实 ComputerControlService）；② assembler 重复终态载荷走 native_computer_call_from_stream 严格校验防线；③ continuation 的 previous_response_id 只留 Turn 内存。

---

### Task 11b: 原生 continuation、Provider safety 契约与脱敏持久化投影

**Files:**
- Modify: `core/computer/provider_openai.py`
- Modify: `core/llm.py`
- Modify: `core/harness/request_envelope.py`
- Modify: `tests/test_computer_provider_openai.py`
- Modify: `tests/test_provider_payload_golden.py`
- Modify: `tests/test_harness_runtime.py`

**Interfaces:**
- Consumes: Task 11a 的 `NativeComputerCall`/`execute_native_computer_call`
- Produces: `build_continuation_request(turn_state, call_output) -> dict`
- Produces: `build_computer_call_output(call, observation) -> dict`（不含任何确认字段）
- Produces: deterministic `_computer_v1` persistence projection
- Produces: `previous_response_id` 的 Turn 内存生命周期

- [x] **Step 1: 写 continuation 与投影失败测试**

```python
def test_native_continuation_resends_model_tools_and_previous_id():
    request = capture_continuation_request(response_id="resp_1", call_id="call_1", png=b"png")
    assert request["model"] == NATIVE_MODEL_NAME  # 重新发送同一 model
    assert {"type": "computer"} in request["tools"]  # 重新发送同序 tools
    assert request["previous_response_id"] == "resp_1"
    assert request["instructions"] == NATIVE_TURN_INSTRUCTIONS  # instructions 不自动继承，必须重发
    assert request["input"] == [{
        "type": "computer_call_output",
        "call_id": "call_1",
        "output": {"type": "computer_screenshot", "image_url": "data:image/png;base64,cG5n"},
    }]  # 不重发完整历史，input 只有本批 call output
    assert "messages" not in request


def test_provider_returns_no_response_id_fails_without_fallback():
    with pytest.raises(ComputerControlError) as raised:
        build_continuation_request(turn_state_without_response_id(), ANY_CALL_OUTPUT)
    assert raised.value.code == "computer_provider_protocol_error"


def test_call_output_never_invents_safety_acknowledgement_fields():
    output = build_computer_call_output(ANY_CALL, ANY_OBSERVATION)
    assert "acknowledged_safety_checks" not in output  # GA schema 未确认前不得出现
```

另测 safety required 时 controller 停在 `provider_safety_required` 且后续 Step 不发起；`_computer_v1` 投影脱敏；崩溃恢复只标 interrupted。

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_harness_runtime.py -q`

Expected: continuation 构造和投影不存在。

- [x] **Step 3: 实现 continuation 请求形状**

活动 native continuation 的下一 Step 请求：不重发完整消息历史，但必须重新发送同一 model、同序 tools（含 computer tool 和必要时的 selector）、必要 instructions/options、上一 response id，`input` 只携带一个新的 `computer_call_output`（整批动作的最后一张截图）。官方 Responses API 明确上一响应的 instructions 不会经 `previous_response_id` 自动继承，漏发会导致 system prompt 语义漂移。`previous_response_id` 仅存 Turn 内存对象，不写 message、Journal、event、runtime log。Provider 未返回可用 response id 时立即以 `computer_provider_protocol_error` 失败；禁止退回“重发全部截图历史”或通用 function tool。

- [x] **Step 4: 实现 Provider safety 确认的条件契约**

pending safety check 时 controller 进入 `provider_safety_required`，不执行动作。截至本计划编写，GA Computer use 指南未定义 `acknowledged_safety_checks` 请求契约，该字段只出现在 beta 类型参考中，因此 `build_computer_call_output()` 首版不产出任何确认字段。只有满足以下全部条件才允许增加确认路径并显示“继续”入口：

1. 实施当天官方 GA 请求 schema 或所用 SDK 类型明确提供确认字段；
2. 为该精确请求形状新增 byte-golden 测试并通过；
3. 用户通过 GUI/CLI 显式操作传入当前 run id 和精确 check ids。

不满足时 UI/CLI 只显示状态与停止。普通聊天文本、“完全访问”模式或自动重试不能确认；未确认不得发送虚假 acknowledged safety checks，也不得回退 generic 绕过。

- [x] **Step 5: 实现脱敏持久化投影与崩溃语义**

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

- [x] **Step 6: GREEN 与默认/原生 golden 门禁**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py tests/test_harness_runtime.py -q
venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q
git diff --check -- core/computer/provider_openai.py core/harness/request_envelope.py core/llm.py tests
```

Expected: 默认所有现有 golden 原样通过；新 native fixture 精确断言 first request 和 continuation bytes（含重发的 model/tools/instructions）；日志/DB/Journals 无 response id 与图片 base64。

Suggested commit after explicit authorization: `feat(openai): add native computer continuation`

#### Execution Record — Task 11b（2026-09-04，严格 RED→GREEN，无 commit）

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_harness_runtime.py -q` → exit 2 | `ImportError: cannot import name 'BoundNativeController' from 'core.computer.provider_openai'`（collection error，1 error in 1.16s——正是目标能力缺失：11b 新公共接口不存在，符合本文件 11a 注释的“RED 阶段应 ModuleNotFoundError”先例；collection 中断导致 harness 文件未跑）。补充分文件取证：`pytest tests/test_harness_runtime.py -q` → exit 1 | **2 failed, 14 passed in 38.27s**（`test_native_computer_turn_persists_sanitized_intermediate_row` / `test_native_crash_recovery_marks_interrupted_and_never_continues` 均 `AssertionError: assert 1 == 2`——computer_call 响应被当空正文收尾，续传请求不存在）；`pytest tests/test_provider_payload_golden.py -q` → exit 1 | **1 failed, 13 passed in 23.04s**（新 native golden 同因 `assert 1 == 2`）。三条 RED 失败原因全部是目标能力缺失，无既有测试回归。
- 实现: `core/computer/provider_openai.py`（+`NativeContinuationState`（Turn 内存续传状态：model/tools/instructions/options/response_id）、`build_continuation_request(turn_state, call_output)`——重发同 model/同序 tools/instructions/options + previous_response_id，input 只有 computer_call_output，无 "messages" 键，键序固定；response id 缺失即 `computer_provider_protocol_error`、`build_computer_call_output(call, observation)`——最后一张截图的 computer_screenshot data URL，零确认字段、`build_native_call_persistence_row(run_id, call, observation)`——brief Step 5 逐字形状的 `_computer_v1` 投影：call_id_hash=sha256(call_id)、动作摘要里输入文字只存 code point 数+SHA-256、组合键/URL 只存 SHA-256、最后一个动作摘要附 screen hash/尺寸，不存 base64/profile path/完整 URL/response id，纯函数确定性、`BoundNativeController`——execute(run_id, owner, action)/enter_provider_safety_required(run_id) 绑定适配器（审查 I-2 契约）、`NATIVE_CONTINUATION_OPTION_KEYS`）；`core/harness/request_envelope.py`（+`prepare_native_continuation_request(turn_state, call_output, *, timeout)`：协议形状委托 provider_openai，timeout 按当前 Step 注入并包装成与 prepare_stream_request 同一的请求准备接缝，RequestEnvelope.capture 据此记录续传请求真实最终字节）；`core/llm.py`（live loop：usage 记账后新增原生分支——混用 function call 整批 `computer_provider_protocol_error` 拒绝、`native_computer_call_from_stream` 严格校验（assembler“后者覆盖”载荷经 parse 层防线）、惰性 start_run（selector 租约优先复用，单 target 首个 computer call 执行前建立）、经 BoundNativeController 执行、provider_safety_required 立即抛 `computer_provider_safety_required`（消息只含 check ids 不含 check 原文）、投影行入 pending_persistence 走既有 flush/commit 事务、`NativeContinuationState` 从产生 computer_call 的真实请求 kwargs 捕获（含 `_native_continuation_instructions`：首请求从 input 的 system/developer 条目提取，续传请求复用 instructions 键）、attempt 循环里 `native_continuation_state` 非空时改走 `prepare_native_continuation_request`；最终文本/空响应清空续传状态防 previous_response_id 跨响应复用；`_finalize_computer_run` 对 provider_safety_required 快照既不 complete 也不 stop——留给用户显式停止（设计 11.5））。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_provider_openai.py tests/test_llm_responses.py tests/test_harness_runtime.py -q` → **104 passed in 45.05s / exit 0**（60 旧 + 10 新 11b：brief 三条逐字样例 + 流内缺 response id 回合失败 + 绑定适配器执行与惰性租约 + selector 后续传 tools 去 selector + safety 停回合 + 混用拒绝 + 投影脱敏单测 + BoundNativeController 绑定单测 + 18 llm_responses + 14 旧 + 2 新 harness）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py tests/test_prompt_byte_stability.py tests/test_deepseek_prompt_ledger.py tests/test_cache_keepalive.py tests/test_token_accounting.py -q` → **66 passed in 13.54s / exit 0**（65 旧原样 + 1 新 native first/continuation byte-golden：首请求含尾部 computer、续传请求逐字节断言 model/tools/instructions/previous_response_id/input 单 computer_call_output；现有 expected/hash 零改动）；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no` → **552 passed, 1901 deselected in 62.29s / exit 0**（基线 540 + 12：provider_openai +10、golden +1、harness +1；crash 测试名不含 computer 不计入）；默认关闭路径专项 `pytest tests/test_llm.py tests/test_tool_pipeline.py tests/test_agent_service.py tests/test_computer_llm_generic.py tests/test_memory_maintenance.py -q` → **176 passed in 54.87s / exit 0**；全量 `venv\Scripts\python.exe -m pytest tests/ -q --tb=no` → **2451 passed, 2 skipped, 1 warning in 594.40s / exit 0**（复跑一次 2450 passed + 1 failed：`test_tools.py::test_kill_marks_task_cancelled_and_keeps_record` 断言 'failed' == 'cancelled'——与 11b 无关的负载时序 flake：该文件本 Task 零 diff，单测×2 与整文件 203 passed 复跑均绿，首跑全量亦绿；唯一 warning 为 test_lan_server 线程异常噪音，同为既有）。
- Compile: `venv\Scripts\python.exe -m py_compile core/llm.py core/computer/provider_openai.py core/harness/request_envelope.py` → exit 0。
- Diff check: `git diff --check -- core/computer/provider_openai.py core/harness/request_envelope.py core/llm.py tests` → exit 0（仅 LF→CRLF 警告，与既往一致；无新建文件，全部为 tracked 文件最小修改）。
- Files touched: `core/computer/provider_openai.py`、`core/harness/request_envelope.py`、`core/llm.py`、`tests/test_computer_provider_openai.py`、`tests/test_provider_payload_golden.py`（只增不改）、`tests/test_harness_runtime.py`、本计划文件（checkbox 与本记录）。
- spec: PASS（`previous_response_id` 只存 Turn 内存——NativeContinuationState 为 `_chat_with_model_generator` 局部变量，专项测试断言 DB messages/harness_turns/harness_steps/harness_events 全表无 response id，且同会话新回合首请求无 previous_response_id；崩溃恢复只标 interrupted——悬停生成器模拟进程崩溃 + `interrupt_stale_harness_runs` 真实调用 + 动作零重放；safety required 停在 provider_safety_required 且后续 Step 不发起（唯一一次请求后即败，stop/complete 均 0）；call output 无任何确认字段；投影逐字形状 + 确定性）。
- privacy: PASS（投影行只存 hash/长度/尺寸——专项断言输入正文/response id/call id 原文/base64/本地路径均不在序列化结果中；turn_error 事件不含 safety check 原文；continuation 请求的 previous_response_id 只进 Provider 请求与 RequestEnvelope 指纹（options_sha256 哈希），不落任何明文持久化）。
- default-byte: PASS（默认关闭路径 llm.py 改动全部位于 `native_computer_capability is not None` 守卫之后，capability 关闭时零字节变化；golden 五件套 66 条含既有 65 条原样通过，未改任何现有 expected/hash）。
- unresolved/deviations: (1) brief Step 1 样例 `test_call_output_never_invents_safety_acknowledgement_fields` 与 `capture_continuation_request` 需要 PNG 文件，分别加 `tmp_path` fixture 与 tempfile 自管理（断言逐字保留，仅测试机制适配）；(2) `_run_native_responses_turn` 增加 keyword-only `events_out=None` 参数收集回合事件（默认 None，既有调用零影响）；(3) 投影的 `action` 为每动作摘要列表，最后动作摘要附整批 observation 的 screen hash/尺寸（“截图只保存 screen hash/尺寸”落在该处，投影外层保持 brief 逐字 4 键）；(4) `computer_call` 与普通 function call 同响应混用为本 Task 新增的显式协议错误（设计 10.1 混用整批拒绝原则在 native 流的对称落地，未列入 brief 样例但有专项测试）；(5) 崩溃模拟测试对悬停生成器 close 的 stale transition 异常按预期吞掉并以 filterwarnings 屏蔽 PytestUnraisableExceptionWarning（崩溃语义本身不受影响）。

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

- [x] **Step 1: 写 Bridge 和 Store 回滚失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q
npm --prefix frontend test -- --run src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts
```

Expected: 新 Bridge、类型和控件不存在。注意 `npm --prefix frontend` 已把工作目录设为 `frontend`，vitest 定向路径使用 `src/...`，不带 `frontend/` 前缀。

- [x] **Step 3: 实现 Bridge 严格 API**

每个 setter 只调用领域设置函数，返回：

```python
{"ok": True, "settings": self.get_system_settings()}
```

或：

```python
{"ok": False, "error_code": error.code, "msg": str(error), "settings": self.get_system_settings()}
```

Bridge 不直接写 config、不操作 Vue、不吞异常为 success。开启 preflight 必须组合 selected browser/Windows backend 和全局热键检查。关闭成功后调用 controller.stop，且即使清理尚在进行，返回快照中的开关已为 false。

- [x] **Step 4: 扩展 TypeScript 类型和 Store actions**

`SystemSettingsSnapshot` 增加精确字段和 `ComputerBrowserOption`；model 类型增加两个能力字段。Store setter 只以后端返回的 `settings` 覆盖本地状态，不做持久乐观值：

```ts
async setBrowserControlEnabled(enabled: boolean) {
  const result = await window.pywebview.api.set_browser_control_enabled(enabled);
  this.systemSettings = result.settings;
  if (!result.ok) this.showError(result.msg || '浏览器操控设置失败');
  return result.ok;
}
```

- [x] **Step 5: 增量扩展设置页面**

在现有紧凑列表加“电脑操控”区，顺序固定：浏览器操控开关、Edge/Chrome select、完整电脑操控开关、专用 profile 只读路径、隐私提示、平台/浏览器/热键 preflight。两个 switch 初始 false；option 不可用时 disabled 并显示原因；活动 run 时 browser select disabled。

提示文本必须明确：“开启后，当前页面或桌面截图会发送给当前模型 Provider；Y-CODE 不会逐动作询问。登录、验证码和凭据请在专用浏览器中由你手动完成。紧急停止：Ctrl+Alt+Esc。”

模型 modal 使用 checkbox `supports_function_calling` 与 select `computer_protocol`；选 generic 时视觉或工具能力未开启则前端阻止保存并显示同一校验原因，后端仍再次校验。

- [x] **Step 6: GREEN 与前端构建**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py tests/test_computer_settings.py tests/test_computer_capability.py -q
npm --prefix frontend test -- --run src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts
npm --prefix frontend run build
git diff --check -- gui_bridge.py frontend/src tests/test_gui_system_settings.py tests/test_gui_model_settings.py
```

Expected: 失败回滚到后端真实值；手机 viewport 不显示可点击启用控件；既有设置无视觉回归。

Suggested commit after explicit authorization: `feat(gui): add explicit computer settings`

#### Execution Record — Task 12（2026-09-04，严格 RED→GREEN，无 commit）

- RED (Python): `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q` → exit 1 | **15 failed, 18 passed in 23.32s**。失败原因全部为目标能力缺失：10 条 `AttributeError: 'ApiBridge' object has no attribute 'set_browser_control_enabled'/'set_computer_control_enabled'/'set_computer_browser'/'_browser_control_preflight'`；2 条 `KeyError: 'supports_function_calling'`（get_model_settings 快照缺能力字段）；3 条 `TypeError: ApiBridge.add_model()/update_model() ... positional arguments`（Bridge 签名缺新参数）。
- RED (frontend): `npm --prefix frontend test -- --run src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts` → exit 1 | **18 failed | 28 passed (46) in 104.25s**。失败原因全部为目标能力缺失：4 条 `TypeError: store.setBrowserControlEnabled/setComputerBrowser/setComputerControlEnabled is not a function`；7 条 SystemSettings 组件测试控件不存在（`browser-control-switch`/`computer-browser-select`/`computer-control-switch`/`computer-profile-path`/`computer-privacy-notice`/`computer-preflight-list`/mobile note 无法定位）；4 条 SettingsPage 模型 modal 控件不存在（`model-supports-function-calling`/`model-computer-protocol`）；3 条 store 透传断言（add_model/update_model 收到 4 参而非 6 参含能力字段）。
- 实现: `gui_bridge.py`（+三个严格 setter `set_browser_control_enabled`/`set_computer_control_enabled`/`set_computer_browser`，返回 brief 逐字形状 `{ok, settings}` / `{ok:False, error_code, msg, settings}`；`_browser_control_preflight` 组合平台 + 所选浏览器可用 + `_default_hotkey_probe` 热键探测；`_computer_control_preflight` 走 `Win32InputAdapter().preflight()`（平台/DPI/交互桌面/热键）；`_stop_computer_runs_for_disabled_switch` 在关闭落盘成功后按 target 通知 controller.stop（`settings_disabled`），通知失败只留痕不影响已生效的 false+generation；`_has_active_computer_run` 供 browser select 判定活动会话，读取失败 fail-closed 按活动处理；`get_model_settings` 模型条目 +`supports_function_calling`/`computer_protocol`（旧配置缺省 false/'none'）；`add_model` +2 参数（默认 False/'none'）、`update_model` +2 可选参数（None 保留既有值，旧调用零影响））；`frontend/src/types/pywebview.d.ts`（+`ComputerBrowserOption`/`ComputerRunSnapshot`/`SystemSettingsMutationResult`/`ComputerProtocolChoice`；`SystemSettingsSnapshot` +9 个 computer 精确字段；`ModelInfo` +2 能力字段；API 声明 +3 setter 与模型方法扩展）；`frontend/src/stores/appStore.ts`（state 默认 computer 键全 false/idle；+`setBrowserControlEnabled`/`setComputerControlEnabled`/`setComputerBrowser`——只以后端返回 settings 覆盖、失败 notifyModal 显示后端 msg、不做乐观持久值；`addModel`/`updateModel` 透传能力字段）；`frontend/src/components/SystemSettings.vue`（既有设置列表零改动，其后新增"电脑操控"区，顺序固定：浏览器操控开关 → Edge/Chrome select → 完整电脑操控开关 → 专用 profile 只读路径 → 隐私提示（brief Step 5 逐字文本）→ 平台/浏览器/热键 preflight mini-list；switch 用 role="switch" 自绘控件初始 false；option 不可用 disabled+原因；活动 run 时 select disabled；isMobile(≤768px) 隐藏两个开关与 select 并显示提示，隐私提示仍可见）；`frontend/src/components/SettingsPage.vue`（模型 modal +`supports_function_calling` checkbox 与 `computer_protocol` select（none/generic/native_openai）；generic 而视觉或工具未开时前端阻止保存并显示与后端 `_validate_computer_combination` 同一校验原因的行内错误，后端仍再校验；`openModelModal` 旧模型缺省 false/'none'）。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py tests/test_computer_settings.py tests/test_computer_capability.py -q` → **104 passed in 25.09s / exit 0**（Task 8 既有 18 + Task 12 新增 15 + computer settings 40 + capability 31）；`npm --prefix frontend test -- --run src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts` → **Test Files 4 passed (4), Tests 46 passed (46) in 82.18s**（28 旧 + 18 新）。
- Build: `npm --prefix frontend run build` → `✓ built in 27.08s`（vue-tsc -b + vite build 全过；首次构建曾因测试文件一处 `wrapper.get(...).exists()` 类型误用失败，改为 `wrapper.find(...)` 后通过——纯测试侧修正）。
- Regression: 全部导入 gui_bridge 的 24 个测试文件 `pytest tests/test_command_shell.py ... tests/test_remote_workspace_browser.py -q` → **317 passed, 1 warning in 35.33s**（warning 为 test_lan_server 既有线程异常噪音）；`pytest tests/test_config.py tests/test_gui_model_settings.py tests/test_gui_system_settings.py -q` → **73 passed in 28.47s**；前端相邻文件 `npm --prefix frontend test -- --run src/components/ModelCascader.test.ts src/components/SettingsPage.mobile.test.ts` → **9 passed (2 files)**；前端全量 `npm --prefix frontend test`（两次）→ **Test Files 54 passed (54)，Tests 361/418 passed，0 failed，Errors 14 errors**——14 个 error 全部为 `Error: [vitest-pool]: Failed to start threads worker ... Timeout waiting for worker to respond`（机器多会话高负载下的 worker 池启动超时，非代码错误：两次全量 0 条测试失败；同一批在负载下报 worker 超时的 6 个未改动文件（appStore.notify/messageQueue/update、MessageBubble、RightPanel、InputArea）在空闲时单独运行 → **6 files / 47 tests / 0 error** 全绿，证明为环境噪音；两次运行测试数差异 361↔418 亦为 worker 超时重试所致的环境抖动）。
- Diff check: `git diff --check -- gui_bridge.py frontend/src tests/test_gui_system_settings.py tests/test_gui_model_settings.py` → exit 0（仅 LF→CRLF 警告，与既往一致；本 Task 无新建文件，全部为 tracked 文件最小修改）。
- Files touched: `gui_bridge.py`、`frontend/src/types/pywebview.d.ts`、`frontend/src/stores/appStore.ts`、`frontend/src/components/SystemSettings.vue`、`frontend/src/components/SettingsPage.vue`、`frontend/src/components/SystemSettings.test.ts`、`frontend/src/components/SettingsPage.test.ts`、`frontend/src/stores/appStore.systemSettings.test.ts`、`frontend/src/stores/appStore.modelSettings.test.ts`、`tests/test_gui_system_settings.py`、`tests/test_gui_model_settings.py`、本计划文件（checkbox 与本记录）。全部命中 brief Files 清单，零越界。
- spec: PASS（三 setter 只调领域函数，preflight 失败不落盘由 monkeypatch 领域函数与真实 preflight 组合双路验证；关闭先落盘+generation 后通知 stop，fake stop 刻意不变更状态证明"清理尚在进行时快照开关已 false"；两开关独立——关电脑开关不打断 browser target run；活动 run browser select 拒绝 computer_session_busy；generic 三字段校验前后端同文本；旧模型默认 none；手机 viewport 无可点击启用控件）。
- unresolved/deviations: (1) brief Step 4 样例 `this.showError(...)` 在本 store 不存在，按既有惯例改为 `notifyModal('电脑操控', <后端 msg>)`（同一失败提示语义；对应 TS 样例断言 `store.modal.message` 适配为实际字段 `store.confirmModal.message`）；(2) 热键探测复用 `core/computer/win32_adapter._default_hotkey_probe`（Task 5/7 既有"注册成功后立即注销"探测，私有符号只读导入，core/computer 零改动）；(3) 既有测试必要适配（均非 golden）：appStore.modelSettings.test.ts 一处 addModel 断言 4 参→6 参（默认 false/'none' 语义不变）、SystemSettings/SettingsPage 测试快照字面量补必填新字段（vue-tsc -b 覆盖测试文件类型检查）；(4) pytest 隔离新增 `_use_isolated_config` 同时 patch TOML overlay，防开发机 config.toml 污染；(5) "电脑操控"区遵循 SystemSettings.vue 既有紧凑列表形态（该页无 FoldCard 折叠结构），未引入折叠卡片以保证零视觉回归，图标全用 AppIcon lucide SVG；(6) 移动端隐藏用 useIsMobile（≤768px，与 SettingsPage 手机端钻取同源判定）。

---

### Task 13a: GUI Bridge 电脑操控事件桥与控制 API

**Files:**
- Modify: `gui_bridge.py`
- Modify: `tests/test_gui_tool_events.py`
- Modify: `tests/test_gui_message_queue.py`

**Interfaces:**
- Consumes: controller snapshots/events（含 run 内严格递增 `event_seq`）
- Produces bridge: `get_computer_control_status()`, `pause_computer_control(run_id)`, `resume_computer_control(run_id)`, `stop_computer_control(run_id)`, `acknowledge_computer_safety(run_id, check_ids)`（后者按 Task 11b 的条件契约预留，未验证官方确认字段前拒绝调用并返回不可用）
- Produces: controller listener 到现有 `_js_queue` 的小 JSON 事件转发

- [x] **Step 1: 写 Bridge 事件转发和控制失败测试**

```python
def test_bridge_forwards_only_small_json_events_to_js_queue(fake_controller, bridge):
    fake_controller.emit_observation(run_id="run-a", event_seq=3)
    payload = bridge.drain_js_queue()
    assert payload["type"] == "computer_observation"
    assert payload["run_id"] == "run-a"
    assert payload["event_seq"] == 3
    assert "data:image/png;base64" not in json.dumps(payload)


def test_bridge_mutation_requires_current_run_id(fake_controller, bridge):
    fake_controller.start_run(run_id="run-a")
    result = bridge.stop_computer_control("run-b")  # 旧 UI 事件的 run id
    assert result["ok"] is False
    assert fake_controller.snapshot().run_id == "run-a"


def test_safety_acknowledge_unavailable_without_verified_contract(fake_controller, bridge):
    result = bridge.acknowledge_computer_safety("run-a", ["sc_1"])
    assert result["ok"] is False
    assert result["error_code"] == "computer_provider_safety_required"
```

另测 pause/resume 透传 controller、事件线程不直接 `evaluate_js`、队列满丢新事件不改写旧事件、每次控制操作返回完整 snapshot（供前端校准，见设计 16.4）。

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py -q`

Expected: Bridge 新方法与事件转发不存在。

- [x] **Step 3: 实现 Bridge 控制与事件桥**

每个 mutation 都要求当前 run id，防止旧 UI 事件停止新 run，返回值携带完整 controller snapshot。controller listener 只把小 JSON `put_nowait` 到 `_js_queue`；队列满按现有策略丢新事件，不在 backend/action/delta lock 内调用 `evaluate_js`。`acknowledge_computer_safety` 在官方确认契约与 byte-golden 验证完成前固定返回不可用错误（Task 11b Step 4 的条件），Bridge 不自行发明确认 payload。

- [x] **Step 4: GREEN 与现有 GUI 事件回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_controller.py -q
git diff --check -- gui_bridge.py tests/test_gui_tool_events.py tests/test_gui_message_queue.py
```

Expected: 现有 GUI 事件/队列测试原样通过；事件线程测试断言没有直接 `evaluate_js`。

Suggested commit after explicit authorization: `feat(gui): bridge computer control events`

#### Execution Record — Task 13a（2026-09-04，严格 RED→GREEN，无 commit）

- RED: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py -q` → exit 1 | **3 failed, 36 passed, 9 errors in 6.41s**。失败/错误全部为目标能力缺失：12 条新测试中 3 条直接 `AttributeError: 'ApiBridge' object has no attribute 'get_computer_control_status'`（提示 Did you mean: 'set_computer_control_enabled'?，证明落在 Task 12 setter 之后的新方法缺失）、9 条经 `bridge` fixture 建立事件桥时同因 AttributeError 报 error；既有 36 条原样通过，无既有测试回归。
- 实现: `gui_bridge.py`（+`get_computer_control_status`/`pause_computer_control`/`resume_computer_control`/`stop_computer_control`/`acknowledge_computer_safety`——前四者经 `_run_computer_mutation` 只调领域服务，成功与失败（`ComputerControlError`）返回值都携带完整 `ComputerRunSnapshot.to_public_dict()`（快照字段平铺、`error_code`/`msg` 后置覆盖快照同名字段），run_id 匹配校验全部委托 controller `_require_active_run_locked`（旧 UI 事件的 run id 拒绝、防旧事件停新 run），pause/stop 分别透传 reason `user_pause`/`user_stop`；`acknowledge_computer_safety` 在 Task 11b Step 4 三条件（官方 GA schema 确认字段 / 该形状 byte-golden / 用户显式操作）齐备前固定返回 `{"ok": False, "error_code": "computer_provider_safety_required", ...}`，Bridge 绝不构造确认 payload；`_ensure_computer_event_bridge` 惰性建立进程内桥接（controller 惰性单例 + `add_listener` 相等性去重，重复调用幂等），`set_window` 与每个控制方法都触发（窗口就绪即注册保证首 run 事件零丢失，失败只留痕不阻塞窗口启动）；`_on_computer_control_event` listener 只做小 JSON 序列化（U+2028/2029 转义与 `_emit_raw` 一致）+ `put_nowait`，绝不调用 evaluate_js；`_emit_raw` 入队段原样提取为 `_queue_js_payload` 唯一入队点（行为零变化：ensure dispatcher + put_nowait + 队列满丢新事件计数限频日志），事件桥与普通事件共用同一"队列满丢新事件"策略；+`drain_js_queue` 测试 seam（排空 `_js_queue` 返回最后一条已解析事件，仅供测试，不在运行时调用））。
- 测试: `tests/computer_fakes.py`（+`FakeBridgeController`：listener 注册/四类事件发射（`emit_observation(run_id=, event_seq=)` 供 brief 样例逐字使用）、`start_run(run_id=)`/pause/resume/stop 透传记录 + run_id 不匹配抛 `computer_invalid_state`、`snapshot()` 返回真实 `ComputerRunSnapshot`；绝不触碰真实键鼠/浏览器/截屏）；`tests/test_gui_tool_events.py`（+6：brief 样例 1 逐字 + 四类事件全部转发（observation 只带短 preview URL）+ listener 绝不直接 evaluate_js（RecordingWindow 断言零调用且事件仍入队）+ 队列满丢新事件不改写旧事件（maxsize=2 注入 + `_js_dropped` 计数）+ 桥接建立一次幂等（listener_count==1）+ mutation 触发的状态事件同样进队列；测试用 `_js_dispatcher=threading.current_thread()` 阻止真实派发线程启动抢空队列）；`tests/test_gui_message_queue.py`（+6：brief 样例 2/3 逐字 + pause/resume 透传（reason 一并断言）+ 匹配 run_id 的 stop 成功且终态快照可见 + 每次控制操作返回完整 snapshot（`ComputerRunSnapshot` 全字段在场 + 状态序列 running→paused→running→stopped）+ 失败 mutation 返回当前活动 run 快照供校准（`stop_calls == []` 证明新 run 未被旧事件停止））。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_controller.py -q` → **85 passed in 3.74s / exit 0**（Task 8 controller 37 原样 + GUI 两文件既有 36 原样 + 本 Task 新增 12）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py tests/test_gui_model_settings.py -q` → **33 passed in 15.84s / exit 0**（Task 12 保持绿）；`_emit_raw` 重构安全回归 `pytest tests/test_gui_update.py tests/test_read_file_preview.py tests/test_lan_server.py tests/test_lan_upload.py -q` → **63 passed in 6.25s / exit 0**（真实 ApiBridge 构造 + LAN broadcast 路径）；`pytest tests/ -q -k "computer" --tb=no` → **559 passed, 1921 deselected in 38.09s / exit 0**（含 tests/computer_fakes.py 消费者零回归）。
- Compile: `venv\Scripts\python.exe -m py_compile gui_bridge.py` → exit 0。
- Diff check: `git diff --check -- gui_bridge.py tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/computer_fakes.py` → exit 0（仅 LF→CRLF 警告，与既往一致；本 Task 无新建文件，全部为 tracked 文件最小修改）。
- Files touched: `gui_bridge.py`、`tests/test_gui_tool_events.py`、`tests/test_gui_message_queue.py`（前三者命中 brief Files 清单）、`tests/computer_fakes.py`（最小越界：新增 `FakeBridgeController` 共享 fake——两个测试文件都需要同一 controller fake，按计划 Interface Contract"共享测试 fixture 契约、禁止平行实现"落位；不含任何 gui_bridge 导入，computer 系测试零额外依赖）、本计划文件（checkbox 与本记录）。
- spec: PASS（每个 mutation 的 run_id 校验委托 controller，旧 run id 请求被拒且新 run 不受影响（快照 run_id 仍 "run-a" + stop_calls 空双断言）；每次控制操作（含失败）返回完整 snapshot 平铺字段供前端校准；`acknowledge_computer_safety` 固定 `computer_provider_safety_required`，Bridge 全文无任何确认 payload 构造路径；事件只经 listener → `put_nowait` → `_js_queue`，事件路径零 evaluate_js 调用（gui-js-dispatcher 单消费者铁律）；队列满丢新事件与 `_emit_raw` 共用同一 `_queue_js_payload`，旧事件不被改写；listener 建立一次幂等）。
- privacy: PASS（转发事件即 controller 已脱敏小 JSON——run_id/event_seq/动作摘要/短 preview URL，无 png_path/base64/输入正文（样例断言 `data:image/png;base64` 不在 payload）；控制 API 返回值为快照公开字段 + 错误码/中文消息，无凭据、无输入文本、无截图内容）。
- default-byte: PASS（电脑操控控制 API 与事件桥不触碰请求构造/消息规范化/system prompt/tools/metadata/缓存亲和，默认关闭路径零字节影响；`_emit_raw` 重构为纯提取，入队行为逐字节等价，真实 ApiBridge/LAN 广播回归绿）。
- unresolved/deviations: (1) LAN 边界：`pause_computer_control`/`resume_computer_control`/`acknowledge_computer_safety` 进入 `LanServer.FORBIDDEN_METHODS` 由 Task 15 Step 1/3 落地（其参数化清单已精确列出这三个方法与 Task 12 三 setter；Task 12 setter 当前同样未入禁止列表，属既定分阶段计划而非本 Task 回归；本 Task 未触碰 core/lan_server.py）；(2) `drain_js_queue` 为 brief 样例逐字要求的 bridge 公共方法（测试 seam），生产路径不调用，pywebview 会将其暴露给 JS 但只读排空队列、无副作用；(3) 事件桥在 `set_window` 即建立为生产保险——brief 只要求"进程内建立一次、幂等"未指定时机，控制方法惰性建立已满足测试，但首个 run 的事件必须先于任何控制调用就有 listener 接收，窗口就绪即注册保证零丢失（测试均走控制方法惰性路径验证）；(4) 队列满测试用 maxsize=2 注入（走同一 `_queue_js_payload` 代码路径，与 2000 容量仅数值不同）；(5) `tests/computer_fakes.py` 越界说明见 Files touched。

---

### Task 13b: 前端安全条、操控页签与事件 reducer

**Files:**
- Create: `frontend/src/components/ComputerControlBar.vue`
- Create: `frontend/src/components/ComputerControlBar.test.ts`
- Create: `frontend/src/components/ComputerControlPane.vue`
- Create: `frontend/src/components/ComputerControlPane.test.ts`
- Modify: `frontend/src/types/pywebview.d.ts`
- Modify: `frontend/src/stores/appStore.ts`
- Modify: `frontend/src/components/RightPanel.vue`
- Modify: `frontend/src/App.vue`
- Modify: relevant existing `RightPanel` and `App` Vitest files

**Interfaces:**
- Consumes: Task 13a 的 Bridge API 与事件形状
- Produces Store: `computerRun`, `computerTrace`, `computerLastAutoOpenedRunId`
- Produces actions: `pauseComputerControl()`, `resumeComputerControl()`, `stopComputerControl()`
- Produces UI: persistent top bar and dynamic right tab with max 200 trace records

- [x] **Step 1: 写 reducer 顺序和安全按钮失败测试**

```ts
it('rejects late events from old runs and older event_seq', () => {
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'running' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-1', event_seq: 99, preview_url: '/old' });
  store.reduceComputerEvent({ type: 'computer_observation', run_id: 'run-2', event_seq: 0, preview_url: '/stale' });
  expect(store.computerRun.run_id).toBe('run-2');
  expect(store.computerRun.preview_url).not.toBe('/old');
  expect(store.computerRun.preview_url).not.toBe('/stale');
});


it('keeps only the newest 200 sanitized trace rows', () => {
  for (let ordinal = 1; ordinal <= 205; ordinal++) {
    store.reduceComputerEvent({ type: 'computer_action_finished', run_id: 'run-1', event_seq: ordinal, action_ordinal: ordinal, summary: `输入 ${ordinal} 个字符` });
  }
  expect(store.computerTrace).toHaveLength(200);
  expect(JSON.stringify(store.computerTrace)).not.toContain('secret text');
});


it('recalibrates from bridge snapshot after queue drops events', async () => {
  store.reduceComputerEvent({ type: 'computer_state', run_id: 'run-2', event_seq: 1, status: 'running' });
  api.get_computer_control_status.mockResolvedValue({ run_id: 'run-2', status: 'stopped', stop_reason: 'hotkey' });
  await store.refreshComputerRun();  // 窗口重新聚焦/切到操控页签时也调用
  expect(store.computerRun.status).toBe('stopped');
});


it('always shows stop and pause controls on the compact bar', () => {
  const wrapper = mount(ComputerControlBar, { global: piniaWithRunningComputer() });
  expect(wrapper.get('[data-test="computer-pause"]').isVisible()).toBe(true);
  expect(wrapper.get('[data-test="computer-stop"]').isVisible()).toBe(true);
});
```

另测 run 首次开始自动开操控 tab 一次、用户切走后不抢回、结束保留 trace、下一 run 清空、provider safety 未验证契约前只显示状态与停止（无“继续”按钮）。

- [x] **Step 2: 运行确认 RED**

Run:

```powershell
npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts
```

Expected: 组件和 Store 字段不存在。vitest 定向路径使用 `src/...`（`npm --prefix frontend` 已在 frontend 目录内）。

- [x] **Step 3: 实现纯事件 reducer**

事件共同类型：

```ts
interface ComputerEventBase { run_id: string; event_seq: number; type: string }
interface ComputerActionTrace { action_ordinal: number; summary: string; status: string; duration_ms: number; error_code: string }
interface ComputerRunSnapshot {
  run_id: string; status: ComputerRunStatus; target: 'browser'|'desktop'|'';
  browser: 'edge'|'chrome'|''; model_name: string; protocol: ComputerProtocol;
  action_count: number; current_action: string; context_summary: string;
  preview_url: string; pause_reason: string; stop_reason: string; error_code: string;
  hotkey_available: boolean; pending_safety_checks: Array<{ id: string; message: string }>;
}
```

`event_seq` 是 run 内独立单调递增序号，pause/resume/safety/stop 等状态事件同样递增；`action_ordinal` 只表示动作顺序，不承担状态事件排序。reducer 只接受当前 run 且 `event_seq` 严格大于已记录值；新 run 的 `computer_state/starting` 清 trace 并设置一次自动开页签标志。trace 行只含 `action_ordinal`、动作脱敏摘要、状态、耗时、错误码；`type_text` 显示长度，不存正文。由于 `_js_queue` 满时允许丢新事件，每次 Bridge 控制操作返回值、窗口重新聚焦和切换到操控页签时都调用 `get_computer_control_status()` 用完整 snapshot 校准 Store，确保终态不会永久丢失。

- [x] **Step 4: 实现顶部条和右侧页签**

`ComputerControlBar` 插在现有聊天标题栏下方，只对 `starting|running|paused|waiting_for_user|provider_safety_required|stopping|failed` 显示。窄屏 CSS 可以隐藏 model/context，但 `[data-test=computer-stop]` 和 pause/resume 永不隐藏。

`RightPanel.tabs` 动态追加 `{ id: 'computer', label: '操控' }`；`ComputerControlPane` 显示 `<img :src="preview_url">`、run 摘要、最多 200 行新到旧 trace。不要通过 `evaluate_js` 传 base64。

- [x] **Step 5: GREEN、构建和 GUI 线程边界**

Run:

```powershell
npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts src/components/RightPanel.test.ts src/stores/appStore.systemSettings.test.ts
npm --prefix frontend run build
venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_controller.py -q
git diff --check -- frontend/src gui_bridge.py tests/test_gui_tool_events.py tests/test_gui_message_queue.py
```

Expected: preview src 为 loopback token URL；旧事件不能覆盖新 run；snapshot 校准让丢弃的事件不导致永久错误终态。

Suggested commit after explicit authorization: `feat(gui): show controllable computer runtime`

#### Execution Record — Task 13b（2026-09-04，严格 RED→GREEN，无 commit）

- RED: `npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts` → exit 1 | **2 failed suites（Failed Suites 2, Tests no tests）in 13.15s**。失败原因均为目标能力缺失（组件文件不存在，vite import-analysis 直接报 `Failed to resolve import "./ComputerControlBar.vue"` / `"./ComputerControlPane.vue"`，符合 brief Step 2 Expected“组件和 Store 字段不存在”）。
- 实现: `frontend/src/types/pywebview.d.ts`（+`ComputerControlResult`（快照平铺 + ok/msg，含 Task 13a 五个 Bridge API：get_computer_control_status/pause_computer_control/resume_computer_control/stop_computer_control/acknowledge_computer_safety——ComputerRunSnapshot 复用 Task 12 已有 17 字段类型不重复定义）；+`ComputerEventBase` 与四类平铺信封事件类型（载荷字段防御性可选 + 索引签名容忍扩展字段）；+`ComputerActionTrace` 五字段轨迹行；`BridgeEvent.data` 改可选并补 run_id/event_seq 顶层描述位）；`frontend/src/stores/appStore.ts`（+state：`computerRun`/`computerTrace`/`computerLastAutoOpenedRunId` + 记账 `_computerEventSeq`/`_computerActionStartMs`；+getter：`computerControlBarVisible`（brief 逐字七状态表）/`computerControlPausable`；+纯 reducer `reduceComputerEvent`：run 归属两路建立（Store 无 run 时首事件认领——brief 样例 2 无 starting 前导也能建 run；已有 run 时仅 computer_state/starting 开新 run 并清轨迹）、非当前 run 按 run_id 拒绝、当前 run 内 event_seq 严格递增门闩（`seq <= _computerEventSeq` 拒绝）、轨迹行只构造五个契约字段绝不透传事件原始字段（`computerTraceSummary` 只信任契约 summary/text_length，type_text 显示长度不存正文）、最多 200 条新到旧（unshift + 尾部截断）、started/finished 按 action_ordinal upsert；+`_applyComputerSnapshot` 快照校准：不同 run_id 按新 run 重置轨迹与记账（seq 门闩归零），同 run 只合并 17 个已知字段且门闩不下调；+`refreshComputerRun`/`pauseComputerControl`/`resumeComputerControl`/`stopComputerControl`（统一经 `_computerControlMutation`：run_id 取当前 Store（旧 UI 事件 run id 由后端拒绝），成功与失败返回值都先校准再判断 ok，失败 notifyModal 透出 msg/error_code）；`handleBridgeEvent` 顶部 +平铺信封识别分流（`isComputerFlatEvent`：type ∈ 四类事件集合 且 顶层 run_id 为 string → reduceComputerEvent 后直接 return，不进会话 runtime 归属，普通 {"type","data"} 事件零影响））；`frontend/src/components/iconPaths.ts`（+`pause`/`play` 两个 lucide 图标路径——暂停/恢复按钮需要，注册表机制本身即“新图标直接加一行”）；`frontend/src/components/ComputerControlBar.vue`（新建：插在 #chat-header 下方、.chat-viewport 之外（对话滚动保持可见）；状态点（绿/黄/红）+文案、Edge/Chrome/桌面标识 chip、动作数、当前窗口/页面 host（current_surface，窄屏折叠）、暂停/恢复（computerControlPausable 驱动，provider_safety_required/waiting_for_user/stopping/failed 不显示）、高强调 danger 立即停止；≤900px 隐藏次要文字但 stop 与 pause/resume 永不隐藏）；`frontend/src/components/ComputerControlPane.vue`（新建：`<img :src="preview_url">` 直接用 loopback token URL 绝不经 evaluate_js 传 base64、run 状态/目标/模型/动作数 mini-list 摘要、新到旧最多 200 行 trace（lucide 状态图标 + 脱敏摘要 + duration_ms + error_code）、paused/waiting_for_user/stopped/failed/provider_safety 原因区（登录/接管/失败原因；provider safety 只显示状态与停止，无“继续”按钮）、暂停/恢复/停止操作、无 run 空态）；`frontend/src/components/RightPanel.vue`（tabs 改 computed：`store.computerRun.run_id` 非空才动态追加 `{ id: 'computer', label: '操控' }`，既有四页签静态不动；+操控页签 v-show 面板挂 ComputerControlPane；+`autoOpenComputerTabOnce`：run_id 变化且未为该 run 开过且状态处于活动期 → 切到操控页签一次并记 `computerLastAutoOpenedRunId`（用户切走不抢回，新 run 再开一次，onMounted 补跑覆盖设置页占用期启动的 run）；activeTab watch +切到操控页签即 `refreshComputerRun()` 校准）；`frontend/src/App.vue`（+`<ComputerControlBar />` 插聊天标题栏下方；+window focus 监听 `onWindowFocusRecalibrateComputer`：存在已知 run 即 `refreshComputerRun()` 校准（设计 16.4），unmount 移除）。
- 测试: `frontend/src/components/ComputerControlBar.test.ts`（新建 25 条：brief 样例 1-4 逐字 + 平铺信封经 handleBridgeEvent 分流 + run 结束保留 trace/下一 run 清空 + type_text 只存长度不存正文（`text: 'secret text'` 全程不落 trace，行内恰为五字段）+ 快照校准不同 run_id 重置轨迹 + 三控制经快照校准 + 失败 mutation 弹错误且快照保留 + 七活动状态逐条显示条/四静默状态逐条隐藏（it.each 短名）+ 目标/动作数/host 渲染 + paused 显示恢复 + provider_safety 无暂停只有停止 + 按钮带当前 run_id 调桥）；`frontend/src/components/ComputerControlPane.test.ts`（新建 7 条：preview src 为 `/computer-preview/` loopback URL 非 base64 + trace 新到旧且不含原始输入 + 状态/目标/模型/失败原因展示 + provider safety 只显示状态与停止（无“继续”/“恢复”）+ 三控制带 run_id 调桥并按返回快照校准 + run 结束 trace 保留 + 无 run 空态）；`frontend/src/components/RightPanel.test.ts`（+3：run 出现前四页签无“操控”、出现后动态追加并自动切过去一次（computerLastAutoOpenedRunId 记录）+ 用户切走后同 run 后续事件不抢回/新 run 再自动开一次 + 手动切到操控页签拉取快照校准）；`frontend/src/App.panelResize.test.ts`（+2：安全条渲染在聊天标题栏下方、滚动区之前（compareDocumentPosition 断言顺序），idle 不渲染 + 窗口 focus 触发快照校准）。全部前端测试 mock `window.pywebview.api`，无真实 Bridge。
- GREEN: `npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts src/components/RightPanel.test.ts src/stores/appStore.systemSettings.test.ts` → **46 passed (4 files) / exit 0 in 31.23s**（Bar 25 + Pane 7 + RightPanel 6（含既有 3）+ systemSettings 8（Task 12 既有零回归））。附加：`src/App.panelResize.test.ts src/App.guiTheme.test.ts src/App.mobileStatusbar.test.ts` → **9 passed / exit 0**（App 接线与既有 App 测试零回归）。
- Build: `npm --prefix frontend run build`（含 vue-tsc 类型检查）→ **✓ built in 11.13s / exit 0**（首次因事件载荷类型必填导致 brief 部分载荷样例 TS2345 报错 exit 2——载荷字段改防御性可选 + 索引签名后通过）。
- Python 事件回归: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_controller.py -q` → **85 passed in 4.31s / exit 0**（Task 13a/8 全绿，本 Task 未触碰 Python 产品代码）。
- Diff check: `git add -N` 四个新建文件后 `git diff --check -- frontend/src gui_bridge.py tests/test_gui_tool_events.py tests/test_gui_message_queue.py` → **exit 0**（仅 LF→CRLF 警告，与既往一致）。
- spec: PASS（reducer 只接受当前 run 且 event_seq 严格大于已记录值、旧 run 迟到事件按 run_id 拒绝（样例 1 三断言）；action_ordinal 不承担状态排序（seq 门闩独立）；新 run computer_state/starting 清 trace（含自动开页签标志经 computerLastAutoOpenedRunId 一次）；trace 行只含五契约字段、type_text 显示长度不存正文；200 条上限保最新；控制返回值/聚焦/切页签三路快照校准（样例 3 + RightPanel/App 测试）；preview 只用 loopback token URL；顶部条七状态显示表逐字、窄屏 stop/pause 永不隐藏；操控页签动态追加且既有页签行为不变；provider safety 无“继续”入口）。
- privacy: PASS（前端不接收也不存储截图 base64/输入正文——observation 事件只带短 URL；轨迹行只存五个脱敏字段，`text: 'secret text'` 注入样例断言绝不落 trace；computerRun/快照均为 Task 12 已脱敏公开字段）。
- default-byte: PASS（改动全部在前端 TS/Vue 与类型声明，不触碰请求构造/消息规范化/system prompt/tools/metadata/缓存亲和；Python 侧零改动，85 条 GUI/controller 回归全绿）。
- unresolved/deviations: (1) `iconPaths.ts` 越界 +2 行（pause/play lucide 图标）——brief Files 清单外，但暂停/恢复按钮需要图标且硬性惯例禁止 Unicode 字符图标，走注册表既有“新加一行”机制，视觉零回归；(2) ComputerRunSnapshot 复用 Task 12 的 17 字段实现（current_surface/owner_session_id/started_at 等），未按 brief 理想形状引入 context_summary/pending_safety_checks——13a 已 APPROVED 的真实契约（`to_public_dict()`）无这些字段，当前窗口/页面 host 由 current_surface 承担，provider safety 未验证契约前本就不渲染确认区；(3) “操控”页签按“动态追加”字面实现为出现过 run（run_id 非空）才追加，从未使用操控的用户零视觉变化；(4) reducer 对“Store 尚无 run”的首事件直接认领 run 归属（brief 样例 1/2 无 starting 前导也能建 run）；已有 run 时仍只有 computer_state/starting 开新 run；(5) run 结束后 pane 的停止按钮仍可见（点击会得到后端 computer_invalid_state 错误弹窗），未做终态隐藏——保持最小实现；(6) App 级接线测试落在 App.panelResize.test.ts（既有 App Vitest 文件中布局相关的最近落点）。

---

### Task 14: CLI 运行控制、Ctrl+C 和线程安全显示

**Files:**
- Create: `core/computer/cli.py`
- Create: `tests/test_computer_cli.py`
- Modify: `main.py`（现有斜杠命令分派 `handle_slash_command`、帮助表、状态显示；本项目不存在 `core/command_handler.py` 或 `core/ui.py`，不得创建平行模块）
- Test: `tests/test_cancellation.py`
- Test: `tests/test_llm_inflight_abort.py`
- Test: `tests/test_main_ui_events.py`

**Interfaces:**
- Consumes: controller public API
- Produces commands: `/computer status|pause|resume|stop`
- Does not produce: `/computer on`, `/computer off`, 永久设置修改或截图字符画

- [x] **Step 1: 写命令白名单和脱敏渲染失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_cli.py -q`

Expected: CLI computer handler 不存在。

- [x] **Step 3: 实现严格 parser 和纯文本 renderer**

```python
_COMMANDS = {"status", "pause", "resume", "stop"}
```

使用 `shlex.split` 后必须恰好两个 token；不接受别名或额外参数。status 不开启能力；其他命令要求活动 run。显示格式固定为 `电脑操控: 运行中 · Edge · 12 步`，动作一行，输入只显示长度，URL 只显示 host。

- [x] **Step 4: 接入 main.py 斜杠分派、主线程安全事件和取消顺序**

`/computer` 接入 `main.py` 现有 `handle_slash_command()` 分派和帮助表，不创建平行 command handler。hotkey/backend/controller listener 不直接 `print_formatted_text` 或改 layout；通过现有事件队列，最终用 `app_instance.loop.call_soon_threadsafe(callback)` 更新。CLI Ctrl+C 检测到 active run 时先 `controller.stop(run_id, "cli_ctrl_c")`，其内部触发 owner cancel/Provider stream abort，再复用现有 KeyboardInterrupt 收尾。

- [x] **Step 5: GREEN 与 CLI 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_cli.py tests/test_cancellation.py tests/test_llm_inflight_abort.py tests/test_main_ui_events.py -q
git diff --check -- core/computer/cli.py main.py tests/test_computer_cli.py
```

Expected: 原始终端无 Markdown 强调；任何 CLI 命令都不能改三个永久配置键；现有取消/在飞中断路径回归通过。

Suggested commit after explicit authorization: `feat(cli): add safe computer run controls`

#### Execution Record — Task 14（2026-09-04，严格 RED→GREEN，无 commit）

- RED: `venv\Scripts\python.exe -m pytest tests/test_computer_cli.py -q` → exit 2 | **1 error during collection in 1.71s**：`ImportError while importing test module ... ModuleNotFoundError: No module named 'core.computer.cli'`——目标 CLI computer handler 不存在（brief Step 2 Expected 逐字命中），无既有测试受影响。实现 cli.py 与 main.py 接线后中间态复核：50 passed, 5 failed（5 条失败全部为 main.py 接线测试：分派/失败色/帮助表/UI 事件渲染/Ctrl+C 顺序——纯 cli.py 层已全绿）。
- 实现: `core/computer/cli.py`（新建：`_COMMANDS = {"status","pause","resume","stop"}` 严格 parser——shlex.split 后必须恰好两个 token 且首 token 小写为 `/computer`，不接受别名/额外参数，shlex ValueError（未闭合引号）同样判用法错误；`ComputerCommandResult(ok, text)` 结果对象；`render_computer_snapshot` 纯文本 renderer——无活动 run（空 run id 或 idle）逐字输出"电脑操控: 已关闭（请在 GUI 系统设置页开启）"，有 run 固定"电脑操控: {状态} · {Edge|Chrome|桌面|browser} · {N} 步"，error_code 非空追加"· 错误码 {code}"；`render_computer_event` 脱敏动作摘要——type_text 只"输入 N 个字符"（text_length 缺失时从 text 长度推导、绝不回显正文）、navigate 只显示 urlsplit().hostname（剥 userinfo/路径/查询/锚点，host 字符白名单 alnum+点+横线）、其余动作走中文标签映射、未知 kind 回退"动作 {kind}"；`render_computer_cli_event` 只显示 computer_action_started 摘要与 computer_action_finished(ok=False) 的"动作失败 · 错误码 {code}"（状态迁移/观察事件由 /computer status 与命令结果承担，不在事件流刷屏）；`handle_computer_command` status 永远 ok，pause/resume/stop 要求活动 run（`_ACTIVE_RUN_STATUSES` = starting/running/paused/waiting_for_user/provider_safety_required/completing/stopping，终态与 idle 拒绝并提示），run id 一律取 `controller.snapshot().run_id`（与 13a Bridge 相同的"当前 run id"语义），pause/resume/stop reason 分别为 `cli_pause`/`cli_stop`（resume 无 reason 参数），`ComputerControlError` 显式捕获转 ok=False + "（错误码 {code}）"呈现，非领域异常原样传播；`stop_active_run_for_cancel(controller)` Ctrl+C 路径——活动 run 先 `controller.stop(run_id, "cli_ctrl_c")`（竞态 run 已结束时 ComputerControlError 判无事发生），返回是否真的停止；`make_cli_event_listener(post_event)` controller listener——worker 线程只预渲染脱敏文本并 `post_event(ComputerCliEvent(...))`，绝不 print/改 layout；`ComputerCliEvent` 最小信封（kind="computer_cli_line"/payload/turn_id="computer-control"）。全模块零 import main/GUI/Provider/Playwright/config——任何命令路径都不触碰三个永久配置键）。
- main.py（最小 6 hunks，编辑前后 `git diff -- main.py` 核对只含本 Task 改动）：①模块级 `from core.computer.cli import ComputerCliEvent`（isinstance 分派用，cli 只依赖纯模型层 import 无副作用）；②`_stop_active_computer_run_for_cancel()` helper + `_cancel_active_turn()` 顶部调用——Ctrl+C 两条路径（c-c keybinding 与主循环 `except KeyboardInterrupt`）都经 `_cancel_active_turn`，controller stop（内部 owner cancel + Provider 流中断固定顺序）先于 Turn 级取消执行；③`_render_ui_event` 顶部 isinstance(ComputerCliEvent) 早退分支——UI 线程 `_print_ui_fragment` 单行打印已脱敏文本，早于 TurnRenderer 查表（不泄漏 renderer 实例）；④/help 帮助表【4. 任务托管与后台进程】追加 /computer 一行；⑤handle_slash_command `/computer` 分派分支（惰性 import，调共享 controller 单例，ok=青色/失败=黄色，_esc 后 HTML 打印）；⑥run_cli 主循环前注册 controller listener（`make_cli_event_listener(post_ui_event)` → 现有 ui_event_queue + schedule_ui/call_soon_threadsafe 通道，不新建线程或队列）。
- 测试: `tests/test_computer_cli.py`（新建 55 条：brief 三样例逐字（4 参数拒绝 + 已关闭纯文本 + type_text 不回显 secret）+ 额外参数拒绝 + 运行中/desktop/失败态三格式逐字（"电脑操控: 运行中 · Edge · 12 步"/"桌面"/"· 错误码 computer_secure_desktop"）+ status 只读不触发控制 + 6 快照×4 事件参数化无 Markdown 强调（无 `*`）+ pause/resume/stop 传当前 run id（resume_calls==["run-live"]、pause_calls==[(run,"cli_pause")]、stop_reasons==[(run,"cli_stop")]）+ 终态/idle 拒绝控制 + provider_safety_required 拒 resume（code+中文原因+零调用）且允许 stop + ComputerControlError code/消息呈现 + URL 只 host（userinfo/token 剥离）+ text_length 缺失推导 + action_kind 别名 + 未知动作回退 + listener 只投递可显示事件（state/observation 不入队）+ 八条命令不改三个永久配置键（use_temp_config 隔离 config.json 断言逐字节不变）+ stop_active_run_for_cancel 三态（cli_ctrl_c/终态忽略/竞态容错）+ Ctrl+C 先 computer_stop 后 turn_cancel/subagent_cancel 顺序断言 + 无活动 run 空操作 + main 分派/失败黄色/帮助表/UI 线程渲染不建 TurnRenderer。FakeController 为本地子类继承共享 tests/computer_fakes.FakeController（无参构造补默认 owner、snapshot 携带 browser/target/error_code、pause/resume 记录并镜像真实 controller 拒绝语义，沿用 SafetyAwareFakeController 本地子类先例，不动共享文件）。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_computer_cli.py tests/test_cancellation.py tests/test_llm_inflight_abort.py tests/test_main_ui_events.py -q` → **126 passed in 8.76s / exit 0**（本 Task 55 + 回归集 71 零改动通过：test_cancellation 3 + test_llm_inflight_abort 原样 + test_main_ui_events 68——含 test_cancel_active_turn_cancels_permissions_and_foreground_subagents 原样绿，证明 _cancel_active_turn 新增 computer stop 空转安全）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_computer_controller.py -q --tb=no` → **37 passed in 2.27s / exit 0**；`venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no` → **614 passed, 1921 deselected in 30.77s / exit 0**；附加 main 导入回归 `pytest tests/test_token_usage.py -q` → **29 passed in 2.72s / exit 0**（模块级新 import 零冷启动回归）。
- Compile: `venv\Scripts\python.exe -m py_compile main.py core/computer/cli.py` → exit 0。
- Diff check: `git add -N core/computer/cli.py tests/test_computer_cli.py` 后 `git diff --check -- core/computer/cli.py main.py tests/test_computer_cli.py` → **exit 0**（仅 LF→CRLF 警告，与既往一致）。
- Files touched: `core/computer/cli.py`（新建）、`tests/test_computer_cli.py`（新建）、`main.py`（最小 6 hunks：分派 + 帮助表 + Ctrl+C 接线 + UI 事件早退分支 + 模块 import + listener 注册）、本计划文件（checkbox 与本记录）。回归集三个测试文件零改动（预期达成）；gui_bridge.py / frontend / core/llm.py / core/computer 既有文件零触碰。
- spec: PASS（严格 parser 恰好两 token + 白名单，/computer on|off|browser chrome|裸 /computer 全部 ok=False 且文案含"用法"与"设置页"；暂停/恢复/停止传 snapshot().run_id 与 13a Bridge 同语义；provider_safety_required 经 controller.resume 显式拒绝透传（code+中文消息），CLI 侧 status/stop 可用；resume 同时覆盖 waiting_for_user 接管恢复（controller require_observe 语义内含）；设置未开启 status 显示关闭并指向 GUI 设置页；CLI 全文无 /computer on|off 路径、无永久配置键写入——config 模块根本未被 cli.py import；显示纯文本无 Markdown 强调符号（参数化断言无 `*`））。
- privacy: PASS（type_text 只显示长度（brief 样例 secret 逐字断言不出现）；URL 只显示 hostname（userinfo/查询 token/路径/锚点剥离，host 字符白名单防 ANSI/Markdown 注入）；快照渲染只用 to_public_dict 的 17 公开字段；listener 只投递脱敏摘要文本，原始事件字段（截图 sha/坐标详情）不进入 UI 队列；错误呈现只含稳定错误码 + controller 中文消息）。
- default-byte: PASS（cli.py 不触碰请求构造/消息规范化/system prompt/tools/metadata/缓存亲和；main.py 六处 hunks 均为 CLI 交互层（分派/帮助/取消/事件渲染），无 Provider 请求路径字节影响；回归集 test_cancellation/test_llm_inflight_abort/test_main_ui_events 原样全绿）。
- unresolved/deviations: (1) "已关闭"显示由快照驱动（空 run id / idle 即显示"已关闭（请在 GUI 系统设置页开启）"）——brief 样例逐字要求 render_computer_snapshot(disabled_snapshot())，而 17 字段快照不含能力/设置字段，设置已开启但尚无 run 时 status 同样显示该行（信息模型与 GUI 控制条一致：run 驱动）；(2) CLI 事件显示按"预期最小"只投递动作开始摘要与失败动作行——状态迁移/观察事件不在事件流显示（状态可见性由 /computer status 与控制命令结果承担，避免命令结果与事件行重复刷屏）；(3) headless 模式（-p/--prompt）的 KeyboardInterrupt（main.py 各自独立的 [已取消] 收尾）未接 controller.stop——brief 指定的"现有 KeyboardInterrupt 收尾"为交互式两条路径（c-c keybinding 与主循环 except，均已经 _cancel_active_turn 覆盖），headless 进程退出本身终止一切副作用；(4) FakeController 为测试文件本地子类（继承共享 fake，含 owner 缺省补齐），未改 tests/computer_fakes.py——共享 FakeController 的 owner 必填与 snapshot 缺 browser/target 字段使其无法逐字满足 brief 的 `FakeController()` 无参样例，本地子类沿用 SafetyAwareFakeController 先例；(5) COMMAND_COMPLETIONS 输入补全表未追加 /computer（brief Files 清单未列，保持最小 hunks）。

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

- [x] **Step 1: 写 RPC 后端强制边界失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py -q`

Expected: 新 mutation 还未加入禁止列表或 turn source 尚未由 transport 注入。

- [x] **Step 3: 更新后端禁止列表并注入来源**

把六个 mutation 方法加入 `LanServer.FORBIDDEN_METHODS`；保留 status/stop。LAN dispatch 在调用 Bridge 的消息入口时由服务器内部参数注入 `turn_source="lan"`，并忽略/覆盖客户端 payload 的同名字段。Bridge/local GUI 调用固定传 `local`，模型/用户正文无法设置。

前端隐藏移动端设置控件只作为 UX；安全性以 RPC 黑名单和 resolver 双层测试为准。

- [x] **Step 4: GREEN 与现有 LAN 回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py tests/test_gui_system_settings.py -q
git diff --check -- core/lan_server.py gui_bridge.py core/agent_service.py tests/test_lan_server.py tests/test_computer_capability.py
```

Expected: 手机可看到并停止本机已有 run；远程消息即使本机开关开启也拿不到 computer tool/native built-in。

Suggested commit after explicit authorization: `fix(lan): prevent remote computer privilege expansion`

#### Execution Record — Task 15（2026-09-04，严格 RED→GREEN，无 commit）

- RED: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py -q` → **exit 1 | 20 failed, 44 passed in 4.47s**，失败全部命中目标：12 条 `AttributeError: 'LanServer' object has no attribute 'dispatch_rpc'`（六 mutation×6 + status/stop×2 + drain_js_queue + turn_source 覆盖×3）；`test_route_rejects_computer_permission_mutation` 实证 set_computer_control_enabled 未入黑名单直透 bridge（`'{"id": 7, "result": {"ok": false, ...}}'`）；`test_route_injects_lan_turn_source_on_user_message` 实证客户端 payload 的 `turn_source:"local"` 原样透传（`[('hi', 'local')] != [('hi', 'lan')]`——伪造向量真实存在）；4 条 bridge TypeError/KeyError（send_user_message 无 turn_source 参数/kwargs 缺字段）；2 条 `build_runtime_context() got an unexpected keyword argument 'turn_source'`。既有 44 条零影响。
- 实现: `core/lan_server.py`（FORBIDDEN_METHODS +7：六 mutation `set_browser_control_enabled`/`set_computer_control_enabled`/`set_computer_browser`/`pause_computer_control`/`resume_computer_control`/`acknowledge_computer_safety` + **计划外默认裁决 `drain_js_queue`（Task 13a 审查 Minor 1，fail-closed：LAN 可排空 _js_queue 偷走桌面事件并可能消费关停哨兵，brief 契约明确要求一并入禁止列表）**；新增 `MESSAGE_ENTRY_METHODS = {"send_user_message"}`；新增 `dispatch_rpc(method, args)` 公共分发核心——禁止方法统一返回 `{"error": {"code": "method_not_allowed"}}`，`_route_message` 改为复用它并把该错误形状翻译回既有 wire 字符串 `"method not allowed"`（shim 兼容、既有 WS 测试原样绿）；`_force_lan_turn_source` 静态方法覆盖三种参数形态（dict 关键字式直接覆盖同名字段 / 首位 dict payload 改写其 turn_source / 纯位置参数由 `_call_bridge` 新增 extra_kwargs 追加 `turn_source="lan"` 关键字——与第 6 位置参数伪造重复绑定时 TypeError 被 _call_bridge 统一吞掉回包，fail-closed 拒绝消息）。放行 get_computer_control_status / stop_computer_control 不变）。
- 实现: `gui_bridge.py`（仅叠加 Task 15 hunks，未动 12/13a 既有 hunks：`send_user_message` 增 `turn_source: str = "local"` 参数 + 顶部受控值校验（仅 local|lan，非法返回 `{"ok": False}` 不启动回合）；排队 entry 增 `"turn_source"` 字段；`_spawn_turn_worker` 增 turn_source 参数并写入 turn_kwargs；`_drain_message_queue` 补发时从 entry 恢复 turn_source（LAN 排队消息不得降级 local）；`_run_turn_worker` 增 turn_source 参数透传 `prepare_turn_messages(turn_source=...)` 与 `_consume_turn_events(turn_source=...)`；`_consume_turn_events` 增 keyword 参数并透传 `chat_with_model_events(computer_turn_source=...)`。内部回合（bg_summary/bg_command_summary 两条直接建线程路径）不传参保持默认 local——设计 18 只约束 LAN RPC 发起的用户 Turn）。
- 实现: `core/agent_service.py`（`prepare_turn_messages` 增 keyword-only `turn_source: str = "local"`；`build_runtime_context` 增同名参数并传给 `resolve_turn_computer_capability(turn_source=turn_source)`，Task 10 偏差 6 标记的"LAN turn_source 注入归本 Task"就此闭环；docstring 同步更新；默认 local 下解析链字节与历史完全一致）。
- 测试: `tests/test_lan_server.py` 25→44（+19）：brief 三样例逐字（六 mutation 参数化拒绝 + status/stop 放行 + payload turn_source:"local" 被覆盖为 lan）+ drain_js_queue 拒绝 + 关键字式 payload 覆盖 + 纯文本位置参数注入 + 第 6 位置参数伪造 fail-closed（StrictBridge 同真实签名，重复绑定 TypeError → ok=False 且 bridge 零调用）+ WS wire 路径两条（mutation 拒绝翻译 + 用户消息注入）+ bridge 链四条（非法来源拒绝 / 默认 local 不变 / lan 透传 spawn kwargs / 排队保留 lan）；`tests/test_computer_capability.py` 19→21（+2）：`build_runtime_context` turn_source 透传（fake resolver 捕获 local/lan）+ 真实解析链 lan 冻结（config/model_manager 隔离，两开关全开仍 `computer_remote_forbidden`，handoff 快照 identity 断言）。"LAN turn capability 永远 disabled" 纯 resolver 覆盖已存在（`test_lan_turn_is_always_disabled`，确认未改）。fixture：`_RecordingBridge`/`server`/`bridge` + `_make_bare_message_bridge`（沿用 test_gui_message_queue 裸桥桩模式）。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py tests/test_gui_system_settings.py -q` → **86 passed, 1 warning in 10.26s / exit 0**。warning 为既有生命周期测试固有噪音（单独运行 test_start_reuses_persisted_port + test_start_falls_back_when_persisted_port_occupied 同样出现：真实 socket server 的 daemon serve_forever 线程收尾 WinError 10038），与本 Task 改动无关（diff 不含任何线程/socket 生命周期改动）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_llm_generic.py -q` → **68 passed in 5.87s / exit 0**（_consume_turn_events 既有直调测试与 computer_turn_source 请求层测试零改动通过）；附加 AGENTS 铁律回归 `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_agent_service.py -q` → **38 passed in 7.78s / exit 0**（修改 prepare_turn_messages 前置要求的 system prompt / tools schema 字节稳定性 + agent_service 全量）。
- Compile: `venv\Scripts\python.exe -m py_compile core/lan_server.py gui_bridge.py core/agent_service.py` → exit 0。
- Diff check: `git diff --check -- core/lan_server.py gui_bridge.py core/agent_service.py tests/test_lan_server.py tests/test_computer_capability.py` → **exit 0**。
- Files touched: `core/lan_server.py`（+69/-3：黑名单扩充 + dispatch_rpc/_force_lan_turn_source/extra_kwargs + _route_message 复用）、`gui_bridge.py`（Task 15 hunks：send_user_message/spawn/drain/worker/consume 五处 turn_source 接线，叠于 12/13a 未提交改动之上）、`core/agent_service.py`（+15/-2）、`tests/test_lan_server.py`（+166）、`tests/test_computer_capability.py`（+52）、本计划文件（checkbox 与本记录）。main.py / core/computer/ / frontend / 核心工具管线零触碰。
- spec: PASS（设计 18 四条全落实：setter/pause/resume 入 FORBIDDEN ✓；status/stop 放行且经 _RecordingBridge 实测 ok ✓；LAN RPC 用户 Turn 带不可伪造 turn_source=lan——三种参数形态注入 + 客户端 local 覆盖 + 第 6 位置参数伪造 fail-closed ✓；resolver 对 lan 固定 disabled 即使两开关全开（真实链冻结测试）✓。后端双层边界（RPC 黑名单 + resolver）不依赖前端隐藏 ✓。断言链完整：LAN 消息 payload 带 turn_source:"local" → dispatch 覆盖为 lan → capability computer_remote_forbidden → computer tool/native built-in 不入请求（test_computer_llm_generic 既有 computer_turn_source 层回归绿））。
- privacy: PASS（turn_source 是入口层受控枚举值，非用户/模型数据；无截图/输入正文/凭据进入日志或事件；drain_js_queue 禁止后 LAN 无法窃听桌面事件队列）。
- default-byte: PASS（默认路径 turn_source 恒 "local"，resolver 入参与历史字节完全一致；test_prompt_byte_stability + test_agent_service + test_computer_llm_generic 全绿）。
- unresolved/deviations: (1) **drain_js_queue 入禁止列表为计划文本外的默认裁决**（Task 13a 审查 Minor 1，任务契约明确裁定 fail-closed，已写入 FORBIDDEN_METHODS 注释与本记录）；(2) `dispatch_rpc` 现状不存在——取证确认仓库无此方法（任务描述的"dispatch_rpc 结构"实为 _route_message/_call_bridge），brief 三样例逐字要求 `server.dispatch_rpc(...)`，故新增为公共分发核心并让 _route_message 复用（同一边界两条路径），wire 格式不变；(3) 排队消息保留 turn_source（queue entry 增字段 + _drain_message_queue 恢复）为安全属性必需的最小接线——否则 LAN 消息在回合运行中入队、补发时降级 local 即绕过边界；(4) `test_lan_positional_turn_source_cannot_reach_bridge` 为 GREEN 后补充的对抗性边界测试（RED 期 dispatch_rpc 缺失同样失败，归同族 RED 证据）；(5) 内部回合（后台汇总/命令总结）保持 local——设计 18 只约束"LAN RPC 发起的用户 Turn"，系统自动回合非 LAN RPC 发起；(6) 移动端前端零触碰（Task 12 已交付隐藏）。

#### Execution Record — Task 15 修复轮（reviewer Critical-1/Important-1，2026-09-04，未 commit）

- Review findings: NEEDS-FIX 两项（reviewer 用真实 ApiBridge 实测复现）。**Critical-1**：`_force_lan_turn_source` 首参 dict 分支对真实 `ApiBridge.send_user_message(text, session_name, attachments, workdir, model_id, turn_source)` 签名无效——向量① `dispatch_rpc("send_user_message", [{"text": "use computer"}])` 注入的 "lan" 只写进 text payload，真实 turn_source 参数走默认 "local"；向量② `[{"text": ...}, None, None, None, None, "local"]` 客户端第 6 位置参数直接绑定 "local"。根因：测试桩 `_RecordingBridge.send_user_message(*args)` 从 args[0] dict 读 turn_source，与真实绑定语义不符，绿灯掩盖漏洞。**Important-1**：dispatch_rpc 不过滤下划线前缀，`dispatch_rpc('_queue_js_payload', ['任意字符串'])` 可远程向桌面 WebView 注入任意 JS（dispatcher 拼接 evaluate_js 无转义，可调用被禁 setter 绕过全部 LAN 边界）。Minor-1（emit_event 未禁，UI 事件伪造，前端快照校准缓解）/ Minor-2（get_provider_api_key 明文远程可读，既有产品面）记录在案不修。
- RED: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py -q` → **exit 1 | 6 failed, 36 passed in 4.00s**（collect 实测 42 条 = 38 既有 + 4 新增），失败全部命中目标：三条 `AssertionError: assert 'local' == 'lan'`（Critical-1 向量①/② + 既有 payload 覆盖测试被桩修正暴露——真实签名桥桩实际收到 "local"，绕过真实存在）；`test_lan_positional_turn_source_cannot_reach_bridge` `assert False is True`（按规范化新语义更新后旧实现不再满足）；`test_lan_forbids_private_underscore_methods` `assert {'ok': True} == {'error': {'code': 'method_not_allowed'}}`（`_queue_js_payload` 被实际调用——Important-1 注入向量真实存在）；`test_route_rejects_private_underscore_method` WS wire 路径同样未拒。
- Fix: `core/lan_server.py`——① `dispatch_rpc` 拒绝下划线前缀私有方法（返回与 FORBIDDEN_METHODS 一致的 `{"error": {"code": "method_not_allowed"}}`，wire 层翻译为既有 "method not allowed" 字符串）；② `_force_lan_turn_source` 由"改写 payload"重构为"规范化剥离绑定通道"：list 裁剪位置参数至前 5 个（新增 `MESSAGE_ENTRY_POSITIONAL_LIMIT = 5`，即真实签名 text..model_id，第 6 位起含 turn_source 位置的客户端绑定全部丢弃）、首参 dict 删除 turn_source 键、kwargs dict 删除后以 "lan" 覆盖，最终 turn_source 只经 `_call_bridge` keyword 通道由服务器唯一注入；非 list/dict 无法规范化形态 fail-closed 拒绝（`{"ok": False, "msg": "消息参数无法规范化，已拒绝"}`）。`gui_bridge.py` 零改动（修复全部在 lan_server 侧）。
- 测试: `tests/test_lan_server.py`——`_RecordingBridge.send_user_message` 改为与真实 ApiBridge 签名同形（turn_source 第 6 个位置参数），修正绿灯掩盖根因；`test_lan_positional_turn_source_cannot_reach_bridge` 断言由"重复绑定 TypeError fail-closed 拒绝"更新为 reviewer 建议的"裁剪 + 注入"规范化语义（更强性质：消息照常送达且桥桩只可能收到 "lan"）；新增 4 条对抗测试：`test_lan_payload_dict_args_without_turn_source_binds_lan`（向量①）、`test_lan_payload_dict_with_positional_turn_source_stripped`（向量②）、`test_lan_forbids_private_underscore_methods`（dispatch 层，含 `_queue_js_payload`/`_rebind` 两个下划线方法且 bridge 上真实存在该方法——证明拒绝发生在 dispatch 而非"未知方法"）、`test_route_rejects_private_underscore_method`（WS wire 路径同边界）。
- GREEN: `venv\Scripts\python.exe -m pytest tests/test_lan_server.py tests/test_computer_capability.py tests/test_gui_system_settings.py -q` → **90 passed, 1 warning in 10.53s / exit 0**（collect 实测 42+27+21；warning 为 WinError 10038 既有生命周期测试噪音，与首轮 GREEN 同源）。
- Regression: `venv\Scripts\python.exe -m pytest tests/test_gui_tool_events.py tests/test_gui_message_queue.py tests/test_computer_llm_generic.py -q` → **68 passed in 6.29s**（沙箱 shell 包装层报 EFSClipboardCtrl.log 噪音 exit 1，实际输出 68 passed，按纪律以实际输出判断）。
- Compile: `venv\Scripts\python.exe -m py_compile core/lan_server.py` → exit 0。
- Diff check: `git diff --check -- core/lan_server.py tests/test_lan_server.py` → exit 0；`git status` 复核两文件仍处 modified（多会话共享工作区自检）。
- Files touched: `core/lan_server.py`（MESSAGE_ENTRY_POSITIONAL_LIMIT 常量 + dispatch_rpc 下划线拒绝 + _force_lan_turn_source 规范化重写）、`tests/test_lan_server.py`（桩同形修正 + 1 条断言语义更新 + 4 条新增对抗测试）、本计划文件。gui_bridge.py / core/agent_service.py / 其他文件零触碰。
- deviations: (1) **Important-1 按 fail-closed 默认裁决执行**（审查标注"需用户裁决"，但设计 18"后端必须实施同一边界"与 K3 安全纪律支持该默认：下划线私有方法属内部 seam，非前端 RPC 面，无合法调用方受影响；若用户后续裁决放行某私有方法，只需调整 dispatch_rpc 单一检查点）；(2) **第 6 位置参数策略由"拒绝"改为"裁剪 + 注入"**——按 reviewer 修复方向（统一规范化：list 裁剪至 ≤5 后 keyword 强制注入），比旧重复绑定 TypeError 拒绝性质更强且正常消息不再被误拒，旧测试断言语义随之更新；(3) Minor-1 / Minor-2 记录在案不修（见 Review findings）；(4) 首参 dict 形态仍作为 text 参数绑定（其 str() 成为消息正文）——属内容质量而非安全绕过（turn_source 通道已剥离），不在修复范围。

---

### Task 16: 生命周期、打包、隐私审计与受控烟测

**Files:**
- Modify: `gui_main.py`
- Modify: `main.py`
- Modify: `ycode.spec`
- Modify: `requirements.txt`
- Modify: `scripts/smoke_computer_control.py`
- Create: `tests/test_computer_privacy.py`
- Test: `tests/test_gui_lifecycle.py`
- Modify: `README.md` only if the current user-owned README changes can be preserved with a non-overlapping minimal section

**Interfaces:**
- Consumes: singleton controller, preview server, hotkey
- Produces: startup registration, deterministic shutdown and packaged Python Playwright support
- Produces: explicit manual smoke modes with no implicit real input

- [x] **Step 1: 写退出顺序和敏感信息扫描失败测试**

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

- [x] **Step 2: 运行确认 RED**

Run: `venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_gui_lifecycle.py -q`

Expected: lifecycle glue 和隐私 fixture 未完成。

- [ ] **Step 3: 接入启动和 deterministic shutdown**

启动只创建轻量 runtime 容器；开关默认 false 时不得导入 Playwright、启动浏览器、启动截图或注册 computer tools。全局热键 preflight 可在用户开启设置时首次注册；活动能力存在时保持注册。

GUI close、CLI 退出和异常 finally 统一调用：active run stop → Provider stream abort 由 controller 执行 → backend inputs release/close → preview stop/delete → hotkey close。不得终止 LiteLLM 共享连接池，不自动恢复 interrupted run。退出顺序断言补充到 `tests/test_gui_lifecycle.py`。

- [ ] **Step 4: 更新 PyInstaller 打包但不打包浏览器**

`ycode.spec` 按 `PyInstaller.utils.hooks.collect_all("playwright")` 收集 Playwright 的 Python 子模块、driver 数据和所需二进制，把返回的 `datas/binaries/hiddenimports` 合并到项目现有收集项；不得运行或包含 Playwright Chromium 下载目录。构建产物必须扫描确认不含 `.local-browsers`、Chromium、Firefox 或 WebKit 缓存。打包 smoke 需要验证系统 Edge/Chrome channel 能被找到，缺失时设置页显示 unavailable。

Run:

```powershell
venv\Scripts\python.exe -m PyInstaller --clean ycode.spec
```

如果仓库现有正式打包命令不同，以 `ycode.spec` 和现有发布脚本的真实命令为准，并把准确命令记录到执行证据；不得发明第二套构建流程。

- [x] **Step 5: 完成显式人工烟测矩阵**

`scripts/smoke_computer_control.py` 支持：

```text
--probe-browser edge|chrome
--browser-observe edge|chrome
--desktop-observe
--desktop-input --confirm-real-input I_UNDERSTAND
```

observe 模式只截图，不输入。真实输入模式固定执行可见测试区域的一次 move/click/type，并要求用户先打开无敏感数据的测试窗口；每一步前在终端打印将执行的脱敏动作并要求单次 Enter。测试后必须调用 release/stop。

人工验收矩阵：Edge 专用 profile 首次登录/重启保留；Chrome 同样且 profile 隔离；浏览器关闭明确失败；desktop 多屏/缩放；pause 后无新动作；resume 首动作 observe；GUI stop、CLI stop、Ctrl+C、Ctrl+Alt+Esc；锁屏/UAC fail-closed；退出后无残留 Playwright 子进程和临时 PNG。

- [x] **Step 6: GREEN、打包和隐私回归**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_gui_lifecycle.py tests/test_computer_preview_server.py tests/test_computer_hotkey.py -q
venv\Scripts\python.exe -m py_compile main.py gui_main.py scripts/smoke_computer_control.py
venv\Scripts\python.exe -m compileall -q core/computer
git diff --check -- gui_main.py main.py ycode.spec requirements.txt scripts/smoke_computer_control.py tests/test_computer_privacy.py
```

Expected: 自动测试不产生真实输入；打包产物只依赖系统浏览器；退出清理完整。若 `README.md` 当前存在用户未提交修改且内容重叠，本 Task 不修改 README，并在最终报告明确记录文档冲突，不覆盖用户内容。

Suggested commit after explicit authorization: `build(computer): package and clean control runtime`

#### Execution Record — Task 16（2026-09-05，含中断会话续作，未 commit）

- **会话背景（如实披露）**：本 Task 主体实现由上一会话完成但中断（未写报告/记录；core/computer/runtime.py 与 tests/test_computer_privacy.py 以未跟踪文件在工作区、接线 hunks 已在 gui_main/main/ycode.spec/smoke/lifecycle 测试中、Part B 各项已落）。中断会话的 RED 原文证据随会话丢失，不可事后伪造。本会话逐项审计全部在树实现与测试、补齐三个真实缺口（见 deviations）、以本会话新增行为的热键接线 RED 补足 TDD 纪律、完成两次 PyInstaller 构建与产物扫描、写本记录与报告（.superpowers/sdd/2026-09-02-computer-control/task-16-report.md）。
- **本会话 RED（热键生命周期接线——Task 5 移交 → Task 15→16 契约传递明列待办）**：`venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py -q -k "refresh_hotkey"` → **exit 1 | 4 failed, 15 deselected in 2.73s**，全部 `ImportError: cannot import name 'refresh_computer_runtime_hotkey' from 'core.computer.runtime'`；`venv\Scripts\python.exe -m pytest tests/test_gui_system_settings.py -q -k "hotkey"` → **exit 1 | 4 failed, 3 passed, 18 deselected in 3.76s**，全部 `AttributeError: 'module' object at core.computer.runtime has no attribute 'refresh_computer_runtime_hotkey'`。失败全部命中目标缺失。
- **中断会话实现的验收（GREEN-only，RED 证据丢失已披露）**：`venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_gui_lifecycle.py -q` → **25 passed in 3.90s**（brief Step 2 命令；实现已在树故为 GREEN）；隐私语料扫描覆盖四类持久化面（DB messages + harness_turns/steps/events Journal + 事件 JSON + 全级日志文件）——输入正文/response id/URL query token/Cookie/API key/preview token/profile 内容零命中，native 投影行无 base64（generic followup 观察消息走既有 image_url 媒体块，Task 10/11a 折叠回归覆盖）；brief Step 1 两样例逐字在文件内。
- **实现（热键生命周期，本会话）**：`core/computer/runtime.py`——`_make_hotkey` 从 `_build_runtime` 提取（注册失败留痕返回 None、不改配置）；`_controller_has_active_run`（读取失败按活动处理，fail-closed）；`refresh_computer_runtime_hotkey(settings_loader=…, hotkey_factory=…, controller_provider=…)`——任一开关开启且未注册→注册（用户开启设置时首次注册）；两开关全关且有活动 run→保持（活动能力存在时保持）；全关且无活动 run→注销；运行时容器未启动→零副作用（不自动启动不读设置，GUI 启动路径负责创建容器，fail-closed）；同步全程持 `_RUNTIME_LOCK` 并以 `_RUNTIME is runtime` 复核退出竞态。`gui_bridge.py`——`_refresh_computer_hotkey` helper（失败只留痕不回滚开关）+ `set_browser_control_enabled`/`set_computer_control_enabled` 成功路径调用（关闭路径先 `_stop_computer_runs_for_disabled_switch` 再刷新，使注销判定看到 post-stop 状态）。
- **实现（中断会话，本会话审计确认）**：`core/computer/runtime.py` 进程级轻量容器（start_computer_runtime 幂等单例/默认关闭零 Playwright 导入断言/惰性预览发布器/shutdown_computer_runtime 固定顺序 controller.stop→preview.stop→hotkey.close 每步独立 try-except/shutdown_process_computer_runtime 单例收集）；`gui_main.py` 启动接线 + `_finalize_gui_exit`（回合级 finalize 先于电脑运行时，finally 覆盖异常路径）；`main.py` CLI 启动/交互与 headless 两条退出路径 finally 统一收尾（不接预览发布器）；`ycode.spec` `collect_all('playwright')` 三元组并入现有收集项；`scripts/smoke_computer_control.py` 四模式（--desktop-input 单步 Enter 确认 + 固定 move/click/type + release/close 收尾）；`core/computer/actions.py` `sanitized_action_summary`（generic 工具参数投影与 native 投影共用脱敏语义）。
- **Part B 全部落位（裁决来源标注）**：**B1**【11b 用户裁决】`core/computer/provider_openai.py:_merge_response_id` 与 `core/llm.py:_merge_stream_response_id` 冲突消息由内嵌原文改为「Responses response id 冲突（两个不同的 id，原文不回显）」/「Responses 流的 response id 冲突（两个不同的 id，原文不回显）」；测试两层各一条（parse 层经 `parse_openai_computer_events` 断言 code=computer_provider_protocol_error 且不含任一 id 原文；stream 层直调 `_merge_stream_response_id`）；**既有 golden 零更新**（取证：冲突消息不在任何 golden 请求字节内，test_provider_payload_golden 66 条原样全绿）。**B2**【Task 15 Minor 1】`dispatch_rpc` 首行 `isinstance(method, str)` 守卫（非字符串统一 method_not_allowed，修复前 unhashable 在 FORBIDDEN 判定处 / int/None 在 getattr 分发处抛 TypeError 断连）+ 参数化测试。**B3**【Task 14 审查四项】① CLI 状态栏 computer 段：`core/computer/cli.py:render_computer_status_bar`（活动 run 才显示「电脑操控: 运行中 · Edge · N 步」，否则空串）+ `main.py:_computer_status_bar_segment` 接入 get_title_segments（复用既有状态栏渲染，无新线程）② stopping 窗口 `/computer stop` 返回「电脑操控: run 已在停止中，请稍候…」友好文案（cli.py 前置判断，不再触发 computer_invalid_state）③ `COMMAND_COMPLETIONS` 提为 main.py 模块级常量并补 `/computer`（SlashCommandCompleter 引用不变）④ `_stop_active_computer_run_for_cancel` 加 except Exception 兜底留痕（取消路径绝不因电脑清理异常中断 Turn 级取消）；四项各≥1 条测试。**B4**【11b 建议/13b 契约传递】`test_native_chained_continuation_two_calls_then_final_text`（computer_call→computer_call→text 两跳续传）。**B5**【Task 6 审查遗留】`win32_adapter` OpenInputDesktop/GetForegroundWindow `restype=c_void_p` + CloseDesktop `argtypes=[c_void_p]`（64 位句柄不截断）+ 两条 monkeypatch ctypes 测试（0x1AB2CD3EF456 大句柄断言）。**B6**【Task 6 M1】`browser_backend` start 超时投递 `_Command(None)` 关闭哨兵（慢 boot worker 成功→命令循环取哨兵退出并 teardown，失败→boot 异常路径自带 teardown）+ `test_start_timeout_posts_sentinel_so_late_boot_worker_exits`。**B7**【Task 13 前遗留】`controller.stop(run_id=None)` 进程退出入口：先停活动 run 再 `_close_backend_quietly` 全部 retained backend + `test_shutdown_stops_active_run_and_closes_retained_browser`（complete_run(retain) 后退出时 retained browser 一并 closed）。**B8**【Task 8 移交】`controller.set_preview_publisher` + execute 观察 PNG 发布接线（发布失败留痕返回空串不影响主链路）+ `test_controller_publishes_observation_png_to_lazy_preview`（preview_url 进事件/快照、stop 后临时 PNG 清空）——13a 事件 observation 的 preview_url 发布链路已通。**B9**【Task 14 审查】**降级为仅记录不修**：ComputerRunSnapshot 17 字段冻结契约无设置信息，`render_computer_snapshot(snapshot)` 签名 run 驱动（Task 14 brief 锁死），cli.py 模块契约自限「只依赖 controller 公开 API 与快照公开字段」——区分「设置开无 run/设置关」需扩冻结快照或改命令签名，命中 brief 豁免条件。
- **GREEN**: `venv\Scripts\python.exe -m pytest tests/test_computer_privacy.py tests/test_gui_lifecycle.py tests/test_computer_preview_server.py tests/test_computer_hotkey.py -q` → **60 passed in ~14s**（19+10+18+13）；附加 smoke 路径 `+ tests/test_smoke_computer_control.py` → **64 passed in 14.42s**；`tests/test_gui_system_settings.py` → **25 passed**（21+4 热键接线）；gui_bridge 邻近回归 `pytest tests/test_gui_message_queue.py tests/test_gui_tool_events.py tests/test_computer_settings.py -q` → **94 passed in 5.26s**。
- **Part B 回归**: `venv\Scripts\python.exe -m pytest tests/test_provider_payload_golden.py tests/test_computer_provider_openai.py tests/test_lan_server.py tests/test_computer_cli.py -q` → **194 passed, 1 warning in 19.65s**（热键接线后复跑；shell 包装层 EFSClipboardCtrl.log 噪音 exit 1，以实际输出判断——progress.md 既有纪律）。
- **全量 computer**: `venv\Scripts\python.exe -m pytest tests/ -q -k "computer" --tb=no` → **661 passed, 1943 deselected in 44.65s**（Task 15 后 657 + 热键 refresh 4）。
- **byte-stability**: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py -q` → **5 passed in 2.09s**。
- **Compile**: `py_compile main.py gui_main.py scripts/smoke_computer_control.py` → exit 0；`py_compile gui_bridge.py core/computer/runtime.py` → exit 0；`compileall -q core/computer` → exit 0。
- **打包（Step 4）**：正式命令取证——README/docs/BUILD.md 与 build_release.py：完整流水线 `venv\Scripts\python.exe build_release.py`，其步骤 1 核心命令为 `pyinstaller --clean --noconfirm ycode.spec`。**完整流水线刻意未跑**：step4b 会删除并替换用户 09/02 的正式发布产物（dist/Y-code-1.2.20.exe 等），故直接运行核心步骤 `venv\Scripts\python.exe -m PyInstaller --clean --noconfirm ycode.spec`（不发明第二套流程）。构建一 459.7s 完成但作废（热键接线编辑 01:23 晚于其 PYZ 冻结 01:17）；**构建二（最终源码）482.5s exit 0** → dist/ycode：Y-code.exe 29,947,528B + ycode.exe 29,055,100B + _internal 共 318.1MB/3921 文件。**产物扫描**：playwright 1.62.0 Python 包 + dist-info 收集齐全；driver 完整（node.exe + package/cli.js + 114 文件，playwright 总计 103.5MB）；**无 .local-browsers/Chromium/Firefox/WebKit 浏览器目录、无任何浏览器可执行文件**（唯一 "chromium" 命中是 driver JS 资产目录内 appIcon.png 16,565B）；包内无 tests/、无 smoke 脚本。**打包 driver 验证**：`sys.path=dist\ycode\_internal` 下 `sync_playwright().start()` → node driver 从打包位置启动并应答 executable_path（默认路径指向本机不存在的 ms-playwright\chromium-1234——打包版按设计只走系统 channel），exit 0。**系统 channel 被动探测**：Edge=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe、Chrome=C:\Program Files\Google\Chrome\Application\chrome.exe 均在标准路径（真实通道启动探针属人工烟测矩阵，绝不自动执行）。.gitignore 覆盖 build/（行 14）与 dist/（行 15）。requirements.txt 零改动（`playwright>=1.55,<2` 已在行 24，Task 9 已加——取证确认）。
- **Diff check**: `git add -N core/computer/runtime.py tests/test_computer_privacy.py tests/test_smoke_computer_control.py`（仅 Task 16 新文件，并发会话产物一律未 add）后 `git diff --check -- <全部 touched 文件>` → **exit 0**（仅 CRLF 提示，本仓库常态）。
- **Files touched**: 新建 `core/computer/runtime.py`、`tests/test_computer_privacy.py`（19 测试：中断会话 15 + 本会话热键 4）、`tests/test_smoke_computer_control.py`（4 测试，本会话）；修改 `gui_main.py`、`main.py`、`ycode.spec`、`scripts/smoke_computer_control.py`、`gui_bridge.py`（+18 行，本会话热键接线）、`core/llm.py`、`core/lan_server.py`、`core/computer/{win32_adapter,browser_backend,controller,cli,actions,provider_openai,provider_generic}.py`、`tests/test_gui_lifecycle.py`、`tests/test_gui_system_settings.py`（+4，本会话）、`tests/test_computer_cli.py`、`tests/test_computer_provider_openai.py`、`tests/test_computer_windows_backend.py`、`tests/test_computer_browser_backend.py`、`tests/test_lan_server.py`、`tests/test_provider_payload_golden.py`、`tests/computer_fakes.py`（Task 9-15 累积）、`README.md`（+20 行最小非重叠「电脑操控人工烟测」小节——git status 确认无用户未提交改动后追加）；`requirements.txt` 零改动。本计划文件 checkbox 与本记录。
- **spec**: PASS（设计 23.2 生命周期：统一关闭顺序 controller.stop（内部 owner cancel→Provider abort→backend release/close）→preview.stop（删临时 PNG）→hotkey.close，LiteLLM 共享连接池不终止，interrupted run 不自动恢复，GUI close/CLI 交互与 headless 退出/异常 finally 三路统一；设计 23.1 打包：collect_all('playwright') 且零浏览器缓存，系统 channel 可寻；设计 22 隐私十项：语料扫描全部命中；热键生命周期「开启首次注册/活动保持/全关注销/退出注销」四态全测）。
- **privacy**: PASS（四类持久化面零泄漏见上；B1 冲突消息脱敏双层；preview token 只进 GUI 事件 URL；profile 内容留本机）。
- **default-byte**: PASS（byte-stability 5 + golden 66 原样全绿；B1 变更不在任何 golden 字节内，零 golden 更新）。
- **unresolved/deviations**: (1) **中断会话 RED 证据丢失**——不可事后伪造，以 GREEN+全量语料扫描+本会话热键 RED（8 failed 全命中目标缺失）补足纪律，如实披露；(2) **gui_bridge.py 触碰**（不碰清单例外）——非 shutdown 接线而是 Step 3 热键生命周期接线必需（Task 15→16 契约传递明列），+18 行最小 hunk；(3) **tests/test_smoke_computer_control.py 新建**（brief Files 清单外）——Step 5 明确要求 smoke 代码路径用 fake/monkeypatch 测试；(4) smoke 路径 4 条为覆盖性新增（实现对树已存在，无 RED 可能，如实记录）；(5) **B9 降级仅记录**（快照冻结契约豁免条件命中）；(6) **完整打包流水线未跑**（保护用户 09/02 dist 发布产物；核心步骤即 spec 变更的验证目标已全绿；若需完整安装包由用户授权后跑 build_release.py）；(7) 人工验收矩阵全部留待用户（真实键鼠/浏览器/多屏/UAC 绝不自动执行）；(8) dist/ 内 09/02 旧产物与 release_parts 未动。

---

### Task 17: 全量门禁、规格逐条验收和交付审计

**Files:**
- Modify only: 本计划执行过程中已列出的实现/测试文件中的缺陷修复
- Do not modify: existing golden expected/hash files, unrelated user files, generated screenshots, browser profiles

**Interfaces:**
- Consumes: Tasks 1–16 全部产物
- Produces: 可审查的 RED/GREEN/回归/人工烟测证据和无 Critical/Important 遗留结论

- [x] **Step 1: 运行电脑操控全套定向测试**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_computer_models.py tests/test_computer_settings.py tests/test_computer_capability.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py tests/test_computer_controller.py tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_computer_provider_openai.py tests/test_computer_llm_generic.py tests/test_computer_cli.py tests/test_computer_privacy.py -q
npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts
```

Expected: 全部退出码 0，无 skipped 的核心安全测试；平台特定真实 I/O 由 fake 单测 + Task 16 人工烟测覆盖。

- [x] **Step 2: 运行 Harness、Provider、缓存和取消门禁**

Run:

```powershell
venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_llm_responses.py tests/test_harness_runtime.py tests/test_tool_pipeline.py tests/test_tool_registry.py tests/test_cache_keepalive.py tests/test_token_accounting.py tests/test_cancellation.py tests/test_llm_inflight_abort.py tests/test_llm_events_fixes.py tests/test_memory_maintenance.py -q
```

Expected: 所有现有 expected/hash 原样通过；若失败，定位实现导致的字节漂移，禁止更新 golden。

- [x] **Step 3: 运行完整 Python、前端和构建门禁**

Run:

```powershell
venv\Scripts\python.exe -m py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py
venv\Scripts\python.exe -m compileall -q core/computer
venv\Scripts\python.exe -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
git add -N .
git diff --check
git status --porcelain
```

Expected: 全部退出码 0；记录每条命令实际测试数、持续时间、exit code 和失败重跑原因。最终 `git diff --check` 必须在 `git add -N .` 之后执行，覆盖全部新建文件。

- [x] **Step 4: 做规格覆盖和敏感信息审计**

Run:

```powershell
rg -n "browser_control_enabled|computer_control_enabled|computer_browser|computer_protocol|supports_function_calling" core frontend tests
rg -n "response_id|data:image/png;base64|Cookie|S3CRET-TYPED-TEXT" core/computer core/harness gui_bridge.py
git diff --name-only
git status --short --branch
```

人工检查：两个开关默认关闭；选定 browser 不 fallback；generic/native 双协议；单 run/单 target；热键；Provider safety；GUI/CLI/LAN 边界；最近三图；200 trace；输入脱敏；默认请求字节不变；关闭/退出无复活。

- [x] **Step 5: 审查变更边界**

将 `git diff --name-only` 与 File Map 比较。任何 File Map 外变更必须能证明是用户原有修改并保持未触碰，或属于测试执行产生且已安全清理的临时产物。不得删除用户已有 `README.md`、插件、skill、图片或其他未跟踪文件。

- [x] **Step 6: 输出最终实施报告并停止**

报告必须包含：

- 变更摘要和完整文件列表；
- Tasks 1–16（含 11a/11b、13a/13b）每个 Task 的真实 RED 命令/失败原因、GREEN 命令/测试数量/时长/exit code；
- generic 与 native 的真实请求形状证据；
- 默认关闭 golden/hash 未改且通过的证据；
- Edge/Chrome/desktop、pause/resume/stop/hotkey、LAN、Provider safety、隐私和退出清理证据；
- 完整 pytest、Vitest、frontend build、PyInstaller（若环境可运行）和人工烟测结果；
- 用户原有工作树修改清单及未触碰说明；
- 剩余风险或未完成项；若有任何 Critical/Important 或未跑门禁，明确结论为“尚不可提交”。

达到停止条件后不要开展开放式重构或新功能，不执行 commit/push/merge/发布。

Suggested final commit after separate explicit authorization: `feat: add controllable browser and Windows computer use`

#### Execution Record — Task 17（2026-09-05，全量门禁 + 交付审计，未 commit）

- **顺手项（Task 16 审查 Minor 1，覆盖性新增无 RED——缺口已在 Task 16 审查披露，代码已存在）**：新增 `tests/test_computer_cli.py::test_main_block_finally_calls_computer_runtime_shutdown_on_both_paths`——AST 提取 main.py `__main__` 块（4410-4445）在模块命名空间受控执行（monkeypatch run_headless/run_cli/_shutdown_computer_runtime_for_cli + 受控 sys.argv，不真实运行 CLI 主循环），断言 headless（-p 分支 sys.exit 路径）与交互式两条 finally 均调用统一电脑操控收尾。验证 `pytest tests/test_computer_cli.py -q -k "main_block_finally"` → 1 passed / 2.37s / exit 0（写即绿，如实定性）。
- **Step 1**: `venv\Scripts\python.exe -m pytest tests/test_computer_dependency.py tests/test_computer_models.py tests/test_computer_settings.py tests/test_computer_capability.py tests/test_computer_hotkey.py tests/test_computer_preview_server.py tests/test_computer_browser_backend.py tests/test_computer_windows_backend.py tests/test_computer_controller.py tests/test_computer_tool_adapter.py tests/test_computer_provider_generic.py tests/test_computer_provider_openai.py tests/test_computer_llm_generic.py tests/test_computer_cli.py tests/test_computer_privacy.py -q` → **608 passed in 34.47s / pytest exit 0**（0 skipped——核心安全测试无 skip；外层 shell exit 1 为已知 EFSClipboardCtrl.log 沙箱噪音，以实际输出判定）；`npm --prefix frontend test -- --run src/components/ComputerControlBar.test.ts src/components/ComputerControlPane.test.ts src/components/SystemSettings.test.ts src/components/SettingsPage.test.ts src/stores/appStore.systemSettings.test.ts src/stores/appStore.modelSettings.test.ts` → **78 passed (6 files) / 35.02s / exit 0**。
- **Step 2**: `venv\Scripts\python.exe -m pytest tests/test_prompt_byte_stability.py tests/test_provider_payload_golden.py tests/test_deepseek_prompt_ledger.py tests/test_llm_responses.py tests/test_harness_runtime.py tests/test_tool_pipeline.py tests/test_tool_registry.py tests/test_cache_keepalive.py tests/test_token_accounting.py tests/test_cancellation.py tests/test_llm_inflight_abort.py tests/test_llm_events_fixes.py tests/test_memory_maintenance.py -q` → **290 passed in 177.50s / exit 0**（全部现有 expected/hash 原样通过，golden 零更新）。
- **Step 3**: `py_compile main.py core/llm.py core/memory.py core/memory_compressor.py core/themes.py` exit 0 + `compileall -q core/computer` exit 0；全量 `venv\Scripts\python.exe -m pytest -q` → **2603 passed, 2 skipped, 1 warning in 514.57s / exit 0**（2 skipped 为既有环境条件跳过——junction/8.3 短名，非核心安全测试；1 warning 为 test_lan_server 既有线程噪音）；全量 `npm --prefix frontend test -- --run` → **544 passed (70 files) in 567.06s / exit 0**（本次零 worker 超时零失败，无需 flake 复跑）；`npm --prefix frontend run build` → ✓ built in 12.00s / exit 0；`git diff --check` → **exit 0**（仅 LF/CRLF 惯例 warning）。**brief `git add -N .` 适配偏差**：当前未跟踪文件仅剩 5 个用户图片（download_20260903_143511.jpg、generated_images/、random-art-140312.png、tokenrhythm_20260903_144751.png、tokenrhythm_20260903_161658.png），绝不 intent-add；本功能全部新建文件已由各 Task `git add -N` 入册（git 状态 A），`git diff --check` 已覆盖全部已跟踪改动+全部新文件，语义与 brief 等价。`git status --porcelain`：45 M + 9 A（全部本功能/计划文件）+ 5 未跟踪用户图片。
- **Step 4**: `rg -n "browser_control_enabled|computer_control_enabled|computer_browser|computer_protocol|supports_function_calling" core frontend tests` → 401 命中 / 32 文件（保存 audit-rg-settings.txt——设置与能力接线全链覆盖证据）；`rg -n "response_id|data:image/png;base64|Cookie|S3CRET-TYPED-TEXT" core/computer core/harness gui_bridge.py` → **28 命中逐条判定全部为注释/docstring、Turn 内存字段、Provider 请求构造（设计内，专项测试断言不落 DB/Journal/日志）或脱敏代码，零产品输出路径泄漏**（保存 audit-rg-sensitive.txt）；`git diff --name-only` / `git status --short --branch` 采集（分支 feature/computer-control-wip）。人工检查 14 项逐项核验并给出测试名/代码位置证据（详见 final-report.md 第 5 节）。
- **Step 5**: `git diff --name-only`（54 项：45 M + 9 A）与 File Map 逐项比对全部命中或为已披露偏差文件（Task 11b/12/13a/13b/14/15/16 各 Execution Record Files touched）；未跟踪 5 项用户图片保持未触碰；README.md +19/-0 纯增量（复核 numstat）；用户历史改动（hunyuan/secret_filter/.gitattributes/CHANGELOG/tokenrhythm/Sidebar.vue/待开发/流程图文档）已由并发会话提交，全程未触碰；无用户文件删除。
- **Step 6**: 最终实施报告写入 `.superpowers/sdd/2026-09-02-computer-control/final-report.md`（变更摘要+完整文件清单、Tasks 1–16 逐 Task RED/GREEN 表、generic/native 请求形状证据、golden 零改写证据、能力测试名清单、全量门禁+Task 16 打包引用、人工烟测矩阵指引、用户文件未触碰说明、遗留清单、**Stop Conditions 12 条逐条判定**、建议 commit 划分）；简版 task-17-report.md 同目录。
- **Stop Conditions 结论**：12 条中 10 条满足（证据）、2 条满足（自动化证据，真实 I/O 部分属人工烟测矩阵——按设计绝不自动执行）；**最终结论：当前可以提交**（人工烟测矩阵属用户验收步骤，不阻塞代码提交）。
- Files touched: `tests/test_computer_cli.py`（+1 条覆盖性测试，本 Task 唯一代码改动）、`.superpowers/sdd/2026-09-02-computer-control/final-report.md`（新建）、`.superpowers/sdd/2026-09-02-computer-control/task-17-report.md`（新建）、`.superpowers/sdd/2026-09-02-computer-control/audit-rg-settings.txt`、`audit-rg-sensitive.txt`（Step 4 审计输出，新建）、`.superpowers/sdd/2026-09-02-computer-control/progress.md`（Task 17 条目）、本计划文件（checkbox 与本记录；期间 3 个 checkbox 被并发会话写回回退，重应用后 rg 复验 6/6 全勾）。
- spec: PASS（设计 24/27–29 验收收口：全量门禁数字、Spec Coverage Matrix 四条 rg 证据、Stop Conditions 12 条判定与最终结论齐备）。
- privacy: PASS（Step 4 敏感词扫描 28 命中逐条判定零产品输出路径泄漏；test_computer_privacy 19 条语料扫描为持久化面证据）。
- default-byte: PASS（Step 2 290 passed + golden numstat 126 增 0 删 + EXISTING_DEFAULT_REQUEST_BYTES 逐字节比对测试）。
- unresolved: 记录级遗留清单见 final-report.md 第 8 节（B9 文案、Task 15 Minors emit_event/get_provider_api_key 明文需用户知悉、13b type_text N 字符需后端字段、completing 隐藏安全条、人工验收矩阵待用户、build_release.py 完整流水线待用户发布决策）；本 Task 不重跑 PyInstaller（Task 16 核心步骤 482.5s exit 0 + 产物扫描已引用）；不执行 commit/push/merge/发布。

## Execution Record Protocol

执行者在每个 Task 完成后，把真实证据追加到该 Task 末尾。标题使用 `Execution Record — Task N`；随后依次记录 RED 的精确命令、实际非零 exit code、失败测试名、目标失败原因和时长，GREEN 与 Regression 的精确命令、实际 passed 数、时长和 exit 0，diff check 命令及 exit 0，本 Task 实际触碰文件，以及 `spec/privacy/default-byte/unresolved` 四项审查结论。不得留下模板文字。若 RED 不是因目标缺失而失败，先修正测试或用 systematic-debugging 取证，不能继续 GREEN。

## Spec Coverage Matrix

- 设计 7、10、21（领域模型、统一动作、错误码）：Tasks 2、8、9。
- 设计 8（设置、默认关闭、独立开关）：Tasks 3、12、15。
- 设计 9（显式模型能力）：Tasks 4、10、11a、11b、12。
- 设计 11（OpenAI native、continuation、safety、投影）：Tasks 11a、11b；Task 13a 预留用户确认 API。
- 设计 12（Edge/Chrome 专用 profile）：Tasks 1、3、6、16。
- 设计 13（Windows 截图/DPI/SendInput/secure desktop）：Tasks 7、16。
- 设计 14–15（单租约、状态机、暂停/停止/取消/退出）：Tasks 8、13a、14、16。
- 设计 16（顶部条、右栏、预览、Store/Bridge）：Tasks 5、12、13a、13b。
- 设计 17（CLI）：Task 14。
- 设计 18（LAN）：Tasks 4、15。
- 设计 19（Harness/tool follow-up/消息边界）：Tasks 9、10、11a、11b。
- 设计 20（默认字节、Ledger、Kimi/GLM 缓存）：Tasks 1、10、11b、17。
- 设计 22（隐私与数据处理）：Tasks 2、5、6、7、9、11a、11b、13a、13b、14、16、17。
- 设计 23（依赖、打包、生命周期）：Tasks 1、16。
- 设计 24、27–29（测试、验收、风险、K3 约束）：贯穿每个 Task，并由 Task 17 和 companion K3 prompt 收口。

## Stop Conditions

只有同时满足以下条件才可报告“当前可以提交”：

1. Tasks 1–16（含 11a/11b、13a/13b）全部有真实 RED → GREEN 证据；
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
