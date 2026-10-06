# -*- coding: utf-8 -*-
"""IDE 编辑器文件读写服务。

与只读预览（gui_bridge.read_file_preview）的关键区别：
- 预览允许截断、允许图片；编辑器不允许——内容不完整会写坏文件；
- 编辑器必须原样保留编码、BOM 与换行风格，写回时一个字节都不能变。

编码口径与预览保持一致（UTF-8 -> GBK），避免同一个文件在两处显示不同。
"""
from __future__ import annotations

import hashlib
import os
import tempfile

# 编辑器单文件上限：超过此体积 CodeMirror 的渲染与语法分析会明显卡顿
IDE_EDIT_MAX_BYTES = 2 * 1024 * 1024

# 编码探测顺序，与 read_file_preview 一致
_ENCODINGS = ("utf-8", "gbk")

_BOM_UTF8 = b"\xef\xbb\xbf"


class FileServiceError(Exception):
    """文件服务业务错误，消息可直接展示给用户。"""


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_eol(text: str) -> str:
    """判断换行风格；两种以上混用时返回 mixed，写回时不做转换。"""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    kinds = [bool(crlf), bool(lf), bool(cr)]
    if sum(kinds) > 1:
        return "mixed"
    if crlf:
        return "crlf"
    if cr:
        return "cr"
    return "lf"


def read_text_file(path: str) -> dict:
    """读取文本文件用于编辑。

    返回 content（不含 BOM 的正文）、encoding、bom、eol、hash（原始字节的
    SHA-256，作为保存时的冲突检测基准）与 size。
    """
    real = os.path.realpath(os.path.abspath(path))
    if not os.path.isfile(real):
        raise FileServiceError("文件不存在或不是普通文件")
    size = os.path.getsize(real)
    if size > IDE_EDIT_MAX_BYTES:
        raise FileServiceError(
            f"文件过大（{size / 1024 / 1024:.1f} MB，编辑上限 "
            f"{IDE_EDIT_MAX_BYTES // 1024 // 1024} MB），请使用只读预览"
        )
    with open(real, "rb") as f:
        raw = f.read()
    if b"\x00" in raw:
        raise FileServiceError("二进制文件不支持编辑，可在系统中打开")
    bom = raw.startswith(_BOM_UTF8)
    payload = raw[len(_BOM_UTF8):] if bom else raw
    content = None
    encoding = ""
    for enc in _ENCODINGS:
        try:
            content = payload.decode(enc)
            encoding = enc
            break
        except UnicodeDecodeError:
            continue
    if content is None:
        raise FileServiceError("无法识别文件编码（仅支持 UTF-8 / GBK 文本）")
    return {
        "path": real,
        "content": content,
        "encoding": encoding,
        "bom": bom,
        "eol": _detect_eol(content),
        "hash": _hash_bytes(raw),
        "size": size,
    }
