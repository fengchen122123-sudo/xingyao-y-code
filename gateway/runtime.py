"""把已经写好的工人、预览和空闲回收装进真正启动的网关进程。"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from gateway.platform_models import load_platform_models
from gateway.usage import apply_usage
from gateway.workers import IdleLoop, IdleSupervisor, WorkerManager, container_name

logger = logging.getLogger(__name__)


class _Outbound(list):
    """append 同时把帧交给上游套接字。测试里的普通 list 不受影响。"""

    def __init__(self, link: "WorkerLink"):
        super().__init__()
        self.link = link

    def append(self, item):
        super().append(item)
        payload = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        self.link.outgoing.put(payload)


class WorkerLink:
    def __init__(self, ws_url: str, token: str):
        self.token = token
        self.downstream: queue.Queue = queue.Queue()
        self.outgoing: queue.Queue = queue.Queue()
        self.sent = _Outbound(self)
        self._closed = False
        threading.Thread(target=self._run, args=(ws_url,), daemon=True, name="worker-link").start()

    def close(self) -> None:
        self._closed = True
        self.downstream.put(None)

    def _run(self, ws_url: str) -> None:
        try:
            from websockets.sync.client import connect

            with connect(ws_url, open_timeout=10) as socket:
                def writer() -> None:
                    while not self._closed:
                        item = self.outgoing.get()
                        if item is None or self._closed:
                            return
                        socket.send(item)

                threading.Thread(target=writer, daemon=True, name="worker-link-writer").start()
                while not self._closed:
                    self.downstream.put(socket.recv())
        except Exception:
            logger.exception("连接工人失败 %s", ws_url)
        finally:
            self.downstream.put(None)


def http_json(url: str, headers: dict | None = None, timeout: float = 5):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type") or "application/json"
            return response.status, body, content_type
    except urllib.error.HTTPError as error:
        return error.code, error.read(), "text/plain"
    except Exception:
        logger.exception("请求工人失败 %s", url)
        return 502, b"", "text/plain"


def http_health(url: str) -> dict | None:
    status, body, _content_type = http_json(url, timeout=3)
    if status != 200:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def fetch_preview(request: dict):
    status, body, content_type = http_json(
        request["url"],
        headers=request.get("headers") or {},
        timeout=5,
    )
    return status, body, content_type


class SdkDocker:
    def __init__(self):
        import docker

        self.client = docker.from_env()

    def exists(self, name: str) -> bool:
        try:
            self.client.containers.get(name)
            return True
        except Exception:
            return False

    def create(self, spec: dict) -> None:
        volumes = {}
        for bind in spec["binds"]:
            raw = bind[:-3] if bind.endswith(":ro") else bind
            mode = "ro" if bind.endswith(":ro") else "rw"
            host, container = raw.rsplit(":", 1)
            volumes[host] = {"bind": container, "mode": mode}
        self.client.containers.create(
            spec["image"],
            name=spec["name"],
            detach=True,
            user=spec["user"],
            labels=spec["labels"],
            cap_drop=spec["cap_drop"],
            security_opt=spec["security_opt"],
            mem_limit=spec["mem_limit"],
            nano_cpus=int(float(spec["cpus"]) * 1_000_000_000),
            pids_limit=spec["pids_limit"],
            shm_size=spec["shm_size"],
            environment=spec["env"],
            volumes=volumes,
            network=spec["network"],
        )

    def start(self, name: str) -> None:
        self.client.containers.get(name).start()

    def stop(self, name: str) -> None:
        try:
            self.client.containers.get(name).stop(timeout=20)
        except Exception:
            logger.exception("停止容器失败 %s", name)


def dial_worker(manager: WorkerManager, account_id: str) -> dict:
    result = manager.ensure_running(account_id, health=http_health)
    if not result.get("ok"):
        return {
            "status": "lost",
            "boot_id": result.get("boot_id"),
            "token": result.get("worker_token") or "",
            "sent": [],
            "downstream": None,
        }
    name = container_name(account_id)
    link = WorkerLink(f"ws://{name}:8766/", result["worker_token"])
    return {
        "status": "ok",
        "token": result["worker_token"],
        "sent": link.sent,
        "downstream": link.downstream,
        "link": link,
    }


def _account_ids(database) -> list[str]:
    rows = database.execute("SELECT id FROM accounts WHERE disabled = 0").fetchall()
    return [row["id"] for row in rows]


def _metrics(manager: WorkerManager, account_id: str) -> dict:
    row = manager.database.execute(
        "SELECT worker_token, desired_state FROM workers WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None or row["desired_state"] != "running":
        return {"active_turn": False, "last_activity_ts": 0}
    name = container_name(account_id)
    status, body, _content_type = http_json(
        f"http://{name}:8766/metrics",
        headers={"Authorization": f"Bearer {row['worker_token']}"},
        timeout=5,
    )
    if status != 200:
        return {"active_turn": True, "last_activity_ts": time.time()}
    try:
        metrics = json.loads(body.decode("utf-8"))
    except Exception:
        return {"active_turn": True, "last_activity_ts": time.time()}
    if isinstance(metrics, dict):
        if metrics.get("token_usage") or metrics.get("turns_total"):
            apply_usage(manager.database, account_id, metrics, time.time())
        return metrics
    return {"active_turn": True, "last_activity_ts": time.time()}


def _idle_loop(manager: WorkerManager) -> None:
    loop = IdleLoop(IdleSupervisor(manager), interval=30)
    while True:
        time.sleep(1)
        try:
            loop.advance(time.time(), _account_ids(manager.database), lambda account_id: _metrics(manager, account_id))
        except Exception:
            logger.exception("空闲回收失败")


def _sweep_loop(relay) -> None:
    while True:
        time.sleep(5)
        try:
            for event in relay.sweep(time.time()):
                device_id = event["data"]["device_id"]
                account_id = relay.devices.get(device_id, {}).get("account_id")
                if account_id:
                    relay.publish(account_id, device_id, event)
        except Exception:
            logger.exception("设备心跳扫描失败")


def attach_runtime(app, config) -> None:
    """Docker 不可用时网关仍能登录和管理，工人接口返回没有 Docker。"""
    try:
        docker = SdkDocker()
    except Exception:
        logger.exception("Docker 不可用，工人容器不会启动")
        return
    manager = WorkerManager(
        docker,
        app.state.database,
        Path(config.accounts_root or "/srv/xingyao/accounts"),
        config.worker_image,
        config.docker_network,
        platform_models=lambda: load_platform_models(app.state.database),
    )
    app.state.workers = manager
    app.state.health = http_health
    app.state.fetch_preview = fetch_preview
    app.state.dial_worker = lambda account_id: dial_worker(manager, account_id)
    threading.Thread(target=_idle_loop, args=(manager,), daemon=True, name="gateway-idle").start()
    threading.Thread(target=_sweep_loop, args=(app.state.relay,), daemon=True, name="gateway-devices").start()
