import os
import re
import subprocess
import sys
import time
from pathlib import Path

from core import tool_cache as _tool_cache


def _get_rg_executable():
    candidates = []
    # 1. PyInstaller 打包后的临时目录
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(os.path.join(meipass, 'vendor', 'ripgrep', 'rg.exe'))
    candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'vendor', 'ripgrep', 'rg.exe'))
    candidates.append(os.path.join(os.getcwd(), 'vendor', 'ripgrep', 'rg.exe'))

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return 'rg'


_DEFINITION_PATTERNS = [
    (r'\bdef\s+(\w+)\s*\(', 'python'),
    (r'\bclass\s+(\w+)\s*[\(:]', 'python'),
    (r'\basync\s+def\s+(\w+)\s*\(', 'python'),
    (r'\bfunction\s+(\w+)\s*\(', 'javascript'),
    (r'\bclass\s+(\w+)\s*\{', 'javascript'),
    (r'\bconst\s+(\w+)\s*=\s*(?:async\s*)?\(', 'javascript'),
    (r'\bconst\s+(\w+)\s*=\s*function', 'javascript'),
    (r'\bexport\s+(?:default\s+)?function\s+(\w+)', 'javascript'),
    (r'\bexport\s+class\s+(\w+)', 'javascript'),
    (r'\bfunc\s+(?:\([^)]*\)\s+)?(\w+)\s*\(', 'go'),
    (r'\btype\s+(\w+)\s+(?:struct|interface)', 'go'),
    (r'\bfn\s+(\w+)\s*[<(]', 'rust'),
    (r'\bstruct\s+(\w+)\s*[\{<]', 'rust'),
    (r'\bimpl\s+(\w+)', 'rust'),
    (r'\btrait\s+(\w+)', 'rust'),
    (r'\b(?:public|private|protected|static|final|virtual)?\s*(?:class|struct)\s+(\w+)', 'java'),
    (r'\b(?:public|private|protected|static)?\s*\w+\s+(\w+)\s*\([^)]*\)\s*\{', 'java'),
]


def find_definition(symbol, dir_path='.', file_extensions=None):
    try:
        from core.tools import _resolve_workspace_path
        dir_path = str(_resolve_workspace_path(dir_path))
    except Exception as e:
        return f"🚨 查找失败: {e}"

    if not symbol or not symbol.replace('_', '').isalnum():
        return f"🚨 符号名只能包含字母、数字、下划线：{symbol!r}"
    # 符号名会被逐条拼进所有语言定义正则并合并为一个大的 alternation，
    # 限制长度避免构造巨型正则拖垮 ripgrep 编译/搜索
    if len(symbol) > 128:
        return f"🚨 符号名过长（{len(symbol)} 字符，上限 128）"

    cache_key = _tool_cache.search_key("find_definition", symbol, dir_path, tuple(file_extensions) if file_extensions else None)
    cached = _tool_cache.search_get(cache_key)
    if cached is not None:
        return cached

    session_id = _tool_cache.get_current_session()
    if session_id:
        shared_key = ("find_definition", cache_key)
        shared = _tool_cache.shared_get(session_id, shared_key)
        if shared is not None:
            return shared

    patterns = []
    for pattern, lang in _DEFINITION_PATTERNS:
        specialized = pattern.replace(r'(\w+)', re.escape(symbol))
        patterns.append((specialized, lang))

    combined = '|'.join(f'(?:{p})' for p, _ in patterns)

    glob_args = []
    if file_extensions:
        for ext in file_extensions:
            if not ext.startswith('.'):
                ext = '.' + ext
            glob_args.extend(['-g', f'*{ext}'])

    try:
        rg_bin = _get_rg_executable()
        cmd = [
            rg_bin, '-n', '--no-heading', '--max-columns', '200',
            '-e', combined,
            dir_path,
        ] + glob_args
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode not in (0, 1):
            return f"🚨 ripgrep 执行失败（退出码 {result.returncode}）"
        output = result.stdout.strip()
        if not output:
            return f"未找到符号 {symbol!r} 的定义。可尝试用 search_code 搜索所有出现位置。"
        # 限制返回行数避免过长
        lines = output.split('\n')
        if len(lines) > 30:
            result_text = '\n'.join(lines[:30]) + f"\n... (共 {len(lines)} 处匹配，已显示前 30 处)"
        else:
            result_text = output
        _tool_cache.search_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("find_definition", cache_key), result_text)
        return result_text
    except FileNotFoundError:
        # ripgrep 未安装，回退到 Python os.walk + 正则
        result_text = _find_definition_fallback(symbol, dir_path, patterns, file_extensions=file_extensions)
        _tool_cache.search_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("find_definition", cache_key), result_text)
        return result_text
    except subprocess.TimeoutExpired:
        return f"🚨 查找超时"
    except Exception as e:
        return f"🚨 查找异常: {e}"


