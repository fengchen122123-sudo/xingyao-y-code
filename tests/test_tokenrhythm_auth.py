"""基元律动账号登录与 API Key 自动绑定单元测试（不联网、不读写真实配置）。"""

import base64
import logging
import time
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
    """protect_secret 替身：base64 代替 DPAPI，测试不依赖系统凭据库。"""
    return _ENCRYPTED_PREFIX + base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _fake_unprotect(value):
    """unprotect_secret 替身：只认 _fake_protect 的产物，其余一律当解密失败。"""
    raw = str(value)
    if not raw.startswith(_ENCRYPTED_PREFIX):
        raise ValueError("不是加密内容")
    return base64.b64decode(raw[len(_ENCRYPTED_PREFIX) :]).decode("utf-8")


def _me(user_id="u1", name="木偶", **extra):
    """GET /api/auth/me 的成功响应：data 直接就是用户对象。"""
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
    """把本地 provider 列表固定成给定内容。"""
    import config

    monkeypatch.setattr(config, "_load", lambda: {"providers": list(providers)})


@pytest.fixture
def http(monkeypatch, tmp_path):
    """打桩 httpx.Client，记录所有请求并按队列返回响应。

    会话文件指向 tmp_path，避免读写用户真实的 ~/.ycode/secrets。
    """
    monkeypatch.setattr(auth, "_SESSION_FILE", tmp_path / "tr_session.json")
    monkeypatch.setattr(auth, "_BINDING_FILE", tmp_path / "tr_binding.json")
    # DPAPI 替身：不依赖系统凭据库，也不读写用户真实的 ~/.ycode/secrets
    from core import credentials

    monkeypatch.setattr(credentials, "protect_secret", _fake_protect)
    monkeypatch.setattr(credentials, "unprotect_secret", _fake_unprotect)
    # 关键隔离：_delete_tr_providers 会遍历真实 load_config 并调 model_manager 删厂商，
    # 不打桩会把用户本机真实的「基元律动」模型配置删掉。默认 no-op，需要验证的用例
    # 再自行 monkeypatch 覆盖。
    monkeypatch.setattr(auth, "_delete_tr_providers", lambda keep_id="": 0)
    auth.logout()
    holder = {"client": None, "responses": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.calls = []
            self.closed = False
            # 真实 httpx.Client 自带 cookies，持久化逻辑会读它
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
            # 队列里放异常时按"请求在传输层就失败"抛出（模拟超时 / 连接不可用）
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


# ---------------------------------------------------------------------------
# 会话持久化：重启后保持登录态
# ---------------------------------------------------------------------------


@pytest.fixture
def session_store(monkeypatch, tmp_path):
    """隔离会话文件，并用 base64 替身代替 DPAPI（测试不依赖系统凭据库）。"""
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
    """构造只带 cookies.jar 的假 client。

    httpx.Cookies.set 不支持 expires 参数，所以这里直接用 SimpleNamespace 拼出
    _save_session 需要的最小结构（name/value/domain/path/expires）。
    """
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
    """cookie 落盘必须加密，且新客户端能原样恢复出同名同值。"""
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
    """已过期的 cookie 既不写入也不再恢复。"""
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
    """退出登录要连同本地会话文件一起清掉，避免下次启动又自动登进去。"""
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    assert session_store.exists()

    auth.logout()

    assert not session_store.exists()


def test_account_status_restores_login_from_saved_session(http, session_store):
    """模拟重启：内存无登录标记，但本地有 cookie → /auth/me 成功即恢复登录态。"""
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None), ("tr_csrf", "def456", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})

    http.queue(_FakeResponse(200, _ok({"user": {"id": "u1", "name": "木偶"}})))
    result = auth.account_status()

    assert result["logged_in"] is True
    assert result["user"]["id"] == "u1"
    # 只发了一次 /auth/me，没有多余的登录请求
    assert len(_requests(http)) == 1


