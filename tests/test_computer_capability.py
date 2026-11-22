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
])
def test_generic_requires_three_explicit_capability_fields(model, enabled_settings):
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


def test_unset_protocol_auto_enables_generic_for_vision_function_models(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is True
    assert capability.protocol == "generic"
    assert capability.targets == ("browser", "desktop")
    assert capability.browser == enabled_settings.computer_browser
    assert capability.settings_generation == enabled_settings.generation


@pytest.mark.parametrize("model", [
    {"supports_function_calling": True},
    {"supports_function_calling": False, "supports_vision": True},
    {},
])
def test_unset_protocol_auto_requires_vision_declared(model, enabled_settings):
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is False
    assert capability.error_code == "computer_model_not_capable"


def test_unset_protocol_auto_enables_with_vision_and_undeclared_tools(enabled_settings):
    for model in ({"supports_vision": True}, {"supports_vision": True, "supports_function_calling": None}):
        capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
        assert capability.enabled is True
        assert capability.protocol == "generic"


def test_explicit_none_stays_disabled_even_with_vision_and_function_calling(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "none"}
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


def test_feishu_turn_is_also_remote(enabled_settings):
    model = {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, {}, enabled_settings, "feishu")
    assert capability.error_code == "computer_remote_forbidden"
    assert capability.enabled is False


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


def test_legacy_model_without_function_calling_field_gets_capability(enabled_settings):
    model = {"supports_vision": True, "computer_protocol": "generic"}
    capability = resolve_computer_capability(model, openai_compatible_provider(), enabled_settings, "local")
    assert capability.enabled is True
    assert capability.protocol == "generic"
    rejected = resolve_computer_capability(
        {"supports_vision": True, "supports_function_calling": False, "computer_protocol": "generic"},
        openai_compatible_provider(), enabled_settings, "local",
    )
    assert rejected.enabled is False
    assert rejected.error_code == "computer_model_not_capable"


@pytest.mark.parametrize("model", [
    {},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "none"},
    {"supports_vision": True, "supports_function_calling": True, "computer_protocol": "auto"},
])
def test_missing_or_unknown_protocol_is_never_capable(model, enabled_settings):
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


def test_build_runtime_context_forwards_turn_source_to_resolver(monkeypatch):
    import core.llm as llm_mod
    from core.agent_service import build_runtime_context
    from core.computer.capability import take_turn_computer_capability

    captured = {}

    def fake_resolve(model_name, api_base=None, model_id=None, turn_source="local"):
        captured["turn_source"] = turn_source
        return DISABLED_CAPABILITY

    monkeypatch.setattr(llm_mod, "resolve_turn_computer_capability", fake_resolve)

    build_runtime_context("openai/gpt-test", "confirm")
    assert captured["turn_source"] == "local"

    build_runtime_context("openai/gpt-test", "confirm", turn_source="lan")
    assert captured["turn_source"] == "lan"
    take_turn_computer_capability()


def test_build_runtime_context_freezes_lan_turn_disabled(monkeypatch):
    import config
    import core.model_manager as model_manager
    from core.agent_service import build_runtime_context
    from core.computer.capability import take_turn_computer_capability

    monkeypatch.setattr(config, "_load", lambda: {
        "browser_control_enabled": True,
        "computer_control_enabled": True,
    })
    monkeypatch.setattr(
        model_manager, "get_model_config_by_litellm_model", lambda *a, **k: {}
    )
    monkeypatch.setattr(model_manager, "get_model_protocol", lambda *a, **k: "openai")

    runtime = build_runtime_context("openai/gpt-test", "confirm", turn_source="lan")

    capability = take_turn_computer_capability()
    assert capability is runtime.computer_capability
    assert capability.enabled is False
    assert capability.error_code == "computer_remote_forbidden"
