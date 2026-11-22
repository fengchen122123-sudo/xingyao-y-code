import hashlib
import json

import pytest

from core.context_injection import (
    INJECTION_SOURCES,
    SOURCE_COMPUTER_CONTROL_SECTION,
    SOURCE_CONTEXT_PREFIX,
    SOURCE_FILE_REFERENCE,
    SOURCE_RUNTIME_CONTEXT_BLOCK,
    SOURCE_STABLE_SYSTEM_SECTIONS,
    SOURCE_TIME_NOTE,
    SOURCE_WORKSPACE_CORRECTIONS,
    Channel,
    ContextInjectionRegistry,
    InjectionSpec,
    Persistence,
    build_default_injection_registry,
)

_EXPECTED_CLASSIFICATION = {
    SOURCE_CONTEXT_PREFIX: (Persistence.PREFIX, Channel.SYSTEM),
    SOURCE_STABLE_SYSTEM_SECTIONS: (Persistence.PREFIX, Channel.SYSTEM),
    SOURCE_RUNTIME_CONTEXT_BLOCK: (Persistence.PERSISTED, Channel.SYSTEM),
    SOURCE_TIME_NOTE: (Persistence.PER_REQUEST, Channel.USER_REMINDER),
    SOURCE_FILE_REFERENCE: (Persistence.PER_REQUEST, Channel.USER_REMINDER),
    SOURCE_COMPUTER_CONTROL_SECTION: (Persistence.PERSISTED, Channel.SYSTEM),
    SOURCE_WORKSPACE_CORRECTIONS: (Persistence.PERSISTED, Channel.SYSTEM),
}

_STABLE_PROMPT_GOLDEN_SHA256 = (
    "bedde81d1a0b0b66858cfb4e6521886dfceefe8ae6f652cdfecf3fb65788b248"
)


def _spec(source, persistence, channel, text):
    return InjectionSpec(
        source=source,
        persistence=persistence,
        channel=channel,
        section_builder=lambda: text,
    )


def _json_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ).encode("utf-8")


def _runtime_context(**overrides):
    from core.context_builder import RuntimeContext

    values = dict(
        model_name="deepseek-chat",
        permission_mode="plan",
        working_dir="D:/workspace",
        current_time="2026-09-21 10:00:00",
        active_plan="build/in_progress: 登记注入点",
        command_shell="pwsh",
        ai_output_dir="C:/Users/test/.ycode/tmp",
        workspace_corrections="",
    )
    values.update(overrides)
    return RuntimeContext(**values)


def test_injection_spec_is_frozen():
    from dataclasses import FrozenInstanceError

    spec = _spec("a", Persistence.PREFIX, Channel.SYSTEM, "A")
    with pytest.raises(FrozenInstanceError):
        spec.source = "b"


def test_register_rejects_duplicate_source():
    registry = ContextInjectionRegistry()
    registry.register(_spec("a", Persistence.PREFIX, Channel.SYSTEM, "A"))

    with pytest.raises(ValueError):
        registry.register(_spec("a", Persistence.PREFIX, Channel.SYSTEM, "A2"))
    with pytest.raises(ValueError):
        registry.register(_spec("a", Persistence.PERSISTED, Channel.SYSTEM, "A3"))


def test_register_rejects_empty_source():
    with pytest.raises(ValueError):
        ContextInjectionRegistry().register(
            _spec("", Persistence.PREFIX, Channel.SYSTEM, "x")
        )


def test_query_methods_group_by_persistence_and_channel():
    registry = ContextInjectionRegistry()
    registry.register(_spec("p1", Persistence.PREFIX, Channel.SYSTEM, "P1"))
    registry.register(_spec("p2", Persistence.PREFIX, Channel.SYSTEM, "P2"))
    registry.register(_spec("d1", Persistence.PERSISTED, Channel.SYSTEM, "D1"))
    registry.register(_spec("r1", Persistence.PER_REQUEST, Channel.USER_REMINDER, "R1"))
    registry.register(_spec("t1", Persistence.PER_REQUEST, Channel.TOOL_RESULT, "T1"))

    assert registry.build_prefix_sections() == ["P1", "P2"]
    assert registry.build_persisted_sections() == ["D1"]
    assert registry.build_per_request_reminders() == ["R1"]
    assert registry.persisted_sources() == frozenset({"d1"})


