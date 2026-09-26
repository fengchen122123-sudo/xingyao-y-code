"""GUI Git 桥接层测试（规格 §13/§17.3 后端侧，真实临时仓库）。"""

import threading
import time

import pytest

from core.git.bootstrap import reset_git_singletons_for_tests
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_commit, make_repo, run_git


@pytest.fixture()
def bridge():
    from gui_bridge import ApiBridge

    reset_git_singletons_for_tests()
    b = ApiBridge()
    yield b
    reset_git_singletons_for_tests()


class _FakeWindow:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def evaluate_js(self, js: str):
        with self.lock:
            self.events.append(js)


def _trust(bridge, repo):
    info = bridge.git_get_trust(str(repo))
    assert info["ok"] and info["level"] == "untrusted"
    assert bridge.git_trust_repository(str(repo))["ok"]
    again = bridge.git_get_trust(str(repo))
    print("TRUST-RECHECK:", again.get("level"))
    import config
    from core.git import trust as trust_mod
    print("TRUST-DIR:", config.YCODE_DIR, "store:", list(trust_mod.load_trust_store().get("repos", {}).keys()))


def test_git_get_status_and_stage_commit(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=1)
    res = bridge.git_get_status(str(repo))
    assert res["ok"] is True
    assert res["snapshot"]["branch_name"] == "main"
    assert res["snapshot"]["operation_kind"] == "idle"
    assert len(res["snapshot"]["worktree_version"]) == 64

    (repo / "f1.txt").write_bytes(b"bridge change\n")
    # 未刷新版本前 stage 用旧版本 → 可能过期；先刷新
    _trust(bridge, repo)
    refreshed = bridge.git_get_status(str(repo))
    assert refreshed["ok"]
    staged = bridge.git_stage(str(repo), ["f1.txt"])
    assert staged["ok"] is True, staged
    assert any(e["path"] == "f1.txt" for e in staged["snapshot"]["staged"])
    committed = bridge.git_commit(str(repo), "feat: bridge commit")
    assert committed["ok"] is True, committed
    assert committed["snapshot"]["staged"] == []
    log = run_git(repo, "log", "--oneline").stdout.decode()
    assert "bridge commit" in log


def test_error_shape_structured(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=1)
    _trust(bridge, repo)
    (repo / "f1.txt").write_bytes(b"x")
    bridge.git_stage(str(repo), ["f1.txt"])
    # 空标题提交 → invalid_state
    res = bridge.git_commit(str(repo), "")
    assert res["ok"] is False
    assert res["error"]["code"] == "invalid_state"
    assert res["error"]["message"]


def test_not_a_repo_error(bridge, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = bridge.git_get_status(str(plain))
    assert res["ok"] is False
    assert res["error"]["code"] in ("not_a_repository", "git_version_unsupported", "git_not_found")


def test_preview_and_confirm_two_phase(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=2)
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "extra")
    _trust(bridge, repo)
    preview = bridge.git_preview_risk(
        str(repo), "reset_hard", {"target": "HEAD~1", "confirm_input": "HEAD~1"}
    )
    assert preview["ok"] and preview["preview"]["confirm_required"] is True
    assert preview["preview"]["token"]
    confirmed = bridge.git_confirm_operation(
        str(repo), "reset_hard",
        {"target": "HEAD~1", "confirm_input": "HEAD~1"},
        preview["preview"]["token"],
    )
    assert confirmed["ok"] is True, confirmed
    log = run_git(repo, "log", "--oneline").stdout.decode()
    assert "extra" not in log


def test_branch_listing_and_switch(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=1)
    run_git(repo, "branch", "feature/b")
    listing = bridge.git_list_branches(str(repo))
    assert listing["ok"] and {b["name"] for b in listing["branches"]} >= {"main", "feature/b"}
    _trust(bridge, repo)
    switched = bridge.git_switch_branch(str(repo), "feature/b")
    assert switched["ok"] is True, switched
    assert switched["snapshot"]["branch_name"] == "feature/b"


def test_history_and_diff_loading(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=2)
    history = bridge.git_load_history(str(repo), 0, 10)
    assert history["ok"] and len(history["commits"]) == 2
    diff = bridge.git_load_diff(str(repo), left="HEAD~1", right="HEAD")
    assert diff["ok"] and "f2.txt" in diff["diff"]["raw_text"]


def test_git_state_changed_event_dispatched(bridge, tmp_path):
    repo = make_repo(tmp_path, commits=1)
    fake = _FakeWindow()
    bridge._window = fake
    assert bridge.git_get_status(str(repo))["ok"]
    time.sleep(0.2)
    # 外部变化
    make_commit(repo, "external")
    deadline = time.time() + 3
    seen = False
    while time.time() < deadline:
        with fake.lock:
            if any("git_state_changed" in e for e in fake.events):
                seen = True
                break
        time.sleep(0.05)
    assert seen, "外部变化应经 _js_queue 派发 git_state_changed 事件"
