"""C3：Hooks 信任机制测试（H-T11 / H-T12 / H-T13）。"""

from __future__ import annotations

from core.hooks.trust import (
    TRUST_BLOCKED,
    TRUST_PENDING,
    TRUST_REVOKED,
    TRUST_STALE,
    TRUST_TRUSTED,
    HookTrustStore,
    compute_hook_digest,
)

WS = "D:/proj"
SOURCE = "D:/proj/.ycode/hooks.json"


def _store(tmp_path) -> HookTrustStore:
    return HookTrustStore(store_path=tmp_path / "hook_trust.json")


def _entry(command="python hook.py", matcher="*", timeout=60000):
    return {
        "event": "PreToolUse",
        "command": command,
        "matcher": matcher,
        "timeout_ms": timeout,
    }


def _status(store, digest, scope="project"):
    return store.status_for(
        workspace_identity=WS, digest=digest, source_path=SOURCE, scope=scope
    )


def test_user_scope_defaults_trusted_and_revocable(tmp_path):
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    assert _status(store, digest, scope="user") == TRUST_TRUSTED
    assert store.revoke(workspace_identity=WS, digest=digest) is True
    assert _status(store, digest, scope="user") == TRUST_REVOKED


def test_project_scope_pending_until_granted(tmp_path):
    """H-T11：项目级未信任默认不可执行；确认后 trusted。"""
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    assert _status(store, digest) == TRUST_PENDING
    assert (
        store.grant(
            workspace_identity=WS,
            digest=digest,
            source_path=SOURCE,
            command="python hook.py",
        )
        is True
    )
    assert _status(store, digest) == TRUST_TRUSTED


def test_digest_ignores_status_message_but_tracks_command(tmp_path):
    """H-T12：command 变更 → 信任失效（stale）；status_message 变更不失效。"""
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    store.grant(workspace_identity=WS, digest=digest, source_path=SOURCE)

    # status_message 根本不在指纹输入里：加入该字段不影响 digest
    with_status = dict(_entry())
    with_status["status_message"] = "任意展示文案"
    assert compute_hook_digest(with_status, SOURCE) == digest

    # command 变更 → 新 digest；无记录但同 source_path 曾 trusted → stale
    new_digest = compute_hook_digest(_entry(command="python evil.py"), SOURCE)
    assert new_digest != digest
    assert _status(store, new_digest) == TRUST_STALE

    # 重新确认后恢复 trusted
    assert store.grant(workspace_identity=WS, digest=new_digest, source_path=SOURCE)
    assert _status(store, new_digest) == TRUST_TRUSTED


def test_digest_stability_and_field_order_independence():
    a = compute_hook_digest(
        {"matcher": "*", "command": "x", "event": "Stop", "timeout_ms": 1000}, SOURCE
    )
    b = compute_hook_digest(
        {"event": "Stop", "timeout_ms": 1000, "command": "x", "matcher": "*"}, SOURCE
    )
    assert a == b
    # source_path 参与指纹（区分同名不同位置的 hook）
    assert compute_hook_digest({"command": "x"}, SOURCE) != compute_hook_digest(
        {"command": "x"}, "D:/other/.ycode/hooks.json"
    )


def test_corrupted_store_fails_closed_for_project_scope(tmp_path):
    """H-T13：存储损坏 → 项目级全部 blocked（fail-closed）；用户级不受影响。"""
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    store.grant(workspace_identity=WS, digest=digest, source_path=SOURCE)
    assert _status(store, digest) == TRUST_TRUSTED

    store.store_path.write_text("{ broken json", encoding="utf-8")
    assert _status(store, digest) == TRUST_BLOCKED
    # 用户级不受存储损坏影响（一次存储故障不应停掉用户自己的 hook）
    assert _status(store, digest, scope="user") == TRUST_TRUSTED


def test_deny_blocks_and_reset_returns_to_pending(tmp_path):
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    assert store.deny(workspace_identity=WS, digest=digest, source_path=SOURCE)
    assert _status(store, digest) == TRUST_BLOCKED
    assert store.reset(workspace_identity=WS, digest=digest)
    assert _status(store, digest) == TRUST_PENDING
