from __future__ import annotations

from gateway.auth import init_admin, issue_remote_token, read_remote_access
from gateway.db import connect
from gateway.relay import RelayRegistry


def test_remote_token_is_bound_to_one_device(tmp_path):
    database = connect(tmp_path / "gateway.db")
    account_id = init_admin(database, "admin", "admin-password")
    registry = RelayRegistry()
    device_id, _ = registry.create_device(database, account_id, "desktop")
    other_id, _ = registry.create_device(database, account_id, "other")
    account = database.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    secret = "test-secret-" * 4
    issued = issue_remote_token(database, account, secret, device_id)
    principal = read_remote_access(database, issued["token"], secret, device_id)
    assert principal is not None
    assert principal.account_id == account_id
    assert read_remote_access(database, issued["token"], secret, other_id) is None
    assert read_remote_access(database, issued["token"], "wrong-secret-" * 4, device_id) is None
