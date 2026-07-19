import pytest
import json
import threading
from types import SimpleNamespace
from pathlib import Path
from core.tools import (
    SubagentManager,
    execute_command,
    fetch_url,
    get_weather,
    list_directory,
    read_file,
    write_file,
)


def test_declared_search_dependency_uses_renamed_ddgs_package():
    from ddgs import DDGS

    tools_source = Path("core/tools.py").read_text(encoding="utf-8")
    requirements_source = Path("requirements.txt").read_text(encoding="utf-8")
    setup_source = Path("setup.py").read_text(encoding="utf-8")

    assert DDGS is not None
    assert "from ddgs import DDGS" in tools_source
    assert "duckduckgo_search" not in tools_source
    assert "ddgs>=9.0.0" in requirements_source
    assert "duckduckgo-search" not in requirements_source
    assert "ddgs>=9.0.0" in setup_source
    assert "duckduckgo-search" not in setup_source


class _FakeHTTPResponse:
    def __init__(self, body, content_type="application/json; charset=utf-8"):
        self._body = body.encode("utf-8")
        self.status = 200
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]


def test_fetch_url_uses_local_http_client_and_returns_response(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)

    result = fetch_url("https://example.com/data")

    assert "HTTP 200" in result
    assert '{"ok": true}' in result
    assert calls[0][0].full_url == "https://example.com/data"
    assert calls[0][0].get_header("User-agent")
    assert calls[0][1] == 20


def test_fetch_url_rejects_non_http_urls():
    result = fetch_url("file:///C:/secret.txt")

    assert result.startswith("🚨")
    assert "http" in result.lower()


def test_get_weather_uses_geocoded_open_meteo_payload(monkeypatch):
    geocoding = {
        "results": [{
            "name": "北京",
            "country": "中国",
            "latitude": 39.9042,
            "longitude": 116.4074,
            "timezone": "Asia/Shanghai",
        }],
    }
    forecast = {
        "current": {
            "temperature_2m": 31,
            "apparent_temperature": 34,
            "weather_code": 0,
            "relative_humidity_2m": 45,
            "wind_speed_10m": 12,
            "wind_direction_10m": 135,
        },
        "daily": {
            "time": ["2026-07-19"],
            "temperature_2m_max": [35],
            "temperature_2m_min": [25],
            "precipitation_probability_max": [20],
            "weather_code": [1],
            "sunrise": ["2026-07-19T04:55"],
            "sunset": ["2026-07-19T19:45"],
        },
    }
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(request.full_url)
        payload = geocoding if "geocoding-api" in request.full_url else forecast
        return _FakeHTTPResponse(json.dumps(payload))

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)

    result = get_weather("北京")

    assert "城市：中国 北京" in result
    assert "当前温度：31°C" in result
    assert "体感温度：34°C" in result
    assert "天气：晴" in result
    assert "风向/风速：东南 12 km/h" in result
    assert "2026-07-19：大部晴朗，25°C ~ 35°C，降雨概率 20%" in result
    assert any("geocoding-api.open-meteo.com" in url for url in calls)
    assert any("api.open-meteo.com" in url for url in calls)


def test_get_weather_requires_explicit_city():
    result = get_weather("")

    assert result.startswith("🚨")
    assert "城市" in result

def test_read_and_write_file(tmp_path):
    """测试 Agent 是否能正确读取和写入文件"""
    test_file = tmp_path / "hello.py"
    content_to_write = "print('Hello, Y-code!')"
    
    write_file(test_file, content_to_write)
    read_content = read_file(test_file)
    
    assert read_content == content_to_write

def test_list_directory(tmp_path):
    """测试雷达功能：能否看清文件夹里有什么"""
    # 1. 准备：在隔离区造一个假项目，里面有两个文件
    (tmp_path / "main.py").touch()
    (tmp_path / "readme.md").touch()
    
    # 2. 执行：启动雷达扫描
    files = list_directory(str(tmp_path))
    
    # 3. 断言验证：检查雷达有没有扫出这两个文件
    assert "main.py" in files
    assert "readme.md" in files

