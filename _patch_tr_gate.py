"""一次性补丁脚本：给 core/tokenrhythm_auth.py 与 gui_bridge.py 增加会话守门。

用法：venv\\Scripts\\python.exe _patch_tr_gate.py
完成后可删除本文件（不属于交付物）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CORE = ROOT / "core" / "tokenrhythm_auth.py"
BRIDGE = ROOT / "gui_bridge.py"


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
    path.write_text(text + block, encoding="utf-8")
    print(f"[OK] {label}")


# ---------------------------------------------------------------------------
# A. core/tokenrhythm_auth.py
# ---------------------------------------------------------------------------

apply(
    CORE,
    """    # 本次进程启动时是否成功从本地恢复过 cookie（没有就不必白白试探 /auth/me）
    "restored": False,
}
""",
    """    # 本次进程启动时是否成功从本地恢复过 cookie（没有就不必白白试探 /auth/me）
    "restored": False,
}

# 会话健康检查（session_health）的上次结论：{"result": dict | None, "at": monotonic}
# at 是判定时刻，用于 60 秒 TTL；登录 / 退出 / 重置时清空，避免拿旧结论卡住新会话。
_HEALTH_CACHE: dict[str, Any] = {"result": None, "at": 0.0}

# 会话健康检查的三种中文原因：区分「没登录」「登录失效」「网络不通」，文案直接给
# 用户看——网络抖动时不该逼用户重新登录，所以这两种结论绝不能合并成一句话。
_HEALTH_REASON_NO_LOGIN = "未登录基元律动账号"
_HEALTH_REASON_EXPIRED = "登录已失效，请重新登录"
_HEALTH_REASON_NETWORK = "会话校验失败（网络异常，可稍后重试）"
""",
    label="core: 健康检查缓存与文案常量",
)

apply(
    CORE,
    """                "restored": False,
            }
        )
    if client is not None:
""",
    """                "restored": False,
            }
        )
    # 会话状态变了：健康检查缓存必须同步作废，否则旧结论会卡住新会话最多 60 秒
    _invalidate_health_cache()
    if client is not None:
""",
    label="core: _reset_state 作废健康检查缓存",
)

apply(
    CORE,
    """    # 登录成功即持久化会话：重启应用后自动恢复登录态（cookie 约 30 天有效）
    _save_session(_get_client())
    return {"user": user, "ok": True}
""",
    """    # 登录成功即持久化会话：重启应用后自动恢复登录态（cookie 约 30 天有效）
    _save_session(_get_client())
    # 刚登录的会话必须立刻重新校验，不能拿登录前的旧健康结论挡住对话
    _invalidate_health_cache()
    return {"user": user, "ok": True}
""",
    label="core: password_login 作废健康检查缓存",
)

apply(
    CORE,
    """    # 短信登录同样持久化会话（未注册手机号会自动建号）
    _save_session(_get_client())
    return {"user": user, "ok": True}
""",
    """    # 短信登录同样持久化会话（未注册手机号会自动建号）
    _save_session(_get_client())
    # 同上：短信登录成功后立即作废旧结论
    _invalidate_health_cache()
    return {"user": user, "ok": True}
