"""R2 第二段：受管浏览器执行进程（父进程侧边界）测试。

使用 tests/browser_worker_stub.py 作为桩子进程——纯 JSON 协议回声，绝不启动
真实 Playwright/浏览器、绝不产生键鼠副作用；硬终止计时在受管子进程上验证
（修复指南 6.2：不用真实点击验证"杀得够不够快"）。真实浏览器的进程模式
生命周期由 scripts/audit_computer_live.py 的 --browser-process 探针验收。
"""

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


def _pid_alive(pid: int) -> bool:
    import psutil

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
    """进程模式完整生命周期：ready → observe/execute → reusable → close。"""
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
    """卡死命令：守卫超时 → 结果未知封闭 → admission 立即关闭 → close 硬终止。"""
    backend = _backend(tmp_path, "hang_command", command_timeout=0.4, guard=0.3)
    backend.start()
    try:
        started = time.perf_counter()
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-a")
        assert time.perf_counter() - started < 2.0
        assert raised.value.code == "computer_action_timeout"
        assert "结果未知" in raised.value.message
        # 封闭后 admission 立即拒绝新命令（远小于守卫超时）。
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
    """启动挂死：start 超时显式失败，profile 执行权释放（可重试）。"""
    backend = _backend(tmp_path, "hang_boot", start_timeout=0.8)
    with pytest.raises(ComputerControlError) as raised:
        backend.start()
    assert raised.value.code == "computer_backend_start_failed"
    # profile 注册表已释放：同 profile 可再次启动（换 normal 桩）。
    retry = _backend(tmp_path, "normal")
    retry.start()
    retry.close()


def test_process_worker_close_without_exit_terminates(tmp_path):
    """子进程假意收尾但不退出：close 仍按有界流程硬终止并确认退出。"""
    backend = _backend(tmp_path, "no_exit_on_close")
    backend.start()
    close_started = time.perf_counter()
    backend.close()
    assert time.perf_counter() - close_started < 6.0
    assert not _pid_alive(_pid_from(tmp_path, "no_exit_on_close"))


def test_process_worker_crash_fails_pending_and_reusable(tmp_path):
    """子进程崩溃：在飞请求归一为 computer_browser_closed，实例不可复用。"""
    backend = _backend(tmp_path, "crash_after_first")
    backend.start()
    try:
        backend.observe("run-a")  # 第一发正常
        with pytest.raises(ComputerControlError) as raised:
            backend.observe("run-b")
        assert raised.value.code == "computer_browser_closed"
        assert not backend.is_reusable()
    finally:
        backend.close()


def test_process_worker_profile_registry_rejects_second_instance(tmp_path):
    """同父进程同 profile 双实例：第二个 start 快速失败（进程内注册表）。"""
    backend = _backend(tmp_path, "normal")
    backend.start()
    try:
        second = _backend(tmp_path, "normal")
        with pytest.raises(ComputerControlError) as raised:
            second.start()
        assert raised.value.code == "computer_profile_locked"
    finally:
        backend.close()
