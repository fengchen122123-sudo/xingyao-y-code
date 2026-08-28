import pytest

from core.command_policy import (
    classify_command_risk,
    is_dangerous_command,
    parse_manual_command_rule,
)
from core.permission_policy import (
    PermissionAction,
    PermissionDecision,
    PermissionFacts,
    PermissionRisk,
    decide_permission,
)


@pytest.mark.parametrize(
    ("mode", "risk", "expected"),
    [
        ("confirm", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("confirm", PermissionRisk.NORMAL_MUTATION, PermissionAction.ASK),
        ("confirm", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ASK),
        ("accept_edits", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("accept_edits", PermissionRisk.NORMAL_MUTATION, PermissionAction.ALLOW),
        ("accept_edits", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ASK),
        ("plan", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("plan", PermissionRisk.NORMAL_MUTATION, PermissionAction.DENY),
        ("plan", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.DENY),
        ("full", PermissionRisk.QUERY, PermissionAction.ALLOW),
        ("full", PermissionRisk.NORMAL_MUTATION, PermissionAction.ALLOW),
        ("full", PermissionRisk.SENSITIVE_MUTATION, PermissionAction.ALLOW),
    ],
)
def test_mode_matrix(mode, risk, expected):
    decision = decide_permission(PermissionFacts(mode=mode, risk=risk))
    assert decision.action is expected


@pytest.mark.parametrize("mode", ["confirm", "accept_edits", "plan", "full"])
def test_catastrophic_always_denied(mode):
    decision = decide_permission(
        PermissionFacts(
            mode=mode,
            risk=PermissionRisk.CATASTROPHIC,
            task_grant_hit=True,
            project_rule_hit=True,
            outside_workspace=True,
            sandbox_restricted=False,
        )
    )
    assert decision == PermissionDecision(
        PermissionAction.DENY,
        "catastrophic_denied",
    )


def test_plan_internal_tools_remain_allowed():
    decision = decide_permission(
        PermissionFacts(
            mode="plan",
            risk=PermissionRisk.NORMAL_MUTATION,
            plan_internal=True,
        )
    )
    assert decision.action is PermissionAction.ALLOW
    assert decision.reason == "plan_internal_allow"


def test_sandbox_boundary_precedes_whitelists():
    decision = decide_permission(
        PermissionFacts(
            mode="full",
            risk=PermissionRisk.NORMAL_MUTATION,
            outside_workspace=True,
            sandbox_restricted=True,
            project_rule_hit=True,
        )
    )
    assert decision.action is PermissionAction.ASK
    assert decision.reason == "sandbox_boundary_requires_confirmation"


def test_project_rule_precedes_mode_default():
    decision = decide_permission(
        PermissionFacts(
            mode="confirm",
            risk=PermissionRisk.SENSITIVE_MUTATION,
            project_rule_hit=True,
        )
    )
    assert decision.action is PermissionAction.ALLOW
    assert decision.reason == "project_rule_hit"


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff --stat",
        "git log -5 --oneline",
        "git show HEAD",
        "git rev-parse --show-toplevel",
        "git ls-files",
        "rg permission core",
        "Get-Content README.md",
        "python --version",
    ],
)
def test_read_only_commands_are_queries(command):
    assert classify_command_risk(command) is PermissionRisk.QUERY


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m change",
        "git push origin dev",
        "git reset --soft HEAD~1",
        "git merge feature",
        "git rebase dev",
        "Remove-Item file.txt",
        "npm install lodash",
        "pip uninstall requests",
        "Stop-Process -Id 1234",
    ],
)
def test_sensitive_commands_require_sensitive_policy(command):
    assert classify_command_risk(command) is PermissionRisk.SENSITIVE_MUTATION


def test_build_command_is_normal_mutation():
    assert classify_command_risk("npm run build") is PermissionRisk.NORMAL_MUTATION


@pytest.mark.parametrize(
    "command",
    [
        "git branch -D victim",
        "git config --global user.name attacker",
        "git restore file.txt",
        "git worktree remove temporary-worktree",
    ],
)
def test_unknown_or_mutating_git_subcommands_are_not_queries(command):
    assert classify_command_risk(command) is PermissionRisk.SENSITIVE_MUTATION


def test_help_flag_does_not_make_an_arbitrary_program_a_query():
    assert (
        classify_command_risk("python malicious.py --help")
        is PermissionRisk.NORMAL_MUTATION
    )


def test_recursive_delete_of_workspace_or_home_is_catastrophic(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    assert is_dangerous_command(
        f'Remove-Item -LiteralPath "{workspace}" -Recurse -Force',
        workdir=str(workspace),
        home=str(home),
    )
    assert is_dangerous_command(
        f'Remove-Item -LiteralPath "{home}" -Recurse -Force',
        workdir=str(workspace),
        home=str(home),
    )
    assert is_dangerous_command(
        f'rm -rf "{workspace}"',
        workdir=str(workspace),
        home=str(home),
    )


def test_composed_or_inline_commands_are_never_queries():
    assert (
        classify_command_risk("git status; Remove-Item x")
        is PermissionRisk.SENSITIVE_MUTATION
    )
    assert classify_command_risk('python -c "print(1)"') is PermissionRisk.SENSITIVE_MUTATION


def test_manual_rule_preserves_complete_safe_token_prefix():
    assert parse_manual_command_rule("git push origin") == ("git", "push", "origin")


@pytest.mark.parametrize(
    "command",
    [
        "git status | Out-File x",
        'python -c "print(1)"',
        "Remove-Item -Recurse -Force C:\\",
    ],
)
def test_manual_rule_rejects_unpersistable_commands(command):
    with pytest.raises(ValueError):
        parse_manual_command_rule(command)
