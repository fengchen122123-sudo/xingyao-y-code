import json
import subprocess
import sys
import pytest

import config


def _use_tmp_config(tmp_path, monkeypatch, data):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    return cfg


def test_windows_shell_options_disable_interpreters_that_are_not_installed(monkeypatch):
    from core.command_shell import list_command_shell_options

    discovered = {
        "cmd.exe": r"C:\Windows\System32\cmd.exe",
        "powershell.exe": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    }
    monkeypatch.setattr("core.command_shell.shutil.which", discovered.get)

    options = list_command_shell_options(platform="win32")

    assert [(item["id"], item["available"]) for item in options] == [
        ("auto", True),
        ("pwsh", False),
        ("powershell", True),
        ("cmd", True),
    ]
    assert options[1]["unavailable_reason"] == "未安装"
    assert options[2]["executable"] == discovered["powershell.exe"]


def test_codex_private_powershell_is_not_exposed_as_a_ycode_installation(monkeypatch):
    from core.command_shell import list_command_shell_options

    private_pwsh = (
        r"C:\Users\demo\.cache\codex-runtimes\codex-primary-runtime"
        r"\dependencies\native\powershell\pwsh.exe"
    )
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: private_pwsh if name == "pwsh.exe" else None,
    )

    options = list_command_shell_options(platform="win32")

    pwsh = next(item for item in options if item["id"] == "pwsh")
    assert pwsh["available"] is False
    assert pwsh["executable"] == ""


def test_unavailable_shell_cannot_be_saved(tmp_path, monkeypatch):
    from core.command_shell import CommandShellUnavailableError, set_selected_command_shell

    cfg = _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    monkeypatch.setattr("core.command_shell.shutil.which", lambda _name: None)

    with pytest.raises(CommandShellUnavailableError, match="未安装"):
        set_selected_command_shell("pwsh", platform="win32")

    assert json.loads(cfg.read_text(encoding="utf-8"))["command_shell"] == "cmd"


def test_selected_powershell_builds_an_explicit_non_shell_invocation(tmp_path, monkeypatch):
    from core.command_shell import build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "powershell"})
    executable = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: executable if name == "powershell.exe" else None,
    )

    invocation = build_command_invocation("Get-Date", platform="win32")

    assert invocation.argv == [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "Get-Date",
    ]
    assert invocation.use_system_shell is False
    assert invocation.shell_id == "powershell"


def test_missing_saved_shell_fails_instead_of_falling_back(tmp_path, monkeypatch):
    from core.command_shell import CommandShellUnavailableError, build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "pwsh"})
    monkeypatch.setattr("core.command_shell.shutil.which", lambda _name: None)

    with pytest.raises(CommandShellUnavailableError, match=r"PowerShell 7\+"):
        build_command_invocation("Get-Date", platform="win32")


def test_missing_saved_shell_allows_a_structured_installed_interpreter_override(tmp_path, monkeypatch):
    from core.command_shell import build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "pwsh"})
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: powershell if name == "powershell.exe" else None,
    )

    invocation = build_command_invocation(
        "Get-Date",
        platform="win32",
        shell_id="powershell",
    )

    assert invocation.argv[0] == powershell
    assert invocation.argv[-1] == "Get-Date"
    assert invocation.use_system_shell is False
    assert invocation.shell_id == "powershell"


def test_missing_saved_shell_does_not_trust_an_interpreter_shaped_command(tmp_path, monkeypatch):
    from core.command_shell import CommandShellUnavailableError, build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "pwsh"})
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: powershell if name == "powershell.exe" else None,
    )

    with pytest.raises(CommandShellUnavailableError, match=r"PowerShell 7\+"):
        build_command_invocation(
            'powershell.exe -Command "Get-Date" & echo cmd-tail',
            platform="win32",
        )


def test_auto_preserves_the_platform_default_shell(tmp_path, monkeypatch):
    from core.command_shell import build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {})

    invocation = build_command_invocation("echo hello", platform="win32")

    assert invocation.argv == "echo hello"
    assert invocation.use_system_shell is True
    assert invocation.shell_id == "auto"


