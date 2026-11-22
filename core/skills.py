import os
import re
import sys
import json
import shutil
import pathlib
import hashlib
import logging
import threading
import config
from core.workspace_context import get_workdir
ACTIVE_SKILLS_FILE = config.ACTIVE_SKILLS_FILE

# 激活技能文件写锁，防止并发写互相截断
_file_lock = threading.Lock()

SKILL_NAME_MAX_CHARS = 120
SKILL_DESCRIPTION_MAX_CHARS = 500
SKILL_TRIGGERS_MAX_CHARS = 240


# ------------------------------------------------------------------
# 内置技能：随安装包发布（PyInstaller datas 打包仓库 skills/ 目录），
# 首次启动释放到用户级技能目录，之后用户可自由开关 / 修改 / 删除。
# ------------------------------------------------------------------
_BUILTIN_SYNC_DONE = False
_builtin_sync_lock = threading.Lock()


def _builtin_skills_dir():
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "skills")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def _builtin_released_file():
    return os.path.join(config.YCODE_DIR, "builtin_skills_released.json")


def _builtin_skills_index_file():
    return os.path.join(config.YCODE_DIR, "builtin_skills_index.json")


# 指纹口径版本：算法或跳过规则发生任何变化都必须递增。递增后由
# _migrate_builtin_index_if_needed 在启动同步里做一次性重建（保守：只有内容与
# 内置源完全一致才写回索引），避免"旧指纹 vs 新指纹"永久不匹配。
_FINGERPRINT_ALGO = 2


def _builtin_skills_index_version_file():
    return os.path.join(config.YCODE_DIR, "builtin_skills_index.version")


_SKIP_DIR_SEGMENTS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv",
    "dist", "build", ".next", ".pytest_cache",
})
_SKIP_FILE_SUFFIXES = (".pyc",)

# 进程内指纹缓存：key = normcase(abspath(skill_dir))，value = (指纹, 失效签名)。
# 命中要求失效签名完全一致（见 _skill_dir_signature），因此"目录内容被修改 / 新增 /
# 删除文件"后必然重算。锁只保护字典读写，指纹计算放在锁外。
_fingerprint_cache = {}
_fingerprint_cache_lock = threading.Lock()


