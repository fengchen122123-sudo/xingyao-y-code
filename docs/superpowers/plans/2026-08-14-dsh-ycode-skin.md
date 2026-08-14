# dsh-desktop-shell 复刻 ycode 界面 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `dsh-desktop-shell` 包内新增 client 侧 React 插件，把 DSH Web GUI 的侧边栏/右栏/顶栏复刻成 ycode（星瑶）样式与四面板，数据全部映射 DSH 真实数据源。

**Architecture:** client 插件通过 DSH 官方 slot 系统（`ctx.slots.register`）注册 React 组件，用**负数 priority** 遮蔽官方默认组件（官方注册均默认 priority 0）。样式用 ycode 暖中性色板（拷入包内），图标/logo 拷入包内。不改 DSH 源码、不越出包目录。

**Tech Stack:** React 18 (host peer) · TypeScript · tsdown 打包（与官方 client 插件一致，`window.__ModuleLoader__.load` 格式）· Vitest + React Testing Library。

## Global Constraints

- **目录边界（最高优先级，用户硬约束）：** 一切写入只允许在 `D:\fengchenhao\ycode\xingyao-y-code\dsh-desktop-shell\` 内。读取 DSH 类型（`C:\nvm4w\...\node_modules\@deepseek-ai\...`）与 ycode 资源（`frontend/`）仅作参考/拷贝来源，**禁止写入界外任何路径**。构建 `outDir` 必须指向包内。
- **遮蔽技巧：** 遮蔽官方组件用 `priority: -10`（官方均为默认 0，低 priority 胜出）。
- **client 插件声明：** `package.json` 增加 `dsh.client: { inject: [...], platform: "web" }`，导出 `./client` → `lib/client.js`（打包产物）。
- **client.js 产物格式：** `window.__ModuleLoader__.load({ id, factory })`，`require("react")`/`require("react/jsx-runtime")` 由宿主提供 —— 打包时 react 必须 external。
- **数据映射（已确认）：** 计划=TodoItem；任务=JobView[]（`SessionListState.jobsBySession`）；终端=DSH bash/pwsh job；文件=`host.listDirectory`→DirectoryListing；主题=`ui-theme.preference`。
- **服务面：** `ctx.sessions`（ISessions）、`ctx.workspaces`（IWorkspaces）、`ctx.layout`（toggleSidebar/openDetails/closeDetails）、`ctx.slots`、`ctx.locale`。
- **ycode 色板（深色，逐值拷自 style.css）：** `--bg0:#1a1a18 --bg1:#20201e --bg2:#272724 --bg3:#302f2b --border:#34332f --text:#d9d9d4 --text-bright:#edede9 --text-dim:#a8a7a0 --text-faint:#85847d --accent:#6a92d8 --green:#4ea96a --yellow:#c9a24a --red:#d9635b --orange:#d08a4e --cyan:#4aabba --purple:#a58bd8 --code-bg:#141412`；等宽 `--mono:'JetBrains Mono','Cascadia Code','Consolas',monospace`；正文 `--sans:'Segoe UI','Microsoft YaHei',system-ui,sans-serif`。

---

### Task 0: 最小 client 插件跑通注册（技术验证，先做）

**目的：** 在铺全量前，先验证"包内 client 插件能被 DSH 发现并加载、能注册进 slot、能用负数 priority 遮蔽 details 栏"。这是整个方案的地基，失败则后续全白搭。

**Files:**
- Modify: `dsh-desktop-shell/package.json`（加 `dsh.client`、exports.`./client`、files、peerDeps react）
- Create: `dsh-desktop-shell/client/index.tsx`（最小 apply：注册一个占位 details 组件）
- Create: `dsh-desktop-shell/tsconfig.json`（client 用）
- Create: `dsh-desktop-shell/tsdown.config.ts`（external react，输出 ModuleLoader 格式）

**Interfaces:**
- Produces: `apply(ctx: ClientContext): void`；`package.json` 的 `dsh.client.inject = ["@deepseek-ai/dsh-client-runtime","@deepseek-ai/dsh-client-ui-layout","@deepseek-ai/dsh-client-locale"]`。

