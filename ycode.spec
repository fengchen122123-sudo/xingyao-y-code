# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# Collect litellm modules, but aggressively prune subsystems that ycode does not
# use at runtime. litellm's __init__ imports many submodules, and PyInstaller
# analyzing all of them creates a large number of threads/SSL contexts, which
# can exhaust Windows TLS slots and trigger R6016 "not enough space for thread
# data" during the build. Pruning also shrinks the final distribution.
_litellm_datas, litellm_binaries, _litellm_hiddenimports = collect_all('litellm')

# Subsystems that ycode does not need. These are excluded from hiddenimports and
# data files. Keep in sync with litellm's public subpackage list.
_LITELLM_EXCLUDE_SUBSYSTEMS = frozenset({
    'a2a_protocol',
    'assistants',
    'batches',
    'batch_completion',
    'endpoints',
    'evals',
    'experimental_mcp_client',
    'files',
    'fine_tuning',
    'images',
    'interactions',
    'ocr',
    'passthrough',
    'proxy',
    'proxy_auth',
    'rag',
    'realtime_api',
    'rerank_api',
    'router_strategy',
    'router_utils',
    'search',
    'skills',
    'vector_stores',
    'vector_store_files',
    'videos',
})

# These specific modules are imported directly by litellm/__init__.py and must
# remain available even though their parent package is otherwise pruned.
_LITELLM_KEEP_MODULES = frozenset({
    'litellm.integrations.custom_logger',
    'litellm.types.integrations.datadog',
    'litellm.types.integrations.datadog_llm_obs',
    'litellm.types.secret_managers.main',
})


def _litellm_module_belongs_to_kept_subsystem(module_name: str) -> bool:
    """Return True if module_name is part of a subsystem we want to keep."""
    # Core subsystems ycode actually uses.
    keep_subsystems = {'litellm_core_utils', 'llms', 'caching', 'types', 'utils',
                       '_logging', 'constants'}
    top = module_name.split('.')[1] if module_name.startswith('litellm.') else ''
    if top in keep_subsystems:
        return True
    # The litellm top-level package itself.
    if module_name == 'litellm' or module_name.startswith('litellm.'):
        # Keep integrations.* / secret_managers.* / types.integrations.* only if
        # explicitly listed above.
        return False
    return True


def _should_keep_litellm_module(module_name: str) -> bool:
    if module_name in _LITELLM_KEEP_MODULES:
        return True
    parts = module_name.split('.')
    if not parts or parts[0] != 'litellm':
        return True
    # Exclude whole subsystems.
    if len(parts) >= 2 and parts[1] in _LITELLM_EXCLUDE_SUBSYSTEMS:
        return False
    if len(parts) >= 3 and parts[1] == 'integrations' and parts[2] != 'custom_logger':
        return False
    if len(parts) >= 4 and parts[1] == 'types' and parts[2] == 'integrations' \
            and '.'.join(parts[:4]) not in _LITELLM_KEEP_MODULES:
        return False
    if len(parts) >= 4 and parts[1] == 'types' and parts[2] == 'secret_managers' \
            and '.'.join(parts[:4]) not in _LITELLM_KEEP_MODULES:
        return False
    return True


def _should_keep_litellm_data(data_item) -> bool:
    """Filter litellm data files by source path."""
    src = data_item[0].replace(os.sep, '/')
    if 'litellm/' not in src:
        return True
    rel = src.split('litellm/', 1)[1]
    rel_module = 'litellm.' + rel.replace('/', '.')

    # Always keep explicit keepers.
    for keeper in _LITELLM_KEEP_MODULES:
        if rel_module.startswith(keeper):
            return True

    top = rel.split('/', 1)[0]
    if top in _LITELLM_EXCLUDE_SUBSYSTEMS:
        return False
    if top == 'integrations' and not rel.startswith('integrations/custom_logger'):
        return False
    if top == 'types':
        sub = rel.split('/', 2)
        if len(sub) >= 2 and sub[1] == 'integrations' \
                and 'types.integrations.' + sub[2].split('/')[0] not in _LITELLM_KEEP_MODULES:
            # Keep generic types files (e.g. types/utils.py).
            pass
        if len(sub) >= 2 and sub[1] == 'secret_managers' \
                and 'types.secret_managers.' + sub[2].split('/')[0] not in _LITELLM_KEEP_MODULES:
            pass
    return True


litellm_hiddenimports = [
    m for m in _litellm_hiddenimports if _should_keep_litellm_module(m)
]
litellm_datas = [
    d for d in _litellm_datas if _should_keep_litellm_data(d)
]

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
    excludes=[
        'tkinter', 'matplotlib', 'IPython', 'notebook', 'PyQt5', 'PyQt6',
        # Additional litellm subsystems to exclude from analysis even if they
        # appear as transitive imports.
        'litellm.a2a_protocol', 'litellm.assistants', 'litellm.batches',
        'litellm.batch_completion', 'litellm.endpoints', 'litellm.evals',
        'litellm.experimental_mcp_client', 'litellm.files', 'litellm.fine_tuning',
        'litellm.images', 'litellm.interactions', 'litellm.ocr',
        'litellm.passthrough', 'litellm.proxy', 'litellm.proxy_auth',
        'litellm.rag', 'litellm.realtime_api', 'litellm.rerank_api',
        'litellm.router_strategy', 'litellm.router_utils', 'litellm.search',
        'litellm.skills', 'litellm.vector_stores', 'litellm.vector_store_files',
        'litellm.videos',
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
