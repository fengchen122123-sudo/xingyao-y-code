import json
import threading
import time
from pathlib import Path

import pytest

from core import plugin_loader
from core.tool_registry import ToolRegistry

def _make_plugin(plugin_dir, name, body, enabled=True, entry="plugin.py"):
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.json").write_text(
        json.dumps({
            "name": name,
            "version": "1.0.0",
            "entry": entry,
            "enabled": enabled,
        }),
        encoding="utf-8",
    )
    (plugin_dir / entry).write_text(body, encoding="utf-8")


_GOOD_PLUGIN = """
from core.tool_protocol import ToolSpec

def register(registry):
    registry.register(ToolSpec(
        name="demo_tool",
        description="demo",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        execute=lambda x="": f"hello {x}",
        read_only=True,
        requires_permission=False,
    ))
"""

_BROKEN_PLUGIN = """
raise RuntimeError("插件加载即崩溃")
"""

_NO_REGISTER_PLUGIN = """
X = 1
"""

_SYSEXIT_PLUGIN = """
import sys
sys.exit(9)
"""

_KEYBOARD_INTERRUPT_PLUGIN = """
raise KeyboardInterrupt("插件要求中断")
"""

_CLEANUP_SYSEXIT_PLUGIN = """
import sys

from core.tool_protocol import ToolSpec


def register(registry):
    registry.register(ToolSpec(
        name="cleanup_demo_tool",
        description="demo",
        input_schema={"type": "object", "properties": {}},
        execute=lambda: "ok",
        read_only=True,
        requires_permission=False,
    ))


def cleanup():
    sys.exit(3)
"""


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    user_dir = tmp_path / "user_plugins"
    builtin_dir = tmp_path / "builtin_plugins"
    user_dir.mkdir()
    builtin_dir.mkdir()
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [user_dir, builtin_dir])
    return user_dir, builtin_dir


def test_load_plugin_registers_tool(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == ["demo"]
    result = registry.execute("demo_tool", x="world")
    assert not result.is_error
    assert result.content == "hello world"


def test_broken_plugin_is_skipped_without_crashing(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "broken", "broken", _BROKEN_PLUGIN)
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == ["demo"]
    assert registry.execute("demo_tool", x="ok").content == "hello ok"


def test_sysexit_plugin_is_skipped_without_crashing(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "aa_exiter", "aa_exiter", _SYSEXIT_PLUGIN)
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == ["demo"]
    assert registry.execute("demo_tool", x="ok").content == "hello ok"
    infos = {i["name"]: i for i in plugin_loader.get_plugin_infos()}
    assert infos["aa_exiter"]["status"] == "failed"
    assert "9" in infos["aa_exiter"]["error"]


def test_sysexit_plugin_during_refresh_is_skipped(plugin_env):
    user_dir, _ = plugin_env
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    _make_plugin(user_dir / "aa_exiter", "aa_exiter", _SYSEXIT_PLUGIN)
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    plugin_loader.refresh_plugins(registry)

    infos = {i["name"]: i for i in plugin_loader.get_plugin_infos()}
    assert infos["aa_exiter"]["status"] == "failed"
    assert registry.execute("demo_tool", x="hot").content == "hello hot"


def test_keyboard_interrupt_from_plugin_propagates(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "ki", "ki", _KEYBOARD_INTERRUPT_PLUGIN)

    registry = ToolRegistry()
    with pytest.raises(KeyboardInterrupt):
        plugin_loader.load_and_register(registry)


def test_toggle_enable_sysexit_plugin_reports_hot_failure(plugin_env, monkeypatch):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "exiter", "exiter", _SYSEXIT_PLUGIN, enabled=False)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)

    ok, msg = plugin_loader.toggle_plugin("exiter", True)

    assert ok is True
    assert "热切换失败" in msg
    assert "重启后生效" in msg


def test_toggle_disable_cleanup_sysexit_does_not_fail_disable(plugin_env, monkeypatch):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "c", "c", _CLEANUP_SYSEXIT_PLUGIN)
    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["c"]
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)

    ok, msg = plugin_loader.toggle_plugin("c", False)

    assert ok is True
    assert "清理失败" in msg


