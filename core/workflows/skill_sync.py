"""工作流 → 星瑶技能生成（双通道呈现之通道二，ZCode 导入设计 §3.3）。

生成的技能放在 ``~/.ycode/skills/zcode-workflows/<name>/SKILL.md``，走
``core/skills.py`` 的简单 frontmatter（单行 name/description）。技能正文告诉
模型何时适用、参数表长什么样、以及「执行时调 workflow_run 工具」——对话里
说人话即可被技能自动匹配命中，不必记住命令。
"""

from __future__ import annotations

from pathlib import Path

from core.workflows.store import validate_name, workflows_dir


def skill_dir(name: str, *, home=None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / ".ycode" / "skills" / "zcode-workflows" / name


def _single_line(text: str) -> str:
    """技能 frontmatter 是逐行简单解析，description 必须压成单行。"""
    return " ".join(str(text or "").split())


def _args_table(args: dict) -> str:
    if not args:
        return "（无参数）"
    rows = ["| 名称 | 类型 | 必填 | 默认 | 说明 |", "|---|---|---|---|---|"]
    for arg_name, decl in args.items():
        default = "" if "default" not in decl else f"`{decl['default']!r}`"
        rows.append(
            "| {name} | {type} | {req} | {default} | {desc} |".format(
                name=arg_name,
                type=decl.get("type", "json"),
                req="是" if decl.get("required") else "否",
                default=default,
                desc=_single_line(decl.get("description", "")),
            )
        )
    return "\n".join(rows)


def generate_skill(name: str, parsed, *, home=None) -> str:
    """为一个已解析的工作流生成 SKILL.md，返回技能文件路径。"""
    name_error = validate_name(name)
    if name_error:
        raise ValueError(name_error)
    description = _single_line(parsed.description) or f"ZCode 导入的工作流 {name}"
    lines: list[str] = [
        "---",
        f"name: {name}",
        f"description: {description}",
        "---",
        "",
        f"# 工作流 {name}",
        "",
        parsed.description.strip(),
        "",
    ]
    if parsed.when_to_use.strip():
        lines += ["## 何时使用", "", parsed.when_to_use.strip(), ""]
    lines += [
        "## 参数",
        "",
        _args_table(parsed.args),
        "",
        "## 如何执行",
        "",
        "调用 workflow_run 工具：name 填本工作流名，args 按上表从用户请求中提取；",
        "未提供的可选参数省略即可。执行结果由固定的映射契约驱动（阶段建计划、",
        "agent 派子代理、world.run 走命令审批、report 逐条汇报产物），不需要",
        "你自行编排流程。",
        "",
    ]
    directory = skill_dir(name, home=home)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SKILL.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
