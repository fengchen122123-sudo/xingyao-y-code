"""T3：飞书通道服务骨架单测（生命周期、事件转发、配置读取）。

不触网：传输层 Client 用假实现替换。
"""

import json
import time

import pytest

import config
from core.credentials import protect_secret
from core.feishu import config as feishu_config
from core.feishu import service as feishu_service
from core.feishu.service import (
    STATE_CONNECTED,
    STATE_DISABLED,
    STATE_STOPPED,
    FeishuService,
)


def _use_isolated_config(tmp_path, monkeypatch, data):
    """隔离 JSON 与 TOML overlay，避免开发机 config.toml 污染断言。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(config, "_TOML_CONFIG_FILE", str(tmp_path / "config.toml"))
    return cfg


class _FakeClient:
    """假传输层：start() 阻塞直到 stop()，用于验证线程生命周期。"""

    instances = []

    def __init__(self, app_id, app_secret, on_event=None, log_level=None, source=None):
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_event = on_event
        self.source = source
        self.started = False
        self.stopped = False
        self.on_connected = None
        self.on_reconnecting = None
        self.on_reconnected = None
        _FakeClient.instances.append(self)

    def start(self):
        self.started = True
        if callable(self.on_connected):
            self.on_connected()
        while not self.stopped:
            time.sleep(0.005)

    def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(feishu_service, "Client", _FakeClient)
    yield


# ---------------------------------------------------------------- 配置读取

def test_load_config_parses_fields_and_plaintext_secret(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "plain_secret",
        "feishu_approvers": "ou_a, ou_b，ou_c",
        "feishu_notify_chat_id": "oc_demo",
    })
    cfg = feishu_config.load_feishu_config()
    assert cfg.enabled is True
    assert cfg.app_id == "cli_demo"
    assert cfg.app_secret == "plain_secret"
    assert cfg.approvers == ["ou_a", "ou_b", "ou_c"]  # 兼容中英文逗号
    assert cfg.notify_chat_id == "oc_demo"
    assert cfg.configured is True
    assert cfg.has_secret is True


def test_load_config_decrypts_protected_secret(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": protect_secret("super_secret"),
    })
    assert feishu_config.load_feishu_config().app_secret == "super_secret"


def test_load_config_defaults_when_absent(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {})
    cfg = feishu_config.load_feishu_config()
    assert cfg.enabled is False
    assert cfg.configured is False
    assert cfg.approvers == []


# ---------------------------------------------------------------- 生命周期

def test_start_skipped_when_not_configured(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {"feishu_enabled": True, "feishu_app_id": "cli_x"})
    svc = FeishuService()
    assert svc.start() is False
    assert svc.status()["state"] == STATE_DISABLED
    assert svc.status()["running"] is False
    assert _FakeClient.instances == []          # 未配置时不得创建任何连接


def test_start_disabled_flag_blocks_start(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": False,
        "feishu_app_id": "cli_x",
        "feishu_app_secret": "s",
    })
    svc = FeishuService()
    assert svc.start() is False
    assert _FakeClient.instances == []


def test_start_and_stop_lifecycle(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "secret",
    })
    svc = FeishuService()
    try:
        assert svc.start() is True
        # 并发重复 start 不得创建第二个连接
        assert svc.start() is False
        deadline = time.time() + 2
        while time.time() < deadline and svc.status()["state"] != STATE_CONNECTED:
            time.sleep(0.01)
        assert svc.status()["state"] == STATE_CONNECTED
        assert len(_FakeClient.instances) == 1
        assert _FakeClient.instances[0].app_id == "cli_demo"
    finally:
        svc.stop()
    assert svc.status()["running"] is False
    assert svc.status()["state"] == STATE_STOPPED
    # stop 可重入
    svc.stop()


def test_status_never_exposes_secret(tmp_path, monkeypatch):
    _use_isolated_config(tmp_path, monkeypatch, {
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "super_secret",
    })
    snapshot = FeishuService().status()
    assert snapshot["has_secret"] is True
    assert "super_secret" not in json.dumps(snapshot, ensure_ascii=False)


# ---------------------------------------------------------------- 事件转发

def test_on_event_is_noop_without_bridge():
    svc = FeishuService()
    svc.on_event("message_final", {"session_name": "s1"})  # 不应抛异常


def test_on_event_forwards_to_bridge_and_swallows_bridge_error():
    calls = []

    class _Bridge:
        def on_agent_event(self, event_type, data):
            calls.append((event_type, data))
            raise RuntimeError("bridge 内部错误")  # 不得冒泡到事件热路径

    svc = FeishuService(bridge=_Bridge())
    svc.on_event("turn_finish", {"session_name": "s1"})
    assert calls == [("turn_finish", {"session_name": "s1"})]


def test_handle_event_dedups_and_returns_bridge_result():
    handled = []

    class _Bridge:
        def on_feishu_event(self, event):
            handled.append(event)
            return {"toast": {"type": "success", "content": "已处理"}}

    svc = FeishuService(bridge=_Bridge())
    payload = json.dumps({
        "header": {"event_id": "ev-1", "event_type": "card.action.trigger"},
        "event": {"action": {"value": {"k": "v"}}},
    }).encode("utf-8")

    first = svc._handle_event(payload)
    second = svc._handle_event(payload)  # 同一 event_id 重投

    assert first == {"toast": {"type": "success", "content": "已处理"}}
    assert second is None                       # 重复事件不再处理
    assert len(handled) == 1
    assert svc.status()["received_events"] == 1
    assert svc.status()["duplicate_events"] == 1


def test_handle_event_ignores_missing_event_id():
    handled = []

    class _Bridge:
        def on_feishu_event(self, event):
            handled.append(event)

    svc = FeishuService(bridge=_Bridge())
    assert svc._handle_event(json.dumps({"header": {"event_type": "x"}}).encode()) is None
    assert handled == []


def test_handle_event_tolerates_invalid_json():
    svc = FeishuService(bridge=object())
    assert svc._handle_event(b"not-json") is None


# ---------------------------------------------------------------- gui_bridge 接线

def test_ensure_feishu_service_tolerates_bare_bridge(tmp_path, monkeypatch):
    """回归：裸实例（__new__ 构造，不经过 __init__）缺状态时不得抛异常。

    此前该场景会在后台启动线程里抛 AttributeError（_feishu_service 属性不存在），
    被 pytest 记为线程未处理异常。
    """
    from gui_bridge import ApiBridge

    _use_isolated_config(tmp_path, monkeypatch, {})  # 未配置：应直接返回 None
    bridge = ApiBridge.__new__(ApiBridge)
    assert bridge._ensure_feishu_service() is None
    # 已配置时应真正启动并缓存实例（换文件名：配置读取有 mtime 缓存，同路径重写不生效）
    cfg2 = tmp_path / "config_enabled.json"
    cfg2.write_text(json.dumps({
        "feishu_enabled": True,
        "feishu_app_id": "cli_demo",
        "feishu_app_secret": "secret",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "_CONFIG_FILE", str(cfg2))
    bridge2 = ApiBridge.__new__(ApiBridge)
    svc = bridge2._ensure_feishu_service()
    try:
        assert svc is not None
        assert bridge2._ensure_feishu_service() is svc   # 双检锁：复用同一实例
    finally:
        svc.stop()
