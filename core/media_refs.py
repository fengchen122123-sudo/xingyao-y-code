from __future__ import annotations

import os
import re
from urllib.parse import unquote

from core.media_store import MediaStoreError, archive_media_file

# 以下两条必须与 markdown.ts 的代码块保护正则保持同构
_FENCED_RE = re.compile(r"```(\w*)\n([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

_MEDIA_SYNTAX_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)\)"
    r"|\[((?:video|视频)[ \t]*[:：][ \t]*)([^\]]*)\]\(([^)\s]+)\)",
    re.IGNORECASE,
)

_REMOTE_LINK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_PATH_FORBIDDEN_CHARS_RE = re.compile(r"[\s<>\"'`]")

_PRIVATE_PLACEHOLDER = "\uE000"

MEDIA_DISPLAY_ERROR_LABELS = {
    "not_found": "源文件不存在",
    "unsupported_type": "格式不支持",
    "too_large": "文件过大",
    "repo_unwritable": "媒体仓库不可写",
    "read_failed": "媒体读取失败",
    "service_unavailable": "媒体服务不可用",
    "invalid_digest": "媒体标识无效",
}


def _strip_protected_spans(text: str) -> str:
    text = _FENCED_RE.sub(_PRIVATE_PLACEHOLDER, text)
    text = _INLINE_CODE_RE.sub(_PRIVATE_PLACEHOLDER, text)
    return text


def _is_local_media_path(path: str) -> bool:
    s = (path or "").strip()
    if not s or _PATH_FORBIDDEN_CHARS_RE.search(s):
        return False
    if s.lower().startswith("file://"):
        return True
    if _REMOTE_LINK_RE.match(s) or s.lower().startswith("data:"):
        return False
    return True


def _file_uri_to_path(uri: str) -> str:
    p = uri[len("file://"):]
    try:
        p = unquote(p)
    except Exception:
        pass
    if re.match(r"^/[A-Za-z]:[\\/]", p):
        p = p[1:]
    return p


def extract_media_refs(text, workdir=None):
    """按出现顺序提取本地媒体引用，ordinal 即出现序号（与前端渲染对齐）。"""
    refs = []
    if not isinstance(text, str) or not text:
        return refs
    stripped = _strip_protected_spans(text)
    for match in _MEDIA_SYNTAX_RE.finditer(stripped):
        if match.group(1) is not None:
            caption, raw_path = match.group(1), match.group(2)
        else:
            caption, raw_path = match.group(4), match.group(5)
        if not _is_local_media_path(raw_path):
            continue
        path = raw_path.strip().strip("\"'")
        if path.lower().startswith("file://"):
            path = _file_uri_to_path(path)
        refs.append({
            "ordinal": len(refs),
            "caption": (caption or "").strip(),
            "path": path,
        })
    return refs


# 单个引用可降级为错误清单项的错误码；其余（仓库不可写等基础设施错误）必须显式传播
_FILE_LEVEL_ERROR_CODES = {"not_found", "unsupported_type", "too_large"}


def _error_item(ref: dict, error_code: str) -> dict:
    return {
        "ordinal": ref["ordinal"],
        "kind": "error",
        "error_code": error_code,
        "caption": ref["caption"],
        "source_name": os.path.basename(ref["path"]),
    }


def build_media_manifest(text, workdir=None):
    """提取正文媒体引用并逐项归档，返回 media_v1 清单；无媒体语法返回 None。

    路径不存在/格式不支持/超大 → kind=error 清单项（前端明确失败态）；
    仓库不可写等基础设施错误按契约抛 MediaStoreError 显式失败，绝不降级。
    """
    refs = extract_media_refs(text, workdir)
    if not refs:
        return None
    items = []
    for ref in refs:
        try:
            archived = archive_media_file(ref["path"], workdir)
        except MediaStoreError as exc:
            if exc.error_code not in _FILE_LEVEL_ERROR_CODES:
                raise
            items.append(_error_item(ref, exc.error_code))
            continue
        archived["ordinal"] = ref["ordinal"]
        archived["caption"] = ref["caption"]
        items.append(archived)
    return {"version": 1, "items": items}


def attach_media_manifest(msg, workdir=None):
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content")
    if not isinstance(content, str) or not content:
        return
    try:
        manifest = build_media_manifest(content, workdir)
    except MediaStoreError:
        raise
    if manifest is None:
        return
    display = msg.get("_display")
    if not isinstance(display, dict):
        display = {}
    display["media_v1"] = manifest
    msg["_display"] = display
