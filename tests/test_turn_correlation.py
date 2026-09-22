"""T4 回合关联贯通测试。

覆盖：
- 回合短码（turn_undo 惯例：turn_id 前 8 位）；
- start_background_task 记录父回合（内存记录 / tasks.json 持久化 / list_active_tasks 输出）；
- 后台任务日志首行标注回合短码（"日志行（副作用相关）"落点）；
- 子代理任务快照暴露 parent_turn_id（既有机制回归守护）。

隔离策略：monkeypatch LOG_DIR / TASK_DB_PATH / ACTIVE_TASKS，Popen 打桩；
不启动真实进程、不触碰真实数据目录。持久化与列表用例直接构造任务记录，
避免监控线程（0.5s 节拍）对打桩进程的轮询干扰断言。
"""

import json
import time
from types import SimpleNamespace

import core.task_manager as task_manager
from core.process_io import platform_console_encoding


# ---------------------------------------------------------------------------
# 回合短码
# ---------------------------------------------------------------------------

def test_turn_short_code_uses_first_eight_chars():
    # GUI/CLI 回合均为完整 uuid hex（128 位）
    assert task_manager.turn_short_code("a1b2c3d4e5f6a7b8") == "a1b2c3d4"
    assert task_manager.turn_short_code("abc") == "abc"
    assert task_manager.turn_short_code("") == ""
    assert task_manager.turn_short_code(None) == ""


# ---------------------------------------------------------------------------
# 后台任务父回合记录（数据层）
# ---------------------------------------------------------------------------

def _isolate_task_env(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})
    monkeypatch.setattr(
        task_manager.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=424242, poll=lambda: None),
    )


def _close_task_handle(task_id):
    info = task_manager.ACTIVE_TASKS.get(task_id) or {}
    handle = info.get("log_handle")
    if handle is not None:
        handle.close()


def _seed_running_task(tmp_path, task_id, *, turn_id):
    """直接构造一条 running 任务记录（避开监控线程对打桩进程的轮询）。"""
    with task_manager._TASKS_LOCK:
        task_manager.ACTIVE_TASKS[task_id] = {
            "id": task_id,
            "command": "demo",
            "session_id": "s1",
            "turn_id": turn_id,
            "process": None,
            "pid": 424242,
            "create_time": None,
            "log_file_path": str(tmp_path / f"task_{task_id}.log"),
            "log_handle": None,
            "start_time": time.time(),
            "output_encoding": "utf-8",
            "status": task_manager.TASK_STATUS_RUNNING,
            "exit_code": None,
            "revision": 0,
            "last_log_size": 0,
        }


def test_start_background_task_records_turn_id(tmp_path, monkeypatch):
    _isolate_task_env(tmp_path, monkeypatch)
    task_id = task_manager.start_background_task(
        "demo", session_id="s1", turn_id="a1b2c3d4e5f6"
    )
    try:
        assert task_manager.ACTIVE_TASKS[task_id]["turn_id"] == "a1b2c3d4e5f6"
        assert task_manager.ACTIVE_TASKS[task_id]["session_id"] == "s1"
    finally:
        _close_task_handle(task_id)


def test_tasks_json_persists_turn_id(tmp_path, monkeypatch):
    _isolate_task_env(tmp_path, monkeypatch)
    _seed_running_task(tmp_path, "t4persist", turn_id="feedc0de")
    task_manager._save_tasks()
    data = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert data["t4persist"]["turn_id"] == "feedc0de"


def test_list_active_tasks_includes_turn_id(tmp_path, monkeypatch):
    _isolate_task_env(tmp_path, monkeypatch)
    # 聚焦"turn_id 透传"：进程状态同步（与本次改动无关）打桩为无操作
    monkeypatch.setattr(task_manager, "_sync_task_locked", lambda tid, info: None)
    _seed_running_task(tmp_path, "t4list", turn_id="a1b2c3d4")
    tasks = task_manager.list_active_tasks()
    matched = [t for t in tasks if t["task_id"] == "t4list"]
    assert matched, tasks
    assert matched[0]["turn_id"] == "a1b2c3d4"


# ---------------------------------------------------------------------------
# 任务日志回合标注（副作用日志行前缀）
# ---------------------------------------------------------------------------

def test_task_log_header_marks_turn(tmp_path, monkeypatch):
    """任务日志首行写入 [turn:xxxxxxxx] 标注，供 /tasks tail 与日志文件追溯回合。"""
    _isolate_task_env(tmp_path, monkeypatch)
    task_id = task_manager.start_background_task("demo", turn_id="abcdef1234567890")
    try:
        raw = (tmp_path / f"task_{task_id}.log").read_bytes()
        # 任务日志按平台控制台编码写入（与既有解码路径一致）
        first_line = raw.decode(platform_console_encoding(), errors="replace").splitlines()[0]
        assert first_line == "[turn:abcdef12] 后台任务启动"
    finally:
        _close_task_handle(task_id)


def test_task_log_header_absent_without_turn(tmp_path, monkeypatch):
    """无父回合（如手工/系统启动）时不写标注行，日志保持原样。"""
    _isolate_task_env(tmp_path, monkeypatch)
    task_id = task_manager.start_background_task("demo")
    try:
        raw = (tmp_path / f"task_{task_id}.log").read_bytes()
        assert raw == b""
    finally:
        _close_task_handle(task_id)


# ---------------------------------------------------------------------------
# 子代理任务快照暴露父回合（既有机制回归守护）
# ---------------------------------------------------------------------------

def test_subagent_snapshot_exposes_parent_turn_id():
    from core.tools import SubagentManager, SubagentTask

    manager = SubagentManager()
    task = SubagentTask(
        task_id="subagent-test1234",
        parent_session="session_x",
        parent_turn_id="a1b2c3d4e5f6",
        description="demo",
        run_in_background=True,
    )
    with manager._lock:
        manager._tasks[task.task_id] = task
    snapshots = manager.snapshot()
    matched = [s for s in snapshots if s.task_id == task.task_id]
    assert matched, snapshots
    assert matched[0].parent_turn_id == "a1b2c3d4e5f6"
