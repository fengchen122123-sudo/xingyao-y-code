from __future__ import annotations

import locale
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from core.hooks.executor import run_hook
from core.hooks.types import HookEvent, HookInput, HookRegistration


def _registration(command: str, timeout_ms: int = 10000) -> HookRegistration:
    return HookRegistration(
        event=HookEvent.STOP,
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
        hook_event_name="Stop",
        session_id="s1",
        turn_id="t1",
        cwd=str(Path.cwd()),
        permission_mode="full",
        timestamp="2026-09-21T10:00:00+08:00",
    )


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def test_hook_executes_and_parses_json_stdout(tmp_path):
    command = _script(
        tmp_path,
        "hook_ok.py",
        "import json\n"
        "print(json.dumps({'continue': True, 'additional_context': 'ctx-1'}))\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "ok"
    assert outcome.exit_code == 0
    assert outcome.output is not None
    assert outcome.output.additional_context == "ctx-1"


def test_hook_receives_stdin_payload(tmp_path):
    command = _script(
        tmp_path,
        "hook_echo.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps({'additional_context': data.get('hook_event_name', '')}))\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.output is not None
    assert outcome.output.additional_context == "Stop"


def test_timeout_does_not_block(tmp_path):
    command = _script(tmp_path, "hook_sleep.py", "import time\ntime.sleep(10)\n")
    outcome = run_hook(_registration(command, timeout_ms=300), _hook_input())
    assert outcome.status == "timeout"
    assert outcome.output is None


def test_nonzero_exit_does_not_block(tmp_path):
    command = _script(tmp_path, "hook_fail.py", "import sys\nsys.exit(3)\n")
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "failed"
    assert outcome.exit_code == 3
    assert outcome.output is None


def test_non_json_stdout_is_no_intervention(tmp_path):
    command = _script(tmp_path, "hook_plain.py", "print('hello')\n")
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "ok"
    assert outcome.output is None


def test_cancelled_before_start_is_skipped(tmp_path):
    cancel = threading.Event()
    cancel.set()
    command = _script(tmp_path, "hook_ok.py", "print('{}')\n")
    outcome = run_hook(_registration(command), _hook_input(), cancel_event=cancel)
    assert outcome.status == "skipped"


def test_chinese_payload_roundtrips_through_hook(tmp_path):
    command = _script(
        tmp_path,
        "echo_cwd.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps({'additional_context': data.get('cwd', '')}, "
        "ensure_ascii=True))\n",
    )
    chinese_input = replace(_hook_input(), cwd=str(tmp_path))
    outcome = run_hook(_registration(command), chinese_input)
    assert outcome.status == "ok"
    assert outcome.output is not None
    assert outcome.output.additional_context == str(tmp_path)


def test_hook_stdout_non_utf8_chinese_is_tolerated(tmp_path):
    preferred = locale.getpreferredencoding(False)
    if preferred.lower().startswith("utf"):
        pytest.skip("本机默认编码已是 UTF-8，GBK 回退路径不适用")
    try:
        "中文".encode(preferred)
    except (UnicodeEncodeError, LookupError):
        pytest.skip("本机 locale 编码无法表示中文，跳过回退用例")

    command = _script(
        tmp_path,
        "gbk_out.py",
        "import json\n"
        "print(json.dumps({'additional_context': '中文回显'}, ensure_ascii=False))\n",
    )
    outcome = run_hook(_registration(command), _hook_input())
    assert outcome.status == "ok"
    assert outcome.output is not None
    assert outcome.output.additional_context == "中文回显"
