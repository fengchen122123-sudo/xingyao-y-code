"""
tests/test_gui_update.py
========================
测试 Gitee Releases 检查更新、版本号比对、流式下载与安装清理逻辑。
"""

import json
import os
import tempfile
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from core.updater import (
    compare_versions,
    cleanup_old_installers,
    fetch_latest_release,
    get_update_cache_dir,
    launch_installer_and_cleanup,
    parse_version,
)
from core.version import __version__ as YCODE_VERSION
from gui_bridge import ApiBridge


def test_parse_and_compare_versions():
    assert parse_version("v1.2.6") == (1, 2, 6)
    assert parse_version("1.2.5") == (1, 2, 5)
    assert parse_version("v2.0.0-beta1") == (2, 0, 0, 1)
    assert parse_version("") == (0, 0, 0)

    assert compare_versions("1.2.6", "1.2.5") == 1
    assert compare_versions("v1.2.5", "1.2.5") == 0
    assert compare_versions("1.2.4", "1.2.5") == -1
    assert compare_versions("1.3.0", "1.2.99") == 1
    assert compare_versions("2.0.0", "1.9.9") == 1
    assert compare_versions("1.2.5.1", "1.2.5") == 1


def test_fetch_latest_release_404():
    """当 Gitee 仓库暂无 Release (404) 时，应优雅返回 has_update: False。"""
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_err = urllib.error.HTTPError(
            url="http://gitee.example/api",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = mock_err
        res = fetch_latest_release(repo="mock/repo")
        assert res["ok"] is True
        assert res["has_update"] is False
        assert "暂无更新" in res["msg"]


def test_fetch_latest_release_with_update():
    """当 Gitee 返回更高版本且包含 exe 附件时，应提取出下载直链与更新信息。"""
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0 大版本更新",
        "body": "- 新增全自动更新功能\n- 修复已知问题",
        "assets": [
            {
                "name": "Y-CODE-Setup-v99.0.0.exe",
                "browser_download_url": "https://gitee.com/download/Y-CODE-Setup.exe",
                "size": 52428800,
            }
        ],
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")
        assert res["ok"] is True
        assert res["has_update"] is True
        assert res["latest_version"] == "99.0.0"
        assert res["download_url"] == "https://gitee.com/download/Y-CODE-Setup.exe"
        assert res["asset_name"] == "Y-CODE-Setup-v99.0.0.exe"
        assert res["asset_size"] == 52428800
        assert "全自动更新" in res["release_notes"]


def test_fetch_latest_release_same_version():
    """当远程版本等于当前版本时，has_update 应为 False。"""
    fake_release = {
        "tag_name": f"v{YCODE_VERSION}",
        "name": f"Y-CODE v{YCODE_VERSION}",
        "body": "最新版",
        "assets": [],
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")
        assert res["ok"] is True
        assert res["has_update"] is False
        assert res["latest_version"] == YCODE_VERSION


def test_cleanup_old_installers():
    """测试旧安装包与临时文件清理。"""
    cache_dir = get_update_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    dummy_exe = os.path.join(cache_dir, "test_old_setup.exe")
    dummy_tmp = os.path.join(cache_dir, "test_download.tmp")
    with open(dummy_exe, "w") as f:
        f.write("dummy")
    with open(dummy_tmp, "w") as f:
        f.write("dummy")

    assert os.path.exists(dummy_exe)
    assert os.path.exists(dummy_tmp)

    cleaned = cleanup_old_installers()
    assert cleaned >= 2
    assert not os.path.exists(dummy_exe)
    assert not os.path.exists(dummy_tmp)


def test_launch_installer_missing_file():
    assert launch_installer_and_cleanup("C:/non_existent_path_123.exe") is False


def test_gui_bridge_update_api():
    bridge = ApiBridge()
    # has_active_tasks 结构
    status = bridge.has_active_tasks()
    assert status["ok"] is True
    assert "busy" in status
    assert "has_turns" in status
