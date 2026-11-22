from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from core.workspace_context import get_workdir

_RULE_FILENAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "YCODE.md",
    os.path.join(".ycode", "AGENTS.md"),
)

_IGNORED_TOP_DIRS = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",
})


def _resolve_root(workdir: str | Path | None) -> Path | None:
    try:
        if workdir is None:
            raw = get_workdir()
            if not raw or str(raw).upper() == "GLOBAL":
                return None
            return Path(raw).resolve()
        if str(workdir).upper() == "GLOBAL":
            return None
        return Path(workdir).resolve()
    except OSError:
        return None


def load_workspace_rules(
    workdir: str | Path | None = None,
    *,
    max_chars: int = 8000,
) -> str:
    root = _resolve_root(workdir)
    if root is None or not root.is_dir():
        return ""

    candidates: list[Path] = [root / rel for rel in _RULE_FILENAMES]
    rules_dir = root / ".ycode" / "rules"
    if rules_dir.is_dir():
        try:
            candidates.extend(
                sorted(p for p in rules_dir.iterdir() if p.is_file() and p.suffix.lower() == ".md")
            )
        except OSError:
            pass

    seen: set[str] = set()
    sections: list[str] = []
    remaining = max_chars

    for path in candidates:
        if remaining <= 0:
            break
        try:
            resolved_key = os.path.normcase(str(path.resolve()))
        except OSError:
            continue
        if resolved_key in seen or not path.is_file():
            continue
        seen.add(resolved_key)

        try:
            content = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not content:
            continue

        try:
            rel_label = str(path.relative_to(root)).replace("\\", "/")
        except ValueError:
            rel_label = path.name

        header = f"### {rel_label}\n"
        budget = remaining - len(header)
        if budget <= 80:
            break
        if len(content) > budget:
            content = (
                content[: max(0, budget - 40)].rstrip()
                + "\n...(该规范文件过长，已按预算截断；需要完整内容请 read_file)"
            )
        block = header + content
        sections.append(block)
        remaining -= len(block) + 2

    if not sections:
        return ""
    return "【工作区工程规范（自动加载）】\n" + "\n\n".join(sections)


def _detect_stack_hints(root: Path) -> list[str]:
    hints: list[str] = []
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file() or (root / "setup.cfg").is_file():
        hints.append("Python")
    if (root / "venv" / "Scripts" / "python.exe").is_file():
        hints.append(r"venv\Scripts\python")
    elif (root / ".venv" / "Scripts" / "python.exe").is_file():
        hints.append(r".venv\Scripts\python")
    elif (root / "venv" / "bin" / "python").is_file():
        hints.append("venv/bin/python")
    elif (root / ".venv" / "bin" / "python").is_file():
        hints.append(".venv/bin/python")

    pkg_path = root / "package.json"
    if pkg_path.is_file():
        node_label = "Node"
        try:
            pkg = json.loads(pkg_path.read_text(encoding="utf-8", errors="replace"))
            scripts = list((pkg.get("scripts") or {}).keys())[:5]
            if scripts:
                node_label += f"(scripts: {', '.join(scripts)})"
        except Exception:
            pass
        hints.append(node_label)

    if (root / "tsconfig.json").is_file():
        hints.append("TypeScript")
    if (root / "Cargo.toml").is_file():
        hints.append("Rust/Cargo")
    if (root / "go.mod").is_file():
        hints.append("Go")
    return hints


def _git_status_summary(root: Path, *, max_lines: int = 12) -> list[str]:
    if not (root / ".git").exists():
        return []
    kwargs: dict = {
        "cwd": str(root),
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 1.5,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(["git", "status", "--porcelain", "-b"], **kwargs)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []

    lines = [line.rstrip() for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return []
    branch_line = lines[0].lstrip("# ").strip()
    changed = lines[1:]
    summary = [f"Git：{branch_line}（工作区变更 {len(changed)} 项）"]
    for item in changed[:max_lines]:
        summary.append(f"  {item}")
    if len(changed) > max_lines:
        summary.append(f"  ... 其余 {len(changed) - max_lines} 项已省略")
    return summary


def build_workspace_bootstrap_context(
    workdir: str | Path | None = None,
    *,
    max_entries: int = 25,
    max_git_lines: int = 10,
) -> str:
    """生成轻量工作区首轮态势摘要（工程栈/测试环境、顶层结构、Git 状态）。

    仅应在首个回合或非静态动态上下文中使用，切勿放入跨轮稳定前缀以免 Git 变更使缓存失效。
    """
    root = _resolve_root(workdir)
    if root is None or not root.is_dir():
        return ""

    parts: list[str] = []
    hints = _detect_stack_hints(root)
    if hints:
        parts.append("技术栈与环境：" + " | ".join(hints))

    try:
        entries = sorted(
            root.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except OSError:
        entries = []

    visible: list[str] = []
    for entry in entries:
        name = entry.name
        if name in _IGNORED_TOP_DIRS:
            continue
        visible.append(f"{name}/" if entry.is_dir() else name)
        if len(visible) >= max_entries:
            break
    if visible:
        suffix = " ..." if len(entries) > len(visible) else ""
        parts.append("顶层目录：" + ", ".join(visible) + suffix)

    parts.extend(_git_status_summary(root, max_lines=max_git_lines))
    if not parts:
        return ""
    return "【工作区首轮概览】\n" + "\n".join(parts)
