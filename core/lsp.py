"""轻量级代码符号定位模块（LSP-lite）。

设计目标：
- 不依赖外部 LSP server（零安装成本，跨语言通用）
- 提供基本的符号定义查找能力，比 search_code 更精确
- 留 LSP server 集成接口给未来扩展（pyright/tsserver/rust-analyzer 等）

参考：
- OpenCode 的 LSP 集成自动加载对应语言 LSP
- Grok Build 的 LSP 信任与 workspace 绑定

当前实现：基于正则的多语言符号定义查找
未来扩展：可接入真实 LSP server，提供 hover/go-to-definition/references/rename
"""

import os
import re
import subprocess
from pathlib import Path


# 多语言符号定义模式（按语言关键字）
# 每条模式匹配"关键字 + 符号名"，捕获组 1 是符号名
_DEFINITION_PATTERNS = [
    # Python
    (r'\bdef\s+(\w+)\s*\(', 'python'),
    (r'\bclass\s+(\w+)\s*[\(:]', 'python'),
    (r'\basync\s+def\s+(\w+)\s*\(', 'python'),
    # JavaScript/TypeScript
    (r'\bfunction\s+(\w+)\s*\(', 'javascript'),
    (r'\bclass\s+(\w+)\s*\{', 'javascript'),
    (r'\bconst\s+(\w+)\s*=\s*(?:async\s*)?\(', 'javascript'),
    (r'\bconst\s+(\w+)\s*=\s*function', 'javascript'),
    (r'\bexport\s+(?:default\s+)?function\s+(\w+)', 'javascript'),
    (r'\bexport\s+class\s+(\w+)', 'javascript'),
    # Go
    (r'\bfunc\s+(?:\([^)]*\)\s+)?(\w+)\s*\(', 'go'),
    (r'\btype\s+(\w+)\s+(?:struct|interface)', 'go'),
    # Rust
    (r'\bfn\s+(\w+)\s*[<(]', 'rust'),
    (r'\bstruct\s+(\w+)\s*[\{<]', 'rust'),
    (r'\bimpl\s+(\w+)', 'rust'),
    (r'\btrait\s+(\w+)', 'rust'),
    # Java/C/C++/C#
    (r'\b(?:public|private|protected|static|final|virtual)?\s*(?:class|struct)\s+(\w+)', 'java'),
    (r'\b(?:public|private|protected|static)?\s*\w+\s+(\w+)\s*\([^)]*\)\s*\{', 'java'),
]


def find_definition(symbol, dir_path='.', file_extensions=None):
    """查找符号的定义位置（跨文件）。

    比单纯的 search_code "symbol" 更精确：
    - 只匹配定义位置（def/class/function/func 等），不匹配调用或字符串字面量
    - 支持多语言
    - 返回 file:line:col 格式，便于 LLM 直接 read_file 跳转

    参数：
    - symbol: 要查找的符号名（函数名/类名/方法名）
    - dir_path: 搜索根目录（默认当前工作目录）
    - file_extensions: 限定文件扩展名列表（如 ['.py', '.ts']），None 则全语言

    返回：找到的定义列表，每行 "file:line: 函数定义"
    """
    try:
        from core.tools import _resolve_workspace_path
        dir_path = str(_resolve_workspace_path(dir_path))
    except Exception as e:
        return f"🚨 查找失败: {e}"

    if not symbol or not symbol.replace('_', '').isalnum():
        return f"🚨 符号名只能包含字母、数字、下划线：{symbol!r}"

    # 构造 ripgrep 正则：匹配任何语言的定义关键字 + 指定符号名
    # 用 word boundary 确保精确匹配
    patterns = []
    for pattern, lang in _DEFINITION_PATTERNS:
        # 把 pattern 中的 (\w+) 替换为指定 symbol
        specialized = pattern.replace(r'(\w+)', re.escape(symbol))
        patterns.append((specialized, lang))

    # 合并为一个大的 alternation（性能更好）
    combined = '|'.join(f'(?:{p})' for p, _ in patterns)

    # 文件扩展名过滤
    glob_args = []
    if file_extensions:
        for ext in file_extensions:
            if not ext.startswith('.'):
                ext = '.' + ext
            glob_args.extend(['-g', f'*{ext}'])

    try:
        cmd = [
            'rg', '-n', '--no-heading', '--max-columns', '200',
            '-e', combined,
            dir_path,
        ] + glob_args
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=15,
        )
        if result.returncode not in (0, 1):
            return f"🚨 ripgrep 执行失败（退出码 {result.returncode}）"
        output = result.stdout.strip()
        if not output:
            return f"未找到符号 {symbol!r} 的定义。可尝试用 search_code 搜索所有出现位置。"
        # 限制返回行数避免过长
        lines = output.split('\n')
        if len(lines) > 30:
            return '\n'.join(lines[:30]) + f"\n... (共 {len(lines)} 处匹配，已显示前 30 处)"
        return output
    except FileNotFoundError:
        # ripgrep 未安装，回退到 Python os.walk + 正则
        return _find_definition_fallback(symbol, dir_path, patterns)
    except subprocess.TimeoutExpired:
        return f"🚨 查找超时"
    except Exception as e:
        return f"🚨 查找异常: {e}"


def _find_definition_fallback(symbol, dir_path, patterns):
    """无 ripgrep 时的回退实现：Python os.walk + 正则匹配。"""
    ignore_dirs = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.idea', '.vscode', 'build', 'dist'}
    results = []
    try:
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith('.')]
            for fname in files:
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                        for lineno, line in enumerate(f, 1):
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
