import pytest


@pytest.fixture(autouse=True)
def _reset_snapshot():
    import core.agent_service as agent_service

    agent_service._SESSION_STABLE_SECTIONS.clear()
    yield
    agent_service._SESSION_STABLE_SECTIONS.clear()


def _stub_prompt_sources(monkeypatch, skills_text):
    import core.agent_service as agent_service

    monkeypatch.setattr(agent_service, "load_system_prompt", lambda is_gui=False: "base")
    monkeypatch.setattr(
        "core.memory_kairos.build_memory_system_section", lambda: "memory"
    )
    monkeypatch.setattr(
        "core.skills.get_active_skills_catalog", lambda: skills_text
    )
    monkeypatch.setattr(
        "core.subagent_registry.get_custom_subagents_catalog",
        lambda: "subagents",
    )


def test_session_snapshot_freezes_system_header_bytes(monkeypatch):
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    first = agent_service.build_stable_system_message(session_name="snapshot")["content"]

    _stub_prompt_sources(monkeypatch, "skills-v2")
    second = agent_service.build_stable_system_message(session_name="snapshot")["content"]

    assert second == first
    assert "skills-v1" in second
    assert "skills-v2" not in second


def test_no_snapshot_when_session_absent_rerenders(monkeypatch):
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    agent_service.build_stable_system_message()

    _stub_prompt_sources(monkeypatch, "skills-v2")
    fresh = agent_service.build_stable_system_message()["content"]

    assert "skills-v2" in fresh
    assert agent_service._SESSION_STABLE_SECTIONS == {}


def test_snapshot_follows_session_rename_and_clear(monkeypatch):
    import core.agent_service as agent_service

    _stub_prompt_sources(monkeypatch, "skills-v1")
    first = agent_service.build_stable_system_message(session_name="before")["content"]

    agent_service.rename_session_stable_sections("before", "after")
    after = agent_service.build_stable_system_message(session_name="after")["content"]
    assert after == first

    agent_service.clear_session_stable_sections("after")
    _stub_prompt_sources(monkeypatch, "skills-v2")
    fresh = agent_service.build_stable_system_message(session_name="after")["content"]
    assert "skills-v2" in fresh


def test_session_stable_skills_len_matches_snapshot(monkeypatch):
    import core.agent_service as agent_service

    assert agent_service.get_session_stable_skills_len("nope") is None
    assert agent_service.get_session_stable_skills_len("") is None

    _stub_prompt_sources(monkeypatch, "skills-v1")
    agent_service.build_stable_system_message(session_name="snap-len")
    assert agent_service.get_session_stable_skills_len("snap-len") == len("skills-v1")

    _stub_prompt_sources(monkeypatch, "skills-v2-longer")
    assert agent_service.get_session_stable_skills_len("snap-len") == len("skills-v1")
