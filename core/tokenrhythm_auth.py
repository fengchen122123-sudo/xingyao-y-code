"""基元律动（TokenRhythm）账号登录与 API Key 自动绑定。

对外提供三类能力：

1. 账号登录：短信验证码登录 / 密码登录 / 会话查询 / 退出登录。
2. 账号概览：一次取回余额、今日用量、个人信息与临时额度（只读，不落盘）。
3. Key 自动绑定：登录成功后**总是**为当前账号新建一枚专属 API Key（名称「星瑶Code」）
   写入模型厂商配置，绝不复用用户在模型配置里手动填过的 Key；「账号 ⇄ 厂商/Key」
   绑定关系加密落盘，同一账号重复登录复用这枚 Key，换账号时先删掉旧账号的 Key 与
   厂商再整体重建，退出登录时也会先删掉这枚 Key。

安全约定：
- 账号会话 cookie 默认只在进程内存；登录成功后可持久化到
  ``~/.ycode/secrets/tokenrhythm_session.json``，内容经 Windows DPAPI 加密
  （绑定当前 Windows 用户），用于重启后保持登录态。cookie 由服务端下发约 30 天
  有效，过期或被服务端判定失效时自动清除；
- 密码、明文 Key 绝不写日志、不写盘（写配置走 config / model_manager 既有
  DPAPI 加密路径）；
- 短信 / 密码登录不做自动重试，失败直接把服务端 message 抛给上层展示。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://tokenrhythm.studio"
API_BASE_URL = BASE_URL + "/v1"
DEFAULT_PROVIDER_NAME = "基元律动"
# 登录自动创建的厂商会在 provider 记录里打上这个标记：退出登录 / 换账号时只清理
# 带标记（或命中本地绑定）的条目。用户自己填 Key 配置的同域厂商没有标记，
# 任何情况下都不会被改名或删除。
MANAGED_PROVIDER_FLAG = "managed_by"
# 注意：这个值不是插件名。core/plugin_loader 的孤儿清理会通过
# _core_managed_markers() 把它排除，否则每次启动都会把账号绑定的厂商连 Key 删掉。
MANAGED_PROVIDER_VALUE = "tokenrhythm"
# 上游某条模型记录缺 contextWindow（或给了非正数）时的保守兜底值：只为让本地
# 配置有个合法正数，不代表模型真实上限——有值一律用接口给的 contextWindow。
DEFAULT_CONTEXT_WINDOW = 128000
# 注意：这里刻意不再保留"默认占位模型"常量。历史实现首次绑定时会硬编码写入
# deepseek-v4.1-flash（128000 上下文）作为占位条目，但该裸名模型已在上游目录
# 下线，只会给用户留下一条调不通的脏数据。模型目录统一由 sync_models() 按
# GET /api/models 返回的真实 contextWindow 填充。
API_KEY_NAME = "星瑶Code"

_REQUEST_TIMEOUT = 20.0
_TERMS_CACHE_TTL = 30 * 60
# 会话健康检查结果缓存时长：对话入口每轮都会问一次，60 秒内复用上次结论，
# 避免每发一条消息都打一次 /api/auth/me。
_SESSION_HEALTH_TTL = 60.0


def _tokenrhythm_secret_dir() -> Path:
    """会话与绑定文件所在目录。YCODE_DATA_DIR 优先，未设置时仍是 ~/.ycode。"""
    env_dir = os.environ.get("YCODE_DATA_DIR")
    root = Path(env_dir) if env_dir and str(env_dir).strip() else Path.home() / ".ycode"
    return root / "secrets"


# 登录会话落盘位置：只存 cookie（已加密），不存密码 / token。
# 测试会整体替换这两个 Path，所以保持模块级常量，不改成每次现算。
_TR_SECRET_DIR = _tokenrhythm_secret_dir()
try:
    _TR_SECRET_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
_SESSION_FILE = _TR_SECRET_DIR / "tokenrhythm_session.json"
# 账号 ⇄ Key 绑定关系落盘位置：只存账号 id / 昵称、厂商 id、Key id、绑定时间
# （不含任何 Key 明文，整体 DPAPI 加密），用于判断本地厂商是否属于当前登录账号，
# 换账号时据此整体重建。
_BINDING_FILE = _TR_SECRET_DIR / "tokenrhythm_binding.json"

# 短信发码阶段可能遇到的安全验证（阿里云验证码）错误码：需要给出可执行的中文提示。
_CAPTCHA_ERROR_CODES = frozenset(
    {"CAPTCHA_REQUIRED", "CAPTCHA_INVALID", "CAPTCHA_SERVICE_UNAVAILABLE"}
)

# 各错误码对应的用户引导：既要说明"要完成安全验证"，也要给出兜底路径（密码登录）。
_CAPTCHA_HINTS = {
    "CAPTCHA_REQUIRED": (
        "短信登录需要完成安全验证，请重试或在弹窗内完成滑块验证；"
        "若仍失败可改用密码登录。"
    ),
    "CAPTCHA_INVALID": (
        "安全验证未通过或已过期，请在弹窗内重新完成滑块验证后再试；"
        "若仍失败可改用密码登录。"
    ),
    "CAPTCHA_SERVICE_UNAVAILABLE": (
        "安全验证服务暂时不可用，请稍后重试或在弹窗内重新完成滑块验证；"
        "若仍失败可改用密码登录。"
    ),
}

# 创建 Key 响应里明文 Key 的字段名未最终确认：按顺序兜底尝试。
_PLAIN_KEY_FIELDS = ("key", "apiKey", "secret", "plaintext", "token")

_LOCK = threading.RLock()
_CLIENT: httpx.Client | None = None
_STATE: dict[str, Any] = {
    "logged_in": False,
    "challenge_id": None,
    "challenge_phone": None,
    "terms_version": None,
    "terms_fetched_at": 0.0,
    # 本次进程启动时是否成功从本地恢复过 cookie（没有就不必白白试探 /auth/me）
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


class TokenRhythmAuthError(RuntimeError):
    """登录 / 绑定失败。message 为可直接展示给用户的中文提示。"""

    def __init__(
        self,
        message: str,
        *,
        code: Any = None,
        server_message: str = "",
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.server_message = server_message
        self.status = status


# ---------------------------------------------------------------------------
# 进程内会话
# ---------------------------------------------------------------------------


def _save_session(client: httpx.Client) -> None:
    """把当前会话 cookie 加密写入本地，供下次启动恢复登录态。

    只保存 name/value/domain/path/expires；cookie 等同于账号凭证，整体经
    DPAPI 加密（绑定当前 Windows 用户）后再落盘。已过期的 cookie 不写。
    保存失败只记日志，绝不影响本次登录结果。
    """
    try:
        now = time.time()
        cookies: list[dict[str, Any]] = []
        for cookie in client.cookies.jar:
            expires = cookie.expires
            if expires is not None:
                try:
                    if float(expires) <= now:
                        continue
                except (TypeError, ValueError):
                    continue
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "expires": expires,
                }
            )
        if not cookies:
            return
        from core.credentials import protect_secret

        payload = json.dumps(
            {"v": 1, "saved_at": int(now), "cookies": cookies}, ensure_ascii=False
        )
        _SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_FILE.write_text(protect_secret(payload), encoding="utf-8")
        try:
            os.chmod(_SESSION_FILE, 0o600)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - 持久化失败不能影响登录主流程
        logger.warning("基元律动会话保存失败（不影响本次登录）", exc_info=True)


def _load_session(client: httpx.Client) -> bool:
    """从本地恢复会话 cookie；文件不存在 / 解密失败 / 全部过期时返回 False。"""
    try:
        if not _SESSION_FILE.exists():
            return False
        from core.credentials import unprotect_secret

        raw = unprotect_secret(_SESSION_FILE.read_text(encoding="utf-8").strip())
        data = json.loads(raw)
        cookies = data.get("cookies") if isinstance(data, dict) else None
        if not isinstance(cookies, list) or not cookies:
            return False
        now = time.time()
        restored = False
        for item in cookies:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if not name or not value:
                continue
            expires = item.get("expires")
            if expires is not None:
                try:
                    if float(expires) <= now:
                        continue
                except (TypeError, ValueError):
                    continue
            client.cookies.set(
                name,
                value,
                domain=str(item.get("domain") or "tokenrhythm.studio"),
                path=str(item.get("path") or "/"),
            )
            restored = True
        return restored
    except Exception:  # noqa: BLE001 - 恢复失败按未登录处理
        logger.warning("基元律动会话恢复失败，按未登录处理", exc_info=True)
        return False


def _clear_session() -> None:
    """删除本地会话文件（退出登录、服务端判定失效时调用）。"""
    try:
        _SESSION_FILE.unlink(missing_ok=True)
    except OSError:
        logger.warning("基元律动会话清理失败", exc_info=True)


def _get_client() -> httpx.Client:
    """返回进程内单例 Client；首次创建时尝试恢复上次登录的 cookie。"""
    global _CLIENT
    with _LOCK:
        if _CLIENT is None:
            client = httpx.Client(timeout=_REQUEST_TIMEOUT)
            if _load_session(client):
                _STATE["restored"] = True
            _CLIENT = client
        return _CLIENT


def _reset_state() -> None:
    global _CLIENT
    with _LOCK:
        client = _CLIENT
        _CLIENT = None
        _STATE.update(
            {
                "logged_in": False,
                "challenge_id": None,
                "challenge_phone": None,
                "terms_version": None,
                "terms_fetched_at": 0.0,
                # 退出/重置后下次必须重新判定是否能从本地恢复会话，
                # 否则残留的 restored=True 会让 account_status 白发一次 /auth/me
                "restored": False,
            }
        )
    # 会话状态变了：健康检查缓存必须同步作废，否则旧结论会卡住新会话最多 60 秒
    _invalidate_health_cache()
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def logout() -> dict:
    """退出登录：先删掉服务端那枚专属 Key，再清本地绑定 / 会话 / 进程内状态。

    顺序固定：``POST /api/api-keys/{id}/delete`` → 清 binding → 清会话文件与进程
    状态。删 Key 失败（网络异常 / 用户已在控制台删掉 / 会话已失效）只记 warning，
    绝不阻断退出——否则用户会被卡在登录态；日志只出现 key_id，绝不出现 Key 明文。
    """
    binding = _load_binding() or {}
    key_id = str(binding.get("key_id") or "").strip()
    deleted = _delete_api_key(key_id) if key_id else False
    # 模型配置一起移除：退出后不该在模型列表里继续留着一个用不了的基元律动
    providers_removed = _delete_tr_providers()
    _clear_binding()
    _reset_state()
    _clear_session()
    # 返回删除结果：删 Key 失败时前端要如实提示用户去控制台手动清理，
    # 不能一律显示"已删除"。
    return {
        "ok": True,
        "had_key": bool(key_id),
        "key_deleted": deleted,
        "providers_removed": providers_removed,
    }


def _require_login() -> None:
    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
    if not logged_in:
        raise TokenRhythmAuthError("尚未登录基元律动账号，请先登录后再自动绑定 API Key")


# ---------------------------------------------------------------------------
# HTTP 基础设施
# ---------------------------------------------------------------------------


def _headers_for(method: str) -> dict[str, str]:
    headers = {"accept": "application/json"}
    if method.upper() != "GET":
        # 非 GET 必须补 CSRF 相关头，否则服务端返回 403 CSRF_INVALID。
        headers.update(
            {
                "content-type": "application/json",
                "origin": BASE_URL,
                "referer": BASE_URL + "/",
                "sec-fetch-site": "same-origin",
            }
        )
        # 登录后服务端会下发 tr_csrf cookie，官方前端在每次非 GET 请求时把它回填到
        # X-CSRF-Token；只带 sec-fetch-site 只能过登录接口，登录后的写接口（建 Key 等）
        # 仍会 403 CSRF_INVALID。
        try:
            csrf_token = _get_client().cookies.get("tr_csrf")
        except Exception:
            csrf_token = None
        if csrf_token:
            headers["x-csrf-token"] = str(csrf_token)
    try:
        from core.llm_policy import tokenrhythm_agent_headers

        headers.update(tokenrhythm_agent_headers(BASE_URL))
    except Exception:
        pass
    return headers


def _is_error_code(code: Any) -> bool:
    return isinstance(code, str) and code.strip() != ""


def _json_payload(response: Any, status: int) -> dict:
    try:
        payload = response.json()
    except Exception as error:
        raise TokenRhythmAuthError(
            f"服务端返回了无法解析的响应（HTTP {status}），请稍后重试"
        ) from error
    if not isinstance(payload, dict):
        raise TokenRhythmAuthError(
            f"服务端返回了非预期的响应格式（HTTP {status}），请稍后重试"
        )
    return payload


def _error_text(server_message: str, code: Any, status: int) -> str:
    text = server_message or f"服务端请求失败（HTTP {status}）"
    if code:
        text = f"{text}（错误码：{code}）"
    return text


def _request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    allow_empty_data: bool = False,
    timeout: float | None = None,
) -> dict:
    """调用 /api/*，成功返回完整 envelope，失败抛 TokenRhythmAuthError。

    allow_empty_data：允许 `data` 为 null 也算成功（删除类接口可能返回空 data）。
    timeout：单次请求超时（秒），缺省用 _REQUEST_TIMEOUT。
    """
    url = BASE_URL + path
    try:
        response = _get_client().request(
            method.upper(),
            url,
            json=body,
            headers=_headers_for(method),
            timeout=timeout if timeout is not None else _REQUEST_TIMEOUT,
        )
    except httpx.HTTPError as error:
        raise TokenRhythmAuthError(
            f"网络请求失败，请检查网络后重试：{error}"
        ) from error
    except Exception as error:  # 兜底：任何非 HTTP 层异常也收敛成可展示中文
        raise TokenRhythmAuthError(f"请求基元律动服务失败：{error}") from error

    status = int(getattr(response, "status_code", 0) or 0)
    payload = _json_payload(response, status)
    code = payload.get("code")
    server_message = str(payload.get("message") or "").strip()
    has_data = payload.get("data") is not None
    failed = (
        status >= 400
        or _is_error_code(code)
        or (code is not None and code != 0)
        or (not has_data and not allow_empty_data)
    )
    if failed:
        raise TokenRhythmAuthError(
            _error_text(server_message, code, status),
            code=code,
            server_message=server_message,
            status=status,
        )
    return payload


def _payload_data(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _user_from(payload: dict) -> dict:
    data = _payload_data(payload)
    user = data.get("user")
    if isinstance(user, dict):
        return user
    return data


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 账号登录
# ---------------------------------------------------------------------------


def password_login(account: str, password: str) -> dict:
    """密码登录，并用 GET /api/auth/me 校验会话。"""
    account = str(account or "").strip()
    password = str(password or "")
    if not account:
        raise TokenRhythmAuthError("请输入基元律动账号（手机号或邮箱）")
    if not password:
        raise TokenRhythmAuthError("请输入基元律动账号密码")

    login_payload = _request(
        "POST", "/api/auth/login", {"account": account, "password": password}
    )
    user = _user_from(login_payload)

    me_payload = _request("GET", "/api/auth/me")
    me_user = _user_from(me_payload)
    if me_user:
        user = me_user
    if not user:
        raise TokenRhythmAuthError("登录成功，但服务端未返回用户信息，请稍后重试")

    with _LOCK:
        _STATE["logged_in"] = True
        _STATE["challenge_id"] = None
        _STATE["challenge_phone"] = None
    # 登录成功即持久化会话：重启应用后自动恢复登录态（cookie 约 30 天有效）
    _save_session(_get_client())
    # 刚登录的会话必须立刻重新校验，不能拿登录前的旧健康结论挡住对话
    _invalidate_health_cache()
    return {"user": user, "ok": True}


def send_sms_code(phone: str, captcha_verify_param: str | None = None) -> dict:
    """发送短信验证码，challengeId 保存在进程内供 sms_login 使用。

    captcha_verify_param 是阿里云验证码 success 回调产出的凭证，官方前端在发码
    请求里原样上送为 ``captchaVerifyParam``。只有非空时才加该字段：空串会被服务端
    当成“提供了但无效”直接判失败，所以空值一律不带该键。该值是一次性校验凭据，
    绝不写日志。
    """
    phone = str(phone or "").strip()
    if not phone:
        raise TokenRhythmAuthError("请输入手机号")
    body: dict[str, Any] = {"scene": "login_register", "phone": phone}
    captcha = str(captcha_verify_param or "")
    if captcha.strip():
        # 值原样透传，不做任何改写。
        body["captchaVerifyParam"] = captcha
    try:
        payload = _request("POST", "/api/auth/sms/send", body)
    except TokenRhythmAuthError as error:
        code = str(error.code or "")
        if code in _CAPTCHA_ERROR_CODES:
            hint = _CAPTCHA_HINTS.get(
                code,
                "短信登录需要完成安全验证，请重试或在弹窗内完成滑块验证；"
                "若仍失败可改用密码登录。",
            )
            detail = f"服务端返回：{error.server_message}" if error.server_message else ""
            raise TokenRhythmAuthError(
                hint + detail,
                code=error.code,
                server_message=error.server_message,
                status=error.status,
            ) from error
        raise

    data = _payload_data(payload)
    challenge_id = str(data.get("challengeId") or "").strip()
    if not challenge_id:
        raise TokenRhythmAuthError("验证码已发送，但服务端未返回 challengeId，请稍后重试")
    with _LOCK:
        _STATE["challenge_id"] = challenge_id
        _STATE["challenge_phone"] = phone
    return {
        "challenge_id": challenge_id,
        "interval_seconds": _as_int(data.get("intervalSeconds"), 60),
        "ttl_seconds": _as_int(data.get("ttlSeconds"), 300),
        "masked_target": str(data.get("maskedTarget") or ""),
    }


def _terms_version() -> str:
    """取当前用户协议版本，进程内缓存 30 分钟。"""
    now = time.monotonic()
    with _LOCK:
        cached = str(_STATE.get("terms_version") or "")
        fetched_at = float(_STATE.get("terms_fetched_at") or 0.0)
        if cached and now - fetched_at < _TERMS_CACHE_TTL:
            return cached

    payload = _request("GET", "/api/legal-documents/current")
    data = _payload_data(payload)
    version = str(data.get("termsVersion") or "").strip()
    if not version:
        raise TokenRhythmAuthError("获取当前用户协议版本失败，请稍后重试")
    with _LOCK:
        _STATE["terms_version"] = version
        _STATE["terms_fetched_at"] = time.monotonic()
    return version


def sms_login(phone: str, code: str, invite_code: str | None = None) -> dict:
    """短信验证码登录 / 注册：verify → 用户协议版本 → login-or-register。

    invite_code 是可选邀请码，仅在注册（首次登录）时生效；登录已存在的账号时
    服务端会忽略它。值原样上送，不做大小写 / 格式改写。
    """
    phone = str(phone or "").strip()
    code = str(code or "").strip()
    if not code:
        raise TokenRhythmAuthError("请输入短信验证码")
    with _LOCK:
        challenge_id = str(_STATE.get("challenge_id") or "")
        challenge_phone = str(_STATE.get("challenge_phone") or "")
    if not challenge_id:
        raise TokenRhythmAuthError("请先获取短信验证码")
    if phone and challenge_phone and phone != challenge_phone:
        raise TokenRhythmAuthError("手机号与获取验证码时不一致，请重新获取验证码")

    verify_payload = _request(
        "POST",
        "/api/auth/sms/verify",
        {"challengeId": challenge_id, "code": code},
    )
    verification_token = str(
        _payload_data(verify_payload).get("verificationToken") or ""
    ).strip()
    if not verification_token:
        raise TokenRhythmAuthError("验证码校验成功，但未返回 verificationToken，请稍后重试")

    terms_version = _terms_version()
    body: dict[str, Any] = {
        "channel": "sms",
        "verificationToken": verification_token,
        "termsAccepted": True,
        "termsVersion": terms_version,
    }
    # 邀请码只有在去空白后非空时才带：官方前端同样是 `...(邀请码 ? { inviteCode } : {})`，
    # 空串会被服务端当成"提供了但无效"而报错，所以空值一律不出现该键。
    invite = str(invite_code or "").strip()
    if invite:
        body["inviteCode"] = invite
    login_payload = _request(
        "POST",
        "/api/auth/login-or-register",
        body,
    )
    user = _user_from(login_payload)
    if not user:
        raise TokenRhythmAuthError("登录成功，但服务端未返回用户信息，请稍后重试")

    with _LOCK:
        _STATE["logged_in"] = True
        _STATE["challenge_id"] = None
        _STATE["challenge_phone"] = None
    # 短信登录同样持久化会话（未注册手机号会自动建号）
    _save_session(_get_client())
    # 同上：短信登录成功后立即作废旧结论
    _invalidate_health_cache()
    return {"user": user, "ok": True}


def account_status() -> dict:
    """查询登录状态：优先用进程内会话，没有时尝试用持久化 cookie 恢复。

    应用重启后内存里没有登录标记，这里用上次保存的 cookie 调一次 /auth/me：
    成功即恢复登录态（用户无需重新登录），失败则清掉本地会话文件，避免每次
    启动都白白试探。
    """
    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
    _get_client()  # 首次调用会尝试恢复 cookie，并置 _STATE["restored"]
    with _LOCK:
        restored = bool(_STATE.get("restored"))

    if not logged_in:
        if not restored:
            return {"logged_in": False}
        try:
            payload = _request("GET", _ME_PATH)
        except TokenRhythmAuthError:
            # cookie 已失效（过期 / 被服务端注销）：清掉，下次按未登录处理
            _clear_session()
            return {"logged_in": False}
        user = _user_from(payload)
        if not user:
            _clear_session()
            return {"logged_in": False}
        with _LOCK:
            _STATE["logged_in"] = True
        return {"logged_in": True, "user": user}

    try:
        payload = _request("GET", "/api/auth/me")
    except TokenRhythmAuthError as error:
        with _LOCK:
            _STATE["logged_in"] = False
        _clear_session()
        return {"logged_in": False, "reason": str(error)}
    user = _user_from(payload)
    if not user:
        with _LOCK:
            _STATE["logged_in"] = False
        _clear_session()
        return {"logged_in": False}
    return {"logged_in": True, "user": user}


def export_session_cookies() -> list[dict[str, Any]]:
    """导出当前登录会话的 cookie，供内嵌浏览器（WebView2）复用免登录。

    只导出 tokenrhythm.studio 域名下的 cookie；未登录或无 cookie 时返回 []。
    返回值每项形如：
        {"name": str, "value": str, "domain": str, "path": str,
         "secure": bool, "http_only": bool, "expires": int}   # expires 为 0 表示会话级
    安全约束：本函数以及调用链绝不把 value 写日志/异常消息。
    """
    try:
        # _get_client() 首次调用会尝试从本地恢复持久化 cookie，所以重启后也能导出。
        client = _get_client()
        jar = getattr(getattr(client, "cookies", None), "jar", None) or ()
        exported: list[dict[str, Any]] = []
        for cookie in jar:
            try:
                name = str(getattr(cookie, "name", "") or "")
                value = str(getattr(cookie, "value", "") or "")
                if not name or not value:
                    continue
                # 保留原始 domain（含 .tokenrhythm.studio 前导点），只按归一后的 host 判定归属
                domain = str(getattr(cookie, "domain", "") or "").strip()
                host = domain.lower().lstrip(".")
                if host != "tokenrhythm.studio" and not host.endswith(
                    ".tokenrhythm.studio"
                ):
                    continue
                if cookie.is_expired():
                    continue
                try:
                    expires = int(cookie.expires or 0)
                except (TypeError, ValueError):
                    expires = 0
                checker = getattr(cookie, "has_nonstandard_attr", None)
                try:
                    http_only = bool(checker("HttpOnly")) if callable(checker) else False
                except Exception:  # noqa: BLE001 - 拿不到就当非 HttpOnly
                    http_only = False
                exported.append(
                    {
                        "name": name,
                        "value": value,
                        "domain": domain,
                        "path": str(getattr(cookie, "path", "") or "/"),
                        "secure": bool(getattr(cookie, "secure", False)),
                        "http_only": http_only,
                        "expires": expires,
                    }
                )
            except Exception:  # noqa: BLE001 - 单枚 cookie 异常不影响其余导出
                continue
        return exported
    except Exception as error:  # noqa: BLE001 - 内嵌浏览器不能因导 cookie 崩溃
        # 只记异常类型：cookie 的 value 绝不进日志
        logger.warning("基元律动会话 cookie 导出失败（%s）", type(error).__name__)
        return []


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


# ---------------------------------------------------------------------------
# 账号概览：余额 + 今日用量 + 个人信息 + 临时额度
# ---------------------------------------------------------------------------

# 四个数据源均取自官方前端页面（会话 cookie 鉴权，复用进程内同一个 Client）。
_WALLET_PATH = "/api/wallet/summary"
_USAGE_TODAY_PATH = "/api/usage/panel?range=today"
# 今日消费全口径：与官方「用量与费用 · 钱包用量」页同源的多模态总览接口。
# panel 只统计语言 Token 与图片任务，视频任务（wan3.0-video 等）完全不计入，
# 只用它会让「今日已用」漏掉视频费用；这个接口的 summary.costCny 覆盖语言 / 视频 / 图片。
_MULTIMODAL_TODAY_PATH = (
    "/api/usage/multimodal/overview"
    "?type=all&metric=cost&groupBy=model&range=today&fundingScope=WALLET"
)
_ME_PATH = "/api/auth/me"
# 临时额度 = 剩余有效期"够近"的有效额度。请求只过滤失效项（includeInactive=false），
# 刻意不传 includePermanent：服务端对"永久赠额"的判定与到期时间并不一致——充值活动赠额
# 的到期时间被写成 2099-12-31 这类远期占位值，但传 includePermanent=true / false
# 返回完全一样（实测），说明这枚参数挡不住它。所以全部取回来，本地按到期时间判定。
# pageSize 上限实测 100：传 200 直接 400 BAD_REQUEST。
_TEMPORARY_CREDITS_PATH = (
    "/api/wallet/expiring-credits?page=1&pageSize=100&includeInactive=false"
)
# 永久额度的判定阈值：到期时间晚于"现在 + 这个跨度"就等价于永久，不计入临时额度。
# 实测同一账号里，真正会过期的邀请奖励是 30 天后到期，充值活动赠额是 2099-12-31
# （70 多年后），10 年既能稳定切开两者，也不会误伤有效期较长的活动额度。
_TEMPORARY_CREDITS_HORIZON = timedelta(days=365 * 10)
# 临时额度接口单独放宽超时：服务端要汇总全部历史额度，冷启动实测 10~30 秒
# （热态 0.3 秒），20 秒的通用超时一抖动就撞线，直接把这栏判成"获取失败"。
_TEMPORARY_CREDITS_TIMEOUT = 45.0
# 服务端冷启动汇总超时时回的错误码：属于"等一下再试就好"，可以重试。
_RETRYABLE_CREDITS_CODES = frozenset({"INTERNAL_ERROR", "TIMEOUT", "SERVICE_UNAVAILABLE"})


def _is_retryable_credits_error(error: TokenRhythmAuthError) -> bool:
    """临时额度请求是否值得重试：只认传输层失败、5xx 与服务端内部错误码。

    鉴权 / 参数类业务错误（HTTP 4xx、code 非内部错误）重试只会白等，直接降级。
    """
    if error.status is None and error.code is None:
        return True  # 超时 / 连接中断
    if error.status is not None and error.status >= 500:
        return True
    return str(error.code or "").strip().upper() in _RETRYABLE_CREDITS_CODES


def _amount_text(value: Any) -> str:
    """金额字段一律按字符串原样保留，绝不做 float 换算（避免浮点精度丢失）。

    服务端契约是字符串（如 "298.09757398"）；只有字段缺失 / 类型异常时用 "0"。
    """
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, bool):
        return "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "0"


def _wallet_default() -> dict:
    """余额接口失败时的降级值：金额 "0"、币种 CNY。"""
    return {
        "available_balance_cny": "0",
        "frozen_balance_cny": "",
        "currency": "CNY",
    }


def _usage_default() -> dict:
    """今日用量接口失败时的降级值：计数 0、金额 "0"。"""
    return {
        "calls": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_cny": "0",
    }


def _temporary_credits_default() -> dict:
    """临时额度接口失败时的降级值：金额 "0"、条数 0、最早到期时间与金额为空。"""
    return {"total_cny": "0", "count": 0, "nearest_expires_at": "", "nearest_amount_cny": "0"}


def _credit_items(payload: dict) -> list[dict]:
    """解析 /api/wallet/expiring-credits 的 data。

    外层结构不固定：可能是 ``{"items": [...]}``，也可能直接是数组；两种都兜底，
    另外兼容 records / list 两种常见分页命名。结构无法识别时按"没有额度"处理。
    """
    data = payload.get("data")
    if isinstance(data, list):
        candidates: list = data
    elif isinstance(data, dict):
        candidates = []
        for key in ("items", "records", "list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
    else:
        candidates = []
    return [item for item in candidates if isinstance(item, dict)]


def _decimal_amount(value: Any) -> Decimal | None:
    """把金额字段转成 Decimal，无法解析返回 None。

    金额契约是字符串（如 remainingCny = "12.30000000"），一律走 Decimal，
    绝不经过 float：float 运算会丢掉 *.**00000001 这类低位精度。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        text = str(value).strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    return None


# 纳秒级小数秒（7~9 位）：fromisoformat 最多只吃 6 位，先截断再解析。
_ISO_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def _parse_expires_at(value: Any) -> datetime | None:
    """把 expiresAt 解析成 UTC 时间；空值 / 解析失败返回 None。

    官方控制台把 expiresAt 直接喂给 `new Date(...)`，说明它既可能是 ISO 字符串，
    也可能是秒/毫秒时间戳；两种都要认，否则整批额度会被误判成"已过期"而清零。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds <= 0:
            return None
        # 毫秒时间戳（13 位）按毫秒处理：1e12 秒对应公元 33658 年，实际不可能出现
        if seconds > 1e12:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"
    text = _ISO_FRACTION_RE.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 无时区信息时按 UTC 解释，避免与 now(timezone.utc) 比较抛 TypeError。
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_field(item: dict, keys: tuple[str, ...]) -> Any:
    """按顺序取第一个存在且非空值的字段：接口字段命名在版本间有出入，做多重兜底。"""
    for key in keys:
        if key in item:
            value = item[key]
            if value is not None and value != "":
                return value
    return None


def _fetch_temporary_credits() -> dict:
    """GET /api/wallet/expiring-credits → 有效临时额度的汇总。

    每项字段（官方前端控制台在用）：grantedCny 授予金额、remainingCny 剩余金额、
    consumedCny 已消费金额、grantedAt 授予时间、expiresAt 到期时间（ISO 字符串）、
    validityStartsAt 生效时间、source / sourceLabel 来源（「邀请奖励」「充值活动赠额」等）。

    口径：临时额度 = 会在**可预见的时间窗口内**到期的有效额度。请求只用
    includeInactive=false 过滤失效项，其余条件全部在本地按到期时间判断：
    remainingCny > 0、expiresAt 可解析、未过期，且到期时间不超过
    ``_TEMPORARY_CREDITS_HORIZON``（2099 这类远期占位值代表永久额度，不计入）。
    服务端 summary.expiringBalanceCny 不采用——它把伪永久的充值赠额一起算进去了。
    求和用 Decimal 保留原始精度，不做两位小数四舍五入。
    """
    try:
        payload = _request("GET", _TEMPORARY_CREDITS_PATH)
    except TokenRhythmAuthError as error:
        # 只重试"临时性"失败：该接口冷启动要现算全量汇总，实测首次请求会在
        # 13~30 秒后超时或直接回 INTERNAL_ERROR，紧接着的第二次只需 0.3~5 秒
        # 就能命中结果（服务端已算完并缓存）。HTTP 4xx / 鉴权类错误重试没有意义，
        # 直接抛给上层走降级。
        if not _is_retryable_credits_error(error):
            raise
        logger.warning("临时额度请求失败，重试一次：%s", error)
        payload = _request(
            "GET", _TEMPORARY_CREDITS_PATH, timeout=_TEMPORARY_CREDITS_TIMEOUT
        )
    items = _credit_items(payload)
    now = datetime.now(timezone.utc)
    # 远期占位到期时间（2099-12-31 之类）等价于永久：把它算进来会让「临时额度」虚高，
    # 「最近到期」也会被它带偏，所以统一按"到期时间是否落在窗口内"判定。
    horizon = now + _TEMPORARY_CREDITS_HORIZON
    total = Decimal("0")
    count = 0
    # 先把有效额度收集起来：算完总合计再定位"最近到期日"，那一天的额度一起加总。
    # 早先只取"最早到期的那一条"，同一到期日有多张体验卡时这个数字会在几张卡之间
    # 来回跳（用户看到 ¥6.39 / ¥15.84 来回变），改成当日合计才稳定。
    valid: list[tuple[datetime, Decimal, str]] = []
    for item in items:
        remaining = _decimal_amount(
            _first_field(item, ("remainingCny", "remainingAmountCny", "remaining", "amountCny"))
        )
        if remaining is None or not remaining.is_finite() or remaining <= 0:
            continue
        raw_expires = _first_field(item, ("expiresAt", "expireAt", "expires_at", "validUntil"))
        expires_at = _parse_expires_at(raw_expires)
        if expires_at is None or expires_at <= now:
            continue
        # 伪永久额度（充值活动赠额等）：有 expiresAt，但远到不构成"临时"
        if expires_at > horizon:
            continue
        # 原始时间字符串一起留着：重新格式化会丢掉服务端给的纳秒精度
        valid.append(
            (expires_at, remaining, str(raw_expires if raw_expires is not None else "").strip())
        )

    nearest_raw = ""
    for _expires_at, remaining, _raw in valid:
        total += remaining
        count += 1
    if valid:
        # 最早到期那一条的原始字符串：保留服务端给的精度，格式化交给展示层
        nearest_raw = min(valid, key=lambda entry: entry[0])[2]
    # 按"到期日"聚合而不是按精确时间：同一批发的体验卡到期时间只差几秒，
    # 精确比较只会命中其中一张，那个数字会随着单张卡被消费光而跳动。
    # 按日期合计表达的是"这一天会过期多少钱"，既稳定也更符合用户直觉。
    nearest_day = min((expires_at.date() for expires_at, _r, _raw in valid), default=None)
    nearest_amount = sum(
        (
            remaining
            for expires_at, remaining, _raw in valid
            if nearest_day is not None and expires_at.date() == nearest_day
        ),
        Decimal("0"),
    )
    # 服务端 summary.expiringBalanceCny / nextExpiryAt 刻意不采用：它把伪永久的
    # 充值赠额（2099 到期）也计入了"到期中余额"，正是这条口径让临时额度虚高。
    # 分页保护：临时额度均为非负金额，服务端总数多于本页返回数时合计会偏小，记一条
    # warning 便于排查（pageSize 已取到上限 100，正常账号远用不到）。
    data = payload.get("data")
    server_total = data.get("total") if isinstance(data, dict) else None
    if isinstance(server_total, int) and server_total > len(items):
        logger.warning(
            "临时额度列表未取全：服务端共 %d 条，本页返回 %d 条", server_total, len(items)
        )

    if not count and items:
        # 接口有返回但一条都没算进合计（全是伪永久 / 已过期 / 字段不匹配）：
        # 把首条的字段名记进日志（不含金额），方便按服务端真实字段补兜底，不必靠猜。
        try:
            sample_keys = sorted(str(key) for key in items[0].keys())[:24]
        except Exception:  # noqa: BLE001 - 诊断信息不能反过来影响主流程
            sample_keys = []
        logger.warning(
            "临时额度未匹配到有效项：共 %d 条，首条字段 %s", len(items), sample_keys
        )
    return {
        # format(total, "f") 走定点表示，既保留全部有效位也不会输出科学计数法。
        "total_cny": format(total, "f"),
        "count": count,
        "nearest_expires_at": nearest_raw,
        "nearest_amount_cny": format(nearest_amount, "f"),
    }


def _fetch_wallet() -> dict:
    """GET /api/wallet/summary → data.availableBalanceCny / frozenBalanceCny / currency。"""
    data = _payload_data(_request("GET", _WALLET_PATH))
    frozen = data.get("frozenBalanceCny")
    return {
        "available_balance_cny": _amount_text(data.get("availableBalanceCny")),
        # 冻结余额在契约里是可缺省字段：缺失给空串，有值则原样保留字符串。
        "frozen_balance_cny": "" if frozen is None else _amount_text(frozen),
        "currency": str(data.get("currency") or "").strip() or "CNY",
    }


def _fetch_multimodal_cost(fallback: str) -> str:
    """今日钱包消费全口径：/api/usage/multimodal/overview 的 summary.costCny。

    该接口与官方「钱包用量」页同源，统计语言调用、视频任务与图片任务；
    panel 接口不含视频，只用它会把「今日已用」算小（实测少掉整笔视频费）。
    它是附加口径：请求失败或结构异常时回落到 panel 的 costCny，
    不让一个补充接口把整块用量判成失败。
    """
    try:
        payload = _request("GET", _MULTIMODAL_TODAY_PATH)
    except TokenRhythmAuthError:
        return fallback
    summary = _payload_data(payload).get("summary")
    if not isinstance(summary, dict) or summary.get("costCny") is None:
        return fallback
    return _amount_text(summary.get("costCny"))


def _fetch_usage_today() -> dict:
    """今日用量：panel 出调用次数与 Token，多模态总览出全口径消费金额。"""
    data = _payload_data(_request("GET", _USAGE_TODAY_PATH))
    summary = data.get("summary")
    if not isinstance(summary, dict):
        raise TokenRhythmAuthError("服务端未返回今日用量汇总数据，请稍后重试")
    return {
        "calls": _as_int(summary.get("calls"), 0),
        "total_tokens": _as_int(summary.get("totalTokens"), 0),
        "input_tokens": _as_int(summary.get("inputTokens"), 0),
        "output_tokens": _as_int(summary.get("outputTokens"), 0),
        # 金额取全口径：语言 + 视频 + 图片，避免「今日已用」漏掉视频任务费用
        "cost_cny": _fetch_multimodal_cost(_amount_text(summary.get("costCny"))),
    }


def _fetch_user_profile() -> dict:
    """GET /api/auth/me → data（用户对象，字段随服务端，原样返回）。"""
    user = _user_from(_request("GET", _ME_PATH))
    if not user:
        raise TokenRhythmAuthError("服务端未返回用户信息，请稍后重试")
    return user


def account_overview() -> dict:
    """一次取回「余额 + 今日用量 + 个人信息 + 临时额度」，供前端账号概览卡片使用。

    字段来源（与官方前端一致，全部需要会话 cookie）：
    - GET /api/wallet/summary            → data.availableBalanceCny / frozenBalanceCny / currency
    - GET /api/usage/panel?range=today   → data.summary.{calls,totalTokens,inputTokens,outputTokens}
    - GET /api/usage/multimodal/overview → data.summary.costCny（今日消费全口径：语言 + 视频 + 图片）
    - GET /api/auth/me                   → data（用户对象，含 id / name / nickname / phone / email / status）
    - GET /api/wallet/expiring-credits    → data.{items|records|list}（直接返回数组也兼容）
      每项取 remainingCny / expiresAt：剩余金额 > 0、未过期，且到期时间落在
      10 年窗口内的项才算"临时额度"（2099 这类远期占位值代表永久赠额，不计入；
      服务端 summary.expiringBalanceCny 因含永久项不采用）。

    未登录（进程内无会话）时不发任何请求，返回 ``{"ok": False, "reason": "未登录"}``：
    与 account_status 风格一致——"没有会话"用返回值表达，而不是抛异常。

    四个接口互相独立：单个失败只降级对应字段（金额 "0"、计数 0），并追加
    ``"partial": True`` 与 ``"errors": {"wallet|usage|user|temporary_credits": 原因}``；
    只有 wallet / usage / user 三个都失败才抛 TokenRhythmAuthError（此时基本等同于
    会话失效 / 服务不可用），临时额度属于附加信息，失败只降级不升级成整体失败。
    金额全程保持字符串，求和用 Decimal，不做 float 换算。

    日志只记录失败分区，绝不打印余额 / 额度金额、手机号等敏感信息。
    """
    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
        has_client = _CLIENT is not None
    if not logged_in or not has_client:
        return {"ok": False, "reason": "未登录"}

    errors: dict[str, str] = {}

    try:
        wallet = _fetch_wallet()
    except TokenRhythmAuthError as error:
        errors["wallet"] = str(error)
        wallet = _wallet_default()

    try:
        usage_today = _fetch_usage_today()
    except TokenRhythmAuthError as error:
        errors["usage"] = str(error)
        usage_today = _usage_default()

    try:
        user = _fetch_user_profile()
    except TokenRhythmAuthError as error:
        errors["user"] = str(error)
        user = {}

    try:
        temporary_credits = _fetch_temporary_credits()
    except TokenRhythmAuthError as error:
        errors["temporary_credits"] = str(error)
        temporary_credits = _temporary_credits_default()

    # 临时额度是附加信息：只有 wallet / usage / user 全失败才算整体失败。
    if all(key in errors for key in ("wallet", "usage", "user")):
        raise TokenRhythmAuthError(
            "获取账号概览失败："
            + "；".join(errors[key] for key in ("wallet", "usage", "user"))
        )

    if errors:
        # 只打分区名与原因，不打任何数值 / 用户标识。
        logger.warning(
            "基元律动账号概览部分失败：%s（%s）",
            "、".join(errors),
            "；".join(errors.values()),
        )

    result: dict[str, Any] = {
        "ok": True,
        "user": user,
        "wallet": wallet,
        "usage_today": usage_today,
        "temporary_credits": temporary_credits,
    }
    if errors:
        result["partial"] = True
        result["errors"] = errors
    return result


# ---------------------------------------------------------------------------
# 账号中心只读查询（供前端「充值 / 用量明细 / 个人信息」弹窗直连官方接口）
# ---------------------------------------------------------------------------
#
# 以下四个函数都用已登录的进程内会话 cookie 直连官方后台，返回「原生弹窗」所需
# 的真实数据；它们是只读 GET，不落任何写操作。未登录时统一返回
# ``{"ok": False, "reason": "未登录"}``；官方返回错误时收敛成 ``{"ok": False, "msg": ...}``，
# 成功时 ``{"ok": True, "data": <服务端 data 原样>}``，字段由前端按需渲染并容错。

# 用法面板支持的时间窗（与服务端路由一致）
_USAGE_RANGES = frozenset(
    {"1h", "3h", "today", "3d", "7d", "14d", "30d", "all", "custom"}
)
_CALL_LOG_PAGE_PATH = "/api/call-logs/page"
_WALLET_TXN_PATH = "/api/wallet/transactions"


def _require_logged_in_client() -> httpx.Client | None:
    """返回已登录进程内 client，否则 None（调用方据此返回未登录）。"""
    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
    client = _get_client()
    with _LOCK:
        has_client = _CLIENT is not None
    if not logged_in or not has_client:
        return None
    return client


def user_profile() -> dict:
    """个人信息：GET /api/auth/me → data（用户对象，字段随服务端，原样返回）。"""
    if _require_logged_in_client() is None:
        return {"ok": False, "reason": "未登录"}
    try:
        user = _fetch_user_profile()
    except TokenRhythmAuthError as error:
        return {"ok": False, "msg": str(error)}
    return {"ok": True, "data": user}


def usage_detail(
    range: str = "7d", page: int = 1, page_size: int = 20
) -> dict:
    """用量明细面板：GET /api/usage/panel?range=&page=&pageSize=。

    range 取 ``1h / 3h / today / 3d / 7d / 14d / 30d / all / custom``，非法值
    收敛成默认 7d（与前端入口一致）。返回 data 原样（含 summary 与各维度 token /
    cost 字段以及分页后的明细 list），由前端渲染并容错缺失字段。
    """
    if _require_logged_in_client() is None:
        return {"ok": False, "reason": "未登录"}
    r = str(range or "7d").strip()
    if r not in _USAGE_RANGES:
        r = "7d"
    try:
        params = f"range={r}&page={int(page)}&pageSize={int(page_size)}"
        data = _payload_data(_request("GET", f"{_USAGE_TODAY_PATH.split('?')[0]}?{params}"))
    except TokenRhythmAuthError as error:
        return {"ok": False, "msg": str(error)}
    return {"ok": True, "data": data if isinstance(data, dict) else {}}


def wallet_transactions() -> dict:
    """钱包流水明细：无参 GET /api/wallet/transactions（官方严格 schema）。

    官方对该接口走严格 schema，任何查询参数（``range/page/pageSize/limit`` 等）
    都会直接 400「请求包含未知字段（错误码：BAD_REQUEST）」，因此这里必须发
    纯无参 GET，不拼接任何 query。
    一次返回服务端默认条数（前端不分页、不做二次拉取），data 原样透传
    （items / total 等字段随服务端），由前端容错渲染。
    """
    if _require_logged_in_client() is None:
        return {"ok": False, "reason": "未登录"}
    try:
        data = _payload_data(_request("GET", _WALLET_TXN_PATH))
    except TokenRhythmAuthError as error:
        return {"ok": False, "msg": str(error)}
    return {"ok": True, "data": data if isinstance(data, dict) else {}}


def call_logs(page: int = 1, page_size: int = 20, status: str = "") -> dict:
    """调用日志：GET /api/call-logs/page?page=&pageSize=&status=。

    status 空 = 全部；传 ``error`` 仅失败；其它字符串原样透传（数字状态码无效，
    需前端约束）。返回 data 原样（list / total / page / pageSize / nextCursor）。
    """
    if _require_logged_in_client() is None:
        return {"ok": False, "reason": "未登录"}
    try:
        params = f"page={int(page)}&pageSize={int(page_size)}"
        status = str(status or "").strip()
        if status:
            params += f"&status={status}"
        data = _payload_data(_request("GET", f"{_CALL_LOG_PAGE_PATH}?{params}"))
    except TokenRhythmAuthError as error:
        return {"ok": False, "msg": str(error)}
    return {"ok": True, "data": data if isinstance(data, dict) else {}}


# ---------------------------------------------------------------------------
# API Key 自动绑定
# ---------------------------------------------------------------------------


def _load_providers() -> list[dict]:
    try:
        import config

        data = config._load()
    except Exception as error:
        raise TokenRhythmAuthError(f"读取本地模型配置失败：{error}") from error
    providers = data.get("providers", []) if isinstance(data, dict) else []
    if not isinstance(providers, list):
        return []
    return [provider for provider in providers if isinstance(provider, dict)]


def _is_tokenrhythm_provider(provider: dict) -> bool:
    return "tokenrhythm.studio" in str(provider.get("base_url") or "").lower()


def _existing_key(provider: dict) -> str:
    raw_key = str(provider.get("api_key") or "")
    if not raw_key:
        return ""
    try:
        from core.credentials import unprotect_secret

        return unprotect_secret(raw_key).strip()
    except Exception:
        return ""


def _models_url(base_url: str) -> str:
    base = str(base_url or API_BASE_URL).strip().rstrip("/")
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


def _verify_api_key(base_url: str, api_key: str) -> bool:
    """用 GET {base_url}/models 验证 Key 是否可用；任何异常都视为不可用。"""
    url = _models_url(base_url)
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {api_key}",
    }
    try:
        from core.llm_policy import tokenrhythm_agent_headers

        headers.update(tokenrhythm_agent_headers(url))
    except Exception:
        pass
    try:
        response = _get_client().request(
            "GET", url, headers=headers, timeout=_REQUEST_TIMEOUT
        )
    except Exception:
        return False
    return int(getattr(response, "status_code", 0) or 0) == 200


def _extract_plain_key(data: Any) -> str:
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return ""
    for field in _PLAIN_KEY_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


# 创建 Key 响应里 Key id 的字段名未最终确认：按顺序兜底，取不到就留空。
_KEY_ID_FIELDS = ("id", "keyId", "apiKeyId")


def _create_api_key() -> tuple[str, str]:
    """创建一枚 API Key，返回 (明文 Key, Key id)；Key id 取不到时为空串。"""
    _require_login()
    payload = _request("POST", "/api/api-keys", {"name": API_KEY_NAME})
    data = payload.get("data")
    key = _extract_plain_key(data)
    if not key:
        raise TokenRhythmAuthError(
            "API Key 创建成功，但服务端未返回明文 Key（已尝试 key / apiKey / "
            "secret / plaintext / token 字段），请到基元律动控制台手动复制 Key 后"
            "填入模型厂商配置"
        )
    key_id = ""
    if isinstance(data, dict):
        key_id = str(_first_field(data, _KEY_ID_FIELDS) or "").strip()
    return key, key_id


def _delete_api_key(key_id: str) -> bool:
    """删掉账号下指定 id 的 API Key（换账号 / 退出登录时清理专属 Key）。

    接口：``POST /api/api-keys/{id}/delete``（成功返回 status=deleted，data 可能为空）。
    失败只记 warning 并返回 False，绝不抛异常：清理失败不能把用户卡在登录态；
    日志只出现 key_id，绝不出现 Key 明文。
    """
    target = str(key_id or "").strip()
    if not target:
        return False
    try:
        # 删除接口没有请求体，但 content-type 是 application/json 时 httpx 会拒绝
        # 空 body（"Body cannot be empty"），所以显式传一个空对象。
        _request(
            "POST",
            f"/api/api-keys/{target}/delete",
            {},
            allow_empty_data=True,
        )
    except TokenRhythmAuthError as error:
        logger.warning(
            "基元律动 API Key 删除失败（key=%s）：%s",
            target,
            error.server_message or error,
        )
        return False
    except Exception as error:  # noqa: BLE001 - 清理失败不能反向打断主流程
        logger.warning("基元律动 API Key 删除异常（key=%s）：%s", target, error)
        return False
    logger.info("基元律动 API Key 已删除（key=%s）", target)
    return True


def _account_key_ids() -> set[str]:
    """当前登录账号名下所有 API Key 的 id 集合（GET /api/api-keys）。

    用于判断绑定里的 key_id 是否还属于这个账号：不在就说明用户已在控制台删掉，
    必须重新创建。data 既可能是数组，也可能是 ``{"items": [...]}`` 这类分页对象；
    读取失败返回空集合（调用方据此走"重新创建"，不会误复用）。
    """
    try:
        payload = _request("GET", "/api/api-keys")
    except TokenRhythmAuthError as error:
        logger.warning(
            "基元律动 API Key 列表读取失败：%s", error.server_message or error
        )
        return set()
    data = payload.get("data")
    items: Any = data
    if isinstance(data, dict):
        items = []
        for key in ("items", "list", "records"):
            value = data.get(key)
            if isinstance(value, list):
                items = value
                break
    if not isinstance(items, list):
        return set()
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key_id = str(_first_field(item, _KEY_ID_FIELDS) or "").strip()
        if key_id:
            ids.add(key_id)
    return ids


# ---------------------------------------------------------------------------
# 账号 ⇄ Key 绑定关系（加密落盘：换账号时据此整体重建）
# ---------------------------------------------------------------------------


def _save_binding(data: dict) -> bool:
    """把「账号 ⇄ 厂商/Key」绑定关系加密写入本地；失败只记日志，返回是否写成功。

    只存账号 id / 昵称、厂商 id、Key id 与绑定时间，绝不存 Key 明文，也不存
    cookie / 密码。落盘走 core.credentials.protect_secret（Windows DPAPI，绑定
    当前 Windows 用户）。
    """
    try:
        payload = {
            "v": 1,
            "account_id": str(data.get("account_id") or "").strip(),
            "account_name": str(data.get("account_name") or "").strip(),
            "provider_id": str(data.get("provider_id") or "").strip(),
            "key_id": str(data.get("key_id") or "").strip(),
            "bound_at": _as_int(data.get("bound_at"), int(time.time())),
        }
        from core.credentials import protect_secret

        _BINDING_FILE.parent.mkdir(parents=True, exist_ok=True)
        _BINDING_FILE.write_text(
            protect_secret(json.dumps(payload, ensure_ascii=False)), encoding="utf-8"
        )
        try:
            os.chmod(_BINDING_FILE, 0o600)
        except OSError:
            pass
        return True
    except Exception:  # noqa: BLE001 - 落盘失败不能影响绑定主流程
        logger.warning("基元律动账号绑定信息保存失败（不影响本次绑定）", exc_info=True)
        return False


def _load_binding() -> dict | None:
    """读取本地绑定信息；不存在 / 解密失败 / 结构异常一律按"无绑定"处理。"""
    try:
        if not _BINDING_FILE.exists():
            return None
        from core.credentials import unprotect_secret

        raw = unprotect_secret(_BINDING_FILE.read_text(encoding="utf-8").strip())
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - 读不出来就当作没绑定过
        logger.warning("基元律动账号绑定信息读取失败，按未绑定处理", exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    account_id = str(data.get("account_id") or "").strip()
    if not account_id:
        # 没有账号 id 就无法判断"是否换了账号"，等同无绑定。
        return None
    return {
        "v": _as_int(data.get("v"), 1),
        "account_id": account_id,
        "account_name": str(data.get("account_name") or "").strip(),
        "provider_id": str(data.get("provider_id") or "").strip(),
        "key_id": str(data.get("key_id") or "").strip(),
        "bound_at": _as_int(data.get("bound_at"), 0),
    }


def _clear_binding() -> None:
    """删除本地绑定信息（换账号时调用）；失败只记日志。"""
    try:
        _BINDING_FILE.unlink(missing_ok=True)
    except OSError:
        logger.warning("基元律动账号绑定信息清理失败", exc_info=True)


def _remember_binding(
    account: dict, provider_id: str, key_id: str = "", bound_at: int = 0
) -> None:
    """记录最新绑定：账号 id / 昵称 + 厂商 id + Key id。

    顺手给这条厂商打上托管标记：新建 / 覆盖 / 复用都会走到这里，标记只写这一处，
    之后退出登录、换账号清理时就能精确区分"自己创建的"与"用户手动配置的"。
    """
    _save_binding(
        {
            "account_id": account.get("id"),
            "account_name": account.get("name"),
            "provider_id": provider_id,
            "key_id": key_id,
            "bound_at": bound_at or int(time.time()),
        }
    )
    _mark_provider_managed(provider_id)


def _current_account() -> dict:
    """取当前登录账号：GET /api/auth/me → data，返回 {"id", "name"}。

    未登录直接抛中文异常；会话已被服务端判定失效时同样提示重新登录。
    name 取不到时依次用 nickname / phoneMasked / emailMasked 兜底，仍为空返回空串
    （调用方回退成默认厂商名）。账号 id / 昵称绝不写日志。
    """
    with _LOCK:
        logged_in = bool(_STATE.get("logged_in"))
    if not logged_in:
        raise TokenRhythmAuthError("请先登录基元律动账号")
    try:
        payload = _request("GET", _ME_PATH)
    except TokenRhythmAuthError as error:
        raise TokenRhythmAuthError(
            "请先登录基元律动账号（登录态已失效，请重新登录后再试）",
            code=error.code,
            server_message=error.server_message,
            status=error.status,
        ) from error
    data = _user_from(payload)
    account_id = str(_first_field(data, ("id", "userId", "uid")) or "").strip()
    if not account_id:
        raise TokenRhythmAuthError("未获取到当前基元律动账号标识，请重新登录后再试")
    account_name = str(
        _first_field(data, ("name", "nickname", "phoneMasked", "emailMasked")) or ""
    ).strip()
    return {"id": account_id, "name": account_name}


def _short_account_label(account_name: str, limit: int = 6) -> str:
    """账号昵称定长截断：超过 limit 个码点就砍掉并补省略号。

    昵称会落到厂商名、浮层标题这类窄位置（实测有「基元律动请送我token我…」
    这种超长昵称），原样写进去会把厂商名撑到 20 多字、标题被 CSS 硬切。
    按码点切分，避免把 emoji / 代理对劈成乱码。
    """
    text = str(account_name or "").strip()
    keep = max(1, int(limit))
    chars = list(text)
    if len(chars) <= keep:
        return text
    return "".join(chars[:keep]) + "…"


def _bound_provider_name(account_name: str) -> str:
    """厂商名称带上账号昵称（昵称超长只取前 6 个字符），与手动配置的「基元律动」区分开。

    昵称取不到时回落到「基元律动（账号）」而不是裸「基元律动」：后者正是用户
    自己手填 Key 时最常用的名字，重名会让两条配置在模型列表里无法分辨。
    """
    nickname = _short_account_label(account_name) or "账号"
    return f"{DEFAULT_PROVIDER_NAME}（{nickname}）"


def _rename_provider(provider: dict, provider_name: str) -> None:
    """把已有厂商改成带账号昵称的名字（模型配置里能一眼看出属于哪个账号）。

    名称已一致或密钥解密失败时直接返回：改名只是展示优化，绝不能影响绑定结果。
    """
    current = str(provider.get("name") or "").strip()
    target_name = str(provider_name or "").strip()
    if not target_name or current == target_name:
        return
    try:
        from core import model_manager
        from core.credentials import unprotect_secret

        plain_key = unprotect_secret(str(provider.get("api_key") or "")).strip()
        if not plain_key:
            return
        model_manager.update_provider(
            str(provider.get("id") or ""),
            name=target_name,
            base_url=str(provider.get("base_url") or API_BASE_URL).strip() or API_BASE_URL,
            api_key=plain_key,
            protocol=str(provider.get("protocol") or "openai").strip() or "openai",
        )
    except Exception:  # noqa: BLE001 - 只是改名，失败不影响绑定
        logger.warning("基元律动厂商重命名失败（不影响绑定）", exc_info=True)


def _find_provider(providers: list[dict], provider_id: str) -> dict | None:
    target = str(provider_id or "").strip()
    if not target:
        return None
    for provider in providers:
        if str(provider.get("id") or "").strip() == target:
            return provider
    return None


def _provider_key_usable(provider: dict) -> bool:
    """厂商里存的 Key 是否仍被服务端认可（GET {base_url}/models）。"""
    key = _existing_key(provider)
    if not key:
        return False
    return _verify_api_key(str(provider.get("base_url") or API_BASE_URL), key)


def _delete_provider_quietly(provider_id: str) -> None:
    """删除厂商；旧账号的条目删不掉也不能阻断新账号绑定，只记 warning。"""
    target = str(provider_id or "").strip()
    if not target:
        return
    try:
        from core import model_manager

        model_manager.delete_provider(target)
    except Exception as error:  # noqa: BLE001 - 删除失败退化为"忽略旧条目"
        logger.warning("基元律动旧账号厂商删除失败（provider=%s）：%s", target, error)


def _bind_result(
    account: dict,
    *,
    created: bool,
    provider_id: Any,
    reason: str,
    provider_name: str = "",
    model_added: bool = False,
    account_switched: bool = False,
    key_created: bool | None = None,
    old_key_deleted: bool = False,
) -> dict:
    """拼装返回值：原字段语义不变，补充 key_created / old_key_deleted。

    created 一直表示"本次是否新建了 Key"；key_created 省略时跟随 created，
    显式传 False 用于"复用已有 Key"的分支，便于上层区分。

    model_added 是历史字段：自首次绑定不再写占位模型后恒为 False，仅为兼容
    老调用方保留键位，新代码不要据此判断模型是否可用（看 sync_models 的结果）。
    """
    created_key = bool(created) if key_created is None else bool(key_created)
    result: dict[str, Any] = {
        "created": bool(created),
        "key_created": created_key,
        "old_key_deleted": bool(old_key_deleted),
        "provider_id": str(provider_id or "").strip(),
        "model_added": bool(model_added),
        "reason": reason,
        "account_id": str(account.get("id") or ""),
        "account_name": str(account.get("name") or ""),
        "provider_name": str(provider_name or "").strip() or DEFAULT_PROVIDER_NAME,
    }
    if account_switched:
        result["account_switched"] = True
    return result


def _reuse_bound_key(account: dict, binding: dict, provider_name: str) -> dict | None:
    """同账号重复登录：Key 仍属于账号且本地可用时复用，否则返回 None（要求新建）。

    两个条件缺一不可：
    1. binding.key_id 出现在 GET /api/api-keys 返回的 id 列表里（用户没在控制台删掉），
       这是"这枚 Key 属于当前账号"的权威依据；
    2. 本地绑定厂商还在，且里面存的 Key 能通过 GET {base_url}/models 校验，保证本地
       配置没被改坏。

    Key 被删 / 厂商丢失 / Key 失效一律返回 None，交给调用方新建一枚覆盖，
    绝不退化成"沿用本地其它可用 Key"。
    """
    key_id = str(binding.get("key_id") or "").strip()
    if not key_id:
        return None
    if key_id not in _account_key_ids():
        logger.info("基元律动绑定 Key 已不在账号下（key=%s），将重新创建", key_id)
        return None
    provider = _find_provider(_load_providers(), binding.get("provider_id") or "")
    if provider is None or not _provider_key_usable(provider):
        logger.info("基元律动绑定厂商缺失或 Key 已失效（key=%s），将重新创建", key_id)
        return None
    provider_id = str(provider.get("id") or "").strip()
    _rename_provider(provider, provider_name)
    _remember_binding(
        account, provider_id, key_id, _as_int(binding.get("bound_at"), 0)
    )
    return _bind_result(
        account,
        created=False,
        provider_id=provider_id,
        reason="复用当前账号已绑定的 Key",
        provider_name=provider_name,
        key_created=False,
    )


def _mark_provider_managed(provider_id: str) -> None:
    """给自动创建/绑定的厂商打托管标记（幂等）。

    标记是"这条是本程序建的"的持久化凭据：即使本地绑定文件丢了，也能识别出来，
    不会和用户手动配置的条目混为一谈。打标记失败不影响绑定主流程。
    """
    target = str(provider_id or "").strip()
    if not target:
        return
    try:
        from core.model_manager import load_config

        provider = next(
            (
                item
                for item in load_config().get("providers", []) or []
                if str(item.get("id") or "").strip() == target
            ),
            None,
        )
        # 厂商不存在或已有标记都不写盘：复用登录每次都走这里，别白白重写配置
        if provider is None:
            return
        if str(provider.get(MANAGED_PROVIDER_FLAG) or "").strip() == MANAGED_PROVIDER_VALUE:
            return

        import config as app_config

        def _mutate(data: dict) -> None:
            for item in data.get("providers", []) or []:
                if str(item.get("id") or "").strip() != target:
                    continue
                item[MANAGED_PROVIDER_FLAG] = MANAGED_PROVIDER_VALUE
                return

        app_config.update_config(_mutate)
    except Exception:  # noqa: BLE001 - 标记失败不能影响登录绑定
        logger.warning("基元律动厂商托管标记写入失败（id=%s）", target, exc_info=True)


def _is_managed_provider(provider: dict, bound_ids: set[str]) -> bool:
    """这条厂商是否由本程序创建：带托管标记，或正是本地绑定记录里的那条。"""
    if str(provider.get(MANAGED_PROVIDER_FLAG) or "").strip() == MANAGED_PROVIDER_VALUE:
        return True
    provider_id = str(provider.get("id") or "").strip()
    return bool(provider_id) and provider_id in bound_ids


def _delete_tr_providers(keep_id: str = "") -> int:
    """删除本程序自动创建/托管的基元律动厂商（keep_id 那条保留），返回删除数量。

    只认两种身份：provider 上的托管标记，或本地绑定信息记录的 provider_id。
    用户自己填 Key 配置的同域厂商两个都不满足——退出登录、换账号都不会动它，
    名字里带「基元律动」也一样（用户手动配置的条目必须原样保留）。
    """
    removed = 0
    try:
        from core import model_manager
        from core.model_manager import load_config

        bound_provider_id = str((_load_binding() or {}).get("provider_id") or "").strip()
        bound_ids = {bound_provider_id} if bound_provider_id else set()
        providers = load_config().get("providers", []) or []
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            if "tokenrhythm.studio" not in str(provider.get("base_url") or "").lower():
                continue
            provider_id = str(provider.get("id") or "").strip()
            if not provider_id or (keep_id and provider_id == keep_id):
                continue
            if not _is_managed_provider(provider, bound_ids):
                continue
            try:
                model_manager.delete_provider(provider_id)
                removed += 1
            except Exception:  # noqa: BLE001 - 单条删不掉不影响其它条目
                logger.warning("删除基元律动厂商失败（id=%s）", provider_id, exc_info=True)
    except Exception:  # noqa: BLE001 - 清理失败不能反过来打断退出/绑定
        logger.warning("清理基元律动厂商失败", exc_info=True)
    return removed


def ensure_bound() -> dict:
    """确保当前登录账号拥有一枚专属 API Key，并绑定到本地模型厂商。

    产品语义（登录即新建专属 Key，绝不复用用户手动填过的 Key）：
    1. 同账号、Key 仍在账号下且本地可用：复用这一枚，不重复创建（created=False）。
    2. 同账号但 Key 已被删 / 厂商丢失 / Key 失效：新建一枚 Key，覆盖绑定过的厂商。
    3. 换账号：先删旧账号的 Key（POST /api/api-keys/{id}/delete），再删旧厂商、
       清绑定，然后新建 Key + 新建厂商（名称「基元律动（昵称）」）。
    4. 无绑定 / 绑定损坏：一定新建 Key + 新建厂商 + 写绑定（不沿用本地任何已有 Key）。

    未登录抛 TokenRhythmAuthError（请先登录基元律动账号）。
    返回：created / key_created（是否新建了 Key）、old_key_deleted（换账号时旧 Key
    是否删除成功）、provider_id、reason、account_id、account_name、provider_name；
    account_switched 只在本次发生账号切换时出现。model_added 为历史字段（恒 False）：
    本函数不再写入任何占位模型，新建厂商后由调用方用 sync_models() 填充真实目录。
    日志只记 key_id / provider_id，绝不打印 Key 明文。
    """
    account = _current_account()
    provider_name = _bound_provider_name(account["name"])

    binding = _load_binding() or {}
    bound_account_id = str(binding.get("account_id") or "").strip()
    account_switched = bool(bound_account_id) and bound_account_id != account["id"]
    old_key_deleted = False

    if account_switched:
        # 旧账号的 Key 属于旧账号，不能留下：先删 Key → 再删旧厂商 → 最后清绑定
        old_key_id = str(binding.get("key_id") or "").strip()
        dropped_provider_id = str(binding.get("provider_id") or "").strip()
        old_key_deleted = _delete_api_key(old_key_id)
        # 旧账号可能留下多条历史厂商，一并清掉，保证配置里只剩新账号那一条
        _delete_tr_providers()
        _delete_provider_quietly(dropped_provider_id)
        _clear_binding()
        binding = {}
    elif binding:
        reused = _reuse_bound_key(account, binding, provider_name)
        if reused is not None:
            return reused

    # 走到这里必须新建一枚专属 Key：不复用本地任何已有 Key（含用户手动填的）。
    # 建之前先清掉本地所有基元律动厂商，保证配置里始终只有一条「基元律动（账号）」。
    _delete_tr_providers()
    new_key, key_id = _create_api_key()

    # 目标厂商：同账号优先覆盖绑定过的那个；换账号 / 无绑定时一律新建厂商
    target: dict | None = None
    if not account_switched and binding:
        target = _find_provider(_load_providers(), binding.get("provider_id") or "")

    if target is not None:
        provider_id = str(target.get("id") or "").strip()
        base_url = str(target.get("base_url") or API_BASE_URL).strip() or API_BASE_URL
        try:
            from core import model_manager

            model_manager.update_provider(
                provider_id,
                name=provider_name,
                base_url=base_url,
                api_key=new_key,
                protocol="openai",
            )
        except Exception as error:
            raise TokenRhythmAuthError(
                f"API Key 已创建，但写入模型厂商配置失败：{error}"
            ) from error
        _remember_binding(account, provider_id, key_id)
        return _bind_result(
            account,
            created=True,
            provider_id=provider_id,
            reason="已自动创建并绑定 Key",
            provider_name=provider_name,
            account_switched=account_switched,
            key_created=True,
            old_key_deleted=old_key_deleted,
        )

    try:
        from core import model_manager

        provider_id = str(
            model_manager.add_provider(provider_name, API_BASE_URL, new_key, "openai")
            or ""
        ).strip()
    except Exception as error:
        raise TokenRhythmAuthError(
            f"API Key 已创建，但新增模型厂商失败：{error}"
        ) from error

    # 刻意不写任何"占位模型"：厂商建好后由调用方紧接着执行 sync_models()，
    # 按上游 GET /api/models 的真实条目（含真实 contextWindow）填充目录。
    # 历史实现此处硬编码 deepseek-v4.1-flash + 128000，而该裸名模型已在上游
    # 下线，只会给用户留下一条调不通、上下文还比真实值小一截的脏模型。
    _remember_binding(account, provider_id, key_id)
    return _bind_result(
        account,
        created=True,
        provider_id=provider_id,
        reason="已自动创建并绑定 Key",
        provider_name=provider_name,
        account_switched=account_switched,
        key_created=True,
        old_key_deleted=old_key_deleted,
    )


# ---------------------------------------------------------------------------
# 模型目录同步：把基元律动提供的全部可用语言模型写入本地厂商
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 模型目录同步：把基元律动提供的全部可用语言模型写入本地厂商
# ---------------------------------------------------------------------------

# 公开模型目录（无需登录）：GET BASE_URL + /api/models
# 注意它和 Key 校验用的 {base_url}/v1/models 不是同一个端点。
MODELS_PATH = "/api/models"
# 只同步对话模型、只同步在线模型；图片 / 视频 / embedding / 下线一律跳过。
_SYNC_MODEL_TYPE = "chat"
_SYNC_MODEL_STATUS = "online"


def _flag(mapping: Any, key: str) -> bool | None:
    """读取能力布尔字段：有明确结论返回 True/False，缺失或无法判定返回 None。

    返回 None 让调用方继续走兜底链（capabilities → 平铺字段 → 默认值），
    避免把"字段缺失"误判成"明确不支持"。
    """
    if not isinstance(mapping, dict) or key not in mapping:
        return None
    value = mapping[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes"):
            return True
        if text in ("false", "0", "no"):
            return False
    return None


def _positive_int(value: Any, default: int | None) -> int | None:
    """把字段转成正整数；缺失 / 非数字 / <=0 时返回默认值。"""
    if value is None or isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _openai_available(model: dict) -> bool:
    """模型是否走 OpenAI 协议：capabilities.openai 优先，再看 protocolCapabilities。"""
    flag = _flag(model.get("capabilities"), "openai")
    if flag is not None:
        return flag
    protocol_capabilities = model.get("protocolCapabilities")
    if isinstance(protocol_capabilities, dict):
        flag = _flag(protocol_capabilities.get("openai"), "available")
        if flag is not None:
            return flag
    return False


def _model_items(payload: dict) -> list[dict]:
    """取模型数组：data 是数组直接用；是对象时兜底 items / models / list。"""
    data = payload.get("data")
    candidates: Any = data
    if isinstance(data, dict):
        candidates = []
        for key in ("items", "models", "list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates = value
                break
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict)]


def _syncable_models(payload: dict) -> list[dict]:
    """过滤出可同步的模型：type=chat、status=online、OpenAI 协议可用、有 id。"""
    usable: list[dict] = []
    for model in _model_items(payload):
        if str(model.get("type") or "").strip() != _SYNC_MODEL_TYPE:
            continue
        if str(model.get("status") or "").strip() != _SYNC_MODEL_STATUS:
            continue
        if not _openai_available(model):
            continue
        if not str(model.get("id") or "").strip():
            continue
        usable.append(model)
    return usable


def _sync_model_fields(model: dict) -> dict[str, Any]:
    """把接口模型对象映射成本地模型条目的字段。

    - 名称：用 id（就是请求时要带的模型名），不用展示名 name；
    - 上下文窗口：contextWindow，缺失 / 非正数用 DEFAULT_CONTEXT_WINDOW 兜底；
    - 最大输出：maxOutputTokens，缺失则不传（交给模型管理器的内置默认）；
    - 视觉：capabilities.vision → supportsVision → 默认 False；
    - 工具调用：capabilities.tools → supportsTools → 默认 True
      （产品要求"尽量全部支持工具调用"，但字段明确为 false 时尊重服务端结论）。
    """
    capabilities = model.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, dict) else {}

    vision = _flag(capabilities, "vision")
    if vision is None:
        vision = _flag(model, "supportsVision")
    if vision is None:
        vision = False

    tools = _flag(capabilities, "tools")
    if tools is None:
        tools = _flag(model, "supportsTools")
    if tools is None:
        tools = True

    return {
        "name": str(model.get("id") or "").strip(),
        "context_window": _positive_int(
            model.get("contextWindow"), DEFAULT_CONTEXT_WINDOW
        ),
        "max_tokens": _positive_int(model.get("maxOutputTokens"), None),
        "supports_vision": vision,
        "supports_function_calling": tools,
    }


def _resolve_sync_provider(provider_id: str | None) -> tuple[str, str]:
    """确定要写入的厂商：入参 → 绑定信息 → 本地 base_url 含 tokenrhythm.studio。"""
    providers = _load_providers()
    explicit = str(provider_id or "").strip()
    if explicit:
        provider = _find_provider(providers, explicit)
        if provider is not None:
            name = str(provider.get("name") or "").strip() or DEFAULT_PROVIDER_NAME
            return explicit, name
    else:
        binding = _load_binding() or {}
        bound_id = str(binding.get("provider_id") or "").strip()
        if bound_id:
            provider = _find_provider(providers, bound_id)
            if provider is not None:
                name = str(provider.get("name") or "").strip() or DEFAULT_PROVIDER_NAME
                return bound_id, name

    # 兜底：优先挑本程序托管的厂商，别把模型塞进用户手动配置的那条里
    fallback: tuple[str, str] | None = None
    for provider in providers:
        if not _is_tokenrhythm_provider(provider):
            continue
        candidate_id = str(provider.get("id") or "").strip()
        if not candidate_id:
            continue
        name = str(provider.get("name") or "").strip() or DEFAULT_PROVIDER_NAME
        if str(provider.get(MANAGED_PROVIDER_FLAG) or "").strip() == MANAGED_PROVIDER_VALUE:
            return candidate_id, name
        if fallback is None:
            fallback = (candidate_id, name)
    if fallback is not None:
        return fallback
    raise TokenRhythmAuthError("未找到基元律动厂商配置，请先登录")


def _existing_model_names(provider_id: str) -> set[str]:
    """本地厂商里已有的模型名（用于幂等：同名跳过，不重复添加）。"""
    provider = _find_provider(_load_providers(), provider_id)
    if provider is None:
        return set()
    names: set[str] = set()
    for model in provider.get("models") or []:
        if isinstance(model, dict):
            name = str(model.get("name") or "").strip()
            if name:
                names.add(name)
    return names


def sync_models(provider_id: str | None = None) -> dict:
    """把基元律动提供的全部可用语言模型按 OpenAI 协议写入本地厂商。

    流程：
    1. 确定目标厂商：provider_id 入参 → 本地绑定信息 → base_url 含
       tokenrhythm.studio 的厂商；都没有抛「未找到基元律动厂商配置，请先登录」。
    2. GET /api/models（公开接口，无需登录），只保留 type=chat、status=online、
       capabilities.openai 为 true（或 protocolCapabilities.openai.available
       为 true）的模型，其余（图片 / 视频 / embedding / 下线）一律跳过。
    3. 逐个调用 model_manager.add_model_to_provider 落盘；已有同名模型跳过。
       单个模型添加失败只记入 failed，不影响其余模型。
    4. 返回 {"ok", "provider_id", "provider_name", "total", "added",
       "skipped", "failed"}；日志只记数量汇总，绝不打印 Key。
    """
    target_id, provider_name = _resolve_sync_provider(provider_id)
    payload = _request("GET", MODELS_PATH)
    models = _syncable_models(payload)
    existing = _existing_model_names(target_id)

    added = 0
    skipped = 0
    failed: list[dict[str, str]] = []
    for model in models:
        fields = _sync_model_fields(model)
        name = fields["name"]
        if not name:
            continue
        if name in existing:
            skipped += 1
            continue
        kwargs: dict[str, Any] = {
            "supports_vision": fields["supports_vision"],
            "supports_function_calling": fields["supports_function_calling"],
        }
        if fields["max_tokens"] is not None:
            kwargs["max_tokens"] = fields["max_tokens"]
        try:
            from core import model_manager

            model_manager.add_model_to_provider(
                target_id, name, fields["context_window"], **kwargs
            )
        except Exception as error:  # noqa: BLE001 - 单个模型失败不整体报错
            failed.append({"model": name, "error": str(error)})
            continue
        existing.add(name)
        added += 1

    logger.info(
        "基元律动模型同步完成（provider=%s）：可用 %d，新增 %d，跳过 %d，失败 %d",
        target_id,
        len(models),
        added,
        skipped,
        len(failed),
    )
    return {
        "ok": True,
        "provider_id": target_id,
        "provider_name": provider_name,
        "total": len(models),
        "added": added,
        "skipped": skipped,
        "failed": failed,
    }


def _server_profile() -> bool:
    return os.environ.get("YCODE_TOOL_PROFILE", "").strip().lower() == "server"


def _server_disabled_payload() -> dict:
    return {
        "ok": False,
        "logged_in": False,
        "reason": "disabled",
        "msg": "server 档已禁用基元律动",
    }


def _disable_on_server(fn):
    """server 档不登录、不联网、不读绑定。其它档位原样调用。"""

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if _server_profile():
            if fn.__name__ == "binding_active":
                return False
            return _server_disabled_payload()
        return fn(*args, **kwargs)

    return wrapped


for _server_gated_name in (
    "password_login",
    "send_sms_code",
    "sms_login",
    "account_status",
    "export_session_cookies",
    "session_health",
    "binding_active",
    "account_overview",
    "user_profile",
    "usage_detail",
    "wallet_transactions",
    "call_logs",
    "logout",
    "ensure_bound",
    "sync_models",
):
    globals()[_server_gated_name] = _disable_on_server(globals()[_server_gated_name])


__all__ = [
    "TokenRhythmAuthError",
    "password_login",
    "send_sms_code",
    "sms_login",
    "account_status",
    "export_session_cookies",
    "session_health",
    "binding_active",
    "account_overview",
    "user_profile",
    "usage_detail",
    "wallet_transactions",
    "call_logs",
    "logout",
    "ensure_bound",
    "sync_models",
]