def _read_builtin_index_algo_version():
    try:
        with open(_builtin_skills_index_version_file(), "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_builtin_index_algo_version(version):
    try:
        with open(_builtin_skills_index_version_file(), "w", encoding="utf-8") as f:
            f.write(str(int(version)))
    except OSError as e:
        _log_warning("写入内置技能索引口径版本失败: %s", e)


def _iter_skill_entries(base_dir, rel_prefix=""):
    try:
        with os.scandir(base_dir) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
    except OSError as e:
        _log_warning("遍历技能目录失败 %s: %s", base_dir, e)
        return
    for entry in ordered:
        name = entry.name
        try:
            is_dir = entry.is_dir(follow_symlinks=True)
        except OSError as e:
            _log_warning("读取技能条目信息失败 %s: %s", entry.path, e)
            continue
        if is_dir:
            if name in _SKIP_DIR_SEGMENTS:
                continue
        elif name.endswith(_SKIP_FILE_SUFFIXES):
            continue
        rel = f"{rel_prefix}/{name}" if rel_prefix else name
        yield rel, entry, is_dir
        if is_dir and not entry.is_symlink():
            yield from _iter_skill_entries(entry.path, rel)


def _skill_dir_signature(skill_dir):
    if not os.path.isdir(skill_dir):
        return ""
    digest = hashlib.sha256()
    for rel, entry, is_dir in _iter_skill_entries(skill_dir):
        try:
            stat = entry.stat(follow_symlinks=True)
            mtime_ns, size = stat.st_mtime_ns, stat.st_size
        except OSError as e:
            _log_warning("读取技能条目信息失败 %s: %s", entry.path, e)
            mtime_ns, size = -1, -1
        digest.update(f"{rel}\0{int(is_dir)}\0{mtime_ns}\0{size}\0".encode("utf-8"))
    return digest.hexdigest()


def _invalidate_fingerprint_cache(skill_dir=None):
    with _fingerprint_cache_lock:
        if skill_dir is None:
            _fingerprint_cache.clear()
            return
        try:
            key = os.path.normcase(os.path.abspath(skill_dir))
        except OSError:
            return
        _fingerprint_cache.pop(key, None)


def _skill_dir_fingerprint_uncached(skill_dir):
    if not os.path.isdir(skill_dir):
        return ""
    digest = hashlib.sha256()
    for rel, entry, is_dir in _iter_skill_entries(skill_dir):
        if is_dir:
            continue
        try:
            data = pathlib.Path(entry.path).read_bytes()
        except OSError as e:
            _log_warning("读取技能文件失败 %s: %s", entry.path, e)
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _skill_dir_fingerprint(skill_dir):
    if not skill_dir:
        return ""
    try:
        cache_key = os.path.normcase(os.path.abspath(skill_dir))
    except OSError as e:
        _log_warning("规范化技能目录失败 %s: %s", skill_dir, e)
        return _skill_dir_fingerprint_uncached(skill_dir)
    signature = _skill_dir_signature(skill_dir)
    with _fingerprint_cache_lock:
        cached = _fingerprint_cache.get(cache_key)
    if cached is not None and cached[1] and cached[1] == signature:
        return cached[0]
    fingerprint = _skill_dir_fingerprint_uncached(skill_dir)
    if fingerprint:
        with _fingerprint_cache_lock:
            _fingerprint_cache[cache_key] = (fingerprint, signature)
    return fingerprint


def load_builtin_skill_index():
    """读取内置技能内容索引为 dict[str, str]。

    文件不存在 / 解析失败 / 内容不是对象 / 值类型不符，一律返回 {}，绝不抛异常
    （来源标注属于展示信息，不应影响技能扫描主流程）。
    """
    try:
        data = config._read_json_with_backup(_builtin_skills_index_file(), default=None)
    except Exception as e:
        _log_warning("读取内置技能索引失败: %s", e)
        return {}
    if not isinstance(data, dict):
        return {}
    index = {}
    for name, fingerprint in data.items():
        if isinstance(name, str) and isinstance(fingerprint, str):
            index[name] = fingerprint
    return index


def classify_skill_source(skill_dir, index=None):
    if not skill_dir:
        return "user"
    try:
        target = os.path.normcase(os.path.abspath(skill_dir))
    except OSError as e:
        _log_warning("规范化技能目录失败 %s: %s", skill_dir, e)
        return "user"
    try:
        builtin_root = os.path.normcase(os.path.abspath(_builtin_skills_dir()))
    except OSError as e:
        _log_warning("规范化内置技能目录失败: %s", e)
        builtin_root = ""
    if builtin_root and (target == builtin_root or target.startswith(builtin_root + os.sep)):
        return "builtin"
    if index is None:
        index = load_builtin_skill_index()
    if not isinstance(index, dict):
        return "user"
    recorded = index.get(os.path.basename(os.path.normpath(str(skill_dir))))
    if not recorded:
        return "user"
    current = _skill_dir_fingerprint(skill_dir)
    return "builtin" if current and current == recorded else "user"


def skill_source_label(skill):
    if not isinstance(skill, dict):
        return "用户"
    return "内置" if skill.get("source") == "builtin" else "用户"


def _update_builtin_skill_index(src, dest_base, released):
    try:
        index = load_builtin_skill_index()
        changed = False
        for name in released:
            if not isinstance(name, str) or not name or name in index:
                continue
            src_dir = os.path.join(src, name)
            dest_dir = os.path.join(dest_base, name)
            if not os.path.isdir(src_dir) or not os.path.isdir(dest_dir):
                continue
            src_fp = _skill_dir_fingerprint(src_dir)
            if src_fp and src_fp == _skill_dir_fingerprint(dest_dir):
                index[name] = src_fp
                changed = True
        if changed:
            config._atomic_write_json(_builtin_skills_index_file(), index, indent=None)
    except Exception as e:
        _log_warning("补齐内置技能索引失败: %s", e)


def _migrate_builtin_index_if_needed(index, released, src, dest_base):
    if _read_builtin_index_algo_version() == _FINGERPRINT_ALGO:
        return False
    changed = False
    for name in released:
        if not isinstance(name, str) or not name:
            continue
        src_dir = os.path.join(src, name)
        dest_dir = os.path.join(dest_base, name)
        if not os.path.isdir(src_dir) or not os.path.isdir(dest_dir):
            continue
        src_fp = _skill_dir_fingerprint(src_dir)
        if not src_fp:
            continue
        dest_fp = _skill_dir_fingerprint(dest_dir)
        if not dest_fp or dest_fp != src_fp:
            continue
        if index.get(name) != src_fp:
            index[name] = src_fp
            changed = True
    try:
        if changed:
            # 落盘 + 原子写会清掉 config 的 mtime 读缓存，保证随后读取索引的
            # _update_builtin_skill_index 看到的是迁移后的数据而不是旧文件
            config._atomic_write_json(_builtin_skills_index_file(), index, indent=None)
        _write_builtin_index_algo_version(_FINGERPRINT_ALGO)
    except Exception as e:
        _log_warning("重建内置技能索引失败: %s", e)
    return changed


def get_released_builtin_names():
    """读取"已释放过的内置技能名"集合。

    文件缺失 / 解析失败 / 内容不是列表，一律返回空集合，绝不抛异常
    （调用方多为展示或恢复流程，不应因记录文件异常而中断）。
    只保留非空字符串名，过滤脏数据。
    """
    try:
        data = config._read_json_with_backup(_builtin_released_file(), default=None)
    except Exception as e:
        _log_warning("读取内置技能释放记录失败: %s", e)
        return set()
    if not isinstance(data, list):
        return set()
    return {name for name in data if isinstance(name, str) and name}


def _replace_skill_dir(src_dir, dest_dir):
    tmp_dir = dest_dir + ".replacing"
    try:
        parent = os.path.dirname(dest_dir)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.copytree(src_dir, tmp_dir)
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir)
        os.replace(tmp_dir, dest_dir)
    except Exception as e:
        _log_warning("替换技能目录失败 %s -> %s: %s", src_dir, dest_dir, e)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False
    # 整目录替换后必须清掉目标目录的指纹缓存：copytree 会保留源文件 mtime，
    # 替换后的失效签名存在与替换前旧签名撞车的可能，届时缓存会返回过期指纹
    _invalidate_fingerprint_cache(dest_dir)
    return True


