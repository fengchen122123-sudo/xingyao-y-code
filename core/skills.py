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

def get_skill_dirs():
    """返回所有可能包含 SKILL.md 的基础目录（只扫描已存在目录，不主动创建项目级目录以免污染任意工作目录）"""
    dirs = []
    # 用户级全局目录（属于 Y-code 自己的数据目录，允许自动创建）
    user_home = os.path.expanduser("~")
    global_skills = os.path.join(user_home, ".ycode", "skills")
    if not os.path.exists(global_skills):
        try:
            os.makedirs(global_skills, exist_ok=True)
        except Exception as e:
            _log_warning("创建全局技能目录失败 %s: %s", global_skills, e)
    if os.path.exists(global_skills):
        dirs.append(global_skills)

    # 兼容用户级全局 .agents 目录
    global_agents = os.path.join(user_home, ".agents", "skills")
    if os.path.exists(global_agents):
        dirs.append(global_agents)

    # 项目级局部目录：仅在已存在时扫描，绝不在任意 cwd 下自动创建
    local_skills = os.path.join(os.getcwd(), ".agents", "skills")
    if os.path.exists(local_skills):
        dirs.append(local_skills)

    return dirs

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
    """扫描目录下的所有 SKILL.md 或 skill.md"""
    skills = []
    for base_dir in get_skill_dirs():
        if not os.path.exists(base_dir):
            continue
            
        # 递归扫描
        for root, dirs, files in os.walk(base_dir):
            for file in files:
                if file.lower() == "skill.md":
                    file_path = os.path.abspath(os.path.join(root, file))
                    skill_info = parse_skill_md(file_path)
                    if skill_info:
                        skills.append(skill_info)
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
