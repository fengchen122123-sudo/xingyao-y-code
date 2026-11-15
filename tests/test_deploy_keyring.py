"""deploy-keyring：共享加密钥匙串、模型更新语义和部署注入。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway.app import create_app
from gateway.workers import WorkerManager, container_spec


SECRET = "jwt-secret-jwt-secret-jwt-secret"


class MemoryKeyring:
    def __init__(self):
        self.secrets = {}

    def set_password(self, service, account, secret):
        self.secrets[(service, account)] = secret

    def get_password(self, service, account):
        return self.secrets.get((service, account))


def _app_with_models(tmp_path):
    from gateway.platform_models import PlatformModelStore

    app = create_app(tmp_path / "gateway.db", SECRET)
    app.state.init_admin("root", "secret-secret")
    app.state.model_store = PlatformModelStore(app.state.database, MemoryKeyring(), "ycode-test")
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "root", "password": "secret-secret"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    return app, client, headers


def _body(name="DeepSeek", model_id="deepseek-chat", api_key="sk-first"):
    provider = {
        "name": name,
        "base_url": "https://api.example.com/v1",
        "model_id": model_id,
        "model_name": model_id,
        "litellm_model": f"openai/{model_id}",
    }
    if api_key is not None:
        provider["api_key"] = api_key
    return {"providers": [provider], "active_model_id": model_id}


def test_model_put_preserves_missing_key_and_merges_existing_providers(tmp_path):
    app, client, headers = _app_with_models(tmp_path)
    assert client.put("/api/admin/models", json=_body(), headers=headers).status_code == 200
    assert client.put(
        "/api/admin/models",
        json=_body(name="Moonshot", model_id="kimi", api_key="sk-second"),
        headers=headers,
    ).status_code == 200

    updated = _body(model_id="deepseek-chat-new", api_key=None)
    assert client.put("/api/admin/models", json=updated, headers=headers).status_code == 200
    loaded = app.state.model_store.load()
    by_id = {provider["id"]: provider for provider in loaded["providers"]}
    assert set(by_id) == {"deepseek", "moonshot"}
    assert by_id["deepseek"]["api_key"] == "sk-first"
    assert by_id["moonshot"]["api_key"] == "sk-second"

    public = client.get("/api/admin/models", headers=headers)
    assert public.status_code == 200
    assert "sk-first" not in public.text
    assert "sk-second" not in public.text
    assert all("api_key" not in provider for provider in public.json()["providers"])


def test_empty_model_list_clears_worker_providers_and_active_model(tmp_path):
    app, client, headers = _app_with_models(tmp_path)
    account = app.state.database.execute("SELECT id FROM accounts").fetchone()["id"]
    manager = WorkerManager(
        object(),
        app.state.database,
        tmp_path / "accounts",
        "image",
        platform_models=app.state.model_store.load,
    )
    app.state.workers = manager
    assert client.put("/api/admin/models", json=_body(), headers=headers).status_code == 200
    config_path = manager.volume_for(account) / "config.json"
    assert json.loads(config_path.read_text(encoding="utf-8"))["providers"]

    response = client.put(
        "/api/admin/models",
        json={"providers": [], "active_model_id": "stale-model"},
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["active_model_id"] == ""
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["providers"] == []
    assert saved["active_model_id"] == ""
    assert app.state.model_store.references() == {"providers": [], "active_model_id": ""}


def test_worker_spec_readonly_mounts_master_keyring_and_password(tmp_path):
    spec = container_spec(
        "a",
        "image",
        tmp_path / "data",
        "xingyao",
        keyring_file="/srv/secrets/keyring_password",
        keyring_source="xingyao-keyring",
    )
    assert "xingyao-keyring:/run/keyring:ro" in spec["binds"]
    assert "/srv/secrets/keyring_password:/run/secrets/keyring_password:ro" in spec["binds"]
    assert spec["env"]["YCODE_KEYRING_FILE"] == "/run/keyring/crypted_pass.cfg"
    assert spec["env"]["YCODE_KEYRING_READ_ONLY"] == "1"
    assert spec["env"]["YCODE_KEYRING_PASSWORD_FILE"] == "/run/secrets/keyring_password"


def test_worker_process_with_other_data_dir_and_home_reads_master_keyring(tmp_path, monkeypatch):
    pytest.importorskip("keyrings.alt")
    import keyring

    from core.serve.keyring_setup import install_server_keyring
    from gateway.db import connect
    from gateway.platform_models import PlatformModelStore

    root = Path(__file__).resolve().parents[1]
    gateway_data = tmp_path / "gateway-data"
    worker_data = tmp_path / "worker-data"
    other_home = tmp_path / "other-home"
    worker_data.mkdir()
    other_home.mkdir()
    password = tmp_path / "keyring_password"
    password.write_text("test-only-password", encoding="utf-8")
    monkeypatch.delenv("YCODE_KEYRING_FILE", raising=False)
    monkeypatch.delenv("YCODE_KEYRING_READ_ONLY", raising=False)
    previous = keyring.get_keyring()
    try:
        assert install_server_keyring(gateway_data, password)
        store = PlatformModelStore(connect(tmp_path / "gateway.db"), keyring)
        store.save([{
            "id": "deepseek",
            "name": "DeepSeek",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-cross-process",
            "models": [{"id": "chat", "name": "Chat", "litellm_model": "openai/chat"}],
        }], "chat")
        reference = store.references()["providers"][0]["api_key"]
        master_file = gateway_data / "keyring" / "crypted_pass.cfg"
        assert master_file.is_file()
    finally:
        keyring.set_keyring(previous)

    program = """
