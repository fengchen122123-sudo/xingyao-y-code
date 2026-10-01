"""GUI 主题 Schema v1、持久化及活动主题选择。"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, UnidentifiedImageError

import config


SCHEMA_VERSION = 1
THEME_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BUILTIN_IDS = ("builtin.dark", "builtin.light", "builtin.qq2009", "builtin.field-notes")
REGION_IDS = (
    "app_header", "sidebar", "conversation_toolbar", "workspace",
    "composer", "right_panel", "statusbar",
)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?$")
OPAQUE_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

_ROOT_KEYS = frozenset({
    "schema_version", "id", "name", "appearance", "extends", "tokens",
    "native_titlebar", "app_image", "regions",
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
# 全局背景图：垫在所有分区之下的最底层（前端 body::before），无文字/边框语义
_APP_IMAGE_KEYS = frozenset({"image", "image_opacity", "image_size", "image_position"})
_REGION_KEYS = frozenset({
    "background", "text", "border", "accent", "background_opacity", "image",
    "image_opacity", "image_size", "image_position",
})
_REGION_COLOR_KEYS = frozenset({"background", "text", "border", "accent"})
_APPEARANCES = frozenset({"light", "dark"})
_IMAGE_SIZES = frozenset({"cover", "contain", "auto"})
_IMAGE_POSITIONS = frozenset({"center", "top", "bottom", "left", "right"})
_IMAGE_FORMATS = {
    "PNG": ("png", "image/png"),
    "JPEG": ("jpg", "image/jpeg"),
    "WEBP": ("webp", "image/webp"),
}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_IMAGE_DIMENSION = 8192
_MAX_ZIP_ENTRIES = 128
_MAX_ZIP_EXPANDED_BYTES = 30 * 1024 * 1024
_MAX_ZIP_COMPRESSION_RATIO = 100


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
    *,
    name: str | None = None,
    regions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    region_defaults = _region(
        tokens["background_panel"], tokens["text_primary"], tokens["border"], tokens["accent"],
    )
    built_regions = {region_id: copy.deepcopy(region_defaults) for region_id in REGION_IDS}
    built_regions["workspace"]["background"] = tokens["background_app"]
    built_regions["statusbar"]["background"] = tokens["background_card"]
    if regions is not None:
        # 按分区覆盖默认配色；未知分区直接报错，避免悄悄失效
        for region_id, overrides in regions.items():
            if region_id not in REGION_IDS:
                raise ValueError(f"未知分区：{region_id}")
            built_regions[region_id].update(overrides)
    return {
        "schema_version": SCHEMA_VERSION,
        "id": theme_id,
        "name": name if name is not None else ("内置深色" if appearance == "dark" else "内置浅色"),
        "appearance": appearance,
        "extends": theme_id,
        "builtin": True,
        "tokens": tokens,
        "native_titlebar": titlebar,
        "app_image": {
            "image": None,
            "image_opacity": 1.0,
            "image_size": "cover",
            "image_position": "center",
        },
        "regions": built_regions,
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
    # 内置 QQ2009：经典蓝色皮肤（蓝色渐变标题栏、浅蓝面板、橙色强调）
    "builtin.qq2009": _builtin_theme(
        "builtin.qq2009",
        "light",
        {
            "background_app": "#F7FBFF", "background_panel": "#EAF3FB", "background_card": "#DCEBFA",
            "background_elevated": "#F2F8FE", "border": "#A9C8E4",
            "text_primary": "#152E42", "text_bright": "#0B2237", "text_secondary": "#4A5A6A",
            "text_faint": "#7A8A9A", "accent": "#3A8FD0", "accent_secondary": "#2E7CC4",
            "accent_soft": "#3A8FD026", "success": "#2E8B3D", "warning": "#B87700",
            "danger": "#C0392B", "orange": "#F39C00", "info": "#0B61A4", "purple": "#7E5FC0",
            "code_background": "#F0F6FC", "float_background": "#FFFFFFF7",
            "font_sans": "'宋体', SimSun, Tahoma, 'Microsoft YaHei', sans-serif",
            "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
            "radius_small": 3, "radius_medium": 5, "radius_large": 8,
            "shadow_color": "#0A2A4A", "shadow_strength": 0.18,
        },
        {"background": "#3A8FD0", "text": "#FFFFFF", "border": "#1F6CB5", "mode": "light"},
        name="内置 QQ2009",
        regions={
            "app_header": _region("#6FB3E5", "#0E3352", "#3A8FD0", "#1F5C96"),
            "sidebar": _region("#E9F3FC", "#123A5C", "#A9C8E4", "#3A8FD0"),
            "conversation_toolbar": _region("#DCEBF9", "#123A5C", "#A9C8E4", "#3A8FD0"),
            "workspace": _region("#FFFFFF", "#152E42", "#C6DBEF", "#3A8FD0"),
            "composer": _region("#F2F8FE", "#123A5C", "#A9C8E4", "#3A8FD0"),
            "right_panel": _region("#E9F3FC", "#123A5C", "#A9C8E4", "#3A8FD0"),
            "statusbar": _region("#D5E5F4", "#2B5A7A", "#A9C8E4", "#3A8FD0"),
        },
    ),
    # 内置田野笔记：纸质笔记本皮肤（墨绿框架 + 米色纸面 + 朱橙强调，直角、细边框、硬投影）
    "builtin.field-notes": _builtin_theme(
        "builtin.field-notes",
        "light",
        {
            "background_app": "#EFEADB", "background_panel": "#E5DFCB", "background_card": "#FBF8EF",
            "background_elevated": "#FDFBF5", "border": "#D8D2BE",
            "text_primary": "#29271F", "text_bright": "#1B1913", "text_secondary": "#6F6A59",
            "text_faint": "#9A9480", "accent": "#E04E10", "accent_secondary": "#B83E0B",
            "accent_soft": "#E04E1026", "success": "#2F9E5F", "warning": "#B08A1E",
            "danger": "#C4442A", "orange": "#C97A10", "info": "#3A7D8C", "purple": "#7D64A8",
            "code_background": "#E7E1CE", "float_background": "#FBF8EFF7",
            "font_sans": "'Segoe UI', 'Microsoft YaHei', system-ui, sans-serif",
            "font_mono": "'JetBrains Mono', 'Cascadia Code', 'Consolas', monospace",
            "radius_small": 0, "radius_medium": 2, "radius_large": 5,
            "shadow_color": "#29271F", "shadow_strength": 0.20,
        },
        {"background": "#1D3A2F", "text": "#E8E2D4", "border": "#122A21", "mode": "dark"},
        name="内置 田野笔记",
        regions={
            "app_header": _region("#1D3A2F", "#E8E2D4", "#122A21", "#E04E10"),
            "sidebar": _region("#1D3A2F", "#DCE5D8", "#163026", "#E04E10"),
            "conversation_toolbar": _region("#E5DFCB", "#29271F", "#D8D2BE", "#E04E10"),
            "workspace": _region("#EFEADB", "#29271F", "#D8D2BE", "#E04E10"),
            "composer": _region("#E5DFCB", "#29271F", "#D8D2BE", "#E04E10"),
            "right_panel": _region("#E5DFCB", "#29271F", "#D8D2BE", "#E04E10"),
            "statusbar": _region("#1D3A2F", "#8FA493", "#163026", "#E04E10"),
        },
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


def _validate_opaque_color(value: Any, path: str) -> None:
    if not isinstance(value, str) or not OPAQUE_COLOR_RE.fullmatch(value):
        raise ThemeError("invalid_color", path, "原生标题栏颜色必须是 #RRGGBB")


def _default_app_image() -> dict[str, Any]:
    return {"image": None, "image_opacity": 1.0, "image_size": "cover", "image_position": "center"}


def _validate_manifest(theme: Mapping[str, Any]) -> dict[str, Any]:
    manifest = dict(_require_mapping(theme, ""))
    # 向后兼容：app_image 是 schema v1 后续新增的可继承字段，旧主题清单缺省时
    # 补默认值（确定性迁移），与未知字段"明确报错"不冲突——只补这一个已知新键。
    if "app_image" not in manifest:
        manifest["app_image"] = _default_app_image()
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
            # Windows COLORREF has no alpha channel. Web tokens/regions retain
            # #RRGGBBAA support, while native chrome is explicitly opaque.
            _validate_opaque_color(value, path)

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
                if value is not None:
                    _validate_asset_reference(value, path)
            elif key == "image_size" and (not isinstance(value, str) or value not in _IMAGE_SIZES):
                raise ThemeError("invalid_value", path, "不支持的图片缩放方式")
            elif key == "image_position" and (not isinstance(value, str) or value not in _IMAGE_POSITIONS):
                raise ThemeError("invalid_value", path, "不支持的图片位置")

    app_image = _require_mapping(manifest["app_image"], "app_image")
    _reject_unknown(app_image, _APP_IMAGE_KEYS, "app_image")
    for key, value in app_image.items():
        path = f"app_image.{key}"
        if key == "image":
            if value is not None:
                _validate_asset_reference(value, path)
        elif key == "image_opacity":
            if not _is_number(value) or not 0 <= value <= 1:
                raise ThemeError("invalid_range", path, "透明度必须在 0 到 1 之间")
        elif key == "image_size" and (not isinstance(value, str) or value not in _IMAGE_SIZES):
            raise ThemeError("invalid_value", path, "不支持的图片缩放方式")
        elif key == "image_position" and (not isinstance(value, str) or value not in _IMAGE_POSITIONS):
            raise ThemeError("invalid_value", path, "不支持的图片位置")
    return copy.deepcopy(dict(manifest))


def _validate_asset_reference(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ThemeError("invalid_type", path, "图片必须是相对路径或 null")
    if "\\" in value or ":" in value:
        raise ThemeError("asset_escape", path, "图片路径必须位于 assets 目录")
    asset_path = PurePosixPath(value)
    if (
        asset_path.is_absolute()
        or asset_path.as_posix() != value
        or len(asset_path.parts) < 2
        or asset_path.parts[0] != "assets"
        or any(part in {"", ".", ".."} for part in asset_path.parts)
    ):
        raise ThemeError("asset_escape", path, "图片路径必须位于 assets 目录")
    return asset_path.as_posix()


def _read_image_bounded(path: Path, label: str, unreadable: ThemeError) -> bytes:
    """先检查文件元数据、再有上限地读取，防止超大文件先整块进入内存。

    stat 与读取之间文件可能增长，因此读取上限仍是 _MAX_IMAGE_BYTES + 1，
    读到的字节数超限同样拒绝。
    """
    try:
        size = path.stat().st_size
    except OSError as error:
        raise unreadable from error
    if size > _MAX_IMAGE_BYTES:
        raise ThemeError("image_too_large", label, "图片文件不能超过 10 MiB")
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_IMAGE_BYTES + 1)
    except OSError as error:
        raise unreadable from error
    if len(data) > _MAX_IMAGE_BYTES:
        raise ThemeError("image_too_large", label, "图片文件不能超过 10 MiB")
    return data


def _inspect_image(data: bytes, path: str) -> tuple[str, str]:
    if not data:
        raise ThemeError("invalid_image", path, "图片内容为空或无法解析")
    if len(data) > _MAX_IMAGE_BYTES:
        raise ThemeError("image_too_large", path, "图片文件不能超过 10 MiB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image_format = image.format
            width, height = image.size
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError, ValueError) as error:
        raise ThemeError("invalid_image", path, "图片内容无法解析") from error
    if image_format not in _IMAGE_FORMATS:
        raise ThemeError("unsupported_image", path, "只支持 PNG、JPEG 和 WEBP 图片")
    if width > _MAX_IMAGE_DIMENSION or height > _MAX_IMAGE_DIMENSION:
        raise ThemeError("image_too_large", path, "图片边长不能超过 8192 像素")
    return _IMAGE_FORMATS[image_format]


def _merge_theme(base: dict[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key in ("tokens", "native_titlebar", "app_image"):
        merged[key].update(manifest[key])
    for region_id, region in manifest["regions"].items():
        merged["regions"][region_id].update(region)
    for key in ("schema_version", "id", "name", "appearance", "extends"):
        merged[key] = manifest[key]
    merged["builtin"] = False
    return merged


def _manifest_asset_references(manifest: Mapping[str, Any]) -> set[str]:
    """主题清单引用的全部资源相对路径（分区图 + 全局背景图）。"""
    references = {
        region["image"] for region in manifest["regions"].values() if region["image"]
    }
    app_image = manifest["app_image"]["image"]
    if app_image:
        references.add(app_image)
    return references


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
        manifest = self._read_manifest(theme_id)
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def save_theme(self, theme: Mapping[str, Any]) -> dict[str, Any]:
        manifest = _validate_manifest(theme)
        theme_dir = self._validated_theme_destination(manifest["id"], create_root=True)
        if theme_dir.exists() and not theme_dir.is_dir():
            raise ThemeError("theme_exists", "id", "主题路径不是目录")
        theme_dir.mkdir(exist_ok=True)
        self._assert_no_symlink_components(theme_dir, "id")
        self._write_manifest(theme_dir / "theme.json", manifest)
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def copy_theme(self, source_id: str, new_id: str, new_name: str) -> dict[str, Any]:
        source_theme = self.get_theme(source_id)
        manifest = self._manifest_for_copy(source_theme, new_id, new_name)
        self._require_free_destination(new_id)
        staging_dir = self._staging_directory()
        try:
            if source_id not in BUILTIN_IDS:
                shutil.copytree(self.root / source_id, staging_dir, dirs_exist_ok=True)
            self._write_manifest(staging_dir / "theme.json", manifest)
            os.replace(staging_dir, self._require_free_destination(new_id))
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def rename_theme(self, theme_id: str, new_id: str, new_name: str) -> dict[str, Any]:
        if theme_id in BUILTIN_IDS:
            raise ThemeError("builtin_read_only", "id", "内置主题不能重命名")
        source_theme = self.get_theme(theme_id)
        manifest = self._manifest_for_copy(source_theme, new_id, new_name)
        destination = self._require_free_destination(new_id)
        staging_dir = self._staging_directory()
        retired_directory = self.root.parent / f".gui-theme-retired-{theme_id}-{uuid.uuid4().hex}"
        with config._CONFIG_LOCK:
            config_data = self._read_config()
            was_active = config_data.get("gui_theme_id") == theme_id
            try:
                shutil.copytree(self.root / theme_id, staging_dir, dirs_exist_ok=True)
                self._write_manifest(staging_dir / "theme.json", manifest)
                os.replace(staging_dir, destination)
            except Exception:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise
            if was_active:
                # At this point both IDs are complete. An ambiguous config write
                # may leave either ID active, but both remain valid; never delete
                # either copy in this failure path.
                config_data["gui_theme_id"] = new_id
                config._atomic_write_json(str(self.config_file), config_data)
            try:
                # Remove the old user-visible ID atomically. Recursive cleanup is
                # performed only on a tombstone outside the scanned themes root,
                # so partial deletion can never corrupt the active theme.
                os.replace(self.root / theme_id, retired_directory)
            except OSError as error:
                raise ThemeError(
                    "theme_cleanup_failed", "id",
                    "主题已复制，旧主题目录暂未移入清理区",
                ) from error
        try:
            shutil.rmtree(retired_directory)
        except OSError as error:
            raise ThemeError(
                "theme_cleanup_failed", "id",
                "主题已重命名，旧主题清理区删除失败",
            ) from error
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def import_image(self, theme_id: str, region: str, source: Path) -> str:
        manifest = self._editable_manifest(theme_id)
        # region 取值：七个分区之一，或 "app"（全局背景图，写入 app_image.image）
        is_app_image = region == "app"
        if not is_app_image and region not in REGION_IDS:
            raise ThemeError("invalid_region", "region", "主题区域不存在")
        source = Path(source)
        self._assert_no_symlink_components(source, "source")
        data = _read_image_bounded(
            source, "source", ThemeError("invalid_image", "source", "图片文件无法读取"),
        )
        extension, _ = _inspect_image(data, "source")
        relative_path = f"assets/{hashlib.sha256(data).hexdigest()[:16]}.{extension}"
        target = self._asset_file(theme_id, relative_path)
        theme_dir = self._theme_directory(theme_id)
        previous = (
            manifest["app_image"]["image"] if is_app_image
            else manifest["regions"][region]["image"]
        )
        # 统计旧图是否仍被别处引用（其它分区或全局图），决定是否计入删除后的体积
        referenced_elsewhere = _manifest_asset_references(manifest) - {previous}
        old_size = 0
        if previous and previous != relative_path and previous not in referenced_elsewhere:
            old_asset = self._asset_file(theme_id, previous)
            try:
                old_size = old_asset.stat().st_size
            except FileNotFoundError:
                pass
        try:
            new_size = 0 if target.exists() else len(data)
        except OSError as error:
            raise ThemeError("asset_escape", relative_path, "图片资源路径无法访问") from error
        if self._theme_directory_size(theme_dir) - old_size + new_size > _MAX_ZIP_EXPANDED_BYTES:
            raise ThemeError("theme_too_large", "assets", "主题目录不能超过 30 MiB")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_bytes(target, data)
        if is_app_image:
            manifest["app_image"]["image"] = relative_path
        else:
            manifest["regions"][region]["image"] = relative_path
        self._write_manifest(theme_dir / "theme.json", manifest)
        self._remove_asset_if_unreferenced(theme_id, manifest, previous)
        return relative_path

    def remove_image(self, theme_id: str, region: str) -> dict[str, Any]:
        manifest = self._editable_manifest(theme_id)
        is_app_image = region == "app"
        if not is_app_image and region not in REGION_IDS:
            raise ThemeError("invalid_region", "region", "主题区域不存在")
        if is_app_image:
            previous = manifest["app_image"]["image"]
            manifest["app_image"]["image"] = None
        else:
            previous = manifest["regions"][region]["image"]
            manifest["regions"][region]["image"] = None
        self._write_manifest(self._theme_directory(theme_id) / "theme.json", manifest)
        self._remove_asset_if_unreferenced(theme_id, manifest, previous)
        return _merge_theme(BUILTIN_THEMES[manifest["extends"]], manifest)

    def read_asset(self, theme_id: str, asset_path: str) -> tuple[str, bytes]:
        manifest = self._read_manifest(theme_id)
        asset_path = _validate_asset_reference(asset_path, "asset_path")
        referenced_assets = _manifest_asset_references(manifest)
        if asset_path not in referenced_assets:
            raise ThemeError("asset_not_found", "asset_path", "图片资源未被主题引用")
        asset = self._asset_file(theme_id, asset_path)
        data = _read_image_bounded(
            asset, asset_path, ThemeError("asset_not_found", asset_path, "图片资源不存在"),
        )
        _, mime_type = _inspect_image(data, asset_path)
        return mime_type, data

    def import_zip(self, source: Path) -> dict[str, Any]:
        source = Path(source)
        self._assert_no_symlink_components(source, "source")
        try:
            archive = ZipFile(source)
        except (BadZipFile, OSError) as error:
            raise ThemeError("invalid_archive", "source", "主题归档无法读取") from error
        with archive:
            infos = archive.infolist()
            self._validate_zip_infos(infos)
            theme_info = next((info for info in infos if info.filename == "theme.json"), None)
            if theme_info is None:
                raise ThemeError("invalid_manifest", "theme.json", "归档缺少主题清单")
            try:
                manifest = _validate_manifest(json.loads(archive.read(theme_info)))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ThemeError("invalid_manifest", "theme.json", "主题清单无法读取") from error
            self._validate_zip_theme_entries(infos, manifest)
            theme_id = manifest["id"]
            self._require_free_destination(theme_id)
            with tempfile.TemporaryDirectory(prefix=".gui-theme-import-", dir=self.root.parent) as temporary:
                staging_dir = Path(temporary) / theme_id
                staging_dir.mkdir()
                self._write_manifest(staging_dir / "theme.json", manifest)
                extracted = 0
                for info in infos:
                    if info.is_dir() or info.filename == "theme.json":
                        continue
                    target = staging_dir.joinpath(*PurePosixPath(info.filename).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    extracted = self._extract_zip_member(archive, info, target, extracted)
                self._validate_theme_assets(staging_dir, manifest)
                os.replace(staging_dir, self._require_free_destination(theme_id))
        return self.get_theme(theme_id)

    def export_zip(self, theme_id: str, destination: Path) -> Path:
        manifest = self._read_manifest(theme_id)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        referenced_assets = sorted(_manifest_asset_references(manifest))
        try:
            with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
                archive.writestr("theme.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for asset_path in referenced_assets:
                    _, data = self.read_asset(theme_id, asset_path)
                    archive.writestr(asset_path, data)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return destination

    def delete_theme(self, theme_id: str) -> None:
        if theme_id in BUILTIN_IDS:
            raise ThemeError("builtin_read_only", "id", "内置主题不能删除")
        self._read_manifest(theme_id)
        with config._CONFIG_LOCK:
            if self._read_config().get("gui_theme_id") == theme_id:
                raise ThemeError("active_theme_delete", "id", "不能删除当前主题")
        theme_dir = self._theme_directory(theme_id)
        shutil.rmtree(theme_dir)

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

    def _read_manifest(self, theme_id: str) -> dict[str, Any]:
        theme_dir = self._theme_directory(theme_id)
        manifest_path = theme_dir / "theme.json"
        if manifest_path.is_symlink():
            raise ThemeError("asset_escape", "theme.json", "不允许符号链接主题清单")
        if not manifest_path.is_file():
            raise ThemeError("theme_not_found", "id", "主题不存在")
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ThemeError("invalid_manifest", "theme.json", "主题清单无法读取") from error
        manifest = _validate_manifest(manifest)
        if manifest["id"] != theme_id:
            raise ThemeError("invalid_id", "id", "主题 ID 必须与目录名一致")
        return manifest

    def _editable_manifest(self, theme_id: str) -> dict[str, Any]:
        if theme_id in BUILTIN_IDS:
            raise ThemeError("builtin_read_only", "id", "内置主题不能修改")
        theme = self.get_theme(theme_id)
        return self._manifest_for_copy(theme, theme_id, theme["name"])

    def _theme_directory(self, theme_id: str) -> Path:
        return self._validated_theme_destination(theme_id)

    def _validated_theme_destination(
        self, theme_id: str, *, create_root: bool = False, require_free: bool = False,
    ) -> Path:
        if not isinstance(theme_id, str) or not THEME_ID_RE.fullmatch(theme_id):
            raise ThemeError("invalid_id", "id", "主题 ID 无效")
        self._assert_no_symlink_components(self.root, "root")
        if create_root:
            if self.root.exists() and not self.root.is_dir():
                raise ThemeError("theme_exists", "root", "主题根路径不是目录")
            self.root.mkdir(parents=True, exist_ok=True)
            self._assert_no_symlink_components(self.root, "root")
        theme_dir = self.root / theme_id
        self._assert_no_symlink_components(theme_dir, "id")
        if require_free and theme_dir.exists():
            raise ThemeError("theme_exists", "id", "主题已存在")
        return theme_dir

    def _asset_file(self, theme_id: str, asset_path: str) -> Path:
        asset_path = _validate_asset_reference(asset_path, "asset_path")
        theme_dir = self._theme_directory(theme_id)
        candidate = theme_dir.joinpath(*PurePosixPath(asset_path).parts)
        self._assert_no_symlink_components(candidate, asset_path)
        try:
            candidate.resolve().relative_to(theme_dir.resolve())
        except ValueError as error:
            raise ThemeError("asset_escape", "asset_path", "图片路径越过主题目录") from error
        return candidate

    def _validate_theme_assets(self, theme_dir: Path, manifest: Mapping[str, Any]) -> None:
        for asset_path in _manifest_asset_references(manifest):
            asset_path = _validate_asset_reference(asset_path, "regions.image")
            asset = theme_dir.joinpath(*PurePosixPath(asset_path).parts)
            self._assert_no_symlink_components(asset, asset_path)
            try:
                data = asset.read_bytes()
            except OSError as error:
                raise ThemeError("asset_not_found", asset_path, "图片资源不存在") from error
            _inspect_image(data, asset_path)

    def _remove_asset_if_unreferenced(
        self, theme_id: str, manifest: Mapping[str, Any], asset_path: str | None,
    ) -> None:
        if not asset_path or asset_path in _manifest_asset_references(manifest):
            return
        asset = self._asset_file(theme_id, asset_path)
        try:
            asset.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _assert_no_symlink_components(path: Path, error_path: str) -> None:
        current = Path(path)
        while True:
            if ThemeRepository._is_disallowed_path_link(current):
                raise ThemeError("asset_escape", error_path, "路径不能包含符号链接")
            if current.parent == current:
                return
            current = current.parent

    @staticmethod
    def _is_disallowed_path_link(path: Path) -> bool:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            return False
        if stat.S_ISLNK(path_stat.st_mode):
            return True
        if os.name != "nt":
            return False
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
        return bool(getattr(path_stat, "st_file_attributes", 0) & reparse_point)

    def _theme_directory_size(self, theme_dir: Path) -> int:
        self._assert_no_symlink_components(theme_dir, "assets")
        total_size = 0
        for directory, subdirectories, files in os.walk(theme_dir, followlinks=False):
            directory_path = Path(directory)
            self._assert_no_symlink_components(directory_path, "assets")
            for name in subdirectories:
                self._assert_no_symlink_components(directory_path / name, "assets")
            for name in files:
                file_path = directory_path / name
                self._assert_no_symlink_components(file_path, "assets")
                total_size += file_path.stat().st_size
        return total_size

    @staticmethod
    def _validate_zip_infos(infos: list[Any]) -> None:
        if len(infos) > _MAX_ZIP_ENTRIES:
            raise ThemeError("theme_too_large", "archive", "归档条目不能超过 128 个")
        names: set[str] = set()
        expanded_size = 0
        for info in infos:
            name = info.filename
            if (
                not name
                or "\\" in name
                or ":" in name
                or name in names
                or stat.S_ISLNK(info.external_attr >> 16)
            ):
                raise ThemeError("asset_escape", name or "archive", "归档路径不安全")
            names.add(name)
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or path.as_posix() != name
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ThemeError("asset_escape", name, "归档路径不安全")
            if info.flag_bits & 0x1:
                raise ThemeError("invalid_archive", name, "不支持加密主题归档")
            if name != "theme.json" and not name.startswith("assets/"):
                raise ThemeError("invalid_archive", name, "归档包含不支持的文件")
            if info.is_dir():
                raise ThemeError("invalid_archive", name, "归档不能包含目录条目")
            expanded_size += info.file_size
            if expanded_size > _MAX_ZIP_EXPANDED_BYTES:
                raise ThemeError("theme_too_large", name, "归档解压后不能超过 30 MiB")
            if info.file_size and (
                not info.compress_size
                or info.file_size / info.compress_size > _MAX_ZIP_COMPRESSION_RATIO
            ):
                raise ThemeError("theme_too_large", name, "归档压缩比过高")
            if name.startswith("assets/") and info.file_size > _MAX_IMAGE_BYTES:
                raise ThemeError("image_too_large", name, "图片文件不能超过 10 MiB")

    @staticmethod
    def _validate_zip_theme_entries(infos: list[Any], manifest: Mapping[str, Any]) -> None:
        referenced_assets = _manifest_asset_references(manifest)
        expected_entries = {"theme.json", *referenced_assets}
        actual_entries = {info.filename for info in infos}
        if actual_entries != expected_entries:
            raise ThemeError("invalid_archive", "archive", "归档必须只包含清单引用的图片资源")

    @staticmethod
    def _extract_zip_member(archive: ZipFile, info: Any, target: Path, extracted: int) -> int:
        try:
            with archive.open(info) as source, target.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    extracted += len(chunk)
                    if extracted > _MAX_ZIP_EXPANDED_BYTES:
                        raise ThemeError("theme_too_large", info.filename, "归档解压后不能超过 30 MiB")
                    destination.write(chunk)
        except (OSError, BadZipFile) as error:
            raise ThemeError("invalid_archive", info.filename, "归档文件无法解压") from error
        return extracted

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

    def _require_free_destination(self, theme_id: str) -> Path:
        return self._validated_theme_destination(theme_id, create_root=True, require_free=True)

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

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
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
