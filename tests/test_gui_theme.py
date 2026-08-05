import config
import core.model_manager as model_manager
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
