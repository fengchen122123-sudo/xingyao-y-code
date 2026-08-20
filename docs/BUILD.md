# Y-code Windows 打包构建指南

本文档说明如何在 Windows 上从源码构建 Y-code 的官方安装包：一个默认安装图形界面版
(GUI) 的主安装包，用户可以在安装过程中选择是否附带安装命令行版 (CLI)；如果当时选择
不装，安装目录里会留一份独立的 CLI 补装程序，以后随时双击即可补装。

## 一、前置条件

1. Windows 10/11。
2. 项目自带的 Python 虚拟环境 `venv\`，且已安装 `pyinstaller`：
   ```bash
   venv\Scripts\pip install pyinstaller
   ```
   （项目运行时依赖见 `requirements.txt`，同样需要提前 `pip install -r requirements.txt`。）
3. Node.js 20 及以上版本（用于构建 `frontend/` 下的 Vue 3 前端，GUI 版加载的就是它的
   编译产物）。安装后确认 `npm -v` 在命令行可用。
4. 前端依赖已安装：首次构建（或 `frontend/node_modules` 不存在时）需先执行
   ```bash
   cd frontend
   npm install
   cd ..
   ```

### 零基础完整流程（从全新 clone 到拿到安装包）

给第一次接手本项目的人（或 AI 助手）的完整命令序列，在项目根目录依次执行：

```bash
# 1. 创建虚拟环境并安装 Python 依赖（已有 venv 可跳过）
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pip install pyinstaller

# 2. 安装前端依赖（已有 frontend\node_modules 可跳过）
cd frontend && npm install && cd ..

# 3. 一条命令构建全部产物
venv\Scripts\python build_release.py

