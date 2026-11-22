from __future__ import annotations

import hashlib
from pathlib import Path

MAX_PREVIEW_BYTES = 8 * 1024 * 1024


def get_preview_dir() -> Path:
    """预览页目录：与 /share 导出共用目录约定（跟随 YCODE_DATA_DIR）。"""
    from core.share import default_export_dir

    return default_export_dir() / "preview"


def preview_file_name(source_path: str) -> str:
    """源路径 → 稳定的预览文件名（同一源文件重复生成时覆盖同一份）。"""
    key = hashlib.sha1(str(source_path or "").strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"preview_{key}.html"


def write_preview_page(html_text: str, source_path: str = "") -> Path:
    """把渲染好的预览 HTML 落盘并返回路径；内容非法/过大时抛 ValueError。"""
    if not isinstance(html_text, str) or not html_text.strip():
        raise ValueError("预览内容为空")
    data = html_text.encode("utf-8")
    if len(data) > MAX_PREVIEW_BYTES:
        raise ValueError(f"预览内容过大（{len(data)} 字节，上限 {MAX_PREVIEW_BYTES}）")

    directory = get_preview_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / preview_file_name(source_path)
    path.write_text(html_text, encoding="utf-8")
    return path
