"""B1-B3：注入描述符注册表 + 7 个注入点登记（登记不接管渲染）。

规格 02 §3.2 步骤 1 的红线：登记不得改变任何现有注入的字节输出。
本文件用「与现有函数逐字节一致 + 复用 golden 数字」双重方式守护该红线。
"""

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

# 规格 02 §2.2 归分表：7 个注入点的 persistence / channel 归类（逐条锁定）
_EXPECTED_CLASSIFICATION = {
    SOURCE_CONTEXT_PREFIX: (Persistence.PREFIX, Channel.SYSTEM),
    SOURCE_STABLE_SYSTEM_SECTIONS: (Persistence.PREFIX, Channel.SYSTEM),
    SOURCE_RUNTIME_CONTEXT_BLOCK: (Persistence.PERSISTED, Channel.SYSTEM),
    SOURCE_TIME_NOTE: (Persistence.PER_REQUEST, Channel.USER_REMINDER),
    SOURCE_FILE_REFERENCE: (Persistence.PER_REQUEST, Channel.USER_REMINDER),
    SOURCE_COMPUTER_CONTROL_SECTION: (Persistence.PERSISTED, Channel.SYSTEM),
    SOURCE_WORKSPACE_CORRECTIONS: (Persistence.PERSISTED, Channel.SYSTEM),
}

# test_prompt_byte_stability.py 的 build_stable_prompt golden（同一数字，独立复核）
_STABLE_PROMPT_GOLDEN_SHA256 = (
    "bedde81d1a0b0b66858cfb4e6521886dfceefe8ae6f652cdfecf3fb65788b248"
)


def _spec(source, persistence, channel, text):
    """构造测试用描述符；builder 固定返回给定文本（空串 = 不注入）。"""
    return InjectionSpec(
        source=source,
        persistence=persistence,
        channel=channel,
        section_builder=lambda: text,
    )


def _json_bytes(value):
    """与 golden 测试同口径的字节序列化（ensure_ascii=False，无空格）。"""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=False, separators=(",", ":")
    ).encode("utf-8")


def _runtime_context(**overrides):
    """构造一个字段齐全的 RuntimeContext（冻结值，保证字节对比可复现）。"""
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


# ── Registry 基础行为（B1/B2） ──


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
    # 同 source 不同分类也视为重复：source 是唯一键，静默覆盖会让清单失真
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
    # TOOL_RESULT 通道当前没有使用方：三类查询都不应把它算进去
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
    """PERSISTED 判定只看 persistence 维度（未来 todo 提醒为 PERSISTED/USER_REMINDER）。"""
    registry = ContextInjectionRegistry()
    registry.register(_spec("todo", Persistence.PERSISTED, Channel.USER_REMINDER, "T"))

    assert registry.persisted_sources() == frozenset({"todo"})
    # build_persisted_sections 只收集 PERSISTED + SYSTEM（规格 02 §2.5）
    assert registry.build_persisted_sections() == []


# ── 7 个注入点登记与归类（B3） ──


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
    """工厂每次返回新注册表：注册不污染其他调用（无模块级可变全局状态）。"""
    first = build_default_injection_registry()
    second = build_default_injection_registry()

    assert first is not second
    with pytest.raises(ValueError):
        first.register(first.specs()[0])
    assert len(second.specs()) == 7


def test_default_registry_unbound_queries_do_not_fabricate_text():
    """未绑定 runtime 时，状态依赖注入点返回空串（= 不注入），不编造文本。"""
    registry = build_default_injection_registry()

    assert registry.build_persisted_sections() == []
    assert registry.build_per_request_reminders() == []


# ── 行为零变化守护（登记不改变任何字节） ──


def _patch_stable_prefix_dependencies(monkeypatch):
    """与 golden 测试同口径地隔离外部依赖（文件 / 记忆 / 技能 / 子 Agent）。"""
    import core.agent_service as agent_service
    import core.memory_kairos as memory_kairos
    import core.skills as skills
    import core.subagent_registry as subagent_registry

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    monkeypatch.setattr(memory_kairos, "build_memory_system_section", lambda: "memory")
    monkeypatch.setattr(skills, "get_active_skills_catalog", lambda: "skills")
    monkeypatch.setattr(subagent_registry, "get_custom_subagents_catalog", lambda: "subagents")


def test_context_prefix_section_matches_golden_digest(monkeypatch):
    """注入点 1：经注册表取出的前缀与 build_stable_prompt golden 逐字节一致。"""
    _patch_stable_prefix_dependencies(monkeypatch)
    sections = build_default_injection_registry().build_prefix_sections()

    assert hashlib.sha256(_json_bytes(sections[0])).hexdigest() == _STABLE_PROMPT_GOLDEN_SHA256


def test_stable_system_sections_split_and_rejoin_match_message(monkeypatch):
    """注入点 1+2（B4 拆分后）：两段各自完整、按序 join 与 build_stable_system_message 逐字节一致。"""
    import core.agent_service as agent_service

    _patch_stable_prefix_dependencies(monkeypatch)
    sections = build_default_injection_registry().build_prefix_sections()

    assert len(sections) == 2
    # 注入点 2 的拆分语义：只含 build_stable_prompt 之外的段落（不含前缀）
    assert sections[1] == "\n\n".join(
        agent_service.build_stable_system_extra_sections()
    )
    # 组装等价：注册表两段按序 join 与原一次性组装逐字节一致
    assert (
        sections[0] + "\n\n" + sections[1]
        == agent_service.build_stable_system_message()["content"]
    )


