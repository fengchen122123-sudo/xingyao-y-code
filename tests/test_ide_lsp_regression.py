# -*- coding: utf-8 -*-
"""IDE 语言智能的回归红线守卫。

红线一（设计规格 §7.3）：IDE 模块不得触碰提示词构造与缓存前缀，
IDE 对模型请求路径零影响。

红线二（设计规格 §7.2）：IDE 模块不得直接操作 WebView 通道，
前端可见事件只能经 gui_bridge.emit_event 进 _js_queue 与 dispatcher 线程。

红线三（AGENTS.md 依赖条款）：core/ide 只允许标准库与项目内模块，
语言服务器一律是外部可执行文件，不引入 Python 第三方依赖。

用 AST 而不是关键字扫描：注释与文档字符串里必然会提到 evaluate_js /
prompt 这类词，字符串匹配会把正常的解释性注释判成违规。
"""
import ast
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDE_DIR = os.path.join(REPO_ROOT, "core", "ide")

# 项目内模块前缀：这些不算第三方依赖
PROJECT_MODULES = {"core", "config", "gui_bridge", "ui", "tools", "agents", "harness"}

FORBIDDEN_NAMES = {
    "build_stable_prompt",
    "build_prompt",
    "PROMPT_TEMPLATE",
    "_js_queue",
}

FORBIDDEN_ATTRS = {
    "evaluate_js",
}

STDLIB_NAMES = set(getattr(sys, "stdlib_module_names", ()))


def _ide_sources():
    names = sorted(n for n in os.listdir(IDE_DIR) if n.endswith(".py"))
    assert names, "core/ide 下没有任何模块，测试本身失去意义"
    for name in names:
        path = os.path.join(IDE_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            yield path, fh.read()


def _iter_violations(source, filename):
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            yield (filename, node.lineno, f"引用了禁用符号 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            yield (filename, node.lineno, f"调用了禁用属性 {node.attr}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield from _module_violation(filename, node.lineno, node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield from _module_violation(filename, node.lineno, alias.name)


def _module_violation(filename, lineno, module):
    if module == "__future__":
        return
    root = module.split(".")[0]
    if root in PROJECT_MODULES or root in STDLIB_NAMES:
        return
    yield (filename, lineno, f"引入了非标准库依赖 {module}")


def test_ide_modules_do_not_touch_prompt_or_webview_channel():
    violations = []
    for path, source in _ide_sources():
        name = os.path.join("core", "ide", os.path.basename(path))
        violations.extend(_iter_violations(source, name))
    assert violations == [], "\n".join(f"{f}:{ln} {msg}" for f, ln, msg in violations)


def test_core_ide_only_imports_stdlib_and_project_modules():
    assert STDLIB_NAMES, "需要 Python 3.10+ 的 sys.stdlib_module_names 才能做依赖白名单检查"
    offenders = []
    for path, source in _ide_sources():
        filename = os.path.basename(path)
        for node in ast.walk(ast.parse(source, filename=filename)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules = [node.module]
            for module in modules:
                if module == "__future__":
                    continue
                root = module.split(".")[0]
                if root in PROJECT_MODULES or root in STDLIB_NAMES:
                    continue
                offenders.append(f"core/ide/{filename}: {module}")
    assert offenders == []


def test_importing_core_ide_does_not_pull_prompt_modules():
    code = (
        "import sys\n"
        "import core.ide.language_registry\n"
        "import core.ide.server_provision\n"
        "import core.ide.lsp_codec\n"
        "import core.ide.lsp_manager\n"
        "import core.ide.diagnostics_batcher\n"
        "import core.ide.file_service\n"
        "import core.ide.layout_store\n"
        "bad = sorted(m for m in sys.modules if 'prompt' in m.lower())\n"
        "print('|'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "", f"导入 core.ide 时加载了提示词模块：{result.stdout.strip()}"


def test_prompt_byte_stability_still_passes():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_prompt_byte_stability.py", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
