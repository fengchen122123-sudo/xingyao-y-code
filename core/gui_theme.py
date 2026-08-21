"""GUI 主题 Schema v1、持久化及活动主题选择。"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import config


SCHEMA_VERSION = 1
THEME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BUILTIN_IDS = ("builtin.dark", "builtin.light")
REGION_IDS = (
    "app_header", "sidebar", "conversation_toolbar", "workspace",
    "composer", "right_panel", "statusbar",
)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")

_ROOT_KEYS = frozenset({
    "schema_version", "id", "name", "appearance", "extends", "tokens",
    "native_titlebar", "regions",
})
_TOKEN_COLOR_KEYS = frozenset({
    "background_app", "background_panel", "background_card", "background_elevated",
    "text_primary", "text_bright", "text_secondary", "text_faint", "accent",
    "accent_secondary", "accent_soft", "border", "success", "warning", "danger",
    "orange", "info", "purple", "code_background", "float_background", "shadow_color",
})
_TOKEN_FONT_KEYS = frozenset({"font_sans", "font_mono"})
_TOKEN_RADIUS_KEYS = frozenset({"radius_small", "radius_medium", "radius_large"})
_TOKEN_NUMBER_KEYS = _TOKEN_RADIUS_KEYS | frozenset({"shadow_strength"})
_TOKEN_KEYS = _TOKEN_COLOR_KEYS | _TOKEN_FONT_KEYS | _TOKEN_NUMBER_KEYS
_TITLEBAR_KEYS = frozenset({"background", "text", "border", "mode"})
_REGION_KEYS = frozenset({
    "background", "text", "border", "accent", "background_opacity", "image",
    "image_opacity", "image_size", "image_position",
})
_REGION_COLOR_KEYS = frozenset({"background", "text", "border", "accent"})
_APPEARANCES = frozenset({"light", "dark"})
_IMAGE_SIZES = frozenset({"cover", "contain", "auto"})
_IMAGE_POSITIONS = frozenset({"center", "top", "bottom", "left", "right"})


class ThemeError(ValueError):
    def __init__(self, code: str, path: str, message: str):
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _region(background: str, text: str, border: str, accent: str) -> dict[str, Any]:
    return {
        "background": background,
        "text": text,
        "border": border,
        "accent": accent,
        "background_opacity": 1.0,
        "image": None,
        "image_opacity": 0.0,
        "image_size": "cover",
        "image_position": "center",
    }


def _builtin_theme(
    theme_id: str,
    appearance: str,
    tokens: dict[str, Any],
    titlebar: dict[str, Any],
) -> dict[str, Any]:
    region_defaults = _region(
        tokens["background_panel"], tokens["text_primary"], tokens["border"], tokens["accent"],
    )
    regions = {region_id: copy.deepcopy(region_defaults) for region_id in REGION_IDS}
    regions["workspace"]["background"] = tokens["background_app"]
    regions["statusbar"]["background"] = tokens["background_card"]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": theme_id,
        "name": "内置深色" if appearance == "dark" else "内置浅色",
        "appearance": appearance,
        "extends": theme_id,
        "builtin": True,
        "tokens": tokens,
        "native_titlebar": titlebar,
        "regions": regions,
    }


# These values are the semantic equivalents of frontend/src/style.css :root/body.light.
BUILTIN_THEMES = {
    "builtin.dark": _builtin_theme(
        "builtin.dark",
        "dark",
        {
            "background_app": "#1a1a18", "background_panel": "#20201e", "background_card": "#272724",
            "background_elevated": "#302f2b", "border": "#34332f",
            "text_primary": "#d9d9d4", "text_bright": "#edede9", "text_secondary": "#a8a7a0",
            "text_faint": "#85847d", "accent": "#6a92d8", "accent_secondary": "#6a92d8",
            "accent_soft": "#6a92d829", "success": "#4ea96a", "warning": "#c9a24a",
            "danger": "#d9635b", "orange": "#d08a4e", "info": "#4aabba", "purple": "#a58bd8",
            "code_background": "#141412", "float_background": "#20201ef7",
            "font_sans": "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
            "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
            "radius_small": 4, "radius_medium": 8, "radius_large": 14,
            "shadow_color": "#000000", "shadow_strength": 0.35,
        },
        {"background": "#20201e", "text": "#d9d9d4", "border": "#34332f", "mode": "dark"},
    ),
    "builtin.light": _builtin_theme(
        "builtin.light",
        "light",
        {
            "background_app": "#f4f4f1", "background_panel": "#ffffff", "background_card": "#efefec",
            "background_elevated": "#e4e4e0", "border": "#e3e3de",
            "text_primary": "#2c2c2a", "text_bright": "#171716", "text_secondary": "#5c5c56",
            "text_faint": "#7a7a73", "accent": "#3a6dd8", "accent_secondary": "#3a6dd8",
            "accent_soft": "#3a6dd81a", "success": "#2c8a4c", "warning": "#96700f",
            "danger": "#cf3d36", "orange": "#b85e1f", "info": "#1a7f92", "purple": "#7b5fc0",
            "code_background": "#f0f0ec", "float_background": "#fffffff7",
            "font_sans": "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
            "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
            "radius_small": 4, "radius_medium": 8, "radius_large": 14,
            "shadow_color": "#000000", "shadow_strength": 0.16,
        },
        {"background": "#ffffff", "text": "#2c2c2a", "border": "#e3e3de", "mode": "light"},
    ),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ThemeError("invalid_type", path, "必须是对象")
    return value


def _reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            field_path = f"{path}.{key}" if path else str(key)
            raise ThemeError("unknown_field", field_path, "不支持的字段")


def _validate_color(value: Any, path: str) -> None:
    if not isinstance(value, str) or not COLOR_RE.fullmatch(value):
        raise ThemeError("invalid_color", path, "颜色必须是 #RRGGBB 或 #RRGGBBAA")


def _validate_manifest(theme: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _require_mapping(theme, "")
    _reject_unknown(manifest, _ROOT_KEYS, "")
    for key in _ROOT_KEYS:
        if key not in manifest:
            raise ThemeError("missing_field", key, "缺少必填字段")

    if manifest["schema_version"] != SCHEMA_VERSION or isinstance(manifest["schema_version"], bool):
        raise ThemeError("unsupported_schema", "schema_version", "不支持的主题 Schema 版本")
    theme_id = manifest["id"]
    if not isinstance(theme_id, str) or not THEME_ID_RE.fullmatch(theme_id):
        raise ThemeError("invalid_id", "id", "主题 ID 只能包含小写字母、数字和连字符")
    if not isinstance(manifest["name"], str) or not manifest["name"].strip():
        raise ThemeError("invalid_name", "name", "主题名称不能为空")
    if not isinstance(manifest["appearance"], str) or manifest["appearance"] not in _APPEARANCES:
        raise ThemeError("invalid_appearance", "appearance", "外观只能是 light 或 dark")
    if not isinstance(manifest["extends"], str) or manifest["extends"] not in BUILTIN_IDS:
        raise ThemeError("invalid_base", "extends", "用户主题只能继承内置主题")

    tokens = _require_mapping(manifest["tokens"], "tokens")
    _reject_unknown(tokens, _TOKEN_KEYS, "tokens")
    for key, value in tokens.items():
        path = f"tokens.{key}"
        if key in _TOKEN_COLOR_KEYS:
            _validate_color(value, path)
        elif key in _TOKEN_FONT_KEYS:
            if not isinstance(value, str) or not value.strip():
                raise ThemeError("invalid_type", path, "字体必须是非空字符串")
        elif not _is_number(value):
            raise ThemeError("invalid_type", path, "必须是数值")
        elif key in _TOKEN_RADIUS_KEYS and not 0 <= value <= 64:
            raise ThemeError("invalid_range", path, "圆角必须在 0 到 64 之间")
        elif key == "shadow_strength" and not 0 <= value <= 1:
            raise ThemeError("invalid_range", path, "阴影强度必须在 0 到 1 之间")

    titlebar = _require_mapping(manifest["native_titlebar"], "native_titlebar")
    _reject_unknown(titlebar, _TITLEBAR_KEYS, "native_titlebar")
    for key, value in titlebar.items():
        path = f"native_titlebar.{key}"
        if key == "mode":
            if not isinstance(value, str) or value not in _APPEARANCES:
                raise ThemeError("invalid_value", path, "标题栏模式只能是 light 或 dark")
        else:
            _validate_color(value, path)

    regions = _require_mapping(manifest["regions"], "regions")
    _reject_unknown(regions, frozenset(REGION_IDS), "regions")
    for region_id, region in regions.items():
        region_path = f"regions.{region_id}"
        region_data = _require_mapping(region, region_path)
        _reject_unknown(region_data, _REGION_KEYS, region_path)
        for key, value in region_data.items():
            path = f"{region_path}.{key}"
            if key in _REGION_COLOR_KEYS:
                _validate_color(value, path)
            elif key in {"background_opacity", "image_opacity"}:
                if not _is_number(value) or not 0 <= value <= 1:
                    raise ThemeError("invalid_range", path, "透明度必须在 0 到 1 之间")
            elif key == "image":
                if value is not None and not isinstance(value, str):
                    raise ThemeError("invalid_type", path, "图片必须是相对路径或 null")
            elif key == "image_size" and (not isinstance(value, str) or value not in _IMAGE_SIZES):
                raise ThemeError("invalid_value", path, "不支持的图片缩放方式")
            elif key == "image_position" and (not isinstance(value, str) or value not in _IMAGE_POSITIONS):
                raise ThemeError("invalid_value", path, "不支持的图片位置")
    return copy.deepcopy(dict(manifest))


def _merge_theme(base: dict[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key in ("tokens", "native_titlebar"):
        merged[key].update(manifest[key])
    for region_id, region in manifest["regions"].items():
        merged["regions"][region_id].update(region)
    for key in ("schema_version", "id", "name", "appearance", "extends"):
        merged[key] = manifest[key]
    merged["builtin"] = False
    return merged


class ThemeRepository:
    def __init__(self, root: Path, config_file: Path):
        self.root = Path(root)
        self.config_file = Path(config_file)

    def list_themes(self) -> list[dict[str, Any]]:
        summaries = [self._summary(self.get_theme(theme_id)) for theme_id in BUILTIN_IDS]
        if not self.root.exists():
            return summaries
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if directory.is_dir():
                summaries.append(self._summary(self.get_theme(directory.name)))
        return summaries

    def get_theme(self, theme_id: str) -> dict[str, Any]:
        if theme_id in BUILTIN_IDS:
            return copy.deepcopy(BUILTIN_THEMES[theme_id])
        if not isinstance(theme_id, str) or not THEME_ID_RE.fullmatch(theme_id):
            raise ThemeError("invalid_id", "id", "主题 ID 无效")
        manifest_path = self.root / theme_id / "theme.json"
        if not manifest_path.is_file():
            raise ThemeError("theme_not_found", "id", "主题不存在")
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise ThemeError("invalid_manifest", "theme.json", "主题清单无法读取") from error
        manifest = _validate_manifest(manifest)
        if manifest["id"] != theme_id:
            raise ThemeError("invalid_id", "id", "主题 ID 必须与目录名一致")
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def save_theme(self, theme: Mapping[str, Any]) -> dict[str, Any]:
        manifest = _validate_manifest(theme)
        self.root.mkdir(parents=True, exist_ok=True)
        theme_dir = self.root / manifest["id"]
        if theme_dir.exists() and not theme_dir.is_dir():
            raise ThemeError("theme_exists", "id", "主题路径不是目录")
        theme_dir.mkdir(exist_ok=True)
        self._write_manifest(theme_dir / "theme.json", manifest)
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def copy_theme(self, source_id: str, new_id: str, new_name: str) -> dict[str, Any]:
        source_theme = self.get_theme(source_id)
        manifest = self._manifest_for_copy(source_theme, new_id, new_name)
        self._require_free_destination(new_id)
        self.root.mkdir(parents=True, exist_ok=True)
        staging_dir = self._staging_directory()
        try:
            if source_id not in BUILTIN_IDS:
                shutil.copytree(self.root / source_id, staging_dir, dirs_exist_ok=True)
            self._write_manifest(staging_dir / "theme.json", manifest)
            os.replace(staging_dir, self.root / new_id)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def rename_theme(self, theme_id: str, new_id: str, new_name: str) -> dict[str, Any]:
        if theme_id in BUILTIN_IDS:
            raise ThemeError("builtin_read_only", "id", "内置主题不能重命名")
        source_theme = self.get_theme(theme_id)
        manifest = self._manifest_for_copy(source_theme, new_id, new_name)
        self._require_free_destination(new_id)
        self.root.mkdir(parents=True, exist_ok=True)
        staging_dir = self._staging_directory()
        try:
            shutil.copytree(self.root / theme_id, staging_dir, dirs_exist_ok=True)
            self._write_manifest(staging_dir / "theme.json", manifest)
            os.replace(staging_dir, self.root / new_id)
            shutil.rmtree(self.root / theme_id)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def set_active(self, theme_id: str) -> dict[str, Any]:
        theme = self.get_theme(theme_id)
        with config._CONFIG_LOCK:
            config_data = self._read_config()
            config_data["gui_theme_id"] = theme_id
            config._atomic_write_json(str(self.config_file), config_data)
        return theme

    def get_active(self) -> dict[str, Any]:
        with config._CONFIG_LOCK:
            config_data = self._read_config()
            active_id = config_data.get("gui_theme_id")
            if active_id is not None:
                return self.get_theme(active_id)
            if "gui_theme_light" in config_data:
                active_id = "builtin.light" if config_data["gui_theme_light"] else "builtin.dark"
                config_data["gui_theme_id"] = active_id
                config._atomic_write_json(str(self.config_file), config_data)
                return self.get_theme(active_id)
        return self.get_theme("builtin.dark")

    def _summary(self, theme: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": theme["id"], "name": theme["name"], "appearance": theme["appearance"],
            "extends": theme["extends"], "builtin": theme["builtin"],
        }

    def _read_config(self) -> dict[str, Any]:
        try:
            with self.config_file.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ThemeError("invalid_config", "config", "配置必须是对象")
        return data

    @staticmethod
    def _manifest_for_copy(source: dict[str, Any], new_id: str, new_name: str) -> dict[str, Any]:
        manifest = {key: copy.deepcopy(source[key]) for key in _ROOT_KEYS}
        manifest["id"] = new_id
        manifest["name"] = new_name
        return _validate_manifest(manifest)

    def _require_free_destination(self, theme_id: str) -> None:
        if not isinstance(theme_id, str) or not THEME_ID_RE.fullmatch(theme_id):
            raise ThemeError("invalid_id", "id", "主题 ID 只能包含小写字母、数字和连字符")
        if (self.root / theme_id).exists():
            raise ThemeError("theme_exists", "id", "主题已存在")

    def _staging_directory(self) -> Path:
        return Path(tempfile.mkdtemp(prefix=".gui-theme-", dir=self.root))

    @staticmethod
    def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{id(manifest)}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def get_repository() -> ThemeRepository:
    return ThemeRepository(Path(config.YCODE_DIR) / "gui-themes", Path(config._CONFIG_FILE))