def test_plugin_without_register_function_is_skipped(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "noreg", "noreg", _NO_REGISTER_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == []


def test_disabled_plugin_is_skipped(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == []
    assert registry.execute("demo_tool").is_error


def test_user_plugin_overrides_builtin_with_same_name(plugin_env):
    user_dir, builtin_dir = plugin_env
    _make_plugin(user_dir / "same", "same", _GOOD_PLUGIN)
    _make_plugin(builtin_dir / "same", "same", _BROKEN_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == ["same"]
    assert registry.execute("demo_tool", x="u").content == "hello u"


def test_missing_plugin_json_is_ignored(plugin_env, tmp_path):
    user_dir, _ = plugin_env
    stray = user_dir / "stray"
    stray.mkdir()
    (stray / "plugin.py").write_text(_GOOD_PLUGIN, encoding="utf-8")

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == []


def test_import_sibling_loads_module_with_unique_name(plugin_env):
    user_dir, _ = plugin_env
    plugin_dir = user_dir / "sib"
    plugin_dir.mkdir()
    (plugin_dir / "helper.py").write_text("VALUE = 42\n", encoding="utf-8")

    module = plugin_loader.import_sibling(str(plugin_dir / "plugin.py"), "helper")
    assert module.VALUE == 42
    again = plugin_loader.import_sibling(str(plugin_dir / "plugin.py"), "helper")
    assert again is module


def test_import_sibling_missing_module_raises(plugin_env):
    user_dir, _ = plugin_env
    plugin_dir = user_dir / "sib2"
    plugin_dir.mkdir()

    with pytest.raises(ImportError):
        plugin_loader.import_sibling(str(plugin_dir / "plugin.py"), "nope")


def test_plugin_infos_record_loaded_plugin_with_tools(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    info = infos[0]
    assert info["name"] == "demo"
    assert info["version"] == "1.0.0"
    assert info["source"] == "user"
    assert info["status"] == "loaded"
    assert info["tools"] == ["demo_tool"]
    assert info["path"].endswith("demo")


def test_plugin_infos_record_builtin_source(plugin_env):
    _, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    assert [i["source"] for i in infos] == ["builtin"]


def test_plugin_infos_record_disabled(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert infos[0]["status"] == "disabled"
    assert infos[0]["tools"] == []


def test_plugin_infos_record_failed_with_error(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "broken", "broken", _BROKEN_PLUGIN)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert infos[0]["status"] == "failed"
    assert "插件加载即崩溃" in infos[0]["error"]


def test_plugin_infos_record_overridden_builtin(plugin_env):
    user_dir, builtin_dir = plugin_env
    _make_plugin(user_dir / "same", "same", _GOOD_PLUGIN)
    _make_plugin(builtin_dir / "same", "same", _GOOD_PLUGIN)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 2
    by_source = {i["source"]: i for i in infos}
    assert by_source["user"]["status"] == "loaded"
    assert by_source["builtin"]["status"] == "overridden"


def test_get_plugin_infos_returns_copies(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    infos = plugin_loader.get_plugin_infos()
    infos[0]["name"] = "tampered"
    assert plugin_loader.get_plugin_infos()[0]["name"] == "demo"


def test_refresh_plugins_discovers_newly_installed(plugin_env):
    user_dir, _ = plugin_env
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    assert plugin_loader.get_plugin_infos() == []

    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert [i["name"] for i in infos] == ["demo"]
    assert infos[0]["status"] == "loaded"
    assert infos[0]["tools"] == ["demo_tool"]
    assert registry.execute("demo_tool", x="hot").content == "hello hot"


def test_refresh_plugins_does_not_reload_existing(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert registry.execute("demo_tool", x="once").content == "hello once"


def test_refresh_plugins_removes_deleted(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    (user_dir / "demo" / "plugin.json").unlink()
    plugin_loader.refresh_plugins(registry)

    assert plugin_loader.get_plugin_infos() == []


def test_refresh_plugins_records_disabled_new_plugin(plugin_env):
    user_dir, _ = plugin_env
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)
    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert infos[0]["status"] == "disabled"
    assert registry.execute("demo_tool").is_error


def test_builtin_plugin_loads(plugin_env):
    _, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]
    assert registry.execute("demo_tool", x="bi").content == "hello bi"

def test_user_plugin_loads_without_trust_confirmation(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]
    assert plugin_loader.get_plugin_infos()[0]["status"] == "loaded"
    assert registry.execute("demo_tool", x="hi").content == "hello hi"


def test_disabled_plugin_is_not_loaded(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == []
    assert plugin_loader.get_plugin_infos()[0]["status"] == "disabled"


def test_toggle_enable_loads_plugin_directly(plugin_env, monkeypatch):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)

    ok, _ = plugin_loader.toggle_plugin("off", True)

    assert ok is True
    assert registry.execute("demo_tool", x="go").content == "hello go"


def test_set_trust_prompt_is_a_noop(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    plugin_loader.set_trust_prompt(lambda *_a: False)
    try:
        assert plugin_loader.load_and_register(ToolRegistry()) == ["demo"]
    finally:
        plugin_loader.set_trust_prompt(None)


def test_trust_plugin_is_a_compat_noop(plugin_env):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    ok, msg = plugin_loader.trust_plugin("demo")
    assert ok is True
    assert "无需确认" in msg

    ok2, _ = plugin_loader.trust_plugin("")
    assert ok2 is False


def test_plugin_code_change_reloads_without_confirmation(plugin_env):
    user_dir, _ = plugin_env
    plugin_dir = user_dir / "demo"
    _make_plugin(plugin_dir, "demo", _GOOD_PLUGIN)
    assert plugin_loader.load_and_register(ToolRegistry()) == ["demo"]

    (plugin_dir / "plugin.py").write_text(_GOOD_PLUGIN + "\nX = 1\n", encoding="utf-8")

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]
    assert plugin_loader.get_plugin_infos()[0]["status"] == "loaded"


def test_core_managed_markers_include_tokenrhythm_account_provider():
    from core.tokenrhythm_auth import MANAGED_PROVIDER_VALUE

    assert MANAGED_PROVIDER_VALUE in plugin_loader._core_managed_markers()


def test_prune_orphan_providers_keeps_account_provider(monkeypatch):
    import config as config_module
    import core.model_manager as model_manager

    cfg = {
        "providers": [
            {
                "id": "tr1",
                "name": "基元律动（基元律动请送…）",
                "managed_by": "tokenrhythm",
                "models": [{"id": "m_tr"}],
            },
            {
                "id": "p_plugin",
                "name": "免费通道",
                "managed_by": "tokenrhythm_image",
                "models": [],
            },
            {"id": "p_user", "name": "手动厂商", "models": [{"id": "m_user"}]},
            {
                "id": "p_orphan",
                "name": "被禁用的插件厂商",
                "managed_by": "gone_plugin",
                "models": [{"id": "m_orphan"}],
            },
        ],
        "active_model_id": "m_orphan",
    }

    monkeypatch.setattr(model_manager, "load_config", lambda: cfg)
    written = {}

    def _fake_update_config(mutator):
        data = json.loads(json.dumps(cfg))
        mutator(data)
        written["data"] = data

    monkeypatch.setattr(config_module, "update_config", _fake_update_config)

    removed = plugin_loader._prune_orphan_providers({"tokenrhythm_image"})

    assert removed == ["被禁用的插件厂商"]
    assert [p["id"] for p in written["data"]["providers"]] == ["tr1", "p_plugin", "p_user"]
    assert written["data"]["active_model_id"] is None


def test_refresh_plugins_serializes_concurrent_loads(plugin_env, monkeypatch):
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "serial", "serial_demo", _GOOD_PLUGIN)
    plugin_loader._PLUGIN_INFOS.clear()

    active = 0
    max_active = 0
    guard = threading.Lock()
    real_load = plugin_loader._load_one

    def _slow_load(plugin_dir, registry):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.1)
            return real_load(plugin_dir, registry)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(plugin_loader, "_load_one", _slow_load)

    registry = ToolRegistry()
    threads = [
        threading.Thread(target=plugin_loader.refresh_plugins, args=(registry,))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert max_active == 1
    loaded = [i for i in plugin_loader.get_plugin_infos() if i["name"] == "serial_demo"]
    assert len(loaded) == 1


def test_delete_plugin_removes_user_directory(plugin_env, monkeypatch):
    user_dir, _builtin_dir = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    registry = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    plugin_loader.load_and_register(registry)

    ok, _msg = plugin_loader.delete_plugin("demo")

    assert ok is True
    assert not (user_dir / "demo").exists()
    assert registry.execute("demo_tool", x="a").is_error


def test_delete_plugin_refuses_builtin(plugin_env):
    _user_dir, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "coreplug", "coreplug", _GOOD_PLUGIN)
    plugin_loader.load_and_register(ToolRegistry())

    ok, msg = plugin_loader.delete_plugin("coreplug")

    assert ok is False
    assert "内置" in msg
    assert (builtin_dir / "coreplug" / "plugin.json").is_file()


@pytest.fixture(autouse=True)
def _removed_env(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_loader, "_removed_file", lambda: tmp_path / "plugin_removed.json")


def test_remove_builtin_plugin_unloads_without_deleting_files(plugin_env, monkeypatch):
    _user_dir, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "coreplug", "coreplug", _GOOD_PLUGIN)
    registry = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    assert plugin_loader.load_and_register(registry) == ["coreplug"]
    assert not registry.execute("demo_tool", x="a").is_error

    ok, msg = plugin_loader.remove_plugin("coreplug")

    assert ok is True
    assert "coreplug" in msg
    assert (builtin_dir / "coreplug" / "plugin.json").is_file()
    assert (builtin_dir / "coreplug" / "plugin.py").is_file()
    assert registry.execute("demo_tool", x="a").is_error
    infos = [i for i in plugin_loader.get_plugin_infos() if i["name"] == "coreplug"]
    assert len(infos) == 1
    assert infos[0]["status"] == "removed"
    assert infos[0]["tools"] == []
    assert "coreplug" in plugin_loader._load_removed()


def test_removed_builtin_plugin_is_skipped_on_reload(plugin_env, monkeypatch):
    _user_dir, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "coreplug", "coreplug", _GOOD_PLUGIN)
    first = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: first)
    plugin_loader.load_and_register(first)
    assert plugin_loader.remove_plugin("coreplug")[0] is True

    second = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: second)
    loaded = plugin_loader.load_and_register(second)

    assert loaded == []
    assert second.execute("demo_tool", x="a").is_error
    infos = [i for i in plugin_loader.get_plugin_infos() if i["name"] == "coreplug"]
    assert [i["status"] for i in infos] == ["removed"]
    assert infos[0]["tools"] == []


