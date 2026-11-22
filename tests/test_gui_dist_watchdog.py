import threading
from types import SimpleNamespace

import pytest

import gui_main


class _FakeStopEvent:

    def __init__(self):
        self._stopped = False

    def set(self):
        self._stopped = True

    def is_set(self):
        return self._stopped

    def wait(self, timeout=None):
        return self._stopped


class _ScriptedSampler:

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
    emitted, sampler = _run_loop([(100, 10), (100, 10), (200, 20), (200, 20)])

    assert emitted == ["200:20"]
    assert sampler.calls == 4


def test_change_still_moving_is_not_pushed():
    emitted, _ = _run_loop([(100, 10), (100, 10), (200, 20), (300, 30)])

    assert emitted == []


def test_missing_file_is_not_pushed():
    emitted, _ = _run_loop([None, None])

    assert emitted == []


def test_unchanged_fingerprint_is_not_pushed():
    emitted, _ = _run_loop([(100, 10), (100, 10), (100, 10)])

    assert emitted == []


def test_same_fingerprint_after_push_is_not_reported_again():
    emitted, sampler = _run_loop(
        [(100, 10), (200, 20), (200, 20), (200, 20), (200, 20)]
    )

    assert emitted == ["200:20"]
    assert sampler.calls == 5


def test_baseline_none_then_file_appears_is_pushed():
    emitted, _ = _run_loop([None, (200, 20), (200, 20)])

    assert emitted == ["200:20"]


def test_change_to_missing_file_is_not_pushed():
    emitted, _ = _run_loop([(100, 10), (100, 10), None, None])

    assert emitted == []


def test_fingerprint_reads_dist_index(tmp_path, monkeypatch):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    index = dist / "index.html"
    index.write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(gui_main, "_get_base_dir", lambda: str(tmp_path))

    fingerprint = gui_main._frontend_dist_fingerprint()

    stat = index.stat()
    assert fingerprint == (int(stat.st_mtime), stat.st_size)


def test_fingerprint_none_when_dist_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_main, "_get_base_dir", lambda: str(tmp_path))

    assert gui_main._frontend_dist_fingerprint() is None


def test_missing_frontend_alerts_natively_before_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_main, "_get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(gui_main, "is_port_open", lambda *args, **kwargs: False)
    alerts = []
    monkeypatch.setattr(
        gui_main, "_frontend_missing_dialog", lambda message: alerts.append(message) or True
    )

    with pytest.raises(SystemExit) as excinfo:
        gui_main.get_frontend_url()

    assert excinfo.value.code == 1
    assert len(alerts) == 1, "必须先弹出原生提示再退出"
    message = alerts[0]
    assert "frontend" in message and "dist" in message and "index.html" in message
    assert str(tmp_path) in message
    assert "重新安装" in message and "重新构建" in message


def test_frontend_missing_dialog_falls_back_to_stderr_on_non_windows(monkeypatch, capsys):
    monkeypatch.setattr(gui_main.sys, "platform", "linux")

    assert gui_main._frontend_missing_dialog("前端产物缺失：请重新构建") is False
    assert "前端产物缺失：请重新构建" in capsys.readouterr().err


def test_frontend_missing_dialog_uses_native_messagebox_on_windows(monkeypatch):
    import ctypes

    calls = []
    monkeypatch.setattr(gui_main.sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(
            user32=SimpleNamespace(MessageBoxW=lambda *args: calls.append(args) or 1)
        ),
        raising=False,
    )

    assert gui_main._frontend_missing_dialog("前端产物缺失") is True

    assert len(calls) == 1
    _hwnd, text, title, flags = calls[0]
    assert text == "前端产物缺失"
    assert "启动失败" in title
    assert flags & 0x10
    assert flags & 0x00040000


def test_start_watchdog_wires_daemon_thread_event_and_stop_handle(monkeypatch):
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
