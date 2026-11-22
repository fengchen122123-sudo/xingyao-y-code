import time

import pytest

from core.lan_server import LanServer


@pytest.fixture
def srv():
    s = LanServer(bridge=None)
    s.port = 8765
    s.lan_ip = "192.168.1.5"
    return s


def test_get_bind_info_reuses_unexpired_token(srv):
    first = srv.get_bind_info()
    second = srv.get_bind_info()
    assert first["token"] == second["token"]
    assert first["url"] == second["url"]
    assert first["qr"] == second["qr"]


def test_get_bind_info_force_new_reissues_token(srv):
    first = srv.get_bind_info()
    second = srv.get_bind_info(force_new=True)
    assert second["token"] != first["token"]
    assert not srv._verify_token(first["token"])


def test_get_bind_info_auto_reissues_expired_token(srv):
    old = srv.get_bind_info()
    srv._token_expires = time.time() - 1
    new = srv.get_bind_info()
    assert new["token"] != old["token"]
    assert srv._verify_token(new["token"])


def test_expires_in_is_remaining_seconds(srv):
    srv._ttl = 1000
    srv.get_bind_info()
    time.sleep(1.1)
    info = srv.get_bind_info()
    assert 0 < info["expires_in"] < 1000


def test_token_persisted_and_reloaded(srv, tmp_path, monkeypatch):
    from core import lan_server as lan_mod

    saved = {}

    def fake_save(key, value):
        saved[key] = value
        return True

    monkeypatch.setattr(lan_mod, "_save_config_value", fake_save)
    srv._ttl = 100
    first = srv.get_bind_info()
    assert saved["remote_pair_token"] == first["token"]
    assert saved["remote_pair_token_expires_at"] > time.time()

    restored = LanServer(bridge=None)
    restored._load_persisted_token(
        saved["remote_pair_token"], saved["remote_pair_token_expires_at"]
    )
    assert restored._token.decode() == first["token"]
    assert restored._token_expires == saved["remote_pair_token_expires_at"]
    assert restored._verify_token(first["token"])


def test_load_persisted_ignores_expired_token(srv):
    srv._load_persisted_token("stale-token", time.time() - 10)
    assert srv._token is None
    assert srv._verify_token("stale-token") is False


def test_load_persisted_ignores_empty_token(srv):
    srv._load_persisted_token("", time.time() + 100)
    assert srv._token is None


def test_ttl_zero_means_never_expires(srv):
    srv.set_ttl(0)
    info = srv.get_bind_info()
    assert info["expires_in"] == 0
    assert srv._verify_token(info["token"]) is True
    srv._token_expires = None
    time.sleep(1.1)
    again = srv.get_bind_info()
    assert again["token"] == info["token"]
    assert again["expires_in"] == 0


def test_load_persisted_accepts_permanent_token(srv):
    srv._load_persisted_token("forever-token", None)
    assert srv._token is not None
    assert srv._verify_token("forever-token") is True
