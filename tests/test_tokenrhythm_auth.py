import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from core import tokenrhythm_auth as auth


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _ok(data):
    return {"code": 0, "message": "ok", "data": data}


_ENCRYPTED_PREFIX = "enc:"


def _fake_protect(value):
    return _ENCRYPTED_PREFIX + base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _fake_unprotect(value):
    raw = str(value)
    if not raw.startswith(_ENCRYPTED_PREFIX):
        raise ValueError("不是加密内容")
    return base64.b64decode(raw[len(_ENCRYPTED_PREFIX) :]).decode("utf-8")


def _me(user_id="u1", name="木偶", **extra):
    return _FakeResponse(200, _ok({"id": user_id, "name": name, **extra}))


def _provider(provider_id="p1", name="基元律动", api_key="", base_url="https://tokenrhythm.studio/v1"):
    return {
        "id": provider_id,
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "protocol": "openai",
        "models": [],
    }


def _patch_providers(monkeypatch, *providers):
    import config

    monkeypatch.setattr(config, "_load", lambda: {"providers": list(providers)})


@pytest.fixture
def http(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_SESSION_FILE", tmp_path / "tr_session.json")
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    from core import credentials

    monkeypatch.setattr(credentials, "protect_secret", _fake_protect)
    monkeypatch.setattr(credentials, "unprotect_secret", _fake_unprotect)
    monkeypatch.setattr(auth, "_delete_tr_providers", lambda keep_id="": 0)
    # 绑定时会给厂商打托管标记（写 config.json）：测试里必须隔离，
    # 否则每个用例都会重写一遍用户真实的模型配置。
    monkeypatch.setattr(auth, "_mark_provider_managed", lambda provider_id: None)
    auth.logout()
    holder = {"client": None, "responses": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = []
            self.closed = False
            self.cookies = httpx.Cookies()
            holder["client"] = self

        def request(self, method, url, json=None, headers=None, timeout=None):  # noqa: A002
            self.calls.append(
                {
                    "method": method,
                    "url": url,
                    "json": json,
                    "headers": dict(headers or {}),
                    "timeout": timeout,
                }
            )
            if not holder["responses"]:
                raise AssertionError(f"未预期的请求：{method} {url}")
            queued = holder["responses"].pop(0)
            if isinstance(queued, Exception):
                raise queued
            return queued

        def close(self):
            self.closed = True

    monkeypatch.setattr(httpx, "Client", FakeClient)

    def queue(*responses):
        holder["responses"].extend(responses)

    yield SimpleNamespace(holder=holder, queue=queue)
    auth.logout()


def _requests(http):
    return http.holder["client"].calls


@pytest.fixture
def session_store(monkeypatch, tmp_path):
    from core import credentials

    path = tmp_path / "tr_session.json"
    monkeypatch.setattr(auth, "_SESSION_FILE", path)
    # 绑定文件也指向 tmp_path：logout 现在会读 binding 并向服务端删 Key，
    # 绝不能读写用户真实的 ~/.ycode/secrets。
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    monkeypatch.setattr(
        credentials,
        "protect_secret",
        lambda value: "enc:" + base64.b64encode(str(value).encode("utf-8")).decode("ascii"),
    )
    monkeypatch.setattr(
        credentials,
        "unprotect_secret",
        lambda value: (
            base64.b64decode(str(value)[4:]).decode("utf-8")
            if str(value).startswith("enc:")
            else str(value)
        ),
    )
    yield path
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})


def _client_with_cookies(*pairs):
    jar = [
        SimpleNamespace(
            name=name,
            value=value,
            domain="tokenrhythm.studio",
            path="/",
            expires=expires,
        )
        for name, value, expires in pairs
    ]
    return SimpleNamespace(cookies=SimpleNamespace(jar=jar))


def test_session_saved_encrypted_and_restored(session_store):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None), ("tr_csrf", "def456", None)))

    assert session_store.exists()
    raw = session_store.read_text(encoding="utf-8")
    assert "abc123" not in raw, "cookie 值绝不能明文落盘"

    fresh = httpx.Client()
    assert auth._load_session(fresh) is True
    values = {cookie.name: cookie.value for cookie in fresh.cookies.jar}
    assert values["tr_session"] == "abc123"
    assert values["tr_csrf"] == "def456"


def test_session_skips_expired_cookies(session_store):
    auth._save_session(
        _client_with_cookies(
            ("old", "x", int(time.time()) - 60),
            ("new", "y", int(time.time()) + 3600),
        )
    )

    fresh = httpx.Client()
    auth._load_session(fresh)
    names = {cookie.name for cookie in fresh.cookies.jar}
    assert "new" in names
    assert "old" not in names


def test_logout_removes_saved_session(session_store):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    assert session_store.exists()

    auth.logout()

    assert not session_store.exists()


def test_account_status_restores_login_from_saved_session(http, session_store):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None), ("tr_csrf", "def456", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})

    http.queue(_FakeResponse(200, _ok({"user": {"id": "u1", "name": "木偶"}})))
    result = auth.account_status()

    assert result["logged_in"] is True
    assert result["user"]["id"] == "u1"
    assert len(_requests(http)) == 1


