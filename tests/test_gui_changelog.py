import gui_bridge
from core.version import __version__ as YCODE_VERSION


def _bare_bridge():
    """不经 __init__ 构造裸实例，避免启动期的重初始化副作用。"""
    return gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)


def test_get_changelog_first_entry_matches_current_version():
    """随包 CHANGELOG.md 的最新条目必须与运行版本一致，否则关于页展示错内容。"""
    res = _bare_bridge().get_changelog()
    assert res["ok"] is True
    assert res["entries"], "更新日志不能为空"
    assert res["entries"][0]["version"] == YCODE_VERSION


def test_get_changelog_entry_shape():
    """每条目都应有版本号与非空更新点列表，日期可缺省。"""
    res = _bare_bridge().get_changelog()
    assert res["ok"] is True
    for entry in res["entries"]:
        assert entry["version"]
        assert isinstance(entry["notes"], list) and entry["notes"]
        assert all(isinstance(n, str) and n for n in entry["notes"])


def test_get_changelog_missing_file_reports_failure(monkeypatch):
    """文件缺失时返回 ok=False 而非抛异常（关于页显示"暂无更新说明"）。"""
    monkeypatch.setattr(gui_bridge.os.path, "isfile", lambda _p: False)
    res = _bare_bridge().get_changelog()
    assert res["ok"] is False
    assert res["entries"] == []
