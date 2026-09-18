"""Rebase 计划编辑与三方冲突解决测试（规格 §5.10，真实临时仓库）。"""

import pytest

from core.git import conflict as git_conflict
from core.git import mutation
from core.git import rebase_plan
from core.git.conflict import accept_side, load_conflict, resolve_conflict
from core.git.models import GitError, GitErrorCode, OperationKind
from core.git.query import fetch_status
from core.git.rebase_plan import (
    parse_plan_text,
    serialize_plan,
    write_rebase_plan,
)
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


@pytest.fixture()
def env(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    return identity, repo, GitRunner()


# ---------------------------------------------------------------- 计划解析（纯函数）

def test_parse_plan_text_actions():
    text = (
        "pick abc111 first\n"
        "# comment\n"
        "\n"
        "reword abc222 second\n"
        "squash abc333 third\n"
        "fixup abc444 fourth\n"
        "drop abc555 fifth\n"
        "pick abc666 sixth\n"
    )
    lines = parse_plan_text(text)
    assert [l.action for l in lines] == [
        "pick", "reword", "squash", "fixup", "drop", "pick",
    ]
    assert lines[0].oid == "abc111" and lines[0].summary == "first"


def test_serialize_plan_roundtrip():
    lines = parse_plan_text("pick abc111 first\nreword abc222 second\ndrop abc333 third\n")
    text = serialize_plan(lines)
    assert parse_plan_text(text) == lines


# ---------------------------------------------------------------- 真实 rebase 计划改写

def test_write_plan_during_stopped_rebase(env):
    identity, repo, runner = env
    # 制造：feature 两个提交，main 分叉 → rebase 停在冲突
    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "f1.txt").write_bytes(b"feature line one\n")
    run_git(repo, "commit", "-qam", "feature one")
    (repo / "f1.txt").write_bytes(b"feature line one\nfeature line two\n")
    run_git(repo, "commit", "-qam", "feature two")
    run_git(repo, "checkout", "-q", "main")
    (repo / "f1.txt").write_bytes(b"main diverged\n")
    run_git(repo, "commit", "-qam", "main diverged")
    # 切回 feature 再 rebase onto main（否则是 main rebase 自身，无操作）
    run_git(repo, "checkout", "-q", "feature")
    with pytest.raises(GitError):
        mutation.rebase(runner, identity, "main")  # 冲突停住
    snap = fetch_status(runner, identity)
    assert snap.has_conflicts
    # 读取计划并 drop 第一个提交，剩余行保留
    plan = rebase_plan.read_rebase_plan(runner, identity)
    assert plan, "rebase 停住时应有 todo"
    keep = [l for l in plan if l.action == "pick"][1:] or plan[1:]
    write_rebase_plan(runner, identity, keep)
    plan2 = rebase_plan.read_rebase_plan(runner, identity)
    assert [l.oid for l in plan2] == [l.oid for l in keep]
    # 中止恢复，不留残余状态
    mutation.operation_abort(runner, identity, "rebase")
    from core.git.service import GitApplicationService

    snap_after = GitApplicationService().get_snapshot(identity.workspace_path)
    assert snap_after.operation_kind is OperationKind.IDLE


def test_read_plan_without_rebase_raises(env):
    identity, repo, runner = env
    with pytest.raises(GitError) as exc:
        rebase_plan.read_rebase_plan(runner, identity)
    assert exc.value.code is GitErrorCode.INVALID_STATE


# ---------------------------------------------------------------- 三方冲突

def _make_conflict(tmp_path):
    """base → ours/theirs 各改同一行 → merge 冲突。"""
    repo = make_repo(tmp_path, commits=1)
    (repo / "shared.txt").write_bytes(b"original line\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "base shared")
    run_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "shared.txt").write_bytes(b"feature line\n")
    run_git(repo, "commit", "-qam", "feature edit")
    run_git(repo, "checkout", "-q", "main")
    (repo / "shared.txt").write_bytes(b"main line\n")
    run_git(repo, "commit", "-qam", "main edit")
    identity = resolve_repository(str(repo), GitRunner())
    runner = GitRunner()
    with pytest.raises(GitError):
        mutation.merge(runner, identity, "feature", ff_mode="no_ff")
    return identity, repo, runner


def test_load_conflict_stages(tmp_path):
    identity, repo, runner = _make_conflict(tmp_path)
    content = load_conflict(runner, identity, "shared.txt")
    assert content.base == "original line\n"
    assert content.ours == "main line\n"       # current（main）
    assert content.theirs == "feature line\n"  # incoming（feature）
    assert "<<<<<<<" in (content.worktree or "")


def test_load_conflict_not_conflicted_raises(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    with pytest.raises(GitError) as exc:
        load_conflict(GitRunner(), identity, "f1.txt")
    assert exc.value.code is GitErrorCode.INVALID_STATE


def test_resolve_conflict_manual_result(tmp_path):
    identity, repo, runner = _make_conflict(tmp_path)
    resolve_conflict(runner, identity, "shared.txt", "user merged line\n")
    snap = fetch_status(runner, identity)
    assert all(c.path != "shared.txt" for c in snap.conflicts)
    # 结果文件即用户文本，git add 已标记
    assert (repo / "shared.txt").read_bytes() == b"user merged line\n"


def test_accept_side_current_and_incoming(tmp_path):
    identity, repo, runner = _make_conflict(tmp_path)
    accept_side(runner, identity, "shared.txt", "current")
    assert (repo / "shared.txt").read_bytes() == b"main line\n"
    # 重置后再选 incoming
    run_git(repo, "merge", "--abort")
    with pytest.raises(GitError):
        mutation.merge(runner, identity, "feature", ff_mode="no_ff")
    accept_side(runner, identity, "shared.txt", "incoming")
    assert (repo / "shared.txt").read_bytes() == b"feature line\n"


def test_accept_side_invalid(tmp_path):
    identity, repo, runner = _make_conflict(tmp_path)
    with pytest.raises(GitError) as exc:
        accept_side(runner, identity, "shared.txt", "sideways")
    assert exc.value.code is GitErrorCode.INVALID_STATE
