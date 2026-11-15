"""server 档使用文件钥匙串。口令只从 secret 文件读取，不走 getpass。

网关（master）在数据目录里创建 crypted_pass.cfg；工人只读挂载同一个文件，
通过 YCODE_KEYRING_FILE 指过去，靠 YCODE_KEYRING_PASSWORD_FILE 里的同一口令解锁。
"""

from __future__ import annotations

import os
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on"}
DEFAULT_PASSWORD_FILE = "/run/secrets/keyring_password"


def keyring_file(data_dir: Path) -> Path:
    """共享钥匙串数据库路径。工人用 YCODE_KEYRING_FILE 指向网关卷里的那份。"""
    override = os.environ.get("YCODE_KEYRING_FILE", "").strip()
    if override:
        return Path(override)
    return Path(data_dir) / "keyring" / "crypted_pass.cfg"


def keyring_read_only() -> bool:
    return os.environ.get("YCODE_KEYRING_READ_ONLY", "").strip().lower() in TRUTHY


def install_server_keyring(data_dir: Path, password_file: Path | None = None) -> bool:
    """装上 EncryptedKeyring。口令文件不存在时返回 False，不调用 getpass。"""
    secret = password_file or Path(
        os.environ.get("YCODE_KEYRING_PASSWORD_FILE") or DEFAULT_PASSWORD_FILE
    )
    if not secret.is_file():
        return False
    password = secret.read_text(encoding="utf-8").strip()
    if not password:
        return False
    path = keyring_file(data_dir)
    if not keyring_read_only():
        path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_DATA_HOME"] = str(data_dir)

    from keyrings.alt.file import EncryptedKeyring

    ring = EncryptedKeyring()
    ring.file_path = str(path)
    ring._get_new_password = lambda: password  # noqa: SLF001
    ring._unlock = lambda: _unlock(ring, password)  # noqa: SLF001

    import keyring

    keyring.set_keyring(ring)
    return True


def _unlock(ring, password: str) -> None:
    ring.keyring_key = password
    reference = ring.get_password("keyring-setting", "password reference")
    if reference != "password reference value":
        if hasattr(ring, "_lock"):
            ring._lock()
        raise ValueError("钥匙串口令不正确")
