from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import threading

import pytest

from core.computer.models import ComputerCapability, ComputerProtocol
from tests.computer_fakes import use_temp_config


def _chunk():
    delta = SimpleNamespace(
        content="done",
        reasoning_content=None,
        tool_calls=None,
        provider_specific_fields=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
        usage=None,
    )


def _capture_request_tool_names(monkeypatch, *, model_cfg, capability=None, allowed_tools=None):
    import core.llm as llm
    from core.tool_protocol import ToolSpec
    from core.tool_registry import ToolRegistry

    fake_registry = ToolRegistry([
        ToolSpec(
            name="fetch_url",
            description="fetch",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            execute=lambda url="": url,
        ),
        ToolSpec(
            name="browser_pane",
            description="embedded pane",
            input_schema={"type": "object", "properties": {"action": {"type": "string"}}},
            execute=lambda action="open": action,
        ),
    ])
    captured = []
    monkeypatch.setattr(llm, "get_tools_schema", lambda: fake_registry.api_schemas())
    monkeypatch.setattr(llm, "estimate_tokens", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr("core.model_manager.get_model_protocol", lambda *_a, **_k: "openai")
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_litellm_model",
        lambda *_a, **_k: model_cfg,
    )
    monkeypatch.setattr(
        "core.model_manager.get_model_config_by_id",
        lambda *_a, **_k: model_cfg,
        raising=False,
    )
    monkeypatch.setattr("core.memory_kairos.build_memory_system_section", lambda: "")
    monkeypatch.setattr(llm, "_describe_images_with_secondary", lambda *_a, **_k: None)

    def fake_completion(**kwargs):
        captured.append(deepcopy(kwargs))
        return iter([_chunk()])

    monkeypatch.setattr(llm, "completion", fake_completion)
    list(
        llm.chat_with_model_events(
            "openai/gpt-test",
            "k",
            [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            api_base="https://api.example.com/v1",
            persist=False,
            consume_background_notifications=False,
            computer_capability=capability,
            allowed_tools=allowed_tools,
        )
    )
    assert len(captured) == 1
    return [tool["function"]["name"] for tool in captured[0]["tools"]]


def test_generic_dynamic_prompt_reflects_selected_browser():
    from core.computer.provider_generic import build_generic_dynamic_prompt

    edge_cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser", "desktop"),
        browser="edge",
    )
    chrome_cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="chrome",
    )
    embedded_cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="embedded",
    )
    desktop_only_cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("desktop",),
        browser="edge",
    )

    edge_prompt = build_generic_dynamic_prompt(edge_cap)
    assert "Microsoft Edge" in edge_prompt
    assert "禁止改用 browser_pane" in edge_prompt
    assert "内嵌浏览器里实时呈现" not in edge_prompt
    assert "右侧面板" in edge_prompt

    chrome_prompt = build_generic_dynamic_prompt(chrome_cap)
    assert "Google Chrome" in chrome_prompt
    assert "禁止改用 browser_pane" in chrome_prompt
    assert "内嵌浏览器里实时呈现" not in chrome_prompt

    embedded_prompt = build_generic_dynamic_prompt(embedded_cap)
    assert "星瑶内置浏览器" in embedded_prompt
    assert "内嵌浏览器里实时呈现" in embedded_prompt
    assert "优先使用 computer_action" in embedded_prompt

    desktop_prompt = build_generic_dynamic_prompt(desktop_only_cap)
    assert "内嵌浏览器里实时呈现" not in desktop_prompt
    assert "observe_region" not in desktop_prompt


def test_native_openai_computer_prompt_reflects_selected_browser():
    from core.context_builder import _build_computer_prompt_section

    chrome_native = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.NATIVE_OPENAI.value,
        targets=("browser",),
        browser="chrome",
    )
    embedded_native = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.NATIVE_OPENAI.value,
        targets=("browser",),
        browser="embedded",
    )

    chrome_text = _build_computer_prompt_section(chrome_native)
    assert "computer_select_target" in chrome_text
    assert "Google Chrome" in chrome_text
    assert "browser_pane" in chrome_text

    embedded_text = _build_computer_prompt_section(embedded_native)
    assert "computer_select_target" in embedded_text
    assert "星瑶内置浏览器" in embedded_text


