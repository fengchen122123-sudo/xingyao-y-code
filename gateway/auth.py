"""账号口令、JWT 与失效矩阵。口令哈希用 argon2id。"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ACCESS_SECONDS = 30 * 60
REFRESH_SECONDS = 30 * 24 * 60 * 60
_HASHER = PasswordHasher()


@dataclass
class Principal:
    account_id: str
    username: str
    role: str
    auth_epoch: int
    jti: str


def hash_password(password: str) -> str:
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_tokens(database, account, secret: str, now: int | None = None) -> dict:
    now = int(time.time() if now is None else now)
    jti = secrets.token_hex(16)
    access = jwt.encode(
        {
            "sub": account["id"],
            "username": account["username"],
            "role": account["role"],
            "auth_epoch": int(account["auth_epoch"]),
            "jti": jti,
            "typ": "access",
            "iat": now,
            "exp": now + ACCESS_SECONDS,
        },
        secret,
        algorithm="HS256",
    )
    refresh = secrets.token_urlsafe(32)
    database.execute(
        """
        INSERT INTO refresh_tokens(token_hash, account_id, expires_at, revoked)
        VALUES (?, ?, ?, 0)
        """,
        (token_hash(refresh), account["id"], now + REFRESH_SECONDS),
    )
    database.commit()
    return {
        "access_token": access,
        "refresh_token": refresh,
        "role": account["role"],
    }


def read_access(database, token: str, secret: str, now: int | None = None) -> Principal | None:
    now = int(time.time() if now is None else now)
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != "access":
        return None
    account = database.execute(
        "SELECT * FROM accounts WHERE id = ?",
        (payload.get("sub"),),
    ).fetchone()
    if account is None or int(account["disabled"]):
        return None
    token_epoch = payload.get("auth_epoch")
    if token_epoch is None or int(account["auth_epoch"]) != int(token_epoch):
        return None
    if account["role"] != payload.get("role"):
        return None
    denied = database.execute(
        "SELECT 1 FROM access_denylist WHERE jti = ? AND expires_at > ?",
        (payload.get("jti"), now),
    ).fetchone()
    if denied:
        return None
    return Principal(
        account_id=account["id"],
        username=account["username"],
        role=account["role"],
        auth_epoch=int(account["auth_epoch"]),
        jti=str(payload.get("jti") or ""),
    )


def logout(database, principal: Principal, refresh_token: str, now: int | None = None) -> None:
    """登出只废这一枚 refresh 和这一枚 access，不增加 auth_epoch。"""
    now = int(time.time() if now is None else now)
    database.execute(
        "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ? AND account_id = ?",
        (token_hash(refresh_token), principal.account_id),
    )
    database.execute(
        """
        INSERT OR REPLACE INTO access_denylist(jti, account_id, expires_at)
        VALUES (?, ?, ?)
        """,
        (principal.jti, principal.account_id, now + ACCESS_SECONDS),
    )
    database.commit()


def bump_epoch(database, account_id: str, *, role: str | None = None, disabled: int | None = None) -> int:
    """改密、禁用、降权：epoch + 1，并废掉该账号全部 refresh。"""
    account = database.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if account is None:
        raise KeyError(account_id)
    epoch = int(account["auth_epoch"]) + 1
    database.execute(
        """
        UPDATE accounts
        SET auth_epoch = ?,
            role = COALESCE(?, role),
            disabled = COALESCE(?, disabled)
        WHERE id = ?
        """,
        (epoch, role, disabled, account_id),
    )
    database.execute(
        "UPDATE refresh_tokens SET revoked = 1 WHERE account_id = ?",
        (account_id,),
    )
    database.commit()
    return epoch


def create_account(database, username: str, password: str, role: str = "user") -> str:
    account_id = str(uuid.uuid4())
    database.execute(
        """
        INSERT INTO accounts(id, username, password_hash, role, disabled, auth_epoch, quota_json, created_at)
        VALUES (?, ?, ?, ?, 0, 0, '{}', ?)
        """,
        (account_id, username, hash_password(password), role, int(time.time())),
    )
    database.commit()
    return account_id


def init_admin(database, username: str, password: str) -> str:
    from gateway.db import account_count

    if account_count(database):
        raise RuntimeError("库里已经有账号，拒绝再次初始化管理员")
    if not username or not password:
        raise RuntimeError("用户名和口令都不能为空")
    return create_account(database, username, password, role="admin")
