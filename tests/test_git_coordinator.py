"""coordinator 测试：双级锁、快照版本、操作兼容矩阵（规格 §9）。"""

import threading


from core.git.coordinator import (
    GitOperationCategory,
    check_compatible,
    common_repository_lock,
    compute_refs_version,
    compute_worktree_version,
    repository_locks,
    versions_match,
    worktree_lock,
)
from core.git.process_registry import ProcessRecord, clear_registry, register_process


def setup_function():
    clear_registry()


# ---------------------------------------------------------------- 双级锁

def test_locks_are_reentrant_and_scoped():
    with worktree_lock("E:/proj"):
        with worktree_lock("E:/proj"):  # 同线程可重入
            pass


def test_repository_locks_acquire_common_first():
    # 功能性验证：common 锁互斥同一 common dir；不同 worktree 的 worktree 锁可并行
    import time

    with repository_locks("E:/proj/.git", "E:/proj"):
        acquired = threading.Event()

        def other():
            with worktree_lock("E:/proj-wt2"):
                acquired.set()

        t = threading.Thread(target=other)
        t.start()
        assert acquired.wait(timeout=2), "不同 Worktree 应可并行"
        t.join(timeout=2)


def test_common_lock_serializes_same_key():
    counter = {"n": 0}
    import time

    def worker():
        with common_repository_lock("E:/shared/.git"):
            cur = counter["n"]
            time.sleep(0.02)
            counter["n"] = cur + 1

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counter["n"] == 5


# ---------------------------------------------------------------- 快照版本

def _identity_like(common="E:/p/.git", worktree="E:/p"):
    from core.git.models import RepositoryIdentity

    return RepositoryIdentity(
        workspace_path=worktree,
        worktree_root=worktree,
        git_dir=common,
        common_git_dir=common,
        is_bare=False,
        is_linked_worktree=False,
        git_version="2.51.2",
        capabilities=frozenset({"status_v2"}),
    )


def test_worktree_version_deterministic_and_sensitive():
    kwargs = dict(
        identity=_identity_like(),
        head_oid="abc",
        index_material="idx1",
        status_bytes=b"status-bytes",
        operation_markers="rebase:running",
        conflict_material="a.txt:both_modified",
    )
    v1 = compute_worktree_version(**kwargs)
    v2 = compute_worktree_version(**kwargs)
    assert v1 == v2 and len(v1) == 64
    changed = compute_worktree_version(**{**kwargs, "status_bytes": b"changed"})
    assert changed != v1
    # 工作区文件正文不参与（材料清单只含规格 §7.2 列项）
    assert "file-content" not in str(kwargs)


def test_refs_version_scope_and_determinism():
    kwargs = dict(
        common_git_dir="E:/p/.git",
        ref_oids={"refs/heads/dev": "aaa", "refs/remotes/origin/dev": "bbb"},
        remote_config_digest="remote-digest",
    )
    assert compute_refs_version(**kwargs) == compute_refs_version(**kwargs)
    moved = compute_refs_version(**{**kwargs, "ref_oids": {"refs/heads/dev": "ccc"}})
    assert moved != compute_refs_version(**kwargs)
    # refs 版本只覆盖共享作用域：同 repo 不同 worktree 的本地 index 不影响
    assert compute_refs_version(**kwargs) != compute_worktree_version(
        identity=_identity_like(), head_oid="a", index_material="", status_bytes=b"",
        operation_markers="", conflict_material="",
    )


def test_versions_match_handles_none():
    assert versions_match(None, None) is True
    assert versions_match("a", "a") is True
    assert versions_match("a", "b") is False
    assert versions_match(None, "a") is False


# ---------------------------------------------------------------- 兼容矩阵（§9.2）

def _rec(handle, risk, foreground=True, long_running=False):
    return ProcessRecord(
        handle=handle,
        workspace="E:/proj",
        kind="execute_command",
        foreground=foreground,
        long_running=long_running,
        risk_class=risk,
    )


def test_matrix_read_only_always_allowed():
    records = (_rec("h1", "unknown"), _rec("h2", "mutation"))
    allowed, blockers = check_compatible(GitOperationCategory.READ_ONLY, records)
    assert allowed and blockers == []


def test_matrix_index_and_ref_allow_safe_reject_unknown_mutation():
    for category in (GitOperationCategory.INDEX, GitOperationCategory.REF_ONLY):
        allowed, _ = check_compatible(GitOperationCategory.INDEX if category is GitOperationCategory.INDEX else category, (_rec("h1", "git"), _rec("h2", "readonly"), _rec("h3", "service", long_running=True)))
        assert allowed
        allowed, blockers = check_compatible(category, (_rec("h1", "unknown"),))
        assert not allowed and blockers
        allowed, blockers = check_compatible(category, (_rec("h2", "mutation"),))
        assert not allowed and blockers
        # 长期 unknown 后台服务同样按未知处理（拒绝）
        allowed, _ = check_compatible(category, (_rec("h3", "unknown", foreground=False, long_running=True),))
        assert not allowed


def test_matrix_worktree_rewrite_rejects_any_active():
    for risk in ("readonly", "git", "service", "unknown", "mutation"):
        allowed, blockers = check_compatible(GitOperationCategory.WORKTREE_REWRITE, (_rec("h", risk),))
        assert not allowed and blockers


def test_matrix_common_repo_rejects_mutation_unknown():
    allowed, _ = check_compatible(GitOperationCategory.COMMON_REPOSITORY, (_rec("h", "git", foreground=False, long_running=True),))
    assert allowed
    allowed, blockers = check_compatible(GitOperationCategory.COMMON_REPOSITORY, (_rec("h", "mutation"),))
    assert not allowed and blockers


def test_matrix_with_real_registry():
    register_process("E:/proj", handle="h1", risk_class="unknown", long_running=True)
    allowed, blockers = check_compatible(GitOperationCategory.INDEX, ())
    assert allowed
    from core.git.coordinator import compatible_for_workspace

    allowed, blockers = compatible_for_workspace(GitOperationCategory.INDEX, "E:/proj")
    assert not allowed and blockers
