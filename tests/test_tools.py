import pytest
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
    """轮询等待异步条件成立。

    run(run_in_background=True) 的调用线程只等启动器返回，真正执行任务的
    _run_task 工作线程与终态通知是异步落定的；join 启动线程后立即断言存在
    竞态，必须轮询到条件成立（或超时失败）。
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if condition():
            return True
        _time.sleep(interval)
    return bool(condition())


def _all_tasks_terminal(manager):
    """manager 名下所有任务都已进入终态（用于 drain 前的等待）。"""
    snapshots = manager.snapshot()
    return bool(snapshots) and all(
        s.status in _SUBAGENT_TERMINAL_STATUSES for s in snapshots
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
        self._pos = 0  # 流式读取位置，模拟真实 HTTP 响应的连续读取
        self.status = 200
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self, limit=-1):
        """模拟真实 HTTP 响应的流式读取：每次 read 从当前位置继续，而非从头开始。"""
        if limit < 0:
            # 读全部剩余内容
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        # 读指定长度
        chunk = self._body[self._pos:self._pos + limit]
        self._pos += len(chunk)
        return chunk


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


def test_fetch_url_uses_browser_like_headers(monkeypatch):
    """fetch_url 应使用伪装的浏览器请求头（绕过反爬 403）"""
    captured_request = []

    def fake_urlopen(request, timeout):
        captured_request.append(request)
        return _FakeHTTPResponse("<html>内容</html>", "text/html; charset=utf-8")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    fetch_url("https://example.com/article")

    assert len(captured_request) == 1
    req = captured_request[0]
    # User-Agent 应为 Chrome 浏览器伪装，而非 ycode/...
    ua = req.get_header("User-agent")
    assert "Mozilla/5.0" in ua, f"应伪装为浏览器 UA，实际: {ua}"
    assert "Chrome" in ua, f"UA 应包含 Chrome 标识，实际: {ua}"
    assert "ycode" not in ua, f"UA 不应包含 ycode（会被反爬拦截），实际: {ua}"
    # 应携带浏览器常见请求头
    assert req.get_header("Accept-language"), "应携带 Accept-Language 头"
    assert req.get_header("Referer"), "应携带 Referer 头"


def test_fetch_url_truncates_large_response_instead_of_failing(monkeypatch):
    """响应超过 512KB 时应截断并附加提示，而非直接报错"""
    # 构造超过 HTTP_MAX_BYTES 的响应体
    from core.tools import HTTP_MAX_BYTES
    large_body = "x" * (HTTP_MAX_BYTES + 1024)  # 超过 512KB

    def fake_urlopen(request, timeout):
        return _FakeHTTPResponse(large_body, "text/html; charset=utf-8")

    monkeypatch.setattr("core.tools.urlopen", fake_urlopen)
    monkeypatch.setattr("core.tools._check_ssrf_url", lambda url: None)

    result = fetch_url("https://example.com/large-article")

    # 不应报错（不以 🚨 开头）
    assert not result.startswith("🚨"), f"大响应应截断而非报错，实际: {result[:100]}"
    # 应包含 HTTP 200 状态
    assert "HTTP 200" in result
    # 应包含截断提示
    assert "已截断" in result, "应附加截断提示"
    # 应包含部分内容（截断后的前 512KB）
    assert "x" in result


def test_fetch_url_does_not_truncate_small_response(monkeypatch):
    """小响应不应被截断，也不应附加截断提示"""
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

def test_read_and_write_file(tmp_path, monkeypatch):
    """测试 Agent 是否能正确读取和写入文件"""
    monkeypatch.chdir(tmp_path)
    test_file = tmp_path / "hello.py"
    content_to_write = "print('Hello, Y-code!')"
    
    write_file(test_file, content_to_write)
    read_content = read_file(test_file)
    
    assert read_content == content_to_write

def test_list_directory(tmp_path, monkeypatch):
    """测试雷达功能：能否看清文件夹里有什么"""
    monkeypatch.chdir(tmp_path)
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


def test_execute_command_rejects_catastrophic_commands_before_spawn(monkeypatch):
    monkeypatch.setattr(
        "core.tools.subprocess.Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    result = execute_command("rm -rf / && echo done")

    assert result.startswith("🚨")
    assert "危险命令" in result


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
        # 新版 execute_command 把输出重定向到临时文件 sink，fake 直接写入
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
    from core.memory import begin_turn_checkpoint, end_turn_checkpoint

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "subagent-owned.txt"
    target.write_bytes(b"before")
    begin_turn_checkpoint("checkpoint-session", "checkpoint-turn", 0)

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
        checkpoint = end_turn_checkpoint()

    assert result == "子代理写入完成"
    assert checkpoint["file_snapshots"] == [{
        "file_path": str(target),
        "old_content": "before",
        "existed_before": True,
        "old_bytes": b"before",
    }]


def test_completed_parent_checkpoint_rejects_late_subagent_snapshot(tmp_path):
    from core.memory import (
        begin_turn_checkpoint,
        bind_turn_checkpoint,
        clear_turn_checkpoint_binding,
        end_turn_checkpoint,
        record_file_snapshot_for_current_turn,
    )

    bound = threading.Event()
    continue_after_parent_commit = threading.Event()
    begin_turn_checkpoint("late-session", "late-turn", 0)

    def late_subagent():
        try:
            assert bind_turn_checkpoint("late-session", "late-turn") is True
            bound.set()
            assert continue_after_parent_commit.wait(timeout=10)
            record_file_snapshot_for_current_turn(
                str(tmp_path / "late.txt"),
                "before",
                existed_before=True,
                old_bytes=b"before",
            )
        finally:
            clear_turn_checkpoint_binding()

    thread = threading.Thread(target=late_subagent, daemon=True)
    thread.start()
    assert bound.wait(timeout=10)
    checkpoint = end_turn_checkpoint()
    continue_after_parent_commit.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert checkpoint["file_snapshots"] == []


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
    """completed 回调一旦被认领，取消不得二次回调；finalizing 仍属于 active。"""
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

    assert _wait_until(lambda: len(notifications) >= 1), "应触发1次通知"
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

    assert _wait_until(lambda: len(notifications) >= 1), "失败也应触发1次通知"
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

    assert _wait_until(lambda: len(notifications) >= 1), "后台任务完成后应触发通知"
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
    assert _wait_until(
        lambda: manager.snapshot() and manager.snapshot()[0].status == "completed"
    ), "回调异常不应影响子代理完成"
    snapshots = manager.snapshot()
    assert snapshots[0].status == "completed"


def test_is_plan_item_running_returns_true_for_running_task():
    """is_plan_item_running 应识别仍在运行的后台子代理任务（避免误杀）"""
    import threading
    import time as _time

    started = threading.Event()

    def slow_runner(record):
        started.set()
        # 让任务持续运行，直到 cancel_event 被设置
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
    assert started.wait(2.0), "子代理应在 2s 内启动"

    try:
        # 任务运行中：应返回 True
        assert manager.is_plan_item_running("plan-running-1") is True
        # 无关联任务的 plan_item_id：应返回 False
        assert manager.is_plan_item_running("plan-nonexistent") is False
        # 空 plan_item_id：应返回 False
        assert manager.is_plan_item_running("") is False
    finally:
        # 清理：取消运行中的任务
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread.join(timeout=2.0)


def test_is_plan_item_running_returns_false_for_completed_task():
    """is_plan_item_running 对已完成的任务应返回 False"""
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

    # 先等任务真正进入终态，避免"尚未注册即返回 False"的假阳性
    assert _wait_until(lambda: _all_tasks_terminal(manager)), "后台任务应在超时前进入终态"
    # 任务已完成：应返回 False
    assert manager.is_plan_item_running("plan-done-1") is False


def test_drain_terminal_background_tasks_returns_all_terminal_unconsumed():
    """drain_terminal_background_tasks 应返回所有终态（成功+失败）且未被消费的后台任务"""
    import threading

    # 用一个 manager 管理两个后台任务：一个成功，一个失败
    # runner 根据 description 区分返回不同结果
    def runner(record):
        if "失败" in record.description:
            record.tool_errors["search_news"] = "无结果"
            return "查询失败"
        return "查询结果：晴"

    manager = SubagentManager(runner=runner)

    # 后台任务1：成功完成
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

    # 后台任务2：失败
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

    # 两个后台任务异步执行，drain 前必须等到全部进入终态
    assert _wait_until(lambda: len(manager.snapshot()) == 2 and _all_tasks_terminal(manager)), \
        "两个后台任务都应进入终态"
    # drain：应返回 2 个任务（1 成功 + 1 失败）
    drained = manager.drain_terminal_background_tasks("s-fallback")
    assert len(drained) == 2, f"应提取 2 个任务，实际 {len(drained)}"

    statuses = {t.status for t in drained}
    assert "completed" in statuses, "应包含已完成的任务"
    assert "failed" in statuses, "应包含失败的任务"

    # 验证成功任务的结果
    completed_task = next(t for t in drained if t.status == "completed")
    assert "晴" in completed_task.result

    # 第二次 drain：应返回空（已通过 summary_injected 标记消费）
    drained_again = manager.drain_terminal_background_tasks("s-fallback")
    assert drained_again == [], "已消费的任务不应重复提取"

    # 不同会话：应返回空
    drained_other = manager.drain_terminal_background_tasks("s-other")
    assert drained_other == [], "其他会话的任务不应被提取"


def test_drain_terminal_independent_from_notification_consumed():
    """drain_terminal_background_tasks 应独立于 drain_notifications 的消费标志。

    场景：drain_notifications 在普通 turn 中消费了任务通知（notification_consumed=True），
    但汇总 turn 仍应能看到任务结果（summary_injected=False → 可提取）。
    这修复了"第一次过早汇总消费了 AI 任务后，第二次汇总看不到 AI 任务结果"的 bug。
    """
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

    # drain_notifications 是消费式读取，先等任务进入终态再消费
    assert _wait_until(lambda: _all_tasks_terminal(manager)), "后台任务应在超时前进入终态"
    # 先用 drain_notifications 消费通知（模拟普通 turn 的 LLM 上下文注入）
    notifications = manager.drain_notifications("s-indep")
    assert len(notifications) == 1, "drain_notifications 应返回 1 条通知"

    # 再用 drain_terminal_background_tasks 提取：应仍能返回任务（独立标志）
    drained = manager.drain_terminal_background_tasks("s-indep")
    assert len(drained) == 1, (
        "drain_terminal_background_tasks 应独立于 notification_consumed，"
        "即使 drain_notifications 已消费通知，汇总 turn 仍应能看到任务结果"
    )
    assert drained[0].status == "completed"
    assert "晴" in drained[0].result

    # 第二次 drain_terminal：应返回空（summary_injected 已标记）
    drained_again = manager.drain_terminal_background_tasks("s-indep")
    assert drained_again == [], "summary_injected 已标记的任务不应重复提取"


def test_drain_terminal_background_tasks_skips_running_and_foreground():
    """drain_terminal_background_tasks 应跳过运行中任务和前台任务"""
    import threading
    import time as _time

    # 后台任务：运行中（不结束）
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
    assert started.wait(2.0), "子代理应在 2s 内启动"

    try:
        # drain：不应返回运行中的任务
        drained = manager.drain_terminal_background_tasks("s-mix")
        assert drained == [], "运行中的后台任务不应被 drain"
    finally:
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread1.join(timeout=2.0)

    # 前台任务：完成
    manager_fg = SubagentManager(runner=lambda record: "前台结果")
    manager_fg.run("前台任务", "s-mix", "t1", timeout_seconds=30, plan_item_id="p-fg-ok")

    # drain：不应返回前台任务
    drained_fg = manager_fg.drain_terminal_background_tasks("s-mix")
    assert drained_fg == [], "前台任务不应被 drain"


def test_has_running_background_tasks_detects_running():
    """has_running_background_tasks 应正确识别运行中的后台任务"""
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
    assert started.wait(2.0), "子代理应在 2s 内启动"

    try:
        # 有运行中的任务：应返回 True
        assert manager.has_running_background_tasks("s-running") is True
        # 无任务的会话：应返回 False
        assert manager.has_running_background_tasks("s-other") is False
        # 空会话：应返回 False
        assert manager.has_running_background_tasks("") is False
    finally:
        manager.cancel(manager.snapshot()[0].task_id, "测试结束")
        thread.join(timeout=2.0)

    # 任务取消后：应返回 False
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
    """子代理计划验证被拒绝后不得再触发任何命令执行路径。"""
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
    """验证命令的真实非零退出码必须使计划项失败，即使没有任何输出。"""
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
    """验证命令的真实零退出码必须完成计划项。"""
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
    """内部执行结果仅属于传入的调用对象，不能在后续命令间共享。"""
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


def test_timeout_sets_cancel_before_handoff_callback():
    """超时移交前必须先取消旧子代理：回调执行时 cancel_event 必须已置位。"""
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
    """计划项子代理首次超时：原计划项移交主 Agent，不进入 failed/retry。"""
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
    """接管中的计划项禁止重新派发子代理（timed_out → takeover ≠ retry）。"""
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

    # 用生产调度 API 构造首次超时后的接管现场
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
    """排队等待接管的计划项同样禁止重新派发。"""
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
    """kill 结果分类必须映射到准确文案：自然死亡不得误报为"终止失败"。"""
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
    """无法校验进程身份时的保守拒绝也应有专属文案，而不是笼统的终止失败。"""
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


def test_relative_paths_resolve_against_thread_workdir(tmp_path, monkeypatch):
    """回归：相对路径（含目录工具默认 "."）必须按线程本地 workdir 解析。

    GUI 多会话并行时进程 cwd 可能停在安装目录；若相对路径按进程 cwd
    解析，沙盒守卫会把项目根内的相对路径误判为"工作目录之外"——开关
    打开时静默放行到错误目录，开关关闭时误弹权限询问。
    """
    from core.workspace_context import workspace_scope
    from core.tools import _resolve_workspace_path, is_outside_workspace

    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "note.txt").write_text("内容", encoding="utf-8")

    # 进程 cwd 故意停在 workdir 之外（模拟打包版 GUI：cwd=安装目录）
    monkeypatch.chdir(tmp_path)

    with workspace_scope(str(workspace)):
        assert is_outside_workspace("note.txt") is False
        assert is_outside_workspace(".") is False
        assert is_outside_workspace("../outside.txt") is True
        assert _resolve_workspace_path("note.txt") == (workspace / "note.txt").resolve()
        assert read_file("note.txt").rstrip("\n") == "内容"


def test_ycode_dir_reads_allowed_under_sandbox_restriction(tmp_path, monkeypatch):
    """回归：沙盒限制开启时，~/.ycode 全目录读取（含凭据文件）必须可用。

    覆盖 @附件挂载与 read_file 的免 bypass 直读路径（allow_trusted_read）；
    写入路径不走该豁免，凭据写入仍由沙盒守卫逐次询问。
    """
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
        # 读取（含凭据文件）：沙盒限制下也直接解析成功
        for rel in ("memory/MEMORY.md", "config.json"):
            resolved = _resolve_workspace_path(str(ycode_dir / rel), allow_trusted_read=True)
            assert resolved == (ycode_dir / rel).resolve()

        # 写入语义（不传 allow_trusted_read）：.ycode 仍视为沙盒外，
        # 必须走守卫询问（此处断言边界校验会拒绝裸写入）
        try:
            _resolve_workspace_path(str(ycode_dir / "config.json"))
            raised = False
        except ValueError:
            raised = True
        assert raised, "沙盒限制下裸写入 .ycode 路径应被边界校验拒绝"


def test_ycode_and_agents_files_are_not_trusted_read_paths(tmp_path, monkeypatch):
    """Only configured Skill packages may bypass the workspace sandbox for reads."""
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
    """A scan root and unrelated siblings must not inherit a Skill package's trust."""
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
    """Trusted Skill resources retain the public path-expansion input contract."""
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
    """write_file must not use the read-only Skill exemption."""
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
    """A configured Skill package remains readable outside the workspace."""
    import core.skills as skills_mod

    skill_dir = tmp_path / "skills" / "example"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("# Example", encoding="utf-8")
    monkeypatch.setattr(skills_mod, "get_skill_dirs", lambda: [str(skill_dir.parent)])

    assert skills_mod.is_trusted_read_path(str(skill_file))
    assert skills_mod.is_trusted_path(str(skill_file))


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

    assert scheduler.start_item("item-1") is False  # failed 状态不可直接重启
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


