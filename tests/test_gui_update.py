import json
import os
import tempfile
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from core.updater import (
    compare_versions,
    cleanup_old_installers,
    fetch_latest_release,
    get_update_cache_dir,
    launch_installer,
    parse_version,
    parse_release_sha256,
    _collect_split_parts,
)
from core.version import __version__ as YCODE_VERSION
from gui_bridge import ApiBridge


@pytest.fixture(autouse=True)
def _no_retry_backoff():
    with patch("core.updater.RETRY_DELAYS", ()):
        yield


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
    mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
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
    fake_release = {
        "tag_name": f"v{YCODE_VERSION}",
        "name": f"Y-CODE v{YCODE_VERSION}",
        "body": "最新版",
        "assets": [],
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")
        assert res["ok"] is True
        assert res["has_update"] is False
        assert res["latest_version"] == YCODE_VERSION


def test_cleanup_old_installers(tmp_path, monkeypatch):
    monkeypatch.setattr("core.updater.get_update_cache_dir", lambda: str(tmp_path))
    dummy_exe = os.path.join(str(tmp_path), "test_old_setup.exe")
    dummy_tmp = os.path.join(str(tmp_path), "Y-CODE-Setup-v9.9.9.exe.downloading")
    with open(dummy_exe, "w") as f:
        f.write("dummy")
    with open(dummy_tmp, "w") as f:
        f.write("dummy")
    old_ts = time.time() - 30 * 24 * 3600
    os.utime(dummy_exe, (old_ts, old_ts))

    assert os.path.exists(dummy_exe)
    assert os.path.exists(dummy_tmp)

    cleaned = cleanup_old_installers(current_version="1.2.5", keep_days=7)
    assert cleaned == 2
    assert not os.path.exists(dummy_exe)
    assert not os.path.exists(dummy_tmp)


def test_launch_installer_missing_file():
    assert launch_installer("C:/non_existent_path_123.exe") is False


def test_launch_installer_starts_wizard_without_deleting_package(tmp_path):
    installer = os.path.join(str(tmp_path), "Y-CODE-Setup-v1.2.6.exe")
    with open(installer, "wb") as f:
        f.write(b"MZ" + b"\x00" * 64)

    norm_path = os.path.normpath(installer)
    with patch("subprocess.Popen") as mock_popen:
        assert launch_installer(installer) is True

    assert mock_popen.call_count == 1
    cmd = mock_popen.call_args[0][0]
    assert cmd == f'start /wait "" "{norm_path}"'
    assert "del" not in cmd
    assert "&" not in cmd
    assert os.path.exists(installer)


def test_gui_bridge_update_api():
    bridge = ApiBridge()
    status = bridge.has_active_tasks()
    assert status["ok"] is True
    assert "busy" in status
    assert "has_turns" in status


def test_gui_bridge_has_active_tasks_fail_closed():
    bridge = ApiBridge()
    with patch("gui_bridge.ApiBridge.has_active_turns", side_effect=RuntimeError("boom")):
        status = bridge.has_active_tasks()
        assert status["busy"] is True
        assert status.get("ok") is False


def test_start_download_rejects_invalid_version():
    from core.updater import UpdateDownloader

    downloader = UpdateDownloader()
    called = {"error": False}

    def _on_error(msg):
        called["error"] = True

    started = downloader.start_download(
        download_url="https://example.com/setup.exe",
        target_version='1.2.6"&calc.exe&"',
        on_progress=lambda d: None,
        on_complete=lambda p: None,
        on_error=_on_error,
    )
    assert started is False
    assert called["error"] is True
    assert downloader.is_downloading is False

    started = downloader.start_download(
        download_url="https://example.com/setup.exe",
        target_version="../evil",
        on_progress=lambda d: None,
        on_complete=lambda p: None,
        on_error=_on_error,
    )
    assert started is False


def test_start_download_rejects_truncated_download():
    from core.updater import UpdateDownloader
    import threading

    cache_dir = get_update_cache_dir()
    dest_path = os.path.join(cache_dir, "Y-CODE-Setup-v9.9.9.exe")
    if os.path.exists(dest_path):
        os.remove(dest_path)

    class _FakeResp:
        headers = {"Content-Length": "1024"}

        def __init__(self):
            self._reads = 0

        def read(self, n):
            if self._reads > 0:
                return b""
            self._reads += 1
            return b"x" * 100

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}

    with patch("urllib.request.urlopen", return_value=_FakeResp()):
        started = downloader.start_download(
            download_url="https://example.com/setup.exe",
            target_version="9.9.9",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=True), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "err" in result
    assert "不完整" in result["err"] or "incomplete" in result["err"].lower()
    assert not os.path.exists(dest_path)


