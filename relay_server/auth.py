from __future__ import annotations

import secrets
import time
from types import SimpleNamespace

import jwt

REMOTE_ACCESS_SECONDS = 23 * 60 * 60


def issue_remote_token(secret: str, device_id: str, now: int | None = None) -> dict:
    """给远端页签发只读设备入口令牌；只能用于 /relay/client，不能用于管理接口。"""
    now = int(time.time() if now is None else now)
    jti = secrets.token_hex(16)
    token = jwt.encode(
        {
            "jti": jti,
            "typ": "remote",
            "device_id": device_id,
            "iat": now,
            "exp": now + REMOTE_ACCESS_SECONDS,
        },
        secret,
        algorithm="HS256",
    )
    return {"token": token, "jti": jti, "expires_at": now + REMOTE_ACCESS_SECONDS}


def bind_active_token(database, device_id: str, jti: str) -> None:
    """把这票登记为设备当前唯一有效的远端票：旧 jti 在写入瞬间就作废。"""
    database.execute("UPDATE devices SET active_jti = ? WHERE id = ?", (jti, device_id))
    database.commit()


def issue_device_token(
    database, secret: str, device_id: str, now: int | None = None
) -> dict:
    """签发并绑定为设备唯一有效票。调用方随后应踢掉该设备已建立的远端连接。"""
    payload = issue_remote_token(secret, device_id, now=now)
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
