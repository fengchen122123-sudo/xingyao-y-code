"""T1 子进程环境变量策略：默认排除敏感变量的安全默认 + 逃生门。

覆盖 build_child_env 纯函数语义、config.json 读取，以及 execute_command /
后台任务两处 spawn 的接入行为。
"""

import json
from types import SimpleNamespace

import config

from core.command_policy import (
    DEFAULT_SENSITIVE_ENV_PATTERNS,
    build_child_env,
    load_shell_env_policy,
)


def _use_tmp_config(tmp_path, monkeypatch, data):
    """让 config._load() 只读临时配置，避免真实用户配置干扰断言。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


def _capture_execute_command_env(monkeypatch, tmp_path, config_data):
    from core.tools import execute_command

    _use_tmp_config(tmp_path, monkeypatch, config_data)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("YCODE_KEEP_ME", "1")
    captured = {}

    class FakeProcess:
        returncode = 0
        pid = 424242

        def poll(self):
            return 0

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        kwargs["stdout"].write(b"ok")
        kwargs["stdout"].flush()
        return FakeProcess()

    monkeypatch.setattr("core.tools.subprocess.Popen", fake_popen)
    result = execute_command("Get-Date")
    assert result == "ok"
    return captured["env"]


# ---------------------------------------------------------------------------
# 纯函数语义
# ---------------------------------------------------------------------------

def test_default_excludes_sensitive_keeps_common():
    base = {"PATH": "/bin", "HOME": "/u", "OPENAI_API_KEY": "sk-x", "GITHUB_TOKEN": "gh"}
    env = build_child_env(base)
    assert "OPENAI_API_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["PATH"] == "/bin" and env["HOME"] == "/u"


def test_include_only_and_none_inherit():
    env = build_child_env({"A": "1", "FOO_BAR": "2"}, policy={"include_only": ["FOO*"]})
    assert env == {"FOO_BAR": "2"}
    assert build_child_env({"A": "1"}, policy={"inherit": "none", "set": {"B": "2"}}) == {"B": "2"}


def test_escape_hatch_restores_old_behavior():
    env = build_child_env({"OPENAI_API_KEY": "sk-x"}, policy={"exclude_sensitive": False})
    assert env["OPENAI_API_KEY"] == "sk-x"


def test_default_sensitive_patterns_cover_documented_families():
    assert tuple(DEFAULT_SENSITIVE_ENV_PATTERNS) == ("*KEY*", "*SECRET*", "*TOKEN*", "*PASSWORD*")


def test_sensitive_matching_is_case_insensitive():
    base = {
        "Openai_Api_Key": "sk-x",
        "aws_secret_access_key": "s",
        "GitHub_Token": "gh",
        "MYSQL_PASSWORD": "pw",
        "Path": "/bin",
    }
    env = build_child_env(base)
    assert env == {"Path": "/bin"}


def test_custom_exclude_patterns_are_appended_and_case_insensitive():
    base = {"DEBUG_MODE": "1", "MY_FLAG": "2", "PATH": "/bin"}
    env = build_child_env(base, policy={"exclude": ["debug_*", "MY_*"]})
    assert env == {"PATH": "/bin"}


def test_set_injection_overrides_and_survives_exclusions():
    base = {"PATH": "/bin", "OPENAI_API_KEY": "sk-x"}
    env = build_child_env(
        base,
        policy={"set": {"OPENAI_API_KEY": "explicit", "EXTRA": "1"}},
    )
    assert env == {"PATH": "/bin", "OPENAI_API_KEY": "explicit", "EXTRA": "1"}
    assert base == {"PATH": "/bin", "OPENAI_API_KEY": "sk-x"}


def test_invalid_policy_values_fall_back_to_safe_defaults():
    base = {"PATH": "/bin", "OPENAI_API_KEY": "sk-x"}
    for policy in (None, "not-a-dict", 42, {"inherit": "weird"}, {"include_only": "PATH"}):
        env = build_child_env(base, policy=policy)
        assert "OPENAI_API_KEY" not in env, policy
        assert env["PATH"] == "/bin", policy


def test_values_are_normalized_to_strings():
    env = build_child_env({"PORT": 8080}, policy={"inherit": "none"})
    assert env == {}
    assert build_child_env({"PORT": 8080}, policy={"set": {"PORT": 9090}}) == {"PORT": "9090"}


# ---------------------------------------------------------------------------
# config.json 读取
# ---------------------------------------------------------------------------

def test_load_shell_env_policy_defaults_without_config(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {})
    assert load_shell_env_policy() == {"inherit": "all", "exclude_sensitive": True}


def test_load_shell_env_policy_reads_user_config(tmp_path, monkeypatch):
    _use_tmp_config(
        tmp_path,
        monkeypatch,
        {"shell_env_policy": {"exclude_sensitive": False, "exclude": ["FOO_*"]}},
    )
    assert load_shell_env_policy() == {
        "inherit": "all",
        "exclude_sensitive": False,
        "exclude": ["FOO_*"],
    }


def test_load_shell_env_policy_ignores_invalid_values(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch, {"shell_env_policy": ["bad"]})
    assert load_shell_env_policy() == {"inherit": "all", "exclude_sensitive": True}


# ---------------------------------------------------------------------------
# 两处 spawn 接入
# ---------------------------------------------------------------------------

def test_execute_command_spawn_env_excludes_sensitive(tmp_path, monkeypatch):
    env = _capture_execute_command_env(monkeypatch, tmp_path, {})

    assert "OPENAI_API_KEY" not in env
    assert env["YCODE_KEEP_ME"] == "1"
    # 编码注入必须保留在安全默认之上
    assert env["PYTHONIOENCODING"]
    assert env["PYTHONUTF8"] == "0"


def test_execute_command_escape_hatch_restores_sensitive_env(tmp_path, monkeypatch):
    env = _capture_execute_command_env(
        monkeypatch,
        tmp_path,
        {"shell_env_policy": {"exclude_sensitive": False}},
    )

    assert env["OPENAI_API_KEY"] == "sk-test-123"


def test_background_task_spawn_env_excludes_sensitive(tmp_path, monkeypatch):
    import core.task_manager as task_manager

    _use_tmp_config(tmp_path, monkeypatch, {})
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-123")
    monkeypatch.setenv("YCODE_KEEP_ME", "1")
    monkeypatch.setattr(task_manager, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(task_manager, "TASK_DB_PATH", str(tmp_path / "tasks.json"))
    monkeypatch.setattr(task_manager, "ACTIVE_TASKS", {})
    captured = {}

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(task_manager.subprocess, "Popen", fake_popen)

    task_manager.start_background_task("demo")

    handle = captured["stdout"]
    try:
        env = captured["env"]
        assert "OPENAI_API_KEY" not in env
        assert env["YCODE_KEEP_ME"] == "1"
        # 实时日志所需的注入必须保留在安全默认之上
        assert env["PYTHONUNBUFFERED"] == "1"
        assert env["PYTHONIOENCODING"]
        assert env["PYTHONUTF8"] == "0"
    finally:
        handle.close()