def test_account_status_clears_invalid_session(http, session_store):
    auth._save_session(_client_with_cookies(("tr_session", "expired", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})

    http.queue(_FakeResponse(401, {"code": "UNAUTHORIZED", "message": "登录已过期", "data": None}))
    result = auth.account_status()

    assert result["logged_in"] is False
    assert not session_store.exists()


def test_password_login_saves_session(http, session_store):
    http.queue(
        _FakeResponse(200, _ok({"user": {"id": "u1"}})),
        _FakeResponse(200, _ok({"user": {"id": "u1"}})),
    )
    http.holder["client"] = None

    client = auth._get_client()
    client.cookies.set("tr_session", "abc123", domain="tokenrhythm.studio", path="/")

    auth.password_login("13800000000", "pwd")

    assert session_store.exists()
    raw = session_store.read_text(encoding="utf-8")
    assert "abc123" not in raw



def test_password_login_success(http):
    http.queue(
        _FakeResponse(200, _ok({"user": {"id": "u1", "phone": "13800000000"}})),
        _FakeResponse(200, _ok({"user": {"id": "u1", "phone": "13800000000"}})),
    )

    result = auth.password_login("13800000000", "p@ssw0rd")

    calls = _requests(http)
    assert len(calls) == 2
    login_call, me_call = calls
    assert login_call["method"] == "POST"
    assert login_call["url"] == "https://tokenrhythm.studio/api/auth/login"
    assert login_call["json"] == {"account": "13800000000", "password": "p@ssw0rd"}
    headers = login_call["headers"]
    assert headers["content-type"] == "application/json"
    assert headers["origin"] == "https://tokenrhythm.studio"
    assert "tokenrhythm.studio" in headers["referer"]
    assert headers["sec-fetch-site"] == "same-origin"
    assert login_call["timeout"] == 20.0

    assert me_call["method"] == "GET"
    assert me_call["url"] == "https://tokenrhythm.studio/api/auth/me"

    assert result["ok"] is True
    assert result["user"]["id"] == "u1"


def test_password_login_server_error_keeps_message(http):
    http.queue(
        _FakeResponse(
            400,
            {"code": "BAD_REQUEST", "message": "账号或密码错误", "data": None},
        )
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.password_login("13800000000", "wrong")

    assert "账号或密码错误" in str(excinfo.value)
    assert "BAD_REQUEST" in str(excinfo.value)
    assert len(_requests(http)) == 1, "登录失败不应继续调用 /api/auth/me"


def test_send_sms_code_returns_challenge(http):
    http.queue(
        _FakeResponse(
            200,
            _ok(
                {
                    "challengeId": "ch-1",
                    "intervalSeconds": 60,
                    "ttlSeconds": 300,
                    "maskedTarget": "138****0000",
                }
            ),
        )
    )

    result = auth.send_sms_code("13800000000")

    assert result == {
        "challenge_id": "ch-1",
        "interval_seconds": 60,
        "ttl_seconds": 300,
        "masked_target": "138****0000",
    }
    assert auth._STATE["challenge_id"] == "ch-1"
    call = _requests(http)[0]
    assert call["url"] == "https://tokenrhythm.studio/api/auth/sms/send"
    assert call["json"] == {"scene": "login_register", "phone": "13800000000"}
    assert "captchaVerifyParam" not in call["json"]


def test_send_sms_code_with_captcha_verify_param(http):
    http.queue(_FakeResponse(200, _ok({"challengeId": "ch-2", "intervalSeconds": 60})))

    auth.send_sms_code("13800000000", "captcha-abc123-=_.%2B/end")

    body = _requests(http)[0]["json"]
    assert body == {
        "scene": "login_register",
        "phone": "13800000000",
        "captchaVerifyParam": "captcha-abc123-=_.%2B/end",
    }


def test_send_sms_code_empty_captcha_omits_field(http):
    http.queue(
        _FakeResponse(200, _ok({"challengeId": "ch-3", "intervalSeconds": 60})),
        _FakeResponse(200, _ok({"challengeId": "ch-4", "intervalSeconds": 60})),
    )

    auth.send_sms_code("13800000000", "")
    auth.send_sms_code("13800000000", "   ")

    first, second = _requests(http)
    assert first["json"] == {"scene": "login_register", "phone": "13800000000"}
    assert second["json"] == {"scene": "login_register", "phone": "13800000000"}
    assert "captchaVerifyParam" not in first["json"]
    assert "captchaVerifyParam" not in second["json"]


@pytest.mark.parametrize(
    "code",
    ["CAPTCHA_REQUIRED", "CAPTCHA_INVALID", "CAPTCHA_SERVICE_UNAVAILABLE"],
)
def test_send_sms_code_captcha_errors_guide_user(http, code):
    http.queue(
        _FakeResponse(
            400,
            {"code": code, "message": "请先完成图形验证码", "data": None},
        )
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.send_sms_code("13800000000", "captcha-param")

    message = str(excinfo.value)
    assert "安全验证" in message
    assert "密码登录" in message
    assert "请先完成图形验证码" in message, "必须保留服务端原文"
    assert excinfo.value.code == code
    assert excinfo.value.server_message == "请先完成图形验证码"


def test_send_sms_code_captcha_required(http):
    http.queue(
        _FakeResponse(
            400,
            {
                "code": "CAPTCHA_REQUIRED",
                "message": "请先完成图形验证码",
                "data": None,
            },
        )
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.send_sms_code("13800000000")

    message = str(excinfo.value)
    assert "安全验证" in message
    assert "密码登录" in message
    assert "请先完成图形验证码" in message


def test_sms_login_three_steps_in_order(http):
    http.queue(
        _FakeResponse(200, _ok({"challengeId": "ch-9", "intervalSeconds": 60})),
        _FakeResponse(200, _ok({"verificationToken": "vt-1"})),
        _FakeResponse(200, _ok({"termsVersion": "v2026-01"})),
        _FakeResponse(200, _ok({"user": {"id": "u9", "nickname": "测试"}})),
    )

    auth.send_sms_code("13900000000")
    _requests(http).clear()

    result = auth.sms_login("13900000000", "123456")

    calls = _requests(http)
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/auth/sms/verify",
        "https://tokenrhythm.studio/api/legal-documents/current",
        "https://tokenrhythm.studio/api/auth/login-or-register",
    ]
    assert calls[0]["json"] == {"challengeId": "ch-9", "code": "123456"}
    assert calls[1]["method"] == "GET"
    assert calls[2]["json"] == {
        "channel": "sms",
        "verificationToken": "vt-1",
        "termsAccepted": True,
        "termsVersion": "v2026-01",
    }
    assert result["ok"] is True
    assert result["user"]["id"] == "u9"
    assert auth._STATE["challenge_id"] is None


def _prepare_sms_login(http):
    http.queue(_FakeResponse(200, _ok({"challengeId": "ch-9", "intervalSeconds": 60})))
    auth.send_sms_code("13900000000")
    _requests(http).clear()


def test_sms_login_with_invite_code_passes_invite_code(http):
    _prepare_sms_login(http)
    http.queue(
        _FakeResponse(200, _ok({"verificationToken": "vt-1"})),
        _FakeResponse(200, _ok({"termsVersion": "v2026-01"})),
        _FakeResponse(200, _ok({"user": {"id": "u9", "nickname": "测试"}})),
    )

    result = auth.sms_login("13900000000", "123456", "INV-AbC123")

    body = _requests(http)[2]["json"]
    assert body["inviteCode"] == "INV-AbC123", "邀请码不做大小写 / 格式改写"
    assert result["ok"] is True


@pytest.mark.parametrize("invite_code", [None, "", "   "])
def test_sms_login_blank_invite_code_omits_field(http, invite_code):
    _prepare_sms_login(http)
    http.queue(
        _FakeResponse(200, _ok({"verificationToken": "vt-1"})),
        _FakeResponse(200, _ok({"termsVersion": "v2026-01"})),
        _FakeResponse(200, _ok({"user": {"id": "u9"}})),
    )

    auth.sms_login("13900000000", "123456", invite_code)

    body = _requests(http)[2]["json"]
    assert "inviteCode" not in body
    assert body == {
        "channel": "sms",
        "verificationToken": "vt-1",
        "termsAccepted": True,
        "termsVersion": "v2026-01",
    }


def test_sms_login_invite_code_keeps_other_fields(http):
    _prepare_sms_login(http)
    http.queue(
        _FakeResponse(200, _ok({"verificationToken": "vt-2"})),
        _FakeResponse(200, _ok({"termsVersion": "v2026-02"})),
        _FakeResponse(200, _ok({"user": {"id": "u9"}})),
    )

    auth.sms_login("13900000000", "123456", "INVITE2026")

    call = _requests(http)[2]
    assert call["url"] == "https://tokenrhythm.studio/api/auth/login-or-register"
    assert call["json"] == {
        "channel": "sms",
        "verificationToken": "vt-2",
        "termsAccepted": True,
        "termsVersion": "v2026-02",
        "inviteCode": "INVITE2026",
    }


def _patch_existing_provider(monkeypatch, api_key="dpapi:stored", base_url="https://tokenrhythm.studio/v1"):
    import config
    from core import credentials

    monkeypatch.setattr(
        config,
        "_load",
        lambda: {
            "providers": [
                {
                    "id": "p1",
                    "name": "基元律动",
                    "base_url": base_url,
                    "api_key": api_key,
                    "protocol": "openai",
                    "models": [],
                }
            ]
        },
    )
    return credentials


def _keys_ok(*key_ids, container="list"):
    items = [
        {"id": key_id, "name": "星瑶Code", "maskedKey": "sk-****"}
        for key_id in key_ids
    ]
    return _FakeResponse(200, _ok(items if container == "list" else {"items": items}))


def _unexpected(name):

    def _boom(*args, **kwargs):
        raise AssertionError(f"不应调用 {name}")

    return _boom


def _creation_recorder(model_manager, monkeypatch):
    calls = {"added": [], "models": []}

    def _add_provider(name, base_url, api_key, protocol):
        calls["added"].append((name, base_url, api_key, protocol))
        return "p-new"

    def _add_model(*args, **kwargs):
        calls["models"].append((args, kwargs))
        return "m-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    monkeypatch.setattr(model_manager, "update_provider", _unexpected("update_provider"))
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    return calls


def test_ensure_bound_without_binding_always_creates_key(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p1", "基元律动", _fake_protect("sk-manual")))
    assert auth._load_binding() is None

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _FakeResponse(200, _ok({"key": "sk-dedicated", "id": "k1"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["old_key_deleted"] is False
    assert result["reason"] == "已自动创建并绑定 Key"
    assert result["provider_id"] == "p-new"
    assert result["provider_name"] == "基元律动（木偶）"
    assert calls["models"] == []
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-dedicated", "openai")
    ]
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"


def test_ensure_bound_never_probes_local_key_when_unbound(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p1", "基元律动", _fake_protect("sk-local")))
    monkeypatch.setattr(
        auth, "_provider_key_usable", _unexpected("_provider_key_usable")
    )

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-new", "id": "k1"})))

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert calls["models"] == []
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_without_provider_creates_provider_without_model(http, monkeypatch):
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {"models": []}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    def _add_model(*args, **kwargs):
        calls["models"].append((args, kwargs))
        return "m-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应更新厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created"})))

    result = auth.ensure_bound()

    assert result == {
        "created": True,
        "key_created": True,
        "old_key_deleted": False,
        "provider_id": "p-new",
        "model_added": False,
        "reason": "已自动创建并绑定 Key",
        "account_id": "u1",
        "account_name": "木偶",
        "provider_name": "基元律动（木偶）",
    }
    create_call = _requests(http)[1]
    assert create_call["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert create_call["json"] == {"name": "星瑶Code"}
    assert calls["models"] == []
    assert calls["add_provider"] == (
        "基元律动（木偶）",
        "https://tokenrhythm.studio/v1",
        "sk-created",
        "openai",
    )


def test_ensure_bound_new_provider_when_local_key_invalid(http, monkeypatch):
    _patch_existing_provider(monkeypatch, api_key="enc:bad-key")

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _FakeResponse(200, _ok({"apiKey": "sk-new"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    assert result["model_added"] is False
    assert calls["models"] == []
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_missing_plaintext_key_raises(http, monkeypatch):
    _patch_providers(monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _FakeResponse(200, _ok({"id": "k1", "maskedKey": "sk-****"})),
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.ensure_bound()

    assert "未返回明文 Key" in str(excinfo.value)


def test_ensure_bound_first_bind_creates_key_provider_and_binding(http, monkeypatch):
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {"models": []}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    def _add_model(*args, **kwargs):
        calls["models"].append((args, kwargs))
        return "m-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应更新厂商")),
    )
    monkeypatch.setattr(
        model_manager,
        "delete_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应删除厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created", "id": "k1"})))

    result = auth.ensure_bound()

    assert result == {
        "created": True,
        "key_created": True,
        "old_key_deleted": False,
        "provider_id": "p-new",
        "model_added": False,
        "reason": "已自动创建并绑定 Key",
        "account_id": "u1",
        "account_name": "木偶",
        "provider_name": "基元律动（木偶）",
    }
    assert calls["add_provider"] == (
        "基元律动（木偶）",
        "https://tokenrhythm.studio/v1",
        "sk-created",
        "openai",
    )
    assert calls["models"] == []
    binding = auth._load_binding()
    assert binding["v"] == 1
    assert binding["account_id"] == "u1"
    assert binding["account_name"] == "木偶"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"
    assert isinstance(binding["bound_at"], int)
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_same_account_second_call_skips_key_creation(http, monkeypatch):
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-good"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k1",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    monkeypatch.setattr(model_manager, "update_provider", _unexpected("update_provider"))
    monkeypatch.setattr(
        model_manager, "add_model_to_provider", _unexpected("add_model_to_provider")
    )
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _keys_ok("k1", "k-other", container="items"),
        _FakeResponse(200, _ok({"data": []})),
    )

    result = auth.ensure_bound()

    assert result["created"] is False
    assert result["key_created"] is False
    assert result["old_key_deleted"] is False
    assert result["provider_id"] == "p1"
    assert result["provider_name"] == "基元律动（木偶）"
    assert result["reason"] == "复用当前账号已绑定的 Key"
    calls = _requests(http)
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/v1/models",
    ]
    assert calls[2]["headers"]["authorization"] == "Bearer sk-good"
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["key_id"] == "k1"
    assert binding["bound_at"] == 1700000000


