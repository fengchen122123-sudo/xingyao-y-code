"""GitApplicationService 测试：乐观并发/兼容矩阵集成/信任门/确认令牌（规格 §9/§13）。"""

import pytest

from core.git import service as service_mod
from core.git import trust as trust_mod
from core.git.models import GitError, GitErrorCode, SnapshotVersions
from core.git.process_registry import clear_registry, register_process
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from core.git.service import GitApplicationService
from tests.git_fixtures import make_repo, run_git


@pytest.fixture()
def env(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    svc = GitApplicationService()
    # 默认信任仓库；信任门行为由专项用例单独验证
    trust_mod.trust_repository(identity, trust_mod.collect_security_fingerprint(GitRunner(), identity))
    return identity, repo, svc


def test_get_snapshot_fields(env):
    identity, repo, svc = env
    snap = svc.get_snapshot(identity.workspace_path)
    assert snap.branch_name == "main"
    assert snap.head_state.value == "normal"
    assert snap.operation_kind.value == "idle"
    assert len(snap.worktree_version) == 64 and len(snap.refs_version) == 64
    assert snap.identity_state == "ok"
    # 版本确定性
    snap2 = svc.get_snapshot(identity.workspace_path)
    assert snap2.worktree_version == snap.worktree_version
    assert snap2.refs_version == snap.refs_version


def test_stage_commit_via_execute(env):
    identity, repo, svc = env
    (repo / "f1.txt").write_bytes(b"changed by service\n")
    snap = svc.get_snapshot(identity.workspace_path)
    svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(snap.worktree_version, snap.refs_version))
    snap2 = svc.get_snapshot(identity.workspace_path)
    assert {e.path for e in snap2.staged} == {"f1.txt"}
    snap2 = svc.commit(identity.workspace_path, "feat: service commit", "", False,
                       SnapshotVersions(snap2.worktree_version, snap2.refs_version))
    assert not snap2.staged
    log = run_git(repo, "log", "--oneline").stdout.decode()
    assert "service commit" in log


def test_stale_version_rejected(env):
    identity, repo, svc = env
    with pytest.raises(GitError) as exc:
        svc.stage(
            identity.workspace_path, ["f1.txt"],
            SnapshotVersions("stale-worktree-hash", None),
        )
    assert exc.value.code is GitErrorCode.STALE_SNAPSHOT


def test_dirty_switch_rejected(env):
    identity, repo, svc = env
    run_git(repo, "branch", "target")
    (repo / "f1.txt").write_bytes(b"dirty\n")
    snap = svc.get_snapshot(identity.workspace_path)
    with pytest.raises(GitError) as exc:
        svc.switch_branch(
            identity.workspace_path, "target",
            SnapshotVersions(snap.worktree_version, snap.refs_version),
        )
    assert exc.value.code is GitErrorCode.DIRTY_WORKTREE


def test_empty_commit_rejected(env):
    identity, repo, svc = env
    snap = svc.get_snapshot(identity.workspace_path)
    with pytest.raises(GitError) as exc:
        svc.commit(identity.workspace_path, "empty", "", False,
                   SnapshotVersions(snap.worktree_version, snap.refs_version))
    assert exc.value.code is GitErrorCode.INVALID_STATE


def test_identity_missing_blocks_commit(env, tmp_path):
    identity, repo, svc = env
    run_git(repo, "config", "--unset", "user.email")
    run_git(repo, "config", "--unset", "user.name")
    (repo / "f1.txt").write_bytes(b"x\n")
    svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(None, None))
    with pytest.raises(GitError) as exc:
        svc.commit(identity.workspace_path, "t", "", False, SnapshotVersions(None, None))
    assert exc.value.code is GitErrorCode.IDENTITY_MISSING


def test_compat_matrix_blocks_commit_with_unknown_process(env):
    identity, repo, svc = env
    register_process(identity.worktree_root, handle="bg-1", kind="background_task",
                     foreground=False, long_running=True, risk_class="unknown")
    try:
        (repo / "f1.txt").write_bytes(b"x\n")
        with pytest.raises(GitError) as exc:
            svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(None, None))
        assert exc.value.code is GitErrorCode.WORKSPACE_PROCESS_ACTIVE
    finally:
        clear_registry()


