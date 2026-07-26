# -*- mode: python ; coding: utf-8 -*-
# ============================================================================
# 主安装向导（完整版，GUI + 可选 TUI）的独立参考 spec。
#
# 注意：正式发布流程由 build_release.py 驱动，它用 `pyinstaller --onefile`
# 加一组动态 --add-data（内嵌 setup_mode.txt / version.txt 这两个每次构建都
# 重新生成的临时 marker 文件，以及上一步骤才产出的 dist/ycode_tui_setup.exe）
# 来分别构建 tui / full 两种变体，这类“依赖上一步骤产物、且每次内容都不同”的
# 构建没法干净地表达成一份静态 spec，因此 build_release.py 不会调用本文件。
#
# 本文件保留下来，是为了给单独手动构建“完整版”安装包提供一份内容正确、
# 可直接使用的参考配置：ycode_setup.py 在找不到内嵌的 setup_mode.txt 时默认
# 按 'full' 模式运行（见 ycode_setup.py 的 read_setup_mode），所以本 spec
# 不需要额外注入模式标记文件。手动构建前请确保 dist/ycode、
# dist/ycode_uninstall.exe（以及可选的 dist/ycode_tui_setup.exe）已经存在，
# 例如先跑一遍 build_release.py，或按顺序手动执行对应的 pyinstaller 命令。
#
# 历史 BUG 提醒：旧版本这里漏掉了 config.example.json / system_prompt.md，
# 导致安装向导给 ~/.ycode 播种配置模板时永远走最简陋的兜底字面量。本文件已
# 补上这两个数据文件。
# ============================================================================
import os

datas = [
    ('dist/ycode', 'ycode_pkg'),
    ('dist/ycode_uninstall.exe', '.'),
    ('config.example.json', '.'),
    ('system_prompt.md', '.'),
]
# TUI 独立安装包是可选内嵌资源：单独手动跑本 spec 时如果还没构建
# dist/ycode_tui_setup.exe，就跳过它——ycode_setup.py 在找不到内嵌 TUI 安装包
# 时会自动跳过“未安装 TUI 时复制补装程序”这一步，不会因此崩溃。
if os.path.exists('dist/ycode_tui_setup.exe'):
    datas.append(('dist/ycode_tui_setup.exe', 'tui'))

a = Analysis(
    ['ycode_setup.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=False：与 ycode.spec 保持一致，降低杀毒软件对压缩后可执行文件的
    # 启发式误报概率。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
