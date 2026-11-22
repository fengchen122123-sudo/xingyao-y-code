from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
from core.memory_kairos import _contains_memory_directive

logger = logging.getLogger("ycode.identity")

MAX_FILE_BYTES = 8 * 1024
MAX_STRING_CHARS = 200
MAX_TRAITS = 12
MAX_VOICE_ITEMS = 6
MAX_VALUES = 10
MAX_GROWTH_LOG = 100
MAX_PROFILE_ITEMS = 20
MAX_CHANGELOG_LINES = 200
MAX_USER_KEY_LEN = 64
MAX_BACKUPS = 5
MAX_LESSON_FIELD_CHARS = 4000

DEFAULT_USER_KEY = "default"
SELF_STAGES = ("awakening", "growing", "settled")
DEFAULT_SETTINGS = {"enabled": True, "learning_enabled": True, "user_key": ""}

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_USER_KEY_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_PROFILE_HEADINGS = {"称呼": "title", "偏好": "preferences", "禁区": "forbidden"}
_LESSON_HEADINGS = {"场景": "scene", "做法": "how"}

# 可重入：写路径（save_self_model / update_user_profile / save_lesson）在持锁
# 期间会调用 append_changelog，后者也要拿同一把锁。所有「读 → 合并 → 写」都
# 在这把锁内完成，否则并发调用会读到同一个旧值并互相覆盖。
_io_lock = threading.RLock()


def _root() -> Path:
    try:
        raw = getattr(config, "YCODE_DIR", "") or ""
    except Exception:
        raw = ""
    try:
        return Path(str(raw)).expanduser()
    except Exception:
        return Path(".")


def _fs_key(path) -> str:
    text = os.fspath(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text))


def _safe_path(*parts) -> Path | None:
    try:
        root = _root().resolve()
    except Exception as exc:
        logger.debug("解析数据根目录失败：%s", exc)
        return None
    try:
        resolved = root.joinpath(*parts).resolve()
    except Exception as exc:
        logger.debug("解析目标路径失败：%s (%s)", parts, exc)
        return None
    # Windows 上 resolve() 有时返回带 \\?\ 长路径前缀的形式（并发创建目录、
    # 或路径经由不同 API 解析时尤其常见），与根目录的普通形态不一致，
    # 直接 relative_to 会把合法路径误判为越界——并发写 lesson 时偶发失败
    # 就是这里被判越界导致的。两边统一规整后再比较。
    root_key = _fs_key(root)
    resolved_key = _fs_key(resolved)
    if resolved_key != root_key and not resolved_key.startswith(root_key + os.sep):
        logger.debug("路径越界被拒绝：%s (解析结果 %s)", parts, resolved_key)
        return None
    return resolved


def _is_safe_name(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if ".." in value or "/" in value or "\\" in value:
        return False
    if os.path.splitdrive(value)[0]:
        return False
    return bool(_NAME_RE.fullmatch(value))


def _sanitize_user_key(raw) -> str:
    text = "" if raw is None else str(raw).strip()
    if not text:
        return ""
    cleaned = _USER_KEY_SAFE_RE.sub("_", text)
    # ".." 即使不含分隔符也是穿越意图，直接压平，避免残留路径语义
    cleaned = cleaned.replace("..", "_")
    cleaned = re.sub(r"_{2,}", "_", cleaned).strip("._-")
    if len(cleaned) > MAX_USER_KEY_LEN:
        digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]
        cleaned = f"{cleaned[: MAX_USER_KEY_LEN - 11]}_{digest}"
    return cleaned


def _system_user() -> str:
    try:
        return getpass.getuser() or ""
    except Exception:
        return ""


def resolve_user_key() -> str:
    try:
        settings_key = get_growth_settings().get("user_key", "")
    except Exception:
        settings_key = ""
    for raw in (settings_key, os.environ.get("YCODE_USER"), _system_user()):
        key = _sanitize_user_key(raw)
        if key:
            return key
    return DEFAULT_USER_KEY