def test_account_status_clears_invalid_session(http, session_store):
    """cookie 失效（服务端 401）时要清掉本地文件，避免每次启动都白试。"""
    auth._save_session(_client_with_cookies(("tr_session", "expired", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})

    http.queue(_FakeResponse(401, {"code": "UNAUTHORIZED", "message": "登录已过期", "data": None}))
    result = auth.account_status()

    assert result["logged_in"] is False
    assert not session_store.exists()


def test_password_login_saves_session(http, session_store):
    """密码登录成功后要立刻把会话写入本地，下次启动才能免登录。"""
    http.queue(
        _FakeResponse(200, _ok({"user": {"id": "u1"}})),
        _FakeResponse(200, _ok({"user": {"id": "u1"}})),
    )
    http.holder["client"] = None

    # FakeClient 在 _get_client() 时才创建，先把 cookies 塞进去要靠实例方法；
    # 这里直接在登录前建好 client 并写入 cookie，模拟服务端下发。
    client = auth._get_client()
    client.cookies.set("tr_session", "abc123", domain="tokenrhythm.studio", path="/")

    auth.password_login("13800000000", "pwd")

    assert session_store.exists()
    raw = session_store.read_text(encoding="utf-8")
    assert "abc123" not in raw



def test_password_login_success(http):
    """密码登录：POST 字段正确，随后 GET /me 校验会话并返回 user。"""
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
    """服务端 400：异常 message 保留服务端原文。"""
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
    """发码成功：保存 challengeId 并返回倒计时 / 掩码手机号。"""
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
    # 未传 captcha：请求体只有 scene + phone，不能出现 captchaVerifyParam 键。
    assert call["json"] == {"scene": "login_register", "phone": "13800000000"}
    assert "captchaVerifyParam" not in call["json"]


def test_send_sms_code_with_captcha_verify_param(http):
    """传入 captcha：请求体带上 captchaVerifyParam，值原样透传。"""
    http.queue(_FakeResponse(200, _ok({"challengeId": "ch-2", "intervalSeconds": 60})))

    auth.send_sms_code("13800000000", "captcha-abc123-=_.%2B/end")

    body = _requests(http)[0]["json"]
    assert body == {
        "scene": "login_register",
        "phone": "13800000000",
        "captchaVerifyParam": "captcha-abc123-=_.%2B/end",
    }


def test_send_sms_code_empty_captcha_omits_field(http):
    """显式传空串 / 纯空白：仍不带 captchaVerifyParam 字段（不传空串上送）。"""
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
    """三个 CAPTCHA_* 错误码都转中文引导，且保留服务端原文。"""
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
    """CAPTCHA_REQUIRED：提示里给出可执行引导（安全验证 + 密码登录兜底）。"""
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
    """短信登录：verify(challengeId+code) → termsVersion → login-or-register。"""
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
    """先走一次发码（拿到 challengeId），再清空调用记录，供后续 sms_login 断言。"""
    http.queue(_FakeResponse(200, _ok({"challengeId": "ch-9", "intervalSeconds": 60})))
    auth.send_sms_code("13900000000")
    _requests(http).clear()


def test_sms_login_with_invite_code_passes_invite_code(http):
    """传入邀请码：login-or-register 请求体带 inviteCode，值原样透传。"""
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
    """不传 / 空串 / 纯空白邀请码：请求体不出现 inviteCode 键。"""
    _prepare_sms_login(http)
    http.queue(
        _FakeResponse(200, _ok({"verificationToken": "vt-1"})),
        _FakeResponse(200, _ok({"termsVersion": "v2026-01"})),
        _FakeResponse(200, _ok({"user": {"id": "u9"}})),
    )

    auth.sms_login("13900000000", "123456", invite_code)

    body = _requests(http)[2]["json"]
    assert "inviteCode" not in body
    # 空邀请码不能影响其它字段：仍是完整的四字段请求体。
    assert body == {
        "channel": "sms",
        "verificationToken": "vt-1",
        "termsAccepted": True,
        "termsVersion": "v2026-01",
    }


def test_sms_login_invite_code_keeps_other_fields(http):
    """带邀请码时其它字段（channel / verificationToken / terms*）一个都不能少。"""
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
    """GET /api/api-keys 的成功响应：覆盖 data 为数组 / {items:[...]} 两种形态。"""
    items = [
        {"id": key_id, "name": "星瑶Code", "maskedKey": "sk-****"}
        for key_id in key_ids
    ]
    return _FakeResponse(200, _ok(items if container == "list" else {"items": items}))


def _unexpected(name):
    """构造"绝不应被调用"的替身，断言某条旧路径已被删除。"""

    def _boom(*args, **kwargs):
        raise AssertionError(f"不应调用 {name}")

    return _boom


def _creation_recorder(model_manager, monkeypatch):
    """打桩 model_manager：记录新增厂商，禁止更新 / 删除厂商。"""
    calls = {"added": []}

    def _add_provider(name, base_url, api_key, protocol):
        calls["added"].append((name, base_url, api_key, protocol))
        return "p-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
    monkeypatch.setattr(model_manager, "update_provider", _unexpected("update_provider"))
    monkeypatch.setattr(model_manager, "delete_provider", _unexpected("delete_provider"))
    return calls


def test_ensure_bound_without_binding_always_creates_key(http, monkeypatch):
    """无绑定首次登录：即使本地已有可用 provider + Key，也必须新建专属 Key。"""
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
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-dedicated", "openai")
    ]
    # 无绑定就不该校验本地 Key（不请求 /v1/models），更不该沿用 sk-manual
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["account_id"] == "u1"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"


