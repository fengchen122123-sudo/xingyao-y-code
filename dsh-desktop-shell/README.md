# dsh-desktop-shell

DeepSeek Harness 桌面壳插件：`dsh --profile web` 启动后不再提示你去浏览器打开
`http://127.0.0.1:3080`，而是自动弹出一个**原生 WebView2 窗口**，内容就是同一个
Web GUI（同一份前端、同一个 `/api`），关窗即退出 dsh。页面默认套上 **ycode 主题**
（ZCode 暖中性黑深色 / 暖白浅色），让桌面版看起来就是你现在的 ycode。

**新增（v0.2）：client 侧 React 插件**，通过 DSH 官方 slot 系统把界面复刻成 ycode 布局：
- **侧边栏**：ycode logo + 新会话 + 技能/MCP/插件菜单 + 会话分组（委托 DSH 原生列表）+ 底部设置
- **右栏**：计划 / 任务 / 终端 / 文件 四标签页（数据映射 DSH 真实数据源）
- **顶栏**：fixed 顶栏（logo + 主题切换 ☾/☀ + 侧栏/右栏开关），通过 `shell.overlay` slot 渲染
- 全部用 `priority: -10` 遮蔽官方默认组件（官方均为 priority 0），不改 DSH 源码，升级不冲突

## 原理

### host 侧（`lib/index.js`，现有）

- 这是一个 Cordis **bundle 插件**：`package.json` 里声明 `dsh.bundle.patch`
  指向 `cordis.patch.yml`，补丁向组合树插入一行 `desktop-shell`。
- 插件本体（`lib/index.js`）**零外部依赖**（只 import node 内置模块），
  inject 不依赖任何服务：等 Loader 树 settle 后读 `webServer` 服务的端口，
  spawn `powershell.exe -STA` 运行 `shell/webview.ps1`。
- `webview.ps1` 用 WPF + WebView2（托管 DLL 与 `WebView2Loader.dll` 已 vendor 在
  `shell/lib/`，版本 1.0.2903.40，net462 口味，跑在 .NET Framework 4.8）创建原生窗口，
  窗口关闭 → 脚本结束 → 插件收到子进程退出 → `process.emit("SIGTERM")` 走 dsh 自带的
  有界优雅关闭（根 fiber dispose + 5s 兜底退出，退出码 0）。标题栏用 DWM
  immersive dark mode 跟随深色主题。
- **主题**：插件注册 webserver 的 `tapIndex` 钩子，把 `shell/theme-ycode.css`
  （DSH `--dsw-*` 设计 token → ycode 色板的映射，深/浅两套，全部 `!important`）
  内联进每个 index 响应；同时（首次启动、用户未设过时）通过宿主 settings API
  把 `ui-theme.preference` 默认写为 `dark`，与 ycode 默认深色一致。浏览器访问
  同样生效。
- 需要的本机环境：Windows 10/11、Windows PowerShell 5.1、WebView2 Runtime
  （随系统更新自带；本机已装 151.x）。

### client 侧（`client/`，新增）

- `package.json` 的 `dsh.client` 字段声明 client 插件（`inject` + `platform: "web"`），
  入口 `./client` → `client-dist/client.js`（tsdown 打包产物）。
- 产物格式：`window.__ModuleLoader__.load({ id, factory })` 包裹，react / @deepseek-ai/*
  全部 external（由宿主模块加载器提供）。
- 三个 slot 遮蔽注册（`priority: -10` 胜出官方 priority 0）：
  - `details` → `YcodeDetailsPanel`（右栏 4 标签页：计划/任务/终端/文件）
  - `sidebar` → `YcodeSidebar`（侧边栏外壳，委托官方 `sidebar.workspaces` 子槽渲染会话列表）
  - `shell.overlay` → `YcodeTopbar`（顶栏：logo + 主题切换 + 面板开关）
- **主题切换**：通过 `ctx.get("theme")` 拿到 `dsh-client-ui-theme` 的 `ThemeRuntime`，
  调 `setTheme("dark"|"light")` 写 settings scope 并触发全局主题切换。
- **数据映射**（全部接 DSH 真实数据源，无模拟）：
  - 计划 = 会话快照 `TodoItem[]`（`useProjection("todos")`）
  - 任务 = `SessionListState.jobsBySession[current]` → `JobView[]`
  - 终端 = 当前会话的 bash/pwsh/pty-send job（只读展示，DSH 无 client 直接执行接口）
  - 文件 = `ctx.workspaces.listDirectory(path)` → `DirectoryListing`（目录浏览器）
- **图标**：ycode `icon.png` 缩到 64x64 后 base64 内联（`client/assets/icon.ts`），
  运行时零外部资源依赖。

## 安装（装进 web profile）

在插件目录的父目录执行（`file:` 相对路径会被 `dsh plugin` 锚定到当前目录）：

```bash
dsh plugin --profile web add file:./dsh-desktop-shell
```

安装后 `dsh.profile.bundles` 会自动并入 `dsh-desktop-shell` 这一层
（`dsh plugin` 的 reconcile 逻辑），下次 `dsh web` / `dsh --profile web`
启动即弹桌面窗口。

> 注意：pnpm 的 `file:` 安装是**拷贝**（`link:` 在 Windows 上创建 junction 会失败，
> 已实测不可用）。因此**每次修改插件源码后，需要重装一次才会生效**：
>
> ```bash
> dsh plugin --profile web remove dsh-desktop-shell
> dsh plugin --profile web add file:./dsh-desktop-shell
> ```
>
> 插件运行日志在 `$DSH_HOME/logs/desktop-shell.log`（即 `C:\Users\<你>\.dsh\logs\desktop-shell.log`），
> 排查问题先看它。

## 配置（在 `~/.dsh/profiles/web/cordis.patch.yml` 里覆盖）

```yaml
- id: desktop-shell
  config:
    autoOpen: false      # 启动时不弹窗（仍可用浏览器访问）
    title: My Harness
    width: 1600
    height: 1000
    closeAction: keep    # 关窗后 dsh 继续在后台服务；shutdown 则一起退出
    theme: ycode         # ycode 主题（默认）；none 用 DSH 原版主题
    themeMode: dark      # 首次启动写入 ui-theme.preference 的默认值
                         # （dark/light/system；keep = 完全不碰该设置）
```

主题相关说明：

- 深色/浅色各有一套 ycode 映射（深色 = ZCode 暖中性黑，浅色 = ZCode 暖白），
  在 DSH 界面外观设置里切 Light/Dark/System 即可实时切换，两套都生效。
- `themeMode: dark` 只在用户从未设过偏好时写入（settings.yaml 的
  `ui-theme.preference`），你在界面里改过之后不会再被覆盖。
- 想整体改色板，直接编辑 `shell/theme-ycode.css` 里的 token 映射
  （改完记得重装一次插件）。

## 卸载

```bash
dsh plugin --profile web remove dsh-desktop-shell
```

## 常见问题

- **弹窗后窗口白屏**：查 `~/.ycode/logs/gui-runtime.log` 不适用 —— 看启动 dsh 的终端输出
  （插件日志带 `desktop-shell:` 前缀），以及 powershell 的 stderr（init 失败会写
  `dsh-desktop-shell: WebView2 init failed: ...`）。
- **关窗不退出**：确认 profile 补丁里 `closeAction` 不是 `keep`。
- **不想让它出现在其他 profile**：本插件只在存在 `webServer` 服务时才动作，其它
  profile 装上也无害（静默不做任何事），但一般只装 web profile。
