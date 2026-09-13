import os
import re
import sys
import json
import glob
import shutil
import pathlib
import hashlib
import logging
import threading
import config
from core.workspace_context import get_workdir
# 使用一个独立的文件来记录当前启用的技能列表
ACTIVE_SKILLS_FILE = config.ACTIVE_SKILLS_FILE

# 激活技能文件写锁，防止并发写互相截断
_file_lock = threading.Lock()

# Skill 元数据会进入稳定 system prompt；仅允许单行、有限长度的数据。
SKILL_NAME_MAX_CHARS = 120
SKILL_DESCRIPTION_MAX_CHARS = 500
SKILL_TRIGGERS_MAX_CHARS = 240


# ------------------------------------------------------------------
# 内置技能：随安装包发布（PyInstaller datas 打包仓库 skills/ 目录），
# 首次启动释放到用户级技能目录，之后用户可自由开关 / 修改 / 删除。
# ------------------------------------------------------------------
_BUILTIN_SYNC_DONE = False
# check-and-set 必须持锁：CLI/GUI 多线程首次调用 get_skill_dirs 时
# 无锁并发会让释放流程重入执行
_builtin_sync_lock = threading.Lock()


def _builtin_skills_dir():
    """内置技能源目录：打包后位于 _MEIPASS/skills，开发态为仓库根 skills/。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "skills")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def _builtin_released_file():
    """记录已释放过的内置技能名：区分"从未释放"与"用户已删除"，防止重启后复活。"""
    return os.path.join(config.YCODE_DIR, "builtin_skills_released.json")


def _builtin_skills_index_file():
    """内置技能内容索引文件路径：{"技能目录名": "内容指纹"}。

    内置技能释放后与用户自建技能物理同居 ~/.ycode/skills，无法靠路径区分来源，
    因此额外记录"真正由内置释放、且内容未被用户改动"的技能指纹，供 GUI/CLI 标注
    来源（内置 / 用户）。索引只增不改：已有记录不重算、不覆盖。
    """
    return os.path.join(config.YCODE_DIR, "builtin_skills_index.json")


def _skill_dir_fingerprint(skill_dir):
    """计算技能目录的内容指纹（sha256）：所有文件的相对路径 posix 形式 + 内容哈希。

    跳过路径中含 __pycache__ 的片段与 .pyc 文件（编译缓存不属于技能内容）。
    单个文件读取失败只告警并跳过，不抛异常：该函数运行在启动扫描路径上，
    不能因为个别文件权限问题中断整个技能扫描。失败时返回 ""（调用方按不匹配处理）。
    """
    digest = hashlib.sha256()
    try:
        paths = sorted(pathlib.Path(skill_dir).rglob("*"))
    except OSError as e:
        _log_warning("遍历技能目录失败 %s: %s", skill_dir, e)
        return ""
    for path in paths:
        try:
            if not path.is_file():
                continue
            rel = path.relative_to(skill_dir).as_posix()
        except (OSError, ValueError) as e:
            _log_warning("读取技能条目信息失败 %s: %s", path, e)
            continue
        if "__pycache__" in rel.split("/") or rel.endswith(".pyc"):
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            _log_warning("读取技能文件失败 %s: %s", path, e)
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


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
    """判定技能来源：返回 "builtin"（内置）或 "user"（用户）。

    判据为"内容指纹精确口径"，与内置插件 source 字段约定（"user" | "builtin"）一致：
      1. 目录直接位于内置源目录（_builtin_skills_dir()）之下 → builtin
         （直接扫描随包发布的 skills/ 目录的场景）；
      2. 否则取目录名命中索引，且该目录当前指纹与索引记录完全一致 → builtin
         （内置技能已被释放到用户目录且内容未被改动）；
      3. 其它情况（含目录名命中但内容已被用户改动）→ user。
    index 为 None 时自行加载索引；显式传入便于测试注入，避免重复读盘。
    """
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
    """把技能字典归一为中文来源标签（"内置" / "用户"），供 CLI 各展示入口复用。

    调用方：/skills list、技能快捷指令下拉、全屏技能选择器。
    source 缺失或取值非 "builtin" 一律回落"用户"：宁可保守，也不能把
    未知来源误标为内置。
    """
    if not isinstance(skill, dict):
        return "用户"
    return "内置" if skill.get("source") == "builtin" else "用户"


def _update_builtin_skill_index(src, dest_base, released):
    """为已释放的内置技能补齐内容索引（只补缺失项，不重算、不覆盖已有记录）。

    覆盖两类情况：本次全新释放的技能；老用户此前已释放、但索引文件尚不存在的技能。
    仅当用户目录中的技能内容与内置源目录指纹完全一致时才打标，用户改动过的技能
    保持 user 来源。写入失败只告警，不影响释放主流程。
    """
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
    """把 src_dir 整目录以"临时目录 + 原子替换"覆盖到 dest_dir。

    先 copytree 到 dest_dir + ".replacing"，再删旧目录并改名 —— 任一环节失败都不会
    留下半成品技能目录（最坏情况是保留 dest_dir 原有内容）。失败时清理临时目录并返回
    False，绝不抛出：跟随升级与恢复内置版本两条路径都不应因此中断。

    "跟随升级"与"恢复内置版本"共用此函数，避免两份替换实现分叉。
    dest_dir 不存在时会连同其父目录一起创建（恢复被删除的技能时需要）。
    """
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
    return True


def _upgrade_released_builtin_skill(src_dir, dest_base, name, index):
    """已释放的内置技能：内容未被用户改动过时，跟随新版内置内容升级。

    三个条件同时成立才覆盖，缺一不动作：
      1. 用户目录仍存在 —— 用户主动删除的技能不复活（保持既有语义）；
      2. 索引里记录了该技能释放时的内容指纹，且与用户目录当前指纹一致
         —— 等价于"用户从未改动过这个内置技能"，改动过的一律保留；
      3. 内置源指纹与用户目录指纹不同 —— 新版本确实有内容变化，避免无谓重写。

    覆盖复用 _replace_skill_dir（"临时目录 + 原子替换"），与"恢复内置版本"共用同一实现，
    任一环节失败都不会留下半成品技能目录（最坏情况是保留用户原有内容）。

    升级成功后把新指纹写回 index（内存），由调用方统一落盘。
    返回是否真的执行了升级。
    """
    dest_dir = os.path.join(dest_base, name)
    if not os.path.isdir(dest_dir):
        return False
    recorded = index.get(name)
    if not recorded:
        # 无释放时指纹（升级机制上线前释放的老技能）：无从判断是否被改动过，保守不动
        return False
    dest_fp = _skill_dir_fingerprint(dest_dir)
    if not dest_fp or dest_fp != recorded:
        # 内容与释放时不一致（用户改过）或指纹计算失败：绝不覆盖
        return False
    src_fp = _skill_dir_fingerprint(src_dir)
    if not src_fp or src_fp == dest_fp:
        # 内置内容没有变化，或读取失败：无需升级
        return False
    if not _replace_skill_dir(src_dir, dest_dir):
        return False
    index[name] = src_fp
    return True


def restore_builtin_skill(name):
    """把用户目录里的内置技能覆盖回随包发布的内容（技能页"恢复内置版本"按钮）。

    与"跟随升级"不同，这是用户显式发起的覆盖动作，不做"用户是否改过该技能"的判断：
    用户点恢复就是明确想要内置版本，因此目标目录被删过（用户之前删掉）也重新创建。

    步骤：
      1. 内置源目录下不存在该技能 → 直接失败（无源可恢复）；
      2. 以"临时目录 + 原子替换"整目录覆盖用户目录；
      3. 覆盖成功后把索引里该技能的指纹更新为内置源新指纹（无 key 则新增）并落盘，
         使来源标注重新回到"内置"。
    返回 {"ok": bool, "msg": str}，不抛异常（供 GUI 直接展示结果）。
    """
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
    except Exception as e:
        # 索引只影响来源标注，写失败不影响已完成的覆盖，只告警
        _log_warning("更新内置技能索引失败 %s: %s", name, e)
    return {"ok": True, "msg": "已恢复内置版本"}


def sync_builtin_skills():
    """把内置技能释放到用户级技能目录（仅缺失时拷贝，绝不覆盖用户改动）。

    - 首次释放的技能默认加入 active_skills.json（默认启用）；
    - 用户在技能页关闭后，开关状态持久化，不会被重置；
    - 用户删除技能目录后不会被重新拷贝（通过 builtin_skills_released.json 记录
      "已释放过"，删除被视为用户主动移除；代价是后续版本的内置技能内容更新
      不会自动覆盖到已释放过的技能，保持简单可预期）。
    由 get_skill_dirs() 首次调用时自动触发一次（CLI/GUI 共用，无需各入口手动挂钩）。
    """
    global _BUILTIN_SYNC_DONE
    if _BUILTIN_SYNC_DONE:
        return
    with _builtin_sync_lock:
        if _BUILTIN_SYNC_DONE:
            return
        _BUILTIN_SYNC_DONE = True
        _sync_builtin_skills_impl()


# 内置技能默认不启用名单：文件照常释放（released 记录 + copytree 均执行），
# 但首次释放后不自动写入 active_skills.json（默认关闭），用户可在技能页手动开启。
_BUILTIN_SKILLS_DEFAULT_DISABLED = {"tokenrhythm-image", "tokenrhythm-account", "tokenrhythm-image-api"}

def _sync_builtin_skills_impl():
    """sync_builtin_skills 的实际执行体（调用方已持 _builtin_sync_lock）。"""
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
    # 升级判据要读"释放时的内容指纹"，读取一次供整个循环复用
    index = load_builtin_skill_index()
    try:
        for entry in sorted(os.listdir(src)):
            src_dir = os.path.join(src, entry)
            if not os.path.isdir(src_dir):
                continue
            if entry in released:
                # 释放过（含已被用户删除的）：内容未被用户改动时跟随新版升级
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
                # 用户已有同名技能：不覆盖，仅标记为已处理（也不强制启用）
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
    # 补齐内置技能内容索引：覆盖本次新释放的技能，以及老用户已释放但索引缺失的技能
    _update_builtin_skill_index(src, dest_base, released)
    if newly_installed:
        # 新释放的技能默认启用；只在 active_skills.json 里追加，不动用户既有选择
        # 默认关闭名单内的技能（tokenrhythm-image / -account / -image-api）不追加，仅文件已释放
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
    """异常诊断写入 runtime warnings 日志，而不是裸吞。"""
    try:
        logging.getLogger("py.warnings").warning(message, *args)
    except Exception:
        pass

def _expand_path(p):
    """展开 ~ 与环境变量，相对路径相对当前线程工作目录解析。返回展开后的路径（不强制存在）。"""
    if not isinstance(p, str) or not p.strip():
        return None
    p = os.path.expandvars(os.path.expanduser(p.strip()))
    if not os.path.isabs(p):
        p = os.path.join(get_workdir(), p)
    return os.path.normpath(p)


def get_user_skill_search_paths():
    """读取 config.json 中用户自定义的 skill 搜索路径列表。

    配置键：skill_search_paths，类型为字符串数组。
    - 未配置或非列表：返回 []（回退到默认目录）
    - 每项支持 ~ 展开、环境变量、相对路径（相对 cwd）
    - 仅返回已存在目录，不主动创建（避免误把笔误路径建出来）
    """
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
    """返回所有可能包含 SKILL.md 的基础目录（只扫描已存在目录，不主动创建项目级目录以免污染任意工作目录）

    目录来源（按顺序，已按物理路径去重）：
      1. 默认用户级：~/.ycode/skills（不存在则自动创建）
      2. 默认兼容全局：~/.agents/skills（仅已存在时扫描）
      3. 用户自定义：config.json 的 skill_search_paths 列表（仅已存在时扫描）

    说明：项目级 <cwd>/.agents/skills 不再隐式扫描——避免在任意 cwd 启动时行为不一致，
    也消除 cwd 在用户主目录下时与全局 .agents/skills 重合导致重复扫描的隐患。
    如需扫描项目级目录，请在 config.json 的 skill_search_paths 中显式配置（如 "./.agents/skills"）。
    """
    # 首次扫描前释放随包内置技能（缺失才拷贝，不覆盖用户改动）
    sync_builtin_skills()
    dirs = []
    seen = set()  # 按规范化绝对路径去重，防止多个路径指向同一物理位置时重复扫描
    user_home = os.path.expanduser("~")

    def _add(p):
        if not p or not os.path.exists(p):
            return
        norm = os.path.normcase(os.path.abspath(p))
        if norm in seen:
            return
        seen.add(norm)
        dirs.append(p)

    # 1. 用户级全局目录（属于 Y-code 自己的数据目录，允许自动创建）
    global_skills = os.path.join(config.YCODE_DIR, "skills")
    if not os.path.exists(global_skills):
        try:
            os.makedirs(global_skills, exist_ok=True)
        except Exception as e:
            _log_warning("创建全局技能目录失败 %s: %s", global_skills, e)
    _add(global_skills)

    # 2. 兼容用户级全局 .agents 目录
    _add(os.path.join(user_home, ".agents", "skills"))

    # 3. 用户自定义目录（来自 config.json 的 skill_search_paths）
    for p in get_user_skill_search_paths():
        _add(p)

    return dirs

def is_skill_path(file_path):
    """判断给定路径是否落在已注册的 skill 扫描目录内。

    用于沙盒白名单：skill 目录是用户自行配置的受信任路径，
    读取其中的 SKILL.md 不应被工作目录沙盒拦截或反复询问。
    返回 True 时表示该路径可免沙盒校验。
    """
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
    """Return whether a path is readable without workspace sandbox confirmation.

    Trust is limited to configured Skill package directories. Y-CODE and agent
    configuration directories can contain credentials and mutable state, so they
    never receive a blanket trust exemption.
    """
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


# Compatibility name for callers that only need the trusted-read policy.
is_trusted_path = is_trusted_read_path

def parse_skill_md(file_path):
    """解析 SKILL.md，提取 YAML frontmatter 和 Markdown 正文"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        name = os.path.basename(os.path.dirname(file_path)) # 默认取文件夹名
        description = "无描述"
        
        # 尝试匹配 YAML frontmatter
        match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
        if match:
            yaml_content = match.group(1)
            markdown_body = match.group(2).strip()
            
            # 简单解析 yaml 里的 name、description 和可选的 triggers（中文触发词）
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
                "id": file_path, # 用绝对路径作为唯一标识
                "name": name,
                "description": description,
                "triggers": triggers,
                "content": content, # 把带有 header 的完整文件也发给模型，有时有帮助
                "body": markdown_body
            }
        else:
            # 如果没有 yaml 头部，整个作为 body
            return {
                "id": file_path,
                "name": name,
                "description": "未解析到 YAML 描述",
                "triggers": "",
                "content": content,
                "body": content.strip()
            }
    except Exception as e:
        # 诊断走 runtime warnings 日志：后台扫描路径不允许裸 print 污染 CLI
        _log_warning("解析技能文件失败 %s: %s", file_path, e)
        return None

