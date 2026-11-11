"""gui_bridge 的基元律动（TokenRhythm）账号桥接方法单测。

全部打桩：不联网、不读写真实 ~/.ycode 配置、不创建 Key。
"""

import pytest

import core.tokenrhythm_auth as tr_auth
import gui_bridge
from gui_bridge import ApiBridge


SETTINGS = {"providers": [{"id": "prov_1", "name": "基元律动"}], "models": []}


def _make_bridge(*, settings=SETTINGS, sync_error=None):
    """裸实例 + 实例级打桩：绕过 ApiBridge.__init__ 的数据库/全局副作用。"""
    bridge = object.__new__(ApiBridge)
    calls = {"sync": 0}

    def _fake_sync(*args, **kwargs):
        calls["sync"] += 1
        if sync_error is not None:
            raise sync_error

    bridge.get_model_settings = lambda: settings
    bridge._sync_active_model_context = _fake_sync
    return bridge, calls


def _login_user():
    return {"user": {"id": 42, "nickname": "星瑶"}, "ok": True}


DEFAULT_SYNC = {
    "ok": True,
    "provider_id": "prov_1",
    "provider_name": "基元律动",
    "total": 0,
    "added": 0,
    "skipped": 0,
    "failed": [],
}


@pytest.fixture(autouse=True)
def _stub_sync_models(monkeypatch):
    """默认不触碰真实 sync_models（会联网并写配置）；需要断言的用例自行覆盖。"""
    monkeypatch.setattr(tr_auth, "sync_models", lambda provider_id=None: dict(DEFAULT_SYNC))


# ---------------------------------------------------------------------------
# 状态查询
# ---------------------------------------------------------------------------


