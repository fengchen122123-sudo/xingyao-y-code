# -*- mode: python ; coding: utf-8 -*-
# 卸载向导：纯 onefile 窗口程序（经典安装程序样式 GUI）。安装目录、注册表路径等
# 都是运行时通过 sys.executable / winreg 现算的；datas 只带窗口/横幅图标，
# 与安装向导保持一致的视觉。复用 ycode_setup.py 的基础工具函数，
# PyInstaller 会随 import 自动打包该模块。由 build_release.py 直接
# `pyinstaller --clean --noconfirm ycode_uninstall.spec` 调用。

a = Analysis(
    ['ycode_uninstall.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets/ycode.ico', 'assets'),
        ('assets/ycode.png', 'assets'),
    ],
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
    name='Y-code-uninstall',
    icon='assets/ycode.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=False：与 ycode.spec 保持一致，避免 UPX 压缩后的可执行文件更容易触发
    # 杀毒软件的启发式误报（卸载程序本身体积不大，压缩收益有限，不值得冒这个险）。
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # GUI 卸载向导：不附带控制台窗口
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
