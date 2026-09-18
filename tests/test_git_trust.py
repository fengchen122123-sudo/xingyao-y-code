"""仓库信任策略测试（规格 §4.5）。"""

import json
import os

import pytest

from core.git import trust as trust_mod
from core.git.repository import resolve_repository
from core.git.runner import GitRunner
from tests.git_fixtures import make_repo, run_git


def _identity(tmp_path, name="repo"):
    repo = make_repo(tmp_path, name=name)
    return resolve_repository(str(repo), GitRunner()), repo


def _fingerprint(identity, repo):
    return trust_mod.collect_security_fingerprint(GitRunner(), identity)


def test_default_untrusted_and_allow_once(tmp_path):
    identity, repo = _identity(tmp_path)
    fp = _fingerprint(identity, repo)
    assert trust_mod.check_trust(identity, fp) == "untrusted"
    trust_mod.allow_once(identity, fp)
    assert trust_mod.check_trust(identity, fp) == "allowed_once"


def test_trust_repository_persists_and_atomic(tmp_path):
    identity, repo = _identity(tmp_path)
    fp = _fingerprint(identity, repo)
    trust_mod.trust_repository(identity, fp)
    store = trust_mod.load_trust_store()
    repo_id = trust_mod.repository_id(identity)
    assert repo_id in store.get("repos", {})
    assert store["repos"][repo_id]["fingerprint"] == fp
    assert "hooks" not in json.dumps(store)  # 不保存 Hook 内容（§4.5）
    assert trust_mod.check_trust(identity, fp) == "trusted"


def test_security_config_change_invalidates_trust(tmp_path):
    identity, repo = _identity(tmp_path)
    fp1 = _fingerprint(identity, repo)
    trust_mod.trust_repository(identity, fp1)
    # 写入 core.hooksPath 指向外部脚本 → 指纹必须变化 → 信任失效
    hook = repo / "evil-hook"
    hook.write_text("#!/bin/sh\nexit 0\n")
    run_git(repo, "config", "core.hooksPath", str(hook))
    fp2 = _fingerprint(identity, repo)
    assert fp2 != fp1
    assert trust_mod.check_trust(identity, fp2) == "untrusted"
    # filter / credential helper 变化同样进指纹
    run_git(repo, "config", "filter.lfs.clean", "git-lfs clean -- %f")
    assert _fingerprint(identity, repo) != fp2
    run_git(repo, "config", "credential.helper", "!f() { evil; }; f")
    assert _fingerprint(identity, repo) != fp2


def test_repository_id_binds_common_dir_and_worktree(tmp_path):
    identity, repo = _identity(tmp_path)
    other_identity, _other = _identity(tmp_path, name="repo2")
    assert trust_mod.repository_id(identity) != trust_mod.repository_id(other_identity)


def test_trust_store_path_uses_ycode_dir(tmp_path):
    import config

    path = trust_mod.trust_store_path()
    assert str(path).startswith(str(config.YCODE_DIR))
    assert os.path.basename(path) == "git_trust.json"
