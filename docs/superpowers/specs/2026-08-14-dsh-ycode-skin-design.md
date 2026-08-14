# dsh-desktop-shell 复刻 ycode 界面 — 设计文档

> 日期：2026-08-14
> 状态：待用户确认

## 0. 硬约束：目录边界（用户指定）

**全部代码与资源只能写入 `D:\fengchenhao\ycode\xingyao-y-code\dsh-desktop-shell\`，不得越界。**
插件必须自包含——用户后续会把这一整个目录单独拿走使用。具体要求：
- client 代码、组件、样式、构建产物（dist）全部落在 `dsh-desktop-shell/` 内。
- 图标资源（icon.png 等）**一次性拷入** `client/assets/`，插件运行时只引用包内文件，
  不依赖 ycode 主仓（`frontend/public` 仅作为拷贝来源，读完即脱钩）。
- 禁止写入 DSH 安装目录、ycode 主仓、全局 node_modules 等任何界外路径
  （读取 DSH 类型定义做参考允许；写入一律禁止）。
- 构建工具链（tsconfig、esbuild 配置）也放在包内，`outDir` 指向包内 dist。

## 1. 目标

把 `dsh-desktop-shell` 插件从"开原生窗 + CSS 套壳"升级为"**用 React 全量复刻 ycode 界面**"：
启动 `dsh --profile web` 后弹出的 WebView2 桌面窗口，看到的不再是 DeepSeek Harness 原版界面，
而是 ycode（星瑶）的样子——三栏布局、右侧"计划/任务/终端/文件"面板、ycode 顶栏与侧边栏、
ycode 配色与图标/logo、深浅主题切换。

## 2. 背景与已确认的机制（事实依据）

### 2.1 DSH 界面是 slot 组合系统（官方支持插件替换界面）
- `dsh-client-ui-slots` 的 `SlotCore.register({ name, priority, ... }, component)`：把 React 组件注册进插槽。
  - **遮蔽（shadowing）**：同一 cell 多个注册按 `priority` 升序，**最低 priority 胜出**渲染；同 cell 同 priority 会抛错。
    → 我们用比官方更低的 priority 遮蔽官方默认组件。
  - `kind`: single / list / keyed / chain；`scope`: root / session-maybe / session。
  - `declare module '@deepseek-ai/dsh-client-ui-slots'` 合并 `SlotMap` 获得类型安全。
- `AppFrame`（`dsh-client-ui-layout`）是三栏框架，暴露插槽：
  `'sidebar' | 'conversation' | 'details' | 'shell.overlay'`。
  - 三栏宽度由 `computeColumns` 求解；`details`（右栏）默认 360px，范围 300–520px，0=关闭。
  - `ctx.layout`（`LayoutController`）：`toggleSidebar() / openDetails() / closeDetails()`。
- client 插件入口：`apply(ctx: ClientContext)`；`ctx.slots` / `ctx.sessions` / `ctx.workspaces` / `ctx.layout` 可用。
- 组件 props 由框架按"四份组合"注入：`PropsRuntime`（owner）+ `PropsRenderSlots`（renderSlot）+
  `PropsStore`（store）+ `InjectFace`（inject 工厂返回的业务面，hooks 源变成 `use<Name>` selector hook）。
- 标准套件 hook：
  - 全局：`useSessions`（会话列表）、`useWorkspaces`（工作区列表）
  - session 作用域：`useSession`（`ConversationSnapshot`）、`sessionId`、`useProjection`
- React 绑定（`dsh-client-web-react`）：`bindSnapshotSelector(source)` → selector hook；`useInvoke(asyncFn)`。

### 2.2 数据源映射（用户已确认：映射 DSH 已有数据）
| ycode 面板 | DSH 数据源 | 类型 | 说明 |
|---|---|---|---|
| 计划 Plan | 会话快照的 TodoItem | `TodoItem`（`dsh-session/types`） | DSH todo 工具维护；含 text/status |
| 任务 Tasks | 后台任务 | `JobView[]`（`SessionListState.jobsBySession`） | id/kind/label/status/detail/startedAt/finishedAt |
| 终端 Term | DSH 终端工具 | bash/pwsh job（kind='bash'/'pwsh'/'pty-send'） | 复用 DSH 终端执行能力 |
| 文件 Files | 工作区目录树 | `ctx.workspaces` + `host.listDirectory` → `DirectoryListing`/`DirectoryEntry` | name/path/hidden；crumbs 面包屑 |
| 主题切换 | `ui-theme.preference` | dark/light/system | 已有 settings API；插件已默认写 dark |

### 2.3 ycode 视觉资源（用户已确认：复用 ycode 图标资源）
- 色板：`frontend/src/style.css` 的 CSS 变量（暖中性黑：`--bg0:#1a1a18`、`--accent:#6a92d8` 等；含浅色一套）。
  → **拷贝进** `client/theme/tokens.css`，插件自包含。