def test_ensure_bound_never_probes_local_key_when_unbound(http, monkeypatch):
    """无绑定：连"本地 Key 是否可用"都不该校验（沿用分支已彻底删除）。"""
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
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_without_provider_creates_key_and_provider(http, monkeypatch):
    """无 provider：创建 Key 后 add_provider（名称带昵称）并追加默认模型。"""
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    def _add_model(provider_id, name, context_window, **kwargs):
        calls["add_model"] = (provider_id, name, context_window, kwargs)
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
        "model_added": True,
        "reason": "已自动创建并绑定 Key",
        "account_id": "u1",
        "account_name": "木偶",
        "provider_name": "基元律动（木偶）",
    }
    create_call = _requests(http)[1]
    assert create_call["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert create_call["json"] == {"name": "星瑶Code"}
    assert calls["add_provider"] == (
        "基元律动（木偶）",
        "https://tokenrhythm.studio/v1",
        "sk-created",
        "openai",
    )
    provider_id, model_name, context_window, kwargs = calls["add_model"]
    assert provider_id == "p-new"
    assert model_name == "deepseek-v4.1-flash"
    assert context_window == 128000
    assert kwargs == {"supports_vision": False, "supports_function_calling": True}


def test_ensure_bound_new_provider_when_local_key_invalid(http, monkeypatch):
    """无绑定 + 本地厂商的 Key 已失效：仍要新建 Key 与厂商，绝不打补丁复用旧条目。"""
    _patch_existing_provider(monkeypatch, api_key="enc:bad-key")

    from core import model_manager

    calls = _creation_recorder(model_manager, monkeypatch)
    auth._STATE["logged_in"] = True
    http.queue(
        _me("u1", "木偶"),
        # 只给 apiKey：验证明文 Key 兜底字段（key 缺失时取 apiKey）。
        _FakeResponse(200, _ok({"apiKey": "sk-new"})),
    )

    result = auth.ensure_bound()

    assert result["created"] is True
    assert result["key_created"] is True
    assert result["provider_id"] == "p-new"
    assert result["model_added"] is True
    assert calls["added"] == [
        ("基元律动（木偶）", "https://tokenrhythm.studio/v1", "sk-new", "openai")
    ]


def test_ensure_bound_missing_plaintext_key_raises(http, monkeypatch):
    """创建成功但响应里没有明文字段：抛明确异常。"""
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
    """首次绑定：建 Key + add_provider（「基元律动（昵称）」）+ 写绑定关系。"""
    _patch_providers(monkeypatch)

    from core import model_manager

    calls = {}

    def _add_provider(name, base_url, api_key, protocol):
        calls["add_provider"] = (name, base_url, api_key, protocol)
        return "p-new"

    monkeypatch.setattr(model_manager, "add_provider", _add_provider)
    monkeypatch.setattr(model_manager, "add_model_to_provider", lambda *a, **k: "m-new")
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
        "model_added": True,
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
    binding = auth._load_binding()
    assert binding["v"] == 1
    assert binding["account_id"] == "u1"
    assert binding["account_name"] == "木偶"
    assert binding["provider_id"] == "p-new"
    assert binding["key_id"] == "k1"
    assert isinstance(binding["bound_at"], int)
    # 无绑定不校验本地 Key：只有 /auth/me 与创建 Key 两次请求
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_same_account_second_call_skips_key_creation(http, monkeypatch):
    """同账号重复登录且 Key 仍在账号下：复用，不重复建 Key，created=False。"""
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
        # data 为 {items:[...]} 形态也要能解析出 key id
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
    """同账号且 Key 仍在账号下，但本地 Key 校验失败：新建 Key 覆盖绑定过的厂商。"""
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
    """同账号但 key_id 已不在账号 Key 列表（被删）：新建 Key 并更新绑定。"""
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
        # 账号下只剩另一枚 Key：k-old 已被控制台删掉
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
    # Key 不在列表里就不必再校验本地 Key，直接重建
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys",
        "https://tokenrhythm.studio/api/api-keys",
    ]
    binding = auth._load_binding()
    assert binding["key_id"] == "k-new"
    assert binding["bound_at"] > 1700000000


