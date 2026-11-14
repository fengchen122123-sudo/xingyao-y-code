"""网关配置。密钥和域名由部署环境注入，不写进仓库。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatewayConfig:
    data_dir: Path
    database: Path
    jwt_secret: str
    docker_network: str = "xingyao"
    worker_image: str = "xingyao-worker:local"
    idle_seconds: int = 30 * 60
    health_timeout_seconds: int = 60
    accounts_root: Path | None = None

    @classmethod
    def from_env(cls) -> "GatewayConfig":
        root = Path(os.environ.get("XINGYAO_GATEWAY_DIR", "/srv/xingyao/gateway"))
        secret = os.environ.get("XINGYAO_JWT_SECRET", "")
        accounts = os.environ.get("XINGYAO_ACCOUNTS_ROOT", "/srv/xingyao/accounts")
        return cls(
            data_dir=root,
            database=root / "gateway.db",
            jwt_secret=secret,
            accounts_root=Path(accounts),
        )