- 图标/logo：`frontend/public/icon.png`、`icons.svg`、`favicon.svg`。
  → **一次性拷入** `client/assets/`，运行时只引用包内文件（见 §0 边界约束）。
- 顶栏：`Topbar.vue`（logo "Y-CODE" + 主题切换 ☾/☀ + 面板切换 ▐）→ 用 React 重实现。
- 侧边栏：`Sidebar.vue`（新会话/技能/MCP/插件 菜单 + 项目会话分组列表）→ 用 React 重实现。
- 右面板：`RightPanel.vue`（4 标签页：计划/任务/终端/文件）→ 用 React 重实现。
- 已有套壳：`shell/theme-ycode.css`（DSH `--dsw-*` token → ycode 色板，`!important`），继续用于全局底色。

## 3. 架构

在现有 `dsh-desktop-shell` 包内**新增 client 侧模块**，不动 DSH 源码、不动现有 host 逻辑、不越出包目录（§0）。

### 3.0 client 插件的打包与加载（已验证的机制）
DSH client 插件是**打包成单个 `lib/client.js` bundle** 被浏览器装载的：
- `package.json` 增加 `exports["./client"]` → 指向打包产物 `lib/client.js`；
  增加 `dsh.client = { inject: [...依赖包名...], platform: "web" }`。
- 加载模型是**懒加载 CJS 工厂**：bundle 执行只注册工厂（`window.__ModuleLoader__.load({id, factory})`），
  模块副作用（含 CSS 注入）在首次 import 时才跑；host 在 `/plugins/<id>/client.js?rev=...` 提供 bundle。
- 构建工具用 **tsdown**（DSH 官方 client 插件同款，`"bundle": "tsdown"`），把 TSX 打成单文件 ESM/CJS。
  React 用 peerDependency（`react@^18.2.0`，DSH 已 vendor，不打进 bundle）。
- `dsh.client.inject` 声明我们依赖的服务包：
  `@deepseek-ai/dsh-client-runtime`（slots/sessions/workspaces）、
  `@deepseek-ai/dsh-client-ui-layout`（AppFrame/details/sidebar/layout）、
  `@deepseek-ai/dsh-client-locale`（可选，i18n）。
- 现有 host 侧 `lib/index.js`（`main`/`exports["."]`）保留不动——它管开窗和主题 tap；
  `./client` 是浏览器侧入口，两者并存。

```
dsh-desktop-shell/
├─ package.json              # 修改：补 client 入口/依赖声明
├─ cordis.patch.yml          # 修改：挂 client 插件
├─ lib/index.js              # 现有 host 逻辑：不动（开窗 + 主题 tap + themeMode）
├─ shell/                    # 现有：不动
└─ client/                   # 新增（本设计的全部工作）
   ├─ index.ts               # apply(ctx)：注册所有 slot 组件、声明 SlotMap/LocaleNamespaceMap 合并
   ├─ shell/
   │  ├─ YcodeTopbar.tsx     # 顶栏：logo + 主题切换 + 面板切换（遮蔽/包裹官方）
   │  ├─ YcodeSidebar.tsx    # 侧边栏：菜单 + 会话分组（遮蔽 'sidebar'）
   │  └─ YcodeDetailsPanel.tsx # 右栏外壳：4 标签页容器（遮蔽 'details'）
   ├─ panels/
   │  ├─ PlanPanel.tsx       # 计划
   │  ├─ TasksPanel.tsx      # 任务
   │  ├─ TermPanel.tsx       # 终端
   │  └─ FilesPanel.tsx      # 文件
   ├─ theme/
   │  └─ tokens.css          # ycode CSS 变量（深/浅两套，从 style.css 移植，作用于我们的组件树）
   ├─ icons.tsx              # 内联 SVG 图标 + icon.png 引用
   └─ assets/icon.png        # 从 frontend/public 拷贝
```

### 数据流
- 各面板通过 slot `inject` 工厂拿到 DSH 数据源（`ctx.sessions`/`ctx.workspaces`/会话快照），
  框架把 hooks 源绑定成 `use<Name>` selector hook 注入组件 props。
- 组件内 `useSession(s => selector)` / `useSessions(s => ...)` / `useWorkspaces(s => ...)` 订阅快照，
  数据变化自动重渲染（uSES 模式）。
- 写操作（新会话、开关面板、切主题、执行终端命令、选目录）走 `ctx.sessions` / `ctx.layout` /
  settings API / DSH 终端接口。

