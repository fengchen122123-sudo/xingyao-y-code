# GUI 自定义主题续作交接方案

## 1. 当前接手点

- 分支：`dev`
- 已完成代码提交：`b20d59e fix: serialize GUI theme transactions`
- 原设计：`docs/superpowers/specs/2026-08-21-gui-custom-theme-design.md`
- 完整实施计划：`docs/superpowers/plans/2026-08-21-gui-custom-theme.md`
- 本交接只描述剩余工作，不继续实现或执行测试。

已完成 Task 1–5：

1. Python GUI 主题 Schema、内置深浅主题、持久化、迁移。
2. 背景图片、主题 ZIP 导入导出、路径和压缩包安全。
3. PyWebView 主题桥、启动恢复、Windows 原生标题栏颜色。
4. TypeScript 主题契约、CSS 变量运行时、Blob URL 生命周期。
5. Pinia 主题状态机、预览/取消/保存/启用、启动错误门禁、顶部深浅主题快捷切换。

CLI 主题未修改，后续也必须保持 `core/themes.py`、CLI `/theme` 和 `config.theme` 现状。

## 2. 当前工作区特别说明

Task 6 被中止时留下两份未提交的测试草稿：

- 已修改：`frontend/src/components/SettingsPage.test.ts`
- 新文件：`frontend/src/components/GuiThemeSettings.test.ts`

尚未创建：

- `frontend/src/components/GuiThemeSettings.vue`
- `frontend/src/components/theme/ThemeRegionEditor.vue`

这两份测试只是草稿，不是已完成实现。换电脑后若只取得提交 `b20d59e`，不会包含它们；可根据原计划重新创建。若保留草稿，需先检查其中直接 mock PyWebView API 的设计，最终组件应优先调用 Pinia store action，不在组件内散落桥接调用。

## 3. 剩余任务一：设置页主题编辑器

目标：让用户在“设置 → 外观与主题”中完成所有 GUI 主题操作。

### 3.1 Store 补充

在 `frontend/src/stores/appStore.ts` 增加主题管理 action，统一封装：

- 刷新主题列表和读取指定主题。
- 复制内置主题为用户主题。
- 重命名、删除、导入、导出用户主题。
- 为指定分区选择图片、移除图片。
- 将后端结构化错误原样传给界面。

组件只调用 store，不直接维护另一套活动主题真值。已有预览、取消、保存、启用操作继续使用 Task 5 的 FIFO 事务队列。

### 3.2 创建主题设置主组件

创建 `frontend/src/components/GuiThemeSettings.vue`，包含：

- 主题列表和当前主题选择。
- 内置主题只显示“启用”和“复制为自定义主题”，所有编辑控件禁用。
- 用户主题提供重命名、删除、导入、导出、保存、取消和恢复继承值。
- 删除活动主题时显示后端返回的明确错误，不自动切换主题。
- 切换主题或离开页面前，如草稿有改动，必须确认保存、放弃或留在当前页面。
- 请求进行中禁用重复提交按钮。
- 显示 `gui_theme_list_error`，但不能用它阻断整个应用；只有 `gui_theme_error` 是启动阻断错误。

### 3.3 创建分区编辑组件

创建 `frontend/src/components/theme/ThemeRegionEditor.vue`，支持：

- 七个 GUI 分区：顶部应用栏、左侧栏、会话工具栏、主工作区、输入区、右侧面板、底部状态栏。
- 全局语义颜色与每个分区的背景、文字、边框、强调色。
- 背景透明度、图片透明度。
- 图片尺寸：`cover`、`contain`、`auto`。
- 图片位置：Schema 允许的固定位置值。
- 图片选择和移除。
- Windows 系统标题栏的背景、文字、边框和 light/dark 模式；系统标题栏不显示图片控件。

颜色编辑必须同时提供颜色选择器和 HEX 文本框，并双向同步。原生标题栏只接受 `#RRGGBB`；其他 Web 颜色按现有 Schema 接受 `#RRGGBB` 或 `#RRGGBBAA`。不提供任意 CSS、HTML、JavaScript 或远程 URL 输入。

### 3.4 即时预览节流

Task 5 会串行执行所有主题事务。编辑器应在输入层合并快速连续变化：

- 同一时刻最多一个预览请求在执行。
- 执行期间只保留最新草稿；当前预览结束后再提交最后一次变化。
- 保存、取消、切换主题前先结束或吸收待处理预览，不能丢掉最终输入值。
- 每次预览仍通过 store，不能绕开事务队列。

### 3.5 接入设置页

修改 `frontend/src/components/SettingsPage.vue`：