def test_empty_section_builder_means_not_injected():
    registry = ContextInjectionRegistry()
    registry.register(_spec("empty", Persistence.PREFIX, Channel.SYSTEM, ""))
    registry.register(_spec("full", Persistence.PREFIX, Channel.SYSTEM, "X"))

    assert registry.build_prefix_sections() == ["X"]


def test_persisted_sources_filters_by_persistence_only():
    registry = ContextInjectionRegistry()
    registry.register(_spec("todo", Persistence.PERSISTED, Channel.USER_REMINDER, "T"))

    assert registry.persisted_sources() == frozenset({"todo"})
    assert registry.build_persisted_sections() == []


def test_default_registry_registers_seven_points_in_spec_order():
    registry = build_default_injection_registry()

    assert [spec.source for spec in registry.specs()] == list(INJECTION_SOURCES)
    assert len(INJECTION_SOURCES) == 7


def test_default_registry_classification_matches_spec_table():
    registry = build_default_injection_registry()
    by_source = {spec.source: spec for spec in registry.specs()}

    assert set(by_source) == set(_EXPECTED_CLASSIFICATION)
    for source, (persistence, channel) in _EXPECTED_CLASSIFICATION.items():
        assert by_source[source].persistence == persistence, source
        assert by_source[source].channel == channel, source


def test_default_registry_persisted_sources_match_spec_table():
    registry = build_default_injection_registry()

    assert registry.persisted_sources() == frozenset({
        SOURCE_RUNTIME_CONTEXT_BLOCK,
        SOURCE_COMPUTER_CONTROL_SECTION,
        SOURCE_WORKSPACE_CORRECTIONS,
    })


def test_factory_returns_fresh_registry_per_call():
    first = build_default_injection_registry()
    second = build_default_injection_registry()

    assert first is not second
    with pytest.raises(ValueError):
        first.register(first.specs()[0])
    assert len(second.specs()) == 7


def test_default_registry_unbound_queries_do_not_fabricate_text():
    registry = build_default_injection_registry()

    assert registry.build_persisted_sections() == []
    assert registry.build_per_request_reminders() == []


def _patch_stable_prefix_dependencies(monkeypatch):
    import core.agent_service as agent_service
    import core.memory_kairos as memory_kairos
    import core.skills as skills
    import core.subagent_registry as subagent_registry

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    monkeypatch.setattr(memory_kairos, "build_memory_system_section", lambda: "memory")
    monkeypatch.setattr(skills, "get_active_skills_catalog", lambda: "skills")
    monkeypatch.setattr(subagent_registry, "get_custom_subagents_catalog", lambda: "subagents")


def test_context_prefix_section_matches_golden_digest(monkeypatch):
    _patch_stable_prefix_dependencies(monkeypatch)
    sections = build_default_injection_registry().build_prefix_sections()

    assert hashlib.sha256(_json_bytes(sections[0])).hexdigest() == _STABLE_PROMPT_GOLDEN_SHA256


def test_stable_system_sections_split_and_rejoin_match_message(monkeypatch):
    import core.agent_service as agent_service

    _patch_stable_prefix_dependencies(monkeypatch)
    sections = build_default_injection_registry().build_prefix_sections()

    assert len(sections) == 2
    assert sections[1] == "\n\n".join(
        agent_service.build_stable_system_extra_sections()
    )
    assert (
        sections[0] + "\n\n" + sections[1]
        == agent_service.build_stable_system_message()["content"]
    )


def test_runtime_bound_builders_delegate_to_existing_functions():
    import core.context_builder as context_builder

    runtime = _runtime_context(workspace_corrections="【按回合撤销纠正】示例纠正块")
    registry = build_default_injection_registry(runtime_provider=lambda: runtime)

    base_block = context_builder._build_runtime_context_block(runtime)
    assert registry.build_persisted_sections() == [
        base_block,
        runtime.workspace_corrections,
    ]
    assert (
        "\n\n".join(registry.build_persisted_sections())
        == context_builder.build_dynamic_prompt(runtime)
    )
    assert registry.build_per_request_reminders() == [
        context_builder.build_time_note(runtime)
    ]