def test_untrusted_repo_blocks_commit_until_trusted(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    svc = GitApplicationService()
    (repo / "f1.txt").write_bytes(b"x\n")
    svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(None, None))
    # commit 属于 _TRUST_REQUIRED；未信任仓库必须先确认
    with pytest.raises(GitError) as exc:
        svc.commit(identity.workspace_path, "t", "", False, SnapshotVersions(None, None))
    assert exc.value.code is GitErrorCode.REPOSITORY_UNTRUSTED
    # 信任后放行
    fp = trust_mod.collect_security_fingerprint(GitRunner(), identity)
    trust_mod.trust_repository(identity, fp)
    snap = svc.commit(identity.workspace_path, "t", "", False, SnapshotVersions(None, None))
    assert not snap.staged


def test_high_risk_requires_token_and_typed_confirm(env):
    identity, repo, svc = env
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "droppable")
    # reset_hard 属不可恢复：无令牌直接拒绝
    with pytest.raises(GitError) as exc:
        svc.execute(identity.workspace_path, "reset_hard", {"target": "HEAD~1"},
                    versions=SnapshotVersions(None, None))
    assert exc.value.code is GitErrorCode.CONFIRMATION_REQUIRED
    # preview 拿令牌
    preview = svc.preview(identity.workspace_path, "reset_hard", {"target": "HEAD~1", "confirm_input": "HEAD~1"})
    assert preview.confirm_required and preview.typed_confirm_required and preview.token
    # 输入不匹配 → 拒绝
    with pytest.raises(GitError) as exc:
        svc.execute(identity.workspace_path, "reset_hard",
                    {"target": "HEAD~1", "confirm_input": "wrong"},
                    versions=preview.versions, token=preview.token)
    assert exc.value.code is GitErrorCode.CONFIRMATION_INVALID
    # 正确输入 + 令牌 → 执行
    preview2 = svc.preview(identity.workspace_path, "reset_hard",
                           {"target": "HEAD~1", "confirm_input": "HEAD~1"})
    svc.execute(identity.workspace_path, "reset_hard",
                {"target": "HEAD~1", "confirm_input": "HEAD~1"},
                versions=preview2.versions, token=preview2.token)
    # 令牌一次性：重放拒绝（快照已因上次执行变化，版本门与一次性门任一生效）
    with pytest.raises(GitError) as exc:
        svc.execute(identity.workspace_path, "reset_hard",
                    {"target": "HEAD~1", "confirm_input": "HEAD~1"},
                    versions=preview2.versions, token=preview2.token)
    assert exc.value.code in (GitErrorCode.CONFIRMATION_INVALID, GitErrorCode.STALE_SNAPSHOT)


def test_token_invalidated_by_snapshot_change(env):
    identity, repo, svc = env
    preview = svc.preview(identity.workspace_path, "reset_hard",
                          {"target": "HEAD~1", "confirm_input": "HEAD~1"})
    # preview 后工作区发生变化 → 快照版本失效 → 令牌随之失效
    (repo / "f1.txt").write_bytes(b"mutated\n")
    run_git(repo, "add", "-A")
    with pytest.raises(GitError) as exc:
        svc.execute(identity.workspace_path, "reset_hard",
                    {"target": "HEAD~1", "confirm_input": "HEAD~1"},
                    versions=preview.versions, token=preview.token)
    assert exc.value.code in (GitErrorCode.CONFIRMATION_INVALID, GitErrorCode.STALE_SNAPSHOT)


def test_on_invalidate_called_after_write(env):
    identity, repo, svc = env
    calls = []
    svc.on_invalidate = lambda ws: calls.append(ws)
    svc.stage(identity.workspace_path, [], SnapshotVersions(None, None)) if False else None
    (repo / "f1.txt").write_bytes(b"x\n")
    svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(None, None))
    assert calls == [identity.workspace_path]


def test_invalidate_event_hook_safe_on_error(env):
    identity, repo, svc = env
    def boom(ws):
        raise RuntimeError("hook error")
    svc.on_invalidate = boom
    (repo / "f1.txt").write_bytes(b"x\n")
    # 回调异常不阻断操作结果
    snap = svc.stage(identity.workspace_path, ["f1.txt"], SnapshotVersions(None, None))
    assert snap.staged