""",
    label="core: sms_login 作废健康检查缓存",
)

SESSION_SECTION = '''
# ---------------------------------------------------------------------------
# 会话守门：cookie 失效 → 绑定在会话上的专属 Key 不可用
# ---------------------------------------------------------------------------


def _invalidate_health_cache() -> None:
    """作废会话健康检查缓存（登录 / 退出 / 会话被清理时调用）。"""
    with _LOCK:
        _HEALTH_CACHE["result"] = None
        _HEALTH_CACHE["at"] = 0.0


def _cached_health(now: float) -> dict | None:
    """取 TTL 内的上次结论；返回副本，调用方改返回值不会污染缓存。"""
    with _LOCK:
        result = _HEALTH_CACHE.get("result")
        at = float(_HEALTH_CACHE.get("at") or 0.0)
    if isinstance(result, dict) and now - at < _SESSION_HEALTH_TTL:
        return dict(result)
    return None


def _remember_health(result: dict, now: float) -> dict:
    """记住本次结论（只保留约定的三个字段）并返回副本。"""
    snapshot = {
        "ok": bool(result.get("ok")),
        "logged_in": bool(result.get("logged_in")),
        "reason": str(result.get("reason") or ""),
    }
    with _LOCK:
        _HEALTH_CACHE["result"] = dict(snapshot)
        _HEALTH_CACHE["at"] = now
    return snapshot


def _is_network_failure(error: BaseException) -> bool:
    """区分「网络层异常」与「服务端否决了会话」。

    网络层错误由 _request 在拿到响应之前抛出，因此没有 HTTP 状态码；status 有值
    就说明服务端确实回过话（401 等），属于鉴权失败。日志与提示只给结论，
    绝不打印 cookie / Key。
    """
    if getattr(error, "status", None) is not None:
        return False
    if isinstance(getattr(error, "__cause__", None), httpx.HTTPError):
        return True
    return str(error).startswith(("网络请求失败", "请求基元律动服务失败"))


def _expire_session() -> None:
    """会话被服务端判定失效：清本地会话文件与进程内登录态，不留半登录状态。"""
    with _LOCK:
        _STATE["logged_in"] = False
        _STATE["restored"] = False
    _clear_session()
    _invalidate_health_cache()


def session_health(force: bool = False) -> dict:
    """会话健康检查：登录时创建的专属 Key 只在会话有效时可用。

    对话入口每轮都要问一次，所以带 60 秒缓存（``force=True`` 强制重新校验），
    避免每发一条消息都打一次 /api/auth/me。返回固定三字段：

    - ok：会话是否仍然可用（只有 True 才允许用这枚 Key 继续对话）；
    - logged_in：当前是否处于登录态；
    - reason：ok=False 时的中文原因。

    判定顺序：
    1. 无进程内登录态、且本地也恢复不出会话 → 直接返回「未登录」，不发任何请求；
    2. 有会话 → ``GET /api/auth/me``：
       - 成功且拿到 user：ok=True，并把登录态写回进程内；
       - 401 / 服务端否决 / 没返回用户：清会话文件与登录态，
         reason「登录已失效，请重新登录」；
       - 网络异常：**不清会话**（网络抖动不等于登录失效，否则一次超时就把用户
         踢下线），reason「会话校验失败（网络异常，可稍后重试）」，logged_in
         保持原有判断。

    日志只记结论与异常类型，绝不打印 cookie 或 Key。
    """
    now = time.monotonic()
    if not force:
        cached = _cached_health(now)
        if cached is not None:
            return cached

    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
    _get_client()  # 首次调用会尝试从本地恢复 cookie，并置 _STATE["restored"]
    with _LOCK:
        restored = bool(_STATE.get("restored"))

    if not logged_in and not restored:
        # 既没进程内登录态、也没有可恢复的会话文件：没有可校验的会话，不发请求
        return _remember_health(
            {"ok": False, "logged_in": False, "reason": _HEALTH_REASON_NO_LOGIN}, now
        )

    try:
        payload = _request("GET", _ME_PATH)
    except TokenRhythmAuthError as error:
        if _is_network_failure(error):
            logger.warning("基元律动会话校验失败（网络异常）：%s", type(error).__name__)
            return _remember_health(
                {"ok": False, "logged_in": logged_in, "reason": _HEALTH_REASON_NETWORK},
                now,
            )
        logger.info("基元律动登录已失效，已清理本地会话")
        _expire_session()
        return _remember_health(
            {"ok": False, "logged_in": False, "reason": _HEALTH_REASON_EXPIRED}, now
        )
    except Exception as error:  # noqa: BLE001 - 任何异常都不能把对话入口打崩
        logger.warning("基元律动会话校验异常：%s", type(error).__name__)
        return _remember_health(
            {"ok": False, "logged_in": logged_in, "reason": _HEALTH_REASON_NETWORK}, now
        )

    user = _user_from(payload)
    if not user:
        # 200 但没有用户对象：等同于会话已被服务端作废，按失效处理
        logger.info("基元律动会话校验未返回用户信息，按登录失效处理")
        _expire_session()
        return _remember_health(
            {"ok": False, "logged_in": False, "reason": _HEALTH_REASON_EXPIRED}, now
        )

    with _LOCK:
        _STATE["logged_in"] = True
    return _remember_health({"ok": True, "logged_in": True, "reason": ""}, now)


def binding_active() -> bool:
    """本地是否存在「账号 ⇄ 专属 Key」绑定（有 key_id 与 provider_id 才算）。

    只读本地加密绑定文件，不联网、不触发任何请求：对话入口用它低成本判断
    「这枚 Key 是不是登录时自动绑定的」，是才需要做会话守门。
    """
    binding = _load_binding() or {}
    return bool(
        str(binding.get("key_id") or "").strip()
        and str(binding.get("provider_id") or "").strip()
    )

'''

apply(
    CORE,
    """    return {"logged_in": True, "user": user}


# ---------------------------------------------------------------------------
# 账号概览：余额 + 今日用量 + 个人信息 + 临时额度
# ---------------------------------------------------------------------------
""",
    """    return {"logged_in": True, "user": user}

