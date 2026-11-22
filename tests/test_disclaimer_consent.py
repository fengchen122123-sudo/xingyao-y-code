import json
from pathlib import Path

import pytest

import config
from core.disclaimer_consent import (
    DISCLAIMER_VERSION,
    accept_disclaimer,
    get_disclaimer_consent,
    is_disclaimer_version_accepted,
    needs_acceptance,
)

_STORE_NAME = "disclaimer_consent.json"


def _store_file() -> Path:
    return Path(config.YCODE_DIR) / _STORE_NAME


@pytest.fixture(autouse=True)
def _clean_consent_store():
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
    accept_disclaimer()
    assert needs_acceptance() is False

    monkeypatch.setattr("core.disclaimer_consent.DISCLAIMER_VERSION", "v1.1")
    assert needs_acceptance() is True
    snapshot = get_disclaimer_consent()
    assert snapshot["accepted"] is False
    assert snapshot["needs_acceptance"] is True
    assert snapshot["accepted_version"] == "v1.0"
    assert snapshot["disclaimer_version"] == "v1.1"


def test_is_disclaimer_version_accepted_semantics():
    assert is_disclaimer_version_accepted("v1.0", "v1.0") is True
    assert is_disclaimer_version_accepted("1.0", "v1.0") is True
    assert is_disclaimer_version_accepted("v1.0", "1.0") is True
    assert is_disclaimer_version_accepted("v1.1", "v1.0") is True
    assert is_disclaimer_version_accepted("v1.0", "v1.1") is False
    assert is_disclaimer_version_accepted("v1.0.1", "v1.0") is True
    assert is_disclaimer_version_accepted("v1.0", "v2.0") is False
    assert is_disclaimer_version_accepted("", "v1.0") is False
    assert is_disclaimer_version_accepted(None, "v1.0") is False


def test_accept_records_server_side_version(monkeypatch):
    monkeypatch.setattr("core.disclaimer_consent.DISCLAIMER_VERSION", "v1.1")
    snapshot = accept_disclaimer()
    assert snapshot["accepted_version"] == "v1.1"


def test_accepted_flag_without_version_is_not_valid():
    _store_file().write_text(json.dumps({"accepted": True}), encoding="utf-8")

    assert get_disclaimer_consent()["accepted"] is False
    assert needs_acceptance() is True


def test_corrupt_file_falls_back_to_not_accepted():
    _store_file().write_text("{ not valid json", encoding="utf-8")

    assert get_disclaimer_consent()["accepted"] is False
    assert needs_acceptance() is True


@pytest.fixture()
def main_module(monkeypatch, tmp_path):
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
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    with pytest.raises(SystemExit):
        main_module._ensure_cli_disclaimer_consent()

    assert needs_acceptance() is True


def test_cli_consent_not_asked_again_once_accepted(monkeypatch, main_module):
    accept_disclaimer()
    asked: list[int] = []
    monkeypatch.setattr("builtins.input", lambda *_: asked.append(1) or "y")

    main_module._ensure_cli_disclaimer_consent()

    assert asked == []
