"""网关账号、用量水位和 worker 约束。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from gateway.auth import bump_epoch, init_admin, issue_tokens, logout, read_access
from gateway.db import connect
from gateway.usage import apply_usage, shanghai_day
from gateway.workers import WorkerManager, container_spec


class FakeDocker:
    def __init__(self):
        self.created = {}
        self.running = set()

    def exists(self, name):
        return name in self.created

    def create(self, spec):
        assert spec["ports"] == {}
        assert spec["docker_sock"] is False
        assert spec["user"] == "10001:10001"
        assert "ALL" in spec["cap_drop"]
        self.created[spec["name"]] = spec

    def start(self, name):
        self.running.add(name)

    def stop(self, name):
        self.running.discard(name)


def test_init_admin_refuses_a_second_account(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    with pytest.raises(RuntimeError):
        init_admin(database, "other", "secret-secret")


def test_logout_does_not_bump_epoch_and_old_access_fails(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    account = database.execute("SELECT * FROM accounts").fetchone()
    issued = issue_tokens(database, account, "jwt-secret-jwt-secret-jwt-secret")
    principal = read_access(database, issued["access_token"], "jwt-secret-jwt-secret-jwt-secret")
    assert principal is not None
    before = int(account["auth_epoch"])
    logout(database, principal, issued["refresh_token"])
    account = database.execute("SELECT * FROM accounts").fetchone()
    assert int(account["auth_epoch"]) == before
    assert read_access(database, issued["access_token"], "jwt-secret-jwt-secret-jwt-secret") is None


def test_disable_bumps_epoch_so_old_access_cannot_list_devices(tmp_path):
    from fastapi.testclient import TestClient

    from gateway.app import create_app

    app = create_app(tmp_path / "gateway.db", "jwt-secret-jwt-secret-jwt-secret")
    app.state.init_admin("root", "secret-secret")
    client = TestClient(app)
    login = client.post("/api/auth/login", json={"username": "root", "password": "secret-secret"})
    token = login.json()["access_token"]
    created = client.post(
        "/api/admin/accounts",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": "ada", "password": "secret-secret", "role": "user"},
    )
    user_id = created.json()["id"]
    user_login = client.post("/api/auth/login", json={"username": "ada", "password": "secret-secret"})
    user_token = user_login.json()["access_token"]
    assert client.get("/api/devices", headers={"Authorization": f"Bearer {user_token}"}).status_code == 200
    patched = client.patch(
        f"/api/admin/accounts/{user_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"disabled": True},
    )
    assert patched.status_code == 200
    denied = client.get("/api/devices", headers={"Authorization": f"Bearer {user_token}"})
    assert denied.status_code == 401


def test_usage_commit_failure_does_not_double_count(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    account_id = database.execute("SELECT id FROM accounts").fetchone()["id"]
    metrics = {
        "turns_total": 2,
        "token_usage": [
            {"model": "demo", "prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        ],
    }
    now = time.time()
    with pytest.raises(RuntimeError):
        apply_usage(database, account_id, metrics, now, fail_before_commit=True)
    apply_usage(database, account_id, metrics, now)
    row = database.execute("SELECT * FROM usage_daily").fetchone()
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 4
    assert row["turns"] == 2
    assert "total_tokens" not in row.keys()
    assert row["day"] == shanghai_day(now)
    apply_usage(
        database,
        account_id,
        {
            "turns_total": 3,
            "token_usage": [
                {"model": "demo", "prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
            ],
        },
        now,
    )
    row = database.execute("SELECT * FROM usage_daily").fetchone()
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 5
    assert row["turns"] == 3


def test_negative_usage_delta_is_treated_as_reset(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    account_id = database.execute("SELECT id FROM accounts").fetchone()["id"]
    now = time.time()
    apply_usage(
        database,
        account_id,
        {"turns_total": 4, "token_usage": [{"model": "", "prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10}]},
        now,
    )
    apply_usage(
        database,
        account_id,
        {"turns_total": 1, "token_usage": [{"model": "", "prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}]},
        now,
    )
    row = database.execute("SELECT * FROM usage_daily").fetchone()
    assert row["input_tokens"] == 11
    assert row["model"] == ""


def test_worker_spec_has_no_published_ports_and_health_timeout(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    account_id = database.execute("SELECT id FROM accounts").fetchone()["id"]
    docker = FakeDocker()
    manager = WorkerManager(docker, database, tmp_path / "accounts", "xingyao-worker:test")
    spec = container_spec(account_id, "image", Path("/data"), "xingyao")
    assert spec["name"] == f"xingyao-worker-{account_id}"
    assert spec["ports"] == {}
    clock = {"now": 0}

    def monotonic():
        clock["now"] += 61
        return clock["now"]

    result = manager.ensure_running(account_id, health=lambda _url: {"ok": False}, clock=monotonic)
    assert result["error"] == "worker_lost"
    assert docker.running


def test_password_change_bumps_epoch_without_touching_device_tokens(tmp_path):
    database = connect(tmp_path / "gateway.db")
    init_admin(database, "root", "secret-secret")
    account = database.execute("SELECT * FROM accounts").fetchone()
    database.execute(
        "INSERT INTO devices(id, account_id, name, token_hash, revoked, created_at) VALUES (?, ?, ?, ?, 0, 1)",
        ("dev", account["id"], "家里的台式机", "hash",),
    )
    database.commit()
    bump_epoch(database, account["id"])
    device = database.execute("SELECT revoked FROM devices").fetchone()
    assert int(device["revoked"]) == 0
    assert read_access(database, issue_tokens(database, account, "jwt-secret-jwt-secret-jwt-secret")["access_token"], "jwt-secret-jwt-secret-jwt-secret") is None
