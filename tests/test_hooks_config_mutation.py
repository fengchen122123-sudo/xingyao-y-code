"""C13：Hooks 配置变更模块测试（GUI 管理页「新建/编辑/删除/启停」的写入侧）。

为什么重点测「往返一致性」：写侧必须与 ``HookRegistry._parse_entry`` 的归一化
口径完全一致（matcher strip、timeout_ms int 转换与上限截断、source_path 取同一
Path 对象的 str()），否则会出现「保存成功但列表里看不到 / digest 对不上信任
记录」的幽灵条目。因此本文件除了功能路径，专门覆盖：写入文件 → ``HookRegistry``
加载取 digest → 用该 digest 调 mutation 编辑成功。

全部用例通过 tmp_path 注入 ``user_hooks_path`` / ``project_hooks_path``（或
``workspace_root``），绝不触碰真实 ``~/.ycode``。
"""

from __future__ import annotations

import json
from pathlib import Path

from core.hooks.config_mutation import (
    delete_hook_config,
    save_hook_config,
    set_hook_disabled,
)
from core.hooks.registry import HookRegistry
from core.hooks.types import DEFAULT_TIMEOUT_MS, MAX_TIMEOUT_MS, HookEvent

#: 与 trust.apply_hook_trust_action 共用的「未命中」文案（GUI 刷新提示）
_NOT_FOUND = "未找到对应的 hook（配置可能已变更，请刷新后重试）"


# ── 测试辅助 ────────────────────────────────────────────────


