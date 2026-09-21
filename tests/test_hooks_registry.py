"""C2：Hooks 配置加载与 matcher 测试（H-T1 / H-T2）。"""

from __future__ import annotations

import json
from pathlib import Path

from core.hooks.registry import (
    HookRegistry,
    matches_tool,
    registration_matches,
)
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
                    {"command": ""},  # 缺 command → 跳过
                    {"command": "ok1"},
                    {"command": "ok2", "timeout_ms": True},  # bool 不是合法超时 → 跳过
                    {"command": "ok3", "disabled": True},  # disabled → 跳过
                    {"command": "ok4", "matcher": ""},  # 空 matcher → 跳过
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
                "NopeEvent": [{"command": "x"}],  # 未知事件 → 跳过该事件
                "Stop": [
                    {"command": "a", "timeout_ms": 99999999},  # 超上限 → 截断
                    {"command": "b", "timeout_ms": 0},  # 非正数 → 跳过
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
    # PreToolUse 事件下事件不同 → 不匹配
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
