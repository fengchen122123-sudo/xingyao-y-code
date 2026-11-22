import subprocess

import pytest

import ycode_setup
from ycode_setup import SetupWizard, schedule_installer_self_cleanup


@pytest.fixture
def installer(tmp_path):
    path = tmp_path / "Y-CODE-Setup-v1.2.6.exe"
    path.write_bytes(b"MZ" + b"\x00" * 32)
    return path


def _capture_popen(monkeypatch):
    calls = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))

    monkeypatch.setattr(ycode_setup.subprocess, "Popen", _FakePopen)
    return calls


def test_schedule_cleanup_builds_detached_delayed_delete_command(installer, monkeypatch):
    calls = _capture_popen(monkeypatch)

    assert schedule_installer_self_cleanup(str(installer)) is True

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == f'cmd /c ping -n 3 127.0.0.1 >nul & del /f /q "{installer}"'
    assert kwargs["shell"] is True
    assert kwargs["close_fds"] is True
    expected_flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    )
    assert kwargs["creationflags"] == expected_flags
    assert installer.exists()


@pytest.mark.parametrize("name", ["python.exe", "evil.bat", "Y-CODE-Setup-v1.2.6.txt"])
def test_schedule_cleanup_rejects_non_whitelisted_names(tmp_path, monkeypatch, name):
    calls = _capture_popen(monkeypatch)
    path = tmp_path / name
    path.write_bytes(b"MZ")

    assert schedule_installer_self_cleanup(str(path)) is False
    assert calls == []
    assert path.exists()


def test_schedule_cleanup_rejects_missing_file(tmp_path, monkeypatch):
    calls = _capture_popen(monkeypatch)

    assert schedule_installer_self_cleanup(str(tmp_path / "Y-CODE-Setup-v1.2.6.exe")) is False
    assert schedule_installer_self_cleanup("") is False
    assert calls == []


def _bare_wizard(**attrs):
    wizard = SetupWizard.__new__(SetupWizard)
    wizard._install_success = True
    wizard._installer_cleanup_scheduled = False
    wizard.startupinfo = None
    for key, value in attrs.items():
        setattr(wizard, key, value)
    return wizard


def test_maybe_schedule_skips_when_install_not_successful(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        ycode_setup, "schedule_installer_self_cleanup",
        lambda *args, **kwargs: scheduled.append(args) or True,
    )
    wizard = _bare_wizard(_install_success=False)

    wizard._maybe_schedule_installer_cleanup()

    assert scheduled == []
    assert wizard._installer_cleanup_scheduled is False


def test_maybe_schedule_skips_when_not_frozen(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        ycode_setup, "schedule_installer_self_cleanup",
        lambda *args, **kwargs: scheduled.append(args) or True,
    )
    monkeypatch.delattr(ycode_setup.sys, "frozen", raising=False)
    wizard = _bare_wizard()

    wizard._maybe_schedule_installer_cleanup()

    assert scheduled == []
    assert wizard._installer_cleanup_scheduled is False


def test_maybe_schedule_runs_exactly_once_when_frozen_and_successful(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        ycode_setup, "schedule_installer_self_cleanup",
        lambda *args, **kwargs: scheduled.append(args) or True,
    )
    monkeypatch.setattr(ycode_setup.sys, "frozen", True, raising=False)
    wizard = _bare_wizard()

    wizard._maybe_schedule_installer_cleanup()
    wizard._maybe_schedule_installer_cleanup()

    assert len(scheduled) == 1
    assert scheduled[0][0] == ycode_setup.sys.executable
    assert wizard._installer_cleanup_scheduled is True
