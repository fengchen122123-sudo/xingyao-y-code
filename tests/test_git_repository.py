"""core/git/repository.py 测试：仓库发现、Worktree/Detached/Unborn/Bare 识别（规格 §5.1）。"""

from pathlib import Path

import pytest

from core.git.models import GitError, GitErrorCode, HeadState
from core.git.repository import (
    normalize_repo_path,
    parse_git_version,
    probe_identity_state,
    resolve_repository,
)
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


def test_resolve_normal_repo(tmp_path):
    repo = make_repo(tmp_path)
    identity = resolve_repository(str(repo), GitRunner())
    assert normalize_repo_path(identity.worktree_root) == normalize_repo_path(str(repo))
    assert identity.common_git_dir == identity.git_dir
    assert identity.is_bare is False
    assert identity.is_linked_worktree is False
    assert identity.git_version.startswith("2.")
    assert "status_v2" in identity.capabilities


def test_resolve_from_subdirectory_finds_root(tmp_path):
    repo = make_repo(tmp_path)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    identity = resolve_repository(str(sub), GitRunner())
    assert normalize_repo_path(identity.worktree_root) == normalize_repo_path(str(repo))


def test_resolve_non_repo_raises(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError) as exc:
        resolve_repository(str(plain), GitRunner())
    assert exc.value.code is GitErrorCode.NOT_A_REPOSITORY


def test_resolve_bare_repo_raises(tmp_path):
    bare = tmp_path / "bare.git"
    run_git(tmp_path, "init", "-q", "--bare", str(bare))
    with pytest.raises(GitError) as exc:
        resolve_repository(str(bare), GitRunner())
    assert exc.value.code is GitErrorCode.BARE_REPOSITORY_UNSUPPORTED


def test_resolve_version_below_baseline(monkeypatch, tmp_path):
    repo = make_repo(tmp_path)
    runner = GitRunner()
    monkeypatch.setattr(runner, "check_available", lambda: (2, 20, 1))
    with pytest.raises(GitError) as exc:
        resolve_repository(str(repo), runner)
    assert exc.value.code is GitErrorCode.GIT_VERSION_UNSUPPORTED


def test_unborn_branch_detected(tmp_path):
    repo = tmp_path / "unborn"
    repo.mkdir()
    run_git(repo.parent, "init", "-q", "-b", "main", str(repo))
    run_git(repo, "config", "user.email", "t@example.com")
    run_git(repo, "config", "user.name", "T")
    identity = resolve_repository(str(repo), GitRunner())
    assert identity.head_state is HeadState.UNBORN
    assert identity.branch_name == "main"


def test_detached_head_detected(tmp_path):
    repo = make_repo(tmp_path, commits=2)
    run_git(repo, "checkout", "-q", "--detach", "HEAD~1")
    identity = resolve_repository(str(repo), GitRunner())
    assert identity.head_state is HeadState.DETACHED
    assert identity.branch_name is None


def test_linked_worktree_shares_common_dir(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    run_git(repo, "branch", "feature/login")
    linked = tmp_path / "wt-login"
    run_git(repo, "worktree", "add", "-q", str(linked), "feature/login")
    identity_main = resolve_repository(str(repo), GitRunner())
    identity_linked = resolve_repository(str(linked), GitRunner())
    assert identity_linked.is_linked_worktree is True
    assert normalize_repo_path(identity_linked.common_git_dir) == normalize_repo_path(
        identity_main.common_git_dir
    )
    assert normalize_repo_path(identity_linked.git_dir) != normalize_repo_path(
        identity_main.git_dir
    )
    assert identity_linked.branch_name == "feature/login"


def test_identity_missing_when_no_local_identity(tmp_path):
    repo = make_repo(tmp_path)
    assert probe_identity_state(GitRunner(), str(repo)) == "ok"
    # 移除本地身份配置后标记缺失（探测屏蔽全局配置，避免环境泄漏）
    run_git(repo, "config", "--unset", "user.email")
    assert probe_identity_state(GitRunner(), str(repo)) == "identity_missing"


def test_parse_git_version_rejects_garbage():
    with pytest.raises(ValueError):
        parse_git_version("not git output")


def test_normalize_repo_path_is_stable_on_windows(tmp_path):
    a = normalize_repo_path(str(tmp_path / "Repo"))
    b = normalize_repo_path(str(tmp_path / "repo"))
    assert a == b
