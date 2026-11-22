from __future__ import annotations

import json

from core.hooks import apply_hook_trust_action, list_hooks_with_status
from core.hooks.registry import HookRegistry
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
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    store.grant(workspace_identity=WS, digest=digest, source_path=SOURCE)

    with_status = dict(_entry())
    with_status["status_message"] = "任意展示文案"
    assert compute_hook_digest(with_status, SOURCE) == digest

    new_digest = compute_hook_digest(_entry(command="python evil.py"), SOURCE)
    assert new_digest != digest
    assert _status(store, new_digest) == TRUST_STALE

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
    assert compute_hook_digest({"command": "x"}, SOURCE) != compute_hook_digest(
        {"command": "x"}, "D:/other/.ycode/hooks.json"
    )


def test_corrupted_store_fails_closed_for_project_scope(tmp_path):
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    store.grant(workspace_identity=WS, digest=digest, source_path=SOURCE)
    assert _status(store, digest) == TRUST_TRUSTED

    store.store_path.write_text("{ broken json", encoding="utf-8")
    assert _status(store, digest) == TRUST_BLOCKED
    assert _status(store, digest, scope="user") == TRUST_TRUSTED


def test_deny_blocks_and_reset_returns_to_pending(tmp_path):
    store = _store(tmp_path)
    digest = compute_hook_digest(_entry(), SOURCE)
    assert store.deny(workspace_identity=WS, digest=digest, source_path=SOURCE)
    assert _status(store, digest) == TRUST_BLOCKED
    assert store.reset(workspace_identity=WS, digest=digest)
    assert _status(store, digest) == TRUST_PENDING


def _write_hooks(path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _two_level(tmp_path, project_command="python proj_hook.py"):
    user_path = tmp_path / "home" / "hooks.json"
    project_path = tmp_path / "proj" / ".ycode" / "hooks.json"
    _write_hooks(
        user_path, {"hooks": {"SessionStart": [{"command": "python user_hook.py"}]}}
    )
    _write_hooks(project_path, {"hooks": {"Stop": [{"command": project_command}]}})
    registry = HookRegistry(user_hooks_path=user_path, project_hooks_path=project_path)
    return registry, project_path, tmp_path / "proj"


def test_list_hooks_with_status_scope_defaults_and_grant(tmp_path):
    registry, _, workspace_root = _two_level(tmp_path)
    store = _store(tmp_path)

    def view():
        return list_hooks_with_status(
            workspace_root=workspace_root, registry=registry, store=store
        )

    hooks = view()
    assert [h["scope"] for h in hooks] == ["user", "project"]
    assert [h["status"] for h in hooks] == [TRUST_TRUSTED, TRUST_PENDING]
    assert set(hooks[1]) == {
        "event",
        "matcher",
        "command",
        "timeout_ms",
        "status_message",
        "source_path",
        "scope",
        "digest",
        "status",
        "disabled",
    }
    assert hooks[1]["event"] == "Stop"
    json.dumps(hooks, ensure_ascii=False)

    ok, message = apply_hook_trust_action(
        "grant",
        hooks[1]["digest"],
        workspace_root=workspace_root,
        registry=registry,
        store=store,
    )
    assert (ok, message) == (True, "已信任")
    assert view()[1]["status"] == TRUST_TRUSTED


def test_list_hooks_with_status_includes_disabled_entries(tmp_path):
    registry, project_path, workspace_root = _two_level(tmp_path)
    _write_hooks(
        project_path,
        {"hooks": {"Stop": [{"command": "python proj_hook.py", "disabled": True}]}},
    )
    store = _store(tmp_path)

    hooks = list_hooks_with_status(
        workspace_root=workspace_root, registry=registry, store=store
    )

    assert [h["disabled"] for h in hooks] == [False, True]
    assert hooks[1]["command"] == "python proj_hook.py"
    assert hooks[1]["scope"] == "project"
    assert hooks[1]["digest"]
    assert [r.command for r in registry.load(workspace_root=workspace_root)] == [
        "python user_hook.py"
    ]


def test_list_hooks_with_status_stale_after_command_change(tmp_path):
    registry, project_path, workspace_root = _two_level(tmp_path)
    store = _store(tmp_path)
    hooks = list_hooks_with_status(
        workspace_root=workspace_root, registry=registry, store=store
    )
    assert apply_hook_trust_action(
        "grant",
        hooks[1]["digest"],
        workspace_root=workspace_root,
        registry=registry,
        store=store,
    ) == (True, "已信任")

    _write_hooks(
        project_path, {"hooks": {"Stop": [{"command": "python changed.py"}]}}
    )
    changed = list_hooks_with_status(
        workspace_root=workspace_root, registry=registry, store=store
    )[1]
    assert changed["digest"] != hooks[1]["digest"]
    assert changed["status"] == TRUST_STALE


def test_apply_hook_trust_action_full_chain_and_unknown(tmp_path):
    registry, _, workspace_root = _two_level(tmp_path)
    store = _store(tmp_path)

    def project():
        return list_hooks_with_status(
            workspace_root=workspace_root, registry=registry, store=store
        )[1]

    digest = project()["digest"]

    def apply(action):
        return apply_hook_trust_action(
            action,
            digest,
            workspace_root=workspace_root,
            registry=registry,
            store=store,
        )

    assert project()["status"] == TRUST_PENDING
    assert apply("grant") == (True, "已信任")
    assert project()["status"] == TRUST_TRUSTED
    assert apply("deny") == (True, "已拒绝，之后不再询问")
    assert project()["status"] == TRUST_BLOCKED
    assert apply("revoke") == (True, "已撤销信任")
    assert project()["status"] == TRUST_REVOKED
    assert apply("reset") == (True, "已重置为待确认")
    assert project()["status"] == TRUST_PENDING

    assert apply_hook_trust_action(
        "grant",
        "0" * 64,
        workspace_root=workspace_root,
        registry=registry,
        store=store,
    ) == (False, "未找到对应的 hook（配置可能已变更，请刷新后重试）")
    assert apply("frobnicate") == (False, "未知操作: frobnicate")
