from __future__ import annotations

import pytest

from core.harness.resume_policy import (
    ReplayDecision,
    ToolReplayFacts,
    decide_replay,
    plan_step_resume,
)


def _facts(**overrides) -> ToolReplayFacts:
    base = dict(
        tool_name="read_file",
        side_effect_scope="none",
        read_only=True,
        source="builtin",
        started=True,
        finished=True,
        result_hash_ok=True,
        tool_available=True,
        hash_anchors_ok=True,
        permission_narrowed=False,
        session_archived=False,
        undo_recovering=False,
    )
    base.update(overrides)
    return ToolReplayFacts(**base)


def test_block_session_archived():
    assert decide_replay(_facts(session_archived=True)) == (
        ReplayDecision.BLOCK,
        "session_archived",
    )


def test_block_undo_recovering():
    assert decide_replay(_facts(undo_recovering=True)) == (
        ReplayDecision.BLOCK,
        "undo_recovering",
    )


def test_block_hash_anchor_mismatch():
    assert decide_replay(_facts(hash_anchors_ok=False)) == (
        ReplayDecision.BLOCK,
        "hash_anchor_mismatch",
    )


def test_block_tool_unavailable():
    assert decide_replay(_facts(tool_available=False)) == (
        ReplayDecision.BLOCK,
        "tool_unavailable",
    )


def test_block_uncertain_state():
    assert decide_replay(_facts(started=True, finished=False)) == (
        ReplayDecision.BLOCK,
        "uncertain_state",
    )


@pytest.mark.parametrize("source", ["plugin", "mcp"])
def test_block_untrusted_source(source):
    assert decide_replay(_facts(source=source)) == (
        ReplayDecision.BLOCK,
        "untrusted_scope",
    )


def test_block_get_weather_even_if_scope_none():
    assert decide_replay(
        _facts(tool_name="get_weather", side_effect_scope="none", read_only=True)
    ) == (ReplayDecision.BLOCK, "known_network_side_effect")


def test_block_permission_narrowed_for_write():
    assert decide_replay(
        _facts(tool_name="write_file", side_effect_scope="workspace",
               read_only=False, permission_narrowed=True)
    ) == (ReplayDecision.BLOCK, "permission_narrowed")


def test_permission_narrowed_pure_read_still_replays():
    assert decide_replay(_facts(permission_narrowed=True)) == (
        ReplayDecision.REPLAY,
        "read_replay",
    )


def test_replay_pure_read():
    assert decide_replay(_facts()) == (ReplayDecision.REPLAY, "read_replay")


def test_replay_fresh_on_hash_mismatch():
    assert decide_replay(_facts(result_hash_ok=False)) == (
        ReplayDecision.REPLAY_FRESH,
        "read_hash_mismatch",
    )


@pytest.mark.parametrize("scope", ["workspace", "git", "network", "system"])
def test_confirm_write_scopes(scope):
    assert decide_replay(
        _facts(tool_name="run_command", side_effect_scope=scope, read_only=False)
    ) == (ReplayDecision.CONFIRM, "write_requires_confirmation")


def test_unknown_scope_defaults_to_confirm():
    assert decide_replay(
        _facts(tool_name="mystery", side_effect_scope="hologram", read_only=False)
    ) == (ReplayDecision.CONFIRM, "write_requires_confirmation")


def test_started_false_is_not_uncertain():
    decision, reason = decide_replay(_facts(started=False, finished=False))
    assert decision is ReplayDecision.REPLAY
    assert reason == "read_replay"


def test_scope_none_but_not_read_only_is_confirm():
    assert decide_replay(
        _facts(side_effect_scope="none", read_only=False)
    ) == (ReplayDecision.CONFIRM, "write_requires_confirmation")


def test_plan_step_resume_keeps_order():
    facts = [
        (0, _facts(tool_name="read_a")),
        (1, _facts(tool_name="write_b", side_effect_scope="workspace", read_only=False)),
        (2, _facts(tool_name="read_c", result_hash_ok=False)),
        (3, _facts(tool_name="read_d", tool_available=False)),
    ]
    groups = plan_step_resume(facts)
    assert groups["replay"] == [(0, "read_a", "read_replay")]
    assert groups["fresh"] == [(2, "read_c", "read_hash_mismatch")]
    assert groups["confirm"] == [(1, "write_b", "write_requires_confirmation")]
    assert groups["block"] == [(3, "read_d", "tool_unavailable")]


def test_plan_step_resume_empty():
    assert plan_step_resume([]) == {
        "replay": [], "fresh": [], "confirm": [], "block": []
    }
    assert plan_step_resume(None) == {
        "replay": [], "fresh": [], "confirm": [], "block": []
    }


def test_plan_step_resume_skips_malformed_item():
    groups = plan_step_resume([("not-a-pair",), (0, _facts())])
    assert groups["replay"] == [(0, "read_file", "read_replay")]


def test_decide_replay_never_raises_on_garbage():
    class Exploding:
        @property
        def tool_name(self):
            raise RuntimeError("boom")

    for payload in (None, object(), 42, "text", Exploding()):
        decision, reason = decide_replay(payload)
        assert decision in tuple(ReplayDecision)
        assert isinstance(reason, str) and reason
