from __future__ import annotations

import base64
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BUILTIN_SKIN_ID = "xingyao"

USER_SKIN_DIR: Path = Path.home() / ".ycode" / "pets"

# 内置星瑶的展示信息写死在代码里：它是产品的一部分，不该由配置文件决定
# （否则用户改坏 config.json 会把"星瑶"这个名字也一起弄丢）。
BUILTIN_SKIN_NAME = "星瑶"
BUILTIN_SKIN_DESCRIPTION = "星瑶的默认形象：白裙长发、会呼吸也会走动的少女，随对话做出拖拽与互动的动作。"
BUILTIN_SKIN_AUTHOR = "星瑶"

_STATE_ROOT = "pet"
_IDLE_STATE = "idle"
_FRAME_PATTERN = "frame_*.png"

_THUMB_SIZE = 96
_DATA_URI_PREFIX = "data:image/png;base64,"
# 缓存上限。正常场景皮肤数远小于它；超限时按插入顺序淘汰最老的一项，
# 不引入 LRU 之类的结构，因为"最老"在这个场景已经足够准（新扫到的一定在末尾）。
_THUMB_CACHE_MAX = 128
_THUMB_CACHE: Dict[Tuple[str, float], str] = {}

__all__ = [
    "BUILTIN_SKIN_ID",
    "USER_SKIN_DIR",
    "pet_dir_path",
    "user_skin_dir",
    "list_skins",
    "get_skin",
    "resolve_active",
    "active_skin_id",
    "set_active_skin",
    "import_skin_from_images",
    "import_skin_from_zip",
    "update_skin_meta",
    "delete_skin",
]


def _is_file(path: Path) -> bool:
    """path 是否是文件；权限/占用等 OSError 一律当"不是"。"""
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    """path 是否是目录；权限/占用等 OSError 一律当"不是"。"""
    try:
        return path.is_dir()
    except OSError:
        return False


def _idle_frames(assets_dir: Path) -> List[Path]:
    try:
        idle_dir = assets_dir / _STATE_ROOT / _IDLE_STATE
        if not idle_dir.is_dir():
            return []
        frames = [p for p in idle_dir.glob(_FRAME_PATTERN) if p.is_file()]
    except OSError as exc:
        logger.debug("读取桌宠帧目录失败 %s: %s", assets_dir, exc)
        return []
    return sorted(frames, key=lambda p: p.name)


def _config_value(key: str, default: Any = None) -> Any:
    try:
        import config
    except Exception:
        return default
    try:
        val = getattr(config, key, None)
    except Exception:
        val = None
    if val not in (None, ""):
        return val
    try:
        return config._load().get(key, default)
    except Exception:
        return default


def pet_dir_path() -> Path:
    env_dir = (os.environ.get("YCODE_PET_DIR") or "").strip()
    if env_dir:
        try:
            candidate = Path(env_dir)
            if candidate.is_dir():
                return candidate
        except OSError:
            logger.debug("YCODE_PET_DIR 不可用: %r", env_dir)

    resolver = None
    try:
        from .pet_manager import pet_dir_path as resolver  # noqa: PLC0415 - 延迟导入避免循环
    except Exception:
        try:
            from core.pet_manager import pet_dir_path as resolver  # noqa: PLC0415
        except Exception:
            resolver = None
    if callable(resolver):
        try:
            return Path(resolver())
        except Exception as exc:
            logger.debug("pet_manager.pet_dir_path() 调用失败，回落内置路径: %s", exc)

    return Path(__file__).resolve().parent.parent / "pets" / "xingyao_pet"


def user_skin_dir() -> Path:
    try:
        USER_SKIN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("创建用户宠物目录失败（按不存在处理）%s: %s", USER_SKIN_DIR, exc)
    except Exception as exc:  # noqa: BLE001 - 目录处理不该影响换肤面板
        logger.debug("创建用户宠物目录异常 %s: %s", USER_SKIN_DIR, exc)
    return USER_SKIN_DIR


def _png_data_uri(raw: bytes) -> str:
    try:
        return _DATA_URI_PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.debug("缩略图 base64 编码失败: %s", exc)
        return ""


