# -*- coding: utf-8 -*-
"""会话级 UI 状态桥方法：成功往返 + 异常必须回落 ok=False。

裸桥范式照抄 tests/test_gui_ide_bridge.py：``ApiBridge(mcp_manager=None)`` 不启动
任何真实服务，只打桩 ``_session_workdir`` / ``_current_session`` 两个读会话名的入口。

数据目录重定向用 ``monkeypatch.setattr(config, "YCODE_DIR", tmp_path)``，**不用**
``monkeypatch.setenv("YCODE_DATA_DIR", ...)``——那是导入期常量，conftest 收集阶段
已导入 config，事后改环境变量不生效，会写进用户真实 ~/.ycode。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_path_policy as gpp
from gui_bridge import ApiBridge


def _make_bridge(workdir: str, monkeypatch, tmp_path) -> ApiBridge:
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))
    bridge = ApiBridge(mcp_manager=None)
    bridge._session_workdir = lambda session_name="": workdir  # type: ignore[method-assign]
    bridge._current_session = "test-session"
    return bridge


@pytest.fixture()
def bridge(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path, raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    return _make_bridge(str(ws), monkeypatch, tmp_path)


def test_get_session_ui_defaults_to_chat_mode(bridge, tmp_path):
    res = bridge.get_session_ui("s1")

    assert res["ok"] is True
    assert res["ui"] == {"version": 1, "ide_mode": False}
    # 只读：不该凭空造出文件
    assert not (tmp_path / "session_ui.json").exists()


def test_save_then_get_session_ui_roundtrip(bridge, tmp_path):
    saved = bridge.save_session_ui("s1", {"ide_mode": True})
    assert saved["ok"] is True
    assert saved["ui"]["ide_mode"] is True

    got = bridge.get_session_ui("s1")
    assert got["ok"] is True
    assert got["ui"]["ide_mode"] is True

    on_disk = json.loads((tmp_path / "session_ui.json").read_text(encoding="utf-8"))
    assert on_disk["sessions"]["s1"]["ide_mode"] is True


def test_save_session_ui_sanitizes_payload(bridge):
    res = bridge.save_session_ui("s1", {"ide_mode": "yes", "extra": 1})

    assert res["ok"] is True
    assert res["ui"] == {"version": 1, "ide_mode": False}


def test_get_session_ui_reports_failure(bridge, monkeypatch):
    """底层读取抛错时必须回 ok=False，而不是把异常抛给 pywebview 调用方。"""
    import core.session_ui as store

    monkeypatch.setattr(store, "get_session_ui", lambda session: (_ for _ in ()).throw(RuntimeError("boom")))

    res = bridge.get_session_ui("s1")

    assert res["ok"] is False
    assert "boom" in res["msg"]


def test_save_session_ui_reports_failure(bridge, monkeypatch):
    import core.session_ui as store

    def boom(session, ui):
        raise store.SessionUiError("会话 UI 状态条目已达上限")

    monkeypatch.setattr(store, "save_session_ui", boom)

    res = bridge.save_session_ui("s1", {"ide_mode": True})

    assert res["ok"] is False
    assert "上限" in res["msg"]