def _find_definition_fallback(symbol, dir_path, patterns, file_extensions=None, timeout_seconds=30):
    ignore_dirs = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode', 'build', 'dist', 'target', 'out', '.gradle', 'bin', 'obj'}
    results = []
    scanned_files = 0
    max_files_to_scan = 3000
    deadline = time.monotonic() + timeout_seconds
    max_file_size = 2 * 1024 * 1024

    # 未指定扩展名时，默认只扫描常见源码扩展名，避免在大仓库扫描所有文件
    if file_extensions:
        allowed_exts = {ext if ext.startswith('.') else '.' + ext for ext in file_extensions}
    else:
        allowed_exts = {'.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.c', '.cpp', '.h', '.hpp', '.cs', '.swift', '.kt', '.scala', '.php', '.rb'}

    install_hint = (
        "\n提示：安装 ripgrep 可大幅加速符号查找。Windows 安装方式："
        "1) 下载 https://github.com/BurntSushi/ripgrep/releases 的 x64 zip；"
        "2) 将 rg.exe 所在目录加入系统 PATH；"
        "3) 重启 ycode。"
    )

    try:
        for root, dirs, files in os.walk(dir_path):
            # 超时保护：避免在大项目上卡住几十分钟
            if time.monotonic() > deadline:
                if results:
                    return '\n'.join(results) + f"\n... (查找超时，仅返回已找到的 {len(results)} 处；已扫描 {scanned_files} 个文件){install_hint}"
                return f"🚨 查找超时（{timeout_seconds}s）：目录过大或 ripgrep 未安装，建议安装 ripgrep 后重试。{install_hint}"
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
            for fname in files:
                if fname.startswith('.'):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext not in allowed_exts:
                    continue
                fpath = os.path.join(root, fname)
                scanned_files += 1
                if scanned_files > max_files_to_scan:
                    if results:
                        return '\n'.join(results) + f"\n... (已扫描文件数达到上限 {max_files_to_scan}，仅返回已找到的 {len(results)} 处){install_hint}"
                    return f"🚨 查找范围过大（已扫描 {max_files_to_scan} 个文件仍未找到）：建议安装 ripgrep 加速，或缩小 dir_path 范围。{install_hint}"
                try:
                    st = os.stat(fpath)
                    if not st.st_size or st.st_size > max_file_size:
                        continue
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        for lineno, line in enumerate(f, 1):
                            if len(line) > 2000:
                                continue
                            for pattern, _ in patterns:
                                if re.search(pattern, line):
                                    rel = os.path.relpath(fpath, dir_path)
                                    results.append(f"{rel}:{lineno}:{line.rstrip()[:200]}")
                                    break
                            if len(results) >= 30:
                                return '\n'.join(results) + f"\n... (限 30 处)"
                except (OSError, UnicodeDecodeError):
                    continue
        if not results:
            return f"未找到符号 {symbol!r} 的定义。"
        return '\n'.join(results)
    except Exception as e:
        return f"🚨 回退查找异常: {e}"


def get_supported_languages():
    """返回当前支持的语言列表。"""
    langs = set()
    for _, lang in _DEFINITION_PATTERNS:
        langs.add(lang)
    return sorted(langs)


_OUTLINE_IGNORE_DIRS = frozenset({
    '.git', '.hg', '.svn', '.venv', 'venv', 'node_modules', '__pycache__',
    '.idea', '.vscode', 'build', 'dist', 'target', 'out', '.gradle', 'bin', 'obj',
})

