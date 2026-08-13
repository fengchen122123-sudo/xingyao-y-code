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

    asyncio.run(manager._connect_server("s1", {"command": "node"}))

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


# ── 连接路径强制校验（手工编辑 mcp.json 绕过 add_server 的场景） ──────

def test_connect_server_rejects_hand_edited_unsafe_command(manager, monkeypatch):
    """手工写入 mcp.json 的任意命令：_connect_server 必须复核 _validate_mcp_launch 规则"""
    spawned = []
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: spawned.append(params))

    result = asyncio.run(manager._connect_server("bad", {"command": "powershell", "args": []}))

    assert result is None
    assert spawned == [], "校验失败不得 spawn 任何子进程"
    assert "bad" not in manager.servers


def test_connect_server_rejects_dangerous_env_from_disk(manager, monkeypatch):
    """手工写入的危险环境变量（如 PATH 覆盖）：拒绝连接且不 spawn"""
    spawned = []
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: spawned.append(params))

    result = asyncio.run(
        manager._connect_server("bad", {"command": "node", "env": {"PATH": "C:\\evil"}})
    )

    assert result is None
    assert spawned == []


def test_connect_server_env_merge_strips_inherited_key_overrides(manager, monkeypatch):
    """通过校验的 env 仍不得覆盖 PATH/COMSPEC/SYSTEMROOT 等继承键（合并时剔除）"""
    captured = {}

    def _fake_stdio(params):
        captured["env"] = params.env
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(mcp_client, "stdio_client", _fake_stdio)

    asyncio.run(manager._connect_server("s1", {
        "command": "node",
        "env": {"SYSTEMROOT": "C:\\evil", "MY_SAFE_VAR": "1"},
    }))

    assert captured["env"]["MY_SAFE_VAR"] == "1"
    assert captured["env"].get("SYSTEMROOT") != "C:\\evil"


def test_connect_server_rejects_non_dict_env(manager, monkeypatch):
    spawned = []
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: spawned.append(params))

    result = asyncio.run(manager._connect_server("bad", {"command": "node", "env": ["X=1"]}))

    assert result is None
    assert spawned == []


# ── env 加密落盘 / 读取解密（与 config.json 加密策略对齐） ────────────

def test_add_server_encrypts_env_values_before_write(manager, tmp_path, monkeypatch):
    _write_config(manager, tmp_path, {})
    monkeypatch.setattr(manager, "reconnect", lambda: (True, "ok"))
    monkeypatch.setattr(
        mcp_client.credentials, "protect_secret", lambda v: "dpapi:enc(" + v + ")"
    )

    ok, _ = manager.add_server("firecrawl", "npx", ["-y", "firecrawl-mcp"], {"FIRECRAWL_API_KEY": "fc-123"})

    assert ok is True
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["firecrawl"]["env"] == {"FIRECRAWL_API_KEY": "dpapi:enc(fc-123)"}


def test_connect_server_decrypts_prefixed_env_values(manager, monkeypatch):
    """读取连接时 dpapi:/keyring: 前缀的值必须解密后注入子进程环境"""
    captured = {}

    def _fake_stdio(params):
        captured["env"] = params.env
        raise RuntimeError("stop-after-capture")

    monkeypatch.setattr(mcp_client, "stdio_client", _fake_stdio)
    monkeypatch.setattr(mcp_client.credentials, "unprotect_secret", lambda v: "plain-key")

    asyncio.run(manager._connect_server("s1", {
        "command": "node",
        "env": {"API_KEY": "dpapi:xyz"},
    }))

    assert captured["env"]["API_KEY"] == "plain-key"


def test_connect_server_treats_unprefixed_env_as_plaintext(manager, monkeypatch):
    """向后兼容：无前缀旧值按明文处理"""
    captured = {}

    def _fake_stdio(params):
        captured["env"] = params.env
        raise RuntimeError("stop-after-capture")

    def _boom(value):
        raise AssertionError("明文值不应走解密")

    monkeypatch.setattr(mcp_client, "stdio_client", _fake_stdio)
    monkeypatch.setattr(mcp_client.credentials, "unprotect_secret", _boom)

    asyncio.run(manager._connect_server("s1", {
        "command": "node",
        "env": {"API_KEY": "legacy-plain"},
    }))

    assert captured["env"]["API_KEY"] == "legacy-plain"


