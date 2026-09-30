# 对话 / 文件树里的 HTML 在右栏内嵌浏览器打开 设计

日期：2026-09-30
状态：已实现（2026-09-30）；同日扩展 PDF / Markdown，见第 8 节

## 1. 背景与目标

对话正文与右栏文件树里的本地文件路径目前统一走「源码预览弹窗」（`store.openFilePreview`
→ 后端 `read_file_preview`），HTML 文件只能看到源码，看不到渲染效果。用户要求：点击
`.html / .htm` 文件时，直接在右侧面板的「浏览器」页签（原生 WebView2 表面）里按网页打开。

## 2. 范围

### 覆盖

1. 对话正文里的 HTML 路径链接（裸路径、显式 Markdown 链接、反引号路径——三者都经
   `MessageBubble.onMdBodyClick` 的 `a[data-file-link]` 分支）。
2. 右栏文件树里的 HTML 文件行（`FileTreeNode`，含任意递归层级）。

### 不覆盖

1. 非 HTML 文件（`.md / .py / .png` 等）：保持原有源码预览弹窗与图片预览。
2. 移动端（<=768px）：右面板整体隐藏，HTML 也回退源码预览弹窗。

## 3. 现状约束

- 内嵌浏览器页面由宿主叠加的原生 WebView2 控件渲染，前端 `BrowserPane.vue` 只做
  工具栏交互 + 矩形上报，导航统一经 `useEmbeddedBrowserSurface.navigate()`。
- 后端 `core/embedded_browser.py` 的 `normalize_url` 是所有导航入口的唯一收敛点，
  原先 `file:` 被 `_REJECTED_SCHEMES_RE` 显式拒绝，`is_navigation_allowed`
  （页面自身发起导航的兜底）同口径拒绝——本地 HTML 根本无法加载。
- 前端没有「按扩展名分流点击行为」的既有机制，`isHtmlFilePath` / `toFileUrl` 为新增工具。

## 4. 架构与数据流

```
点击 HTML（MessageBubble 正文链接 / FileTreeNode 文件行）
  → 绝对路径（相对路径先用 store.workdir 拼）
  → isHtmlFilePath 命中且非移动端
  → store.openLocalFileInBrowser(path)
      → toFileUrl(path) 生成 file:///D:/x/a.html
      → 写入 browserNavRequest + browserNavNonce 自增，并 openRightPanelTab('browser')
  → BrowserPane watch(nonce) → consumeBrowserNavRequest()
      → 桥就绪：goTo(url) → surface.navigate(url) → 后端 normalize_url → WebView2 Navigate
      → 桥未就绪：挂起到 pendingExternalUrl，pywebviewready 后补导航
      → 宿主不支持（非 Windows / 缺桥）：回退 store.openFilePreview(path) + 提示
```

## 5. 组件改动

### 5.1 后端 `core/embedded_browser.py`（放行口径：整体放开 file://，方案 B）

- 新增 `path_to_file_uri(path)`：盘符路径（`C:\x`、`C:/x`）与 UNC 路径（`\\srv\share\x`）
  → `file:///C:/x` / `file://srv/share/x`；每段 `quote(safe="")` 编码（空格、中文、
  `#'?` 都能正确转义）。相对路径、只有盘符、含 `..` 的路径一律返回 `''`。
- 新增 `normalize_file_url(raw)`：接受 `file://` URL 与裸绝对路径，统一走 `path_to_file_uri`；
  `file:C:/x` 这类非标准写法拒绝。
- `normalize_url` 新增本地文件分支，**放在「含空格即拒绝」之前**（本地路径带空格是常态）；
  `file:` 从 `_REJECTED_SCHEMES_RE` 移除。
- `is_navigation_allowed` 口径自动同步（它复用 `normalize_url`）：本地 HTML 页内互链、
  meta refresh 到另一个本地页都会放行，行为对齐系统浏览器。

### 5.2 前端工具 `frontend/src/utils/localPath.ts`

- `isHtmlFilePath(path)`：扩展名 `.html / .htm`（忽略查询串与锚点）。
- `toFileUrl(absPath)`：与后端 `path_to_file_uri` **同口径**产出 URI；额外做一个
  `encodeSeg`（`encodeURIComponent` + 补编码 `! ' ( ) *`），保证前端乐观显示的地址栏
  与后端规范化结果字面一致。

### 5.3 store（`appStore.ts`）

- 新增瞬态字段 `browserNavRequest: { url, path } | null` 与 `browserNavNonce`：
  nonce 自增让同一文件重复点击也能被组件侧 watch 观察到（对象内容相同不会触发 watch）。
- 新增 action `openLocalFileInBrowser(path)`：转 URL 失败（相对路径等）直接回退
  `openFilePreview`，不静默失败。