_OUTLINE_LANG_BY_EXT = {
    '.py': 'python',
    '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
    '.ts': 'javascript', '.tsx': 'javascript',
    '.go': 'go',
    '.rs': 'rust',
    '.java': 'java', '.c': 'java', '.cpp': 'java', '.cc': 'java',
    '.h': 'java', '.hpp': 'java', '.cs': 'java',
}


def _python_ast_outline(text: str) -> list[str] | None:
    import ast

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    def _fmt_args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        names = [a.arg for a in getattr(fn.args, "posonlyargs", ())]
        names.extend(a.arg for a in fn.args.args)
        if fn.args.vararg:
            names.append("*" + fn.args.vararg.arg)
        names.extend(a.arg for a in fn.args.kwonlyargs)
        if fn.args.kwarg:
            names.append("**" + fn.args.kwarg.arg)
        joined = ", ".join(names)
        if len(joined) > 60:
            joined = joined[:57] + "..."
        return joined

    lines: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            end_ln = getattr(node, "end_lineno", node.lineno)
            lines.append(f"L{node.lineno}-{end_ln}: class {node.name}")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                    c_end = getattr(child, "end_lineno", child.lineno)
                    lines.append(
                        f"  L{child.lineno}-{c_end}: {prefix} {child.name}({_fmt_args(child)})"
                    )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            end_ln = getattr(node, "end_lineno", node.lineno)
            lines.append(
                f"L{node.lineno}-{end_ln}: {prefix} {node.name}({_fmt_args(node)})"
            )
    return lines


def get_outline(file_path):
    try:
        from core.tools import _resolve_workspace_path

        resolved = _resolve_workspace_path(file_path)
    except Exception as e:
        return f"🚨 提取文件大纲失败: {e}"

    if not resolved.exists():
        return f"🚨 文件不存在：{file_path}"
    if resolved.is_dir():
        return f"'{file_path}' 是一个目录，请改用 list_directory。"

    try:
        file_mtime = os.path.getmtime(resolved)
    except OSError:
        file_mtime = 0.0
    cache_key = _tool_cache.search_key("get_outline", str(resolved), str(resolved.parent), (file_mtime,))
    cached = _tool_cache.search_get(cache_key)
    if cached is not None:
        return cached

    try:
        raw = resolved.read_bytes()
    except OSError as e:
        return f"🚨 无法读取文件 {file_path}：{e}"
    if b"\x00" in raw[:8192] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return f"🚨 二进制文件不支持大纲提取：{file_path}"

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gbk", errors="replace")

    ext = resolved.suffix.lower()
    outline_lines: list[str] | None = None
    if ext == ".py":
        outline_lines = _python_ast_outline(text)

    if outline_lines is None:
        lang = _OUTLINE_LANG_BY_EXT.get(ext)
        lang_patterns = [
            re.compile(pat)
            for pat, p_lang in _DEFINITION_PATTERNS
            if lang is None or p_lang == lang
        ]
        outline_lines = []
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped or len(stripped) > 300:
                continue
            if any(rx.search(line) for rx in lang_patterns):
                outline_lines.append(f"L{lineno}: {stripped[:120]}")

    if not outline_lines:
        result_text = f"文件 {file_path}（共 {len(text.splitlines())} 行）未识别出顶层类或函数定义。"
    else:
        max_items = 200
        total_lines = len(text.splitlines())
        body = outline_lines[:max_items]
        if len(outline_lines) > max_items:
            body.append(f"... (共 {len(outline_lines)} 项，已截断前 {max_items} 项)")
        result_text = f"📑 {file_path}（共 {total_lines} 行，{len(outline_lines)} 项结构定义）：\n" + "\n".join(body)

    _tool_cache.search_put(cache_key, result_text)
    session_id = _tool_cache.get_current_session()
    if session_id:
        _tool_cache.shared_put(session_id, ("get_outline", cache_key), result_text)
    return result_text