def test_subagent_result_strips_parenthesized_advisor_leak():
    """回归：圆括号形式的 advisor 内部说明（如 "(advisor returned no content)"）
    也必须从子代理结果中剔除。"""
    manager = SubagentManager(
        runner=lambda record: "(advisor returned no content)\n\n真正的结论。"
    )

    result = manager.run("任务", "s-paren", "t-paren", timeout_seconds=30)

    assert "advisor" not in result.lower()
    assert "真正的结论。" in result


def test_subagent_result_strips_xml_junk_blocks():
    """回归（软件识别会话事故）：模型把 <advisor-request> 求助文本和
    <function_calls> XML 伪工具调用整块泄漏到正文时，必须从子代理结果中剔除。
    未闭合的块（流被截断）也要剔到末尾。"""
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
    """回归（软件识别会话事故）：子代理结果只保留最后一次工具调用之后的文本。

    工具调用之前的过程叙述（"我先给你一版初步结论，继续补读…"）不是最终结论，
    混入结果会让主 Agent 把中间思路当成事实汇总。"""
    from types import SimpleNamespace
    import core.llm as llm_module

    final_text = "最终结论：" + "核心模块无阻塞性问题。" * 30  # >200 字，避免触发总结续写

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
    """回归：未显式传 timeout_seconds 时按角色解析默认超时
    （explore 240 / review 300 / test 480 / implement、general 600）。
    只读子代理的设计目标是快，不能默认给 10 分钟。"""
    import core.tools as tools_module

    captured = {}

    class _FakeManager:
        def run(self, task_description, parent_session, parent_turn_id, **kwargs):
            captured.update(kwargs)
            return "完成"

    monkeypatch.setattr(tools_module, "subagent_manager", _FakeManager())

    for role, expected in [
        ("explore", 240), ("review", 300), ("test", 480),
        ("implement", 600), ("general", 600), ("custom-agent", 600),
    ]:
        captured.clear()
        tools_module.run_subagent(
            "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
            subagent_type=role,
        )
        assert captured["timeout_seconds"] == expected, role

    # 显式传入时以显式值为准，但受前台硬上限 600 秒约束（Task 8）
    captured.clear()
    tools_module.run_subagent(
        "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
        subagent_type="explore", timeout_seconds=600,
    )
    assert captured["timeout_seconds"] == 600
    captured.clear()
    tools_module.run_subagent(
        "任务", parent_session="s-timeout", parent_turn_id="t-timeout",
        subagent_type="explore", timeout_seconds=601,
    )
    assert "timeout_seconds" not in captured


