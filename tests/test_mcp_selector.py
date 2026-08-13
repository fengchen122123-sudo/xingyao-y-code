"""core/mcp_selector.py 预设组装与路径展开的单元测试。"""

import os

import core.mcp_selector as mcp_selector


def test_build_server_config_expands_home_arg():
    """filesystem 预设的 $HOME 必须展开为实际用户目录，而不是按字面量传给子进程"""
    preset = {"args": ["-y", "@modelcontextprotocol/server-filesystem", "$HOME"], "fields": []}

    args, env = mcp_selector._build_server_config(preset, {})

    assert args[-1] == os.path.expanduser("~")
    assert env == {}


def test_build_server_config_expands_tilde_and_env_vars(monkeypatch):
    monkeypatch.setenv("YC_TEST_EXPAND_DIR", "xdata")
    preset = {"args": ["$YC_TEST_EXPAND_DIR/sub", "~/data"], "fields": []}

    args, _ = mcp_selector._build_server_config(preset, {})

    assert args[0] == "xdata/sub"
    assert args[1] == os.path.expanduser("~") + "/data"


def test_postgres_preset_passes_dsn_via_env_not_args():
    """postgres 预设：含密码连接串走 env（写盘前会被加密），不暴露在子进程命令行"""
    preset = next(p for p in mcp_selector.PRESET_MCPS if p["name"] == "postgres")
    dsn = "postgresql://user:pass@host:5432/db"
    field_name = preset["fields"][0]["name"]

    args, env = mcp_selector._build_server_config(preset, {field_name: dsn})

    assert all(dsn not in str(a) for a in args), "DSN 不得出现在 args（可被 WMI 读取）"
    assert env[field_name] == dsn


def test_filesystem_preset_no_longer_keeps_literal_home():
    preset = next(p for p in mcp_selector.PRESET_MCPS if p["name"] == "filesystem")

    args, _ = mcp_selector._build_server_config(preset, {})

    assert "$HOME" not in args
    assert args[-1] == os.path.expanduser("~")