def _upgrade_released_builtin_skill(src_dir, dest_base, name, index):
    dest_dir = os.path.join(dest_base, name)
    if not os.path.isdir(dest_dir):
        return False
    recorded = index.get(name)
    if not recorded:
        return False
    dest_fp = _skill_dir_fingerprint(dest_dir)
    if not dest_fp or dest_fp != recorded:
        # 内容与释放时不一致（用户改过）或指纹计算失败：绝不覆盖
        return False
    src_fp = _skill_dir_fingerprint(src_dir)
    if not src_fp or src_fp == dest_fp:
        return False
    if not _replace_skill_dir(src_dir, dest_dir):
        return False
    index[name] = src_fp
    return True


def restore_builtin_skill(name):
    if not isinstance(name, str) or not name:
        return {"ok": False, "msg": "内置源中不存在该技能"}
    src_dir = os.path.join(_builtin_skills_dir(), name)
    if not os.path.isdir(src_dir):
        return {"ok": False, "msg": "内置源中不存在该技能"}
    dest_dir = os.path.join(config.YCODE_DIR, "skills", name)
    if not _replace_skill_dir(src_dir, dest_dir):
        return {"ok": False, "msg": "恢复内置版本失败"}
    try:
        index = load_builtin_skill_index()
        src_fp = _skill_dir_fingerprint(src_dir)
        if src_fp:
            index[name] = src_fp
            config._atomic_write_json(_builtin_skills_index_file(), index, indent=None)
            _write_builtin_index_algo_version(_FINGERPRINT_ALGO)
    except Exception as e:
        _log_warning("更新内置技能索引失败 %s: %s", name, e)
    return {"ok": True, "msg": "已恢复内置版本"}