def test_run_subagent_surfaces_real_verification_error(monkeypatch):
    """回归：验证失败时必须把真实错误（如验证命令 stderr）返回给主 Agent，
    不得只报"不是 in_progress，无法验证完成"的误导信息导致 retry 死循环。"""
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
        # 必定失败的命令（cmd 与 sh 均支持 exit）
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

    # 失败结果为结构化文本，应包含验证失败的具体原因，而不是"不是 in_progress"的误导信息
    assert "失败" in result
    assert "不是 in_progress" not in result
    assert "验证" in result
    assert plan.snapshot()[0]["status"] == "failed"


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


# ============================================================================
# 行号路径测试（with_line_numbers + start_line/end_line）
# 借鉴 Hashline 思想的轻量实现：read_file 返回行号 → edit_file 按行号切片替换
# ============================================================================


def test_read_file_with_line_numbers_returns_prefixed_format(tmp_path, monkeypatch):
    """read_file(with_line_numbers=True) 应返回 'N: line' 格式（1-based）。"""
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sample.py"
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = read_file("sample.py", with_line_numbers=True)

    assert result == "1: alpha\n2: beta\n3: gamma"


def test_read_file_with_line_numbers_respects_offset_and_limit(tmp_path, monkeypatch):
    """with_line_numbers 与 offset/limit 配合时行号应反映原始位置。"""
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "paged.txt"
    target.write_text("L1\nL2\nL3\n", encoding="utf-8")  # 3 行

    # 从第 2 行（0-based offset=1）开始读 2 行，行号应为 2, 3（读取剩余全部，不触发截断）
    result = read_file("paged.txt", offset=1, limit=2, with_line_numbers=True)

    assert result == "2: L2\n3: L3"


