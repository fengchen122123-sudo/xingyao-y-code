"""只读查询（规格 §5.5/§5.7/§5.9/§5.12）：分支、历史、Diff、Blame、Reflog、Tag、Remote、Stash。

全部使用机器可读格式（NUL 分隔的 for-each-ref / 自定义 --format），
不解析本地化人类输出；Remote URL 一律脱敏。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from core.git.models import GitError, GitErrorCode, redact_text
from core.git.models import RepositoryIdentity
from core.git.repository import normalize_repo_path
from core.git.runner import ExecScope, GitRunner

_RS = "\x1e"  # 记录分隔符（RS）
_US = "\x1f"  # 字段分隔符（US）；argv 不能嵌 NUL，故用标准控制符


# ---------------------------------------------------------------- 值对象

@dataclass(frozen=True)
class BranchRef:
    name: str
    is_remote: bool
    is_current: bool = False
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0
    upstream_gone: bool = False
    oid: str = ""


@dataclass(frozen=True)
class CommitSummary:
    oid: str
    short: str
    parents: Tuple[str, ...]
    author_name: str
    author_email: str
    author_time: int
    committer_time: int
    summary: str
    refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TagRef:
    name: str
    oid: str
    is_annotated: bool
    message: str = ""


@dataclass(frozen=True)
class RemoteInfo:
    name: str
    fetch_url: str          # 已脱敏
    push_url: str           # 已脱敏


@dataclass(frozen=True)
class StashEntry:
    index: int
    message: str
    oid: str


@dataclass(frozen=True)
class ReflogEntry:
    oid: str
    selector: str           # 例如 HEAD@{2}
    message: str


@dataclass(frozen=True)
class DiffHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str
    lines: Tuple[str, ...]


@dataclass(frozen=True)
class DiffDocument:
    """Git Diff 独立数据契约（规格 §11.2：不复用模型工具的 ChangedFilesPanel 数据）。"""

    raw_text: str
    is_binary: bool
    hunks: Tuple[DiffHunk, ...]


# ---------------------------------------------------------------- 分支

def _track_marker_to_ab(marker: str) -> Tuple[int, int, bool]:
    """%(upstream:track) 形如 ``[ahead 1, behind 2]`` / ``[gone]`` / 空。"""
    if not marker:
        return 0, 0, False
    ahead = 0
    behind = 0
    gone = "gone" in marker
    m = re.search(r"ahead (\d+)", marker)
    if m:
        ahead = int(m.group(1))
    m = re.search(r"behind (\d+)", marker)
    if m:
        behind = int(m.group(1))
    return ahead, behind, gone


def list_branches(runner: GitRunner, identity: RepositoryIdentity) -> Tuple[BranchRef, ...]:
    fmt = _US.join(
        ["%(refname)", "%(objectname)", "%(upstream:short)", "%(upstream:track)", "%(HEAD)"]
    )
    result = runner.run_checked(
        ["for-each-ref", "refs/heads", "refs/remotes", f"--format={fmt}{_RS}"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    branches: List[BranchRef] = []
    for record in result.stdout_text.split(_RS):
        record = record.strip("\n")
        if not record:
            continue
        fields = (record.split(_US) + [""] * 5)[:5]
        refname = fields[0]
        if refname.startswith("refs/heads/"):
            name = refname[len("refs/heads/"):]
            is_remote = False
        elif refname.startswith("refs/remotes/"):
            name = refname[len("refs/remotes/"):]
            is_remote = True
            if name.endswith("/HEAD"):
                # 远端 HEAD 指针不作为普通分支列出
                continue
        else:
            continue
        upstream_raw = fields[2] if len(fields) > 2 else ""
        upstream = redact_text(upstream_raw) or None
        ahead, behind, gone = _track_marker_to_ab(fields[3] if len(fields) > 3 else "")
        branches.append(
            BranchRef(
                name=name,
                is_remote=is_remote,
                is_current=(fields[4].strip() == "*"),
                upstream=upstream,
                ahead=ahead,
                behind=behind,
                upstream_gone=gone,
                oid=fields[1] if len(fields) > 1 else "",
            )
        )
    return tuple(branches)


def occupied_branches(runner: GitRunner, identity: RepositoryIdentity) -> Tuple[str, ...]:
    """被其他 Worktree 占用的分支，用于切换/删除硬规则（§5.6）。"""
    result = runner.run_checked(
        ["worktree", "list", "--porcelain"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    occupied: List[str] = []
    for block in result.stdout_text.split("\n\n"):
        branch = None
        is_current_wt = False
        for line in block.splitlines():
            if line.startswith("branch "):
                branch = line[len("branch "):].strip()
            elif line.startswith("worktree "):
                wt = line[len("worktree "):].strip()
                is_current_wt = normalize_repo_path(wt) == normalize_repo_path(
                    identity.worktree_root
                )
        if branch and not is_current_wt:
            occupied.append(branch.removeprefix("refs/heads/"))
    return tuple(occupied)


# ---------------------------------------------------------------- 历史与提交

_LOG_FORMAT = _US.join(["%H", "%h", "%P", "%an", "%ae", "%at", "%ct", "%s", "%D"])


def _parse_log_records(stdout_text: str) -> List[CommitSummary]:
    commits: List[CommitSummary] = []
    for record in stdout_text.split(_RS):
        record = record.strip("\n")
        if not record.strip():
            continue
        f = record.split(_US)
        if len(f) < 9:
            continue
        refs = tuple(r for r in f[8].replace(", ", ",").split(",") if r and r != "->")
        commits.append(
            CommitSummary(
                oid=f[0],
                short=f[1],
                parents=tuple(p for p in f[2].split(" ") if p),
                author_name=f[3],
                author_email=f[4],
                author_time=int(f[5] or 0),
                committer_time=int(f[6] or 0),
                summary=redact_text(f[7]),
                refs=refs,
            )
        )
    return commits


def log_page(
    runner: GitRunner,
    identity: RepositoryIdentity,
    *,
    skip: int = 0,
    limit: int = 50,
    refs: Optional[Tuple[str, ...]] = None,
    author: Optional[str] = None,
    since: Optional[str] = None,
    path: Optional[str] = None,
    follow: bool = False,
) -> Tuple[List[CommitSummary], bool]:
    """提交分页。返回 (提交列表, 是否还有更多)。不解析 --graph ASCII 文本（§5.9）。"""
    args: List[str] = [
        "log", f"--format={_LOG_FORMAT}{_RS}", f"--max-count={limit + 1}",
    ]
    if skip > 0:
        args.append(f"--skip={skip}")
    if author:
        args.append(f"--author={author}")
    if since:
        args.append(f"--since={since}")
    if follow and path:
        args.append("--follow")
    args.extend(refs or ["HEAD"])
    if path:
        args += ["--", path]
    result = runner.run_checked(
        args, cwd=identity.worktree_root, scope=ExecScope.READ_ONLY, timeout=60.0
    )
    commits = _parse_log_records(result.stdout_text)
    has_more = len(commits) > limit
    return commits[:limit], has_more


def commit_summary_of(
    runner: GitRunner,
    identity: RepositoryIdentity,
    oid: str,
) -> CommitSummary:
    result = runner.run_checked(
        ["show", f"--format={_LOG_FORMAT}{_RS}", "--no-patch", str(oid)],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=60.0,
    )
    commits = _parse_log_records(result.stdout_text)
    if not commits:
        raise GitError(GitErrorCode.INVALID_REF, f"提交不存在: {oid}")
    return commits[0]


def parse_unified_diff(raw_text: str) -> Tuple[DiffHunk, ...]:
    """把 unified diff 文本解析为 Hunk 列表（纯函数，供 GUI 渲染）。"""
    hunks: List[DiffHunk] = []
    current: Optional[Dict] = None
    header_re = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)")
    for line in raw_text.splitlines():
        m = header_re.match(line)
        if m:
            if current:
                hunks.append(_make_hunk(current))
            current = {
                "old_start": int(m.group(1)),
                "old_lines": int(m.group(2) or 1),
                "new_start": int(m.group(3)),
                "new_lines": int(m.group(4) or 1),
                "header": (m.group(5) or "").strip(),
                "lines": [],
            }
        elif current is not None:
            current["lines"].append(line)
    if current:
        hunks.append(_make_hunk(current))
    return tuple(hunks)


def _make_hunk(state: Dict) -> DiffHunk:
    return DiffHunk(
        old_start=state["old_start"],
        old_lines=state["old_lines"],
        new_start=state["new_start"],
        new_lines=state["new_lines"],
        header=state["header"],
        lines=tuple(state["lines"]),
    )


def diff_document(
    runner: GitRunner,
    identity: RepositoryIdentity,
    *,
    left: Optional[str] = None,
    right: Optional[str] = None,
    path: Optional[str] = None,
    staged: bool = False,
) -> DiffDocument:
    """工作区 Diff / 暂存区 Diff / 任意两引用 Diff（§5.3）。"""
    args: List[str] = ["diff", "--no-ext-diff", "--no-textconv", "--find-renames"]
    if left is not None and right is not None:
        args.append(f"{left}..{right}")
    elif staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    result = runner.run_checked(
        args, cwd=identity.worktree_root, scope=ExecScope.READ_ONLY, timeout=120.0
    )
    raw = result.stdout_text
    is_binary = "Binary files" in raw or "GIT binary patch" in raw
    return DiffDocument(raw_text=raw, is_binary=is_binary, hunks=parse_unified_diff(raw))


# ---------------------------------------------------------------- blame / reflog / tag / remote / stash

def blame(
    runner: GitRunner,
    identity: RepositoryIdentity,
    path: str,
) -> Tuple[Tuple[str, int, str], ...]:
    """单行 Blame（--porcelain）：返回 (短提交, 最终行号, 内容) 列表。"""
    result = runner.run_checked(
        ["blame", "--porcelain", "--", path],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=60.0,
    )
    output: List[Tuple[str, int, str]] = []
    current_short = ""
    current_line = 0
    for line in result.stdout_text.splitlines():
        if line.startswith("\t"):
            output.append((current_short, current_line, line[1:]))
            continue
        parts = line.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and _is_hex(parts[0]):
            # porcelain 头行: <sha> <orig_line> <final_line> [<num_lines>]
            current_short = parts[0][:8]
            try:
                current_line = int(parts[2])
            except ValueError:
                current_line = 0
    return tuple(output)


def _is_hex(text: str) -> bool:
    return all(c in "0123456789abcdef" for c in text)


def reflog_page(
    runner: GitRunner,
    identity: RepositoryIdentity,
    *,
    skip: int = 0,
    limit: int = 50,
) -> Tuple[ReflogEntry, ...]:
    result = runner.run_checked(
        [
            "reflog", f"--format=%H{_US}%gd{_US}%gs{_RS}",
            f"--max-count={limit}", f"--skip={skip}",
        ],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    entries: List[ReflogEntry] = []
    for record in result.stdout_text.split(_RS):
        record = record.strip()
        if not record:
            continue
        f = record.split(_US)
        if len(f) >= 3:
            entries.append(ReflogEntry(oid=f[0], selector=f[1], message=redact_text(f[2])))
    return tuple(entries)


def list_tags(runner: GitRunner, identity: RepositoryIdentity) -> Tuple[TagRef, ...]:
    fmt = _US.join(
        ["%(refname:short)", "%(objectname)", "%(objecttype)", "%(contents:subject)"]
    )
    result = runner.run_checked(
        ["for-each-ref", "refs/tags", f"--format={fmt}{_RS}"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    tags: List[TagRef] = []
    for record in result.stdout_text.split(_RS):
        record = record.strip()
        if not record:
            continue
        f = record.split(_US)
        if len(f) >= 3:
            tags.append(
                TagRef(
                    name=f[0],
                    oid=f[1],
                    is_annotated=(f[2] == "tag"),
                    message=redact_text(f[3]) if len(f) > 3 else "",
                )
            )
    return tuple(tags)


def list_remotes(runner: GitRunner, identity: RepositoryIdentity) -> Tuple[RemoteInfo, ...]:
    result = runner.run_checked(
        ["remote", "-v"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    remotes: Dict[str, Dict[str, str]] = {}
    for line in result.stdout_text.splitlines():
        if "\t" not in line:
            continue
        name, rest = line.split("\t", 1)
        if " " not in rest:
            continue
        url, kind = rest.rsplit(" ", 1)
        bucket = remotes.setdefault(name.strip(), {})
        if kind.strip() == "(push)":
            bucket["push"] = url
        else:
            bucket.setdefault("fetch", url)
    return tuple(
        RemoteInfo(
            name=name,
            fetch_url=redact_text(bucket.get("fetch", "")),
            push_url=redact_text(bucket.get("push", bucket.get("fetch", ""))),
        )
        for name, bucket in sorted(remotes.items())
    )


def list_stashes(
    runner: GitRunner,
    identity: RepositoryIdentity,
) -> Tuple[StashEntry, ...]:
    result = runner.run(
        ["stash", "list", f"--format=%gd{_US}%H{_US}%gs{_RS}"],
        cwd=identity.worktree_root,
        scope=ExecScope.READ_ONLY,
        timeout=30.0,
    )
    if result.returncode != 0:
        return ()
    stashes: List[StashEntry] = []
    for record in result.stdout_text.split(_RS):
        record = record.strip()
        if not record:
            continue
        f = record.split(_US)
        if len(f) >= 3:
            selector = f[0]  # stash@{0}
            try:
                index = int(selector.split("{")[1].rstrip("}"))
            except (IndexError, ValueError):
                index = len(stashes)
            stashes.append(StashEntry(index=index, oid=f[1], message=redact_text(f[2])))
    return tuple(stashes)
