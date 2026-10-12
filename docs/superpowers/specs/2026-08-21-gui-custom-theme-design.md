# GUI 自定义主题系统设计

## 目标

为 Y-CODE GUI 增加声明式自定义主题系统。用户可在设置页创建、复制、编辑、导入、导出和切换主题，并对应用分区分别设置颜色、透明度和背景图片。主题只影响 Vue GUI 与 Windows 原生标题栏，不修改 CLI 的 `/theme`、`core/themes.py`、`config.theme` 或 prompt_toolkit 样式。

第一版采用声明式主题（方案 A），不加载第三方 JavaScript 或任意 CSS。主题作者通过稳定、版本化的语义令牌开发主题，不依赖前端 DOM、组件名或 CSS 类名。

## 设计原则

- GUI 与 CLI 主题配置完全分离：CLI 继续使用 `theme`，GUI 使用 `gui_theme_id`。
- 用户主题存储在 `config.YCODE_DIR/gui-themes/`，升级程序不得把主题资源写入或依赖安装目录。
- 主题包只覆盖系统明确开放的语义令牌；不允许隐藏、移动或重排应用组件。
- 每个用户主题继承一个内置基础主题。未覆盖的新令牌按声明式继承规则取基础主题值，保证向前扩展。
- Schema、资源路径、颜色和数值范围必须严格校验；不支持或损坏的主题不得启用。
- 无效活动主题不得静默切换到其他主题。GUI 显示阻断式错误与明确的“重置为内置主题”操作，由用户决定是否重置。
- 主题预览与持久化分离。预览只存在于前端草稿；保存成功后才更新活动主题和 `config.json`。

## 范围

### 第一版包含

- 内置 `builtin.dark`、`builtin.light` 两个 GUI 主题。
- 设置页新增“外观与主题”导航项及独立编辑组件。
- 可视化选色、HEX 输入、透明度、图片显示方式和实时预览。
- 按分区覆盖背景、文字、边框与强调色。
- 上传、替换、清除分区背景图片。
- 创建、复制、重命名、删除、导入和导出用户主题。
- Windows 原生标题栏颜色、文字颜色、边框颜色和明暗按钮模式。
- 旧 `gui_theme_light` 配置到新主题 ID 的确定性迁移。
- 主题资源与 ZIP 导入的路径、格式、尺寸和总量校验。

### 第一版不包含

- CLI 主题改动或 GUI/CLI 主题同步。
- 任意 CSS、JavaScript、HTML 或组件布局扩展。
- Windows 原生标题栏背景图片。
- 在线主题商店、远程主题地址或自动下载资源。
- 动态视频背景、动画 GIF 或主题脚本。

## 主题模型

### 目录结构

```text
~/.ycode/gui-themes/
  sakura-light/
    theme.json
    assets/
      workspace.webp
      header.png
```

内置主题由代码提供，不复制图片，不显著增加安装包体积。用户选择的图片由后端复制到对应主题的 `assets/`，主题不保存工作区或用户临时文件的绝对路径。

### Schema v1

```json
{
  "schema_version": 1,
  "id": "sakura-light",
  "name": "樱花浅色",
  "appearance": "light",
  "extends": "builtin.light",
  "tokens": {
    "text_primary": "#2c2529",
    "text_secondary": "#766970",
    "accent": "#d96b9f",
    "border": "#e5ccd8",
    "card": "#fff9fc",
    "code_background": "#241f22"
  },
  "native_titlebar": {
    "background": "#fff7fb",
    "text": "#2c2529",
    "border": "#e5ccd8",
    "mode": "light"
  },
  "regions": {
    "app_header": {
      "background": "#fff7fb",
      "text": "#2c2529",
      "border": "#e5ccd8",
      "background_opacity": 1.0,
      "image": "assets/header.png",
      "image_opacity": 0.15,
      "image_size": "cover",
      "image_position": "center"
    },
    "workspace": {
      "background": "#faf6f8",
      "image": "assets/workspace.webp",
      "image_opacity": 0.2,
      "image_size": "cover",
      "image_position": "center"
    }
  }
}
```

### 稳定语义令牌

全局令牌至少包括：

- `background_app`、`background_panel`、`background_card`
- `text_primary`、`text_secondary`、`text_faint`
- `accent`、`accent_soft`
- `border`
- `success`、`warning`、`danger`、`info`
- `code_background`
- `font_sans`、`font_mono`
- `radius_small`、`radius_medium`、`radius_large`
- `shadow_color`、`shadow_strength`

可独立覆盖的分区包括：

- `app_header`：头像和 Y-CODE 所在的 Vue 顶栏。
- `sidebar`：左侧导航和会话列表。
- `conversation_toolbar`：中间会话顶部工具栏。
- `workspace`：中间消息与空白工作区。
- `composer`：输入框及其控制区。
- `right_panel`：计划、任务、终端、文件面板。
- `statusbar`：底部状态栏。

