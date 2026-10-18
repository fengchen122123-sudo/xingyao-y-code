"""会话跨文件夹移动（move_session_to_workdir）的桥接层测试。

覆盖：跨项目移动、移回 GLOBAL、空目标拒绝、同分组不写库、运行中拒绝，
以及数据不丢 / 目标目录登记 / 事件推送 / 后台任务守卫等副作用。

移植自二改包 tests/test_gui_move_session.py，按本仓（官方 1.3.6）口径调整：
- 空 workdir 在本仓语义里是非法目标（源包当时视作 GLOBAL），用例改为断言拒绝；
- 秘书会话固定归「秘书」分组（源包无此分组），新增拒绝对应用例；
- 同分组不写库用"预置旧 updated_at 再比对"证伪，避免同一秒内比较失真。
"""
import threading

import gui_bridge
from core.memory import MemoryManager
from gui_bridge import ApiBridge


class _AliveTurn:
    """伪回合句柄：is_alive() 恒为 True，用于触发「运行中禁止移动」守卫。"""

    def is_alive(self):
        return True


def _bare_gui_bridge(db=":memory:"):
    """不经 __init__ 构造裸实例，挂一个内存版 MemoryManager。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._memory = MemoryManager(db)
    bridge._current_session = "当前会话"
    bridge._turn_start_lock = threading.RLock()
    bridge._message_queues = {}
    bridge._active_turns = {}
    bridge._shutting_down = True   # 让 emit_event 走早返回，不依赖窗口
    bridge._window = None
    return bridge


def _events(bridge):
    """收集 emit_event 推送的事件。"""
    got = []
    bridge.emit_event = lambda t, d: got.append((t, d))
    return got


def _workdir_of(bridge, name):
    for s in bridge._memory.get_all_sessions(workdir=None, include_empty=True):
        if s["name"] == name:
            return s["workdir"]
    return None


def _seed(bridge, name, workdir="GLOBAL"):
    """建会话并写一条消息（避免被 get_sessions 的空会话过滤跳过）。"""
    bridge._memory.create_session_explicit(name, workdir=workdir, source="GUI")
    bridge._memory.add_message(name, "user", "hi")


# ── 场景 1：跨项目移动 ──────────────────────────────────────────────────

def test_move_session_across_projects(tmp_path):
    """项目 A 的会话能移到项目 B，归属与分组同步变化。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "projA"
    proj_b = tmp_path / "projB"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert res["moved"] is True
    assert res["workdir"] == b_norm
    assert _workdir_of(bridge, "任务") == b_norm
    # 旧分组里没有了、新分组里有了
    a_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=a_norm, include_empty=True)}
    b_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=b_norm, include_empty=True)}
    assert "任务" not in a_names
    assert "任务" in b_names


def test_move_global_session_into_project(tmp_path):
    """「对话」分组（GLOBAL）的会话能移进项目分组。"""
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "闲聊", workdir="GLOBAL")

    res = bridge.move_session_to_workdir("闲聊", str(proj))

    assert res["ok"] is True
    assert res["moved"] is True
    assert _workdir_of(bridge, "闲聊") == p_norm


# ── 场景 2：移回 GLOBAL ────────────────────────────────────────────────

def test_move_project_session_back_to_global(tmp_path):
    """项目会话能移回「对话」分组（反向操作）。"""
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", "GLOBAL")

    assert res["ok"] is True
    assert res["moved"] is True
    assert _workdir_of(bridge, "任务") == "GLOBAL"


# ── 场景 3：空目标拒绝 ──────────────────────────────────────────────────

def test_move_rejects_empty_workdir(tmp_path):
    """空字符串视为非法目标：不移动、不改归属。

    侧栏只会给出真实分组或 "GLOBAL"，空值必是传参错误；若静默当 GLOBAL 处理，
    会把会话误移出项目（源包当年的实现就是"空=GLOBAL"，本仓按新语义改为拒绝）。
    """
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", "")

    assert res["ok"] is False
    assert res["msg"]
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_rejects_empty_session_name():
    bridge = _bare_gui_bridge()
    res = bridge.move_session_to_workdir("", "GLOBAL")
    assert res["ok"] is False