### 主题
- `tokens.css` 定义 ycode 深/浅两套变量，挂在我们组件树的根节点（`data-ycode-theme` 容器）。
- 主题切换按钮写 `ui-theme.preference`（dark/light），与现有 host 侧 `themeMode: dark` 默认值一致；
  深色时 `webview.ps1` 已做 DWM 深色标题栏。

## 4. 组件设计

### 4.1 YcodeTopbar（顶栏）
- 内容：折叠侧栏 ☰、logo（icon.png + "Y-CODE"）、弹性占位、主题切换 ☾/☀、切换右栏 ▐。
- 行为：`ctx.layout.toggleSidebar()` / `toggleRightPanel`(openDetails/closeDetails) / 写 `ui-theme.preference`。
- 接入方式：DSH 顶栏若无独立 slot，则在 `'shell.overlay'` 渲染一个 fixed 顶栏并用 CSS 给下方内容让出 48px；
  若有 header 类 slot 则直接遮蔽。**实现时先探测实际 slot 名再定。**

### 4.2 YcodeSidebar（侧边栏，遮蔽 `'sidebar'`）
- 菜单：新会话（`ctx.sessions` 创建/复用空会话）、技能、MCP、插件（这些若无对应 DSH 数据则先占位/隐藏）。
- 项目会话分组：`useSessions` 拿会话列表，按工作区分组渲染（对齐 ycode 的分组/计数/当前高亮）。
- 折叠态：读 layout store 的 sidebar 宽度，宽=完整列表，窄=56px 图标栏。

### 4.3 YcodeDetailsPanel（右栏，遮蔽 `'details'`）
- 顶部 4 标签页：计划 / 任务 / 终端 / 文件（`activeTab` 本地 state）。
- 计划徽标：有计划项时显示 ●。

### 4.4 PlanPanel（计划）
- 进度条（完成项/总数 → %）+ 计划项列表（✓/⟳/○ 状态图标，对齐 ycode 配色）。
- 数据：`useSession` 读会话快照的 TodoItem；无会话/无计划时显示空态文案。

### 4.5 TasksPanel（任务）
- 后台任务卡片列表：label + status + 耗时 + 终止按钮。
- 数据：`useSessions(s => s.jobsBySession[currentSessionId])` → `JobView[]`；
  终止走 DSH job 中断接口（subagent/job interrupt）。

### 4.6 TermPanel（终端）
- 深色终端框：输出区（按行着色：命令白/成功绿/失败黄）+ 底部 `$` 输入行，回车执行。
- 数据/执行：复用 DSH 终端工具（bash/pwsh job）；输出从对应 job 的流读取。

### 4.7 FilesPanel（文件）
- 头部：刷新 ↻ + 引入新文件夹；文件树（懒加载子目录）；点击文件预览（代码高亮/Markdown 渲染）。
- 数据：`ctx.workspaces` + `host.listDirectory(path)` 逐级展开；预览读文件内容。

## 5. 错误处理
- 无会话：计划/终端显示空态；任务显示"暂无后台任务"；文件显示"未引入项目文件夹"。
- 数据源缺失（某服务不可用）：对应面板降级为空态 + 日志，不崩整棵树（slot 有 entry 错误边界）。
- 终端命令失败：输出区按 ycode 规则着色为黄色 `[启动失败…]`/`[进程已退出，代码 N]`。

## 6. 测试
- client 组件：Vitest + React Testing Library，mock `ctx.slots`/`useSession`/`useSessions`/`useWorkspaces`。
- 关键用例：遮蔽优先级生效、4 个面板各自渲染真实 DSH 数据结构、主题切换写 settings、
  空态文案、终端命令执行与输出着色。
- 端到端：`dsh plugin --profile web add` 后启动，肉眼核对窗口内是 ycode 样子。

## 7. 范围与非目标
- **做**：顶栏、侧边栏、右栏 4 面板、主题切换、图标/logo、ycode 配色，全部映射 DSH 真实数据。
- **不做**：不改 DSH 源码；不模拟 ycode 独有而 DSH 无对应的数据（技能/MCP/插件菜单若 DSH 无数据则占位）；
  不做 ycode 的设置页/二级抽屉（超出本次范围，可后续迭代）。

## 8. 风险与缓解
| 风险 | 缓解 |
|---|---|
| 顶栏无独立 slot 可遮蔽 | 用 `'shell.overlay'` fixed 顶栏 + CSS 让位；实现时先探测 slot 名 |
| client 插件打包/加载方式未验证 | 先做一个最小 client 插件跑通注册流程，再铺全量 |
| DSH 升级改 slot 名/数据结构 | 集中在 `client/index.ts` 的注册点，升级时只需对一处 |
| React 版本/JSX 编译 | 复用 DSH 已 vendor 的 React 18；构建用 esbuild/tsc 产出 ESM |
