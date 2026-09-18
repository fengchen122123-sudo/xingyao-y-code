"""真实临时 Git 仓库测试基建（规格 §17.2）。

直接用 subprocess 驱动系统 git 建仓，不经被测代码，保证用例互相独立。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0):
    """在临时仓库内执行一条 git 命令（测试基建专用，不走被测 GitRunner）。"""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        timeout=timeout,
    )


def make_commit(repo: Path, message: str, files: dict | None = None) -> str:
    """写入文件并提交，返回提交 OID。"""
    for rel, content in (files or {"note.txt": f"{message}\n"}).items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", message)
    return run_git(repo, "rev-parse", "HEAD").stdout.decode().strip()


def make_repo(
    tmp_path,
    name: str = "repo",
    initial_branch: str = "main",
    commits: int = 1,
) -> Path:
    """创建带本地身份配置的真实仓库（避免依赖全局 user.name/email）。"""
    repo = tmp_path / name
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", initial_branch)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test User")
    run_git(repo, "config", "commit.gpgsign", "false")
    run_git(repo, "config", "core.autocrlf", "false")
    for i in range(commits):
        make_commit(repo, f"commit {i + 1}", files={f"f{i + 1}.txt": f"content {i + 1}\n"})
    return repo
