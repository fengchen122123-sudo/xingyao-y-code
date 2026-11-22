import pytest
import hashlib
import json
import threading
import time
from collections import deque
from unittest.mock import ANY
from types import SimpleNamespace
from pathlib import Path
from core.tools import (
    SubagentManager,
    _SUBAGENT_TERMINAL_STATUSES,
    execute_command,
    fetch_url,
    get_weather,
    list_directory,
    read_file,
    write_file,
)


@pytest.fixture(autouse=True)
def _disable_free_web_search(monkeypatch):
    from core import web_search

    def _unavailable(*args, **kwargs):
        raise web_search.SearchBackendError("测试中禁用免费网页通道")

    monkeypatch.setattr(web_search, "search_web", _unavailable)


def test_subagent_task_id_validation_is_strict():
    from core.tools import is_subagent_task_id

    assert is_subagent_task_id("subagent-1234abcd") is True
    for value in (
        "subagent-demo:12345678",
        "subagent-1234ABCd",
        "subagent-1234abc",
        "prefix-subagent-1234abcd",
        "subagent-1234abcd-extra",
    ):
        assert is_subagent_task_id(value) is False


def _wait_until(condition, timeout=5.0, interval=0.02):
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if condition():
            return True
        _time.sleep(interval)
    return bool(condition())


def _all_tasks_terminal(manager):
    snapshots = manager.snapshot()
    return bool(snapshots) and all(
        s.status in _SUBAGENT_TERMINAL_STATUSES for s in snapshots
    )


_TURN_UNDO_SHARED: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _turn_undo_module_service(tmp_path_factory):
    from core.memory import MemoryManager
    from core.turn_undo import service as turn_undo_service

    root = tmp_path_factory.mktemp("turn-undo-module")
    manager = MemoryManager(str(root / "module.db"))
    _TURN_UNDO_SHARED["storage"] = turn_undo_service.configure_default_manager(
        manager, blob_root=root / "blobs"
    )
    _TURN_UNDO_SHARED["manager"] = manager
    yield


@pytest.fixture(autouse=True)
def _turn_undo_service_ready():
    from core.turn_undo import service as turn_undo_service

    with turn_undo_service._service_lock:
        prev = (turn_undo_service._default_manager, turn_undo_service._default_storage)
        turn_undo_service._default_manager = _TURN_UNDO_SHARED["manager"]
        turn_undo_service._default_storage = _TURN_UNDO_SHARED["storage"]
    yield
    with turn_undo_service._service_lock:
        turn_undo_service._default_manager, turn_undo_service._default_storage = prev


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
        self._pos = 0
        self.status = 200
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit=-1):
        if limit < 0:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk


def test_fetch_url_uses_local_http_client_and_returns_response(monkeypatch):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request, timeout))
        return _FakeHTTPResponse('{"ok": true}')

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    result = fetch_url("https://example.com/data")

    assert "HTTP 200" in result
    assert '{"ok": true}' in result
    assert calls[0][0].full_url == "https://example.com/data"
    assert calls[0][0].get_header("User-agent")
    assert calls[0][1] == 20


def test_fetch_url_uses_browser_like_headers(monkeypatch):
    captured_request = []

    def fake_urlopen(request, timeout):
        captured_request.append(request)
        return _FakeHTTPResponse("<html>内容</html>", "text/html; charset=utf-8")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    fetch_url("https://example.com/article")

    assert len(captured_request) == 1
    req = captured_request[0]
    ua = req.get_header("User-agent")
    assert "Mozilla/5.0" in ua, f"应伪装为浏览器 UA，实际: {ua}"
    assert "Chrome" in ua, f"UA 应包含 Chrome 标识，实际: {ua}"
    assert "ycode" not in ua, f"UA 不应包含 ycode（会被反爬拦截），实际: {ua}"
    assert req.get_header("Accept-language"), "应携带 Accept-Language 头"
    assert req.get_header("Referer"), "应携带 Referer 头"


def test_fetch_url_truncates_large_response_instead_of_failing(monkeypatch):
    from core.tools import HTTP_MAX_BYTES
    large_body = "x" * (HTTP_MAX_BYTES + 1024)

    def fake_urlopen(request, timeout):
        return _FakeHTTPResponse(large_body, "text/html; charset=utf-8")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    result = fetch_url("https://example.com/large-article")

    assert not result.startswith("🚨"), f"大响应应截断而非报错，实际: {result[:100]}"
    assert "HTTP 200" in result
    assert "已截断" in result, "应附加截断提示"
    assert "x" in result


def test_fetch_url_does_not_truncate_small_response(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeHTTPResponse('{"small": "content"}', "application/json; charset=utf-8")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    result = fetch_url("https://example.com/small")

    assert "HTTP 200" in result
    assert '{"small": "content"}' in result
    assert "已截断" not in result, "小响应不应附加截断提示"


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

def test_read_and_write_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test_file = tmp_path / "hello.py"
    content_to_write = "print('Hello, Y-code!')"
    
    write_file(test_file, content_to_write)
    read_content = read_file(test_file)
    
    assert read_content == content_to_write

def test_list_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "main.py").touch()
    (tmp_path / "readme.md").touch()
    
    files = list_directory(str(tmp_path))
    
    assert "main.py" in files
    assert "readme.md" in files

def test_execute_command():
    import sys
    result = execute_command(f'{sys.executable} -c "print(\'hello engine\')"')

    assert "hello engine" in result


def test_execute_command_rejects_catastrophic_commands_before_spawn(monkeypatch):
    monkeypatch.setattr(
        "core.tools.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    result = execute_command("rm -rf / && echo done")

    assert result.startswith("🚨")
    assert "危险命令" in result
    assert "write_file" in result


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

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        kwargs["stdout"].write(b"shell handled command")
        kwargs["stdout"].flush()
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


def test_subagent_write_is_recorded_in_parent_turn_checkpoint(tmp_path, monkeypatch):
    from core.memory import MemoryManager
    from core.turn_undo import service as turn_undo_service
    from core.turn_undo import recorder as turn_undo_recorder
    from core.turn_undo.models import OwnerKind, NodeKind

    monkeypatch.chdir(tmp_path)
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("checkpoint-session", [("user", "hi")])
    session_id = turn_undo_service.get_default_storage().resolve_session_id(
        "checkpoint-session"
    )
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=session_id,
        turn_id="checkpoint-turn",
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )

    target = tmp_path / "subagent-owned.txt"
    target.write_bytes(b"before")

    def runner(_record):
        assert "写入成功" in write_file(str(target), "after")
        return "子代理写入完成"

    try:
        result = SubagentManager(runner=runner).run(
            "修改文件",
            "checkpoint-session",
            "checkpoint-turn",
            timeout_seconds=30,
        )
    finally:
        status = turn_undo_service.finish_turn(ctx)

    assert result == "子代理写入完成"
    assert status.value == "ready"
    files = turn_undo_service.get_default_storage().list_files(checkpoint.checkpoint_id)
    assert len(files) == 1
    row = files[0]
    assert row.display_path == "subagent-owned.txt"
    assert row.before_sha256 == hashlib.sha256(b"before").hexdigest()
    assert row.after_sha256 == hashlib.sha256(b"after").hexdigest()
    assert row.verified_at_ms is not None
    assert row.last_source_kind == "subagent"

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


def test_completed_callback_claim_blocks_cancel_idle_and_prune_until_done():
    entered = threading.Event()
    release = threading.Event()
    callbacks = []
    plan_terminal = []

    def terminal_callback(_task, status, _result, _error):
        callbacks.append(status)
        entered.set()
        assert release.wait(2.0)
        plan_terminal.append(status)

    manager = SubagentManager(runner=lambda _record: "完成")
    run_thread = threading.Thread(
        target=manager.run,
        args=("执行任务", "s-finalize", "turn-finalize"),
        kwargs={"timeout_seconds": 30, "terminal_callback": terminal_callback},
        daemon=True,
    )
    run_thread.start()
    assert entered.wait(2.0)
    task = next(iter(manager._tasks.values()))

    cancel_results = []
    cancel_thread = threading.Thread(
        target=lambda: cancel_results.append(
            manager.cancel_foreground("s-finalize", "turn-finalize")
        ),
        daemon=True,
    )
    cancel_thread.start()
    time.sleep(0.05)

    try:
        callbacks_while_blocked = list(callbacks)
        active_while_blocked = manager.get_active_count()
        idle_while_blocked = manager.wait_for_idle(0)
        with manager._lock:
            manager._prune_completed_locked()
            retained_while_blocked = task.task_id in manager._tasks
            # 旧实现可能错误清掉 finalizing 记录；补回仅用于让测试线程安全收尾。
            manager._tasks.setdefault(task.task_id, task)
    finally:
        release.set()
        run_thread.join(timeout=2.0)
        cancel_thread.join(timeout=2.0)

    assert callbacks_while_blocked == ["completed"]
    assert active_while_blocked == 1
    assert idle_while_blocked is False
    assert retained_while_blocked is True
    assert callbacks == ["completed"]
    assert plan_terminal == ["completed"]
    assert manager.snapshot()[0].status == "completed"
    assert task.done_event.is_set()
    assert manager.get_active_count() == 0


def test_subagent_structured_tool_failure_cannot_complete():
    def runner(record):
        record.tool_errors["get_weather"] = "天气服务超时"
        return "天气查询失败"

    manager = SubagentManager(runner=runner)
    result = manager.run("查询天气", "s1", "t1", timeout_seconds=30)

    assert "失败" in result
    assert "天气服务超时" in result
    assert "get_weather" in result
    assert manager.snapshot()[0].status == "failed"