- 在基础设置中新增“外观与主题”导航。
- 使用独立 section 挂载 `GuiThemeSettings`。
- 不移动或破坏现有模型、Token、权限和关于页面。

完成标志：用户可以在设置页复制内置主题、编辑任意 GUI 分区、上传背景图、预览、取消、保存并重新启用主题。

## 4. 剩余任务二：七个 GUI 分区真正使用主题样式

当前运行时已经生成 CSS 变量，但各页面区域还没有全部接入。

修改以下文件：

- `frontend/src/App.vue`
- `frontend/src/components/Topbar.vue`
- `frontend/src/components/Sidebar.vue`
- `frontend/src/components/InputArea.vue`
- `frontend/src/components/RightPanel.vue`
- `frontend/src/style.css`

实施方式：

1. 在现有根节点上添加稳定的 `data-theme-region`，不要增加会改变 flex/grid 尺寸的包装层。
2. 七个 ID 必须与 Schema 完全一致：
   - `app_header`
   - `sidebar`
   - `conversation_toolbar`
   - `workspace`
   - `composer`
   - `right_panel`
   - `statusbar`
3. 每个区域把自己的 `--theme-<region>-*` 变量别名到统一的 `--theme-region-*`。
4. 用区域变量设置文字、背景、边框、强调色、背景图片、图片尺寸、位置和透明度。
5. 背景图使用不可交互的伪元素，保持在内容后方，不能遮挡点击或滚动。
6. 保留旧 `--bg0`、`--bg1` 等全局别名并映射到新语义 token，避免尚未迁移的小组件突然变色。
7. 不根据主题生成动态 class，不读取或插入用户提供的 style 字符串。

完成标志：截图中指出的顶部系统条、应用顶部栏、左栏、中央会话区、输入区、右栏和底部状态栏都能分别改变颜色；除 Windows 原生系统标题栏外，其余七区可以设置声明式背景图片。

## 5. 剩余任务三：文档与安装包说明

更新：

- `docs/all_features.md`
- `assets/CHANGELOG.md`

文档写清：

- 设置入口和创建/复制主题流程。
- 每个分区可修改的字段。
- 图片格式和大小限制。
- 主题 ZIP 导入导出方式。
- 活动主题损坏时的恢复/重置方式。
- GUI 主题与 CLI 主题互相独立。

安装包结论应写为：主题功能本身只增加少量 Python、TypeScript 和 CSS 代码，安装包增量很小；用户上传的图片和导入主题保存在用户数据目录 `gui-themes/`，不写入安装目录，也不进入 PyInstaller 或前端构建产物，因此不会随着安装包发布。用户本地数据占用取决于其图片数量，当前限制为单图 10 MiB、单主题 30 MiB。

## 6. 最终收尾时要处理的已知项

这些不是 Task 6/7 的主体，但正式交付前应决定是否修复：

1. 非 UTF-8 的 `theme.json` 当前可能抛出 `UnicodeDecodeError`，应统一转换为结构化 `ThemeError("invalid_manifest", ...)`。
2. `import_image()` 和 `read_asset()` 当前在 10 MiB 限制检查前调用 `read_bytes()`；应改为先检查文件元数据，并使用有上限的读取方式，防止大文件先进入内存。
3. Windows 10/11 的真实 WebView2 标题栏颜色和 capability 状态仍需在打包应用中人工确认。
4. 活动主题重命名若墓碑清理失败会保留可恢复目录；可后续增加显式清理入口，但不能为了清理而破坏当前安全事务顺序。
5. 主题运行时已有 MIME/Base64 校验，后续若补回归覆盖，应验证 Blob 的实际 MIME 和字节，以及非法 MIME/Base64 不改变当前界面。

## 7. 明天换电脑后的继续顺序

1. 获取仓库并切到 `dev`。
2. 确认 HEAD 至少包含 `b20d59e`。
3. 阅读本文件、原设计和原实施计划的 Task 6–8。
4. 先完成 Store 的主题 CRUD action，再实现两个设置页组件。
5. 接入 SettingsPage。
6. 再做七区域 DOM 标记和 CSS 变量映射。
7. 最后补文档、安装包说明和已知项。
8. 完成后再统一做代码审查与交付验证；本次暂停不包含验证结果。

## 8. 不得改变的边界

- 只修改 GUI 主题，CLI 保持现状。
- 用户主题和图片永远放在用户数据目录，不打进安装包。
- 不加载任意 CSS、JS、HTML、远程 URL、GIF 或视频。
- 未知字段、非法路径、损坏图片和不支持 Schema 必须显式报错。
- 活动主题损坏时不得偷偷切回内置主题，只有用户明确重置才可更改活动 ID。
- 所有主题副作用继续通过现有串行状态机和安全桥接接口。
