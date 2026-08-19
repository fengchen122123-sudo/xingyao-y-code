"""自定义子 Agent 注册中心。

扫描 ~/.ycode/agents/*.md（用户级）和项目级 .ycode/agents/*.md，
解析 frontmatter（name/description/when_to_use/model/allowed_tools/color），
Markdown 正文作为子代理系统提示词。

格式参考 Claude Code 的 .claude/agents/*.md，复用 skills.py 的简单 frontmatter 解析模式，
不引入 yaml 库依赖。

frontmatter 字段：
  - name: 子代理唯一标识（与内置 5 种 type 重名时跳过并警告）
  - description: 简短描述
  - when_to_use: 调用时机说明（注入主 Agent 系统提示词用于自动委派）
  - base_type: 继承的内置类型（explore/implement/test/review/general），默认 general
  - model: inherit（继承主模型）/ secondary（用副模型），默认 inherit
  - allowed_tools: 逗号分隔的工具白名单，留空则继承 base_type 默认权限
  - color: 可选，颜色标识（如 "#E67E22"）

示例：
  ---
  name: code-reviewer
  description: 审查代码质量、安全性、可维护性
  when_to_use: 需要代码审查或质量评估时使用
  base_type: review
  model: secondary
  allowed_tools: read_file, search_code, glob_files, list_directory
  color: "#E67E22"
  ---
  你是专业代码审查员，专注于...
"""

import os
import re
import hashlib
import logging
import threading

from core.workspace_context import get_workdir

# 内置 5 种 type，自定义 name 与之重名时跳过
BUILTIN_SUBAGENT_TYPES = {"explore", "implement", "test", "review", "general"}

# 指纹内容哈希只对小文件计算（子 agent 定义文件正常只有几 KB）
_FINGERPRINT_HASH_MAX_BYTES = 256 * 1024

# 扫描结果缓存与上次扫描的文件指纹，避免每次调用都重扫磁盘
_cache_lock = threading.Lock()
_cache = {"fingerprint": None, "subagents": []}


def _log_warning(message, *args):
    """异常诊断写入 runtime warnings 日志，不裸 print 污染 TUI。"""
    try:
        logging.getLogger("py.warnings").warning(message, *args)
    except Exception:
        pass


def get_agent_search_dirs():
    """返回自定义子 agent 的扫描目录列表。

    目录来源（按顺序，已按物理路径去重）：
      1. 默认用户级：~/.ycode/agents（不存在则自动创建）
      2. 项目级：<cwd>/.ycode/agents（仅已存在时扫描，不主动创建）

    与 skills.py 不同，这里不读 config.json 的 skill_search_paths，
    因为子 agent 目录与 skill 目录语义不同，独立配置更清晰。
    """
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

    # 1. 用户级
    user_dir = os.path.join(user_home, ".ycode", "agents")
    if not os.path.exists(user_dir):
        try:
            os.makedirs(user_dir, exist_ok=True)
        except Exception:
            pass
    _add(user_dir)

    # 2. 项目级（当前线程工作目录下，GUI 多会话并行时各自扫描各自项目）
    project_dir = os.path.join(get_workdir(), ".ycode", "agents")
    _add(project_dir)

    return dirs


def _compute_fingerprint(dirs):
    """计算扫描目录的内容指纹，用于判断是否需要重新扫描。"""
    fingerprint = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            for entry in sorted(os.listdir(d)):
                full = os.path.join(d, entry)
                if os.path.isfile(full) and entry.lower().endswith(".md"):
                    stat = os.stat(full)
                    entry_fp = (full, stat.st_mtime, stat.st_size)
                    # 仅有 (mtime, size) 时等长改写（同长替换/mtime 保留工具）会
                    # 命中旧缓存；小文件附带内容哈希，让指纹真实反映内容变化
                    if stat.st_size <= _FINGERPRINT_HASH_MAX_BYTES:
                        try:
                            with open(full, "rb") as fh:
                                entry_fp = entry_fp + (
                                    hashlib.sha256(fh.read()).hexdigest()[:16],
                                )
                        except Exception:
                            pass
                    fingerprint.append(entry_fp)
        except Exception:
            continue
    return tuple(fingerprint)


def _parse_frontmatter(content):
    """解析 frontmatter 和 Markdown 正文。

    返回 (frontmatter_dict, body_str)，解析失败返回 ({}, content)。
    """
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', content, re.DOTALL)
    if not match:
        return {}, content.strip()

    yaml_content = match.group(1)
    body = match.group(2).strip()

    frontmatter = {}
    for line in yaml_content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # 只处理简单的 key: value 格式，不处理嵌套
        if ':' not in line:
            continue
        key, _, value = line.partition(':')
        key = key.strip().lower()
        value = value.strip().strip('"\'')
        frontmatter[key] = value

    return frontmatter, body


def _parse_allowed_tools(value):
    """解析 allowed_tools 字段（逗号分隔字符串）为 set。"""
    if not value:
        return None
    tools = set()
    for part in value.split(','):
        tool = part.strip().lower()
        if tool:
            tools.add(tool)
    return tools if tools else None


