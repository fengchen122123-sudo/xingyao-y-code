"""备份入口与恢复语义。

备份是时点替换，不是双向同步。这里只冻结「要备哪些文件」和「恢复之后
旧状态怎么解释」，真正搬运由运维脚本做；网关不自己复制秘密到别处。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# 账号卷里必须进备份的东西。wal/shm 先 checkpoint 再拷。
VOLUME_NAMES = (
    "config.json",
    "ycode_gui.db",
    "ycode_gui.db-wal",
    "ycode_gui.db-shm",
    "keyring",
)
WORKSPACE_DIR = "workspace"
GATEWAY_DB_NAMES = ("gateway.db", "gateway.db-wal", "gateway.db-shm")
DATABASE_MODE = 0o600


def volume_backup_paths(volume: Path) -> list[Path]:
    return [Path(volume) / name for name in VOLUME_NAMES]


def checkpoint(database) -> None:
    """备份前把 WAL 并回主库，否则拷走的是一个不完整的时点。"""
    database.commit()
    try:
        database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        # 别的连接还开着 WAL 时 checkpoint 会返回忙，备份方自己重试即可。
        pass


def checkpoint_volume(volume: Path) -> None:
    """账号卷里的 ycode_gui.db 同理，能连上就 checkpoint，连不上不阻塞备份。"""
    import sqlite3

    path = Path(volume) / "ycode_gui.db"
    if not path.is_file():
        return
    try:
        connection = sqlite3.connect(path)
    except sqlite3.Error:
        return
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.commit()
    except sqlite3.Error:
        pass
    finally:
        connection.close()


def harden(path: Path) -> None:
    """gateway.db 里有密码哈希、refresh 哈希、设备 token 哈希和工人 token 明文。"""
    try:
        os.chmod(path, DATABASE_MODE)
    except OSError:
        return


def entry(
    database,
    accounts_root: Path,
    gateway_db: Path,
    keyring_password_file: Path | None = None,
    account_ids: list[str] | None = None,
) -> dict:
    """冻结的备份集合。返回结构，不落盘、不猜路径。"""
    accounts_root = Path(accounts_root)
    if account_ids is None:
        account_ids = [
            row["id"]
            for row in database.execute("SELECT id, disabled FROM accounts ORDER BY created_at").fetchall()
        ]
    volumes = []
    for account_id in account_ids:
        volume = accounts_root / account_id / "data"
        workspace = accounts_root / account_id / WORKSPACE_DIR
        volumes.append({
            "account_id": account_id,
            "volume": str(volume),
            "workspace": str(workspace),
            "paths": [str(path) for path in volume_backup_paths(volume)]
            + [str(workspace)],
            "checkpoint": ["ycode_gui.db"],
        })
    gateway_db = Path(gateway_db)
    return {
        "volumes": volumes,
        "gateway": {
            "database": str(gateway_db),
            "paths": [str(gateway_db.parent / name) for name in GATEWAY_DB_NAMES],
            "mode": oct(DATABASE_MODE),
            "contains": ["password_hash", "refresh_token_hash", "device_token_hash", "worker_token"],
        },
        "keyring_password": {
            "path": None if keyring_password_file is None else str(keyring_password_file),
            "separate_from_volume": True,
            "note": "口令文件与卷分开存放，不进同一个备份包",
        },
    }


def restore_semantics() -> dict:
    """恢复之后旧状态怎么算。运维说明直接引用这里的措辞。"""
    return {
        "mode": "point_in_time",
        "stop_before_restore": ["worker 容器", "网关进程"],
        "sessions": "所有 WS 断开，用户重新登录",
        "rpc": "进行中的 RPC 直接失败，不自动重放",
        "worker_token": "以恢复出来的 gateway.db 为准；与 config.json 不一致时写回 config 并重启该容器",
        "device_token": "已吊销状态以恢复出来的 gateway.db 为准",
        "warning": "旧库会把后来发生的设备吊销倒回去，这是时点恢复的结果，不是吊销失效",
    }


def reconcile_worker_token(database, account_id: str, volume: Path) -> str:
    """两边不一致时采用库里的令牌，并写回 config.json。"""
    row = database.execute(
        "SELECT worker_token FROM workers WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        raise KeyError(account_id)
    token = row["worker_token"]
    path = Path(volume) / "config.json"
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data["remote_pair_token"] = token
    data["remote_token_ttl"] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return token


def restore_account(database, account_id: str, volume: Path, restart=None) -> dict:
    """按恢复语义收尾：令牌以库为准，不一致就写回并重启容器。"""
    path = Path(volume) / "config.json"
    before = ""
    if path.is_file():
        try:
            before = str(json.loads(path.read_text(encoding="utf-8")).get("remote_pair_token") or "")
        except json.JSONDecodeError:
            before = ""
    token = reconcile_worker_token(database, account_id, volume)
    changed = before != token
    restarted = False
    if changed and callable(restart):
        restart(account_id)
        restarted = True
    return {"token": token, "rewritten": changed, "restarted": restarted}
