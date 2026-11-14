"""server 档钥匙串：同一口令能读回，卷里没有明文。"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_missing_password_file_does_not_prompt(tmp_path: Path, monkeypatch):
    pytest.importorskip("keyrings.alt")
    from core.serve.keyring_setup import install_server_keyring

    monkeypatch.delenv("YCODE_KEYRING_PASSWORD_FILE", raising=False)

    def fail_getpass(*_args, **_kwargs):
        raise AssertionError("不应该调用 getpass")

    monkeypatch.setattr("getpass.getpass", fail_getpass)
    assert install_server_keyring(tmp_path, tmp_path / "missing") is False


def test_encrypted_keyring_roundtrip_hides_plaintext(tmp_path: Path, monkeypatch):
    pytest.importorskip("keyrings.alt")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    password = tmp_path / "keyring_password"
    password.write_text("correct horse", encoding="utf-8")
    import keyring
    from core.serve.keyring_setup import install_server_keyring

    previous = keyring.get_keyring()
    try:
        assert install_server_keyring(tmp_path, password)
        keyring.set_password("ycode-test", "model", "sk-secret-value")
        assert keyring.get_password("ycode-test", "model") == "sk-secret-value"
        stored = (tmp_path / "keyring" / "crypted_pass.cfg").read_bytes()
        assert b"sk-secret-value" not in stored
    finally:
        keyring.set_keyring(previous)