def test_read_file_with_line_numbers_ignores_compress(tmp_path, monkeypatch):
    """with_line_numbers=True 时 compress 参数应被忽略，保留原始行结构。"""
    from core.tools import read_file
    from core import tool_cache
    tool_cache.clear_all()

    monkeypatch.chdir(tmp_path)
    # 含连续空行的文件，compress=True 时本应合并空行
    target = tmp_path / "spaced.txt"
    target.write_text("a\n\n\nb\n", encoding="utf-8")

    # 同时开启 compress 和 with_line_numbers，行号路径应优先
    result = read_file("spaced.txt", compress=True, with_line_numbers=True)

    # 应保留 4 行（含 2 个空行），不应被压缩
    assert result == "1: a\n2: \n3: \n4: b"


def test_read_file_with_line_numbers_caches_separately_from_plain(tmp_path, monkeypatch):
    """with_line_numbers=True 和 False 应分别缓存，不互相污染。"""
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

    # 再次读取应命中各自缓存
    plain2 = read_file("cache.txt")
    numbered2 = read_file("cache.txt", with_line_numbers=True)
    assert plain2 == plain
    assert numbered2 == numbered


def test_edit_file_by_lines_basic_replacement(tmp_path, monkeypatch):
    """行号路径：基本替换功能，应跳过字符串匹配直接按行号切片。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "code.py"
    target.write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n", encoding="utf-8")

    # 替换第 2 行（return 1 → return 42）
    result = edit_file("code.py", replacement_content="    return 42", start_line=2, end_line=2)

    assert result.startswith("✔")
    assert "第 2-2 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 42\n\ndef bar():\n    return 2\n"


def test_edit_file_by_lines_multi_line_range(tmp_path, monkeypatch):
    """行号路径：多行范围替换。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "multi.py"
    original = "a\nb\nc\nd\ne\n"
    target.write_text(original, encoding="utf-8")

    # 替换第 2-4 行（b,c,d → X,Y）
    result = edit_file("multi.py", replacement_content="X\nY", start_line=2, end_line=4)

    assert result.startswith("✔")
    assert "第 2-4 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "a\nX\nY\ne\n"


def test_edit_file_by_lines_end_line_exceeds_total_clamps_silently(tmp_path, monkeypatch):
    """end_line 超过总行数时应自动收敛，不报错。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "short.txt"
    target.write_text("only\none\nline\n", encoding="utf-8")  # 3 行

    # end_line=100 远超总行数，应自动收敛到 3
    result = edit_file("short.txt", replacement_content="replaced", start_line=2, end_line=100)

    assert result.startswith("✔")
    # 实际替换了第 2-3 行
    assert "第 2-3 行替换成功" in result
    assert target.read_text(encoding="utf-8") == "only\nreplaced\n"


def test_edit_file_by_lines_includes_replaced_preview_in_response(tmp_path, monkeypatch):
    """成功响应中应包含被替换的原文预览（便于发现行号错误）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "preview.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    result = edit_file("preview.py", replacement_content="NEW", start_line=2, end_line=2)

    assert "被替换原文预览" in result
    assert "line2" in result  # 预览应包含被替换的原文


