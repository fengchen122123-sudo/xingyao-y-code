# -*- coding: utf-8 -*-
"""publishDiagnostics 的节流合批语义。"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide.diagnostics_batcher import DEFAULT_THROTTLE_MS, DiagnosticsBatcher


class FakeTimer:
    """可手动触发的定时器，让节流窗口在测试里完全确定。"""

    instances = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.callback()


def _diag(message):
    return [{
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}},
        "severity": 1,
        "message": message,
        "source": "fake",
    }]


def _batcher():
    FakeTimer.instances = []
    flushes = []
    batcher = DiagnosticsBatcher(
        on_flush=lambda path, diagnostics: flushes.append((path, diagnostics)),
        timer_factory=FakeTimer,
    )
    return batcher, flushes


def test_default_window_is_200ms():
    batcher, _ = _batcher()
    assert batcher.interval_ms == DEFAULT_THROTTLE_MS == 200
    batcher.push("E:\\ws\\a.py", _diag("x"))
    assert FakeTimer.instances[-1].interval == 0.2


def test_consecutive_pushes_are_coalesced_into_one_flush():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("第一次"))
    batcher.push("E:\\ws\\a.py", _diag("第二次"))
    batcher.push("E:\\ws\\a.py", _diag("第三次"))
    assert flushes == []
    FakeTimer.instances[-1].fire()
    assert len(flushes) == 1
    assert flushes[0][1][0]["message"] == "第三次"


def test_unchanged_diagnostics_are_not_flushed_twice():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    assert len(flushes) == 1
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    # 内容没变就不该再打扰前端，否则连续击键会把 evaluate_js 队列打满
    assert len(flushes) == 1


def test_changed_diagnostics_are_flushed_again():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("旧"))
    batcher.flush()
    batcher.push("E:\\ws\\a.py", _diag("新"))
    batcher.flush()
    assert [f[1][0]["message"] for f in flushes] == ["旧", "新"]


def test_empty_diagnostics_report_clears_previous():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("有错"))
    batcher.flush()
    batcher.push("E:\\ws\\a.py", [])
    batcher.flush()
    assert flushes[-1][1] == []


def test_different_paths_are_batched_independently():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("a"))
    batcher.push("E:\\ws\\b.py", _diag("b"))
    batcher.flush()
    assert sorted(path for path, _ in flushes) == ["E:\\ws\\a.py", "E:\\ws\\b.py"]


def test_discard_forgets_path_state():
    batcher, flushes = _batcher()
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    batcher.discard("E:\\ws\\a.py")
    batcher.push("E:\\ws\\a.py", _diag("一样"))
    batcher.flush()
    # 关闭再打开同一个文件，诊断必须原样再推一次
    assert len(flushes) == 2


def test_real_timer_fires_without_manual_flush():
    flushes = []
    batcher = DiagnosticsBatcher(
        interval_ms=10,
        on_flush=lambda path, diagnostics: flushes.append(path),
    )
    batcher.push("E:\\ws\\a.py", _diag("自动"))
    deadline = threading.Event()
    deadline.wait(2.0)
    assert flushes == ["E:\\ws\\a.py"]
