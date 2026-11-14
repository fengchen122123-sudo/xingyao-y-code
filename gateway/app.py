"""网关 HTTP。WebSocket 透传在连接建立后单独拨上游。"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from gateway.auth import (
    Principal,
    bump_epoch,
    create_account,
    init_admin,
    issue_tokens,
    logout,
    read_access,
    token_hash,
)
from gateway.db import connect
from gateway.relay import RelayRegistry


class LoginBody(BaseModel):
    username: str
    password: str


class RefreshBody(BaseModel):
    refresh_token: str


class LogoutBody(BaseModel):
    refresh_token: str


class AccountBody(BaseModel):
    username: str
    password: str
    role: str = "user"


class PasswordBody(BaseModel):
    password: str


class DeviceBody(BaseModel):
    name: str


class PatchAccount(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    password: str | None = None


def create_app(database_path: Path, jwt_secret: str) -> FastAPI:
    app = FastAPI()
    database = connect(database_path)
    app.state.database = database
    app.state.secret = jwt_secret
    app.state.relay = RelayRegistry()
    app.state.sockets = {}

    def principal(authorization: str = Header(default="")) -> Principal:
        if not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401)
        found = read_access(database, authorization[7:].strip(), jwt_secret)
        if found is None:
            raise HTTPException(status_code=401)
        return found

    def admin(user: Principal = Depends(principal)) -> Principal:
        if user.role != "admin":
            raise HTTPException(status_code=403)
        return user

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/auth/login")
    def login(body: LoginBody):
        from gateway.auth import verify_password

        account = database.execute(
            "SELECT * FROM accounts WHERE username = ?",
            (body.username,),
        ).fetchone()
        if account is None or int(account["disabled"]) or not verify_password(account["password_hash"], body.password):
            raise HTTPException(status_code=401)
        return issue_tokens(database, account, jwt_secret)

    @app.post("/api/auth/refresh")
    def refresh(body: RefreshBody):
        row = database.execute(
            "SELECT * FROM refresh_tokens WHERE token_hash = ?",
            (token_hash(body.refresh_token),),
        ).fetchone()
        now = int(time.time())
        if row is None or int(row["revoked"]) or int(row["expires_at"]) <= now:
            raise HTTPException(status_code=401)
        database.execute(
            "UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?",
            (row["token_hash"],),
        )
        account = database.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
        if account is None or int(account["disabled"]):
            database.commit()
            raise HTTPException(status_code=401)
        return issue_tokens(database, account, jwt_secret)

    @app.post("/api/auth/logout")
    def do_logout(body: LogoutBody, user: Principal = Depends(principal)):
        logout(database, user, body.refresh_token)
        app.state.sockets.pop(user.jti, None)
        return {"ok": True}

    @app.get("/api/devices")
    def devices(user: Principal = Depends(principal)):
        rows = database.execute(
            "SELECT id, name, revoked, last_seen FROM devices WHERE account_id = ?",
            (user.account_id,),
        ).fetchall()
        return {
            "devices": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "revoked": bool(row["revoked"]),
                    "online": bool(app.state.relay.devices.get(row["id"], {}).get("online")),
                    "last_seen": row["last_seen"],
                }
                for row in rows
            ]
        }

    @app.get("/api/admin/accounts")
    def list_accounts(_admin: Principal = Depends(admin)):
        rows = database.execute(
            "SELECT id, username, role, disabled, auth_epoch, created_at FROM accounts"
        ).fetchall()
        return {"accounts": [dict(row) for row in rows]}

    @app.post("/api/admin/accounts")
    def add_account(body: AccountBody, _admin: Principal = Depends(admin)):
        account_id = create_account(database, body.username, body.password, body.role)
        return {"id": account_id}

    @app.patch("/api/admin/accounts/{account_id}")
    def patch_account(account_id: str, body: PatchAccount, _admin: Principal = Depends(admin)):
        if body.disabled:
            bump_epoch(database, account_id, disabled=1)
        elif body.role:
            bump_epoch(database, account_id, role=body.role)
        elif body.password:
            from gateway.auth import hash_password

            database.execute(
                "UPDATE accounts SET password_hash = ? WHERE id = ?",
                (hash_password(body.password), account_id),
            )
            database.commit()
            bump_epoch(database, account_id)
        return {"ok": True}

    @app.post("/api/admin/devices")
    def add_device(body: DeviceBody, account_id: str, user: Principal = Depends(admin)):
        device_id, token = app.state.relay.create_device(database, account_id, body.name)
        owner = database.execute("SELECT account_id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if owner["account_id"] != account_id:
            raise HTTPException(status_code=404)
        return {"id": device_id, "token": token, "account_id": user.account_id}

    @app.delete("/api/admin/devices/{device_id}")
    def delete_device(device_id: str, _admin: Principal = Depends(admin)):
        app.state.relay.revoke(database, device_id)
        return {"ok": True}

    @app.get("/api/admin/usage")
    def usage(_admin: Principal = Depends(admin)):
        rows = database.execute(
            """
            SELECT account_id, day, model, input_tokens, output_tokens, turns
            FROM usage_daily ORDER BY day, account_id, model
            """
        ).fetchall()
        return {"usage": [dict(row) for row in rows]}

    app.state.init_admin = lambda username, password: init_admin(database, username, password)
    return app


def app_from_env():
    from gateway.config import GatewayConfig

    config = GatewayConfig.from_env()
    if not config.jwt_secret:
        raise RuntimeError("缺少 XINGYAO_JWT_SECRET")
    return create_app(config.database, config.jwt_secret)