def test_encrypt_plaintext_env_on_disk_migrates_after_success(manager, tmp_path, monkeypatch):
    """连接成功后把明文 env 一次性回写为加密版本"""
    _write_config(manager, tmp_path, {
        "s1": {"command": "node", "args": [], "env": {"API_KEY": "legacy-plain"}}
    })
    manager._load_config()
    monkeypatch.setattr(
        mcp_client.credentials, "protect_secret", lambda v: "dpapi:enc(" + v + ")"
    )

    manager._encrypt_plaintext_env_on_disk("s1")

    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["s1"]["env"] == {"API_KEY": "dpapi:enc(legacy-plain)"}
    # 幂等：已加密的值不会二次加密
    manager._encrypt_plaintext_env_on_disk("s1")
    saved = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert saved["mcpServers"]["s1"]["env"] == {"API_KEY": "dpapi:enc(legacy-plain)"}


# ── 取消竞态 / schema 幂等（M39） ─────────────────────────────────

class _FakeTool:
    def __init__(self, name):
        self.name = name
        self.description = ""
        self.inputSchema = {}


class _FakeToolsResponse:
    def __init__(self, names):
        self.tools = [_FakeTool(n) for n in names]


class _FakeSession:
    def __init__(self, tool_names):
        self._tool_names = tool_names

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolsResponse(self._tool_names)


class _FakeStdioCM:
    async def __aenter__(self):
        return (object(), object())

    async def __aexit__(self, *exc):
        return False


def _session_cm(session):
    class _CM:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    return _CM()


def test_reconnect_same_server_replaces_schemas_idempotently(manager, monkeypatch):
    """同 server 重复连接（取消-重试竞态）：不产生双份 server/双份工具 schema"""
    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: _FakeStdioCM())
    monkeypatch.setattr(
        mcp_client, "ClientSession", lambda r, w: _session_cm(_FakeSession(["alpha"]))
    )

    asyncio.run(manager._connect_server("s1", {"command": "node"}))
    asyncio.run(manager._connect_server("s1", {"command": "node"}))

    names = [s["function"]["name"] for s in manager.tool_schemas]
    assert names == ["s1__alpha"]
    assert len(manager.servers) == 1


def test_cancelled_connect_cleans_up_orphan_entry(manager, monkeypatch):
    """取消晚于 initialize 完成：已入 self.servers 的孤儿连接必须被移除并关闭"""
    closed = []

    class _SlowListToolsSession(_FakeSession):
        async def list_tools(self):
            await asyncio.sleep(60)

    class _TrackStackCM:
        async def __aenter__(self):
            return _SlowListToolsSession(["alpha"])

        async def __aexit__(self, *exc):
            closed.append(True)
            return False

    monkeypatch.setattr(mcp_client, "stdio_client", lambda params: _FakeStdioCM())
    monkeypatch.setattr(mcp_client, "ClientSession", lambda r, w: _TrackStackCM())

    async def _run():
        task = asyncio.ensure_future(manager._connect_server("s1", {"command": "node"}))
        await asyncio.sleep(0.1)  # 让 initialize 完成、entry 已入 servers
        assert "s1" in manager.servers
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run())

    assert "s1" not in manager.servers, "取消后孤儿连接必须移除"
    assert closed, "取消后必须关闭已建立的 stack"


# ── call_tool_sync 超时契约（M40） ────────────────────────────────

def test_call_tool_sync_timeout_returns_friendly_error(manager, tmp_path, monkeypatch):
    """超时不得抛给调用线程：返回 🚨 开头的友好错误文本并取消底层调用"""
    _write_config(manager, tmp_path, {})

    async def _slow(tool_name, arguments):
        await asyncio.sleep(30)
        return "late"

    monkeypatch.setattr(manager, "_call_tool_async", _slow)
    monkeypatch.setattr(mcp_client, "CALL_TOOL_TIMEOUT", 0.1)

    result = manager.call_tool_sync("srv__tool", {})

    assert result.startswith("🚨")
    assert "超时" in result
