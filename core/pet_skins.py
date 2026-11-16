r"""桌宠（桌面宠物）宠物库：内置星瑶 + 用户自建宠物包的统一读取层。

为什么单独成一个模块：
- 设置页的"换肤"要列出一张宠物卡片列表（缩略图、名字、作者、是否当前激活）。
  如果这套扫描逻辑散落在 js_api 桥和渲染前端里，"什么算一个合法宠物包"的判断
  迟早两处不一致。集中到这里以后，桥层只把 list_skins() 的结果原样转成 JSON，
  前端只负责画，换肤的语义只有一份实现。
- 内置星瑶不占用户目录：它的 assets 在 <桌宠目录>/assets，配置在
  <桌宠目录>/src/renderer/config.json；用户宠物包则放在
  %USERPROFILE%\.ycode\pets\<id>\，两者结构不同，但对调用方归一成同一份 dict，
  需要区分时看 "source" 字段即可。

宠物包约定（用户包目录 %USERPROFILE%\.ycode\pets\<id>\）：
    pet.json                          可选，{"id","name","description","author"}
                                      其中 id 只作说明用，真正的主键是目录名：
                                      否则作者把 id 写成和目录名不一致时，会出现
                                      "列表里看得到、按 id 却查不到"的怪现象。
    assets/pet/<state>/frame_NNN.png  至少要有 idle 状态的一张帧，否则该包视为无效
    assets/bubbles/<state>.txt        可选台词；缺省时桌宠不冒气泡，不影响可用性
    config.json                       可选，渲染配置覆盖；缺失时渲染端用内置默认值

内置星瑶（id=xingyao）：assets 在 <桌宠目录>/assets，配置在
<桌宠目录>/src/renderer/config.json。它永远排在列表第一位，也不能被删除或覆盖。

设计约束（读代码前先看这几条，能解释大部分写法）：
- 全部对外函数都不抛异常。用户目录属于"外部输入"：用户可能塞进半成品包、目录
  权限不足、文件正被同步盘占用。列表页因为一个坏包整体白屏是最糟的体验，因此
  坏包只 logger.debug 记一句原因就跳过，任何 OSError 都不外抛。
- 缩略图按 (路径, mtime) 做进程内缓存：设置页每次刷新都要重算所有宠物，一张
  256x256 PNG 走一次 Pillow 缩放要几毫秒，几十个皮肤就能让面板明显卡顿。
- 路径字段统一输出字符串而不是 Path：它们最终要经 js_api 序列化成 JSON 给前端，
  Path 不可 JSON 化；config_path 不存在时给空串，前端据此隐藏"打开配置"入口。

模块不 import pet_manager.get_skin 之外的东西，也不在 import 期读配置，避免与
core.pet_manager 形成循环导入（见 _config_value 的说明）。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 内置星瑶的固定 id。所有"未设置/设置无效"的情况都回落到它。
BUILTIN_SKIN_ID = "xingyao"

# 用户宠物包根目录。放在模块级常量里方便测试和别处引用；
# 运行期请用 user_skin_dir()（它会尽力创建目录，并保证返回的是这个常量）。
USER_SKIN_DIR: Path = Path.home() / ".ycode" / "pets"

# 内置星瑶的展示信息写死在代码里：它是产品的一部分，不该由配置文件决定
# （否则用户改坏 config.json 会把"星瑶"这个名字也一起弄丢）。
BUILTIN_SKIN_NAME = "星瑶"
BUILTIN_SKIN_DESCRIPTION = "星瑶的默认形象：白裙长发、会呼吸也会走动的少女，随对话做出拖拽与互动的动作。"
BUILTIN_SKIN_AUTHOR = "星瑶"

# 宠物包内部结构常量。改这里等于改"宠物包格式"，渲染端按同一套约定读盘。
_STATE_ROOT = "pet"
_IDLE_STATE = "idle"
_FRAME_PATTERN = "frame_*.png"

# 缩略图规格：设置页卡片按 96px 显示，输出再大只是浪费内存与 IPC 带宽。
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
]


# ---------- 小工具：一律不抛 -------------------------------------------------
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
    """返回 assets_dir/pet/idle 下按文件名排序的帧文件。

    有效性判断和缩略图都从这一个入口取帧，保证"列出来的一定画得出图"。
    目录不存在、无权限、遍历中途报错都返回空列表（调用方据此跳过该宠物包）。
    """
    try:
        idle_dir = assets_dir / _STATE_ROOT / _IDLE_STATE
        if not idle_dir.is_dir():
            return []
        frames = [p for p in idle_dir.glob(_FRAME_PATTERN) if p.is_file()]
    except OSError as exc:
        logger.debug("读取桌宠帧目录失败 %s: %s", assets_dir, exc)
        return []
    # 排序让缩略图稳定取 frame_000（哪个状态的第几帧不该影响列表外观）。
    return sorted(frames, key=lambda p: p.name)


def _config_value(key: str, default: Any = None) -> Any:
    """读单个配置项：先看模块级属性，再回落 config.json 的载荷。

    这里是有意照抄 core/pet_manager.py 同名函数的防御式写法，而不是 import 它：
    pet_manager 迟早要 import 本模块去解析激活皮肤，反向依赖会形成循环导入。
    两级都吞异常的原因也相同：config 模块的 __getattr__ 对未登记名字会抛
    AttributeError，而"读不到皮肤设置"只该回落默认形象，不该让整个换肤面板报错。
    """
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


# ---------- 路径 ----------
def pet_dir_path() -> Path:
    """桌宠根目录（内置星瑶的 assets / 配置都相对它定位）。

    优先级：YCODE_PET_DIR 环境变量（目录真实存在才采信）→ pet_manager 的
    同名函数 → 源码树里的 pets/xingyao_pet。

    为什么让 pet_manager 说话：打包后（PyInstaller onefile/onefolder）桌宠被解到
    _MEIPASS 或 exe 旁的 pets 目录，那套"装到哪"的规则只应有一份实现，这里抄一遍
    就会在改安装布局时悄悄失效。但它现在可能还没提供 pet_dir_path（历史版本只
    暴露 _pet_dir/PET_DIR），所以两种导入形式都试、都拿异常兜住，最后才用相对
    本文件的路径兜底——那只在开发机源码树里正确，属于"能用就行"的最后选择。
    """
    env_dir = (os.environ.get("YCODE_PET_DIR") or "").strip()
    if env_dir:
        try:
            candidate = Path(env_dir)
            if candidate.is_dir():
                return candidate
        except OSError:
            # 非法路径（空字节、盘符不存在）当环境变量没设，继续走下一条候选。
            logger.debug("YCODE_PET_DIR 不可用: %r", env_dir)

    # 两种导入形式都试：作为包被导入时用相对导入（不依赖 sys.path 上有项目根），
    # 被脚本/测试直接导入时相对导入会抛 ImportError，再退回绝对导入。
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
    """用户宠物包根目录，并尽力创建它。

    为什么要 mkdir：设置页第一次打开时这个目录通常还不存在，前端拿不到目录就会
    把"导入宠物"按钮灰掉；这里顺手建好，用户点一次就有地方落包。建不出来（只读
    磁盘、权限不足）也不能让列表页失败——返回路径让调用方按"空目录"处理即可。
    """
    try:
        USER_SKIN_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug("创建用户宠物目录失败（按不存在处理）%s: %s", USER_SKIN_DIR, exc)
    except Exception as exc:  # noqa: BLE001 - 目录处理不该影响换肤面板
        logger.debug("创建用户宠物目录异常 %s: %s", USER_SKIN_DIR, exc)
    return USER_SKIN_DIR


# ---------- 缩略图 ----------
def _png_data_uri(raw: bytes) -> str:
    """把原始 PNG 字节包成 data URI；编码异常返回空串（宁可没图也别抛）。"""
    try:
        return _DATA_URI_PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.debug("缩略图 base64 编码失败: %s", exc)
        return ""


def _encode_thumbnail(frame: Path) -> str:
    """把一帧桌宠图缩成 ≤96px 的 PNG data URI。

    先读原始字节再做 Pillow 处理：Pillow 导入失败（裁剪构建）或图片损坏时，退回
    直接 base64 原图——设置页显示一张没缩放的图，总好过显示占位符；只有连文件都
    读不出来时才返回空串。
    """
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
            # 统一成 RGBA：桌宠帧带透明通道，列表卡片背景可能是任意色，丢 alpha 会出黑边。
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
    """取 idle 第一帧的缩略图 data URI，失败返回空串。

    为什么只截 idle：列表要的是"这个宠物长什么样"，idle 是唯一被约定必须存在的
    状态（别的状态缺了不算坏包），用它当封面才不会出现"列表里有、却没头图"。
    缓存键带 mtime：作者改图后无需重启星瑶，下一次刷新就是新图。
    """
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
    # 空串也缓存：损坏的帧反复重试只会拖慢每次刷新，且用户换图会改 mtime 自动失效。
    _THUMB_CACHE[cache_key] = data_uri
    if len(_THUMB_CACHE) > _THUMB_CACHE_MAX:
        try:
            _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)), None)
        except Exception:  # noqa: BLE001 - 清理失败不影响本次返回
            _THUMB_CACHE.clear()
    return data_uri


# ---------- 皮肤条目 ----------
def _builtin_entry(active_id: str) -> Dict[str, Any]:
    """内置星瑶的皮肤条目。结构性字段不依赖磁盘，缩略图失败也给空串。"""
    root = pet_dir_path()
    assets_dir = root / "assets"
    return {
        "id": BUILTIN_SKIN_ID,
        "name": BUILTIN_SKIN_NAME,
        "description": BUILTIN_SKIN_DESCRIPTION,
        "author": BUILTIN_SKIN_AUTHOR,
        "source": "builtin",
        "dir": str(root),
        "assets_dir": str(assets_dir),
        # 内置配置是必装的，即便暂时找不到也给出路径：前端据此可以提示"文件缺失"。
        "config_path": str(root / "src" / "renderer" / "config.json"),
        "active": BUILTIN_SKIN_ID == active_id,
        "thumbnail": _thumbnail(assets_dir),
    }


def _meta_str(meta: Dict[str, Any], key: str, default: str = "") -> str:
    """取 pet.json 里的字符串字段：缺失、非字符串、纯空白都回落 default。"""
    value = meta.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _read_pet_json(pkg_dir: Path) -> Dict[str, Any]:
    """读宠物包元数据；缺失、JSON 损坏、不是对象都当"没有元数据"。

    坏 pet.json 不应该让整个包消失：包里只要有 idle 帧就能用，只是显示目录名，
    比在列表里凭空丢一个宠物更好解释。
    """
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
        # 主键永远是目录名：它才是 %USERPROFILE%\.ycode\pets\ 下的唯一标识。
        "id": pkg_dir.name,
        "name": _meta_str(meta, "name", pkg_dir.name),
        "description": _meta_str(meta, "description", ""),
        "author": _meta_str(meta, "author", ""),
        "source": "user",
        "dir": str(pkg_dir),
        "assets_dir": str(assets_dir),
        # 空串表示"这个包没有配置覆盖"，前端据此隐藏"打开配置"入口。
        "config_path": str(config_path) if _is_file(config_path) else "",
        "active": pkg_dir.name == active_id,
        "thumbnail": _thumbnail(assets_dir),
    }


def _user_entries(active_id: str) -> List[Dict[str, Any]]:
    """扫用户目录，返回有效宠物包条目（按显示名排序）。"""
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
    # 按名字排（大小写不敏感），同名再按 id 兜住稳定顺序：列表位置不该每次刷新都变。
    entries.sort(key=lambda item: (str(item.get("name") or "").casefold(), str(item.get("id") or "")))
    return entries


def list_skins(active_id: str = "") -> List[Dict[str, Any]]:
    """列出全部可用皮肤：内置星瑶恒在首位，其后是用户宠物包（按名字排序）。

    active_id 由调用方传入而不是在这里读配置：桥层可能刚在别处改过激活项（或正在
    预览某个未保存的选择），让调用方决定"谁是当前皮肤"比分两次读配置更可控。
    内置项构造失败也返回最小条目，保证返回值永远非空——调用方（含 resolve_active）
    可以无条件取 [0]。
    """
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
    """内置条目都构造不出来时的最小可用条目（字段齐全、图留空）。"""
    return {
        "id": BUILTIN_SKIN_ID,
        "name": BUILTIN_SKIN_NAME,
        "description": BUILTIN_SKIN_DESCRIPTION,
        "author": BUILTIN_SKIN_AUTHOR,
        "source": "builtin",
        "dir": "",
        "assets_dir": "",
        "config_path": "",
        "active": not active_id or active_id == BUILTIN_SKIN_ID,
        "thumbnail": "",
    }


def get_skin(skin_id: str) -> Optional[Dict[str, Any]]:
    """按 id 取皮肤条目；空 id 或找不到返回 None。

    找不到就返回 None 而不是回落内置：调用方需要区分"用户指定的 id 无效"（例如
    set_active_skin 应拒绝写入）与"没设置，用默认"。要回落请用 resolve_active。
    """
    key = (skin_id or "").strip()
    if not key:
        return None
    for skin in list_skins(key):
        if skin.get("id") == key:
            return skin
    return None


def resolve_active(active_id: str = "") -> Dict[str, Any]:
    """按 id 取皮肤，无效时回落内置星瑶（list_skins 的首项）。

    桌宠启动时只有"配置里的 id"这一个输入，而配置可能指向已删除/改名的宠物包。
    桌面宠物因为一个失效 id 起不来是灾难性的，所以这里必须给出可渲染的默认形象。
    """
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
    """当前激活皮肤的 id；读不到或指向不存在的包时返回内置 id。

    返回值一定经过 get_skin 校验：桌面宠物按"saved id → 资产目录"直接读盘，配置里
    留着一个已删除的皮肤 id 会让它整只显示成白框，这里提前收敛掉这类脏值。
    """
    value = _config_value("pet_skin_id", "")
    key = str(value or "").strip()
    if key and get_skin(key) is not None:
        return key
    return BUILTIN_SKIN_ID


def set_active_skin(skin_id: str) -> bool:
    """把当前皮肤写入配置（键 pet_skin_id），成功返回 True。

    先 get_skin 校验再写：配置是所有会话共享的，写进一个不存在的 id 等于把别处
    正在用的桌宠一起弄坏。写失败只 warning 并返回 False（save_config_value 内部
    已记过具体原因），调用方据此提示用户，不必关心异常类型。
    """
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
