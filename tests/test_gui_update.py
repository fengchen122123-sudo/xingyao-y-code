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
    parse_release_sha256,
    _collect_split_parts,
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


def test_gui_bridge_has_active_tasks_fail_closed():
    """任务查询异常时应失败关闭：返回 busy=True，避免放过运行中的任务被安装中断。"""
    bridge = ApiBridge()
    with patch("gui_bridge.ApiBridge.has_active_turns", side_effect=RuntimeError("boom")):
        status = bridge.has_active_tasks()
        assert status["busy"] is True
        assert status.get("ok") is False


def test_start_download_rejects_invalid_version():
    """target_version 含特殊字符时必须拒绝（防 shell 注入/路径穿越）。"""
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
    """下载字节数与 Content-Length 不符（连接中断）时必须报错，不得落盘视为成功。"""
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
            # 只返回 100 字节后 EOF，模拟连接中断
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


# ============================================================================
# 分卷发布（Gitee 单附件 100MB 上限适配）
# ============================================================================

def test_parse_release_sha256():
    """Release 描述里的 SHA256 标记行能被解析并小写归一。"""
    body = "## 更新内容\n- 新功能\n\nSHA256: ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789"
    assert parse_release_sha256(body) == "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    # 中文冒号 / 大小写混合 / 未发布校验和
    assert parse_release_sha256("SHA256：ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789")
    assert parse_release_sha256("") == ""
    assert parse_release_sha256("SHA256: not-a-hash") == ""


def test_collect_split_parts_full_sequence():
    """完整 1..N 连续分卷按序号升序分组返回。"""
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
    """分卷断号（漏传某片）的组必须整体丢弃，防止拼出残缺 exe。"""
    assets = [
        {"name": "a.exe.part1", "size": 1},
        {"name": "a.exe.part3", "size": 3},
    ]
    assert _collect_split_parts(assets) == {}


def test_collect_split_parts_groups_coexisting_files():
    """主安装包与 CLI 安装包两组分卷共存时各自独立成组。"""
    assets = [
        {"name": "Y-code-1.2.6.exe.part1", "size": 1},
        {"name": "Y-code-cli-1.2.6.exe.part1", "size": 1},
        {"name": "Y-code-1.2.6.exe.part2", "size": 2},
        {"name": "Y-code-cli-1.2.6.exe.part2", "size": 2},
    ]
    complete = _collect_split_parts(assets)
    assert set(complete) == {"Y-code-1.2.6.exe", "Y-code-cli-1.2.6.exe"}


def test_pick_main_installer_group_prefers_gui_installer():
    """多组分卷时必须选主安装包（Y-code-{版本}.exe），绝不能错选 CLI 组。"""
    from core.updater import _pick_main_installer_group

    complete = {
        "Y-code-cli-1.2.6.exe": [{"name": "Y-code-cli-1.2.6.exe.part1"}],
        "Y-code-1.2.6.exe": [{"name": "Y-code-1.2.6.exe.part1"}],
    }
    base, parts = _pick_main_installer_group(complete, "1.2.6")
    assert base == "Y-code-1.2.6.exe"
    assert parts[0]["name"] == "Y-code-1.2.6.exe.part1"

    # 无精确名时回退 Y-code 前缀且非 cli
    complete2 = {
        "Y-code-cli-1.2.6.exe": [{"name": "c1"}],
        "Y-code-setup.exe": [{"name": "m1"}],
    }
    base2, _ = _pick_main_installer_group(complete2, "9.9.9")
    assert base2 == "Y-code-setup.exe"

    # 只有 CLI 组时兜底选它（总比拒绝更新好）
    base3, _ = _pick_main_installer_group(
        {"Y-code-cli-1.2.6.exe": [{"name": "c1"}]}, "1.2.6"
    )
    assert base3 == "Y-code-cli-1.2.6.exe"

    assert _pick_main_installer_group({}, "1.2.6") is None


def test_pick_main_installer_exe_prefers_gui_installer():
    """多个单 exe 附件共存时优先主安装包，避免错下 CLI 安装包。"""
    from core.updater import _pick_main_installer_exe

    assets = [
        {"name": "Y-code-cli-1.2.6.exe", "url": "cli"},
        {"name": "Y-code-1.2.6.exe", "url": "main"},
    ]
    assert _pick_main_installer_exe(assets, "1.2.6")["url"] == "main"

    # CLI 排前面也一样
    assert _pick_main_installer_exe(list(reversed(assets)), "1.2.6")["url"] == "main"

    # 兜底：只有 CLI 时选 CLI
    assert _pick_main_installer_exe(assets[:1], "1.2.6")["url"] == "cli"
    assert _pick_main_installer_exe([], "1.2.6") is None


