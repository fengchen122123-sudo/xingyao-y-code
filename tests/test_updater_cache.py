"""
tests/test_updater_cache.py
===========================
覆盖「更新包不再无故消失 + 复用已下载包」相关的两个策略函数：
  * cleanup_old_installers：只清理已装上/已过时的包与下载残片，保留更高版本；
  * find_cached_installer：命中并校验缓存安装包（大小 / SHA256），坏包即删。

所有用例都 monkeypatch core.updater.get_update_cache_dir 指向 tmp_path，
绝不触碰真实 %TEMP%/ycode_updates（真实目录里可能有用户待安装的包）。
"""

import hashlib
import os
import time

import pytest

from core.updater import (
    _MIN_INSTALLER_BYTES,
    cleanup_old_installers,
    find_cached_installer,
)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """把更新缓存目录重定向到 tmp_path，隔离真实 %TEMP%/ycode_updates。"""
    monkeypatch.setattr("core.updater.get_update_cache_dir", lambda: str(tmp_path))
    return tmp_path


def _touch(directory, name, payload=b"MZ" + b"\x00" * 32):
    """在隔离缓存目录里写一个假安装包并返回路径对象。"""
    path = directory / name
    path.write_bytes(payload)
    return path


def _pe_payload(size: int = _MIN_INSTALLER_BYTES + 512) -> bytes:
    """构造体积与文件头都像正常安装包的假包（MZ 头 + 填充到 size 字节）。

    find_cached_installer 对缓存包有「最小体积 + MZ 头」兜底，测试命中路径
    必须用这个辅助，否则会被当成未完成下载的残片删掉。
    """
    return b"MZ" + b"\x00" * (size - 2)


# ---------------------------------------------------------------------------
# cleanup_old_installers：更新包不再无故消失
# ---------------------------------------------------------------------------

def test_cleanup_removes_stale_and_keeps_newer(cache_dir):
    """v <= 当前版本的包删除、v > 当前版本的包保留、.downloading 残片删除，
    返回值等于实际清理的条目数。"""
    old = _touch(cache_dir, "Y-CODE-Setup-v1.0.0.exe")
    same = _touch(cache_dir, "Y-CODE-Setup-v1.2.5.exe")
    newer = _touch(cache_dir, "Y-CODE-Setup-v9.9.9.exe")
    fragment = _touch(cache_dir, "Y-CODE-Setup-v2.0.0.exe.downloading")

    cleaned = cleanup_old_installers(current_version="1.2.5", keep_days=7)

    assert cleaned == 3
    assert not old.exists()
    assert not same.exists()
    assert not fragment.exists()
    # 关键：版本高于当前运行版本的包必须留给用户手动安装
    assert newer.exists()


def test_cleanup_keeps_all_exes_when_version_unknown(cache_dir, monkeypatch):
    """当前版本取不到（空串）时视为无法比较：任何 exe 都不删，只清残片。"""
    monkeypatch.setattr("core.version.__version__", "")
    old = _touch(cache_dir, "Y-CODE-Setup-v1.0.0.exe")
    newer = _touch(cache_dir, "Y-CODE-Setup-v9.9.9.exe")
    fragment = _touch(cache_dir, "Y-CODE-Setup-v9.9.9.exe.downloading")

    cleaned = cleanup_old_installers(keep_days=7)

    assert cleaned == 1
    assert old.exists()
    assert newer.exists()
    assert not fragment.exists()


def test_cleanup_removes_expired_non_standard_leftover(cache_dir):
    """命名不合规的残留按 keep_days 老化清理，未超期的保留。"""
    stale = _touch(cache_dir, "leftover_old.bin")
    fresh = _touch(cache_dir, "leftover_fresh.bin")
    old_ts = time.time() - 30 * 24 * 3600
    os.utime(stale, (old_ts, old_ts))

    cleaned = cleanup_old_installers(current_version="1.2.5", keep_days=7)

    assert cleaned == 1
    assert not stale.exists()
    assert fresh.exists()


# ---------------------------------------------------------------------------
# find_cached_installer：复用已下载的包
# ---------------------------------------------------------------------------

def test_find_cached_installer_by_name_only(cache_dir):
    """未给 size/sha 时仅按文件名命中（文件须像正常安装包），返回缓存路径。"""
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", _pe_payload())

    assert find_cached_installer("1.2.6") == str(cached)


def test_find_cached_installer_missing_returns_empty(cache_dir):
    """缓存目录里没有目标版本时返回空串（需重新下载）。"""
    assert find_cached_installer("1.2.6") == ""


def test_find_cached_installer_size_mismatch_deletes_bad_file(cache_dir):
    """大小不符视为不完整下载：返回空串并删除坏包。"""
    payload = _pe_payload()
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", payload)

    assert find_cached_installer("1.2.6", expected_size=len(payload) + 1) == ""
    assert not cached.exists()


def test_find_cached_installer_sha256_mismatch_deletes_bad_file(cache_dir):
    """SHA256 不符（包被篡改/损坏）：返回空串并删除坏包。"""
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", _pe_payload())

    assert find_cached_installer("1.2.6", expected_sha256="0" * 64) == ""
    assert not cached.exists()


def test_find_cached_installer_sha256_match_is_case_insensitive(cache_dir):
    """SHA256 相符（含大写形式）时命中并保留文件。"""
    payload = _pe_payload()
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert find_cached_installer("1.2.6", expected_sha256=digest.upper()) == str(cached)
    assert find_cached_installer(
        "1.2.6", expected_sha256=digest, expected_size=len(payload)
    ) == str(cached)
    assert cached.exists()


def test_find_cached_installer_rejects_tiny_file(cache_dir):
    """体积远小于正常安装包（未完成下载/HTML 错误页）→ 拒绝复用并删除该包。"""
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", b"MZ" + b"\x00" * 32)

    assert find_cached_installer("1.2.6") == ""
    assert not cached.exists()


def test_find_cached_installer_rejects_non_pe_file(cache_dir):
    """体积够大但缺少 MZ 头（HTML 错误页/损坏文件）→ 拒绝复用并删除该包。"""
    cached = _touch(
        cache_dir,
        "Y-CODE-Setup-v1.2.6.exe",
        b"<html>" + b"x" * (_MIN_INSTALLER_BYTES + 64),
    )

    assert find_cached_installer("1.2.6") == ""
    assert not cached.exists()


def test_find_cached_installer_rejects_path_traversal(cache_dir):
    """版本号含路径穿越时必须拒绝，且不得触碰缓存目录外的文件。"""
    outside = cache_dir.parent / "evil.exe"
    outside.write_bytes(b"MZ" + b"A" * 16)

    assert find_cached_installer("../../evil") == ""
    assert outside.exists()