# Win10 窗口边界（阴影 + 边框）修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Y-CODE GUI 窗口在 Windows 10 上恢复可见边界（DWM 系统阴影 + 1px 主题色内边框），效果对齐 Windows 11。

**Architecture:** 方案 C = 原生层 + 前端层双管齐下。原生层：Win10（build < 22000）下把 `DwmExtendFrameIntoClientArea` 的 margins 从清零改为 1px，换取 DWM 系统阴影（Win10 无 `DWMWA_BORDER_COLOR` API，1px 玻璃线无法隐藏，由前端 CSS 边框覆盖视觉）。前端层：启动 fragment 注入 `os=win10` 标记，`applyBootTheme` 给根元素加 `os-win10` class，样式表用 `inset box-shadow` 画 1px 主题色边框（不占布局空间，避免溢出滚动条）。

**Tech Stack:** Python 3 / ctypes DWM API / pywebview(WinForms) / Vue 前端 / pytest / vitest

## Global Constraints

- 仅 Windows 10（`sys.getwindowsversion().build < 22000`）改变行为；Windows 11 现有表现（阴影 + 圆角 + 无边框线）必须逐像素保持不变。
- 最小改动：不重构 `core/frameless.py` 的子类化/命中测试逻辑，不动 `WS_THICKFRAME` 方案。
- 所有测试在 Win11 开发机上必须通过（Win10 行为用 monkeypatch 版本号模拟，不得依赖真实 OS）。
- 版本判断复用现有写法 `sys.getwindowsversion().build < 22000`（与 `core/frameless.py` 一致）。

## 背景与根因（执行前必读）

- 窗口创建：`gui_main.py:_create_main_window` 使用 `frameless=True, shadow=False`。
- `core/frameless.py:enable_native_window_behaviors` 加回 `WS_THICKFRAME|WS_MINIMIZEBOX|WS_MAXIMIZEBOX` 恢复 resize/Snap；`WM_NCCALCSIZE` 返回 0 清空非客户区。
- `core/frameless.py:suppress_frame_border(hwnd)` 当前逻辑：无条件 `DwmExtendFrameIntoClientArea(0,0,0,0)` 清零 → Win11 设 `DWMWA_BORDER_COLOR=NONE` + 圆角；Win10 直接 `return None`（只打 warning 日志）。
- Win11 DWM 对 THICKFRAME 窗口自动画阴影；**Win10 DWM 只给带标题栏窗口画阴影**，纯 THICKFRAME + 无非客户区窗口既无边框也无阴影 → 窗口与背景融为一体（本 issue）。
- Win10 唯一获得 DWM 阴影的公开办法：`DwmExtendFrameIntoClientArea` margins ≥ 1px（Qt FramelessHelper 同款配方），代价是 1px 玻璃线且无 API 隐藏。
- `suppress_frame_border` 会被 `gui_bridge.py:_apply_frameless_titlebar_memory`（主题切换时）反复调用，改动必须幂等。
- 前端启动主题通道：`gui_main.py:_build_gui_theme_fragment` 产出 `#theme=light&bg=f4f4f1`；`frontend/src/themes/runtime.ts:applyBootTheme(hash, body)` 用 `literalFragmentValue` 解析。

---

### Task 1: Win10 恢复 DWM 系统阴影（core/frameless.py）

**Files:**
- Modify: `core/frameless.py`（`suppress_frame_border` 函数，约 185-238 行）
- Test: `tests/test_frameless.py`

**Interfaces:**
- Consumes: 现有 `_DWM_MARGINS`、`ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea`
- Produces: `suppress_frame_border(hwnd) -> bool | None` 签名与返回语义不变（Win10 仍返回 `None` 表示"不支持隐藏边框"）；行为变化仅 Win10 margins=1px。

- [ ] **Step 1: 更新失败测试（先红）**

`tests/test_frameless.py` 中：
1. 找到 `test_suppress_frame_border_reports_unsupported_before_windows_11`（约 118-137 行，monkeypatch 版本为 Win10 的用例），新增断言：Win10 下 `DwmExtendFrameIntoClientArea` 收到的 margins 四边均为 1：

```python
call = fake_dwm.DwmExtendFrameIntoClientArea.call_args
assert call is not None
margins = call.args[1].contents  # 或按现有测试的取参方式（先读现有代码对齐写法）
assert (margins.cxLeftWidth, margins.cxRightWidth,
        margins.cyTopHeight, margins.cyBottomHeight) == (1, 1, 1, 1)
assert frameless.suppress_frame_border(123) is None
fake_dwm.DwmSetWindowAttribute.assert_not_called()
```

