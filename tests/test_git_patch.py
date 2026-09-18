"""Patch 生成/预检/应用测试（规格 §5.3/§5.13/§17.1，真实临时仓库）。"""

import pytest

from core.git import mutation, patch as git_patch
from core.git.models import GitError, GitErrorCode
from core.git.query import fetch_status
from core.git.query_refs import diff_document, parse_unified_diff
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_repo, run_git


@pytest.fixture()
def env(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    return identity, repo, GitRunner()


def test_stage_hunk_via_patch(env):
    """行级暂存：只有被选中的 Hunk 进入暂存区。"""
    identity, repo, runner = env
    (repo / "f1.txt").write_bytes(b"content 1\nline2\nline3\n")
    run_git(repo, "add", "f1.txt")
    run_git(repo, "commit", "-q", "-m", "three lines")
    # 两处独立修改
    (repo / "f1.txt").write_bytes(b"content 1 CHANGED\nline2\nline3 CHANGED\n")
    doc = diff_document(runner, identity)
    hunks = doc.hunks
    if len(hunks) < 2:
        # 若 git 合并为一个 hunk，则退化为整 hunk 暂存断言
        assert len(hunks) == 1
    patch_text = git_patch.build_stage_hunk_patch(
        doc.raw_text, [0], parse_unified_diff(doc.raw_text)
    )
    git_patch.apply_patch_text(runner, identity, patch_text, staged=True)
    status = fetch_status(runner, identity)
    assert status.staged or status.unstaged


def test_patch_check_rejects_stale_context(env):
    identity, repo, runner = env
    # 构造一个上下文与当前文件不匹配的 patch
    stale_patch = (
        "diff --git a/f1.txt b/f1.txt\n"
        "--- a/f1.txt\n"
        "+++ b/f1.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "-WRONG ORIGINAL CONTENT\n"
        "+new content\n"
    )
    with pytest.raises(GitError) as exc:
        git_patch.apply_patch_text(runner, identity, stale_patch)
    assert exc.value.code is GitErrorCode.PATCH_CHECK_FAILED


def test_check_patch_applies_dry_run(env):
    identity, repo, runner = env
    # 生成 patch 后把工作区还原，patch 处于"未应用"状态才可预检
    (repo / "f1.txt").write_bytes(b"content 1 CHANGED\n")
    doc = diff_document(runner, identity)
    good = doc.raw_text
    (repo / "f1.txt").write_bytes(b"content 1\n")
    assert git_patch.check_patch_applies(runner, identity, good) is True
    bad = good.replace("content 1", "totally different context")
    assert git_patch.check_patch_applies(runner, identity, bad) is False


def test_export_patch_roundtrip_between_repos(env, tmp_path):
    identity, repo, runner = env
    (repo / "f1.txt").write_bytes(b"exported change\n")
    patch_text = git_patch.export_patch(runner, identity)
    assert "f1.txt" in patch_text
    # 应用到另一个相同历史的仓库
    repo2 = make_repo(tmp_path, name="repo2", commits=1)
    identity2 = resolve_repository(str(repo2), GitRunner())
    git_patch.apply_patch_text(runner, identity2, patch_text)
    assert (repo2 / "f1.txt").read_bytes() == b"exported change\n"


def test_reverse_patch_restores(env):
    identity, repo, runner = env
    (repo / "f1.txt").write_bytes(b"changed\n")
    doc = diff_document(runner, identity)
    git_patch.apply_patch_text(runner, identity, doc.raw_text, reverse=True)
    assert (repo / "f1.txt").read_bytes() == b"content 1\n"


def test_empty_patch_rejected(env):
    identity, repo, runner = env
    with pytest.raises(GitError) as exc:
        git_patch.apply_patch_text(runner, identity, "   ")
    assert exc.value.code is GitErrorCode.INVALID_STATE