""" + SESSION_SECTION + """
# ---------------------------------------------------------------------------
# 账号概览：余额 + 今日用量 + 个人信息 + 临时额度
# ---------------------------------------------------------------------------
""",
    label="core: 新增 session_health / binding_active",
)

apply(
    CORE,
    """    "account_status",
    "account_overview",
""",
    """    "account_status",
    "session_health",
    "binding_active",
    "account_overview",
""",
    label="core: __all__ 补充导出",
)

# ---------------------------------------------------------------------------
# B. gui_bridge.py
# ---------------------------------------------------------------------------

GATE_HELPERS = '''
    # 会话守门命中时返回给前端的固定提示（前端据此弹「重新登录」）。
    _TR_NEED_LOGIN_MSG = "基元律动登录已失效，请重新登录后再使用该模型"

    def _tr_effective_base_url(self, model_id: str = "") -> str:
        """本条消息真正会用的模型的 base_url；解析不出来返回空串。

        优先用前端随消息带来的激活模型 id（落库后就是会话绑定模型），取不到再回退
        会话 / 全局激活模型。只读本地配置，不联网、不发请求。
        """
        target = str(model_id or "").strip()
        if target:
            try:
                for provider in load_config().get("providers", []) or []:
                    if not isinstance(provider, dict):
                        continue
                    for model in provider.get("models") or []:
                        if isinstance(model, dict) and str(model.get("id") or "") == target:
                            return str(provider.get("base_url") or "")
            except Exception:
                logger.debug("解析激活模型厂商失败", exc_info=True)
        try:
            active = get_active_model(getattr(self, "_current_session", None)) or {}
        except Exception:
            logger.debug("解析会话生效模型失败，按非基元律动处理", exc_info=True)
            return ""
        return str(active.get("base_url") or "")

    def _tr_session_gate(self, model_id: str = "") -> Optional[Dict[str, Any]]:
        """基元律动会话守门：专属 Key 的登录会话失效时返回拦截结果，否则 None。

        只有「本条消息用的模型来自 tokenrhythm 厂商」且「本地存在账号绑定的专属
        Key」时才校验会话，其它模型的调用路径完全不受影响（不多发任何请求）。
        守门自身异常一律放行并记日志：宁可漏拦一次，也不能把对话入口打挂。
        """
        try:
            base_url = self._tr_effective_base_url(model_id).lower()
            if "tokenrhythm.studio" not in base_url:
                return None
            if not tokenrhythm_auth.binding_active():
                return None
            health = tokenrhythm_auth.session_health() or {}
            if bool(health.get("ok")):
                return None
            logger.warning(
                "基元律动会话不可用，已拦截发消息：%s", str(health.get("reason") or "")
            )
            return {"ok": False, "msg": self._TR_NEED_LOGIN_MSG, "need_login": True}
        except Exception:
            logger.warning("基元律动会话守门校验失败，本次放行", exc_info=True)
            return None

'''

apply(
    BRIDGE,
    """    def _tr_settings_snapshot(self) -> Dict[str, Any]:
""",
    GATE_HELPERS + """    def _tr_settings_snapshot(self) -> Dict[str, Any]:
""",
    label="bridge: 新增会话守门 helper",
)

apply(
    BRIDGE,
    """    def tr_auth_status(self) -> Dict[str, Any]:
""",
    '''    def tr_session_health(self, force: bool = False) -> Dict[str, Any]:
        """透传 core 的会话健康检查：前端可在发消息前 / 定时刷新登录态提示。

        force=True 时跳过 core 的 60 秒缓存强制校验（用户点「重新检查」用）。
        """
        try:
            health = tokenrhythm_auth.session_health(force=bool(force)) or {}
        except Exception as e:
            logger.exception("tr_session_health 失败")
            return {"ok": False, "logged_in": False, "reason": str(e), "msg": str(e)}
        return {
            "ok": bool(health.get("ok")),
            "logged_in": bool(health.get("logged_in")),
            "reason": str(health.get("reason") or ""),
        }

    def tr_auth_status(self) -> Dict[str, Any]:
''',
    label="bridge: 新增 tr_session_health 桥接方法",
)

apply(
    BRIDGE,
    """            target_session = session_name or self._current_session
            clean_model_id = str(model_id or "").strip()
""",
    """            target_session = session_name or self._current_session
            clean_model_id = str(model_id or "").strip()
            # 会话守门：基元律动登录时创建的专属 Key 与登录会话绑定，cookie 失效后
            # 这枚 Key 不允许继续对话——在排队 / 启动回合之前就拦住，并提示重新登录。
            # 非 tokenrhythm 厂商的模型直接跳过（不触发任何额外请求）。
            blocked = self._tr_session_gate(clean_model_id)
            if blocked is not None:
                return blocked
""",
    label="bridge: send_user_message 接入守门",
)

print("patch 完成")
