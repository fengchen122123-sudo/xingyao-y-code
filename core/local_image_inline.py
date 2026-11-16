"""把 markdown 里的容器本地图片路径内联成 data URI。

web 端浏览器访问不到 `/data/...` 这类容器内路径，原样渲染就是坏图。
这里在服务端出口统一改写：只处理白名单目录 + 图片扩展名 + 体积上限，
读不到或超限一律原样返回，绝不因为一张图让整条消息失败。
"""
from __future__ import annotations

import base64
import json
import os
import re

__all__ = ["inline_local_images_in_text", "inline_local_images_in_payload"]

_LOCAL_IMAGE = re.compile(
    r"!\[(?P<alt>[^\]\n]*)\]\((?P<path>(?:/data/|/tmp/|/home/|file:///)[^)\s]+)\)"
)
_HINT = re.compile(r"\]\((?:/data/|/tmp/|/home/|file:///)")
_MAX_BYTES = 512 * 1024
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def inline_local_images_in_text(text: str) -> str:
    """把一段 markdown 里的本地图片路径换成 data URI；其余内容原样返回。"""
    if not isinstance(text, str) or "![" not in text or not _HINT.search(text):
        return text

    def _replace(match: "re.Match[str]") -> str:
        raw = match.group("path")
        # file:///C:/x 三斜杠去掉后只剩 C:/x；file://host/path 这类非常规形态原样保留
        if raw.startswith("file:///"):
            path = raw[len("file:///"):]
        elif raw.startswith("file://"):
            path = raw[len("file://"):]
        else:
            path = raw
        mime = _MIME.get(os.path.splitext(path)[1].lower())
        if mime is None:
            return match.group(0)
        try:
            if not os.path.isfile(path) or os.path.getsize(path) > _MAX_BYTES:
                return match.group(0)
            with open(path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except OSError:
            return match.group(0)
        return f"![{match.group('alt')}](data:{mime};base64,{encoded})"

    return _LOCAL_IMAGE.sub(_replace, text)


def _walk(value):
    if isinstance(value, str):
        return inline_local_images_in_text(value)
    if isinstance(value, list):
        return [_walk(item) for item in value]
    if isinstance(value, dict):
        return {key: _walk(item) for key, item in value.items()}
    return value


def inline_local_images_in_payload(payload: str) -> str:
    """对整条 JSON payload 递归内联本地图片；没有图片直接原样返回。"""
    if not isinstance(payload, str) or "![" not in payload or not _HINT.search(payload):
        return payload
    try:
        parsed = json.loads(payload)
    except Exception:
        return payload
    return json.dumps(_walk(parsed), ensure_ascii=False)