def test_ensure_bound_switches_account_rebuilds_provider(http, monkeypatch):
    """换账号：先删旧 Key（断言删除请求 id）→ 删旧厂商 → 新建 Key + 厂商 + 绑定。"""
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
        # 记录删厂商那一刻已经发过的请求：必须已经删掉旧 Key
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
    # 第一条业务请求就是删旧账号的 Key，且用的是绑定里的 key_id
    assert calls_list[1]["method"] == "POST"
    assert calls_list[1]["url"] == "https://tokenrhythm.studio/api/api-keys/k-old/delete"
    assert calls_list[2]["url"] == "https://tokenrhythm.studio/api/api-keys"
    assert calls_list[2]["json"] == {"name": "星瑶Code"}
    # 删厂商时旧 Key 已经删过了
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
    # 换账号不复用旧 Key：/auth/me → 删旧 Key → 建新 Key，共三次请求
    assert [call["url"] for call in calls_list] == [
        "https://tokenrhythm.studio/api/auth/me",
        "https://tokenrhythm.studio/api/api-keys/k-old/delete",
        "https://tokenrhythm.studio/api/api-keys",
    ]


def test_ensure_bound_switch_account_keeps_going_when_old_key_delete_fails(http, monkeypatch):
    """换账号时旧 Key 删不掉（500）：仍继续删厂商 + 建新 Key，old_key_deleted=False。"""
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
    """绑定文件损坏 / 解密失败：按无绑定处理，不崩溃并重建绑定。"""
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
    """未登录：直接抛"请先登录基元律动账号"，且不发任何请求。"""
    auth._STATE["logged_in"] = False

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.ensure_bound()

    assert "请先登录基元律动账号" in str(excinfo.value)
    client = http.holder["client"]
    assert client is None or client.calls == []


# ---------------------------------------------------------------------------
# 退出登录：先删服务端专属 Key，再清本地
# ---------------------------------------------------------------------------


def _spy_local_cleanup(monkeypatch, http):
    """记录清理 binding / 会话时已经发出的请求数，用于断言"先删 Key 后清本地"。"""
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
    """退出登录：先调服务端删掉专属 Key（data 为空也算成功），再清 binding / 会话。"""
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
    # binding 与 session 都是在删 Key 请求发出之后才清的
    assert order == {"clear_binding": 1, "clear_session": 1}
    assert auth._load_binding() is None
    assert not auth._SESSION_FILE.exists()
    assert auth._STATE["logged_in"] is False


def test_logout_removes_tokenrhythm_providers(http, monkeypatch):
    """退出登录要把模型配置里的基元律动厂商一起删掉，不只是删 Key。"""
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


def test_logout_clears_local_even_when_key_delete_fails(http, monkeypatch, caplog):
    """删除 Key 返回 500：仍清 binding / 会话 / 登录态，只记 warning。"""
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
    """本地没有绑定（未登录 / 已退出）：logout 不该发任何请求。"""
    auth._STATE["logged_in"] = False

    auth.logout()

    client = http.holder["client"]
    assert client is None or client.calls == []


