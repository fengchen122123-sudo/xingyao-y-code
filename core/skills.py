import os
import re
import json
import glob

import config
# 使用一个独立的文件来记录当前启用的技能列表
ACTIVE_SKILLS_FILE = config.ACTIVE_SKILLS_FILE

def get_skill_dirs():
    """返回所有可能包含 SKILL.md 的基础目录"""
    dirs = []
    # 用户级全局目录
    user_home = os.path.expanduser("~")
    global_skills = os.path.join(user_home, ".ycode", "skills")
    if not os.path.exists(global_skills):
        try:
            os.makedirs(global_skills, exist_ok=True)
        except:
            pass
    dirs.append(global_skills)
    
    # 兼容用户级全局 .agents 目录
    global_agents = os.path.join(user_home, ".agents", "skills")
    if os.path.exists(global_agents):
        dirs.append(global_agents)
    
    # 项目级局部目录
    local_skills = os.path.join(os.getcwd(), ".agents", "skills")
    if not os.path.exists(local_skills):
        try:
            os.makedirs(local_skills, exist_ok=True)
        except:
            pass
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
        print(f"⚠️ 解析技能文件失败 {file_path}: {e}")
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
    if os.path.exists(ACTIVE_SKILLS_FILE):
        try:
            with open(ACTIVE_SKILLS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_active_skill_ids(active_ids):
    try:
        with open(ACTIVE_SKILLS_FILE, "w", encoding="utf-8") as f:
            json.dump(active_ids, f)
    except:
        pass

def get_active_skills_markdown():
    """返回所有已启用技能合并后的 Markdown 字符串，供 main.py 注入到系统提示词"""
    active_ids = set(get_active_skill_ids())
    if not active_ids:
        return ""
        
    all_skills = scan_all_skills()
    active_skills = [s for s in all_skills if s["id"] in active_ids]
    
    if not active_skills:
        return ""
        
    combined = "\n\n【启用附加技能 (Skills)】\n"
    combined += "以下是你被授予的附加技能指令，请在用户要求或适当的时机遵循这些指令：\n"
    
    for s in active_skills:
        skill_dir = os.path.dirname(s['id'])
        combined += f"\n--- 技能开始: {s['name']} ---\n"
        combined += f"【系统提示：该技能的物理绝对路径为 {skill_dir}，如果技能指令要求你运行其中的脚本，请去该路径下寻找】\n\n"
        combined += s['content'] + "\n"
        combined += f"--- 技能结束: {s['name']} ---\n"
        
    return combined

def get_active_skills_list():
    """返回当前所有已启用技能的字典映射 {short_id: skill_info}"""
    import os
    active_ids = set(get_active_skill_ids())
    if not active_ids:
        return {}
        
    all_skills = scan_all_skills()
    mapping = {}
    for s in all_skills:
        if s["id"] in active_ids:
            short_id = os.path.basename(os.path.dirname(s["id"])).lower()
            mapping[short_id] = s
    return mapping
