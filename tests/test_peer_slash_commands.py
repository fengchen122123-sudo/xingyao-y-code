import pytest

import config
from core import credentials, lan_peers
from core.mcp_server import McpServer
from core.memory import MemoryManager
from core.slash_commands.dispatch import dispatch_command
from core.slash_commands.peer_commands import (
    build_peer_slash_specs,
    clear_peer_command_cache,
)
from core.slash_commands.specs import register_builtin_commands


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    monkeypatch.setattr(credentials, "protect_secret", lambda v: f"enc:{v}")
    monkeypatch.setattr(credentials, "unprotect_secret", lambda v: v[4:] if v.startswith("enc:") else v)
    register_builtin_commands()
    clear_peer_command_cache()
    lan_peers.lan_peer_manager.reset_for_tests()
    yield
    clear_peer_command_cache()
    lan_peers.lan_peer_manager.reset_for_tests()


def test_peer_slash_commands_end_to_end(tmp_path):
    ws_a = tmp_path / "workspace_alpha"
    ws_b = tmp_path / "workspace_beta"
    ws_a.mkdir()
    ws_b.mkdir()
    mem = MemoryManager(db_path=str(tmp_path / "peer_b.db"))
    mem.set_workdir(str(ws_a))
    mem.add_workspace(str(ws_a))
    mem.add_workspace(str(ws_b))
    mem.ensure_session("nb-existing", workdir=str(ws_a), source="GUI")

    fake_cfg = {
        "active_model_id": "ds-v4",
        "providers": [
            {
                "id": "deepseek",
                "name": "深度求索",
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-secret",
                "models": [
                    {"id": "ds-v4", "name": "deepseek-v4", "context_window": 128000},
                    {"id": "kimi-k2", "name": "kimi-k2", "context_window": 256000},
                ],
            }
        ],
    }

    class PeerBridge:
        _memory = mem
        _current_session = "local-on-b"

        def _load_config(self):
            return fake_cfg

        def set_model(self, model_id, session_name=None):
            if session_name:
                mem.set_session_model_id(session_name, model_id)
            return {"ok": True}

    srv = McpServer(bridge=PeerBridge(), port=0, host="127.0.0.1", token_provider=lambda: "tok-nb")
    assert srv.start()["ok"]
    try:
        lan_peers.save_peer({
            "id": "192-168-1-22",
            "name": "笔记本",
            "url": f"http://127.0.0.1:{srv.port}/a2a",
            "token": "tok-nb",
        })

        specs = build_peer_slash_specs()
        spec_names = [s["name"] for s in specs]
        assert "/笔记本目录" in spec_names
        assert "/笔记本对话" in spec_names
        assert "/笔记本厂商" in spec_names
        assert "/笔记本模型" in spec_names
        assert "/笔记本选对话" in spec_names
        assert "/笔记本新建" in spec_names
        assert "/笔记本选模型" in spec_names

        # 1. /笔记本目录 与空格写法 /笔记本 目录
        r_dirs = dispatch_command("/笔记本目录")
        assert r_dirs.handled and r_dirs.kind == "notice"
        assert "workspace_alpha" in r_dirs.notice and "workspace_beta" in r_dirs.notice

        r_dirs2 = dispatch_command("/笔记本 目录")
        assert r_dirs2.handled and r_dirs2.kind == "notice"
        assert "工作目录" in r_dirs2.notice

        # 2. /笔记本对话
        r_sess = dispatch_command("/笔记本对话")
        assert r_sess.handled and r_sess.kind == "notice"
        assert "nb-existing" in r_sess.notice

        # 3. /笔记本选对话 1
        r_pick_sess = dispatch_command("/笔记本选对话 1")
        assert r_pick_sess.handled and r_pick_sess.kind == "notice"
        assert "nb-existing" in r_pick_sess.notice
        assert "沙盒已解除" in r_pick_sess.notice
        assert lan_peers.get_peer("192-168-1-22")["default_session"] == "nb-existing"

        # 4. /笔记本新建 指定目录编号与会话名
        r_new = dispatch_command(f"/笔记本新建 {ws_b} dev-chat")
        assert r_new.handled and r_new.kind == "notice"
        assert "dev-chat" in r_new.notice
        assert lan_peers.get_peer("192-168-1-22")["default_session"] == "dev-chat"

        # 5. /笔记本厂商 & /笔记本模型 & /笔记本选模型 2
        r_prov = dispatch_command("/笔记本厂商")
        assert r_prov.handled and r_prov.kind == "notice"
        assert "深度求索" in r_prov.notice and "sk-secret" not in r_prov.notice

        r_mod = dispatch_command("/笔记本模型")
        assert r_mod.handled and r_mod.kind == "notice"
        assert "kimi-k2" in r_mod.notice

        r_pick_mod = dispatch_command("/笔记本选模型 2")
        assert r_pick_mod.handled and r_pick_mod.kind == "notice"
        assert "kimi-k2" in r_pick_mod.notice
        assert mem.get_session_model_id("dev-chat") == "kimi-k2"

        # 6. /笔记本状态 与 无关路径不被误拦
        r_stat = dispatch_command("/笔记本状态")
        assert r_stat.handled and r_stat.kind == "notice"
        assert "dev-chat" in r_stat.notice

        r_unrelated = dispatch_command("/usr/local/bin")
        assert r_unrelated.handled is False
    finally:
        srv.stop()
        mem.close()