def test_ensure_bound_same_account_recreates_key_when_local_key_invalid(http, monkeypatch):
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-broken"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k1",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    calls = {}

    def _update_provider(provider_id, **kwargs):
        calls["update"] = (provider_id, kwargs)

    monkeypatch.setattr(model_manager, "update_provider", _update_provider)
    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _keys_ok("k1"),
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "invalid key", "data": None}),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p1"
    assert calls["update"] == (
        "p1",
        {
            "name": "基元律动（木偶）",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "sk-new",
            "protocol": "openai",
        },
    )
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/v1/models",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["key_id"] == "k2"
    assert binding["bound_at"] > 1700000000


def test_ensure_bound_same_account_deleted_key_creates_new_key(http, monkeypatch):
    _patch_providers(
        monkeypatch, _provider("p1", "基元律动（木偶）", _fake_protect("sk-old"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p1",
            "key_id": "k-old",
            "bound_at": 1700000000,
        }
    )

    from core import model_manager

    calls = {}

    def _update_provider(provider_id, **kwargs):
        calls["update"] = (provider_id, kwargs)

    monkeypatch.setattr(model_manager, "update_provider", _update_provider)
    monkeypatch.setattr(model_manager, "add_provider", _unexpected("add_provider"))
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        _keys_ok("k-other"),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k-new"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["old_key_deleted"] is False
    assert result["provider_id"] == "p1"
    assert calls["update"][0] == "p1"
    assert calls["update"][1]["api_key"] == "sk-new"
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["key_id"] == "k-new"
    assert binding["bound_at"] > 1700000000


def test_ensure_bound_switches_account_rebuilds_provider(http, monkeypatch):
    _patch_providers(
        monkeypatch, _provider("p-old", "基元律动（旧号）", _fake_protect("sk-old"))
    )
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "旧号",
            "provider_id": "p-old",
            "key_id": "k-old",
        }
    )

    from core import model_manager

    calls = {"deleted": [], "added": []}

    def _delete_provider(provider_id):
        calls["deleted"].append(provider_id)
        calls["requests_at_delete"] = [call["url"] for call in _requests(http)]

    def _add_provider(name, base_url, api_key, protocol):
        calls["added"].append((name, base_url, api_key, protocol))
        return "p-new"

    monkeypatch.setattr(model_manager, "delete_provider", _delete_provider)
    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    monkeypatch.setattr(
        model_manager,
        "update_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("换账号时不应复用旧厂商")),
    )
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u2", "新号"),
        _FakeResponse(200, _ok({"status": "deleted"})),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["account_switched"] is True
    assert result["old_key_deleted"] is True
    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    assert result["account_id"] == "u2"
    assert result["account_name"] == "新号"
    assert result["provider_name"] == "基元律动（新号）"
    calls_list = _requests(http)
    assert calls_list[1]["method"] == "POST"
    assert calls_list[1]["url"] == "https://tokenrhythm.studio/api/api-keys/k-old/delete"
    assert calls_list[2]["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert calls_list[2]["json"] == {"name": "星瑶Code"}
    assert calls["requests_at_delete"] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys/k-old/delete",
    ]
    assert calls["deleted"] == ["p-old"]
    assert calls["added"] == [
        ("基元律动（新号）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]
    binding = auth._load_binding()
    assert binding["account_id"] == "u2"
    assert binding["account_name"] == "新号"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k2"
    assert [call["url"] for call in calls_list] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys/k-old/delete",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_switch_account_keeps_going_when_old_key_delete_fails(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p-old", "基元律动（旧号）", ""))
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "旧号",
            "provider_id": "p-old",
            "key_id": "k-old",
        }
    )

    from core import model_manager

    deleted = []
    monkeypatch.setattr(model_manager, "delete_provider", lambda pid: deleted.append(pid))
    monkeypatch.setattr(model_manager, "add_provider", lambda *a, **k: "p-new")
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u2", "新号"),
        _FakeResponse(500, {"code": "INTERNAL_ERROR", "message": "boom", "data": None}),
        _FakeResponse(200, _ok({"key": "sk-new", "id": "k2"})),
    )

    result = auth.ensure_bound()

    assert result["account_switched"] is True
    assert result["old_key_deleted"] is False
    assert result["key_created"] is True
    assert deleted == ["p-old"]
    binding = auth._load_binding()
    assert binding["account_id"] == "u2"
    assert binding["key_id"] == "k2"