def scan_all_skills():
    """扫描目录下的所有 SKILL.md 或 skill.md。

    只扫描 base_dir 的直接子目录（深度=1），避免嵌套 skill 目录被重复展示：
        base_dir/
          skill-a/SKILL.md   ← 扫描
          skill-a/sub/SKILL.md  ← 不扫描（双层）
    """
    skills = []
    seen_ids = set()  # 按绝对路径去重，防止多个扫描目录指向同一物理位置时重复加载
    # 先取基础目录：get_skill_dirs() 首次调用会释放内置技能并补齐内容索引，
    # 索引读取必须在其之后，否则进程内首扫会读到空索引而把内置技能误标为用户。
    base_dirs = get_skill_dirs()
    # 内置技能内容索引只读一次，避免逐个技能重复读盘
    builtin_index = load_builtin_skill_index()

    for base_dir in base_dirs:
        if not os.path.exists(base_dir):
            continue
        base_real = os.path.normcase(os.path.realpath(os.path.abspath(base_dir)))

        # 只遍历 base_dir 的直接子条目（不递归）
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
                        # 来源标注：内置（内容未被改动）或用户自建/已改动
                        skill_info["source"] = classify_skill_source(skill_dir, builtin_index)
                        skills.append(skill_info)
                    break  # 找到一个就不再找 skill.md

    return skills

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

# 英文描述技能的中文触发词提示：英文描述在中文业务对话里难以被模型命中，
# 仅对描述中不含中文的技能追加极简触发词（frontmatter 的 triggers: 可覆盖）。
# 提示只出现在跨轮稳定的技能目录块中（属缓存前缀），不增加每轮请求的边际成本。
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
    """Return enabled skill metadata without eagerly injecting skill bodies."""
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
        # 中文描述本身含触发语义，无需追加；英文描述追加极简中文触发词
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