def test_parse_release_sha256():
    body = "## 更新内容\n- 新功能\n\nSHA256: ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789"
    assert parse_release_sha256(body) == "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert parse_release_sha256("SHA256：ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789")
    assert parse_release_sha256("") == ""
    assert parse_release_sha256("SHA256: not-a-hash") == ""


def test_collect_split_parts_full_sequence():
    assets = [
        {"name": "Y-code-1.2.6.exe.part2", "size": 2},
        {"name": "Y-code-1.2.6.exe.part1", "size": 1},
        {"name": "Y-code-1.2.6.exe.part3", "size": 3},
    ]
    complete = _collect_split_parts(assets)
    assert list(complete) == ["Y-code-1.2.6.exe"]
    assert [p["name"] for p in complete["Y-code-1.2.6.exe"]] == [
        "Y-code-1.2.6.exe.part1",
        "Y-code-1.2.6.exe.part2",
        "Y-code-1.2.6.exe.part3",
    ]


def test_collect_split_parts_rejects_gapped_sequence():
    assets = [
        {"name": "a.exe.part1", "size": 1},
        {"name": "a.exe.part3", "size": 3},
    ]
    assert _collect_split_parts(assets) == {}


def test_collect_split_parts_groups_coexisting_files():
    assets = [
        {"name": "Y-code-1.2.6.exe.part1", "size": 1},
        {"name": "Y-code-cli-1.2.6.exe.part1", "size": 1},
        {"name": "Y-code-1.2.6.exe.part2", "size": 2},
        {"name": "Y-code-cli-1.2.6.exe.part2", "size": 2},
    ]
    complete = _collect_split_parts(assets)
    assert set(complete) == {"Y-code-1.2.6.exe", "Y-code-cli-1.2.6.exe"}


def test_pick_main_installer_group_prefers_gui_installer():
    from core.updater import _pick_main_installer_group

    complete = {
        "Y-code-cli-1.2.6.exe": [{"name": "Y-code-cli-1.2.6.exe.part1"}],
        "Y-code-1.2.6.exe": [{"name": "Y-code-1.2.6.exe.part1"}],
    }
    base, parts = _pick_main_installer_group(complete, "1.2.6")
    assert base == "Y-code-1.2.6.exe"
    assert parts[0]["name"] == "Y-code-1.2.6.exe.part1"

    complete2 = {
        "Y-code-cli-1.2.6.exe": [{"name": "c1"}],
        "Y-code-setup.exe": [{"name": "m1"}],
    }
    base2, _ = _pick_main_installer_group(complete2, "9.9.9")
    assert base2 == "Y-code-setup.exe"

    base3, _ = _pick_main_installer_group(
        {"Y-code-cli-1.2.6.exe": [{"name": "c1"}]}, "1.2.6"
    )
    assert base3 == "Y-code-cli-1.2.6.exe"

    assert _pick_main_installer_group({}, "1.2.6") is None