def _user_path(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".ycode" / "hooks.json"


def _project_path(tmp_path: Path) -> Path:
    return tmp_path / "proj" / ".ycode" / "hooks.json"


def _write(path: Path, payload) -> None:
    """写文件；字符串按原文写入（用于制造非法 JSON / 原样对比）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load(user_path, *, workspace_root=None, project_path=None):
    """用 HookRegistry 按与 mutation 相同的路径注入方式加载。"""
    registry = HookRegistry(user_hooks_path=user_path, project_hooks_path=project_path)
    return registry.load(workspace_root=workspace_root)


# ── 新增 ────────────────────────────────────────────────────


def test_create_user_hook_creates_file(tmp_path):
    """新增用户级：文件不存在 → 以 {"hooks": {}} 为基底创建。"""
    user_path = _user_path(tmp_path)
    ok, msg = save_hook_config(
        {
            "scope": "user",
            "event": "PreToolUse",
            "matcher": "write_file",
            "command": "echo hi",
            "timeout_ms": 1500,
            "status_message": "写入前提示",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已保存")
    assert _read(user_path) == {
        "hooks": {
            "PreToolUse": [
                {
                    "command": "echo hi",
                    "matcher": "write_file",
                    "timeout_ms": 1500,
                    "status_message": "写入前提示",
                }
            ]
        }
    }


def test_create_project_hook_via_workspace_root(tmp_path):
    """新增项目级：workspace_root 推导 <root>/.ycode/hooks.json。"""
    user_path = _user_path(tmp_path)
    workspace = tmp_path / "ws"
    ok, msg = save_hook_config(
        {"scope": "project", "event": "Stop", "command": "echo bye"},
        workspace_root=workspace,
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已保存")
    assert _read(workspace / ".ycode" / "hooks.json")["hooks"]["Stop"] == [
        {"command": "echo bye"}
    ]


def test_create_project_hook_via_explicit_path_without_workspace(tmp_path):
    """新增项目级：显式 project_hooks_path 注入时无需 workspace_root。"""
    user_path = _user_path(tmp_path)
    project_path = _project_path(tmp_path)
    ok, msg = save_hook_config(
        {"scope": "project", "event": "SessionStart", "command": "echo start"},
        user_hooks_path=user_path,
        project_hooks_path=project_path,
    )
    assert (ok, msg) == (True, "已保存")
    assert _read(project_path)["hooks"]["SessionStart"] == [{"command": "echo start"}]


def test_create_preserves_existing_entries_and_top_level_fields(tmp_path):
    """新增保留已有条目与其他顶层字段（version / 自定义键）。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {"version": 1, "hooks": {"Stop": [{"command": "echo old"}]}, "extra": "keep"},
    )
    ok, msg = save_hook_config(
        {"scope": "user", "event": "Stop", "command": "echo new"},
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已保存")
    data = _read(user_path)
    assert data["version"] == 1
    assert data["extra"] == "keep"
    assert [e["command"] for e in data["hooks"]["Stop"]] == ["echo old", "echo new"]


def test_create_normalizes_matcher_and_omits_defaults(tmp_path):
    """matcher 缺失/空白 → "*"；默认值与空值不落盘（保持文件干净）。"""
    user_path = _user_path(tmp_path)
    ok, _ = save_hook_config(
        {"scope": "user", "event": "Stop", "command": "echo a"},
        user_hooks_path=user_path,
    )
    assert ok
    ok, _ = save_hook_config(
        {
            "scope": "user",
            "event": "Stop",
            "matcher": "   ",
            "command": "echo b",
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "status_message": "",
        },
        user_hooks_path=user_path,
    )
    assert ok
    assert _read(user_path)["hooks"]["Stop"] == [
        {"command": "echo a"},
        {"command": "echo b"},
    ]
    # 加载口径：matcher 回落 "*"、timeout 回落默认值
    regs = _load(user_path)
    assert [r.matcher for r in regs] == ["*", "*"]
    assert [r.timeout_ms for r in regs] == [DEFAULT_TIMEOUT_MS, DEFAULT_TIMEOUT_MS]


def test_create_truncates_timeout_above_max(tmp_path):
    """timeout_ms 超上限按 MAX_TIMEOUT_MS 截断落盘（与 registry 口径一致）。"""
    user_path = _user_path(tmp_path)
    ok, _ = save_hook_config(
        {
            "scope": "user",
            "event": "Stop",
            "command": "echo a",
            "timeout_ms": MAX_TIMEOUT_MS + 12345,
        },
        user_hooks_path=user_path,
    )
    assert ok
    assert _read(user_path)["hooks"]["Stop"][0]["timeout_ms"] == MAX_TIMEOUT_MS
    assert _load(user_path)[0].timeout_ms == MAX_TIMEOUT_MS


def test_create_then_registry_load_roundtrip(tmp_path):
    """往返加载：新建的条目字段被 registry 按预期读出。"""
    user_path = _user_path(tmp_path)
    ok, _ = save_hook_config(
        {
            "scope": "user",
            "event": "SessionStart",
            "matcher": "write_file",
            "command": "echo hi",
            "timeout_ms": 1200,
            "status_message": "提示",
        },
        user_hooks_path=user_path,
    )
    assert ok
    reg = _load(user_path)[0]
    assert reg.event == HookEvent.SESSION_START
    assert reg.matcher == "write_file"
    assert reg.command == "echo hi"
    assert reg.timeout_ms == 1200
    assert reg.status_message == "提示"
    assert reg.scope == "user"
    assert reg.source_path == str(user_path)


# ── 编辑 ────────────────────────────────────────────────────


def test_edit_with_registry_digest_updates_fields(tmp_path):
    """往返一致性：HookRegistry 加载取 digest → mutation 编辑成功并更新字段。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "command": "echo old",
                        "matcher": "write_file",
                        "timeout_ms": 1000,
                        "status_message": "旧",
                    }
                ]
            }
        },
    )
    reg = _load(user_path)[0]
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "PreToolUse",
            "matcher": "edit_file",
            "command": "echo new",
            "timeout_ms": 2000,
            "status_message": "新",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")
    assert _read(user_path)["hooks"]["PreToolUse"] == [
        {
            "command": "echo new",
            "matcher": "edit_file",
            "timeout_ms": 2000,
            "status_message": "新",
        }
    ]
    reg2 = _load(user_path)[0]
    assert (reg2.command, reg2.matcher, reg2.timeout_ms, reg2.status_message) == (
        "echo new",
        "edit_file",
        2000,
        "新",
    )
    assert reg2.digest != reg.digest


def test_edit_migrates_event_and_cleans_empty_key(tmp_path):
    """event 变化：从旧数组移除（空键清理）并 append 到新数组。"""
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"PreToolUse": [{"command": "echo move"}]}})
    reg = _load(user_path)[0]
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo move",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")
    assert _read(user_path)["hooks"] == {"Stop": [{"command": "echo move"}]}


def test_edit_migrates_event_appends_to_existing_array(tmp_path):
    """event 变化且目标数组已存在：原条目保留，新条目追加到末尾。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {
            "hooks": {
                "PreToolUse": [{"command": "echo move"}],
                "Stop": [{"command": "echo stay"}],
            }
        },
    )
    reg = next(r for r in _load(user_path) if r.command == "echo move")
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo move",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")
    data = _read(user_path)
    assert [e["command"] for e in data["hooks"]["Stop"]] == ["echo stay", "echo move"]
    assert "PreToolUse" not in data["hooks"]