def test_subagent_cancel_terminates_running_task():
    import threading
    import time as _time

    started = threading.Event()
    cancel_ready = threading.Event()

    def runner(record):
        started.set()
        for _ in range(100):
            if cancel_ready.is_set() or record.status == "cancelled":
                return "被取消"
            _time.sleep(0.01)
        return "未取消"

    manager = SubagentManager(runner=runner)
    thread = threading.Thread(
        target=manager.run,
        args=("长任务", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    assert started.wait(10.0), "子代理未在 10 秒内启动"

    snapshots = manager.snapshot()
    assert snapshots, "snapshot 不应为空"
    task_id = snapshots[0].task_id
    assert snapshots[0].status not in {"completed", "failed", "cancelled", "timed_out"}

    ok = manager.cancel(task_id, reason="测试取消")
    assert ok is True, "cancel 应返回 True"

    cancel_ready.set()
    thread.join(timeout=5.0)

    final = [s for s in manager.snapshot() if s.task_id == task_id][0]
    assert final.status == "cancelled", f"期望 cancelled，实际 {final.status}"


def test_subagent_cancel_returns_false_for_unknown_or_terminal_task():
    manager = SubagentManager(runner=lambda record: "完成")
    assert manager.cancel("nonexistent-id") is False

    manager.run("已完成任务", "s1", "t1", timeout_seconds=30)
    snapshots = manager.snapshot()
    task_id = snapshots[0].task_id
    assert snapshots[0].status == "completed"
    assert manager.cancel(task_id) is False


def test_background_subagent_terminal_notification_fires_on_complete():
    import threading
    import time as _time

    notifications = []
    lock = threading.Lock()

    def on_terminal(task_id, status, description, summary):
        with lock:
            notifications.append((task_id, status, description, summary))

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

    assert _wait_until(lambda: len(notifications) >= 1), "应触发1次通知"
    with lock:
        assert len(notifications) == 1, f"应触发1次通知，实际 {len(notifications)}"
        tid, status, desc, summary = notifications[0]
        assert status == "completed"
        assert "后台查询天气" in desc
        assert "晴" in summary


def test_background_subagent_terminal_notification_fires_on_failure():
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

    assert _wait_until(lambda: len(notifications) >= 1), "失败也应触发1次通知"
    with lock:
        assert len(notifications) == 1
        tid, status, desc, summary = notifications[0]
        assert status == "failed"
        assert "后台查新闻" in desc
        assert "search_news" in summary or "无结果" in summary


def test_foreground_subagent_does_not_trigger_terminal_notification():
    notifications = []

    def on_terminal(task_id, status, description, summary):
        notifications.append((task_id, status, description, summary))

    manager = SubagentManager(
        runner=lambda record: "前台结果",
        on_terminal=on_terminal,
    )
    result = manager.run("前台任务", "s1", "t1", timeout_seconds=30)
    assert "前台结果" in result
    assert len(notifications) == 0, f"前台任务不应触发通知，实际触发了 {len(notifications)} 次"


def test_set_on_terminal_can_register_callback_after_init():
    notifications = []

    manager = SubagentManager(runner=lambda record: "结果")
    assert manager._on_terminal is None

    manager.set_on_terminal(lambda tid, st, desc, sum: notifications.append((tid, st, desc, sum)))
    assert manager._on_terminal is not None

    import threading
    thread = threading.Thread(
        target=manager.run,
        args=("后台任务", "s1", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert _wait_until(lambda: len(notifications) >= 1), "后台任务完成后应触发通知"
    assert len(notifications) == 1
    assert notifications[0][1] == "completed"


def test_terminal_notification_callback_exception_does_not_break_subagent():
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

    assert _wait_until(
        lambda: manager.snapshot() and manager.snapshot()[0].status == "completed"
    ), "回调异常不应影响子代理完成"
    snapshots = manager.snapshot()
    assert snapshots[0].status == "completed"


def test_is_plan_item_running_returns_true_for_running_task():
    import threading
    import time as _time

    started = threading.Event()

    def slow_runner(record):
        started.set()
        while not record.cancel_event.is_set():
            _time.sleep(0.01)
        return "已取消"

    manager = SubagentManager(runner=slow_runner)
    thread = threading.Thread(
        target=manager.run,
        args=("后台长任务", "s1", "t1"),
        kwargs={
            "timeout_seconds": 30,
            "run_in_background": True,
            "plan_item_id": "plan-running-1",
        },
        daemon=True,
    )
    thread.start()
    assert started.wait(10.0), "子代理应在 10s 内启动"

    try:
        assert manager.is_plan_item_running("plan-running-1") is True
        assert manager.is_plan_item_running("plan-nonexistent") is False
        assert manager.is_plan_item_running("") is False
    finally:
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread.join(timeout=2.0)


def test_is_plan_item_running_returns_false_for_completed_task():
    import threading

    manager = SubagentManager(runner=lambda record: "完成")
    thread = threading.Thread(
        target=manager.run,
        args=("后台任务", "s1", "t1"),
        kwargs={
            "timeout_seconds": 30,
            "run_in_background": True,
            "plan_item_id": "plan-done-1",
        },
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert _wait_until(lambda: _all_tasks_terminal(manager)), "后台任务应在超时前进入终态"
    assert manager.is_plan_item_running("plan-done-1") is False


def test_drain_terminal_background_tasks_returns_all_terminal_unconsumed():
    import threading

    def runner(record):
        if "失败" in record.description:
            record.tool_errors["search_news"] = "无结果"
            return "查询失败"
        return "查询结果：晴"

    manager = SubagentManager(runner=runner)

    thread1 = threading.Thread(
        target=manager.run,
        args=("后台查天气", "s-fallback", "t1"),
        kwargs={
            "timeout_seconds": 30,
            "run_in_background": True,
            "plan_item_id": "plan-ok-1",
        },
        daemon=True,
    )
    thread1.start()
    thread1.join(timeout=5.0)

    thread2 = threading.Thread(
        target=manager.run,
        args=("后台查失败新闻", "s-fallback", "t2"),
        kwargs={
            "timeout_seconds": 30,
            "run_in_background": True,
            "plan_item_id": "plan-failed-1",
        },
        daemon=True,
    )
    thread2.start()
    thread2.join(timeout=5.0)

    assert _wait_until(lambda: len(manager.snapshot()) == 2 and _all_tasks_terminal(manager)), \
        "两个后台任务都应进入终态"
    drained = manager.drain_terminal_background_tasks("s-fallback")
    assert len(drained) == 2, f"应提取 2 个任务，实际 {len(drained)}"

    statuses = {t.status for t in drained}
    assert "completed" in statuses, "应包含已完成的任务"
    assert "failed" in statuses, "应包含失败的任务"

    completed_task = next(t for t in drained if t.status == "completed")
    assert "晴" in completed_task.result

    drained_again = manager.drain_terminal_background_tasks("s-fallback")
    assert drained_again == [], "已消费的任务不应重复提取"

    drained_other = manager.drain_terminal_background_tasks("s-other")
    assert drained_other == [], "其他会话的任务不应被提取"


def test_drain_terminal_independent_from_notification_consumed():
    import threading

    manager = SubagentManager(runner=lambda record: "查询结果：晴")
    thread = threading.Thread(
        target=manager.run,
        args=("后台查天气", "s-indep", "t1"),
        kwargs={
            "timeout_seconds": 30,
            "run_in_background": True,
            "plan_item_id": "plan-ok-1",
        },
        daemon=True,
    )
    thread.start()
    thread.join(timeout=5.0)

    assert _wait_until(lambda: _all_tasks_terminal(manager)), "后台任务应在超时前进入终态"
    notifications = manager.drain_notifications("s-indep")
    assert len(notifications) == 1, "drain_notifications 应返回 1 条通知"

    drained = manager.drain_terminal_background_tasks("s-indep")
    assert len(drained) == 1, (
        "drain_terminal_background_tasks 应独立于 notification_consumed，"
        "即使 drain_notifications 已消费通知，汇总 turn 仍应能看到任务结果"
    )
    assert drained[0].status == "completed"
    assert "晴" in drained[0].result

    drained_again = manager.drain_terminal_background_tasks("s-indep")
    assert drained_again == [], "summary_injected 已标记的任务不应重复提取"


def test_drain_terminal_background_tasks_skips_running_and_foreground():
    import threading
    import time as _time

    started = threading.Event()
    manager = SubagentManager(runner=lambda record: (
        started.set(),
        _time.sleep(10),
        "结果"
    )[2])
    thread1 = threading.Thread(
        target=manager.run,
        args=("后台运行中任务", "s-mix", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True, "plan_item_id": "p-bg-running"},
        daemon=True,
    )
    thread1.start()
    assert started.wait(10.0), "子代理应在 10s 内启动"

    try:
        drained = manager.drain_terminal_background_tasks("s-mix")
        assert drained == [], "运行中的后台任务不应被 drain"
    finally:
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread1.join(timeout=2.0)

    manager_fg = SubagentManager(runner=lambda record: "前台结果")
    manager_fg.run("前台任务", "s-mix", "t1", timeout_seconds=30, plan_item_id="p-fg-ok")

    drained_fg = manager_fg.drain_terminal_background_tasks("s-mix")
    assert drained_fg == [], "前台任务不应被 drain"


def test_has_running_background_tasks_detects_running():
    import threading
    import time as _time

    started = threading.Event()

    def slow_runner(record):
        started.set()
        while not record.cancel_event.is_set():
            _time.sleep(0.01)
        return "已取消"

    manager = SubagentManager(runner=slow_runner)
    thread = threading.Thread(
        target=manager.run,
        args=("后台长任务", "s-running", "t1"),
        kwargs={"timeout_seconds": 30, "run_in_background": True},
        daemon=True,
    )
    thread.start()
    assert started.wait(10.0), "子代理应在 10s 内启动"

    try:
        assert manager.has_running_background_tasks("s-running") is True
        assert manager.has_running_background_tasks("s-other") is False
        assert manager.has_running_background_tasks("") is False
    finally:
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread.join(timeout=2.0)

    assert manager.has_running_background_tasks("s-running") is False


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


def test_run_subagent_denied_command_verification_never_executes_command(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{
        "id": "build",
        "subject": "构建项目",
        "stage": "build",
        "verification_type": "command",
        "verification": "pytest -q",
    }])
    permission_calls = []
    command_calls = []
    shell_launches = []

    class FakeProcess:
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return b"verification passed", b""

    def deny_permission(*args, **kwargs):
        permission_calls.append((args, kwargs))
        return False

    monkeypatch.setattr("core.llm.check_permission_and_ask", deny_permission)
    monkeypatch.setattr(
        tools_module,
        "execute_command",
        lambda *args, **kwargs: command_calls.append((args, kwargs)) or "should not run",
    )
    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *args, **kwargs: shell_launches.append((args, kwargs)) or FakeProcess(),
    )
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler",
        lambda: TaskScheduler(plan),
    )
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "实现完成"),
    )

    tools_module.run_subagent(
        "完成构建",
        parent_session="subagent-denied-verification-test",
        parent_turn_id="turn-denied-verification",
        subagent_type="implement",
        plan_item_id="build",
        timeout_seconds=30,
    )

    assert permission_calls == [(
        ("execute_command", {"command": "pytest -q"}),
        {
            "event_callback": None,
            "permission_owner": "subagent:implement",
            "parent_turn_id": "turn-denied-verification",
            "permission_source": "计划项验证：构建项目",
            "cancel_event": ANY,
        },
    )]
    assert command_calls == []
    assert shell_launches == []
    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "未获用户授权" in item["error"]


def test_run_subagent_command_verification_fails_for_nonzero_exit_without_output(monkeypatch):
    import sys
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{
        "id": "build",
        "subject": "构建项目",
        "stage": "build",
        "verification_type": "command",
        "verification": f'"{sys.executable}" -c "import sys; sys.exit(7)"',
    }])
    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler",
        lambda: TaskScheduler(plan),
    )
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "实现完成"),
    )

    tools_module.run_subagent(
        "完成构建",
        parent_session="subagent-nonzero-verification-test",
        parent_turn_id="turn-nonzero-verification",
        subagent_type="implement",
        plan_item_id="build",
        timeout_seconds=30,
    )

    item = plan.snapshot()[0]
    assert item["status"] == "failed"
    assert "退出码 7" in item["error"]


def test_run_subagent_command_verification_completes_for_zero_exit(monkeypatch):
    import sys
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{
        "id": "build",
        "subject": "构建项目",
        "stage": "build",
        "verification_type": "command",
        "verification": f'"{sys.executable}" -c "import sys; sys.exit(0)"',
    }])
    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler",
        lambda: TaskScheduler(plan),
    )
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "实现完成"),
    )

    tools_module.run_subagent(
        "完成构建",
        parent_session="subagent-zero-verification-test",
        parent_turn_id="turn-zero-verification",
        subagent_type="implement",
        plan_item_id="build",
        timeout_seconds=30,
    )

    assert plan.snapshot()[0]["status"] == "completed"


def test_execute_command_internal_results_are_call_scoped():
    import sys
    import core.tools as tools_module

    successful = tools_module._CommandExecutionResult()
    failed = tools_module._CommandExecutionResult()

    tools_module.execute_command(
        f'"{sys.executable}" -c "import sys; sys.exit(0)"',
        _execution_result=successful,
    )
    tools_module.execute_command(
        f'"{sys.executable}" -c "import sys; sys.exit(4)"',
        _execution_result=failed,
    )

    assert successful.returncode == 0
    assert failed.returncode == 4


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


def test_background_subagent_startup_failure_reaches_terminal_state():
    from core.turn_undo import service as turn_undo_service

    manager = SubagentManager(runner=lambda record: "不应被执行到")
    with turn_undo_service._service_lock:
        prev = (turn_undo_service._default_manager, turn_undo_service._default_storage)
        turn_undo_service._default_manager = None
        turn_undo_service._default_storage = None
    try:
        result = manager.run(
            "后台任务",
            "s1",
            "t1",
            run_in_background=True,
            timeout_seconds=30,
        )
        assert "subagent-" in result
        assert _wait_until(lambda: _all_tasks_terminal(manager)), "启动失败应落成终态"
        snapshot = manager.snapshot()[0]
        assert snapshot.status == "failed"
        assert "启动失败" in snapshot.error
        assert "turn_undo" in snapshot.error
    finally:
        with turn_undo_service._service_lock:
            turn_undo_service._default_manager, turn_undo_service._default_storage = prev


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


def test_timeout_sets_cancel_before_handoff_callback():
    seen = {}

    def terminal_callback(task, status, _result, _error):
        seen["status"] = status
        seen["cancelled"] = task.cancel_event.is_set()

    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0]),
        wait_interval=0.001,
    )
    manager.run(
        "slow", "s", "t", timeout_seconds=30,
        terminal_callback=terminal_callback,
    )
    assert seen == {"status": "timed_out", "cancelled": True}


