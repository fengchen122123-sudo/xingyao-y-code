"""ZCode hooks 导入转换器测试（ZCode 导入设计 §2）。

纯转换层直接调函数穷举；plan/apply/rollback 用 tmp 目录构造来源与目标，
绝不触碰真实用户目录。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.hooks.zcode_import import (
    apply_plan,
    convert_entry,
    convert_file,
    convert_matcher,
    plan_import,
    rollback_import,
    target_hooks_path,
)


# ── convert_matcher ────────────────────────────────────────────


def test_matcher_omitted_is_wildcard():
    assert convert_matcher("PreToolUse", None) == ("*", "")
    assert convert_matcher("PreToolUse", "") == ("*", "")
    assert convert_matcher("PreToolUse", "   ") == ("*", "")


def test_matcher_exact_alternation_maps_tool_names():
    assert convert_matcher("PreToolUse", "Bash") == ("execute_command", "")
    assert convert_matcher("PreToolUse", "^Bash$") == ("execute_command", "")
    assert convert_matcher("PreToolUse", "^(Bash|Read)$") == ("execute_command|read_file", "")
    assert convert_matcher("PostToolUse", "Edit | Write") == ("edit_file|write_file", "")
    # ApplyPatch 是 Write/Edit 的别名 → 多值 matcher
    assert convert_matcher("PreToolUse", "^ApplyPatch$") == ("write_file|edit_file", "")
    # 未知名保留原样（匹配不上任何星瑶工具，无害）
    assert convert_matcher("PreToolUse", "^Frobnicate$") == ("Frobnicate", "")


def test_matcher_regex_semantics_rejected_not_degraded():
    # 含元字符：无法无损转换 → 跳过，绝不降级为 *（那会命中所有工具）
    matcher, reason = convert_matcher("PreToolUse", "^Bash.*$")
    assert matcher is None and reason
    matcher, _ = convert_matcher("PreToolUse", "(?:Bash)")
    assert matcher is None


def test_matcher_event_specific_policies():
    # SessionStart 匹配子类型：全子类集 → 全匹配；出现工具名 → 跳过
    assert convert_matcher("SessionStart", "^(startup|resume|clear|compact)$") == ("*", "")
    assert convert_matcher("SessionStart", "startup")[0] == "*"
    matcher, reason = convert_matcher("SessionStart", "^Bash$")
    assert matcher is None and reason
    # Stop 的 matcher 匹配回复文本：星瑶无对应语义，任何显式 matcher → 跳过
    matcher, reason = convert_matcher("Stop", "summary")
    assert matcher is None and reason


# ── convert_entry ──────────────────────────────────────────────


def test_entry_command_type_with_seconds_timeout_trap():
    entry, reason = convert_entry(
        {"type": "command", "command": "echo hi", "timeout": 30}
    )
    assert reason == ""
    assert entry == {"command": "echo hi", "timeout_ms": 30000}


def test_entry_timeout_ms_takes_precedence_over_seconds():
    entry, _ = convert_entry(
        {"type": "command", "command": "echo hi", "timeout": 30, "timeoutMs": 1500}
    )
    assert entry["timeout_ms"] == 1500


def test_entry_timeout_falls_back_to_config_default_then_60000():
    entry, _ = convert_entry(
        {"type": "command", "command": "echo hi"}, config_default_timeout_ms=2000
    )
    assert entry["timeout_ms"] == 2000
    entry, _ = convert_entry({"type": "command", "command": "echo hi"})
    assert entry["timeout_ms"] == 60000


def test_entry_timeout_clamped_to_star_upper_bound():
    entry, _ = convert_entry({"type": "command", "command": "echo hi", "timeoutMs": 10**9})
    assert entry["timeout_ms"] == 300000


def test_entry_process_type_joins_argv_and_maps_status_message():
    entry, reason = convert_entry(
        {
            "type": "process",
            "command": "tools/check.sh",
            "args": ["--fix", "a b.txt"],
            "timeoutMs": 2500,
            "statusMessage": "checking",
            "async": True,  # ZCode 文档：async 无运行时效果，忽略
        }
    )
    assert reason == ""
    assert entry["timeout_ms"] == 2500
    assert entry["status_message"] == "checking"
    assert "tools/check.sh" in entry["command"]
    assert "--fix" in entry["command"]
    # 含空格参数必须被引号包裹（Windows list2cmdline / POSIX shlex 均如此）
    assert '"a b.txt"' in entry["command"]


def test_entry_rejects_unknown_type_and_missing_command():
    entry, reason = convert_entry({"type": "webhook"})
    assert entry is None and reason
    entry, reason = convert_entry({"type": "command", "command": "  "})
    assert entry is None and reason
    entry, reason = convert_entry({"type": "process", "command": "x", "args": "nope"})
    assert entry is None and reason


# ── convert_file：事件映射与跳过报告 ──────────────────────────


def _config_payload(*, enabled=True):
    return {
        "hooks": {
            "enabled": enabled,
            "timeoutMs": 90000,
            "events": {
                "PreToolUse": [
                    {
                        "matcher": "^(Write|Edit)$",
                        "hooks": [
                            {"type": "command", "command": "fmt.sh", "timeout": 20},
                            {"type": "command", "command": "lint.sh"},
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "audit.sh"}]}
                ],
                # Stop 的 matcher 匹配回复文本：显式给 matcher 触发「无对应语义」跳过
                "Stop": [
                    {
                        "matcher": "summary",
                        "hooks": [{"type": "command", "command": "notify.sh"}],
                    }
                ],
            },
        }
    }


def test_convert_file_maps_events_and_reports_skips():
    report = convert_file(_config_payload(), label="user", path="p", scope="user")
    assert report["status"] == "imported"
    assert len(report["entries"]) == 2
    first = report["entries"][0]
    assert first["event"] == "PreToolUse"
    assert first["matcher"] == "write_file|edit_file"
    assert first["timeout_ms"] == 20000  # timeout 秒 → 毫秒
    assert first["imported_from"] == "zcode:user"
    second = report["entries"][1]
    assert second["timeout_ms"] == 90000  # 配置级默认超时兜底
    # UserPromptSubmit 无事件点：条目点名跳过；Stop 的显式 matcher 无语义：跳过
    assert {item["event"] for item in report["skipped"]} == {"UserPromptSubmit", "Stop"}


def test_convert_file_respects_zcode_enabled_gate():
    report = convert_file(_config_payload(enabled=False), label="user", path="p", scope="user")
    assert report["status"] == "skipped"
    assert "未启用" in report["reason"]
    assert report["entries"] == []


def test_convert_file_plugin_shape_has_no_enabled_gate():
    plugin_payload = {
        "hooks": {
            "PostToolUse": [{"hooks": [{"type": "process", "command": "t", "args": ["-x"]}]}]
        }
    }
    report = convert_file(plugin_payload, label="plugin", path="p", scope="user")
    assert report["status"] == "imported"
    assert report["entries"][0]["event"] == "PostToolUse"
    assert report["entries"][0]["matcher"] == "*"


# ── plan / apply / rollback（tmp 目录，project scope）──────────


def _write(path: Path, payload) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_plan_import_missing_files_are_silent(tmp_path):
    plan = plan_import(
        sources=[{"label": "user", "path": tmp_path / "nope.json", "scope": "user"}]
    )
    assert plan["files"] == []


def test_plan_import_reports_bad_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    plan = plan_import(sources=[{"label": "user", "path": bad, "scope": "user"}])
    assert plan["files"][0]["status"] == "skipped"
    assert "JSON 非法" in plan["files"][0]["reason"]


def test_apply_and_rollback_project_scope(tmp_path):
    source = _write(tmp_path / ".zcode" / "config.json", _config_payload())
    plan = plan_import(sources=[{"label": "project-config", "path": source, "scope": "project"}])
    summary = apply_plan(plan, workspace_root=str(tmp_path))
    assert summary["written"] == 2

    target = target_hooks_path("project", str(tmp_path))
    data = json.loads(target.read_text(encoding="utf-8"))
    entries = data["hooks"]["PreToolUse"]
    assert len(entries) == 2
    assert entries[0]["imported_from"] == "zcode:project-config"
    # 星瑶侧字段名：timeout_ms / status_message
    assert entries[0]["timeout_ms"] == 20000

    # 重复导入：同事件同命令视为重复，跳过并计数
    summary_again = apply_plan(plan, workspace_root=str(tmp_path))
    assert summary_again["written"] == 0
    assert summary_again["duplicates"] == 2

    # 混入一条原生条目后回滚：只删 imported_from=zcode: 的
    data["hooks"]["PreToolUse"].append(
        {"command": "native.sh", "matcher": "*", "timeout_ms": 60000}
    )
    target.write_text(json.dumps(data), encoding="utf-8")
    result = rollback_import(workspace_root=str(tmp_path))
    assert result["removed"] == 2
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["hooks"]["PreToolUse"] == [
        {"command": "native.sh", "matcher": "*", "timeout_ms": 60000}
    ]


def test_apply_skips_unparseable_target_without_overwriting(tmp_path):
    target = target_hooks_path("project", str(tmp_path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{corrupt", encoding="utf-8")
    source = _write(tmp_path / ".zcode" / "config.json", _config_payload())
    plan = plan_import(sources=[{"label": "project-config", "path": source, "scope": "project"}])
    summary = apply_plan(plan, workspace_root=str(tmp_path))
    assert summary["written"] == 0
    assert summary["errors"]
    assert target.read_text(encoding="utf-8") == "{corrupt"  # 坏文件未被覆盖