def test_execute_command():
    """测试引擎功能：能否在终端里执行命令"""
    # 1. 执行：让 Python 自己在终端里跑一句打印命令
    # 注意：为了跨平台兼容，我们用 python -c 来执行一句简单的代码
    result = execute_command('python -c "print(\'hello engine\')"')
    
    # 2. 断言验证：终端里是不是真的输出了这句话
    assert "hello engine" in result


def test_skill_command_reports_missing_dependencies_without_installing(tmp_path, monkeypatch):
    script_dir = tmp_path / "skills" / "demo" / "scripts"
    script_dir.mkdir(parents=True)
    script = script_dir / "run.py"
    script.write_text("print('never runs')", encoding="utf-8")
    (script_dir.parent / "requirements.txt").write_text(
        "definitely-missing-ycode-package==1.0\n",
        encoding="utf-8",
    )
    subprocess_calls = []
    monkeypatch.setattr(
        "core.tools.subprocess.run",
        lambda *args, **kwargs: subprocess_calls.append((args, kwargs)),
    )

    result = execute_command(f'python "{script.as_posix()}"')

    assert "缺少依赖" in result
    assert "definitely-missing-ycode-package" in result
    assert subprocess_calls == []


def test_non_skill_command_bypasses_dependency_tokenization(monkeypatch):
    calls = []

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return b"shell handled command", b""

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)

    result = execute_command('powershell -Command "unterminated')

    assert calls
    assert result == "shell handled command"
    assert "技能依赖清单无效" not in result
    assert calls[0][1]["env"]["PYTHONIOENCODING"]
    assert calls[0][1]["env"]["PYTHONUTF8"] == "0"


def test_foreground_subagent_waits_and_returns_public_result():
    manager = SubagentManager(runner=lambda record: "天气：晴")

    result = manager.run("查询天气", "s1", "t1", timeout_seconds=30)

    assert result == "天气：晴"
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "completed"
    assert snapshot.stage == "completed"
    assert [stage.name for stage in snapshot.stages] == [
        "queued",
        "running",
        "completed",
    ]


def test_subagent_records_role_and_plan_item():
    manager = SubagentManager(runner=lambda record: "完成")

    result = manager.run(
        "探索代码",
        "s1",
        "t1",
        subagent_type="explore",
        plan_item_id="explore-1",
        timeout_seconds=30,
    )

    assert result == "完成"
    snapshot = manager.snapshot()[0]
    assert snapshot.subagent_type == "explore"
    assert snapshot.plan_item_id == "explore-1"


def test_subagent_terminal_callback_receives_real_result():
    manager = SubagentManager(runner=lambda record: "真实结果")
    completed = []

    result = manager.run(
        "执行任务",
        "s1",
        "t1",
        timeout_seconds=30,
        terminal_callback=lambda task, status, value, error: completed.append(
            (task.plan_item_id, status, value, error)
        ),
        plan_item_id="item-1",
    )

    assert result == "真实结果"
    assert completed == [("item-1", "completed", "真实结果", "")]


def test_subagent_structured_tool_failure_cannot_complete():
    def runner(record):
        record.tool_errors["get_weather"] = "天气服务超时"
        return "天气查询失败"

    manager = SubagentManager(runner=runner)
    result = manager.run("查询天气", "s1", "t1", timeout_seconds=30)

    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "天气服务超时" in payload["error"]


def test_run_subagent_updates_associated_plan_only_after_completion(monkeypatch):
    import core.tools as tools_module
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("subagent-plan-test")
    plan.replace([{"id": "item-1", "subject": "真实子任务", "stage": "build"}])
    monkeypatch.setattr(tools_module, "subagent_manager", SubagentManager(runner=lambda record: "完成"))
    events = []

    result = tools_module.run_subagent(
        "执行真实子任务",
        parent_session="subagent-plan-test",
        parent_turn_id="turn-1",
        subagent_type="implement",
        plan_item_id="item-1",
        timeout_seconds=30,
        event_callback=lambda kind, payload: events.append((kind, payload)),
    )

    assert result == "完成"
    assert plan.snapshot()[0]["status"] == "completed"
    assert [kind for kind, _ in events if kind.startswith("plan_item_")] == [
        "plan_item_started", "plan_item_completed",
    ]
    bind_default_plan_session(previous_session)