def test_restore_builtin_plugin_reloads_tools(plugin_env, monkeypatch):
    _user_dir, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "coreplug", "coreplug", _GOOD_PLUGIN)
    registry = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    plugin_loader.load_and_register(registry)
    assert plugin_loader.remove_plugin("coreplug")[0] is True
    assert registry.execute("demo_tool", x="a").is_error

    ok, _msg = plugin_loader.restore_plugin("coreplug")

    assert ok is True
    result = registry.execute("demo_tool", x="back")
    assert not result.is_error
    assert result.content == "hello back"
    infos = [i for i in plugin_loader.get_plugin_infos() if i["name"] == "coreplug"]
    assert len(infos) == 1
    assert infos[0]["status"] == "loaded"
    assert infos[0]["tools"] == ["demo_tool"]
    assert "coreplug" not in plugin_loader._load_removed()


def test_toggle_enabled_refused_for_removed_builtin(plugin_env, monkeypatch):
    _user_dir, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "coreplug", "coreplug", _GOOD_PLUGIN)
    registry = ToolRegistry()
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    plugin_loader.load_and_register(registry)
    assert plugin_loader.remove_plugin("coreplug")[0] is True

    ok, msg = plugin_loader.toggle_plugin("coreplug", True)

    assert ok is False
    assert "被移除" in msg and "恢复" in msg


def test_removed_name_does_not_block_user_plugin_with_same_name(plugin_env):
    user_dir, builtin_dir = plugin_env
    _make_plugin(user_dir / "dup", "dup", _GOOD_PLUGIN)
    _make_plugin(builtin_dir / "dup", "dup", _BROKEN_PLUGIN)
    plugin_loader._save_removed({"dup"})

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    assert loaded == ["dup"]
    assert registry.execute("demo_tool", x="u").content == "hello u"
    by_source = {i["source"]: i for i in plugin_loader.get_plugin_infos()}
    assert by_source["user"]["status"] == "loaded"
    assert by_source["builtin"]["status"] == "overridden"