def test_status_logged_out_and_not_configured(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(tr_auth, "account_status", lambda: {"logged_in": False})
    monkeypatch.setattr(gui_bridge, "load_config", lambda: {"providers": []})

    result = bridge.tr_auth_status()

    assert result == {"ok": True, "logged_in": False, "user": None, "configured": False}


def test_status_logged_in_and_configured(monkeypatch):
    bridge, _ = _make_bridge()
    user = {"id": 42, "nickname": "星瑶"}
    monkeypatch.setattr(tr_auth, "account_status", lambda: {"logged_in": True, "user": user})
    monkeypatch.setattr(
        gui_bridge,
        "load_config",
        lambda: {
            "providers": [
                {"id": "other", "base_url": "https://api.openai.com/v1", "api_key": "sk-other"},
                {
                    "id": "prov_1",
                    "base_url": "https://api.tokenrhythm.studio/v1",
                    "api_key": "sk-tr",
                },
            ]
        },
    )

    result = bridge.tr_auth_status()

    assert result["ok"] is True
    assert result["logged_in"] is True
    assert result["user"] == user
    assert result["configured"] is True


def test_status_provider_without_key_is_not_configured(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(tr_auth, "account_status", lambda: {"logged_in": False})
    monkeypatch.setattr(
        gui_bridge,
        "load_config",
        lambda: {
            "providers": [
                {"id": "prov_1", "base_url": "https://tokenrhythm.studio/v1", "api_key": ""}
            ]
        },
    )

    assert bridge.tr_auth_status()["configured"] is False


def test_status_config_read_failure_is_not_configured(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(tr_auth, "account_status", lambda: {"logged_in": False})

    def _boom():
        raise RuntimeError("config 坏了")

    monkeypatch.setattr(gui_bridge, "load_config", _boom)

    result = bridge.tr_auth_status()

    assert result["ok"] is True
    assert result["configured"] is False


def test_status_failure_returns_error(monkeypatch):
    bridge, _ = _make_bridge()

    def _boom():
        raise RuntimeError("会话查询炸了")

    monkeypatch.setattr(tr_auth, "account_status", _boom)
    monkeypatch.setattr(gui_bridge, "load_config", lambda: {"providers": []})

    assert bridge.tr_auth_status() == {"ok": False, "msg": "会话查询炸了"}


# ---------------------------------------------------------------------------
# 发送短信验证码
# ---------------------------------------------------------------------------


def test_send_sms_success(monkeypatch):
    bridge, _ = _make_bridge()
    seen = []

    def _fake_send(phone, captcha_verify_param=None):
        seen.append((phone, captcha_verify_param))
        return {
            "challenge_id": "chal_1",
            "interval_seconds": 60,
            "ttl_seconds": 300,
            "masked_target": "138****8000",
        }

    monkeypatch.setattr(tr_auth, "send_sms_code", _fake_send)

    result = bridge.tr_auth_send_sms(" 13800138000 ")

    # 未传 captcha：桥接层把空串归一成 None，core 层据此不写该字段。
    assert seen == [("13800138000", None)]
    assert result == {
        "ok": True,
        "challenge_id": "chal_1",
        "interval_seconds": 60,
        "ttl_seconds": 300,
        "masked_target": "138****8000",
        "msg": "验证码已发送",
    }


def test_send_sms_passes_captcha_verify_param(monkeypatch):
    """桥接层把前端给的 captchaVerifyParam 原样透传给 core 层。"""
    bridge, _ = _make_bridge()
    seen = []

    def _fake_send(phone, captcha_verify_param=None):
        seen.append((phone, captcha_verify_param))
        return {"challenge_id": "chal_2", "interval_seconds": 30, "ttl_seconds": 300}

    monkeypatch.setattr(tr_auth, "send_sms_code", _fake_send)

    result = bridge.tr_auth_send_sms("13800138000", "captcha-abc123-=_.%2B/end")

    assert seen == [("13800138000", "captcha-abc123-=_.%2B/end")]
    assert result["ok"] is True
    assert result["challenge_id"] == "chal_2"
    assert result["msg"] == "验证码已发送"


def test_send_sms_blank_captcha_normalized_to_none(monkeypatch):
    """captcha 为空串 / 纯空白时归一成 None，不把空串透传给 core。"""
    bridge, _ = _make_bridge()
    seen = []

    def _fake_send(phone, captcha_verify_param=None):
        seen.append((phone, captcha_verify_param))
        return {"challenge_id": "chal_3"}

    monkeypatch.setattr(tr_auth, "send_sms_code", _fake_send)

    assert bridge.tr_auth_send_sms("13800138000", "")["ok"] is True
    assert bridge.tr_auth_send_sms("13800138000", "   ")["ok"] is True

    assert seen == [("13800138000", None), ("13800138000", None)]


def test_send_sms_empty_phone_rejected(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "send_sms_code", lambda *a, **k: pytest.fail("空手机号不应调用服务端")
    )

    assert bridge.tr_auth_send_sms("   ") == {"ok": False, "msg": "请输入手机号"}


def test_send_sms_error_message_passthrough(monkeypatch):
    bridge, _ = _make_bridge()
    expected = (
        "短信登录需要完成安全验证，请重试或在弹窗内完成滑块验证；"
        "若仍失败可改用密码登录。服务端返回：请先完成图形验证码"
    )

    def _boom(phone, captcha_verify_param=None):
        raise tr_auth.TokenRhythmAuthError(expected)

    monkeypatch.setattr(tr_auth, "send_sms_code", _boom)

    result = bridge.tr_auth_send_sms("13800138000", "captcha-param")

    assert result["ok"] is False
    assert result["msg"] == expected
    assert "安全验证" in result["msg"]
    assert "密码登录" in result["msg"]

# ---------------------------------------------------------------------------
# 短信登录
# ---------------------------------------------------------------------------


def test_sms_login_creates_key_and_syncs(monkeypatch):
    bridge, calls = _make_bridge()
    sms_seen = []
    bind_calls = []

    def _fake_sms_login(phone, code, invite_code=None):
        sms_seen.append((phone, code, invite_code))
        return _login_user()

    def _fake_ensure_bound():
        bind_calls.append(True)
        return {
            "created": True,
            "provider_id": "prov_1",
            "model_added": True,
            "reason": "已自动创建并绑定 Key",
        }

    monkeypatch.setattr(tr_auth, "sms_login", _fake_sms_login)
    monkeypatch.setattr(tr_auth, "ensure_bound", _fake_ensure_bound)

    result = bridge.tr_auth_sms_login(" 13800138000 ", " 123456 ")

    assert sms_seen == [("13800138000", "123456", None)]
    assert bind_calls == [True]
    assert calls["sync"] == 1
    assert result["ok"] is True
    assert result["user"] == {"id": 42, "nickname": "星瑶"}
    assert result["bind"]["created"] is True
    assert result["bind_ok"] is True
    assert result["bind_error"] == ""
    assert result["settings"] == SETTINGS
    assert result["msg"] == "登录成功，已自动配置基元律动 API Key（同步 0 个模型）"


def test_sms_login_existing_key_message(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )
    monkeypatch.setattr(
        tr_auth,
        "ensure_bound",
        lambda: {"created": False, "provider_id": "prov_1", "reason": "已有可用 Key"},
    )

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert result["ok"] is True
    assert result["bind"]["created"] is False
    assert result["bind_ok"] is True
    assert result["msg"] == "登录成功，本地已有可用 API Key（同步 0 个模型）"


def test_sms_login_bind_failure_is_not_silenced(monkeypatch):
    """登录成功但绑定失败：ok 仍为 True（账号已登录），但 bind/msg 明确报错。"""
    bridge, calls = _make_bridge()
    error_message = "API Key 已创建，但写入模型厂商配置失败：磁盘只读"
    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )

    def _boom():
        raise tr_auth.TokenRhythmAuthError(error_message)

    monkeypatch.setattr(tr_auth, "ensure_bound", _boom)

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert result["ok"] is True
    assert result["user"] == {"id": 42, "nickname": "星瑶"}
    assert result["bind_ok"] is False
    assert result["bind"]["ok"] is False
    assert result["bind"]["created"] is False
    assert result["bind_error"] == error_message
    assert result["bind"]["error"] == error_message
    assert error_message in result["msg"]
    assert "自动绑定 API Key 失败" in result["msg"]
    assert result["settings"] == SETTINGS
    assert calls["sync"] == 0  # 绑定失败时不再宣称已同步


def test_sms_login_sync_failure_ignored(monkeypatch):
    bridge, _ = _make_bridge(sync_error=RuntimeError("sync 炸了"))
    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )
    monkeypatch.setattr(
        tr_auth,
        "ensure_bound",
        lambda: {"created": True, "provider_id": "prov_1", "model_added": True},
    )

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert result["ok"] is True
    assert result["bind_ok"] is True


def test_sms_login_empty_params(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "sms_login", lambda *a, **k: pytest.fail("参数不全不应调用登录")
    )

    assert bridge.tr_auth_sms_login("", "123456") == {"ok": False, "msg": "请输入手机号"}
    assert bridge.tr_auth_sms_login("13800138000", "  ") == {"ok": False, "msg": "请输入验证码"}


def test_sms_login_error_message_passthrough(monkeypatch):
    bridge, _ = _make_bridge()

    def _boom(phone, code, invite_code=None):
        raise tr_auth.TokenRhythmAuthError("验证码错误或已过期")

    monkeypatch.setattr(tr_auth, "sms_login", _boom)

    result = bridge.tr_auth_sms_login("13800138000", "000000")

    assert result == {"ok": False, "msg": "验证码错误或已过期"}


def test_sms_login_passes_invite_code(monkeypatch):
    """桥接层把前端给的邀请码原样透传给 core 层（不做大小写 / 格式改写）。"""
    bridge, _ = _make_bridge()
    seen = []

    def _fake_sms_login(phone, code, invite_code=None):
        seen.append((phone, code, invite_code))
        return _login_user()

    monkeypatch.setattr(tr_auth, "sms_login", _fake_sms_login)
    monkeypatch.setattr(
        tr_auth,
        "ensure_bound",
        lambda: {"created": False, "provider_id": "prov_1", "reason": "已有可用 Key"},
    )

    result = bridge.tr_auth_sms_login("13800138000", "123456", "INV-AbC123")

    assert seen == [("13800138000", "123456", "INV-AbC123")]
    assert result["ok"] is True
    assert result["msg"] == "登录成功，本地已有可用 API Key（同步 0 个模型）"


@pytest.mark.parametrize("invite_code", ["", "   ", None])
def test_sms_login_blank_invite_code_normalized_to_none(monkeypatch, invite_code):
    """邀请码为空串 / 纯空白（或未传）时归一成 None，不把空串透传给 core。"""
    bridge, _ = _make_bridge()
    seen = []

    def _fake_sms_login(phone, code, invite_code=None):
        seen.append(invite_code)
        return _login_user()

    monkeypatch.setattr(tr_auth, "sms_login", _fake_sms_login)
    monkeypatch.setattr(
        tr_auth,
        "ensure_bound",
        lambda: {"created": False, "provider_id": "prov_1", "reason": "已有可用 Key"},
    )

    assert bridge.tr_auth_sms_login("13800138000", "123456", invite_code)["ok"] is True
    assert seen == [None]


# ---------------------------------------------------------------------------
# 密码登录
# ---------------------------------------------------------------------------


def test_password_login_success(monkeypatch):
    bridge, calls = _make_bridge()
    seen = []
    bind_calls = []

    def _fake_login(account, password):
        seen.append((account, password))
        return _login_user()

    def _fake_ensure_bound():
        bind_calls.append(True)
        return {"created": True, "provider_id": "prov_1", "model_added": True}

    monkeypatch.setattr(tr_auth, "password_login", _fake_login)
    monkeypatch.setattr(tr_auth, "ensure_bound", _fake_ensure_bound)

    result = bridge.tr_auth_password_login(" me@example.com ", "secret")

    assert seen == [("me@example.com", "secret")]
    assert bind_calls == [True]
    assert calls["sync"] == 1
    assert result["ok"] is True
    assert result["user"] == {"id": 42, "nickname": "星瑶"}
    assert result["bind"]["created"] is True
    assert result["settings"] == SETTINGS
    assert result["msg"] == "登录成功，已自动配置基元律动 API Key（同步 0 个模型）"


def test_password_login_error_passthrough(monkeypatch):
    bridge, _ = _make_bridge()

    def _boom(account, password):
        raise tr_auth.TokenRhythmAuthError("账号或密码错误")

    monkeypatch.setattr(tr_auth, "password_login", _boom)

    result = bridge.tr_auth_password_login("me@example.com", "wrong")

    assert result == {"ok": False, "msg": "账号或密码错误"}


def test_password_login_empty_params(monkeypatch):
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "password_login", lambda *a, **k: pytest.fail("参数不全不应调用登录")
    )

    assert bridge.tr_auth_password_login("  ", "pw") == {
        "ok": False,
        "msg": "请输入手机号或邮箱",
    }
    assert bridge.tr_auth_password_login("me@example.com", "") == {
        "ok": False,
        "msg": "请输入密码",
    }


