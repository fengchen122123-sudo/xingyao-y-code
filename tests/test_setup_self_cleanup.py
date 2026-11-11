"""ycode_setup.py「安装成功才清理安装包」相关逻辑的单测。

背景（用户反馈的缺陷）：更新包下载好后，用户点「立即安装并重启」，
只要把安装向导关掉（未完成安装），安装包就会被删掉，只能重新下载。
修复后清理只在「安装成功 + 向导退出」这一条路径上发生。

覆盖要点：
  * schedule_installer_self_cleanup：命令构造、文件名白名单、缺文件拒绝；
  * SetupWizard._maybe_schedule_installer_cleanup：安装失败不调度、
    开发态(frozen 缺失)不调度、成功且打包态只调度一次。

本文件**不会真的删除任何文件**：只替换 subprocess.Popen 检查命令构造，
以及替换调度函数检查分支走向。
"""

import subprocess

import pytest

import ycode_setup
from ycode_setup import SetupWizard, schedule_installer_self_cleanup


@pytest.fixture
def installer(tmp_path):
    """造一个文件名合规的假安装包（不会被执行，只验证命令构造）。"""
    path = tmp_path / "Y-CODE-Setup-v1.2.6.exe"
    path.write_bytes(b"MZ" + b"\x00" * 32)
    return path


def _capture_popen(monkeypatch):
    """替换 subprocess.Popen，记录 (cmd, kwargs) 而不真的启动进程。"""
    calls = []

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            calls.append((cmd, kwargs))

    monkeypatch.setattr(ycode_setup.subprocess, "Popen", _FakePopen)
    return calls


def test_schedule_cleanup_builds_detached_delayed_delete_command(installer, monkeypatch):
    """合法安装包：调度 detached 命令，延时到向导退出后再删，本进程不动文件。"""
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
    # 真正的删除由外部命令执行，本进程不得动安装包
    assert installer.exists()


@pytest.mark.parametrize("name", ["python.exe", "evil.bat", "Y-CODE-Setup-v1.2.6.txt"])
def test_schedule_cleanup_rejects_non_whitelisted_names(tmp_path, monkeypatch, name):
    """白名单外的文件名一律不清理（防止误删用户文件或解释器本身）。"""
    calls = _capture_popen(monkeypatch)
    path = tmp_path / name
    path.write_bytes(b"MZ")

    assert schedule_installer_self_cleanup(str(path)) is False
    assert calls == []
    assert path.exists()


def test_schedule_cleanup_rejects_missing_file(tmp_path, monkeypatch):
    """安装包已不存在时不调度（也绝不为空路径构造删除命令）。"""
    calls = _capture_popen(monkeypatch)

    assert schedule_installer_self_cleanup(str(tmp_path / "Y-CODE-Setup-v1.2.6.exe")) is False
    assert schedule_installer_self_cleanup("") is False
    assert calls == []


def _bare_wizard(**attrs):
    """绕过 __init__（不创建 Tk 窗口）构造最小向导对象，仅用于验证调度分支。"""
    wizard = SetupWizard.__new__(SetupWizard)
    wizard._install_success = True
    wizard._installer_cleanup_scheduled = False
    wizard.startupinfo = None
    for key, value in attrs.items():
        setattr(wizard, key, value)
    return wizard


def test_maybe_schedule_skips_when_install_not_successful(monkeypatch):
    """用户中途取消 / 安装失败：安装包必须保留，不得调度任何清理。"""
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
    """开发态运行（sys.executable 是 python.exe）绝不调度，避免删掉解释器。"""
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
    """安装成功 + 打包态：只调度一次（点按钮与关窗口可能都触发）。"""
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
