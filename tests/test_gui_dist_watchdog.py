"""gui_main 前端产物运行期变更监控（dist watchdog）单元测试。

背景：运行中的 WebView2 页面已按旧构建哈希加载 chunk，重新构建
frontend/dist 后旧哈希 chunk 404（典型表现：mermaid「渲染器加载失败」）。
gui_main 新增 "gui-dist-watchdog" daemon 线程，以 dist/index.html 的
(int(mtime), size) 指纹感知运行期重建，复核确认构建完成后经 api.emit_event
推送 frontend_dist_updated 事件，由前端自愈刷新。

测试策略：采样脚本 / 停止事件 / emitter 全部注入，不真实等待、不依赖真实
dist 目录；真实采样路径与算法单独用 tmp_path 验证。
"""

import threading

import gui_main


class _FakeStopEvent:
    """最小停止事件替身：wait() 不真实等待，立即返回是否已收到停止请求。"""

    def __init__(self):
        self._stopped = False

    def set(self):
        self._stopped = True

    def is_set(self):
        return self._stopped

    def wait(self, timeout=None):
        return self._stopped


class _ScriptedSampler:
    """按脚本顺序返回采样值；最后一个值返回后置位停止事件，让循环自然退出。"""

    def __init__(self, values, stop_event):
        self._values = list(values)
        self._stop_event = stop_event
        self.calls = 0

    def __call__(self):
        value = self._values.pop(0)
        self.calls += 1
        if not self._values:
            self._stop_event.set()
        return value


def _run_loop(values):
    """用受控采样脚本跑一次核心循环，返回（推送记录, 采样器）。"""
    stop = _FakeStopEvent()
    sampler = _ScriptedSampler(values, stop)
    emitted = []
    gui_main._frontend_dist_watch_loop(
        sampler=sampler,
        emit=emitted.append,
        stop_event=stop,
        sleep_fn=lambda _seconds: None,
    )
    return emitted, sampler


def test_change_confirmed_pushes_exactly_once():
    """指纹变化 + 复核一致 → emit 恰好一次，payload 为 mtime:size。"""
    emitted, sampler = _run_loop([(100, 10), (100, 10), (200, 20), (200, 20)])

    assert emitted == ["200:20"]
    assert sampler.calls == 4


def test_change_still_moving_is_not_pushed():
    """变化后复核仍不一致（构建中间态）→ 不推送、不更新基线。"""
    emitted, _ = _run_loop([(100, 10), (100, 10), (200, 20), (300, 30)])

    assert emitted == []


def test_missing_file_is_not_pushed():
    """dist 不存在（指纹 None，启动基线也为 None）→ 不推送。"""
    emitted, _ = _run_loop([None, None])

    assert emitted == []


def test_unchanged_fingerprint_is_not_pushed():
    """指纹不变 → 不推送。"""
    emitted, _ = _run_loop([(100, 10), (100, 10), (100, 10)])

    assert emitted == []


def test_same_fingerprint_after_push_is_not_reported_again():
    """已推送过的指纹再次采样 → 基线机制去重，不重复推送。"""
    emitted, sampler = _run_loop(
        [(100, 10), (200, 20), (200, 20), (200, 20), (200, 20)]
    )

    assert emitted == ["200:20"]
    assert sampler.calls == 5


def test_baseline_none_then_file_appears_is_pushed():
    """启动时无 dist（基线 None），随后构建完成 → 视为有效变更推送。"""
    emitted, _ = _run_loop([None, (200, 20), (200, 20)])

    assert emitted == ["200:20"]


def test_change_to_missing_file_is_not_pushed():
    """基线存在、随后文件消失（重建清空中）→ 复核仍缺失，不推送。"""
    emitted, _ = _run_loop([(100, 10), (100, 10), None, None])

    assert emitted == []


def test_fingerprint_reads_dist_index(tmp_path, monkeypatch):
    """采样路径与算法：frontend/dist/index.html 的 (int(mtime), size)。"""
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(gui_main, "_get_base_dir", lambda: str(tmp_path))

    fingerprint = gui_main._frontend_dist_fingerprint()

    stat = index.stat()
    assert fingerprint == (int(stat.st_mtime), stat.st_size)


def test_fingerprint_none_when_dist_missing(tmp_path, monkeypatch):
    """dist 缺失时返回 None（不抛异常）。"""
    monkeypatch.setattr(gui_main, "_get_base_dir", lambda: str(tmp_path))

    assert gui_main._frontend_dist_fingerprint() is None


def test_start_watchdog_wires_daemon_thread_event_and_stop_handle(monkeypatch):
    """_start_frontend_dist_watchdog：线程名/daemon/事件名与 payload/停止句柄。"""
    captured = {}
    ran = threading.Event()

    def _fake_loop(**kwargs):
        captured["thread_name"] = threading.current_thread().name
        captured["daemon"] = threading.current_thread().daemon
        captured["stop_event"] = kwargs["stop_event"]
        kwargs["emit"]("123:456")
        ran.set()

    monkeypatch.setattr(gui_main, "_frontend_dist_watch_loop", _fake_loop)

    class _FakeApi:
        def __init__(self):
            self.events = []

        def emit_event(self, name, payload):
            self.events.append((name, payload))

    api = _FakeApi()
    stop_event = gui_main._start_frontend_dist_watchdog(api)

    assert ran.wait(5)
    assert captured["thread_name"] == "gui-dist-watchdog"
    assert captured["daemon"] is True
    assert api.events == [("frontend_dist_updated", {"fingerprint": "123:456"})]
    assert captured["stop_event"] is stop_event
    stop_event.set()