def test_ensure_bound_corrupt_binding_treated_as_unbound(http, monkeypatch):
    _patch_providers(monkeypatch)
    auth._BINDING_FILE.write_text("这不是加密内容", encoding="utf-8")
    assert auth._load_binding() is None

    from core import model_manager

    monkeypatch.setattr(model_manager, "add_provider", lambda *a, **k: "p-new")
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    auth._STATE["logged_in"] = True
    http.queue(_me("u1", "木偶"), _FakeResponse(200, _ok({"key": "sk-created", "id": "k1"})))

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["provider_id"] == "p-new"


def test_ensure_bound_requires_login(http):
    auth._STATE["logged_in"] = False

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.ensure_bound()

    assert "请先登录基元律动账号" in str(excinfo.value)
    client = http.holder["client"]
    assert client is None or client.calls == []


def _spy_local_cleanup(monkeypatch, http):
    order = {}
    original_clear_binding = auth._clear_binding
    original_clear_session = auth._clear_session

    def _clear_binding():
        order["clear_binding"] = len(_requests(http))
        original_clear_binding()

    def _clear_session():
        order["clear_session"] = len(_requests(http))
        original_clear_session()

    monkeypatch.setattr(auth, "_clear_binding", _clear_binding)
    monkeypatch.setattr(auth, "_clear_session", _clear_session)
    return order


def test_logout_deletes_key_before_clearing_local(http, monkeypatch):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    assert auth._SESSION_FILE.exists()
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "p1", "key_id": "k1"}
    )
    assert auth._load_binding()["key_id"] == "k1"
    auth._STATE["logged_in"] = True
    order = _spy_local_cleanup(monkeypatch, http)
    http.queue(_FakeResponse(200, _ok(None)))

    auth.logout()

    calls = _requests(http)
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/api-keys/k1/delete"
    ]
    assert calls[0]["method"] == "POST"
    assert order == {"clear_binding": 1, "clear_session": 1}
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False


def test_logout_removes_tokenrhythm_providers(http, monkeypatch):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "p1", "key_id": "k1"}
    )
    auth._STATE["logged_in"] = True
    removed_calls = []
    monkeypatch.setattr(
        auth,
        "_delete_tr_providers",
        lambda keep_id="": removed_calls.append(keep_id) or 1,
    )
    http.queue(_FakeResponse(200, _ok({})))

    result = auth.logout()

    assert removed_calls == [""]
    assert result["providers_removed"] == 1


def _patch_model_config(monkeypatch, providers):
    from core import model_manager

    deleted: list[str] = []
    monkeypatch.setattr(model_manager, "load_config", lambda: {"providers": providers})
    monkeypatch.setattr(model_manager, "delete_provider", lambda pid: deleted.append(pid))
    return deleted


def test_delete_tr_providers_keeps_user_manual_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    providers = [
        {
            "id": "manual",
            "name": "基元律动",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
        },
        {
            "id": "auto",
            "name": "基元律动（木偶）",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
            auth.MANAGED_PROVIDER_FLAG: auth.MANAGED_PROVIDER_VALUE,
        },
    ]
    deleted = _patch_model_config(monkeypatch, providers)
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "auto", "key_id": "k1"}
    )

    removed = auth._delete_tr_providers()

    assert deleted == ["auto"]
    assert removed == 1


def test_delete_tr_providers_removes_bound_legacy_provider_only(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    providers = [
        {
            "id": "manual",
            "name": "基元律动",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
        },
        {
            "id": "legacy",
            "name": "基元律动（木偶）",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
        },
    ]
    deleted = _patch_model_config(monkeypatch, providers)
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "legacy", "key_id": "k1"}
    )

    removed = auth._delete_tr_providers()

    assert deleted == ["legacy"]
    assert removed == 1


def test_bound_provider_name_falls_back_to_account_suffix():
    assert auth._bound_provider_name("木偶") == "基元律动（木偶）"
    assert auth._bound_provider_name("") == "基元律动（账号）"
    assert auth._bound_provider_name(None) == "基元律动（账号）"


def test_bound_provider_name_truncates_long_nickname():
    assert auth._bound_provider_name("基元律动请送我token我") == "基元律动（基元律动请送…）"
    assert auth._bound_provider_name("六个字符名字") == "基元律动（六个字符名字）"
    assert auth._bound_provider_name("六个字符名字多") == "基元律动（六个字符名字…）"
    assert auth._bound_provider_name("😀😀😀😀😀😀😀") == "基元律动（😀😀😀😀😀😀…）"


def test_resolve_sync_provider_prefers_managed_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    providers = [
        {
            "id": "manual",
            "name": "基元律动",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
        },
        {
            "id": "auto",
            "name": "基元律动（木偶）",
            "base_url": "https://tokenrhythm.studio/v1",
            "api_key": "",
            "protocol": "openai",
            "models": [],
            auth.MANAGED_PROVIDER_FLAG: auth.MANAGED_PROVIDER_VALUE,
        },
    ]
    monkeypatch.setattr(auth, "_load_providers", lambda: providers)

    provider_id, name = auth._resolve_sync_provider(None)

    assert provider_id == "auto"
    assert name == "基元律动（木偶）"


def test_logout_clears_local_even_when_key_delete_fails(http, monkeypatch, caplog):
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    auth._save_binding(
        {"account_id": "u1", "account_name": "木偶", "provider_id": "p1", "key_id": "k1"}
    )
    auth._STATE["logged_in"] = True
    http.queue(
        _FakeResponse(500, {"code": "INTERNAL_ERROR", "message": "boom", "data": None})
    )

    with caplog.at_level(logging.WARNING, logger=auth.logger.name):
        result = auth.logout()
        assert result["ok"] is True
        assert result["had_key"] is True
        assert result["key_deleted"] is False

    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/api-keys/k1/delete"
    ]
    assert "删除失败" in caplog.text
    assert "k1" in caplog.text
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False


def test_logout_without_binding_sends_no_request(http):
    auth._STATE["logged_in"] = False

    auth.logout()

    client = http.holder["client"]
    assert client is None or client.calls == []


def test_account_status_without_session(http):
    result = auth.account_status()

    assert result == {"logged_in": False}
    # 现在会先建 Client 以便尝试恢复持久化会话，但绝不能发任何请求
    assert http.holder["client"].calls == []


def _login(http):
    auth._STATE["logged_in"] = True
    auth._get_client()


def _wallet_ok(**overrides):
    data = {
        "availableBalanceCny": "298.09757398",
        "frozenBalanceCny": "0.50",
        "currency": "CNY",
    }
    data.update(overrides)
    return _FakeResponse(200, _ok(data))