def _encode_thumbnail(frame: Path) -> str:
    try:
        raw = frame.read_bytes()
    except OSError as exc:
        logger.debug("读取宠物帧失败 %s: %s", frame, exc)
        return ""
    if not raw:
        logger.debug("宠物帧为空文件，跳过: %s", frame)
        return ""
    try:
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 缺 Pillow 只影响缩略图质量
        logger.debug("Pillow 不可用，缩略图退回原图: %s", exc)
        return _png_data_uri(raw)
    try:
        with Image.open(io.BytesIO(raw)) as img:
            image = img.convert("RGBA")
            resample = getattr(Image, "Resampling", Image).LANCZOS
            # thumbnail 只缩不放：小图保持原尺寸，避免把 32px 的图糊成马赛克。
            image.thumbnail((_THUMB_SIZE, _THUMB_SIZE), resample)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        return _png_data_uri(buffer.getvalue())
    except Exception as exc:  # noqa: BLE001
        logger.debug("生成宠物缩略图失败，退回原图 %s: %s", frame, exc)
        return _png_data_uri(raw)


def _thumbnail(assets_dir: Path) -> str:
    frames = _idle_frames(assets_dir)
    if not frames:
        return ""
    frame = frames[0]
    try:
        cache_key = (str(frame), frame.stat().st_mtime)
    except OSError as exc:
        logger.debug("读取宠物帧 mtime 失败 %s: %s", frame, exc)
        return ""
    cached = _THUMB_CACHE.get(cache_key)
    if cached is not None:
        return cached
    data_uri = _encode_thumbnail(frame)
    _THUMB_CACHE[cache_key] = data_uri
    if len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        try:
            _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)), None)
        except Exception:  # noqa: BLE001 - 清理失败不影响本次返回
            _THUMB_CACHE.clear()
    return data_uri


def _builtin_entry(active_id: str) -> Dict[str, Any]:
    root = pet_dir_path()
    assets_dir = root / "assets"
    return {
        "id": BUILTIN_SKIN_ID,
        "name": BUILTIN_SKIN_NAME,
        "description": BUILTIN_SKIN_DESCRIPTION,
        "persona": "",
        "author": BUILTIN_SKIN_AUTHOR,
        "source": "builtin",
        "dir": str(root),
        "assets_dir": str(assets_dir),
        "config_path": str(root / "src" / "renderer" / "config.json"),
        "active": BUILTIN_SKIN_ID == active_id,
        "thumbnail": _thumbnail(assets_dir),
    }


def _meta_str(meta: Dict[str, Any], key: str, default: str = "") -> str:
    value = meta.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _read_pet_json(pkg_dir: Path) -> Dict[str, Any]:
    path = pkg_dir / "pet.json"
    if not _is_file(path):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("宠物包 %s 的 pet.json 不可用 %s: %s", pkg_dir.name, path, exc)
        return {}
    if not isinstance(data, dict):
        logger.debug("宠物包 %s 的 pet.json 不是对象，忽略: %r", pkg_dir.name, type(data))
        return {}
    return data


def _user_entry(pkg_dir: Path, active_id: str) -> Optional[Dict[str, Any]]:
    """把一个用户宠物包目录转成皮肤条目；不是合法包时返回 None 并记 debug。

    有效性只认一条硬指标：assets/pet/idle/frame_*.png 至少一张。名字、作者、台词
    都是可选的锦上添花，缺了不影响桌宠跑起来。
    """
    assets_dir = pkg_dir / "assets"
    if not _idle_frames(assets_dir):
        logger.debug(
            "跳过宠物包 %s：%s 下没有 %s",
            pkg_dir.name,
            assets_dir / _STATE_ROOT / _IDLE_STATE,
            _FRAME_PATTERN,
        )
        return None
    meta = _read_pet_json(pkg_dir)
    config_path = pkg_dir / "config.json"
    return {
        "id": pkg_dir.name,
        "name": _meta_str(meta, "name", pkg_dir.name),
        "description": _meta_str(meta, "description", ""),
        "persona": _meta_str(meta, "persona", ""),
        "author": _meta_str(meta, "author", ""),
        "source": "user",
        "dir": str(pkg_dir),
        "assets_dir": str(assets_dir),
        "config_path": str(config_path) if _is_file(config_path) else "",
        "active": pkg_dir.name == active_id,
        "thumbnail": _thumbnail(assets_dir),
    }


