"""免责条款同意记录（core/disclaimer_consent.py）单元测试。

覆盖四类行为：
1. 默认未同意 —— 首次使用必须弹确认框；
2. 同意后落盘带版本与时间戳，可举证；
3. 条款升级后已同意旧版本的用户需重新确认；
4. 异常输入（无版本号的 accepted、文件损坏）一律降级为"未同意"。

数据目录由 tests/conftest.py 指向临时目录，不会触碰真实 ~/.ycode。
"""

import json
from pathlib import Path

import pytest

import config
from core.disclaimer_consent import (
    DISCLAIMER_VERSION,
    accept_disclaimer,
    get_disclaimer_consent,
    needs_acceptance,
)

_STORE_NAME = "disclaimer_consent.json"


def _store_file() -> Path:
    return Path(config.YCODE_DIR) / _STORE_NAME


@pytest.fixture(autouse=True)
def _clean_consent_store():
    """每个用例独占一份同意记录，避免用例间相互影响。"""
    base = Path(config.YCODE_DIR)
    for name in (_STORE_NAME, _STORE_NAME + ".bak", _STORE_NAME + ".tmp"):
        try:
            (base / name).unlink()
        except OSError:
            pass
    yield


def test_defaults_to_not_accepted():
    snapshot = get_disclaimer_consent()
    assert snapshot["accepted"] is False
    assert snapshot["accepted_version"] == ""
    assert snapshot["accepted_at"] == ""
    assert snapshot["disclaimer_version"] == DISCLAIMER_VERSION
    assert needs_acceptance() is True


def test_accept_writes_version_and_timestamp_to_disk():
    snapshot = accept_disclaimer()

    assert snapshot["accepted"] is True
    assert snapshot["accepted_version"] == DISCLAIMER_VERSION
    assert snapshot["accepted_at"]

    stored = json.loads(_store_file().read_text(encoding="utf-8"))
    assert stored["accepted_version"] == DISCLAIMER_VERSION
    assert stored["accepted_at"] == snapshot["accepted_at"]


def test_needs_acceptance_false_after_accept():
    accept_disclaimer()
    assert needs_acceptance() is False
    assert get_disclaimer_consent()["accepted_version"] == DISCLAIMER_VERSION


def test_version_bump_requires_reacceptance(monkeypatch):
    """条款升级后，已同意旧版本的用户必须重新确认。"""
    accept_disclaimer()
    assert needs_acceptance() is False

    monkeypatch.setattr("core.disclaimer_consent.DISCLAIMER_VERSION", "v1.1")
    assert needs_acceptance() is True


def test_accept_records_server_side_version(monkeypatch):
    """版本号以服务端为准：前端缓存旧版本也不能把"同意旧条款"记成当前版本。"""
    monkeypatch.setattr("core.disclaimer_consent.DISCLAIMER_VERSION", "v1.1")
    snapshot = accept_disclaimer()
    assert snapshot["accepted_version"] == "v1.1"


def test_accepted_flag_without_version_is_not_valid():
    """手改文件只写 accepted=true 而无版本号，不构成有效同意。"""
    _store_file().write_text(json.dumps({"accepted": True}), encoding="utf-8")

    assert get_disclaimer_consent()["accepted"] is False
    assert needs_acceptance() is True


def test_corrupt_file_falls_back_to_not_accepted():
    """配置文件损坏时降级为"未同意"——读取失败不能变成放行。"""
    _store_file().write_text("{ not valid json", encoding="utf-8")

    assert get_disclaimer_consent()["accepted"] is False
    assert needs_acceptance() is True


@pytest.fixture()
def main_module(monkeypatch, tmp_path):
    """导入 main 模块。

    main 顶层会 import pyfiglet，后者要求 APPDATA/LOCALAPPDATA 存在；
    精简的测试环境下这两个变量可能缺失，这里补上，避免无关的环境依赖
    把 CLI 确认逻辑的测试带崩。
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    import main

    return main


def test_cli_consent_records_on_confirm(monkeypatch, main_module):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    main_module._ensure_cli_disclaimer_consent()

    assert needs_acceptance() is False
    assert get_disclaimer_consent()["accepted_at"]


def test_cli_consent_exits_when_declined(monkeypatch, main_module):
    """未接受条款不得进入软件：CLI 侧同样以退出收场。"""
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SystemExit):
        main_module._ensure_cli_disclaimer_consent()

    assert needs_acceptance() is True


def test_cli_consent_not_asked_again_once_accepted(monkeypatch, main_module):
    """GUI/CLI 共用同一份记录：已同意过就不再重复询问。"""
    accept_disclaimer()
    asked: list[int] = []
    monkeypatch.setattr("builtins.input", lambda *_: asked.append(1) or "y")

    main_module._ensure_cli_disclaimer_consent()

    assert asked == []