# ── 场景 4：同组不写库 ──────────────────────────────────────────────────

def test_move_to_same_group_is_noop(tmp_path):
    """已在目标分组时不写库：moved=False 且 updated_at 不被刷新。

    updated_at 被刷新会把会话顶到该组列表最前，看起来像"什么都没干却跳了位置"。
    这里先把 updated_at 预置成一个旧值，若实现仍执行了 UPDATE 就必然被改成当前时间。
    """
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))
    bridge._memory.conn.execute(
        "UPDATE sessions SET updated_at = '2000-01-01 00:00:00' WHERE session_name = '任务'"
    )
    bridge._memory.conn.commit()

    res = bridge.move_session_to_workdir("任务", str(proj))

    assert res["ok"] is True
    assert res["moved"] is False
    row_updated = next(
        s for s in bridge._memory.get_all_sessions(include_empty=True, include_archived=True)
        if s["name"] == "任务"
    )["updated_at"]
    assert row_updated == "2000-01-01 00:00:00"
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_global_to_global_is_noop():
    """已经是「对话」的会话再移回「对话」：ok 但 moved=False，不写库。"""
    bridge = _bare_gui_bridge()
    _seed(bridge, "闲聊", workdir="GLOBAL")

    res = bridge.move_session_to_workdir("闲聊", "GLOBAL")

    assert res["ok"] is True
    assert res["moved"] is False


# ── 场景 5：运行中拒绝 ──────────────────────────────────────────────────

def test_move_rejects_while_turn_active(tmp_path):
    """回合运行中禁止移动，归属保持不变。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    _seed(bridge, "任务", workdir=str(proj_a))
    bridge._active_turns["任务"] = _AliveTurn()

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is False
    assert "运行" in res["msg"]
    assert _workdir_of(bridge, "任务") == a_norm


def test_move_rejects_with_running_background_tasks(tmp_path, monkeypatch):
    """会话还有后台任务在执行时也禁止移动。

    后台任务是在旧目录里起的进程，cwd 不会跟着搬；此时改归属会让任务收尾事件
    与产出写回旧分组，两侧状态错位。
    """
    from core.tools import subagent_manager

    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    _seed(bridge, "任务", workdir=str(proj_a))
    monkeypatch.setattr(subagent_manager, "has_running_background_tasks",
                        lambda s: s == "任务")

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is False
    assert "后台任务" in res["msg"]
    assert _workdir_of(bridge, "任务") == a_norm


def test_move_proceeds_when_background_probe_raises(tmp_path, monkeypatch):
    """后台任务状态查询本身失败时不阻断移动（该能力是增强项，非硬依赖）。"""
    from core.tools import subagent_manager

    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    def boom(_s):
        raise RuntimeError("probe down")

    monkeypatch.setattr(subagent_manager, "has_running_background_tasks", boom)

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert _workdir_of(bridge, "任务") == b_norm


# ── 其它拒绝路径 ────────────────────────────────────────────────────────

def test_move_rejects_unknown_session(tmp_path):
    """不存在的会话要报错，不能被 _session_workdir 的 cwd 兜底误判成「已在目标分组」。"""
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    res = bridge.move_session_to_workdir("查无此会话", str(proj))

    assert res["ok"] is False
    assert res["msg"]


def test_move_rejects_missing_target_dir(tmp_path):
    """目标文件夹不存在时拒绝。"""
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()
    p_norm = gui_bridge._resolve_real_path(str(proj))

    _seed(bridge, "任务", workdir=str(proj))

    res = bridge.move_session_to_workdir("任务", str(tmp_path / "不存在"))

    assert res["ok"] is False
    assert _workdir_of(bridge, "任务") == p_norm


def test_move_rejects_secretary_session(tmp_path):
    """秘书会话归属由 source 决定（SECRETARY 分组不是磁盘目录），不允许移动。

    本仓特有：秘书分组的 folder_path='SECRETARY' 只是分组标识，改 workdir 既不
    会改变它在侧栏的位置，又会悄悄改掉回合 cwd，故后端直接拒绝。
    """
    bridge = _bare_gui_bridge()
    proj = tmp_path / "proj"
    proj.mkdir()

    bridge._memory.create_session_explicit("session_abc123", workdir="GLOBAL", source="SECRETARY")
    bridge._memory.add_message("session_abc123", "user", "hi")

    res = bridge.move_session_to_workdir("session_abc123", str(proj))

    assert res["ok"] is False
    assert "秘书" in res["msg"]
    assert _workdir_of(bridge, "session_abc123") == "GLOBAL"


# ── 数据不丢 ────────────────────────────────────────────────────────────

def test_move_preserves_messages_and_model_binding(tmp_path):
    """移动只改归属：消息与会话级模型绑定都原样保留。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    bridge._memory.create_session_explicit("任务", workdir=str(proj_a), source="GUI")
    bridge._memory.add_message("任务", "user", "第一条")
    bridge._memory.add_message("任务", "assistant", "第二条")
    bridge._memory.set_session_model_id("任务", "model-xyz")
    before = bridge._memory.get_history("任务")

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    after = bridge._memory.get_history("任务")
    assert [m["content"] for m in after] == [m["content"] for m in before]
    assert len(after) == len(before)
    assert bridge._memory.get_session_model_id("任务") == "model-xyz"