def test_selected_cmd_preserves_the_raw_command_line(tmp_path, monkeypatch):
    from core.command_shell import build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    executable = r"C:\Windows\System32\cmd.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: executable if name == "cmd.exe" else None,
    )

    command = r'"C:\Windows\System32\where.exe" cmd.exe'
    invocation = build_command_invocation(command, platform="win32")

    assert invocation.argv == command
    assert invocation.use_system_shell is True
    assert invocation.executable == executable


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows cmd.exe")
def test_selected_cmd_executes_a_quoted_path_without_mangling(tmp_path, monkeypatch):
    from core.command_shell import build_command_invocation

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    command = r'"C:\Windows\System32\where.exe" cmd.exe'
    invocation = build_command_invocation(command, platform="win32")

    completed = subprocess.run(
        invocation.argv,
        shell=invocation.use_system_shell,
        executable=invocation.executable,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "cmd.exe" in completed.stdout.lower()


def test_execute_command_uses_the_selected_interpreter(tmp_path, monkeypatch):
    from core.tools import execute_command

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "powershell"})
    executable = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: executable if name == "powershell.exe" else None,
    )
    calls = []

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        kwargs["stdout"].write(b"ok")
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)

    result = execute_command("Get-Date")

    assert result == "ok"
    assert calls[0][0][0][-1] == "Get-Date"
    assert calls[0][0][0][0] == executable
    assert calls[0][1]["shell"] is False


def test_execute_command_reports_a_missing_selected_interpreter(tmp_path, monkeypatch):
    from core.tools import execute_command

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "pwsh"})
    monkeypatch.setattr("core.command_shell.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "core.tools.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    result = execute_command("Get-Date")

    assert result.startswith("🚨")
    assert "PowerShell 7+" in result
    assert "未安装" in result


def test_execute_command_accepts_a_structured_shell_override(tmp_path, monkeypatch):
    from core.tools import execute_command

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "pwsh"})
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: powershell if name == "powershell.exe" else None,
    )
    calls = []

    class FakeProcess:
        returncode = 0

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        kwargs["stdout"].write(b"ok")
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)

    result = execute_command("Get-Date", shell_id="powershell")

    assert result == "ok"
    assert calls[0][0][0][0] == powershell
    assert calls[0][0][0][-1] == "Get-Date"
    assert calls[0][1]["shell"] is False


def test_execute_command_schema_exposes_the_structured_shell_override():
    from core.tool_registry import build_default_registry

    schema = build_default_registry().get("execute_command").input_schema

    assert schema["properties"]["shell_id"]["enum"] == [
        "auto",
        "cmd",
        "powershell",
        "pwsh",
        "bash",
        "zsh",
        "sh",
    ]


def test_execute_command_passes_shell_override_to_background_tasks(monkeypatch):
    from core.tools import execute_command

    captured = {}

    def fake_start(command, session_id="", shell_id="", turn_id=""):
        captured.update(command=command, session_id=session_id, shell_id=shell_id, turn_id=turn_id)
        return "task-1"

    monkeypatch.setattr("core.task_manager.start_background_task", fake_start)

    result = execute_command(
        "Get-Date",
        run_in_background=True,
        shell_id="powershell",
    )

    assert "task-1" in result
    assert captured["command"] == "Get-Date"
    assert captured["shell_id"] == "powershell"


