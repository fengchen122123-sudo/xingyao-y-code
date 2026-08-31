"""core/plugin_loader.py 的单元测试。"""

import json

import pytest

from core import plugin_loader
from core.tool_registry import ToolRegistry

# autouse fixture 会替换 _confirm_plugin_trust，这里在导入期保留原始实现
_REAL_CONFIRM = plugin_loader._confirm_plugin_trust


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


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    """把插件扫描目录重定向到临时目录。"""
    user_dir = tmp_path / "user_plugins"
    builtin_dir = tmp_path / "builtin_plugins"
    user_dir.mkdir()
    builtin_dir.mkdir()
    monkeypatch.setattr(plugin_loader, "plugin_dirs", lambda: [user_dir, builtin_dir])
    return user_dir, builtin_dir


@pytest.fixture(autouse=True)
def _trust_env(tmp_path, monkeypatch):
    """隔离信任持久化文件，并默认自动通过信任确认（信任机制有专项测试覆盖）。"""
    monkeypatch.setattr(plugin_loader, "_trust_file", lambda: tmp_path / "plugin_trust.json")
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: True)


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
    assert registry.execute("demo_tool").is_error  # unknown tool


def test_user_plugin_overrides_builtin_with_same_name(plugin_env):
    user_dir, builtin_dir = plugin_env
    _make_plugin(user_dir / "same", "same", _GOOD_PLUGIN)
    _make_plugin(builtin_dir / "same", "same", _BROKEN_PLUGIN)

    registry = ToolRegistry()
    loaded = plugin_loader.load_and_register(registry)

    # 用户目录先扫描，内置同名插件被跳过，不会触发崩溃也不会重复注册
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
    # 第二次调用命中 sys.modules 缓存，返回同一对象
    again = plugin_loader.import_sibling(str(plugin_dir / "plugin.py"), "helper")
    assert again is module


def test_import_sibling_missing_module_raises(plugin_env):
    user_dir, _ = plugin_env
    plugin_dir = user_dir / "sib2"
    plugin_dir.mkdir()

    with pytest.raises(ImportError):
        plugin_loader.import_sibling(str(plugin_dir / "plugin.py"), "nope")


# ── get_plugin_infos：/plugins 命令的数据来源 ──────────────────────

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


# ── refresh_plugins：运行期间安装的插件无需重启即可显示 ──────────────

def test_refresh_plugins_discovers_newly_installed(plugin_env):
    """启动后新安装的插件，刷新后即可显示并热加载"""
    user_dir, _ = plugin_env
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    assert plugin_loader.get_plugin_infos() == []

    # 模拟"运行期间 Agent 自己安装了插件"
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert [i["name"] for i in infos] == ["demo"]
    assert infos[0]["status"] == "loaded"
    assert infos[0]["tools"] == ["demo_tool"]
    assert registry.execute("demo_tool", x="hot").content == "hello hot"


def test_refresh_plugins_does_not_reload_existing(plugin_env):
    """已加载的插件不会被重复注册/重复记录"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert registry.execute("demo_tool", x="once").content == "hello once"


def test_refresh_plugins_removes_deleted(plugin_env):
    """插件目录被删除后，刷新即从列表移除"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    (user_dir / "demo" / "plugin.json").unlink()
    plugin_loader.refresh_plugins(registry)

    assert plugin_loader.get_plugin_infos() == []


def test_refresh_plugins_records_disabled_new_plugin(plugin_env):
    """新安装的禁用插件显示为 disabled，不注册工具"""
    user_dir, _ = plugin_env
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)

    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)
    plugin_loader.refresh_plugins(registry)

    infos = plugin_loader.get_plugin_infos()
    assert len(infos) == 1
    assert infos[0]["status"] == "disabled"
    assert registry.execute("demo_tool").is_error


# ── 插件信任机制：首次加载/指纹变化需确认 ─────────────────────────

def test_user_plugin_skipped_when_not_confirmed(plugin_env, monkeypatch, caplog):
    """headless/未确认场景：用户级插件跳过加载并记 warning"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)

    registry = ToolRegistry()
    with caplog.at_level("WARNING", logger="ycode.plugins"):
        loaded = plugin_loader.load_and_register(registry)

    assert loaded == []
    assert registry.execute("demo_tool").is_error
    infos = plugin_loader.get_plugin_infos()
    assert infos[0]["status"] == "untrusted"
    assert any("未获信任" in r.message for r in caplog.records)


def test_user_plugin_loads_after_confirmation_persisted(plugin_env, monkeypatch, tmp_path):
    """确认一次后指纹持久化，之后无需再确认即可加载"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]
    trust = json.loads((tmp_path / "plugin_trust.json").read_text(encoding="utf-8"))
    assert len(trust) == 1

    # 模拟下次启动：确认回调被拒绝，但指纹已持久化，仍应直接加载
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)
    registry2 = ToolRegistry()
    assert plugin_loader.load_and_register(registry2) == ["demo"]


