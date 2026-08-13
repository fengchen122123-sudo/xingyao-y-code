from core.gui_lifecycle import ApplicationLifecycle

import time


class FakeWindow:
    def __init__(self):
        self.hidden = 0
        self.shown = 0
        self.destroyed = 0

    def hide(self):
        self.hidden += 1

    def show(self):
        self.shown += 1

    def restore(self):
        pass

    def destroy(self):
        self.destroyed += 1


class FakeTray:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def run_detached(self, setup=None):
        self.started += 1
        if setup is not None:
            setup(self)

    @property
    def visible(self):
        return True

    @visible.setter
    def visible(self, value):
        pass

    def stop(self):
        self.stopped += 1


def _wait_for(predicate, timeout=5.0):
    """exit_application 的 stop+destroy 序列在独立线程执行，测试轮询等待完成。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_close_hides_to_ready_tray_and_tray_exit_is_idempotent():
    window = FakeWindow()
    tray = FakeTray()
    shutdown_calls = []
    lifecycle = ApplicationLifecycle(
        window,
        lambda: shutdown_calls.append("shutdown"),
        tray_factory=lambda show, exit_app: tray,
    )

    assert lifecycle.start_tray() is True
    assert lifecycle.on_closing() is False
    assert window.hidden == 1

    lifecycle.show_window()
    lifecycle.exit_application()
    lifecycle.exit_application()

    assert window.shown == 1
    assert _wait_for(lambda: window.destroyed == 1)
    assert shutdown_calls == ["shutdown"]
    assert tray.stopped == 1


def test_tray_start_failure_never_hides_the_only_window():
    lifecycle = ApplicationLifecycle(
        FakeWindow(),
        lambda: None,
        tray_factory=lambda *args: (_ for _ in ()).throw(RuntimeError("no tray")),
    )

    assert lifecycle.start_tray() is False
    assert lifecycle.on_closing() is True


def test_finalize_after_native_loop_runs_shutdown_once_without_destroying_again():
    window = FakeWindow()
    shutdown_calls = []
    lifecycle = ApplicationLifecycle(
        window,
        lambda: shutdown_calls.append("shutdown"),
        tray_factory=lambda *args: FakeTray(),
    )
    lifecycle.start_tray()

    lifecycle.finalize()
    lifecycle.finalize()

    assert shutdown_calls == ["shutdown"]
    assert window.destroyed == 0
