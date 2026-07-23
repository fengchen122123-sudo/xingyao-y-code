# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

# Collect all litellm modules to avoid missing imports
litellm_datas, litellm_binaries, litellm_hiddenimports = collect_all('litellm')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=litellm_binaries,
    datas=[
        ('config.example.json', '.'),
        ('system_prompt.md', '.'),
        ('vendor/ripgrep/rg.exe', 'vendor/ripgrep'),
    ] + litellm_datas,
    hiddenimports=litellm_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'IPython', 'notebook', 'PyQt5', 'PyQt6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='ycode',
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
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='ycode',
)
