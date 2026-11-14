"""网关 HTTP。WebSocket 透传在连接建立后单独拨上游。"""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Response, WebSocket
from pydantic import BaseModel

from gateway.sessions import SocketTable
from gateway.ws_agent import open_agent, preview_request

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
    app.state.sockets = SocketTable()
    app.state.workers = None
    app.state.dial_worker = lambda _account_id: None
    app.state.fetch_preview = lambda request: (200, b"", "image/png")

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
            app.state.sockets.close_account(account_id)
            if app.state.workers is not None:
                app.state.workers.stop(account_id)
            app.state.relay.devices = {
                key: value
                for key, value in app.state.relay.devices.items()
                if value.get("account_id") != account_id
            }
        elif body.role:
            bump_epoch(database, account_id, role=body.role)
            app.state.sockets.close_account(account_id)
        elif body.password:
            from gateway.auth import hash_password

            database.execute(
                "UPDATE accounts SET password_hash = ? WHERE id = ?",
                (hash_password(body.password), account_id),
            )
            database.commit()
            bump_epoch(database, account_id)
            app.state.sockets.close_account(account_id)
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

    @app.get("/api/admin/devices")
    def list_devices(_admin: Principal = Depends(admin)):
        rows = database.execute(
            "SELECT id, account_id, name, revoked, last_seen FROM devices"
        ).fetchall()
        return {"devices": [dict(row) for row in rows]}

    @app.get("/api/admin/workers")
    def list_workers(_admin: Principal = Depends(admin)):
        rows = database.execute(
            "SELECT account_id, container_name, desired_state, last_activity FROM workers"
        ).fetchall()
        return {"workers": [dict(row) for row in rows]}

    @app.post("/api/admin/workers/{account_id}")
    def control_worker(account_id: str, action: str, _admin: Principal = Depends(admin)):
        if app.state.workers is None:
            raise HTTPException(status_code=503, detail="没有 Docker")
        if action == "stop":
            app.state.workers.stop(account_id)
        elif action == "start":
            app.state.workers.ensure_running(account_id, health=lambda _url: {"ok": True, "boot_id": "x"})
        else:
            raise HTTPException(status_code=400)
        return {"ok": True}

    @app.get("/computer-preview/{preview_token}/{run_id}/{name}")
    def computer_preview(
        preview_token: str,
        run_id: str,
        name: str,
        user: Principal = Depends(principal),
    ):
        row = database.execute(
            "SELECT worker_token FROM workers WHERE account_id = ?",
            (user.account_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404)
        public_path = f"/computer-preview/{preview_token}/{run_id}/{name}"
        request = preview_request(user.account_id, public_path, row["worker_token"])
        status, body, content_type = app.state.fetch_preview(request)
        if status != 200:
            raise HTTPException(status_code=status)
        return Response(content=body, media_type=content_type)

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            raw = await websocket.receive_text()
            first = json.loads(raw)
        except Exception:
            await websocket.close(code=4401)
            return
        principal_now = read_access(database, str(first.get("jwt") or ""), jwt_secret)
        if principal_now is None:
            await websocket.close(code=4401)
            return
        opened = open_agent(
            {
                "account_id": principal_now.account_id,
                "last_seq": first.get("last_seq"),
                "last_boot_id": first.get("last_boot_id"),
            },
            app.state.dial_worker,
        )
        for frame in opened["client_frames"]:
            await websocket.send_text(json.dumps(frame, ensure_ascii=False))
        if opened["upstream_frame"] is not None:
            link = opened["link"]
            link["sent"].append(opened["upstream_frame"])
        app.state.sockets.add(principal_now.account_id, principal_now.jti, websocket.close)
        await websocket.close()

    @app.websocket("/relay/device")
    async def relay_device(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        result = app.state.relay.register(
            database,
            str(first.get("device_id") or ""),
            str(first.get("token") or ""),
            str(first.get("boot_id") or ""),
        )
        if not result.get("ok"):
            await websocket.close(code=4401)
            return
        await websocket.send_text(json.dumps(result["registered"], ensure_ascii=False))
        device_id = str(first.get("device_id") or "")
        while True:
            try:
                raw = await websocket.receive_text()
            except Exception:
                return
            frame = json.loads(raw)
            if frame.get("type") == "ping":
                app.state.relay.touch(device_id, time.time())
                await websocket.send_text(json.dumps({"type": "pong"}))

    @app.websocket("/relay/client")
    async def relay_client(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        user = read_access(database, str(first.get("jwt") or ""), jwt_secret)
        device_id = str(first.get("device_id") or "")
        row = database.execute("SELECT account_id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if user is None or row is None or row["account_id"] != user.account_id:
            await websocket.close(code=4401)
            return
        attached = app.state.relay.attach_app(device_id, user.jti)
        if not attached.get("ok"):
            await websocket.close(code=4401)
            return
        plan = app.state.relay.frames_for(device_id, first.get("last_seq"), first.get("last_boot_id"))
        await websocket.send_text(json.dumps(plan, ensure_ascii=False))
        await websocket.close()

    @app.websocket("/ws/admin")
    async def admin_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        user = read_access(database, str(first.get("jwt") or ""), jwt_secret)
        if user is None or user.role != "admin":
            await websocket.close(code=4401)
            return
        await websocket.send_text(json.dumps({"type": "ready"}))
        await websocket.close()

    @app.websocket("/ws/devices")
    async def device_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        user = read_access(database, str(first.get("jwt") or ""), jwt_secret)
        if user is None:
            await websocket.close(code=4401)
            return
        await websocket.send_text(json.dumps({"type": "ready", "data": {"account_id": user.account_id}}))
        await websocket.close()

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