def test_account_status_without_session(http):
    """无会话：直接返回 logged_in=False，且不发任何网络请求。"""
    result = auth.account_status()

    assert result == {"logged_in": False}
    # 现在会先建 Client 以便尝试恢复持久化会话，但绝不能发任何请求
    assert http.holder["client"].calls == []


# ---------------------------------------------------------------------------
# 账号概览：余额 + 今日用量 + 个人信息
# ---------------------------------------------------------------------------


def _login(http):
    """置登录态并建好进程内会话 Client（fixture 已打桩 Client，不发真实请求）。"""
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


def _credit(remaining="1.00000000", expires="2099-01-01T00:00:00Z", **overrides):
    """一条临时额度记录，字段名与官方 /api/wallet/expiring-credits 契约一致。"""
    item = {
        "grantedCny": "10.00000000",
        "remainingCny": remaining,
        "consumedCny": "0.00000000",
        "grantedAt": "2026-01-01T00:00:00Z",
        "expiresAt": expires,
        "validityStartsAt": "2026-01-01T00:00:00Z",
        "lineType": "promo",
    }
    item.update(overrides)
    return item


def _credits_ok(items=None):
    """临时额度接口成功响应：外层用官方常见的 {items: [...]} 形态。"""
    return _FakeResponse(200, _ok({"items": list(items or [])}))


_CREDITS_URL = (
    "https://tokenrhythm.studio/api/wallet/expiring-credits"
    "?page=1&pageSize=50&includeInactive=false&includePermanent=false"
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
    """四个接口都正常：返回完整结构，金额字符串原样保留、计数为 int。"""
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _credits_ok([_credit(remaining="5.00000000", expires="2099-02-01T00:00:00Z")]),
    )

    result = auth.account_overview()

    calls = _requests(http)
    assert [call["method"] for call in calls] == ["GET", "GET", "GET", "GET"]
    assert [call["url"] for call in calls] == [
        "https://tokenrhythm.studio/api/wallet/summary",
        "https://tokenrhythm.studio/api/usage/panel?range=today",
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
            "cost_cny": "1.23456789",
        },
        "temporary_credits": {
            "total_cny": "5.00000000",
            "count": 1,
            "nearest_expires_at": "2099-02-01T00:00:00Z",
            "nearest_amount_cny": "5.00000000",
        },
    }
    # 全部成功时不出现 partial / errors，结构保持与契约一致。
    assert "partial" not in result
    assert "errors" not in result
    assert isinstance(result["usage_today"]["total_tokens"], int)


