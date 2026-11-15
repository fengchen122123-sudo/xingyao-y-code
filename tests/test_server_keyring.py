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


def test_model_store_uses_keyring_and_worker_config_only_gets_a_reference(tmp_path: Path):
    pytest.importorskip("keyrings.alt")
    import json
    import keyring

    from gateway.db import connect
    from gateway.platform_models import PlatformModelStore
    from gateway.workers import WorkerManager
    from core.serve.keyring_setup import install_server_keyring

    password = tmp_path / "keyring_password"
    password.write_text("correct horse", encoding="utf-8")
    previous = keyring.get_keyring()
    try:
        assert install_server_keyring(tmp_path, password)
        database = connect(tmp_path / "gateway.db")
        store = PlatformModelStore(database, keyring)
        store.save([{
            "id": "deepseek",
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-never-store-plaintext",
            "models": [{"id": "chat", "name": "Chat", "litellm_model": "openai/chat"}],
        }], "chat")
        stored = database.execute("SELECT providers_json FROM platform_models").fetchone()[0]
        assert "sk-never-store-plaintext" not in stored
        assert store.load()["providers"][0]["api_key"] == "sk-never-store-plaintext"

        database.execute(
            "INSERT INTO accounts(id, username, password_hash, role, created_at) VALUES ('a', 'a', 'x', 'user', 1)"
        )
        database.commit()
        manager = WorkerManager(object(), database, tmp_path / "accounts", "image", platform_models=store.load)
        manager.ensure_token("a")
        worker_config = (manager.volume_for("a") / "config.json").read_text(encoding="utf-8")
        assert "sk-never-store-plaintext" not in worker_config
        saved = json.loads(worker_config)
        assert saved["providers"][0]["api_key"].startswith("keyring:")
        assert saved["permission_mode"] == "full"
        from core.credentials import unprotect_secret
        assert unprotect_secret(saved["providers"][0]["api_key"]) == "sk-never-store-plaintext"
        assert b"sk-never-store-plaintext" not in (tmp_path / "keyring" / "crypted_pass.cfg").read_bytes()
    finally:
        keyring.set_keyring(previous)