每个分区允许覆盖 `background`、`text`、`border`、`accent`、`background_opacity`、`image`、`image_opacity`、`image_size` 和 `image_position`。两种透明度只作用于独立的背景层，不降低文字和交互控件的不透明度。缺失字段严格从主题的 `extends` 基础主题继承。

Windows 原生标题栏不是前端分区，只允许设置 `background`、`text`、`border` 和 `mode`。标准 DWM 标题栏不支持图片；第一版不改成无边框自绘窗口。

## 校验与兼容性

- `schema_version` 只接受明确支持的整数版本。版本不支持时返回结构化错误并拒绝加载。
- `id` 只接受小写字母、数字和连字符，且必须与主题目录名一致。
- 用户主题只允许继承 `builtin.dark` 或 `builtin.light`，不允许用户主题链式继承，避免循环和删除依赖。
- 颜色使用 `#RRGGBB` 或 `#RRGGBBAA`；数值必须在 Schema 规定范围内。
- 未知令牌、未知分区或未知字段直接报错，不自动丢弃。
- 资源路径必须是主题目录内的规范化相对路径，不允许绝对路径、`..`、符号链接或目录逃逸。
- 图片只接受经过实际解码验证的 PNG、JPEG、WebP；扩展名与内容必须一致。
- 单张图片最大 10 MiB、最大 8192×8192；单主题资源总量最大 30 MiB。
- ZIP 导入限制文件数量、解压后总大小和压缩比，先解压到临时目录并完整校验，再原子移动到主题目录。
- 保存 `theme.json` 使用临时文件与原子替换；不得留下半写入主题。
- Schema v1 发布后只新增可继承令牌，不删除或改变既有令牌语义。需要破坏性变更时发布新 Schema 和确定性迁移器。

## 后端组件

新增 `core/gui_theme.py`，集中负责：

- 内置主题定义与 Schema v1 校验。
- 列出、读取、合并和保存用户主题。
- 创建、复制、重命名、删除主题。
- 图片导入、严格校验与主题资源读取。
- ZIP 主题导入与导出。
- 旧 `gui_theme_light` 到 `gui_theme_id` 的迁移。
- 解析启动所需的背景色和标题栏颜色。

`gui_bridge.ApiBridge` 增加窄接口：

- `list_gui_themes()`
- `get_gui_theme(theme_id)`
- `save_gui_theme(theme)`
- `set_active_gui_theme(theme_id)`
- `create_gui_theme(...)`、`delete_gui_theme(theme_id)`
- `choose_gui_theme_image(theme_id, region)`、`remove_gui_theme_image(...)`
- `import_gui_theme()`、`export_gui_theme(theme_id)`
- `get_gui_theme_asset(theme_id, asset_path)`，返回 MIME 与 Base64 资源载荷
- `preview_native_titlebar(native_titlebar)`、`restore_native_titlebar()`

桥接层只做参数转换、原生对话框和 DWM 调用，主题规则与文件操作全部委托给 `core/gui_theme.py`。现有 `set_gui_theme(bool)` 在迁移期保留为内置深浅主题切换兼容入口，不承载新主题编辑逻辑。

## 前端组件

新增：

- `frontend/src/components/GuiThemeSettings.vue`：主题列表、分区选择、字段编辑、图片操作和保存/取消。
- `frontend/src/themes/types.ts`：前后端主题契约类型。
- `frontend/src/themes/runtime.ts`：主题合并结果到 CSS 变量、分区样式和资源 Blob URL 的纯转换与生命周期管理。

修改：

- `SettingsPage.vue`：增加“外观与主题”导航，挂载独立组件，避免继续扩大现有模型设置逻辑。
- `appStore.ts`：保存活动主题、已持久化快照和预览草稿；提供预览、保存、取消与启动恢复动作。
- `style.css`：把现有 `:root` 和 `body.light` 值映射到语义 CSS 变量；组件仍只消费变量。
- `main.ts`、`gui_main.py`：启动时传递已校验的 appearance 和首屏背景色，避免加载完整主题前反色闪烁。
- `pywebview.d.ts`：补齐主题桥接契约。

前端不得把用户内容拼成 `<style>` 或 HTML。运行时只通过 `style.setProperty` 写入预定义 CSS 变量，并为经过后端验证的图片创建 Blob URL；主题切换时撤销旧 Blob URL。

## 设置页交互

“外观与主题”页面分为主题列表、编辑区域选择和属性编辑器：