def test_password_login_settings_failure_degrades(monkeypatch):
    bridge = object.__new__(ApiBridge)
    bridge._sync_active_model_context = lambda *a, **k: None

    def _boom_settings():
        raise RuntimeError("设置读取失败")

    bridge.get_model_settings = _boom_settings
    monkeypatch.setattr(tr_auth, "password_login", lambda account, password: _login_user())
    monkeypatch.setattr(
        tr_auth, "ensure_bound", lambda: {"created": False, "provider_id": "prov_1"}
    )

    result = bridge.tr_auth_password_login("me@example.com", "pw")

    assert result["ok"] is True
    assert result["settings"] == {}


# ---------------------------------------------------------------------------
# 退出登录
# ---------------------------------------------------------------------------


def test_logout_success(monkeypatch):
    bridge, _ = _make_bridge()
    logout_calls = []

    def _fake_logout():
        logout_calls.append(True)
        return {"ok": True, "had_key": True, "key_deleted": True}

    monkeypatch.setattr(tr_auth, "logout", _fake_logout)

    result = bridge.tr_auth_logout()

    assert logout_calls == [True]
    assert result["ok"] is True
    assert result["key_deleted"] is True
    assert result["msg"] == "已退出基元律动账号，该账号的专属 API Key 已删除"


