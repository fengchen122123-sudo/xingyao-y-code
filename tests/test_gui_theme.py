import copy
from unittest.mock import Mock

import pytest

import config
import core.model_manager as model_manager
import gui_bridge
import gui_main
from core.gui_theme import BUILTIN_THEMES, ThemeError
from gui_bridge import ApiBridge


def _use_tmp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(model_manager, "_CONFIG_FILE", str(cfg))
    return cfg


def _bare_bridge():
    """不经 __init__ 构造裸实例，避免启动期的重初始化副作用。"""
    bridge = ApiBridge.__new__(ApiBridge)
    bridge._hwnd = 0
    bridge._titlebar_dark = True
    return bridge


def complete_light_theme():
    return copy.deepcopy(BUILTIN_THEMES["builtin.light"])


class BrokenRepo:
    def list_themes(self):
        raise ThemeError("theme_not_found", "gui_theme_id", "missing")


class RecordingRepo:
    def __init__(self, theme):
        self.theme = theme
        self.calls = []

    def get_theme(self, theme_id):
        self.calls.append(f"get:{theme_id}")
        return self.theme

    def set_active(self, theme_id):
        self.calls.append(f"set_active:{theme_id}")
        return self.theme

    def get_active(self):
        self.calls.append("get_active")
        return self.theme


class RepoWithActive:
    def __init__(self, theme):
        self.theme = theme

    def get_active(self):
        return self.theme


def test_list_gui_themes_returns_structured_error(monkeypatch):
    bridge = _bare_bridge()
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: BrokenRepo())
    result = bridge.list_gui_themes()
    assert result == {
        "ok": False,
        "error": {"code": "theme_not_found", "path": "gui_theme_id", "message": "missing"},
    }


def test_set_active_gui_theme_applies_dwm_before_persisting(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=True))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is True
    assert repo.calls == ["get:rose", "set_active:rose"]


def test_dwm_failure_does_not_change_active_id(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", Mock(return_value=False))
    result = bridge.set_active_gui_theme("rose")
    assert result["ok"] is False
    assert result["error"]["code"] == "native_titlebar_failed"
    assert repo.calls == ["get:rose"]


def test_preview_and_restore_titlebar_do_not_change_active_id(monkeypatch):
    bridge = _bare_bridge()
    repo = RecordingRepo(theme=complete_light_theme())
    monkeypatch.setattr(gui_bridge, "get_gui_theme_repository", lambda: repo)
    apply = Mock(return_value=True)
    monkeypatch.setattr(bridge, "_apply_titlebar_colors", apply)
    assert bridge.preview_native_titlebar(complete_light_theme()["native_titlebar"])["ok"] is True
    assert bridge.restore_native_titlebar()["ok"] is True
    assert repo.calls == ["get_active"]
    assert not any(call.startswith("set_active:") for call in repo.calls)


@pytest.mark.parametrize("color,expected", [
    ("#20201e", 0x001E2020),
    ("#ffffff", 0x00FFFFFF),
    ("#6a92d8", 0x00D8926A),
])
def test_rgb_hex_to_colorref(color, expected):
    assert gui_bridge._rgb_hex_to_colorref(color) == expected


def test_initial_theme_metadata_comes_from_repository(monkeypatch):
    monkeypatch.setattr(gui_main, "get_gui_theme_repository", lambda: RepoWithActive(complete_light_theme()))
    meta = gui_main._initial_gui_theme()
    assert meta == {"appearance": "light", "background": "#f4f4f1"}


def test_set_gui_theme_persists_light(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    bridge = _bare_bridge()
    res = bridge.set_gui_theme(True)
    assert res["ok"] is True
    assert model_manager.load_config().get("gui_theme_light") is True
    # 标题栏同步为浅色（dark=False）
    assert bridge._titlebar_dark is False


def test_set_gui_theme_persists_dark(tmp_path, monkeypatch):
    _use_tmp_config(tmp_path, monkeypatch)
    bridge = _bare_bridge()
    bridge.set_gui_theme(True)
    bridge.set_gui_theme(False)
    assert model_manager.load_config().get("gui_theme_light") is False
    assert bridge._titlebar_dark is True


def test_theme_light_defaults_to_false_in_config(tmp_path, monkeypatch):
    """老配置没有 gui_theme_light 键时按深色处理。"""
    _use_tmp_config(tmp_path, monkeypatch)
    assert bool(model_manager.load_config().get("gui_theme_light", False)) is False
