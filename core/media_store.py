"""core/media_store.py
=====================
永久媒体仓库：SHA-256 内容寻址的本地 Blob 存储。

设计见 docs/superpowers/specs/2026-08-30-media-display-design.md。
要点：
- 仓库目录 ``~/.ycode/media/blobs/``（YCODE_DATA_DIR 优先），
  文件名 ``<sha256hex><ext>``，相同内容跨会话自动去重。
- MIME 以文件头魔数嗅探为准，不信任扩展名；白名单外的类型一律拒绝
  （SVG/HTML 有脚本注入面，明确不支持）。
- Blob 先写临时文件再 ``os.replace`` 原子入位；数据库提交失败只会留下
  未引用 Blob，不会出现"消息引用了半个文件"。
- 仓库不可写、源文件读失败属于基础设施错误，按调用契约显式抛
  MediaStoreError，绝不静默降级假装成功。
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time

import config

MEDIA_BLOBS_DIR = os.path.join(config.YCODE_DIR, "media", "blobs")
MEDIA_TMP_DIR = os.path.join(MEDIA_BLOBS_DIR, "tmp")

# mime -> 扩展名白名单（双向映射的唯一事实源）。
# 不含 image/svg+xml：SVG 可携带脚本与外部资源，默认拒绝。
SUPPORTED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}

# 扩展名 -> 候选 mime（粗筛用；最终以嗅探为准）
_EXT_TO_MIME = {}
for _mime, _ext in SUPPORTED_MIME.items():
    _EXT_TO_MIME.setdefault(_ext.lstrip(".").lower(), set()).add(_mime)
# jpeg 的常见别名
_EXT_TO_MIME.setdefault("jpeg", set()).add("image/jpeg")

MAX_MEDIA_BYTES = 512 * 1024 * 1024  # 512MB，防止误归档超大文件

_CHUNK_SIZE = 1024 * 1024


class MediaStoreError(Exception):
    """媒体仓库基础设施错误（仓库不可写、源文件读取失败等）。"""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def _read_header(path: str, size: int = 64) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(size)


def _sniff_ftyp_mime(header: bytes) -> str | None:
    """ISO BMFF 容器（ftyp box）品牌判定：AVIF 或 MP4，认不出返回 None。"""
    if len(header) < 12 or header[4:8] != b"ftyp":
        return None
    brand = header[8:12].decode("ascii", errors="ignore").lower()
    if brand.startswith(("avif", "avis")):
        return "image/avif"
    if brand.startswith(("heic", "heix")):
        # HEIC 属 iPhone 常见格式但浏览器支持差，白名单未收录 → 明确拒绝
        return None
    return "video/mp4"


def sniff_mime(path: str) -> str | None:
    """按文件头魔数嗅探真实 MIME；不在白名单的格式返回 None。"""
    try:
        header = _read_header(path)
    except OSError:
        return None
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    ftyp = _sniff_ftyp_mime(header)
    if ftyp is not None:
        return ftyp
    return None


def _extension_precheck(path: str) -> bool:
    """扩展名粗筛：明显不属于白名单格式的文件直接跳过嗅探。"""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext in _EXT_TO_MIME


def _probe_image_dimensions(path: str) -> tuple[int | None, int | None]:
    """Pillow 探测图片宽高；Pillow 缺失或解码失败一律置 None（非关键字段）。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as img:
            width, height = img.size
            return int(width), int(height)
    except Exception:
        return None, None