def sync_builtin_skills():
    global _BUILTIN_SYNC_DONE
    if _BUILTIN_SYNC_DONE:
        return
    with _builtin_sync_lock:
        if _BUILTIN_SYNC_DONE:
            return
        _BUILTIN_SYNC_DONE = True
        _sync_builtin_skills_impl()


_BUILTIN_SKILLS_DEFAULT_DISABLED = {"tokenrhythm-image", "tokenrhythm-account", "tokenrhythm-image-api"}

def _sync_builtin_skills_impl():
    src = _builtin_skills_dir()
    if not os.path.isdir(src):
        return
    dest_base = os.path.join(config.YCODE_DIR, "skills")
    try:
        released = config._read_json_with_backup(_builtin_released_file(), default=[])
    except Exception:
        released = []
    if not isinstance(released, list):
        released = []
    newly_installed = []
    upgraded = []
    released_changed = False
    index = load_builtin_skill_index()
    _migrate_builtin_index_if_needed(index, released, src, dest_base)
    try:
        for entry in sorted(os.listdir(src)):
            src_dir = os.path.join(src, entry)
            if not os.path.isdir(src_dir):
                continue
            if entry in released:
                if _upgrade_released_builtin_skill(src_dir, dest_base, entry, index):
                    upgraded.append(entry)
                continue
            skill_md = None
            for fname in ("SKILL.md", "skill.md"):
                if os.path.isfile(os.path.join(src_dir, fname)):
                    skill_md = fname
                    break
            if not skill_md:
                continue
            dest_dir = os.path.join(dest_base, entry)
            if os.path.exists(dest_dir):
                released.append(entry)
                released_changed = True
                continue
            try:
                os.makedirs(dest_base, exist_ok=True)
                shutil.copytree(src_dir, dest_dir)
                released.append(entry)
                released_changed = True
                newly_installed.append(os.path.abspath(os.path.join(dest_dir, skill_md)))
            except Exception as e:
                _log_warning("释放内置技能失败 %s -> %s: %s", src_dir, dest_dir, e)
    except OSError as e:
        _log_warning("读取内置技能目录失败 %s: %s", src, e)
        return
    if released_changed:
        try:
            config._atomic_write_json(_builtin_released_file(), released, indent=None)
        except Exception as e:
            _log_warning("保存内置技能释放记录失败: %s", e)
    if upgraded:
        # 先把升级后的新指纹落盘，再走"只补缺失"的索引补齐，避免旧指纹被写回
        try:
            config._atomic_write_json(_builtin_skills_index_file(), index, indent=None)
        except Exception as e:
            _log_warning("更新内置技能索引失败: %s", e)
    _update_builtin_skill_index(src, dest_base, released)
    if newly_installed:
        newly_installed = [
            p for p in newly_installed
            if os.path.basename(os.path.dirname(p)) not in _BUILTIN_SKILLS_DEFAULT_DISABLED
        ]
    if newly_installed:
        active_ids = get_active_skill_ids()
        added = [sid for sid in newly_installed if sid not in active_ids]
        if added:
            save_active_skill_ids(active_ids + added)


def _log_warning(message, *args):
    try:
        logging.getLogger("py.warnings").warning(message, *args)
    except Exception:
        pass

def _expand_path(p):
    if not isinstance(p, str) or not p.strip():
        return None
    p = os.path.expandvars(os.path.expanduser(p.strip()))
    if not os.path.isabs(p):
        p = os.path.join(get_workdir(), p)
    return os.path.normpath(p)


def get_user_skill_search_paths():
    try:
        data = config._read_json_with_backup(config._CONFIG_FILE, default=None)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("skill_search_paths")
    if not isinstance(raw, list):
        return []
    result = []
    for item in raw:
        path = _expand_path(item)
        if path and os.path.isdir(path) and path not in result:
            result.append(path)
    return result