def test_edit_touches_only_targeted_entry(tmp_path):
    """多条目：只改 digest 命中的那一条，其余条目原样保留。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {
            "hooks": {
                "Stop": [
                    {"command": "echo a"},
                    {"command": "echo b", "matcher": "write_file"},
                ]
            }
        },
    )
    target = next(r for r in _load(user_path) if r.command == "echo a")
    ok, _ = save_hook_config(
        {
            "source_path": target.source_path,
            "digest": target.digest,
            "event": "Stop",
            "command": "echo a2",
            "timeout_ms": 3000,
        },
        user_hooks_path=user_path,
    )
    assert ok
    assert _read(user_path)["hooks"]["Stop"] == [
        {"command": "echo a2", "timeout_ms": 3000},
        {"command": "echo b", "matcher": "write_file"},
    ]


def test_edit_cleans_default_fields_and_keeps_unknown(tmp_path):
    """编辑后默认值不落盘；未知字段原样保留。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {
            "hooks": {
                "Stop": [
                    {
                        "command": "echo a",
                        "matcher": "write_file",
                        "timeout_ms": 1000,
                        "status_message": "提示",
                        "category": "自定义",
                    }
                ]
            }
        },
    )
    reg = _load(user_path)[0]
    ok, _ = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo a",
            "matcher": "*",
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "status_message": "",
        },
        user_hooks_path=user_path,
    )
    assert ok
    assert _read(user_path)["hooks"]["Stop"] == [
        {"command": "echo a", "category": "自定义"}
    ]


def test_edit_preserves_disabled_state(tmp_path):
    """禁用条目仍可被定位（disabled 不参与 digest）；编辑不改变启停状态。"""
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo a", "note": "keep"}]}})
    reg = _load(user_path)[0]
    ok, msg = set_hook_disabled(
        reg.source_path, reg.digest, True, user_hooks_path=user_path
    )
    assert (ok, msg) == (True, "已禁用")
    assert _load(user_path) == []  # 禁用后对 registry 不可见，但 digest 未变
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo a2",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")
    entry = _read(user_path)["hooks"]["Stop"][0]
    assert entry["command"] == "echo a2"
    assert entry["note"] == "keep"
    assert entry["disabled"] is True


