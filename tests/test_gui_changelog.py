import gui_bridge
from core.version import __version__ as YCODE_VERSION


def _bare_bridge():
    return gui_bridge.ApiBridge.__new__(gui_bridge.ApiBridge)


def test_get_changelog_first_entry_matches_current_version():
    res = _bare_bridge().get_changelog()
    assert res["ok"] is True
    assert res["entries"], "更新日志不能为空"
    assert res["entries"][0]["version"] == YCODE_VERSION


def test_get_changelog_entry_shape():
    res = _bare_bridge().get_changelog()
    assert res["ok"] is True
    for entry in res["entries"]:
        assert entry["version"]
        assert isinstance(entry["notes"], list) and entry["notes"]
        assert all(isinstance(n, str) and n for n in entry["notes"])


def test_get_changelog_missing_file_reports_failure(monkeypatch):
    monkeypatch.setattr(gui_bridge.os.path, "isfile", lambda _p: False)
    res = _bare_bridge().get_changelog()
    assert res["ok"] is False
    assert res["entries"] == []
