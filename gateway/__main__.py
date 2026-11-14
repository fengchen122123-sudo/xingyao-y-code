"""网关命令。init-admin 的口令只从终端读，不接受命令行参数。"""

from __future__ import annotations

import getpass
import sys

from gateway.config import GatewayConfig
from gateway.db import connect
from gateway.auth import init_admin


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "init-admin":
        print("用法: python -m gateway init-admin --username 名字", file=sys.stderr)
        return 2
    if "--password" in argv:
        print("口令不能写在命令行里", file=sys.stderr)
        return 2
    if "--username" not in argv:
        print("缺少 --username", file=sys.stderr)
        return 2
    username = argv[argv.index("--username") + 1]
    password = getpass.getpass("口令: ")
    config = GatewayConfig.from_env()
    database = connect(config.database)
    try:
        init_admin(database, username, password)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 2
    print("管理员已创建")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
