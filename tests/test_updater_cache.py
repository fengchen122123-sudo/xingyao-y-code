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
    monkeypatch.setattr("core.updater.get_update_cache_dir", lambda: str(tmp_path))
    return tmp_path


def _touch(directory, name, payload=b"MZ" + b"\x00" * 32):
    path = directory / name
    path.write_bytes(payload)
    return path


def _pe_payload(size: int = _MIN_INSTALLER_BYTES + 512) -> bytes:
    return b"MZ" + b"\x00" * (size - 2)


def test_cleanup_removes_stale_and_keeps_newer(cache_dir):
    old = _touch(cache_dir, "Y-CODE-Setup-v1.0.0.exe")
    same = _touch(cache_dir, "Y-CODE-Setup-v1.2.5.exe")
    newer = _touch(cache_dir, "Y-CODE-Setup-v9.9.9.exe")
    fragment = _touch(cache_dir, "Y-CODE-Setup-v2.0.0.exe.downloading")

    cleaned = cleanup_old_installers(current_version="1.2.5", keep_days=7)

    assert cleaned == 3
    assert not old.exists()
    assert not same.exists()
    assert not fragment.exists()
    assert newer.exists()


def test_cleanup_keeps_all_exes_when_version_unknown(cache_dir, monkeypatch):
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
    stale = _touch(cache_dir, "leftover_old.bin")
    fresh = _touch(cache_dir, "leftover_fresh.bin")
    old_ts = time.time() - 30 * 24 * 3600
    os.utime(stale, (old_ts, old_ts))

    cleaned = cleanup_old_installers(current_version="1.2.5", keep_days=7)

    assert cleaned == 1
    assert not stale.exists()
    assert fresh.exists()


def test_find_cached_installer_by_name_only(cache_dir):
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", _pe_payload())

    assert find_cached_installer("1.2.6") == str(cached)


def test_find_cached_installer_missing_returns_empty(cache_dir):
    assert find_cached_installer("1.2.6") == ""


def test_find_cached_installer_size_mismatch_deletes_bad_file(cache_dir):
    payload = _pe_payload()
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", payload)

    assert find_cached_installer("1.2.6", expected_size=len(payload) + 1) == ""
    assert not cached.exists()


def test_find_cached_installer_sha256_mismatch_deletes_bad_file(cache_dir):
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", _pe_payload())

    assert find_cached_installer("1.2.6", expected_sha256="0" * 64) == ""
    assert not cached.exists()


def test_find_cached_installer_sha256_match_is_case_insensitive(cache_dir):
    payload = _pe_payload()
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert find_cached_installer("1.2.6", expected_sha256=digest.upper()) == str(cached)
    assert find_cached_installer(
        "1.2.6", expected_sha256=digest, expected_size=len(payload)
    ) == str(cached)
    assert cached.exists()


def test_find_cached_installer_rejects_tiny_file(cache_dir):
    cached = _touch(cache_dir, "Y-CODE-Setup-v1.2.6.exe", b"MZ" + b"\x00" * 32)

    assert find_cached_installer("1.2.6") == ""
    assert not cached.exists()


def test_find_cached_installer_rejects_non_pe_file(cache_dir):
    cached = _touch(
        cache_dir,
        "Y-CODE-Setup-v1.2.6.exe",
        b"<html>" + b"x" * (_MIN_INSTALLER_BYTES + 64),
    )

    assert find_cached_installer("1.2.6") == ""
    assert not cached.exists()


def test_find_cached_installer_rejects_path_traversal(cache_dir):
    outside = cache_dir.parent / "evil.exe"
    outside.write_bytes(b"MZ" + b"A" * 16)

    assert find_cached_installer("../../evil") == ""
    assert outside.exists()