def test_pick_main_installer_exe_prefers_gui_installer():
    from core.updater import _pick_main_installer_exe

    assets = [
        {"name": "Y-code-cli-1.2.6.exe", "url": "cli"},
        {"name": "Y-code-1.2.6.exe", "url": "main"},
    ]
    assert _pick_main_installer_exe(assets, "1.2.6")["url"] == "main"

    assert _pick_main_installer_exe(list(reversed(assets)), "1.2.6")["url"] == "main"

    assert _pick_main_installer_exe(assets[:1], "1.2.6")["url"] == "cli"
    assert _pick_main_installer_exe([], "1.2.6") is None


def test_pick_main_installer_group_prefers_new_brand_name_either_order():
    from core.updater import _pick_main_installer_group

    main_parts = [
        {"name": "XingYaoCode-1.2.28.exe.part1", "size": 1},
        {"name": "XingYaoCode-1.2.28.exe.part2", "size": 2},
        {"name": "XingYaoCode-1.2.28.exe.part3", "size": 3},
    ]
    cli_parts = [
        {"name": "Y-code-cli-1.2.28.exe.part1", "size": 1},
        {"name": "Y-code-cli-1.2.28.exe.part2", "size": 2},
        {"name": "Y-code-cli-1.2.28.exe.part3", "size": 3},
    ]

    base, parts = _pick_main_installer_group(
        {"XingYaoCode-1.2.28.exe": main_parts, "Y-code-cli-1.2.28.exe": cli_parts},
        "1.2.28",
    )
    assert base == "XingYaoCode-1.2.28.exe"
    assert [p["name"] for p in parts] == [p["name"] for p in main_parts]

    base2, parts2 = _pick_main_installer_group(
        {"Y-code-cli-1.2.28.exe": cli_parts, "XingYaoCode-1.2.28.exe": main_parts},
        "1.2.28",
    )
    assert base2 == "XingYaoCode-1.2.28.exe"
    assert [p["name"] for p in parts2] == [p["name"] for p in main_parts]


def test_pick_main_installer_group_uses_new_brand_prefix_when_version_mismatch():
    from core.updater import _pick_main_installer_group

    complete = {
        "Y-code-cli-9.9.9.exe": [{"name": "Y-code-cli-9.9.9.exe.part1"}],
        "XingYaoCode-9.9.9.exe": [{"name": "XingYaoCode-9.9.9.exe.part1"}],
    }
    base, _ = _pick_main_installer_group(complete, "1.2.28")
    assert base == "XingYaoCode-9.9.9.exe"


def test_pick_main_installer_group_accepts_legacy_ycode_name():
    from core.updater import _pick_main_installer_group

    parts = [{"name": "Y-code-1.2.28.exe.part1", "size": 1},
             {"name": "Y-code-1.2.28.exe.part2", "size": 2}]
    base, picked = _pick_main_installer_group({"Y-code-1.2.28.exe": parts}, "1.2.28")
    assert base == "Y-code-1.2.28.exe"
    assert picked is parts


def test_pick_main_installer_group_keeps_unbranded_fallback():
    from core.updater import _pick_main_installer_group

    parts = [{"name": "installer.exe.part1"}, {"name": "installer.exe.part2"}]
    base, picked = _pick_main_installer_group({"installer.exe": parts}, "1.2.28")
    assert base == "installer.exe"
    assert picked is parts


def test_pick_main_installer_exe_prefers_new_brand_name():
    from core.updater import _pick_main_installer_exe

    assets = [
        {"name": "Y-code-cli-1.2.28.exe", "url": "cli"},
        {"name": "XingYaoCode-1.2.28.exe", "url": "main"},
    ]
    assert _pick_main_installer_exe(assets, "1.2.28")["url"] == "main"
    assert _pick_main_installer_exe(list(reversed(assets)), "1.2.28")["url"] == "main"


def test_pick_main_installer_exe_accepts_legacy_ycode_name():
    from core.updater import _pick_main_installer_exe

    assets = [{"name": "Y-code-1.2.28.exe", "url": "legacy"}]
    assert _pick_main_installer_exe(assets, "1.2.28")["url"] == "legacy"


