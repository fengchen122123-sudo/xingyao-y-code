"""ZCode 导入设计 §2.5 的执行器/输出契约增强测试。

- 退出码契约：0 放行（解析 stdout）、2 = 主动阻断（stderr 末行作 reason）、
  其他非 0 = failed；
- stdout camelCase 别名（stopReason/additionalContext/permissionDecision）；
- 模板变量：命令展开（纯函数）+ 环境变量注入（真实子进程）。

沿用 test_hooks_executor.py 的真实子进程风格（python 解释器 + tmp 脚本）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.hooks.executor import (
    _build_hook_env,
    expand_template_variables,
    run_hook,
)
from core.hooks.types import HookEvent, HookInput, HookRegistration


def _registration(command: str, event=HookEvent.PRE_TOOL_USE, timeout_ms: int = 10000) -> HookRegistration:
    return HookRegistration(
        event=event,
        matcher="*",
        command=command,
        timeout_ms=timeout_ms,
        status_message="",
        source_path="test",
        scope="user",
        digest="digest",
    )


def _hook_input() -> HookInput:
    return HookInput(
        hook_event_name="PreToolUse",
        session_id="sess-42",
        turn_id="t1",
        cwd=str(Path.cwd()),
        permission_mode="confirm",
        timestamp="2026-10-25T10:00:00+08:00",
    )


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_exit_code_2_blocks_with_stderr_tail_as_reason(tmp_path):
    command = _script(
        tmp_path,
        "hook_block.py",
        "import sys\n"
        "print('noise line')\n"
        "print('危险命令已被策略拦截', file=sys.stderr)\n"
        "sys.exit(2)\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    # 2 = 主动阻断，不是失败：status ok + decision=block，走既有合并语义
    assert outcome.status == "ok"
    assert outcome.exit_code == 2
    assert outcome.output is not None
    assert outcome.output.decision == "block"
    assert outcome.output.reason == "危险命令已被策略拦截"


def test_exit_code_2_without_stderr_gets_default_reason(tmp_path):
    command = _script(tmp_path, "hook_block_silent.py", "import sys\nsys.exit(2)\n")
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "ok"
    assert outcome.output.decision == "block"
    assert "退出码 2" in outcome.output.reason


def test_other_nonzero_exit_still_failed(tmp_path):
    command = _script(tmp_path, "hook_fail.py", "import sys\nsys.exit(3)\n")
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "failed"
    assert outcome.exit_code == 3
    assert outcome.output is None


def test_stdout_camelcase_aliases_parsed(tmp_path):
    command = _script(
        tmp_path,
        "hook_camel.py",
        "import json\n"
        "print(json.dumps({\n"
        "    'decision': 'block',\n"
        "    'stopReason': 'camel-reason',\n"
        "    'additionalContext': 'camel-ctx',\n"
        "    'permissionDecision': 'deny',\n"
        "}))\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "ok"
    output = outcome.output
    assert output.decision == "block"
    assert output.reason == "camel-reason"
    assert output.additional_context == "camel-ctx"
    assert output.permission_decision == "deny"


def test_snake_case_still_wins_over_camelcase_alias(tmp_path):
    command = _script(
        tmp_path,
        "hook_both.py",
        "import json\n"
        "print(json.dumps({'reason': 'snake', 'stopReason': 'camel'}))\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.output.reason == "snake"


def test_template_variables_expanded_in_command():
    command = "run-check --project ${ZCODE_PROJECT_DIR} --session ${CLAUDE_SESSION_ID}"
    expanded = expand_template_variables(command, _hook_input())
    assert "run-check --project " + str(Path.cwd()) in expanded
    assert "--session sess-42" in expanded
    # 三组拼写等价（YCODE/ZCODE/CLAUDE）
    assert expand_template_variables("${YCODE_PROJECT_DIR}", _hook_input()) == str(Path.cwd())
    assert expand_template_variables("${CLAUDE_PROJECT_DIR}", _hook_input()) == str(Path.cwd())


def test_unknown_template_variable_left_untouched():
    command = "echo ${NOT_A_KNOWN_VAR} ${CLAUDE_PLUGIN_ROOT}"
    assert expand_template_variables(command, _hook_input()) == command


def test_empty_field_not_expanded():
    empty_input = HookInput(
        hook_event_name="Stop",
        session_id="",
        turn_id="",
        cwd="",
        permission_mode="confirm",
        timestamp="",
    )
    assert expand_template_variables("${CLAUDE_SESSION_ID}", empty_input) == "${CLAUDE_SESSION_ID}"


def test_env_injection_visible_to_hook_process(tmp_path):
    command = _script(
        tmp_path,
        "hook_env.py",
        "import json, os\n"
        "print(json.dumps({\n"
        "    'additional_context': os.environ.get('ZCODE_PROJECT_DIR', ''),\n"
        "    'reason': os.environ.get('CLAUDE_SESSION_ID', ''),\n"
        "}))\n",
    )
    outcome = run_hook(_registration(command, event=HookEvent.POST_TOOL_USE), _hook_input())
    assert outcome.status == "ok"
    assert outcome.output.additional_context == str(Path.cwd())
    assert outcome.output.reason == "sess-42"


def test_build_hook_env_does_not_inject_empty_values():
    env = _build_hook_env(
        HookInput(
            hook_event_name="Stop",
            session_id="",
            turn_id="",
            cwd="/some/dir",
            permission_mode="confirm",
            timestamp="",
        )
    )
    assert env["ZCODE_PROJECT_DIR"] == "/some/dir"
    assert "CLAUDE_SESSION_ID" not in env  # 空 session 不注入（不清空继承值）


def test_post_tool_use_stdin_includes_tool_response_superset(tmp_path):
    """导入的 ZCode hook 读 tool_response；星瑶 stdin 同值双写（超集）。"""
    command = _script(
        tmp_path,
        "hook_stdin.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "assert data['tool_response'] == data['tool_result_preview']\n"
        "print(json.dumps({'additional_context': 'superset-ok'}))\n",
    )
    hook_input = HookInput(
        hook_event_name="PostToolUse",
        session_id="sess-42",
        turn_id="t1",
        cwd=str(Path.cwd()),
        permission_mode="confirm",
        timestamp="2026-10-25T10:00:00+08:00",
        tool_name="write_file",
        tool_result_preview="file written: a.txt (120 lines)",
    )
    outcome = run_hook(
        _registration(command, event=HookEvent.POST_TOOL_USE), hook_input
    )
    assert outcome.status == "ok"
    assert outcome.output.additional_context == "superset-ok"