2. 检查 `test_suppress_frame_border_clears_pywebview_extended_glass_margin`（约 77 行）：确认其 monkeypatch 的版本号为 Win11（build ≥ 22000），断言 margins 清零保持不变；若该用例未 mock 版本号，需补上 Win11 mock（否则 Win10 真机跑测试会挂）。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_frameless.py -x -q`
Expected: 新断言 FAIL（Win10 分支当前 margins=0）

- [ ] **Step 3: 修改 suppress_frame_border（最小实现）**

`core/frameless.py` `suppress_frame_border` 内，将"无条件清零 margins"改为按版本分支：

```python
    win11_or_later = sys.getwindowsversion().build >= 22000
    # Win10：DWM 阴影只跟随玻璃框延伸，1px 延伸换系统阴影（无 API 隐藏边线，
    # 视觉边线由前端 os-win10 CSS 边框覆盖）。Win11：清零撤销 pywebview 延伸，
    # 后续用 DWMWA_BORDER_COLOR=NONE 隐藏外框。
    margin = 0 if win11_or_later else 1
    margins = _DWM_MARGINS(margin, margin, margin, margin)
```

后续 `extend_frame(...)` 调用、Win11 的 COLOR_NONE/圆角逻辑、`build < 22000` 返回 `None` 均保持不变（把 `sys.getwindowsversion().build < 22000` 判断改为复用 `win11_or_later` 变量亦可，二选一，不重复调用）。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_frameless.py -q`
Expected: PASS（全部用例）

- [ ] **Step 5: Commit**

```bash
git add core/frameless.py tests/test_frameless.py
git commit -m "fix(gui): Win10 frameless 窗口恢复 DWM 系统阴影（1px 玻璃框延伸）"
```

---

### Task 2: 启动 fragment 注入 os=win10 标记（gui_main.py）

**Files:**
- Modify: `gui_main.py`（`_build_gui_theme_fragment`，约 212-220 行）
- Test: `tests/test_gui_theme.py`

**Interfaces:**
- Consumes: `sys.getwindowsversion()`（标准库）
- Produces: fragment 新增可选段 `&os=win10`（仅 Win10）；前端 `applyBootTheme` 按 Task 3 解析。fragment 现有段顺序 `theme=..&bg=..[&gui_theme_error=1]` 保持，`os=win10` 追加在 `bg` 之后、`gui_theme_error` 之前或最后均可（解析端正则按 `&` 分段，与顺序无关）。

- [ ] **Step 1: 写失败测试**

`tests/test_gui_theme.py` 新增两个用例（monkeypatch `sys.getwindowsversion`，参照 `tests/test_frameless.py` 里版本 mock 的写法）：

```python
def test_startup_fragment_marks_win10(monkeypatch):
    monkeypatch.setattr(gui_main.sys, "getwindowsversion",
                        lambda: SimpleNamespace(build=19045))
    assert gui_main._build_gui_theme_fragment({
        "appearance": "light", "background": "#f4f4f1",
    }) == "theme=light&bg=f4f4f1&os=win10"


def test_startup_fragment_no_os_marker_on_win11(monkeypatch):
    monkeypatch.setattr(gui_main.sys, "getwindowsversion",
                        lambda: SimpleNamespace(build=22631))
    assert gui_main._build_gui_theme_fragment({
        "appearance": "light", "background": "#f4f4f1",
    }) == "theme=light&bg=f4f4f1"
```

注意：现有用例 `test_startup_fragment_marks_only_active_theme_error`（约 260 行）硬编码断言 fragment 相等，**在 Win10 真机上会因多出 `&os=win10` 而失败**——需给这些现有用例统一补 Win11 版本 mock（monkeypatch build=22631），保证跨 OS 可重复。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_gui_theme.py -x -q`
Expected: 新用例 FAIL（fragment 无 os 段）

- [ ] **Step 3: 修改 _build_gui_theme_fragment**

```python
def _build_gui_theme_fragment(gui_theme: dict) -> str:
    fragment = (
        f"theme={gui_theme['appearance']}"
        f"&bg={gui_theme['background'].lstrip('#')}"
    )
    # Win10 frameless 无 DWM 外框可隐藏（无 DWMWA_BORDER_COLOR），
    # 前端据此补 1px CSS 内边框；Win11 由 DWM 阴影+圆角勾勒边界，无需标记。
    if sys.getwindowsversion().build < 22000:
        fragment += "&os=win10"
    if gui_theme.get("error") is not None:
        fragment += "&gui_theme_error=1"
    return fragment
```

（`gui_main.py` 已 `import sys`，无需新增 import；若无则补。）

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_gui_theme.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add gui_main.py tests/test_gui_theme.py
git commit -m "feat(gui): 启动 fragment 注入 os=win10 标记供前端补边框"
```

---

### Task 3: 前端 os-win10 的 1px 主题边框（frontend）

