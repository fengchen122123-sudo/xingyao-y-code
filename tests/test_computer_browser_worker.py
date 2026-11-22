import sys
import time
from pathlib import Path

import pytest

from core.computer.browser_backend import BrowserComputerBackend, WORKER_ARGV_FLAG
from core.computer.models import ComputerAction, ComputerControlError

_STUB = Path(__file__).parent / "browser_worker_stub.py"


def _command(mode: str, tmp_path: Path) -> list[str]:
    pid_file = tmp_path / f"pid-{mode}.txt"
    return [
        sys.executable,
        str(_STUB),
        "--mode",
        mode,
        "--pid-file",
        str(pid_file),
    ]


def _pid_alive(pid: int, timeout: float = 1.0) -> bool:
    import psutil

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if not psutil.Process(pid).is_running():
                return False
        except psutil.NoSuchProcess:
            return False
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    try:
        return psutil.Process(pid).is_running()
    except psutil.NoSuchProcess:
        return False


def _backend(tmp_path, mode, *, command_timeout=5.0, guard=0.4, start_timeout=15.0):
    return BrowserComputerBackend(
        "edge",
        tmp_path / "profile",
        use_process_worker=True,
        worker_command=_command(mode, tmp_path),
        command_timeout=command_timeout,
        process_guard_slack=guard,
        start_timeout=start_timeout,
    )


def _pid_from(tmp_path, mode) -> int:
    return int((tmp_path / f"pid-{mode}.txt").read_text(encoding="ascii"))


def test_frozen_default_worker_command_uses_argv_dispatch(monkeypatch, tmp_path):
    backend = BrowserComputerBackend("edge", tmp_path / "profile", use_process_worker=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/dist/ycode.exe")
    command = backend._default_worker_command()
    assert command[:2] == ["C:/dist/ycode.exe", WORKER_ARGV_FLAG]
    assert "-m" not in command


def test_process_worker_roundtrip(tmp_path):
    backend = _backend(tmp_path, "normal")
    backend.start()
    try:
        observation = backend.observe("run-a")
        assert observation.page_origin == "https://stub.example"
        assert observation.surface_id == "page-1"
        assert observation.surface_generation == 1
        backend.execute(
            "run-a", ComputerAction(kind="click", target="browser", x=10, y=10)
        )
        assert backend.is_reusable()
    finally:
        backend.close()
    assert not backend.is_reusable()
    assert not _pid_alive(_pid_from(tmp_path, "normal"))


def test_process_worker_command_timeout_seals_and_hard_stop_kills(tmp_path):
    backend = _backend(tmp_path, "hang_command", command_timeout=0.4, guard=0.3)
    backend.start()
    try:
        started = time.perf_counter()
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert time.perf_counter() - started < 2.0
        assert raised.value.code == "computer_action_timeout"
        assert "结果未知" in raised.value.message
        admission_started = time.perf_counter()
        with pytest.raises(ComputerControlError) as sealed:
            backend.observe("run-b")
        assert time.perf_counter() - admission_started < 0.2
        assert sealed.value.code == "computer_backend_error"
        assert not backend.is_reusable()

        close_started = time.perf_counter()
        backend.close()
        close_total = time.perf_counter() - close_started
        assert close_total < 6.0, f"close 耗时 {close_total:.2f}s 超出有界预期"
        pid = _pid_from(tmp_path, "hang_command")
        assert not _pid_alive(pid), "close 后受管执行进程仍存活"
    finally:
        backend.close()


def test_process_worker_boot_timeout_releases_profile(tmp_path):
    backend = _backend(tmp_path, "hang_boot", start_timeout=0.8)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_backend_start_failed"
    retry = _backend(tmp_path, "normal")
    retry.start()
    retry.close()


def test_process_worker_close_without_exit_terminates(tmp_path):
    backend = _backend(tmp_path, "no_exit_on_close")
    backend.start()
    close_started = time.perf_counter()
    backend.close()
    assert time.perf_counter() - close_started < 6.0
    assert not _pid_alive(_pid_from(tmp_path, "no_exit_on_close"))


def test_process_worker_crash_fails_pending_and_reusable(tmp_path):
    backend = _backend(tmp_path, "crash_after_first")
    backend.start()
    try:
        backend.observe("run-a")
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-b")
        assert raised.value.code == "computer_browser_closed"
        assert not backend.is_reusable()
    finally:
        backend.close()


def test_process_worker_profile_registry_rejects_second_instance(tmp_path):
    backend = _backend(tmp_path, "normal")
    backend.start()
    try:
        second = _backend(tmp_path, "normal")
        with pytest.raises(ComputerControlError) as raised:
            second.start()
        assert raised.value.code == "computer_profile_locked"
    finally:
        backend.close()
