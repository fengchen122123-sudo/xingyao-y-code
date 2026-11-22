from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(repo: Path, *args: str, check: bool = True, timeout: float = 60.0):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        timeout=timeout,
    )


def make_commit(repo: Path, message: str, files: dict | None = None) -> str:
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