**Files:**
- Modify: `frontend/src/themes/runtime.ts`（`applyBootTheme`，约 194-209 行）
- Modify: 主题样式表（执行时先定位 token：在 `frontend/src` 下搜 `--border` / `.light` 变量定义所在 css/scss 文件，选承载全局主题变量的那个）
- Test: `frontend/src/themes/runtime.test.ts`

**Interfaces:**
- Consumes: Task 2 的 fragment 段 `os=win10`；现有 `literalFragmentValue(fragment, name)` 解析器
- Produces: `document.documentElement` class `os-win10`；样式规则 `.os-win10` 生效

- [ ] **Step 1: 写失败测试**

`frontend/src/themes/runtime.test.ts` 追加（对齐现有 `applyBootTheme` 用例写法）：

```ts
it('marks os-win10 class on documentElement for win10 fragment', () => {
  applyBootTheme('#theme=light&bg=f4f4f1&os=win10', document.body);
  expect(document.documentElement.classList.contains('os-win10')).toBe(true);
});

it('does not mark os-win10 without the fragment flag', () => {
  applyBootTheme('#theme=light&bg=f4f4f1', document.body);
  expect(document.documentElement.classList.contains('os-win10')).toBe(false);
});
```

（注意用例间清理：现有文件若未在 afterEach 清 class，测试内手动 `document.documentElement.classList.remove('os-win10')`。）

- [ ] **Step 2: 跑前端测试确认失败**

Run: `cd frontend; npx vitest run src/themes/runtime.test.ts`
Expected: 新用例 FAIL

- [ ] **Step 3: 修改 applyBootTheme + 样式**

`frontend/src/themes/runtime.ts` 的 `applyBootTheme` 末尾追加：

```ts
  if (literalFragmentValue(fragment, 'os') === 'win10') {
    body.ownerDocument.documentElement.classList.add('os-win10');
  }
```

主题样式表追加（颜色用现有 border token；用 inset box-shadow 而非 border，不占布局空间、不会引入 1px 溢出滚动条；Win10 窗口为直角，无需 radius 适配）：

```css
/* Win10 frameless 窗口无 DWM 阴影边线（build<22000 无 DWMWA_BORDER_COLOR），
   用 1px 内阴影勾勒窗口边界，颜色跟随主题边框 token */
.os-win10 body {
  box-shadow: inset 0 0 0 1px var(--border-color); /* token 名以代码库实际为准 */
}
```

若 token 不存在，按现有主题变量文件里的边框色命名接入；再没有则用 `rgba(0,0,0,0.12)`（light）/ `rgba(255,255,255,0.12)`（dark）两条规则兜底。

- [ ] **Step 4: 跑前端测试确认通过**

Run: `cd frontend; npx vitest run src/themes/runtime.test.ts`
Expected: PASS；随后 `cd frontend; npm run build`（或项目既有构建命令）确认样式编译无误

- [ ] **Step 5: Commit**

```bash
git add frontend/src/themes/runtime.ts frontend/src/themes/runtime.test.ts <样式文件>
git commit -m "feat(gui): Win10 前端 1px 主题内边框勾勒窗口边界"
```

---

### Task 4: 整体验证

**Files:** 无（只读验证）

- [ ] **Step 1: 全量回归**

Run: `pytest tests/test_frameless.py tests/test_gui_theme.py -q; cd frontend; npx vitest run src/themes/runtime.test.ts`
Expected: 全 PASS

- [ ] **Step 2: Win11 开发机回归（防劣化）**

本机启动 GUI（`python gui_main.py` 或既有启动方式），确认 Win11 下窗口阴影/圆角/无边框线与现状一致；切换明暗主题无异常。

- [ ] **Step 3: Win10 实机验收（用户配合）**

在 Win10 机器启动应用，逐项确认：
1. 窗口四边有 DWM 阴影（与背景可区分）
2. 窗口边缘有 1px 主题色边框（浅色/深色主题均可见、不刺眼）
3. 边缘拖拽 resize、Aero Snap（Win+方向键）、双击标题栏最大化、最大化不遮任务栏——均不回退（本计划未动这些路径，属回归确认）
4. 切换明暗主题后阴影与边框仍在（`suppress_frame_border` 幂等重设）

## Self-Review 记录

- Spec 覆盖：阴影（Task 1）+ 边框线（Task 3）+ OS 标记通道（Task 2）+ 验收（Task 4）✓
- 风险点：Win10 实机效果无法在本机（Win11）验证，Task 4 Step 3 为必要人工验收；`suppress_frame_border` 幂等性已在 Task 1 Step 3 保持（函数每次调用都重设 margins）。
- 已排除方案 D（透明窗口自绘阴影）：WebView2 客户区不透明，风险/收益不成比例。
