# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# Y-code 打包 spec：**一份共享 _internal 依赖目录 + 三个 onedir 入口**
#   - Y-code.exe  ：GUI 窗口版 (入口 gui_main.py, console=False)；同时也是
#                   `--cli` 模式下的 CLI 实现（gui_main._dispatch_cli_argv 在
#                   导入任何 GUI 重模块之前转交 main.py 的 run_cli/run_headless）
#   - ycode.exe   ：不再由 PyInstaller 产出，改由 build_release.build_cli_launcher
#                   用 csc 把 packaging/ycode_launcher.cs 编译成几 KB 的 C#
#                   控制台启动器（透传参数 + 追加 --cli 拉起 Y-code.exe）。
#   - Y-code-uninstall.exe：卸载向导 (入口 ycode_uninstall.py, console=False)。
#                   2026-11 起从「独立 onefile exe（13.4MB，由
#                   ycode_uninstall.spec 单独构建并被安装包内嵌）」改为「第三个
#                   onedir 入口，与主程序共享 dist/ycode/_internal」。收益：
#                   ①安装包里不再多嵌一份卸载器（主安装包与 CLI 安装包各省一份）；
#                   ②卸载器使用的 python/tcl-tk 等 DLL 在安装目录里只有一份；
#                   ③卸载器仍随载荷一起落盘到安装目录根，注册表 UninstallString
#                   指向不变。
#
# 为什么合并（2026-10 体积优化）：原先两个 Analysis 各产出一份约 31MB 的 PYZ，
# 实测 dist/ycode/Y-code.exe 与 ycode.exe 的 PYZ 分别为 31.75MB / 31.25MB，
# 其中共用模块 5723 个、两侧各 30.83MB 完全相同，CLI 独有模块仅 0.12MB。
# 合并后安装包省下约 31MB 载荷，且两个入口的依赖图不再需要各自维护一遍。
# ============================================================================
import os
import sys

from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------------------
# datas 裁剪规则与实现已抽到项目根的 packaging_datas.py（普通模块，可被 pytest
# 单测覆盖）：spec 由 PyInstaller 用 exec 执行、本身不能被 import，规则留在 spec
# 里等于永远没有测试，而删错一项就是打包版运行期崩溃（缺内置技能/前端资源，
# 或缺 litellm 导入期必读的 tokenizer）。规则细节与每条保留/剔除理由见
# packaging_datas.py 的注释。
#
# PyInstaller 不会把 spec 所在目录加进 sys.path（它只 extend pathex，本项目为
# 空），而 build_release.py 调用的是 venv\Scripts\pyinstaller.exe（sys.path[0]
# 是 Scripts 目录，本仓库 editable 安装的 finder 也只映射 core/main/tests），
# 因此这里显式把 spec 目录插到 sys.path 头部，保证下面的 import 一定成立。
# ---------------------------------------------------------------------------
_SPEC_DIR = globals().get('SPECPATH') or os.path.dirname(os.path.abspath(globals().get('SPEC', '.')))
if _SPEC_DIR not in sys.path:
    sys.path.insert(0, _SPEC_DIR)

from packaging_datas import strip_unused_datas  # noqa: E402  (须在 sys.path 补好之后)

# ---------------------------------------------------------------------------
# 第三方库隐藏依赖收集。CLI（main.py 的 run_cli/run_headless，经 gui_main 的
# --cli 分支 import）和 GUI 共用同一套核心机器（core.llm / core.tools 等模块
# 两边都会 import），因此下面几个 collect_all 结果只构建一份 Analysis 也要全部收齐。
# ---------------------------------------------------------------------------

# litellm 已彻底移除：core/llm_native_transport.py 用原生 httpx.Client + SSEDecoder
# 直连 provider，token 估算也是自写的，全仓没有一行 import litellm。此前的
# collect_all('litellm') 会把整个包（约 29MB）无差别塞进产物，属于纯死重量，故移除。
# 若日后重新引入 litellm，需同时恢复这里与下方三处拼接。

# pyfiglet 通过 importlib.resources 在 pyfiglet.fonts 包里加载 .flf 字体文件，
# PyInstaller 默认不会收集，需要 collect_all 才能避免运行时 "No module named
# 'pyfiglet.fonts'"（GUI 入口虽然不显示 ASCII Logo，但和 CLI 共用同一份
# _internal 依赖目录，统一收集更简单可靠）。
pyfiglet_datas, pyfiglet_binaries, pyfiglet_hiddenimports = collect_all('pyfiglet')

