import json
import os
from unittest.mock import Mock

import pytest

from core.gui_theme import ThemeError, ThemeRepository


def make_repo(tmp_path, config_data=None):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config_data or {}), encoding="utf-8")
    return ThemeRepository(tmp_path / "gui-themes", config_file)


def minimal_theme(theme_id, accent=None):
    tokens = {} if accent is None else {"accent": accent}
    return {
        "schema_version": 1,
        "id": theme_id,
        "name": theme_id,
        "appearance": "light",
        "extends": "builtin.light",
        "tokens": tokens,
        "native_titlebar": {},
        "regions": {},
    }


def test_builtin_dark_is_complete(tmp_path):
    repo = ThemeRepository(tmp_path / "gui-themes", tmp_path / "config.json")
    theme = repo.get_theme("builtin.dark")
    assert theme["id"] == "builtin.dark"
    assert set(theme["regions"]) == {
        "app_header", "sidebar", "conversation_toolbar", "workspace",
        "composer", "right_panel", "statusbar",
    }
    assert theme["native_titlebar"]["background"] == "#20201e"


def test_user_theme_inherits_only_missing_fields(tmp_path):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose", accent="#d96b9f"))
    theme = repo.get_theme("rose")
    assert theme["tokens"]["accent"] == "#d96b9f"
    assert theme["tokens"]["text_primary"] == repo.get_theme("builtin.light")["tokens"]["text_primary"]


@pytest.mark.parametrize("field,value,code", [
    ("id", "../escape", "invalid_id"),
    ("schema_version", 2, "unsupported_schema"),
    ("extends", "rose-parent", "invalid_base"),
])
def test_invalid_manifest_is_rejected(tmp_path, field, value, code):
    repo = make_repo(tmp_path)
    manifest = minimal_theme("rose")
    manifest[field] = value
    with pytest.raises(ThemeError) as exc:
        repo.save_theme(manifest)
    assert exc.value.code == code


def test_list_themes_includes_builtin_and_user_summaries(tmp_path):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose"))
    assert repo.list_themes() == [
        {"id": "builtin.dark", "name": "内置深色", "appearance": "dark", "extends": "builtin.dark", "builtin": True},
        {"id": "builtin.light", "name": "内置浅色", "appearance": "light", "extends": "builtin.light", "builtin": True},
        {"id": "rose", "name": "rose", "appearance": "light", "extends": "builtin.light", "builtin": False},
    ]


def test_old_light_flag_migrates_once(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text('{"gui_theme_light": true, "theme": "dracula"}', encoding="utf-8")
    repo = ThemeRepository(tmp_path / "gui-themes", config_file)
    assert repo.get_active()["id"] == "builtin.light"
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["gui_theme_id"] == "builtin.light"
    assert saved["theme"] == "dracula"


def test_invalid_active_theme_does_not_rewrite_config(tmp_path):
    repo = make_repo(tmp_path, {"gui_theme_id": "missing-theme"})
    before = repo.config_file.read_bytes()
    with pytest.raises(ThemeError) as exc:
        repo.get_active()
    assert exc.value.code == "theme_not_found"
    assert repo.config_file.read_bytes() == before


def test_set_active_writes_only_resolved_theme_id(tmp_path):
    repo = make_repo(tmp_path, {"theme": "dracula"})
    selected = repo.set_active("builtin.light")
    assert selected["id"] == "builtin.light"
    assert json.loads(repo.config_file.read_text(encoding="utf-8")) == {
        "theme": "dracula", "gui_theme_id": "builtin.light",
    }


def test_failed_save_preserves_previous_manifest(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose"))
    before = (repo.root / "rose" / "theme.json").read_bytes()
    monkeypatch.setattr(os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        repo.save_theme({**minimal_theme("rose"), "name": "changed"})
    assert (repo.root / "rose" / "theme.json").read_bytes() == before


def test_copy_builtin_creates_editable_user_theme(tmp_path):
    repo = make_repo(tmp_path)
    copied = repo.copy_theme("builtin.light", "rose", "Rose")
    assert copied["id"] == "rose"
    assert copied["name"] == "Rose"
    assert copied["builtin"] is False
    assert (repo.root / "rose" / "theme.json").is_file()


def test_rename_theme_moves_directory_and_preserves_assets(tmp_path):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme("rose"))
    asset = repo.root / "rose" / "assets" / "background.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"asset")
    renamed = repo.rename_theme("rose", "rose-new", "Rose New")
    assert renamed["id"] == "rose-new"
    assert (repo.root / "rose-new" / "assets" / "background.png").read_bytes() == b"asset"
    assert not (repo.root / "rose").exists()