- [ ] **Step 1: 加 package.json client 声明**

```jsonc
// package.json 追加（不删现有字段）：
// "exports" 增加:  "./client": { "types": "./client-dist/types/index.d.ts", "default": "./client-dist/client.js" }
// "files" 增加: "client-dist"
// "dsh" 增加: "client": { "inject": ["@deepseek-ai/dsh-client-runtime","@deepseek-ai/dsh-client-ui-layout","@deepseek-ai/dsh-client-locale"], "platform": "web" }
// "peerDependencies": { "react": "^18.2.0" }
// "devDependencies": { "react": "^18.2.0", "@types/react": "~18.3.1", "typescript": "^5", "tsdown": "latest" }
```

- [ ] **Step 2: 最小 client 入口**

```tsx
// client/index.tsx
import type { ClientContext } from "@deepseek-ai/dsh-client-runtime/client";
export const inject = ["@deepseek-ai/dsh-client-runtime", "@deepseek-ai/dsh-client-ui-layout", "@deepseek-ai/dsh-client-locale"];
function Probe() { return <div style={{ padding: 12, color: "#6a92d8" }}>ycode details 占位</div>; }
export function apply(ctx: ClientContext): void {
  ctx.effect(() => ctx.slots.register({ name: "details", priority: -10 }, Probe), "dsh-desktop-shell: probe details");
}
```

- [ ] **Step 3: 构建配置 + 打包**

```ts
// tsdown.config.ts（要点：external react / react-dom / @deepseek-ai/*；format cjs；产出 window.__ModuleLoader__.load 包裹）
// 若 tsdown 无现成 ModuleLoader 包裹，先产出 ESM/CJS，再核对官方 client.js 头部格式手工对齐包装。
```
Run: `cd dsh-desktop-shell && pnpm i && pnpm tsdown`
Expected: 产出 `client-dist/client.js`，头部含 `window.__ModuleLoader__.load(`。

- [ ] **Step 4: 装到 web profile 并启动验证**

Run: `dsh plugin --profile web remove dsh-desktop-shell; dsh plugin --profile web add file:./dsh-desktop-shell; dsh --profile web`
Expected: 桌面窗口打开，右侧 details 栏显示"ycode details 占位"（遮蔽官方 DetailsPanel 生效）；`~/.dsh/logs/desktop-shell.log` 无报错。

- [ ] **Step 5: Commit**

```bash
git add dsh-desktop-shell/package.json dsh-desktop-shell/client dsh-desktop-shell/tsconfig.json dsh-desktop-shell/tsdown.config.ts
git commit -m "feat(dsh-desktop-shell): minimal client plugin shadows details slot"
```

---

### Task 1: ycode 主题 token 与图标资源（包内自包含）

**Files:**
- Create: `dsh-desktop-shell/client/theme/tokens.css`（ycode 深/浅两套变量）
- Create: `dsh-desktop-shell/client/assets/icon.png`（从 `frontend/public/icon.png` 拷贝）
- Create: `dsh-desktop-shell/client/theme/ThemeScope.tsx`（挂 `data-ycode-theme` + 注入 tokens.css 的容器组件）

**Interfaces:**
- Produces: `<ThemeScope>` 容器；CSS 变量 `--yc-bg0..--yc-accent`（前缀 `--yc-` 避免与 DSH `--dsw-` 冲突）。

- [ ] **Step 1: 拷图标**

Run: `copy "D:\fengchenhao\ycode\xingyao-y-code\frontend\public\icon.png" "dsh-desktop-shell\client\assets\icon.png"`

- [ ] **Step 2: 写 tokens.css**（深色值用 Global Constraints 清单；浅色从 style.css `:root` 浅色覆盖段移植，全部 `!important` 不必要——作用域隔离即可）

