import threading

from gui_bridge import ApiBridge


class _FakeMcp:
    """模拟 MCPManager：记录 add_server 调用，list_servers/get_schemas 供 _build_mcp_list 使用。"""

    def __init__(self, ok=True, msg="已新增服务 fs"):
        self.calls = []
        self._ok = ok
        self._msg = msg

    def add_server(self, name, command, args, env=None, enabled=True):
        self.calls.append({
            "name": name, "command": command, "args": args, "env": env, "enabled": enabled,
        })
        return self._ok, self._msg

    def list_servers(self):
        return "[]"

    def get_schemas(self):
        return []


def _bare_bridge(fake):
    """不经 __init__ 构造裸实例；emit_event 改为捕获。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._mcp = fake
    bridge._events = []
    bridge.emit_event = lambda event, payload: bridge._events.append((event, payload))
    return bridge


def test_add_mcp_server_unavailable_when_manager_missing():
    bridge = _bare_bridge(None)
    res = bridge.add_mcp_server("fs", "npx", ["-y", "srv"])
    assert res["ok"] is False


def test_add_mcp_server_rejects_invalid_fields():
    fake = _FakeMcp()
    bridge = _bare_bridge(fake)
    assert bridge.add_mcp_server("", "npx", ["-y", "x"])["ok"] is False
    assert bridge.add_mcp_server("  ", "npx", ["-y", "x"])["ok"] is False
    assert bridge.add_mcp_server("fs", "", ["-y", "x"])["ok"] is False
    assert bridge.add_mcp_server("fs", "npx", "not-a-list")["ok"] is False
    assert bridge.add_mcp_server("fs", "npx", [], env="not-a-dict")["ok"] is False
    # 校验失败绝不能触达底层 add_server
    assert fake.calls == []


def test_add_mcp_server_success_in_background():
    fake = _FakeMcp()
    bridge = _bare_bridge(fake)
    done = threading.Event()
    orig_emit = bridge.emit_event
    bridge.emit_event = lambda e, p: (orig_emit(e, p), done.set())

    res = bridge.add_mcp_server("fs", "npx", ["-y", "srv"], env={"KEY": "v"}, enabled=True)
    assert res["ok"] is True
    assert "mcps" in res

    assert done.wait(5), "后台线程应完成并发出 mcp_status 事件"
    assert fake.calls == [{
        "name": "fs", "command": "npx", "args": ["-y", "srv"],
        "env": {"KEY": "v"}, "enabled": True,
    }]
    event, payload = bridge._events[-1]
    assert event == "mcp_status"
    assert payload["ok"] is True


def test_add_mcp_server_failure_propagates_to_event():
    fake = _FakeMcp(ok=False, msg="已存在同名服务")
    bridge = _bare_bridge(fake)
    done = threading.Event()
    orig_emit = bridge.emit_event
    bridge.emit_event = lambda e, p: (orig_emit(e, p), done.set())

    res = bridge.add_mcp_server("fs", "npx", [])
    assert res["ok"] is True  # 已受理，结果走事件
    assert done.wait(5)
    event, payload = bridge._events[-1]
    assert event == "mcp_status"
    assert payload["ok"] is False
    assert "已存在同名服务" in payload["msg"]


def test_add_mcp_server_empty_env_passed_as_none():
    """空 env 字典应归一化为 None，避免写入无意义的空对象。"""
    fake = _FakeMcp()
    bridge = _bare_bridge(fake)
    done = threading.Event()
    orig_emit = bridge.emit_event
    bridge.emit_event = lambda e, p: (orig_emit(e, p), done.set())

    bridge.add_mcp_server("fs", "npx", [], env={})
    assert done.wait(5)
    assert fake.calls[0]["env"] is None