def _usage_ok(**overrides):
    summary = {
        "calls": 12,
        "totalTokens": 3456,
        "inputTokens": 2000,
        "outputTokens": 1456,
        "costCny": "1.23456789",
    }
    summary.update(overrides)
    return _FakeResponse(200, _ok({"summary": summary}))


_MULTIMODAL_URL = (
    "https://tokenrhythm.studio/api/usage/multimodal/overview"
    "?type=all&metric=cost&groupBy=model&range=today&fundingScope=WALLET"
)


def _multimodal_ok(cost="9.87654321"):
    return _FakeResponse(200, _ok({"summary": {"costCny": cost}}))


# 临时额度用例的时间基准：一律相对"现在"生成，绝不写死年份——写死 2099 会被
# "永久额度"阈值判成永久赠额，写死具体日期也会随时间流逝而失效。
_CREDIT_BASE = datetime.now(timezone.utc).replace(microsecond=0)


def _in(days: int) -> datetime:
    return _CREDIT_BASE + timedelta(days=days)


def _iso(moment: datetime, fraction: str = "") -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


_DEFAULT_EXPIRY = object()


def _credit(remaining="1.00000000", expires=_DEFAULT_EXPIRY, **overrides):
    item = {
        "grantedCny": "10.00000000",
        "remainingCny": remaining,
        "consumedCny": "0.00000000",
        "grantedAt": "2026-01-01T00:00:00Z",
        "expiresAt": _iso(_in(30)) if expires is _DEFAULT_EXPIRY else expires,
        "validityStartsAt": "2026-01-01T00:00:00Z",
        "lineType": "promo",
    }
    item.update(overrides)
    return item


def _credits_ok(items=None):
    return _FakeResponse(200, _ok({"items": list(items or [])}))


_CREDITS_URL = (
    "https://tokenrhythm.studio/api/wallet/expiring-credits"
    "?page=1&pageSize=100&includeInactive=false&includeModelCredits=true"
)


_ME_USER = {
    "id": "u1",
    "name": "冯同学",
    "nickname": "星瑶",
    "phone": "13800000000",
    "email": "me@example.com",
    "status": "active",
}


def test_account_overview_success(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok([_credit(remaining="5.00000000", expires=_iso(_in(30)))]),
    )

    result = auth.account_overview()

    calls = _requests(http)
    assert [call["method"] for call in calls] == ["GET", "GET", "GET", "GET", "GET"]
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/wallet/summary",
        "https://tokenrhythm.studio/api/usage/panel?range=today",
        _MULTIMODAL_URL,
        "https://tokenrhythm.studio/api/auth/me",
        _CREDITS_URL,
    ]
    assert result == {
        "ok": True,
        "user": _ME_USER,
        "wallet": {
            "available_balance_cny": "298.09757398",
            "frozen_balance_cny": "0.50",
            "currency": "CNY",
        },
        "usage_today": {
            "calls": 12,
            "total_tokens": 3456,
            "input_tokens": 2000,
            "output_tokens": 1456,
            "cost_cny": "9.87654321",
        },
        "temporary_credits": {
            "total_cny": "5.00000000",
            "count": 1,
            "nearest_expires_at": _iso(_in(30)),
            "nearest_amount_cny": "5.00000000",
        },
        "model_credits": [],
    }
    assert "partial" not in result
    assert "errors" not in result
    assert isinstance(result["usage_today"]["total_tokens"], int)


def test_account_overview_cost_uses_multimodal_total(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(costCny="0.20000000"),
        _multimodal_ok(cost="22.70000000"),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok(),
    )

    result = auth.account_overview()

    assert result["usage_today"]["cost_cny"] == "22.70000000", "金额必须含视频任务费用"
    assert result["usage_today"]["calls"] == 12
    assert result["usage_today"]["total_tokens"] == 3456
    assert "partial" not in result


def test_account_overview_multimodal_failure_falls_back(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(costCny="0.20000000"),
        _FakeResponse(500, {"code": "INTERNAL", "message": "多模态服务不可用", "data": None}),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok(),
    )

    result = auth.account_overview()

    assert result["usage_today"]["cost_cny"] == "0.20000000", "失败时回落到 panel 口径"
    assert "partial" not in result
    assert "errors" not in result


def test_account_overview_keeps_amount_strings_verbatim(http):
    _login(http)
    http.queue(
        _wallet_ok(availableBalanceCny="0.00000001", frozenBalanceCny=None, currency=""),
        _usage_ok(costCny="100.10000000"),
        _multimodal_ok(cost="100.10000000"),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok(),
    )

    result = auth.account_overview()

    wallet = result["wallet"]
    assert wallet["available_balance_cny"] == "0.00000001", "必须保持字符串原样"
    assert wallet["frozen_balance_cny"] == "", "冻结余额缺失时给空串"
    assert wallet["currency"] == "CNY", "币种缺失时兜底 CNY"
    assert result["usage_today"]["cost_cny"] == "100.10000000"


def test_account_overview_wallet_failure_partial(http):
    _login(http)
    http.queue(
        _FakeResponse(500, {"code": "INTERNAL", "message": "服务内部错误", "data": None}),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok([_credit()]),
    )

    result = auth.account_overview()

    assert result["ok"] is True
    assert result["partial"] is True
    assert set(result["errors"]) == {"wallet"}
    assert "服务内部错误" in result["errors"]["wallet"]
    assert result["wallet"] == {
        "available_balance_cny": "0",
        "frozen_balance_cny": "",
        "currency": "CNY",
    }
    assert result["usage_today"]["calls"] == 12
    assert result["user"]["id"] == "u1"


def test_account_overview_usage_failure_partial(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _FakeResponse(200, _ok({"summary": None})),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok(),
    )

    result = auth.account_overview()

    assert result["partial"] is True
    assert set(result["errors"]) == {"usage"}
    assert "用量" in result["errors"]["usage"]
    assert result["usage_today"] == {
        "calls": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_cny": "0",
    }
    assert result["wallet"]["available_balance_cny"] == "298.09757398"


def test_account_overview_user_failure_partial(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "登录已过期", "data": None}),
        _credits_ok(),
    )

    result = auth.account_overview()

    assert result["partial"] is True
    assert set(result["errors"]) == {"user"}
    assert "登录已过期" in result["errors"]["user"]
    assert result["user"] == {}
    assert result["usage_today"]["total_tokens"] == 3456


def test_account_overview_all_failed_raises(http):
    _login(http)
    http.queue(
        _FakeResponse(500, {"code": "INTERNAL", "message": "余额服务不可用", "data": None}),
        _FakeResponse(500, {"code": "INTERNAL", "message": "用量服务不可用", "data": None}),
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "登录已过期", "data": None}),
        _FakeResponse(500, {"code": "INTERNAL", "message": "额度服务不可用", "data": None}),
    )

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.account_overview()

    message = str(excinfo.value)
    assert "获取账号概览失败" in message
    assert "余额服务不可用" in message
    assert "登录已过期" in message


def test_account_overview_without_session(http):
    result = auth.account_overview()

    assert result == {"ok": False, "reason": "未登录"}
    assert http.holder["client"] is None, "未登录时不应创建 / 使用会话客户端"
    assert _requests_safe(http) == []


def _requests_safe(http):
    client = http.holder["client"]
    return list(getattr(client, "calls", []) or [])


_WALLET_TXN_URL = "https://tokenrhythm.studio/api/wallet/transactions"


