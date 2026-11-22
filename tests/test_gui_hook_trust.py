import pathlib

import pytest

import core.hooks.config_mutation as hook_mutation
import core.hooks.trust as hook_trust
import gui_bridge
from gui_bridge import ApiBridge


def _bare_bridge(workdir: str = "D:\\proj"):
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "s1"
    bridge._session_workdir = lambda name: workdir
    return bridge


def _resolved(path) -> str:
    return str(pathlib.Path(str(path)).resolve())


def test_get_hooks_ok_passes_workspace_root(monkeypatch, tmp_path):
    calls = []
    hooks = [{"digest": "d1", "event": "PostToolUse", "status": "pending"}]

    def fake_list(*, workspace_root=None, registry=None, store=None):
        calls.append(workspace_root)
        return hooks

    monkeypatch.setattr(hook_trust, "list_hooks_with_status", fake_list)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.get_hooks()

    assert res["ok"] is True
    assert res["hooks"] == hooks
    assert res["workdir"] == _resolved(tmp_path)
    assert calls == [_resolved(tmp_path)]


def test_get_hooks_swallows_exceptions(monkeypatch, tmp_path):

    def boom(*, workspace_root=None, registry=None, store=None):
        raise RuntimeError("扫描 hook 配置崩溃")

    monkeypatch.setattr(hook_trust, "list_hooks_with_status", boom)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.get_hooks()

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
    assert res["hooks"] == []


@pytest.mark.parametrize(
    "method, action",
    [
        ("grant_hook_trust", "grant"),
        ("deny_hook_trust", "deny"),
        ("revoke_hook_trust", "revoke"),
        ("reset_hook_trust", "reset"),
    ],
)
def test_hook_trust_actions_forward_action_and_return_refreshed(
    monkeypatch, tmp_path, method, action
):
    refreshed = [{"digest": "d1", "status": "trusted"}]
    applied = []

    def fake_apply(act, digest, *, workspace_root=None, registry=None, store=None):
        applied.append((act, digest, workspace_root))
        return True, "操作成功"

    monkeypatch.setattr(hook_trust, "apply_hook_trust_action", fake_apply)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: refreshed)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = getattr(bridge, method)("d1")

    root = _resolved(tmp_path)
    assert applied == [(action, "d1", root)]
    assert res == {"ok": True, "msg": "操作成功", "hooks": refreshed, "workdir": root}


def test_hook_trust_action_propagates_failure(monkeypatch, tmp_path):
    hooks = [{"digest": "d9", "status": "pending"}]
    monkeypatch.setattr(
        hook_trust,
        "apply_hook_trust_action",
        lambda action, digest, **kw: (False, "未找到对应的 hook（配置可能已变更，请刷新后重试）"),
    )
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: hooks)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.grant_hook_trust("d9")

    assert res["ok"] is False
    assert "未找到" in res["msg"]
    assert res["hooks"] == hooks


def test_hook_trust_action_swallows_exceptions(monkeypatch, tmp_path):

    def boom(action, digest, *, workspace_root=None, registry=None, store=None):
        raise RuntimeError("信任存储崩溃")

    monkeypatch.setattr(hook_trust, "apply_hook_trust_action", boom)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: [])
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.revoke_hook_trust("d1")

    assert res["ok"] is False
    assert "崩溃" in res["msg"]


def test_hook_workspace_root_falls_back_to_global_workspace_for_global():
    bridge = _bare_bridge(workdir="GLOBAL")

    assert bridge._hook_workspace_root() == gui_bridge._global_workspace_root()


def test_hook_workspace_root_resolves_session_workdir(tmp_path):
    bridge = _bare_bridge(workdir=str(tmp_path))

    assert bridge._hook_workspace_root() == _resolved(tmp_path)


def test_workspace_identity_parity_between_gui_and_runtime(tmp_path):
    gui_identity = hook_trust.normalize_workspace_identity(
        gui_bridge._resolve_real_path(str(tmp_path))
    )
    runtime_identity = hook_trust.normalize_workspace_identity(str(tmp_path.resolve()))

    assert gui_identity == runtime_identity


