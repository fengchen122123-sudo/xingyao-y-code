import pytest

from core.slash_commands import registry, tooling
from core.slash_commands.specs import register_builtin_commands


def test_agent_without_task_returns_usage():
    result = tooling.handle_agent("explore")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_agent_splits_type_and_task():
    result = tooling.handle_agent("explore 分析主流程")

    assert result["ok"] is True
    assert result["action"] == "spawn_subagent"
    assert result["subagent_type"] == "explore"
    assert result["task"] == "分析主流程"


def test_skill_without_name_returns_usage():
    result = tooling.handle_skill("   ")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_skill_allows_empty_requirement(monkeypatch):
    import core.skills as skills_mod

    monkeypatch.setattr(
        skills_mod,
        "get_active_skills_list",
        lambda: {
            "debug": {
                "name": "debug",
                "id": "/tmp/x/SKILL.md",
                "body": "技能正文",
                "content": "技能全文",
                "description": "调试技能",
            }
        },
    )
    result = tooling.handle_skill("debug 补充说明")

    assert result["ok"] is True
    assert result["action"] == "use_skill"
    assert result["skill"] == "debug"
    assert result["requirement"] == "补充说明"
    assert "技能正文" in result["content"]


def test_workflow_without_name_returns_usage():
    result = tooling.handle_workflow("")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_workflow_parses_json_args():
    result = tooling.handle_workflow('发版 {"tag":"v1"}')

    assert result["ok"] is True
    assert result["action"] == "run_workflow"
    assert result["name"] == "发版"
    assert result["args"] == {"tag": "v1"}


def test_workflow_without_args_defaults_to_empty_dict():
    result = tooling.handle_workflow("发版")

    assert result["ok"] is True
    assert result["args"] == {}


@pytest.mark.parametrize("bad", ['发版 {bad json}', '发版 {"tag":}'])
def test_workflow_with_invalid_json_reports_json_error(bad):
    result = tooling.handle_workflow(bad)

    assert result["ok"] is False
    assert "JSON" in result["msg"]


def test_commit_returns_action_with_confirm_required():
    result = tooling.handle_commit("修复登录")

    assert result["ok"] is True
    assert result["action"] == "commit"
    assert result["message"] == "修复登录"
    assert result["confirm_required"] is True


def test_commit_allows_blank_message():
    result = tooling.handle_commit("  ")

    assert result["ok"] is True
    assert result["message"] == ""


def test_commit_without_yes_is_not_confirmed():
    result = tooling.handle_commit("修复登录")

    assert result["action"] == "commit"
    assert result["confirmed"] is False
    assert result["message"] == "修复登录"
    assert result["confirm_required"] is True


def test_commit_with_yes_is_confirmed_and_strips_flag():
    result = tooling.handle_commit("--yes 修复登录")

    assert result["action"] == "commit"
    assert result["confirmed"] is True
    assert result["message"] == "修复登录"


def test_commit_with_bare_yes_confirms_with_empty_message():
    result = tooling.handle_commit("--yes")

    assert result["confirmed"] is True
    assert result["message"] == ""


def test_forget_returns_action_with_confirm_required_and_rejects_blank():
    result = tooling.handle_forget("旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["keyword"] == "旧偏好"
    assert result["confirm_required"] is True

    blank = tooling.handle_forget("   ")
    assert blank["ok"] is False
    assert "用法" in blank["msg"]


def test_forget_without_yes_is_not_confirmed():
    result = tooling.handle_forget("旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["keyword"] == "旧偏好"
    assert result["confirmed"] is False
    assert result["confirm_required"] is True


def test_forget_with_yes_is_confirmed_and_strips_flag():
    result = tooling.handle_forget("--yes 旧偏好")

    assert result["ok"] is True
    assert result["action"] == "forget_memory"
    assert result["confirmed"] is True
    assert result["keyword"] == "旧偏好"


def test_forget_with_bare_yes_returns_usage():
    result = tooling.handle_forget("--yes")

    assert result["ok"] is False
    assert "用法" in result["msg"]


def test_register_builtin_commands_registers_tooling_kinds():
    registry.reset()
    try:
        register_builtin_commands()
        register_builtin_commands()

        expected = {
            "/agent": "local",
            "/skill": "hybrid",
            "/workflow": "local",
            "/commit": "hybrid",
            "/forget": "local",
        }
        for name, kind in expected.items():
            spec = registry.get(name)
            assert spec is not None, name
            assert spec.kind == kind, name
            assert spec.handler is not None, name
    finally:
        registry.reset()
