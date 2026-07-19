from types import SimpleNamespace

import core.task_manager as task_manager
from core.process_io import platform_console_encoding


def test_background_task_log_is_opened_as_binary(tmp_path, monkeypatch):
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})
    monkeypatch.setattr(task_manager.subprocess, "Popen", fake_popen)

    task_id = task_manager.start_background_task("demo")

    handle = captured["stdout"]
    try:
        assert "b" in handle.mode
        assert captured["stderr"] is handle
        assert captured["env"]["PYTHONIOENCODING"] == platform_console_encoding()
        assert captured["env"]["PYTHONUTF8"] == "0"
        assert task_manager.ACTIVE_TASKS[task_id]["log_handle"] is handle
        assert task_manager.ACTIVE_TASKS[task_id]["output_encoding"] == platform_console_encoding()
    finally:
        handle.close()


def test_background_task_output_decodes_raw_log_bytes(tmp_path, monkeypatch):
    task_id = "raw-log"
    raw = "星期六\n".encode("gbk")
    (tmp_path / f"task_{task_id}.log").write_bytes(raw)
    decoded = []

    def fake_decode(data, expected_encoding=None):
        decoded.append((data, expected_encoding))
        return data.decode(expected_encoding)

    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(
        task_manager,
        "ACTIVE_TASKS",
        {task_id: {"log_handle": None, "output_encoding": "gbk"}},
    )
    monkeypatch.setattr(
        task_manager,
        "decode_process_output",
        fake_decode,
        raising=False,
    )

    output = task_manager.get_task_output(task_id)

    assert decoded == [(raw, "gbk")]
    assert output == "星期六\n"


def test_start_failure_closes_log_file_handle(tmp_path, monkeypatch):
    opened = []
    real_open = open

    def tracking_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})
    monkeypatch.setattr("builtins.open", tracking_open)
    monkeypatch.setattr(
        task_manager.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("无法启动")),
    )

    try:
        task_manager.start_background_task("demo")
        assert False, "应当抛出 RuntimeError"
    except RuntimeError:
        pass

    log_handles = [h for h in opened if str(getattr(h, "name", "")).endswith(".log")]
    assert log_handles and all(h.closed for h in log_handles)


def test_get_task_output_reads_only_tail_of_large_log(tmp_path, monkeypatch):
    task_id = "big-log"
    # 写入远超尾部读取上限的日志
    lines = [f"log line {index:06d}\n" for index in range(40000)]
    (tmp_path / f"task_{task_id}.log").write_text("".join(lines), encoding="utf-8")

    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})

    output = task_manager.get_task_output(task_id, tail_lines=40)

    assert "log line 039999" in output
    assert "log line 000000" not in output
    assert "仅显示末尾内容" in output


def test_kill_refuses_to_kill_reused_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        task_manager,
        "ACTIVE_TASKS",
        {
            "reused": {
                "id": "reused",
                "command": "demo",
                "process": None,
                "pid": 4321,
                "create_time": 1000.0,
                "log_file_path": str(tmp_path / "task_reused.log"),
                "log_handle": None,
                "start_time": 1,
                "output_encoding": "utf-8",
            }
        },
    )
    # 当前 PID 的启动时间与记录严重不符 → PID 已被无关进程复用
    monkeypatch.setattr(task_manager, "_process_create_time", lambda pid: 999999.0)
    monkeypatch.setattr(task_manager, "is_pid_running", lambda pid: True)
    taskkill_calls = []
    monkeypatch.setattr(
        task_manager.subprocess,
        "run",
        lambda *args, **kwargs: taskkill_calls.append(args),
    )

    assert task_manager.kill_background_task("reused") is False
    assert taskkill_calls == []
    assert "reused" not in task_manager.ACTIVE_TASKS


def test_kill_refuses_when_process_identity_cannot_be_verified(tmp_path, monkeypatch):
    """psutil 缺失导致无法校验进程身份时保守拒绝 kill，只清理记录不杀进程。"""
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        task_manager,
        "ACTIVE_TASKS",
        {
            # 有启动时间记录但当前无法读取（psutil 缺失）
            "unverifiable": {
                "id": "unverifiable",
                "command": "demo",
                "process": None,
                "pid": 4321,
                "create_time": 1000.0,
                "log_file_path": str(tmp_path / "task_unverifiable.log"),
                "log_handle": None,
                "start_time": 1,
                "output_encoding": "utf-8",
            },
            # 旧记录无启动时间且命令行也无法获取（psutil 缺失）
            "legacy": {
                "id": "legacy",
                "command": "demo",
                "process": None,
                "pid": 4322,
                "create_time": None,
                "log_file_path": str(tmp_path / "task_legacy.log"),
                "log_handle": None,
                "start_time": 1,
                "output_encoding": "utf-8",
            },
        },
    )
    monkeypatch.setattr(task_manager, "_process_create_time", lambda pid: None)
    monkeypatch.setattr(task_manager, "_process_cmdline", lambda pid: None)
    monkeypatch.setattr(task_manager, "is_pid_running", lambda pid: True)
    taskkill_calls = []
    monkeypatch.setattr(
        task_manager.subprocess,
        "run",
        lambda *args, **kwargs: taskkill_calls.append(args),
    )

    assert task_manager.kill_background_task("unverifiable") is False
    assert task_manager.kill_background_task("legacy") is False
    assert taskkill_calls == []
    assert task_manager.ACTIVE_TASKS == {}


def test_kill_returns_false_when_taskkill_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(
        task_manager,
        "ACTIVE_TASKS",
        {
            "stubborn": {
                "id": "stubborn",
                "command": "demo",
                "process": None,
                "pid": 4321,
                "create_time": 1000.0,
                "log_file_path": str(tmp_path / "task_stubborn.log"),
                "log_handle": None,
                "start_time": 1,
                "output_encoding": "utf-8",
            }
        },
    )
    monkeypatch.setattr(task_manager, "_process_create_time", lambda pid: 1000.0)
    monkeypatch.setattr(task_manager, "is_pid_running", lambda pid: True)
    monkeypatch.setattr(
        task_manager.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1),
    )

    assert task_manager.kill_background_task("stubborn") is False
    assert "stubborn" not in task_manager.ACTIVE_TASKS