def test_fetch_latest_release_picks_new_brand_split_group_either_order():
    main_assets = [
        {"name": "XingYaoCode-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/m1", "size": 100},
        {"name": "XingYaoCode-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/m2", "size": 80},
    ]
    cli_assets = [
        {"name": "Y-code-cli-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/c1", "size": 60},
        {"name": "Y-code-cli-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/c2", "size": 60},
    ]
    for assets in (cli_assets + main_assets, main_assets + cli_assets):
        fake_release = {
            "tag_name": "v99.0.0",
            "name": "Y-CODE v99.0.0",
            "body": "- 分卷发布",
            "assets": list(assets),
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = fetch_latest_release(repo="mock/repo")

        assert res["is_split"] is True
        assert res["asset_name"] == "XingYaoCode-99.0.0.exe"
        assert res["part_urls"] == ["https://gitee.com/m1", "https://gitee.com/m2"]
        assert res["asset_size"] == 180


def test_fetch_latest_release_with_split_parts():
    sha = "a" * 64
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0",
        "body": f"## 更新内容\n- 分卷发布\n\nSHA256: {sha}",
        "assets": [
            {"name": "Y-code-cli-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/cli1", "size": 60},
            {"name": "Y-code-cli-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/cli2", "size": 60},
            {"name": "Y-code-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/p1", "size": 100},
            {"name": "Y-code-99.0.0.exe.part3", "browser_download_url": "https://gitee.com/p3", "size": 50},
            {"name": "Y-code-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/p2", "size": 80},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["ok"] is True
    assert res["is_split"] is True
    assert res["part_count"] == 3
    # 必须是主安装包分卷，绝不能是排在前面的 CLI 组
    assert res["part_urls"] == ["https://gitee.com/p1", "https://gitee.com/p2", "https://gitee.com/p3"]
    assert res["asset_size"] == 230
    assert res["asset_name"] == "Y-code-99.0.0.exe"
    assert res["sha256"] == sha
    assert "releases/tag" in res["download_url"]


def test_fetch_latest_release_single_exe_prefers_main_over_cli():
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0",
        "body": "- 小安装包",
        "assets": [
            {"name": "Y-code-cli-99.0.0.exe", "browser_download_url": "https://gitee.com/cli", "size": 40},
            {"name": "Y-code-99.0.0.exe", "browser_download_url": "https://gitee.com/main", "size": 90},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["is_split"] is False
    assert res["download_url"] == "https://gitee.com/main"
    assert res["asset_name"] == "Y-code-99.0.0.exe"
    assert res["asset_size"] == 90


def test_fetch_latest_release_single_exe_keeps_compat():
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0",
        "body": "- 小安装包",
        "assets": [
            {"name": "Y-code-99.0.0.exe", "browser_download_url": "https://gitee.com/dl", "size": 40},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([fake_release]).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["is_split"] is False
    assert res["part_urls"] == []
    assert res["download_url"] == "https://gitee.com/dl"
    assert res["sha256"] == ""


def test_fetch_latest_release_skips_prerelease_entry():
    fake_releases = [
        {
            "tag_name": "v99.1.0",
            "name": "Y-CODE v99.1.0 预览",
            "body": "- 预发布",
            "prerelease": True,
            "assets": [],
        },
        {
            "tag_name": "v99.0.0",
            "name": "Y-CODE v99.0.0",
            "body": "- 正式版",
            "prerelease": False,
            "assets": [
                {"name": "Y-code-99.0.0.exe", "browser_download_url": "https://gitee.com/dl", "size": 40},
            ],
        },
    ]
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_releases).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["ok"] is True
    assert res["has_update"] is True
    assert res["latest_version"] == "99.0.0"
    assert res["download_url"] == "https://gitee.com/dl"


def test_fetch_latest_release_empty_list():
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps([]).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["ok"] is True
    assert res["has_update"] is False
    assert "暂无更新" in res["msg"]


class _FakePartResp:

    def __init__(self, payload: bytes, content_type: str = "application/octet-stream"):
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": content_type,
        }
        self._payload = payload
        self._pos = 0

    def read(self, n):
        chunk = self._payload[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_download_rejects_html_error_page():
    from core.updater import UpdateDownloader
    import threading

    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v9.9.9.exe")
    if os.path.exists(dest):
        os.remove(dest)

    html_payload = b"<!DOCTYPE html><html><head><title>Login</title></head><body>Please login</body></html>"
    responses = [_FakePartResp(html_payload, content_type="text/html; charset=utf-8")]

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}

    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="https://gitee.com/fake.exe",
            target_version="9.9.9",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "err" in result
    assert "html" in result["err"].lower() or "登录" in result["err"] or "限流" in result["err"]
    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v9.9.9.exe")
    assert not os.path.exists(dest)


def test_download_recovers_after_transient_html_with_retry():
    from core.updater import UpdateDownloader
    import threading

    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v9.9.7.exe")
    if os.path.exists(dest):
        os.remove(dest)

    html_payload = b"<html>login</html>"
    real_payload = b"MZ" + b"\x00" * (2 * 1024 * 1024)
    responses = [
        _FakePartResp(html_payload, content_type="text/html; charset=utf-8"),
        _FakePartResp(real_payload),
    ]
    attempts = {"n": 0}

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}

    def _urlopen(req, timeout=None):
        attempts["n"] += 1
        return responses.pop(0)

    with patch("urllib.request.urlopen", side_effect=_urlopen), \
         patch("core.updater.RETRY_DELAYS", (0,)):
        started = downloader.start_download(
            download_url="https://gitee.com/fake.exe",
            target_version="9.9.7",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "ok" in result and "err" not in result, result
    assert attempts["n"] == 2
    assert os.path.exists(dest)
    assert os.path.getsize(dest) == len(real_payload)
    os.remove(dest)


def test_download_rejects_suspiciously_small_file():
    from core.updater import UpdateDownloader
    import threading

    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v9.9.8.exe")
    if os.path.exists(dest):
        os.remove(dest)

    tiny_payload = b"x" * 500
    responses = [_FakePartResp(tiny_payload)]

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}

    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="https://gitee.com/fake.exe",
            target_version="9.9.8",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "err" in result
    assert "过小" in result["err"] or "异常" in result["err"] or "invalid" in result["err"].lower()
    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v9.9.8.exe")
    assert not os.path.exists(dest)


def test_launch_installer_rejects_non_pe_file():
    fake_exe = os.path.join(tempfile.gettempdir(), "fake_installer.html")
    with open(fake_exe, "wb") as f:
        f.write(b"<!DOCTYPE html><html><body>not an exe</body></html>")

    try:
        assert launch_installer(fake_exe) is False
    finally:
        os.remove(fake_exe)


def test_split_download_concatenates_and_verifies():
    from core.updater import UpdateDownloader, _sha256_file
    import hashlib
    import threading

    # 测试 payload 必须 > 1MB，否则会触发大小异常校验
    part1 = b"A" * (1024 * 1024 + 100)
    part2 = b"B" * (1024 * 1024 + 50)
    full = part1 + part2
    good_sha = hashlib.sha256(full).hexdigest()

    cache_dir = get_update_cache_dir()
    dest_path = os.path.join(cache_dir, "Y-CODE-Setup-v8.8.8.exe")
    if os.path.exists(dest_path):
        os.remove(dest_path)

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}
    responses = [_FakePartResp(part1), _FakePartResp(part2)]

    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="",
            target_version="8.8.8",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
            part_urls=["https://gitee.com/p1", "https://gitee.com/p2"],
            expected_sha256=good_sha,
            total_size_hint=len(full),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "ok" in result and "err" not in result
    with open(result["ok"], "rb") as f:
        assert f.read() == full
    os.remove(result["ok"])


