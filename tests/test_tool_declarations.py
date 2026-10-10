"""工具声明防回归（T9）：全量注册表 side_effect_scope 契约。

阶段 A（提交 c955e80）补齐了 31 个内置工具与插件的 side_effect_scope；
本文件把当时确立的契约固化为常驻用例，任何新增工具 / 插件漏声明或
错声明都会在这里失败。

规则以 A 阶段落地后的现状为准（03 规格 §3.2 取值表 + 实际归类）：
1. 任何已注册工具（含插件）的 scope 非空且属于合法集合；
2. read_only=True ⇒ scope ∈ {none, network}——纯本地只读为 none，
   只读网络访问（fetch_url / search_news）为 network；只读工具不得声明
   workspace / git / system 这类写类副作用；
3. mutates_files=True ⇒ scope ∈ {workspace, git, network, system}——会写
   文件的工具必须声明实际副作用范围（下载落盘类为 network，如
   fetch_image / 生图插件），不得标为"无副作用"（none）。

注：本计划字面稿写的是「read_only ⇒ none」「mutates_files ⇒ {workspace,
git}」，与既有归类（fetch_url 只读但 network；fetch_image 落盘但 network）
不符，按复核现状修正为上述两条。
"""

import importlib.util
import os

from core.tool_registry import ToolRegistry, build_default_registry

_VALID_SCOPES = {"none", "workspace", "git", "network", "system"}
_READ_ONLY_SCOPES = {"none", "network"}
_WRITE_SCOPES = {"workspace", "git", "network", "system"}

# 防"注册表意外为空导致断言真空"的锚点
_BUILTIN_ANCHORS = (
    "read_file", "write_file", "edit_file", "execute_command", "fetch_image",
)


def _assert_declaration_consistent(spec) -> None:
    assert spec.side_effect_scope, f"{spec.name} 的 side_effect_scope 不得为空"
    assert spec.side_effect_scope in _VALID_SCOPES, (
        f"{spec.name} 的 side_effect_scope 非法: {spec.side_effect_scope!r}"
    )
    if spec.read_only:
        assert spec.side_effect_scope in _READ_ONLY_SCOPES, (
            f"{spec.name} 声明为只读却带写类副作用: {spec.side_effect_scope!r}"
        )
    if spec.mutates_files:
        assert spec.side_effect_scope in _WRITE_SCOPES, (
            f"{spec.name} 会写文件却声明 side_effect_scope={spec.side_effect_scope!r}"
        )


def test_default_registry_declarations_are_consistent():
    """全量注册表（内置 + 当前已加载插件）逐项校验声明契约。"""
    specs = build_default_registry().all()

    assert len(specs) >= 31, f"默认注册表工具数异常: {len(specs)}"
    names = {spec.name for spec in specs}
    assert set(_BUILTIN_ANCHORS) <= names, names

    for spec in specs:
        _assert_declaration_consistent(spec)


def _load_plugin_module_by_path(module_name, plugin_dir):
    """按路径加载插件薄壳（与 plugin_loader 的加载方式一致）。

    直接走 importlib 而不用插件加载器：本测试只验证插件源码里的 ToolSpec
    声明，不应依赖插件启用状态 / 信任状态 / 全局注册表等外部环境。
    """
    entry = os.path.join(plugin_dir, "plugin.py")
    module_spec = importlib.util.spec_from_file_location(module_name, entry)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_shipped_plugins_are_covered_by_the_same_rules():
    """项目自带插件即使未被信任加载，也要满足同一套声明规则。"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_root = os.path.join(project_root, "plugins")

    registry = ToolRegistry()
    for module_name, plugin_name in (
        ("_test_declaration_hunyuan_image", "hunyuan_image"),
        ("_test_declaration_tokenrhythm_image", "tokenrhythm_image"),
    ):
        module = _load_plugin_module_by_path(
            module_name, os.path.join(plugins_root, plugin_name)
        )
        module.register(registry)

    registered = registry.all()
    assert registered, "项目自带插件必须注册至少一个工具"
    for spec in registered:
        _assert_declaration_consistent(spec)


def test_default_registry_is_fully_covered_by_turn_undo_recorder():
    """规格 9.5：默认注册表（内置 + 已加载插件）中不得存在未接入按回合撤销
    记录器的 mutates_files 工具。本断言防同类声明遗漏再现（曾拦截
    opencode_zen_sync 误声明 mutates_files 导致启动链 RuntimeError 的问题）。"""
    from core.turn_undo.service import validate_recorder_coverage

    assert validate_recorder_coverage(build_default_registry()) == []