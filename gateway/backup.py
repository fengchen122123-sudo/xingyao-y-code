"""备份是时点替换。工人令牌以 gateway.db 为准，再写回账号卷。"""

from __future__ import annotations

import json
from pathlib import Path


VOLUME_NAMES = (
    "config.json",
    "ycode_gui.db",
    "ycode_gui.db-wal",
    "ycode_gui.db-shm",
    "keyring",
)


def volume_backup_paths(volume: Path) -> list[Path]:
    return [Path(volume) / name for name in VOLUME_NAMES]


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