# tiktoken 运行时通过 pkgutil.iter_modules 动态发现 tiktoken_ext 下的编码插件
# （如 openai_public），PyInstaller 默认不会收集这个命名空间包，需要显式收集，
# 否则打包后会报 "Unknown encoding cl100k_base. Plugins found: []"。
tiktoken_ext_datas, tiktoken_ext_binaries, tiktoken_ext_hiddenimports = collect_all('tiktoken_ext')

# 办公文档工具（plugins/office）：xlsx 走 openpyxl、pdf 走 pypdf，两者都是纯
# Python 包。注意这里的收集不是"保险起见"——plugins/ 目录是以 datas 原样拷进
# 安装包的（见下方 ('plugins', 'plugins')），PyInstaller 根本不会分析插件文件里
# 的 import，所以 office_read / office_write 依赖的这两个包静态分析一律收不到，
# 不显式 collect_all 就是打包版直接 ModuleNotFoundError。
# 另一个好处：build_release.verify_spec_collect_all_targets 从 spec 源码解析
# collect_all 目标，补上后会被纳入构建前环境硬校验，构建机漏装时立刻失败，
# 不会再静默产出缺依赖的安装包。
openpyxl_datas, openpyxl_binaries, openpyxl_hiddenimports = collect_all('openpyxl')
pypdf_datas, pypdf_binaries, pypdf_hiddenimports = collect_all('pypdf')

# Playwright（电脑操控，Task 16 / 设计 23.1）：**不再随包收集**（原先这里是
# collect_all('playwright')）。原因：运行时只用系统 Edge/Chrome
# （channel="msedge"/"chrome"，见 core/computer/browser_backend.py），
# Playwright 自己的浏览器缓存从不打包；而 wheel 里的 node driver 光是
# driver/package 就体积可观，其中 types 声明与 vite 产物运行期永不读取。
# 打包版改为「按需补装组件」：电脑操控的浏览器后端在
# core/computer/browser_backend.py 里是函数内惰性 import（from playwright.sync_api
# import sync_playwright），包缺失时只有该后端启动失败，其它功能不受影响。
# 因此这里不调用 collect_all('playwright')；同时在 common_excludes 中排除
# 'playwright'，防止 hook 或隐式导入把它重新拖进依赖图。

# pywinpty（GUI 内嵌交互式终端）：core/gui_terminal.py 被 gui_bridge.py 顶层导入，
# 其内部的 `from winpty import PtyProcess` 虽是函数内 import，但仍是字面 import，能被
# PyInstaller 字节码扫描识别，Python 模块本身不会漏收；必须 collect_all 是因为其原生
# 二进制与 datas（Rust 扩展、conpty.dll / winpty.dll / OpenConsole.exe / winpty-agent.exe
# 等）默认收集不全，否则打包后打开底部终端直接报错。
pywinpty_datas, pywinpty_binaries, pywinpty_hiddenimports = collect_all('winpty')

# CLI 与 GUI 都会用到的数据文件：默认配置模板、系统提示词模板、内置 ripgrep
# （core.tools 的 search_code 工具依赖，两个入口共享同一套工具注册表）、前端
# 编译产物（GUI 加载的静态资源；CLI 虽然用不到，但沿用原 spec 已生效的打包
# 方式，避免改变既有行为）。
common_datas = [
    ('config.example.json', '.'),
    ('system_prompt.md', '.'),
    # GUI 专用提示词：与通用版并列随包，GUI 严格使用自己的，不回退混用
    ('system_prompt_gui.md', '.'),
    ('vendor/ripgrep/rg.exe', 'vendor/ripgrep'),
    # Mermaid 渲染器（CLI 流程图导出用：core.diagram_export 生成的本地 HTML
    # 以 file:// 引用此文件，浏览器端离线渲染；缺失时页面降级只显示源码）
    ('vendor/mermaid/mermaid-11.17.2.min.js', 'vendor/mermaid'),
    ('frontend/dist', 'frontend/dist'),
    # 独立移动端子应用（/m/ 路径托管，co-team 风格界面）：lan_server 的
    # _serve_mobile_static() 在打包环境从 _MEIPASS/frontend-mobile/dist 读取；
    # 缺失时手机扫码打开 /m/ 路径会 404（经典界面不受影响）
    ('frontend-mobile/dist', 'frontend-mobile/dist'),
    # 手机远程控制传输 shim：LAN server 注入到手机端页面 <head>，
    # _serve_shim() 在打包环境从 _MEIPASS/frontend/ 读取；缺失时手机端
    # window.pywebview 永不注入，前端 30s 轮询超时后弹「pywebview API 不可用」
    ('frontend/mobile-shim.js', 'frontend'),
    # 内置技能（plugin-creator 等）：首次启动释放到 ~/.ycode/skills，用户可自由开关
    ('skills', 'skills'),
    # 内置插件目录（随包发布）：core/plugin_loader.py 自动扫描并默认信任
    ('plugins', 'plugins'),
    # GUI 运行时用 WM_SETICON 设置窗口/任务栏图标，需随包携带 ico 文件
    ('assets/ycode.ico', 'assets'),
    # 更新日志（关于页展示当前版本更新内容 + 历史版本列表）
    ('assets/CHANGELOG.md', 'assets'),
] + pyfiglet_datas + tiktoken_ext_datas + pywinpty_datas \
  + openpyxl_datas + pypdf_datas

