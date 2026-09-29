"""porcelain v2 解析与操作状态检测测试（规格 §5.2/§14/§17.1）。"""

from pathlib import Path

from core.git.query import (
    detect_operation_state,
    fetch_status,
    head_state_from,
    parse_status_v2,
    strip_ignored_records,
)
from core.git.models import HeadState, OperationKind, OperationPhase
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


# ---------------------------------------------------------------- 合成字节表驱动

def test_parse_branch_headers_and_untracked():
    raw = (
        b"# branch.oid abc123\x00"
        b"# branch.head dev\x00"
        b"# branch.upstream origin/dev\x00"
        b"# branch.ab +2 -1\x00"
        b"# stash 3\x00"
        b"? u1.txt\x00"
        b"? \xe4\xb8\xad\xe6\x96\x87 \xe6\x96\x87\xe4\xbb\xb6.txt\x00"  # "中文 文件.txt"
    )
    status = parse_status_v2(raw)
    assert status.head_oid == "abc123"
    assert status.branch_name == "dev"
    assert status.upstream == "origin/dev"
    assert status.ahead == 2 and status.behind == 1
    assert status.stash_count == 3
    assert status.untracked == ("u1.txt", "中文 文件.txt")


def test_parse_detached_and_unborn_markers():
    detached = parse_status_v2(b"# branch.oid abc\x00# branch.head (detached)\x00")
    assert detached.branch_name is None
    unborn = parse_status_v2(b"# branch.oid (initial)\x00# branch.head main\x00")
    assert unborn.head_oid is None and unborn.branch_name == "main"
    assert head_state_from(unborn) is HeadState.UNBORN
    assert head_state_from(detached) is HeadState.DETACHED


def test_parse_changed_entries_with_special_paths():
    raw = (
        b"1 M. N... 100644 100644 100644 h1 h2 \xe5\x90\xab \xe7\xa9\xba\xe6\xa0\xbc.txt\x00"
        b"1 .M N... 100644 100644 100644 h1 h2 plain.txt\x00"
        b"1 D. N... 100644 100644 000000 h1 0000000000000000000000000000000000000000 gone.txt\x00"
    )
    status = parse_status_v2(raw)
    assert status.staged[0].path == "含 空格.txt"
    assert status.staged[0].index_status == "M"
    assert status.unstaged[0].path == "plain.txt"
    assert status.unstaged[0].worktree_status == "M"
    assert status.staged[1].path == "gone.txt"
    assert status.staged[1].index_status == "D"


def test_parse_rename_entry_path_then_orig_path():
    # 字节格式经真实仓库实验核对：score 后 path\0origPath\0
    raw = (
        b"# branch.oid a\x00# branch.head main\x00"
        b"2 R. N... 100644 100644 100644 h1 h2 R60 "
        b"\xe6\x96\xb0 \xe5\x90\x8d.txt\x00old-name.txt\x00"
    )
    status = parse_status_v2(raw)
    assert len(status.entries) == 1
    entry = status.entries[0]
    assert entry.path == "新 名.txt"
    assert entry.orig_path == "old-name.txt"
    assert entry.index_status == "R"


def test_parse_conflict_entries_kinds():
    raw = (
        b"u UU N... 100644 100644 100644 100644 h1 h2 h3 both.txt\x00"
        b"u AA N... 0 0 0 0 h1 h2 h3 new.txt\x00"
        b"u DU N... 0 0 0 0 h1 h2 h3 del.txt\x00"
    )
    status = parse_status_v2(raw)
    kinds = {c.path: c.kind for c in status.conflicts}
    assert kinds == {
        "both.txt": "both_modified",
        "new.txt": "both_added",
        "del.txt": "deleted_by_us",
    }
    assert status.has_conflicts


def test_staged_unstaged_grouping():
    raw = (
        b"1 MM N... 100644 100644 100644 h1 h2 both.txt\x00"
        b"1 M. N... 100644 100644 100644 h1 h2 only-staged.txt\x00"
        b"1 .M N... 100644 100644 100644 h1 h2 only-wt.txt\x00"
    )
    status = parse_status_v2(raw)
    assert [e.path for e in status.staged] == ["both.txt", "only-staged.txt"]
    assert [e.path for e in status.unstaged] == ["both.txt", "only-wt.txt"]


# ---------------------------------------------------------------- 真实仓库

