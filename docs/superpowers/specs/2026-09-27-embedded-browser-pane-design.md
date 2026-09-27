# 右侧面板内嵌浏览器设计（原生 WebView2 表面叠加）

- 日期：2026-09-27
- 状态：已实施，前后端测试全绿
- 目标读者：接手维护的 agent（无需查阅前置对话）

---

## 1. 背景与目标

右侧面板的「浏览器」页签此前只是 `ComputerControlPane` 的占位复用（与电脑操控页签共享同一
引导态），没有真实网页浏览能力。本次为该页签落地一个真正的内嵌浏览器：地址栏（输入网址或
搜索词）、后退/前进、刷新/停止、快捷起始页，等价于 ZCode Electron 的 `<webview>` 方案。

核心约束：pywebview 自身的 webview 以 `Dock=Fill + BringToFront` 铺满整个主窗体，DOM 侧
无法承载第三方浏览器内核。因此采用**原生表面叠加**方案——在主窗体之上再叠加一个独立的
WebView2 WinForms 控件，用前端持续上报的矩形（CSS 像素）做绝对定位，并每次 `BringToFront`
压住主 webview，在右侧面板内形成「内嵌浏览器」。

## 2. 架构与文件构成

### 2.1 后端

| 文件 | 职责 |
|---|---|
| `core/embedded_browser.py` | `EmbeddedBrowserController`：WebView2 控件的创建、定位、导航与状态快照 |
| `gui_browser_mixin.py` | `BrowserBridgeMixin`：`browser_pane_*` 系列 js_api 桥方法 + 状态事件回推 |
| `gui_bridge.py` | `ApiBridge` 挂载 mixin；退出链 best-effort 调用 `shutdown()` 释放控件 |

`core/embedded_browser.py` 的关键设计：

- **惰性 import**：顶层只依赖标准库，`clr` / WinForms / WebView2 全部在首次使用时 import，
  保证 headless 环境（pytest、非 Windows）能安全 import 本模块。
- **GUI 线程纪律**：桥方法从 js_api 线程进入，一律用 `form.Invoke` 同步 marshal 到 GUI
  线程执行；WebView2 事件回调本身就在 GUI 线程，状态经 `on_state` 回调线程安全入队。
- **状态推送节流**：导航过程会密集触发 SourceChanged/TitleChanged/NavigationStarting/
  Completed 等事件，`_schedule_emit` 做 150ms 节流 + 尾随补发，把密集事件合并为末态快照。
- **DPI 换算**：`css_rect_to_physical` 把前端上报的 CSS 像素矩形乘以缩放系数转为物理像素；
  frameless 主窗体客户区原点与窗口原点一致（无标题栏偏移），负值截 0。
- **弹窗拦截**：`NewWindowRequested`（`target=_blank` 等）一律 `Handled=True`，目标地址经
  `normalize_url` 过滤后在**同一控件内**导航，不产生不受控的弹出窗口。
- **生命周期**：`shutdown()` 释放控件、停掉节流定时器；内部吞异常，失败不阻断退出链。

`gui_browser_mixin.py` 的关键设计：

- 控制器为**惰性单例**（`self._embedded_browser`），仅 Windows 上创建；创建失败或非
  Windows 时所有桥方法统一返回 `{'ok': False, 'error': 'unsupported_platform'}`。
- 主窗体通过 `webview.platforms.winforms.BrowserView.instances[uid]` 获取。
- 控制器状态快照经 `emit_event('embedded_browser', snapshot)` 回推前端。

### 2.2 前端

| 文件 | 职责 |
|---|---|
| `frontend/src/components/BrowserPane.vue` | 浏览器工作台 UI：工具栏 + 表面占位区 + 起始页/不支持态 |
| `frontend/src/composables/useEmbeddedBrowserSurface.ts` | 「矩形上报」逻辑收口：测量、去重、心跳、遮挡检测 |
| `frontend/src/stores/appStore.ts` | `embeddedBrowser` 状态 + `embedded_browser` 桥事件独立支路 |
| `frontend/src/components/RightPanel.vue` | 浏览器页签激活时给内容区加 `flush` 类（去内边距、竖向 flex） |
| `frontend/src/types/pywebview.d.ts` | `EmbeddedBrowserRect/State/PaneResult` 类型与桥方法签名 |

