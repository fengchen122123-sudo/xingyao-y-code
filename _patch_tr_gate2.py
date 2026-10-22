"""一次性补丁脚本 2：修正空行 + 追加会话守门单测。

用法：venv\\Scripts\\python.exe _patch_tr_gate2.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRIDGE = ROOT / "gui_bridge.py"
CORE_TESTS = ROOT / "tests" / "test_tokenrhythm_auth.py"
BRIDGE_TESTS = ROOT / "tests" / "test_gui_bridge_tokenrhythm_auth.py"


def apply(path: Path, old: str, new: str, *, label: str, expect: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    if found != expect:
        raise SystemExit(f"[FAIL] {label}: 预期匹配 {expect} 处，实际 {found} 处（{path.name}）")
    path.write_text(text.replace(old, new, expect), encoding="utf-8")
    print(f"[OK] {label}")


def append(path: Path, block: str, *, label: str) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text.rstrip("\n") + "\n" + block, encoding="utf-8")
    print(f"[OK] {label}")


# 版式修正：类注释块与常量之间只留一个空行
apply(
    BRIDGE,
    """    # ------------------------------------------------------------------


    # 会话守门命中时返回给前端的固定提示（前端据此弹「重新登录」）。
""",
    """    # ------------------------------------------------------------------

    # 会话守门命中时返回给前端的固定提示（前端据此弹「重新登录」）。
""",
    label="bridge: 修掉多余空行",
)

# ---------------------------------------------------------------------------
# C. core 单测
# ---------------------------------------------------------------------------

CORE_TESTS_BLOCK = r'''

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
'''

append(CORE_TESTS, CORE_TESTS_BLOCK, label="core 测试：会话守门 9 例")

# ---------------------------------------------------------------------------
# D. bridge 单测
# ---------------------------------------------------------------------------

BRIDGE_TESTS_BLOCK = r'''

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
'''

append(BRIDGE_TESTS, BRIDGE_TESTS_BLOCK, label="bridge 测试：会话守门 7 例")

print("patch2 完成")
