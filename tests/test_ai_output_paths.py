"""AI 生成内容目录解析测试（规格：2026-09-20-ai-output-dir-design.md）。"""

import os
import stat
from pathlib import Path

import pytest

from core import ai_output_paths as aop
from core.workspace_context import workspace_scope


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离配置读取与默认目录，测试绝不触碰真实 ~/.ycode。"""
    default_dir = tmp_path / "data" / "tmp"
    monkeypatch.setattr(aop, "default_ai_output_dir", lambda: default_dir)
    monkeypatch.setattr(aop, "_read_setting", lambda: "")
    return default_dir


def test_unset_setting_falls_back_to_default_and_creates_dir(isolated):
    resolved = aop.resolve_ai_output_dir()
    assert resolved == isolated.resolve()
    assert resolved.is_dir(), "默认目录不存在时应自动创建"


def test_configured_dir_wins(isolated, tmp_path, monkeypatch):
    custom = tmp_path / "my_ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(custom))
    assert aop.resolve_ai_output_dir() == custom.resolve()
    assert custom.is_dir()


def test_tilde_is_expanded(isolated, monkeypatch):
    monkeypatch.setattr(aop, "_read_setting", lambda: "~/ycode_ai_out")
    resolved = aop.resolve_ai_output_dir()
    assert resolved == (Path.home() / "ycode_ai_out").resolve()


def test_workdir_inside_configured_dir_returns_workdir(isolated, tmp_path, monkeypatch):
    """规则：当前工作目录已在配置目录内 → 直接用工作目录，不再套一层。"""
    configured = tmp_path / "ai_out"
    project = configured / "proj"
    project.mkdir(parents=True)
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    with workspace_scope(str(project)):
        assert aop.resolve_ai_output_dir() == project.resolve()


def test_workdir_equal_to_configured_dir_returns_workdir(isolated, tmp_path, monkeypatch):
    configured = tmp_path / "ai_out"
    configured.mkdir(parents=True)
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    with workspace_scope(str(configured)):
        assert aop.resolve_ai_output_dir() == configured.resolve()


def test_unwritable_configured_dir_falls_back_to_default(isolated, tmp_path, monkeypatch):
    """配置目录不可写 → 回退默认目录，不抛异常、不静默丢文件。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setattr(aop, "_read_setting", lambda: str(blocked))
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    assert aop.resolve_ai_output_dir() == isolated.resolve()


def test_is_ai_output_path_matches_dir_and_children(isolated, tmp_path, monkeypatch):
    configured = tmp_path / "ai_out"
    monkeypatch.setattr(aop, "_read_setting", lambda: str(configured))
    assert aop.is_ai_output_path(configured / "hunyuan_20260920_120000.jpg")
    assert aop.is_ai_output_path(configured)
    assert not aop.is_ai_output_path(tmp_path / "other" / "x.png")
    assert not aop.is_ai_output_path("")


def test_set_setting_rejects_unwritable_dir(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    blocked = tmp_path / "blocked2"
    blocked.mkdir()
    monkeypatch.setattr(aop, "_is_writable", lambda path: False)
    with pytest.raises(aop.AiOutputDirError):
        aop.set_ai_output_dir_setting(str(blocked))
    assert saved == {}, "校验失败时不得写盘"


def test_set_setting_persists_and_creates_dir(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    target = tmp_path / "custom_out"
    result = aop.set_ai_output_dir_setting(str(target))
    assert saved["v"] == str(target)
    assert result == target.resolve() and target.is_dir()


def test_empty_setting_resets_to_default(isolated, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(aop, "_write_setting", lambda value: saved.setdefault("v", value) or True)
    assert aop.set_ai_output_dir_setting("") == isolated.resolve()
    assert saved["v"] == ""