def get_skill_dirs():
    sync_builtin_skills()
    dirs = []
    seen = set()
    user_home = os.path.expanduser("~")

    def _add(p):
        if not p or not os.path.exists(p):
            return
        norm = os.path.normcase(os.path.abspath(p))
        if norm in seen:
            return
        seen.add(norm)
        dirs.append(p)

    global_skills = os.path.join(config.YCODE_DIR, "skills")
    if not os.path.exists(global_skills):
        try:
            os.makedirs(global_skills, exist_ok=True)
        except Exception as e:
            _log_warning("创建全局技能目录失败 %s: %s", global_skills, e)
    _add(global_skills)

    if os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() != "server":
        _add(os.path.join(user_home, ".agents", "skills"))

    for p in get_user_skill_search_paths():
        _add(p)

    return dirs

def is_skill_path(file_path):
    if not file_path:
        return False
    try:
        p = str(file_path).strip()
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.join(get_workdir(), p)
        candidate = os.path.normcase(os.path.abspath(p))
    except Exception:
        return False
    for base_dir in get_skill_dirs():
        base_norm = os.path.normcase(os.path.abspath(base_dir))
        if candidate == base_norm or candidate.startswith(base_norm + os.sep):
            return True
    return False


def is_trusted_read_path(file_path):
    if not file_path:
        return False
    try:
        candidate = str(file_path).strip()
        if not candidate:
            return False
        candidate = os.path.expandvars(candidate)
        candidate = os.path.expanduser(candidate)
        if not os.path.isabs(candidate):
            candidate = os.path.join(get_workdir(), candidate)
        candidate = os.path.normcase(os.path.realpath(os.path.abspath(candidate)))
        if not os.path.exists(candidate):
            return False
        for search_root in get_skill_dirs():
            root = os.path.normcase(os.path.realpath(os.path.abspath(search_root)))
            if candidate == root or not candidate.startswith(root + os.sep):
                continue
            relative = os.path.relpath(candidate, root)
            package_name = relative.split(os.sep, 1)[0]
            package = os.path.normcase(os.path.realpath(os.path.join(root, package_name)))
            if not package.startswith(root + os.sep) or not os.path.isdir(package):
                continue
            has_skill_file = any(
                os.path.isfile(skill_file)
                and os.path.normcase(os.path.realpath(skill_file)).startswith(package + os.sep)
                for skill_file in (
                    os.path.join(package, "SKILL.md"),
                    os.path.join(package, "skill.md"),
                )
            )
            if has_skill_file and (candidate == package or candidate.startswith(package + os.sep)):
                return True
    except Exception:
        return False
    return False


is_trusted_path = is_trusted_read_path

def parse_skill_md(file_path):
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        name = os.path.basename(os.path.dirname(file_path))
        description = "无描述"
        
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
        if match:
            yaml_content = match.group(1)
            markdown_body = match.group(2).strip()
            
            triggers = ""
            for line in yaml_content.split('\n'):
                line = line.strip()
                if line.startswith("name:"):
                    name = line[5:].strip().strip('"\'')
                elif line.startswith("description:"):
                    description = line[12:].strip().strip('"\'')
                elif line.startswith("triggers:"):
                    triggers = line[9:].strip().strip('"\'')
                    
            return {
                "id": file_path,
                "name": name,
                "description": description,
                "triggers": triggers,
                "content": content,
                "body": markdown_body
            }
        else:
            return {
                "id": file_path,
                "name": name,
                "description": "未解析到 YAML 描述",
                "triggers": "",
                "content": content,
                "body": content.strip()
            }
    except Exception as e:
        _log_warning("解析技能文件失败 %s: %s", file_path, e)
        return None