def test_run_subagent_timeout_hands_off_plan_item_to_main(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "task-1", "subject": "超时任务"}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    slow_manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 31.0]),
        wait_interval=0.001,
    )
    monkeypatch.setattr(tools_module, "subagent_manager", slow_manager)

    result = tools_module.run_subagent(
        "慢任务",
        parent_session="s-handoff",
        parent_turn_id="turn-h1",
        plan_item_id="task-1",
        subagent_type="implement",
        timeout_seconds=30,
    )

    payload = json.loads(result)
    assert payload["status"] == "timed_out"
    assert payload["handoff"] == "main_takeover"
    assert payload["plan_item_id"] == "task-1"
    assert payload["inspection_required"] is True
    item = plan.snapshot()[0]
    assert item["status"] == "in_progress"
    assert item["execution_mode"] == "main_takeover"
    assert item["takeover_turn_id"] == "turn-h1"
    assert item["attempts"] == 1
    assert item["handoff_task_id"] == payload["task_id"]
    assert item["handoff_task_id"].startswith("subagent-")


def test_run_subagent_rejects_redelegation_after_timeout_takeover(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([{"id": "item", "subject": "same work"}])
    scheduler = TaskScheduler(plan)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    assert scheduler.start_delegated_item(
        "item", "subagent-abcd1234", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "item", "subagent-abcd1234", "turn-1", "foreground timeout"
    ) is not None

    class UnusedManager:
        def run(self, *args, **kwargs):
            raise AssertionError("接管中的计划项不得再次派发子代理")

    monkeypatch.setattr(tools_module, "subagent_manager", UnusedManager())

    result = tools_module.run_subagent(
        "retry same work",
        parent_session="s",
        parent_turn_id="t",
        plan_item_id="item",
        subagent_type="implement",
    )
    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "main_takeover_required"
    item = plan.snapshot()[0]
    assert item["status"] == "in_progress"
    assert item["execution_mode"] == "main_takeover"


def test_run_subagent_rejects_redelegation_of_queued_takeover(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([
        {"id": "first", "subject": "first"},
        {"id": "second", "subject": "second"},
    ])
    scheduler = TaskScheduler(plan, max_workers=2)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    for item_id, source in (("first", "subagent-aaa1"), ("second", "subagent-bbb2")):
        assert scheduler.start_delegated_item(
            item_id, source, "subagent:implement", None
        ) is True
    assert scheduler.handoff_to_main(
        "first", "subagent-aaa1", "turn-1", "timeout"
    ) is not None
    assert scheduler.handoff_to_main(
        "second", "subagent-bbb2", "turn-1", "timeout"
    ) is not None

    result = tools_module.run_subagent(
        "retry queued",
        parent_session="s",
        parent_turn_id="t",
        plan_item_id="second",
        subagent_type="implement",
    )
    payload = json.loads(result)
    assert payload["status"] == "blocked"
    assert payload["reason"] == "main_takeover_required"
    assert plan.snapshot()[1]["execution_mode"] == "takeover_queued"


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
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "undo.py"
    target.write_text("alpha\n", encoding="utf-8")

    result = edit_file("undo.py", "不存在的片段", "beta")

    assert result.startswith("🚨")
    assert target.read_text(encoding="utf-8") == "alpha\n"


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


@pytest.mark.parametrize(
    "result_constant,expected_fragment",
    [
        ("KILL_RESULT_KILLED", "已被强行终结清除"),
        ("KILL_RESULT_NATURAL_DEATH", "已自行结束"),
        ("KILL_RESULT_NOT_FOUND", "未找到任务"),
        ("KILL_RESULT_FAILED", "仍在运行"),
    ],
)
def test_manage_task_kill_messages_match_kill_result(monkeypatch, result_constant, expected_fragment):
    import core.tools as tools_module
    import core.task_manager as task_manager_module

    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        task_manager_module,
        "kill_background_task_detailed",
        lambda task_id: getattr(task_manager_module, result_constant),
    )

    result = tools_module.manage_task("kill", task_id="t1")

    assert expected_fragment in result
    if result_constant == "KILL_RESULT_NATURAL_DEATH":
        assert "失败" not in result


def test_manage_task_kill_identity_unverifiable_message(monkeypatch):
    import core.tools as tools_module
    import core.task_manager as task_manager_module

    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        task_manager_module,
        "kill_background_task_detailed",
        lambda task_id: task_manager_module.KILL_RESULT_IDENTITY_UNVERIFIABLE,
    )

    result = tools_module.manage_task("kill", task_id="t1")

    assert "无法校验进程身份" in result


def test_file_tools_reject_paths_outside_workspace(tmp_path, monkeypatch):
    import pytest as _pytest
    import core.tools as tools_mod
    from core.tools import edit_file, list_directory, read_file, write_file

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: False)

    with _pytest.raises(ValueError, match="路径越界"):
        read_file("../outside.txt")
    with _pytest.raises(ValueError, match="路径越界"):
        write_file("../outside.txt", "data")
    assert "路径越界" in edit_file("../outside.txt", "a", "b")
    assert "路径越界" in list_directory("..")


def test_file_tools_allow_outside_workspace_when_sandbox_off(tmp_path, monkeypatch):
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


def test_relative_paths_resolve_against_thread_workdir(tmp_path, monkeypatch):
    from core.workspace_context import workspace_scope
    from core.tools import _resolve_workspace_path, is_outside_workspace

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("内容", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    with workspace_scope(str(workspace)):
        assert is_outside_workspace("note.txt") is False
        assert is_outside_workspace(".") is False
        assert is_outside_workspace("../outside.txt") is True
        assert _resolve_workspace_path("note.txt") == (workspace / "note.txt").resolve()
        assert read_file("note.txt").rstrip("\n") == "内容"


def test_ycode_dir_reads_allowed_under_sandbox_restriction(tmp_path, monkeypatch):
    import core.memory_kairos as mk
    from core.tools import _resolve_workspace_path
    from core.workspace_context import workspace_scope

    workspace = tmp_path / "project"
    workspace.mkdir()
    ycode_dir = tmp_path / ".ycode"
    (ycode_dir / "memory").mkdir(parents=True)
    (ycode_dir / "memory" / "MEMORY.md").write_text("记忆", encoding="utf-8")
    (ycode_dir / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(mk, "YCODE_DIR", ycode_dir)
    monkeypatch.chdir(workspace)

    with workspace_scope(str(workspace)):
        for rel in ("memory/MEMORY.md", "config.json"):
            resolved = _resolve_workspace_path(str(ycode_dir / rel), allow_trusted_read=True)
            assert resolved == (ycode_dir / rel).resolve()

        try:
            _resolve_workspace_path(str(ycode_dir / "config.json"))
            raised = False
        except ValueError:
            raised = True
        assert raised, "沙盒限制下裸写入 .ycode 路径应被边界校验拒绝"


def test_ycode_and_agents_files_are_not_trusted_read_paths(tmp_path, monkeypatch):
    import core.skills as skills_mod

    fake_home = tmp_path / "home"
    skill_dir = fake_home / ".ycode" / "skills" / "example"
    skill_dir.mkdir(parents=True)
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(skill_dir)])
    monkeypatch.setattr(
        skills_mod.os.path,
        "expanduser",
        lambda value: str(fake_home) if value == "~" else value,
    )

    sensitive_paths = [
        fake_home / ".ycode" / "config.json",
        fake_home / ".ycode" / "ycode.db",
        fake_home / ".ycode" / "mcp.json",
        fake_home / ".ycode" / "system_prompt.md",
        fake_home / ".agents" / "settings.json",
    ]

    for sensitive_path in sensitive_paths:
        assert not skills_mod.is_trusted_read_path(str(sensitive_path))


def test_trusted_read_path_requires_a_skill_package_not_just_its_search_root(tmp_path, monkeypatch):
    import core.skills as skills_mod

    search_root = tmp_path / "skills"
    package = search_root / "approved"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text("# Approved", encoding="utf-8")
    resource = package / "references" / "guide.md"
    resource.parent.mkdir()
    resource.write_text("guide", encoding="utf-8")
    unrelated = search_root / "settings.json"
    unrelated.write_text("settings", encoding="utf-8")
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(search_root)])

    assert skills_mod.is_trusted_read_path(str(package / "SKILL.md"))
    assert skills_mod.is_trusted_read_path(str(resource))
    assert not skills_mod.is_trusted_read_path(str(search_root))
    assert not skills_mod.is_trusted_read_path(str(unrelated))


def test_skill_path_expansion_supports_home_environment_and_relative_inputs(tmp_path, monkeypatch):
    import core.skills as skills_mod

    fake_home = tmp_path / "home"
    search_root = fake_home / "skills"
    package = search_root / "demo"
    package.mkdir(parents=True)
    skill_file = package / "SKILL.md"
    skill_file.write_text("# Demo", encoding="utf-8")
    original_expanduser = skills_mod.os.path.expanduser

    def expand_fake_home(value):
        value = str(value)
        if value == "~":
            return str(fake_home)
        if value.startswith("~/") or value.startswith("~\\"):
            return str(fake_home) + value[1:]
        return original_expanduser(value)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(search_root)])
    monkeypatch.setattr(skills_mod.os.path, "expanduser", expand_fake_home)
    monkeypatch.setenv("YCODE_TEST_SKILL_ROOT", str(search_root))
    monkeypatch.setenv("YCODE_TEST_SKILL_HOME", "~/skills")

    assert skills_mod.is_trusted_read_path("~/skills/demo/SKILL.md")
    assert skills_mod.is_trusted_read_path("%YCODE_TEST_SKILL_ROOT%/demo/SKILL.md")
    assert skills_mod.is_trusted_read_path("%YCODE_TEST_SKILL_HOME%/demo/SKILL.md")
    assert skills_mod.is_trusted_read_path("home/skills/demo/SKILL.md")


def test_write_file_rejects_trusted_skill_path_without_outside_approval(tmp_path, monkeypatch):
    import pytest as _pytest
    import core.skills as skills_mod
    import core.tools as tools_mod

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_dir = tmp_path / "skills" / "example"
    skill_dir.mkdir(parents=True)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(skill_dir)])
    monkeypatch.setattr(tools_mod, "_outside_workspace_allowed", lambda: False)

    with _pytest.raises(ValueError, match="路径越界"):
        tools_mod.write_file(str(skill_dir / "SKILL.md"), "# Changed")


def test_skill_markdown_is_trusted_for_read_only(tmp_path, monkeypatch):
    import core.skills as skills_mod

    skill_dir = tmp_path / "skills" / "example"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Example", encoding="utf-8")
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(skill_dir.parent)])

    assert skills_mod.is_trusted_read_path(str(skill_file))
    assert skills_mod.is_trusted_path(str(skill_file))


def test_outside_workspace_allowed_reads_config(monkeypatch):
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


def test_search_news_no_results_returns_error_prefix(monkeypatch):
    import core.tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return []

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    result = tools_mod.search_news("Kimi AI")

    assert result.startswith("🚨"), "无结果必须返回 🚨 前缀以触发子代理失败流程"
    assert "无结果" in result or "未返回" in result
    assert "Kimi AI" in result


def test_search_news_with_results_returns_normal_text(monkeypatch):
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
    fake_result = {"content": result_text}

    assert result_text.startswith(TOOL_ERROR_PREFIX)
    assert _tool_result_ok("search_news", fake_result) is False


def test_subagent_with_search_news_no_results_is_marked_failed(monkeypatch):
    import core.tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=5):
            return []

    monkeypatch.setattr(tools_mod, "DDGS", _FakeDDGS)

    result = tools_mod.search_news("nonexistent topic 12345")
    assert result.startswith("🚨"), \
        "search_news 无结果必须以 🚨 开头，否则子代理会误判为 completed"


def test_ssrf_default_mode_allows_fakeip_domain(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: False)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: [("AF_INET", "STREAM", "PROTO", None, ("198.18.0.92", 0))]
    )
    tools_mod._check_ssrf_url("https://open-meteo.com/api")


def test_ssrf_default_mode_allows_private_ip_literal(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: False)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    tools_mod._check_ssrf_url("http://10.0.0.1/api")
    tools_mod._check_ssrf_url("http://192.168.1.1/")
    tools_mod._check_ssrf_url("http://198.18.0.92/")


def test_ssrf_both_modes_block_cloud_metadata(monkeypatch):
    import core.tools as tools_mod

    for strict_mode in (False, True):
        monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: strict_mode)
        monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
        with pytest.raises(tools_mod.SSRFBlockedError, match="元数据接口"):
            tools_mod._check_ssrf_url("http://169.254.169.254/latest/meta-data/")
        with pytest.raises(tools_mod.SSRFBlockedError, match="元数据接口"):
            tools_mod._check_ssrf_url("http://metadata.google.internal/")