def test_fetch_latest_release_with_split_parts():
    """分卷 Release：part_urls 按序返回，asset_size 为各片之和，SHA256 被提取。

    同 Release 混入 CLI 安装包分卷时必须选主安装包那组。
    """
    sha = "a" * 64
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0",
        "body": f"## 更新内容\n- 分卷发布\n\nSHA256: {sha}",
        "assets": [
            # CLI 分卷排前面，干扰选择
            {"name": "Y-code-cli-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/cli1", "size": 60},
            {"name": "Y-code-cli-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/cli2", "size": 60},
            {"name": "Y-code-99.0.0.exe.part1", "browser_download_url": "https://gitee.com/p1", "size": 100},
            {"name": "Y-code-99.0.0.exe.part3", "browser_download_url": "https://gitee.com/p3", "size": 50},
            {"name": "Y-code-99.0.0.exe.part2", "browser_download_url": "https://gitee.com/p2", "size": 80},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
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
    # 分卷模式 download_url 回退 Release 页面（不再用于直连下载）
    assert "releases/tag" in res["download_url"]


def test_fetch_latest_release_single_exe_prefers_main_over_cli():
    """单 exe 双附件（主 + CLI）共存时必须挑主安装包。"""
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
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["is_split"] is False
    assert res["download_url"] == "https://gitee.com/main"
    assert res["asset_name"] == "Y-code-99.0.0.exe"
    assert res["asset_size"] == 90


def test_fetch_latest_release_single_exe_keeps_compat():
    """单 exe 附件（≤100MB 或旧版发布）继续走原路径，不受分卷改造影响。"""
    fake_release = {
        "tag_name": "v99.0.0",
        "name": "Y-CODE v99.0.0",
        "body": "- 小安装包",
        "assets": [
            {"name": "Y-code-99.0.0.exe", "browser_download_url": "https://gitee.com/dl", "size": 40},
        ],
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(fake_release).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = fetch_latest_release(repo="mock/repo")

    assert res["is_split"] is False
    assert res["part_urls"] == []
    assert res["download_url"] == "https://gitee.com/dl"
    assert res["sha256"] == ""


class _FakePartResp:
    """模拟单个分卷响应：返回固定字节串并声明 Content-Length。"""

    def __init__(self, payload: bytes):
        self.headers = {"Content-Length": str(len(payload))}
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


def test_split_download_concatenates_and_verifies():
    """分卷下载：顺序拼接还原 exe，SHA256 匹配时才落盘成功。"""
    from core.updater import UpdateDownloader, _sha256_file
    import hashlib
    import threading

    part1 = b"A" * 100
    part2 = b"B" * 50
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
        assert f.read() == full  # 拼接顺序必须严格 part1 + part2
    os.remove(result["ok"])


def test_split_download_rejects_sha256_mismatch():
    """安全底线：拼接结果 SHA256 与官方校验和不一致时必须拒绝安装并清理产物。"""
    from core.updater import UpdateDownloader
    import threading

    part1 = b"A" * 100
    part2 = b"B" * 50

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
            expected_sha256="0" * 64,  # 故意错误的官方校验和
            total_size_hint=150,
        )
        assert started is True
        assert done.wait(timeout=10)

    assert "err" in result and "ok" not in result
    assert "校验失败" in result["err"] or "SHA256" in result["err"]
    assert not os.path.exists(dest_path)   # 产物不落盘
    assert not os.path.exists(tmp_path)    # 临时拼接文件已清理


def test_split_download_reports_part_progress():
    """分卷进度：part_index/part_count 上报正确，percent 按累计字节单调推进。"""
    from core.updater import UpdateDownloader
    import threading

    part1 = b"A" * 60
    part2 = b"B" * 40
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
            total_size_hint=100,
        )
        assert started is True
        assert done.wait(timeout=10)

    assert progress_events
    last = progress_events[-1]
    assert last["part_count"] == 2
    assert last["percent"] == 100
    assert last["downloaded"] == 100
    # 覆盖到第二片时 part_index 必须推进到 2
    assert any(e["part_index"] == 2 for e in progress_events)
    # 清理产物
    dest = os.path.join(get_update_cache_dir(), "Y-CODE-Setup-v6.6.6.exe")
    if os.path.exists(dest):
        os.remove(dest)


def test_single_part_url_falls_back_to_direct_download():
    """单片『分卷』没有拼接意义，自动降级为单文件直连下载。"""
    from core.updater import UpdateDownloader
    import threading

    payload = b"C" * 30
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
    """桥方法：分卷列表/SHA256/总大小透传给下载器。"""
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