def test_fingerprint_change_requires_reconfirmation(plugin_env, monkeypatch):
    """已信任插件内容被篡改（指纹变化）后必须重新确认"""
    user_dir, _ = plugin_env
    plugin_dir = user_dir / "demo"
    _make_plugin(plugin_dir, "demo", _GOOD_PLUGIN)
    assert plugin_loader.load_and_register(ToolRegistry()) == ["demo"]

    (plugin_dir / "plugin.py").write_text(_GOOD_PLUGIN + "\nX = 1\n", encoding="utf-8")
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == []
    assert plugin_loader.get_plugin_infos()[0]["status"] == "untrusted"


def test_builtin_plugin_auto_trusted(plugin_env, monkeypatch):
    """内置插件随仓库发布：记录指纹并自动信任，无需确认"""
    _, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "demo", "demo", _GOOD_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]


def test_trust_prompt_callback_allows_loading(plugin_env, monkeypatch):
    """入口层注册的确认回调返回 True 时加载"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_trust_prompt", lambda path, name, fp: True)
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", _REAL_CONFIRM)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == ["demo"]


def test_disabled_plugin_does_not_trigger_trust_prompt(plugin_env, monkeypatch):
    """禁用插件不加载也不应弹出信任确认"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)

    def _boom(*a, **k):
        raise AssertionError("禁用插件不应触发信任确认")

    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", _boom)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == []
    assert plugin_loader.get_plugin_infos()[0]["status"] == "disabled"


def test_toggle_enable_untrusted_plugin_refused(plugin_env, monkeypatch, tmp_path):
    """热启用未获信任的插件：拒绝加载"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)

    ok, msg = plugin_loader.toggle_plugin("off", True)

    assert ok is False
    assert "信任" in msg
    assert registry.execute("demo_tool").is_error


# ── trust_plugin：显式信任接口（供 GUI 桥接层调用，等效 CLI 首次加载答 y）──

def test_trust_plugin_untrusted_then_loads_without_confirm(plugin_env, monkeypatch):
    """显式信任后指纹持久化：即使确认回调拒绝也能直接加载"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)

    registry = ToolRegistry()
    assert plugin_loader.load_and_register(registry) == []
    assert plugin_loader.get_plugin_infos()[0]["status"] == "untrusted"

    ok, msg = plugin_loader.trust_plugin("demo")
    assert ok is True
    assert msg  # 有可展示的中文提示

    # 下次加载（模拟重启后）：指纹已持久化，确认回调返回 False 也不弹确认
    registry2 = ToolRegistry()
    assert plugin_loader.load_and_register(registry2) == ["demo"]
    assert plugin_loader.get_plugin_infos()[0]["status"] == "loaded"
    assert plugin_loader.get_plugin_infos()[0]["tools"] == ["demo_tool"]
    assert registry2.execute("demo_tool", x="hi").content == "hello hi"


def test_trust_plugin_unknown_name_rejected(plugin_env):
    """未知插件名：返回失败，错误信息含"未找到" """
    ok, msg = plugin_loader.trust_plugin("no_such_plugin")
    assert ok is False
    assert "未找到" in msg


def test_trust_plugin_builtin_ok_idempotent(plugin_env):
    """内置插件可直接信任（幂等：指纹本就自动记录，重复信任仍成功）"""
    _, builtin_dir = plugin_env
    _make_plugin(builtin_dir / "bi", "bi", _GOOD_PLUGIN)

    ok, _ = plugin_loader.trust_plugin("bi")
    assert ok is True
    ok2, _ = plugin_loader.trust_plugin("bi")
    assert ok2 is True


def test_trust_plugin_disabled_only_persists_trust(plugin_env, monkeypatch):
    """禁用插件：只持久化信任，不改变 enabled、不注册工具"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "off", "off", _GOOD_PLUGIN, enabled=False)
    registry = ToolRegistry()
    plugin_loader.load_and_register(registry)
    assert plugin_loader.get_plugin_infos()[0]["status"] == "disabled"

    ok, _ = plugin_loader.trust_plugin("off")
    assert ok is True

    meta = json.loads((user_dir / "off" / "plugin.json").read_text(encoding="utf-8"))
    assert meta["enabled"] is False
    assert plugin_loader.get_plugin_infos()[0]["status"] == "disabled"
    assert registry.execute("demo_tool").is_error

    # 信任已持久化：之后热启用时确认回调拒绝也无需再确认即可加载
    # （trust_plugin 不改 enabled，禁用插件在 load_and_register 中仍按 disabled 跳过）
    monkeypatch.setattr(plugin_loader, "_confirm_plugin_trust", lambda *a, **k: False)
    monkeypatch.setattr(plugin_loader, "_live_registry", lambda: registry)
    ok2, msg2 = plugin_loader.toggle_plugin("off", True)
    assert ok2 is True
    assert registry.execute("demo_tool", x="go").content == "hello go"


def test_trust_plugin_persists_fingerprint_entry(plugin_env, tmp_path):
    """信任后 plugin_trust.json 出现 {resolve 后路径: 当前指纹} 条目"""
    user_dir, _ = plugin_env
    _make_plugin(user_dir / "demo", "demo", _GOOD_PLUGIN)

    ok, _ = plugin_loader.trust_plugin("demo")
    assert ok is True

    trust = json.loads((tmp_path / "plugin_trust.json").read_text(encoding="utf-8"))
    assert trust == {
        str((user_dir / "demo").resolve()):
            plugin_loader._plugin_fingerprint(user_dir / "demo")
    }