def test_ssrf_allowlist_bypasses_both_modes(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(
        tools_mod, "_get_fetch_url_allow_hosts",
        lambda: {"open-meteo.com", "geocoding-api.open-meteo.com"}
    )
    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(
        tools_mod.socket, "getaddrinfo",
        lambda host, *a, **k: [("AF_INET", "STREAM", "PROTO", None, ("198.18.0.92", 0))]
    )
    tools_mod._check_ssrf_url("https://open-meteo.com/api")
    tools_mod._check_ssrf_url("https://geocoding-api.open-meteo.com/v1/search")


def test_ssrf_strict_mode_blocks_private_ip_literal(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    with pytest.raises(tools_mod.SSRFBlockedError, match="非公网 IP"):
        tools_mod._check_ssrf_url("http://198.18.0.92/")
    with pytest.raises(tools_mod.SSRFBlockedError, match="非公网 IP"):
        tools_mod._check_ssrf_url("http://10.0.0.1/")


def test_ssrf_strict_mode_blocks_dns_resolved_private_ip(monkeypatch):
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
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    tools_mod._check_ssrf_url("http://8.8.8.8/")
    tools_mod._check_ssrf_url("http://1.1.1.1/")


def test_fetch_url_ssrf_error_hint_mentions_fakeip(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    result = tools_mod.fetch_url("http://198.18.0.92/")
    assert "🚨" in result
    assert "fake-ip" in result or "Clash" in result or "V2Ray" in result
    assert "fetch_url_strict_ssrf" in result
    assert "fetch_url_allow_hosts" in result


def test_fetch_url_ssrf_metadata_error_no_fakeip_hint(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    result = tools_mod.fetch_url("http://169.254.169.254/latest/meta-data/")
    assert "🚨" in result
    assert "元数据接口" in result
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
    from core.plan import PlanManager

    plan = PlanManager()
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


def test_run_subagent_rolls_back_plan_item_when_startup_fails(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    scheduler = TaskScheduler(plan)
    plan.replace([{"id": "item-1", "subject": "任务", "files": ["shared.py"]}])

    class FailingManager:
        def run(self, *args, **kwargs):
            raise RuntimeError("subagent task capacity reached")

    monkeypatch.setattr(tools_module, "subagent_manager", FailingManager())
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler",
        lambda: scheduler,
    )

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

    assert scheduler.start_item("item-1") is False
    assert "shared.py" not in scheduler._in_flight_files


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
    import sys
    result = execute_command(f'{sys.executable} -c "import sys; sys.exit(3)"')

    assert "退出码 3" in result
    assert "执行成功" not in result


def test_subagent_result_strips_advisor_tags():
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


def test_subagent_result_strips_parenthesized_advisor_leak():
    manager = SubagentManager(
        runner=lambda record: "(advisor returned no content)\n\n真正的结论。"
    )

    result = manager.run("任务", "s-paren", "t-paren", timeout_seconds=30)

    assert "advisor" not in result.lower()
    assert "真正的结论。" in result


def test_subagent_result_strips_xml_junk_blocks():
    manager = SubagentManager(
        runner=lambda record: (
            "<advisor-request>\n我需要读取所有指定的核心文件。\n</advisor-request>\n"
            "真正的审查结论。\n"
            '<function_calls>\n<invoke name="advisor">\n<parameter name="message">未闭合的伪调用'
        )
    )

    result = manager.run("任务", "s-xml", "t-xml", timeout_seconds=30)

    assert "advisor-request" not in result
    assert "function_calls" not in result
    assert "伪调用" not in result
    assert "真正的审查结论。" in result


def test_subagent_result_keeps_only_text_after_last_tool_call(monkeypatch):
    from types import SimpleNamespace
    import core.llm as llm_module

    final_text = "最终结论：" + "核心模块无阻塞性问题。" * 30

    def fake_events(*args, **kwargs):
        yield SimpleNamespace(kind="assistant_delta", payload={"text": "我先给你一版初步结论，继续补读文件。"})
        yield SimpleNamespace(kind="tool_started", payload={"name": "read_file"})
        yield SimpleNamespace(kind="tool_finished", payload={"name": "read_file", "ok": True})
        yield SimpleNamespace(kind="assistant_delta", payload={"text": final_text})

    monkeypatch.setattr(llm_module, "chat_with_model_events", fake_events)

    manager = SubagentManager()
    result = manager.run("审查任务", "s-last-text", "t-last-text", timeout_seconds=30)

    assert "初步结论" not in result
    assert "最终结论" in result


def test_run_subagent_resolves_per_role_default_timeout(monkeypatch):
    import core.tools as tools_module

    captured = {}

    class _FakeManager:
        def run(self, task_description, parent_session, parent_turn_id, **kwargs):
            captured.update(kwargs)
            return "完成"

    monkeypatch.setattr(tools_module, "subagent_manager", _FakeManager())

    for role, expected in [
        ("explore", 300), ("review", 420), ("test", 900),
        ("implement", 900), ("general", 900), ("custom-agent", 900),
    ]:
        captured.clear()
        tools_module.run_subagent(
            "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
            subagent_type=role,
        )
        assert captured["timeout_seconds"] == expected, role

    captured.clear()
    tools_module.run_subagent(
        "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
        subagent_type="explore", run_in_background=True,
    )
    assert captured["timeout_seconds"] == 450

    captured.clear()
    tools_module.run_subagent(
        "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
        subagent_type="explore", timeout_seconds=600,
    )
    assert captured["timeout_seconds"] == 600
    captured.clear()
    tools_module.run_subagent(
        "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
        subagent_type="explore", timeout_seconds=901,
    )
    assert "timeout_seconds" not in captured


def test_run_subagent_surfaces_real_verification_error(monkeypatch):
    import core.tools as tools_module
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    scheduler = TaskScheduler(plan)
    plan.replace([{
        "id": "item-1",
        "subject": "验证失败任务",
        "stage": "build",
        "verification_type": "command",
        "verification": "exit 1",
    }])
    monkeypatch.setattr(
        tools_module,
        "subagent_manager",
        SubagentManager(runner=lambda record: "子任务做完了"),
    )
    monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler",
        lambda: scheduler,
    )

    result = tools_module.run_subagent(
        "执行子任务",
        parent_session="subagent-verification-error-test",
        parent_turn_id="turn-1",
        subagent_type="implement",
        plan_item_id="item-1",
        timeout_seconds=30,
    )

    assert "失败" in result
    assert "不是 in_progress" not in result
    assert "验证" in result
    assert plan.snapshot()[0]["status"] == "failed"


def test_edit_file_fuzzy_match_minor_trailing_whitespace(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "fuzzy.txt"
    target.write_text("def foo():\n    pass\n", encoding="utf-8")

    result = edit_file("fuzzy.txt", "def foo():\n    pass", "def bar():\n    return 1")
    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "def bar():\n    return 1\n"


def test_edit_file_fuzzy_match_minor_pollution(tmp_path, monkeypatch):
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
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "large.txt"
    target.write_text("\n".join(f"line {i}" for i in range(600)), encoding="utf-8")

    result = write_file("large.txt", "new content")
    assert isinstance(result, str)
    assert result.startswith("\U0001f6a8")
    assert "large.txt" in result
    assert "new content" not in target.read_text(encoding="utf-8")


def test_write_file_allows_overwrite_small_existing_file(tmp_path, monkeypatch):
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "small.txt"
    target.write_text("old", encoding="utf-8")

    result = write_file("small.txt", "new")
    assert isinstance(result, str)
    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "new"


def test_write_file_auto_creates_missing_parent_directory(tmp_path, monkeypatch):
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "memory" / "feedback" / "note.md"
    assert not target.parent.exists()

    result = write_file(str(target), "test content")

    assert isinstance(result, str)
    assert result.startswith("\u2714"), f"应写入成功，实际：{result}"
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "test content"
    assert target.parent.exists()


def test_write_file_auto_creates_nested_parent_directories(tmp_path, monkeypatch):
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "a" / "b" / "c" / "d" / "file.txt"
    assert not target.parent.exists()

    result = write_file(str(target), "deep")

    assert result.startswith("\u2714")
    assert target.read_text(encoding="utf-8") == "deep"


def test_edit_file_auto_creates_missing_parent_directory(tmp_path, monkeypatch):
    from core.tools import write_file, edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sub" / "note.md"
    write_file(str(target), "old content")

    result = edit_file(str(target), "old content", "new content")
    assert "✔" in str(result) or "成功" in str(result), f"edit_file 应成功，实际：{result}"
    assert target.read_text(encoding="utf-8") == "new content"


def test_read_file_with_line_numbers_returns_prefixed_format(tmp_path, monkeypatch):
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = read_file("sample.py", with_line_numbers=True)

    assert result == "1: alpha\n2: beta\n3: gamma"


def test_read_file_with_line_numbers_respects_offset_and_limit(tmp_path, monkeypatch):
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "paged.txt"
    target.write_text("L1\nL2\nL3\n", encoding="utf-8")

    result = read_file("paged.txt", offset=1, limit=2, with_line_numbers=True)

    assert result == "2: L2\n3: L3"


def test_read_file_with_line_numbers_ignores_compress(tmp_path, monkeypatch):
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "spaced.txt"
    target.write_text("a\n\n\nb\n", encoding="utf-8")

    result = read_file("spaced.txt", compress=True, with_line_numbers=True)

    assert result == "1: a\n2: \n3: \n4: b"


def test_read_file_with_line_numbers_caches_separately_from_plain(tmp_path, monkeypatch):
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "cache.txt"
    target.write_text("hello\nworld\n", encoding="utf-8")

    plain = read_file("cache.txt")
    numbered = read_file("cache.txt", with_line_numbers=True)

    assert plain == "hello\nworld\n"
    assert numbered == "1: hello\n2: world"

    plain2 = read_file("cache.txt")
    numbered2 = read_file("cache.txt", with_line_numbers=True)
    assert plain2 == plain
    assert numbered2 == numbered


def test_edit_file_by_lines_basic_replacement(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")

    result = edit_file("code.py", replacement_content="    return 42", start_line=2, end_line=2)

    assert result.startswith("✔")
    assert "第 2-2 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 42\n\ndef bar():\n    return 2\n"


def test_edit_file_by_lines_multi_line_range(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "multi.py"
    original = "a\nb\nc\nd\ne\n"
    target.write_text(original, encoding="utf-8")

    result = edit_file("multi.py", replacement_content="X\nY", start_line=2, end_line=4)

    assert result.startswith("✔")
    assert "第 2-4 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\ne\n"


def test_edit_file_by_lines_end_line_exceeds_total_clamps_silently(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "short.txt"
    target.write_text("only\none\nline\n", encoding="utf-8")

    result = edit_file("short.txt", replacement_content="replaced", start_line=2, end_line=100)

    assert result.startswith("✔")
    assert "第 2-3 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "only\nreplaced\n"


def test_edit_file_by_lines_includes_replaced_preview_in_response(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "preview.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = edit_file("preview.py", replacement_content="NEW", start_line=2, end_line=2)

    assert "被替换原文预览" in result
    assert "line2" in result


def test_edit_file_by_lines_preview_truncates_long_blocks(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "long_block.txt"
    content = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"
    target.write_text(content, encoding="utf-8")

    result = edit_file("long_block.txt", replacement_content="REPLACED", start_line=1, end_line=10)

    assert result.startswith("✔")
    assert "省略中间部分" in result
    assert "line1" in result
    assert "line3" in result
    assert "line10" in result
    assert "line5" not in result


def test_edit_file_by_lines_rejects_start_line_below_one(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("content\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=0, end_line=1)

    assert result.startswith("🚨")
    assert "start_line 必须 >= 1" in result
    assert target.read_text(encoding="utf-8") == "content\n"


def test_edit_file_by_lines_rejects_end_below_start(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=3, end_line=1)

    assert result.startswith("🚨")
    assert "不能小于" in result


def test_edit_file_by_lines_rejects_start_exceeds_total(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("only\none\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=100, end_line=200)

    assert result.startswith("🚨")
    assert "超出文件总行数" in result
    assert "read_file(with_line_numbers=True)" in result


def test_edit_file_by_lines_rejects_partial_line_args(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result1 = edit_file("x.txt", replacement_content="new", start_line=1)
    assert result1.startswith("🚨")
    assert "同时提供" in result1

    result2 = edit_file("x.txt", replacement_content="new", end_line=1)
    assert result2.startswith("🚨")
    assert "同时提供" in result2


def test_edit_file_by_lines_rejects_bool_arguments(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=True, end_line=True)

    assert result.startswith("🚨")
    assert "布尔值" in result


def test_edit_file_by_lines_rejects_non_int_arguments(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=1.5, end_line=2)

    assert result.startswith("🚨")
    assert "整数" in result


def test_edit_file_by_lines_rejects_empty_file(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = edit_file("empty.txt", replacement_content="new", start_line=1, end_line=1)

    assert result.startswith("🚨")
    assert "为空" in result


def test_edit_file_by_lines_no_change_returns_error(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "same.txt"
    target.write_text("identical\n", encoding="utf-8")

    result = edit_file("same.txt", replacement_content="identical", start_line=1, end_line=1)

    assert result.startswith("🚨")
    assert "相同" in result


def test_edit_file_by_lines_writes_undo_stack(tmp_path, monkeypatch):
    import hashlib

    from core.memory import MemoryManager
    from core.tools import edit_file
    from core.turn_undo import service as turn_undo_service
    from core.turn_undo.models import OwnerKind

    monkeypatch.chdir(tmp_path)
    manager = MemoryManager(str(tmp_path / "ycode.db"))
    storage = turn_undo_service.configure_default_manager(
        manager, blob_root=tmp_path / "undo_blobs"
    )
    manager.add_messages("undo-sess", [("user", "hi")])
    session_id = storage.resolve_session_id("undo-sess")
    checkpoint, ctx = turn_undo_service.begin_turn(
        session_id=session_id,
        turn_id="undo-turn-lines",
        workspace_root=str(tmp_path),
        owner_kind=OwnerKind.FOREGROUND_TURN,
    )

    target = tmp_path / "undo.py"
    target.write_bytes(b"original\n")

    try:
        result = edit_file(
            "undo.py", replacement_content="modified", start_line=1, end_line=1
        )
    finally:
        turn_undo_service.finish_turn(ctx)

    assert result.startswith("✔")
    files = storage.list_files(checkpoint.checkpoint_id)
    assert len(files) == 1
    row = files[0]
    assert row.before_sha256 == hashlib.sha256(b"original\n").hexdigest()
    assert row.after_sha256 == hashlib.sha256(b"modified\n").hexdigest()
    assert row.verified_at_ms is not None


def test_edit_file_by_lines_failed_validation_does_not_pollute_undo_stack(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "safe.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result = edit_file("safe.txt", replacement_content="new", start_line=100, end_line=200)

    assert result.startswith("🚨")
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_edit_file_by_lines_preserves_gbk_encoding(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "gbk_lines.txt"
    original = "第一行\r\n第二行\r\n第三行\r\n"
    target.write_bytes(original.encode("gbk"))

    result = edit_file("gbk_lines.txt", replacement_content="替换行", start_line=2, end_line=2)

    assert result.startswith("✔")
    assert target.read_bytes() == "第一行\r\n替换行\r\n第三行\r\n".encode("gbk")


def test_edit_file_by_lines_adapts_crlf_replacement(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "crlf_lines.txt"
    target.write_bytes(b"alpha\r\nbeta\r\nomega\r\n")

    result = edit_file("crlf_lines.txt", replacement_content="line1\nline2", start_line=2, end_line=2)

    assert result.startswith("✔")
    raw = target.read_bytes()
    assert raw == b"alpha\r\nline1\r\nline2\r\nomega\r\n"
    assert b"\r\r\n" not in raw


def test_edit_file_by_lines_ignores_target_content(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "ignore_target.py"
    target.write_text("actual_line\n", encoding="utf-8")

    result = edit_file(
        "ignore_target.py",
        target_content="this is wrong and should be ignored",
        replacement_content="new_line",
        start_line=1,
        end_line=1,
    )

    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8") == "new_line\n"


def test_edit_file_string_path_still_works_when_no_line_args(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "compat.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    result = edit_file("compat.py", target_content="return 1", replacement_content="return 42")

    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 42\n"


def test_edit_file_only_replacement_triggers_smart_replace(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hint.py"
    target.write_text("def old_func():\n    return 'old'\n", encoding="utf-8")

    replacement = "def new_func():\n    return 'new'\n"

    result = edit_file("hint.py", replacement_content=replacement)

    assert result.startswith("✔")
    assert "智能修改" in result
    new_content = target.read_text(encoding="utf-8")
    assert "new_func" in new_content
    assert "old_func" not in new_content


def test_edit_file_only_replacement_rejects_when_no_similarity(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hint.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    replacement = "completely unrelated content with no similarity at all here"

    result = edit_file("hint.py", replacement_content=replacement)

    assert result.startswith("🚨")
    assert "start_line" in result or "target_content" in result


def test_edit_file_by_lines_handles_duplicate_code_blocks_without_ambiguity(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "dup_blocks.py"
    # 多个完全相同的 case 块，字符串路径会因 target_content 多次出现而失败
    content = (
        "def handle(x):\n"
        "    case_a()\n"
        "    case_a()\n"
        "    case_a()\n"
        "    return\n"
    )
    target.write_text(content, encoding="utf-8")

    # 字符串路径：target_content="    case_a()" 出现 3 次，应失败
    string_result = edit_file(
        "dup_blocks.py",
        target_content="    case_a()",
        replacement_content="    case_b()",
    )
    assert string_result.startswith("🚨")
    assert "3 次" in string_result

    # 行号路径：直接定位第 3 行（第二个 case_a），无歧义
    line_result = edit_file(
        "dup_blocks.py",
        replacement_content="    case_b()",
        start_line=3,
        end_line=3,
    )
    assert line_result.startswith("✔")
    new_content = target.read_text(encoding="utf-8")
    # 第 2 行和第 4 行的 case_a 未变，只有第 3 行被替换
    assert new_content == "def handle(x):\n    case_a()\n    case_b()\n    case_a()\n    return\n"


def test_read_file_with_line_numbers_then_edit_file_by_lines_end_to_end(tmp_path, monkeypatch):
    from core.tools import read_file, edit_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "e2e.py"
    target.write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "def main():\n"
        "    print('hello')\n"
        "    return 0\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )

    view = read_file("e2e.py", with_line_numbers=True)
    assert "5:     print('hello')" in view

    result = edit_file(
        "e2e.py",
        replacement_content="    print('world')",
        start_line=5,
        end_line=5,
    )
    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8").count("print('world')") == 1
    assert "print('hello')" not in target.read_text(encoding="utf-8")


def test_edit_file_by_lines_replacement_appends_newline_when_needed(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "tail.txt"
    target.write_bytes(b"a\nb\nc\n")

    result = edit_file("tail.txt", replacement_content="C", start_line=3, end_line=3)

    assert result.startswith("✔")
    assert target.read_bytes() == b"a\nb\nC\n"


def test_edit_file_by_lines_empty_replacement_deletes_lines(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "delete.txt"
    target.write_bytes(b"a\nb\nc\nd\n")

    result = edit_file("delete.txt", replacement_content="", start_line=2, end_line=3)

    assert result.startswith("✔")
    assert target.read_bytes() == b"a\nd\n"


def test_edit_file_by_lines_insert_at_start(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "head.py"
    target.write_text("old_first\nsecond\n", encoding="utf-8")

    result = edit_file(
        "head.py",
        replacement_content="new_first\nnew_second",
        start_line=1,
        end_line=1,
    )

    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8") == "new_first\nnew_second\nsecond\n"


def test_execute_command_no_output_nonzero_exit_is_informational(monkeypatch):
    class FakeProcess:
        returncode = 1

        def poll(self):
            return 1

    monkeypatch.setattr(
        "core.tools.subprocess.Popen", lambda *args, **kwargs: FakeProcess()
    )

    result = execute_command('findstr /i "nope" somefile.txt')

    assert not result.startswith("🚨")
    assert "退出码 1" in result
    assert "findstr" in result


def test_execute_command_nonzero_exit_with_output_still_error(monkeypatch):
    class FakeProcess:
        returncode = 2

        def poll(self):
            return 2

    def fake_popen(*args, **kwargs):
        kwargs["stdout"].write("爆炸了".encode("utf-8"))
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)

    result = execute_command("boom-command")

    assert result.startswith("🚨")
    assert "退出码 2" in result
    assert "爆炸了" in result


def test_list_directory_on_file_returns_soft_hint(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "SflF400_mk.v"
    target.write_text("x", encoding="utf-8")

    result = list_directory(str(target))

    assert not result.startswith("🚨")
    assert "read_file" in result


def test_ssrf_strict_mode_is_default_when_config_missing(monkeypatch):
    import core.tools as tools_mod
    import config as config_mod

    monkeypatch.setattr(config_mod, "_read_json_with_backup", lambda *a, **k: {})
    assert tools_mod._get_fetch_url_strict_ssrf() is True
    monkeypatch.setattr(config_mod, "_read_json_with_backup", lambda *a, **k: {"fetch_url_strict_ssrf": False})
    assert tools_mod._get_fetch_url_strict_ssrf() is False


def test_ssrf_default_strict_blocks_loopback_and_private(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())
    for url in ("http://127.0.0.1:8080/admin", "http://localhost/", "http://10.0.0.1/"):
        try:
            tools_mod._check_ssrf_url(url)
        except tools_mod.SSRFBlockedError:
            continue
        raise AssertionError(f"应被 SSRF 拦截: {url}")


def test_redirect_revalidates_ssrf_per_hop(monkeypatch):
    import core.tools as tools_mod
    from urllib.request import Request

    monkeypatch.setattr(tools_mod, "_get_fetch_url_strict_ssrf", lambda: True)
    monkeypatch.setattr(tools_mod, "_get_fetch_url_allow_hosts", lambda: set())

    handler = tools_mod._SSRFRecheckRedirectHandler()
    req = Request("https://example.com/start")
    with pytest.raises(tools_mod.SSRFBlockedError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data")
    with pytest.raises(tools_mod.SSRFBlockedError):
        handler.redirect_request(req, None, 302, "Found", {}, "http://127.0.0.1/internal")


def test_redirect_to_safe_url_passes(monkeypatch):
    import core.tools as tools_mod
    from urllib.request import Request

    monkeypatch.setattr(tools_mod, "_check_ssrf_url", lambda url: None)
    handler = tools_mod._SSRFRecheckRedirectHandler()
    req = Request("https://example.com/start")
    new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/next")
    assert new_req is not None
    assert new_req.full_url == "https://example.com/next"


def test_execute_command_output_cap_terminates_verbose_command():
    import sys
    from core.tools import EXECUTE_COMMAND_MAX_OUTPUT_BYTES, execute_command

    overflow = EXECUTE_COMMAND_MAX_OUTPUT_BYTES + 512 * 1024
    result = execute_command(
        f'{sys.executable} -c "import sys; sys.stdout.write(\'x\' * {overflow})"',
        timeout_seconds=60,
    )
    assert "截断" in result or "上限" in result
    assert len(result) < overflow


def test_read_file_missing_file_returns_friendly_error(tmp_path, monkeypatch):
    from core.tools import read_file

    monkeypatch.chdir(tmp_path)
    result = read_file("definitely_missing.txt")
    assert result.startswith("🚨")
    assert "不存在" in result


def test_edit_file_detects_external_modification_between_read_and_write(tmp_path, monkeypatch):
    import core.tools as tools_mod
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "race.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

    original_fuzzy = tools_mod._fuzzy_replace

    def racing_fuzzy(content, match_target, match_replacement, replace_all=False):
        target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
        import os as _os
        st = target.stat()
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        return original_fuzzy(content, match_target, match_replacement, replace_all=replace_all)

    monkeypatch.setattr(tools_mod, "_fuzzy_replace", racing_fuzzy)

    result = edit_file("race.py", "beta = 2", "beta = 20")

    assert result.startswith("🚨")
    assert "外部修改" in result
    assert target.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\ngamma = 3\n"


def test_write_file_detects_external_modification_before_replace(tmp_path, monkeypatch):
    import core.tools as tools_mod
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)

    original_mkstemp = tools_mod.tempfile.mkstemp

    def racing_mkstemp(*args, **kwargs):
        (tmp_path / "new.txt").write_text("external", encoding="utf-8")
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tools_mod.tempfile, "mkstemp", racing_mkstemp)

    result = write_file("new.txt", "mine")

    assert result.startswith("🚨")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "external"


def test_smart_replace_all_replaces_every_high_score_block(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "multi.py"
    target.write_text(
        "def old_func():\n    return 'old'\n\n"
        "def old_func():\n    return 'old'\n",
        encoding="utf-8",
    )

    result = edit_file(
        "multi.py",
        replacement_content="def new_func():\n    return 'new'\n",
        replace_all=True,
    )

    assert result.startswith("✔")
    assert "2 处" in result
    content = target.read_text(encoding="utf-8")
    assert content.count("new_func") == 2
    assert "old_func" not in content


def test_smart_replace_rejects_low_similarity_candidate(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "loose.py"
    target.write_text("alpha_keep\nbeta\nbeta\ngamma\n", encoding="utf-8")

    result = edit_file(
        "loose.py",
        replacement_content="alpha_keep\ncompletely different\nnothing alike\n",
    )

    assert result.startswith("🚨")
    assert "0.7" in result
    assert "target_content" in result
    assert target.read_text(encoding="utf-8") == "alpha_keep\nbeta\nbeta\ngamma\n"


def test_edit_file_write_failure_does_not_pollute_undo_stack(tmp_path, monkeypatch):
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "undo2.py"
    target.write_text("alpha\n", encoding="utf-8")

    def failing_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.tools.os.replace", failing_replace)

    result = edit_file("undo2.py", "alpha", "beta")

    assert result.startswith("🚨")
    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_search_code_respects_cancel_event(tmp_path, monkeypatch):
    import core.tools as tools_mod

    monkeypatch.chdir(tmp_path)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.search_code("anything", cancel_event=cancel)

    assert result.startswith("🛑")


def test_glob_files_respects_cancel_event(tmp_path, monkeypatch):
    import core.tools as tools_mod

    monkeypatch.chdir(tmp_path)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.glob_files("*.py", cancel_event=cancel)

    assert result.startswith("🛑")


def test_fetch_url_respects_cancel_event(monkeypatch):
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_check_ssrf_url", lambda url: None)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.fetch_url("https://example.com/", cancel_event=cancel)

    assert result.startswith("🛑")


def test_search_news_clamps_num_results(monkeypatch):
    import core.tools as tools_mod

    captured = {}

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def news(self, query, max_results=None):
            captured["max_results"] = max_results
            return [{"title": "t", "url": "u", "body": "b"}]

    monkeypatch.setattr(tools_mod, "DDGS", lambda: FakeDDGS())

    result = tools_mod.search_news("query", num_results=100000)

    assert not result.startswith("🚨")
    assert captured["max_results"] == 20


def test_glob_files_uses_search_timeout_constant(tmp_path, monkeypatch):
    import subprocess as _subprocess
    import core.tools as tools_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tools_mod, "_get_rg_executable", lambda: "rg")
    captured = {}

    def fake_polled(cmd, timeout, cancel_event=None):
        captured["timeout"] = timeout
        return _subprocess.CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr(tools_mod, "_run_process_polled", fake_polled)

    tools_mod.glob_files("*.py")

    assert captured["timeout"] == tools_mod.SEARCH_CODE_TIMEOUT_SECONDS


def test_subagent_approval_wait_over_limit_is_terminated():
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(1),
        monotonic=FakeMonotonic([0.0, 400.0]),
        wait_interval=0.001,
        permission_waiting=lambda owner_id: True,
    )

    result = manager.run("等待审批", "s1", "t1", timeout_seconds=600)

    assert "超时" in result
    assert "审批等待" in result
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "timed_out"


def test_background_subagent_approval_wait_over_limit_is_terminated():
    import core.tools as tools_mod

    release = threading.Event()
    manager = SubagentManager(
        runner=lambda record: record.cancel_event.wait(2) and "完成",
        monotonic=FakeMonotonic([0.0, 400.0, 401.0]),
        wait_interval=0.001,
        permission_waiting=lambda owner_id: True,
    )
    manager.set_on_terminal(lambda *args: release.set())

    manager.run("后台等待审批", "s1", "t1", run_in_background=True, timeout_seconds=600)

    assert release.wait(5)
    snapshot = manager.snapshot()[0]
    assert snapshot.status == "timed_out"
    assert "审批等待" in snapshot.error


def test_execute_command_background_echo_redacts_inline_credentials(monkeypatch):
    monkeypatch.setattr(
        "core.task_manager.start_background_task",
        lambda command, session_id="", turn_id="": "abc12345",
    )

    result = execute_command(
        "export TOKEN=sk-abc123def456 && npm run dev",
        run_in_background=True,
    )

    assert "sk-abc123def456" not in result
    assert "***" in result


def test_manage_task_list_redacts_inline_credentials(monkeypatch):
    import core.tools as tools_mod
    from core.task_manager import TaskStatusSnapshot

    monkeypatch.setattr(
        "core.task_manager.list_task_snapshots",
        lambda: [
            TaskStatusSnapshot(
                task_id="abc12345",
                status="running",
                revision=0,
                terminal=False,
                output="",
                exit_code=None,
            )
        ],
    )

    result = tools_mod.manage_task("list")
    payload = json.loads(result)

    assert set(payload) == {"tasks"}
    assert payload["tasks"][0]["task_id"] == "abc12345"
    assert "command" not in payload["tasks"][0]
    assert "hunter2" not in result


@pytest.fixture
def isolated_task_manager(tmp_path, monkeypatch):
    import core.task_manager as tm

    monkeypatch.setattr(tm, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(tm, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(tm, "_LOADED", True)
    monkeypatch.setattr(tm, "ACTIVE_TASKS", {})
    monkeypatch.setattr(tm, "_REMOVED_TIDS", set())
    monkeypatch.setattr(tm, "_REMOVED_TIDS_FIFO", deque(), raising=False)
    yield tm
    # 清理：杀掉测试遗留的后台进程，避免泄漏真实子进程
    for tid in list(tm.ACTIVE_TASKS):
        try:
            tm.kill_background_task(tid)
        except Exception:
            pass


def _py_cmd(script):
    import sys as _sys

    return f'"{_sys.executable}" -c "{script}"'


def test_background_task_revision_bumps_on_new_output(isolated_task_manager):
    tm = isolated_task_manager
    tid = tm.start_background_task(
        _py_cmd("import time; time.sleep(1.5); print('marker-hello', flush=True); time.sleep(30)")
    )
    try:
        snap0 = tm.get_task_snapshot(tid)
        assert snap0 is not None
        assert snap0.revision == 0
        assert snap0.status == "running"
        assert snap0.terminal is False

        def _advanced():
            snap = tm.get_task_snapshot(tid)
            return snap.revision > 0 and "marker-hello" in snap.output

        assert _wait_until(_advanced, timeout=10.0), "日志出现新输出后 revision 必须单调增加"
    finally:
        tm.kill_background_task(tid)


def test_background_task_terminal_transition_bumps_revision_and_exit_code(isolated_task_manager):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import sys; sys.exit(3)"))

    assert _wait_until(lambda: tm.get_task_snapshot(tid).terminal, timeout=10.0)
    snap = tm.get_task_snapshot(tid)
    assert snap.status == "failed"
    assert snap.exit_code == 3
    assert snap.revision >= 1
    assert tid in {s.task_id for s in tm.list_task_snapshots()}
    assert tid not in {t["task_id"] for t in tm.list_active_tasks()}


def test_background_task_success_exit_marks_completed(isolated_task_manager):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("pass"))

    assert _wait_until(lambda: tm.get_task_snapshot(tid).terminal, timeout=10.0)
    snap = tm.get_task_snapshot(tid)
    assert snap.status == "completed"
    assert snap.exit_code == 0


def test_wait_returns_immediately_when_revision_advanced(isolated_task_manager):
    import time
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(
        _py_cmd("import time; time.sleep(1.0); print('marker-adv', flush=True); time.sleep(30)")
    )
    try:
        rev0 = tm.get_task_snapshot(tid).revision
        assert _wait_until(
            lambda: tm.get_task_snapshot(tid).revision > rev0, timeout=10.0
        )

        started = time.monotonic()
        result = manage_task("wait", task_id=tid, after_revision=rev0, timeout_seconds=10)
        elapsed = time.monotonic() - started

        payload = json.loads(result)
        assert payload["changed"] is True
        assert payload["revision"] > rev0
        assert payload["task_id"] == tid
        assert elapsed < 2.0, "revision 已推进时 wait 必须立即返回"
    finally:
        tm.kill_background_task(tid)


def test_wait_timeout_returns_changed_false_without_error(isolated_task_manager):
    import time
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        rev0 = tm.get_task_snapshot(tid).revision

        started = time.monotonic()
        result = manage_task("wait", task_id=tid, after_revision=rev0, timeout_seconds=1)
        elapsed = time.monotonic() - started

        assert not result.startswith("🚨"), "超时不是错误，不得返回 🚨 前缀"
        payload = json.loads(result)
        assert payload["changed"] is False
        assert payload["revision"] == rev0
        assert payload["status"] == "running"
        assert 0.8 <= elapsed < 5.0
    finally:
        tm.kill_background_task(tid)


def test_wait_returns_terminal_snapshot_immediately(isolated_task_manager):
    import time

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("pass"))
    assert _wait_until(lambda: tm.get_task_snapshot(tid).terminal, timeout=10.0)
    rev = tm.get_task_snapshot(tid).revision

    started = time.monotonic()
    snap = tm.wait_task_change(tid, rev, 10)
    elapsed = time.monotonic() - started

    assert snap is not None
    assert snap.terminal is True
    assert snap.exit_code == 0
    assert elapsed < 2.0


def test_wait_cancel_event_interrupts_immediately(isolated_task_manager):
    import time

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        cancel_event = threading.Event()
        cancel_event.set()
        with pytest.raises(KeyboardInterrupt):
            tm.wait_task_change(tid, 0, 30, cancel_event=cancel_event)

        cancel_event2 = threading.Event()
        timer = threading.Timer(0.3, cancel_event2.set)
        timer.start()
        started = time.monotonic()
        try:
            with pytest.raises(KeyboardInterrupt):
                tm.wait_task_change(tid, 0, 30, cancel_event=cancel_event2)
        finally:
            timer.cancel()
        assert time.monotonic() - started < 5.0
    finally:
        tm.kill_background_task(tid)


def test_background_command_attributes_launching_thread_session(isolated_task_manager):
    import re as _re

    import core.app_context as app_context
    from core import tool_cache

    tm = isolated_task_manager
    original_current = app_context.get_current_session()
    app_context.set_current_session("分析一下当前目录代码中有")
    app_context.set_thread_session("请在项目E盘执行修复计划")
    tool_cache.set_current_session(None)
    try:
        result = execute_command(
            _py_cmd("import time; time.sleep(30)"), run_in_background=True
        )
        m = _re.search(r"任务 ID: ([0-9a-f]{8})", result)
        assert m, result
        assert tm.ACTIVE_TASKS[m.group(1)]["session_id"] == "请在项目E盘执行修复计划"
    finally:
        for tid in list(tm.ACTIVE_TASKS):
            try:
                tm.kill_background_task(tid)
            except Exception:
                pass
        app_context.set_thread_session(None)
        app_context.set_current_session(original_current)


def test_background_command_falls_back_to_visible_session(isolated_task_manager):
    import re as _re

    import core.app_context as app_context
    from core import tool_cache

    tm = isolated_task_manager
    original_current = app_context.get_current_session()
    app_context.set_current_session("cli-session")
    app_context.set_thread_session(None)
    tool_cache.set_current_session(None)
    try:
        result = execute_command(
            _py_cmd("import time; time.sleep(30)"), run_in_background=True
        )
        m = _re.search(r"任务 ID: ([0-9a-f]{8})", result)
        assert m, result
        assert tm.ACTIVE_TASKS[m.group(1)]["session_id"] == "cli-session"
    finally:
        for tid in list(tm.ACTIVE_TASKS):
            try:
                tm.kill_background_task(tid)
            except Exception:
                pass
        app_context.set_thread_session(None)
        app_context.set_current_session(original_current)


def test_manage_task_status_and_list_return_stable_json(isolated_task_manager):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        status_payload = json.loads(manage_task("status", task_id=tid))
        assert status_payload["task_id"] == tid
        assert status_payload["status"] == "running"
        assert status_payload["revision"] == 0
        assert status_payload["terminal"] is False
        assert status_payload["exit_code"] is None
        assert "output" in status_payload
        assert "changed" not in status_payload

        list_payload = json.loads(manage_task("list"))
        entry = next(t for t in list_payload["tasks"] if t["task_id"] == tid)
        assert entry["status"] == "running"
        assert entry["terminal"] is False
    finally:
        tm.kill_background_task(tid)

    assert manage_task("list") == "ℹ 当前没有任何运行中的后台异步任务。"


def test_manage_task_wait_validates_arguments(isolated_task_manager):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        assert manage_task("wait", after_revision=0).startswith("🚨")
        result = manage_task("wait", task_id=tid, timeout_seconds=1)
        assert not result.startswith("🚨")
        assert json.loads(result)["changed"] is False
        assert manage_task("wait", task_id=tid, after_revision=-1).startswith("🚨")
        assert manage_task("wait", task_id=tid, after_revision="abc").startswith("🚨")
        assert manage_task("wait", task_id="不存在id", after_revision=0).startswith("🚨")
        assert manage_task("status", task_id="不存在id").startswith("🚨")
    finally:
        tm.kill_background_task(tid)


def test_manage_task_wait_shell_defaults_after_revision_to_zero(isolated_task_manager):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(
        _py_cmd("import time; time.sleep(1.0); print('marker-default-rev', flush=True); time.sleep(30)")
    )
    try:
        result = manage_task("wait", task_id=tid, timeout_seconds=10)
        payload = json.loads(result)
        assert payload["changed"] is True
        assert payload["revision"] > 0
        assert "marker-default-rev" in payload["output"]
    finally:
        tm.kill_background_task(tid)


def _bridge_manager(monkeypatch, runner):
    import core.tools as tools_module

    manager = SubagentManager(runner=runner, wait_interval=0.02)
    monkeypatch.setattr(tools_module, "subagent_manager", manager)
    return manager


def test_manage_task_status_and_wait_route_subagent(monkeypatch):
    import core.tools as tools_module
    from core.tools import manage_task

    release = threading.Event()

    def runner(_record):
        release.wait(timeout=5)
        return "审查结论：改动可以提交"

    manager = _bridge_manager(monkeypatch, runner)
    start_result = manager.run(
        "审查未提交改动", "bridge-session", "bridge-turn",
        run_in_background=True, timeout_seconds=30,
    )
    task_id = json.loads(start_result)["task_id"]
    try:
        assert _wait_until(
            lambda: json.loads(
                manage_task("status", task_id=task_id)
            )["status"] == "running"
        )
        status_payload = json.loads(manage_task("status", task_id=task_id))
        assert status_payload["kind"] == "subagent"
        assert status_payload["task_id"] == task_id
        assert status_payload["status"] == "running"
        assert status_payload["terminal"] is False
        assert status_payload["subagent_type"] == "general"
        assert "changed" not in status_payload

        release.set()
        wait_payload = json.loads(manage_task("wait", task_id=task_id, timeout_seconds=10))
        assert wait_payload["kind"] == "subagent"
        assert wait_payload["terminal"] is True
        assert wait_payload["status"] == "completed"
        assert wait_payload["changed"] is True
        assert "审查结论：改动可以提交" in wait_payload["result"]
    finally:
        release.set()
        manager.shutdown(timeout=5)


def test_manage_task_wait_subagent_ignores_after_revision_and_unknown(monkeypatch):
    from core.tools import manage_task

    manager = _bridge_manager(monkeypatch, lambda _record: "完成")
    assert manage_task("wait", task_id="subagent-00000000", timeout_seconds=1).startswith("🚨")
    assert manage_task(
        "wait", task_id="subagent-00000000", after_revision=0, timeout_seconds=1
    ).startswith("🚨")
    assert manage_task("status", task_id="subagent-00000000").startswith("🚨")


def test_manage_task_list_merges_shell_and_subagent(isolated_task_manager, monkeypatch):
    from core.tools import manage_task

    tm = isolated_task_manager
    shell_tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    release = threading.Event()

    def runner(_record):
        release.wait(timeout=5)
        return "完成"

    manager = _bridge_manager(monkeypatch, runner)
    sub_tid = json.loads(manager.run(
        "后台审查", "merge-session", "merge-turn",
        run_in_background=True, timeout_seconds=30,
    ))["task_id"]
    try:
        payload = json.loads(manage_task("list"))
        kinds = {t["task_id"]: t["kind"] for t in payload["tasks"]}
        assert kinds[shell_tid] == "shell"
        assert kinds[sub_tid] == "subagent"
        sub_entry = next(t for t in payload["tasks"] if t["task_id"] == sub_tid)
        assert sub_entry["terminal"] is False
        assert "result" not in sub_entry
    finally:
        release.set()
        manager.shutdown(timeout=5)
        tm.kill_background_task(shell_tid)


def test_manage_task_kill_subagent_uses_cooperative_cancel(monkeypatch):
    import core.tools as tools_module
    from core.tools import manage_task

    release = threading.Event()

    def runner(_record):
        release.wait(timeout=5)
        return "完成"

    manager = _bridge_manager(monkeypatch, runner)
    task_id = json.loads(manager.run(
        "待取消任务", "kill-session", "kill-turn",
        run_in_background=True, timeout_seconds=30,
    ))["task_id"]
    try:
        monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *a, **k: False)
        assert manage_task("kill", task_id=task_id).startswith("🚨 用户拒绝")
        assert manager.task_snapshot(task_id).status == "running"

        monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *a, **k: True)
        result = manage_task("kill", task_id=task_id)
        assert result.startswith("✔"), result
        assert _wait_until(
            lambda: manager.task_snapshot(task_id).status == "cancelled"
        )
    finally:
        release.set()
        manager.shutdown(timeout=5)


@pytest.mark.parametrize("value", [0, 61, "abc", True, 1.5])
def test_manage_task_wait_rejects_invalid_timeout(isolated_task_manager, value):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        result = manage_task(
            "wait", task_id=tid, after_revision=0, timeout_seconds=value
        )
        assert result.startswith("🚨")
    finally:
        tm.kill_background_task(tid)


@pytest.mark.parametrize("value", [-1, "abc", True, 1.5])
def test_manage_task_wait_rejects_invalid_after_revision(
    isolated_task_manager, value
):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        result = manage_task(
            "wait", task_id=tid, after_revision=value, timeout_seconds=1
        )
        assert result.startswith("🚨")
    finally:
        tm.kill_background_task(tid)


def test_background_tasks_fingerprint_tracks_task_state(isolated_task_manager):
    tm = isolated_task_manager
    fp_empty = tm.background_tasks_fingerprint()
    assert len(fp_empty) == 64
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        fp_running = tm.background_tasks_fingerprint()
        assert fp_running != fp_empty
        assert tm.background_tasks_fingerprint() == fp_running
    finally:
        tm.kill_background_task(tid)
    assert tm.background_tasks_fingerprint() != fp_running


def test_background_fingerprint_syncs_restored_dead_task(isolated_task_manager):
    tm = isolated_task_manager
    tm.ACTIVE_TASKS["restored1"] = {
        "id": "restored1",
        "command": "restored",
        "pid": 99999999,
        "process": None,
        "start_time": time.time(),
        "create_time": None,
        "status": tm.TASK_STATUS_RUNNING,
        "exit_code": None,
        "revision": 0,
        "last_log_size": 0,
    }
    before = tm.background_tasks_fingerprint()
    snap = tm.get_task_snapshot("restored1")
    after = tm.background_tasks_fingerprint()

    assert snap.terminal is True
    assert before == after


def test_kill_marks_task_cancelled_and_keeps_record(isolated_task_manager):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    rev0 = tm.get_task_snapshot(tid).revision

    assert tm.kill_background_task(tid) is True
    snap = tm.get_task_snapshot(tid)
    assert snap is not None, "kill 后任务记录必须保留为终态"
    assert snap.status == "cancelled"
    assert snap.terminal is True
    assert snap.revision > rev0
    assert tid not in {t["task_id"] for t in tm.list_active_tasks()}


def test_natural_terminal_task_is_not_merged_back_into_tasks_file(
    isolated_task_manager, tmp_path
):
    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("pass"))
    assert _wait_until(lambda: tm.get_task_snapshot(tid).terminal, timeout=10)
    tm._save_tasks()
    saved = json.loads(Path(tm.TASK_DB_PATH).read_text(encoding="utf-8"))
    assert tid not in saved


def test_snapshot_api_evicts_terminal_records_at_cap(isolated_task_manager, tmp_path):
    tm = isolated_task_manager
    log_file_path = tmp_path / "terminal-record.log"
    log_file_path.write_text("", encoding="utf-8")
    for index in range(tm._TERMINAL_KEEP_CAP + 1):
        tid = f"term{index:04d}"
        tm.ACTIVE_TASKS[tid] = {
            "id": tid,
            "command": "finished",
            "pid": None,
            "process": None,
            "start_time": index,
            "log_file_path": str(log_file_path),
            "status": tm.TASK_STATUS_COMPLETED,
            "exit_code": 0,
            "revision": 1,
            "last_log_size": 0,
        }
    snapshots = tm.list_task_snapshots()
    assert len([snap for snap in snapshots if snap.terminal]) == tm._TERMINAL_KEEP_CAP
    assert "term0000" not in tm.ACTIVE_TASKS


def test_removed_tombstones_drop_oldest_instead_of_clearing_all(isolated_task_manager):
    tm = isolated_task_manager
    for index in range(tm._REMOVED_TIDS_CAP + 1):
        tm._record_removed_locked(f"task{index:04d}")
    assert len(tm._REMOVED_TIDS) == tm._REMOVED_TIDS_CAP
    assert "task0000" not in tm._REMOVED_TIDS
    assert f"task{tm._REMOVED_TIDS_CAP:04d}" in tm._REMOVED_TIDS


def test_watcher_initial_poll_error_reports_failed_callback(
    isolated_task_manager, tmp_path, monkeypatch
):
    tm = isolated_task_manager

    class PollRaisesProcess:
        def poll(self):
            raise RuntimeError("poll failed")

    log_file_path = tmp_path / "task_pollerr1.log"
    log_file_path.write_text("", encoding="utf-8")
    process = PollRaisesProcess()
    callbacks = []
    monkeypatch.setattr(tm, "_on_task_finished", lambda *args: callbacks.append(args))
    tm.ACTIVE_TASKS["pollerr1"] = {
        "id": "pollerr1",
        "command": "broken poll",
        "pid": None,
        "process": process,
        "start_time": time.time(),
        "log_file_path": str(log_file_path),
        "status": tm.TASK_STATUS_RUNNING,
        "exit_code": None,
        "revision": 0,
        "last_log_size": 0,
    }
    tm._watch_task_completion("pollerr1", process, "cmd", "session")
    snap = tm.get_task_snapshot("pollerr1")
    assert snap.status == tm.TASK_STATUS_FAILED
    assert snap.terminal is True
    assert snap.exit_code is None
    assert len(callbacks) == 1
    assert callbacks[0][2] == -1


def test_watcher_sync_poll_error_stops_before_a_third_poll(
    isolated_task_manager, tmp_path, monkeypatch
):
    tm = isolated_task_manager

    class FirstPollReturnsThenSyncPollRaises:
        def __init__(self):
            self.calls = 0

        def poll(self):
            self.calls += 1
            if self.calls == 1:
                return None
            raise RuntimeError("sync poll failed")

    log_file_path = tmp_path / "task_pollerr2.log"
    log_file_path.write_text("", encoding="utf-8")
    process = FirstPollReturnsThenSyncPollRaises()
    callbacks = []
    monkeypatch.setattr(tm, "_on_task_finished", lambda *args: callbacks.append(args))
    monkeypatch.setattr(tm.time, "sleep", lambda _seconds: None)
    tm.ACTIVE_TASKS["pollerr2"] = {
        "id": "pollerr2",
        "command": "broken sync poll",
        "pid": None,
        "process": process,
        "start_time": time.time(),
        "log_file_path": str(log_file_path),
        "status": tm.TASK_STATUS_RUNNING,
        "exit_code": None,
        "revision": 0,
        "last_log_size": 0,
    }

    tm._watch_task_completion("pollerr2", process, "cmd", "session")

    snap = tm.get_task_snapshot("pollerr2")
    assert snap.status == tm.TASK_STATUS_FAILED
    assert snap.terminal is True
    assert snap.exit_code is None
    assert process.calls == 2
    assert len(callbacks) == 1
    assert callbacks[0][2] == -1


def test_plan_finish_rejects_wrong_turn_non_active_and_queued(monkeypatch):
    from core.plan import PlanManager, plan_finish
    from core.task_scheduler import TaskScheduler

    plan = PlanManager()
    plan.replace([
        {"id": "active", "subject": "active"},
        {"id": "queued", "subject": "queued"},
        {"id": "plain", "subject": "plain"},
    ])
    scheduler = TaskScheduler(plan, max_workers=3)
    monkeypatch.setattr(
        "core.task_scheduler.get_default_scheduler", lambda: scheduler
    )
    monkeypatch.setattr("core.plan.get_default_plan_manager", lambda: plan)

    assert scheduler.start_delegated_item(
        "active", "subagent-act1", "subagent:implement", None
    ) is True
    assert scheduler.start_delegated_item(
        "queued", "subagent-que2", "subagent:implement", None
    ) is True
    assert scheduler.handoff_to_main(
        "active", "subagent-act1", "turn-1", "timeout"
    ) is not None
    assert scheduler.handoff_to_main(
        "queued", "subagent-que2", "turn-1", "timeout"
    ) is not None


    wrong_turn = plan_finish(
        "active", "done", parent_turn_id="turn-2",
    )
    assert wrong_turn.startswith("🚨")
    queued = plan_finish("queued", "done", parent_turn_id="turn-1")
    assert queued.startswith("🚨")
    plain = plan_finish("plain", "done", parent_turn_id="turn-1")
    assert plain.startswith("🚨")
    no_turn = plan_finish("active", "done", parent_turn_id=None)
    assert no_turn.startswith("🚨")

    items = {item["id"]: item for item in plan.snapshot()}
    assert items["active"]["status"] == "in_progress"
    assert items["queued"]["status"] == "in_progress"
    assert items["plain"]["status"] == "pending"


def test_plan_finish_schema_hides_runtime_context():
    from core.tool_registry import build_default_registry

    schema = (
        build_default_registry().get("plan_finish")
        .api_schema()["function"]["parameters"]
    )
    assert set(schema["properties"]) == {"item_id", "result"}
    assert set(schema["required"]) == {"item_id", "result"}


def test_run_subagent_rejects_oversized_description(monkeypatch):
    import core.tools as tools_module

    result = tools_module.run_subagent(
        "x" * 6001, parent_session="s", parent_turn_id="t",
    )
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "6000" in payload["error"]


def test_run_subagent_enforces_foreground_hard_timeout(monkeypatch):
    import core.tools as tools_module

    class UnusedManager:
        def run(self, *args, **kwargs):
            raise AssertionError("前台硬上限拦截后不得启动子代理")

    monkeypatch.setattr(tools_module, "subagent_manager", UnusedManager())
    result = tools_module.run_subagent(
        "长任务", parent_session="s", parent_turn_id="t", timeout_seconds=901,
    )
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "900" in payload["error"]
    assert "manage_task" in payload["error"]


def test_internal_run_subagent_keeps_explicit_foreground_timeout(monkeypatch):
    import core.tools as tools_module

    received = {}

    class CapturingManager:
        def run(self, *args, **kwargs):
            received.update(kwargs)
            return "completed"

    monkeypatch.setattr(tools_module, "subagent_manager", CapturingManager())

    result = tools_module.run_subagent(
        "内部任务",
        parent_session="s",
        parent_turn_id="t",
        timeout_seconds=600,
    )

    assert result == "completed"
    assert received["timeout_seconds"] == 600


def test_run_subagent_background_still_allows_long_timeout(monkeypatch):
    import core.tools as tools_module

    bg_manager = SubagentManager(runner=lambda record: "完成")
    monkeypatch.setattr(tools_module, "subagent_manager", bg_manager)
    try:
        result = tools_module.run_subagent(
            "后台长任务",
            run_in_background=True,
            parent_session="s",
            parent_turn_id="t",
            timeout_seconds=3600,
        )
        payload = json.loads(result)
        assert payload["status"] == "running"
    finally:
        assert bg_manager.wait_for_idle(2)


def test_foreground_waits_for_failed_terminal_callback_before_returning():
    entered = threading.Event()
    release = threading.Event()
    results = []

    def runner(_record):
        raise RuntimeError("boom")

    def terminal_callback(_task, _status, _result, _error):
        entered.set()
        assert release.wait(2.0)

    manager = SubagentManager(runner=runner, wait_interval=0.001)
    thread = threading.Thread(
        target=lambda: results.append(manager.run(
            "失败任务",
            "session",
            "turn",
            timeout_seconds=30,
            terminal_callback=terminal_callback,
        )),
        daemon=True,
    )
    thread.start()
    assert entered.wait(2.0)
    task = next(iter(manager._tasks.values()))

    try:
        time.sleep(0.05)
        assert thread.is_alive() is True
        assert task.status == "failed"
        assert task.done_event.is_set() is False
        assert results == []
    finally:
        release.set()
        thread.join(timeout=2.0)

    assert thread.is_alive() is False
    assert task.done_event.is_set() is True
    assert len(results) == 1


def test_subagent_partial_tool_failure_still_completes():
    conclusion = "已完成 core/tools.py 的改动，共修改 3 处，验证命令通过。" * 6

    def runner(record):
        record.tool_success_count = 3
        record.tool_errors["edit_file"] = "文件不存在：core/foo.py"
        return conclusion

    manager = SubagentManager(runner=runner)
    result = manager.run("改代码", "s1", "t1", timeout_seconds=30)

    assert manager.snapshot()[0].status == "completed"
    assert "已完成 core/tools.py 的改动" in result
    assert "子代理执行期有工具报错" in result
    assert "edit_file" in result


def test_edit_file_missing_file_suggests_write_file():
    import core.tools as tools_mod

    result = tools_mod.edit_file(
        "不存在的文件_xyz_不要创建.py",
        target_content="a",
        replacement_content="b",
    )

    assert result.startswith("🚨")
    assert "write_file" in result


def test_subagent_edit_file_missing_file_suggests_write_file():
    test_edit_file_missing_file_suggests_write_file()


def test_subagent_timeout_salvages_partial_result():
    body = "已完成的现场：" + "改动 core/tools.py 并跑通验证。" * 12

    def runner(record):
        record.cancel_event.set()
        return body

    manager = SubagentManager(runner=runner)
    result = manager.run("长任务", "s1", "t1", timeout_seconds=30)

    snapshot = manager.snapshot()[0]
    assert snapshot.status == "cancelled"
    assert body in snapshot.partial_result
    assert "已完成的部分" in result


def test_subagent_captures_turn_error_event(monkeypatch):
    import core.llm as llm_mod
    from core.turn_events import TurnEvent

    def fake_events(*args, **kwargs):
        yield TurnEvent("t1", 1, "assistant_delta", {"text": "先读一下文件"})
        yield TurnEvent(
            "t1", 2, "turn_error",
            {"message": "连续 12 轮没有检测到状态变化，回合已终止"},
        )

    monkeypatch.setattr(llm_mod, "chat_with_model_events", fake_events)

    manager = SubagentManager()
    result = manager.run("测试", "s1", "t1", timeout_seconds=30)

    assert manager.snapshot()[0].status == "failed"
    assert "连续 12 轮" in result


def test_subagent_turn_error_skips_summary_continuation(monkeypatch):
    import core.llm as llm_mod
    from core.turn_events import TurnEvent

    calls = []

    def fake_events(*args, **kwargs):
        calls.append(1)
        yield TurnEvent(
            "t1", 1, "turn_error",
            {"message": "连续 12 轮没有检测到状态变化，回合已终止"},
        )

    monkeypatch.setattr(llm_mod, "chat_with_model_events", fake_events)

    manager = SubagentManager()
    result = manager.run("测试", "s1", "t1", timeout_seconds=30)

    assert manager.snapshot()[0].status == "failed"
    assert "连续 12 轮" in result
    assert len(calls) == 1


def test_subagent_partial_salvaged_when_event_loop_raises(monkeypatch):
    import core.llm as llm_mod
    from core.turn_events import TurnEvent

    def fake_events(*args, **kwargs):
        yield TurnEvent(
            "t1", 1, "assistant_delta",
            {"text": "已定位问题：core/foo.py 第 10 行循环边界写反，"},
        )
        raise RuntimeError("网络连接中断")

    monkeypatch.setattr(llm_mod, "chat_with_model_events", fake_events)

    manager = SubagentManager()
    result = manager.run("测试", "s1", "t1", timeout_seconds=30)

    snapshot = manager.snapshot()[0]
    assert snapshot.status == "failed"
    assert "core/foo.py" in snapshot.partial_result
    assert "已完成的部分" in result
    assert "core/foo.py" in result


def test_truncate_subagent_result_keeps_warning_footer():
    from core.tools import SUBAGENT_WARNING_MARKER, _truncate_subagent_result

    footer = (
        SUBAGENT_WARNING_MARKER
        + "（1 个，可能影响结论可靠性，供主 Agent 判断是否复核）：\n"
        + "  - edit_file: 文件不存在：core/foo.py"
    )
    out = _truncate_subagent_result(
        "x" * 20000 + footer,
        16000,
        "\n\n... [子代理结果已截断，省略 {omitted} 字符]",
    )

    assert "edit_file: 文件不存在：core/foo.py" in out
    assert "省略" in out and "字符]" in out
    assert out.index("... [子代理结果已截断") < out.index(SUBAGENT_WARNING_MARKER)

    plain = _truncate_subagent_result(
        "y" * 20000, 16000, "\n\n... [省略 {omitted} 字符]"
    )
    assert plain.startswith("y")
    assert "省略 4000 字符" in plain
    assert SUBAGENT_WARNING_MARKER not in plain

    assert _truncate_subagent_result("short", 16000, "省略 {omitted}") == "short"

def test_subagent_budget_notice_thresholds_and_dedup():
    from core.tools import build_subagent_budget_notice

    assert build_subagent_budget_notice(600, 600, 0) == (0, None)
    assert build_subagent_budget_notice(151, 600, 0) == (0, None)

    level, text = build_subagent_budget_notice(150, 600, 0)
    assert level == 1
    assert "时间预算提醒" in text and "600s" in text and "150s" in text

    assert build_subagent_budget_notice(140, 600, 1) == (1, None)

    level, text = build_subagent_budget_notice(60, 600, 1)
    assert level == 2
    assert "时间预算即将耗尽" in text

    assert build_subagent_budget_notice(20, 600, 2) == (2, None)

    level, text = build_subagent_budget_notice(30, 300, 0)
    assert level == 2 and "即将耗尽" in text

    assert build_subagent_budget_notice(0, 0, 0) == (0, None)
    assert build_subagent_budget_notice("bad", 600, 0) == (0, None)


def test_subagent_timeout_failure_text_delivers_partial_and_progress():
    from core.tools import (
        SUBAGENT_PARTIAL_DELIVERY_MAX_CHARS,
        SubagentManager,
        SubagentTask,
    )

    manager = SubagentManager(runner=lambda record: "")
    task = SubagentTask(
        task_id="subagent-test",
        parent_session="s",
        parent_turn_id="t",
        description="大改造",
        subagent_type="implement",
        status="timed_out",
        stage="tool",
        timeout_seconds=900,
        execution_elapsed=900.0,
        approval_wait_elapsed=3.0,
        tool_success_count=7,
        partial_result="已完成一半的改动说明\n" * 2000,
    )
    text = manager._format_failure_text(task)

    assert "进度: 阶段 tool / 工具成功 7 次" in text
    assert "预算 900s" in text
    assert "不要从头重做" in text
    assert "部分产出已截断" in text
    assert len(text) > 10000
    assert len(text) < len(task.partial_result)


def test_subagent_timeout_without_partial_still_guides_direct_completion():
    from core.tools import SubagentManager, SubagentTask

    manager = SubagentManager(runner=lambda record: "")
    task = SubagentTask(
        task_id="subagent-empty",
        parent_session="s",
        parent_turn_id="t",
        description="空任务",
        subagent_type="explore",
        status="timed_out",
        stage="tool",
        timeout_seconds=300,
        execution_elapsed=300.0,
    )
    text = manager._format_failure_text(task)

    assert "直接用自身工具完成" in text
    assert "不要从头重做" not in text
