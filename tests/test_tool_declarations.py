import importlib.util
import os

from core.tool_registry import ToolRegistry, build_default_registry

_VALID_SCOPES = {"none", "workspace", "git", "network", "system"}
_READ_ONLY_SCOPES = {"none", "network"}
_WRITE_SCOPES = {"workspace", "git", "network", "system"}

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
    specs = build_default_registry().all()

    assert len(specs) >= 31, f"默认注册表工具数异常: {len(specs)}"
    names = {spec.name for spec in specs}
    assert set(_BUILTIN_ANCHORS) <= names, names

    for spec in specs:
        _assert_declaration_consistent(spec)


def _load_plugin_module_by_path(module_name, plugin_dir):
    entry = os.path.join(plugin_dir, "plugin.py")
    module_spec = importlib.util.spec_from_file_location(module_name, entry)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_shipped_plugins_are_covered_by_the_same_rules():
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
    from core.turn_undo.service import validate_recorder_coverage

    assert validate_recorder_coverage(build_default_registry()) == []