def test_split_download_rejects_sha256_mismatch():
    from core.updater import UpdateDownloader
    import threading

    part1 = b"A" * (1024 * 1024 + 100)
    part2 = b"B" * (1024 * 1024 + 50)

    cache_dir = get_update_cache_dir()
    dest_path = os.path.join(cache_dir, "Y-CODE-Setup-v7.7.7.exe")
    tmp_path = os.path.join(cache_dir, "Y-CODE-Setup-v7.7.7.exe.downloading")
    for p in (dest_path, tmp_path):
        if os.path.exists(p):
            os.remove(p)

    downloader = UpdateDownloader()
    done = threading.Event()
    result = {}
    responses = [_FakePartResp(part1), _FakePartResp(part2)]

    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="",
            target_version="7.7.7",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
            part_urls=["https://gitee.com/p1", "https://gitee.com/p2"],
            expected_sha256="0" * 64,
            total_size_hint=150,
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "err" in result and "ok" not in result
    assert "校验失败" in result["err"] or "SHA256" in result["err"]
    assert not os.path.exists(dest_path)
    assert not os.path.exists(tmp_path)


class _ChunkedPartResp:

    def __init__(self, payload: bytes, content_type: str = "application/octet-stream"):
        self.headers = {"Content-Type": content_type}
        self._payload = payload
        self._pos = 0

    def read(self, n):
        chunk = self._payload[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_split_download_survives_missing_content_length():
    from core.updater import UpdateDownloader
    import hashlib
    import threading

    part1 = b"A" * (1024 * 1024 + 100)
    part2 = b"B" * (1024 * 1024 + 50)
    full = part1 + part2
    good_sha = hashlib.sha256(full).hexdigest()

    cache_dir = get_update_cache_dir()
    dest_path = os.path.join(cache_dir, "Y-CODE-Setup-v4.4.4.exe")
    tmp_path = os.path.join(cache_dir, "Y-CODE-Setup-v4.4.4.exe.downloading")
    for p in (dest_path, tmp_path):
        if os.path.exists(p):
            os.remove(p)

    responses = [_ChunkedPartResp(part1), _ChunkedPartResp(part2)]
    progress_events = []
    done = threading.Event()
    result = {}

    downloader = UpdateDownloader()
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="",
            target_version="4.4.4",
            on_progress=lambda d: progress_events.append(dict(d)),
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
            part_urls=["https://gitee.com/p1", "https://gitee.com/p2"],
            expected_sha256=good_sha,
            total_size_hint=0,
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "ok" in result and "err" not in result, result
    with open(result["ok"], "rb") as f:
        assert f.read() == full
    assert progress_events
    assert progress_events[-1]["percent"] == 100
    assert progress_events[-1]["downloaded"] == len(full)
    os.remove(result["ok"])


def test_split_download_reports_part_progress():
    from core.updater import UpdateDownloader
    import threading

    part1 = b"A" * (1024 * 1024 + 60)
    part2 = b"B" * (1024 * 1024 + 40)
    responses = [_FakePartResp(part1), _FakePartResp(part2)]
    progress_events = []
    done = threading.Event()

    downloader = UpdateDownloader()
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="",
            target_version="6.6.6",
            on_progress=lambda d: progress_events.append(dict(d)),
            on_complete=lambda p: done.set(),
            on_error=lambda m: done.set(),
            part_urls=["https://gitee.com/p1", "https://gitee.com/p2"],
            total_size_hint=len(part1) + len(part2),
        )
        assert started is True
        assert done.wait(timeout=10)

    assert progress_events
    last = progress_events[-1]
    assert last["part_count"] == 2
    assert last["percent"] == 100
    assert last["downloaded"] == len(part1) + len(part2)
    assert any(e["part_index"] == 2 for e in progress_events)
    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v6.6.6.exe")
    if os.path.exists(dest):
        os.remove(dest)


