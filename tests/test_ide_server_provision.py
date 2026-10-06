# -*- coding: utf-8 -*-
"""语言服务器可执行文件的定位优先级与手动覆盖。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ide import language_registry, server_provision as sp


def _prepare(tmp_path, monkeypatch):
    """把数据目录与内置件目录都指到临时目录，测试之间互不污染。"""
    monkeypatch.setenv("YCODE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(sp, "_builtin_dir", lambda: str(tmp_path / "bin"))
    sp.invalidate_cache()


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_missing_when_nowhere_found(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "missing"
    assert res["executable"] == ""
    assert "jedi-language-server" in res["msg"]
    assert "手动指定" in res["msg"]


def test_bundled_wins_over_path(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    _touch(tmp_path / "bin" / "jedi-language-server")
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/" + name)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "bundled"
    assert res["executable"].endswith("jedi-language-server")
    assert res["extra_args"] == []


def test_path_used_when_no_bundled(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/opt/ts/" + name)
    res = sp.resolve_executable("typescript")
    assert res["source"] == "path"
    assert res["executable"].endswith("typescript-language-server")


def test_override_wins_over_bundled(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    _touch(tmp_path / "bin" / "jedi-language-server")
    custom = _touch(tmp_path / "my-jedi")
    sp.save_override("jedi", str(custom))
    res = sp.resolve_executable("jedi")
    assert res["source"] == "override"
    assert res["executable"] == os.path.realpath(str(custom))


def test_save_override_rejects_missing_file(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(sp.ProvisionError):
        sp.save_override("jedi", str(tmp_path / "nope"))


def test_save_override_rejects_unknown_server(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    f = _touch(tmp_path / "x")
    with pytest.raises(sp.ProvisionError):
        sp.save_override("no-such-server", str(f))


def test_stale_override_falls_back_and_explains(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    gone = _touch(tmp_path / "gone")
    sp.save_override("jedi", str(gone))
    os.unlink(str(gone))
    sp.invalidate_cache()
    monkeypatch.setattr(sp.shutil, "which", lambda name: "/usr/bin/" + name)
    res = sp.resolve_executable("jedi")
    assert res["source"] == "path"
    assert "失效" in res["msg"]


def test_clear_override_restores_autodetect(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    custom = _touch(tmp_path / "my-jedi")
    sp.save_override("jedi", str(custom))
    cleared = sp.clear_override("jedi")
    assert cleared["cleared"] is True
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    assert sp.resolve_executable("jedi")["source"] == "missing"


def test_lookup_is_cached_until_invalidated(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    calls = {"n": 0}

    def _which(name):
        calls["n"] += 1
        return None

    monkeypatch.setattr(sp.shutil, "which", _which)
    sp.resolve_executable("jedi")
    first = calls["n"]
    sp.resolve_executable("jedi")
    assert calls["n"] == first
    sp.invalidate_cache()
    sp.resolve_executable("jedi")
    assert calls["n"] > first


def test_all_server_status_covers_every_spec(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(sp.shutil, "which", lambda name: None)
    rows = sp.all_server_status()
    assert len(rows) == len(language_registry.SPECS)
    assert {r["server"] for r in rows} == set(language_registry.SPECS)
    python_row = [r for r in rows if r["server"] == "jedi"][0]
    assert python_row["languages"] == ["python"]
    assert python_row["command"] == "jedi-language-server"
