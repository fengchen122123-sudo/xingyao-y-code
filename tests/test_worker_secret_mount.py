from __future__ import annotations

from gateway.workers import WorkerManager


class FakeDocker:
    def __init__(self):
        self.spec = None

    def exists(self, _name):
        return False

    def create(self, spec):
        self.spec = spec

    def start(self, _name):
        return None


def test_worker_mounts_host_secret_path_not_gateway_container_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XINGYAO_KEYRING_PASSWORD_FILE", "/run/secrets/keyring_password")
    monkeypatch.setenv("XINGYAO_KEYRING_HOST_PASSWORD_FILE", "/etc/xingyao/keyring_password")
    docker = FakeDocker()
    manager = WorkerManager(docker, _database(tmp_path), tmp_path / "accounts", "image")
    result = manager.ensure_running(
        "account-1",
        lambda _url: {"ok": True, "boot_id": "boot-1"},
        clock=lambda: 0.0,
    )
    assert result["ok"] is True
    assert "/etc/xingyao/keyring_password:/run/secrets/keyring_password:ro" in docker.spec["binds"]
    assert not any(item.startswith("/run/secrets/") for item in docker.spec["binds"])


def _database(tmp_path):
    import sqlite3

    database = sqlite3.connect(tmp_path / "gateway.db")
    database.row_factory = sqlite3.Row
    database.execute(
        "CREATE TABLE workers(account_id TEXT PRIMARY KEY, container_name TEXT NOT NULL, worker_token TEXT NOT NULL, desired_state TEXT NOT NULL DEFAULT 'stopped', last_activity INTEGER, updated_at INTEGER NOT NULL, started_at INTEGER, last_boot_id TEXT, version TEXT NOT NULL DEFAULT '', active_turn INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')"
    )
    database.commit()
    return database
