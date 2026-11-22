# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.gui_path_policy as gpp


def _inject(monkeypatch, tmp_path):
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(tmp_path / "ydata"))


def test_edit_allowed_inside_workspace(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    f = ws / "a.py"
    f.write_text("x = 1", encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is True
    assert msg == ""


def test_edit_denied_outside_workspace_when_sandbox_off(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is False
    assert "工作区" in msg


def test_edit_allowed_outside_workspace_when_sandbox_on(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    ok, _ = gpp.gui_path_access_allowed(str(f), str(ws), action="edit")
    assert ok is True


def test_edit_denied_on_dotdot_traversal(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    sneaky = os.path.join(str(ws), "..", "outside", "b.py")
    ok, _ = gpp.gui_path_access_allowed(sneaky, str(ws), action="edit")
    assert ok is False


def test_edit_credential_config_always_denied(tmp_path, monkeypatch):
    ydata = tmp_path / "ydata"
    ydata.mkdir()
    monkeypatch.setattr(gpp, "_ycode_dir_override", str(ydata))
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: True)
    f = ydata / "config.json"
    f.write_text('{"api_key": "secret"}', encoding="utf-8")
    ok, msg = gpp.gui_path_access_allowed(str(f), "GLOBAL", action="edit")
    assert ok is False
    assert "编辑" in msg


def test_preview_action_noun_unchanged(tmp_path, monkeypatch):
    _inject(monkeypatch, tmp_path)
    monkeypatch.setattr(gpp, "_allow_outside_workspace", lambda: False)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    f = outside / "b.py"
    f.write_text("y = 2", encoding="utf-8")
    _, msg_preview = gpp.gui_path_access_allowed(str(f), str(ws), action="preview")
    _, msg_open = gpp.gui_path_access_allowed(str(f), str(ws), action="open")
    assert "预览" in msg_preview
    assert "打开" in msg_open
