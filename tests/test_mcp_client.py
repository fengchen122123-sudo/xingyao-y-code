import asyncio
import json
import threading

import pytest

import core.mcp_client as mcp_client
from core.mcp_client import MCPManager


@pytest.fixture
def manager(monkeypatch):
    """独立的 MCPManager 实例；状态输出打桩，测试结束时停掉 event loop。"""
    monkeypatch.setattr(mcp_client, "_emit_status", lambda *a, **k: None)
    mgr = MCPManager()
    yield mgr
    try:
        mgr.close()
        mgr.loop.call_soon_threadsafe(mgr.loop.stop)
        mgr.thread.join(timeout=2)
    except Exception:
        pass


def _write_config(manager, tmp_path, servers):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    manager.config_file = str(cfg)


def test_lazy_connect_on_init(manager):
    """import/构造时绝不连接，首次使用才惰性启动"""
    assert manager._connect_future is None


def test_empty_config_never_connects(manager, tmp_path):
    """空配置走占位 future，绝不发起任何连接，后续访问零开销"""
    _write_config(manager, tmp_path, {})

    def _boom(*args, **kwargs):
        raise AssertionError("空配置不应发起任何连接")

    manager._connect_all = _boom

    assert manager.get_schemas() == []
    assert manager._connect_future is not None
    assert manager._connect_future.done()


def test_failed_connection_resets_future_for_retry(manager, tmp_path, monkeypatch):
    """连接失败后 _connect_future 必须重置回 None，允许下次调用重连"""
    _write_config(manager, tmp_path, {"s1": {"command": "nonexistent-cmd"}})

    async def _failing():
        raise RuntimeError("boom")

    monkeypatch.setattr(manager, "_connect_all", _failing)
    manager._ensure_connected()
    assert manager._connect_future is None, "失败后必须重置标记，而不是永久失效"

    called = threading.Event()

    async def _ok():
        called.set()

    monkeypatch.setattr(manager, "_connect_all", _ok)
    manager._ensure_connected()
    assert called.is_set(), "重试路径应真正重新发起连接"
    assert manager._connect_future is not None


def test_stdio_spawn_is_wrapped_with_timeout(manager, monkeypatch):
    """stdio_client 进入（spawn 子进程）必须包超时，挂起的 server 不得永久阻塞"""

    class _HangingCM:
        async def __aenter__(self):
            await asyncio.sleep(60)

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: _HangingCM())
    monkeypatch.setattr(mcp_client, "CONNECT_TIMEOUT", 0.2)

    asyncio.run(manager._connect_server("s1", {"command": "true"}))

    assert "s1" not in manager.servers, "超时失败的 server 不应注册为已连接"


def test_call_unknown_tool_returns_error(manager, tmp_path):
    """未连接/未知工具返回错误字符串而不是抛异常"""
    _write_config(manager, tmp_path, {})

    result = manager.call_tool_sync("srv__tool", {})

    assert "不存在" in result


def test_close_is_idempotent(manager):
    """close() 可重复调用不出错"""
    manager.close()
    manager.close()
    assert manager._closed


def test_get_schemas_returns_sorted_by_tool_name(manager, tmp_path):
    """tools 列表必须跨轮次逐字节稳定：get_schemas 按工具名排序返回，
    与各 server 连接完成的先后顺序无关（DeepSeek/Kimi 前缀缓存命中的前提）。"""
    _write_config(manager, tmp_path, {})
    manager.tool_schemas = [
        {"type": "function", "function": {"name": "srvb__zeta", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "srva__beta", "description": "", "parameters": {}}},
        {"type": "function", "function": {"name": "srva__alpha", "description": "", "parameters": {}}},
    ]

    names = [s["function"]["name"] for s in manager.get_schemas()]
    assert names == ["srva__alpha", "srva__beta", "srvb__zeta"]


def test_mcp_admin_mutations_require_permission():
    from core.tool_registry import build_default_registry

    registry = build_default_registry()

    for name in ("mcp_add_server", "mcp_remove_server", "mcp_set_enabled"):
        assert registry.get(name).requires_permission is True


def test_add_server_rejects_shell_interpreters(manager, tmp_path, monkeypatch):
    _write_config(manager, tmp_path, {})
    monkeypatch.setattr(manager, "reconnect", lambda: (True, "ok"))

    ok, message = manager.add_server(
        "unsafe",
        "powershell",
        ["-Command", "Write-Host compromised"],
    )

    assert ok is False
    assert "不允许" in message
    assert json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8")) == {
        "mcpServers": {}
    }


def test_add_server_rejects_process_injection_environment(manager, tmp_path, monkeypatch):
    _write_config(manager, tmp_path, {})
    monkeypatch.setattr(manager, "reconnect", lambda: (True, "ok"))

    ok, message = manager.add_server(
        "unsafe-env",
        "node",
        ["server.js"],
        {"NODE_OPTIONS": "--require=payload.js"},
    )

    assert ok is False
    assert "NODE_OPTIONS" in message


@pytest.mark.parametrize("command", ["C:/attacker/node.exe", "..\\attacker\\python"])
def test_add_server_rejects_executable_paths(manager, tmp_path, monkeypatch, command):
    _write_config(manager, tmp_path, {})
    monkeypatch.setattr(manager, "reconnect", lambda: (True, "ok"))

    ok, _ = manager.add_server("bad", command, [])

    assert ok is False


@pytest.mark.parametrize(
    "key", ["PATH", "PATHEXT", "COMSPEC", "PYTHONHOME", "NODE_PATH", "DYLD_INSERT_LIBRARIES"]
)
def test_add_server_rejects_loader_and_search_path_environment(
    manager, tmp_path, monkeypatch, key
):
    _write_config(manager, tmp_path, {})
    monkeypatch.setattr(manager, "reconnect", lambda: (True, "ok"))

    ok, _ = manager.add_server("bad", "node", [], {key: "attacker"})

    assert ok is False


def test_list_servers_redacts_env_and_argument_secrets(manager, tmp_path):
    _write_config(manager, tmp_path, {
        "postgres": {
            "command": "npx",
            "args": [
                "-y",
                "server",
                "postgresql://alice:secret-pass@localhost/app",
                "--api-key=plain-token",
                "--password",
                "short",
            ],
            "env": {
                "PASSWORD": "tiny",
                "PUBLIC_MODE": "development",
            },
        }
    })

    payload = manager.list_servers()

    assert "secret-pass" not in payload
    assert "plain-token" not in payload
    assert "tiny" not in payload
    assert "development" not in payload
    assert "***" in payload


def test_list_servers_redacts_query_userinfo_and_authorization_header(manager, tmp_path):
    _write_config(manager, tmp_path, {
        "demo": {
            "command": "node",
            "args": [
                "https://host/?mode=prod&token=plain-token",
                "https://user-token@host/path",
                "--header=Authorization: Bearer plain-bearer",
            ],
        }
    })

    rendered = manager.list_servers()

    for secret in ("plain-token", "user-token", "plain-bearer"):
        assert secret not in rendered


def test_closed_manager_cannot_start_new_connections(manager, tmp_path, monkeypatch):
    _write_config(manager, tmp_path, {"srv": {"command": "npx", "args": ["server"]}})
    started = []

    async def connect_all():
        started.append(True)

    monkeypatch.setattr(manager, "_connect_all", connect_all)
    manager.close()

    assert manager.get_schemas() == []
    assert started == []
