"""Byte-level golden guards for provider prompt-cache inputs."""

import hashlib
import json

from tests.request_payload_snapshot import ALPHA_TOOL, ZETA_TOOL


def _bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _controlled_registry():
    from core.tool_protocol import ToolSpec
    from core.tool_registry import ToolRegistry

    def make_spec(tool):
        function = tool["function"]
        return ToolSpec(
            name=function["name"],
            description=function["description"],
            input_schema=function["parameters"],
            execute=lambda **_kwargs: None,
        )

    return ToolRegistry([make_spec(ZETA_TOOL), make_spec(ALPHA_TOOL)])


def test_stable_prompt_has_fixed_sha256(monkeypatch):
    import core.agent_service as agent_service
    from core.context_builder import build_stable_prompt

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    digest = hashlib.sha256(_bytes(build_stable_prompt(agent_service.load_system_prompt()))).hexdigest()
    assert digest == "626f1ab481c68e14aa4a0d0fd356e5f61c94121c6b8b7d64e0f92830c1a26c9a"


def test_stable_system_message_has_fixed_sha256(monkeypatch):
    import core.agent_service as agent_service
    import core.memory_kairos as memory_kairos
    import core.skills as skills
    import core.subagent_registry as subagent_registry

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    monkeypatch.setattr(memory_kairos, "build_memory_system_section", lambda: "memory")
    monkeypatch.setattr(skills, "get_active_skills_catalog", lambda: "skills")
    monkeypatch.setattr(subagent_registry, "get_custom_subagents_catalog", lambda: "subagents")
    digest = hashlib.sha256(_bytes(agent_service.build_stable_system_message())).hexdigest()
    assert digest == "ef286e513e9a5275cb79ecd48dd81da343f086c54575c921550985309e83c1e5"


def test_tools_schema_serialization_is_deterministic():
    from core.llm import get_tools_schema

    assert _bytes(get_tools_schema()) == _bytes(get_tools_schema())


def test_tools_schema_order_is_stable():
    from core.llm import get_tools_schema

    names_a = [tool["function"]["name"] for tool in get_tools_schema()]
    names_b = [tool["function"]["name"] for tool in get_tools_schema()]
    assert names_a == names_b


def test_controlled_tools_schema_has_fixed_sha256():
    digest = hashlib.sha256(_bytes(_controlled_registry().api_schemas())).hexdigest()
    assert digest == "59636f5697aa07288be3fe402aa678c7f59cc025363028c551aa582c1d5eed25"
