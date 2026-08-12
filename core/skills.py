import os
import re
import sys
import json
import glob
import shutil
import logging
import threading

import config
# 使用一个独立的文件来记录当前启用的技能列表
ACTIVE_SKILLS_FILE = config.ACTIVE_SKILLS_FILE

# 激活技能文件写锁，防止并发写互相截断
_file_lock = threading.Lock()


# ------------------------------------------------------------------
# 内置技能：随安装包发布（PyInstaller datas 打包仓库 skills/ 目录），
# 首次启动释放到用户级技能目录，之后用户可自由开关 / 修改 / 删除。
# ------------------------------------------------------------------
_BUILTIN_SYNC_DONE = False


def _builtin_skills_dir():
    """内置技能源目录：打包后位于 _MEIPASS/skills，开发态为仓库根 skills/。"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "skills")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills")


def _builtin_released_file():
    """记录已释放过的内置技能名：区分"从未释放"与"用户已删除"，防止重启后复活。"""
    return os.path.join(os.path.expanduser("~"), ".ycode", "builtin_skills_released.json")


def sync_builtin_skills():
    """把内置技能释放到用户级技能目录（仅缺失时拷贝，绝不覆盖用户改动）。

    - 首次释放的技能默认加入 active_skills.json（默认启用）；
    - 用户在技能页关闭后，开关状态持久化，不会被重置；
    - 用户删除技能目录后不会被重新拷贝（通过 builtin_skills_released.json 记录
      "已释放过"，删除被视为用户主动移除；代价是后续版本的内置技能内容更新
      不会自动覆盖到已释放过的技能，保持简单可预期）。
    由 get_skill_dirs() 首次调用时自动触发一次（TUI/GUI 共用，无需各入口手动挂钩）。
    """
    global _BUILTIN_SYNC_DONE
    if _BUILTIN_SYNC_DONE:
        return
    _BUILTIN_SYNC_DONE = True
    src = _builtin_skills_dir()
    if not os.path.isdir(src):
        return
    dest_base = os.path.join(os.path.expanduser("~"), ".ycode", "skills")
    try:
        released = config._read_json_with_backup(_builtin_released_file(), default=[])
    except Exception:
        released = []
    if not isinstance(released, list):
        released = []
    newly_installed = []
    released_changed = False
    try:
        for entry in sorted(os.listdir(src)):
            if entry in released:
                continue  # 释放过（含已被用户删除的），跳过
            src_dir = os.path.join(src, entry)
            if not os.path.isdir(src_dir):
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
    if newly_installed:
        # 新释放的技能默认启用；只在 active_skills.json 里追加，不动用户既有选择
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
    """展开 ~ 与环境变量，相对路径相对 cwd 解析。返回展开后的路径（不强制存在）。"""
    if not isinstance(p, str) or not p.strip():
        return None
    p = os.path.expandvars(os.path.expanduser(p.strip()))
    if not os.path.isabs(p):
        p = os.path.join(os.getcwd(), p)
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
    global_skills = os.path.join(user_home, ".ycode", "skills")
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
            p = os.path.join(os.getcwd(), p)
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
            candidate = os.path.join(os.getcwd(), candidate)
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
        # 诊断走 runtime warnings 日志：后台扫描路径不允许裸 print 污染 TUI
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

    for base_dir in get_skill_dirs():
        if not os.path.exists(base_dir):
            continue

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
            # 优先匹配 SKILL.md，其次 skill.md（大小写兼容）
            for fname in ("SKILL.md", "skill.md"):
                candidate = os.path.join(skill_dir, fname)
                if os.path.isfile(candidate):
                    file_path = os.path.abspath(candidate)
                    norm_id = os.path.normcase(file_path)
                    if norm_id in seen_ids:
                        break
                    seen_ids.add(norm_id)
                    skill_info = parse_skill_md(file_path)
                    if skill_info:
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
        hint = skill.get("triggers") or ""
        if not hint and not _contains_cjk(skill["description"]):
            dir_name = os.path.basename(os.path.dirname(skill["id"]))
            hint = _TRIGGER_HINTS.get(dir_name, "")
        suffix = f"（触发:{hint}）" if hint else ""
        lines.append(
            f'- {skill["name"]}: {skill["description"]}{suffix} | {skill["id"]}'
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