# 【ffmpeg 不再随安装包分发】历史上这里会把 vendor/ffmpeg/ffmpeg.exe 与
# NOTICE.txt 追加进 common_datas（打进 _MEIPASS/vendor/ffmpeg）。96.4 MiB 的
# GPLv3 二进制让安装包凭空大出 ~35 MiB，而「背景图转换」只是少数用户会用的
# 可选功能——为一个可选功能让所有人多下 35 MiB 并不划算。
# 现在改为**按需组件**：运行期由 core/components.py 的 FFMPEG_SPEC 下载安装到
# ~/.ycode/components/ffmpeg/（用户在「设置 → 背景图转换」点下载），组件包由
# build_release.py::step0d_build_ffmpeg_component 单独产出并挂到同一个 Gitee
# Release 上。
# 因此本 spec 刻意不再引用 vendor/ffmpeg：仓库里的文件保留（scripts/verify_vendor.py
# 仍按 vendor/VENDOR_MANIFEST.json 校验其哈希），只是不再进入安装包。
# 注：GPLv3 的分发义务（许可证全文 + 源码获取方式）随组件包内的 NOTICE.txt
# 一并提供给下载者，不再由安装包承担。

common_binaries = (
    tiktoken_ext_binaries + pywinpty_binaries
    + openpyxl_binaries + pypdf_binaries
)

common_hiddenimports = (
    pyfiglet_hiddenimports + tiktoken_ext_hiddenimports
    + pywinpty_hiddenimports + openpyxl_hiddenimports + pypdf_hiddenimports
) + [
    # core.hunyuan_image 只被内置插件 plugins/hunyuan_image 在运行时动态 import，
    # PyInstaller 静态分析不可达，必须显式声明，否则打包后插件调用直接
    # ModuleNotFoundError。
    'core.hunyuan_image',
    # 同上：core.novel_service 只被内置插件 plugins/novel_workspace 动态 import。
    'core.novel_service',
    # comtypes 在运行时从系统 UIAutomationCore.dll 生成 comtypes.gen.* 绑定，
    # PyInstaller 静态扫描不可达（仅 Windows 桌面语义定位路径使用）。
    'comtypes',
    'comtypes.client',
    'comtypes.gen',
    'comtypes.gen.UIAutomationClient',
]

