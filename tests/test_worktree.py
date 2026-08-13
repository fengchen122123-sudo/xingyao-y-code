"""T4：/worktree 参数注入与路径边界校验测试。"""

import core.worktree as worktree


def _fake_git_repo(monkeypatch):
    """绕过真实 git 调用，只测参数校验层。"""
    monkeypatch.setattr(worktree, "_is_git_repo", lambda cwd=None: True)
    monkeypatch.setattr(worktree, "_run_git", lambda args, cwd=None: (0, "", ""))


def test_create_worktree_rejects_option_like_branch(tmp_path, monkeypatch):
    """--force 之类的选项式分支名不得传给 git（旧字符集校验可绕过）"""
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("--force", "wt", cwd=tmp_path)

    assert "🚨" in result
    assert "- 开头" in result


def test_create_worktree_rejects_option_like_base_branch(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("feature", "wt", base_branch="--force", cwd=tmp_path)

    assert "🚨" in result


def test_create_worktree_rejects_option_like_path(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("feature", "--checkout", cwd=tmp_path)

    assert "🚨" in result


def test_create_worktree_rejects_repo_root_path(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("feature", ".", cwd=tmp_path)

    assert "🚨" in result


def test_create_worktree_rejects_path_inside_git_dir(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("feature", ".git/evil", cwd=tmp_path)

    assert "🚨" in result


def test_create_worktree_accepts_normal_branch_and_path(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.create_worktree("feature-x", ".worktrees/feature-x", cwd=tmp_path)

    assert "✔" in result


def test_remove_worktree_rejects_option_like_path(tmp_path, monkeypatch):
    _fake_git_repo(monkeypatch)

    result = worktree.remove_worktree("--force", cwd=tmp_path)

    assert "🚨" in result