def test_computer_section_builder_follows_runtime_capability():
    import core.context_builder as context_builder
    from core.computer.models import ComputerCapability, ComputerProtocol

    capability = ComputerCapability(
        enabled=True, protocol=ComputerProtocol.GENERIC.value
    )
    expected = context_builder._build_computer_prompt_section(capability)
    assert expected

    enabled_runtime = _runtime_context(computer_capability=capability)
    registry = build_default_injection_registry(runtime_provider=lambda: enabled_runtime)
    assert registry.build_persisted_sections() == [
        context_builder._build_runtime_context_block(enabled_runtime),
        expected,
    ]
    assert (
        "\n\n".join(registry.build_persisted_sections())
        == context_builder.build_dynamic_prompt(enabled_runtime)
    )

    disabled_runtime = _runtime_context()
    disabled_registry = build_default_injection_registry(
        runtime_provider=lambda: disabled_runtime
    )
    assert disabled_registry.build_persisted_sections() == [
        context_builder._build_runtime_context_block(disabled_runtime)
    ]
    assert (
        "\n\n".join(disabled_registry.build_persisted_sections())
        == context_builder.build_dynamic_prompt(disabled_runtime)
    )


def test_registry_builders_do_not_mutate_context_builder_output(monkeypatch):
    import core.context_builder as context_builder

    _patch_stable_prefix_dependencies(monkeypatch)
    runtime = _runtime_context()
    before_prompt = context_builder.build_stable_prompt("base")
    before_dynamic = context_builder.build_dynamic_prompt(runtime)

    registry = build_default_injection_registry(runtime_provider=lambda: runtime)
    registry.build_prefix_sections()
    registry.build_persisted_sections()
    registry.build_per_request_reminders()
    registry.persisted_sources()

    assert context_builder.build_stable_prompt("base") == before_prompt
    assert context_builder.build_dynamic_prompt(runtime) == before_dynamic


def test_compression_uses_persisted_sources_and_locates_dynamic_block():
    from core.context_builder import DYNAMIC_CONTEXT_PREFIX
    from core.memory_compressor import (
        _persisted_context_anchor_index,
        persisted_injection_sources,
    )

    sources = persisted_injection_sources()
    assert SOURCE_RUNTIME_CONTEXT_BLOCK in sources
    assert SOURCE_COMPUTER_CONTROL_SECTION in sources
    assert SOURCE_WORKSPACE_CORRECTIONS in sources

    messages = [
        {"role": "user", "content": "早先的任务"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": f"新任务\n\n{DYNAMIC_CONTEXT_PREFIX}\n当前模型：x"},
        {"role": "assistant", "content": "回复2"},
        {"role": "user", "content": "继续"},
    ]
    assert _persisted_context_anchor_index(messages) == 2

    assert _persisted_context_anchor_index(messages[:2]) is None
    assert _persisted_context_anchor_index([]) is None


def test_seven_injection_points_byte_comparison(monkeypatch):
    import core.agent_service as agent_service
    import core.context_builder as context_builder

    _patch_stable_prefix_dependencies(monkeypatch)
    runtime = _runtime_context(workspace_corrections="【纠正】块")
    registry = build_default_injection_registry(runtime_provider=lambda: runtime)
    by_source = {spec.source: spec for spec in registry.specs()}

    assert by_source[SOURCE_CONTEXT_PREFIX].section_builder() == (
        context_builder.build_stable_prompt(agent_service.load_system_prompt())
    )
    assert by_source[SOURCE_STABLE_SYSTEM_SECTIONS].section_builder() == (
        "\n\n".join(agent_service.build_stable_system_extra_sections())
    )
    assert by_source[SOURCE_RUNTIME_CONTEXT_BLOCK].section_builder() == (
        context_builder._build_runtime_context_block(runtime)
    )
    assert by_source[SOURCE_TIME_NOTE].section_builder() == (
        context_builder.build_time_note(runtime)
    )
    assert by_source[SOURCE_FILE_REFERENCE].section_builder() == ""
    assert by_source[SOURCE_COMPUTER_CONTROL_SECTION].section_builder() == ""
    assert by_source[SOURCE_WORKSPACE_CORRECTIONS].section_builder() == "【纠正】块"

    assert context_builder.build_dynamic_prompt(runtime) == (
        context_builder._build_runtime_context_block(runtime)
        + "\n\n" + "【纠正】块"
    )
