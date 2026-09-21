"""GUI Hook 信任入口（get_hooks / grant/deny/revoke/reset_hook_trust）单元测试。

复用 test_gui_plugin_trust.py 的 _bare_bridge 模式：ApiBridge.__new__ 绕过
__init__，只设置测试所需属性；桥接方法内部为局部导入
（from core.hooks.trust import ...），因此打桩需 patch core.hooks.trust
的模块属性。全部用例打桩 core 层，不触碰真实 ~/.ycode。
"""

import os
import pathlib

import pytest

import core.hooks.trust as hook_trust
import gui_bridge
from gui_bridge import ApiBridge


def _bare_bridge(workdir: str = "D:\\proj"):
    """不经 __init__ 构造裸实例；固定会话名与其 workdir 查询结果。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._current_session = "s1"
    bridge._session_workdir = lambda name: workdir
    return bridge


def _resolved(path) -> str:
    """与 _resolve_real_path 对「已存在路径」的口径一致。"""
    return str(pathlib.Path(str(path)).resolve())


def test_get_hooks_ok_passes_workspace_root(monkeypatch, tmp_path):
    """正常路径：workspace_root 透传 _resolve_real_path 处理后的路径，回传 hooks。"""
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
    """底层抛异常：桥接层捕获并返回 {"ok": False, "msg": 异常文本, "hooks": []}。"""

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
    """四个公开信任方法：apply 收到正确 action/digest/workspace_root，回传刷新列表。"""
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
    """apply 返回 (False, msg)：桥接 ok False 且 msg 透传，hooks 仍为刷新结果。"""
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
    """apply 抛异常：桥接层捕获，绝不让异常穿过。"""

    def boom(action, digest, *, workspace_root=None, registry=None, store=None):
        raise RuntimeError("信任存储崩溃")

    monkeypatch.setattr(hook_trust, "apply_hook_trust_action", boom)
    monkeypatch.setattr(hook_trust, "list_hooks_with_status", lambda **kw: [])
    bridge = _bare_bridge(workdir=str(tmp_path))

    res = bridge.revoke_hook_trust("d1")

    assert res["ok"] is False
    assert "崩溃" in res["msg"]


def test_hook_workspace_root_falls_back_to_cwd_for_global():
    """GLOBAL 会话（自由对话）：与回合运行时同口径，回退进程 cwd。"""
    bridge = _bare_bridge(workdir="GLOBAL")

    assert bridge._hook_workspace_root() == gui_bridge._resolve_real_path(os.getcwd())


def test_hook_workspace_root_resolves_session_workdir(tmp_path):
    """普通会话：返回 _resolve_real_path(workdir)。"""
    bridge = _bare_bridge(workdir=str(tmp_path))

    assert bridge._hook_workspace_root() == _resolved(tmp_path)


def test_workspace_identity_parity_between_gui_and_runtime(tmp_path):
    """P2-2 口径一致性：GUI 的 _resolve_real_path 与运行时 workspace_identity 同口径。

    GUI 侧 _hook_workspace_root() 先过 _resolve_real_path 再交给 trust 层；
    运行时（registry/runner）直接吃原始 workdir。两条路径经
    normalize_workspace_identity 归一后必须是同一 workspace_identity，
    否则 GUI 的授权/撤销会写进与运行时读取不同的信任条目（信任永远不生效）。
    """
    gui_identity = hook_trust.normalize_workspace_identity(
        gui_bridge._resolve_real_path(str(tmp_path))
    )
    runtime_identity = hook_trust.normalize_workspace_identity(str(tmp_path.resolve()))

    assert gui_identity == runtime_identity
