from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .auth import issue_device_token, issue_remote_token, read_remote_token
from .db import connect
from .mcp_bridge import mount_mcp_and_skill_routes
from .registry import (
    OUTBOUND_LIMIT,
    DeviceAlreadyEnrolledError,
    DeviceSweeper,
    RelayRegistry,
    hash_token,
)


class DeviceTokenBody(BaseModel):
    device_id: str
    device_token: str


class DeviceEnrollBody(BaseModel):
    device_code: str
    name: str | None = None
    enroll_key: str | None = None


def _namespace_rpc(relay: RelayRegistry, device_id: str, app_key: str, raw: str) -> dict | None:
    try:
        frame = json.loads(raw)
    except Exception:
        return None
    if not isinstance(frame, dict) or "id" not in frame:
        return None
    frame["id"] = relay.namespace_rpc(device_id, app_key, frame["id"])
    return frame


def _offline_reply(raw: str) -> bool | dict:
    try:
        frame = json.loads(raw)
    except Exception:
        return False
    if not isinstance(frame, dict):
        return False
    return {"id": frame.get("id"), "error": "device_offline"}


async def _fanout(
    websocket: WebSocket,
    relay: RelayRegistry,
    device_id: str,
    on_text=None,
    app_key: str | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    pending_frames: asyncio.Queue = asyncio.Queue()

    def send(frame: dict) -> None:
        loop.call_soon_threadsafe(pending_frames.put_nowait, frame)

    item = relay.listen(device_id, send, app_key=app_key)
    receive = asyncio.create_task(websocket.receive_text())
    # kicked 只建一次、不反复 cancel：签发新票时由它的 closer 置位，立即断开本连接
    kicked = asyncio.create_task(stop.wait()) if stop is not None else None
    try:
        while True:
            incoming = asyncio.create_task(pending_frames.get())
            waits = {receive, incoming}
            if kicked is not None:
                waits.add(kicked)
            done, pending = await asyncio.wait(
                waits,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                if task is not receive and task is not kicked:
                    task.cancel()
            if stop is not None and stop.is_set():
                await websocket.close(code=4401)
                return
            # 同批完成的任务必须全部消费：incoming 与 receive 同时就绪时
            # 既发帧也处理上行帧，严禁 continue 丢弃同批另一个结果。
            if incoming in done:
                await websocket.send_text(json.dumps(incoming.result(), ensure_ascii=False))
            if receive not in done:
                continue
            try:
                raw = receive.result()
            except Exception:
                return
            receive = asyncio.create_task(websocket.receive_text())
            if on_text is not None:
                handled = on_text(raw)
                if handled is False:
                    await websocket.send_text(json.dumps({"error": "device_offline"}))
                elif isinstance(handled, dict):
                    await websocket.send_text(json.dumps(handled, ensure_ascii=False))
    finally:
        receive.cancel()
        try:
            await receive
        except (asyncio.CancelledError, Exception):
            pass
        if kicked is not None:
            kicked.cancel()
            try:
                await kicked
            except (asyncio.CancelledError, Exception):
                pass
        relay.unlisten(item)


def _web_file(root: Path, relative: str) -> Path | None:
    if not relative:
        return None
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def _mount_desktop(app: FastAPI, desktop_dir: Path | None, shim: Path | None) -> None:
    if desktop_dir is None or not (desktop_dir / "index.html").is_file():
        return
    if desktop_dir.is_dir():
        app.mount("/desktop-assets", StaticFiles(directory=str(desktop_dir)), name="desktop-assets")
    assets = desktop_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="desktop-root-assets")
    icon = desktop_dir / "icon.png"

    @app.get("/icon.png", include_in_schema=False)
    def desktop_icon():
        if not icon.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(icon), media_type="image/png")

    @app.get("/desktop-shim.js", include_in_schema=False)
    def desktop_shim():
        if shim is None or not shim.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(shim), media_type="application/javascript; charset=utf-8")

    @app.get("/desktop/{device_id}", include_in_schema=False)
    def desktop_index(device_id: str):
        html = (desktop_dir / "index.html").read_text(encoding="utf-8")
        injection = (
            f"<script>window.__ycRelayDevice={json.dumps(device_id)};</script>"
            '<script src="/desktop-shim.js"></script>'
        )
        html = html.replace("</head>", injection + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _mount_mobile(app: FastAPI, mobile_dir: Path | None, shim: Path | None) -> None:
    if mobile_dir is None or not (mobile_dir / "index.html").is_file():
        return
    root = Path(mobile_dir)
    # frontend-mobile 的 vite base 是 /m/，dist 里的资源 URL 形如 /m/assets/xxx.js。
    assets = root / "assets"
    if assets.is_dir():
        app.mount("/m/assets", StaticFiles(directory=str(assets)), name="mobile-assets")

    # 必须先注册 shim：/m/{device_id} 会把 /m/mobile-shim.js 当成设备号吃掉。
    @app.get("/m/mobile-shim.js", include_in_schema=False)
    def mobile_shim():
        if shim is None or not shim.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(str(shim), media_type="application/javascript; charset=utf-8")

    @app.get("/m/{device_id:path}", include_in_schema=False)
    def mobile_index(device_id: str):
        # dist 根下的散装文件（favicon.svg / manifest.json / icon.png）按真实文件返回，
        # 否则会被当成设备号返回一份 HTML。
        static = _web_file(root, device_id)
        if static is not None:
            return FileResponse(static)
        # SPA 深链（如 /m/convo/session_123）回退到 index.html；带扩展名的缺失资源照常 404。
        last_seg = device_id.rstrip("/").split("/")[-1] if device_id.rstrip("/") else ""
        if "." in last_seg:
            raise HTTPException(status_code=404)
        head_device = device_id.split("/", 1)[0] if "/" in device_id else device_id
        html = (root / "index.html").read_text(encoding="utf-8")
        injection = (
            f"<script>window.__ycRelayDevice={json.dumps(head_device)};</script>"
            '<script src="/m/mobile-shim.js"></script>'
        )
        html = html.replace("</head>", injection + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def create_app(
    db_path,
    jwt_secret: str,
    *,
    desktop_dir=None,
    mobile_dir=None,
    shim_path=None,
    ticket_rate: tuple[int, float] = (6, 600),
    enroll_key: str | None = None,
    enroll_rate: tuple[int, float] = (10, 60),
) -> FastAPI:
    database = connect(db_path)
    relay = RelayRegistry()
    ticket_max, ticket_window = int(ticket_rate[0]), float(ticket_rate[1])
    _ticket_hits: dict[str, list[float]] = {}
    _ticket_lock = threading.Lock()
    enroll_max, enroll_window = int(enroll_rate[0]), float(enroll_rate[1])
    _enroll_hits: dict[str, list[float]] = {}
    _enroll_lock = threading.Lock()
    expected_enroll_key = str(enroll_key or "").strip() or None

    def _rate_allowed(
        hits_map: dict[str, list[float]],
        lock: threading.Lock,
        key: str,
        now: float,
        limit: int,
        window: float,
    ) -> bool:
        with lock:
            for k in list(hits_map):
                stamps = [stamp for stamp in hits_map[k] if now - stamp < window]
                if stamps:
                    hits_map[k] = stamps
                else:
                    del hits_map[k]
            hits = hits_map.get(key, [])
            if len(hits) >= limit:
                return False
            hits.append(now)
            hits_map[key] = hits
            return True

    def _ticket_allowed(device_id: str, now: float) -> bool:
        return _rate_allowed(_ticket_hits, _ticket_lock, device_id, now, ticket_max, ticket_window)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 45 秒无心跳标离线（心跳 15s + 容差）；桌面侧断开立刻由路由自己标，这里兜底。
        stop = asyncio.Event()
        sweeper = DeviceSweeper(relay, relay.publish, interval=5.0)
        task = asyncio.create_task(sweeper.run(stop))
        app.state.relay = relay
        app.state.device_sweeper = sweeper
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            database.close()

    app = FastAPI(lifespan=lifespan)
    app.state.database = database
    app.state.secret = jwt_secret
    app.state.relay = relay

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/relay/device-token")
    def relay_device_token(body: DeviceTokenBody):
        row = database.execute("SELECT * FROM devices WHERE id = ?", (body.device_id,)).fetchone()
        # 令牌比对走常量时间：不能按响应耗时逐字节反推 token_hash
        if row is None or int(row["revoked"]) or not secrets.compare_digest(
            str(row["token_hash"]), hash_token(body.device_token)
        ):
            raise HTTPException(status_code=401, detail="设备令牌无效")
        # 限流只计已验证设备：无效令牌不占额度，防止伪造请求把合法设备的换票锁死。
        if not _ticket_allowed(body.device_id, time.time()):
            raise HTTPException(status_code=429, detail="换票过于频繁")
        payload = issue_device_token(database, jwt_secret, body.device_id)
        # 签发新票即换代：旧链接（含泄露出去的那张）立刻作废，
        # 已连着的远端页面一并踢下线，并回收旧 jti 的 App 名额
        relay.kick_apps(body.device_id)
        return payload

    @app.post("/api/relay/device-enroll")
    def relay_device_enroll(body: DeviceEnrollBody, request: Request):
        """客户端携带唯一设备编码申请独立 device_id 与设备令牌（不同设备不同令牌）。"""
        if expected_enroll_key is not None and not secrets.compare_digest(
            expected_enroll_key, str(body.enroll_key or "").strip()
        ):
            raise HTTPException(status_code=401, detail="注册密钥无效")
        client_ip = request.client.host if request.client else "unknown"
        if not _rate_allowed(
            _enroll_hits, _enroll_lock, client_ip, time.time(), enroll_max, enroll_window
        ):
            raise HTTPException(status_code=429, detail="注册过于频繁")
        try:
            device_id, token = relay.enroll_device(database, body.device_code, body.name)
        except DeviceAlreadyEnrolledError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "device_id": device_id,
            "token": token,
            "device_code": str(body.device_code or "").strip(),
        }

    @app.websocket("/relay/device")
    async def relay_device(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        if not isinstance(first, dict) or first.get("type") != "relay_register":
            await websocket.close(code=4401)
            return
        device_id = str(first.get("device_id") or "")
        result = relay.register(
            database,
            device_id,
            str(first.get("token") or ""),
            str(first.get("boot_id") or ""),
        )
        if not result.get("ok"):
            await websocket.close(code=4401)
            return
        kicked = result.get("kicked_closer")
        if callable(kicked):
            kicked()
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
                    lost = relay.enqueue(device_id)
                    if lost is not None:
                        relay.publish(device_id, lost)

            loop.call_soon_threadsafe(put)
            return True

        device = relay.devices[device_id]
        device["connection"] = closer
        device["send"] = send_raw
        await websocket.send_text(json.dumps(result["registered"], ensure_ascii=False))
        if result.get("reset_frame"):
            relay.publish(device_id, result["reset_frame"])
        if result.get("online_frame"):
            relay.publish(device_id, result["online_frame"])
        receive = asyncio.create_task(websocket.receive_text())
        try:
            while not stop.is_set():
                kicked_wait = asyncio.create_task(stop.wait())
                from_phone = asyncio.create_task(outgoing.get())
                done, pending = await asyncio.wait(
                    {receive, kicked_wait, from_phone},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                # receive 常驻：pending 时绝不取消——取消会偷走已从 ASGI 通道出队的帧。
                # 只取消等待型任务（stop 事件/出站队列）；receive 仅在消费结果后重建。
                for task in pending:
                    if task is not receive:
                        task.cancel()
                if stop.is_set():
                    await websocket.close(code=4401)
                    return
                # 同批完成的任务必须全部消费：from_phone 与 receive 同时就绪时
                # 既发帧也处理上行帧，严禁 continue 丢弃同批另一个结果。
                if from_phone in done:
                    await websocket.send_text(from_phone.result())
                if receive not in done:
                    continue
                try:
                    raw = receive.result()
                except Exception:
                    return
                receive = asyncio.create_task(websocket.receive_text())
                try:
                    frame = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("type") == "ping":
                    relay.touch(device_id)
                    await websocket.send_text(json.dumps({"type": "pong"}))
                    continue
                if frame.get("type") == "relay_lost":
                    lost = relay.enqueue(device_id)
                    if lost is not None:
                        relay.publish(device_id, lost)
                    continue
                seq = frame.get("seq")
                if isinstance(seq, int):
                    relay.remember(device_id, seq, raw)
                route = relay.resolve_rpc(device_id, frame.get("id"))
                if route is not None:
                    app_key, original = route
                    reply = dict(frame)
                    reply["id"] = original
                    relay.publish(device_id, reply, app_key=app_key)
                    continue
                relay.publish(device_id, frame)
        finally:
            receive.cancel()
            try:
                await receive
            except (asyncio.CancelledError, Exception):
                pass
            current = relay.devices.get(device_id) or {}
            if current.get("send") is send_raw:
                # 只有这条连接还占着设备时才标离线：新一代连接已接手就不能打掉它。
                relay.mark_offline(device_id)

    @app.websocket("/relay/client")
    async def relay_client(websocket: WebSocket):
        await websocket.accept()
        try:
            first = json.loads(await websocket.receive_text())
        except Exception:
            await websocket.close(code=4401)
            return
        if not isinstance(first, dict):
            await websocket.close(code=4401)
            return
        device_id = str(first.get("device_id") or "")
        token = str(first.get("jwt") or first.get("token") or "")
        claims = read_remote_token(database, token, jwt_secret, device_id)
        if claims is None:
            await websocket.close(code=4401)
            return
        attached = relay.attach_app(device_id, claims.jti)
        if not attached.get("ok"):
            await websocket.close(code=4401)
            return
        app_key = attached["key"]
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def closer() -> None:
            # 可能从 HTTP 路由（线程池）调用，必须回到本连接的事件循环置位
            loop.call_soon_threadsafe(stop.set)

        relay.bind_app_closer(device_id, app_key, closer)
        try:
            plan = relay.frames_for(device_id, first.get("last_seq"), first.get("last_boot_id"))
            await websocket.send_text(json.dumps(plan, ensure_ascii=False))

            def forward(raw: str) -> bool | dict:
                send = (relay.devices.get(device_id) or {}).get("send")
                if not callable(send):
                    return _offline_reply(raw)
                frame = _namespace_rpc(relay, device_id, app_key, raw)
                send(json.dumps(frame, ensure_ascii=False) if frame is not None else raw)
                return True

            await _fanout(
                websocket, relay, device_id, on_text=forward, app_key=app_key, stop=stop
            )
        finally:
            relay.unbind_app_closer(device_id, app_key)
            # 断开/登出都要释放名额，否则名额泄漏会把第 5 个 App 误判成超限。
            relay.detach_app(device_id, app_key)

    mount_mcp_and_skill_routes(app, database, relay)

    shim = Path(shim_path) if shim_path is not None else None
    _mount_desktop(app, Path(desktop_dir) if desktop_dir is not None else None, shim)
    _mount_mobile(app, Path(mobile_dir) if mobile_dir is not None else None, shim)
    return app
