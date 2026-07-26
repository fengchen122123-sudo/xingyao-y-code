# -*- mode: python ; coding: utf-8 -*-
# 卸载向导：纯 onefile 控制台程序，不依赖任何项目内数据文件（安装目录、注册表
# 路径等都是运行时通过 sys.executable / winreg 现算的），因此 Analysis 不需要
# 额外的 datas/hiddenimports。由 build_release.py 直接
# `pyinstaller --clean --noconfirm ycode_uninstall.spec` 调用，不再用 CLI flag
# 重复表达一遍同样的配置。

a = Analysis(
    ['ycode_uninstall.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
    name='ycode_uninstall',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=False：与 ycode.spec 保持一致，避免 UPX 压缩后的可执行文件更容易触发
    # 杀毒软件的启发式误报（卸载程序本身体积不大，压缩收益有限，不值得冒这个险）。
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
