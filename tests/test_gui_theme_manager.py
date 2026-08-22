import json
import os
import stat
import subprocess
from unittest.mock import Mock
from zipfile import ZIP_STORED, ZipFile

import pytest
from PIL import Image

from core.gui_theme import ThemeError, ThemeRepository


def make_repo(tmp_path, config_data=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
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


def make_repo_with_saved_theme(tmp_path, theme_id):
    repo = make_repo(tmp_path)
    repo.save_theme(minimal_theme(theme_id))
    return repo


def write_image(path, image_format, size):
    Image.new("RGB", size, "#d96b9f").save(path, format=image_format)
    return path


def write_noisy_png(path, seed):
    side = 1800
    pixels = os.urandom(side * side * 3)
    Image.frombytes("RGB", (side, side), pixels).save(path, format="PNG")
    return path


def link_directory(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("当前 Windows 环境无法创建目录链接")


def make_junction(link, target):
    if os.name != "nt":
        pytest.skip("junction 仅适用于 Windows")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    attributes = os.lstat(link).st_file_attributes
    assert attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    assert not link.is_symlink()


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


def test_import_image_copies_valid_webp_into_theme(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = write_image(tmp_path / "source.webp", "WEBP", (32, 24))
    rel = repo.import_image("rose", "workspace", source)
    assert rel.startswith("assets/") and rel.endswith(".webp")
    assert (repo.root / "rose" / rel).is_file()
    assert repo.get_theme("rose")["regions"]["workspace"]["image"] == rel


@pytest.mark.parametrize("factory,code", [
    (lambda p: p.write_bytes(b"not an image"), "invalid_image"),
    (lambda p: write_image(p, "GIF", (32, 24)), "unsupported_image"),
])
def test_import_image_rejects_bad_content(tmp_path, factory, code):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = tmp_path / "input.bin"
    factory(source)
    with pytest.raises(ThemeError) as exc:
        repo.import_image("rose", "workspace", source)
    assert exc.value.code == code


def test_read_asset_rejects_escape(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    with pytest.raises(ThemeError) as exc:
        repo.read_asset("rose", "../config.json")
    assert exc.value.code == "asset_escape"


def test_read_asset_returns_validated_mime_type_and_bytes(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = write_image(tmp_path / "source.png", "PNG", (32, 24))
    relative_path = repo.import_image("rose", "workspace", source)
    mime_type, data = repo.read_asset("rose", relative_path)
    assert mime_type == "image/png"
    assert data == source.read_bytes()


def test_non_utf8_manifest_is_structured_invalid_manifest(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    (repo.root / "rose" / "theme.json").write_bytes(b"\xff\xfe\x00b\x00r\x00o\x00k\x00e\x00n")
    with pytest.raises(ThemeError) as exc:
        repo.get_theme("rose")
    assert exc.value.code == "invalid_manifest"


def test_import_image_rejects_oversize_source_before_parsing(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = tmp_path / "oversize.bin"
    # 全零内容不是合法图片：若大小检查没有先于内容解析，会得到 invalid_image
    source.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ThemeError) as exc:
        repo.import_image("rose", "workspace", source)
    assert exc.value.code == "image_too_large"


def test_read_asset_rejects_oversize_asset_before_parsing(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = write_image(tmp_path / "source.png", "PNG", (32, 24))
    relative_path = repo.import_image("rose", "workspace", source)
    (repo.root / "rose" / relative_path).write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
    with pytest.raises(ThemeError) as exc:
        repo.read_asset("rose", relative_path)
    assert exc.value.code == "image_too_large"


def test_app_image_round_trip_and_included_in_references(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    source = write_image(tmp_path / "bg.png", "PNG", (32, 24))
    relative_path = repo.import_image("rose", "app", source)
    assert relative_path.startswith("assets/")
    theme = repo.get_theme("rose")
    assert theme["app_image"]["image"] == relative_path
    # 全局图计入引用集，可读回且导出时被收进 ZIP
    mime_type, _ = repo.read_asset("rose", relative_path)
    assert mime_type == "image/png"
    destination = tmp_path / "out.zip"
    repo.export_zip("rose", destination)
    with ZipFile(destination) as archive:
        assert relative_path in archive.namelist()


def test_app_image_removed_and_unreferenced_asset_deleted(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    relative_path = repo.import_image(
        "rose", "app", write_image(tmp_path / "bg.png", "PNG", (32, 24)),
    )
    updated = repo.remove_image("rose", "app")
    assert updated["app_image"]["image"] is None
    assert not (repo.root / "rose" / relative_path).exists()


def test_app_image_invalid_scope_rejected(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    with pytest.raises(ThemeError) as exc:
        repo.import_image("rose", "no_such_scope", tmp_path / "x.png")
    assert exc.value.code == "invalid_region"


def test_old_theme_without_app_image_migrates_default(tmp_path):
    # 旧清单无 app_image：读取时补默认值，活动主题不因新增字段损坏
    repo = make_repo(tmp_path)
    theme = minimal_theme("legacy")
    theme.pop("app_image", None)  # minimal_theme 本就不含，确保显式缺失
    theme_dir = repo.root / "legacy"
    theme_dir.mkdir(parents=True)
    (theme_dir / "theme.json").write_text(json.dumps(theme), encoding="utf-8")
    loaded = repo.get_theme("legacy")
    assert loaded["app_image"]["image"] is None
    assert loaded["app_image"]["image_size"] == "cover"


def test_remove_image_clears_manifest_and_unreferenced_asset(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    relative_path = repo.import_image(
        "rose", "workspace", write_image(tmp_path / "source.png", "PNG", (32, 24)),
    )
    updated = repo.remove_image("rose", "workspace")
    assert updated["regions"]["workspace"]["image"] is None
    assert not (repo.root / "rose" / relative_path).exists()


def test_import_zip_rejects_zip_slip_without_partial_directory(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "evil.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("evil")))
        zf.writestr("../escaped.txt", "owned")
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "asset_escape"
    assert not (repo.root / "evil").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_import_zip_rejects_declared_size_over_30_mib(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "large.zip"
    with ZipFile(archive, "w", compression=ZIP_STORED) as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("large")))
        with zf.open("assets/large.bin", "w") as entry:
            chunk = b"0" * (1024 * 1024)
            for _ in range(31):
                entry.write(chunk)
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "theme_too_large"


def test_manifest_rejects_remote_image_url(tmp_path):
    theme = minimal_theme("rose")
    theme["regions"] = {"workspace": {"image": "https://example.com/rose.webp"}}
    with pytest.raises(ThemeError) as exc:
        make_repo(tmp_path).save_theme(theme)
    assert exc.value.code == "asset_escape"


def test_export_and_import_zip_round_trip_referenced_image(tmp_path):
    source_repo = make_repo_with_saved_theme(tmp_path / "source", "rose")
    source_repo.import_image(
        "rose", "workspace", write_image(tmp_path / "rose.png", "PNG", (32, 24)),
    )
    (source_repo.root / "rose" / "assets" / "unused.bin").write_bytes(b"unused")
    archive = source_repo.export_zip("rose", tmp_path / "rose.zip")
    with ZipFile(archive) as zf:
        asset_names = [name for name in zf.namelist() if name.startswith("assets/") and name != "assets/"]
        assert sorted(zf.namelist()) == [asset_names[0], "theme.json"]

    imported = make_repo(tmp_path / "target").import_zip(archive)
    assert imported["id"] == "rose"
    assert imported["regions"]["workspace"]["image"].startswith("assets/")


def test_delete_theme_rejects_active_and_deletes_inactive_theme(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    repo.set_active("rose")
    with pytest.raises(ThemeError) as exc:
        repo.delete_theme("rose")
    assert exc.value.code == "active_theme_delete"

    repo.set_active("builtin.light")
    repo.delete_theme("rose")
    assert not (repo.root / "rose").exists()


@pytest.mark.parametrize("entry_name", ["assets/payload.js", "assets/clip.mp4", "assets/animated.gif"])
def test_import_zip_rejects_unreferenced_asset_entries(tmp_path, entry_name):
    repo = make_repo(tmp_path)
    archive = tmp_path / "payload.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("evil")))
        zf.writestr(entry_name, b"unreferenced payload")
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "invalid_archive"
    assert not (repo.root / "evil").exists()


def test_import_zip_rejects_referenced_asset_with_unsupported_content(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "animated.zip"
    theme = minimal_theme("evil")
    theme["regions"] = {"workspace": {"image": "assets/animated.gif"}}
    gif = write_image(tmp_path / "animated.gif", "GIF", (32, 24))
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(theme))
        zf.write(gif, "assets/animated.gif")
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "unsupported_image"
    assert not (repo.root / "evil").exists()


def test_import_image_replaces_unshared_old_asset(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    old_path = repo.import_image(
        "rose", "workspace", write_image(tmp_path / "old.png", "PNG", (32, 24)),
    )
    new_path = repo.import_image(
        "rose", "workspace", write_image(tmp_path / "new.png", "PNG", (33, 24)),
    )
    assert new_path != old_path
    assert not (repo.root / "rose" / old_path).exists()


def test_import_image_rejects_theme_total_over_30_mib(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    for region, seed in zip(("app_header", "sidebar", "workspace"), range(3)):
        repo.import_image(region=region, theme_id="rose", source=write_noisy_png(tmp_path / f"{seed}.png", seed))
    with pytest.raises(ThemeError) as exc:
        repo.import_image(
            "rose", "composer", write_noisy_png(tmp_path / "next.png", 3),
        )
    assert exc.value.code == "theme_too_large"
    assert repo.get_theme("rose")["regions"]["composer"]["image"] is None


@pytest.mark.parametrize("asset_path", ["assets//a.png", "assets/./a.png", "assets/a.png/"])
def test_manifest_rejects_noncanonical_asset_paths(tmp_path, asset_path):
    theme = minimal_theme("rose")
    theme["regions"] = {"workspace": {"image": asset_path}}
    with pytest.raises(ThemeError) as exc:
        make_repo(tmp_path).save_theme(theme)
    assert exc.value.code == "asset_escape"


def test_import_zip_rejects_noncanonical_referenced_asset_path(tmp_path):
    repo = make_repo(tmp_path)
    archive = tmp_path / "noncanonical.zip"
    theme = minimal_theme("evil")
    theme["regions"] = {"workspace": {"image": "assets//background.png"}}
    image = write_image(tmp_path / "background.png", "PNG", (32, 24))
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(theme))
        zf.write(image, "assets//background.png")
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "asset_escape"


def test_import_image_rejects_source_with_symlinked_parent(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    outside = tmp_path / "outside"
    outside.mkdir()
    image = write_image(outside / "source.png", "PNG", (32, 24))
    linked_parent = tmp_path / "linked-parent"
    link_directory(linked_parent, outside)
    with pytest.raises(ThemeError) as exc:
        repo.import_image("rose", "workspace", linked_parent / image.name)
    assert exc.value.code == "asset_escape"


def test_read_asset_rejects_resource_with_symlinked_parent_inside_theme(tmp_path):
    repo = make_repo_with_saved_theme(tmp_path, "rose")
    asset_path = repo.import_image(
        "rose", "workspace", write_image(tmp_path / "source.png", "PNG", (32, 24)),
    )
    assets = repo.root / "rose" / "assets"
    real_assets = repo.root / "rose" / "assets-real"
    assets.rename(real_assets)
    link_directory(assets, real_assets)
    with pytest.raises(ThemeError) as exc:
        repo.read_asset("rose", asset_path)
    assert exc.value.code == "asset_escape"


def test_save_theme_rejects_junction_destination_without_external_write(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    repo.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(repo.root / "rose", outside)
    monkeypatch.setattr(type(repo.root), "is_junction", lambda _path: False, raising=False)
    with pytest.raises(ThemeError) as exc:
        repo.save_theme(minimal_theme("rose"))
    assert exc.value.code == "asset_escape"
    assert not (outside / "theme.json").exists()


def test_import_zip_rejects_junction_root_without_external_write(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    config_file = tmp_path / "config.json"
    config_file.write_text("{}", encoding="utf-8")
    root = tmp_path / "gui-themes"
    make_junction(root, outside)
    repo = ThemeRepository(root, config_file)
    monkeypatch.setattr(type(root), "is_junction", lambda _path: False, raising=False)
    archive = tmp_path / "evil.zip"
    with ZipFile(archive, "w") as zf:
        zf.writestr("theme.json", json.dumps(minimal_theme("evil")))
    with pytest.raises(ThemeError) as exc:
        repo.import_zip(archive)
    assert exc.value.code == "asset_escape"
    assert not (outside / "evil").exists()