def test_fetch_status_real_repo_roundtrip(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    (repo / "中文 目录").mkdir()
    (repo / "中文 目录" / "a b.txt").write_text("x\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    identity = resolve_repository(str(repo), GitRunner())
    status = fetch_status(GitRunner(), identity)
    staged_paths = [e.path for e in status.staged]
    assert "中文 目录/a b.txt" in staged_paths
    (repo / "w.txt").write_text("wt\n", encoding="utf-8")
    status2 = fetch_status(GitRunner(), identity)
    assert "w.txt" in status2.untracked
    assert status2.stash_count == 0


def test_rename_roundtrip_real_repo(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    run_git(repo, "mv", "f1.txt", "f1-renamed.txt")
    identity = resolve_repository(str(repo), GitRunner())
    status = fetch_status(GitRunner(), identity)
    renamed = [e for e in status.staged if e.index_status == "R"]
    assert len(renamed) == 1
    assert renamed[0].path == "f1-renamed.txt"
    assert renamed[0].orig_path == "f1.txt"


def test_ahead_behind_real_repo(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD~0")
    make_commit(repo, "extra")
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    # upstream 需要 remote.origin 配置 + 跟踪分支，git 才输出 branch.upstream/ab
    run_git(repo, "config", "remote.origin.url", "https://example.com/probe.git")
    run_git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    run_git(repo, "branch", "-u", "origin/main", "main")
    identity = resolve_repository(str(repo), GitRunner())
    status = fetch_status(GitRunner(), identity)
    assert status.upstream == "origin/main"
    assert status.ahead == 1 and status.behind == 0


# ---------------------------------------------------------------- 操作状态检测

def test_detect_operation_merge_and_rebase(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    runner = GitRunner()
    assert detect_operation_state(runner, identity, False) == (
        OperationKind.IDLE,
        OperationPhase.NONE,
    )

    # MERGE_HEAD 模拟 merge 进行中
    (Path(identity.git_dir) / "MERGE_HEAD").write_text("abc\n", encoding="utf-8")
    assert detect_operation_state(runner, identity, False) == (
        OperationKind.MERGE,
        OperationPhase.RUNNING,
    )
    assert detect_operation_state(runner, identity, True) == (
        OperationKind.MERGE,
        OperationPhase.PAUSED_FOR_CONFLICT,
    )
    (Path(identity.git_dir) / "MERGE_HEAD").unlink()

    # rebase-merge 目录 → rebase；有冲突 = paused_for_conflict，无 = paused_for_edit
    rebase_dir = Path(identity.git_dir) / "rebase-merge"
    rebase_dir.mkdir()
    (rebase_dir / "interactive").write_text("", encoding="utf-8")
    assert detect_operation_state(runner, identity, False) == (
        OperationKind.REBASE,
        OperationPhase.PAUSED_FOR_EDIT,
    )
    assert detect_operation_state(runner, identity, True) == (
        OperationKind.REBASE,
        OperationPhase.PAUSED_FOR_CONFLICT,
    )
    import shutil

    shutil.rmtree(rebase_dir, ignore_errors=True)


def test_detect_bisect_marked_unsupported(tmp_path):
    repo = make_repo(tmp_path, commits=2)
    identity = resolve_repository(str(repo), GitRunner())
    (Path(identity.git_dir) / "BISECT_LOG").write_text("", encoding="utf-8")
    kind, phase = detect_operation_state(GitRunner(), identity, False)
    assert kind is OperationKind.UNSUPPORTED_EXTERNAL
    assert phase is OperationPhase.RUNNING


def test_parse_ignored_records_counted():
    # --ignored=matching 合并进单次扫描后，`!` 记录在解析时计数，其余条目不受影响
    raw = (
        b"# branch.oid a\x00# branch.head main\x00"
        b"1 .M N... 100644 100644 100644 h1 h2 tracked.txt\x00"
        b"! node_modules/pkg/index.js\x00"
        b"? u.txt\x00"
        b"! build/out.log\x00"
    )
    status = parse_status_v2(raw)
    assert status.ignored_count == 2
    assert [e.path for e in status.unstaged] == ["tracked.txt"]
    assert status.untracked == ("u.txt",)


def test_parse_ignored_without_flag_is_zero():
    status = parse_status_v2(b"# branch.oid a\x00# branch.head main\x00? u.txt\x00")
    assert status.ignored_count == 0


def test_strip_ignored_records_byte_fidelity():
    # 剥离 `!` 记录后须与不带 --ignored 的扫描字节完全一致（worktree_version 材料不变）
    plain = (
        b"# branch.oid a\x00# branch.head main\x00"
        b"1 .M N... 100644 100644 100644 h1 h2 tracked.txt\x00"
        b"? u.txt\x00"
    )
    merged = (
        b"# branch.oid a\x00# branch.head main\x00"
        b"! build/out.log\x00"
        b"1 .M N... 100644 100644 100644 h1 h2 tracked.txt\x00"
        b"? u.txt\x00"
        b"! node_modules/pkg/index.js\x00"
    )
    assert strip_ignored_records(merged) == plain


def test_strip_ignored_records_keeps_rename_orig_path():
    # `2` 记录的 <origPath> 是独立 NUL 段，恰好以 "! " 开头也不能被误剥
    raw = (
        b"2 R. N... 100644 100644 100644 h1 h2 R60 new.txt\x00! tricky-orig.txt\x00"
        b"! real-ignored.log\x00"
    )
    stripped = strip_ignored_records(raw)
    assert stripped == (
        b"2 R. N... 100644 100644 100644 h1 h2 R60 new.txt\x00! tricky-orig.txt\x00"
    )
    status = parse_status_v2(stripped)
    assert status.entries[0].orig_path == "! tricky-orig.txt"
    assert status.ignored_count == 0


def test_ignored_count_from_merged_scan_real_repo(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    (repo / "ignore-me.log").write_text("x\n", encoding="utf-8")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    run_git(repo, "add", ".gitignore")
    run_git(repo, "commit", "-q", "-m", "gitignore")
    identity = resolve_repository(str(repo), GitRunner())
    result = GitRunner().run(
        [
            "status", "--porcelain=v2", "-z", "--branch", "--show-stash",
            "--ignored=matching",
        ],
        cwd=identity.worktree_root,
        timeout=60.0,
    )
    status = parse_status_v2(result.stdout)
    assert status.ignored_count >= 1