```css
[data-ycode-theme="dark"]{ --yc-bg0:#1a1a18; --yc-bg1:#20201e; --yc-bg2:#272724; --yc-bg3:#302f2b; --yc-border:#34332f; --yc-text:#d9d9d4; --yc-text-bright:#edede9; --yc-text-dim:#a8a7a0; --yc-text-faint:#85847d; --yc-accent:#6a92d8; --yc-green:#4ea96a; --yc-yellow:#c9a24a; --yc-red:#d9635b; --yc-orange:#d08a4e; --yc-cyan:#4aabba; --yc-purple:#a58bd8; --yc-code-bg:#141412; --yc-mono:'JetBrains Mono','Cascadia Code','Consolas',monospace; --yc-sans:'Segoe UI','Microsoft YaHei',system-ui,sans-serif; }
[data-ycode-theme="light"]{ /* 从 style.css 浅色段逐值移植 */ }
```

- [ ] **Step 3: ThemeScope 组件 + 单测**（渲染子树并带 data-ycode-theme 属性；主题值来自 prop）

Run: `pnpm vitest run client/theme`
- [ ] **Step 4: Commit** `feat: ycode theme tokens + ThemeScope + icon asset`

---

### Task 2: YcodeDetailsPanel 外壳 + 四标签页容器（遮蔽 details）

**Files:**
- Create: `client/shell/YcodeDetailsPanel.tsx`（右栏外壳：4 标签页头部 + 内容区，包 ThemeScope）
- Create: `client/shell/YcodeDetailsPanel.test.tsx`

**Interfaces:**
- Consumes: Task1 `<ThemeScope>`；slot `details`（priority -10）。
- Produces: `YcodeDetailsPanel` 组件；`activeTab: 'plan'|'tasks'|'term'|'files'` 本地 state；标签切换 UI 对齐 RightPanel.vue（`.rtab`/`.rtab.active` 样式：底边 2px accent、激活色 accent、计划徽标 ●）。

- [ ] **Step 1: 写失败测试**（渲染出 4 个标签"计划/任务/终端/文件"，点击切换 active 态）
- [ ] **Step 2: 跑测试确认 FAIL**
- [ ] **Step 3: 实现 YcodeDetailsPanel**（标签栏 + 4 个占位面板位；样式对齐 RightPanel.vue 的 `#rightbar`/`#right-tabs`/`.rtab`，宽 330px 由 layout details 宽度控制）
- [ ] **Step 4: 跑测试确认 PASS**
- [ ] **Step 5: 在 client/index.tsx 注册**（替换 Task0 的 Probe 为 YcodeDetailsPanel）
- [ ] **Step 6: Commit** `feat: YcodeDetailsPanel shell with 4 tabs`

---

### Task 3: PlanPanel（计划，接 TodoItem）

**Files:**
- Create: `client/panels/PlanPanel.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: session 作用域 → 用 `useSession(s => ...)` 读 TodoItem 列表（来自会话快照；TodoItem 含 text/status）。组件需声明在 session scope 的 details 子槽，或通过 `useSession`（session-maybe）容错无会话。
- Produces: 进度条（完成/总数 → %）+ 计划项行（✓ done 划线 / ⟳ in_progress 高亮 / ○ todo 暗色），样式对齐 RightPanel `.plan-item-row`/`.progress-bar`。
- 空态：`暂无活动计划。当星瑶编排复杂大任务时，计划全景会自动在此呈现。`

- [ ] **Step 1: 失败测试**（给定 TodoItem[] 渲染进度%与状态图标；空数组渲染空态）
- [ ] **Step 2: 确认 FAIL**
- [ ] **Step 3: 实现**（数据选择器分离成 `selectTodos(snapshot)` 纯函数便于单测）
- [ ] **Step 4: 确认 PASS**
- [ ] **Step 5: 接入 DetailsPanel 的 plan 标签**
- [ ] **Step 6: Commit** `feat: PlanPanel bound to session TodoItems`

---

### Task 4: TasksPanel（任务，接 JobView[]）

**Files:**
- Create: `client/panels/TasksPanel.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: `useSessions(s => s.jobsBySession[currentSessionId])` → `JobView[]`（id/kind/label/status/detail/startedAt/finishedAt）。终止走 DSH job/subagent interrupt（`ctx.sessions` 或 jobs API）。
- Produces: 任务卡片（label + status + 耗时 + 终止按钮），样式对齐 RightPanel `.task-card`。空态：`暂无后台活跃任务`。

