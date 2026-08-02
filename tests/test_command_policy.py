from core.command_policy import command_matches_session_rule, session_rule_for_command


def test_git_status_rule_does_not_authorize_other_git_subcommands():
    rule = session_rule_for_command("git status")
    assert rule == ("git", "status")
    assert command_matches_session_rule("git status --short", rule)
    assert not command_matches_session_rule("git reset --hard", rule)
    assert not command_matches_session_rule("git push --force", rule)


def test_shell_composition_is_never_session_allowlisted():
    assert session_rule_for_command("git status; whoami") is None
    assert session_rule_for_command("git status && whoami") is None
    assert session_rule_for_command("git status | Out-File result.txt") is None
    assert session_rule_for_command("git status\nwhoami") is None


def test_version_rule_keeps_flag_and_requires_exact_command():
    rule = session_rule_for_command("python --version")

    assert rule == ("python", "--version")
    assert command_matches_session_rule("python --version", rule)
    assert not command_matches_session_rule("python script.py", rule)
    assert not command_matches_session_rule("python --version extra", rule)


def test_single_token_rule_does_not_authorize_arguments():
    rule = session_rule_for_command("whoami")

    assert rule == ("whoami",)
    assert command_matches_session_rule("whoami", rule)
    assert not command_matches_session_rule("whoami /all", rule)


def test_option_led_rule_keeps_full_argv_and_requires_exact_command():
    powershell_rule = session_rule_for_command("powershell -File script.ps1")
    git_rule = session_rule_for_command("git -c user.name=test status")

    assert powershell_rule == ("powershell", "-File", "script.ps1")
    assert git_rule == ("git", "-c", "user.name=test", "status")
    assert command_matches_session_rule("powershell -File script.ps1", powershell_rule)
    assert not command_matches_session_rule("powershell -File other.ps1", powershell_rule)
    assert not command_matches_session_rule("powershell -File script.ps1 extra", powershell_rule)
    assert command_matches_session_rule("git -c user.name=test status", git_rule)
    assert not command_matches_session_rule("git -c user.name=test status --short", git_rule)


def test_help_query_after_subcommand_keeps_full_argv_and_matches_exactly():
    commands = (
        "pip install --help",
        "git status --help",
        "python script.py --help",
    )

    for command in commands:
        rule = session_rule_for_command(command)
        assert rule == tuple(command.split())
        assert command_matches_session_rule(command, rule)
        assert not command_matches_session_rule(f"{command} extra", rule)

    pip_rule = session_rule_for_command("pip install --help")
    assert not command_matches_session_rule("pip install untrusted-package", pip_rule)