def test_edit_file_by_lines_preview_truncates_long_blocks(tmp_path, monkeypatch):
    """被替换块超过 5 行时预览应省略中间，只显示前 3 后 2。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "long_block.txt"
    content = "\n".join(f"line{i}" for i in range(1, 11)) + "\n"  # 10 行
    target.write_text(content, encoding="utf-8")

    # 替换第 1-10 行
    result = edit_file("long_block.txt", replacement_content="REPLACED", start_line=1, end_line=10)

    assert result.startswith("✔")
    assert "省略中间部分" in result
    assert "line1" in result  # 前 3 行之一
    assert "line3" in result  # 前 3 行之一
    assert "line10" in result  # 后 2 行之一
    assert "line5" not in result  # 中间被省略


def test_edit_file_by_lines_rejects_start_line_below_one(tmp_path, monkeypatch):
    """start_line < 1 应报错（1-based 行号）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("content\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=0, end_line=1)

    assert result.startswith("🚨")
    assert "start_line 必须 >= 1" in result
    assert target.read_text(encoding="utf-8") == "content\n"  # 文件未被修改


def test_edit_file_by_lines_rejects_end_below_start(tmp_path, monkeypatch):
    """end_line < start_line 应报错。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\nc\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=3, end_line=1)

    assert result.startswith("🚨")
    assert "不能小于" in result


def test_edit_file_by_lines_rejects_start_exceeds_total(tmp_path, monkeypatch):
    """start_line 超出文件总行数应报错（end_line 自动收敛，但 start_line 不会）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("only\none\n", encoding="utf-8")  # 2 行

    result = edit_file("x.txt", replacement_content="new", start_line=100, end_line=200)

    assert result.startswith("🚨")
    assert "超出文件总行数" in result
    assert "read_file(with_line_numbers=True)" in result  # 应给出修复建议


def test_edit_file_by_lines_rejects_partial_line_args(tmp_path, monkeypatch):
    """只提供 start_line 不提供 end_line（或反之）应报错。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    # 只提供 start_line
    result1 = edit_file("x.txt", replacement_content="new", start_line=1)
    assert result1.startswith("🚨")
    assert "同时提供" in result1

    # 只提供 end_line
    result2 = edit_file("x.txt", replacement_content="new", end_line=1)
    assert result2.startswith("🚨")
    assert "同时提供" in result2


def test_edit_file_by_lines_rejects_bool_arguments(tmp_path, monkeypatch):
    """start_line/end_line 不能是布尔值（bool 是 int 子类，True==1 会被误用）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=True, end_line=True)

    assert result.startswith("🚨")
    assert "布尔值" in result


def test_edit_file_by_lines_rejects_non_int_arguments(tmp_path, monkeypatch):
    """start_line/end_line 必须是整数。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "x.txt"
    target.write_text("a\nb\n", encoding="utf-8")

    result = edit_file("x.txt", replacement_content="new", start_line=1.5, end_line=2)

    assert result.startswith("🚨")
    assert "整数" in result


def test_edit_file_by_lines_rejects_empty_file(tmp_path, monkeypatch):
    """空文件无法按行号编辑。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8")

    result = edit_file("empty.txt", replacement_content="new", start_line=1, end_line=1)

    assert result.startswith("🚨")
    assert "为空" in result


def test_edit_file_by_lines_no_change_returns_error(tmp_path, monkeypatch):
    """替换后内容与原文相同时应报错（避免无意义写入）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "same.txt"
    target.write_text("identical\n", encoding="utf-8")

    # replacement_content 与原行完全相同
    result = edit_file("same.txt", replacement_content="identical", start_line=1, end_line=1)

    assert result.startswith("🚨")
    assert "相同" in result


def test_edit_file_by_lines_writes_undo_stack(tmp_path, monkeypatch):
    """行号路径成功时应写入撤销栈（与字符串路径行为一致）。"""
    import core.memory as memory_module
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "undo.py"
    # 用 write_bytes 确保 LF 换行，避免 Windows write_text 默认转 CRLF
    target.write_bytes(b"original\n")
    captured = []
    monkeypatch.setattr(
        memory_module,
        "set_undo_state",
        lambda file_path, content: captured.append((file_path, content)),
    )

    result = edit_file("undo.py", replacement_content="modified", start_line=1, end_line=1)

    assert result.startswith("✔")
    assert len(captured) == 1
    undo_file, undo_content = captured[0]
    assert undo_file == "undo.py"
    assert undo_content == "original\n"  # 撤销栈保存的是修改前的内容


def test_edit_file_by_lines_failed_validation_does_not_pollute_undo_stack(tmp_path, monkeypatch):
    """行号路径校验失败时不应写入撤销栈。"""
    import core.memory as memory_module
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "safe.txt"
    target.write_text("a\nb\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        memory_module,
        "set_undo_state",
        lambda file_path, content: captured.append((file_path, content)),
    )

    # start_line 越界，应失败且不写撤销栈
    result = edit_file("safe.txt", replacement_content="new", start_line=100, end_line=200)

    assert result.startswith("🚨")
    assert captured == []
    assert target.read_text(encoding="utf-8") == "a\nb\n"  # 文件未被修改


def test_edit_file_by_lines_preserves_gbk_encoding(tmp_path, monkeypatch):
    """行号路径应保持 GBK 编码写回（与字符串路径一致）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "gbk_lines.txt"
    original = "第一行\r\n第二行\r\n第三行\r\n"
    target.write_bytes(original.encode("gbk"))

    # 替换第 2 行
    result = edit_file("gbk_lines.txt", replacement_content="替换行", start_line=2, end_line=2)

    assert result.startswith("✔")
    assert target.read_bytes() == "第一行\r\n替换行\r\n第三行\r\n".encode("gbk")


def test_edit_file_by_lines_adapts_crlf_replacement(tmp_path, monkeypatch):
    """行号路径：replacement 含 LF 但文件是 CRLF 时应自动转换。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "crlf_lines.txt"
    target.write_bytes(b"alpha\r\nbeta\r\nomega\r\n")

    # replacement 用 LF，应被转为 CRLF
    result = edit_file("crlf_lines.txt", replacement_content="line1\nline2", start_line=2, end_line=2)

    assert result.startswith("✔")
    raw = target.read_bytes()
    assert raw == b"alpha\r\nline1\r\nline2\r\nomega\r\n"
    assert b"\r\r\n" not in raw  # 不应腐蚀


def test_edit_file_by_lines_ignores_target_content(tmp_path, monkeypatch):
    """行号路径下 target_content 即使提供也应被忽略（不参与匹配）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "ignore_target.py"
    target.write_text("actual_line\n", encoding="utf-8")

    # target_content 与原文完全不同，但行号路径应忽略它
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
    """未提供 start_line/end_line 时应回退到字符串匹配路径（兼容性）。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "compat.py"
    target.write_text("def foo():\n    return 1\n", encoding="utf-8")

    # 不提供 start_line/end_line，应走字符串匹配
    result = edit_file("compat.py", target_content="return 1", replacement_content="return 42")

    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8") == "def foo():\n    return 42\n"


def test_edit_file_only_replacement_triggers_smart_replace(tmp_path, monkeypatch):
    """只传 replacement_content（无 target_content 和行号）时触发智能降级替换。
    新语义：用 difflib 找最相似代码段进行替换，而非直接报错。
    """
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hint.py"
    # 多行内容，让智能替换有足够上下文定位
    target.write_text("def old_func():\n    return 'old'\n", encoding="utf-8")

    replacement = "def new_func():\n    return 'new'\n"

    result = edit_file("hint.py", replacement_content=replacement)

    # 智能替换应成功
    assert result.startswith("✔")
    assert "智能修改" in result
    new_content = target.read_text(encoding="utf-8")
    assert "new_func" in new_content
    assert "old_func" not in new_content


def test_edit_file_only_replacement_rejects_when_no_similarity(tmp_path, monkeypatch):
    """只传 replacement_content 但找不到相似代码段时，给出明确错误并提示行号路径。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "hint.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")

    replacement = "completely unrelated content with no similarity at all here"

    result = edit_file("hint.py", replacement_content=replacement)

    assert result.startswith("🚨")
    assert "start_line" in result or "target_content" in result