- 用户选择内置主题时只能预览或“复制为自定义主题”，不能覆盖内置主题。
- 用户主题支持重命名、另存、删除、导入和导出。
- “编辑区域”包含全局、应用顶栏、左侧栏、会话工具栏、工作区、输入区、右侧面板、状态栏和 Windows 标题栏。
- 颜色字段同时提供原生颜色选择器和 HEX 输入框，二者保持同步。
- 每个分区提供“继承全局/单独设置”，清除覆盖后恢复继承。
- 图片提供选择、清除、透明度、填充方式和位置设置。
- 输入变化立即应用到 GUI 和原生标题栏，但只修改前端草稿。
- “取消”恢复进入编辑前的完整活动主题；“保存”只有在后端完整校验和原子写入成功后才提交。
- 切换主题或退出设置页时如果存在未保存草稿，必须明确询问保存、放弃或继续编辑。

顶部现有深浅主题按钮继续作为内置 `builtin.dark`/`builtin.light` 的快捷切换。选择自定义主题后，点击该按钮会明确切换到对应内置主题并持久化，不隐式修改用户主题。

## 启动与数据流

1. `gui_main.py` 通过 `core.gui_theme` 读取 `gui_theme_id`。旧配置没有该键时，根据 `gui_theme_light` 确定性迁移为内置主题。
2. 后端严格校验并合并活动主题，得到完整主题快照和首屏元数据。
3. `gui_main.py` 用主题背景创建原生窗口，并通过 URL fragment 传递 appearance 与首屏背景色。
4. `main.ts` 在 Vue 挂载前应用首屏背景；`get_initial_state()` 返回完整主题快照。
5. 前端加载主题资源，创建 Blob URL，调用 `runtime.ts` 应用所有预定义变量和分区背景。
6. 用户在设置页编辑时，草稿重复执行同一运行时路径；保存成功后更新 `gui_theme_id` 和活动快照。

活动主题不存在、损坏或版本不支持时，后端返回明确错误。前端显示阻断式主题错误页和“重置为 builtin.dark / builtin.light”操作，不自动替换活动主题。

## 安装包与运行资源

- 内置主题仅包含 JSON 等价常量和现有 CSS 默认值，不内置背景图片。
- 用户背景存储在 `YCODE_DIR/gui-themes`，不进入 PyInstaller `datas` 或发布 ZIP。
- 新增代码和主题 Schema 对当前约 290 MB 安装包的影响可忽略；实际用户主题占用计入用户数据目录，而不是安装包。
- 图片加载发生在主题启动或切换时；Blob URL 必须复用并在切换时释放，避免长期占用内存。

## 错误处理

- 所有主题错误返回稳定错误码、字段路径和用户可读消息，例如 `unsupported_schema`、`invalid_color`、`asset_escape`、`asset_too_large`。
- 保存、导入和切换失败时保持原活动主题与配置不变，并显示错误；不写入部分结果。
- 主题定义保存和主题激活是两个明确操作。DWM 调用失败不回滚已保存的主题定义，但激活接口必须报告“原生标题栏应用失败”，不得把本次完整应用标记为成功，也不得更新活动主题 ID。
- 图片读取失败时主题预览失败并指出具体资源，不自动移除图片字段。
- 删除当前活动主题前必须先显式选择另一个主题。

## 测试

### Python

- Schema v1 合法/非法字段、颜色、范围、继承和未知键测试。
- 路径逃逸、符号链接、伪装图片、超限图片和 ZIP 炸弹测试。
- 保存与导入原子性测试；失败后原主题与 `config.json` 字节不变。
- `gui_theme_light` 到内置主题 ID 的迁移测试。
- 活动主题损坏时返回阻断错误、不自动改写配置的测试。
- DWM COLORREF 转换与桥接参数测试。
- 断言 CLI 的 `theme` 配置及 `core.themes.THEMES` 不受影响。

### 前端

- 主题运行时的令牌映射、分区继承和 Blob URL 释放测试。
- 颜色选择器与 HEX 输入同步、非法输入阻止保存测试。
- 实时预览、取消恢复、保存提交和未保存草稿确认测试。
- 内置主题只读、复制后可编辑、删除活动主题受阻测试。
- 设置页导航和启动主题恢复测试。
- 顶部深浅按钮切换内置主题的兼容测试。

### 集成与视觉验证

- 深色、浅色和含多分区背景图的自定义主题分别启动，验证无明显反色闪烁。
- 检查顶栏、侧栏、工作区、输入区、右面板、状态栏和原生标题栏的实际效果。
- 在 100%、125%、150% DPI 下验证原生标题栏与页面分区。
- 完整运行前端单元测试、Python 主题测试、现有 GUI 主题回归和前端构建。

## 交付边界

- 不修改 Provider 请求、Prompt Ledger、Harness、缓存前缀或消息数据库。
- 不修改 CLI 主题模块和命令行为。
- 不为主题引入远程网络访问。
- 不做与主题无关的设置页重构。