# tkinter/matplotlib/IPython/notebook 均未被本项目使用；PyQt5/PyQt6 是
# pywebview 的可选 Qt 后端（本项目在 Windows 上固定走 edgechromium/winforms
# 后端），一并排除可显著减小体积、加快启动时的模块扫描。
# numpy/numpydoc 仅被 litellm 的冷门可选路径引用（nvidia riva 音频转写 /
# function_to_dict 文档解析，本项目均不使用，且为函数内懒 import），排除后
# 可省 ~27MB（openblas 单文件即 20MB）。
#
# 【tkinter 不再排除】卸载向导 ycode_uninstall.py 是 tkinter GUI（经典 Windows
# 安装程序样式），且与主程序共用同一份 _internal 依赖目录，因此 tcl/tk 的数据
# 与 _tkinter.pyd 必须随包收集，不能再把 tkinter 放进 excludes（否则卸载器
# 一启动就 ImportError）。
#   注：主 GUI 这一路也会收集 tkinter——core/mcp_selector.py 读剪贴板的
#   第 3 级回退里有函数内 `import tkinter`，PyInstaller 的字节码扫描能识别。
#   两份 Analysis 收的是同一个解释器里的同一批文件，最终 _internal 的内容是
#   两者的并集（体积不因谁收而变），重名条目统一在下方 _dedupe_secondary_toc
#   里按 dest 去重，避免 COLLECT 阶段重复拷贝。
common_excludes = [
    'matplotlib', 'IPython', 'notebook', 'PyQt5', 'PyQt6',
    'numpy', 'numpydoc',
    # Playwright 改按需补装（见上方 collect_all 说明）：排除模块本身，避免
    # 隐式 import 或第三方 hook 把 wheel 与 node driver 重新收进安装包。
    'playwright',
    # 网页搜索链路（ddgs）改按需组件（见 core/components.py 的 SEARCH_SPEC）：
    # 以下四个包只服务 web_search，已改为运行时下载，安装包不再内置。
    #   ddgs           -> 搜索聚合库本身（core/tools.py 已改为函数内懒 import）
    #   primp / lxml   -> ddgs 的 Rust / C 扩展，平台绑定且体积不小
    #   fake_useragent -> 仅 ddgs 用到的 UA 伪装库
    # 刻意不排除 click / httpx / certifi：它们虽然也在 ddgs 的 Requires 里，
    # 但主程序自己（litellm、uvicorn、各处 HTTP 调用）就在用，排除会直接
    # ImportError。组件包内同名目录只是插在 sys.path 前面，仍可从主包复用。
    'ddgs', 'primp', 'lxml', 'fake_useragent',
]


# ---------------------------------------------------------------------------
# datas 裁剪：剔除第三方库 collect_all 带进来的无用资产。
#
# 规则（DROP_SUBTREES / TOKENIZERS_* / PYFIGLET_*）与实现整体在
# packaging_datas.strip_unused_datas 里，并且有单测（tests/test_spec_datas.py）
# 锁住行为。spec 里不再保留副本，避免两份规则各说各话；调用点在下方 Analysis
# 建好之后（见 "datas 裁剪：COLLECT 直接读取 a_gui.datas" 一节）。
# ---------------------------------------------------------------------------


# ============================================================================
# COLLECT 去重：多份 Analysis 共享同一个 _internal 目录
#
# 卸载器那一路与 GUI 那一路收的是同一个解释器里的同一批运行时文件
# （python314.dll、ucrtbase.dll、base_library.zip、_tkinter.pyd、tcl/tk 数据……），
# 目的 TOC 里会出现同名条目。本机 PyInstaller（6.21.0）的 COLLECT.assemble()
# 只在 strict_collect_mode 为真时才因重名抛错，默认 False 时是「后写覆盖先写」
# 且每个重名文件都白拷一遍 —— 既不报错也不做声明，一旦将来打开严格模式就直接
# 崩构建，而且重复处理几百个文件纯属浪费构建时间。
#
# 因此这里显式去重：以 GUI 那一路为准（primary），卸载器那一路（secondary）只
# 保留 GUI 里不存在的目标条目（tcl/tk 数据、卸载器独有的 PYZ/PKG 等）。
# 判定键 = 「目标目录 + 文件名」的整条相对路径（忽略分隔符与大小写，因为
# Windows 上的目标文件系统本身大小写不敏感）。两份 Analysis 的源文件完全相同，
# 被丢弃的那一份内容必然等价，不存在「覆盖成不同版本」的风险。
# ============================================================================
def _toc_dest_key(entry):
    """TOC 条目的去重键：归一化后的目标相对路径（(dest 目录 + basename)）。"""
    dest = str(entry[0] or '').replace('\\', '/').strip()
    while dest.startswith('./'):
        dest = dest[2:]
    return dest.strip('/').lower()