def test_runtime_bound_builders_delegate_to_existing_functions():
    """注入点 3/4/7（B4 拆分后）：基础块/纠正/时间注记各自独立，join 与 build_dynamic_prompt 一致。"""
    import core.context_builder as context_builder

    runtime = _runtime_context(workspace_corrections="【按回合撤销纠正】示例纠正块")
    registry = build_default_injection_registry(runtime_provider=lambda: runtime)

    base_block = context_builder._build_runtime_context_block(runtime)
    # PERSISTED + SYSTEM：基础块（不含 6/7）在前，纠正块 source 随后
    assert registry.build_persisted_sections() == [
        base_block,
        runtime.workspace_corrections,
    ]
    # 组装等价：PERSISTED 通道 join 即 build_dynamic_prompt 的完整输出
    assert (
        "\n\n".join(registry.build_persisted_sections())
        == context_builder.build_dynamic_prompt(runtime)
    )
    # PER_REQUEST + USER_REMINDER：时间注记
    assert registry.build_per_request_reminders() == [
        context_builder.build_time_note(runtime)
    ]


def test_computer_section_builder_follows_runtime_capability():
    """注入点 6（B4 拆分后）：电脑操控说明独立成段；组装与 build_dynamic_prompt 一致。"""
    import core.context_builder as context_builder
    from core.computer.models import ComputerCapability, ComputerProtocol

    capability = ComputerCapability(
        enabled=True, protocol=ComputerProtocol.GENERIC.value
    )
    expected = context_builder._build_computer_prompt_section(capability)
    assert expected  # 前置：generic 能力开启必须产出说明文本

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

    # 能力关闭：该 source 返回空串被过滤（不注入，与历史字节一致）
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
    """登记 + 查询前后，直接调用现有函数的输出字节不变（无副作用）。"""
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


# ── B5：压缩协同（persisted_sources 引用 + 动态块锚点） ──


def test_compression_uses_persisted_sources_and_locates_dynamic_block():
    """B5：压缩侧引用注册表 persisted_sources()；承载 PERSISTED 注入的消息可被锚点定位。"""
    from core.context_builder import DYNAMIC_CONTEXT_PREFIX
    from core.memory_compressor import (
        _persisted_context_anchor_index,
        persisted_injection_sources,
    )

    # 压缩侧"必须保留"依据来自注册表（当前即三个 PERSISTED 注入）
    sources = persisted_injection_sources()
    assert SOURCE_RUNTIME_CONTEXT_BLOCK in sources
    assert SOURCE_COMPUTER_CONTROL_SECTION in sources
    assert SOURCE_WORKSPACE_CORRECTIONS in sources

    # 锚点 = 最新一条内联了动态块的 user 消息
    messages = [
        {"role": "user", "content": "早先的任务"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": f"新任务\n\n{DYNAMIC_CONTEXT_PREFIX}\n当前模型：x"},
        {"role": "assistant", "content": "回复2"},
        {"role": "user", "content": "继续"},
    ]
    assert _persisted_context_anchor_index(messages) == 2

    # 无动态块 / 空列表 → None（不锚定，压缩行为与改造前一致）
    assert _persisted_context_anchor_index(messages[:2]) is None
    assert _persisted_context_anchor_index([]) is None


# ── B6：7 个注入点逐字节对比（改造后 vs 改造前参照） ──


def test_seven_injection_points_byte_comparison(monkeypatch):
    """B6：7 个注入点输出逐一与改造前参照逐字节一致，且组装等价。"""
    import core.agent_service as agent_service
    import core.context_builder as context_builder

    _patch_stable_prefix_dependencies(monkeypatch)
    runtime = _runtime_context(workspace_corrections="【纠正】块")
    registry = build_default_injection_registry(runtime_provider=lambda: runtime)
    by_source = {spec.source: spec for spec in registry.specs()}

    # 1) context_prefix：build_stable_prompt（与 golden 同口径）
    assert by_source[SOURCE_CONTEXT_PREFIX].section_builder() == (
        context_builder.build_stable_prompt(agent_service.load_system_prompt())
    )
    # 2) stable_system_sections：拆出的 extra 段
    assert by_source[SOURCE_STABLE_SYSTEM_SECTIONS].section_builder() == (
        "\n\n".join(agent_service.build_stable_system_extra_sections())
    )
    # 3) runtime_context_block：基础块（不含 6/7）
    assert by_source[SOURCE_RUNTIME_CONTEXT_BLOCK].section_builder() == (
        context_builder._build_runtime_context_block(runtime)
    )
    # 4) time_note：与 build_time_note 逐字节一致
    assert by_source[SOURCE_TIME_NOTE].section_builder() == (
        context_builder.build_time_note(runtime)
    )
    # 5) file_reference：未接入（空串 = 不注入，不改变现有行为）
    assert by_source[SOURCE_FILE_REFERENCE].section_builder() == ""
    # 6) computer_control_section：能力关闭时为空串（不注入）
    assert by_source[SOURCE_COMPUTER_CONTROL_SECTION].section_builder() == ""
    # 7) workspace_corrections：字段直读
    assert by_source[SOURCE_WORKSPACE_CORRECTIONS].section_builder() == "【纠正】块"

    # 组装等价：改造前 build_dynamic_prompt 的完整输出（基础块 + 纠正块）
    assert context_builder.build_dynamic_prompt(runtime) == (
        context_builder._build_runtime_context_block(runtime)
        + "\n\n" + "【纠正】块"
    )