def test_background_task_uses_the_selected_interpreter(tmp_path, monkeypatch):
    import core.task_manager as task_manager

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "cmd"})
    executable = r"C:\Windows\System32\cmd.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: executable if name == "cmd.exe" else None,
    )
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})
    monkeypatch.setattr(task_manager, "_save_tasks", lambda: None)
    monkeypatch.setattr(task_manager, "_prune_old_logs", lambda: None)
    monkeypatch.setattr(task_manager, "_process_create_time", lambda _pid: 1.0)
    calls = []

    class FakeProcess:
        pid = 12345

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(task_manager.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        task_manager.subprocess,
        "Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeProcess(),
    )

    task_id = task_manager.start_background_task("echo hi")

    try:
        assert calls[0][0][0] == "echo hi"
        assert calls[0][1]["shell"] is True
        assert calls[0][1]["executable"] == executable
    finally:
        task_manager.ACTIVE_TASKS[task_id]["log_handle"].close()


def test_plan_verification_uses_the_selected_interpreter(tmp_path, monkeypatch):
    from core.plan import PlanManager
    from core.task_scheduler import TaskScheduler

    _use_tmp_config(tmp_path, monkeypatch, {"command_shell": "powershell"})
    executable = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(
        "core.command_shell.shutil.which",
        lambda name: executable if name == "powershell.exe" else None,
    )
    calls = []

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):
            return b"ok", b""

    monkeypatch.setattr(
        "core.task_scheduler.subprocess.Popen",
        lambda *args, **kwargs: calls.append((args, kwargs)) or FakeProcess(),
    )

    passed, detail = TaskScheduler(PlanManager())._run_verification("Get-Date")

    assert passed is True
    assert detail == "ok"
    assert calls[0][0][0][-1] == "Get-Date"
    assert calls[0][0][0][0] == executable
    assert calls[0][1]["shell"] is False


def test_system_settings_include_active_model_computer_capability(monkeypatch, tmp_path):
    """设置负载携带活动模型电脑操控资格（与 Turn 起点同一判定函数）。"""
    from tests.computer_fakes import use_temp_config
    import core.computer.settings as computer_settings
    from core.computer.capability import resolve_computer_capability

    use_temp_config(tmp_path, monkeypatch, {
        "active_model_id": "m1",
        "providers": [{
            "protocol": "openai", "base_url": "https://example.test/v1",
            "api_key": "k",
            "models": [{
                "id": "m1", "name": "test-vision-model", "litellm_model": "openai/t-v",
                "supports_vision": True, "supports_function_calling": True,
            }],
        }],
    })
    monkeypatch.setattr(
        computer_settings, "list_browser_options", lambda platform=None: ()
    )

    from core import command_shell

    payload = command_shell.get_system_settings()
    hint = payload["computer_active_model_capability"]
    assert hint["model"] == "test-vision-model"
    # 开关默认关闭 → 不合格原因是未开启开关，而不是模型缺能力
    assert hint["eligible"] is False
    assert hint["error_code"] == "computer_disabled"

    # 打开开关后模型合格
    config_data = {
        "active_model_id": "m1",
        "browser_control_enabled": True,
        "computer_control_enabled": True,
        "computer_browser": "edge",
        "providers": [{
            "protocol": "openai", "base_url": "https://example.test/v1",
            "api_key": "k",
            "models": [{
                "id": "m1", "name": "test-vision-model", "litellm_model": "openai/t-v",
                "supports_vision": True, "supports_function_calling": True,
            }],
        }],
    }
    (tmp_path / "b").mkdir()
    use_temp_config(tmp_path / "b", monkeypatch, config_data)
    payload = command_shell.get_system_settings()
    hint = payload["computer_active_model_capability"]
    assert hint["eligible"] is True
    assert hint["protocol"] == "generic"


def test_system_settings_capability_hint_fail_closed_when_model_missing(monkeypatch, tmp_path):
    """未选择模型/模型缺能力：提示不合格且不拖垮设置页。"""
    from tests.computer_fakes import use_temp_config
    import core.computer.settings as computer_settings

    use_temp_config(tmp_path, monkeypatch, {"providers": []})
    monkeypatch.setattr(
        computer_settings, "list_browser_options", lambda platform=None: ()
    )
    from core import command_shell

    hint = command_shell.get_system_settings()["computer_active_model_capability"]
    assert hint["eligible"] is False
    assert hint["reason"]
