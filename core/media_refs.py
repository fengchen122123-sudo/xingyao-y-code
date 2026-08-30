"""core/media_refs.py
=====================
媒体引用解析器：从助手消息正文提取图片/视频语法并组装 media_v1 清单。

设计见 docs/superpowers/specs/2026-08-30-media-display-design.md。
要点：
- 语法：图片 ``![caption](path)``；视频 ``[video: caption](path)`` 与
  ``[视频：caption](path)``（全角冒号兼容，video 大小写不敏感）。
- 围栏代码块与行内代码内的语法不提取。围栏/行内代码的正则必须与
  frontend/src/utils/markdown.ts 完全同构，保证「第 N 个媒体语法」在
  前后端对齐同一个 ordinal（前端渲染按 ordinal 取清单项）。
- 远程 URL（http/https/ftp/data 等）不是媒体项、不占 ordinal，渲染层同规则
  把它当普通链接——生成工具必须先把媒体落盘再引用（防 SSRF）。
- 单个引用解析失败（不存在/格式不支持/超大）生成 kind=error 的清单项；
  仓库不可写等基础设施错误按契约抛 MediaStoreError 显式失败，绝不降级。
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote

from core.media_store import MediaStoreError, archive_media_file

# 以下两条必须与 markdown.ts 的代码块保护正则保持同构
_FENCED_RE = re.compile(r"```(\w*)\n([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# 单遍扫描图片与视频语法：组 1/2 = 图片 caption/path，组 4/5 = 视频 caption/path
_MEDIA_SYNTAX_RE = re.compile(
    r"!\[([^\]]*)\]\(([^)\s]+)\)"
    r"|\[((?:video|视频)[ \t]*[:：][ \t]*)([^\]]*)\]\(([^)\s]+)\)",
    re.IGNORECASE,
)

# 带 scheme 的远程链接（file: 除外）；data: 无 // 也按远程处理
_REMOTE_LINK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")
_PATH_FORBIDDEN_CHARS_RE = re.compile(r"[\s<>\"'`]")

_PRIVATE_PLACEHOLDER = "\uE000"

# 错误清单项的中文展示文案（gui_bridge 组装前端 media 时使用）
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
    """移除围栏代码块与行内代码 span，避免其中的媒体语法被误提取。"""
    text = _FENCED_RE.sub(_PRIVATE_PLACEHOLDER, text)
    text = _INLINE_CODE_RE.sub(_PRIVATE_PLACEHOLDER, text)
    return text


def _is_local_media_path(path: str) -> bool:
    """与前端渲染层同规则的本地媒体路径判定；远程 URL 返回 False。"""
    s = (path or "").strip()
    if not s or _PATH_FORBIDDEN_CHARS_RE.search(s):
        return False
    if s.lower().startswith("file://"):
        return True
    if _REMOTE_LINK_RE.match(s) or s.lower().startswith("data:"):
        return False
    return True


def _file_uri_to_path(uri: str) -> str:
    """file:///C:/x -> C:/x（与前端 toAbsoluteLocalPath 同规则）。"""
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
    """把 media_v1 清单挂到落库副本的 _display（仅 assistant 文本消息）。

    只改传入的落库副本字典，不影响发给 LLM 的内存消息。
    """
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return
    content = msg.get("content")
    if not isinstance(content, str) or not content:
        return
    try:
        manifest = build_media_manifest(content, workdir)
    except MediaStoreError:
        # 与普通 MediaStoreError 语义一致：显式失败，由持久化路径向上传播
        raise
    if manifest is None:
        return
    display = msg.get("_display")
    if not isinstance(display, dict):
        display = {}
    display["media_v1"] = manifest
    msg["_display"] = display