def _dedupe_secondary_toc(primary_tocs, secondary_toc, label):
    """剔除 secondary_toc 中目标路径已存在于 primary_tocs 的条目并打印统计。"""
    seen = set()
    for toc in primary_tocs:
        for entry in toc:
            seen.add(_toc_dest_key(entry))
    kept = []
    dropped = 0
    dropped_bytes = 0
    for entry in secondary_toc:
        if _toc_dest_key(entry) in seen:
            dropped += 1
            try:
                dropped_bytes += os.path.getsize(entry[1])
            except OSError:
                # 源文件不可读（已删除/无权限）不影响去重决策，仅少统计字节数
                pass
            continue
        kept.append(entry)
    print(
        f'[ycode.spec] {label} 与 GUI 载荷去重 {dropped} 项'
        f'（约 {dropped_bytes / (1024 * 1024):.1f} MB），保留 {len(kept)} 项'
    )
    return kept

# ============================================================================
# 唯一 Python 载荷：gui_main.py → Y-code.exe（窗口程序，无控制台）
#
# CLI 不再是独立 Analysis：ycode.exe 只是 C# 启动器，
# `ycode ...` ≡ `Y-code.exe --cli ...`（参数原样透传 + 追加 --cli），
# 由 gui_main 的分派逻辑接管，因此这里只有一份 PYZ / 一份 EXE。
# ============================================================================
gui_hiddenimports = common_hiddenimports + [
    # CLI 入口模块：gui_main._dispatch_cli_argv 在 --cli 模式（ycode.exe 启动器
    # 注入）下 `import main` 并调用 main.run_cli() / main.run_headless()。这句
    # import 写在函数体内，PyInstaller 的字节码扫描通常能覆盖，但漏收的后果是
    # 安装版 CLI（ycode / ycode -p）运行时直接 ModuleNotFoundError，且 GUI 用户
    # 完全无感——属于"只在新版本用命令行时才炸"的高代价缺陷，这里显式声明兜底。
    'main',
    # pywebview 按运行平台动态选择后端实现（运行时 importlib 动态 import），
    # PyInstaller 静态扫描找不到这类引用，必须显式声明为 hiddenimports，
    # 否则打包后在其他电脑上启动会直接报
    # "No module named 'webview.platforms.xxx'"。
    'webview',
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    # edgechromium 后端底层通过 pythonnet/clr_loader 桥接 .NET WebView2 控件。
    'clr_loader',
    'pythonnet',
    'pystray',
    'pystray._win32',
    # 飞书通道（core/feishu/*）：由 gui_bridge 在函数内惰性 import，静态分析
    # 本可见；但打包版无控制台，漏收只会在运行期才炸，这里显式声明兜底。
    # 该通道不引入第三方依赖（传输层为自维护代码），故无需新增 collect_all。
    'core.feishu.service',
    'core.feishu.bridge',
    'core.feishu.client',
    'core.feishu.cards',
    'core.feishu.commands',
    'core.feishu.config',
    'core.feishu._lark_ws',
]

a_gui = Analysis(
    ['gui_main.py'],
    pathex=[],
    binaries=common_binaries,
    datas=common_datas,
    hiddenimports=gui_hiddenimports,
    hookspath=[],
    hooksconfig={},
    # 注册“空输出”运行时钩子：console=False 时 sys.stdout/stderr 为 None，
    # 任何遗留 print() 都会 AttributeError 崩溃且没有控制台可看报错，
    # 详见 packaging/runtime_hook_nullstdout.py 的说明。
    # --cli 模式（ycode.exe 启动器 + 控制台）下 gui_main._ensure_cli_stdio 会先
    # AttachConsole 再用 CONOUT$/CONIN$ 顶掉这里的哑对象（哑对象 fileno() 抛
    # OSError，被判为不可用），因此同一份 hook 不会拖累 CLI 输出。
    runtime_hooks=['packaging/runtime_hook_nullstdout.py'],
    excludes=common_excludes,
    noarchive=False,
    optimize=0,
)
pyz_gui = PYZ(a_gui.pure)

exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name='Y-code',
    icon='assets/ycode.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# ============================================================================
# datas 裁剪：COLLECT 直接读取 a_gui.datas，所以必须在 Analysis 建好之后、
# COLLECT 之前裁剪（合并为单载荷后只剩 GUI 这一路；CLI 走同一个入口，
# 用的是同一份 datas，无需再裁第二遍）。
# ============================================================================
a_gui.datas, _gui_removed, _gui_removed_bytes = strip_unused_datas(a_gui.datas, 'GUI(Y-code.exe)')
print(f'[ycode.spec] GUI(Y-code.exe) 裁剪 {_gui_removed} 项，约 {_gui_removed_bytes / (1024 * 1024):.1f} MB')