def scan_all_skills():
    skills = []
    seen_ids = set()
    # 先取基础目录：get_skill_dirs() 首次调用会释放内置技能并补齐内容索引，
    # 索引读取必须在其之后，否则进程内首扫会读到空索引而把内置技能误标为用户。
    base_dirs = get_skill_dirs()
    # 内置技能内容索引只读一次，避免逐个技能重复读盘
    builtin_index = load_builtin_skill_index()

    for base_dir in base_dirs:
        if not os.path.exists(base_dir):
            continue
        base_real = os.path.normcase(os.path.realpath(os.path.abspath(base_dir)))

        try:
            entries = os.listdir(base_dir)
        except OSError as e:
            _log_warning("扫描技能目录失败 %s: %s", base_dir, e)
            continue

        for entry in entries:
            skill_dir = os.path.join(base_dir, entry)
            if not os.path.isdir(skill_dir):
                continue
            skill_real = os.path.normcase(os.path.realpath(os.path.abspath(skill_dir)))
            if not skill_real.startswith(base_real + os.sep):
                _log_warning("跳过扫描根外的技能目录: %s", skill_dir)
                continue
            # 优先匹配 SKILL.md，其次 skill.md（大小写兼容）
            for fname in ("SKILL.md", "skill.md"):
                candidate = os.path.join(skill_dir, fname)
                if os.path.isfile(candidate):
                    file_path = os.path.realpath(os.path.abspath(candidate))
                    norm_id = os.path.normcase(file_path)
                    if not norm_id.startswith(skill_real + os.sep):
                        _log_warning("跳过技能包外的 SKILL.md: %s", candidate)
                        break
                    if norm_id in seen_ids:
                        break
                    seen_ids.add(norm_id)
                    skill_info = parse_skill_md(file_path)
                    if skill_info:
                        skill_info["source"] = classify_skill_source(skill_dir, builtin_index)
                        skills.append(skill_info)
                    break

    return skills

def delete_skill(skill_id):
    if not isinstance(skill_id, str) or not skill_id.strip():
        return {"ok": False, "msg": "技能标识无效"}
    try:
        skill_file = os.path.realpath(os.path.abspath(skill_id.strip()))
    except Exception:
        return {"ok": False, "msg": "技能标识无效"}
    if not os.path.isfile(skill_file):
        return {"ok": False, "msg": "技能文件不存在"}
    if os.path.basename(skill_file).lower() != "skill.md":
        return {"ok": False, "msg": "只能删除技能目录"}
    skill_dir = os.path.dirname(skill_file)
    if os.path.islink(skill_dir):
        return {"ok": False, "msg": "不能删除符号链接技能目录"}
    skill_dir_real = os.path.normcase(os.path.realpath(skill_dir))
    if not os.path.normcase(skill_file).startswith(skill_dir_real + os.sep):
        return {"ok": False, "msg": "技能文件不在技能目录内"}

    allowed = False
    for base in get_skill_dirs():
        try:
            base_real = os.path.normcase(os.path.realpath(os.path.abspath(base)))
        except Exception:
            continue
        if os.path.dirname(skill_dir_real) == base_real and skill_dir_real.startswith(base_real + os.sep):
            allowed = True
            break
    if not allowed:
        return {"ok": False, "msg": "该技能不在可管理的技能目录中，已拒绝删除"}

    try:
        builtin_root = os.path.normcase(os.path.realpath(os.path.abspath(_builtin_skills_dir())))
    except Exception:
        builtin_root = ""
    if builtin_root and (skill_dir_real == builtin_root or skill_dir_real.startswith(builtin_root + os.sep)):
        return {"ok": False, "msg": "不能删除安装包内的内置技能源"}

    try:
        shutil.rmtree(skill_dir)
    except OSError as e:
        return {"ok": False, "msg": f"删除技能目录失败: {e}"}
    _invalidate_fingerprint_cache(skill_dir)
    _drop_active_skill_id(skill_file)
    return {"ok": True, "msg": "已删除技能"}


def _drop_active_skill_id(skill_file):
    try:
        target = os.path.normcase(os.path.realpath(os.path.abspath(skill_file)))
    except Exception:
        return
    kept = []
    changed = False
    for sid in get_active_skill_ids():
        if not isinstance(sid, str):
            changed = True
            continue
        try:
            current = os.path.normcase(os.path.realpath(os.path.abspath(sid)))
        except Exception:
            current = os.path.normcase(os.path.abspath(sid))
        if current == target:
            changed = True
            continue
        kept.append(sid)
    if changed:
        save_active_skill_ids(kept)


