"""Turn 起点电脑操控能力决策测试（计划 Task 4 / 设计 9.3）。

resolver 是纯函数：不读全局 config、不网络探测、不按模型名分支。
"""

import dataclasses

import pytest

from core.computer.capability import resolve_computer_capability
from tests.computer_fakes import (
    DISABLED_CAPABILITY,
    GENERIC_CAPABILITY,
    NATIVE_BROWSER_CAPABILITY,
    enabled_settings,
    openai_compatible_provider,
)


@pytest.mark.parametrize("model", [
    {"supports_vision": True, "supports_function_calling": False, "computer_protocol": "generic"},
    {"supports_vision": False, "supports_function_calling": True, "computer_protocol": "generic"},
    {"supports_vision": True, "supports_function_calling": True},
])
def test_generic_requires_three_explicit_capability_fields(model, enabled_settings):
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


def test_generic_capable_model_receives_enabled_targets(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is True
    assert capability.targets == ("browser", "desktop")


def test_lan_turn_is_always_disabled(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    assert resolve_computer_capability(model, {}, enabled_settings, "lan").error_code == "computer_remote_forbidden"


def test_native_requires_official_openai_responses(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "native_openai"}
    provider = {"protocol": "openai", "base_url": "https://proxy.example/v1"}
    capability = resolve_computer_capability(model, provider, enabled_settings, "local")
    assert capability.error_code == "computer_protocol_mismatch"


def test_all_switches_off_report_computer_disabled(enabled_settings):
    settings = dataclasses.replace(
        enabled_settings, browser_control_enabled=False, computer_control_enabled=False
    )
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_disabled"
    assert capability.targets == ()


def test_non_windows_platform_is_rejected(enabled_settings):
    settings = dataclasses.replace(enabled_settings, platform_supported=False)
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_unsupported_os"


def test_legacy_model_without_function_calling_field_stays_disabled(enabled_settings):
    """旧配置缺 supports_function_calling：普通对话/工具不受影响，但不得获得电脑操控资格。"""
    model = {"supports_vision": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


@pytest.mark.parametrize("model", [
    {},
    {"name": "deepseek-v4-flash-vision-exp", "supports_vision": True},
    {"name": "gpt-5.4", "supports_vision": True, "supports_function_calling": True},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "none"},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "auto"},
])
def test_missing_or_unknown_protocol_is_never_capable(model, enabled_settings):
    """缺失/未知 computer_protocol 一律不获得资格；resolver 绝不按模型名猜测。"""
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


@pytest.mark.parametrize("provider", [
    {"protocol": "openai", "base_url": "https://api.openai.com/v1"},
    {"protocol": "responses", "base_url": "https://proxy.example/v1"},
    {"protocol": "responses", "base_url": "https://api.openai.com.evil.example/v1"},
    {"protocol": "responses"},
    {"protocol": "responses", "base_url": "not a url"},
])
def test_native_rejects_non_official_base_or_non_responses_protocol(provider, enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "native_openai"}
    capability = resolve_computer_capability(model, provider, enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_protocol_mismatch"


def test_native_official_openai_responses_is_enabled(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "native_openai"}
    provider = {"protocol": "responses", "base_url": "https://api.openai.com/v1"}
    capability = resolve_computer_capability(model, provider, enabled_settings, "local")
    assert capability.enabled is True
    assert capability.protocol == "native_openai"
    assert capability.targets == ("browser", "desktop")
    assert capability.browser == "edge"
    assert capability.settings_generation == enabled_settings.generation
    assert capability.error_code == ""
    assert capability.reason == ""


def test_native_does_not_require_generic_vision_function_fields(enabled_settings):
    """native 判定只看显式原生标记 + Responses 协议 + 官方 base（设计 9.3）。"""
    model = {"computer_protocol": "native_openai"}
    provider = {"protocol": "responses", "base_url": "https://api.openai.com/v1"}
    capability = resolve_computer_capability(model, provider, enabled_settings, "local")
    assert capability.enabled is True
    assert capability.protocol == "native_openai"


def test_single_browser_switch_yields_browser_only_target(enabled_settings):
    settings = dataclasses.replace(enabled_settings, computer_control_enabled=False)
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), settings, "local")
    assert capability.enabled is True
    assert capability.targets == ("browser",)


def test_single_desktop_switch_yields_desktop_only_target(enabled_settings):
    settings = dataclasses.replace(enabled_settings, browser_control_enabled=False)
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), settings, "local")
    assert capability.enabled is True
    assert capability.targets == ("desktop",)
    assert capability.browser == "edge"


def test_capability_decision_is_frozen_value_object(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    with pytest.raises(dataclasses.FrozenInstanceError):
        capability.enabled = False


def test_shared_capability_constants_have_contract_shape():
    assert GENERIC_CAPABILITY.enabled is True
    assert GENERIC_CAPABILITY.protocol == "generic"
    assert GENERIC_CAPABILITY.targets == ("browser", "desktop")
    assert NATIVE_BROWSER_CAPABILITY.enabled is True
    assert NATIVE_BROWSER_CAPABILITY.protocol == "native_openai"
    assert NATIVE_BROWSER_CAPABILITY.targets == ("browser",)
    assert DISABLED_CAPABILITY.enabled is False
    assert DISABLED_CAPABILITY.targets == ()
