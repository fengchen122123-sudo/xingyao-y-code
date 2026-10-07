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
CORE_DIR = os.path.join(REPO_ROOT, "core")

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

# 「core 层不得引 webview」的唯一例外（且有且只有它）：
# `core/embedded_browser.py` 是内嵌浏览器功能，它要把原生 WebView2 控件叠加到
# pywebview 主窗体上，必须 import pywebview 自己的工具函数来定位 interop DLL。
# 它与事件通道无关（不碰 evaluate_js，也不碰 _js_queue），是既有的独立特性的
# 硬需求。例外写成一个显式白名单而不是放宽规则：新增任何一处 import webview
# 都会在本测试立刻失败，评审时也一眼能看出例外只有一条。
WEBVIEW_IMPORT_ALLOWLIST = {"core/embedded_browser.py"}

STDLIB_NAMES = set(getattr(sys, "stdlib_module_names", ()))


def _ide_sources():
    names = sorted(n for n in os.listdir(IDE_DIR) if n.endswith(".py"))
    assert names, "core/ide 下没有任何模块，测试本身失去意义"
    for name in names:
        path = os.path.join(IDE_DIR, name)
        with open(path, "r", encoding="utf-8") as fh:
            yield path, fh.read()


def _core_sources():
    """`core/` 全域的 Python 源文件（递归，跳过 __pycache__）。"""
    count = 0
    for root, dirs, files in os.walk(CORE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            count += 1
            path = os.path.join(root, name)
            with open(path, "r", encoding="utf-8") as fh:
                yield path, fh.read()
    assert count > 0, "core/ 下没有任何模块，测试本身失去意义"


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


def test_core_layer_does_not_import_webview():
    """红线二（core 全域版）：除显式例外外，core/ 任何模块都不得引入 webview。

    原先只扫 `core/ide/`，而「core 层不得引 webview」是整层的红线：任何一个
    core 模块直接 import webview，事件就可能绕开 `_js_queue` 从任意线程直写
    WebView。这里按 AST 递归扫 `core/**/*.py`（注释与文档字符串里提到 webview
    不算违规，那正是用 AST 而不是关键字扫描的原因）。

    例外只有 `WEBVIEW_IMPORT_ALLOWLIST` 一条，且例外文件必须真实存在：白名单
    里的路径被删/改名时本测试会失败，避免例外名单变成一堆没人清理的通行证。
    """
    for allowed in WEBVIEW_IMPORT_ALLOWLIST:
        assert os.path.exists(os.path.join(REPO_ROOT, allowed)), f"白名单里的 {allowed} 不存在"

    offenders = []
    seen_allowlisted = 0
    for path, source in _core_sources():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                if module.split(".")[0] != "webview":
                    continue
                if rel in WEBVIEW_IMPORT_ALLOWLIST:
                    seen_allowlisted += 1
                    continue
                offenders.append(f"{rel}:{node.lineno} {module}")
    assert offenders == [], "core 层引用了 webview：\n" + "\n".join(offenders)
    assert seen_allowlisted > 0, "白名单里的例外已经不存在了，请从 WEBVIEW_IMPORT_ALLOWLIST 删掉"


def test_gui_bridge_has_exactly_one_evaluate_js_call_site_in_dispatcher():
    """红线二（静态版）：`gui_bridge.py` 里 `evaluate_js` 只能有唯一调用点，且在 dispatcher 线程内。

    现有保护只有运行时断言（`test_gui_lsp_bridge.py` 检查调用线程名是
    `gui-js-dispatcher`）——它挡不住「有人新增第二个直接调用点」：只有当那条新
    路径真的被测试跑到时才会失败。静态断言把「只有一个调用点」本身钉死，
    新增调用点会在本测试立刻暴露。
    """
    path = os.path.join(REPO_ROOT, "gui_bridge.py")
    with open(path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    calls = []
    dispatcher = "_js_dispatcher_loop"

    def walk(node, func_name):
        for child in ast.iter_child_nodes(node):
            name = func_name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            elif (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "evaluate_js"
            ):
                calls.append((child.lineno, func_name))
            walk(child, name)

    walk(tree, None)

    assert len(calls) == 1, f"gui_bridge.py 中 evaluate_js 调用点必须唯一，实际 {calls}"
    lineno, owner = calls[0]
    assert owner == dispatcher, f"evaluate_js 只能由 {dispatcher} 调用，实际在 {owner}（第 {lineno} 行）"


# --- S1 遗留守卫的 AST 版（原 tests/test_ide_regression.py 已删除）---------
#
# 原实现是**字符串扫描**：注释里提到 `core.lsp` 会误报、`from core import lsp`
# 会漏报，且只覆盖 `file_service` / `layout_store` 两个模块。下面用 AST 覆盖
# 全部 `core/ide` 模块，语义等价或更强。

FORBIDDEN_SYMBOLS = {
    "find_definition",     # core/lsp.py 的工具实现，IDE 侧不得依赖
    "context_builder",
    "system_prompt",
    "llm_transport",
    "llm_native_transport",
    "llm_dialects",
    "turn_controller",     # 模型请求构造链路，IDE 不得出现在其上
}


def _forbidden_reference_violations(source, filename):
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_SYMBOLS:
            yield f"{filename}:{node.lineno} 引用了禁用符号 {node.id}"
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SYMBOLS:
            yield f"{filename}:{node.lineno} 引用了禁用属性 {node.attr}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "core":
                for alias in node.names:
                    if alias.name == "lsp":
                        yield f"{filename}:{node.lineno} 引入了 core.lsp 工具层"
            elif module.split(".")[0] == "core" and module.split(".")[-1] == "lsp":
                yield f"{filename}:{node.lineno} 引入了 core.lsp 工具层"
            for alias in node.names:
                if alias.name in FORBIDDEN_SYMBOLS:
                    yield f"{filename}:{node.lineno} 引入了禁用符号 {alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "core.lsp":
                    yield f"{filename}:{node.lineno} 引入了 core.lsp 工具层"


def test_ide_modules_stay_clear_of_lsp_tool_and_request_chain():
    """IDE 模块不得依赖 `core/lsp.py` 工具层，也不得出现在模型请求构造链路上。

    这条是原 `tests/test_ide_regression.py` 里真正有价值的两条断言，改成 AST
    后覆盖范围从 2 个模块扩到全部 `core/ide/*.py`：注释里提 `core.lsp` 不误报，
    `from core import lsp` 这种写法也拦得住。
    """
    violations = []
    for path, source in _ide_sources():
        name = os.path.join("core", "ide", os.path.basename(path))
        violations.extend(_forbidden_reference_violations(source, name))
    assert violations == [], "\n".join(violations)


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