def get_active_skill_ids():
    # 读失败时优先从 .bak 备份恢复，避免激活技能无声丢失
    data = config._read_json_with_backup(ACTIVE_SKILLS_FILE, default=None)
    if isinstance(data, list):
        return data
    if data is not None:
        _log_warning("active_skills.json 内容不是列表，已忽略: %r", type(data))
    return []

def save_active_skill_ids(active_ids):
    # 临时文件 + os.replace 原子写，防止写一半崩溃导致激活技能丢失
    try:
        with _file_lock:
            config._atomic_write_json(ACTIVE_SKILLS_FILE, active_ids, indent=None)
    except Exception as e:
        _log_warning("保存 active_skills.json 失败: %s", e)

_TRIGGER_HINTS = {
    "brainstorming": "新功能/设计/改造前",
    "writing-plans": "多步任务/写实施计划",
    "executing-plans": "按计划执行",
    "subagent-driven-development": "多独立任务并行开发",
    "dispatching-parallel-agents": "多个独立任务并行",
    "test-driven-development": "新功能/修bug/写测试",
    "systematic-debugging": "排查/报错/异常/bug",
    "verification-before-completion": "声称完成前验证",
    "requesting-code-review": "任务完成后审查代码",
    "receiving-code-review": "收到评审意见",
    "using-git-worktrees": "隔离工作区/新分支开发",
    "finishing-a-development-branch": "合并分支/收尾",
    "writing-skills": "创建/修改技能",
    "using-superpowers": "会话开始/查找技能",
}


def _contains_cjk(text):
    return any('\u4e00' <= ch <= '\u9fff' for ch in (text or ""))


def _bounded_skill_metadata(value, limit):
    """Flatten controls/newlines so metadata cannot forge catalog structure."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def get_active_skills_catalog():
    active_ids = set(get_active_skill_ids())
    if not active_ids:
        return ""

    all_skills = scan_all_skills()
    active_skills = [s for s in all_skills if s["id"] in active_ids]

    if not active_skills:
        return ""

    lines = [
        "【可用技能目录】开始任何工作前先扫一遍本目录：任务与某技能哪怕轻微相关，"
        "必须先 read_file 读取其 SKILL.md 再动手；确定无关才跳过。",
    ]
    for skill in sorted(
        active_skills,
        key=lambda item: (item["name"].lower(), item["id"]),
    ):
        name = _bounded_skill_metadata(skill.get("name"), SKILL_NAME_MAX_CHARS)
        description = _bounded_skill_metadata(
            skill.get("description"), SKILL_DESCRIPTION_MAX_CHARS
        )
        hint = _bounded_skill_metadata(
            skill.get("triggers"), SKILL_TRIGGERS_MAX_CHARS
        )
        if not hint and not _contains_cjk(description):
            dir_name = os.path.basename(os.path.dirname(skill["id"]))
            hint = _TRIGGER_HINTS.get(dir_name, "")
        suffix = f"（触发:{hint}）" if hint else ""
        lines.append(
            f'- {name}: {description}{suffix} | {skill["id"]}'
        )
    return "\n".join(lines)


def get_active_skills_markdown():
    """Compatibility alias for callers migrated from eager skill injection."""
    return get_active_skills_catalog()

def get_active_skills_list():
    """返回当前所有已启用技能的字典映射 {short_id: skill_info}"""
    active_ids = set(get_active_skill_ids())
    if not active_ids:
        return {}
        
    all_skills = scan_all_skills()
    mapping = {}
    for s in all_skills:
        if s["id"] in active_ids:
            short_id = os.path.basename(os.path.dirname(s["id"])).lower()
            # 同名短 ID 冲突：扫描顺序为用户级在前、项目级在后，后者覆盖前者（项目 > 用户），
            # 覆盖前写一条告警日志，绝不静默覆盖。
            if short_id in mapping:
                _log_warning(
                    "技能短 ID 冲突 '%s'：%s 被 %s 覆盖（项目级优先）",
                    short_id, mapping[short_id]["id"], s["id"],
                )
            mapping[short_id] = s
    return mapping
