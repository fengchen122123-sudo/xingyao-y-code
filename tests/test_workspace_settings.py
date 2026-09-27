"""core/workspace_settings.py（项目级 KV 存储）的行为测试。"""

import json

import pytest

from core import workspace_settings


def test_set_then_get_roundtrip_and_merge(tmp_path):
    assert workspace_settings.get_workspace_setting(tmp_path, "k") is None
    assert workspace_settings.set_workspace_setting(tmp_path, "k", {"a": 1}) is True
    assert workspace_settings.get_workspace_setting(tmp_path, "k") == {"a": 1}
    # 合并语义：写入第二个键不得覆盖第一个
    workspace_settings.set_workspace_setting(tmp_path, "k2", 7)
    assert workspace_settings.get_workspace_setting(tmp_path, "k") == {"a": 1}
    assert workspace_settings.get_workspace_setting(tmp_path, "k2") == 7


def test_missing_file_or_key_returns_default(tmp_path):
    assert workspace_settings.get_workspace_setting(tmp_path, "nope", "dft") == "dft"
    missing = tmp_path / "not-created-yet"
    # 目录不存在：读路径静默回退（绝不让回合失败）
    assert workspace_settings.get_workspace_setting(missing, "k", "dft") == "dft"


def test_corrupt_file_falls_back_and_self_heals_on_write(tmp_path):
    path = tmp_path / ".ycode_tasks" / "settings.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")
    assert workspace_settings.get_workspace_setting(tmp_path, "k", "dft") == "dft"
    workspace_settings.set_workspace_setting(tmp_path, "k", 1)
    assert json.loads(path.read_text(encoding="utf-8")) == {"k": 1}


def test_relative_workdir_is_rejected():
    with pytest.raises(ValueError):
        workspace_settings.set_workspace_setting("relative/path", "k", 1)


def test_blank_key_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        workspace_settings.get_workspace_setting(tmp_path, "   ")
    with pytest.raises(ValueError):
        workspace_settings.set_workspace_setting(tmp_path, "", 1)
