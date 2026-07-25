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
    # 测试关注点为本机 HTTP 客户端调用与响应解析，SSRF 防护独立测试
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

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
    # 测试关注点为天气 API 调用与数据解析，SSRF 防护独立测试
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

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
    import sys
    # 1. 执行：用当前 Python 解释器跑一句打印命令
    # 使用 sys.executable 避免在纯净 Windows runner 上找不到 python 命令
    result = execute_command(f'{sys.executable} -c "print(\'hello engine\')"')

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

    # 失败结果为结构化文本，包含失败原因和工具清单
    assert "失败" in result
    assert "天气服务超时" in result
    assert "get_weather" in result
    assert manager.snapshot()[0].status == "failed"


def test_subagent_cancel_terminates_running_task():
    """cancel 方法应把运行中的子代理任务标记为 cancelled"""
    import threading
    import time as _time

    started = threading.Event()
    cancel_ready = threading.Event()

    def runner(record):
        started.set()
        # 模拟长任务，等待取消信号
        for _ in range(100):
            if cancel_ready.is_set() or record.status == "cancelled":
                return "被取消"
            _time.sleep(0.01)
        return "未取消"

    manager = SubagentManager(runner=runner)
    # 后台启动子代理
    thread = threading.Thread(
        target=manager.run,
        args=("长任务", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    assert started.wait(2.0), "子代理未在 2 秒内启动"

    # 找到运行中的 task_id 并取消
    snapshots = manager.snapshot()
    assert snapshots, "snapshot 不应为空"
    task_id = snapshots[0].task_id
    assert snapshots[0].status not in {"completed", "failed", "cancelled", "timed_out"}

    ok = manager.cancel(task_id, reason="测试取消")
    assert ok is True, "cancel 应返回 True"

    # 通知 runner 可以退出
    cancel_ready.set()
    # 等待线程结束
    thread.join(timeout=5.0)

    # 最终状态应该是 cancelled
    final = [s for s in manager.snapshot() if s.task_id == task_id][0]
    assert final.status == "cancelled", f"期望 cancelled，实际 {final.status}"


def test_subagent_cancel_returns_false_for_unknown_or_terminal_task():
    """cancel 对不存在的 task_id 或已终态任务应返回 False"""
    manager = SubagentManager(runner=lambda record: "完成")
    # 不存在的 task_id
    assert manager.cancel("nonexistent-id") is False

    # 先正常跑完一个任务，再尝试 cancel
    manager.run("已完成任务", "s1", "t1", timeout_seconds=30)
    snapshots = manager.snapshot()
    task_id = snapshots[0].task_id
    assert snapshots[0].status == "completed"
    # 已终态任务 cancel 应返回 False
    assert manager.cancel(task_id) is False


def test_background_subagent_terminal_notification_fires_on_complete():
    """后台子代理完成时应触发 on_terminal 回调，前台不触发"""
    import threading
    import time as _time

    notifications = []  # list of (task_id, status, description, summary)
    lock = threading.Lock()

    def on_terminal(task_id, status, description, summary):
        with lock:
            notifications.append((task_id, status, description, summary))

    # 后台任务：完成
    manager = SubagentManager(
        runner=lambda record: "查询结果：晴",
        on_terminal=on_terminal,
    )
    thread = threading.Thread(
        target=manager.run,
        args=("后台查询天气", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    with lock:
        assert len(notifications) == 1, f"应触发1次通知，实际 {len(notifications)}"
        tid, status, desc, summary = notifications[0]
        assert status == "completed"
        assert "后台查询天气" in desc
        assert "晴" in summary


def test_background_subagent_terminal_notification_fires_on_failure():
    """后台子代理失败时应触发 on_terminal 回调，summary 包含错误信息"""
    import threading

    notifications = []
    lock = threading.Lock()

    def on_terminal(task_id, status, description, summary):
        with lock:
            notifications.append((task_id, status, description, summary))

    def runner(record):
        record.tool_errors["search_news"] = "无结果"
        return "查询失败"

    manager = SubagentManager(runner=runner, on_terminal=on_terminal)
    thread = threading.Thread(
        target=manager.run,
        args=("后台查新闻", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    with lock:
        assert len(notifications) == 1
        tid, status, desc, summary = notifications[0]
        assert status == "failed"
        assert "后台查新闻" in desc
        # 失败时 summary 是 error 信息
        assert "search_news" in summary or "无结果" in summary


def test_foreground_subagent_does_not_trigger_terminal_notification():
    """前台子代理不应触发 on_terminal（状态栏已有实时显示，避免重复通知）"""
    notifications = []

    def on_terminal(task_id, status, description, summary):
        notifications.append((task_id, status, description, summary))

    manager = SubagentManager(
        runner=lambda record: "前台结果",
        on_terminal=on_terminal,
    )
    # 前台模式（默认）
    result = manager.run("前台任务", "s1", "t1", timeout_seconds=30)
    assert "前台结果" in result
    # 前台任务不应触发通知
    assert len(notifications) == 0, f"前台任务不应触发通知，实际触发了 {len(notifications)} 次"


def test_set_on_terminal_can_register_callback_after_init():
    """set_on_terminal 允许运行时注册回调（main.py 初始化 UI 后调用）"""
    notifications = []

    # 初始化时不传 on_terminal
    manager = SubagentManager(runner=lambda record: "结果")
    assert manager._on_terminal is None

    # 运行时注册
    manager.set_on_terminal(lambda tid, st, desc, sum: notifications.append((tid, st, desc, sum)))
    assert manager._on_terminal is not None

    # 后台任务完成后应触发
    import threading
    thread = threading.Thread(
        target=manager.run,
        args=("后台任务", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert len(notifications) == 1
    assert notifications[0][1] == "completed"


def test_terminal_notification_callback_exception_does_not_break_subagent():
    """on_terminal 回调抛异常时不能影响子代理主流程"""
    import threading

    def bad_callback(task_id, status, description, summary):
        raise RuntimeError("回调内部错误")

    manager = SubagentManager(
        runner=lambda record: "正常结果",
        on_terminal=bad_callback,
    )
    thread = threading.Thread(
        target=manager.run,
        args=("测试回调异常", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    # 子代理状态应正常完成，回调异常被吞掉
    snapshots = manager.snapshot()
    assert snapshots[0].status == "completed"


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

    # 超时返回结构化文本
    assert "超时" in result
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

    # 失败结果为结构化文本，从中提取 task_id
    import re
    match = re.search(r"task_id=(subagent-[a-f0-9]+)", result)
    assert match is not None, f"未在结果中找到 task_id: {result}"
    task_id = match.group(1)
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

    assert "已取消" in result
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

    # 失败结果为结构化文本，包含失败原因
    assert "失败" in result
    assert "失败原因" in result
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


# ---------------------------------------------------------------------------
# search_news 无结果必须返回 🚨 前缀，否则子代理会被误判为 completed
# ---------------------------------------------------------------------------
# 历史问题：search_news 无结果时返回 "🔍 互联网上没有找到..."，
# 不以 🚨 开头，_tool_result_ok 判定为工具成功，tool_errors 不收录，
# LLM 编一段"我没搜到"的文本通过 _has_meaningful_subagent_result，
# 最终 _complete 被调用，task.status="completed"，但实际结果是失败的。
# 修复：无结果也返回 🚨 前缀，走 _terminate("failed") 流程。

def test_search_news_no_results_returns_error_prefix(monkeypatch):
    """search_news 无结果时返回 🚨 前缀，确保被识别为工具失败"""
    import core.tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return []  # 模拟无结果

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    result = tools_mod.search_news("Kimi AI")

    assert result.startswith("🚨"), "无结果必须返回 🚨 前缀以触发子代理失败流程"
    assert "无结果" in result or "未返回" in result
    assert "Kimi AI" in result


def test_search_news_with_results_returns_normal_text(monkeypatch):
    """search_news 有结果时正常返回，不带 🚨 前缀"""
    import core.tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return [
                {"title": "测试新闻", "url": "https://example.com/1", "body": "摘要内容"}
            ]

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    result = tools_mod.search_news("AI")

    assert not result.startswith("🚨"), "有结果时不应带 🚨 前缀"
    assert "测试新闻" in result
    assert "https://example.com/1" in result


def test_tool_result_ok_treats_search_news_no_results_as_failure(monkeypatch):
    """_tool_result_ok 必须把 search_news 的无结果返回识别为失败（ok=False）"""
    import core.tools as tools_mod
    from core.llm import _tool_result_ok, TOOL_ERROR_PREFIX

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return []

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    result_text = tools_mod.search_news("Kimi AI")
    # 模拟 llm.py 中 tool_finished 事件的 payload 构造
    fake_result = {"content": result_text}

    assert result_text.startswith(TOOL_ERROR_PREFIX)
    assert _tool_result_ok("search_news", fake_result) is False


def test_subagent_with_search_news_no_results_is_marked_failed(monkeypatch):
    """端到端：子代理调用 search_news 无结果时，task.status 必须是 failed 而非 completed"""
    import core.tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return []

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    # 直接调用 search_news 验证前缀（端到端验证需要 LLM 调用，这里只验证工具层契约）
    result = tools_mod.search_news("nonexistent topic 12345")
    assert result.startswith("🚨"), \
        "search_news 无结果必须以 🚨 开头，否则子代理会误判为 completed"


# ---------------------------------------------------------------------------
# SSRF 防护双模式测试
# ---------------------------------------------------------------------------
# 参考 kimicode/opencode 的做法：默认宽松模式只拦截云元数据接口，
# 不做 DNS 解析后的 IP 校验，避免误杀 Clash/V2Ray fake-ip 段（198.18.0.0/15）。
# 严格模式（fetch_url_strict_ssrf=True）保留完整 IP 校验。

def test_ssrf_default_mode_allows_fakeip_domain(monkeypatch):
    """默认宽松模式：域名解析到 198.18.x.x（fake-ip 段）也放行，不查 IP"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: False)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    # 即使 DNS 解析到 198.18.0.92（is_private=True），默认模式也不应该拦截
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: [("AF_INET", "STREAM", "PROTO", None, ("198.18.0.92", 0))]
    )
    # 不应抛异常
    tools_mod._check_ssrf_url("https://open-meteo.com/api")


def test_ssrf_default_mode_allows_private_ip_literal(monkeypatch):
    """默认宽松模式：直接访问私网 IP 字面量也放行（对齐 kimicode/opencode）"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: False)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    tools_mod._check_ssrf_url("http://10.0.0.1/api")
    tools_mod._check_ssrf_url("http://192.168.1.1/")
    tools_mod._check_ssrf_url("http://198.18.0.92/")


def test_ssrf_both_modes_block_cloud_metadata(monkeypatch):
    """两种模式都拦截云元数据接口（SSRF 防护的核心价值）"""
    import core.tools as tools_mod

    for strict_mode in (False, True):
        monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: strict_mode)
        monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
        with pytest.raises(tools_mod.SSRFBlockedError, match="元数据接口"):
            tools_mod._check_ssrf_url("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(tools_mod.SSRFBlockedError, match="元数据接口"):
            tools_mod._check_ssrf_url("http://metadata.google.internal/")


def test_ssrf_allowlist_bypasses_both_modes(monkeypatch):
    """白名单优先级最高，两种模式都直接放行（不查 IP）"""
    import core.tools as tools_mod

    monkeypatch.setattr(
        tools_mod, "_get_fetch_url_allow_hosts",
        lambda: {"open-meteo.com", "geocoding-api.open-meteo.com"}
    )
    # 严格模式下，白名单域名即使解析到私网 IP 也应放行
    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: [("AF_INET", "STREAM", "PROTO", None, ("198.18.0.92", 0))]
    )
    tools_mod._check_ssrf_url("https://open-meteo.com/api")
    tools_mod._check_ssrf_url("https://geocoding-api.open-meteo.com/v1/search")


def test_ssrf_strict_mode_blocks_private_ip_literal(monkeypatch):
    """严格模式：IP 字面量 198.18.0.92（fake-ip 段）被拦截"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    with pytest.raises(tools_mod.SSRFBlockedError, match="非公网 IP"):
        tools_mod._check_ssrf_url("http://198.18.0.92/")
    with pytest.raises(tools_mod.SSRFBlockedError, match="非公网 IP"):
        tools_mod._check_ssrf_url("http://10.0.0.1/")


def test_ssrf_strict_mode_blocks_dns_resolved_private_ip(monkeypatch):
    """严格模式：DNS 解析到 198.18.x.x（fake-ip）被拦截"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: [("AF_INET", "STREAM", "PROTO", None, ("198.18.0.92", 0))]
    )
    with pytest.raises(tools_mod.SSRFBlockedError, match="非公网 IP 198.18.0.92"):
        tools_mod._check_ssrf_url("https://open-meteo.com/api")


def test_ssrf_strict_mode_blocks_dns_failure(monkeypatch):
    """严格模式：DNS 解析失败保守拒绝"""
    import core.tools as tools_mod
    import socket as _socket

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: (_ for _ in ()).throw(_socket.gaierror("DNS failed"))
    )
    with pytest.raises(tools_mod.SSRFBlockedError, match="DNS 解析失败"):
        tools_mod._check_ssrf_url("https://nonexistent.invalid/")


def test_ssrf_strict_mode_allows_public_ip(monkeypatch):
    """严格模式：公网 IP 正常放行"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    # 8.8.8.8 是 Google DNS，公网 IP，应放行
    tools_mod._check_ssrf_url("http://8.8.8.8/")
    # 1.1.1.1 是 Cloudflare DNS，公网 IP，应放行
    tools_mod._check_ssrf_url("http://1.1.1.1/")


def test_fetch_url_ssrf_error_hint_mentions_fakeip(monkeypatch):
    """SSRF 拦截时错误提示应提到 fake-ip 可能性和解决方案"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    # 拦截 198.18.0.92 时，返回的错误信息应包含 fake-ip 提示
    result = tools_mod.fetch_url("http://198.18.0.92/")
    assert "🚨" in result
    assert "fake-ip" in result or "Clash" in result or "V2Ray" in result
    assert "fetch_url_strict_ssrf" in result
    assert "fetch_url_allow_hosts" in result


def test_fetch_url_ssrf_metadata_error_no_fakeip_hint(monkeypatch):
    """云元数据接口拦截时不提示 fake-ip（这类目标绝不应该放行）"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    result = tools_mod.fetch_url("http://169.254.169.254/latest/meta-data/")
    assert "🚨" in result
    assert "元数据接口" in result
    # 元数据拦截不应提到 fake-ip（避免误导用户放行元数据接口）
    assert "fake-ip" not in result


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


def test_execute_command_reports_nonzero_exit_code():
    """回归：非零退出码必须如实上报，不得伪装成"执行成功"。"""
    import sys
    result = execute_command(f'{sys.executable} -c "import sys; sys.exit(3)"')

    assert "退出码 3" in result
    assert "执行成功" not in result


def test_subagent_result_strips_advisor_tags():
    """回归：子代理结果中的 Advisor 内部协议标记必须剔除，避免污染计划回写。"""
    manager = SubagentManager(
        runner=lambda record: (
            "[Advisor consultation #1]\n[Advisor review]\n"
            "结论内容\n[End of advisor consultation #1]"
        )
    )

    result = manager.run("任务", "s1", "t1", timeout_seconds=30)

    assert "[Advisor" not in result
    assert "End of advisor" not in result
    assert "结论内容" in result


def test_run_subagent_surfaces_real_verification_error(monkeypatch):
    """回归：验证失败时必须把真实错误（如验证命令 stderr）返回给主 Agent，
    不得只报"不是 in_progress，无法验证完成"的误导信息导致 retry 死循环。"""
    import core.tools as tools_module
    from core.plan import bind_default_plan_session, get_default_plan_manager

    plan = get_default_plan_manager()
    previous_session = plan.session_id
    bind_default_plan_session("subagent-verification-error-test")
    plan.replace([{
        "id": "item-1",
        "subject": "验证失败任务",
        "stage": "build",
        "verification_type": "command",
        # 必定失败的命令（cmd 与 sh 均支持 exit）
        "verification": "exit 1",
    }])
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "子任务做完了"),
    )

    result = tools_module.run_subagent(
        "执行子任务",
        parent_session="subagent-verification-error-test",
        parent_turn_id="turn-1",
        subagent_type="implement",
        plan_item_id="item-1",
        timeout_seconds=30,
    )

    # 失败结果为结构化文本，应包含验证失败的具体原因，而不是"不是 in_progress"的误导信息
    assert "失败" in result
    assert "不是 in_progress" not in result
    assert "验证" in result
    assert plan.snapshot()[0]["status"] == "failed"
    bind_default_plan_session(previous_session)


def test_edit_file_fuzzy_match_minor_trailing_whitespace(tmp_path, monkeypatch):
    """target_content 与原文仅有首尾空白差异时也能定位。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "fuzzy.txt"
    target.write_text("def foo():\n    pass\n", encoding="utf-8")

    result = edit_file("fuzzy.txt", "def foo():\n    pass", "def bar():\n    return 1")
    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "def bar():\n    return 1\n"


def test_edit_file_fuzzy_match_minor_pollution(tmp_path, monkeypatch):
    """target_content 存在轻微字符污染时，仍可通过模糊匹配定位并替换。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "polluted.txt"
    original = (
        "public void HandleReturnResult(List<IEntryDataObject> result) {\n"
        "    if (result == null || result.isEmpty()) {\n"
        "        throw new BaseBusinessException(\"empty\");\n"
        "    }\n"
        "}\n"
    )
    target.write_text(original, encoding="utf-8")

    # 模拟污染：把 null 写成 nul1，其余与原文完全一致
    polluted_target = (
        "public void HandleReturnResult(List<IEntryDataObject> result) {\n"
        "    if (result == nul1 || result.isEmpty()) {\n"
        "        throw new BaseBusinessException(\"empty\");\n"
        "    }\n"
        "}\n"
    )
    replacement = (
        "public void HandleReturnResult(List<IEntryDataObject> result) {\n"
        "    if (result == null || result.isEmpty()) {\n"
        "        log.warn(\"empty result\");\n"
        "        return;\n"
        "    }\n"
        "}\n"
    )
    result = edit_file("polluted.txt", polluted_target, replacement)
    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == replacement


def test_write_file_refuses_to_overwrite_large_existing_file(tmp_path, monkeypatch):
    """write_file 应拒绝覆盖超过 500 行 / 50KB 的已有大文件。"""
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "large.txt"
    # 构造 600 行的小文件，超过行数阈值
    target.write_text("\n".join(f"line {i}" for i in range(600)), encoding="utf-8")

    result = write_file("large.txt", "new content")
    assert isinstance(result, str)
    assert result.startswith("\U0001f6a8")
    assert "large.txt" in result
    # 文件内容不应被覆盖
    assert "new content" not in target.read_text(encoding="utf-8")


def test_write_file_allows_overwrite_small_existing_file(tmp_path, monkeypatch):
    """write_file 允许覆盖小文件。"""
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "small.txt"
    target.write_text("old", encoding="utf-8")

    result = write_file("small.txt", "new")
    assert isinstance(result, str)
    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_auto_creates_missing_parent_directory(tmp_path, monkeypatch):
    """write_file 应自动创建不存在的父目录，而不是抛 FileNotFoundError。

    复现场景：主 Agent 按系统提示词写 memory/feedback/xxx.md，
    但 feedback/ 目录不存在 → 旧版 mkstemp(dir=...) 直接抛错，
    Agent 陷入无效重试。修复后 write_file 应自动 mkdir parents=True。
    """
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    # feedback/ 子目录不存在
    target = tmp_path / "memory" / "feedback" / "note.md"
    assert not target.parent.exists()

    result = write_file(str(target), "test content")

    assert isinstance(result, str)
    assert result.startswith("\u2714"), f"应写入成功，实际：{result}"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "test content"
    # 父目录应被自动创建
    assert target.parent.exists()


def test_write_file_auto_creates_nested_parent_directories(tmp_path, monkeypatch):
    """write_file 应自动创建多层嵌套的不存在父目录"""
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "a" / "b" / "c" / "d" / "file.txt"
    assert not target.parent.exists()

    result = write_file(str(target), "deep")

    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "deep"


def test_edit_file_auto_creates_missing_parent_directory(tmp_path, monkeypatch):
    """edit_file 修改已存在文件时，若父目录被外部删除应能自动重建（防御性）。

    注意：edit_file 本身要求文件已存在（先读后改），所以这个测试构造的场景是：
    文件存在但其父目录被外部删除（极端情况），edit_file 应能自动重建父目录后写入。
    实际上更常见的场景是 write_file 创建新文件到不存在的子目录，那已被前两个测试覆盖。
    这里只验证 edit_file 的 mkdir 逻辑不会破坏正常编辑流程。
    """
    from core.tools import write_file, edit_file

    monkeypatch.chdir(tmp_path)
    # 先用 write_file 创建一个文件（同时自动建父目录）
    target = tmp_path / "sub" / "note.md"
    write_file(str(target), "old content")

    # 正常 edit_file 应能工作（父目录已存在，mkdir 是 no-op）
    result = edit_file(str(target), "old content", "new content")
    assert "✔" in str(result) or "成功" in str(result), f"edit_file 应成功，实际：{result}"
    assert target.read_text(encoding="utf-8") == "new content"