def test_move_does_not_touch_other_sessions(tmp_path):
    """移动一个会话不得影响同组的其他会话。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    a_norm = gui_bridge._resolve_real_path(str(proj_a))

    for n in ("任务一", "任务二"):
        _seed(bridge, n, workdir=str(proj_a))

    bridge.move_session_to_workdir("任务一", str(proj_b))

    assert _workdir_of(bridge, "任务二") == a_norm
    a_names = {s["name"] for s in bridge._memory.get_all_sessions(workdir=a_norm, include_empty=True)}
    assert a_names == {"任务二"}


def test_move_does_not_migrate_permission_grants(tmp_path):
    """移动不迁移任何会话态：授权白名单、沙盒放行、排队消息都保持原键。

    与重命名不同（rename_session 要迁移 permission_grants / sandbox bypass /
    消息队列），移动只改 workdir，会话名不变，故这些状态一律不该被动到。
    """
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()

    _seed(bridge, "任务", workdir=str(proj_a))
    bridge._message_queues["任务"] = ["排队消息"]

    bridge.move_session_to_workdir("任务", str(proj_b))

    assert bridge._message_queues == {"任务": ["排队消息"]}


# ── 副作用 ──────────────────────────────────────────────────────────────

def test_move_registers_target_as_workspace(tmp_path):
    """移动后目标目录要被登记为工作区，否则侧栏拿不到这个分组。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    bridge.move_session_to_workdir("任务", str(proj_b))

    assert b_norm in bridge._memory.get_all_workspaces()


def test_move_emits_session_moved_event(tmp_path):
    """推送 session_moved 事件，让其他客户端（手机端）同步侧栏。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    got = _events(bridge)

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    assert res["ok"] is True
    assert any(t == "session_moved" for t, _ in got)
    payload = next(d for t, d in got if t == "session_moved")
    assert payload["name"] == "任务"
    assert payload["workdir"] == gui_bridge._resolve_real_path(str(proj_b))
    # 负载带最新分组树，前端整份覆盖即可
    assert isinstance(payload["sessions"], list)
    assert any(g.get("folder_path") == payload["workdir"] for g in payload["sessions"])


def test_move_returns_group_tree(tmp_path):
    """返回值带 sessions 分组树：前端据此刷新侧栏，无需再发一次 get_sessions。"""
    bridge = _bare_gui_bridge()
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    b_norm = gui_bridge._resolve_real_path(str(proj_b))

    _seed(bridge, "任务", workdir=str(proj_a))

    res = bridge.move_session_to_workdir("任务", str(proj_b))

    groups = {g["folder_path"]: g for g in res["sessions"]}
    assert b_norm in groups
    assert "任务" in {s["name"] for s in groups[b_norm]["sessions"]}
