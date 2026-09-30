"""T7：飞书设置的读写接口单测（密钥加密、快照不回明文、配置变更生效）。不触网。"""

import json

import pytest

import config
from core.feishu import config as feishu_config
from gui_bridge import ApiBridge


def _use_isolated_config(tmp_path, monkeypatch, data=None):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data or {}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


def _bare_bridge():
    return ApiBridge.__new__(ApiBridge)


class _FakeService:
    def __init__(self):
        self.stopped = 0
        self.restarted = 0

    def status(self):
        return {"state": "connected", "running": True, "received_events": 0,
                "duplicate_events": 0, "uptime_seconds": 1, "last_error": ""}

    def stop(self, timeout=5.0):
        self.stopped += 1

    def restart(self):
        self.restarted += 1
        return True


# ---------------------------------------------------------------- 读取

def test_get_settings_hides_secret(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "super_secret",
        "feishu_approvers": ["ou_a"],
        "feishu_notify_chat_id": "oc_x",
    })
    bridge = _bare_bridge()

    result = bridge.get_feishu_settings()

    assert result["ok"] is True
    settings = result["settings"]
    assert settings["app_id"] == "cli_demo"
    assert settings["has_secret"] is True
    assert settings["approvers"] == ["ou_a"]
    assert settings["notify_chat_id"] == "oc_x"
    assert "app_secret" not in settings
    assert "super_secret" not in json.dumps(settings, ensure_ascii=False)


def test_get_settings_reports_missing_secret(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"feishu_enabled": False, "feishu_app_id": "cli_x"})
    assert _bare_bridge().get_feishu_settings()["settings"]["has_secret"] is False


def test_get_secret_returns_plaintext_on_demand(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"feishu_app_secret": "super_secret"})
    result = _bare_bridge().get_feishu_secret()
    assert result == {"ok": True, "app_secret": "super_secret"}


# ---------------------------------------------------------------- 写入

def test_set_settings_encrypts_secret_and_round_trips(tmp_path, monkeypatch):
    cfg_path = _use_isolated_config(tmp_path, monkeypatch, {})
    monkeypatch.setattr(ApiBridge, "_ensure_feishu_service", lambda self: None)
    bridge = _bare_bridge()

    result = bridge.set_feishu_settings({
        "enabled": True,
        "app_id": "cli_demo",
        "app_secret": "plain_secret",
        "approvers": ["ou_a", "ou_b"],
        "notify_chat_id": "oc_x",
    })

    assert result["ok"] is True
    stored = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert stored["feishu_enabled"] is True
    assert stored["feishu_app_id"] == "cli_demo"
    assert stored["feishu_approvers"] == ["ou_a", "ou_b"]
    assert stored["feishu_notify_chat_id"] == "oc_x"
    # 密钥必须加密落盘，且能读回明文
    assert stored["feishu_app_secret"] != "plain_secret"
    assert feishu_config.load_feishu_config().app_secret == "plain_secret"
    # 快照不回明文
    assert "plain_secret" not in json.dumps(result, ensure_ascii=False)


def test_set_settings_empty_secret_keeps_existing(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"feishu_app_secret": "kept_secret"})
    monkeypatch.setattr(ApiBridge, "_ensure_feishu_service", lambda self: None)

    _bare_bridge().set_feishu_settings({"enabled": True, "app_id": "cli_x", "app_secret": ""})

    assert feishu_config.load_feishu_config().app_secret == "kept_secret"


def test_set_settings_enabling_starts_service(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    started = []
    monkeypatch.setattr(ApiBridge, "_ensure_feishu_service", lambda self: started.append(True))
    bridge = _bare_bridge()

    bridge.set_feishu_settings({"enabled": True, "app_id": "cli_x", "app_secret": "s"})

    assert started == [True]


def test_set_settings_disabling_stops_service(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    service = _FakeService()
    bridge._feishu_service = service

    result = bridge.set_feishu_settings({"enabled": False, "app_id": "cli_x"})

    assert result["ok"] is True
    assert service.stopped == 1
    assert getattr(bridge, "_feishu_service", None) is None


def test_set_settings_keeps_service_when_still_configured(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    bridge = _bare_bridge()
    service = _FakeService()
    bridge._feishu_service = service

    bridge.set_feishu_settings({"enabled": True, "app_id": "cli_x", "app_secret": "s"})

    assert service.restarted == 1
    assert service.stopped == 0


# ---------------------------------------------------------------- 测试连接

def test_test_connection_requires_credentials(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    result = _bare_bridge().test_feishu_connection()
    assert result["ok"] is False
    assert "请先填写" in result["msg"]


def test_test_connection_uses_endpoint_probe(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})

    class _FakeClient:
        def __init__(self, app_id, app_secret, source=None, **kwargs):
            self.app_id = app_id
            self.app_secret = app_secret

        def _get_conn_url(self):
            return "wss://example.invalid/ws?device_id=d1&service_id=s1"

    monkeypatch.setattr("core.feishu._lark_ws.Client", _FakeClient)

    result = _bare_bridge().test_feishu_connection("cli_x", "secret")

    assert result["ok"] is True
    assert result["endpoint_ready"] is True


def test_test_connection_reports_failure(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def _get_conn_url(self):
            raise RuntimeError("invalid app_id")

    monkeypatch.setattr("core.feishu._lark_ws.Client", _FakeClient)

    result = _bare_bridge().test_feishu_connection("cli_bad", "bad")

    assert result["ok"] is False
    assert "invalid app_id" in result["msg"]
