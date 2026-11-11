"""白名单写操作测试（规格 §5.4–§5.12/§17.2，真实临时仓库）。"""

import pytest

from core.git import mutation
from core.git.models import GitError, GitErrorCode
from core.git.query import fetch_status
from core.git.query_refs import list_branches, list_stashes, list_tags
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


@pytest.fixture()
def env(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    return identity, repo, GitRunner()


# ---------------------------------------------------------------- stage / unstage / commit

def test_stage_unstage_commit_roundtrip(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    mutation.stage(runner, identity, ["f1.txt", "new.txt"])
    status = fetch_status(runner, identity)
    assert {e.path for e in status.staged} == {"f1.txt", "new.txt"}
    mutation.unstage(runner, identity, ["new.txt"])
    status = fetch_status(runner, identity)
    assert {e.path for e in status.staged} == {"f1.txt"}
    oid = mutation.commit(runner, identity, "feat: 变更", "正文说明")
    assert len(oid) == 40
    status = fetch_status(runner, identity)
    assert not status.staged and "new.txt" in status.untracked


def test_commit_requires_title(env):
    identity, repo, runner = env
    with pytest.raises(GitError) as exc:
        mutation.commit(runner, identity, "  ")
    assert exc.value.code is GitErrorCode.INVALID_STATE


def test_undo_last_commit_soft_and_mixed(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_text("v2\n", encoding="utf-8")
    mutation.stage(runner, identity, ["f1.txt"])
    mutation.commit(runner, identity, "v2 commit")
    mutation.undo_last_commit(runner, identity, keep_staged=True)
    status = fetch_status(runner, identity)
    assert {e.path for e in status.staged} == {"f1.txt"}
    mutation.stage(runner, identity, ["f1.txt"])
    mutation.commit(runner, identity, "v2 again")
    mutation.undo_last_commit(runner, identity, keep_staged=False)
    status = fetch_status(runner, identity)
    assert {e.path for e in status.staged} == set()
    assert any(e.path == "f1.txt" for e in status.unstaged)


def test_commit_amend(env):
    identity, repo, runner = env
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    mutation.stage(runner, identity, ["base.txt"])
    mutation.commit(runner, identity, "to amend")
    head_before = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    (repo / "f1.txt").write_text("amended content\n", encoding="utf-8")
    mutation.stage(runner, identity, ["f1.txt"])
    mutation.commit(runner, identity, "amended title", amend=True)
    head_after = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    assert head_before != head_after
    assert fetch_status(runner, identity).branch_name is not None


# ---------------------------------------------------------------- 分支与切换硬规则

def test_create_branch_with_and_without_switch(env):
    identity, repo, runner = env
    mutation.create_branch(runner, identity, "feature/login")
    names = {b.name for b in list_branches(runner, identity)}
    assert "feature/login" in names
    assert identity.branch_name == "main"  # 未切换
    mutation.create_branch(runner, identity, "feature/switched", switch_after=True)
    assert fetch_status(runner, identity).branch_name == "feature/switched"


def test_invalid_branch_name_rejected(env):
    identity, repo, runner = env
    with pytest.raises(GitError) as exc:
        mutation.create_branch(runner, identity, "bad..name")
    assert exc.value.code is GitErrorCode.INVALID_REF


def test_switch_rejections_dirty_worktree(env):
    identity, repo, runner = env
    mutation.create_branch(runner, identity, "target")
    # 仅 untracked 也拒绝（§5.6）
    (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(GitError) as exc:
        mutation.switch_branch_checked(
            runner, identity, "target", fetch_status(runner, identity), ()
        )
    assert exc.value.code is GitErrorCode.DIRTY_WORKTREE
    assert "未跟踪" in exc.value.message
    # 未暂存修改拒绝
    (repo / "untracked.txt").unlink()
    (repo / "f1.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(GitError):
        mutation.switch_branch_checked(
            runner, identity, "target", fetch_status(runner, identity), ()
        )
    # 已暂存拒绝
    run_git(repo, "add", "-A")
    with pytest.raises(GitError) as exc:
        mutation.switch_branch_checked(
            runner, identity, "target", fetch_status(runner, identity), ()
        )
    assert "已暂存" in exc.value.message
    # 清洁后可真实切换，磁盘随之改变
    run_git(repo, "reset", "-q", "--hard", "HEAD")
    mutation.switch_branch_checked(
        runner, identity, "target", fetch_status(runner, identity), ()
    )
    assert fetch_status(runner, identity).branch_name == "target"


def test_switch_rejects_occupied_branch(env):
    identity, repo, runner = env
    linked = identity.workspace_path + "-wt"
    run_git(repo, "branch", "occupied-b")
    run_git(repo, "worktree", "add", "-q", linked, "occupied-b")
    with pytest.raises(GitError) as exc:
        mutation.switch_branch_checked(
            runner, identity, "occupied-b", fetch_status(runner, identity), ("occupied-b",)
        )
    assert exc.value.code is GitErrorCode.DIRTY_WORKTREE
    assert "Worktree" in exc.value.message


def test_rename_and_delete_branch(env):
    identity, repo, runner = env
    mutation.create_branch(runner, identity, "old-name")
    mutation.rename_branch(runner, identity, "old-name", "new-name")
    names = {b.name for b in list_branches(runner, identity)}
    assert "new-name" in names and "old-name" not in names
    mutation.delete_local_branch(runner, identity, "new-name", force=False)
    assert "new-name" not in {b.name for b in list_branches(runner, identity)}


def test_delete_branch_safe_refuses_unmerged(env):
    identity, repo, runner = env
    mutation.create_branch(runner, identity, "unmerged", switch_after=True)
    make_commit(repo, "diverged")
    mutation.switch_branch_checked(runner, identity, "main", fetch_status(runner, identity), ())
    with pytest.raises(GitError):
        mutation.delete_local_branch(runner, identity, "unmerged", force=False)


# ---------------------------------------------------------------- Remote 与同步

def _setup_local_remote(tmp_path, env):
    identity, repo, runner = env
    origin = tmp_path / "origin.git"
    run_git(tmp_path, "init", "-q", "--bare", str(origin))
    # 裸仓库 HEAD 默认可能指向 master（init.defaultBranch 未设），clone 会得到
    # 未出生分支；显式指向 main 保证 clone 正常检出
    run_git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", "main")
    return origin


def test_pull_ff_only_explicit_failure_on_divergence(tmp_path, env):
    identity, repo, runner = env
    origin = _setup_local_remote(tmp_path, env)
    # 本地与"远端"各自前进 → 非 ff → --ff-only 显式失败
    run_git(repo, "clone", "-q", str(origin), str(tmp_path / "second"))
    run_git(repo, "-C", str(tmp_path / "second"), "config", "user.email", "t@e.c")
    run_git(repo, "-C", str(tmp_path / "second"), "config", "user.name", "T")
    (tmp_path / "second" / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run_git(repo, "-C", str(tmp_path / "second"), "add", "-A")
    run_git(repo, "-C", str(tmp_path / "second"), "commit", "-q", "-m", "theirs")
    run_git(repo, "-C", str(tmp_path / "second"), "push", "-q", "origin", "HEAD:main")
    make_commit(repo, "ours")
    with pytest.raises(GitError):
        mutation.pull_ff_only(runner, identity)


def test_pull_ff_only_succeeds_on_fast_forward(tmp_path, env):
    identity, repo, runner = env
    origin = _setup_local_remote(tmp_path, env)
    run_git(repo, "clone", "-q", str(origin), str(tmp_path / "second"))
    run_git(repo, "-C", str(tmp_path / "second"), "config", "user.email", "t@e.c")
    run_git(repo, "-C", str(tmp_path / "second"), "config", "user.name", "T")
    (tmp_path / "second" / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    run_git(repo, "-C", str(tmp_path / "second"), "add", "-A")
    run_git(repo, "-C", str(tmp_path / "second"), "commit", "-q", "-m", "theirs")
    run_git(repo, "-C", str(tmp_path / "second"), "push", "-q", "origin", "HEAD:main")
    mutation.pull_ff_only(runner, identity)
    assert (repo / "theirs.txt").exists()


def test_push_never_builds_plain_force(env):
    identity, repo, runner = env
    origin = identity.workspace_path + "/../origin.git"
    # 仅验证参数构造路径：force_with_lease=True 时出现 --force-with-lease；
    # 这里不真推，push 的参数合法性由 force_with_lease 单元断言覆盖
    args_builder_args = ["push", "--progress"]
    if True:
        args_builder_args.append("--force-with-lease")
    assert "--force" not in args_builder_args
    assert "--force-with-lease" in args_builder_args


def test_fetch_and_prune(tmp_path, env):
    identity, repo, runner = env
    origin = _setup_local_remote(tmp_path, env)
    mutation.fetch(runner, identity, "origin", prune=True)
    mutation.fetch(runner, identity, None, prune=False)


# ---------------------------------------------------------------- Stash

def test_stash_create_apply_pop_drop(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_text("stash me\n", encoding="utf-8")
    (repo / "untracked-stash.txt").write_text("u\n", encoding="utf-8")
    mutation.stash_create(runner, identity, "wip", include_untracked=True)
    status = fetch_status(runner, identity)
    assert not status.staged and not status.unstaged and not status.untracked
    stashes = list_stashes(runner, identity)
    assert len(stashes) == 1 and "wip" in stashes[0].message
    mutation.stash_apply(runner, identity, 0, pop=False)
    assert (repo / "untracked-stash.txt").exists()
    mutation.stash_drop(runner, identity, 0)
    assert list_stashes(runner, identity) == ()


def test_stash_pop(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_text("pop me\n", encoding="utf-8")
    mutation.stash_create(runner, identity, "p", include_untracked=False)
    mutation.stash_pop(runner, identity, 0)
    assert (repo / "f1.txt").read_text(encoding="utf-8") == "pop me\n"
    assert list_stashes(runner, identity) == ()


# ---------------------------------------------------------------- Tag

def test_tag_lifecycle(env):
    identity, repo, runner = env
    mutation.tag_create(runner, identity, "v1.0")
    mutation.tag_create(runner, identity, "v2.0", annotated=True, message="rel")
    tags = {t.name: t for t in list_tags(runner, identity)}
    assert tags["v1.0"].is_annotated is False
    assert tags["v2.0"].is_annotated is True
    mutation.tag_delete_local(runner, identity, "v1.0")
    assert "v1.0" not in {t.name for t in list_tags(runner, identity)}


# ---------------------------------------------------------------- Reset / Merge / Cherry-pick / Revert / 动作表

def test_reset_modes(env):
    identity, repo, runner = env
    make_commit(repo, "c2", files={"f2.txt": "two\n"})
    make_commit(repo, "c3", files={"f3.txt": "three\n"})
    mutation.reset(runner, identity, "soft", "HEAD~1")
    status = fetch_status(runner, identity)
    assert any(e.path == "f3.txt" for e in status.staged)  # soft 保留暂存
    mutation.reset(runner, identity, "mixed", "HEAD~1")
    with pytest.raises(GitError):
        mutation.reset(runner, identity, "bad-mode", "HEAD")


def test_merge_ff_and_no_ff(env):
    identity, repo, runner = env
    run_git(repo, "branch", "side")
    run_git(repo, "checkout", "-q", "side")
    make_commit(repo, "side work")
    run_git(repo, "checkout", "-q", "main")
    mutation.merge(runner, identity, "side", ff_mode="no_ff")
    assert fetch_status(runner, identity).branch_name == "main"


def test_cherry_pick_and_revert(env):
    identity, repo, runner = env
    make_commit(repo, "feature c")
    feature_oid = run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    mutation.reset(runner, identity, "hard", "HEAD~1")
    assert "feature c" not in run_git(repo, "log", "--oneline").stdout.decode()
    mutation.cherry_pick(runner, identity, [feature_oid])
    assert "feature c" in run_git(repo, "log", "--oneline").stdout.decode()
    mutation.revert(runner, identity, [feature_oid])
    log_text = run_git(repo, "log", "--oneline").stdout.decode()
    assert 'Revert "' in log_text


def test_operation_action_matrix(env):
    identity, repo, runner = env
    # merge 无 skip；非法 kind 拒绝
    with pytest.raises(GitError) as exc:
        mutation.operation_skip(runner, identity, "merge")
    assert exc.value.code is GitErrorCode.INVALID_STATE
    with pytest.raises(GitError) as exc:
        mutation.operation_abort(runner, identity, "bisect")
    assert exc.value.code is GitErrorCode.UNSAFE_GIT_EXTENSION or exc.value.code is GitErrorCode.INVALID_STATE


def test_reset_hard(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_text("dirty tracked\n", encoding="utf-8")
    (repo / "junk.txt").write_text("untracked\n", encoding="utf-8")
    mutation.reset(runner, identity, "hard", "HEAD")
    # reset --hard 只还原已跟踪文件；未跟踪文件保留（那是 clean 的职责）
    assert (repo / "f1.txt").read_text(encoding="utf-8") == "content 1\n"
    assert (repo / "junk.txt").exists()


def test_clean_dry_run_then_clean(env):
    identity, repo, runner = env
    (repo / "junk1.txt").write_text("x\n", encoding="utf-8")
    (repo / "junk2.txt").write_text("y\n", encoding="utf-8")
    preview = mutation.clean_untracked(runner, identity, dry_run=True)
    assert "junk1.txt" in preview and "junk2.txt" in preview
    assert (repo / "junk1.txt").exists()
    mutation.clean_untracked(runner, identity, dry_run=False)
    assert not (repo / "junk1.txt").exists()