def test_wallet_transactions_request_has_no_query(http):
    _login(http)
    data = {
        "items": [
            {"id": "t1", "type": "recharge", "amountCny": "1.00000000"},
        ],
        "total": 1,
    }
    http.queue(_FakeResponse(200, _ok(data)))

    result = auth.wallet_transactions()

    calls = _requests(http)
    assert len(calls) == 1, "钱包流水只应发一次请求"
    assert calls[0]["method"].upper() == "GET"
    url = calls[0]["url"]
    assert url == _WALLET_TXN_URL, "钱包流水接口不能带任何查询参数"
    assert "?" not in url and "pageSize" not in url
    assert result == {"ok": True, "data": data}, "data 必须原样透传"


def _overview_with_credits(http, data):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(200, _ok(data)),
    )
    return auth.account_overview()


def test_temporary_credits_sums_with_decimal_precision(http):
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="0.10000000", expires=_iso(_in(3))),
                _credit(remaining="0.20000000", expires=_iso(_in(2))),
                _credit(
                    remaining="0.00000001",
                    expires=_iso(_in(1), ".123456789"),
                ),
            ]
        },
    )

    credits = result["temporary_credits"]
    assert credits["total_cny"] == "0.30000001", "Decimal 求和不能丢低位精度"
    assert isinstance(credits["total_cny"], str), "金额必须是字符串"
    assert credits["count"] == 3
    assert credits["nearest_expires_at"] == _iso(_in(1), ".123456789")
    assert "partial" not in result


def test_temporary_credits_excludes_non_positive_remaining(http):
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="0.00000000"),
                _credit(remaining="0"),
                _credit(remaining="-5.00000000"),
                _credit(remaining=None),
                _credit(remaining="3.00000000"),
            ]
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "3.00000000",
        "count": 1,
        "nearest_expires_at": _iso(_in(30)),
        "nearest_amount_cny": "3.00000000",
    }


def test_temporary_credits_excludes_expired_and_blank_expiry(http):
    same_day_utc = _in(40).replace(hour=16, minute=0, second=0)
    plus8 = same_day_utc.astimezone(timezone(timedelta(hours=8))).strftime(
        "%Y-%m-%dT%H:%M:%S+08:00"
    )
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="9.00000000", expires="2000-01-01T00:00:00Z"),
                _credit(remaining="8.00000000", expires=""),
                _credit(remaining="7.00000000", expires=None),
                _credit(remaining="6.00000000", expires="not-a-date"),
                _credit(remaining="4.00000000", expires=plus8),
                _credit(remaining="1.50000000", expires=_iso(same_day_utc + timedelta(hours=4))),
            ]
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "5.50000000",
        "count": 2,
        "nearest_expires_at": plus8,
        "nearest_amount_cny": "5.50000000",
    }


def test_temporary_credits_excludes_pseudo_permanent_grants(http):
    real_expiry = _iso(_in(24))
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(
                    remaining="57.77609144",
                    expires=real_expiry,
                    source="REFERRAL_INVITER_REWARD",
                    sourceLabel="邀请奖励",
                ),
                _credit(
                    remaining="50.00000000",
                    expires="2099-12-31T15:59:59Z",
                    source="RECHARGE_MATCH_BONUS",
                    sourceLabel="充值活动赠额",
                ),
            ],
            "summary": {
                "expiringBalanceCny": "107.77609144",
                "nextExpiryAt": real_expiry,
                "cumulativeGiftGrantedCny": "888.73386216",
            },
            "total": 2,
        },
    )

    credits = result["temporary_credits"]
    assert credits["total_cny"] == "57.77609144", "永久赠额不能算进临时额度"
    assert credits["count"] == 1
    assert credits["nearest_expires_at"] == real_expiry
    assert credits["nearest_amount_cny"] == "57.77609144"


def test_temporary_credits_permanent_horizon_boundary(http):
    inside = _iso(_in(365 * 10 - 1))
    outside = _iso(_in(365 * 10 + 1))
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="8.00000000", expires=inside),
                _credit(remaining="9.00000000", expires=outside),
            ]
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "8.00000000",
        "count": 1,
        "nearest_expires_at": inside,
        "nearest_amount_cny": "8.00000000",
    }


def test_temporary_credits_all_permanent_shows_zero(http):
    result = _overview_with_credits(
        http,
        {
            "items": [_credit(remaining="50.00000000", expires="2099-12-31T15:59:59Z")],
            "summary": {
                "expiringBalanceCny": "50.00000000",
                "nextExpiryAt": "2099-12-31T15:59:59Z",
            },
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "0",
        "count": 0,
        "nearest_expires_at": "",
        "nearest_amount_cny": "0",
    }


def test_temporary_credits_accepts_timestamps_and_alias_fields(http):
    future_ms = int(_in(20).timestamp() * 1000)
    result = _overview_with_credits(
        http,
        {
            "items": [
                {"remainingCny": "2.50000000", "expiresAt": future_ms},
                {"remaining": "1.50000000", "expireAt": _iso(_in(25))},
            ]
        },
    )

    credits = result["temporary_credits"]
    assert credits["count"] == 2
    assert credits["total_cny"] == "4.00000000"
    assert credits["nearest_amount_cny"] == "2.50000000"


def test_temporary_credits_seconds_timestamp(http):
    result = _overview_with_credits(
        http,
        {"items": [{"remainingCny": "1.00000000", "expiresAt": int(_in(15).timestamp())}]},
    )

    credits = result["temporary_credits"]
    assert credits["count"] == 1
    assert credits["total_cny"] == "1.00000000"


def test_temporary_credits_empty_returns_zero_defaults(http):
    result = _overview_with_credits(http, {"items": []})

    assert result["temporary_credits"] == {
        "total_cny": "0",
        "count": 0,
        "nearest_expires_at": "",
        "nearest_amount_cny": "0",
    }
    assert "partial" not in result
    assert "errors" not in result


@pytest.mark.parametrize("container", ["bare", "items", "records", "list"])
def test_temporary_credits_accepts_multiple_envelope_shapes(http, container):
    items = [_credit(remaining="2.00000000")]
    data = items if container == "bare" else {container: items}

    result = _overview_with_credits(http, data)

    assert result["temporary_credits"] == {
        "total_cny": "2.00000000",
        "count": 1,
        "nearest_expires_at": _iso(_in(30)),
        "nearest_amount_cny": "2.00000000",
    }


def test_temporary_credits_retries_once_after_network_timeout(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        httpx.ReadTimeout("read operation timed out"),
        _credits_ok([_credit(remaining="5.00000000", expires=_iso(_in(30)))]),
    )

    result = auth.account_overview()

    assert result["temporary_credits"]["total_cny"] == "5.00000000"
    assert "partial" not in result
    calls = _requests(http)
    assert [call["url"] for call in calls].count(_CREDITS_URL) == 2
    assert calls[-1]["timeout"] == auth._TEMPORARY_CREDITS_TIMEOUT


def test_temporary_credits_retries_after_server_internal_error(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(500, {"code": "INTERNAL_ERROR", "message": "服务内部错误", "data": None}),
        _credits_ok([_credit(remaining="7.50000000", expires=_iso(_in(60)))]),
    )

    result = auth.account_overview()

    assert result["temporary_credits"]["total_cny"] == "7.50000000"
    assert "partial" not in result
    assert [call["url"] for call in _requests(http)].count(_CREDITS_URL) == 2


def test_temporary_credits_does_not_retry_auth_errors(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "请先登录", "data": None}),
    )

    result = auth.account_overview()

    assert result["partial"] is True
    assert set(result["errors"]) == {"temporary_credits"}
    assert [call["url"] for call in _requests(http)].count(_CREDITS_URL) == 1