class _FlakyPartResp:

    def __init__(self, payload: bytes, fail_after: int):
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": "application/octet-stream",
        }
        self._payload = payload
        self._pos = 0
        self._fail_after = fail_after

    def read(self, n):
        if self._pos >= self._fail_after:
            raise urllib.error.URLError("connection reset by peer")
        end = min(len(self._payload), self._pos + n)
        chunk = self._payload[self._pos : end]
        self._pos = end
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_retry_progress_never_exceeds_total():
    from core.updater import UpdateDownloader
    import threading

    payload = b"R" * (3 * 1024 * 1024)
    responses = [
        _FlakyPartResp(payload, fail_after=1024 * 1024),
        _FakePartResp(payload),
    ]
    progress_events = []
    done = threading.Event()
    result = {}

    downloader = UpdateDownloader()
    with patch("core.updater.RETRY_DELAYS", (0,)), patch(
        "urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)
    ):
        started = downloader.start_download(
            download_url="https://gitee.com/fake/x.exe",
            target_version="8.8.8",
            on_progress=lambda d: progress_events.append(dict(d)),
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
            total_size_hint=len(payload),
        )
        assert started is True
        assert done.wait(timeout=15)

    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v8.8.8.exe")
    try:
        assert "err" not in result, result
        assert progress_events
        assert all(0 <= e["percent"] <= 100 for e in progress_events), [
            e["percent"] for e in progress_events if e["percent"] > 100
        ]
        assert all(e["downloaded"] <= len(payload) for e in progress_events), [
            e["downloaded"] for e in progress_events if e["downloaded"] > len(payload)
        ]
        last = progress_events[-1]
        assert last["percent"] == 100
        assert last["downloaded"] == len(payload)
        with open(result["ok"], "rb") as f:
            assert f.read() == payload
    finally:
        for p in (dest, dest + ".downloading"):
            if os.path.exists(p):
                os.remove(p)