def _user_entries(active_id: str) -> List[Dict[str, Any]]:
    root = user_skin_dir()
    try:
        packages = sorted(
            (p for p in root.iterdir() if p.is_dir()),
            key=lambda p: p.name.casefold(),
        )
    except OSError as exc:
        logger.debug("扫描用户宠物目录失败 %s: %s", root, exc)
        return []
    entries: List[Dict[str, Any]] = []
    for pkg_dir in packages:
        try:
            entry = _user_entry(pkg_dir, active_id)
        except OSError as exc:
            # 单个包读盘失败（占用、权限、路径过长）只丢这一项，绝不冒泡。
            logger.debug("跳过宠物包 %s: %s", pkg_dir.name, exc)
            continue
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("id") or "")))
    return entries


def list_skins(active_id: str = "") -> List[Dict[str, Any]]:
    key = (active_id or "").strip()
    skins: List[Dict[str, Any]] = []
    try:
        skins.append(_builtin_entry(key))
    except Exception as exc:  # noqa: BLE001 - 内置项失败也要给调用方一个兜底条目
        logger.debug("构造内置皮肤条目失败: %s", exc)
        skins.append(_fallback_entry(key))
    try:
        skins.extend(_user_entries(key))
    except Exception as exc:  # noqa: BLE001 - 用户目录异常只影响"有没有额外皮肤"
        logger.debug("扫描用户皮肤失败: %s", exc)
    return skins


def _fallback_entry(active_id: str) -> Dict[str, Any]:
    return {
        "id": BUILTIN_SKIN_ID,
        "name": BUILTIN_SKIN_NAME,
        "description": BUILTIN_SKIN_DESCRIPTION,
        "persona": "",
        "author": BUILTIN_SKIN_AUTHOR,
        "source": "builtin",
        "dir": "",
        "assets_dir": "",
        "config_path": "",
        "active": not active_id or active_id == BUILTIN_SKIN_ID,
        "thumbnail": "",
    }


def get_skin(skin_id: str) -> Optional[Dict[str, Any]]:
    key = (skin_id or "").strip()
    if not key:
        return None
    for skin in list_skins(key):
        if skin.get("id") == key:
            return skin
    return None


def resolve_active(active_id: str = "") -> Dict[str, Any]:
    key = (active_id or "").strip()
    if key:
        skin = get_skin(key)
        if skin is not None:
            return skin
    skins = list_skins(key)
    if skins:
        return skins[0]
    return _fallback_entry(key)


def active_skin_id() -> str:
    value = _config_value("pet_skin_id", "")
    key = str(value or "").strip()
    if key and get_skin(key) is not None:
        return key
    return BUILTIN_SKIN_ID


