import os

DROP_SUBTREES = (
    'litellm/proxy/_experimental/out',
    'litellm/proxy/swagger',
    'litellm/llms/huggingface/huggingface_llms_metadata',
    # Playwright 的 TS 类型声明与内置 vite 打包产物：只有开发时生成 node driver
    # 的构建链会用，运行期 driver 完全不读。playwright 现已整体移出打包（见
    # common_excludes），这两条规则作为兜底保留：若将来又被 hook 带回资源，
    # 也不会把这两坨带进安装包。
    'playwright/driver/package/types',
    'playwright/driver/package/lib/vite',
)

TOKENIZERS_DIR = 'litellm/litellm_core_utils/tokenizers'
TOKENIZERS_KEPT_NAMES = frozenset({'anthropic_tokenizer.json'})

PYFIGLET_FONTS_DIR = 'pyfiglet/fonts'
PYFIGLET_KEPT_FONTS = frozenset({'slant.flf', 'slant_relief.flf'})


def _dest_rel_path(src, dest):
    """把 (src, dest) 归一化成包内相对路径，返回 (归一化 dest, 完整相对路径)。

    dest 是目标目录（Windows 下为反斜杠），文件名取自 src 的 basename
    —— basename 不含路径分隔符，不会引入分隔符差异。
    """
    norm_dest = (dest or '').replace('\\', '/').strip('/')
    if norm_dest == '.':
        norm_dest = ''
    name = os.path.basename((src or '').replace('\\', '/'))
    return norm_dest, (f'{norm_dest}/{name}' if norm_dest else name)


def _under(path, prefix):
    return path == prefix or path.startswith(prefix + '/')


def _entry_size(src):
    try:
        return os.path.getsize(src)
    except OSError:
        # 源文件不可读（已删除/无权限）不影响裁剪决策，仅少统计字节数
        return 0


def _bundle_rel_path(entry):
    if len(entry) >= 3:
        dest, src = entry[0], entry[1]
        norm_dest, _ = _dest_rel_path(src, dest)
        return norm_dest
    src, dest = entry[0], entry[1]
    _, rel_path = _dest_rel_path(src, dest)
    return rel_path


def _filter_entries(entries, predicate):
    kept = []
    removed_count = 0
    removed_bytes = 0
    for entry in entries:
        if len(entry) >= 3:
            dest, src = entry[0], entry[1]
        else:
            src, dest = entry[0], entry[1]
        norm_dest, rel_path = _dest_rel_path(src, dest)
        name = os.path.basename(rel_path)
        if predicate(entry, src, norm_dest, rel_path, name):
            removed_count += 1
            removed_bytes += _entry_size(src)
        else:
            # 原样保留 entry：TOC 三元组必须保持三元组，否则 COLLECT 崩
            kept.append(entry)
    return kept, removed_count, removed_bytes


def strip_unused_datas(datas, label):
    def _should_drop(_entry, _src, norm_dest, rel_path, name):
        if any(_under(norm_dest, rule) or _under(rel_path, rule) for rule in DROP_SUBTREES):
            return True
        if _under(norm_dest, TOKENIZERS_DIR):
            return not (name.lower().endswith('.py') or name in TOKENIZERS_KEPT_NAMES)
        if _under(norm_dest, PYFIGLET_FONTS_DIR):
            return name not in PYFIGLET_KEPT_FONTS
        return False

    return _filter_entries(datas, _should_drop)


DROP_BLOAT_DATA_SUBTREES = (
    'reportlab/fonts',
    'reportlab/graphics',
    'reportlab/platypus',
    '_tcl_data/tzdata',
    '_tk_data/images',
    '_tk_data/demos',
    'winpty/tests',
    'webview/lib/runtimes/win-arm64',
    'webview/lib/runtimes/win-x86',
)
DROP_BLOAT_DATA_PREFIXES = (
    'tcl8/8.5/tcltest',
)
DROP_BLOAT_DATA_EXACT = frozenset({
    'pythonnet/runtime/python.runtime.xml',
    'webview/lib/pywebview-android.jar',
    'webview/lib/webbrowserinterop.x86.dll',
})
REDUNDANT_PY_DATA_PREFIXES = (
    'openpyxl/',
    'pypdf/',
    'reportlab/',
)

DROP_UNUSED_BINARY_SUBTREES = (
    'pythonwin',
    'webview/lib/runtimes/win-arm64',
    'webview/lib/runtimes/win-x86',
)
DROP_UNUSED_BINARY_PREFIXES = (
    'pil/_avif',
)
DROP_UNUSED_BINARY_EXACT = frozenset({
    'webview/lib/webbrowserinterop.x86.dll',
})
DROP_UNUSED_BINARY_NAME_PREFIXES = (
    '_avif',
    'win32ui',
    'mfc140',
    'mfcm140',
)


def strip_bloat_datas(datas, label=''):
    """裁剪非必要数据资产、冗余 .py 源码副本与 __pycache__ 缓存，返回 (kept, removed_count, removed_bytes)。"""
    def _should_drop(entry, _src, _norm_dest, _rel_path, _name):
        bundle_rel = _bundle_rel_path(entry).lower()
        if any(_under(bundle_rel, rule) for rule in DROP_BLOAT_DATA_SUBTREES):
            return True
        if any(bundle_rel.startswith(prefix) for prefix in DROP_BLOAT_DATA_PREFIXES):
            return True
        if bundle_rel in DROP_BLOAT_DATA_EXACT:
            return True
        if '__pycache__' in bundle_rel.split('/') or bundle_rel.endswith(('.pyc', '.pyo')):
            return True
        if any(bundle_rel.startswith(prefix) for prefix in REDUNDANT_PY_DATA_PREFIXES):
            return bundle_rel.endswith(('.py', '.pyc', '.pyo'))
        return False

    return _filter_entries(datas, _should_drop)


def strip_unused_binaries(binaries, label=''):
    """裁剪主程序从不加载的原生扩展与非目标架构 DLL，返回 (kept, removed_count, removed_bytes)。"""
    def _should_drop(entry, _src, _norm_dest, _rel_path, _name):
        bundle_rel = _bundle_rel_path(entry).lower()
        base = os.path.basename(bundle_rel)
        if any(_under(bundle_rel, rule) for rule in DROP_UNUSED_BINARY_SUBTREES):
            return True
        if any(bundle_rel.startswith(prefix) for prefix in DROP_UNUSED_BINARY_PREFIXES):
            return True
        if bundle_rel in DROP_UNUSED_BINARY_EXACT:
            return True
        return base.startswith(DROP_UNUSED_BINARY_NAME_PREFIXES)

    return _filter_entries(binaries, _should_drop)


def filter_hidden_imports(modules, excluded_prefixes):
    prefixes = tuple(p.strip('.') for p in excluded_prefixes if p)
    return [
        mod for mod in modules
        if not any(mod == prefix or mod.startswith(prefix + '.') for prefix in prefixes)
    ]