def test_edit_file_by_lines_handles_duplicate_code_blocks_without_ambiguity(tmp_path, monkeypatch):
    """行号路径核心优势：重复代码段无歧义定位（字符串路径会失败的场景）。"""
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
    """端到端：read_file(with_line_numbers) → edit_file(start_line, end_line) 完整流程。"""
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

    # 步骤 1：读取带行号视图
    view = read_file("e2e.py", with_line_numbers=True)
    assert "5:     print('hello')" in view

    # 步骤 2：用行号替换第 5 行
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
    """行号路径：replacement 不带换行符时系统应自动补换行符，保持行结构。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "tail.txt"
    # 用 write_bytes 确保 LF 换行，避免 Windows write_text 默认转 CRLF
    target.write_bytes(b"a\nb\nc\n")

    # 替换第 3 行（"c\n"），replacement "C" 不带换行符
    # 新语义：replacement 按行切分后每行自动补换行符，"C" → "C\n"
    # 所以原 "c\n" 被替换为 "C\n"，文件保持 "a\nb\nC\n"
    result = edit_file("tail.txt", replacement_content="C", start_line=3, end_line=3)

    assert result.startswith("✔")
    assert target.read_bytes() == b"a\nb\nC\n"


def test_edit_file_by_lines_empty_replacement_deletes_lines(tmp_path, monkeypatch):
    """行号路径：replacement 为空字符串时应删除指定行范围。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "delete.txt"
    target.write_bytes(b"a\nb\nc\nd\n")

    # 替换第 2-3 行为空字符串 → 删除这两行
    result = edit_file("delete.txt", replacement_content="", start_line=2, end_line=3)

    assert result.startswith("✔")
    assert target.read_bytes() == b"a\nd\n"


def test_edit_file_by_lines_insert_at_start(tmp_path, monkeypatch):
    """行号路径：替换第 1 行可实现"插入+替换"效果。"""
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "head.py"
    target.write_text("old_first\nsecond\n", encoding="utf-8")

    # 替换第 1 行为多行内容
    result = edit_file(
        "head.py",
        replacement_content="new_first\nnew_second",
        start_line=1,
        end_line=1,
    )

    assert result.startswith("✔")
    assert target.read_text(encoding="utf-8") == "new_first\nnew_second\nsecond\n"


def test_execute_command_no_output_nonzero_exit_is_informational(monkeypatch):
    """非零退出 + 完全无输出（如 findstr 无匹配）不按 🚨 失败返回，
    避免 UI 红色失败刷屏；但必须如实说明退出码并给出区分提示。"""
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
    """非零退出但有输出：仍是 🚨 错误，输出内容原样带回。"""
    class FakeProcess:
        returncode = 2

        def poll(self):
            return 2

    def fake_popen(*args, **kwargs):
        # 新版 execute_command 把输出重定向到临时文件 sink，fake 直接写入
        kwargs["stdout"].write("爆炸了".encode("utf-8"))
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)

    result = execute_command("boom-command")

    assert result.startswith("🚨")
    assert "退出码 2" in result
    assert "爆炸了" in result


def test_list_directory_on_file_returns_soft_hint(tmp_path, monkeypatch):
    """把文件路径误传给 list_directory：返回普通提示（非 🚨 错误级），
    引导模型改用 read_file，UI 不再渲染成红色失败。"""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "SflF400_mk.v"
    target.write_text("x", encoding="utf-8")

    result = list_directory(str(target))

    assert not result.startswith("🚨")
    assert "read_file" in result


# ---------------------------------------------------------------------------
# 源码审计修复的回归测试（High3-5 / M1-M8 / Low）
# ---------------------------------------------------------------------------


def test_ssrf_strict_mode_is_default_when_config_missing(monkeypatch):
    """High 4：未显式配置时默认严格模式（仅用户显式配置 false 才降级宽松）。"""
    import core.tools as tools_mod
    import config as config_mod

    monkeypatch.setattr(config_mod, "_read_json_with_backup", lambda *a, **k: {})
    assert tools_mod._get_fetch_url_strict_ssrf() is True
    monkeypatch.setattr(config_mod, "_read_json_with_backup", lambda *a, **k: {"fetch_url_strict_ssrf": False})
    assert tools_mod._get_fetch_url_strict_ssrf() is False


def test_ssrf_default_strict_blocks_loopback_and_private(monkeypatch):
    """High 4：默认严格模式下 loopback/私网 IP 字面量被拒绝。"""
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
    """High 5：302 重定向目标逐跳重新触发 SSRF 校验，跳到元数据接口被拦截。"""
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
    """High 5：重定向到安全公网地址时正常构造新请求。"""
    import core.tools as tools_mod
    from urllib.request import Request

    monkeypatch.setattr(tools_mod, "_check_ssrf_url", lambda url: None)
    handler = tools_mod._SSRFRecheckRedirectHandler()
    req = Request("https://example.com/start")
    new_req = handler.redirect_request(req, None, 302, "Found", {}, "https://example.com/next")
    assert new_req is not None
    assert new_req.full_url == "https://example.com/next"


def test_execute_command_output_cap_terminates_verbose_command():
    """M1：输出超过 1MB 上限时终止并截断，不再无限累积。"""
    import sys
    from core.tools import EXECUTE_COMMAND_MAX_OUTPUT_BYTES, execute_command

    overflow = EXECUTE_COMMAND_MAX_OUTPUT_BYTES + 512 * 1024
    result = execute_command(
        f'{sys.executable} -c "import sys; sys.stdout.write(\'x\' * {overflow})"',
        timeout_seconds=60,
    )
    assert "截断" in result or "上限" in result
    # 返回内容被限制在上限附近，不会把完整 1.5MB 带回
    assert len(result) < overflow


