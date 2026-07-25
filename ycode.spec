# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# 收集 litellm 全部模块与数据文件。
# 注意：虽然这里可以精简部分子系统来缩小体积，但 litellm/__init__.py 在顶层
# 会导入大量子模块（如 batches、images、router_utils、router_strategy 等）。
# PyInstaller 的 excludes 一旦把这些子系统排除，打包后的 exe 在运行时就会直接
# 报 No module named 'litellm.xxx'，导致回合异常结束。因此不再对 litellm 做
# 子系统级排除，优先保证运行时完整性。
litellm_datas, litellm_binaries, litellm_hiddenimports = collect_all('litellm')

# 收集 pyfiglet 字体数据和子模块，避免打包后在其他电脑上启动报
# "No module named 'pyfiglet.fonts'"。pyfiglet 通过 importlib.resources
# 在 pyfiglet.fonts 包中加载 .flf 字体文件，PyInstaller 默认不会收集。
pyfiglet_datas, pyfiglet_binaries, pyfiglet_hiddenimports = collect_all('pyfiglet')

# 收集 tiktoken_ext 插件包。tiktoken 在运行时通过 pkgutil.iter_modules 动态发现
# tiktoken_ext 下的编码插件（如 openai_public），PyInstaller 默认不会收集这个
# 命名空间包，导致打包后出现 "Unknown encoding cl100k_base. Plugins found: []"。
tiktoken_ext_datas, tiktoken_ext_binaries, tiktoken_ext_hiddenimports = collect_all('tiktoken_ext')

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=litellm_binaries + tiktoken_ext_binaries,
    datas=[
        ('config.example.json', '.'),
        ('system_prompt.md', '.'),
        ('vendor/ripgrep/rg.exe', 'vendor/ripgrep'),
        ('frontend/dist', 'frontend/dist'),
    ] + litellm_datas + pyfiglet_datas + tiktoken_ext_datas,
    hiddenimports=litellm_hiddenimports + pyfiglet_hiddenimports + tiktoken_ext_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'matplotlib', 'IPython', 'notebook', 'PyQt5', 'PyQt6',
    ],
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