@pytest.mark.parametrize(
    ("browser", "enabled", "expect_phrases", "forbid_phrases"),
    [
        (
            "chrome",
            True,
            ("Google Chrome", "computer_action", "禁止改用"),
            (),
        ),
        (
            "edge",
            False,
            ("Microsoft Edge", "禁止改用 browser_pane", "fetch_url"),
            (),
        ),
        (
            "embedded",
            True,
            ("星瑶内置浏览器", "computer_action"),
            (),
        ),
        (
            "embedded",
            False,
            ("星瑶内置浏览器", "browser_pane"),
            (),
        ),
    ],
)
def test_runtime_context_injects_system_browser_priority(
    tmp_path, monkeypatch, browser, enabled, expect_phrases, forbid_phrases
):
    import core.model_manager as model_manager
    from core.agent_service import build_runtime_context
    from core.context_builder import build_dynamic_prompt

    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": enabled, "computer_browser": browser},
    )
    monkeypatch.setattr(
        model_manager,
        "get_model_config_by_litellm_model",
        lambda *_a, **_k: {"supports_vision": True, "supports_function_calling": True},
    )
    monkeypatch.setattr(model_manager, "get_model_protocol", lambda *_a, **_k: "openai")
    monkeypatch.setattr(
        "core.computer.platforms.get_platform_adapter",
        lambda *_a, **_k: SimpleNamespace(
            supported=True,
            unavailable_reason="",
            find_browser_executable=lambda b: f"{b}.exe",
        ),
    )

    runtime = build_runtime_context("openai/gpt-vision", "confirm")
    dynamic = build_dynamic_prompt(runtime)
    for phrase in expect_phrases:
        assert phrase in dynamic
    for phrase in forbid_phrases:
        assert phrase not in dynamic


def test_llm_request_tools_hide_browser_pane_when_external_browser_selected(tmp_path, monkeypatch):
    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": True, "computer_browser": "chrome"},
    )
    chrome_cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="chrome",
    )
    names = _capture_request_tool_names(
        monkeypatch,
        model_cfg={"supports_vision": True, "supports_function_calling": True},
        capability=chrome_cap,
    )
    assert "computer_action" in names
    assert "browser_pane" not in names


def test_llm_request_tools_hide_browser_pane_when_external_and_control_disabled(tmp_path, monkeypatch):
    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": False, "computer_browser": "edge"},
    )
    disabled_cap = ComputerCapability(enabled=False, protocol=ComputerProtocol.NONE.value)
    names = _capture_request_tool_names(
        monkeypatch,
        model_cfg={"supports_vision": True, "supports_function_calling": True},
        capability=disabled_cap,
    )
    assert "browser_pane" not in names
    assert "fetch_url" in names


def test_llm_request_tools_keep_browser_pane_when_embedded_selected(tmp_path, monkeypatch):
    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": False, "computer_browser": "embedded"},
    )
    disabled_cap = ComputerCapability(enabled=False, protocol=ComputerProtocol.NONE.value)
    names = _capture_request_tool_names(
        monkeypatch,
        model_cfg={"supports_vision": True, "supports_function_calling": True},
        capability=disabled_cap,
    )
    assert "browser_pane" in names


def _build_pipeline(registry, *, capability, permission_calls):
    from core.harness.tool_adapters import ToolAdapterSet
    from core.harness.tool_pipeline import DecodedToolCall, ToolExecutionContext, ToolPipeline

    def permission_checker(tool_name, arguments, **_kwargs):
        permission_calls.append((tool_name, dict(arguments)))
        return True

    pipeline = ToolPipeline(ToolAdapterSet())
    context = ToolExecutionContext(
        event_callback=None,
        parent_session="s1",
        parent_turn_id="t1",
        cancel_event=threading.Event(),
        allow_subagents=True,
        permission_owner=None,
        permission_source="主代理",
        permission_checker=permission_checker,
        registry=registry,
        mcp_manager=None,
        computer_capability=capability,
    )
    return pipeline, context, DecodedToolCall


def test_tool_pipeline_blocks_browser_pane_before_permission_when_external_browser(
    tmp_path, monkeypatch
):
    from core.tool_protocol import ToolSpec
    from core.tool_registry import ToolRegistry

    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": True, "computer_browser": "chrome"},
    )
    executed = []
    permission_calls = []
    registry = ToolRegistry([
        ToolSpec(
            name="browser_pane",
            description="pane",
            input_schema={"type": "object", "properties": {"action": {"type": "string"}, "url": {"type": "string"}}},
            execute=lambda **kw: executed.append(kw) or '{"ok": true}',
            requires_permission=True,
        )
    ])
    cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="chrome",
    )
    pipeline, context, DecodedToolCall = _build_pipeline(
        registry, capability=cap, permission_calls=permission_calls
    )
    result = pipeline.execute_decoded(
        DecodedToolCall(
            call_id="c1",
            name="browser_pane",
            arguments={"action": "open", "url": "https://example.com"},
            raw_arguments="{}",
        ),
        context,
    )
    assert permission_calls == []
    assert executed == []
    assert "Google Chrome" in result["content"]
    assert "computer_action" in result["content"]