def test_run_subagent_rejects_user_input_preparation_item(monkeypatch):
    import core.tools as tools_module
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("subagent-input-preparation-test")
    plan.replace([{
        "id": "city",
        "subject": "确认城市",
        "kind": "input_preparation",
    }])
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "不应执行"),
    )

    result = tools_module.run_subagent(
        "确认城市",
        parent_session="subagent-input-preparation-test",
        parent_turn_id="turn-input-preparation",
        plan_item_id="city",
        timeout_seconds=30,
    )

    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert "用户输入准备项" in payload["error"]
    assert plan.snapshot()[0]["status"] == "pending"
    bind_default_plan_session(previous_session)


def test_background_subagent_returns_task_id_immediately():
    release = threading.Event()

    def runner(record):
        release.wait(1)
        return "完成"

    manager = SubagentManager(runner=runner)

    result = manager.run(
        "独立任务",
        "s1",
        "t1",
        run_in_background=True,
        timeout_seconds=30,
    )

    assert "subagent-" in result
    assert manager.snapshot()[0].status in {"pending", "running"}
    release.set()
    assert manager.wait_for_idle(1)


def test_background_notifications_are_scoped_to_parent_session():
    manager = SubagentManager(runner=lambda record: "结果")
    manager.run(
        "任务",
        "s1",
        "t1",
        run_in_background=True,
        timeout_seconds=30,
    )

    assert manager.wait_for_idle(1)
    assert manager.drain_notifications("other") == []
    notifications = manager.drain_notifications("s1")
    assert len(notifications) == 1
    assert "<status>completed</status>" in notifications[0]
    assert "<result>结果</result>" in notifications[0]
    assert manager.drain_notifications("s1") == []


def test_completed_task_records_are_bounded():
    manager = SubagentManager(runner=lambda record: "完成", max_records=2)

    for index in range(3):
        manager.run(f"任务{index}", "s1", f"t{index}", timeout_seconds=30)

    snapshots = manager.snapshot()
    assert len(snapshots) == 2
    assert [task.description for task in snapshots] == ["任务1", "任务2"]


class FakeMonotonic:
    def __init__(self, values):
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


def test_foreground_timeout_sets_terminal_state():
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0]),
        wait_interval=0.001,
    )

    result = manager.run("慢任务", "s1", "t1", timeout_seconds=30)

    payload = json.loads(result)
    assert payload["status"] == "timed_out"
    assert payload["latest_stage"] == "timed_out"
    assert payload["execution_elapsed"] >= 30
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "timed_out"
    assert snapshot.stage == "timed_out"
    assert snapshot.execution_elapsed >= 30


def test_approval_wait_does_not_consume_foreground_timeout():
    release = threading.Event()

    class ApprovalState:
        calls = 0

        def __call__(self, owner_id):
            assert owner_id.startswith("subagent-")
            self.calls += 1
            if self.calls == 1:
                return True
            release.set()
            return False

    manager = SubagentManager(
        runner=lambda record: release.wait(1) and "完成",
        monotonic=FakeMonotonic([0.0, 40.0, 41.0]),
        wait_interval=0.001,
        permission_waiting=ApprovalState(),
    )

    result = manager.run("等待审批", "s1", "t1", timeout_seconds=30)

    assert result == "完成"
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "completed"
    assert snapshot.execution_elapsed == 1.0
    assert snapshot.approval_wait_elapsed == 40.0
    assert "waiting_approval" in [stage.name for stage in snapshot.stages]


def test_foreground_timeout_cancels_owned_permission_requests(monkeypatch):
    from core.permission_broker import permission_broker

    cancelled = []
    monkeypatch.setattr(permission_broker, "cancel_owner", cancelled.append)
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0]),
        wait_interval=0.001,
    )

    result = manager.run("慢任务", "s1", "t1", timeout_seconds=30)

    task_id = json.loads(result)["task_id"]
    assert cancelled == [task_id]


def test_parent_cancel_marks_foreground_task_cancelled():
    parent_cancel = threading.Event()
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        wait_interval=0.001,
    )
    timer = threading.Timer(0.01, parent_cancel.set)
    timer.start()

    result = manager.run(
        "可取消任务",
        "s1",
        "t1",
        timeout_seconds=30,
        parent_cancel_event=parent_cancel,
    )

    assert json.loads(result)["status"] == "cancelled"
    assert manager.snapshot()[0].status == "cancelled"


