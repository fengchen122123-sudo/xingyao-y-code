import os
import re
import json
import threading
from datetime import date, datetime
from pathlib import Path

MEMORY_DIR = Path.home() / ".ycode" / "memory"
SESSIONS_DIR = Path.home() / ".ycode" / "sessions"

# 记忆文件读写锁：防止后台 dream 整理线程与主线程 append_to_daily_log 并发写导致丢行/截断
_memory_io_lock = threading.Lock()


def _atomic_write_text(path, content):
    """临时文件 + replace 原子写入，避免写一半崩溃产生截断文件。调用方需持有 _memory_io_lock。"""
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except BaseException:
        # os.replace 在 Windows 上若目标文件被占用会抛 PermissionError；
        # 用 try/finally 确保临时文件不会永久残留。
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _is_safe_memory_filename(fname):
    """校验 LLM 输出的记忆文件名，拒绝路径穿越（如 ../x、dir/x、绝对路径）。"""
    if not fname or not isinstance(fname, str):
        return False
    if ".." in fname:
        return False
    # 只允许裸文件名：不能包含任何路径分隔/盘符结构
    return Path(fname).name == fname and not os.path.splitdrive(fname)[0]

def ensure_memory_dir():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    (MEMORY_DIR / "logs").mkdir(parents=True, exist_ok=True)

def daily_log_path(today=None):
    today = today or date.today()
    path = MEMORY_DIR / "logs" / str(today.year) / f"{today.month:02d}" / f"{today.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path

def append_to_daily_log(entry):
    ensure_memory_dir()
    path = daily_log_path()
    timestamp = datetime.now().strftime("%H:%M")
    with _memory_io_lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"- [{timestamp}] {entry}\n")

def extract_memory_tags(text):
    if not text:
        return []
    return [m.strip() for m in re.findall(r"<memory>(.*?)</memory>", text, re.DOTALL)]

def load_memory_index():
    path = MEMORY_DIR / "MEMORY.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:10000]
    except OSError:
        return ""

def build_memory_system_section():
    ensure_memory_dir()
    index = load_memory_index()
    section = f"""
# Auto Memory

You have a persistent, file-based memory system at `{MEMORY_DIR.as_posix()}/`.
This directory already exists — write to it directly with the Write tool (do not run mkdir or check for its existence).

You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.

If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.

## Types of memory

There are four discrete types of memory:

### user
Information about the user's role, goals, responsibilities, and knowledge. Great user memories help you tailor future behavior to the user's preferences.
**When to save:** When you learn details about the user's role, preferences, responsibilities, or knowledge.

### feedback
Guidance or correction the user has given you. These are very important — they allow you to remain coherent and responsive across sessions. Without these, you will repeat the same mistakes.
**When to save:** Any time the user corrects your approach in a way applicable to future conversations (e.g. "don't mock the database", "stop summarizing").
**Body structure:** Lead with the rule, then a **Why:** line and a **How to apply:** line.

### project
Information about ongoing work, goals, initiatives, bugs, or incidents not derivable from code or git history.
**When to save:** When you learn who is doing what, why, or by when. Always convert relative dates to absolute dates.
**Body structure:** Lead with the fact/decision, then **Why:** and **How to apply:** lines.

### reference
Pointers to where information lives in external systems.
**When to save:** When you learn about resources and their purpose (e.g. "bugs tracked in Linear project INGEST").

## What NOT to save
- Code patterns, architecture, file paths — derivable from reading the project
- Git history, recent changes — `git log` / `git blame` are authoritative
- Debugging solutions — the fix is in the code; the commit message has context
- Ephemeral task details or current conversation context

## How to save memories

**Option A — <memory> tags (quick notes):**
Wrap text in `<memory>...</memory>` tags in your response. These are automatically extracted and appended to the daily log.

**Option B — Write files directly (structured memories):**
Write a `.md` file to `{MEMORY_DIR.as_posix()}/` with this frontmatter:

```markdown
---
name: {{{{memory name}}}}
description: {{{{one-line description — used to decide relevance later}}}}
type: {{{{user | feedback | project | reference}}}}
---

{{{{memory content}}}}
```

Then add a pointer to that file in `{MEMORY_DIR.as_posix()}/MEMORY.md`.
MEMORY.md is an index, not a memory — it should contain only links with brief descriptions. Keep it under 200 lines.

## When to access memories
- When specific known memories seem relevant to the task at hand
- When the user seems to be referring to work from a prior conversation
- You MUST access memory when the user explicitly asks you to recall or remember
"""
    if index:
        section += f"\n## Current Memory Index (MEMORY.md)\n{index}\n"
    else:
        section += "\nNo memories consolidated yet.\n"

    return section