def parse_agent_md(file_path):
    """解析单个 agent .md 文件，返回配置字典或 None（解析失败/无效）。"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        frontmatter, body = _parse_frontmatter(content)
        if not frontmatter:
            return None

        name = frontmatter.get("name", "").strip().strip('"\'')
        if not name:
            # 文件名作为默认 name
            name = os.path.splitext(os.path.basename(file_path))[0]

        # 内置 type 重名时跳过，避免覆盖内置行为
        if name.lower() in BUILTIN_SUBAGENT_TYPES:
            _log_warning(
                "自定义子 agent 文件 %s 的 name '%s' 与内置 type 重名，已跳过",
                file_path, name,
            )
            return None

        description = frontmatter.get("description", "").strip() or "无描述"
        when_to_use = frontmatter.get("when_to_use", "").strip() or description
        base_type = frontmatter.get("base_type", "general").strip().lower() or "general"
        if base_type not in BUILTIN_SUBAGENT_TYPES:
            _log_warning(
                "自定义子 agent %s 的 base_type '%s' 不支持（仅支持 %s），回退 general",
                name, base_type, ", ".join(sorted(BUILTIN_SUBAGENT_TYPES)),
            )
            base_type = "general"
        model = frontmatter.get("model", "inherit").strip().lower() or "inherit"
        if model not in ("inherit", "secondary"):
            _log_warning(
                "自定义子 agent %s 的 model '%s' 不支持（仅支持 inherit/secondary），回退 inherit",
                name, model,
            )
            model = "inherit"

        allowed_tools = _parse_allowed_tools(frontmatter.get("allowed_tools", ""))
        color = frontmatter.get("color", "").strip() or None

        # body 作为系统提示词，为空则无效
        if not body:
            _log_warning("自定义子 agent %s 的系统提示词为空，已跳过", name)
            return None

        return {
            "name": name,
            "description": description,
            "when_to_use": when_to_use,
            "base_type": base_type,
            "model": model,
            "allowed_tools": allowed_tools,  # None 表示继承 base_type 默认白名单
            "color": color,
            "system_prompt": body,
            "source": file_path,
        }
    except Exception as e:
        _log_warning("解析子 agent 文件失败 %s: %s", file_path, e)
        return None


def scan_custom_subagents():
    """扫描所有自定义子 agent 目录，返回配置列表。

    使用 mtime 指纹缓存，文件未变更时直接返回缓存结果。
    """
    dirs = get_agent_search_dirs()
    fingerprint = _compute_fingerprint(dirs)

    with _cache_lock:
        if _cache["fingerprint"] == fingerprint:
            return list(_cache["subagents"])

    # 重新扫描
    subagents = []
    seen_names = set()
    for d in dirs:
        if not os.path.isdir(d):
            continue
        try:
            for entry in sorted(os.listdir(d)):
                if not entry.lower().endswith(".md"):
                    continue
                full = os.path.join(d, entry)
                if not os.path.isfile(full):
                    continue
                config = parse_agent_md(full)
                if config is None:
                    continue
                if config["name"] in seen_names:
                    _log_warning(
                        "自定义子 agent %s 重复定义，后加载的已跳过",
                        config["name"],
                    )
                    continue
                seen_names.add(config["name"])
                subagents.append(config)
        except Exception:
            continue

    with _cache_lock:
        _cache["fingerprint"] = fingerprint
        _cache["subagents"] = subagents

    return list(subagents)


def get_custom_subagent(name):
    """按 name 获取单个自定义子 agent 配置，不存在返回 None。"""
    if not name:
        return None
    name_lower = name.lower()
    for config in scan_custom_subagents():
        if config["name"].lower() == name_lower:
            return config
    return None


def is_custom_subagent(name):
    """判断 name 是否为自定义子 agent。"""
    return get_custom_subagent(name) is not None


def get_custom_subagents_catalog():
    """返回可注入主 Agent 系统提示词的自定义子 agent 清单文本。

    无自定义子 agent 时返回空字符串。
    """
    subagents = scan_custom_subagents()
    if not subagents:
        return ""

    lines = [
        "【可用自定义子 Agent】",
        "通过 run_subagent(subagent_type=\"<name>\", task_description=\"...\") 调用；",
        "子 Agent 失败/超时时主 Agent 应直接用自身工具完成任务，不要反复重试。",
    ]
    for s in sorted(subagents, key=lambda x: x["name"].lower()):
        tools_hint = ""
        if s.get("allowed_tools"):
            tools_hint = f" | 工具: {', '.join(sorted(s['allowed_tools']))}"
        elif s.get("base_type") and s["base_type"] != "general":
            tools_hint = f" | 继承 {s['base_type']} 工具权限"
        model_hint = ""
        if s.get("model") == "secondary":
            model_hint = " | 副模型"
        lines.append(
            f"- {s['name']}: {s['description']} | 调用时机: {s['when_to_use']}{tools_hint}{model_hint}"
        )
    return "\n".join(lines)
