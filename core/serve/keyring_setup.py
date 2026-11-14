"""server 档使用文件钥匙串。口令只从 secret 文件读取，不走 getpass。"""

from __future__ import annotations

import os
from pathlib import Path


def install_server_keyring(data_dir: Path, password_file: Path | None = None) -> bool:
    """装上 EncryptedKeyring。口令文件不存在时返回 False，不调用 getpass。"""
    secret = password_file or Path(
        os.environ.get("YCODE_KEYRING_PASSWORD_FILE", "/run/secrets/keyring_password")
    )
    if not secret.is_file():
        return False
    password = secret.read_text(encoding="utf-8").strip()
    if not password:
        return False
    keyring_dir = Path(data_dir) / "keyring"
    keyring_dir.mkdir(parents=True, exist_ok=True)
    os.environ["XDG_DATA_HOME"] = str(data_dir)

    from keyrings.alt.file import EncryptedKeyring

    ring = EncryptedKeyring()
    ring.file_path = str(keyring_dir / "crypted_pass.cfg")
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