def _resolve_uid(uid=None) -> str:
    """解析用户目录名：显式 uid 优先，缺省用 resolve_user_key()；非法返回 ""。

    显式传入的 uid 必须原样通过白名单校验（含 ".."、路径分隔符、盘符一律拒绝），
    不做静默改写——否则 "../etc" 会被悄悄改写成 "etc"，写进别的用户目录。
    """
    if uid is None:
        key = _sanitize_user_key(resolve_user_key())
    else:
        key = uid if _is_safe_name(uid) else ""
    if not key or not _is_safe_name(key):
        return ""
    return key


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


_REDACT_FN = None


def _redact(text: str) -> str:
    global _REDACT_FN
    if _REDACT_FN is None:
        try:
            from core.decision_gateway import redact_sensitive as _fn

            _REDACT_FN = _fn
        except Exception as exc:
            logger.debug("脱敏模块不可用，跳过脱敏：%s", exc)
            _REDACT_FN = False
    if not _REDACT_FN or not text:
        return text
    try:
        return _REDACT_FN(text)
    except Exception as exc:
        logger.debug("脱敏失败，保留原文：%s", exc)
        return text


def _one_line(value, limit=MAX_STRING_CHARS) -> str:
    """压成单行、脱敏并截断：frontmatter / 列表项都不允许换行破坏结构。"""
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    text = _redact(text)
    return text[:limit]


def _clip_list(
    value, limit, item_chars=MAX_STRING_CHARS, keep="head"
) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items = []
    for item in value:
        text = _one_line(item, item_chars)
        if not text:
            continue
        items.append(text)
        if keep != "tail" and len(items) >= limit:
            break
    if keep == "tail" and len(items) > limit:
        items = items[-limit:]
    return items


