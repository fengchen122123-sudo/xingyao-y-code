"""core/git/runner.py 单元测试：参数数组执行、白名单、超时、错误归类与脱敏（规格 §8）。"""

import subprocess

import pytest

from core.git.models import GitError, GitErrorCode
from core.git.runner import (
    MIN_GIT_VERSION,
    ExecScope,
    GitRunner,
    parse_git_version,
    with_paths,
)
from tests.git_fixtures import make_repo


def test_min_git_version_is_spec_baseline():
    assert MIN_GIT_VERSION == (2, 36, 0)


def test_parse_git_version():
    assert parse_git_version("git version 2.51.2.windows.1") == (2, 51, 2)
    assert parse_git_version("git version 2.36.0") == (2, 36, 0)
    assert parse_git_version("git version 2.39 (Apple Git-143)") == (2, 39, 0)


def test_run_status_on_real_repo(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    result = runner.run(["status", "--porcelain=v2", "-z", "--branch"], cwd=str(repo))
    assert result.returncode == 0
    assert b"# branch.oid" in result.stdout


def test_run_nonzero_does_not_raise_but_run_checked_does(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    # rev-parse 一个不存在的对象：返回非零但不该抛
    result = runner.run(["rev-parse", "-q", "--verify", "no-such-ref"], cwd=str(repo))
    assert result.returncode != 0
    with pytest.raises(GitError) as exc:
        runner.run_checked(["rev-parse", "-q", "--verify", "no-such-ref"], cwd=str(repo))
    assert exc.value.exit_code == result.returncode


def test_whitelist_rejects_unlisted_subcommand(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    for forbidden in (["gc", "--prune=now"], ["fsck"], ["filter-branch", "--", "all"]):
        with pytest.raises(GitError) as exc:
            runner.run(forbidden, cwd=str(repo))
        assert exc.value.code is GitErrorCode.UNSAFE_GIT_EXTENSION
        assert exc.value.exit_code is None  # 未真正启动进程


def test_scope_whitelist_write_scope_cannot_run_read_command(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    with pytest.raises(GitError) as exc:
        runner.run(["log", "-1"], cwd=str(repo), scope=ExecScope.WRITE)
    assert exc.value.code is GitErrorCode.UNSAFE_GIT_EXTENSION


def test_subprocess_uses_param_array_no_shell_no_stdin(tmp_path, monkeypatch):
    make_repo(tmp_path)
    runner = GitRunner()
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        raise subprocess.TimeoutExpired(cmd, 0)

    monkeypatch.setattr("core.git.runner.subprocess.Popen", fake_popen)
    with pytest.raises(Exception):
        runner.run(["status"], cwd=str(tmp_path), timeout=1)
    assert isinstance(captured["cmd"], list)
    assert captured["kwargs"].get("shell", False) is False
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


def test_safe_config_and_env_injected(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    captured = {}
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = dict(kwargs)
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("core.git.runner.subprocess.Popen", fake_popen)
    runner.run(["status", "--porcelain=v2"], cwd=str(repo))
    cmd = captured["cmd"]
    assert "--no-optional-locks" in cmd
    assert "-c" in cmd and "protocol.ext.allow=never" in cmd
    # status 覆盖 fsmonitor，避免读取用户全局配置里的外部监视器
    assert "core.fsmonitor=false" in cmd
    env = captured["kwargs"]["env"]
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_env_extra_passthrough(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    result = runner.run(
        ["var", "GIT_COMMITTER_IDENT"],
        cwd=str(repo),
        env_extra={"GIT_COMMITTER_NAME": "env-user", "GIT_COMMITTER_EMAIL": "env@example.com"},
    )
    assert result.returncode == 0
    assert b"env-user" in result.stdout


def test_timeout_kills_process(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    # 用内部低层通道执行非 git 白名单的慢命令，验证超时与进程终止
    import sys

    slow = [sys.executable, "-c", "import time; time.sleep(15)"]
    with pytest.raises(GitError) as exc:
        runner.execute_raw(slow, cwd=str(repo), timeout=1.0)
    assert exc.value.code is GitErrorCode.TIMEOUT


def test_diff_gets_no_ext_diff_textconv(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    captured = []
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr("core.git.runner.subprocess.Popen", fake_popen)
    runner.run(["diff", "HEAD~1..HEAD"], cwd=str(repo))
    cmd = captured[-1]
    assert "--no-ext-diff" in cmd and "--no-textconv" in cmd


def test_map_error_classification():
    runner = GitRunner()
    cases = [
        (b"fatal: not a git repository (or any of the parent directories)", 128, GitErrorCode.NOT_A_REPOSITORY),
        (b"fatal: Authentication failed for 'https://x'", 128, GitErrorCode.AUTHENTICATION_REQUIRED),
        (b"fatal: could not read Username for 'https://x': terminal prompts disabled", 128, GitErrorCode.AUTHENTICATION_REQUIRED),
        (b"error: src refspec bad does not match any", 1, GitErrorCode.INVALID_REF),
        (b"fatal: another git process seems to be running in this repository, index.lock exists", 128, GitErrorCode.REPOSITORY_BUSY),
        (b"fatal: unable to access 'https://x/': Could not resolve host: x", 128, GitErrorCode.NETWORK_FAILED),
        (b"fatal: Permission denied", 128, GitErrorCode.PERMISSION_DENIED),
        (b"fatal: some other failure", 1, GitErrorCode.GIT_COMMAND_FAILED),
    ]
    for stderr, code, expected in cases:
        result = subprocess.CompletedProcess([], code, b"", stderr)
        err = runner.map_error(
            type("R", (), {"returncode": code, "stdout": b"", "stderr": stderr})(), ["status"]
        )
        assert err.code is expected, f"{stderr!r} -> {err.code} != {expected}"


def test_map_error_sanitizes_command_summary():
    runner = GitRunner()
    err = runner.map_error(
        type("R", (), {"returncode": 1, "stdout": b"", "stderr": b"boom"})(),
        ["push", "https://user:secret@github.com/a/b.git", "main"],
    )
    assert "user:secret" not in err.command_summary
    assert "github.com/a/b.git" in err.command_summary


def test_with_paths_appends_double_dash():
    args = with_paths(["add"], ["-weird.txt", "中文 文件.txt"])
    assert args == ["add", "--", "-weird.txt", "中文 文件.txt"]


def test_check_branch_name_uses_git_validation(tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    runner.check_branch_name("feature/login", cwd=str(repo))
    with pytest.raises(GitError) as exc:
        runner.check_branch_name("bad..name", cwd=str(repo))
    assert exc.value.code is GitErrorCode.INVALID_REF
    # 选项注入形状的名字必须先被本地拒绝
    with pytest.raises(GitError):
        runner.check_branch_name("--upload-pack=evil", cwd=str(repo))


def test_check_available_reports_version_and_missing_git(monkeypatch):
    runner = GitRunner()
    version = runner.check_available()
    assert version and version[0] >= 2

    from core.git import runner as runner_mod

    def raise_fne(*args, **kwargs):
        raise FileNotFoundError("git")

    runner2 = GitRunner(git_executable="definitely-not-git-xyz")
    with pytest.raises(GitError) as exc:
        runner2.check_available()
    assert exc.value.code is GitErrorCode.GIT_NOT_FOUND


def test_version_below_baseline_rejected():
    runner = GitRunner()
    with pytest.raises(GitError) as exc:
        runner.ensure_version((2, 36, 0), detected=(2, 20, 1))
    assert exc.value.code is GitErrorCode.GIT_VERSION_UNSUPPORTED
    # 达标版本放行
    assert runner.ensure_version((2, 36, 0), detected=(2, 51, 2)) == (2, 51, 2)