### 5.4 组件

- `BrowserPane.vue`：新增 `consumeBrowserNavRequest()`——不支持态回退预览 + 提示、
  桥未就绪挂起到 `pendingExternalUrl`（`onBridgeReady` 补导航）、就绪直接 `goTo(url)`；
  挂载时统一消费一次（请求可能早于组件挂载）。
- `MessageBubble.vue` / `FileTreeNode.vue`：点击分支按 `isHtmlFilePath` 分流，移动端走预览。

## 6. 错误处理与安全

- 路径无法转成 `file://`（相对路径）→ 回退源码预览。
- 宿主不支持内嵌浏览器（非 Windows 桌面端 GUI / 桥缺失）→ 回退源码预览 + 聊天区提示。
- 桥两步注入竞态：请求先到则挂起，`pywebviewready` 后补导航（不丢点击）。
- 后端仍拒绝 `file:C:/x`、相对路径、含 `..` 的路径；`javascript:` / `data:` 等危险
  scheme 继续被 `normalize_url` 与 `is_navigation_allowed` 拦截（回归用例保留）。

## 7. 测试计划与结果

- 后端 `tests/test_embedded_browser.py`：`normalize_url` 的 file 用例（含空格/中文编码、
  UNC、拒绝相对路径与 `..`）、`normalize_file_url` 独立用例、导航白名单参数化更新
  （`file:///C:/Windows/win.ini` 由「取消」改为「放行」）。结果：**77 → 87 passed**
  （含 `tests/test_gui_browser_mixin.py`）。
- 前端：`localPath.test.ts`（`isHtmlFilePath` / `toFileUrl`）、新增
  `appStore.browserNav.test.ts`、`FileTreeNode.test.ts`、`BrowserPane.test.ts`
  （外部请求导航 / 桥未就绪挂起后补导航 / 不支持态回退预览）、`MessageBubble.test.ts`
  （HTML 走浏览器、非 HTML 走预览）。结果：**全量 126 文件 1060 测试全通过**，
  `vue-tsc -b` 无类型错误。

## 8. 第二轮扩展：PDF 与 Markdown（2026-09-30）

### 8.1 实测结论（关键技术前提）

临时探针脚本（独立 pywebview 窗口加载 `file://`，不动用户界面）：

- **PDF：内嵌 WebView2 的内置 PDF 查看器可用**。导航完成（loading 转回 false）、
  `DocumentTitle` 变成文件名、未触发下载 —— 因此直接 `file://` 导航即可，
  不需要额外包装或引入 PDF.js。
- **Markdown：内联显示的是源码文本**（标题同样变成文件名）。若直接导航，效果比
  原来的源码预览弹窗还差 —— 所以 md 必须走「先渲染成 HTML 再打开」。

### 8.2 分流

类型判定集中到 `canOpenInRightPanel`（html / htm / pdf / md / markdown），
点击入口统一调 `store.openInRightPanel(path)`：

- **html / pdf** → `openLocalFileInBrowser` 直接 `file://` 导航。
- **md** → 前端渲染成预览页再打开：
  `read_file_preview` 取内容 → `buildMarkdownPreviewHtml`（与消息气泡同一渲染器 +
  内联阅读样式，深浅色自适应）→ 桥 `write_preview_page` 落盘
  （`core/web_preview`：目录 `~/.ycode/exports/preview`，文件名取源路径 sha1 前 12 位，
  同一源文件重复点击覆盖同一份）→ `openLocalFileInBrowser(生成的 HTML 路径)`。
- **任一环失败**（缺桥 / 文件读取失败 / 落盘失败 / 内容超 8MB）→ 回退源码预览弹窗。

### 8.3 预览页的两个后处理

1. 相对图片/链接地址 → 源文件所在目录下的 `file://` 绝对地址（预览页落在导出目录，
   与源文件不同目录）；中文与空格按段百分号编码。
2. 渲染器在「没有媒体上下文」时会把 `![alt](a.png)` 降级成 `a[data-file-link]`
   （消息气泡里靠点击协议处理），预览页是普通网页、点了没反应 —— 因此把指向图片的
   这类链接还原成 `<img>`（浏览器能直接显示本地图片），其余改成可点的 `file://` 链接。

### 8.4 测试与验证

- 后端：新增 `tests/test_web_preview.py`，`tests/test_gui_browser_mixin.py` 补
  `write_preview_page` 用例 → **95 passed**。
- 前端：新增 `previewHtml.test.ts`，并扩展 `localPath` / `appStore.browserNav` /
  `FileTreeNode` / `MessageBubble` 用例 → **全量 127 文件 1079 测试全通过**；
  `vue-tsc -b` 与 `npm run build` 均通过。