`useEmbeddedBrowserSurface.ts` 的关键设计（原生控件不是 DOM 元素、不参与浏览器布局，
前端唯一能做的是持续把占位区矩形上报给宿主）：

- **去重**：`open`/`hide` 是重操作（创建/显隐 WebView2），只在可见性翻转时各调一次；
  `set_bounds` 只在矩形真正变化时调用，避免每帧刷屏。
- **心跳兜底**：每 2s 重报一次矩形，覆盖缩放/滚动/DPI 变化等异常路径的漏报，自愈错位。
- **遮挡检测**：原生控件永远绘制在 DOM 之上。若有 DOM 覆盖层（弹窗、标签页选择页）压住
  占位区，必须主动 `hide`，否则控件会「穿」过覆盖层显示出来。
- **起始页让位**：起始页/不支持态下 `shouldShowSurface()` 返回 false，前端主动 hide——
  否则原生空白页会盖住 DOM 起始页。
- 所有桥调用 try/catch 吞掉：桥未就绪或平台不支持不应打断 UI。

地址栏输入规整（`BrowserPane.vue`）：含空白字符或不构成合法域名/URL 的输入，转为必应搜索
`https://www.bing.com/search?q=<encodeURIComponent(text)>`；快捷起始页含必应等常用站点卡片。

## 3. API 契约

`browser_pane_*` 系列统一返回：成功 `{ok: true, state?}`；失败 `{ok: false, error}`。
`error` 取值：`unsupported_platform` / `invalid_rect` / `form_unavailable` / `not_open` /
`invoke_failed` / `not_ready` / `invalid_url`（前端只特判 `unsupported_platform`）。

| 方法 | 行为 |
|---|---|
| `browser_pane_open(rect, url?)` | 创建/显示控件并定位到 rect；url 为空串表示只创建不导航 |
| `browser_pane_set_bounds(rect)` | 重新定位（矩形为 DOM 占位区的 CSS px） |
| `browser_pane_navigate(url)` | 导航（先经后端 `normalize_url` 过滤） |
| `browser_pane_back / forward / reload / stop` | 导航控制 |
| `browser_pane_hide()` | 隐藏原生表面（让出 DOM 起始页/避免穿透覆盖层） |
| `browser_pane_get_state()` | 主动拉取一次状态快照 |

状态快照字段（`EmbeddedBrowserState`）：`url / title / can_go_back / can_go_forward /
loading / ready / visible`。前端 `applyEmbeddedBrowserState` 做字段强转合并，容忍缺省。

## 4. 安全

后端 `normalize_url` 是所有导航入口的唯一收敛点：

- 显式危险 scheme 一律拒绝：`javascript:` `data:` `file:` `vbscript:` `blob:` `mailto:`
  `about:`（`about:blank` 除外）`tel:` `chrome:` `edge:` `view-source:`。
- 含空白字符的输入拒绝（避免把任意文本当 URL），搜索语义由前端转必应搜索 URL 承接。
- `http(s)://` 直通；`//host` 补 `https:`；裸域名/localhost/IPv4（可带端口）补 `https://`；
  其余一律返回空串拒绝加载。

## 5. 平台降级

- 仅 Windows 桌面端 GUI 支持（`is_supported()` 判 `sys.platform == 'win32'`）。
- 非 Windows、缺桥、控件创建失败：桥方法统一返回 `unsupported_platform`，前端收到后切
  「不支持态」（工具栏禁用 + 提示文案），且后续完全不再发桥调用。

## 6. 测试与验证（2026-09-27 实测）

- 后端新增：`tests/test_embedded_browser.py` + `tests/test_gui_browser_mixin.py`，46 用例通过。
- 前端新增：`BrowserPane.test.ts`（12）+ `RightPanel.test.ts`（13）+
  `appStore.embeddedBrowser.test.ts`（1），26 用例通过。
- 全量回归：前端 vitest 117 文件 953 用例全绿，`vue-tsc -b` 类型检查通过；后端 pytest
  2108 通过、唯一失败 `test_real_pty_session_roundtrip` 为环境缺 `winpty` 包（既有环境
  问题，`core/gui_terminal.py` 本次未改动，与本特性无关）。