def test_account_overview_temporary_credits_failure_partial(http):
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _multimodal_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(
            401, {"code": "UNAUTHORIZED", "message": "额度服务不可用", "data": None}
        ),
    )

    result = auth.account_overview()

    assert result["ok"] is True
    assert result["partial"] is True
    assert set(result["errors"]) == {"temporary_credits"}
    assert "额度服务不可用" in result["errors"]["temporary_credits"]
    assert result["temporary_credits"] == {
        "total_cny": "0",
        "count": 0,
        "nearest_expires_at": "",
        "nearest_amount_cny": "0",
    }
    assert result["model_credits"] == []
    assert result["wallet"]["available_balance_cny"] == "298.09757398"
    assert result["usage_today"]["calls"] == 12
    assert result["user"]["id"] == "u1"


def test_model_credits_extracts_active_and_keeps_temporary_separate(http):
    exp_near = _iso(_in(7))
    exp_later = _iso(_in(14))
    result = _overview_with_credits(
        http,
        {
            "list": [
                {
                    "id": "mg-2",
                    "source": "MODEL_GIFT",
                    "kind": "MODEL_GIFT",
                    "modelId": "kimi-k3",
                    "modelName": "Kimi K3",
                    "grantedCny": "17.00000000",
                    "remainingCny": "17.00000000",
                    "status": "ACTIVE",
                    "expiresAt": exp_later,
                },
                {
                    "id": "mg-1",
                    "source": "MODEL_GIFT",
                    "kind": "MODEL_GIFT",
                    "modelId": "deepseek-v4.1-flash-1017",
                    "modelName": "DeepSeek V4.1 Flash 1017",
                    "grantedCny": "17.00000000",
                    "remainingCny": "16.64283356",
                    "status": "ACTIVE",
                    "expiresAt": exp_near,
                },
                _credit(remaining="5.00000000", expires=exp_later, kind="WALLET_GIFT"),
            ]
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "5.00000000",
        "count": 1,
        "nearest_expires_at": exp_later,
        "nearest_amount_cny": "5.00000000",
    }
    assert result["model_credits"] == [
        {
            "id": "mg-1",
            "model_id": "deepseek-v4.1-flash-1017",
            "model_name": "DeepSeek V4.1 Flash 1017",
            "remaining_cny": "16.64283356",
            "granted_cny": "17.00000000",
            "expires_at": exp_near,
        },
        {
            "id": "mg-2",
            "model_id": "kimi-k3",
            "model_name": "Kimi K3",
            "remaining_cny": "17.00000000",
            "granted_cny": "17.00000000",
            "expires_at": exp_later,
        },
    ]


def test_model_credits_excludes_depleted_expired_and_inactive(http):
    future_far = "2099-12-31T15:59:59.000Z"
    result = _overview_with_credits(
        http,
        {
            "list": [
                {
                    "id": "mg-zero",
                    "kind": "MODEL_GIFT",
                    "modelId": "hy4",
                    "modelName": "Hy4",
                    "grantedCny": "17.00000000",
                    "remainingCny": "0.00000000",
                    "status": "ACTIVE",
                    "expiresAt": future_far,
                },
                {
                    "id": "mg-used-up",
                    "kind": "MODEL_GIFT",
                    "modelId": "glm-5.3",
                    "modelName": "GLM-5.3",
                    "grantedCny": "17.00000000",
                    "remainingCny": "1.00000000",
                    "status": "USED_UP",
                    "expiresAt": future_far,
                },
                {
                    "id": "mg-expired",
                    "kind": "MODEL_GIFT",
                    "modelId": "kimi-k3",
                    "modelName": "Kimi K3",
                    "grantedCny": "17.00000000",
                    "remainingCny": "17.00000000",
                    "status": "ACTIVE",
                    "expiresAt": "2020-01-01T00:00:00Z",
                },
                {
                    "id": "mg-perm",
                    "source": "MODEL_GIFT",
                    "modelId": "glm-5.3",
                    "modelName": "GLM-5.3",
                    "grantedCny": "17.00000000",
                    "remainingCny": "12.34567890",
                    "status": "ACTIVE",
                    "expiresAt": None,
                },
            ]
        },
    )

    assert result["model_credits"] == [
        {
            "id": "mg-perm",
            "model_id": "glm-5.3",
            "model_name": "GLM-5.3",
            "remaining_cny": "12.34567890",
            "granted_cny": "17.00000000",
            "expires_at": "",
        }
    ]

def _api_model(model_id, name=None, type_="chat", status="online", **extra):
    model = {
        "id": model_id,
        "name": name or model_id.upper(),
        "type": type_,
        "status": status,
        "contextWindow": 200000,
        "maxOutputTokens": 128000,
        "capabilities": {
            "openai": True,
            "tools": True,
            "vision": False,
            "reasoning": True,
            "stream": True,
        },
    }
    model.update(extra)
    return model


def _models_ok(*models):
    return _FakeResponse(200, _ok(list(models)))


def _sync_calls(monkeypatch):
    from core import model_manager

    calls = []

    def _add_model(provider_id, name, context_window, **kwargs):
        calls.append((provider_id, name, context_window, kwargs))
        return "m-" + name

    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    return calls


def test_sync_models_adds_all_available_chat_models(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p1", "基元律动（木偶）"))
    calls = _sync_calls(monkeypatch)
    http.queue(
        _models_ok(
            _api_model(
                "glm-5.1",
                name="GLM-5.1",
                capabilities={"openai": True, "tools": True, "vision": True},
                contextWindow=200000,
                maxOutputTokens=128000,
            ),
            _api_model(
                "qwen3-coder",
                contextWindow=131072,
                maxOutputTokens=8192,
            ),
            _api_model(
                "kimi-k2",
                contextWindow=262144,
                maxOutputTokens=None,
                capabilities={"openai": True, "tools": False, "vision": False},
            ),
        )
    )

    result = auth.sync_models()

    assert result == {
        "ok": True,
        "provider_id": "p1",
        "provider_name": "基元律动（木偶）",
        "total": 3,
        "added": 3,
        "skipped": 0,
        "failed": [],
    }
    calls_http = _requests(http)
    assert [call["url"] for call in calls_http] == [
        "https://tokenrhythm.studio/api/models"
    ]
    assert "authorization" not in calls_http[0]["headers"]
    assert [call[1] for call in calls] == ["glm-5.1", "qwen3-coder", "kimi-k2"]
    assert calls[0] == (
        "p1",
        "glm-5.1",
        200000,
        {
            "supports_vision": True,
            "supports_function_calling": True,
            "max_tokens": 128000,
        },
    )
    assert calls[1] == (
        "p1",
        "qwen3-coder",
        131072,
        {
            "supports_vision": False,
            "supports_function_calling": True,
            "max_tokens": 8192,
        },
    )
    assert calls[2] == (
        "p1",
        "kimi-k2",
        262144,
        {"supports_vision": False, "supports_function_calling": False},
    )


def test_sync_models_skips_non_chat_offline_and_non_openai(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p1"), _provider("p2", "基元律动"))
    auth._save_binding(
        {
            "account_id": "u1",
            "account_name": "木偶",
            "provider_id": "p2",
            "key_id": "k1",
            "bound_at": 1700000000,
        }
    )
    calls = _sync_calls(monkeypatch)
    http.queue(
        _models_ok(
            _api_model("glm-5.1"),
            _api_model("img-1", type_="image"),
            _api_model("vid-1", type_="video"),
            _api_model(
                "emb-1", type_="embedding", capabilities={"openai": True, "embeddings": True}
            ),
            _api_model("beta-1", status="testing"),
            _api_model("old-1", status="offline"),
            _api_model("anthropic-only", capabilities={"openai": False, "anthropic": True}),
            {"id": "no-caps", "type": "chat", "status": "online"},
            {
                "id": "proto-only",
                "name": "Proto Only",
                "type": "chat",
                "status": "online",
                "protocolCapabilities": {"openai": {"available": True}},
            },
        )
    )

    result = auth.sync_models()

    assert result["provider_id"] == "p2"
    assert result["total"] == 2
    assert result["added"] == 2
    assert result["skipped"] == 0
    assert [call[1] for call in calls] == ["glm-5.1", "proto-only"]
    assert calls[1] == (
        "p2",
        "proto-only",
        128000,
        {"supports_vision": False, "supports_function_calling": True},
    )


def test_sync_models_is_idempotent(http, monkeypatch):
    import config

    provider = _provider("p1", "基元律动")
    monkeypatch.setattr(
        config,
        "_load",
        lambda: {
            "providers": [
                {"id": "p1", "name": "基元律动", "base_url": provider["base_url"],
                 "api_key": "", "protocol": "openai",
                 "models": list(provider["models"])}
            ]
        },
    )

    from core import model_manager

    added_names = []

    def _add_model(provider_id, name, context_window, **kwargs):
        added_names.append(name)
        provider["models"].append(
            {"id": name, "name": name, "context_window": context_window}
        )
        return name

    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    payload = _models_ok(_api_model("glm-5.1"), _api_model("qwen3-coder"))
    http.queue(payload, _models_ok(_api_model("glm-5.1"), _api_model("qwen3-coder")))

    first = auth.sync_models()
    second = auth.sync_models("p1")

    assert (first["added"], first["skipped"]) == (2, 0)
    assert (second["added"], second["skipped"]) == (0, 2)
    assert second["total"] == 2
    assert second["failed"] == []
    assert added_names == ["glm-5.1", "qwen3-coder"]


def test_sync_models_records_failure_and_keeps_adding(http, monkeypatch):
    _patch_providers(monkeypatch, _provider("p1"))
    from core import model_manager

    added_names = []

    def _add_model(provider_id, name, context_window, **kwargs):
        if name == "broken-model":
            raise ValueError("厂商不存在: p1")
        added_names.append(name)
        return name

    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    http.queue(
        _models_ok(
            _api_model("glm-5.1"),
            _api_model("broken-model"),
            _api_model("qwen3-coder"),
        )
    )

    result = auth.sync_models()

    assert result["ok"] is True
    assert result["total"] == 3
    assert result["added"] == 2
    assert result["skipped"] == 0
    assert result["failed"] == [{"model": "broken-model", "error": "厂商不存在: p1"}]
    assert added_names == ["glm-5.1", "qwen3-coder"]


def test_sync_models_without_provider_raises(http, monkeypatch):
    _patch_providers(monkeypatch)
    _sync_calls(monkeypatch)

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.sync_models()

    assert "未找到基元律动厂商配置，请先登录" in str(excinfo.value)
    assert http.holder["client"] is None


def _restore_cookie_session():
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})
    auth._invalidate_health_cache()


def test_session_health_without_session_sends_no_request(http):
    result = auth.session_health()

    assert result == {"ok": False, "logged_in": False, "reason": "未登录基元律动账号"}
    assert _requests(http) == []


def test_session_health_ok_with_restored_cookie(http):
    _restore_cookie_session()
    http.queue(_me())

    result = auth.session_health()

    assert result == {"ok": True, "logged_in": True, "reason": ""}
    assert auth._STATE["logged_in"] is True
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me"
    ]