def find_references(symbol, dir_path='.', file_extensions=None):
    """查找符号在代码库中的全部引用位置（按整词边界匹配，含调用、导入与赋值）。"""
    try:
        from core.tools import _resolve_workspace_path

        dir_path = str(_resolve_workspace_path(dir_path))
    except Exception as e:
        return f"🚨 查找引用失败: {e}"

    if not symbol or not str(symbol).replace('_', '').isalnum():
        return f"🚨 符号名只能包含字母、数字、下划线：{symbol!r}"
    if len(symbol) > 128:
        return f"🚨 符号名过长（{len(symbol)} 字符，上限 128）"

    ext_tuple = tuple(file_extensions) if file_extensions else None
    cache_key = _tool_cache.search_key("find_references", symbol, dir_path, ext_tuple)
    cached = _tool_cache.search_get(cache_key)
    if cached is not None:
        return cached

    session_id = _tool_cache.get_current_session()
    if session_id:
        shared = _tool_cache.shared_get(session_id, ("find_references", cache_key))
        if shared is not None:
            return shared

    glob_args: list[str] = []
    for ignored in sorted(_OUTLINE_IGNORE_DIRS):
        glob_args.extend(['--glob', f'!**/{ignored}/'])
    if file_extensions:
        for ext in file_extensions:
            norm = ext if str(ext).startswith('.') else '.' + str(ext)
            glob_args.extend(['-g', f'*{norm}'])

    max_refs = 60
    try:
        rg_bin = _get_rg_executable()
        cmd = [
            rg_bin, '-n', '--no-heading', '-w', '--max-columns', '200',
            '-F', symbol, dir_path,
        ] + glob_args
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
        if result.returncode not in (0, 1):
            return f"🚨 查找引用失败（ripgrep 退出码 {result.returncode}）"
        raw_out = result.stdout.strip()
        if not raw_out:
            result_text = f"未找到符号 {symbol!r} 的引用。"
        else:
            lines = raw_out.split('\n')
            if len(lines) > max_refs:
                result_text = '\n'.join(lines[:max_refs]) + f"\n... (共 {len(lines)} 处引用，已显示前 {max_refs} 处)"
            else:
                result_text = raw_out
        _tool_cache.search_put(cache_key, result_text)
        if session_id:
            _tool_cache.shared_put(session_id, ("find_references", cache_key), result_text)
        return result_text
    except FileNotFoundError:
        pass
    except subprocess.TimeoutExpired:
        return "🚨 查找引用超时（15 秒），请缩小 dir_path 范围或指定 file_extensions 重试。"
    except Exception as e:
        return f"🚨 查找引用异常: {e}"

    # Python 回退扫描（按整词边界匹配）
    word_re = re.compile(rf'\b{re.escape(symbol)}\b')
    allowed_exts = (
        {e if str(e).startswith('.') else '.' + str(e) for e in file_extensions}
        if file_extensions
        else {'.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.php'}
    )
    results: list[str] = []
    scanned = 0
    deadline = time.monotonic() + 20
    try:
        for root, dirs, files in os.walk(dir_path):
            if time.monotonic() > deadline:
                break
            dirs[:] = [d for d in dirs if d not in _OUTLINE_IGNORE_DIRS and not d.startswith('.')]
            for fname in files:
                if fname.startswith('.') or os.path.splitext(fname)[1].lower() not in allowed_exts:
                    continue
                scanned += 1
                if scanned > 3000:
                    break
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getsize(fpath) > 2 * 1024 * 1024:
                        continue
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as fh:
                        for lineno, line in enumerate(fh, 1):
                            if len(line) <= 2000 and word_re.search(line):
                                rel = os.path.relpath(fpath, dir_path)
                                results.append(f"{rel}:{lineno}:{line.rstrip()[:200]}")
                                if len(results) >= max_refs:
                                    result_text = '\n'.join(results) + f"\n... (限 {max_refs} 处引用)"
                                    _tool_cache.search_put(cache_key, result_text)
                                    return result_text
                except OSError:
                    continue
            if scanned > 3000:
                break
    except Exception as e:
        return f"🚨 查找引用失败: {e}"

    result_text = '\n'.join(results) if results else f"未找到符号 {symbol!r} 的引用。"
    _tool_cache.search_put(cache_key, result_text)
    if session_id:
        _tool_cache.shared_put(session_id, ("find_references", cache_key), result_text)
    return result_text
