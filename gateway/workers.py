"""worker 容器生命周期。Docker 客户端由外面注入，测试不需要守护进程。"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path


IDLE_SECONDS = 30 * 60
HEALTH_TIMEOUT = 60


def container_name(account_id: str) -> str:
    return f"xingyao-worker-{account_id}"


def container_spec(account_id: str, image: str, volume: Path, network: str) -> dict:
    return {
        "name": container_name(account_id),
        "image": image,
        "user": "10001:10001",
        "network": network,
        "labels": {"xingyao.account": account_id},
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "mem_limit": "2g",
        "cpus": 2,
        "pids_limit": 512,
        "shm_size": "1g",
        "ports": {},
        "binds": [f"{volume}:/data"],
        "env": {
            "TZ": "Asia/Shanghai",
            "YCODE_DATA_DIR": "/data",
            "YCODE_TOOL_PROFILE": "server",
            "YCODE_SERVE_BIND": "0.0.0.0",
            "YCODE_SERVE_PORT": "8766",
        },
        "docker_sock": False,
    }


class WorkerManager:
    def __init__(self, docker, database, accounts_root: Path, image: str, network: str = "xingyao"):
        self.docker = docker
        self.database = database
        self.accounts_root = Path(accounts_root)
        self.image = image
        self.network = network

    def volume_for(self, account_id: str) -> Path:
        path = self.accounts_root / account_id / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure_token(self, account_id: str) -> str:
        row = self.database.execute(
            "SELECT worker_token FROM workers WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        if row:
            token = row["worker_token"]
        else:
            token = secrets.token_urlsafe(24)
            now = int(time.time())
            self.database.execute(
                """
                INSERT INTO workers(account_id, container_name, worker_token, desired_state, updated_at)
                VALUES (?, ?, ?, 'stopped', ?)
                """,
                (account_id, container_name(account_id), token, now),
            )
            self.database.commit()
        self._write_worker_config(account_id, token)
        return token

    def _write_worker_config(self, account_id: str, token: str) -> None:
        path = self.volume_for(account_id) / "config.json"
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        data["remote_pair_token"] = token
        data["remote_token_ttl"] = 0
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def ensure_running(self, account_id: str, health, clock=time.monotonic) -> dict:
        token = self.ensure_token(account_id)
        spec = container_spec(account_id, self.image, self.volume_for(account_id), self.network)
        if spec["ports"] or spec["docker_sock"]:
            raise RuntimeError("worker 不允许发布端口或挂载 docker.sock")
        name = spec["name"]
        if not self.docker.exists(name):
            self.docker.create(spec)
        self.docker.start(name)
        deadline = clock() + HEALTH_TIMEOUT
        while clock() < deadline:
            try:
                body = health(f"http://{name}:8766/health")
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("ok") is True and body.get("boot_id"):
                self.database.execute(
                    "UPDATE workers SET desired_state = 'running', updated_at = ? WHERE account_id = ?",
                    (int(time.time()), account_id),
                )
                self.database.commit()
                return {"ok": True, "boot_id": body["boot_id"], "worker_token": token}
            time.sleep(0.01)
        return {"ok": False, "error": "worker_lost", "boot_id": None}

    def stop(self, account_id: str) -> None:
        name = container_name(account_id)
        if self.docker.exists(name):
            self.docker.stop(name)
        self.database.execute(
            "UPDATE workers SET desired_state = 'stopped', updated_at = ? WHERE account_id = ?",
            (int(time.time()), account_id),
        )
        self.database.commit()

    def consider_idle(self, account_id: str, metrics: dict, now: float) -> bool:
        if metrics.get("active_turn"):
            return False
        last = int(metrics.get("last_activity_ts") or 0)
        if now - last <= IDLE_SECONDS:
            return False
        self.stop(account_id)
        return True


class IdleSupervisor:
    """30 秒一次的指标轮询由调用方驱动。这里只根据一份指标决定要不要停。"""

    def __init__(self, manager: WorkerManager):
        self.manager = manager

    def tick(self, account_id: str, metrics: dict, now: float) -> bool:
        return self.manager.consider_idle(account_id, metrics, now)
