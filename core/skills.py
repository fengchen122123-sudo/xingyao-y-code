import os
import re
import json
import glob
import logging
import threading

import config
# 使用一个独立的文件来记录当前启用的技能列表
ACTIVE_SKILLS_FILE = config.ACTIVE_SKILLS_FILE

# 激活技能文件写锁，防止并发写互相截断
_file_lock = threading.Lock()


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
            
            # 简单解析 yaml 里的 name 和 description
            for line in yaml_content.split('\n'):
                line = line.strip()
                if line.startswith("name:"):
                    name = line[5:].strip().strip('"\'')
                elif line.startswith("description:"):
                    description = line[12:].strip().strip('"\'')
                    
            return {
                "id": file_path, # 用绝对路径作为唯一标识
                "name": name,
                "description": description,
                "content": content, # 把带有 header 的完整文件也发给模型，有时有帮助
                "body": markdown_body
            }
        else:
            # 如果没有 yaml 头部，整个作为 body
            return {
                "id": file_path,
                "name": name,
                "description": "未解析到 YAML 描述",
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
        "【可用技能目录】",
        "仅在任务直接匹配时读取对应 SKILL.md；不要加载无关技能。",
    ]
    for skill in sorted(
        active_skills,
        key=lambda item: (item["name"].lower(), item["id"]),
    ):
        lines.append(
            f'- {skill["name"]}: {skill["description"]} | {skill["id"]}'
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