def run_dream_consolidation_sync(api_model, api_key, api_base=None):
    """
    后台执行 dream 归纳整理：
    1. 收集Logs文件夹中的所有今日日志与MEMORY.md内容；
    2. 利用大模型生成整理策略，修改对应的 .md 记忆文件，并更新 MEMORY.md 索引。
    """
    ensure_memory_dir()
    index = load_memory_index()
    
    # 搜集所有日记账文件内容
    log_content = ""
    logs_dir = MEMORY_DIR / "logs"
    if logs_dir.exists():
        log_files = sorted(list(logs_dir.glob("**/*.md")))
        for f in log_files[-5:]:
            try:
                log_content += f"\n### File: {f.name}\n" + f.read_text(encoding="utf-8")
            except Exception:
                pass
            
    if not log_content.strip() and not index:
        return "没有找到可以整理的历史日记账或记忆文件。"

    from litellm import completion
    
    prompt = f"""
你正在为星瑶 (Y-code) 整理长期记忆文件。你的任务是将最近的每日日志 (Daily Logs) 里的新事实、反馈和规则整理进分类记忆文件中，并更新索引 MEMORY.md。

目前记忆目录的物理路径在 `{MEMORY_DIR.as_posix()}/` 目录下。
已有的记忆索引 (MEMORY.md) 内容为：
{index}

最近的每日日志内容为：
{log_content}

请结合已有索引和最近日志：
1. 提炼出需要新创建或更新的长期记忆（类型包括 user, feedback, project, reference）；
2. 给出具体的合并与修改指令。你的输出应该是一个包含具体行动的 JSON 数组，格式如下：
[
  {{
    "action": "create_or_update",
    "filename": "feedback_rules.md",
    "content": "---\\nname: 编写代码缩进规则\\ndescription: 缩进要求与注释偏好\\ntype: feedback\\n---\\n\\n- 使用4空格缩进。\\nWhy: 用户喜欢清晰的4空格缩进，极度反感Tab或2空格。\\nHow to apply: 每次编辑代码时确保用4空格。"
  }},
  {{
    "action": "update_index",
    "content": "- [编写代码缩进规则](feedback_rules.md) — 缩进要求为4空格"
  }}
]

注意：
- 转换所有的相对日期为绝对日期。
- 保持极其精简，去粗取精。
- 直接输出合法的 JSON 数组，不要带任何 ```json 的包裹，也不要带任何解释文字，以便直接解析。
"""
    try:
        kwargs = {
            "model": api_model,
            "api_key": api_key,
            "messages": [{"role": "user", "content": prompt}],
            "timeout": 60
        }
        if api_base:
            kwargs["api_base"] = api_base
            
        res = completion(**kwargs)
        reply = res.choices[0].message.content.strip()
        # 清理多余字符
        reply = re.sub(r"^```json\s*", "", reply)
        reply = re.sub(r"\s*```$", "", reply)
        
        actions = json.loads(reply)
        summary_lines = []
        for act in actions:
            action_type = act.get("action")
            if action_type == "create_or_update":
                fname = act.get("filename")
                content = act.get("content")
                if fname and content:
                    # MEMORY.md 是索引文件：只允许走 update_index 追加索引行，
                    # 禁止 create_or_update 整体覆写，绕过索引去重合并逻辑
                    if str(fname).strip().lower() == "memory.md":
                        summary_lines.append("⚠️ 拒绝直接覆写索引文件 MEMORY.md（索引请使用 update_index 追加）")
                        continue
                    if not _is_safe_memory_filename(fname):
                        summary_lines.append(f"⚠️ 拒绝非法记忆文件名（路径穿越风险）: {fname}")
                        continue
                    with _memory_io_lock:
                        fpath = MEMORY_DIR / fname
                        _atomic_write_text(fpath, content)
                    summary_lines.append(f"✍️ 创建/更新记忆文件: {fname}")
            elif action_type == "update_index":
                idx_content = act.get("content")
                if idx_content:
                    with _memory_io_lock:
                        idx_path = MEMORY_DIR / "MEMORY.md"
                        old_idx = ""
                        if idx_path.exists():
                            old_idx = idx_path.read_text(encoding="utf-8")

                        lines = [line.strip() for line in old_idx.split('\n') if line.strip()]
                        new_line = idx_content.strip()
                        if new_line not in lines:
                            lines.append(new_line)
                            _atomic_write_text(idx_path, "\n".join(lines) + "\n")
                            summary_lines.append(f"📝 更新索引 MEMORY.md: {new_line}")
                        
        if summary_lines:
            return "梦境整理完毕：\n" + "\n".join(summary_lines)
        return "未检测到需要更新的记忆内容。"
    except Exception as e:
        return f"梦境整理失败：{str(e)}"
