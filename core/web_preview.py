"""右栏内嵌浏览器用的本地预览页落盘（markdown → 带样式的 HTML 文件）。

为什么需要这一层：实测内嵌 WebView2 打开 `file://` 的 `.md` 只会显示源码
文本，而右栏浏览器要给出「阅读样式」的渲染结果。渲染这件事复用前端已有的
markdown 渲染器（与消息气泡同一套），本模块只负责把渲染好的完整 HTML 落盘成
文件，再交浏览器按 file:// 打开。

落盘策略：文件名取源文件绝对路径的 sha1 前 12 位——同一文件重复点击覆盖同一
份预览页，不在磁盘上堆垃圾；目录跟随 core.share 的导出目录约定
（`~/.ycode/exports/preview`，可用 YCODE_DATA_DIR 覆盖）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# 单页体积上限：预览页由前端渲染结果拼装，正常远小于此；超限说明调用方
# 误传（例如把整个文件树塞进来），直接拒绝而不是往磁盘写大文件。
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