def test_single_part_url_falls_back_to_direct_download():
    from core.updater import UpdateDownloader
    import threading

    payload = b"C" * (1024 * 1024 + 30)
    responses = [_FakePartResp(payload)]
    done = threading.Event()
    result = {}

    downloader = UpdateDownloader()
    with patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: responses.pop(0)):
        started = downloader.start_download(
            download_url="",
            target_version="5.5.5",
            on_progress=lambda d: None,
            on_complete=lambda p: (result.update(ok=p), done.set()),
            on_error=lambda m: (result.update(err=m), done.set()),
            part_urls=["https://gitee.com/only"],
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "ok" in result
    with open(result["ok"], "rb") as f:
        assert f.read() == payload
    os.remove(result["ok"])


def test_gui_bridge_download_update_passes_split_args():
    bridge = ApiBridge()
    captured = {}

    def _fake_start(download_url, target_version, on_progress, on_complete, on_error,
                    part_urls=None, expected_sha256="", total_size_hint=0):
        captured.update(
            url=download_url, version=target_version, parts=part_urls,
            sha=expected_sha256, total=total_size_hint,
        )
        return True

    with patch("gui_bridge.updater_downloader.start_download", side_effect=_fake_start):
        res = bridge.download_update(
            "https://gitee.com/releases/tag/v1.2.6",
            "1.2.6",
            part_urls=["https://gitee.com/p1", "https://gitee.com/p2"],
            sha256="B" * 64,
            total_size=292 * 1024 * 1024,
        )

    assert res["ok"] is True
    assert captured["parts"] == ["https://gitee.com/p1", "https://gitee.com/p2"]
    assert captured["sha"] == "b" * 64
    assert captured["total"] == 292 * 1024 * 1024
    assert captured["version"] == "1.2.6"