def test_save_hook_ok_passes_payload_and_workspace_root(monkeypatch, tmp_path):
    calls = []
    refreshed = [{"digest": "d1", "status": "pending", "disabled": False}]
    payload = {"scope": "user", "event": "Stop", "command": "echo hi"}

    def fake_save(data, *, workspace_root=None, **kwargs):
        calls.append((data, workspace_root))
        return True, "已保存"

    monkeypatch.setattr(hook_mutation, "save_hook_config", fake_save)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: refreshed)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.save_hook(payload)

    root = _resolved(tmp_path)
    assert calls == [(payload, root)]
    assert res == {"ok": True, "msg": "已保存", "hooks": refreshed, "workdir": root}

    bridge.save_hook(None)
    assert calls[1] == ({}, root)


def test_save_hook_propagates_failure_msg(monkeypatch, tmp_path):
    hooks = [{"digest": "d2", "status": "pending"}]
    monkeypatch.setattr(
        hook_mutation,
        "save_hook_config",
        lambda data, **kw: (False, "scope 非法：'x'（仅支持 user/project）"),
    )
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: hooks)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.save_hook({"scope": "x"})

    assert res["ok"] is False
    assert "scope 非法" in res["msg"]
    assert res["hooks"] == hooks


def test_save_hook_swallows_exceptions(monkeypatch, tmp_path):

    def boom(data, *, workspace_root=None, **kwargs):
        raise RuntimeError("写盘崩溃")

    monkeypatch.setattr(hook_mutation, "save_hook_config", boom)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: [])
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.save_hook({"scope": "user"})

    assert res["ok"] is False
    assert "崩溃" in res["msg"]


def test_delete_hook_ok_passes_args_and_returns_refreshed(monkeypatch, tmp_path):
    calls = []
    refreshed = [{"digest": "d3", "status": "pending"}]

    def fake_delete(source_path, digest, *, workspace_root=None, **kwargs):
        calls.append((source_path, digest, workspace_root))
        return True, "已删除"

    monkeypatch.setattr(hook_mutation, "delete_hook_config", fake_delete)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: refreshed)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.delete_hook("D:\\proj\\.ycode\\hooks.json", "d3")

    root = _resolved(tmp_path)
    assert calls == [("D:\\proj\\.ycode\\hooks.json", "d3", root)]
    assert res == {"ok": True, "msg": "已删除", "hooks": refreshed, "workdir": root}


def test_delete_hook_swallows_exceptions(monkeypatch, tmp_path):

    def boom(source_path, digest, *, workspace_root=None, **kwargs):
        raise RuntimeError("删除崩溃")

    monkeypatch.setattr(hook_mutation, "delete_hook_config", boom)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: [])
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.delete_hook("D:\\proj\\.ycode\\hooks.json", "d4")

    assert res["ok"] is False
    assert "崩溃" in res["msg"]


def test_set_hook_disabled_ok_passes_args_and_returns_refreshed(monkeypatch, tmp_path):
    calls = []
    refreshed = [{"digest": "d5", "status": "trusted", "disabled": True}]

    def fake_set(source_path, digest, disabled, *, workspace_root=None, **kwargs):
        calls.append((source_path, digest, disabled, workspace_root))
        return True, "已禁用"

    monkeypatch.setattr(hook_mutation, "set_hook_disabled", fake_set)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: refreshed)
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.set_hook_disabled("D:\\proj\\.ycode\\hooks.json", "d5", True)

    root = _resolved(tmp_path)
    assert calls == [("D:\\proj\\.ycode\\hooks.json", "d5", True, root)]
    assert res == {"ok": True, "msg": "已禁用", "hooks": refreshed, "workdir": root}


def test_set_hook_disabled_swallows_exceptions(monkeypatch, tmp_path):

    def boom(source_path, digest, disabled, *, workspace_root=None, **kwargs):
        raise RuntimeError("状态写入崩溃")

    monkeypatch.setattr(hook_mutation, "set_hook_disabled", boom)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: [])
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.set_hook_disabled("D:\\proj\\.ycode\\hooks.json", "d6", False)

    assert res["ok"] is False
    assert "崩溃" in res["msg"]
