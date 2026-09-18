"""GitStateMonitor 测试（规格 §10/§17.2，真实临时仓库 + 快轮询）。"""

import threading
import time

import pytest

from core.git.monitor import GitStateMonitor
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


POLL = dict(poll_interval=0.05, debounce=0.05)


@pytest.fixture()
def env(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    identity = resolve_repository(str(repo), GitRunner())
    monitor = GitStateMonitor(poll_interval=0.05, debounce=0.05)
    yield identity, repo, monitor
    monitor.stop()


class _Collector:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, ws):
        with self.lock:
            self.events.append((ws, time.time()))

    def wait_count(self, n, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                if len(self.events) >= n:
                    return True
            time.sleep(0.02)
        return False


def test_external_switch_notifies_within_deadline(env):
    identity, repo, monitor = env
    run_git(repo, "branch", "feature/switched")
    collector = _Collector()
    monitor.subscribe(identity.workspace_path, collector)
    time.sleep(0.2)  # 建立基线指纹
    # 外部终端切换分支（真实 git 进程）
    run_git(repo, "checkout", "-q", "feature/switched")
    assert collector.wait_count(1, timeout=2.0), "外部切换应在 ~1 秒内触发失效通知"


def test_external_commit_and_stash_notify(env):
    identity, repo, monitor = env
    collector = _Collector()
    monitor.subscribe(identity.workspace_path, collector)
    time.sleep(0.2)
    make_commit(repo, "external commit")
    assert collector.wait_count(1, timeout=2.0)
    time.sleep(0.3)
    (repo / "f1.txt").write_bytes(b"stashed\n")
    run_git(repo, "stash", "push", "-q", "-m", "w")
    assert collector.wait_count(2, timeout=2.0)


def test_no_change_no_notification(env):
    identity, repo, monitor = env
    collector = _Collector()
    monitor.subscribe(identity.workspace_path, collector)
    assert not collector.wait_count(1, timeout=0.6), "无变化不应通知"
    assert collector.events == []


def test_same_worktree_subscribers_share_and_others_isolated(tmp_path):
    repo = make_repo(tmp_path / "a", commits=1)
    other = make_repo(tmp_path / "b", commits=1)
    monitor = GitStateMonitor(poll_interval=0.05, debounce=0.05)
    try:
        c1, c2, c3 = _Collector(), _Collector(), _Collector()
        monitor.subscribe(str(repo), c1)
        monitor.subscribe(str(repo), c2)
        monitor.subscribe(str(other), c3)
        time.sleep(0.2)
        make_commit(repo, "change a only")
        assert c1.wait_count(1) and c2.wait_count(1)
        time.sleep(0.3)
        assert c3.events == [], "其他 Worktree 不应被误刷新"
    finally:
        monitor.stop()


def test_force_check_notifies_without_change(env):
    identity, repo, monitor = env
    collector = _Collector()
    monitor.subscribe(identity.workspace_path, collector)
    time.sleep(0.2)
    monitor.force_check(identity.workspace_path)
    assert collector.wait_count(1, timeout=1.0)


def test_unsubscribe_stops_notifications(env):
    identity, repo, monitor = env
    collector = _Collector()
    sub_id = monitor.subscribe(identity.workspace_path, collector)
    assert monitor.unsubscribe(sub_id) is True
    assert monitor.unsubscribe(sub_id) is False
    time.sleep(0.1)
    make_commit(repo, "after unsub")
    time.sleep(0.4)
    assert collector.events == []
    assert monitor.subscriber_count(identity.workspace_path) == 0


def test_linked_worktree_gitdir_file_probing(tmp_path):
    repo = make_repo(tmp_path, commits=1)
    run_git(repo, "branch", "feature/link")
    linked = tmp_path / "wt-link"
    run_git(repo, "worktree", "add", "-q", str(linked), "feature/link")
    monitor = GitStateMonitor(poll_interval=0.05, debounce=0.05)
    try:
        collector = _Collector()
        monitor.subscribe(str(linked), collector)
        time.sleep(0.2)
        # 在 linked worktree 内提交（其 .git 是文件 + gitdir: 指针）
        make_commit(linked, "in linked wt")
        assert collector.wait_count(1, timeout=2.0)
    finally:
        monitor.stop()


def test_unreadable_workspace_does_not_crash(tmp_path):
    missing = tmp_path / "vanished"
    missing.mkdir()
    monitor = GitStateMonitor(poll_interval=0.05, debounce=0.05)
    try:
        collector = _Collector()
        monitor.subscribe(str(missing), collector)
        import shutil

        shutil.rmtree(missing)
        time.sleep(0.3)  # 探测目录消失不崩溃
        assert monitor.subscriber_count(str(missing)) == 1
    finally:
        monitor.stop()
