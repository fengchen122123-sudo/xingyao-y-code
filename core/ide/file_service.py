# -*- coding: utf-8 -*-
"""IDE 编辑器文件读写服务。

与只读预览（gui_bridge.read_file_preview）的关键区别：
- 预览允许截断、允许图片；编辑器不允许——内容不完整会写坏文件；
- 编辑器必须原样保留编码与 BOM（含 BOM 的原始字节口径，读写两侧一致），
  并按原文件的换行风格还原。
  已知例外：mixed 换行（同文件内混用 CRLF/LF/CR）在编辑器内部已被归一为
  LF，保存后统一成 LF，见 _normalize_eol。

编码口径与预览保持一致（UTF-8 -> GBK），避免同一个文件在两处显示不同。
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import threading

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
    """判断换行风格；两种以上混用时返回 mixed。

    mixed 无法逐行还原（编辑器内部已归一为 LF），写回时按 LF 统一落盘，
    见 _normalize_eol。
    """
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


def _normalize_eol(text: str, eol: str) -> str:
    """把编辑器内部的 LF 还原成原文件的换行风格。

    注意 mixed 的处理：编辑器内部（CodeMirror 6 的 Text）在构造时就把
    ``\\r\\n`` 与 ``\\r`` 归一为 ``\\n``，传下来的内容已是纯 LF，原始的行级
    换行分布已经丢失，架构上无法逐行还原。因此 mixed 文件保存后其换行符会
    被统一为 LF，而不是「保持不变」。
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    if eol == "crlf":
        return unified.replace("\n", "\r\n")
    if eol == "cr":
        return unified.replace("\n", "\r")
    return unified


def _atomic_write_bytes(path: str, data: bytes) -> None:
    """临时文件 + 原子替换；失败时清理临时文件。"""
    directory = os.path.dirname(path) or "."
    try:
        mode = os.stat(path).st_mode
    except OSError:
        mode = None
    fd, tmp = tempfile.mkstemp(prefix=".ycode-ide-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if mode is not None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def write_text_file(
    path: str,
    content: str,
    expected_hash: str,
    *,
    encoding: str,
    bom: bool,
    eol: str,
) -> dict:
    """保存编辑器内容。

    expected_hash 为打开文件时 read_text_file 返回的 hash：磁盘内容与它不
    一致说明文件在编辑期间被外部（星瑶或其他程序）改过，返回 conflict 且
    不落盘，由前端让用户选择。传空字符串表示调用方明确要求覆盖。

    冲突分支返回的 ``conflict["disk_hash"]`` 是磁盘当前内容的 SHA-256；空
    字符串是**哨兵值，表示目标文件已不存在**（被删除或改名）。调用方应据此
    提示「文件已删除」而不是「内容冲突」。绝不把这种情况当作无冲突覆盖。
    """
    real = os.path.realpath(os.path.abspath(path))
    if expected_hash:
        try:
            with open(real, "rb") as fh:
                disk_hash = _hash_bytes(fh.read())
        except FileNotFoundError:
            disk_hash = ""
        if disk_hash != expected_hash:
            return {
                "ok": False,
                "conflict": {"disk_hash": disk_hash},
                "msg": "文件已被外部修改，请选择以磁盘为准重载或用当前内容覆盖",
            }
    text = _normalize_eol(content, eol)
    try:
        data = text.encode(encoding)
    except UnicodeEncodeError as exc:
        raise FileServiceError(
            f"内容包含 {encoding} 无法表示的字符（{exc.start} 处），请先改用 UTF-8 保存"
        ) from exc
    if bom:
        data = _BOM_UTF8 + data
    _atomic_write_bytes(real, data)
    return {"ok": True, "hash": _hash_bytes(data)}


class OpenFileWatcher:
    """只轮询当前已打开文件的 (mtime, size)。

    刻意不做全仓扫描：IDE 打开的文件通常个位数，按 1 秒轮询的代价可以忽略，
    而目录监听（watchdog）需要额外依赖，与项目的依赖红线冲突。
    """

    def __init__(self, interval: float = 1.0):
        self._interval = interval
        self._files: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    @property
    def interval(self) -> float:
        return self._interval

    def _stamp(self, real: str) -> tuple[float, int]:
        try:
            st = os.stat(real)
            return (st.st_mtime, st.st_size)
        except OSError:
            return (0.0, -1)

    def set_open_files(self, paths) -> None:
        """重设监视集合，并以当前磁盘状态建立基线。"""
        snapshot: dict[str, tuple[float, int]] = {}
        for p in paths or []:
            real = os.path.realpath(os.path.abspath(str(p)))
            snapshot[real] = self._stamp(real)
        with self._lock:
            self._files = snapshot

    def poll(self) -> list[str]:
        """返回自上次 poll 后发生变化的文件（含被删除的文件，标记为 size=-1）。"""
        changed: list[str] = []
        with self._lock:
            for real in list(self._files.keys()):
                current = self._stamp(real)
                if current != self._files[real]:
                    self._files[real] = current
                    changed.append(real)
        return changed
