from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

import config
from core import components


class _FakeResponse:

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0
        self.headers = {"Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._offset:]
            self._offset = len(self._data)
            return chunk
        chunk = self._data[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


def _patch_urlopen(monkeypatch, payload: bytes) -> None:

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr(components.urllib.request, "urlopen", fake_urlopen)


def _make_component_dir(pw_dir: Path, version: str = "1.50.0") -> None:
    pw_dir = Path(pw_dir)
    (pw_dir / "playwright" / "driver").mkdir(parents=True, exist_ok=True)
    (pw_dir / "playwright" / "__init__.py").write_text("", encoding="utf-8")
    (pw_dir / "playwright" / "driver" / "node.exe").write_bytes(b"node-driver")
    for dependency in ("greenlet", "pyee"):
        (pw_dir / dependency).mkdir(parents=True, exist_ok=True)
        (pw_dir / dependency / "__init__.py").write_text("", encoding="utf-8")
    (pw_dir / "COMPONENT.json").write_text(
        json.dumps({"playwright_version": version}), encoding="utf-8"
    )


def _build_component_zip(zip_path: Path, version: str) -> bytes:
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(
            "COMPONENT.json",
            json.dumps({"playwright_version": version, "name": "playwright"}),
        )
        archive.writestr("playwright/__init__.py", "")
        archive.writestr("playwright/driver/node.exe", b"node-driver")
        archive.writestr("greenlet/__init__.py", "")
        archive.writestr("pyee/__init__.py", "")
    return zip_path.read_bytes()


def _staging_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.name.startswith(".playwright-extract-")]


@pytest.fixture()
def component_env(tmp_path, monkeypatch):
    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.delenv("YCODE_COMPONENT_DIR", raising=False)
    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
    monkeypatch.setattr(components, "COMPONENT_ROOT", str(root))
    monkeypatch.setattr(components, "PLAYWRIGHT_DIR", str(root / "playwright"))
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(components, "_import_ready", False)
    monkeypatch.setattr(components, "_download_thread", None)
    monkeypatch.setattr(
        components,
        "_state",
        {
            "downloading": False,
            "stage": "",
            "percent": 0,
            "downloaded_bytes": 0,
            "total_bytes": 0,
            "error": "",
        },
    )
    components._cancel_event.clear()
    return root


def test_not_installed_status_and_ensure_raises(component_env, monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)

    assert components.playwright_installed() is False
    assert components.playwright_version() == ""
    status = components.get_status()
    assert status["installed"] is False
    assert status["downloading"] is False
    assert status["dir"] == str(components.PLAYWRIGHT_DIR)
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""

    with pytest.raises(components.ComponentError) as excinfo:
        components.ensure_playwright()
    assert excinfo.value.code == "playwright_component_missing"
    assert "电脑操控" in str(excinfo.value)

    target = os.path.normcase(os.path.abspath(str(components.PLAYWRIGHT_DIR)))
    inserted = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    assert len(inserted) == 1
    with pytest.raises(components.ComponentError):
        components.ensure_playwright()
    inserted = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    assert len(inserted) == 1


def test_installed_component_detected_and_ensure_ok(component_env):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.51.0")

    assert components.playwright_installed() is True
    assert components.playwright_version() == "1.51.0"
    assert components.get_status()["installed"] is True
    components.ensure_playwright()
    assert components._import_ready is True


def test_version_falls_back_to_repo_version_file(component_env):
    pw_dir = Path(components.PLAYWRIGHT_DIR)
    _make_component_dir(pw_dir, version="1.50.0")
    (pw_dir / "COMPONENT.json").unlink()
    (pw_dir / "playwright" / "_repo_version.py").write_text(
        '__version__ = "1.49.1"\n', encoding="utf-8"
    )
    assert components.playwright_version() == "1.49.1"


def test_ensure_inserts_component_dir_once_then_caches(component_env, monkeypatch):
    calls: list[bool] = []

    def fake_try_import() -> bool:
        calls.append(True)
        return len(calls) > 1

    monkeypatch.setattr(components, "_try_import_playwright", fake_try_import)
    components.ensure_playwright()
    assert len(calls) == 2
    target = os.path.normcase(os.path.abspath(str(components.PLAYWRIGHT_DIR)))
    assert [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    components.ensure_playwright()
    assert len(calls) == 2


def test_extract_rejects_path_traversal(component_env, tmp_path):
    archive_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil.txt", "pwned")
        archive.writestr("playwright/driver/node.exe", b"node-driver")

    staging = tmp_path / "staging"
    with pytest.raises(components.ComponentError) as excinfo:
        components._extract_zip(str(archive_path), str(staging))
    assert excinfo.value.code == "playwright_component_corrupt"
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_extract_rejects_absolute_member_path(component_env, tmp_path):
    archive_path = tmp_path / "absolute.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr(str(tmp_path / "abs.txt"), "pwned")

    staging = tmp_path / "staging-abs"
    with pytest.raises(components.ComponentError) as excinfo:
        components._extract_zip(str(archive_path), str(staging))
    assert excinfo.value.code == "playwright_component_corrupt"
    assert not (tmp_path / "abs.txt").exists()


def test_sha256_mismatch_fails_and_keeps_previous_install(
    component_env, monkeypatch, tmp_path
):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.0.0")
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")
    _patch_urlopen(monkeypatch, payload)

    events: list[dict] = []
    started = components.start_download(
        on_event=events.append,
        expected_sha256="0" * 64,
        url="https://example.invalid/playwright-component.zip",
    )
    assert started is True
    assert components.start_download(on_event=events.append) is False

    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["downloading"] is False
    assert status["installed"] is True
    assert status["error"]
    assert components.playwright_version() == "1.0.0"
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []
    assert events[-1]["stage"] == "error"
    assert events[-1]["error"]


def test_download_success_replaces_old_component(component_env, monkeypatch, tmp_path):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.0.0")
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")
    _patch_urlopen(monkeypatch, payload)
    import hashlib

    events: list[dict] = []
    started = components.start_download(
        on_event=events.append,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        url="https://example.invalid/playwright-component.zip",
    )
    assert started is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["installed"] is True
    assert status["downloading"] is False
    assert status["percent"] == 100
    assert status["error"] == ""
    assert components.playwright_version() == "9.9.9"
    stages = [event["stage"] for event in events]
    assert "downloading" in stages
    assert "extracting" in stages
    assert stages[-1] == "done"
    assert not [p for p in component_env.iterdir() if ".bak-" in p.name]
    assert _staging_dirs(component_env) == []
    assert not (component_env / "playwright-component.zip").exists()


def test_cancel_download_cleans_temp_files(component_env, monkeypatch, tmp_path):
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")

    def slow_urlopen(request, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr(components.urllib.request, "urlopen", slow_urlopen)
    events: list[dict] = []
    assert components.start_download(on_event=events.append, url="https://example.invalid/c.zip")
    components.cancel_download()
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["downloading"] is False
    assert status["installed"] is False
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []


def test_resolve_component_url_prefers_explicit_and_env(component_env, monkeypatch):
    assert components._resolve_component_url("https://example.invalid/explicit.zip") == (
        "https://example.invalid/explicit.zip"
    )
    monkeypatch.setenv("YCODE_PLAYWRIGHT_COMPONENT_URL", "https://example.invalid/env.zip")
    assert components._resolve_component_url("") == "https://example.invalid/env.zip"


def test_resolve_component_url_uses_release_asset(component_env, monkeypatch):
    import core.updater

    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
    monkeypatch.setattr(components, "_fetch_latest_release_entry", lambda: {})
    monkeypatch.setattr(
        core.updater,
        "fetch_latest_release",
        lambda *args, **kwargs: {
            "ok": True,
            "asset_name": "playwright-component-1.51.0.zip",
            "download_url": "https://example.invalid/release/pw.zip",
        },
    )
    assert components._resolve_component_url("") == (
        "https://example.invalid/release/pw.zip"
    )


def test_resolve_component_url_raises_when_no_asset(component_env, monkeypatch):
    import core.updater

    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
    monkeypatch.setattr(components, "_fetch_latest_release_entry", lambda: {})
    monkeypatch.setattr(
        core.updater,
        "fetch_latest_release",
        lambda *args, **kwargs: {
            "ok": True,
            "asset_name": "XingYaoCode-1.2.6.exe",
            "download_url": "https://gitee.com/x/releases/tag/v1.2.6",
        },
    )
    with pytest.raises(components.ComponentError) as excinfo:
        components._resolve_component_url("")
    assert excinfo.value.code == "playwright_component_download_failed"
    assert "未找到组件下载源" in str(excinfo.value)



_MAIN_INSTALLER_DIGEST = "b" * 64


def _component_asset_name(version: str = "9.9.9") -> str:
    return f"playwright-component-{version}.zip"


def _make_release_notes(component_lines: str = "") -> str:
    lines = [
        "## 更新内容",
        "（从 assets/CHANGELOG.md 复制本版本条目）",
        "",
        f"SHA256: {_MAIN_INSTALLER_DIGEST}",
        "",
        "产物 SHA256",
        "-" * 52,
        f"{'XingYaoCode-1.2.6.exe':<40} （4 分卷） {_MAIN_INSTALLER_DIGEST}",
    ]
    if component_lines:
        lines.append(component_lines)
    return "\n".join(lines) + "\n"


def _patch_release_entry(monkeypatch, asset_name: str, notes: str) -> None:
    monkeypatch.setattr(
        components,
        "_fetch_latest_release_entry",
        lambda *args, **kwargs: {
            "tag_name": "v1.2.6",
            "body": notes,
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": "https://example.invalid/release/pw.zip",
                    "size": 1024,
                }
            ],
        },
    )


def test_parse_component_digest_line_from_release_notes(component_env):
    import core.updater

    digest = "a" * 64
    name = _component_asset_name()
    notes = _make_release_notes(f"{name}  SHA256={digest}")

    assert components._parse_component_digest(notes, name) == digest
    assert components._parse_component_digest(
        notes, "playwright-component-2.0.0.zip"
    ) == digest
    assert core.updater.parse_release_sha256(notes) == _MAIN_INSTALLER_DIGEST
    # 说明里没有组件包行时返回空串（由调用层告警，绝不能静默放行）
    assert components._parse_component_digest(_make_release_notes(""), name) == ""


def test_parse_component_digest_prefers_matching_asset_name(component_env):
    first, second = "1" * 64, "2" * 64
    notes = _make_release_notes(
        f"playwright-component-1.50.0.zip  SHA256={first}\n"
        f"playwright-component-1.51.0.zip  SHA256={second}"
    )
    assert components._parse_component_digest(
        notes, "playwright-component-1.51.0.zip"
    ) == second
    assert components._parse_component_digest(
        notes, "playwright-component-1.50.0.zip"
    ) == first
    assert components._parse_component_digest(
        notes, "playwright-component-9.9.9.zip"
    ) == ""


def test_release_digest_mismatch_blocks_install(component_env, monkeypatch, tmp_path):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.0.0")
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")
    _patch_urlopen(monkeypatch, payload)
    name = _component_asset_name("9.9.9")
    wrong_digest = "0" * 64
    _patch_release_entry(
        monkeypatch, name, _make_release_notes(f"{name}  SHA256={wrong_digest}")
    )

    verified: list[str] = []
    original_verify = components._verify_sha256

    def spy(archive_path, expected_sha256):
        verified.append(expected_sha256)
        try:
            original_verify(archive_path, expected_sha256)
        except components.ComponentError as error:
            verified.append(f"code={error.code}")
            raise

    monkeypatch.setattr(components, "_verify_sha256", spy)

    events: list[dict] = []
    assert components.start_download(on_event=events.append) is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    assert verified[0] == wrong_digest
    assert "code=playwright_component_corrupt" in verified
    status = components.get_status()
    assert status["downloading"] is False
    assert status["error"]
    assert "SHA256" in status["error"]
    assert components.playwright_version() == "1.0.0"
    assert events[-1]["stage"] == "error"
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []


def test_release_digest_match_installs_component(component_env, monkeypatch, tmp_path):
    import hashlib

    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")
    _patch_urlopen(monkeypatch, payload)
    name = _component_asset_name("9.9.9")
    digest = hashlib.sha256(payload).hexdigest()
    _patch_release_entry(
        monkeypatch, name, _make_release_notes(f"{name}  SHA256={digest}")
    )

    events: list[dict] = []
    assert components.start_download(on_event=events.append) is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["installed"] is True
    assert status["error"] == ""
    assert status["digest_verified"] is True
    assert status["warning"] == ""
    assert components.playwright_version() == "9.9.9"
    assert [event["stage"] for event in events][-1] == "done"


def test_missing_release_digest_installs_with_warning(
    component_env, monkeypatch, tmp_path, caplog
):
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")
    _patch_urlopen(monkeypatch, payload)
    name = _component_asset_name("9.9.9")
    _patch_release_entry(monkeypatch, name, _make_release_notes(""))

    events: list[dict] = []
    with caplog.at_level(logging.WARNING, logger="core.components"):
        assert components.start_download(on_event=events.append) is True
        thread = components._download_thread
        thread.join(timeout=20)
    assert not thread.is_alive()

    assert "SHA256" in caplog.text
    status = components.get_status()
    assert status["installed"] is True
    assert components.playwright_version() == "9.9.9"
    assert status["digest_verified"] is False
    assert status["warning"]
    assert any(event.get("warning") for event in events)


# ---------------------------------------------------------------- 安装后清理组件包
# 组件包约 25MB：安装成功（stage=done）后必须删掉，避免长期占着 ~/.ycode/components/；
# 失败路径必须保留（见 _download_worker 里「临时文件转正」注释），那是失败后人工
# 复用/排查的唯一凭据。两条语义都要被用例钉住，缺一条就会走回旧行为。


def test_success_install_removes_component_zip(component_env, monkeypatch, tmp_path):
    import hashlib

    payload = _build_component_zip(tmp_path / "new.zip", "1.60.0")
    _patch_urlopen(monkeypatch, payload)

    events: list[dict] = []
    assert components.start_download(
        on_event=events.append,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        url="https://example.invalid/playwright-component.zip",
    ) is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["stage"] == "done"
    assert status["installed"] is True
    assert status["error"] == ""
    assert components.playwright_version() == "1.60.0"
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []
    assert events[-1]["stage"] == "done"


def test_failed_install_keeps_component_zip_for_diagnosis(component_env, monkeypatch):
    import hashlib

    payload = b"definitely not a zip archive"
    _patch_urlopen(monkeypatch, payload)

    events: list[dict] = []
    assert components.start_download(
        on_event=events.append,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        url="https://example.invalid/playwright-component.zip",
    ) is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["stage"] == "error"
    assert status["installed"] is False
    assert events[-1]["stage"] == "error"
    assert (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []


def test_required_playwright_spec_matches_requirements_txt():
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    spec = ""
    for line in requirements.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.lower().startswith("playwright"):
            spec = stripped[len("playwright"):].strip()
            break
    assert spec, "requirements.txt 里找不到 playwright 约束行"
    assert spec == components.REQUIRED_PLAYWRIGHT_SPEC


def test_version_compatible_within_and_outside_spec():
    assert components.playwright_version_compatible("1.62.0") is True
    assert components.playwright_version_compatible("1.55") is True
    assert components.playwright_version_compatible("1.60.0.post1") is True
    assert components.playwright_version_compatible("1.54.9") is False
    assert components.playwright_version_compatible("2.0.0") is False
    assert components.playwright_version_compatible("") is None
    assert components.playwright_version_compatible("unknown") is None


def test_status_flags_outdated_component(component_env):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.51.0")

    status = components.get_status()
    assert status["installed"] is True
    assert status["version"] == "1.51.0"
    assert status["version_compatible"] is False
    assert status["outdated"] is True
    assert status["required_version"] == components.REQUIRED_PLAYWRIGHT_SPEC
    warning = status["version_warning"]
    assert "1.51.0" in warning
    assert components.REQUIRED_PLAYWRIGHT_SPEC in warning
    assert "重新下载" in warning


def test_status_accepts_component_matching_spec(component_env):
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.62.0")

    status = components.get_status()
    assert status["installed"] is True
    assert status["version_compatible"] is True
    assert status["outdated"] is False
    assert status["version_warning"] == ""


def test_status_keeps_quiet_when_version_unknown(component_env):
    pw_dir = Path(components.PLAYWRIGHT_DIR)
    _make_component_dir(pw_dir, version="1.62.0")
    (pw_dir / "COMPONENT.json").unlink()

    status = components.get_status()
    assert status["installed"] is True
    assert status["version"] == ""
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""


# ---------------------------------------------------------------- ffmpeg 组件
# 第二个接入组件注册表的组件（视频转换组件）。这里只验证「注册表驱动」这条主线上
# 最容易回归的点：spec 字段值、未安装/已安装两种状态、未知 id 的错误码。
# 目录一律落在 tmp_path（component_env 已把 COMPONENT_ROOT 指过去），绝不碰真实用户目录。


_FFMPEG_STATUS_KEYS = {
    "installed",
    "version",
    "downloading",
    "percent",
    "downloaded_bytes",
    "total_bytes",
    "error",
    "digest_verified",
    "warning",
    "required_version",
    "version_compatible",
    "outdated",
    "version_warning",
    "dir",
    "stage",
}


def test_ffmpeg_spec_registered():
    spec = components._COMPONENTS["ffmpeg"]
    assert spec is components.FFMPEG_SPEC
    assert components.get_component_spec("ffmpeg") is spec
    assert spec.display_name == "视频转换组件"
    assert spec.install_dir == "ffmpeg"
    assert spec.archive_name == "ffmpeg-component.zip"
    assert spec.extract_prefix == ".ffmpeg-extract-"
    assert spec.asset_hint == "ffmpeg-component"
    assert spec.required_files == ("ffmpeg.exe",)
    assert spec.required_dirs == ()
    assert spec.version_spec == ""
    assert spec.version_probe_file == ""
    assert spec.import_name == ""
    assert spec.manifest_version_key == "ffmpeg_version"
    assert spec.env_url_var == "YCODE_FFMPEG_COMPONENT_URL"
    assert spec.error_prefix == "ffmpeg_component"
    assert spec.settings_hint == "设置 → 背景图转换"
    assert spec.size_hint_mb == 97
    assert spec.version_label == "ffmpeg"


def test_ffmpeg_not_installed_status(component_env):
    status = components.get_component_status("ffmpeg")

    assert set(status.keys()) == _FFMPEG_STATUS_KEYS
    assert len(status) == 15
    assert status["installed"] is False
    assert status["downloading"] is False
    assert status["version"] == ""
    assert status["required_version"] == ""
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""
    expected_dir = os.path.join(os.path.abspath(str(component_env)), "ffmpeg")
    assert os.path.normcase(status["dir"]) == os.path.normcase(expected_dir)


def test_ffmpeg_installed_detection(component_env):
    ffmpeg_dir = Path(components.component_dir("ffmpeg"))
    ffmpeg_dir.mkdir(parents=True, exist_ok=True)
    (ffmpeg_dir / "ffmpeg.exe").write_bytes(b"stub-ffmpeg")
    (ffmpeg_dir / "COMPONENT.json").write_text(
        json.dumps({"ffmpeg_version": "8.1.0"}), encoding="utf-8"
    )

    status = components.get_component_status("ffmpeg")
    assert status["installed"] is True
    assert status["version"] == "8.1.0"
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""

    (ffmpeg_dir / "ffmpeg.exe").unlink()
    assert components.get_component_status("ffmpeg")["installed"] is False


def test_unknown_component_raises():
    with pytest.raises(components.ComponentError) as excinfo:
        components.get_component_status("nope")
    assert excinfo.value.code == "nope_component_unknown"


def test_search_spec_registered():
    spec = components._COMPONENTS["search"]
    assert spec is components.SEARCH_SPEC
    assert components.get_component_spec("search") is spec
    assert spec.display_name == "网页搜索组件"
    assert spec.install_dir == "search"
    assert spec.archive_name == "search-component.zip"
    assert spec.extract_prefix == ".search-extract-"
    assert spec.asset_hint == "search-component"
    assert spec.required_files == ()
    assert spec.required_dirs == ("ddgs", "primp", "lxml", "fake_useragent")
    assert spec.version_spec == ""
    assert spec.version_probe_file == ""
    assert spec.manifest_version_key == "search_version"
    assert spec.import_name == "ddgs"
    assert spec.env_url_var == "YCODE_SEARCH_COMPONENT_URL"
    assert spec.error_prefix == "search_component"
    assert spec.settings_hint == "设置 → 网页搜索"
    assert spec.size_hint_mb == 18
    assert spec.version_label == "search"


def test_search_not_installed_status(component_env):
    status = components.get_component_status("search")

    assert set(status.keys()) == _FFMPEG_STATUS_KEYS
    assert len(status) == 15
    assert status["installed"] is False
    assert status["downloading"] is False
    assert status["version"] == ""
    assert status["required_version"] == ""
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""
    expected_dir = os.path.join(os.path.abspath(str(component_env)), "search")
    assert os.path.normcase(status["dir"]) == os.path.normcase(expected_dir)


def test_search_installed_detection(component_env):
    search_dir = Path(components.component_dir("search"))
    for package in ("ddgs", "primp", "lxml", "fake_useragent"):
        (search_dir / package).mkdir(parents=True, exist_ok=True)
        (search_dir / package / "__init__.py").write_text("", encoding="utf-8")
    (search_dir / "COMPONENT.json").write_text(
        json.dumps({"search_version": "9.14.4"}), encoding="utf-8"
    )

    status = components.get_component_status("search")
    assert status["installed"] is True
    assert status["version"] == "9.14.4"
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""

    shutil.rmtree(search_dir / "fake_useragent")
    assert components.get_component_status("search")["installed"] is False


def test_ensure_search_component_inserts_dir_then_caches(component_env, monkeypatch):
    calls: list[bool] = []

    def fake_try_import() -> bool:
        calls.append(True)
        return len(calls) > 1

    monkeypatch.setattr(components, "_try_import_ddgs", fake_try_import)
    monkeypatch.setattr(components, "_search_import_ready", False)
    monkeypatch.setattr(components, "_import_ready", True)

    components.ensure_search_component()
    assert len(calls) == 2
    target = os.path.normcase(os.path.abspath(str(components.component_dir("search"))))
    assert [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    assert components._search_import_ready is True

    components.ensure_search_component()
    assert len(calls) == 2


def test_ensure_search_component_missing_raises(component_env, monkeypatch):
    monkeypatch.setattr(components, "_try_import_ddgs", lambda: False)
    monkeypatch.setattr(components, "_search_import_ready", False)

    with pytest.raises(components.ComponentError) as excinfo:
        components.ensure_search_component()
    assert excinfo.value.code == "search_component_missing"
    assert "网页搜索" in str(excinfo.value)
