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


@pytest.mark.parametrize("theme_id,expected_tokens", [
    ("builtin.dark", {
        "background_app": "#1a1a18", "background_panel": "#20201e",
        "background_card": "#272724", "background_elevated": "#302f2b",
        "border": "#34332f", "text_primary": "#d9d9d4", "text_bright": "#edede9",
        "text_secondary": "#a8a7a0", "text_faint": "#85847d", "accent": "#6a92d8",
        "accent_secondary": "#6a92d8", "accent_soft": "#6a92d829",
        "success": "#4ea96a", "warning": "#c9a24a", "danger": "#d9635b",
        "orange": "#d08a4e", "info": "#4aabba", "purple": "#a58bd8",
        "code_background": "#141412", "float_background": "#20201ef7",
        "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
        "font_sans": "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
    }),
    ("builtin.light", {
        "background_app": "#f4f4f1", "background_panel": "#ffffff",
        "background_card": "#efefec", "background_elevated": "#e4e4e0",
        "border": "#e3e3de", "text_primary": "#2c2c2a", "text_bright": "#171716",
        "text_secondary": "#5c5c56", "text_faint": "#7a7a73", "accent": "#3a6dd8",
        "accent_secondary": "#3a6dd8", "accent_soft": "#3a6dd81a",
        "success": "#2c8a4c", "warning": "#96700f", "danger": "#cf3d36",
        "orange": "#b85e1f", "info": "#1a7f92", "purple": "#7b5fc0",
        "code_background": "#f0f0ec", "float_background": "#fffffff7",
        "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
        "font_sans": "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
    }),
])
def test_builtin_tokens_cover_every_current_css_variable(tmp_path, theme_id, expected_tokens):
    tokens = ThemeRepository(tmp_path / "gui-themes", tmp_path / "config.json").get_theme(theme_id)["tokens"]
    assert {key: tokens[key] for key in expected_tokens} == expected_tokens


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


@pytest.mark.parametrize("field,value,code", [
    ("appearance", [], "invalid_appearance"),
    ("appearance", {}, "invalid_appearance"),
    ("extends", [], "invalid_base"),
    ("extends", {}, "invalid_base"),
])
def test_enum_fields_reject_unhashable_json_values_with_theme_error(tmp_path, field, value, code):
    manifest = minimal_theme("rose")
    manifest[field] = value
    with pytest.raises(ThemeError) as exc:
        make_repo(tmp_path).save_theme(manifest)
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
