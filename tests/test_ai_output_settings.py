"""AI 生成内容目录设置项：系统设置快照字段与 GUI 桥接行为。

对应实施计划 Task 4（docs/superpowers/plans/2026-09-20-ai-output-dir.md）。
测试全程 monkeypatch `core.ai_output_paths` 的读写探测函数，
绝不触碰真实 ~/.ycode/config.json。
"""

from core import ai_output_paths as aop


def _stub_snapshot():
    """替身快照：只需证明桥接把 settings 原样回传，不依赖真实配置。"""
    return {
        "ai_output_dir": "X:/ai_out",
        "ai_output_dir_setting": "X:/ai_out",
        "ai_output_dir_default": "X:/tmp",
    }


def test_system_settings_snapshot_contains_ai_output_fields(tmp_path, monkeypatch):
    """快照必须同时给出生效值、原始配置值与默认值三个字段。"""
    from core.command_shell import get_system_settings

    target = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(target))
    settings = get_system_settings()
    assert settings["ai_output_dir"] == str(target.resolve())
    assert settings["ai_output_dir_setting"] == str(target)
    assert settings["ai_output_dir_default"].endswith("tmp")


def test_system_settings_default_respects_data_dir(tmp_path, monkeypatch):
    """默认目录走 config.YCODE_DIR，必须跟随 YCODE_DATA_DIR 覆盖。"""
    import config
    from core.command_shell import get_system_settings

    data_dir = tmp_path / "data"
    monkeypatch.setattr(aop, "_read_setting", lambda: "")
    monkeypatch.setattr(config, "YCODE_DIR", str(data_dir))
    settings = get_system_settings()
    assert settings["ai_output_dir_default"] == str(data_dir / "tmp")


def test_set_ai_output_dir_rejects_unwritable(tmp_path, monkeypatch):
    """不可写目录必须显式拒绝（ok is False）且不写盘，不做静默降级。"""
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
    """可写目录：写盘成功，返回 ok=True 与新快照。"""
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
