# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# 主安装向导（完整版，GUI + 可选 CLI）的独立参考 spec。
#
# 注意：正式发布流程由 build_release.py 驱动，它用 `pyinstaller --onefile`
# 加一组动态 --add-data（内嵌 setup_mode.txt / version.txt 这两个每次构建都
# 重新生成的临时 marker 文件，以及上一步骤才产出的 dist/ycode_cli_setup.exe）
# 来分别构建 CLI / full 两种变体，这类“依赖上一步骤产物、且每次内容都不同”的
# 构建没法干净地表达成一份静态 spec，因此 build_release.py 不会调用本文件。
#
# 本文件保留下来，是为了给单独手动构建“完整版”安装包提供一份内容正确、
# 可直接使用的参考配置：ycode_setup.py 在找不到内嵌的 setup_mode.txt 时默认
# 按 'full' 模式运行（见 ycode_setup.py 的 read_setup_mode），所以本 spec
# 不需要额外注入模式标记文件。手动构建前请确保 dist/ycode、
# dist/ycode_uninstall.exe（以及可选的 dist/ycode_cli_setup.exe）已经存在，
# 例如先跑一遍 build_release.py，或按顺序手动执行对应的 pyinstaller 命令。
#
# 历史 BUG 提醒：旧版本这里漏掉了 config.example.json / system_prompt.md，
# 导致安装向导给 ~/.ycode 播种配置模板时永远走最简陋的兜底字面量。本文件已
# 补上这两个数据文件。
# ============================================================================
import os

datas = [
    ('dist/ycode_uninstall.exe', '.'),
    ('config.example.json', '.'),
    ('system_prompt.md', '.'),
    # GUI 专用提示词：与通用版并列随包，GUI 严格使用自己的，不回退混用
    ('system_prompt_gui.md', '.'),
    # GUI 向导资源：侧栏 logo（构建期预缩放到 168x168 的 PNG，运行期用
    # tk.PhotoImage 直接读，因此不再需要随包 Pillow）+ 窗口图标
    ('assets/ycode-logo-168.png', 'assets'),
    ('assets/ycode.ico', 'assets'),
]
# 核心程序载荷：优先单文件压缩包（onefile 启动解压快），
# 没有构建过压缩包时回退到目录形式（启动会慢很多）。
if os.path.exists('build/ycode_pkg.zip'):
    datas.append(('build/ycode_pkg.zip', '.'))
else:
    datas.append(('dist/ycode', 'ycode_pkg'))
# CLI 独立安装包是可选内嵌资源：单独手动跑本 spec 时如果还没构建
# dist/ycode_cli_setup.exe，就跳过它——ycode_setup.py 在找不到内嵌 CLI 安装包
# 时会自动跳过“未安装 CLI 时复制补装程序”这一步，不会因此崩溃。
if os.path.exists('dist/ycode_cli_setup.exe'):
    datas.append(('dist/ycode_cli_setup.exe', 'cli'))

a = Analysis(
    ['ycode_setup.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # excludes 与 build_release.py 的 build_setup_variant 保持同步：安装向导
    # 只用标准库 + tkinter，PIL（~15MB）与 numpy（~31MB，PIL 的可选依赖）
    # 都不需要——logo 已在构建期预缩放为 168x168 PNG。
    excludes=['PIL', 'numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ycode_setup',
    icon='assets/ycode.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=False：与 ycode.spec 保持一致，降低杀毒软件对压缩后可执行文件的
    # 启发式误报概率。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # console=False：安装向导是 tkinter GUI，不能弹出黑色控制台窗口
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