def test_edit_unknown_digest_rejected(tmp_path):
    """digest 不存在 → 拒绝且文件不变。"""
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo a"}]}})
    before = user_path.read_text(encoding="utf-8")
    ok, msg = save_hook_config(
        {
            "source_path": str(user_path),
            "digest": "0" * 64,
            "event": "Stop",
            "command": "echo b",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (False, _NOT_FOUND)
    assert user_path.read_text(encoding="utf-8") == before


def test_edit_rejects_source_path_outside_known_files(tmp_path):
    """source_path 不是解析出的用户级/项目级路径 → 拒绝且不改任何文件。"""
    user_path = _user_path(tmp_path)
    foreign = tmp_path / "elsewhere" / "hooks.json"
    _write(foreign, {"hooks": {"Stop": [{"command": "echo a"}]}})
    ok, msg = save_hook_config(
        {
            "source_path": str(foreign),
            "digest": "x",
            "event": "Stop",
            "command": "echo b",
        },
        user_hooks_path=user_path,
    )
    assert ok is False
    assert "不允许" in msg
    assert _read(foreign)["hooks"]["Stop"] == [{"command": "echo a"}]


def test_edit_project_hook_via_workspace_root(tmp_path):
    """项目级编辑：source_path 与 workspace_root 推导出的路径 str 比对一致。"""
    user_path = _user_path(tmp_path)
    workspace = tmp_path / "ws"
    project_path = workspace / ".ycode" / "hooks.json"
    _write(project_path, {"hooks": {"Stop": [{"command": "echo p"}]}})
    reg = _load(user_path, workspace_root=workspace)[0]
    assert reg.scope == "project"
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo p2",
        },
        workspace_root=workspace,
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")
    assert _read(project_path)["hooks"]["Stop"] == [{"command": "echo p2"}]


# ── 删除 ────────────────────────────────────────────────────


def test_delete_hook_and_cleanup_empty_event_key(tmp_path):
    """删除：数组剩空则删除事件键；删除最后一条后 hooks 为空对象。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path, {"hooks": {"Stop": [{"command": "echo a"}, {"command": "echo b"}]}}
    )
    target = next(r for r in _load(user_path) if r.command == "echo a")
    ok, msg = delete_hook_config(
        target.source_path, target.digest, user_hooks_path=user_path
    )
    assert (ok, msg) == (True, "已删除")
    assert [e["command"] for e in _read(user_path)["hooks"]["Stop"]] == ["echo b"]
    rest = _load(user_path)[0]
    ok, msg = delete_hook_config(
        rest.source_path, rest.digest, user_hooks_path=user_path
    )
    assert (ok, msg) == (True, "已删除")
    assert _read(user_path)["hooks"] == {}


def test_delete_unknown_digest_rejected(tmp_path):
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo a"}]}})
    before = user_path.read_text(encoding="utf-8")
    ok, msg = delete_hook_config(str(user_path), "0" * 64, user_hooks_path=user_path)
    assert (ok, msg) == (False, _NOT_FOUND)
    assert user_path.read_text(encoding="utf-8") == before


# ── 启停 ────────────────────────────────────────────────────


def test_set_hook_disabled_toggle_roundtrip(tmp_path):
    """禁用写入 disabled 键；启用删除该键；digest 与 disabled 无关可反复定位。"""
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo x", "note": "keep"}]}})
    reg = _load(user_path)[0]
    ok, msg = set_hook_disabled(
        reg.source_path, reg.digest, True, user_hooks_path=user_path
    )
    assert (ok, msg) == (True, "已禁用")
    entry = _read(user_path)["hooks"]["Stop"][0]
    assert entry["disabled"] is True
    assert entry["note"] == "keep"
    assert _load(user_path) == []
    # 启用：删除 disabled 键（不写 false）；digest 不变，仍用原 digest 定位
    ok, msg = set_hook_disabled(
        reg.source_path, reg.digest, False, user_hooks_path=user_path
    )
    assert (ok, msg) == (True, "已启用")
    assert "disabled" not in _read(user_path)["hooks"]["Stop"][0]
    assert _load(user_path)[0].command == "echo x"


def test_set_hook_disabled_rejects_non_bool_and_missing_digest(tmp_path):
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo a"}]}})
    ok, msg = set_hook_disabled(
        str(user_path), "deadbeef", "true", user_hooks_path=user_path
    )
    assert ok is False
    assert msg
    ok, msg = set_hook_disabled(
        str(user_path), "deadbeef", True, user_hooks_path=user_path
    )
    assert (ok, msg) == (False, _NOT_FOUND)


# ── 文件级容错与校验 ──────────────────────────────────────────


def test_invalid_json_rejected_and_file_unchanged(tmp_path):
    """非法 JSON：三个操作都拒绝（提示先修复）且文件原样不变。"""
    user_path = _user_path(tmp_path)
    _write(user_path, "{ not json")
    before = user_path.read_text(encoding="utf-8")
    ok, msg = save_hook_config(
        {"scope": "user", "event": "Stop", "command": "echo a"},
        user_hooks_path=user_path,
    )
    assert ok is False
    assert "修复" in msg
    ok, msg = delete_hook_config(str(user_path), "d", user_hooks_path=user_path)
    assert ok is False and "修复" in msg
    ok, msg = set_hook_disabled(str(user_path), "d", True, user_hooks_path=user_path)
    assert ok is False and "修复" in msg
    assert user_path.read_text(encoding="utf-8") == before


def test_structurally_bad_file_rejected(tmp_path):
    """顶层非对象 / hooks 非对象 → 拒绝且文件不变。"""
    user_path = _user_path(tmp_path)
    for bad in ("[1, 2]", '{"hooks": []}'):
        _write(user_path, bad)
        ok, msg = save_hook_config(
            {"scope": "user", "event": "Stop", "command": "echo a"},
            user_hooks_path=user_path,
        )
        assert ok is False and "修复" in msg
        assert user_path.read_text(encoding="utf-8") == bad


def test_validation_failures_do_not_create_file(tmp_path):
    """校验失败：空 command / 未知 event / 非法 scope / 非正 timeout / 缺根目录。"""
    user_path = _user_path(tmp_path)
    bad_payloads = [
        {"scope": "user", "event": "Nope", "command": "echo a"},  # 未知事件
        {"scope": "user", "event": "Stop", "command": "   "},  # 空 command
        {"scope": "global", "event": "Stop", "command": "echo a"},  # 非法 scope
        {"scope": "user", "event": "Stop", "command": "echo a", "timeout_ms": 0},
        {"scope": "user", "event": "Stop", "command": "echo a", "timeout_ms": -5},
        {"scope": "user", "event": "Stop", "command": "echo a", "timeout_ms": "abc"},
        {"scope": "user", "event": "Stop", "command": "echo a", "timeout_ms": True},
        {"scope": "project", "event": "Stop", "command": "echo a"},  # 无 root/path
        "not-a-dict",
    ]
    for payload in bad_payloads:
        ok, msg = save_hook_config(payload, user_hooks_path=user_path)
        assert ok is False, payload
        assert msg, payload
    assert not user_path.exists()


# ── digest 口径一致性（往返）────────────────────────────────


def test_roundtrip_truncated_timeout_digest_matches(tmp_path):
    """超限 timeout：registry 截断到上限后算 digest，mutation 定位口径一致。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {"hooks": {"Stop": [{"command": "echo a", "timeout_ms": MAX_TIMEOUT_MS + 999}]}},
    )
    reg = _load(user_path)[0]
    assert reg.timeout_ms == MAX_TIMEOUT_MS
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo a2",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")


def test_roundtrip_stripped_matcher_digest_matches(tmp_path):
    """matcher 带首尾空白：registry strip 后算 digest，mutation 定位口径一致。"""
    user_path = _user_path(tmp_path)
    _write(
        user_path,
        {"hooks": {"PreToolUse": [{"command": "echo a", "matcher": " write_file "}]}},
    )
    reg = _load(user_path)[0]
    assert reg.matcher == "write_file"
    ok, msg = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "PreToolUse",
            "command": "echo b",
        },
        user_hooks_path=user_path,
    )
    assert (ok, msg) == (True, "已更新")


def test_roundtrip_float_timeout_digest_matches(tmp_path):
    """float timeout：registry int() 截断后算 digest，mutation 定位口径一致。"""
    user_path = _user_path(tmp_path)
    _write(user_path, {"hooks": {"Stop": [{"command": "echo a", "timeout_ms": 1500.7}]}})
    reg = _load(user_path)[0]
    assert reg.timeout_ms == 1500
    ok, _ = save_hook_config(
        {
            "source_path": reg.source_path,
            "digest": reg.digest,
            "event": "Stop",
            "command": "echo a2",
        },
        user_hooks_path=user_path,
    )
    assert ok is True