def _has_directive(value) -> bool:
    if isinstance(value, str):
        return _contains_memory_directive(value)
    if isinstance(value, dict):
        return any(_has_directive(k) or _has_directive(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return any(_has_directive(item) for item in value)
    return False


def _filter_directive_items(values) -> list[str]:
    """逐条剔除命中劫持句式的条目。

    读路径也走这一步：历史文件里已落盘的脏内容必须在这里被「稀释」掉，
    否则它会一直留在合并结果里，让之后每一次写入都被整体拒绝（毒丸效应）。
    """
    if not isinstance(values, (list, tuple)):
        return []
    return [item for item in values if not _contains_memory_directive(item)]


def _clean_text(value, limit=MAX_STRING_CHARS) -> str:
    text = _one_line(value, limit)
    if text and _contains_memory_directive(text):
        return ""
    return text


def _too_large(text) -> bool:
    try:
        return len(str(text).encode("utf-8")) > MAX_FILE_BYTES
    except Exception:
        return True


def _read_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as exc:
        logger.debug("读取 %s 失败，回落默认值：%s", path, exc)
        return ""


def _quarantine_corrupt(path: Path | None) -> None:
    """把解析失败的文件改名留证，避免下一次写入静默覆盖原始内容。"""
    if path is None:
        return
    try:
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = path.with_name(f"{path.name}.corrupt-{stamp}")
        index = 2
        while target.exists() and index <= 99:
            target = path.with_name(f"{path.name}.corrupt-{stamp}-{index}")
            index += 1
        path.rename(target)
        logger.debug("坏文件已隔离：%s", target.name)
    except Exception as exc:
        logger.debug("隔离坏文件 %s 失败：%s", path, exc)


def _read_json(path: Path | None):
    raw = _read_text(path)
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.debug("解析 %s 失败，回落默认值并隔离原文件：%s", path, exc)
        _quarantine_corrupt(path)
        return None


def _write_text(path: Path | None, text: str) -> bool:
    """临时文件 + os.replace 原子写；父目录懒创建。失败只记 debug 返回 False。

    Windows 上 os.replace 可能被索引 / 杀毒服务瞬时占用而失败，因此短暂重试几次。
    没有重试时，并发写入会偶发静默丢更新（lesson 的 hits 少计就是这么来的）。
    """
    if path is None:
        return False
    for attempt in range(3):
        tmp_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(
                f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
            )
            tmp_path.write_text(text, encoding="utf-8")
            os.replace(tmp_path, path)
            return True
        except Exception as exc:
            logger.debug("写入 %s 第 %s 次失败：%s", path, attempt + 1, exc)
            try:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            if attempt < 2:
                time.sleep(0.02)
    # 原子替换持续失败（Windows 上索引 / 杀毒服务可能长时间占用目标文件）：
    # 退化为直接写。所有写入都在 _io_lock 内串行，直接写不会互相覆盖，
    # 比「静默丢更新」安全得多。
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except Exception as exc:
        logger.debug("写入 %s 回退直接写也失败：%s", path, exc)
        return False


def safe_data_path(*parts) -> Path | None:
    """公开的路径解析：同层模块（learning）复用同一套越界校验。"""
    return _safe_path(*parts)


def write_text_atomic(path: Path | None, text: str) -> bool:
    """公开的原子写：同层模块复用重试 + 回退逻辑，避免各自实现一份。"""
    return _write_text(path, text)


def _split_frontmatter(text: str):
    if not text:
        return {}, ""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(lines[index + 1 :])
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return {}, text


def _render_frontmatter(meta: dict, body: str) -> str:
    head = ["---"]
    head.extend(f"{key}: {_one_line(value, 300)}" for key, value in meta.items())
    head.append("---")
    return "\n".join(head) + "\n\n" + (body or "").strip() + "\n"


def _parse_sections(body: str, headings: dict) -> dict:
    result = {field: ("" if field == "title" else []) for field in headings.values()}
    current = None
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = headings.get(stripped[3:].strip())
            continue
        if current is None or not stripped:
            continue
        if current == "title":
            if not result["title"]:
                result["title"] = stripped
        else:
            result[current].append(stripped[2:].strip() if stripped.startswith("- ") else stripped)
    return result


def _settings_path() -> Path | None:
    return _safe_path("self", "settings.json")


def get_growth_settings() -> dict:
    """读 self/settings.json；坏文件或字段缺失一律回落默认值。"""
    result = dict(DEFAULT_SETTINGS)
    data = _read_json(_settings_path())
    if not isinstance(data, dict):
        return result
    for key, default in DEFAULT_SETTINGS.items():
        value = data.get(key, default)
        if isinstance(default, bool):
            result[key] = bool(value)
        else:
            result[key] = _one_line(value, MAX_USER_KEY_LEN)
    return result


def _migrate_user_profile(old_key: str, new_key: str) -> None:
    if not old_key or not new_key or old_key == new_key:
        return
    source = _safe_path("users", old_key)
    target = _safe_path("users", new_key)
    if source is None or target is None:
        return
    try:
        if not source.is_dir() or target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
        append_changelog("user", f"档案已从 {old_key} 迁移到 {new_key}")
    except Exception as exc:
        logger.debug("迁移用户档案 %s → %s 失败：%s", old_key, new_key, exc)


def save_growth_settings(**patch) -> dict:
    """合并写回成长设置（未知键忽略），返回合并后的完整设置。"""
    with _io_lock:
        # 必须在合并之前取旧键：合并后再读设置拿到的已经是新值了。
        previous_key = _resolve_uid(None)
        merged = get_growth_settings()
        for key, value in (patch or {}).items():
            if key not in DEFAULT_SETTINGS:
                continue
            merged[key] = (
                bool(value)
                if isinstance(DEFAULT_SETTINGS[key], bool)
                else _one_line(value, MAX_USER_KEY_LEN)
            )
        # 改用户键时把旧档案迁到新键下，否则旧数据会变成界面看不见的孤儿目录。
        new_key = merged.get("user_key") or ""
        if new_key and new_key != previous_key:
            _migrate_user_profile(previous_key, new_key)
        if _has_directive(merged):
            append_changelog("settings", "拒绝写入：设置内容含可疑指令")
            return merged
        path = _settings_path()
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        if _too_large(text):
            append_changelog("settings", "拒绝写入：settings.json 超过 8KB 上限")
            return merged
        _write_text(path, text)
        return merged


def _default_self_model() -> dict:
    return {
        "version": 1,
        "stage": "awakening",
        "traits": [],
        "voice": {"openers": [], "avoid": []},
        "values": [],
        "growth_log": [],
        "updated_at": "",
    }


def _normalize_growth_log(value) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    entries = []
    for item in value:
        if isinstance(item, dict):
            date_text = _one_line(item.get("date"), 40)
            note = _one_line(item.get("note"), MAX_STRING_CHARS)
        else:
            date_text, note = "", _one_line(item, MAX_STRING_CHARS)
        if not note or _contains_memory_directive(note):
            continue
        entries.append({"date": date_text, "note": note})
    if len(entries) > MAX_GROWTH_LOG:
        entries = entries[-MAX_GROWTH_LOG:]
    return entries


def _normalize_self_model(data) -> dict:
    model = _default_self_model()
    if not isinstance(data, dict):
        return model
    try:
        version = int(data.get("version", 1))
    except Exception:
        version = 1
    model["version"] = version if version > 0 else 1
    stage = _one_line(data.get("stage"), 20)
    model["stage"] = stage if stage in SELF_STAGES else "awakening"
    model["traits"] = _filter_directive_items(
        _clip_list(data.get("traits"), MAX_TRAITS)
    )
    voice = data.get("voice")
    if isinstance(voice, dict):
        model["voice"] = {
            "openers": _filter_directive_items(
                _clip_list(voice.get("openers"), MAX_VOICE_ITEMS)
            ),
            "avoid": _filter_directive_items(
                _clip_list(voice.get("avoid"), MAX_VOICE_ITEMS)
            ),
        }
    model["values"] = _filter_directive_items(
        _clip_list(data.get("values"), MAX_VALUES)
    )
    model["growth_log"] = _normalize_growth_log(data.get("growth_log"))
    model["updated_at"] = _one_line(data.get("updated_at"), 40)
    return model


def get_self_model() -> dict:
    """读 self/self.json；缺失/坏 JSON 都回落默认 schema，绝不抛异常。"""
    return _normalize_self_model(_read_json(_safe_path("self", "self.json")))


def save_self_model(patch: dict) -> bool:
    """白名单字段合并写入 self/self.json；本次入参含劫持指令或超 8KB 拒绝。"""
    if not isinstance(patch, dict):
        return False
    path = _safe_path("self", "self.json")
    if path is None:
        return False
    # 整个「读 → 合并 → 写」持锁：否则并发调用会读到同一份旧模型并互相覆盖。
    with _io_lock:
        # 只检查本次 patch。历史遗留的脏条目已在读路径被剔除，不能因为它们
        # 让此后每一次写入都被整体拒绝（毒丸效应）。
        if _has_directive(patch):
            append_changelog("self", "拒绝写入：自我模型内容含可疑指令")
            return False
        merged = _normalize_self_model(get_self_model())
        if "stage" in patch:
            stage = _one_line(patch.get("stage"), 20)
            if stage in SELF_STAGES:
                merged["stage"] = stage
        if "traits" in patch:
            merged["traits"] = _clip_list(patch.get("traits"), MAX_TRAITS)
        if "values" in patch:
            merged["values"] = _clip_list(patch.get("values"), MAX_VALUES)
        if isinstance(patch.get("voice"), dict):
            voice = patch["voice"]
            for key in ("openers", "avoid"):
                if key in voice:
                    merged["voice"][key] = _clip_list(voice.get(key), MAX_VOICE_ITEMS)
        if "growth_log" in patch:
            merged["growth_log"] = _normalize_growth_log(patch.get("growth_log"))
        if _has_directive(merged):
            append_changelog("self", "拒绝写入：自我模型内容含可疑指令")
            return False
        merged["updated_at"] = _now_iso()
        text = json.dumps(merged, ensure_ascii=False, indent=2)
        if _too_large(text):
            append_changelog("self", "拒绝写入：self.json 超过 8KB 上限")
            return False
        return _write_text(path, text)


def _profile_path(uid=None) -> Path | None:
    key = _resolve_uid(uid)
    if not key:
        return None
    return _safe_path("users", key, "profile.md")


def get_user_profile(uid=None) -> dict:
    """读用户档案；缺失时返回空称呼的空档案。"""
    key = _resolve_uid(uid)
    profile = {
        "uid": key,
        "title": "",
        "relation": "",
        "preferences": [],
        "forbidden": [],
        "updated_at": "",
    }
    if not key:
        return profile
    text = _read_text(_profile_path(key))
    meta, body = _split_frontmatter(text)
    sections = _parse_sections(body, _PROFILE_HEADINGS)
    profile["uid"] = _one_line(meta.get("uid"), 64) or key
    # 读侧同样保留尾部：写侧被裁剪到最新 20 条后，读侧再按头部截一次会把
    # 刚写入的新偏好立刻丢掉，两侧口径必须一致。同时逐条剔除劫持句式：
    # 历史脏数据必须在读时被稀释，否则它会一直留在合并结果里毒化后续写入。
    profile["title"] = _clean_text(
        _one_line(meta.get("title"), MAX_STRING_CHARS) or sections["title"]
    )
    profile["relation"] = _clean_text(meta.get("relation"))
    profile["updated_at"] = _one_line(meta.get("updated_at"), 40)
    profile["preferences"] = _filter_directive_items(
        _clip_list(sections["preferences"], MAX_PROFILE_ITEMS, keep="tail")
    )
    profile["forbidden"] = _filter_directive_items(
        _clip_list(sections["forbidden"], MAX_PROFILE_ITEMS, keep="tail")
    )
    return profile


def update_user_profile(
    uid=None, title=None, preferences=None, forbidden=None, relation=None
) -> bool:
    """合并写入用户档案（None 表示该字段不变），超 8KB 或本次入参含劫持指令拒绝。"""
    key = _resolve_uid(uid)
    path = _profile_path(key)
    if not key or path is None:
        return False
    with _io_lock:
        if _has_directive(
            {
                "title": title,
                "relation": relation,
                "preferences": preferences,
                "forbidden": forbidden,
            }
        ):
            append_changelog("user", "拒绝写入：用户档案内容含可疑指令")
            return False
        current = get_user_profile(key)
        if title is not None:
            current["title"] = _one_line(title, MAX_STRING_CHARS)
        if relation is not None:
            current["relation"] = _one_line(relation, MAX_STRING_CHARS)
        if preferences is not None:
            current["preferences"] = _clip_list(
                preferences, MAX_PROFILE_ITEMS, keep="tail"
            )
        if forbidden is not None:
            current["forbidden"] = _clip_list(
                forbidden, MAX_PROFILE_ITEMS, keep="tail"
            )
        if _has_directive(current):
            append_changelog("user", "拒绝写入：用户档案内容含可疑指令")
            return False
        current["updated_at"] = _now_iso()
        body_lines = ["## 称呼", current["title"], "", "## 偏好"]
        body_lines.extend(f"- {item}" for item in current["preferences"])
        body_lines.extend(["", "## 禁区"])
        body_lines.extend(f"- {item}" for item in current["forbidden"])
        meta = {
            "uid": key,
            "title": current["title"],
            "relation": current["relation"],
            "updated_at": current["updated_at"],
        }
        text = _render_frontmatter(meta, "\n".join(body_lines))
        if _too_large(text):
            append_changelog("user", "拒绝写入：profile.md 超过 8KB 上限")
            return False
        return _write_text(path, text)


def _normalize_lesson(meta: dict, body: str) -> dict:
    sections = _parse_sections(body, _LESSON_HEADINGS)
    try:
        hits = max(0, int(meta.get("hits", 0) or 0))
    except Exception:
        hits = 0
    return {
        "slug": _one_line(meta.get("slug"), 64),
        "kind": _one_line(meta.get("kind"), 32) or "lesson",
        "hits": hits,
        "created_at": _one_line(meta.get("created_at"), 40),
        "last_hit_at": _one_line(meta.get("last_hit_at"), 40),
        "source": _one_line(meta.get("source"), MAX_STRING_CHARS),
        "scene": sections["scene"][0] if sections["scene"] else "",
        "how": sections["how"][0] if sections["how"] else "",
    }


def iter_lessons() -> list[dict]:
    """列出全部经验教训（按最近命中时间倒序）；目录缺失/坏文件都跳过。"""
    directory = _safe_path("lessons")
    if directory is None:
        return []
    try:
        paths = sorted(directory.glob("*.md"))
    except Exception as exc:
        logger.debug("列举 lessons 失败：%s", exc)
        return []
    lessons = []
    for path in paths:
        if not _is_safe_name(path.stem):
            continue
        meta, body = _split_frontmatter(_read_text(path))
        if not meta:
            meta = {"slug": path.stem}
        lesson = _normalize_lesson(meta, body)
        lesson["slug"] = lesson["slug"] or path.stem
        lessons.append(lesson)
    lessons.sort(key=lambda item: (item["last_hit_at"], item["slug"]), reverse=True)
    return lessons


def save_lesson(slug, scene, how, kind="lesson", source="") -> bool:
    if not _is_safe_name(slug):
        return False
    path = _safe_path("lessons", f"{slug}.md")
    if path is None:
        return False
    # hits 的「读旧值 → +1 → 写回」必须持锁，否则并发命中会互相覆盖导致少计。
    with _io_lock:
        scene_text = _one_line(scene, MAX_LESSON_FIELD_CHARS)
        how_text = _one_line(how, MAX_LESSON_FIELD_CHARS)
        kind_text = _one_line(kind, 32) or "lesson"
        source_text = _one_line(source, MAX_STRING_CHARS)
        if _has_directive([slug, scene_text, how_text, kind_text, source_text]):
            append_changelog("lesson", f"拒绝写入：{slug} 内容含可疑指令")
            return False
        now = _now_iso()
        previous = {}
        if path.exists():
            meta, _body = _split_frontmatter(_read_text(path))
            previous = _normalize_lesson(meta, "") if meta else {}
        try:
            hits = int(previous.get("hits", 0) or 0) + 1
        except Exception:
            hits = 1
        meta = {
            "slug": slug,
            "kind": kind_text,
            "hits": hits,
            "created_at": previous.get("created_at") or now,
            "last_hit_at": now,
            "source": source_text,
        }
        body = f"## 场景\n{scene_text}\n\n## 做法\n{how_text}"
        text = _render_frontmatter(meta, body)
        if _too_large(text):
            append_changelog("lesson", f"拒绝写入：{slug} 超过 8KB 上限")
            return False
        return _write_text(path, text)


def _changelog_path() -> Path | None:
    return _safe_path("self", "changelog.md")


def append_changelog(kind: str, summary: str) -> None:
    """追加一行变更记录（超 200 条从头截断）；失败静默，绝不抛出。"""
    kind_text = _one_line(kind, 20) or "misc"
    summary_text = _one_line(summary, MAX_STRING_CHARS)
    if not summary_text or _has_directive([kind_text, summary_text]):
        return
    path = _changelog_path()
    if path is None:
        return
    line = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} [{kind_text}] {summary_text}"
    try:
        with _io_lock:
            existing = [row for row in _read_text(path).splitlines() if row.strip()]
            existing.append(line)
            if len(existing) > MAX_CHANGELOG_LINES:
                existing = existing[-MAX_CHANGELOG_LINES:]
            _write_text(path, "\n".join(existing) + "\n")
    except Exception as exc:
        logger.debug("追加 changelog 失败：%s", exc)


def get_changelog(limit=50) -> list[str]:
    """返回文件尾部最近 limit 行（时间倒序由调用方处理）。"""
    try:
        limit = int(limit)
    except Exception:
        limit = 50
    if limit <= 0:
        return []
    rows = [row for row in _read_text(_changelog_path()).splitlines() if row.strip()]
    return rows[-limit:]


def _prune_backups(parent: Path | None, base_name: str) -> None:
    """只保留最近 ``MAX_BACKUPS`` 份 <base>.bak-* 备份，其余删除。

    备份是「误删后救回来」的兜底，不是长期归档；不清理会随 reset 次数无限堆积。
    清理失败只记 debug，绝不因为删不掉旧备份而让 reset 失败。
    """
    if parent is None:
        return
    try:
        backups = sorted(parent.glob(f"{base_name}.bak-*"))
    except Exception as exc:
        logger.debug("列举备份失败：%s", exc)
        return
    if len(backups) <= MAX_BACKUPS:
        return
    for stale in backups[:-MAX_BACKUPS]:
        try:
            if stale.is_dir():
                shutil.rmtree(stale)
            else:
                stale.unlink()
            logger.debug("已清理旧备份：%s", stale.name)
        except Exception as exc:
            logger.debug("清理旧备份 %s 失败：%s", stale, exc)


def _backup_dir(path: Path | None) -> bool:
    if path is None:
        return False
    try:
        if not path.exists():
            return True
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        target = path.with_name(f"{path.name}.bak-{stamp}")
        index = 2
        while target.exists() and index <= 99:
            target = path.with_name(f"{path.name}.bak-{stamp}-{index}")
            index += 1
        path.rename(target)
        _prune_backups(path.parent, path.name)
        return True
    except Exception as exc:
        logger.debug("重置目录 %s 失败：%s", path, exc)
        return False


def reset_self() -> bool:
    """重置自我成长数据：self/ 整体改名备份，下次写入时再建。"""
    return _backup_dir(_safe_path("self"))


def reset_user(uid=None) -> bool:
    """重置单个用户档案：users/<uid> 整体改名备份。"""
    key = _resolve_uid(uid)
    if not key:
        return False
    return _backup_dir(_safe_path("users", key))


def reset_all() -> bool:
    """重置 self/users/lessons 三处数据；任一失败即 False（已完成部分不回滚）。"""
    results = [
        reset_self(),
        _backup_dir(_safe_path("users")),
        _backup_dir(_safe_path("lessons")),
    ]
    return all(results)


def get_growth_snapshot() -> dict:
    """给 GUI 的一次性快照：身份、称呼、阶段、特质、经验数与设置。"""
    try:
        settings = get_growth_settings()
    except Exception:
        settings = dict(DEFAULT_SETTINGS)
    try:
        user_key = resolve_user_key()
    except Exception:
        user_key = DEFAULT_USER_KEY
    profile = get_user_profile(user_key)
    model = get_self_model()
    lessons = iter_lessons()
    last_learning = ""
    for lesson in lessons:
        if lesson["last_hit_at"] > last_learning:
            last_learning = lesson["last_hit_at"]
    return {
        "user_key": user_key,
        "title": profile.get("title", ""),
        "stage": model.get("stage", "awakening"),
        "traits": list(model.get("traits", [])),
        "lessons_count": len(lessons),
        "last_learning_at": last_learning,
        "settings": settings,
    }
