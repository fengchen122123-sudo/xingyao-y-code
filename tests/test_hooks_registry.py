from __future__ import annotations

import json
from pathlib import Path

from core.hooks.registry import (
    HookRegistry,
    matches_tool,
    registration_matches,
)
from core.hooks.trust import compute_hook_digest
from core.hooks.types import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, HookEvent


def _write_hooks(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _registry(tmp_path, user_payload=None, project_payload=None):
    user_path = tmp_path / "home" / "hooks.json"
    project_path = tmp_path / "proj" / ".ycode" / "hooks.json"
    if user_payload is not None:
        _write_hooks(user_path, user_payload)
    if project_payload is not None:
        _write_hooks(project_path, project_payload)
    return HookRegistry(user_hooks_path=user_path, project_hooks_path=project_path)


def test_load_valid_config_and_defaults(tmp_path):
    registry = _registry(
        tmp_path,
        project_payload={
            "version": 1,
            "hooks": {
                "PreToolUse": [
                    {"matcher": "write_file|edit_file", "command": "echo hi"}
                ]
            },
        },
    )
    regs = registry.load()
    assert len(regs) == 1
    reg = regs[0]
    assert reg.event == HookEvent.PRE_TOOL_USE
    assert reg.timeout_ms == DEFAULT_TIMEOUT_MS
    assert reg.scope == "project"
    assert reg.matcher == "write_file|edit_file"


def test_invalid_json_skips_file_without_affecting_other(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={"hooks": {"Stop": [{"command": "echo user"}]}},
        project_payload="{ not json",
    )
    regs = registry.load()
    assert [r.command for r in regs] == ["echo user"]


def test_bad_entry_is_skipped_but_good_entries_kept(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={
            "hooks": {
                "Stop": [
                    {"command": ""},
                    {"command": "ok1"},
                    {"command": "ok2", "timeout_ms": True},
                    {"command": "ok3", "disabled": True},
                    {"command": "ok4", "matcher": ""},
                ]
            }
        },
    )
    regs = registry.load()
    assert [r.command for r in regs] == ["ok1"]


def test_unknown_event_and_timeout_bounds(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={
            "hooks": {
                "NopeEvent": [{"command": "x"}],
                "Stop": [
                    {"command": "a", "timeout_ms": 99999999},
                    {"command": "b", "timeout_ms": 0},
                ],
            }
        },
    )
    regs = registry.load()
    assert [r.command for r in regs] == ["a"]
    assert regs[0].timeout_ms == MAX_TIMEOUT_MS


def test_matcher_patterns():
    assert matches_tool("*", "write_file")
    assert matches_tool("write_file|edit_file", "edit_file")
    assert matches_tool(" write_file | edit_file ", "write_file")
    assert not matches_tool("write_file", "read_file")
    assert not matches_tool("write_file", "")


def test_registration_matches_non_tool_events_always(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={"hooks": {"Stop": [{"matcher": "write_file", "command": "x"}]}},
    )
    reg = registry.load()[0]
    # Stop 事件下 matcher 不生效（默认全匹配）——否则用户配置的 Stop hook 会静默失联
    assert registration_matches(reg, HookEvent.STOP, tool_name="") is True
    assert registration_matches(reg, HookEvent.PRE_TOOL_USE, tool_name="write_file") is False


def test_user_hooks_load_before_project_hooks(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={"hooks": {"Stop": [{"command": "user-hook"}]}},
        project_payload={"hooks": {"Stop": [{"command": "project-hook"}]}},
    )
    regs = registry.load()
    assert [r.scope for r in regs] == ["user", "project"]
    assert [r.command for r in regs] == ["user-hook", "project-hook"]


def test_missing_files_are_silently_skipped(tmp_path):
    registry = _registry(tmp_path)
    assert registry.load() == []


def test_disabled_entry_hidden_by_default_visible_with_include_disabled(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={
            "hooks": {
                "Stop": [
                    {"command": "off-hook", "disabled": True},
                    {"command": "on-hook"},
                ]
            }
        },
    )
    default_regs = registry.load()
    assert [r.command for r in default_regs] == ["on-hook"]
    assert default_regs[0].disabled is False

    visible_regs = registry.load(include_disabled=True)
    assert [r.command for r in visible_regs] == ["off-hook", "on-hook"]
    assert [r.disabled for r in visible_regs] == [True, False]
    assert default_regs[0].digest == visible_regs[1].digest


def test_disabled_entry_digest_matches_field_whitelist(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={"hooks": {"Stop": [{"command": "c1", "disabled": True}]}},
    )
    reg = registry.load(include_disabled=True)[0]
    source_path = str(tmp_path / "home" / "hooks.json")
    assert reg.digest == compute_hook_digest(
        {
            "event": "Stop",
            "command": "c1",
            "matcher": "*",
            "timeout_ms": DEFAULT_TIMEOUT_MS,
        },
        source_path,
    )


def test_invalid_disabled_value_skipped_in_both_modes(tmp_path):
    registry = _registry(
        tmp_path,
        user_payload={
            "hooks": {
                "Stop": [
                    {"command": "bad", "disabled": "true"},
                    {"command": "good"},
                ]
            }
        },
    )
    assert [r.command for r in registry.load()] == ["good"]
    assert [r.command for r in registry.load(include_disabled=True)] == ["good"]


def test_project_scope_disabled_visible_with_include_disabled(tmp_path):
    registry = _registry(
        tmp_path,
        project_payload={
            "hooks": {
                "PreToolUse": [{"command": "proj-off", "disabled": True}],
            }
        },
    )
    assert registry.load(workspace_root=tmp_path / "proj") == []
    regs = registry.load(workspace_root=tmp_path / "proj", include_disabled=True)
    assert [r.command for r in regs] == ["proj-off"]
    assert regs[0].disabled is True