def test_tool_pipeline_embedded_browser_redirects_web_open_but_allows_close_and_file_preview(
    tmp_path, monkeypatch
):
    from core.tool_protocol import ToolSpec
    from core.tool_registry import ToolRegistry

    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": True, "computer_browser": "embedded"},
    )
    executed = []
    permission_calls = []
    registry = ToolRegistry([
        ToolSpec(
            name="browser_pane",
            description="pane",
            input_schema={"type": "object", "properties": {"action": {"type": "string"}, "url": {"type": "string"}}},
            execute=lambda **kw: executed.append(kw) or json.dumps({"ok": True, **kw}),
            requires_permission=True,
        )
    ])
    cap = ComputerCapability(
        enabled=True,
        protocol=ComputerProtocol.GENERIC.value,
        targets=("browser",),
        browser="embedded",
    )
    pipeline, context, DecodedToolCall = _build_pipeline(
        registry, capability=cap, permission_calls=permission_calls
    )

    web_res = pipeline.execute_decoded(
        DecodedToolCall(
            call_id="c1",
            name="browser_pane",
            arguments={"action": "open", "url": "https://example.com"},
            raw_arguments="{}",
        ),
        context,
    )
    assert executed == []
    assert permission_calls == []
    assert "computer_action" in web_res["content"]

    file_res = pipeline.execute_decoded(
        DecodedToolCall(
            call_id="c2",
            name="browser_pane",
            arguments={"action": "open", "url": "file:///C:/demo.html"},
            raw_arguments="{}",
        ),
        context,
    )
    assert json.loads(file_res["content"])["ok"] is True

    close_res = pipeline.execute_decoded(
        DecodedToolCall(
            call_id="c3",
            name="browser_pane",
            arguments={"action": "close"},
            raw_arguments="{}",
        ),
        context,
    )
    assert json.loads(close_res["content"])["ok"] is True
    assert len(executed) == 2


def test_tool_pipeline_embedded_allows_browser_pane_when_browser_control_disabled(
    tmp_path, monkeypatch
):
    from core.tool_protocol import ToolSpec
    from core.tool_registry import ToolRegistry

    use_temp_config(
        tmp_path,
        monkeypatch,
        {"browser_control_enabled": False, "computer_browser": "embedded"},
    )
    executed = []
    permission_calls = []
    registry = ToolRegistry([
        ToolSpec(
            name="browser_pane",
            description="pane",
            input_schema={"type": "object", "properties": {"action": {"type": "string"}, "url": {"type": "string"}}},
            execute=lambda **kw: executed.append(kw) or json.dumps({"ok": True, **kw}),
            requires_permission=True,
        )
    ])
    disabled_cap = ComputerCapability(enabled=False, protocol=ComputerProtocol.NONE.value)
    pipeline, context, DecodedToolCall = _build_pipeline(
        registry, capability=disabled_cap, permission_calls=permission_calls
    )
    res = pipeline.execute_decoded(
        DecodedToolCall(
            call_id="c1",
            name="browser_pane",
            arguments={"action": "open", "url": "https://example.com"},
            raw_arguments="{}",
        ),
        context,
    )
    assert json.loads(res["content"])["ok"] is True
    assert len(executed) == 1
    assert len(permission_calls) == 1


def test_cli_profile_browser_preference_does_not_suggest_browser_pane():
    from core.computer.settings import (
        ComputerSettingsSnapshot,
        describe_runtime_browser_preference,
    )

    snapshot = ComputerSettingsSnapshot(
        browser_control_enabled=False,
        computer_control_enabled=False,
        computer_browser="embedded",
        browser_options=(),
        profile_display_path="",
        platform_supported=True,
        generation=1,
    )
    text = describe_runtime_browser_preference(snapshot, None, ui_profile="cli")
    assert "星瑶内置浏览器" in text
    assert "browser_pane" not in text
    assert "fetch_url" in text