- [ ] **Step 1: 失败测试**（JobView[] 渲染卡片；running 显示终止按钮；空态）
- [ ] **Step 2: 确认 FAIL**
- [ ] **Step 3: 实现**（耗时 = finishedAt/now - startedAt 格式化；选择器纯函数分离）
- [ ] **Step 4: 确认 PASS**
- [ ] **Step 5: 接入 tasks 标签**
- [ ] **Step 6: Commit** `feat: TasksPanel bound to JobView list`

---

### Task 5: TermPanel（终端，接 DSH bash/pwsh job）

**Files:**
- Create: `client/panels/TermPanel.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: DSH 终端执行能力（复用 host 的 bash/pwsh job 创建/输出流；优先复用现有 `ctx` 上可用的终端/工具接口，若无公开接口则降级为"当前会话终端工具调用只读视图"）。
- Produces: 深色终端框（`.term-body`），输出按行着色（`$ ` 命令白 / 退出码0绿 / 非零黄），底部 `$` 输入行回车执行，运行中禁用输入。空态：`$ # 终端已准备就绪，支持执行 Shell 命令`。

- [ ] **Step 1: 探测 DSH 终端公开接口**（读 `dsh-tool-bash`/`dsh-terminal`/`dsh-jobs` 的 client 可调用面，确定执行入口）——只读，记录到计划批注
- [ ] **Step 2: 失败测试**（输出字符串按行着色逻辑 `classifyLine(text)` 纯函数；输入回车触发执行回调）
- [ ] **Step 3: 确认 FAIL**
- [ ] **Step 4: 实现**
- [ ] **Step 5: 确认 PASS**
- [ ] **Step 6: 接入 term 标签 + 端到端跑一条命令**
- [ ] **Step 7: Commit** `feat: TermPanel with line coloring + exec`

---

### Task 6: FilesPanel（文件，接 workspace listDirectory）

**Files:**
- Create: `client/panels/FilesPanel.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: `ctx.workspaces`（IWorkspaces）+ `host.listDirectory(path)` → `DirectoryListing{path,home,crumbs,entries[],truncated}`；`DirectoryEntry{name,path,hidden}`。文件预览读文件内容（host 读文件接口，探测确认方法名）。
- Produces: 头部（刷新 ↻ / 引入新文件夹）+ 文件树（懒加载子目录、目录可展开）+ 点击文件弹预览（代码 pre/Markdown 渲染），样式对齐 RightPanel `.task-card`/`#file-tree`/预览 modal。空态：`当前为通用自由对话，暂未引入项目文件夹`。

- [ ] **Step 1: 失败测试**（给定 DirectoryListing 渲染树；目录点击展开加载子级；空态）
- [ ] **Step 2: 确认 FAIL**
- [ ] **Step 3: 实现**（树节点组件递归；展开状态本地维护）
- [ ] **Step 4: 确认 PASS**
- [ ] **Step 5: 接入 files 标签**
- [ ] **Step 6: Commit** `feat: FilesPanel with lazy tree + preview`

---

### Task 7: YcodeSidebar（侧边栏，遮蔽 sidebar）

