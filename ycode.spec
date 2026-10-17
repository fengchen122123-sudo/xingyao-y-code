# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# Y-code 双主程序打包 spec：一次 PyInstaller 构建同时产出
#   - ycode.exe   ：CLI 控制台版 (入口 main.py,     console=True)
#   - Y-code.exe  ：GUI 窗口版   (入口 gui_main.py, console=False)
# 两者共享同一份运行时依赖，最终合并进同一个 onedir 输出目录 dist/ycode/
# （两个 exe 平铺在根目录，依赖统一收在 dist/ycode/_internal/ 下），方便安装
# 程序把它们当作同一个安装包里的两个入口来管理。
# ============================================================================
from PyInstaller.utils.hooks import collect_all

# ---------------------------------------------------------------------------
# 第三方库隐藏依赖收集。CLI 和 GUI 两个入口共用同一套核心机器（core.llm /
# core.tools 等模块两边都会 import），因此下面几个 collect_all 结果两个
# Analysis 都要用到。
# ---------------------------------------------------------------------------

# litellm/__init__.py 顶层会导入大量子模块（batches、images、router_utils 等），
# 一旦被 excludes 排除，运行时会直接报 No module named 'litellm.xxx'，因此不做
# 子系统级裁剪，优先保证运行时完整性。
litellm_datas, litellm_binaries, litellm_hiddenimports = collect_all('litellm')

# pyfiglet 通过 importlib.resources 在 pyfiglet.fonts 包里加载 .flf 字体文件，
# PyInstaller 默认不会收集，需要 collect_all 才能避免运行时 "No module named
# 'pyfiglet.fonts'"（GUI 入口虽然不显示 ASCII Logo，但和 CLI 共用同一份
# _internal 依赖目录，统一收集更简单可靠）。
pyfiglet_datas, pyfiglet_binaries, pyfiglet_hiddenimports = collect_all('pyfiglet')

# tiktoken 运行时通过 pkgutil.iter_modules 动态发现 tiktoken_ext 下的编码插件
# （如 openai_public），PyInstaller 默认不会收集这个命名空间包，需要显式收集，
# 否则打包后会报 "Unknown encoding cl100k_base. Plugins found: []"。
tiktoken_ext_datas, tiktoken_ext_binaries, tiktoken_ext_hiddenimports = collect_all('tiktoken_ext')

# Playwright（电脑操控，Task 16 / 设计 23.1）：collect_all 收集 Python 子模块、
# node driver 脚本与所需二进制资源。运行时只使用系统 Edge/Chrome channel
# （channel="msedge"/"chrome"），绝不下载或打包 Playwright 的 .local-browsers
# 浏览器缓存（Chromium/Firefox/WebKit 均不在 wheel 内，collect_all 不会带入）。
playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all('playwright')

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
] + litellm_datas + pyfiglet_datas + tiktoken_ext_datas + playwright_datas + pywinpty_datas

common_binaries = litellm_binaries + tiktoken_ext_binaries + playwright_binaries + pywinpty_binaries

common_hiddenimports = litellm_hiddenimports + pyfiglet_hiddenimports + tiktoken_ext_hiddenimports + playwright_hiddenimports + pywinpty_hiddenimports + [
    # core.hunyuan_image 只被内置插件 plugins/hunyuan_image 在运行时动态 import，
    # PyInstaller 静态分析不可达，必须显式声明，否则打包后插件调用直接
    # ModuleNotFoundError。
    'core.hunyuan_image',
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
common_excludes = [
    'tkinter', 'matplotlib', 'IPython', 'notebook', 'PyQt5', 'PyQt6',
    'numpy', 'numpydoc',
]


# ============================================================================
# CLI 入口：main.py → ycode.exe（控制台程序）
# ============================================================================
a_cli = Analysis(
    ['main.py'],
    pathex=[],
    binaries=common_binaries,
    datas=common_datas,
    hiddenimports=common_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=common_excludes,
    noarchive=False,
    optimize=0,
)
pyz_cli = PYZ(a_cli.pure)

exe_cli = EXE(
    pyz_cli,
    a_cli.scripts,
    [],
    exclude_binaries=True,
    name='ycode',
    icon='assets/ycode.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)


# ============================================================================
# GUI 入口：gui_main.py → Y-code.exe（窗口程序，无控制台）
# ============================================================================
gui_hiddenimports = common_hiddenimports + [
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
    # 仅 GUI 入口注册“空输出”运行时钩子：console=False 时 sys.stdout/stderr
    # 为 None，任何遗留 print() 都会 AttributeError 崩溃且没有控制台可看报错，
    # 详见 packaging/runtime_hook_nullstdout.py 的说明。
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
# 合并收集：两个 EXE 共用同一个 onedir 输出目录 dist/ycode（同一份
# _internal 依赖，避免重复安装两份体积高达数百 MB 的运行时依赖）。
# a_*.zipfiles 在当前依赖集合下通常是空列表（现代 wheel 基本不再以 egg/
# zipimport 形式分发），但保留它与原单 EXE 版 spec 的 COLLECT 组成保持一致，
# 避免以后出现极端情况下静默漏收文件。
# ============================================================================
coll = COLLECT(
    exe_gui,
    exe_cli,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    a_cli.binaries,
    a_cli.zipfiles,
    a_cli.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ycode',
)