def test_logout_failure(monkeypatch):
    bridge, _ = _make_bridge()

    def _boom():
        raise RuntimeError("退出失败")

    monkeypatch.setattr(tr_auth, "logout", _boom)

    assert bridge.tr_auth_logout() == {"ok": False, "msg": "退出失败"}


# ---------------------------------------------------------------------------
# 账号概览
# ---------------------------------------------------------------------------

_OVERVIEW = {
    "ok": True,
    "user": {"id": 42, "nickname": "星瑶", "phone": "13800000000"},
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
        "total_cny": "12.30000000",
        "count": 2,
        "nearest_expires_at": "2026-11-01T10:20:30+08:00",
        "nearest_amount_cny": "5.00000000",
    },
}


def test_account_overview_success_passthrough(monkeypatch):
    """成功：透传 user / wallet / usage_today / temporary_credits，金额仍是字符串。"""
    bridge, _ = _make_bridge()
    monkeypatch.setattr(tr_auth, "account_overview", lambda: dict(_OVERVIEW))

    result = bridge.tr_account_overview()

    assert result == _OVERVIEW
    assert isinstance(result["wallet"]["available_balance_cny"], str)
    assert isinstance(result["usage_today"]["cost_cny"], str)
    # 临时额度金额走字符串原样透传，桥接层不做任何数值换算
    assert result["temporary_credits"]["total_cny"] == "12.30000000"
    assert isinstance(result["temporary_credits"]["total_cny"], str)
    assert result["temporary_credits"]["count"] == 2
    assert result["temporary_credits"]["nearest_expires_at"] == "2026-11-01T10:20:30+08:00"
    assert "partial" not in result