def test_account_overview_keeps_amount_strings_verbatim(http):
    """金额不做 float 换算：高位小数字符串一位都不能变；缺省字段给默认值。"""
    _login(http)
    http.queue(
        _wallet_ok(availableBalanceCny="0.00000001", frozenBalanceCny=None, currency=""),
        _usage_ok(costCny="100.10000000"),
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
    """余额接口失败：只降级 wallet，其余字段照常返回，并带 partial/errors。"""
    _login(http)
    http.queue(
        _FakeResponse(500, {"code": "INTERNAL", "message": "服务内部错误", "data": None}),
        _usage_ok(),
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
    # 其余两个接口的结果不受影响。
    assert result["usage_today"]["calls"] == 12
    assert result["user"]["id"] == "u1"


def test_account_overview_usage_failure_partial(http):
    """用量接口返回了缺 summary 的数据：视为该接口失败，计数降级成 0。"""
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
    """个人信息接口失败：user 给空字典，余额 / 用量照常返回。"""
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
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
    """三个接口全失败：不能只返回默认值，必须抛异常让上层提示会话失效。"""
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
    """未登录：直接返回 ok=False + reason，且不发任何网络请求。"""
    result = auth.account_overview()

    assert result == {"ok": False, "reason": "未登录"}
    assert http.holder["client"] is None, "未登录时不应创建 / 使用会话客户端"
    assert _requests_safe(http) == []


def _requests_safe(http):
    client = http.holder["client"]
    return list(getattr(client, "calls", []) or [])


# ---------------------------------------------------------------------------
# 临时额度：/api/wallet/expiring-credits
# ---------------------------------------------------------------------------


def _overview_with_credits(http, data):
    """跑一次 account_overview，只把临时额度接口的 data 换成指定内容。"""
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(200, _ok(data)),
    )
    return auth.account_overview()


def test_temporary_credits_sums_with_decimal_precision(http):
    """多条额度用 Decimal 求和：0.10000000 + 0.20000000 + 0.00000001 = 0.30000001。

    若用 float 求和会得到 0.30000001000000003 一类的脏尾；这里必须逐位精确，
    且 total_cny 保持字符串、按原始精度返回（不四舍五入到两位小数）。
    """
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="0.10000000", expires="2099-01-03T00:00:00Z"),
                _credit(remaining="0.20000000", expires="2099-01-02T00:00:00Z"),
                _credit(
                    remaining="0.00000001",
                    expires="2099-01-01T12:00:00.123456789Z",
                ),
            ]
        },
    )

    credits = result["temporary_credits"]
    assert credits["total_cny"] == "0.30000001", "Decimal 求和不能丢低位精度"
    assert isinstance(credits["total_cny"], str), "金额必须是字符串"
    assert credits["count"] == 3
    # 最早到期的一条原样返回（含纳秒小数秒），格式化交给展示层。
    assert credits["nearest_expires_at"] == "2099-01-01T12:00:00.123456789Z"
    assert "partial" not in result


def test_temporary_credits_excludes_non_positive_remaining(http):
    """remainingCny 为 0 / 负数 / 缺失的项不计入 sum 与 count。"""
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
        "nearest_expires_at": "2099-01-01T00:00:00Z",
        "nearest_amount_cny": "3.00000000",
    }


def test_temporary_credits_excludes_expired_and_blank_expiry(http):
    """expiresAt 已过期 / 空 / 缺失 / 解析失败的项不计入；有效项按 UTC 比较。"""
    result = _overview_with_credits(
        http,
        {
            "items": [
                _credit(remaining="9.00000000", expires="2000-01-01T00:00:00Z"),
                _credit(remaining="8.00000000", expires=""),
                _credit(remaining="7.00000000", expires=None),
                _credit(remaining="6.00000000", expires="not-a-date"),
                # +08:00 与 Z 混用：两者都是 2099-04-30 16:00:00Z 之后，前者更早。
                _credit(remaining="4.00000000", expires="2099-05-01T00:00:00+08:00"),
                _credit(remaining="1.50000000", expires="2099-04-30T20:00:00Z"),
            ]
        },
    )

    assert result["temporary_credits"] == {
        "total_cny": "5.50000000",
        "count": 2,
        "nearest_expires_at": "2099-05-01T00:00:00+08:00",
        # 同日两条额度（4.0 + 1.5）：按到期日聚合，浮层显示的是"这天会过期多少钱"
        "nearest_amount_cny": "5.50000000",
    }


def test_temporary_credits_accepts_timestamps_and_alias_fields(http):
    """expiresAt 给毫秒时间戳、金额字段用别名时也要能算出来。

    官方控制台把 expiresAt 直接传给 new Date()，说明它可能是时间戳而不是 ISO 字符串；
    若只认 ISO，整批额度会被误判成"已过期"而清零，浮层就只剩一个「—」。
    """
    future_ms = 4102444800000  # 2100-01-01T00:00:00Z（毫秒）
    result = _overview_with_credits(
        http,
        {
            "items": [
                {"remainingCny": "2.50000000", "expiresAt": future_ms},
                {"remaining": "1.50000000", "expireAt": "2100-02-01T00:00:00Z"},
            ]
        },
    )

    credits = result["temporary_credits"]
    assert credits["count"] == 2
    assert credits["total_cny"] == "4.00000000"
    assert credits["nearest_amount_cny"] == "2.50000000"


def test_temporary_credits_seconds_timestamp(http):
    """秒级时间戳（10 位）同样认，不与毫秒混淆。"""
    result = _overview_with_credits(
        http,
        {"items": [{"remainingCny": "1.00000000", "expiresAt": 4102444800}]},
    )

    credits = result["temporary_credits"]
    assert credits["count"] == 1
    assert credits["total_cny"] == "1.00000000"