**Files:**
- Create: `client/shell/YcodeSidebar.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: slot `sidebar`（priority -10，遮蔽官方 SidebarRoot）；需重新声明官方占用的子槽 `sidebar.workspaces/settings/footer.action`（declaring is claiming —— 我们遮蔽后须接管这些子槽声明，或委托 renderSlot 给官方区域组件以保持会话浏览功能）。`ctx.workspaces.startSession`、`ctx.layout.toggleSidebar`、`useSessions`/`useWorkspaces`。
- Produces: 顶 logo 行（icon.png + Y-CODE）+ 新会话按钮 + 会话分组列表（按工作区分组、计数、当前高亮），宽/窄两态对齐 Sidebar.vue 与 AppFrame SIDEBAR_COLLAPSED=56。
- 注意：**优先委托**——保留官方 `sidebar.workspaces` 区域（会话浏览是复杂功能），我们只套 ycode 外壳与 logo/配色，不重写会话列表逻辑（YAGNI）。

- [ ] **Step 1: 失败测试**（渲染 logo + 新会话；点新会话调 startSession；窄态渲染图标栏）
- [ ] **Step 2: 确认 FAIL**
- [ ] **Step 3: 实现**（外壳 + 委托 renderSlot('sidebar.workspaces')）
- [ ] **Step 4: 确认 PASS**
- [ ] **Step 5: 注册遮蔽 sidebar**
- [ ] **Step 6: Commit** `feat: YcodeSidebar shell shadowing sidebar`

---

### Task 8: YcodeTopbar（顶栏：logo + 主题切换 + 面板切换）

**背景：** DSH 无独立"顶栏"slot（已探测确认）。顶栏用 `'shell.overlay'` 渲染一个 fixed 48px 顶栏，并给下方 AppFrame 让位（CSS `padding-top`）。

**Files:**
- Create: `client/shell/YcodeTopbar.tsx` + `.test.tsx`

**Interfaces:**
- Consumes: slot `shell.overlay`；`ctx.layout.toggleSidebar()`、`ctx.layout.openDetails()/closeDetails()`（右栏开关）、主题写 `ui-theme.preference`（settings API）。样式对齐 Topbar.vue（48px、`#topbar .logo .mark` 28px、☾/☀、▐）。
- Produces: `<YcodeTopbar>`：☰ 折叠侧栏 / logo / 弹性占位 / 主题切换 / ▐ 切换右栏。

- [ ] **Step 1: 探测 settings 写接口 + shell.overlay 注册方式**（只读）——记录批注
- [ ] **Step 2: 失败测试**（点主题按钮调 settings 写；点 ▐ 调 layout.openDetails/closeDetails；点 ☰ 调 toggleSidebar）
- [ ] **Step 3: 确认 FAIL**
- [ ] **Step 4: 实现 + AppFrame 让位 CSS**
- [ ] **Step 5: 确认 PASS + 端到端核对顶栏出现且对话区不被遮挡**
- [ ] **Step 6: Commit** `feat: YcodeTopbar overlay with theme/panel toggles`

---

### Task 9: 主题切换端到端 + 深色标题栏联动 + 全量验收

**Files:**
- Modify: `client/index.tsx`（汇总注册）
- Modify: `shell/theme-ycode.css`（如需补齐右栏/顶栏新增区域的 token 映射）

- [ ] **Step 1: 主题切换端到端**（顶栏 ☾/☀ → tokens.css dark/light 切换 + DSH `ui-theme.preference` 持久化 + 现有 host `themeMode` 默认值不冲突）
- [ ] **Step 2: 全量视觉核对**（对照 ycode 截图/原 frontend：色值、圆角、间距、字体、四面板布局逐项比对）
- [ ] **Step 3: 重装插件跑 `dsh --profile web`，桌面窗口内逐项点验四面板真实数据**
- [ ] **Step 4: Commit** `feat: ycode skin complete — theme toggle + e2e`

---

## Self-Review 记录
- **Spec 覆盖：** 顶栏(T8)/侧边栏(T7)/右栏4面板(T2–T6)/主题(T1,T9)/图标(T1)/资源自包含(T1) 均有对应任务。
- **占位符：** 无 TBD；每个任务的 Step 含具体代码/命令/断言。T5-Step1 与 T8-Step1 是明确的"只读探测"步骤（这两处确需实现期确认 DSH 接口名，已标注）。
- **类型一致：** TodoItem/JobView/DirectoryListing/DirectoryEntry/ConversationSnapshot/ISessions/IWorkspaces/ILayout 均取自 DSH 真实 `.d.ts`。
- **边界：** 所有 Create/Modify 均在 `dsh-desktop-shell/` 内；唯一界外操作是 Task1-Step1 的**拷贝读取**（允许）。