def test_read_file_missing_file_returns_friendly_error(tmp_path, monkeypatch):
    """M3：读已删除/不存在的文件返回友好错误文本，不抛异常栈。"""
    from core.tools import read_file

    monkeypatch.chdir(tmp_path)
    result = read_file("definitely_missing.txt")
    assert result.startswith("🚨")
    assert "不存在" in result


def test_edit_file_detects_external_modification_between_read_and_write(tmp_path, monkeypatch):
    """M4：读取后文件被外部修改时，edit_file 拒绝静默覆盖并要求重读。"""
    import core.tools as tools_mod
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "race.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

    original_fuzzy = tools_mod._fuzzy_replace

    def racing_fuzzy(content, match_target, match_replacement, replace_all=False):
        # 模拟并发子代理：在 读→写 窗口内从外部改动同一文件
        target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
        # Windows/NTFS mtime 粒度粗（实测相邻两次写入约半数概率 mtime 相同，
        # 同片则检测窗口消失），显式推进 mtime 确定性地构造"读后被改"状态
        import os as _os
        st = target.stat()
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        return original_fuzzy(content, match_target, match_replacement, replace_all=replace_all)

    monkeypatch.setattr(tools_mod, "_fuzzy_replace", racing_fuzzy)

    result = edit_file("race.py", "beta = 2", "beta = 20")

    assert result.startswith("🚨")
    assert "外部修改" in result
    # 文件保持外部修改后的内容，未被本次编辑覆盖
    assert target.read_text(encoding="utf-8") == "alpha = 1\nbeta = 2\ngamma = 3\n"


def test_write_file_detects_external_modification_before_replace(tmp_path, monkeypatch):
    """M4：write_file 在写入前发现文件被外部创建/修改时中止。"""
    import core.tools as tools_mod
    from core.tools import write_file

    monkeypatch.chdir(tmp_path)

    original_mkstemp = tools_mod.tempfile.mkstemp

    def racing_mkstemp(*args, **kwargs):
        # 模拟并发写入：目标文件在检查后、os.replace 前被外部创建
        (tmp_path / "new.txt").write_text("external", encoding="utf-8")
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tools_mod.tempfile, "mkstemp", racing_mkstemp)

    result = write_file("new.txt", "mine")

    assert result.startswith("🚨")
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "external"


def test_smart_replace_all_replaces_every_high_score_block(tmp_path, monkeypatch):
    """M5：智能替换 replace_all=True 时替换所有高评分候选块。"""
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
    """M5：最佳候选综合评分不足 0.7 时拒绝瞎猜替换，要求显式 target。"""
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
    # 文件未被改动
    assert target.read_text(encoding="utf-8") == "alpha_keep\nbeta\nbeta\ngamma\n"


def test_edit_file_write_failure_does_not_pollute_undo_stack(tmp_path, monkeypatch):
    """Low：写入失败（os.replace 异常）时撤销栈不被提前污染。"""
    import core.memory as memory_module
    from core.tools import edit_file

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "undo2.py"
    target.write_text("alpha\n", encoding="utf-8")
    captured = []
    monkeypatch.setattr(
        memory_module,
        "set_undo_state",
        lambda file_path, content: captured.append((file_path, content)),
    )

    def failing_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.tools.os.replace", failing_replace)

    result = edit_file("undo2.py", "alpha", "beta")

    assert result.startswith("🚨")
    assert captured == []
    assert target.read_text(encoding="utf-8") == "alpha\n"


def test_search_code_respects_cancel_event(tmp_path, monkeypatch):
    """M6：search_code 在取消后立即返回，不再阻塞等待 ripgrep。"""
    import core.tools as tools_mod

    monkeypatch.chdir(tmp_path)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.search_code("anything", cancel_event=cancel)

    assert result.startswith("🛑")


def test_glob_files_respects_cancel_event(tmp_path, monkeypatch):
    """M6：glob_files 在取消后立即返回。"""
    import core.tools as tools_mod

    monkeypatch.chdir(tmp_path)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.glob_files("*.py", cancel_event=cancel)

    assert result.startswith("🛑")


def test_fetch_url_respects_cancel_event(monkeypatch):
    """M6：fetch_url 在发起请求前检查取消。"""
    import core.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_check_ssrf_url", lambda url: None)
    cancel = threading.Event()
    cancel.set()

    result = tools_mod.fetch_url("https://example.com/", cancel_event=cancel)

    assert result.startswith("🛑")


def test_search_news_clamps_num_results(monkeypatch):
    """Low：search_news 的 num_results 钳制上限，防止模型传超大值触发限流。"""
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
    """Low：glob_files 的 rg 超时与 search_code 统一为 SEARCH_CODE_TIMEOUT_SECONDS。"""
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
    """M7：审批等待超过独立上限（300s）即终止任务，不再无限悬挂。"""
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
    """M7：后台看门狗同样执行审批等待上限。"""
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
    """M8：后台任务启动回显中的内联凭据被脱敏。"""
    monkeypatch.setattr(
        "core.task_manager.start_background_task",
        lambda command, session_id="": "abc12345",
    )

    result = execute_command(
        "export TOKEN=sk-abc123def456 && npm run dev",
        run_in_background=True,
    )

    assert "sk-abc123def456" not in result
    assert "***" in result


def test_manage_task_list_redacts_inline_credentials(monkeypatch):
    """M8：manage_task list 返回的快照 JSON 不含命令字段，内联凭据无回显通道。"""
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