def test_account_overview_temporary_credits_missing_default(monkeypatch):
    """core 没给 temporary_credits（老版本）时补零值三键，前端不必判空。"""
    bridge, _ = _make_bridge()
    legacy = {k: v for k, v in _OVERVIEW.items() if k != "temporary_credits"}
    monkeypatch.setattr(tr_auth, "account_overview", lambda: dict(legacy))

    result = bridge.tr_account_overview()

    assert result["ok"] is True
    assert result["temporary_credits"] == {
        "total_cny": "0",
        "count": 0,
        "nearest_expires_at": "",
        "nearest_amount_cny": "0",
    }


@pytest.mark.parametrize("broken", [None, "", [], "12.3"])
def test_account_overview_temporary_credits_not_dict_default(monkeypatch, broken):
    """temporary_credits 不是字典（None / 字符串等）时同样降级成零值三键。"""
    bridge, _ = _make_bridge()
    data = dict(_OVERVIEW)
    data["temporary_credits"] = broken
    monkeypatch.setattr(tr_auth, "account_overview", lambda: dict(data))

    result = bridge.tr_account_overview()

    assert result["temporary_credits"] == {
        "total_cny": "0",
        "count": 0,
        "nearest_expires_at": "",
        "nearest_amount_cny": "0",
    }


