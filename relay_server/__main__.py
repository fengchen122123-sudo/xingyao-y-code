"""relay_server CLI：serve / create-device / list-devices / revoke / rotate。

env：RELAY_BIND（默认 127.0.0.1）、RELAY_PORT（默认 18080）、RELAY_DB_PATH、
RELAY_JWT_SECRET 或 RELAY_JWT_SECRET_FILE、RELAY_DESKTOP_DIR、RELAY_MOBILE_DIR、
RELAY_SHIM_PATH。设备令牌明文只在创建/轮换时输出一次。
"""

from __future__ import annotations

import argparse
import os
import sys

from .db import connect
from .registry import RelayRegistry


def _db_path() -> str:
    return os.environ.get("RELAY_DB_PATH") or "relay.db"


def _jwt_secret() -> str | None:
    secret = os.environ.get("RELAY_JWT_SECRET")
    if secret:
        return secret
    path = os.environ.get("RELAY_JWT_SECRET_FILE")
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            print(f"无法读取 RELAY_JWT_SECRET_FILE：{exc}", file=sys.stderr)
            return None
    return None


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    secret = _jwt_secret()
    if not secret:
        print("缺少 RELAY_JWT_SECRET 或 RELAY_JWT_SECRET_FILE", file=sys.stderr)
        return 2
    app = create_app(
        _db_path(),
        secret,
        desktop_dir=os.environ.get("RELAY_DESKTOP_DIR") or None,
        mobile_dir=os.environ.get("RELAY_MOBILE_DIR") or None,
        shim_path=os.environ.get("RELAY_SHIM_PATH") or None,
        enroll_key=os.environ.get("RELAY_ENROLL_KEY") or None,
    )
    host = os.environ.get("RELAY_BIND") or "127.0.0.1"
    port = int(os.environ.get("RELAY_PORT") or "18080")
    # 放大 WS 单帧上限（默认 16 MiB），与桌面端 70 MiB 对齐以承载 ≤50 MB 附件上传。
    uvicorn.run(app, host=host, port=port, ws_max_size=70 * 1024 * 1024)
    return 0


def cmd_create_device(args: argparse.Namespace) -> int:
    conn = connect(_db_path())
    try:
        device_id, token = RelayRegistry().create_device(conn, args.name)
    finally:
        conn.close()
    # 明文令牌仅此次输出：库里只存 SHA-256 哈希，丢了只能 rotate。
    print(f"device_id: {device_id}")
    print(f"token: {token}")
    return 0


def cmd_list_devices(_args: argparse.Namespace) -> int:
    conn = connect(_db_path())
    try:
        rows = conn.execute(
            "SELECT id, name, revoked, created_at, last_seen FROM devices ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    for row in rows:
        print(
            f'{row["id"]}  name={row["name"]}  revoked={row["revoked"]}  '
            f'created_at={row["created_at"]}  last_seen={row["last_seen"]}'
        )
    if not rows:
        print("（无设备）")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    conn = connect(_db_path())
    try:
        row = conn.execute("SELECT id FROM devices WHERE id = ?", (args.device_id,)).fetchone()
        if row is None:
            print(f"设备不存在：{args.device_id}", file=sys.stderr)
            return 1
        RelayRegistry().revoke(conn, args.device_id)
    finally:
        conn.close()
    print(f"已吊销：{args.device_id}")
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    conn = connect(_db_path())
    try:
        token = RelayRegistry().rotate(conn, args.device_id)
    finally:
        conn.close()
    if token is None:
        print(f"设备不存在：{args.device_id}", file=sys.stderr)
        return 1
    # 明文令牌仅此次输出；旧令牌即刻失效，旧连接被踢。
    print(f"device_id: {args.device_id}")
    print(f"token: {token}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay_server", description="极简中转服务器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="启动 HTTP/WS 服务（env 配置绑定与凭据）")
    p_serve.set_defaults(func=cmd_serve)

    p_create = sub.add_parser("create-device", help="创建设备，明文令牌仅输出一次")
    p_create.add_argument("--name", required=True, help="设备名")
    p_create.set_defaults(func=cmd_create_device)

    p_list = sub.add_parser("list-devices", help="列出设备")
    p_list.set_defaults(func=cmd_list_devices)

    p_revoke = sub.add_parser("revoke", help="吊销设备")
    p_revoke.add_argument("device_id")
    p_revoke.set_defaults(func=cmd_revoke)

    p_rotate = sub.add_parser("rotate", help="轮换设备令牌，明文仅输出一次")
    p_rotate.add_argument("device_id")
    p_rotate.set_defaults(func=cmd_rotate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