def archive_media_file(path: str, workdir: str | None = None) -> dict:
    """把一个本地媒体文件归档进内容寻址仓库，返回清单 item 字典。

    抛出 MediaStoreError 的情形：
      - ``not_found``：解析后的路径不存在（调用方通常转为错误 item，
        本模块仅在传入路径明显不可读时使用）
      - ``repo_unwritable`` / ``read_failed``：基础设施错误，必须显式失败
    """
    resolved = str(path)
    if workdir and not os.path.isabs(resolved):
        resolved = os.path.join(workdir, resolved)
    resolved = os.path.realpath(resolved)
    if not os.path.isfile(resolved):
        raise MediaStoreError("not_found", f"媒体文件不存在: {resolved}")

    mime = sniff_mime(resolved) if _extension_precheck(resolved) else None
    if mime is None or mime not in SUPPORTED_MIME:
        raise MediaStoreError(
            "unsupported_type", f"不支持的媒体格式: {os.path.basename(resolved)}"
        )

    try:
        size = os.path.getsize(resolved)
    except OSError as exc:
        raise MediaStoreError("read_failed", f"读取媒体文件失败: {exc}") from exc
    if size > MAX_MEDIA_BYTES:
        raise MediaStoreError("too_large", f"媒体文件超过 {MAX_MEDIA_BYTES} 字节上限")

    ext = SUPPORTED_MIME[mime]
    digest = hashlib.sha256()
    tmp_name = None
    try:
        os.makedirs(MEDIA_BLOBS_DIR, exist_ok=True)
        os.makedirs(MEDIA_TMP_DIR, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="archive-", suffix=".part", dir=MEDIA_TMP_DIR)
        with os.fdopen(fd, "wb") as tmp_file, open(resolved, "rb") as src:
            while True:
                chunk = src.read(_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                tmp_file.write(chunk)
        blob_name = digest.hexdigest() + ext
        blob_path = os.path.join(MEDIA_BLOBS_DIR, blob_name)
        if os.path.exists(blob_path):
            # 去重命中：内容寻址保证同名即同内容，丢弃临时副本直接复用
            os.remove(tmp_name)
            tmp_name = None
        else:
            os.replace(tmp_name, blob_path)
            tmp_name = None
    except MediaStoreError:
        raise
    except OSError as exc:
        raise MediaStoreError("repo_unwritable", f"媒体仓库写入失败: {exc}") from exc
    finally:
        # 失败路径（源文件读失败 / 写盘失败 / os.replace 失败等）不留 .part
        # 残留；成功路径 tmp_name 已置 None，无需也不能再删（文件已入库）
        if tmp_name is not None:
            try:
                os.remove(tmp_name)
            except OSError:
                pass

    width = height = None
    if mime.startswith("image/"):
        width, height = _probe_image_dimensions(resolved)

    return {
        "kind": "image" if mime.startswith("image/") else "video",
        "digest": digest.hexdigest(),
        "mime": mime,
        "ext": ext.lstrip("."),
        "bytes": size,
        "width": width,
        "height": height,
        "duration_ms": None,
        "source_name": os.path.basename(resolved),
    }


# 仓库内合法 Blob 文件名：64 位十六进制摘要 + 白名单扩展名
_BLOB_NAME_RE_STRICT = r"[0-9a-f]{64}\.(?:%s)" % "|".join(
    sorted({ext.lstrip(".") for ext in SUPPORTED_MIME.values()})
)


def blob_name_is_safe(blob_name: str) -> bool:
    """文件名必须精确匹配 <sha256hex>.<白名单扩展名>，杜绝任何路径成分。"""
    return bool(re.fullmatch(_BLOB_NAME_RE_STRICT, blob_name or ""))


def blob_path_for(digest: str, mime: str) -> str:
    """由摘要与 mime 解析仓库内 Blob 绝对路径（供 HTTP 服务取文件）。"""
    if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise MediaStoreError("invalid_digest", "非法的媒体摘要格式")
    ext = SUPPORTED_MIME.get(mime)
    if ext is None:
        raise MediaStoreError("unsupported_type", f"未登记的媒体类型: {mime}")
    path = os.path.realpath(os.path.join(MEDIA_BLOBS_DIR, digest + ext))
    base = os.path.realpath(MEDIA_BLOBS_DIR)
    if not path.startswith(base + os.sep):
        # 纵深防御：摘要与扩展名都来自白名单，正常不可能越界
        raise MediaStoreError("invalid_digest", "媒体路径越界")
    return path


def prune_orphan_tmp(max_age_seconds: int = 24 * 3600) -> int:
    """清理归档过程中残留的临时分片（超过 max_age_seconds 的 .part 文件）。"""
    removed = 0
    try:
        for name in os.listdir(MEDIA_TMP_DIR):
            path = os.path.join(MEDIA_TMP_DIR, name)
            try:
                if time.time() - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed


# 孤儿 Blob 回收宽限期（秒）：刚归档的文件在宽限期内一律保留——消息落库、
# 输入框缩略图展示等引用建立存在时间差，只回收"确定无引用且已过期"的 Blob。
ORPHAN_BLOB_GRACE_SECONDS = 24 * 3600


def prune_orphan_blobs(
    referenced_digests, max_age_seconds: int = ORPHAN_BLOB_GRACE_SECONDS
) -> int:
    """回收未被消息引用的孤儿 Blob，返回删除数（内容寻址仓库的增量 GC）。

    删除会话/消息不会联动删除 Blob（仓库与数据库解耦），本函数是唯一回收
    通道，由闲时维护按小时节流调用。``referenced_digests`` 由消息层给出
    （全库媒体清单引用的摘要集合）：摘要命中或 mtime 在宽限期内的一律保留；
    只删超过宽限期的无引用文件，失败残留与废弃归档因此最终会被清理。
    """
    refs = set(referenced_digests or ())
    now = time.time()
    removed = 0
    try:
        names = os.listdir(MEDIA_BLOBS_DIR)
    except OSError:
        return 0
    for name in names:
        # 只处理规范 Blob 文件名；tmp 子目录等非规范名一律不动
        # （tmp 残留由 prune_orphan_tmp 负责）
        if not blob_name_is_safe(name):
            continue
        if name.split(".", 1)[0] in refs:
            continue
        path = os.path.join(MEDIA_BLOBS_DIR, name)
        try:
            if now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            continue
    return removed