import os
from pathlib import Path
from core.credentials import unprotect_secret
from core.serve.keyring_setup import install_server_keyring
assert install_server_keyring(Path(os.environ['YCODE_DATA_DIR']))
assert unprotect_secret(os.environ['TEST_MODEL_REFERENCE']) == 'sk-cross-process'
assert not (Path(os.environ['YCODE_DATA_DIR']) / 'keyring' / 'crypted_pass.cfg').exists()
"""
    env = os.environ.copy()
    env.update({
        "HOME": str(other_home),
        "YCODE_DATA_DIR": str(worker_data),
        "YCODE_KEYRING_FILE": str(master_file),
        "YCODE_KEYRING_PASSWORD_FILE": str(password),
        "YCODE_KEYRING_READ_ONLY": "1",
        "TEST_MODEL_REFERENCE": reference,
        "PYTHONPATH": str(root),
    })
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert b"sk-cross-process" not in master_file.read_bytes()


def test_gateway_fails_clearly_without_keyring_password(tmp_path, monkeypatch):
    import gateway.app as gateway_app
    import gateway.runtime

    monkeypatch.setenv("XINGYAO_JWT_SECRET", SECRET)
    monkeypatch.setenv("XINGYAO_GATEWAY_DIR", str(tmp_path / "gateway"))
    monkeypatch.setenv("XINGYAO_ACCOUNTS_ROOT", str(tmp_path / "accounts"))
    monkeypatch.setenv("XINGYAO_WEB_DIST", "")
    monkeypatch.delenv("XINGYAO_KEYRING_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(gateway.runtime, "attach_runtime", lambda _app, _config: None)
    with pytest.raises(RuntimeError, match="钥匙串口令"):
        gateway_app.app_from_env()


def test_deploy_files_wire_secret_persistent_master_and_early_worker_unlock():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    service = (root / "deploy" / "gateway.service").read_text(encoding="utf-8")
    worker_image = (root / "deploy" / "worker.Dockerfile").read_text(encoding="utf-8")
    readme = (root / "deploy" / "README.md")

    assert "XINGYAO_KEYRING_PASSWORD_FILE: /run/secrets/keyring_password" in compose
    assert "XINGYAO_WORKER_KEYRING_SOURCE: xingyao-keyring" in compose
    assert "keyring-data:/srv/xingyao-state/gateway/keyring" in compose
    assert "name: xingyao-keyring" in compose
    assert "mode: 0400" in compose
    assert "${XINGYAO_KEYRING_PASSWORD_FILE:?" in compose
    assert "LoadCredential=keyring_password:/etc/xingyao/keyring_password" in service
    assert "XINGYAO_WORKER_KEYRING_SOURCE=/srv/xingyao-state/gateway/keyring" in service
    assert "gateway.worker_bootstrap" in worker_image
    assert readme.is_file()
    instructions = readme.read_text(encoding="utf-8")
    assert "0400" in instructions
    assert "docker compose config" in instructions