def test_account_overview_partial_passthrough(monkeypatch):
    """部分失败：partial 与 errors 一并透传，前端可提示"部分数据暂不可用"。"""
    bridge, _ = _make_bridge()
    partial = dict(_OVERVIEW)
    partial["wallet"] = {
        "available_balance_cny": "0",
        "frozen_balance_cny": "",
        "currency": "CNY",
    }
    partial["partial"] = True
    partial["errors"] = {"wallet": "服务端请求失败（HTTP 500）"}
    monkeypatch.setattr(tr_auth, "account_overview", lambda: partial)

    result = bridge.tr_account_overview()

    assert result["ok"] is True
    assert result["partial"] is True
    assert result["errors"] == {"wallet": "服务端请求失败（HTTP 500）"}
    assert result["wallet"]["available_balance_cny"] == "0"
    assert result["usage_today"]["calls"] == 12


def test_account_overview_not_logged_in(monkeypatch):
    """core 返回 ok=False（未登录）：桥接层收敛成 ok=False + msg，不抛异常。"""
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "account_overview", lambda: {"ok": False, "reason": "未登录"}
    )

    assert bridge.tr_account_overview() == {"ok": False, "msg": "未登录"}


def test_account_overview_error_message_passthrough(monkeypatch):
    """异常：返回 ok=False 且 msg 为异常原文。"""
    bridge, _ = _make_bridge()

    def _boom():
        raise tr_auth.TokenRhythmAuthError("获取账号概览失败：登录已过期")

    monkeypatch.setattr(tr_auth, "account_overview", _boom)

    assert bridge.tr_account_overview() == {
        "ok": False,
        "msg": "获取账号概览失败：登录已过期",
    }

# ---------------------------------------------------------------------------
# 登录后模型同步 / 手动同步模型
# ---------------------------------------------------------------------------


def test_sms_login_syncs_models_with_bound_provider(monkeypatch):
    """绑定成功后按 provider_id 同步模型，新增数量写进 msg。"""
    bridge, _ = _make_bridge()
    seen = []

    def _fake_sync_models(provider_id=None):
        seen.append(provider_id)
        return {
            "ok": True,
            "provider_id": provider_id,
            "provider_name": "基元律动",
            "total": 7,
            "added": 5,
            "skipped": 2,
            "failed": [],
        }

    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )
    monkeypatch.setattr(
        tr_auth,
        "ensure_bound",
        lambda: {"created": True, "provider_id": "prov_9", "reason": "已自动创建并绑定 Key"},
    )
    monkeypatch.setattr(tr_auth, "sync_models", _fake_sync_models)

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert seen == ["prov_9"]
    assert result["ok"] is True
    assert result["models"]["ok"] is True
    assert result["models"]["added"] == 5
    assert result["models"]["provider_id"] == "prov_9"
    assert "同步 5 个模型" in result["msg"]


def test_sms_login_bind_failure_skips_model_sync(monkeypatch):
    """绑定失败时不做模型同步，models.ok=False。"""
    bridge, _ = _make_bridge()
    sync_calls = []

    def _boom():
        raise tr_auth.TokenRhythmAuthError("写入模型厂商配置失败：磁盘只读")

    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )
    monkeypatch.setattr(
        tr_auth, "sync_models", lambda *a, **k: sync_calls.append(a) or {}
    )
    monkeypatch.setattr(tr_auth, "ensure_bound", _boom)

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert sync_calls == []
    assert result["ok"] is True
    assert result["models"]["ok"] is False
    assert "自动绑定 API Key 失败" in result["msg"]


def test_sms_login_model_sync_failure_keeps_login_ok(monkeypatch):
    """模型同步异常不影响登录结论：ok 仍为 True，models 带失败原因。"""
    bridge, _ = _make_bridge()
    monkeypatch.setattr(
        tr_auth, "sms_login", lambda phone, code, invite_code=None: _login_user()
    )
    monkeypatch.setattr(
        tr_auth, "ensure_bound", lambda: {"created": False, "provider_id": "prov_1"}
    )

    def _boom(provider_id=None):
        raise RuntimeError("模型接口 502")

    monkeypatch.setattr(tr_auth, "sync_models", _boom)

    result = bridge.tr_auth_sms_login("13800138000", "123456")

    assert result["ok"] is True
    assert result["bind_ok"] is True
    assert result["models"]["ok"] is False
    assert "模型同步失败" in result["msg"]
    assert "模型接口 502" in result["msg"]


