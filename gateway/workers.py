"""worker 容器生命周期。Docker 客户端由外面注入，测试不需要守护进程。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)


IDLE_SECONDS = 30 * 60
HEALTH_TIMEOUT = 60

# desired_state 的取值：库里的期望状态，不是 docker 的实时状态。
STATE_STARTING = "starting"
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_LOST = "lost"


def container_name(account_id: str) -> str:
    return f"xingyao-worker-{account_id}"


def docker_host_gateway() -> str:
    """容器访问宿主机的地址。工人和网关在同一张网桥上，默认网关就是宿主机。"""
    override = os.environ.get("XINGYAO_HOST_GATEWAY", "").strip()
    if override:
        return override
    route = Path("/proc/net/route")
    if not route.is_file():
        return ""
    try:
        lines = route.read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[1] != "00000000" or len(fields[2]) != 8:
            continue
        raw = fields[2]
        return ".".join(str(int(raw[index:index + 2], 16)) for index in (6, 4, 2, 0))
    return ""


def worker_base_url(url: str, host: str) -> str:
    """127.0.0.1 在工人容器里是容器自己。改写成宿主机地址，路径和端口保持不变。"""
    if not host or not url:
        return url
    parts = urlsplit(url)
    if parts.hostname not in {"127.0.0.1", "localhost"}:
        return url
    port = f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, f"{host}{port}", parts.path, parts.query, parts.fragment))


def container_spec(
    account_id: str,
    image: str,
    volume: Path,
    network: str,
    keyring_file: str = "",
    keyring_source: str = "",
    source_dir: str = "",
) -> dict:
    binds = [
        f"{volume}:/data",
        f"{volume.parent / 'workspace'}:/workspace",
    ]
    if source_dir:
        # 热更：宿主机源码直挂进容器，改完重启工人即生效，不必重建镜像
        binds.append(f"{source_dir}:/opt/xingyao")
    env = {
        "TZ": "Asia/Shanghai",
        "LANG": "C.UTF-8",
        "HOME": "/data/home",
        "YCODE_DATA_DIR": "/data",
        "YCODE_TOOL_PROFILE": "server",
        "YCODE_SERVE_BIND": "0.0.0.0",
        "YCODE_SERVE_PORT": "8766",
        "YCODE_KEYRING_PASSWORD_FILE": "/run/secrets/keyring_password",
    }
    if keyring_source:
        # 只读共享网关的钥匙串数据库：工人的数据卷里不再各留一份空钥匙串。
        binds.append(f"{keyring_source}:/run/keyring:ro")
        env["YCODE_KEYRING_FILE"] = "/run/keyring/crypted_pass.cfg"
        env["YCODE_KEYRING_READ_ONLY"] = "1"
    if keyring_file:
        binds.append(f"{keyring_file}:/run/secrets/keyring_password:ro")
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
        "binds": binds,
        "env": env,
        "docker_sock": False,
    }


class WorkerManager:
    def __init__(self, docker, database, accounts_root: Path, image: str, network: str = "xingyao", platform_models=None, host_gateway: str | None = None, source_dir: str = ""):
        self.docker = docker
        self.database = database
        self.accounts_root = Path(accounts_root)
        self.image = image
        self.network = network
        self.platform_models = platform_models
        self.host_gateway = docker_host_gateway() if host_gateway is None else host_gateway
        # 源码挂载点：容器内 /opt/xingyao 换成宿主机目录
        self.source_dir = source_dir
        self._guard = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}

    def _account_lock(self, account_id: str) -> threading.Lock:
        with self._guard:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._account_locks[account_id] = lock
            return lock

    def volume_for(self, account_id: str) -> Path:
        path = self.accounts_root / account_id / "data"
        path.mkdir(parents=True, exist_ok=True)
        (path / "home").mkdir(parents=True, exist_ok=True)
        workspace = path.parent / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        if os.name == "posix" and hasattr(os, "chown") and os.geteuid() == 0:
            # 工人以 UID 10001 运行；网关创建卷目录后必须把可写目录交给它。
            for target in (path, path / "home", workspace):
                try:
                    os.chown(target, 10001, 10001)
                except OSError:
                    logger.exception("无法设置工人目录权限 %s", target)
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

    def row(self, account_id: str):
        return self.database.execute(
            "SELECT * FROM workers WHERE account_id = ?",
            (account_id,),
        ).fetchone()

    def desired_state(self, account_id: str) -> str:
        row = self.row(account_id)
        return "" if row is None else str(row["desired_state"])

    def container_state(self, account_id: str) -> str:
        """docker 的实时状态，只给管理页看，不写库。"""
        probe = getattr(self.docker, "state", None)
        if callable(probe):
            try:
                return str(probe(container_name(account_id)) or "unknown")
            except Exception:
                return "unknown"
        return "running" if self.docker.exists(container_name(account_id)) else "missing"

    def set_state(self, account_id: str, state: str, **fields) -> None:
        """写 desired_state 和附带字段。只改传进来的列，其它列保持原样。"""
        row = self.row(account_id)
        if row is None:
            self.ensure_token(account_id)
        allowed = {"started_at", "last_boot_id", "version", "active_turn", "last_error", "last_activity"}
        unknown = set(fields) - allowed
        if unknown:
            raise TypeError(f"未知的工人字段：{sorted(unknown)}")
        assignments = ["desired_state = ?", "updated_at = ?"]
        values: list = [state, int(time.time())]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            values.append(value)
        values.append(account_id)
        self.database.execute(
            f"UPDATE workers SET {', '.join(assignments)} WHERE account_id = ?",
            values,
        )
        self.database.commit()

    def record_metrics(self, account_id: str, metrics: dict, now: float) -> None:
        """轮询拿到的指标落库，管理页和空闲回收都用这一份。"""
        row = self.row(account_id)
        if row is None:
            return
        last_activity = metrics.get("last_activity_ts")
        active_turn = 1 if metrics.get("active_turn") else 0
        version = row["version"]
        reported = metrics.get("version")
        if isinstance(reported, str) and reported:
            version = reported
        self.database.execute(
            """
            UPDATE workers
            SET last_activity = COALESCE(?, last_activity),
                active_turn = ?,
                version = ?,
                updated_at = ?
            WHERE account_id = ?
            """,
            (
                None if last_activity is None else int(last_activity),
                active_turn,
                version,
                int(now),
                account_id,
            ),
        )
        self.database.commit()

    def _write_worker_config(self, account_id: str, token: str) -> None:
        path = self.volume_for(account_id) / "config.json"
        data = {}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
        data["remote_pair_token"] = token
        data["remote_token_ttl"] = 0
        data["permission_mode"] = "full"
        if self.platform_models is not None:
            models = self.platform_models() or {}
            model_store = getattr(self.platform_models, "__self__", None)
            if model_store is not None and hasattr(model_store, "references"):
                models = model_store.references()
            providers = []
            for item in models.get("providers") or []:
                if not isinstance(item, dict):
                    continue
                copied = dict(item)
                copied["base_url"] = worker_base_url(str(copied.get("base_url") or ""), self.host_gateway)
                providers.append(copied)
            data["providers"] = providers
            data["active_model_id"] = str(models.get("active_model_id") or "")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def ensure_running(self, account_id: str, health, clock=time.monotonic) -> dict:
        with self._account_lock(account_id):
            return self._ensure_running_locked(account_id, health, clock)

    def _ensure_running_locked(self, account_id: str, health, clock) -> dict:
        token = self.ensure_token(account_id)
        # 网关容器内看到的是 /run/secrets/...，但 Docker daemon 解析 bind source
        # 时用的是宿主机路径。两者必须分开，否则 worker 容器拿到的是容器内路径。
        keyring_file = os.environ.get("XINGYAO_KEYRING_HOST_PASSWORD_FILE", "").strip()
        if not keyring_file:
            secret = os.environ.get("XINGYAO_KEYRING_PASSWORD_FILE", "")
            keyring_file = secret if secret and Path(secret).is_file() else ""
        spec = container_spec(
            account_id,
            self.image,
            self.volume_for(account_id),
            self.network,
            keyring_file,
            keyring_source=os.environ.get("XINGYAO_WORKER_KEYRING_SOURCE", "").strip(),
            source_dir=self.source_dir,
        )
        if spec["ports"] or spec["docker_sock"]:
            raise RuntimeError("worker 不允许发布端口或挂载 docker.sock")
        name = spec["name"]
        self.set_state(account_id, STATE_STARTING, last_error="")
        try:
            if not self.docker.exists(name):
                self.docker.create(spec)
            self.docker.start(name)
        except Exception:
            logger.exception("启动工人容器失败 %s", name)
            self.set_state(account_id, STATE_LOST, last_error="worker_lost")
            return {"ok": False, "error": "worker_lost", "boot_id": None}
        deadline = clock() + HEALTH_TIMEOUT
        while clock() < deadline:
            try:
                body = health(f"http://{name}:8766/health")
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("ok") is True and body.get("boot_id"):
                self.set_state(
                    account_id,
                    STATE_RUNNING,
                    started_at=int(time.time()),
                    last_boot_id=str(body["boot_id"]),
                    version=str(body.get("version") or ""),
                    last_error="",
                )
                return {"ok": True, "boot_id": body["boot_id"], "worker_token": token}
            time.sleep(0.01)
        # 60 秒没起来就只放弃这一次，状态回 stopped，管理页能看见 last_error。
        self.set_state(account_id, STATE_STOPPED, last_error="worker_lost")
        return {"ok": False, "error": "worker_lost", "boot_id": None}

    def restart(self, account_id: str, health, clock=time.monotonic) -> dict:
        self.stop(account_id)
        return self.ensure_running(account_id, health, clock)

    def stop(self, account_id: str) -> None:
        name = container_name(account_id)
        if self.docker.exists(name):
            self.docker.stop(name)
        self.set_state(account_id, STATE_STOPPED)

    def discover(self) -> dict:
        """网关重启后按标签把容器重新认回来，库里以 desired_state 为准。"""
        listed: list[dict] = []
        lister = getattr(self.docker, "list_by_label", None)
        if callable(lister):
            try:
                listed = list(lister("xingyao.account") or [])
            except Exception:
                logger.exception("按标签枚举工人容器失败")
                listed = []
        known = {
            row["account_id"]: row
            for row in self.database.execute("SELECT * FROM workers").fetchall()
        }
        accounts = {
            row["id"]
            for row in self.database.execute("SELECT id FROM accounts").fetchall()
        }
        mapped: list[str] = []
        orphans: list[str] = []
        for item in listed:
            account_id = str(item.get("account_id") or "")
            if not account_id or account_id not in accounts:
                orphans.append(account_id or str(item.get("name") or ""))
                continue
            container_state = str(item.get("state") or "").lower()
            state = STATE_RUNNING if container_state == "running" else STATE_STOPPED
            if account_id not in known:
                self.ensure_token(account_id)
            self.set_state(account_id, state, last_error="")
            mapped.append(account_id)
        missing: list[str] = []
        for account_id, row in known.items():
            if account_id in mapped or account_id not in accounts:
                continue
            if row["desired_state"] in (STATE_RUNNING, STATE_STARTING):
                # 库说该跑，标签里没找到它：标成 lost，等下一次活动重新拉起。
                self.set_state(account_id, STATE_LOST, last_error="container_missing")
                missing.append(account_id)
        return {"mapped": mapped, "orphans": orphans, "missing": missing}

    def consider_idle(self, account_id: str, metrics: dict, now: float) -> bool:
        row = self.row(account_id)
        if row is None or row["desired_state"] != STATE_RUNNING:
            # 已经停过的不要每 30 秒再打一次 docker stop。
            return False
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


class IdleLoop:
    """每 30 秒看一次指标。时钟由调用方推进，测试不必真等。"""

    def __init__(self, supervisor: IdleSupervisor, interval: float = 30):
        self.supervisor = supervisor
        self.interval = interval
        self.next_at = None

    def advance(self, now: float, accounts: list[str], fetch) -> list[str]:
        if self.next_at is None:
            self.next_at = now
        if now < self.next_at:
            return []
        self.next_at = now + self.interval
        stopped = []
        for account_id in accounts:
            if self.supervisor.tick(account_id, fetch(account_id), now):
                stopped.append(account_id)
        return stopped
