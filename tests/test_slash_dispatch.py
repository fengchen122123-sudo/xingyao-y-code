import pytest

from core.slash_commands import local
from core.slash_commands.dispatch import dispatch_command
from core.slash_commands.specs import register_builtin_commands


@pytest.fixture(autouse=True)
def _ensure_builtins():
    register_builtin_commands()


def test_path_like_text_is_not_a_command():
    result = dispatch_command("/usr/bin 是什么")

    assert result.handled is False


def test_prompt_command_expands_args_into_prompt():
    result = dispatch_command("/fix boom")

    assert result.handled is True
    assert result.kind == "prompt"
    assert "boom" in result.prompt


def test_remember_without_content_reports_error():
    result = dispatch_command("/remember   ")

    assert result.handled is True
    assert result.kind == "error"
    assert "用法" in result.error


def test_remember_success_becomes_notice(monkeypatch):
    monkeypatch.setattr(local, "append_to_daily_log", lambda entry: True)

    result = dispatch_command("/remember 用中文回答")

    assert result.handled is True
    assert result.kind == "notice"
    assert "记忆" in result.notice


def test_agent_command_becomes_action_with_type():
    result = dispatch_command("/agent explore 分析主流程")

    assert result.kind == "action"
    assert result.action["action"] == "spawn_subagent"
    assert result.action["subagent_type"] == "explore"


def test_workflow_with_bad_json_reports_json_error():
    result = dispatch_command("/workflow 发布 {bad}")

    assert result.kind == "error"
    assert "JSON" in result.error


def test_double_slash_is_escaped_text():
    result = dispatch_command("//fix 正文")

    assert result.handled is False


def test_compact_without_compress_capability_reports_error():
    result = dispatch_command("/compact", ctx={"compress": None})

    assert result.kind == "error"
    assert "不支持" in result.error