def test_tr_auth_sync_models_passthrough_and_error(monkeypatch):
    """tr_auth_sync_models：成功透传 core 结果并附 settings；异常收敛成 ok=False。"""
    bridge, calls = _make_bridge()
    payload = {
        "ok": True,
        "provider_id": "prov_1",
        "provider_name": "基元律动",
        "total": 3,
        "added": 2,
        "skipped": 1,
        "failed": [],
    }
    monkeypatch.setattr(tr_auth, "sync_models", lambda provider_id=None: dict(payload))

    ok_result = bridge.tr_auth_sync_models()

    assert ok_result == {**payload, "settings": SETTINGS}
    assert calls["sync"] == 1

    def _boom(provider_id=None):
        raise tr_auth.TokenRhythmAuthError("未找到基元律动厂商配置，请先登录")

    monkeypatch.setattr(tr_auth, "sync_models", _boom)

    bad_result = bridge.tr_auth_sync_models()

    assert bad_result == {"ok": False, "msg": "未找到基元律动厂商配置，请先登录"}


# ---------------------------------------------------------------------------
# 会话守门：cookie 失效 → 绑定的 Key 不可用（只影响 tokenrhythm 厂商）
# ---------------------------------------------------------------------------

import threading  # noqa: E402  - 追加段自带导入，避免动文件头部
from types import SimpleNamespace  # noqa: E402

_GATE_PROVIDERS = [
    {
        "id": "prov_tr",
        "name": "基元律动",
        "base_url": "https://tokenrhythm.studio/v1",
        "api_key": "dpapi:stored",
        "protocol": "openai",
        "models": [{"id": "tr/m1", "name": "m1", "litellm_model": "openai/m1"}],
    },
    {
        "id": "prov_other",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "api_key": "dpapi:other",
        "protocol": "openai",
        "models": [{"id": "oa/m1", "name": "m1", "litellm_model": "openai/m1"}],
    },
]

_NEED_LOGIN_RESULT = {
    "ok": False,
    "msg": "基元律动登录已失效，请重新登录后再使用该模型",
    "need_login": True,
}


def _make_send_bridge(monkeypatch, *, active_model_id="tr/m1"):
    """能走完 send_user_message 的裸实例：回合启动全部打桩，不碰数据库 / 线程。"""
    bridge = object.__new__(ApiBridge)
    bridge._shutting_down = False
    bridge._current_session = "session_1"
    bridge._turn_start_lock = threading.Lock()
    spawned = []
    bridge._is_turn_active = lambda session: False
    bridge._spawn_turn_worker = lambda *a, **k: spawned.append({"args": a, "kwargs": k})
    bridge._memory = SimpleNamespace(
        set_session_model_id=lambda *a, **k: None,
        ensure_session=lambda *a, **k: None,
    )
    bridge._set_current_session = lambda session: None
    monkeypatch.setattr(
        gui_bridge,
        "load_config",
        lambda: {"providers": _GATE_PROVIDERS, "active_model_id": active_model_id},
    )
    return bridge, spawned


def test_send_message_blocked_when_tr_session_expired(monkeypatch):
    """tokenrhythm 模型 + 会话失效：发消息被拦截，不进入模型调用。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    monkeypatch.setattr(tr_auth, "binding_active", lambda: True)
    monkeypatch.setattr(
        tr_auth,
        "session_health",
        lambda force=False: {"ok": False, "logged_in": False, "reason": "登录已失效，请重新登录"},
    )

    result = bridge.send_user_message("你好", model_id="tr/m1")

    assert result == _NEED_LOGIN_RESULT
    assert spawned == [], "会话失效时绝不启动回合 / 不调用模型"


def test_send_message_proceeds_when_tr_session_ok(monkeypatch):
    """tokenrhythm 模型 + 会话有效：照常发消息（守门不误伤）。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    seen = []
    monkeypatch.setattr(tr_auth, "binding_active", lambda: True)
    monkeypatch.setattr(
        tr_auth,
        "session_health",
        lambda force=False: seen.append(force) or {"ok": True, "logged_in": True, "reason": ""},
    )

    result = bridge.send_user_message("你好", model_id="tr/m1")

    assert result == {"ok": True}
    assert len(spawned) == 1
    assert seen == [False], "正常路径用缓存，不强制校验"


