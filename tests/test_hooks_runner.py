"""C5：HookRunner 编排测试（H-T5 / H-T14 / H-T15 / H-T17）。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import core.hooks.runner as runner_module
from core.hooks.registry import HookRegistry
from core.hooks.runner import HookRunner
from core.hooks.trust import HookTrustStore, normalize_workspace_identity
from core.hooks.types import REDACTED_PLACEHOLDER, HookEvent, HookInput


def _script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{script}"'


def _hook_input(cwd, tool_input=None) -> HookInput:
    return HookInput(
        hook_event_name="PreToolUse",
        session_id="s1",
        turn_id="t1",
        cwd=str(cwd),
        permission_mode="full",
        timestamp="2026-09-21T10:00:00+08:00",
        tool_name="write_file",
        tool_input=tool_input,
        risk_level="medium",
        permission_decision="ask",
    )


def _make_runner(tmp_path, hooks_payload, *, full_input=False):
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    hooks_path = proj / ".ycode" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(
        json.dumps({"version": 1, "hooks": hooks_payload}, ensure_ascii=False),
        encoding="utf-8",
    )
    registry = HookRegistry(
        user_hooks_path=home / "hooks.json", project_hooks_path=hooks_path
    )
    trust = HookTrustStore(store_path=tmp_path / "hook_trust.json")
    runner = HookRunner(
        registry, trust, project_full_input_resolver=lambda: full_input
    )
    return runner, proj, trust


def _grant_all(runner, proj, trust) -> None:
    """把项目级全部 hook 置为 trusted（模拟用户在 GUI 里确认）。"""
    identity = normalize_workspace_identity(str(proj))
    for reg in runner.registry.load(workspace_root=str(proj)):
        trust.grant(
            workspace_identity=identity,
            digest=reg.digest,
            source_path=reg.source_path,
            command=reg.command,
        )


def test_merge_deny_wins_and_contexts_accumulate(tmp_path):
    """H-T5：多 hook 合并——contexts 累加、permission 向最严格收敛。"""
    script_a = _script(
        tmp_path,
        "a.py",
        "import json\nprint(json.dumps("
        "{'additional_context': 'ctx-A', 'permission_decision': 'allow'}))\n",
    )
    script_b = _script(
        tmp_path,
        "b.py",
        "import json\nprint(json.dumps("
        "{'additional_context': 'ctx-B', 'permission_decision': 'deny', "
        "'reason': 'no'}))\n",
    )
    payload = {
        "PreToolUse": [
            {"matcher": "*", "command": script_a},
            {"matcher": "*", "command": script_b},
        ]
    }
    runner, proj, trust = _make_runner(tmp_path, payload)
    _grant_all(runner, proj, trust)

    result = runner.run(
        HookEvent.PRE_TOOL_USE, _hook_input(proj, tool_input={"file_path": "a.py"})
    )
    assert result.executed_count == 2
    assert result.additional_contexts == ["ctx-A", "ctx-B"]
    assert result.permission_override == "deny"


def test_revoke_during_execution_affects_later_hooks(tmp_path):
    """H-T14：前序 hook 运行期间撤销信任 → 后续 hook 不执行（不缓存 admission）。"""
    trust_path = tmp_path / "hook_trust.json"
    revoke_body = (
        "import json, pathlib\n"
        f"path = pathlib.Path(r'{trust_path}')\n"
        "data = json.loads(path.read_text(encoding='utf-8'))\n"
        "revoked = data.setdefault('revoked', [])\n"
        "for key in list(data.get('records', {})):\n"
        "    revoked.append(key)\n"
        "data['records'] = {}\n"
        "path.write_text(json.dumps(data), encoding='utf-8')\n"
        "print('{}')\n"
    )
    script_a = _script(tmp_path, "a.py", revoke_body)
    script_b = _script(tmp_path, "b.py", "print('{}')\n")
    payload = {
        "PreToolUse": [
            {"matcher": "*", "command": script_a},
            {"matcher": "*", "command": script_b},
        ]
    }
    runner, proj, trust = _make_runner(tmp_path, payload)
    _grant_all(runner, proj, trust)

    result = runner.run(
        HookEvent.PRE_TOOL_USE, _hook_input(proj, tool_input={"file_path": "a.py"})
    )
    # A 执行时撤销了全部信任 → B 变为 revoked → 不再执行
    assert result.executed_count == 1
    assert any("未获信任" in failure for failure in result.failures)


def test_cancel_event_stops_subsequent_hooks(tmp_path, monkeypatch):
    """H-T15：cancel_event 生效 → 停止后续 hook，已执行结果保留。"""
    script_a = _script(
        tmp_path,
        "a.py",
        "import json\nprint(json.dumps({'additional_context': 'first'}))\n",
    )
    script_b = _script(
        tmp_path,
        "b.py",
        "import json\nprint(json.dumps({'additional_context': 'second'}))\n",
    )
    payload = {
        "PreToolUse": [
            {"matcher": "*", "command": script_a},
            {"matcher": "*", "command": script_b},
        ]
    }
    runner, proj, trust = _make_runner(tmp_path, payload)
    _grant_all(runner, proj, trust)

    cancel = threading.Event()
    real_run_hook = runner_module.run_hook

    def fake_run_hook(registration, hook_input, **kwargs):
        outcome = real_run_hook(registration, hook_input, **kwargs)
        cancel.set()  # 第一个 hook 执行完立即取消
        return outcome

    monkeypatch.setattr(runner_module, "run_hook", fake_run_hook)
    result = runner.run(
        HookEvent.PRE_TOOL_USE,
        _hook_input(proj, tool_input={"file_path": "a.py"}),
        cancel_event=cancel,
    )
    assert result.executed_count == 1
    assert result.additional_contexts == ["first"]


def test_project_hook_input_redacted_by_default(tmp_path):
    """H-T17：项目级 hook 默认只拿到键名（值转占位符），防数据窃取。"""
    spy = _script(
        tmp_path,
        "spy.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps({'additional_context': "
        "json.dumps(data.get('tool_input'), ensure_ascii=False)}))\n",
    )
    payload = {"PreToolUse": [{"matcher": "*", "command": spy}]}
    runner, proj, trust = _make_runner(tmp_path, payload)
    _grant_all(runner, proj, trust)

    result = runner.run(
        HookEvent.PRE_TOOL_USE,
        _hook_input(proj, tool_input={"file_path": "secret.py", "content": "TOPSECRET"}),
    )
    received = json.loads(result.additional_contexts[0])
    assert received == {
        "file_path": REDACTED_PLACEHOLDER,
        "content": REDACTED_PLACEHOLDER,
    }
    assert "TOPSECRET" not in result.additional_contexts[0]


def test_project_hook_full_input_when_explicitly_enabled(tmp_path):
    """显式开启 project_hook_full_input 后，项目级 hook 才收到完整入参。"""
    spy = _script(
        tmp_path,
        "spy.py",
        "import json, sys\n"
        "data = json.load(sys.stdin)\n"
        "print(json.dumps({'additional_context': "
        "json.dumps(data.get('tool_input'), ensure_ascii=False)}))\n",
    )
    payload = {"PreToolUse": [{"matcher": "*", "command": spy}]}
    runner, proj, trust = _make_runner(tmp_path, payload, full_input=True)
    _grant_all(runner, proj, trust)

    result = runner.run(
        HookEvent.PRE_TOOL_USE,
        _hook_input(proj, tool_input={"file_path": "secret.py", "content": "TOPSECRET"}),
    )
    received = json.loads(result.additional_contexts[0])
    assert received == {"file_path": "secret.py", "content": "TOPSECRET"}


def test_untrusted_project_hook_is_not_executed(tmp_path):
    """H-T11 集成：未信任的项目级 hook 不执行（fail-closed 到 pending）。"""
    marker = tmp_path / "executed.marker"
    script = _script(
        tmp_path,
        "mark.py",
        "import pathlib\n"
        f"pathlib.Path(r'{marker}').write_text('ran', encoding='utf-8')\n"
        "print('{}')\n",
    )
    payload = {"PreToolUse": [{"matcher": "*", "command": script}]}
    runner, proj, trust = _make_runner(tmp_path, payload)
    # 不调用 _grant_all：未信任
    result = runner.run(
        HookEvent.PRE_TOOL_USE, _hook_input(proj, tool_input={"file_path": "a.py"})
    )
    assert result.executed_count == 0
    assert not marker.exists()
    assert any("未获信任" in failure for failure in result.failures)
