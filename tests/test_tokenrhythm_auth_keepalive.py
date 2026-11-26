"""基元律动会话保活与 cookie 回写。

覆盖三个回归点：
1. cookie jar 变化才回写，避免每个请求都做一次 DPAPI 加密；
2. 服务端轮换后的新 cookie 会落盘，重启不再恢复作废快照；
3. 网络异常不误清本地会话，保活间隔可配置且有下限。
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from core import tokenrhythm_auth as auth


_ENCRYPTED_PREFIX = "enc:"


def _fake_protect(value: str) -> str:
    return _ENCRYPTED_PREFIX + base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _fake_unprotect(value: str) -> str:
    raw = str(value)
    if not raw.startswith(_ENCRYPTED_PREFIX):
        raise ValueError("不是加密内容")
    return base64.b64decode(raw[len(_ENCRYPTED_PREFIX) :]).decode("utf-8")


@pytest.fixture
def store(monkeypatch, tmp_path):
    from core import credentials

    path = tmp_path / "tr_session.json"
    monkeypatch.setattr(auth, "_SESSION_FILE", path)
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    monkeypatch.setattr(credentials, "protect_secret", _fake_protect)
    monkeypatch.setattr(credentials, "unprotect_secret", _fake_unprotect)
    monkeypatch.setattr(auth, "_COOKIE_DIGEST", "")
    monkeypatch.setattr(auth, "_KEEPALIVE_THREAD", None)
    yield path
    auth._stop_keepalive()
    auth._COOKIE_DIGEST = ""
    auth._STATE.update({"logged_in": False, "restored": False})


def _client(*triples):
    jar = [
        SimpleNamespace(
            name=name,
            value=value,
            domain="tokenrhythm.studio",
            path="/",
            expires=expires,
        )
        for name, value, expires in triples
    ]
    return SimpleNamespace(cookies=SimpleNamespace(jar=jar))


def _restored_values():
    fresh = auth.httpx.Client()
    assert auth._load_session(fresh) is True
    return {cookie.name: cookie.value for cookie in fresh.cookies.jar}


def test_save_if_changed_skips_identical_jar(store):
    client = _client(("tr_session", "v1", None), ("tr_csrf", "c1", None))

    auth._save_session_if_changed(client)
    first = store.read_text(encoding="utf-8")
    auth._save_session_if_changed(client)

    assert store.read_text(encoding="utf-8") == first


def test_save_if_changed_persists_rotated_cookie(store):
    auth._save_session_if_changed(_client(("tr_session", "old", None)))
    auth._save_session_if_changed(_client(("tr_session", "new", None)))

    assert _restored_values()["tr_session"] == "new"


def test_request_success_rewrites_rotated_cookie(store, monkeypatch):
    jar = [
        SimpleNamespace(
            name="tr_session",
            value="before",
            domain="tokenrhythm.studio",
            path="/",
            expires=None,
        )
    ]

    class _RotatingClient:
        def __init__(self):
            self.cookies = SimpleNamespace(jar=jar, get=lambda name: None)

        def request(self, *args, **kwargs):
            jar[0].value = "after"
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"code": 0, "message": "ok", "data": {"ok": True}},
            )

    monkeypatch.setattr(auth, "_get_client", lambda: _RotatingClient())

    auth._request("GET", "/api/auth/me")

    assert _restored_values()["tr_session"] == "after"


def test_network_failure_keeps_saved_session(store, monkeypatch):
    auth._save_session(_client(("tr_session", "v1", None)))
    auth._STATE.update({"logged_in": False, "restored": True})
    monkeypatch.setattr(auth, "_get_client", lambda: SimpleNamespace())

    def boom(*args, **kwargs):
        raise auth.TokenRhythmAuthError("网络请求失败，请检查网络后重试：timeout")

    monkeypatch.setattr(auth, "_request", boom)
    auth._invalidate_health_cache()

    result = auth.session_health(force=True)

    assert result["ok"] is False
    assert store.exists(), "网络异常绝不能清掉本地会话"


def test_server_rejection_clears_saved_session(store, monkeypatch):
    auth._save_session(_client(("tr_session", "v1", None)))
    auth._STATE.update({"logged_in": False, "restored": True})
    monkeypatch.setattr(auth, "_get_client", lambda: SimpleNamespace())

    def denied(*args, **kwargs):
        raise auth.TokenRhythmAuthError("登录已过期", code="UNAUTHORIZED", status=401)

    monkeypatch.setattr(auth, "_request", denied)
    auth._invalidate_health_cache()

    result = auth.session_health(force=True)

    assert result["logged_in"] is False
    assert not store.exists()


def test_keepalive_interval_defaults_to_25_minutes(monkeypatch):
    monkeypatch.delenv("YCODE_TR_KEEPALIVE_MINUTES", raising=False)

    assert auth._keepalive_interval_seconds() == 25 * 60


def test_keepalive_interval_can_be_disabled(monkeypatch):
    monkeypatch.setenv("YCODE_TR_KEEPALIVE_MINUTES", "0")

    assert auth._keepalive_interval_seconds() == 0.0


def test_keepalive_interval_has_60_second_floor(monkeypatch):
    monkeypatch.setenv("YCODE_TR_KEEPALIVE_MINUTES", "0.1")

    assert auth._keepalive_interval_seconds() == 60.0


def test_ensure_keepalive_is_idempotent(store, monkeypatch):
    monkeypatch.setattr(auth, "_keepalive_interval_seconds", lambda: 3600.0)

    auth._ensure_keepalive()
    first = auth._KEEPALIVE_THREAD
    auth._ensure_keepalive()

    assert first is not None and first.is_alive()
    assert auth._KEEPALIVE_THREAD is first