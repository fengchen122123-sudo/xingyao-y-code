"""core/components.py（Playwright 按需组件）单元测试。

覆盖：
  - 未安装时 playwright_installed() 为 False、ensure_playwright() 抛 ComponentError
  - 手工造出的组件目录被识别为已安装、ensure_playwright() 不抛
  - 解压安全（zip-slip）：含 ``../`` 成员的包被拒绝且临时目录外无文件落盘
  - 下载安装状态机：SHA256 不匹配时安装失败且旧组件保持原状；正常包能装进去
  - Release 说明里的组件包 digest：命中则强制校验（错就 corrupt 失败、对就装进去），
    缺失时仍安装但必须留下告警（不允许静默放行）

全部用例不发起真实网络请求（urlopen 被 monkeypatch），组件目录与 YCODE_DIR
都指向 pytest 的 tmp_path。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import zipfile
from pathlib import Path

import pytest

import config
from core import components


class _FakeResponse:
    """urllib.request.urlopen 的最小替身：实现 context manager + read(size)。"""

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
    """把 urllib.request.urlopen 换成返回固定字节的替身，杜绝真实网络。"""

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(payload)

    monkeypatch.setattr(components.urllib.request, "urlopen", fake_urlopen)


def _make_component_dir(pw_dir: Path, version: str = "1.50.0") -> None:
    """手工造一个结构完整的组件目录（playwright 包 + node driver + 依赖 + manifest）。"""
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
    """构造一个结构合法的组件压缩包，返回其字节内容。"""
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
    """列出组件根目录下残留的临时解压目录。"""
    if not root.exists():
        return []
    return [p for p in root.iterdir() if p.name.startswith(".playwright-extract-")]


@pytest.fixture()
def component_env(tmp_path, monkeypatch):
    """把组件目录整体指到 tmp_path，并重置模块级单例状态。"""
    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.delenv("YCODE_COMPONENT_DIR", raising=False)
    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
    monkeypatch.setattr(components, "COMPONENT_ROOT", str(root))
    monkeypatch.setattr(components, "PLAYWRIGHT_DIR", str(root / "playwright"))
    monkeypatch.setattr(config, "YCODE_DIR", str(tmp_path))
    # sys.path 换成副本：ensure_playwright 的插入不污染真实解释器状态
    monkeypatch.setattr(sys, "path", list(sys.path))
    # 模块级缓存/单例状态复位（避免用例间互相影响）
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
    """未安装：installed 为 False、无版本号，ensure_playwright() 抛 missing 错误码。"""
    # 开发环境 site-packages 里装着 playwright，用 None 占位强制 import 失败
    monkeypatch.setitem(sys.modules, "playwright", None)

    assert components.playwright_installed() is False
    assert components.playwright_version() == ""
    status = components.get_status()
    assert status["installed"] is False
    assert status["downloading"] is False
    assert status["dir"] == str(components.PLAYWRIGHT_DIR)
    # 未安装时版本比对为「未知」，不得误报 outdated
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""

    with pytest.raises(components.ComponentError) as excinfo:
        components.ensure_playwright()
    assert excinfo.value.code == "playwright_component_missing"
    assert "电脑操控" in str(excinfo.value)

    # 组件目录只允许被插入 sys.path 一次（重复调用不得重复插入）
    target = os.path.normcase(os.path.abspath(str(components.PLAYWRIGHT_DIR)))
    inserted = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    assert len(inserted) == 1
    with pytest.raises(components.ComponentError):
        components.ensure_playwright()
    inserted = [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    assert len(inserted) == 1


def test_installed_component_detected_and_ensure_ok(component_env):
    """结构完整的组件目录：检测为已安装，ensure_playwright() 不抛异常。"""
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.51.0")

    assert components.playwright_installed() is True
    assert components.playwright_version() == "1.51.0"
    assert components.get_status()["installed"] is True
    components.ensure_playwright()  # 不抛即通过
    assert components._import_ready is True


def test_version_falls_back_to_repo_version_file(component_env):
    """COMPONENT.json 缺失版本号时回退读 playwright/_repo_version.py。"""
    pw_dir = Path(components.PLAYWRIGHT_DIR)
    _make_component_dir(pw_dir, version="1.50.0")
    (pw_dir / "COMPONENT.json").unlink()
    (pw_dir / "playwright" / "_repo_version.py").write_text(
        '__version__ = "1.49.1"\n', encoding="utf-8"
    )
    assert components.playwright_version() == "1.49.1"


def test_ensure_inserts_component_dir_once_then_caches(component_env, monkeypatch):
    """首轮 import 失败时插入组件目录并重试；成功后走缓存不再重试。"""
    calls: list[bool] = []

    def fake_try_import() -> bool:
        calls.append(True)
        return len(calls) > 1  # 首次失败（需插入 sys.path），插入后成功

    monkeypatch.setattr(components, "_try_import_playwright", fake_try_import)
    components.ensure_playwright()
    assert len(calls) == 2
    target = os.path.normcase(os.path.abspath(str(components.PLAYWRIGHT_DIR)))
    assert [p for p in sys.path if os.path.normcase(os.path.abspath(p)) == target]
    components.ensure_playwright()  # 缓存命中，不再调用
    assert len(calls) == 2


def test_extract_rejects_path_traversal(component_env, tmp_path):
    """含 ../ 成员的 zip 被拒绝解压，且临时目录外没有任何文件落盘。"""
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
    """含绝对路径成员的 zip 同样被拒绝（防目录穿越的另一半）。"""
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
    """SHA256 不匹配：报 corrupt 错误码、清理临时文件、旧组件目录保持原状。"""
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
    # 下载中重复调用返回 False（单例语义）
    assert components.start_download(on_event=events.append) is False

    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    status = components.get_status()
    assert status["downloading"] is False
    assert status["installed"] is True
    assert status["error"]
    assert components.playwright_version() == "1.0.0"
    # 不留半装状态：临时 zip / 临时解压目录全部清理
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []
    assert events[-1]["stage"] == "error"
    assert events[-1]["error"]


def test_download_success_replaces_old_component(component_env, monkeypatch, tmp_path):
    """正常包：下载→解压→原子替换，旧组件被新版本替换且不留 .bak。"""
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
    # 旧目录已被删除，没有 .bak 残留
    assert not [p for p in component_env.iterdir() if ".bak-" in p.name]
    assert _staging_dirs(component_env) == []
    # Minor 3：安装成功后组件包不再保留（见 test_success_install_removes_component_zip）
    assert not (component_env / "playwright-component.zip").exists()


def test_cancel_download_cleans_temp_files(component_env, monkeypatch, tmp_path):
    """取消下载：线程尽快退出、状态复位且不留临时文件。"""
    payload = _build_component_zip(tmp_path / "new.zip", "9.9.9")

    def slow_urlopen(request, timeout=None):
        # 首块之后就让取消标志生效：模拟用户在下载中途点取消
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
    """下载源解析：显式参数 > 环境变量 > Release 附件（这里只测前两级，不联网）。"""
    assert components._resolve_component_url("https://example.invalid/explicit.zip") == (
        "https://example.invalid/explicit.zip"
    )
    monkeypatch.setenv("YCODE_PLAYWRIGHT_COMPONENT_URL", "https://example.invalid/env.zip")
    assert components._resolve_component_url("") == "https://example.invalid/env.zip"


def test_resolve_component_url_uses_release_asset(component_env, monkeypatch):
    """无显式地址时从 fetch_latest_release 的摊平字段里挑 playwright-component 资产。"""
    import core.updater

    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
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
    """Release 里没有组件包时抛 download_failed 且提示未找到下载源。"""
    import core.updater

    monkeypatch.delenv("YCODE_PLAYWRIGHT_COMPONENT_URL", raising=False)
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



# ---------------------------------------------------------------- 组件包 digest
# 发版侧（build_release.py::step4c_make_release_parts）把组件包 SHA256 写进 Release
# 说明的固定行：``<资产文件名>  SHA256=<64 位十六进制>``。以下用例锁定「客户端必须
# 用这条 digest 强制校验」以及「拿不到 digest 时不得静默放行」。

_MAIN_INSTALLER_DIGEST = "b" * 64


def _component_asset_name(version: str = "9.9.9") -> str:
    return f"playwright-component-{version}.zip"


def _make_release_notes(component_lines: str = "") -> str:
    """仿 build_release.py 生成的发布说明（含主安装包 SHA256 行与组件包校验行）。"""
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
    """把 Release 条目查询换成固定结构（含 body=发布说明，不发起网络请求）。"""
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
    """发布说明里的组件包校验行能被解析，且不会污染主安装包的 SHA256 解析。"""
    import core.updater

    digest = "a" * 64
    name = _component_asset_name()
    notes = _make_release_notes(f"{name}  SHA256={digest}")

    assert components._parse_component_digest(notes, name) == digest
    # 发版时组件包换了版本号（名字对不上）也只有一条组件行，仍然认它
    assert components._parse_component_digest(
        notes, "playwright-component-2.0.0.zip"
    ) == digest
    # 关键回归：组件行不得被主安装包的 SHA256 解析器误吞
    assert core.updater.parse_release_sha256(notes) == _MAIN_INSTALLER_DIGEST
    # 说明里没有组件包行时返回空串（由调用层告警，绝不能静默放行）
    assert components._parse_component_digest(_make_release_notes(""), name) == ""


def test_parse_component_digest_prefers_matching_asset_name(component_env):
    """多条组件校验行时按资产名精确匹配；都对不上且有多条则不瞎猜。"""
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
    """Release 说明里的 digest 与包内容不符：报 corrupt、拒绝安装、旧组件保持原状。"""
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
    # 刻意不传 expected_sha256：调用层必须自己从 Release 说明解析出来
    assert components.start_download(on_event=events.append) is True
    thread = components._download_thread
    thread.join(timeout=20)
    assert not thread.is_alive()

    # digest 确实来自 Release 说明（而不是空值直接放行）
    assert verified[0] == wrong_digest
    assert "code=playwright_component_corrupt" in verified
    status = components.get_status()
    assert status["downloading"] is False
    assert status["error"]
    assert "SHA256" in status["error"]
    assert components.playwright_version() == "1.0.0"  # 旧组件保持原状
    assert events[-1]["stage"] == "error"
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []


def test_release_digest_match_installs_component(component_env, monkeypatch, tmp_path):
    """Release 说明里的 digest 与包内容一致：正常完成安装并标记已校验。"""
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
    """Release 说明缺组件包校验行：仍可安装，但必须留下日志与进度告警。"""
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
    # 告警必须同时出现在进度事件里，前端/CLI 才看得见
    assert any(event.get("warning") for event in events)


# ---------------------------------------------------------------- 安装后清理组件包
# 组件包约 25MB：安装成功（stage=done）后必须删掉，避免长期占着 ~/.ycode/components/；
# 失败路径必须保留（见 _download_worker 里「临时文件转正」注释），那是失败后人工
# 复用/排查的唯一凭据。两条语义都要被用例钉住，缺一条就会走回旧行为。


def test_success_install_removes_component_zip(component_env, monkeypatch, tmp_path):
    """安装成功（stage=done）后组件包被删除，组件目录照常可用。"""
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
    # 关键断言：删除只发生在安装确认成功之后
    assert not (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []
    assert events[-1]["stage"] == "done"


def test_failed_install_keeps_component_zip_for_diagnosis(component_env, monkeypatch):
    """解压失败（下载内容不是有效 zip）：转正后的组件包必须保留供人工排查。"""
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
    # 失败路径保留组件包（排查凭据），只清临时 zip 与解压暂存目录
    assert (component_env / "playwright-component.zip").exists()
    assert not (component_env / "playwright-component.zip.tmp").exists()
    assert _staging_dirs(component_env) == []


# ---------------------------------------------------------------- 组件版本比对
# 主程序对 Playwright 有版本要求（requirements.txt 的 playwright>=1.55,<2），组件目录
# COMPONENT.json 的 playwright_version 必须落在该约束内；Release 升级主程序后沿用旧
# 组件时，get_status() 必须给出可见提示，而不是只判断「能否 import」。


def test_required_playwright_spec_matches_requirements_txt():
    """运行期常量必须与 requirements.txt 的 playwright 约束逐字一致（防漂移）。"""
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
    """比对函数：范围内的算匹配，越界/跨大版本算不匹配，无法解析算未知（None）。"""
    assert components.playwright_version_compatible("1.62.0") is True
    assert components.playwright_version_compatible("1.55") is True
    assert components.playwright_version_compatible("1.60.0.post1") is True
    assert components.playwright_version_compatible("1.54.9") is False
    assert components.playwright_version_compatible("2.0.0") is False
    assert components.playwright_version_compatible("") is None
    assert components.playwright_version_compatible("unknown") is None


def test_status_flags_outdated_component(component_env):
    """旧组件：installed 仍为 True，但 status 必须给出中文「版本不匹配」提示。"""
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
    """版本落在要求范围内：不报 outdated，提示为空串。"""
    _make_component_dir(Path(components.PLAYWRIGHT_DIR), version="1.62.0")

    status = components.get_status()
    assert status["installed"] is True
    assert status["version_compatible"] is True
    assert status["outdated"] is False
    assert status["version_warning"] == ""


def test_status_keeps_quiet_when_version_unknown(component_env):
    """已安装但版本号读不出来：判断为未知（None），不得误报 outdated。"""
    pw_dir = Path(components.PLAYWRIGHT_DIR)
    _make_component_dir(pw_dir, version="1.62.0")
    # 无 COMPONENT.json 也无 playwright/_repo_version.py → playwright_version() 返回空串
    (pw_dir / "COMPONENT.json").unlink()

    status = components.get_status()
    assert status["installed"] is True
    assert status["version"] == ""
    assert status["version_compatible"] is None
    assert status["outdated"] is False
    assert status["version_warning"] == ""
