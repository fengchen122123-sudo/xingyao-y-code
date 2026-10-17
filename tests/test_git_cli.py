"""CLI Git 工作台测试：子命令解析、命令执行、状态栏段（规格 §12/§17.1）。

print_formatted_text 在无控制台的 pytest 环境不可用，注入收集器验证输出。
"""

import pytest

import core.git_cli as git_cli
from core.git.bootstrap import reset_git_singletons_for_tests
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git

# 本文件的用例全部依赖真实 git 子进程（tmp_path 小仓库夹具，不依赖项目仓库
# 规模）。慢点在于 Windows 上每次 git 子进程启动约 6 秒，单测实测 66s，整个
# 文件需数分钟。标记为 slow 以便快速回归时用 `pytest -m "not slow"` 跳过。
pytestmark = pytest.mark.slow


@pytest.fixture(autouse=True)
def _reset_singletons():
    reset_git_singletons_for_tests()
    yield
    reset_git_singletons_for_tests()
    git_cli._pending_cli_confirm.clear()
    git_cli._status_cache.update(key=None, at=0.0, text="")


@pytest.fixture()
def output(monkeypatch):
    collected: list = []
    monkeypatch.setattr(
        git_cli, "_printer",
        lambda text, style="ansigray": collected.append((style, text)),
    )
    return collected


@pytest.fixture()
def env(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, commits=1)
    monkeypatch.setattr(git_cli, "_workspace", lambda: str(repo))
    return resolve_repository(str(repo), GitRunner()), repo


def _text(output) -> str:
    return "\n".join(text for _style, text in output)


# ---------------------------------------------------------------- 解析器

def test_parse_git_subcommand():
    assert git_cli.parse_git_subcommand("/git status") == ("status", ["status"])
    assert git_cli.parse_git_subcommand("/git diff --staged a.py") == ("diff", ["diff", "--staged", "a.py"])
    assert git_cli.parse_git_subcommand("/git") == ("", [])


def test_unknown_subcommand_reports_usage(output, env):
    git_cli.handle_git_command("/git --evil-flag")
    assert "未知子命令" in _text(output)
    assert "用法" in _text(output)


# ---------------------------------------------------------------- 命令执行

def test_cmd_status_clean(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git status")
    out = _text(output)
    assert "main" in out and "工作区干净" in out


def test_cmd_stage_commit_roundtrip(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git trust repo")
    _text(output)
    (repo / "f1.txt").write_bytes(b"cli change\n")
    git_cli.handle_git_command("/git stage f1.txt")
    assert "已暂存" in _text(output)
    git_cli.handle_git_command("/git commit cli commit")
    assert "已提交" in _text(output)
    log = run_git(repo, "log", "--oneline").stdout.decode()
    assert "cli commit" in log


def test_cmd_switch_dirty_rejected(output, env):
    identity, repo = env
    run_git(repo, "branch", "target")
    git_cli.handle_git_command("/git trust repo")
    _text(output)
    (repo / "f1.txt").write_bytes(b"dirty\n")
    git_cli.handle_git_command("/git switch target")
    assert "dirty_worktree" in _text(output)


def test_cmd_branch_create_list(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git branch create feature/cli")
    _text(output)
    git_cli.handle_git_command("/git branch list")
    assert "feature/cli" in _text(output)


def test_cmd_log(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git log 5")
    assert "commit 1" in _text(output)


def test_cmd_trust_flow(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git trust status")
    assert "untrusted" in _text(output)
    git_cli.handle_git_command("/git trust repo")
    assert "已信任" in _text(output)
    git_cli.handle_git_command("/git trust status")
    assert "trusted" in _text(output)


def test_cmd_reset_hard_two_step_confirm(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git trust repo")
    _text(output)
    make_commit(repo, "droppable")
    # 第一次：需要确认（展示 preview + 提示 --confirm）
    git_cli.handle_git_command("/git reset --hard HEAD~1")
    first = _text(output)
    assert "--confirm" in first and "高风险" in first
    # 第二次：带 --confirm 执行
    git_cli.handle_git_command("/git reset --hard HEAD~1 --confirm HEAD~1")
    second = _text(output)
    assert "完成" in second
    log = run_git(repo, "log", "--oneline").stdout.decode()
    assert "droppable" not in log


def test_cmd_clean_dry_run_then_go(output, env):
    identity, repo = env
    (repo / "junk.txt").write_bytes(b"x")
    git_cli.handle_git_command("/git clean --dry-run")
    assert "junk.txt" in _text(output)
    assert (repo / "junk.txt").exists()
    git_cli.handle_git_command("/git trust repo")
    _text(output)
    # 第一次 --go：preview + 暂存令牌；第二次 --go：带 --confirm 执行
    git_cli.handle_git_command("/git clean --go")
    first = _text(output)
    assert "--confirm" in first
    git_cli.handle_git_command("/git clean --go")
    second = _text(output)
    assert "完成" in second
    assert not (repo / "junk.txt").exists()


def test_cmd_push_force_requires_confirm(output, env):
    identity, repo = env
    git_cli.handle_git_command("/git trust repo")
    _text(output)
    git_cli.handle_git_command("/git push origin main --force-with-lease")
    assert "--confirm" in _text(output)


# ---------------------------------------------------------------- 状态栏

def test_render_status_bar(env):
    identity, repo = env
    text = git_cli.render_git_status_bar(str(repo))
    assert "⎇" in text and "main" in text and "clean" in text
    # 缓存生效：第二次调用同 key 短间隔返回一致
    assert git_cli.render_git_status_bar(str(repo)) == text


def test_render_status_bar_dirty(env):
    identity, repo = env
    (repo / "f1.txt").write_bytes(b"dirty\n")
    text = git_cli.render_git_status_bar(str(repo))
    assert "*" in text and "1 changes" in text


def test_render_status_bar_error_is_empty():
    assert git_cli.render_git_status_bar("") == ""
    assert git_cli.render_git_status_bar("Z:/definitely/not/a/repo") == ""
