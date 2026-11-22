import pytest

from core.llm import COMPUTER_TURN_SOURCES, chat_with_model_events
from gui_bridge import TURN_SOURCES


def test_entry_and_low_level_whitelists_match():
    assert set(TURN_SOURCES) == set(COMPUTER_TURN_SOURCES)


def test_expected_sources_present():
    for source in ("local", "lan", "feishu", "mcp"):
        assert source in TURN_SOURCES
        assert source in COMPUTER_TURN_SOURCES


def test_every_entry_source_passes_low_level_validation(monkeypatch):
    import core.llm as llm

    sentinel = RuntimeError("passed-source-validation")
    monkeypatch.setattr(llm, "take_turn_computer_capability", lambda: None)

    def _boom(*args, **kwargs):
        raise sentinel

    monkeypatch.setattr(llm, "resolve_turn_computer_capability", _boom)

    for source in llm.COMPUTER_TURN_SOURCES:
        gen = chat_with_model_events(
            "nonexistent-model", "sk-test", [], computer_turn_source=source, persist=False
        )
        with pytest.raises(RuntimeError) as exc:
            next(gen)
        assert exc.value is sentinel, f"{source} 未通过低层来源校验"


def test_low_level_still_rejects_unknown_source():
    gen = chat_with_model_events(
        "nonexistent-model", "sk-test", [], computer_turn_source="git_panel", persist=False
    )
    with pytest.raises(ValueError) as exc:
        next(gen)
    assert "只允许受控值" in str(exc.value)
    for source in COMPUTER_TURN_SOURCES:
        assert source in str(exc.value)
