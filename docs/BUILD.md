# Y-code Windows 打包构建指南

本文档说明如何在 Windows 上从源码构建 Y-code 的官方安装包：一个默认安装图形界面版
(GUI) 的主安装包，用户可以在安装过程中选择是否附带安装命令行版 (TUI)；如果当时选择
不装，安装目录里会留一份独立的 TUI 补装程序，以后随时双击即可补装。

## 一、前置条件

1. Windows 10/11。
2. 项目自带的 Python 虚拟环境 `venv\`，且已安装 `pyinstaller`：
   ```bash
   venv\Scripts\pip install pyinstaller
   ```
   （项目运行时依赖见 `requirements.txt`，同样需要提前 `pip install -r requirements.txt`。）
3. Node.js 20 及以上版本（用于构建 `frontend/` 下的 Vue 3 前端，GUI 版加载的就是它的
   编译产物）。安装后确认 `npm -v` 在命令行可用。

## 二、一条命令完成构建

在项目根目录下执行：

```bash
venv\Scripts\python build_release.py
```

该脚本会依次完成：前端构建 → 核心双主程序编译 → 卸载程序编译 → TUI 独立安装向导
编译 → 主安装向导编译 → 产物校验，并打印每一步的中文进度（✅ 成功 / ❌ 失败）。

### 可选参数

| 参数 | 作用 |
| --- | --- |
| `--skip-frontend` | 跳过前端构建，直接复用现有的 `frontend/dist`（适合只改了 Python 代码时快速重新打包；若 `frontend/dist` 从未构建过，生成的 GUI 版会无法加载界面）。 |
| `--rebuild-frontend` | 强制重新执行 `npm run build`，即使 `frontend/dist/index.html` 已经存在。 |
| `--tui-only` | 只构建 TUI 相关产物（核心程序包、卸载程序、`ycode_tui_setup.exe`），跳过体积较大的主安装包 `ycode_setup.exe`，适合只想验证/分发命令行版时使用。 |

## 三、构建产物

全部输出在 `dist/` 目录下：

| 路径 | 说明 |
| --- | --- |
| `dist/ycode/Y-code.exe` | GUI 窗口版主程序（`console=False`，双击直接打开窗口，无黑框） |
| `dist/ycode/ycode.exe` | TUI 控制台版主程序 |
| `dist/ycode/_internal/` | 两个主程序共享的运行时依赖与数据文件 |
| `dist/ycode_uninstall.exe` | 独立卸载向导 |
| `dist/ycode_tui_setup.exe` | TUI 独立安装向导（既内嵌在主安装包里，也可单独分发给只想要命令行版的用户） |
| `dist/ycode_setup.exe` | 最终交付给用户的主安装包（双击这个即可） |

## 四、可选 TUI 安装机制

`ycode_setup.py` 是同一份源码，靠内嵌的 `setup_mode.txt` marker 文件区分两种运行模式：

- `full` 模式（`ycode_setup.exe`，默认）：
  1. 询问安装目录，释放完整的核心程序包（GUI + TUI + 依赖）。
  2. 询问“是否同时安装命令行版 TUI (ycode 命令)？[Y/n]”：
     - 选 是：保留 `ycode.exe`，把安装目录加入 PATH，创建桌面快捷方式
       “Y-code TUI”。
     - 选 否：从安装目录里删掉 `ycode.exe`，不写 PATH，转而把内嵌的
       `ycode_tui_setup.exe` 释放到 `<安装目录>\tui\ycode_tui_setup.exe`，
       并提示“以后可双击 tui\ycode_tui_setup.exe 补装命令行版”。
  3. 无论上一步选什么，都会创建桌面快捷方式 “Y-code”（指向 `Y-code.exe`）、
     复制卸载程序、播种 `~/.ycode` 下的默认配置，并在
     “控制面板 → 程序和功能”（即注册表 `HKCU\...\Uninstall\Y-code`）里注册一条
     卸载入口。
- `tui` 模式（`ycode_tui_setup.exe`）：只安装 `ycode.exe` 及其依赖，加入 PATH，
  创建 “Y-code TUI” 快捷方式。如果目标目录已经是一次完整版安装（检测到
  `Y-code.exe` 已存在），会直接合并进去，并复用完整版已经注册好的卸载入口，
  不会重复注册。

## 五、卸载流程

用户可以通过以下任意一种方式卸载：

1. Windows 设置 → 应用 → 已安装的应用 → 找到 “Y-code”（或独立安装 TUI 时的
   “Y-code TUI”）→ 卸载。
2. 直接运行安装目录下的 `ycode_uninstall.exe`。

卸载向导会清理：桌面快捷方式（GUI 版 + TUI 版）、PATH 环境变量里的安装路径、
注册表里的卸载入口（`Y-code` 与 `Y-code-TUI` 都会尝试清理）、可选的 `~/.ycode`
用户配置与会话记录，最后自我删除并清空整个安装目录。

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
