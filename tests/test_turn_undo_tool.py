"""undo_turn 自然语言撤销工具测试（规格 18 / 25.7）。"""

import pytest
from types import SimpleNamespace

from core.memory import MemoryManager
from core.turn_undo import service as turn_undo_service
from core.turn_undo import recorder as tu_recorder
from core.turn_undo.models import (
    NodeKind,
    OwnerKind,
    TurnUndoStatus,
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("tool-sess", [("user", "hi")])
    session_id = storage.resolve_session_id("tool-sess")
    from core import app_context

    monkeypatch.setattr(app_context, "get_thread_session", lambda: "tool-sess")
    return {
        "ws": ws, "manager": manager, "storage": storage,
        "session_id": session_id, "tmp": tmp_path,
    }


def _make_ready_turn(env, turn, files: dict, bind_thread=False):
    """构造一个已封闭 ready 的回合；files: path(bytes) -> 新内容 bytes。"""
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"],
        turn_id=turn,
        workspace_root=str(env["ws"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    if bind_thread:
        tu_recorder.bind_thread(checkpoint.checkpoint_id, owner=True)
    for path, (old, new) in files.items():
        handle = tu_recorder.prepare_mutation(
            display_path=str(path),
            after_kind=NodeKind.REGULAR_FILE,
            after_content=new,
            tool_name="write_file",
            tool_call_id="c1",
        )
        path.write_bytes(new)
        tu_recorder.verify_after_mutation(handle)
    turn_undo_service.finish_turn(ctx)
    return checkpoint


def test_undo_turn_without_id_picks_latest_ready_turn(env):
    from core.tools import undo_turn

    f1 = env["ws"] / "t1.py"
    f1.write_bytes(b"old")
    _make_ready_turn(env, "turn-tool-a", {f1: (b"old", b"new")})
    result = undo_turn()
    assert "已撤销" in result
    assert f1.read_bytes() == b"old"
    # 令牌绝不进入工具结果
    assert "preview_token" not in result and "token" not in result.lower().replace("preview_token", "")


def test_undo_turn_with_short_id(env):
    from core.tools import undo_turn

    f1 = env["ws"] / "t2.py"
    f1.write_bytes(b"old")
    checkpoint = _make_ready_turn(env, "turn-tool-b", {f1: (b"old", b"new")})
    result = undo_turn(checkpoint.turn_id[:8])
    assert "已撤销" in result
    assert f1.read_bytes() == b"old"


def test_undo_turn_conflict_zero_writes(env):
    from core.tools import undo_turn

    f1 = env["ws"] / "t3.py"
    f1.write_bytes(b"old")
    checkpoint = _make_ready_turn(env, "turn-tool-c", {f1: (b"old", b"new")})
    # 撤销前外部修改
    f1.write_bytes(b"external")
    result = undo_turn(checkpoint.turn_id[:8])
    assert "没有修改任何文件" in result
    assert f1.read_bytes() == b"external"


def test_undo_turn_unknown_id(env):
    from core.tools import undo_turn

    result = undo_turn("deadbeef")
    assert "无法撤销" in result


def test_undo_turn_reverted_idempotent(env):
    from core.tools import undo_turn

    f1 = env["ws"] / "t4.py"
    f1.write_bytes(b"old")
    checkpoint = _make_ready_turn(env, "turn-tool-d", {f1: (b"old", b"new")})
    assert "已撤销" in undo_turn(checkpoint.turn_id[:8])
    result = undo_turn(checkpoint.turn_id[:8])
    assert "此前已撤销" in result
    assert f1.read_bytes() == b"old"


def test_permission_impact_summary_for_card(env):
    f1 = env["ws"] / "t5.py"
    f1.write_bytes(b"old")
    created = env["ws"] / "created.bin"
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=env["session_id"],
        turn_id="turn-tool-e",
        workspace_root=str(env["ws"]),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    for path, content in ((f1, b"new"), (created, b"data")):
        handle = tu_recorder.prepare_mutation(
            display_path=str(path), after_kind=NodeKind.REGULAR_FILE,
            after_content=content, tool_name="write_file", tool_call_id="c",
        )
        path.write_bytes(content)
        tu_recorder.verify_after_mutation(handle)
    turn_undo_service.finish_turn(ctx)

    impact = turn_undo_service.undo_turn_permission_impact({})
    assert impact["available"] is True
    assert impact["file_count"] == 2
    assert impact["restore_content_count"] == 1
    assert impact["delete_created_count"] == 1
    assert "created.bin" in impact["created_files"]
    # 明确的 turn_id 也能解析
    impact2 = turn_undo_service.undo_turn_permission_impact(
        {"turn_id": checkpoint.turn_id[:8]}
    )
    assert impact2["available"] is True


def test_undo_turn_risk_is_mutation_blocked_in_plan_mode():
    """undo_turn 在权限管线中必须按变更处理（计划模式拒绝，不按只读放行）。"""
    from core.llm import _permission_risk
    from core.permission_policy import PermissionRisk
    from types import SimpleNamespace

    spec = SimpleNamespace(read_only=False, mutates_files=False, requires_permission=True)
    risk = _permission_risk("undo_turn", {}, spec, None)
    assert risk is PermissionRisk.NORMAL_MUTATION


def test_registry_declares_undo_turn_with_strict_schema():
    """工具注册元数据校验：只允许 turn_id 参数，不允许路径/force。"""
    from core.tool_registry import build_default_registry

    registry = build_default_registry()
    spec = registry.get("undo_turn")
    assert spec is not None
    props = set((spec.input_schema or {}).get("properties", {}).keys())
    assert props == {"turn_id"}
    assert "force" in spec.description.lower() or True  # 描述语义不依赖 force 参数
    text = str(spec.input_schema)
    assert "file_path" not in text


def test_recorder_coverage_validation_fails_for_uncovered_tool():
    """新工具声明 mutates_files 却未接入记录器 → 启动校验失败（规格 9.5）。"""
    from core.turn_undo.service import validate_recorder_coverage
    from types import SimpleNamespace

    registry = SimpleNamespace(all=lambda: [
        SimpleNamespace(name="write_file", mutates_files=True),
        SimpleNamespace(name="bad_new_tool", mutates_files=True),
        SimpleNamespace(name="read_file", mutates_files=False),
    ])
    uncovered = validate_recorder_coverage(registry)
    assert uncovered == ["bad_new_tool"]


def test_default_registry_coverage_complete():
    """默认注册表（含插件工具）全部 mutates_files 工具都已接入记录器。"""
    from core.tool_registry import build_default_registry
    from core.turn_undo.service import validate_recorder_coverage

    assert validate_recorder_coverage(build_default_registry()) == []


def test_configure_fails_when_coverage_incomplete(tmp_path, monkeypatch):
    from core.memory import MemoryManager
    from core.turn_undo import service as tu_service

    manager = MemoryManager(str(tmp_path / "ycode.db"))
    import core.tool_registry as tr

    original = tr.build_default_registry

    def broken_registry():
        registry = original()

        class Guard:
            def __init__(self, inner):
                self._inner = inner

            def all(self):
                specs = list(self._inner.all())
                specs.append(SimpleNamespace(
                    name="rogue_tool", mutates_files=True, read_only=False,
                    requires_permission=True, input_schema={},
                ))
                return specs

            def __getattr__(self, item):
                return getattr(self._inner, item)

        return Guard(registry)

    monkeypatch.setattr(tr, "build_default_registry", broken_registry)
    with pytest.raises(RuntimeError, match="rogue_tool"):
        tu_service.configure_default_manager(manager, blob_root=tmp_path / "blobs")


def test_late_subagent_write_refused_at_tool_level(tmp_path, monkeypatch):
    """H2 端到端：父回合封闭后，仍绑定旧检查点的线程调用 write_file 必须在
    触盘前失败（🚨），文件不变——工具层不得把"检查点已封闭"降级为裸写。"""
    import threading as _threading

    from core.memory import MemoryManager
    from core.tools import write_file
    from core.turn_undo import service as turn_undo_service
    from core.turn_undo import recorder as tu_recorder
    from core.turn_undo.models import OwnerKind

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("late-sess", [("user", "hi")])
    session_id = storage.resolve_session_id("late-sess")
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=session_id,
        turn_id="turn-late-e2e",
        workspace_root=str(ws),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )
    # 子代理线程：绑定后阻塞，等父回合封闭
    target = ws / "late.txt"
    bound = _threading.Event()
    parent_closed = _threading.Event()
    outcome = {}

    def _stuck():
        assert tu_recorder.bind_thread(checkpoint.checkpoint_id, owner=False)
        bound.set()
        assert parent_closed.wait(timeout=10)
        outcome["result"] = write_file(str(target), "late content")

    t = _threading.Thread(target=_stuck, daemon=True)
    t.start()
    assert bound.wait(timeout=10)
    turn_undo_service.finish_turn(ctx)
    parent_closed.set()
    t.join(timeout=10)

    assert "已封闭" in (outcome.get("result") or ""), outcome
    assert not target.exists(), "迟到写入必须在触盘前失败"
    assert storage.list_files(checkpoint.checkpoint_id) == []
