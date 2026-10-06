# -*- coding: utf-8 -*-
"""IDE 桥方法：路径策略、读写往返、布局存取、变更事件派发。

夹具说明（两处对计划原文的等价修正，均在下方用例内标明）：
1. 落盘一律用 ``write_bytes``。Windows 上 ``Path.write_text("x = 1\\n")`` 会把
   "\\n" 翻译成 os.linesep（实测本机落盘为 b"x = 1\\r\\n"，size=7），会让「纯 LF
   文件」用例悄悄变成 CRLF 用例、体积断言随之失真。这与
   ``tests/test_ide_file_service.py`` 文件头记录的是同一个坑。
2. ``test_ide_layout_roundtrip`` 不用 ``monkeypatch.setenv("YCODE_DATA_DIR", ...)``：
   那是导入期常量，``tests/conftest.py`` 在收集阶段已导入 config，事后改环境变量
   不生效，会写进共享临时目录并跨用例污染。改为直接打桩 ``config.YCODE_DIR``。
"""
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


def test_ide_read_write_roundtrip(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)

    read = bridge.ide_read_file(str(f))
    assert read["ok"] is True
    assert read["content"] == "x = 1\n"

    written = bridge.ide_write_file(
        str(f), "x = 2\n", read["hash"], read["encoding"], read["bom"], read["eol"]
    )
    assert written["ok"] is True
    assert f.read_bytes() == b"x = 2\n"


def test_ide_write_outside_workspace_denied(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "a.py"
    f.write_bytes(b"x = 1\n")
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)

    res = bridge.ide_write_file(str(f), "hacked\n", "", "utf-8", False, "lf")
    assert res["ok"] is False
    assert "工作区" in res["msg"]
    assert f.read_bytes() == b"x = 1\n"


def test_ide_read_credential_config_denied(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    f = ydata / "config.json"
    f.write_bytes(b'{"api_key": "secret"}')
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    bridge = _make_bridge("GLOBAL", monkeypatch, tmp_path)
    res = bridge.ide_read_file(str(f))
    assert res["ok"] is False


def test_ide_write_conflict_reported(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    read = bridge.ide_read_file(str(f))
    f.write_bytes(b"x = 1  # agent\n")
    res = bridge.ide_write_file(
        str(f), "x = 2\n", read["hash"], read["encoding"], read["bom"], read["eol"]
    )
    assert res["ok"] is False
    assert "conflict" in res


def test_ide_stat_files_reports_size_and_hash(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    res = bridge.ide_stat_files([str(f), str(ws / "nope.py")])
    assert res["ok"] is True
    assert res["stats"][0]["size"] == 6
    assert len(res["stats"][0]["hash"]) == 64
    assert res["stats"][1]["missing"] is True


def test_ide_set_open_files_returns_count(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    res = bridge.ide_set_open_files([str(f)])
    assert res["ok"] is True
    assert res["count"] == 1


def test_ide_poll_once_emits_event_on_change(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    events = []
    bridge.emit_event = lambda name, payload=None: events.append((name, payload))  # type: ignore[method-assign]

    bridge.ide_set_open_files([str(f)])
    base = os.stat(str(f)).st_mtime
    os.utime(str(f), (base + 10, base + 10))

    assert bridge._ide_poll_once() == 1
    assert events == [("ide_file_changed", {"path": os.path.realpath(str(f))})]
    # 无变化时不再派发
    assert bridge._ide_poll_once() == 0
    assert len(events) == 1


def test_ide_layout_roundtrip(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    # 修正：计划原文用 monkeypatch.setenv("YCODE_DATA_DIR", ...)，但 config.YCODE_DIR
    # 是导入期常量（conftest 收集阶段已导入 config），改环境变量不生效。直接打桩
    # config.YCODE_DIR，与 tests/test_ui_layout.py / tests/test_ide_layout_store.py 一致。
    import config

    monkeypatch.setattr(config, "YCODE_DIR", tmp_path, raising=False)
    saved = bridge.ide_save_layout("s1", {"tabs": [{"path": "a.py", "line": 3}], "active": "a.py", "breakpoints": []})
    assert saved["ok"] is True
    got = bridge.ide_get_layout("s1")
    assert got["ok"] is True
    assert got["layout"]["active"] == "a.py"


# ---------------------------------------------------------------------------
# expected_hash 必填（来自 Task 4 评审的 Important 发现）
# ---------------------------------------------------------------------------
def test_ide_write_file_requires_expected_hash(tmp_path, monkeypatch):
    """漏传 expected_hash 必须报 TypeError，绝不静默退化成无条件覆盖。

    空串在 write_text_file 里的语义是「调用方明确要求覆盖」，若桥方法给出
    expected_hash: str = "" 这样的宽松默认值，调用方漏传就悄悄关掉了全部冲突
    保护，用户会在不知情的情况下覆盖掉星瑶刚写的内容。
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)

    with pytest.raises(TypeError):
        bridge.ide_write_file(str(f), "x = 2\n")
    # 未落盘，磁盘一字不动
    assert f.read_bytes() == b"x = 1\n"


# ---------------------------------------------------------------------------
# 保存后回填轮询基线（来自 Task 5 评审的 Important 发现）
# ---------------------------------------------------------------------------
def test_ide_write_file_refreshes_watch_baseline(tmp_path, monkeypatch):
    """星瑶保存自己写盘的文件，轮询不得当成外部改动上报。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_bytes(b"x = 1\n")
    bridge = _make_bridge(str(ws), monkeypatch, tmp_path)
    bridge.ide_set_open_files([str(f)])

    read = bridge.ide_read_file(str(f))
    res = bridge.ide_write_file(
        str(f), "x = 2\n", read["hash"], read["encoding"], read["bom"], read["eol"]
    )
    assert res["ok"] is True
    # 保存动作必须回填基线：紧接着 poll 一次不得上报为外部改动
    assert bridge._ide_poll_once() == 0