@pytest.mark.parametrize(
    "runner",
    [
        lambda record: "",
        lambda record: "**\n```\n，。",
        lambda record: (_ for _ in ()).throw(RuntimeError("boom")),
    ],
)
def test_empty_or_exceptional_subagent_is_failed(runner):
    manager = SubagentManager(runner=runner)

    result = manager.run("失败任务", "s1", "t1", timeout_seconds=30)

    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert payload["error"]
    assert manager.snapshot()[0].status == "failed"


@pytest.mark.parametrize("timeout_seconds", [29, 3601])
def test_subagent_timeout_range_is_enforced(timeout_seconds):
    manager = SubagentManager(runner=lambda record: "完成")

    with pytest.raises(ValueError, match="30.*3600"):
        manager.run("任务", "s1", "t1", timeout_seconds=timeout_seconds)


def test_edit_file_rejects_non_unique_target(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "dup.py"
    target.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")

    result = edit_file("dup.py", "x = 1", "x = 3")

    assert result.startswith("🚨")
    assert "2 次" in result
    assert target.read_text(encoding="utf-8") == "x = 1\ny = 2\nx = 1\n"

    result_all = edit_file("dup.py", "x = 1", "x = 3", replace_all=True)
    assert result_all.startswith("✔")
    assert target.read_text(encoding="utf-8") == "x = 3\ny = 2\nx = 3\n"


def test_edit_file_failure_does_not_pollute_undo_stack(tmp_path, monkeypatch):
    import core.memory as memory_module
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "undo.py"
    target.write_text("alpha\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        memory_module,
        "set_undo_state",
        lambda file_path, content: captured.append((file_path, content)),
    )

    result = edit_file("undo.py", "不存在的片段", "beta")

    assert result.startswith("🚨")
    assert captured == []


def test_edit_file_preserves_gbk_encoding_and_crlf(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "gbk.txt"
    original = "第一行\r\n第二行\r\n"
    target.write_bytes(original.encode("gbk"))

    result = edit_file("gbk.txt", "第二行", "第三行")

    assert result.startswith("✔")
    assert target.read_bytes() == "第一行\r\n第三行\r\n".encode("gbk")


def test_edit_file_crlf_replacement_with_existing_crlf_is_not_corrupted(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\nomega\r\n")

    # target 用 LF（不匹配 CRLF 文件，触发换行转换分支），replacement 自带 \r\n
    result = edit_file("crlf.txt", "beta\nomega", "line1\r\nline2")

    assert result.startswith("✔")
    raw = target.read_bytes()
    assert raw == b"alpha\r\nline1\r\nline2\r\n"
    assert b"\r\r\n" not in raw


def test_manage_task_kill_passes_permission_context(monkeypatch):
    import core.tools as tools_module

    captured = {}

    def fake_check(function_name, arguments, **kwargs):
        captured.update(kwargs)
        return False

    monkeypatch.setattr("core.llm.check_permission_and_ask", fake_check)
    cancel_event = threading.Event()

    result = tools_module.manage_task(
        "kill",
        task_id="abc12345",
        event_callback=lambda *args: None,
        permission_owner="owner-1",
        cancel_event=cancel_event,
    )

    assert "拒绝" in result
    assert captured["permission_owner"] == "owner-1"
    assert captured["cancel_event"] is cancel_event
    assert captured["event_callback"] is not None


def test_file_tools_reject_paths_outside_workspace(tmp_path, monkeypatch):
    import pytest as _pytest
    import core.tools as tools_mod
    from core.tools import edit_file, list_directory, read_file, write_file

    monkeypatch.chdir(tmp_path)
    # 不依赖真实配置文件，强制沙盒开启
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: False)

    with _pytest.raises(ValueError, match="路径越界"):
        read_file("../outside.txt")
    with _pytest.raises(ValueError, match="路径越界"):
        write_file("../outside.txt", "data")
    assert "路径越界" in edit_file("../outside.txt", "a", "b")
    assert "路径越界" in list_directory("..")


def test_file_tools_allow_outside_workspace_when_sandbox_off(tmp_path, monkeypatch):
    """/sandbox on 后（allow_outside_workspace=True）文件工具可访问工作目录外的路径"""
    import core.tools as tools_mod
    from core.tools import list_directory, read_file, write_file

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.txt").write_text("外部内容", encoding="utf-8")
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: True)

    assert read_file(str(outside / "note.txt")) == "外部内容"
    write_file(str(outside / "new.txt"), "写入外部")
    assert (outside / "new.txt").read_text(encoding="utf-8") == "写入外部"
    assert "note.txt" in list_directory(str(outside))


def test_outside_workspace_allowed_reads_config(monkeypatch):
    """开关默认 False，配置 allow_outside_workspace=True 时为 True"""
    import core.tools as tools_mod

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {"allow_outside_workspace": True},
    )
    assert tools_mod._outside_workspace_allowed() is True

    monkeypatch.setattr(
        "core.model_manager.load_config",
        lambda: {},
    )
    assert tools_mod._outside_workspace_allowed() is False


