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
    return {"token": token, "expires_at": now + REMOTE_ACCESS_SECONDS}


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
        "SELECT revoked FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None or int(row["revoked"]):
        return None
    return SimpleNamespace(jti=str(payload.get("jti") or ""), device_id=device_id)
