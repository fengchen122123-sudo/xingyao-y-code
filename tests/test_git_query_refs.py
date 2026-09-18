"""只读查询测试：分支/历史/Diff/Blame/Reflog/Tag/Remote/Stash（规格 §17.2）。"""

from core.git.query_refs import (
    blame,
    diff_document,
    list_branches,
    list_remotes,
    list_stashes,
    list_tags,
    log_page,
    occupied_branches,
    parse_unified_diff,
    reflog_page,
)
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


def _identity(tmp_path, name="repo"):
    repo = make_repo(tmp_path, name=name, commits=2)
    return resolve_repository(str(repo), GitRunner()), repo


def test_list_branches_current_and_remote(tmp_path):
    identity, repo = _identity(tmp_path)
    run_git(repo, "branch", "feature/login")
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    branches = list_branches(GitRunner(), identity)
    by_name = {b.name: b for b in branches}
    assert by_name["main"].is_current is True
    assert by_name["main"].is_remote is False
    assert by_name["feature/login"].is_current is False
    assert by_name["origin/main"].is_remote is True


def test_list_branches_ahead_behind_marker(tmp_path):
    identity, repo = _identity(tmp_path)
    run_git(repo, "config", "remote.origin.url", "https://example.com/x.git")
    run_git(repo, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    make_commit(repo, "extra")
    run_git(repo, "update-ref", "refs/remotes/origin/main", "HEAD~1")
    run_git(repo, "branch", "-u", "origin/main", "main")
    branches = {b.name: b for b in list_branches(GitRunner(), identity)}
    assert branches["main"].ahead == 1
    assert branches["main"].upstream == "origin/main"


def test_occupied_branches(tmp_path):
    identity, repo = _identity(tmp_path)
    run_git(repo, "branch", "feature/busy")
    linked = tmp_path / "wt-busy"
    run_git(repo, "worktree", "add", "-q", str(linked), "feature/busy")
    occupied = occupied_branches(GitRunner(), identity)
    assert "feature/busy" in occupied
    assert "main" not in occupied


def test_log_page_pagination_and_parents(tmp_path):
    identity, repo = _identity(tmp_path)
    make_commit(repo, "third")
    page1, more = log_page(GitRunner(), identity, skip=0, limit=2)
    assert more is True and len(page1) == 2
    assert page1[0].summary == "third"
    page2, more2 = log_page(GitRunner(), identity, skip=2, limit=2)
    assert more2 is False
    # 拓扑由父提交关系计算（不解析 --graph 文本）
    child = page1[0]
    assert child.parents and child.parents[0] in {c.oid for c in page1[1:] + page2}


def test_log_page_author_filter(tmp_path):
    identity, repo = _identity(tmp_path)
    hits, _ = log_page(GitRunner(), identity, author="Test User", limit=10)
    assert len(hits) >= 2
    misses, _ = log_page(GitRunner(), identity, author="nobody-here", limit=10)
    assert misses == []


def test_diff_document_staged_and_refs(tmp_path):
    identity, repo = _identity(tmp_path)
    (repo / "f1.txt").write_text("content 1 changed\n", encoding="utf-8")
    worktree_diff = diff_document(GitRunner(), identity)
    assert "-content 1" in worktree_diff.raw_text
    assert worktree_diff.hunks and worktree_diff.hunks[0].new_start >= 1
    run_git(repo, "add", "-A")
    staged_diff = diff_document(GitRunner(), identity, staged=True)
    assert "content 1 changed" in staged_diff.raw_text
    ref_diff = diff_document(GitRunner(), identity, left="HEAD~1", right="HEAD")
    assert "content 2" in ref_diff.raw_text


def test_parse_unified_diff_pure():
    raw = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,2 +1,2 @@ hunk header\n"
        "-old 1\n"
        "-old 2\n"
        "+new 1\n"
        "+new 2\n"
    )
    hunks = parse_unified_diff(raw)
    assert len(hunks) == 1
    h = hunks[0]
    assert (h.old_start, h.old_lines, h.new_start, h.new_lines) == (1, 2, 1, 2)
    assert h.lines == ("-old 1", "-old 2", "+new 1", "+new 2")


def test_blame_basic(tmp_path):
    identity, repo = _identity(tmp_path)
    lines = blame(GitRunner(), identity, "f1.txt")
    assert len(lines) >= 1
    short, lineno, content = lines[0]
    assert len(short) == 8 and lineno == 1 and "content" in content


def test_reflog_page(tmp_path):
    identity, repo = _identity(tmp_path)
    make_commit(repo, "another")
    entries = reflog_page(GitRunner(), identity)
    assert entries and entries[0].selector.startswith("HEAD@{")
    assert "commit" in entries[0].message or entries[0].message


def test_list_tags_annotated_flag(tmp_path):
    identity, repo = _identity(tmp_path)
    run_git(repo, "tag", "v1.0")
    run_git(repo, "tag", "-a", "v2.0", "-m", "release two")
    tags = {t.name: t for t in list_tags(GitRunner(), identity)}
    assert tags["v1.0"].is_annotated is False
    assert tags["v2.0"].is_annotated is True
    assert tags["v2.0"].message == "release two"


def test_list_remotes_redacts_credentials(tmp_path):
    identity, repo = _identity(tmp_path)
    run_git(
        repo, "remote", "add", "origin",
        "https://user:secret@github.com/a/b.git",
    )
    remotes = list_remotes(GitRunner(), identity)
    assert len(remotes) == 1
    assert remotes[0].name == "origin"
    assert "user:secret" not in remotes[0].fetch_url
    assert "github.com/a/b.git" in remotes[0].fetch_url


def test_list_stashes(tmp_path):
    identity, repo = _identity(tmp_path)
    (repo / "f1.txt").write_text("stash me\n", encoding="utf-8")
    run_git(repo, "stash", "push", "-q", "-m", "wip change")
    stashes = list_stashes(GitRunner(), identity)
    assert len(stashes) == 1
    assert stashes[0].index == 0
    assert "wip change" in stashes[0].message
