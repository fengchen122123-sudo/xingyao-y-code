"""风险分级与两段式确认测试（规格 §13）。"""

import time

import pytest

from core.git.models import GitError, GitErrorCode, SnapshotVersions
from core.git.risk import (
    ConfirmationService,
    RiskLevel,
    classify_risk,
    requires_confirmation,
    requires_typed_confirmation,
)


# ---------------------------------------------------------------- 风险分级

def test_risk_classification_table():
    assert classify_risk("stage") is RiskLevel.LOCAL_REVERSIBLE
    assert classify_risk("commit") is RiskLevel.LOCAL_REVERSIBLE
    assert classify_risk("fetch") is RiskLevel.READ_ONLY
    assert classify_risk("push") is RiskLevel.REMOTE_WRITE
    assert classify_risk("push_force_with_lease") is RiskLevel.HISTORY_REWRITE
    assert classify_risk("reset_hard") is RiskLevel.UNRECOVERABLE
    assert classify_risk("stash_drop") is RiskLevel.UNRECOVERABLE
    assert classify_risk("rebase") is RiskLevel.HISTORY_REWRITE
    assert classify_risk("remote_delete") is RiskLevel.UNRECOVERABLE


def test_unknown_operation_rejected():
    with pytest.raises(GitError) as exc:
        classify_risk("filter-branch")
    assert exc.value.code is GitErrorCode.UNSAFE_GIT_EXTENSION


def test_confirmation_requirements():
    assert requires_confirmation(RiskLevel.READ_ONLY) is False
    assert requires_confirmation(RiskLevel.LOCAL_REVERSIBLE) is False
    assert requires_confirmation(RiskLevel.REMOTE_WRITE) is False
    assert requires_confirmation(RiskLevel.HISTORY_REWRITE) is True
    assert requires_confirmation(RiskLevel.UNRECOVERABLE) is True
    # 不可恢复操作要求输入分支名/短 Hash 二次确认（§13）
    assert requires_typed_confirmation(RiskLevel.UNRECOVERABLE) is True
    assert requires_typed_confirmation(RiskLevel.HISTORY_REWRITE) is False


# ---------------------------------------------------------------- 确认令牌

_PARAMS = {"path": ["a.py"], "message": "hi"}
_VERSIONS = SnapshotVersions(worktree_version="w1", refs_version="r1")


def _digests():
    return {
        "session_id": "sess-1",
        "repo_digest": "repo-hash",
        "params_digest": ConfirmationService.params_digest(_PARAMS),
    }


def test_token_issue_and_verify_roundtrip():
    svc = ConfirmationService()
    d = _digests()
    token = svc.issue(versions=_VERSIONS, **d)
    assert token
    svc.verify_and_consume(token, versions=_VERSIONS, **d)  # 不抛即通过


def test_token_one_time_use():
    svc = ConfirmationService()
    d = _digests()
    token = svc.issue(versions=_VERSIONS, **d)
    svc.verify_and_consume(token, versions=_VERSIONS, **d)
    with pytest.raises(GitError) as exc:
        svc.verify_and_consume(token, versions=_VERSIONS, **d)
    assert exc.value.code is GitErrorCode.CONFIRMATION_INVALID


def test_token_rejects_forged_or_unknown():
    svc = ConfirmationService()
    with pytest.raises(GitError) as exc:
        svc.verify_and_consume("forged-token", versions=_VERSIONS, **_digests())
    assert exc.value.code is GitErrorCode.CONFIRMATION_INVALID


def test_token_binding_mismatch_rejected():
    svc = ConfirmationService()
    d = _digests()
    token = svc.issue(versions=_VERSIONS, **d)
    # 换会话
    with pytest.raises(GitError):
        svc.verify_and_consume(token, versions=_VERSIONS, **{**d, "session_id": "other"})
    # 换参数摘要（前端改参数重放）
    with pytest.raises(GitError):
        svc.verify_and_consume(
            token,
            versions=_VERSIONS,
            session_id=d["session_id"],
            repo_digest=d["repo_digest"],
            params_digest="different",
        )
    # 换快照版本（状态过期后旧令牌立即失效，§13）
    with pytest.raises(GitError):
        svc.verify_and_consume(
            token,
            versions=SnapshotVersions(worktree_version="stale", refs_version=None),
            **d,
        )


def test_token_expiry():
    svc = ConfirmationService(ttl_seconds=0.05)
    d = _digests()
    token = svc.issue(versions=_VERSIONS, **d)
    time.sleep(0.08)
    with pytest.raises(GitError) as exc:
        svc.verify_and_consume(token, versions=_VERSIONS, **d)
    assert exc.value.code is GitErrorCode.CONFIRMATION_INVALID


def test_params_digest_is_canonical():
    d1 = ConfirmationService.params_digest({"a": [1, 2], "b": "x"})
    d2 = ConfirmationService.params_digest({"b": "x", "a": [1, 2]})
    assert d1 == d2
    assert ConfirmationService.params_digest({"a": [1], "b": "x"}) != d1
