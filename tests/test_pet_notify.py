"""Tests for GUI -> desktop pet task notifications and stdin command framing."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

import gui_pet_mixin
from core import pet_manager


class _DummyBridge(gui_pet_mixin.PetBridgeMixin):
    pass


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 43210
        self.stdin = io.BytesIO()

    def poll(self):
        return None


def test_notify_bubble_spec_covers_all_four_scenarios():
    finish = gui_pet_mixin._notify_bubble_spec("turn_finish", {"session_name": "s1"})
    assert finish is not None
    assert finish["kind"] == "finish"
    assert finish["emote"] == "happy"
    assert finish["sticky"] is False
    assert "完成" in finish["text"]

    err_with_msg = gui_pet_mixin._notify_bubble_spec("turn_error", {"message": "连接超时"})
    assert err_with_msg is not None
    assert err_with_msg["kind"] == "error"
    assert err_with_msg["emote"] == "look_around"
    assert err_with_msg["sticky"] is False
    assert "连接超时" in err_with_msg["text"]

    err_plain = gui_pet_mixin._notify_bubble_spec("error", {})
    assert err_plain is not None
    assert err_plain["kind"] == "error"
    assert "出错" in err_plain["text"]

    ask = gui_pet_mixin._notify_bubble_spec(
        "permission_request",
        {"function_name": "ask_user", "arguments": {"question": "请选择部署环境"}},
    )
    assert ask is not None
    assert ask["kind"] == "question"
    assert ask["emote"] == "interact"
    assert ask["sticky"] is True
    assert "等你选" in ask["text"]
    assert "请选择部署环境" in ask["text"]

    perm = gui_pet_mixin._notify_bubble_spec(
        "permission_request",
        {"function_name": "execute_command", "summary": "执行命令：\npytest"},
    )
    assert perm is not None
    assert perm["kind"] == "permission"
    assert perm["emote"] == "look_around"
    assert perm["sticky"] is True
    assert "权限" in perm["text"]
    assert "执行命令" in perm["text"]

    assert gui_pet_mixin._notify_bubble_spec("token_delta", {}) is None


def test_pet_notify_event_pushes_clears_and_dedups_errors(monkeypatch):
    bridge = _DummyBridge()
    pushed = []
    cleared = []

    monkeypatch.setattr(gui_pet_mixin, "_config_bool", lambda key, default=False: True)
    monkeypatch.setattr(gui_pet_mixin, "_last_error_notify", (0.0, ""))
    monkeypatch.setattr(
        pet_manager,
        "push_bubble",
        lambda text, ms=2600, **kw: pushed.append((text, ms, kw)) or {"ok": True, "msg": "ok"},
    )
    monkeypatch.setattr(
        pet_manager,
        "clear_bubble",
        lambda kinds=None: cleared.append(tuple(kinds or ())) or {"ok": True, "msg": "ok"},
    )

    bridge._pet_notify_event("permission_request", {"function_name": "ask_user", "arguments": {"question": "选哪项"}})
    assert len(pushed) == 1
    assert pushed[0][2]["kind"] == "question"
    assert pushed[0][2]["sticky"] is True

    bridge._pet_notify_event("permission_dismiss", {})
    assert cleared == [("question", "permission")]

    # turn_error + immediate duplicate error -> only 1 push
    bridge._pet_notify_event("turn_error", {"message": "网络失败"})
    bridge._pet_notify_event("error", {"message": "网络失败"})
    assert len(pushed) == 2

    # standalone error with different message -> pushes normally
    bridge._pet_notify_event("error", {"message": "未激活模型"})
    assert len(pushed) == 3
    assert "未激活模型" in pushed[2][0]

    bridge._pet_notify_event("turn_finish", {})
    assert len(pushed) == 4
    assert pushed[3][2]["kind"] == "finish"
    assert pushed[3][2]["emote"] == "happy"


def test_pet_notify_event_respects_disabled_setting(monkeypatch):
    bridge = _DummyBridge()
    pushed = []
    monkeypatch.setattr(gui_pet_mixin, "_config_bool", lambda key, default=False: False)
    monkeypatch.setattr(
        pet_manager,
        "push_bubble",
        lambda text, ms=2600, **kw: pushed.append(text) or {"ok": True},
    )
    bridge._pet_notify_event("turn_finish", {})
    assert pushed == []


def test_pet_manager_push_and_clear_bubble_stdin_json(monkeypatch):
    fake_proc = _FakeProc()
    monkeypatch.setattr(pet_manager, "_PROC", fake_proc)

    r1 = pet_manager.push_bubble("需要权限：执行命令", 15000, kind="permission", emote="look_around", sticky=True)
    assert r1["ok"] is True
    r2 = pet_manager.clear_bubble(["question", "permission"])
    assert r2["ok"] is True

    lines = fake_proc.stdin.getvalue().decode("utf-8").strip().splitlines()
    assert len(lines) == 2
    p1 = json.loads(lines[0])
    assert p1 == {
        "text": "需要权限：执行命令",
        "ms": 15000,
        "kind": "permission",
        "emote": "look_around",
        "sticky": True,
    }
    p2 = json.loads(lines[1])
    assert p2 == {"clear": True, "kinds": ["question", "permission"]}