def test_read_file_truncates_and_pages_long_files(tmp_path, monkeypatch):
    from core.tools import READ_FILE_MAX_LINES, read_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "long.txt"
    total = READ_FILE_MAX_LINES + 50
    target.write_text("".join(f"line {index}\n" for index in range(total)), encoding="utf-8")

    result = read_file("long.txt")

    assert "已显示第 1-2000 行" in result
    assert f"line {total - 1}" not in result

    tail = read_file("long.txt", offset=READ_FILE_MAX_LINES)
    assert f"line {total - 1}" in tail
    assert "已显示" not in tail


def test_run_subagent_rejects_invalid_timeout_before_claiming_plan_item(monkeypatch):
    import core.tools as tools_module
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("subagent-invalid-timeout-test")
    plan.replace([{"id": "item-1", "subject": "任务"}])
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "不应执行"),
    )

    result = tools_module.run_subagent(
        "任务",
        parent_session="subagent-invalid-timeout-test",
        parent_turn_id="turn-1",
        plan_item_id="item-1",
        timeout_seconds=10,
    )

    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert plan.snapshot()[0]["status"] == "pending"
    bind_default_plan_session(previous_session)


def test_run_subagent_rolls_back_plan_item_when_startup_fails(monkeypatch):
    import core.tools as tools_module
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("subagent-rollback-test")
    plan.replace([{"id": "item-1", "subject": "任务", "files": ["shared.py"]}])

    class FailingManager:
        def run(self, *args, **kwargs):
            raise RuntimeError("subagent task capacity reached")

    monkeypatch.setattr(tools_module, "subagent_manager", FailingManager())

    result = tools_module.run_subagent(
        "任务",
        parent_session="subagent-rollback-test",
        parent_turn_id="turn-1",
        plan_item_id="item-1",
        timeout_seconds=30,
    )

    payload = json.loads(result)
    assert payload["status"] == "failed"
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "capacity" in item["error"]

    from core.task_scheduler import get_default_scheduler

    scheduler = get_default_scheduler()
    assert scheduler.start_item("item-1") is False  # failed 状态不可直接重启
    assert "shared.py" not in scheduler._in_flight_files
    bind_default_plan_session(previous_session)


def test_background_subagent_has_timeout_watchdog():
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0, 32.0]),
        wait_interval=0.001,
    )

    result = manager.run("慢任务", "s1", "t1", run_in_background=True, timeout_seconds=30)

    assert json.loads(result)["status"] == "running"
    assert manager.wait_for_idle(1)
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "timed_out"
    assert snapshot.execution_elapsed >= 30


def test_terminal_callback_fires_only_once_under_concurrent_termination():
    release = threading.Event()
    manager = SubagentManager(
        runner=lambda record: release.wait(1) and "完成",
        wait_interval=0.001,
    )
    callback_count = []
    barrier = threading.Barrier(2)

    result = manager.run(
        "任务",
        "s1",
        "t1",
        run_in_background=True,
        timeout_seconds=30,
        terminal_callback=lambda *args: callback_count.append(args),
    )
    task_id = json.loads(result)["task_id"]

    def terminate():
        barrier.wait()
        manager._terminate(task_id, "failed", "并发终止")

    threads = [threading.Thread(target=terminate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    release.set()
    assert manager.wait_for_idle(1)
    assert len(callback_count) == 1
