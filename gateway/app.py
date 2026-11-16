"""网关 HTTP。WebSocket 透传在连接建立后单独拨上游。"""

from __future__ import annotations

import asyncio
import asyncio
import json
import os
import secrets
import threading
import time
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from gateway.workers import container_name
from gateway.sessions import SessionReaper, SocketTable, close_with_4401
from gateway.ws_agent import open_agent, preview_request

from gateway.auth import (
    Principal,
    bump_epoch,
    create_account,
    init_admin,
    issue_remote_token,
    read_remote_access,
    issue_tokens,
    logout,
    read_access,
    still_valid,
    token_hash,
)
from gateway.db import connect
from gateway.platform_models import PlatformModelStore
from gateway.relay import OUTBOUND_LIMIT, DeviceSweeper, RelayRegistry, hash_token


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
    quota: dict | None = None


class PasswordBody(BaseModel):
    password: str


class DeviceBody(BaseModel):
    name: str


class DeviceTokenBody(BaseModel):
    """桌面用设备令牌自证换票：桌面只持有长期 device token，没有账号口令。"""

    device_id: str
    device_token: str


class PatchAccount(BaseModel):
    disabled: bool | None = None
    role: str | None = None
    password: str | None = None
    quota: dict | None = None


class ModelBody(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None
    model_id: str
    model_name: str
    litellm_model: str


class ModelsBody(BaseModel):
    providers: list[ModelBody]
    active_model_id: str = ""


def create_app(database_path: Path, jwt_secret: str) -> FastAPI:
    app = FastAPI()
    database = connect(database_path)
    app.state.database = database
    app.state.secret = jwt_secret
    app.state.relay = RelayRegistry()
    app.state.sockets = SocketTable()
    app.state.workers = None
    app.state.model_store = None
    app.state.health = lambda _url: {"ok": True, "boot_id": "x"}
    app.state.dial_worker = lambda _account_id: None
    app.state.fetch_preview = lambda request: (200, b"", "image/png")
    app.state.accounts_root = Path(database_path).parent / "accounts"
    app.state.keyring_password_file = None

    def socket_valid(item: dict) -> bool:
        return still_valid(
            database,
            item["account_id"],
            item["jti"],
            item["auth_epoch"],
            item["role"],
        )

    app.state.reaper = SessionReaper(app.state.sockets, socket_valid)

    def publish_relay(account_id: str, device_id: str, frame: dict, app_key: str | None = None) -> None:
        app.state.relay.publish(account_id, device_id, frame, app_key=app_key)

    def _announce_offline(account_id: str | None, device_id: str) -> None:
        """踢线/吊销/轮换后主动播一次离线，App 与设备页都不用等 45 秒扫描。"""
        if not account_id:
            return
        publish_relay(
            account_id,
            device_id,
            {"type": "device_status", "data": {"device_id": device_id, "online": False}},
        )

    # 45 秒无心跳标离线（心跳 15s + 容差）；桌面侧断开立刻由路由自己标，这里兜底。
    app.state.device_sweeper = DeviceSweeper(app.state.relay, publish_relay)

    def start_reaper() -> None:
        stop = asyncio.Event()
        app.state.reaper_stop = stop
        app.state.reaper_task = asyncio.create_task(app.state.reaper.run(stop))
        sweep_stop = asyncio.Event()
        app.state.sweep_stop = sweep_stop
        app.state.sweep_task = asyncio.create_task(app.state.device_sweeper.run(sweep_stop))

    async def stop_reaper() -> None:
        stop = getattr(app.state, "reaper_stop", None)
        if stop is not None:
            stop.set()
        task = getattr(app.state, "reaper_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        sweep_stop = getattr(app.state, "sweep_stop", None)
        if sweep_stop is not None:
            sweep_stop.set()
        sweep_task = getattr(app.state, "sweep_task", None)
        if sweep_task is not None:
            sweep_task.cancel()
            try:
                await sweep_task
            except (asyncio.CancelledError, Exception):
                pass

    app.router.add_event_handler("startup", start_reaper)
    app.router.add_event_handler("shutdown", stop_reaper)

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
    async def do_logout(body: LogoutBody, user: Principal = Depends(principal)):
        logout(database, user, body.refresh_token)
        await app.state.sockets.pop(user.jti, None)
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
            "SELECT id, username, role, disabled, auth_epoch, quota_json, created_at FROM accounts"
        ).fetchall()
        return {"accounts": [_account_row(row) for row in rows]}

    @app.post("/api/admin/accounts")
    def add_account(body: AccountBody, _admin: Principal = Depends(admin)):
        account_id = create_account(database, body.username, body.password, body.role)
        if body.quota is not None:
            database.execute(
                "UPDATE accounts SET quota_json = ? WHERE id = ?",
                (json.dumps(body.quota, ensure_ascii=False), account_id),
            )
            database.commit()
        return {"id": account_id}

    @app.patch("/api/admin/accounts/{account_id}")
    async def patch_account(account_id: str, body: PatchAccount, _admin: Principal = Depends(admin)):
        account = database.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if account is None:
            raise HTTPException(status_code=404)
        if body.quota is not None:
            database.execute(
                "UPDATE accounts SET quota_json = ? WHERE id = ?",
                (json.dumps(body.quota, ensure_ascii=False), account_id),
            )
            database.commit()
        if body.disabled is True:
            bump_epoch(database, account_id, disabled=1)
            await app.state.sockets.close_account(account_id)
            if app.state.workers is not None:
                app.state.workers.stop(account_id)
            for device_id, value in list(app.state.relay.devices.items()):
                if value.get("account_id") == account_id:
                    # 矩阵只要求「立即 close 4401」，不吊销设备 token。
                    app.state.relay.disconnect(device_id)
                    _announce_offline(account_id, device_id)
        elif body.disabled is False:
            database.execute("UPDATE accounts SET disabled = 0 WHERE id = ?", (account_id,))
            database.commit()
        if body.role and body.role != account["role"]:
            bump_epoch(database, account_id, role=body.role)
            await app.state.sockets.close_account(account_id)
        if body.password:
            from gateway.auth import hash_password

            database.execute(
                "UPDATE accounts SET password_hash = ? WHERE id = ?",
                (hash_password(body.password), account_id),
            )
            database.commit()
            bump_epoch(database, account_id)
            await app.state.sockets.close_account(account_id)
        return {"ok": True}

    @app.post("/api/remote/device/{device_id}/token")
    def remote_device_token(device_id: str, user: Principal = Depends(principal)):
        device = database.execute(
            "SELECT * FROM devices WHERE id = ? AND account_id = ? AND revoked = 0",
            (device_id, user.account_id),
        ).fetchone()
        if device is None:
            raise HTTPException(status_code=404, detail="设备不存在")
        account = database.execute("SELECT * FROM accounts WHERE id = ?", (user.account_id,)).fetchone()
        return issue_remote_token(database, account, jwt_secret, device_id)

    # 换票限流窗口：设备令牌是长期密钥，换票接口不能是无成本的可爆破面。
    # 网关按单进程 uvicorn 部署，进程内计数够用；重启清零只会放宽一个窗口。
    _TICKET_WINDOW_SECONDS = 600.0
    _TICKET_MAX_PER_WINDOW = 6
    _ticket_hits: dict[str, list[float]] = {}
    _ticket_lock = threading.Lock()

    def _ticket_allowed(device_id: str, now: float) -> bool:
        """同一设备 10 分钟内最多换 6 次票：正常用法是「用户打开二维码」级别。"""
        with _ticket_lock:
            hits = [stamp for stamp in _ticket_hits.get(device_id, []) if now - stamp < _TICKET_WINDOW_SECONDS]
            if len(hits) >= _TICKET_MAX_PER_WINDOW:
                _ticket_hits[device_id] = hits
                return False
            hits.append(now)
            _ticket_hits[device_id] = hits
            return True

    @app.post("/api/relay/device-token")
    def relay_device_token(body: DeviceTokenBody):
        """桌面用设备令牌自证，换取只读 remote JWT，供公网二维码扫码入站。

        设备令牌换票等于把它能做的事扩大到「读这台设备的远端页」，所以三处收口：
        只签给未吊销且账号未禁用的设备、票里只绑自己这台 device_id、
        同一设备 10 分钟内最多换 6 次。
        """
        if not _ticket_allowed(body.device_id, time.time()):
            raise HTTPException(status_code=429, detail="换票过于频繁")
        row = database.execute("SELECT * FROM devices WHERE id = ?", (body.device_id,)).fetchone()
        # 令牌比对走常量时间：不能按响应耗时逐字节反推 token_hash
        if row is None or int(row["revoked"]) or not secrets.compare_digest(
            str(row["token_hash"]), hash_token(body.device_token)
        ):
            raise HTTPException(status_code=401, detail="设备令牌无效")
        account = database.execute("SELECT * FROM accounts WHERE id = ?", (row["account_id"],)).fetchone()
        if account is None or int(account["disabled"]):
            raise HTTPException(status_code=403, detail="账号已停用")
        return issue_remote_token(database, account, jwt_secret, body.device_id)

    @app.post("/api/admin/devices")
    def add_device(body: DeviceBody, account_id: str, user: Principal = Depends(admin)):
        device_id, token = app.state.relay.create_device(database, account_id, body.name)
        owner = database.execute("SELECT account_id FROM devices WHERE id = ?", (device_id,)).fetchone()
        if owner["account_id"] != account_id:
            raise HTTPException(status_code=404)
        return {"id": device_id, "token": token, "account_id": user.account_id}

    @app.delete("/api/admin/devices/{device_id}")
    def delete_device(device_id: str, _admin: Principal = Depends(admin)):
        account_id = (app.state.relay.devices.get(device_id) or {}).get("account_id")
        app.state.relay.revoke(database, device_id)
        _announce_offline(account_id, device_id)
        return {"ok": True}

    @app.get("/api/admin/devices")
    def list_devices(_admin: Principal = Depends(admin)):
        rows = database.execute(
            "SELECT id, account_id, name, revoked, last_seen FROM devices"
        ).fetchall()
        return {
            "devices": [
                dict(dict(row), online=bool(app.state.relay.devices.get(row["id"], {}).get("online")))
                for row in rows
            ]
        }

    @app.get("/api/admin/workers")
    def list_workers(_admin: Principal = Depends(admin)):
        worker_rows = database.execute(
            """
            SELECT account_id, container_name, desired_state, last_activity, started_at,
                   last_boot_id, version, active_turn, last_error, updated_at
            FROM workers
            """
        ).fetchall()
        now = time.time()
        workers = []
        for row in worker_rows:
            item = dict(row)
            item["active_turn"] = bool(item.get("active_turn"))
            started_at = item.get("started_at")
            item["uptime_seconds"] = int(max(0.0, now - float(started_at))) if started_at else 0
            if app.state.workers is not None:
                item["container_state"] = app.state.workers.container_state(row["account_id"])
            else:
                item["container_state"] = "unknown"
            workers.append(item)
        return {"workers": workers}

    @app.get("/api/admin/backup")
    def backup_summary(_admin: Principal = Depends(admin)):
        from gateway.backup import entry, restore_semantics

        listed = database.execute("PRAGMA database_list").fetchone()
        gateway_db = Path(listed["file"] or "")
        accounts_root = app.state.accounts_root or gateway_db.parent / "accounts"
        return {
            "entry": entry(database, accounts_root, gateway_db, app.state.keyring_password_file),
            "restore": restore_semantics(),
        }

    def model_store() -> PlatformModelStore:
        if app.state.model_store is None:
            raise HTTPException(status_code=503, detail="系统凭据存储不可用")
        return app.state.model_store

    @app.get("/api/admin/models")
    def list_models(_admin: Principal = Depends(admin)):
        return model_store().public()

    @app.put("/api/admin/models")
    def put_models(body: ModelsBody, _admin: Principal = Depends(admin)):
        store = model_store()
        if not body.providers:
            # 空列表表示管理员清空模型：工人 config.json 也要跟着清掉旧 providers。
            store.save([], "")
            _stamp_workers()
            return {"ok": True, "active_model_id": ""}
        existing = {
            str(provider.get("id")): provider
            for provider in (store.references().get("providers") or [])
            if isinstance(provider, dict)
        }
        providers = []
        seen = set()
        for item in body.providers:
            provider_id = item.name.strip().lower().replace(" ", "-") or "provider"
            seen.add(provider_id)
            old = existing.get(provider_id) or {}
            # 没带 api_key 表示沿用旧引用：钥匙串里的 Key 不能因为改个模型名就丢。
            api_key = item.api_key if item.api_key else str(old.get("api_key") or "")
            providers.append({
                "id": provider_id,
                "name": item.name,
                "base_url": item.base_url,
                "api_key": api_key,
                "protocol": "openai",
                "models": [{
                    "id": item.model_id,
                    "name": item.model_name,
                    "litellm_model": item.litellm_model,
                    "context_window": 1000000,
                    "supports_vision": False,
                }],
            })
        for provider_id, old in existing.items():
            if provider_id not in seen:
                providers.append(old)
        active = body.active_model_id or (providers[0]["models"][0]["id"] if providers else "")
        store.save(providers, active)
        _stamp_workers()
        return {"ok": True, "active_model_id": active}

    def _stamp_workers() -> None:
        if app.state.workers is None:
            return
        for row in database.execute("SELECT id FROM accounts").fetchall():
            app.state.workers.ensure_token(row["id"])

    @app.post("/api/admin/devices/{device_id}/rotate")
    def rotate_device(device_id: str, _admin: Principal = Depends(admin)):
        account_id = (app.state.relay.devices.get(device_id) or {}).get("account_id")
        token = app.state.relay.rotate(database, device_id)
        if token is None:
            raise HTTPException(status_code=404)
        # 轮换等于把旧桌面踢下线：App 侧也要立刻看到离线，别等 45 秒扫描。
        _announce_offline(account_id, device_id)
        return {"id": device_id, "token": token}

    @app.post("/api/admin/workers/{account_id}")
    def control_worker(account_id: str, action: str, _admin: Principal = Depends(admin)):
        if app.state.workers is None:
            raise HTTPException(status_code=503, detail="没有 Docker")
        if action == "stop":
            app.state.workers.stop(account_id)
        elif action == "start":
            result = app.state.workers.ensure_running(account_id, health=app.state.health)
            if not result.get("ok"):
                raise HTTPException(status_code=503, detail="worker_lost")
        elif action == "restart":
            result = app.state.workers.restart(account_id, health=app.state.health)
            if not result.get("ok"):
                raise HTTPException(status_code=503, detail="worker_lost")
        else:
            raise HTTPException(status_code=400)
        return {"ok": True, "desired_state": app.state.workers.desired_state(account_id)}

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

    @app.get("/novnc/{account_id}/{asset_path:path}")
    async def novnc_asset(account_id: str, asset_path: str, request: Request, token: str = ""):
        """把 noVNC 的静态资源从 worker 的 6080 转发出来。"""
        import httpx

        principal_now = read_access(database, token, jwt_secret)
        if principal_now is None or principal_now.account_id != account_id:
            raise HTTPException(status_code=401, detail="unauthorized")
        target = f"http://{container_name(account_id)}:6080/{asset_path}"
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                upstream = await client.get(target, params=request.query_params)
        except Exception:
            raise HTTPException(status_code=503, detail="novnc_unreachable")
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

    @app.websocket("/novnc/{account_id}/websockify")
    async def novnc_socket(websocket: WebSocket, account_id: str, token: str = ""):
        """VNC 的 WebSocket 隧道：浏览器 <-> gateway <-> worker:6080。"""
        import websockets

        principal_now = read_access(database, token, jwt_secret)
        if principal_now is None or principal_now.account_id != account_id:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        endpoint = f"ws://{container_name(account_id)}:6080/websockify"
        try:
            async with websockets.connect(endpoint, max_size=None) as upstream:
                async def browser_to_worker():
                    while True:
                        frame = await websocket.receive_bytes()
                        await upstream.send(frame)

                async def worker_to_browser():
                    async for message in upstream:
                        await websocket.send_bytes(message)

                await asyncio.gather(browser_to_worker(), worker_to_browser())
        except Exception:
            # 任一端断开都静默收尾：noVNC 自己会重连
            pass
        finally:
            try:
                await websocket.close()
            except Exception:
                pass

    @app.websocket("/ws/agent")
    async def agent_socket(websocket: WebSocket):
        await websocket.accept()
        try:
            raw = await websocket.receive_text()
            first = json.loads(raw)
        except Exception:
            await websocket.close(code=4401)
            return
        presented = str(first.get("jwt") or "")
        device_id = str(first.get("device_id") or "")
        principal_now = read_access(database, presented, jwt_secret)
        if principal_now is None and device_id:
            principal_now = read_remote_access(database, presented, jwt_secret, device_id)
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
        if opened["upstream_frame"] is None:
            app.state.sockets.add(
                principal_now.account_id,
                principal_now.jti,
                close_with_4401(websocket),
                auth_epoch=principal_now.auth_epoch,
                role=principal_now.role,
            )
            await websocket.close()
            return
        link = opened["link"]
        link["sent"].append(opened["upstream_frame"])
        app.state.sockets.add(
            principal_now.account_id,
            principal_now.jti,
            close_with_4401(websocket),
            auth_epoch=principal_now.auth_epoch,
            role=principal_now.role,
        )
        await _bridge(websocket, link)

    @app.websocket("/relay/device")
    async def relay_device(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        device_id = str(first.get("device_id") or "")
        result = app.state.relay.register(
            database,
            device_id,
            str(first.get("token") or ""),
            str(first.get("boot_id") or ""),
        )
        if not result.get("ok"):
            await websocket.close(code=4401)
            return
        kicked = result.get("kicked")
        if callable(kicked):
            kicked()
        device_id = str(first.get("device_id") or "")
        account_id = app.state.relay.devices[device_id]["account_id"]
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def closer() -> None:
            loop.call_soon_threadsafe(stop.set)

        # 出站 500 条有界队列：满则丢帧并要求 App 整态自愈，绝不阻塞事件循环。
        outgoing: asyncio.Queue = asyncio.Queue(maxsize=OUTBOUND_LIMIT)

        def send_raw(text: str) -> bool:
            def put() -> None:
                try:
                    outgoing.put_nowait(text)
                except asyncio.QueueFull:
                    lost = app.state.relay.enqueue(device_id)
                    if lost is not None:
                        publish_relay(account_id, device_id, lost)

            loop.call_soon_threadsafe(put)
            return True

        device = app.state.relay.devices[device_id]
        device["connection"] = closer
        device["send"] = send_raw
        await websocket.send_text(json.dumps(result["registered"], ensure_ascii=False))
        if result.get("reset"):
            # GUI 重启换了 boot_id：先让已连上的 App 重置水位，再播在线。
            publish_relay(account_id, device_id, result["reset"])
        if result.get("online"):
            publish_relay(account_id, device_id, result["online"])
        try:
            while not stop.is_set():
                receive = asyncio.create_task(websocket.receive_text())
                kicked_wait = asyncio.create_task(stop.wait())
                from_phone = asyncio.create_task(outgoing.get())
                done, pending = await asyncio.wait(
                    {receive, kicked_wait, from_phone},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if stop.is_set():
                    await websocket.close(code=4401)
                    return
                if from_phone in done:
                    await websocket.send_text(from_phone.result())
                    continue
                try:
                    raw = receive.result()
                except Exception:
                    return
                frame = json.loads(raw)
                if frame.get("type") == "ping":
                    app.state.relay.touch(device_id, time.time())
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    continue
                if frame.get("type") == "relay_lost":
                    # 桌面出站队列满丢过帧：这段补不回来，只能让 App 整态自愈。
                    lost = app.state.relay.enqueue(device_id)
                    if lost is not None:
                        publish_relay(account_id, device_id, lost)
                    continue
                seq = frame.get("seq")
                if isinstance(seq, int):
                    app.state.relay.remember(device_id, seq, raw)
                route = app.state.relay.resolve_rpc(device_id, frame.get("id"))
                if route is not None:
                    # RPC 回程定向回原 App，并把网关加的前缀还原成 App 自己的 id。
                    app_key, original = route
                    reply = dict(frame)
                    reply["id"] = original
                    publish_relay(account_id, device_id, reply, app_key=app_key)
                    continue
                publish_relay(account_id, device_id, frame)
        finally:
            current = app.state.relay.devices.get(device_id) or {}
            if current.get("send") is send_raw:
                # 只有这条连接还占着设备时才标离线：新一代连接已接手就不能打掉它。
                offline = app.state.relay.mark_offline(device_id)
                if offline is not None:
                    publish_relay(account_id, device_id, offline)

    @app.websocket("/relay/client")
    async def relay_client(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        device_id = str(first.get("device_id") or "")
        user = read_access(database, str(first.get("jwt") or ""), jwt_secret)
        if user is None and device_id:
            user = read_remote_access(database, str(first.get("jwt") or ""), jwt_secret, device_id)
        row = database.execute(
            "SELECT account_id, revoked FROM devices WHERE id = ?", (device_id,)
        ).fetchone()
        if user is None or row is None or row["account_id"] != user.account_id or int(row["revoked"]):
            await websocket.close(code=4401)
            return
        attached = app.state.relay.attach_app(device_id, user.jti)
        if not attached.get("ok"):
            await websocket.close(code=4401)
            return
        app_key = attached["key"]
        try:
            plan = app.state.relay.frames_for(device_id, first.get("last_seq"), first.get("last_boot_id"))
            await websocket.send_text(json.dumps(plan, ensure_ascii=False))

            def forward(raw: str) -> bool | dict:
                send = (app.state.relay.devices.get(device_id) or {}).get("send")
                if not callable(send):
                    # 在途 RPC 由网关立即报错并带上 id，App 不用等到 30 秒超时。
                    return _offline_reply(raw)
                frame = _namespace_rpc(app.state.relay, device_id, app_key, raw)
                send(json.dumps(frame, ensure_ascii=False) if frame is not None else raw)
                return True

            await _fanout(
                websocket,
                app.state.relay,
                user.account_id,
                device_id,
                on_text=forward,
                app_key=app_key,
            )
        finally:
            # 断开/登出都要释放名额，否则名额泄漏会把第 5 个 App 误判成超限。
            app.state.relay.detach_app(device_id, app_key)

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
        await _fanout(websocket, app.state.relay, user.account_id, None)

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
        await _fanout(websocket, app.state.relay, user.account_id, None)

    @app.get("/api/admin/usage")
    def usage(_admin: Principal = Depends(admin)):
        rows = database.execute(
            """
            SELECT d.account_id, a.username, d.day, d.model, d.input_tokens, d.output_tokens, d.turns
            FROM usage_daily AS d LEFT JOIN accounts AS a ON a.id = d.account_id
            ORDER BY d.day, d.account_id, d.model
            """
        ).fetchall()
        return {"usage": [dict(row) for row in rows]}

    app.state.init_admin = lambda username, password: init_admin(database, username, password)
    return app


def _account_row(row) -> dict:
    item = dict(row)
    try:
        quota = json.loads(item.pop("quota_json", "{}") or "{}")
    except json.JSONDecodeError:
        quota = {}
    item["quota"] = quota if isinstance(quota, dict) else {}
    return item


async def _bridge(websocket: WebSocket, link: dict) -> None:
    """浏览器帧原样交给上游。上游帧送回浏览器，其中不带工人令牌。"""
    downstream = link.get("downstream")

    async def from_browser() -> None:
        while True:
            link["sent"].append(await websocket.receive_text())

    async def from_worker() -> None:
        if downstream is None:
            await asyncio.Future()
            return
        while True:
            item = await asyncio.to_thread(downstream.get)
            if item is None:
                return
            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            if link.get("token") and link["token"] in text:
                continue
            await websocket.send_text(text)

    tasks = {asyncio.create_task(from_browser()), asyncio.create_task(from_worker())}
    _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    if downstream is not None:
        downstream.put(None)
    for task in pending:
        task.cancel()


async def _fanout(
    websocket: WebSocket,
    relay,
    account_id: str,
    device_id: str | None,
    on_text=None,
    app_key: str | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    pending_frames: asyncio.Queue = asyncio.Queue()

    def send(frame: dict) -> None:
        loop.call_soon_threadsafe(pending_frames.put_nowait, frame)

    item = relay.listen(account_id, device_id, send, app_key=app_key)
    try:
        while True:
            receive = asyncio.create_task(websocket.receive_text())
            incoming = asyncio.create_task(pending_frames.get())
            done, pending = await asyncio.wait(
                {receive, incoming},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if incoming in done:
                await websocket.send_text(json.dumps(incoming.result(), ensure_ascii=False))
                continue
            try:
                raw = receive.result()
            except Exception:
                return
            if on_text is not None:
                handled = on_text(raw)
                if handled is False:
                    await websocket.send_text(json.dumps({"error": "device_offline"}))
                elif isinstance(handled, dict):
                    await websocket.send_text(json.dumps(handled, ensure_ascii=False))
    finally:
        relay.unlisten(item)


def _namespace_rpc(relay, device_id: str, app_key: str, raw: str) -> dict | None:
    """只改传输层的 id：两个 App 各自的 id=1 不串，method/args 原样透传。"""
    try:
        frame = json.loads(raw)
    except Exception:
        return None
    if not isinstance(frame, dict) or "id" not in frame:
        return None
    frame["id"] = relay.namespace_rpc(device_id, app_key, frame["id"])
    return frame


def _offline_reply(raw: str) -> bool | dict:
    """设备不在线：带 id 的 RPC 立刻回 device_offline，App 不必等超时。"""
    try:
        frame = json.loads(raw)
    except Exception:
        return False
    if not isinstance(frame, dict):
        return False
    return {"id": frame.get("id"), "error": "device_offline"}


def _web_file(root: Path, relative: str) -> Path | None:
    """只认打包目录内真实存在的文件；逃出目录或不存在都当没有。"""
    if not relative:
        return None
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def mount_web(app: FastAPI, directory: Path | None) -> None:
    """把打包后的网页挂到根路径。目录里没有首页时保持只有接口。"""
    if directory is None:
        return
    root = Path(directory)
    index = root / "index.html"
    if not index.is_file():
        return
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.get("/", include_in_schema=False)
    def web_index():
        return FileResponse(index)

    @app.get("/{front_path:path}", include_in_schema=False)
    def web_route(front_path: str):
        """前端深链回首页：真实文件优先，接口与打包资源不动，带扩展名的缺文件仍 404。"""
        if front_path.split("/", 1)[0] in {"api", "assets"}:
            raise HTTPException(status_code=404)
        real = _web_file(root, front_path)
        if real is not None:
            return FileResponse(real)
        if PurePosixPath(front_path).suffix:
            # /assets/xxx.js 已经挂了静态目录，这里只剩真的缺文件，不能拿首页冒充。
            raise HTTPException(status_code=404)
        return FileResponse(index)


def mount_desktop_ui(app: FastAPI, desktop_dir: Path | None) -> None:
    """把完整 GUI 前端暴露为 /desktop/<device_id>，资源使用独立前缀。"""
    if desktop_dir is None or not (desktop_dir / "index.html").is_file():
        return
    assets = desktop_dir / "assets"
    if assets.is_dir():
        app.mount("/desktop-assets", StaticFiles(directory=str(desktop_dir)), name="desktop-assets")
    shim = desktop_dir.parent / "mobile-shim.js"
    icon = desktop_dir / "icon.png"

    @app.get("/icon.png", include_in_schema=False)
    def desktop_icon():
        if not icon.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(icon), media_type="image/png")

    @app.get("/desktop-shim.js", include_in_schema=False)
    def desktop_shim():
        return FileResponse(str(shim), media_type="application/javascript; charset=utf-8")

    @app.get("/desktop/{device_id}", include_in_schema=False)
    def desktop_index(device_id: str):
        from fastapi.responses import HTMLResponse

        html = (desktop_dir / "index.html").read_text(encoding="utf-8")
        injection = (
            f"<script>window.__ycRelayDevice={json.dumps(device_id)};</script>"
            '<script src="/desktop-shim.js"></script>'
        )
        html = html.replace("</head>", injection + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def mount_mobile(app: FastAPI, mobile_dir: Path | None, shim_path: Path | None = None) -> None:
    """把独立移动端（frontend-mobile/dist）暴露为 /m/<device_id>。

    与桌面 UI 共用同一套 relay 协议：注入 __ycRelayDevice，shim 连 /relay/client。
    目录缺失（没部署移动端包）时完全不挂路由，二维码退回 /desktop/<device_id>。
    """
    if mobile_dir is None or not (mobile_dir / "index.html").is_file():
        return
    root = Path(mobile_dir)
    # frontend-mobile 的 vite base 是 /m/，dist 里的资源 URL 形如 /m/assets/xxx.js：
    # 必须挂着这个前缀，否则会落到 mount_web 的通配路由上白屏。
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/m/assets", StaticFiles(directory=str(assets)), name="mobile-assets")
    # mobile-shim.js 在 frontend/ 下（与桌面 UI 共用一份），不在 dist 目录里。
    shim = Path(shim_path) if shim_path is not None else root.parent / "mobile-shim.js"

    # 必须先注册 shim：/m/{device_id} 会把 /m/mobile-shim.js 当成设备号吃掉。
    @app.get("/m/mobile-shim.js", include_in_schema=False)
    def mobile_shim():
        if not shim.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(shim), media_type="application/javascript; charset=utf-8")

    @app.get("/m/{device_id}", include_in_schema=False)
    def mobile_index(device_id: str):
        from fastapi.responses import HTMLResponse

        # dist 根下的散装文件（favicon.svg / manifest.json / icon.png）按真实文件返回，
        # 否则会被当成设备号返回一份 HTML。
        static = _web_file(root, device_id)
        if static is not None:
            return FileResponse(static)
        html = (root / "index.html").read_text(encoding="utf-8")
        injection = (
            f"<script>window.__ycRelayDevice={json.dumps(device_id)};</script>"
            '<script src="/m/mobile-shim.js"></script>'
        )
        html = html.replace("</head>", injection + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def app_from_env():
    from gateway.config import GatewayConfig
    from gateway.runtime import attach_runtime

    config = GatewayConfig.from_env()
    if not config.jwt_secret:
        raise RuntimeError("缺少 XINGYAO_JWT_SECRET")
    app = create_app(config.database, config.jwt_secret)
    try:
        import keyring
        from core.serve.keyring_setup import install_server_keyring
    except ImportError as error:  # pragma: no cover - 依赖缺失时直接说清楚
        raise RuntimeError(f"缺少共享钥匙串依赖：{error}") from error
    password_file = Path(os.environ.get("XINGYAO_KEYRING_PASSWORD_FILE", "") or "")
    if not password_file.is_file():
        # 没有口令就不能悄悄退回系统凭据：部署时必须注入 secret 文件。
        raise RuntimeError("缺少钥匙串口令：请用 secret 注入 XINGYAO_KEYRING_PASSWORD_FILE")
    if not install_server_keyring(config.data_dir, password_file):
        raise RuntimeError("钥匙串口令为空或不可用：请检查 XINGYAO_KEYRING_PASSWORD_FILE")
    app.state.model_store = PlatformModelStore(app.state.database, keyring)
    attach_runtime(app, config)
    mount_desktop_ui(
        app,
        Path(os.environ.get("XINGYAO_DESKTOP_DIST", "/srv/xingyao/frontend/desktop-dist")),
    )
    # 独立移动端可选部署：目录不存在时 mount_mobile 自己不挂路由，行为与从前一致。
    # 默认指向 frontend-mobile/dist（移动端是独立前端子项目），shim 复用 frontend/ 下那份。
    mount_mobile(
        app,
        Path(os.environ.get("XINGYAO_MOBILE_DIST", "/srv/xingyao/frontend-mobile/dist")),
        Path(os.environ.get("XINGYAO_MOBILE_SHIM", "/srv/xingyao/frontend/mobile-shim.js")),
    )
    mount_web(app, config.web_dist)
    return app
