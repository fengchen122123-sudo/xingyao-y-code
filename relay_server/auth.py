from __future__ import annotations

import secrets
import time
from types import SimpleNamespace

import jwt

# 旧客户端换票不传 ttl 时的缺省有效期（23 小时，略短于一天）
REMOTE_ACCESS_SECONDS = 23 * 60 * 60
# 显式 ttl 的下限：1 分钟，避免签出一落地就过期的废票
REMOTE_ACCESS_MIN_SECONDS = 60
# 显式 ttl 的上限：30 天，避免客户端把票签成无限期
REMOTE_ACCESS_MAX_SECONDS = 30 * 24 * 60 * 60
# ttl=0 视为“永久”：用 10 年表达，绝不签不带 exp 的令牌
REMOTE_ACCESS_FOREVER_SECONDS = 10 * 365 * 24 * 60 * 60


def resolve_remote_ttl(ttl_seconds=None) -> int:
    """把请求里指定的有效期归一成实际签进 exp 的秒数。

    None / 非数字 / 负数 → 缺省 23 小时（旧客户端行为不变）；
    0 → 永久（用 10 年表达，仍带 exp）；其他正数夹在 [1 分钟, 30 天] 之间。
    """
    try:
        value = int(ttl_seconds) if ttl_seconds is not None else None
    except (TypeError, ValueError):
        return REMOTE_ACCESS_SECONDS
    if value is None or value < 0:
        return REMOTE_ACCESS_SECONDS
    if value == 0:
        return REMOTE_ACCESS_FOREVER_SECONDS
    return max(REMOTE_ACCESS_MIN_SECONDS, min(REMOTE_ACCESS_MAX_SECONDS, value))


def issue_remote_token(
    secret: str,
    device_id: str,
    now: int | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """给远端页签发只读设备入口令牌；只能用于 /relay/client，不能用于管理接口。

    ttl_seconds 由调用方（换票请求）指定，经 resolve_remote_ttl 归一；不传即沿用
    旧行为（23 小时）。返回值结构保持不变，向后兼容。
    """
    now = int(time.time() if now is None else now)
    ttl = resolve_remote_ttl(ttl_seconds)
    jti = secrets.token_hex(16)
    token = jwt.encode(
        {
            "jti": jti,
            "typ": "remote",
            "device_id": device_id,
            "iat": now,
            "exp": now + ttl,
        },
        secret,
        algorithm="HS256",
    )
    return {"token": token, "jti": jti, "expires_at": now + ttl}


def bind_active_token(database, device_id: str, jti: str) -> None:
    """把这票登记为设备当前唯一有效的远端票：旧 jti 在写入瞬间就作废。"""
    database.execute("UPDATE devices SET active_jti = ? WHERE id = ?", (jti, device_id))
    database.commit()


def issue_device_token(
    database,
    secret: str,
    device_id: str,
    now: int | None = None,
    ttl_seconds: int | None = None,
) -> dict:
    """签发并绑定为设备唯一有效票。调用方随后应踢掉该设备已建立的远端连接。

    ttl_seconds 原样透传给 issue_remote_token，单活绑定语义不受有效期影响。
    """
    payload = issue_remote_token(secret, device_id, now=now, ttl_seconds=ttl_seconds)
    bind_active_token(database, device_id, payload["jti"])
    return payload


def read_remote_token(
    database,
    token: str,
    secret: str,
    device_id: str,
    now: int | None = None,
):
    """校验远端页 token：签名、类型、设备绑定、过期、设备 revoked 缺一不可。

    通过返回带 .jti / .device_id 的对象；任何一项不过返回 None。
    """
    now = int(time.time() if now is None else now)
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "remote" or str(payload.get("device_id") or "") != device_id:
        return None
    if int(payload.get("exp") or 0) <= now:
        return None
    row = database.execute(
        "SELECT revoked, active_jti FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None or int(row["revoked"]):
        return None
    jti = str(payload.get("jti") or "")
    if not jti:
        return None
    active = str(row["active_jti"] or "")
    if not active:
        # 旧版本签发的票（库里还没有独占标记）：首次使用就地绑定，之后任何新签发
        # 都会让它失效；用这种方式平滑升级，不一次性废掉手机里正在用的链接。
        bind_active_token(database, device_id, jti)
    elif not secrets.compare_digest(active, jti):
        # 刷新已签出新票：旧链接（包括泄露出去的那张）到此为止
        return None
    return SimpleNamespace(jti=jti, device_id=device_id)