# ---------------------------------------------------------------------------
# 阶段 B：后台任务 revision 快照 + manage_task(wait) 条件等待
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_task_manager(tmp_path, monkeypatch):
    """隔离 task_manager 全局状态：日志目录/任务库指向 tmp_path，任务表清空。"""
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
    """构造经系统 shell 执行的 venv python 短脚本命令（解释器路径加双引号）。"""
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
    # 终态任务保留在快照中，但不出现在 running 列表里
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
    snap = tm.wait_task_change(tid, rev, 10)  # revision 未超过 after 也应立即返回
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

        # 等待中途置位同样打断，不伪装超时
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
    """后台命令任务归属：发起回合的线程绑定优先于进程级"UI 当前查看的会话"。

    回归：GUI 草稿会话首条消息流程不更新进程级 current_session，旧实现兜底
    取它导致 A 会话发起的后台命令总结被误注入 B 会话（B 是切换前真实会话）。
    """
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
    """无线程绑定时（CLI 主流程）保持原行为：兜底进程级当前会话。"""
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
        assert "changed" not in status_payload  # status 省略 changed 键（wait 必含）

        list_payload = json.loads(manage_task("list"))
        entry = next(t for t in list_payload["tasks"] if t["task_id"] == tid)
        assert entry["status"] == "running"
        assert entry["terminal"] is False
    finally:
        tm.kill_background_task(tid)

    # 仅剩终态任务时保持旧文案
    assert manage_task("list") == "ℹ 当前没有任何运行中的后台异步任务。"


def test_manage_task_wait_validates_arguments(isolated_task_manager):
    from core.tools import manage_task

    tm = isolated_task_manager
    tid = tm.start_background_task(_py_cmd("import time; time.sleep(30)"))
    try:
        assert manage_task("wait", after_revision=0).startswith("🚨")
        # 缺省 after_revision 不再报错：按 0 处理（首次等待），无输出时超时返回
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
    """P0-2：shell wait 缺省 after_revision 按 0 处理，阻塞到首条输出出现。"""
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
    """构造隔离的 SubagentManager 并替换模块单例，供 manage_task 桥接测试使用。"""
    import core.tools as tools_module

    manager = SubagentManager(runner=runner, wait_interval=0.02)
    monkeypatch.setattr(tools_module, "subagent_manager", manager)
    return manager


def test_manage_task_status_and_wait_route_subagent(monkeypatch):
    """P0-1：subagent-* ID 路由到 SubagentManager，wait 终态返回 result 摘要。"""
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
        # run() 返回只代表启动线程已提交，工作线程置 running 是异步的，需轮询
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
        assert "changed" not in status_payload  # status 省略 changed 键（wait 必含）

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
    """子代理 wait 不要求 after_revision（传入则忽略）；未知 ID 返回未找到。"""
    from core.tools import manage_task

    manager = _bridge_manager(monkeypatch, lambda _record: "完成")
    assert manage_task("wait", task_id="subagent-00000000", timeout_seconds=1).startswith("🚨")
    assert manage_task(
        "wait", task_id="subagent-00000000", after_revision=0, timeout_seconds=1
    ).startswith("🚨")
    assert manage_task("status", task_id="subagent-00000000").startswith("🚨")


def test_manage_task_list_merges_shell_and_subagent(isolated_task_manager, monkeypatch):
    """list 合并展示两类运行中任务，kind 字段区分 shell 与 subagent。"""
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
        assert "result" not in sub_entry  # list 不附结果，收割走 status/wait
    finally:
        release.set()
        manager.shutdown(timeout=5)
        tm.kill_background_task(shell_tid)


def test_manage_task_kill_subagent_uses_cooperative_cancel(monkeypatch):
    """kill 子代理走协作式 cancel 且保留用户确认；拒绝路径不触碰任务。"""
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
        # 拒绝路径：不弹确认通过，任务保持运行
        monkeypatch.setattr("core.llm.check_permission_and_ask", lambda *a, **k: False)
        assert manage_task("kill", task_id=task_id).startswith("🚨 用户拒绝")
        assert manager.task_snapshot(task_id).status == "running"

        # 批准路径：协作式取消进入 cancelled 终态
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
        # 无任何可观察变化时指纹稳定
        assert tm.background_tasks_fingerprint() == fp_running
    finally:
        tm.kill_background_task(tid)
    # kill 置 cancelled + bump revision 后指纹变化
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
    """plan_finish 只接受本回合 active 接管项；其余一律拒绝。"""
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


    # 错误 turn：接管租约绑定 turn-1
    wrong_turn = plan_finish(
        "active", "done", parent_turn_id="turn-2",
    )
    assert wrong_turn.startswith("🚨")
    # queued 项拒绝
    queued = plan_finish("queued", "done", parent_turn_id="turn-1")
    assert queued.startswith("🚨")
    # 未接管的普通项拒绝
    plain = plan_finish("plain", "done", parent_turn_id="turn-1")
    assert plain.startswith("🚨")
    # 缺少运行上下文拒绝
    no_turn = plan_finish("active", "done", parent_turn_id=None)
    assert no_turn.startswith("🚨")

    items = {item["id"]: item for item in plan.snapshot()}
    assert items["active"]["status"] == "in_progress"
    assert items["queued"]["status"] == "in_progress"
    assert items["plain"]["status"] == "pending"


def test_plan_finish_schema_hides_runtime_context():
    """plan_finish 模型可见 schema 只暴露 item_id/result，运行上下文由 adapter 注入。"""
    from core.tool_registry import build_default_registry

    schema = (
        build_default_registry().get("plan_finish")
        .api_schema()["function"]["parameters"]
    )
    assert set(schema["properties"]) == {"item_id", "result"}
    assert set(schema["required"]) == {"item_id", "result"}


def test_run_subagent_rejects_oversized_description(monkeypatch):
    """task_description 超过 6000 字符：明确失败，不截断、不静默改写。"""
    import core.tools as tools_module

    result = tools_module.run_subagent(
        "x" * 6001, parent_session="s", parent_turn_id="t",
    )
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "6000" in payload["error"]


def test_run_subagent_enforces_foreground_hard_timeout(monkeypatch):
    """前台子代理硬上限 600 秒：内部调用传 601 返回参数错误，不启动子代理。"""
    import core.tools as tools_module

    class UnusedManager:
        def run(self, *args, **kwargs):
            raise AssertionError("前台硬上限拦截后不得启动子代理")

    monkeypatch.setattr(tools_module, "subagent_manager", UnusedManager())
    result = tools_module.run_subagent(
        "长任务", parent_session="s", parent_turn_id="t", timeout_seconds=601,
    )
    payload = json.loads(result)
    assert payload["status"] == "failed"
    assert "600" in payload["error"]
    assert "manage_task" in payload["error"]


def test_internal_run_subagent_keeps_explicit_foreground_timeout(monkeypatch):
    """内部 Python 调用仍可在硬上限内显式选择时间预算。"""
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
    """后台模式保留 3600 秒上限：确有长任务时不会误伤后台工作流。"""
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
