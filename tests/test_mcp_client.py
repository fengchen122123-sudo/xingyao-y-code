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
