"""21 个管理类新工具的注册、契约与错误路径回归（T10）。

覆盖意图：
1. ``build_default_registry()`` 必须装配全部 21 个管理类工具；
2. 每个 spec 的 ``api_schema()`` 可 JSON 序列化，且 ``parameters`` 是
   ``type=object`` 并带非空 ``properties``；
3. 写类工具的 ``action`` 必须是 ``enum`` 且包含约定取值（防 action 名漂移），
   个别工具的关键取值字段（usage_stats.days / session_export.fmt /
   screenshot_capture.mode）同样是 enum；
4. 元数据合法：``requires_permission=True``、``side_effect_scope`` 属于合法集合、
   ``read_only=True`` 时 scope 只能是 none / network；
5. 错误路径：未知 action / 非法取值 / 缺参一律返回 ``ok=false`` 的 JSON 字符串，
   且不得触发真实网络、真实写盘、真实 GUI 调用。

隔离策略：错误路径只允许走"入参校验失败"这一层，因此用 tripwire 把
PTY / 浏览器 / 截屏 / 剪贴板 / 插件钩子 / hook 落盘 / socket / 会话管理器
等外部入口全部替换成"被调用即失败"的 Mock——测试能稳定跑在无 GUI、无网络、
无真实用户数据的 CI 环境里，同时反向证明错误分支没有越界产生副作用
（不 ensure PTY、不截屏、不读剪贴板、不打网络、不写 hook 配置）。
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from core.tool_registry import build_default_registry

# 任务规定的 21 个管理类工具（顺序与需求清单一致）
ADMIN_TOOL_NAMES = (
    "session_manage",
    "model_manage",
    "skill_manage",
    "plugin_manage",
    "hook_manage",
    "usage_stats",
    "session_export",
    "update_manage",
    "system_settings",
    "diagram_manage",
    "doctor_run",
    "theme_manage",
    "persona_manage",
    "feishu_manage",
    "lan_manage",
    "decision_manage",
    "pty_manage",
    "browser_pane",
    "screenshot_capture",
    "clipboard_image",
    "computer_control",
)

_VALID_SCOPES = {"none", "workspace", "git", "network", "system"}
_READ_ONLY_SCOPES = {"none", "network"}

# 写类工具的 action 必须声明 enum，且至少包含这里列出的取值。
# 只锚定"调用方/文档明确依赖"的取值，不锁死全集，避免正常扩展 action 时误报。
ACTION_ENUM_EXPECTATIONS = {
    "session_manage": ("list", "create", "rename", "delete",
                       "checkpoint_list", "checkpoint_restore", "undo_recover"),
    "model_manage": ("list_providers", "list_models", "add_provider",
                     "update_provider", "add_model", "set_active", "fetch_models"),
    "skill_manage": ("list", "enable", "disable", "refresh", "restore"),
    "plugin_manage": ("list", "enable", "disable", "settings_get", "settings_action"),
    "hook_manage": ("list", "save", "delete", "set_disabled"),
    "update_manage": ("check", "download", "cancel", "install", "cleanup"),
    "system_settings": ("get", "set"),
    "diagram_manage": ("export", "list", "open"),
    "theme_manage": ("list", "get", "active", "activate", "save", "export", "delete"),
    "persona_manage": ("get", "set"),
    "feishu_manage": ("get", "set", "test", "status"),
    "lan_manage": ("status", "start", "stop"),
    "decision_manage": ("get", "set", "probe", "scenes", "set_scene"),
    "pty_manage": ("ensure", "write", "resize", "kill", "state"),
    "browser_pane": ("open", "close", "navigate", "state", "set_bounds"),
    "clipboard_image": ("read", "save"),
    "computer_control": ("status", "pause", "resume", "stop", "browser"),
}

# 没有 action 参数的工具：关键取值字段同样必须是 enum
VALUE_ENUM_EXPECTATIONS = {
    "usage_stats": ("days", {1, 7, 30, 365}),
    "session_export": ("fmt", {"md", "html", "json"}),
    "screenshot_capture": ("mode", {"full", "region"}),
}

# 错误路径入参：每个工具走"入参校验失败"分支并返回 ok=false。
# usage_stats / session_export 用非法取值，screenshot_capture 用非法 mode，
# 其余用未知 action——都必须在任何副作用之前返回。
ERROR_PATH_ARGUMENTS = {
    "session_manage": {"action": "no_such_action"},
    "model_manage": {"action": "no_such_action"},
    "skill_manage": {"action": "no_such_action"},
    "plugin_manage": {"action": "no_such_action"},
    "hook_manage": {"action": "no_such_action"},
    "usage_stats": {"days": 9999},
    "session_export": {"fmt": "pdf"},
    "update_manage": {"action": "no_such_action"},
    "system_settings": {"action": "no_such_action"},
    "diagram_manage": {"action": "no_such_action"},
    "theme_manage": {"action": "no_such_action"},
    "persona_manage": {"action": "no_such_action"},
    "feishu_manage": {"action": "no_such_action"},
    "lan_manage": {"action": "no_such_action"},
    "decision_manage": {"action": "no_such_action"},
    "pty_manage": {"action": "no_such_action"},
    "browser_pane": {"action": "no_such_action"},
    "screenshot_capture": {"mode": "no_such_mode"},
    "clipboard_image": {"action": "no_such_action"},
    "computer_control": {"action": "no_such_action"},
}
# doctor_run 没有 action / enum 入参，其错误路径用受控失败注入单独覆盖

# 外部副作用入口：错误路径里任何一处被调用都说明校验顺序越界，直接判失败
_TRIPWIRE_TARGETS = (
    "socket.socket",
    "socket.create_connection",
    "core.tools_admin_interaction._get_pty_holder",
    "core.tools_admin_interaction._browser_context",
    "core.tools_admin_interaction._probe_pillow",
    "core.tools_admin_interaction._grab_clipboard_image",
    "core.tools_admin_ops._resolve_manager",
    "core.tools_admin_integration._port_listening",
    "core.tools_admin_integration._feishu_test_connection",
    "core.plugin_loader.refresh_plugins",
    "core.plugin_loader.toggle_plugin",
    "core.plugin_loader.call_plugin_hook",
    "core.hooks.config_mutation.save_hook_config",
    "core.hooks.config_mutation.delete_hook_config",
    "core.hooks.config_mutation.set_hook_disabled",
)


@pytest.fixture(scope="module")
def registry():
    """模块级复用默认注册表（装配一次，避免 21 次重复构造）。"""
    return build_default_registry()


@pytest.fixture()
def external_guard():
    """把所有外部副作用入口替换成 tripwire：被调用即抛 AssertionError，并记录调用。"""
    tripwires: dict[str, mock.Mock] = {}
    active = []
    for target in _TRIPWIRE_TARGETS:
        tripwire = mock.Mock(
            side_effect=AssertionError(f"错误路径不应触发外部副作用: {target}"),
        )
        patcher = mock.patch(target, tripwire)
        patcher.start()
        active.append(patcher)
        tripwires[target] = tripwire
    try:
        yield tripwires
    finally:
        for patcher in reversed(active):
            patcher.stop()


def _assert_no_external_side_effect(tripwires: dict[str, mock.Mock]) -> None:
    """错误路径跑完后，所有外部入口的调用次数必须为 0。

    @_guard 会把异常收敛成 ok=false，所以"只断言返回 ok=false"不足以证明没越界；
    这里直接检查入口调用记录，防止错误分支在校验之后才判错。
    """
    called = [target for target, tripwire in tripwires.items() if tripwire.called]
    assert called == [], f"错误路径触达了外部副作用入口: {called}"


def _error_payload(raw: str) -> dict:
    """断言返回值是合法 JSON 字符串并返回解析结果。"""
    assert isinstance(raw, str), f"工具必须返回字符串，实际: {type(raw)!r}"
    payload = json.loads(raw)
    assert isinstance(payload, dict), f"工具返回值必须是 JSON 对象: {raw!r}"
    return payload


# ---------------------------------------------------------------------------
# 1) 注册完整性
# ---------------------------------------------------------------------------

def test_admin_tool_list_has_21_entries():
    """清单本身是后续断言的锚点，先自检条数与唯一性。"""
    assert len(ADMIN_TOOL_NAMES) == 21
    assert len(set(ADMIN_TOOL_NAMES)) == 21


def test_default_registry_registers_all_admin_tools(registry):
    registered = {spec.name for spec in registry.all()}
    missing = [name for name in ADMIN_TOOL_NAMES if name not in registered]
    assert missing == [], f"默认注册表缺少管理类工具: {missing}"


# ---------------------------------------------------------------------------
# 2) schema 可序列化 + 结构合法
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ADMIN_TOOL_NAMES)
def test_admin_tool_schema_is_json_serialisable(registry, tool_name):
    spec = registry.get(tool_name)
    schema = spec.api_schema()

    # 不抛异常即证明可 JSON 序列化（enforce_ascii=False 保留中文描述）
    serialised = json.dumps(schema, ensure_ascii=False)
    assert json.loads(serialised) == schema

    assert schema["type"] == "function"
    assert schema["function"]["name"] == tool_name
    assert isinstance(schema["function"]["description"], str)
    assert schema["function"]["description"].strip(), f"{tool_name} 描述不得为空"

    parameters = schema["function"]["parameters"]
    assert parameters["type"] == "object", f"{tool_name} parameters.type 必须是 object"
    properties = parameters.get("properties")
    assert isinstance(properties, dict) and properties, (
        f"{tool_name} parameters.properties 必须是非空对象"
    )
    for prop_name, prop_schema in properties.items():
        assert isinstance(prop_schema, dict), f"{tool_name}.{prop_name} 描述必须是对象"
        assert prop_schema.get("type") or prop_schema.get("anyOf"), (
            f"{tool_name}.{prop_name} 缺少类型声明"
        )


# ---------------------------------------------------------------------------
# 3) 写类工具 action 是 enum 且含约定取值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(ACTION_ENUM_EXPECTATIONS))
def test_write_tool_action_is_enum_with_expected_values(registry, tool_name):
    spec = registry.get(tool_name)
    action_schema = spec.input_schema["properties"].get("action")
    assert isinstance(action_schema, dict), f"{tool_name} 缺少 action 参数"
    assert action_schema.get("type") == "string"
    enum_values = action_schema.get("enum")
    assert isinstance(enum_values, list) and enum_values, (
        f"{tool_name} 的 action 必须是非空 enum"
    )
    missing = [v for v in ACTION_ENUM_EXPECTATIONS[tool_name] if v not in enum_values]
    assert missing == [], f"{tool_name} 的 action enum 缺少取值: {missing}"


@pytest.mark.parametrize("tool_name", sorted(VALUE_ENUM_EXPECTATIONS))
def test_named_value_field_is_enum_with_expected_values(registry, tool_name):
    field_name, expected = VALUE_ENUM_EXPECTATIONS[tool_name]
    field_schema = registry.get(tool_name).input_schema["properties"].get(field_name)
    assert isinstance(field_schema, dict), f"{tool_name} 缺少 {field_name} 参数"
    enum_values = field_schema.get("enum")
    assert isinstance(enum_values, list) and enum_values, (
        f"{tool_name}.{field_name} 必须是非空 enum"
    )
    assert expected <= set(enum_values), (
        f"{tool_name}.{field_name} enum 缺少取值: {sorted(expected - set(enum_values))}"
    )


# ---------------------------------------------------------------------------
# 4) 元数据合法
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", ADMIN_TOOL_NAMES)
def test_admin_tool_metadata_is_valid(registry, tool_name):
    spec = registry.get(tool_name)

    assert spec.requires_permission is True, f"{tool_name} 必须声明需要授权"
    assert spec.activity_description, f"{tool_name} 缺少活动描述"
    assert spec.side_effect_scope in _VALID_SCOPES, (
        f"{tool_name} side_effect_scope 非法: {spec.side_effect_scope!r}"
    )
    if spec.read_only:
        assert spec.side_effect_scope in _READ_ONLY_SCOPES, (
            f"{tool_name} 只读却声明写类副作用: {spec.side_effect_scope!r}"
        )
    if spec.mutates_files:
        assert spec.side_effect_scope != "none", (
            f"{tool_name} 会写文件却声明无副作用"
        )


# ---------------------------------------------------------------------------
# 5) 错误路径：未知 action / 非法取值
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool_name", sorted(ERROR_PATH_ARGUMENTS))
def test_unknown_action_returns_error_json(registry, external_guard, tool_name):
    spec = registry.get(tool_name)
    raw = spec.execute(**ERROR_PATH_ARGUMENTS[tool_name])

    payload = _error_payload(raw)
    assert payload["ok"] is False, f"{tool_name} 未知 action 必须返回 ok=false"
    assert str(payload.get("error") or "").strip(), f"{tool_name} 失败必须给出原因"
    _assert_no_external_side_effect(external_guard)


def test_side_effect_guard_is_armed(external_guard):
    """守卫自检：合法 action 确实会触达被监控入口，证明上面的"零调用"断言不空转。"""
    from core import tools_admin_interaction

    payload = _error_payload(tools_admin_interaction.pty_manage(action="state"))

    # tripwire 抛出 AssertionError 后被 @_guard 收敛成失败 JSON，且入口确实被调用过
    assert payload["ok"] is False
    assert external_guard["core.tools_admin_interaction._get_pty_holder"].called


def test_doctor_run_failure_is_reported_as_error_json(registry):
    """doctor_run 无 action/enum 入参：注入自检失败，验证异常被收敛成 ok=false。"""
    with mock.patch("core.doctor.run_checks", side_effect=RuntimeError("自检引擎不可用")):
        raw = registry.get("doctor_run").execute()

    payload = _error_payload(raw)
    assert payload["ok"] is False
    assert "自检引擎不可用" in payload["error"]


# ---------------------------------------------------------------------------
# 6) 缺参 / 非法参路径（需求点名的 4 类）
# ---------------------------------------------------------------------------

# (用例 id, 工具名, 入参, 失败原因里必须出现的关键字)
# 说明：session_manage / theme_manage / update_manage 的 action 是必填位置参数，
# 「缺参」用空字符串触发其"缺少 action"分支（等价语义，且不会变成 TypeError）。
_MISSING_OR_INVALID_CASES = (
    ("session_unknown_action", "session_manage", {"action": "no_such_action"}, "action"),
    ("session_empty_action", "session_manage", {"action": ""}, "action"),
    ("model_missing_provider", "model_manage", {"action": "update_provider"}, "provider"),
    ("theme_unknown_action", "theme_manage", {"action": "no_such_action"}, "action"),
    ("update_unknown_action", "update_manage", {"action": "no_such_action"}, "action"),
    ("update_empty_action", "update_manage", {"action": ""}, "action"),
)


@pytest.mark.parametrize(
    "tool_name,arguments,keyword",
    [case[1:] for case in _MISSING_OR_INVALID_CASES],
    ids=[case[0] for case in _MISSING_OR_INVALID_CASES],
)
def test_missing_or_invalid_parameters(registry, external_guard, tool_name, arguments, keyword):
    spec = registry.get(tool_name)
    payload = _error_payload(spec.execute(**arguments))

    assert payload["ok"] is False
    assert keyword in payload["error"], (
        f"{tool_name} 失败原因应提示缺失/非法参数 {keyword!r}，实际: {payload['error']!r}"
    )
    _assert_no_external_side_effect(external_guard)


def test_registry_execute_wraps_error_result(registry):
    """经注册表调度层调用未知 action 时，同样以成功返回的 JSON 字符串上报 ok=false。"""
    result = registry.execute("session_manage", action="no_such_action")

    assert result.is_error is False
    payload = _error_payload(result.content)
    assert payload["ok"] is False
