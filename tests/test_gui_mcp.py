import json
import threading

from gui_bridge import ApiBridge


class _FakeMcp:
    """模拟 MCPManager：记录 add_server/remove_server 调用，list_servers/get_schemas 供 _build_mcp_list 使用。"""

    def __init__(
        self,
        ok=True,
        msg="已新增服务 fs",
        servers=None,
        remove_ok=True,
        remove_msg="已删除服务 fs",
    ):
        self.calls = []
        self.remove_calls = []
        self.remove_threads = []
        self._ok = ok
        self._msg = msg
        self._servers = servers if servers is not None else []
        self._remove_ok = remove_ok
        self._remove_msg = remove_msg

    def add_server(self, name, command, args, env=None, enabled=True):
        self.calls.append({
            "name": name, "command": command, "args": args, "env": env, "enabled": enabled,
        })
        return self._ok, self._msg

    def remove_server(self, name):
        """记录调用与所在线程名，并从假配置中真正移除该服务。"""
        self.remove_calls.append(name)
        self.remove_threads.append(threading.current_thread().name)
        if self._remove_ok:
            self._servers = [s for s in self._servers if s.get("name") != name]
        return self._remove_ok, self._remove_msg

    def list_servers(self):
        return json.dumps(self._servers, ensure_ascii=False)

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


def test_build_mcp_list_passes_last_error():
    """last_error 应原样透传：无错误为 None，有错误为具体原因。"""
    fake = _FakeMcp(servers=[
        {"name": "fs", "enabled": True, "connected": True, "last_error": None},
        {"name": "bad", "enabled": False, "connected": False, "last_error": "连接超时"},
    ])
    bridge = _bare_bridge(fake)

    items = bridge._build_mcp_list()
    assert [i["name"] for i in items] == ["fs", "bad"]
    assert items[0]["last_error"] is None
    assert items[1]["last_error"] == "连接超时"
    # 其余字段语义不变
    assert items[0]["enabled"] is True and items[0]["connected"] is True
    assert items[1]["enabled"] is False and items[1]["connected"] is False
    assert all(i["tools_count"] == 0 for i in items)
    # get_mcp_servers 走同一条路径
    assert bridge.get_mcp_servers() == items


def test_build_mcp_list_missing_last_error_defaults_to_none():
    """底层未返回 last_error 字段时兜底为 None。"""
    fake = _FakeMcp(servers=[{"name": "fs", "enabled": True, "connected": False}])
    bridge = _bare_bridge(fake)
    assert bridge._build_mcp_list()[0]["last_error"] is None


def test_remove_mcp_server_unavailable_when_manager_missing():
    bridge = _bare_bridge(None)
    res = bridge.remove_mcp_server("fs")
    assert res["ok"] is False
    assert "MCP 管理器不可用" in res["msg"]


def test_remove_mcp_server_rejects_blank_name():
    fake = _FakeMcp()
    bridge = _bare_bridge(fake)
    assert bridge.remove_mcp_server("")["ok"] is False
    assert bridge.remove_mcp_server("   ")["ok"] is False
    assert bridge.remove_mcp_server(None)["ok"] is False
    # 校验失败绝不能触达底层 remove_server
    assert fake.remove_calls == []


def test_remove_mcp_server_success_in_background():
    fake = _FakeMcp(servers=[{"name": "fs", "enabled": True, "connected": True, "last_error": None}])
    bridge = _bare_bridge(fake)
    done = threading.Event()
    orig_emit = bridge.emit_event
    bridge.emit_event = lambda e, p: (orig_emit(e, p), done.set())

    res = bridge.remove_mcp_server(" fs ")
    assert res["ok"] is True
    assert "正在删除 fs" in res["msg"]
    assert "mcps" in res

    assert done.wait(5), "后台线程应完成并发出 mcp_status 事件"
    assert fake.remove_calls == ["fs"]  # 名称已去空白
    assert fake.remove_threads == ["gui-mcp-remove"]
    event, payload = bridge._events[-1]
    assert event == "mcp_status"
    assert payload["ok"] is True
    assert "已删除服务 fs" in payload["msg"]
    assert payload["mcps"] == []  # 删除后刷新出的列表


def test_remove_mcp_server_failure_propagates_to_event():
    fake = _FakeMcp(
        servers=[{"name": "fs", "enabled": True, "connected": True, "last_error": None}],
        remove_ok=False,
        remove_msg="未找到 MCP 服务: fs",
    )
    bridge = _bare_bridge(fake)
    done = threading.Event()
    orig_emit = bridge.emit_event
    bridge.emit_event = lambda e, p: (orig_emit(e, p), done.set())

    res = bridge.remove_mcp_server("fs")
    assert res["ok"] is True  # 已受理，结果走事件
    assert done.wait(5)
    event, payload = bridge._events[-1]
    assert event == "mcp_status"
    assert payload["ok"] is False
    assert payload["msg"] == "未找到 MCP 服务: fs"
    assert payload["mcps"] == [{"name": "fs", "enabled": True, "connected": True, "tools_count": 0, "last_error": None}]
