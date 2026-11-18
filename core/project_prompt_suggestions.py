"""工作区项目感知提示词推荐（新会话空态用，纯只读）。

根据显式传入的 workdir、可选 Git 快照及工作区近况，生成 0~3 条直接可执行的
提示词建议。对 GLOBAL、空路径或不存在目录一律返回空列表，保持自由对话干净留白。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from core import novel_service

MAX_SUGGESTIONS = 3
_SKIP_SCAN_NAMES = frozenset({".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build"})


def _make_item(item_id: str, label: str, prompt: str) -> dict[str, str]:
    return {"id": item_id, "label": label, "prompt": prompt}


def _short_title(text: str, limit: int = 16) -> str:
    clean = " ".join(str(text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return f"{clean[:limit - 1]}…"


def _detect_novel_suggestions(root: Path) -> list[dict[str, str]]:
    """探测小说项目并生成章节核对、表达清理、续写建议。"""
    discovered = novel_service.discover_novel_projects(root, max_depth=1, limit=5)
    projects = discovered.get("projects") if discovered.get("ok") else None
    if not projects:
        return []

    book = max(projects, key=lambda p: int(p.get("chapters_written") or 0))
    title = str(book.get("title") or "").strip() or root.name or "本书"
    chapters = int(book.get("chapters_written") or 0)
    book_dir = root if book.get("path") in (None, "", ".") else (root / str(book["path"]))

    if chapters > 0:
        suggestions = [
            _make_item(
                "novel-continuity",
                f"核对《{title}》前 {chapters} 章衔接",
                f"请核对《{title}》当前已写 {chapters} 章的剧情衔接、伏笔状态与人物一致性，指出需要修补的断档。",
            ),
            _make_item(
                "novel-polish",
                f"排查《{title}》重复表达与字数",
                f"帮我扫描《{title}》近期章节的字数分布与高频机械表达，列出最值得优先润色的章节。",
            ),
        ]
        state_file = book_dir / "追踪" / "tracking-state.json"
        if state_file.is_file():
            suggestions.append(
                _make_item(
                    "novel-next",
                    "依据追踪进度推进下一章",
                    f"读取《{title}》的追踪进度与大纲设定，梳理接下来一章的事件安排并准备起稿。",
                )
            )
        return suggestions

    return [
        _make_item(
            "novel-outline",
            f"梳理《{title}》核心设定与大纲",
            f"读取《{title}》现有设定与大纲文档，帮我梳理主线脉络与开篇前几章的推进节奏。",
        ),
        _make_item(
            "novel-chapter1",
            f"起草《{title}》第 1 章正文",
            f"依据《{title}》当前设定与大纲，先列出第 1 章细纲并确认开篇切入点。",
        ),
    ]


def _extract_git_counts(git_snapshot: Mapping[str, Any] | None) -> tuple[str, int, int, int]:
    """从前端已持有的 GitSnapshot 提取 (branch, conflicts, dirty, ahead)。"""
    if not isinstance(git_snapshot, Mapping):
        return "", 0, 0, 0
    branch = str(git_snapshot.get("branch_name") or "").strip()
    conflicts = len(git_snapshot.get("conflicts") or ())
    dirty = (
        len(git_snapshot.get("staged") or ())
        + len(git_snapshot.get("unstaged") or ())
        + len(git_snapshot.get("untracked") or ())
    )
    ahead = int(git_snapshot.get("ahead") or 0)
    return branch, conflicts, dirty, ahead


def _has_package_scripts(root: Path) -> bool:
    pkg = root / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
        return isinstance(data, dict) and bool(data.get("scripts"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _detect_code_markers(root: Path) -> dict[str, bool]:
    has_pkg = (root / "package.json").is_file() or (root / "frontend" / "package.json").is_file()
    has_py = (
        (root / "pyproject.toml").is_file()
        or (root / "requirements.txt").is_file()
        or (root / "setup.py").is_file()
    )
    has_tests = (root / "tests").is_dir() or (root / "frontend" / "src").is_dir()
    has_changelog = (root / "CHANGELOG.md").is_file() or (root / "assets" / "CHANGELOG.md").is_file()
    has_git = (root / ".git").exists()
    is_code = (
        has_git
        or has_pkg
        or has_py
        or (root / "Cargo.toml").is_file()
        or (root / "go.mod").is_file()
        or _has_package_scripts(root)
    )
    return {
        "is_code": is_code,
        "has_git": has_git,
        "has_pkg": has_pkg,
        "has_py": has_py,
        "has_tests": has_tests,
        "has_changelog": has_changelog,
    }


def _detect_code_suggestions(
    root: Path, git_snapshot: Mapping[str, Any] | None
) -> list[dict[str, str]]:
    markers = _detect_code_markers(root)
    branch, conflicts, dirty, ahead = _extract_git_counts(git_snapshot)
    has_git = markers["has_git"] or bool(git_snapshot)
    if not markers["is_code"] and not has_git:
        return []

    suggestions: list[dict[str, str]] = []
    if conflicts > 0:
        suggestions.append(
            _make_item(
                "git-conflicts",
                f"处理 {conflicts} 个 Git 合并冲突",
                "查看当前仓库中的 Git 冲突文件，逐个分析冲突双方改动并给出合入方案。",
            )
        )
    elif dirty > 0:
        branch_hint = f" {branch} 分支" if branch else ""
        suggestions.append(
            _make_item(
                "git-dirty",
                f"检视 {dirty} 处未提交改动",
                f"查看当前{branch_hint}的未提交改动与 diff 摘要，梳理变更模块并确认是否可分批提交。",
            )
        )
    elif ahead > 0 and branch:
        suggestions.append(
            _make_item(
                "git-ahead",
                f"核对 {branch} 待推送的 {ahead} 个提交",
                f"检查 {branch} 分支领先远端的 {ahead} 个提交记录与差异，确认推送或发版前是否有遗漏。",
            )
        )
    elif has_git:
        suggestions.append(
            _make_item(
                "git-status",
                "查看分支状态与近期改动",
                "检查当前仓库的分支状态、最近提交记录与工作区干净度，准备下一步开发。",
            )
        )

    if markers["has_tests"] or markers["has_py"] or markers["has_pkg"]:
        suggestions.append(
            _make_item(
                "code-tests",
                "运行测试与语法校验",
                "运行当前项目相关的回归测试与语法/类型检查，若有失败项直接定位到具体文件与原因。",
            )
        )

    if markers["has_changelog"] and len(suggestions) < MAX_SUGGESTIONS:
        suggestions.append(
            _make_item(
                "code-release",
                "核对变更与更新日志",
                "对照近期代码改动核对更新日志或发布清单，梳理尚未记录或需要收尾的事项。",
            )
        )
    elif len(suggestions) < MAX_SUGGESTIONS:
        suggestions.append(
            _make_item(
                "code-arch",
                "梳理项目模块与当前待办",
                "快速概览当前项目的主要目录与核心入口，整理目前可以继续推进的改造或优化点。",
            )
        )
    return suggestions[:MAX_SUGGESTIONS]


def _detect_general_suggestions(
    root: Path, recent_session_titles: Sequence[str] | None
) -> list[dict[str, str]]:
    """非小说、非典型代码仓库的一般目录：根据近况文件与最近会话生成建议。"""
    try:
        entries = [
            p for p in root.iterdir()
            if p.name not in _SKIP_SCAN_NAMES and not p.name.startswith(".")
        ]
    except OSError:
        return []

    if not entries and not recent_session_titles:
        return []

    suggestions: list[dict[str, str]] = []
    if entries:
        suggestions.append(
            _make_item(
                "dir-overview",
                "概览当前目录与重点文件",
                "扫描当前工作区下的目录结构与关键文件，帮我整理一份简明情况说明。",
            )
        )

    doc_count = sum(
        1 for p in entries if p.is_file() and p.suffix.lower() in {".md", ".txt", ".docx", ".pdf"}
    )
    if doc_count >= 2:
        suggestions.append(
            _make_item(
                "dir-docs",
                f"汇总目录内 {doc_count} 份文档要点",
                "读取当前目录中的文档资料，提炼核心内容、差异或待处理事项。",
            )
        )

    for raw_title in recent_session_titles or ():
        clean = str(raw_title or "").strip()
        if clean:
            suggestions.append(
                _make_item(
                    "recent-followup",
                    f"延续「{_short_title(clean, 14)}」",
                    f"结合工作区中与「{clean}」相关的最新文件变动，帮我检查当前进度和下一步待办。",
                )
            )
            break

    return suggestions[:MAX_SUGGESTIONS]


def build_workspace_prompt_suggestions(
    workdir: str,
    *,
    git_snapshot: Mapping[str, Any] | None = None,
    recent_session_titles: Sequence[str] | None = None,
) -> list[dict[str, str]]:
    """为指定工作区生成最多 3 条项目相关的推荐提示词。

    - workdir 为空、'GLOBAL' 或不存在目录时返回 []。
    - 优先级：小说项目 > 代码/Git 工程 > 普通非空目录。
    """
    raw = str(workdir or "").strip()
    if not raw or raw.upper() == "GLOBAL":
        return []
    try:
        root = Path(raw).expanduser()
        if not root.is_dir():
            return []
    except OSError:
        return []

    try:
        novel_items = _detect_novel_suggestions(root)
        if novel_items:
            return novel_items[:MAX_SUGGESTIONS]

        code_items = _detect_code_suggestions(root, git_snapshot)
        if code_items:
            return code_items[:MAX_SUGGESTIONS]

        return _detect_general_suggestions(root, recent_session_titles)[:MAX_SUGGESTIONS]
    except Exception:
        return []