# 4. 确认产物：最终交付用户的是 dist\ycode_setup_v{版本}.exe
dir dist
```

打包所需的一切文件都在版本库内，不依赖任何库外文件：
`build_release.py`（构建入口）、`ycode.spec`（GUI+CLI 双主程序）、
`ycode_uninstall.spec` / `ycode_uninstall.py`（卸载程序）、
`ycode_setup.spec` / `ycode_setup.py`（tkinter 图形化安装向导，同一份源码按内嵌
marker 区分 full/cli 两种模式）、`packaging/runtime_hook_nullstdout.py`
（windowed 模式下 stdout 兜底的运行时钩子）、`assets/`（程序与向导图标）、
`config.example.json` 与
`system_prompt.md`（安装时播种到 `~/.ycode` 的默认配置）、`frontend/`
（Vue 3 前端源码）。版本号唯一来源是 `core/version.py`，安装包版本号
与文件名中的版本号都自动从它读取，不要在别处硬编码。

## 二、一条命令完成构建

在项目根目录下执行：

```bash
venv\Scripts\python build_release.py
```

该脚本会依次完成：前端构建 → 核心双主程序编译 → 载荷压缩 → 卸载程序编译 → CLI 独立安装向导
编译 → 主安装向导编译 → 安装包重命名为带版本号的分发名（步骤 4.5）→ 产物校验，并打印
每一步的中文进度（✅ 成功 / ❌ 失败）。

### 安装包文件名带版本号（步骤 4.5）

构建期产物用固定名（`ycode_setup.exe` / `ycode_cli_setup.exe`），因为主安装包内嵌的
`cli/ycode_cli_setup.exe` 由安装引擎按固定名查找。构建完成后统一重命名为
`ycode_setup_v{版本}.exe` / `ycode_cli_setup_v{版本}.exe` 分发名，并自动清理 dist/
里上一版本留下的同名旧产物，多渠道分发时可直接从文件名区分新旧。

### 单文件 zip 载荷（安装向导秒开的关键）

核心程序包 `dist/ycode` 含 4000+ 个文件。安装向导不直接内嵌这些散文件，而是先把整个
目录压缩成单个 `build/ycode_pkg.zip`（中间产物，约百余 MB），向导 exe 只需内嵌这个 zip
加卸载程序等十几个文件——双击后 PyInstaller 自解压的文件数量从 4000+ 降到十几个，
界面几秒内出现（旧方案曾需 20~95 秒）；真正的文件释放挪到用户点“安装”后的步骤里做，
带进度条展示。两个安装向导（full/cli）共用同一份 zip 载荷。

### 可选参数

| 参数 | 作用 |
| --- | --- |
| `--skip-frontend` | 跳过前端构建，直接复用现有的 `frontend/dist`（适合只改了 Python 代码时快速重新打包；若 `frontend/dist` 从未构建过，生成的 GUI 版会无法加载界面）。 |
| `--rebuild-frontend` | 强制重新执行 `npm run build`，即使 `frontend/dist/index.html` 已经存在。 |
| `--cli-only` | 只构建 CLI 相关产物（核心程序包、卸载程序、`ycode_cli_setup_v{版本}.exe`），跳过体积较大的主安装包，适合只想验证/分发命令行版时使用。 |
| `--setup-only` | 只重建两个安装向导：跳过前端、核心程序包与卸载程序的编译，从现有 `dist/ycode` 重新压缩载荷后直接走步骤 3/4，全程约 2 分钟。仅改动 `ycode_setup.py` 后快速重建时使用；要求此前已经完整构建过一次（`dist/ycode` 与 `dist/ycode_uninstall.exe` 存在），全新机器上不可用。 |

## 三、构建产物

全部输出在 `dist/` 目录下：

| 路径 | 说明 |
| --- | --- |
| `dist/ycode/Y-code.exe` | GUI 窗口版主程序（`console=False`，双击直接打开窗口，无黑框） |
| `dist/ycode/ycode.exe` | CLI 控制台版主程序 |
| `dist/ycode/_internal/` | 两个主程序共享的运行时依赖与数据文件 |
| `dist/ycode_uninstall.exe` | 独立卸载向导（经典样式 GUI） |
| `dist/ycode_cli_setup_v{版本}.exe` | CLI 独立安装向导（既内嵌在主安装包里，也可单独分发给只想要命令行版的用户） |
| `dist/ycode_setup_v{版本}.exe` | 最终交付给用户的主安装包（双击这个即可） |

> 两个安装向导文件名中的 `{版本}` 自动取 `core/version.py` 的版本号；重命名时会清理
> dist/ 里上一版本留下的同名旧产物。主安装包内嵌的 `cli/ycode_cli_setup.exe`
> 仍为固定名（安装引擎按名查找）。

## 四、可选 CLI 安装机制

`ycode_setup.py` 是同一份源码的 tkinter 图形化向导（经典 Windows 安装程序样式，
白色顶部横幅 + 底部 上一步/下一步/取消 按钮组），靠内嵌的 `setup_mode.txt` marker
文件区分两种运行模式：

- `full` 模式（`ycode_setup_v{版本}.exe`，默认）：页面流程为
  欢迎 → 安装用户 → 路径与选项 → 安装中 → 完成。
  1. “安装用户”页选择安装范围：
     - 仅为我安装（推荐）：默认装到 `%LOCALAPPDATA%\Programs\Y-code`，无需管理员，
       写 HKCU 卸载注册项、用户 PATH、个人桌面快捷方式。
     - 为所有人安装：默认装到 `%ProgramFiles%\Y-code`，写 HKLM 卸载注册项、系统 PATH、
       公共桌面快捷方式；非管理员运行时自动以 UAC 提权重启自身（携带安装选项）后继续。
  2. 选项页可修改安装目录，并有两个复选框（默认都勾选）：
     “同时安装命令行版 CLI” 与 “创建桌面快捷方式”。
  3. “同时安装命令行版 CLI” 勾选时：保留 `ycode.exe`，把安装目录加入 PATH。
     不勾选时：从安装目录里删掉 `ycode.exe`，不写 PATH，转而把内嵌的
     `ycode_cli_setup.exe` 释放到 `<安装目录>\cli\ycode_cli_setup.exe`，
     完成页提示以后可双击它补装命令行版。
  4. 重装检测：读取卸载注册表发现已有安装时，自动沿用原安装范围与路径并锁定
     相关选项，提示本次为覆盖安装。
  5. 无论怎么选，都会释放完整核心程序包（GUI + 依赖）、播种 `~/.ycode` 下的默认
     配置与内置技能，并在“应用和功能”里注册一条卸载入口（user 范围写
     `HKCU\...\Uninstall\Y-code`，machine 范围写 HKLM 同路径）。
- `cli` 模式（`ycode_cli_setup_v{版本}.exe`）：页面流程为 欢迎 → 路径与选项 → 安装中
  → 完成（无“安装用户”页，CLI 始终按当前用户安装），只安装 `ycode.exe` 及其依赖，
  加入用户 PATH，创建 “Y-code CLI” 快捷方式。如果目标目录已经是一次完整版安装（检测到
  `Y-code.exe` 已存在），会直接合并进去，并复用完整版已经注册好的卸载入口，
  不会重复注册。

## 五、卸载流程

用户可以通过以下任意一种方式卸载：

1. Windows 设置 → 应用 → 已安装的应用 → 找到 "Y-code"（或独立安装 CLI 时的
   "Y-code CLI"）→ 卸载。
2. 直接运行安装目录下的 `ycode_uninstall.exe`。

卸载向导是同款经典样式的图形化向导（欢迎 → 卸载选项 → 卸载中 → 完成），会清理：
桌面快捷方式（GUI 版 + CLI 版）、PATH 环境变量里的安装路径、
注册表里的卸载入口（`Y-code` 与 `Y-code-CLI`（含旧版 `Y-code-CLI`）都会尝试清理）、
可选的 `~/.ycode` 用户配置与会话记录（卸载选项页复选框，默认保留），最后自我删除并
清空整个安装目录。

卸载范围自动适配安装范围：检测到 machine 安装（HKLM 注册项或位于 Program Files）时，
会额外清理 HKLM 卸载注册项、系统 PATH 与公共桌面快捷方式；非管理员运行时自动发起
UAC 提权重启后继续。

## 六、常见问题排查

- 提示找不到 npm / `npm run build` 报错退出：
  确认已安装 Node.js 20+，且新开一个命令行窗口执行 `npm -v` 能正常输出版本号
  （刚安装完 Node.js 的旧终端窗口可能还没刷新 PATH）。如果暂时不需要重新构建
  前端，可以加 `--skip-frontend` 先跳过。

- 提示找不到 PyInstaller：
  激活虚拟环境后执行 `venv\Scripts\pip install pyinstaller`，或确认
  `venv\Scripts\pyinstaller.exe` 确实存在。

- GUI 版打开后界面空白 / 提示找不到前端入口：
  说明 `frontend/dist/index.html` 不存在或是旧产物。执行
  `venv\Scripts\python build_release.py --rebuild-frontend` 强制重新构建前端后
  再打包。

- 杀毒软件误报（提示安装包/主程序是病毒或木马）：
  这是 PyInstaller 打包的可执行文件的通病（尤其是未做代码签名的 exe），并非
  真的中毒。项目里所有 spec 都已设置 `upx=False`（不做二次压缩），可以降低但
  不能完全消除误报概率。可以在被拦截时将 `dist/` 目录或安装目录加入杀毒软件的
  信任/白名单，或者上传到 VirusTotal 之类的多引擎查杀站点自行核实。长期方案是
  给发布的 exe 做代码签名（本项目目前未配置签名证书，不在本文档范围内）。
