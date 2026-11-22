import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_lifecycle as gui_lifecycle


@pytest.fixture
def record_popen_init(monkeypatch):
    calls = []

    def fake_init(self, *args, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", fake_init)
    return fake_init, calls


@pytest.fixture
def no_console(monkeypatch):
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: 0, raising=True)


def test_no_console_wraps_popen_with_create_no_window(record_popen_init, no_console):
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True
    assert subprocess.Popen.__init__ is not fake_init

    subprocess.Popen(["cmd"])

    assert len(calls) == 1
    assert calls[0]["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_explicit_creationflags_are_or_ed(record_popen_init, no_console):
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True

    subprocess.Popen(["cmd"], creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert flags & subprocess.CREATE_NO_WINDOW


def test_has_console_leaves_popen_untouched(record_popen_init, monkeypatch):
    fake_init, _calls = record_popen_init
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: 12345, raising=True)

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init
    assert not getattr(fake_init, "_ycode_hides_console", False)


def test_unknown_console_state_leaves_popen_untouched(record_popen_init, monkeypatch):
    fake_init, _calls = record_popen_init
    monkeypatch.setattr(gui_lifecycle, "_console_window_handle", lambda: None, raising=True)

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init


def test_repeated_calls_do_not_double_wrap(record_popen_init, no_console):
    fake_init, calls = record_popen_init

    assert gui_lifecycle.hide_child_console_windows() is True
    first_wrapper = subprocess.Popen.__init__
    assert getattr(first_wrapper, "_ycode_hides_console", False) is True

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is first_wrapper
    assert getattr(first_wrapper, "__wrapped__", None) is fake_init

    subprocess.Popen(["cmd"])
    assert len(calls) == 1
    assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_non_windows_is_noop(record_popen_init, monkeypatch):
    import types

    fake_init, _calls = record_popen_init
    monkeypatch.setattr(
        gui_lifecycle, "os", types.SimpleNamespace(name="posix"), raising=True
    )

    assert gui_lifecycle.hide_child_console_windows() is False
    assert subprocess.Popen.__init__ is fake_init
