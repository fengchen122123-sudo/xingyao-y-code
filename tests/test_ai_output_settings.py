from core import ai_output_paths as aop


def _stub_snapshot():
    return {
        "ai_output_dir": "X:/ai_out",
        "ai_output_dir_setting": "X:/ai_out",
        "ai_output_dir_default": "X:/tmp",
    }


def test_system_settings_snapshot_contains_ai_output_fields(tmp_path, monkeypatch):
    from core.command_shell import get_system_settings

    target = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(target))
    settings = get_system_settings()
    assert settings["ai_output_dir"] == str(target.resolve())
    assert settings["ai_output_dir_setting"] == str(target)
    assert settings["ai_output_dir_default"].endswith("tmp")


def test_system_settings_default_respects_data_dir(tmp_path, monkeypatch):
    import config
    from core.command_shell import get_system_settings

    data_dir = tmp_path / "data"
    monkeypatch.setattr(aop, "_read_setting", lambda: "")
    monkeypatch.setattr(config, "YCODE_DIR", str(data_dir))
    settings = get_system_settings()
    assert settings["ai_output_dir_default"] == str(data_dir / "tmp")


def test_set_ai_output_dir_rejects_unwritable(tmp_path, monkeypatch):
    from gui_bridge import ApiBridge

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    saved = {}
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    # 桥接实现只读 self 的 get_system_settings：用替身避免真实 ApiBridge 实例化成本
    monkeypatch.setattr(ApiBridge, "get_system_settings", lambda self: _stub_snapshot())
    bridge = ApiBridge.__new__(ApiBridge)
    result = bridge.set_ai_output_dir(str(blocked))
    assert result["ok"] is False
    assert result["msg"]
    assert result["settings"] == _stub_snapshot()
    assert saved == {}, "校验失败时不得写盘"


def test_set_ai_output_dir_success_returns_effective_dir(tmp_path, monkeypatch):
    from gui_bridge import ApiBridge

    saved = {}
    monkeypatch.setattr(aop, "_is_writable", lambda path: True)
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    monkeypatch.setattr(ApiBridge, "get_system_settings", lambda self: _stub_snapshot())
    bridge = ApiBridge.__new__(ApiBridge)
    target = tmp_path / "custom_out"
    result = bridge.set_ai_output_dir(str(target))
    assert result["ok"] is True
    assert saved["v"] == str(target)
    assert result["settings"] == _stub_snapshot()
    assert str(target.resolve()) in result["msg"]