def test_temporary_credits_empty_returns_zero_defaults(http):
    """没有任何临时额度：返回 0 值且不算 partial（接口本身是成功的）。"""
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
    """外层响应两种兜底：data 为裸数组，或 dict 里取 items / records / list。"""
    items = [_credit(remaining="2.00000000")]
    data = items if container == "bare" else {container: items}

    result = _overview_with_credits(http, data)

    assert result["temporary_credits"] == {
        "total_cny": "2.00000000",
        "count": 1,
        "nearest_expires_at": "2099-01-01T00:00:00Z",
        "nearest_amount_cny": "2.00000000",
    }


def test_account_overview_temporary_credits_failure_partial(http):
    """额度接口失败：只降级 temporary_credits，其余三个分区照常返回。"""
    _login(http)
    http.queue(
        _wallet_ok(),
        _usage_ok(),
        _FakeResponse(200, _ok(dict(_ME_USER))),
        _FakeResponse(
            500, {"code": "INTERNAL", "message": "额度服务不可用", "data": None}
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
    # 其它分区不受影响，仍是成功取回的值。
    assert result["wallet"]["available_balance_cny"] == "298.09757398"
    assert result["usage_today"]["calls"] == 12
    assert result["user"]["id"] == "u1"
# ---------------------------------------------------------------------------
# 模型目录同步：把基元律动全部可用语言模型写入本地厂商
# ---------------------------------------------------------------------------


def _api_model(model_id, name=None, type_="chat", status="online", **extra):
    """GET /api/models 里的单个模型（字段取服务端真实形状）。"""
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
    """打桩 add_model_to_provider，记录 (provider_id, name, ctx, kwargs)。"""
    from core import model_manager

    calls = []

    def _add_model(provider_id, name, context_window, **kwargs):
        calls.append((provider_id, name, context_window, kwargs))
        return "m-" + name

    monkeypatch.setattr(model_manager, "add_model_to_provider", _add_model)
    return calls


def test_sync_models_adds_all_available_chat_models(http, monkeypatch):
    """3 个可用模型全部添加：名称取 id，上下文/输出/视觉/工具映射正确。"""
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
    # 只打公开模型目录一次，且不带任何 Key
    calls_http = _requests(http)
    assert [call["url"] for call in calls_http] == [
        "https://tokenrhythm.studio/api/models"
    ]
    assert "authorization" not in calls_http[0]["headers"]
    # 名称一律用 id，不用展示名
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
    # maxOutputTokens 缺失时不传 max_tokens，交给模型管理器的默认值
    assert calls[2] == (
        "p1",
        "kimi-k2",
        262144,
        {"supports_vision": False, "supports_function_calling": False},
    )


def test_sync_models_skips_non_chat_offline_and_non_openai(http, monkeypatch):
    """图片/视频/embedding、testing、openai=false、capabilities 缺失都跳过。"""
    _patch_providers(monkeypatch, _provider("p1"), _provider("p2", "基元律动"))
    # 绑定信息优先于 base_url 扫描：应写入 p2
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
    # protocolCapabilities 兜底：没有 capabilities 时按 supportsTools 缺失 → 默认支持工具调用
    assert calls[1] == (
        "p2",
        "proto-only",
        128000,
        {"supports_vision": False, "supports_function_calling": True},
    )


def test_sync_models_is_idempotent(http, monkeypatch):
    """再同步一次：已有同名模型全部跳过，added=0 / skipped=N。"""
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
    """单个模型添加失败只进 failed，其余模型照常添加。"""
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
    """本地没有任何基元律动厂商：抛中文异常，且不发请求。"""
    _patch_providers(monkeypatch)
    _sync_calls(monkeypatch)

    with pytest.raises(auth.TokenRhythmAuthError) as excinfo:
        auth.sync_models()

    assert "未找到基元律动厂商配置，请先登录" in str(excinfo.value)
    assert http.holder["client"] is None


# ---------------------------------------------------------------------------
# 会话守门：cookie 失效 → 绑定的专属 Key 不可用
# ---------------------------------------------------------------------------


def _restore_cookie_session():
    """本地写一份有效 cookie 并卸掉进程内 client，模拟"重启后靠会话文件恢复"。"""
    auth._save_session(_client_with_cookies(("tr_session", "abc123", None)))
    auth._CLIENT = None
    auth._STATE.update({"logged_in": False, "restored": False})
    auth._invalidate_health_cache()


def test_session_health_without_session_sends_no_request(http):
    """未登录且本地没有会话文件：直接返回「未登录」，一个请求都不发。"""
    result = auth.session_health()

    assert result == {"ok": False, "logged_in": False, "reason": "未登录基元律动账号"}
    assert _requests(http) == []


def test_session_health_ok_with_restored_cookie(http):
    """有会话且 /auth/me 成功：ok=True，并把登录态写回进程内。"""
    _restore_cookie_session()
    http.queue(_me())

    result = auth.session_health()

    assert result == {"ok": True, "logged_in": True, "reason": ""}
    assert auth._STATE["logged_in"] is True
    assert [call["url"] for call in _requests(http)] == [
        "https://tokenrhythm.studio/api/auth/me"
    ]


def test_session_health_401_clears_session_and_state(http, session_store):
    """401：清本地会话文件与进程内登录态，reason 提示重新登录。"""
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
    """200 但没返回用户对象：等同会话被作废，同样清会话并提示重新登录。"""
    _restore_cookie_session()
    http.queue(_FakeResponse(200, {"code": 0, "message": "ok", "data": {}}))

    result = auth.session_health()

    assert result["ok"] is False
    assert result["reason"] == "登录已失效，请重新登录"
    assert not session_store.exists()


def test_session_health_network_error_keeps_session(http, session_store):
    """网络异常：文案与鉴权失败区分，且绝不清掉仍然有效的会话。"""
    _restore_cookie_session()
    http.queue(httpx.ConnectTimeout("连接超时"))

    result = auth.session_health()

    assert result["ok"] is False
    assert result["reason"] == "会话校验失败（网络异常，可稍后重试）"
    assert session_store.exists(), "网络抖动不等于登录失效，不能清会话"


def test_session_health_cached_for_60s_and_force_refetches(http):
    """60 秒内重复调用只校验一次；force=True 立即重新校验。"""
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
    """缓存超过 60 秒自动失效，下次调用重新校验。"""
    _restore_cookie_session()
    http.queue(_me(), _me())

    auth.session_health()
    auth._HEALTH_CACHE["at"] -= auth._SESSION_HEALTH_TTL + 1
    auth.session_health()

    assert len(_requests(http)) == 2


def test_session_health_cache_invalidated_by_login(http, session_store):
    """登录成功会作废缓存：不会拿"未登录"的旧结论挡住刚登录的会话。"""
    assert auth.session_health()["ok"] is False
    assert _requests(http) == []

    client = auth._get_client()
    client.cookies.set("tr_session", "abc123", domain="tokenrhythm.studio", path="/")
    http.queue(_FakeResponse(200, _ok({"user": {"id": "u1"}})), _me())
    auth.password_login("13800000000", "pwd")

    http.queue(_me())
    result = auth.session_health()

    assert result["ok"] is True
    # 登录 2 次请求 + 登录后的 1 次健康检查：说明缓存确实被作废了
    assert len(_requests(http)) == 3


def test_binding_active_requires_key_and_provider(http):
    """只有同时有 key_id 与 provider_id 才算绑定生效；无绑定文件时为 False。"""
    try:
        assert auth.binding_active() is False
        auth._save_binding({"account_id": "u1", "provider_id": "p1", "key_id": "k1"})
        assert auth.binding_active() is True
        auth._save_binding({"account_id": "u1", "provider_id": "p1", "key_id": ""})
        assert auth.binding_active() is False
        auth._save_binding({"account_id": "u1", "provider_id": "", "key_id": "k1"})
        assert auth.binding_active() is False
    finally:
        # 必须清干净：http fixture 收尾的 logout 会按 binding 去删服务端 Key
        auth._clear_binding()
    assert auth.binding_active() is False