def test_send_message_non_tokenrhythm_model_skips_session_health(monkeypatch):
    """非 tokenrhythm 模型：完全不触发 binding_active / session_health（零额外请求）。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    called = []

    def _boom(*args, **kwargs):
        called.append(True)
        raise AssertionError("非 tokenrhythm 模型不得触发会话校验")

    monkeypatch.setattr(tr_auth, "binding_active", _boom)
    monkeypatch.setattr(tr_auth, "session_health", _boom)

    result = bridge.send_user_message("你好", model_id="oa/m1")

    assert result == {"ok": True}
    assert spawned, "非 tokenrhythm 模型必须照常进入模型调用"
    assert called == []


def test_send_message_tr_model_without_binding_skips_session_health(monkeypatch):
    """tokenrhythm 模型但本地没有绑定（用户手填 Key）：不做会话校验。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    called = []
    monkeypatch.setattr(tr_auth, "binding_active", lambda: False)
    monkeypatch.setattr(
        tr_auth, "session_health", lambda *a, **k: called.append(True) or {"ok": False}
    )

    result = bridge.send_user_message("你好", model_id="tr/m1")

    assert result == {"ok": True}
    assert spawned
    assert called == []


def test_send_message_falls_back_to_active_model_for_gate(monkeypatch):
    """前端没带 model_id 时按会话生效模型判断，tokenrhythm 同样受守门。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    monkeypatch.setattr(
        gui_bridge,
        "get_active_model",
        lambda session=None: {"base_url": "https://tokenrhythm.studio/v1", "id": "tr/m1"},
    )
    monkeypatch.setattr(tr_auth, "binding_active", lambda: True)
    monkeypatch.setattr(
        tr_auth, "session_health", lambda *a, **k: {"ok": False, "logged_in": False}
    )

    result = bridge.send_user_message("你好")

    assert result == _NEED_LOGIN_RESULT
    assert spawned == []


def test_send_message_fails_open_when_gate_errors(monkeypatch):
    """守门自身异常必须放行：不能让健康检查故障把对话入口打挂。"""
    bridge, spawned = _make_send_bridge(monkeypatch)
    monkeypatch.setattr(tr_auth, "binding_active", lambda: True)

    def _boom(*args, **kwargs):
        raise RuntimeError("健康检查炸了")

    monkeypatch.setattr(tr_auth, "session_health", _boom)

    result = bridge.send_user_message("你好", model_id="tr/m1")

    assert result == {"ok": True}
    assert spawned


def test_tr_session_health_passthrough(monkeypatch):
    """tr_session_health 透传 core 结果（含 force），异常收敛成 ok=False。"""
    bridge, _ = _make_bridge()
    seen = []
    monkeypatch.setattr(
        tr_auth,
        "session_health",
        lambda force=False: seen.append(force)
        or {"ok": True, "logged_in": True, "reason": ""},
    )

    assert bridge.tr_session_health() == {"ok": True, "logged_in": True, "reason": ""}
    assert bridge.tr_session_health(force=True) == {
        "ok": True,
        "logged_in": True,
        "reason": "",
    }
    assert seen == [False, True]

    def _boom(force=False):
        raise tr_auth.TokenRhythmAuthError("会话查询失败")

    monkeypatch.setattr(tr_auth, "session_health", _boom)

    assert bridge.tr_session_health() == {
        "ok": False,
        "logged_in": False,
        "reason": "会话查询失败",
        "msg": "会话查询失败",
    }