# ============================================================================
# 第三套入口：卸载向导 Y-code-uninstall.exe（onedir，共享同一份 _internal）
#
# 只带 tkinter 界面需要的运行时（Analysis 会按 ycode_uninstall.py 的静态 import
# 收 _tkinter.pyd 与 tcl/tk 数据），datas 仅窗口/横幅两个图标：
#   - assets/ycode.ico：窗口 + 任务栏图标（_set_icon 用 iconbitmap + WM_SETICON）
#   - assets/ycode.png：向导横幅图标（_build_chrome 用 tk.PhotoImage 读取，
#     缺失时静默不显示，因此必须显式随包，否则卸载向导横幅会少一个图标）
# 下载/安装相关的重资产（frontend/dist、plugins、skills、litellm、ffmpeg……）
# 一概不收：卸载器只需要 tkinter + os/shutil/winreg 这些标准库。
#
# 不使用 packaging/runtime_hook_nullstdout.py：那个 hook 是给 GUI 主程序
# （无控制台时 print 会崩）准备的；卸载器自己在 main() 里已把 None 的
# stdout/stderr 顶成 devnull，两套机制叠加没必要。
# ============================================================================
a_uninst = Analysis(
    ['ycode_uninstall.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/ycode.ico', 'assets'),
        ('assets/ycode.png', 'assets'),
    ],
    # installer_common 是 ycode_uninstall.py 顶层 `from installer_common import ...`
    # 的普通模块 import，静态分析本就能收到；显式声明只是兜底（漏收会让卸载器
    # 一启动就 ImportError，且卸载器没有控制台可看报错）。
    hiddenimports=['installer_common'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 卸载器只用 tkinter（横幅图标走 tk.PhotoImage 读 assets/ycode.png），
    # 全程不 import PIL，因此这里在 common_excludes 之外单独再排除 PIL：
    # 它只服务卸载器这一路，不能进 common_excludes —— 主程序
    # core/gui_theme.py 顶层 `from PIL import Image` 真的要用 PIL，
    # 一旦全局排除，GUI 一启动就 ImportError。
    # （numpy 本来就在 common_excludes 里，这里显式重申只为表明卸载器同样不需要它。）
    excludes=common_excludes + ['PIL', 'numpy'],
    noarchive=False,
    optimize=0,
)
pyz_uninst = PYZ(a_uninst.pure)

exe_uninst = EXE(
    pyz_uninst,
    a_uninst.scripts,
    [],
    exclude_binaries=True,
    name='Y-code-uninstall',
    icon='assets/ycode.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# ============================================================================
# 收集：三个 EXE + 单份依赖，输出 onedir 目录 dist/ycode（依赖统一收在
# dist/ycode/_internal/ 下）。ycode.exe 由 build_release.build_cli_launcher
# 在本步骤之后用 csc 编译（几 KB 的 C# 启动器），不再是 PyInstaller 产物；
# Y-code-uninstall.exe 也落在 dist/ycode 根目录，由安装向导随载荷一起释放到
# 安装目录，因此它运行时能找到同一目录下的 _internal。
#
# 卸载器那一路的 binaries/datas/zipfiles 先与 GUI 那一路去重（见
# _dedupe_secondary_toc）：重名条目以 GUI 的为准，只保留卸载器独有的文件。
# zipfiles 在当前依赖集合下通常是空列表（现代 wheel 基本不再以 egg/zipimport
# 形式分发），保留它与原 spec 的 COLLECT 组成保持一致，避免以后出现极端情况下
# 静默漏收文件。
# ============================================================================
_uninst_binaries = _dedupe_secondary_toc(
    [a_gui.binaries, a_gui.datas, a_gui.zipfiles], a_uninst.binaries, '卸载器 binaries')
_uninst_datas = _dedupe_secondary_toc(
    [a_gui.binaries, a_gui.datas, a_gui.zipfiles], a_uninst.datas, '卸载器 datas')
_uninst_zipfiles = _dedupe_secondary_toc(
    [a_gui.binaries, a_gui.datas, a_gui.zipfiles], a_uninst.zipfiles, '卸载器 zipfiles')

coll = COLLECT(
    exe_gui,
    exe_uninst,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    _uninst_binaries,
    _uninst_zipfiles,
    _uninst_datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ycode',
)
