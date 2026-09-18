"""core/git/models.py 单元测试：错误码契约、脱敏与操作状态机（规格 §7.4/§14）。"""

import pytest

from core.git.models import (
    ConflictEntry,
    FileChange,
    GitError,
    GitErrorCode,
    GitSnapshot,
    HeadState,
    OperationKind,
    OperationPhase,
    RepositoryIdentity,
    SnapshotVersions,
    redact_text,
    validate_operation_state,
)

# 规格 §7.4 错误码契约表（"至少包含" + 追加 invalid_state）
SPEC_ERROR_CODES = {
    "git_not_found",
    "git_version_unsupported",
    "not_a_repository",
    "bare_repository_unsupported",
    "repository_untrusted",
    "unsafe_git_extension",
    "repository_busy",
    "workspace_process_active",
    "stale_snapshot",
    "dirty_worktree",
    "branch_in_use",
    "operation_in_progress",
    "conflicts_present",
    "invalid_ref",
    "identity_missing",
    "authentication_required",
    "permission_denied",
    "network_failed",
    "timeout",
    "cancelled",
    "patch_check_failed",
    "confirmation_required",
    "confirmation_invalid",
    "unsupported_file_kind",
    "git_command_failed",
    "invalid_state",
}


def test_error_code_contract_matches_spec():
    assert {code.value for code in GitErrorCode} == SPEC_ERROR_CODES


def test_git_error_is_exception_with_full_fields():
    err = GitError(
        code=GitErrorCode.NETWORK_FAILED,
        message="push rejected",
        exit_code=1,
        command_summary="git push origin main",
        retryable=True,
    )
    assert isinstance(err, Exception)
    assert err.code is GitErrorCode.NETWORK_FAILED
    assert err.exit_code == 1
    assert err.command_summary == "git push origin main"
    assert err.retryable is True
    assert "push rejected" in str(err)
    assert "network_failed" in str(err)


def test_git_error_defaults():
    err = GitError(code=GitErrorCode.INVALID_REF, message="bad ref")
    assert err.exit_code is None
    assert err.command_summary == ""
    assert err.retryable is False


def test_redact_text_strips_url_userinfo_and_credential_query():
    text = "push https://user:secret@github.com/a/b.git failed?token=abc123&x=1"
    out = redact_text(text)
    assert "user:secret@" not in out
    assert "abc123" not in out
    assert "github.com/a/b.git" in out
    assert "token=***" in out


def test_redact_text_keeps_clean_text():
    assert redact_text("git status --porcelain") == "git status --porcelain"
    assert redact_text("") == ""


def test_state_machine_idle_phase_coupling():
    # kind==IDLE ⇔ phase==NONE（冻结契约）
    validate_operation_state(OperationKind.IDLE, OperationPhase.NONE, ())
    with pytest.raises(GitError) as exc:
        validate_operation_state(OperationKind.IDLE, OperationPhase.RUNNING, ())
    assert exc.value.code is GitErrorCode.INVALID_STATE

    with pytest.raises(GitError) as exc:
        validate_operation_state(OperationKind.MERGE, OperationPhase.NONE, ())
    assert exc.value.code is GitErrorCode.INVALID_STATE


def test_state_machine_conflicts_are_orthogonal():
    conflict = ConflictEntry(path="a.txt", kind="both_modified")
    # Rebase 冲突：kind=rebase + phase=paused_for_conflict + conflicts 非空
    validate_operation_state(OperationKind.REBASE, OperationPhase.PAUSED_FOR_CONFLICT, (conflict,))
    # Rebase 停在 Edit 步骤可以没有冲突（规格 §14）
    validate_operation_state(OperationKind.REBASE, OperationPhase.PAUSED_FOR_EDIT, ())
    # Idle 但遗留冲突（外部工具制造）：冲突维度独立，合法可表示
    validate_operation_state(OperationKind.IDLE, OperationPhase.NONE, (conflict,))
    # 中断状态合法
    validate_operation_state(OperationKind.CHERRY_PICK, OperationPhase.INTERRUPTED, ())


def test_state_machine_rejects_unknown_types():
    with pytest.raises(GitError) as exc:
        validate_operation_state("merge", OperationPhase.RUNNING, ())
    assert exc.value.code is GitErrorCode.INVALID_STATE


def test_snapshot_value_object_holds_contract_fields():
    identity = RepositoryIdentity(
        workspace_path="E:/proj",
        worktree_root="E:/proj",
        git_dir="E:/proj/.git",
        common_git_dir="E:/proj/.git",
        is_bare=False,
        is_linked_worktree=False,
        git_version="2.51.2",
        capabilities=frozenset({"status_v2", "worktree"}),
    )
    snapshot = GitSnapshot(
        repository=identity,
        head_oid="abc123",
        branch_name="dev",
        head_state=HeadState.NORMAL,
        upstream="origin/dev",
        ahead=2,
        behind=1,
        staged=(FileChange(path="a.py", index_status="M", worktree_status=" "),),
        unstaged=(FileChange(path="b.py", index_status=" ", worktree_status="M"),),
        untracked=("c.py",),
        ignored_count=0,
        conflicts=(),
        stash_count=1,
        operation_kind=OperationKind.IDLE,
        operation_phase=OperationPhase.NONE,
        identity_state="ok",
        worktree_version="w-hash",
        refs_version="r-hash",
    )
    assert snapshot.ahead == 2 and snapshot.behind == 1
    assert snapshot.staged[0].path == "a.py"
    # frozen：不可变
    with pytest.raises(Exception):
        snapshot.ahead = 5
    assert SnapshotVersions(worktree_version="w").worktree_version == "w"