def set_active_skin(skin_id: str) -> bool:
    key = (skin_id or "").strip()
    if not key:
        logger.debug("set_active_skin 收到空 id，忽略")
        return False
    try:
        skin = get_skin(key)
    except Exception as exc:  # noqa: BLE001 - 校验失败等价于"找不到"
        logger.warning("校验宠物皮肤 %r 失败: %s", key, exc)
        return False
    if skin is None:
        logger.debug("set_active_skin 拒绝未知皮肤 id: %r", key)
        return False
    try:
        from config import save_config_value
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存宠物皮肤失败（config 不可用）: %s", exc)
        return False
    try:
        return bool(save_config_value("pet_skin_id", skin["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("保存宠物皮肤 %r 失败: %s", key, exc)
        return False


_IMPORT_FRAME_MAX_PX = 256
_IMPORT_MAX_GIF_FRAMES = 60
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _safe_skin_id(name: str, root: Path) -> str:
    import re
    import time

    ascii_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip()).strip("_").lower()
    base = ascii_slug[:32] if ascii_slug else f"pet_{int(time.time())}"
    if base == BUILTIN_SKIN_ID:
        base = f"{base}_custom"
    candidate = base
    idx = 2
    while (root / candidate).exists():
        candidate = f"{base}_{idx}"
        idx += 1
    return candidate


def _write_pet_json(pkg_dir: Path, payload: Dict[str, Any]) -> None:
    """写回宠物包 pet.json（UTF-8 美化输出）。"""
    path = pkg_dir / "pet.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _save_pil_frame(img: Any, out_path: Path) -> None:
    from PIL import Image

    frame = img.convert("RGBA")
    if max(frame.size) > _IMPORT_FRAME_MAX_PX:
        resample = getattr(Image, "Resampling", Image).LANCZOS
        frame.thumbnail((_IMPORT_FRAME_MAX_PX, _IMPORT_FRAME_MAX_PX), resample)
    frame.save(out_path, format="PNG")


def _convert_images_to_frames(images: List[bytes], idle_dir: Path) -> int:
    idle_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    try:
        from PIL import Image, ImageSequence
    except Exception:
        for raw in images:
            if not raw:
                continue
            (idle_dir / f"frame_{count:03d}.png").write_bytes(raw)
            count += 1
        return count

    for raw in images:
        if not raw:
            continue
        try:
            with Image.open(io.BytesIO(raw)) as img:
                n_frames = getattr(img, "n_frames", 1)
                if n_frames > 1:
                    for idx, frame in enumerate(ImageSequence.Iterator(img)):
                        if idx >= _IMPORT_MAX_GIF_FRAMES:
                            break
                        _save_pil_frame(frame, idle_dir / f"frame_{count:03d}.png")
                        count += 1
                else:
                    _save_pil_frame(img, idle_dir / f"frame_{count:03d}.png")
                    count += 1
        except Exception as exc:
            logger.debug("跳过无法解码的导入图片: %s", exc)
    return count


def import_skin_from_images(
    name: str,
    images: List[bytes],
    *,
    persona: str = "",
    description: str = "",
    author: str = "",
) -> Dict[str, Any]:
    """从一组图片/GIF 字节创建用户宠物包，返回 {ok, msg, skin?}。"""
    clean_name = (name or "").strip() or "我的桌宠"
    if not images:
        return {"ok": False, "msg": "请至少选择一张图片或 GIF 动图"}

    root = user_skin_dir()
    skin_id = _safe_skin_id(clean_name, root)
    pkg_dir = root / skin_id
    idle_dir = pkg_dir / "assets" / _STATE_ROOT / _IDLE_STATE

    import shutil

    try:
        written = _convert_images_to_frames(images, idle_dir)
        if written <= 0:
            shutil.rmtree(pkg_dir, ignore_errors=True)
            return {"ok": False, "msg": "未能识别所选图片，请确认格式为 PNG/JPG/WebP/GIF"}
        meta = {
            "id": skin_id,
            "name": clean_name,
            "persona": (persona or "").strip(),
            "description": (description or "").strip(),
            "author": (author or "").strip() or "自定义",
        }
        _write_pet_json(pkg_dir, meta)
        skin = get_skin(skin_id)
        if skin is None:
            return {"ok": False, "msg": "导入成功但读取宠物包失败"}
        return {"ok": True, "msg": f"已导入桌宠「{clean_name}」（共 {written} 帧）", "skin": skin}
    except OSError as exc:
        shutil.rmtree(pkg_dir, ignore_errors=True)
        return {"ok": False, "msg": f"写入宠物目录失败: {exc}"}


def _find_package_root(extracted: Path) -> Path:
    if _idle_frames(extracted / "assets"):
        return extracted
    children = [p for p in extracted.iterdir() if p.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        if _idle_frames(children[0] / "assets"):
            return children[0]
        return children[0]
    for sub in children:
        if sub.is_dir() and _idle_frames(sub / "assets"):
            return sub
    return extracted


def import_skin_from_zip(
    zip_bytes: bytes,
    *,
    name: str = "",
    persona: str = "",
    description: str = "",
    author: str = "",
) -> Dict[str, Any]:
    """从 ZIP 压缩包导入用户宠物包：兼容标准星瑶宠物包及普通图片文件夹压缩包。"""
    import shutil
    import tempfile
    import zipfile

    if not zip_bytes:
        return {"ok": False, "msg": "压缩包内容为空"}

    root = user_skin_dir()
    with tempfile.TemporaryDirectory(prefix="ycode_pet_import_") as tmp:
        tmp_dir = Path(tmp)
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                for member in zf.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        return {"ok": False, "msg": "压缩包包含不安全路径，已拒绝导入"}
                zf.extractall(tmp_dir)
        except zipfile.BadZipFile:
            return {"ok": False, "msg": "无效的 ZIP 压缩包"}
        except OSError as exc:
            return {"ok": False, "msg": f"解压失败: {exc}"}

        src_root = _find_package_root(tmp_dir)
        existing_meta = _read_pet_json(src_root)
        clean_name = (
            (name or "").strip()
            or _meta_str(existing_meta, "name", "")
            or (src_root.name if src_root != tmp_dir else "")
            or "导入桌宠"
        )
        skin_id = _safe_skin_id(clean_name, root)
        dst_dir = root / skin_id

        try:
            if _idle_frames(src_root / "assets"):
                shutil.copytree(src_root, dst_dir)
            else:
                img_files = sorted(
                    (
                        p
                        for p in src_root.rglob("*")
                        if p.is_file()
                        and "__MACOSX" not in p.parts
                        and p.suffix.lower() in _IMAGE_SUFFIXES
                    ),
                    key=lambda p: str(p).casefold(),
                )
                if not img_files:
                    return {
                        "ok": False,
                        "msg": "压缩包内未找到 assets/pet/idle/frame_*.png 或可用图片",
                    }
                raw_list = [p.read_bytes() for p in img_files[:60]]
                written = _convert_images_to_frames(raw_list, dst_dir / "assets" / _STATE_ROOT / _IDLE_STATE)
                if written <= 0:
                    shutil.rmtree(dst_dir, ignore_errors=True)
                    return {"ok": False, "msg": "压缩包内图片无法解码"}

            merged_meta = dict(existing_meta)
            merged_meta["id"] = skin_id
            merged_meta["name"] = clean_name
            if persona.strip():
                merged_meta["persona"] = persona.strip()
            elif "persona" not in merged_meta:
                merged_meta["persona"] = ""
            if description.strip():
                merged_meta["description"] = description.strip()
            if author.strip():
                merged_meta["author"] = author.strip()
            elif not _meta_str(merged_meta, "author", ""):
                merged_meta["author"] = "自定义"
            _write_pet_json(dst_dir, merged_meta)
        except OSError as exc:
            shutil.rmtree(dst_dir, ignore_errors=True)
            return {"ok": False, "msg": f"导入宠物包失败: {exc}"}

    skin = get_skin(skin_id)
    if skin is None:
        return {"ok": False, "msg": "导入成功但读取宠物包失败"}
    return {"ok": True, "msg": f"已导入桌宠「{clean_name}」", "skin": skin}


def update_skin_meta(
    skin_id: str,
    *,
    name: Optional[str] = None,
    persona: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    """更新用户宠物包的名称与人设（内置星瑶不可改）。"""
    key = (skin_id or "").strip()
    if not key or key == BUILTIN_SKIN_ID:
        return {"ok": False, "msg": "内置星瑶不支持修改元数据"}
    pkg_dir = user_skin_dir() / key
    if not _is_dir(pkg_dir) or not _idle_frames(pkg_dir / "assets"):
        return {"ok": False, "msg": "找不到指定的自定义桌宠"}

    meta = _read_pet_json(pkg_dir)
    meta["id"] = key
    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            return {"ok": False, "msg": "桌宠名称不能为空"}
        meta["name"] = clean_name
    if persona is not None:
        meta["persona"] = persona.strip()
    if description is not None:
        meta["description"] = description.strip()

    try:
        _write_pet_json(pkg_dir, meta)
    except OSError as exc:
        return {"ok": False, "msg": f"保存人设失败: {exc}"}

    skin = get_skin(key)
    return {"ok": True, "msg": "已更新桌宠人设", "skin": skin}


def delete_skin(skin_id: str) -> Dict[str, Any]:
    """删除用户宠物包（内置星瑶不可删）；若删除当前激活项则回落星瑶。"""
    import shutil

    key = (skin_id or "").strip()
    if not key or key == BUILTIN_SKIN_ID:
        return {"ok": False, "msg": "内置星瑶不能删除"}
    pkg_dir = user_skin_dir() / key
    if not _is_dir(pkg_dir):
        return {"ok": False, "msg": "找不到指定的自定义桌宠"}

    was_active = active_skin_id() == key
    if was_active:
        set_active_skin(BUILTIN_SKIN_ID)

    try:
        shutil.rmtree(pkg_dir)
    except OSError as exc:
        return {"ok": False, "msg": f"删除失败: {exc}"}

    return {"ok": True, "msg": "已删除自定义桌宠", "was_active": was_active}