def test_session_health_401_clears_session_and_state(http, session_store):
    _restore_cookie_session()
    http.queue(
        _FakeResponse(401, {"code": "UNAUTHORIZED", "message": "登录已过期", "data": None})
    )

    result = auth.session_health()

    assert result == {"ok": False, "logged_in": False, "reason": "登录已失效，请重新登录"}
    assert not session_store.exists(), "鉴权失败必须清掉本地会话"
    assert auth._STATE["logged_in"] is False
    assert auth._STATE["restored"] is False


def test_session_health_empty_user_counts_as_expired(http, session_store):
    _restore_cookie_session()
    http.queue(_FakeResponse(200, {"code": 0, "message": "ok", "data": {}}))

    result = auth.session_health()

    assert result["ok"] is False
    assert result["reason"] == "登录已失效，请重新登录"
    assert not session_store.exists()


def test_session_health_network_error_keeps_session(http, session_store):
    _restore_cookie_session()
    http.queue(httpx.ConnectTimeout("连接超时"))

    result = auth.session_health()

    assert result["ok"] is False
    assert result["reason"] == "会话校验失败（网络异常，可稍后重试）"
    assert session_store.exists(), "网络抖动不等于登录失效，不能清会话"


def test_session_health_cached_for_60s_and_force_refetches(http):
    _restore_cookie_session()
    http.queue(_me(), _me())

    first = auth.session_health()
    second = auth.session_health()

    assert first == second == {"ok": True, "logged_in": True, "reason": ""}
    assert len(_requests(http)) == 1, "缓存期内不得重复请求 /api/auth/me"

    forced = auth.session_health(force=True)

    assert forced["ok"] is True
    assert len(_requests(http)) == 2, "force=True 必须重新校验"


def test_session_health_cache_expires_after_ttl(http):
    _restore_cookie_session()
    http.queue(_me(), _me())

    auth.session_health()
    auth._HEALTH_CACHE["at"] -= auth._SESSION_HEALTH_TTL + 1
    auth.session_health()

    assert len(_requests(http)) == 2


def test_session_health_cache_invalidated_by_login(http, session_store):
    assert auth.session_health()["ok"] is False
    assert _requests(http) == []

    client = auth._get_client()
    client.cookies.set("tr_session", "abc123", domain="tokenrhythm.studio", path="/")
    http.queue(_FakeResponse(200, _ok({"user": {"id": "u1"}})), _me())
    auth.password_login("13800000000", "pwd")

    http.queue(_me())
    result = auth.session_health()

    assert result["ok"] is True
    assert len(_requests(http)) == 3


def test_export_session_cookies_returns_tokenrhythm_cookies(http):
    client = auth._get_client()
    client.cookies.set("tr_session", "abc123", domain="tokenrhythm.studio", path="/")
    client.cookies.set("tr_csrf", "def456", domain=".tokenrhythm.studio", path="/")
    client.cookies.set("other", "zzz", domain="example.com", path="/")
    try:
        cookies = auth.export_session_cookies()
    finally:
        client.cookies.clear()

    by_name = {item["name"]: item for item in cookies}
    assert set(by_name) == {"tr_session", "tr_csrf"}, "example.com 的 cookie 不能导出"
    assert _requests(http) == []

    for name, value in (("tr_session", "abc123"), ("tr_csrf", "def456")):
        item = by_name[name]
        assert set(item) == {
            "name",
            "value",
            "domain",
            "path",
            "secure",
            "http_only",
            "expires",
        }
        assert item["value"] == value, "value 必须原样导出"
        assert item["domain"] in {"tokenrhythm.studio", ".tokenrhythm.studio"}
        assert item["path"] == "/"
        assert item["secure"] is False
        assert isinstance(item["http_only"], bool)
        assert item["expires"] == 0


def test_export_session_cookies_empty_when_logged_out(http):
    client = auth._get_client()
    client.cookies.clear()
    try:
        assert auth.export_session_cookies() == []
    finally:
        client.cookies.clear()


def test_binding_active_requires_key_and_provider(http):
    try:
        assert auth.binding_active() is False
        auth._save_binding({"account_id": "u1", "provider_id": "p1", "key_id": "k1"})
        assert auth.binding_active() is True
        auth._save_binding({"account_id": "u1", "provider_id": "p1", "key_id": ""})
        assert auth.binding_active() is False
        auth._save_binding({"account_id": "u1", "provider_id": "", "key_id": "k1"})
        assert auth.binding_active() is False
    finally:
        auth._clear_binding()
    assert auth.binding_active() is False
